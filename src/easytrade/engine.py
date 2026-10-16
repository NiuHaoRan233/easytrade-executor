from __future__ import annotations

import threading
from decimal import Decimal

from .adapters import Adapter, DraftCleanupError, PaperAdapter
from .domain import Instrument, Signal, ValidationError, build_plan, timestamp, utc_now
from .store import Store


class Executor:
    def __init__(self, store: Store, adapter: Adapter, config: dict):
        self.store, self.adapter, self.config = store, adapter, config
        self.gui_lock = threading.Lock()
        self.instruments = {key: Instrument.parse(value) for key, value in config["instruments"].items()}
        self.fee_reserve = Decimal(config.get("buy_fee_reserve", "5"))
        if not self.fee_reserve.is_finite() or self.fee_reserve < 0:
            raise ValidationError("buy_fee_reserve不合法")

    def execute(self, raw: dict, *, scenario: str = "fill") -> dict:
        signal = Signal.parse(raw)
        if scenario not in {"fill", "partial", "reject", "timeout_after_submit"}:
            raise ValidationError("未知模拟场景")
        if signal.account not in self.config["accounts"]:
            raise ValidationError("账户别名未配置")
        if signal.symbol not in self.instruments:
            raise ValidationError("品种规则未配置，不能猜测交易单位或价格精度")
        if scenario != "fill" and self.adapter.mode != "paper":
            raise ValidationError("真实客户端模式不接受模拟场景参数")
        order, created = self.store.accept(signal, self.adapter.mode)
        if not created:
            return {**order, "replayed": True}
        with self.gui_lock:
            if not self.store.claim(signal.signal_id):
                return self.store.get(signal.signal_id)
            if timestamp(signal.expires_at) <= utc_now():
                return self.store.transition(signal.signal_id, "EXPIRED", expected=("PREPARING",), detail={"reason": "信号已过期"})
            try:
                if self.adapter.mode == "ths-draft" and self.store.pending_draft(signal.account, exclude_signal_id=signal.signal_id):
                    raise ValidationError("已有未解决的草稿或UNKNOWN订单；先核对，禁止覆盖旧现场")
                plan = build_plan(signal, self.instruments[signal.symbol], self.fee_reserve if signal.side == "buy" else Decimal("0"))
                self.store.transition(signal.signal_id, "PREPARING", expected=("PREPARING",),
                                      plan=plan.payload(), detail={"reason": "计划已持久化，尚未填写客户端"})
                preparation = self.adapter.prepare(plan, self.instruments[signal.symbol])
                if timestamp(signal.expires_at) <= utc_now():
                    if hasattr(self.adapter, "clear_draft"):
                        self.adapter.clear_draft()
                    return self.store.transition(signal.signal_id, "EXPIRED", expected=("PREPARING",), plan=plan.payload(), detail={"reason": "填写核对期间信号过期；未提交"})
                self.store.transition(signal.signal_id, "PREPARED", expected=("PREPARING",), plan=plan.payload(), detail=preparation)
            except Exception as error:
                status = "UNKNOWN" if isinstance(error, DraftCleanupError) else "PREPARATION_FAILED"
                return self.store.transition(signal.signal_id, status, expected=("PREPARING",), detail={"reason": str(error)})
            if self.adapter.mode != "paper":
                return self.store.transition(signal.signal_id, "READY_FOR_MANUAL_SUBMIT", expected=("PREPARED",), detail={"reason": "字段已核对；没有提交买卖委托", "preparation": preparation})
            self.store.transition(signal.signal_id, "SUBMITTING", expected=("PREPARED",), detail={"simulated": True})
            try:
                assert isinstance(self.adapter, PaperAdapter)
                receipts = self.adapter.execute(plan, scenario)
                for receipt in receipts:
                    self.store.record_receipt(signal.signal_id, receipt)
            except Exception as error:
                current = self.store.get(signal.signal_id)
                if current["status"] not in {"FILLED", "CANCELLED", "REJECTED"}:
                    self.store.transition(signal.signal_id, "UNKNOWN", expected=(current["status"],), detail={"reason": str(error), "resubmit_allowed": False})
            return self.store.get(signal.signal_id)

    def reconcile_paper(self, signal_id: str) -> dict:
        if not isinstance(self.adapter, PaperAdapter):
            raise ValidationError("该命令只用于核对模拟柜台记录")
        order = self.store.get(signal_id)
        if order["mode"] != "paper":
            raise ValidationError("不是模拟订单")
        for receipt in self.adapter.lookup(signal_id):
            self.store.record_receipt(signal_id, receipt)
        return self.store.get(signal_id)

    def discard_draft(self, signal_id: str) -> dict:
        with self.gui_lock:
            order = self.store.get(signal_id)
            if self.adapter.mode != "ths-draft" or order["mode"] != "ths-draft" or order["status"] != "READY_FOR_MANUAL_SUBMIT":
                raise ValidationError("只可作废确认尚未提交的客户端草稿")
            self.adapter.connect()
            if self.adapter.clear_draft() is False:
                raise ValidationError("不在交易草稿页，无法确认清空；保留原草稿状态")
            return self.store.transition(signal_id, "PREPARATION_FAILED", expected=("READY_FOR_MANUAL_SUBMIT",), detail={"reason": "用户确认未提交，已作废本地草稿"})
