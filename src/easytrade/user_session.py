"""User-operated console controller. Tests inject fakes; no startup trading."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from threading import Event
from uuid import uuid4

from .adapters import PaperAdapter
from .domain import Instrument, OrderPlan, Signal, TERMINAL, ValidationError, build_plan, timestamp, utc_now
from .engine import Executor
from .lease import session_lease
from .quotes import Quote, fetch_quote
from .store import Store, ConflictError

DISPLAY_PRICE_KEYS = {"current_price", "market_value"}


class UserSession:
    def __init__(self, root, config, *, live=False, adapter=None, quote_provider=None, log=lambda message: None, database=None):
        from pathlib import Path
        self.root, self.live, self.log = Path(root), live, log
        self.config = config
        self.account = config.get("ths_profile", {}).get("account_alias") or config.get("account") or next(iter(config["accounts"]))
        if self.account not in config.get("accounts", {}):
            raise ValidationError("会话账户必须列在accounts配置中")
        from .domain import decimal_value
        self.fee_reserve = decimal_value(config.get("buy_fee_reserve", "5"), "buy_fee_reserve", positive=False)
        if type(config.get("pretrade_account", True)) is not bool:
            raise ValidationError("pretrade_account必须是true或false")
        self.mode = "ths-user" if live else "paper"
        self.store = Store(database or self.root / ("runtime/ths-draft.sqlite" if live else "runtime/console-paper.sqlite"))
        from .domain import validate_symbol
        self.instruments = {validate_symbol(key): Instrument.parse(value) for key, value in config["instruments"].items()}
        if not self.instruments:
            raise ValidationError("至少配置一个证券及其交易规则")
        self.symbol = config.get("default_symbol", next(iter(self.instruments)))
        self.instrument = self.get_instrument(self.symbol)
        self.last_account_snapshot = None
        self.lock_path = self.root / (config.get("session_lock", "runtime/ths-account.lock") if live else "runtime/console-paper.lock")
        self._recovered = False
        if live:
            if adapter is None:
                from .user_native import ThsUserAdapter
                adapter = ThsUserAdapter({**config["ths_profile"], "console_diagnostics": str(self.root / "runtime/console-dialogs.jsonl")})
            self.adapter = adapter
            self.engine = None
        else:
            seed = {"cash": "1000", "holdings": {}, **config.get("accounts", {}).get(self.account, {})}
            paper_config = {"accounts": {self.account: seed},
                            "instruments": config["instruments"], "buy_fee_reserve": str(self.fee_reserve)}
            self.adapter = PaperAdapter(self.store, paper_config["accounts"])
            self.engine = Executor(self.store, self.adapter, paper_config)
        self.quote_source = config.get("quote_source", "ths_client")
        self.set_quote_source(self.quote_source)
        self.quote_provider = quote_provider

    def get_instrument(self, symbol):
        if symbol not in self.instruments:
            raise ValidationError("证券未配置；请先配置名称、交易单位及价格最小单位")
        return self.instruments[symbol]

    def execute(self, raw):
        """Model/HTTP entry: immutable explicit limit signal, same native path as UI."""
        signal = Signal.parse(raw)
        if signal.account != self.account:
            raise ValidationError("信号账户与当前会话不匹配")
        instrument = self.get_instrument(signal.symbol)
        plan = build_plan(signal, instrument, self.fee_reserve if signal.side == "buy" else Decimal("0"))
        with session_lease(self.lock_path):
            self._recover()
            return self._submit(signal.side, signal.limit_price, signal.signal_id, quantity=plan.quantity,
                                symbol=signal.symbol, explicit_signal=signal)

    def _recover(self):
        if not self._recovered:
            count = self.store.recover(self.mode)
            self._recovered = True
            if count:
                self.log(f"{count}笔中断订单已标记UNKNOWN；没有自动重发")

    def orders(self):
        with session_lease(self.lock_path):
            self._recover()
            return [order for order in self.store.list_orders() if order["signal"]["account"] == self.account]

    def quote(self, symbol=None):
        with session_lease(self.lock_path):
            return self._quote(symbol)

    def set_quote_source(self, source):
        if source not in {"ths_client", "tencent_web"}:
            raise ValidationError("未知行情来源")
        self.quote_source = source

    def _quote(self, symbol=None):
        symbol = symbol or self.symbol
        instrument = self.get_instrument(symbol)
        if self.quote_provider is not None:
            quote = self.quote_provider()
        elif not self.live:
            sample = self.config.get("paper_quotes", {}).get(symbol, {})
            bid = Decimal(sample.get("bid", str(instrument.price_tick * 1400)))
            ask = Decimal(sample.get("ask", str(bid + instrument.price_tick)))
            quote = Quote(symbol, bid, ask, utc_now(), source="paper")
        elif self.quote_source == "ths_client":
            quote = self.adapter.read_quote(symbol, instrument)
        else:
            quote = fetch_quote(symbol)
        if quote.symbol != symbol:
            raise ValidationError("行情标的与测试台不符")
        return quote

    def submit(self, side, *, symbol=None, quantity=100, manual_price=None, signal_id=None, stop=None):
        with session_lease(self.lock_path):
            self._recover()
            return self._submit(side, manual_price, signal_id or "console-" + uuid4().hex, stop, quantity, symbol=symbol)

    def submit_and_follow(self, side, *, symbol=None, quantity=100, manual_price=None, signal_id=None, stop=None, wait_seconds=30):
        """One user request, then read-only fill polling; no chase/cancel/resubmit."""
        import time
        stop = stop if stop is not None else Event()
        with session_lease(self.lock_path):
            self._recover()
            order = self._submit(side, manual_price, signal_id or "console-" + uuid4().hex, stop, quantity, symbol=symbol)
            deadline = time.monotonic() + wait_seconds
            while order["status"] in {"BROKER_ACCEPTED", "PARTIALLY_FILLED"} and not stop.is_set():
                if time.monotonic() >= deadline:
                    self.log("委托已受理，等待成交超时；保留委托状态，可继续查询或撤单，没有补发")
                    break
                try:
                    order = self._query(order["signal_id"])
                except Exception as error:
                    import traceback
                    from .diagnostics import record_action
                    record_action(self.root, "fill_query_error", signal_id=order["signal_id"],
                        broker_order_id=order["broker_order_id"], error=str(error), traceback=traceback.format_exc())
                    self.log(f"委托已受理，成交查询失败：{error}；没有重新下单")
                    break
                if order["status"] in {"BROKER_ACCEPTED", "PARTIALLY_FILLED"}:
                    stop.wait(1)
            return order

    def account_snapshot(self):
        with session_lease(self.lock_path):
            return self._account_snapshot()

    def _account_snapshot(self):
        import json
        if self.live:
            snapshot = self.adapter.read_account()
            if snapshot["account"] != self.account:
                raise ValidationError("账户快照别名与会话不匹配")
        else:
            with self.store.connect() as db:
                row = db.execute("SELECT cash,holdings FROM paper_accounts WHERE account=?", (self.account,)).fetchone()
            holdings = json.loads(row["holdings"])
            snapshot = {"account": self.account, "observed_at": utc_now().isoformat(), "source": "paper",
                "balances": {"available_cash": row["cash"], "cash_balance": row["cash"], "frozen_cash": "0"},
                "positions": [{"symbol": symbol, "name": self.config["instruments"].get(symbol, {}).get("name", symbol),
                    "position_quantity": quantity, "available_quantity": quantity} for symbol, quantity in holdings.items()],
                "positions_complete": True, "positions_scope": "paper_ledger"}
        path = self.root / ("runtime/console-account-live.json" if self.live else "runtime/console-account-paper.json")
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        self.last_account_snapshot = snapshot
        self.log(f"账户快照已更新：可用资金{snapshot['balances']['available_cash']}；可见持仓{len(snapshot['positions'])}行；{snapshot['observed_at']}")
        for warning in snapshot.get("positions_read_warnings", []):
            self.log("持仓显示提示：" + warning)
        return snapshot

    def check_client(self):
        """Read-only onboarding check; never proves OCR accuracy or sends orders."""
        import json
        checks, account, orders = [], None, None
        try:
            account = self.account_snapshot()
            checks.append({"item": "账户身份与资金", "status": "已读取", "detail": "请对照客户端账户及可用资金"})
            checks.append({"item": "持仓读取范围", "status": "已读取" if account["positions_complete"] else "需调整",
                           "detail": "滚动范围已核对" if account["positions_complete"] else account.get("positions_incomplete_reason") or "尚未取得完整范围"})
            rows = account["positions"]
            missing = [r["symbol"] for r in rows if not DISPLAY_PRICE_KEYS.issubset(r)]
            warnings = account.get("positions_read_warnings", [])
            checks.append({"item": "持仓市价与市值", "status": "需调整" if missing or warnings else ("待人工核对" if rows else "待有持仓时核对"),
                           "detail": "；".join(warnings) or ("部分行未取得市价/市值：" + ", ".join(missing) if missing else "读取成功不代表识别完全正确，请逐项对照小数点")})
        except Exception as error:
            checks.append({"item": "账户与持仓", "status": "读取失败", "detail": str(error)})
        try:
            orders = self.broker_orders_snapshot()
            checks.append({"item": "券商当日委托", "status": "已读取" if orders["complete"] else "需调整",
                           "detail": f"共{len(orders['rows'])}条；请核对完整编号、方向、数量、状态"})
        except Exception as error:
            checks.append({"item": "券商当日委托", "status": "读取失败", "detail": str(error)})
        checks.append({"item": "证券行情及人工验收", "status": "待人工核对",
                       "detail": "选择要使用的证券，单独刷新五档对照；账户、持仓、委托与报价一致后保持当前客户端大小、列宽、缩放"})
        report = {"observed_at": utc_now().isoformat(), "mode": self.mode, "submitted": False,
                  "manual_review_required": True, "checks": checks,
                  "layout": account.get("positions_layout", {}) if account else {},
                  "account_snapshot": account, "broker_snapshot": orders}
        path = self.root / ("runtime/client-check-live.json" if self.live else "runtime/client-check-paper.json")
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def _submit(self, side, manual_price, signal_id, stop=None, quantity=100, *, symbol=None, explicit_signal=None):
        symbol = symbol or self.symbol
        instrument = self.get_instrument(symbol)
        if side not in {"buy", "sell"}:
            raise ValidationError("side必须是buy或sell")
        if type(quantity) is not int or quantity <= 0 or quantity % instrument.lot_size:
            raise ValidationError(f"数量必须是{instrument.lot_size}的正整数倍")
        try:
            old = self.store.get(signal_id)
        except KeyError:
            old = None
        if old:
            if explicit_signal is not None:
                if old["mode"] != self.mode or Signal.parse(old["signal"]).digest() != explicit_signal.digest():
                    raise ConflictError("同一signal_id已用于不同信号或模式")
                return {**old, "replayed": True}
            if (old["mode"] != self.mode or old["signal"]["account"] != self.account
                    or old["signal"]["symbol"] != symbol or old["signal"]["side"] != side
                    or old["signal"].get("quantity") != quantity
                    or (manual_price is not None and Decimal(manual_price) != Decimal(old["signal"]["limit_price"]))):
                raise ValidationError("重复请求的模式或交易参数发生变化")
            self.log(f"复用已有请求 {signal_id}：{old['status']}，没有重新提交")
            return old
        if stop is not None and stop.is_set():
            raise ValidationError("用户已停止后续步骤；未发起新委托")
        if explicit_signal is not None and timestamp(explicit_signal.expires_at) <= utc_now():
            self.store.accept(explicit_signal, self.mode)
            self.store.claim(signal_id)
            return self.store.transition(signal_id, "EXPIRED", expected=("PREPARING",), detail={"reason": "信号已过期；未连接客户端"})
        # Each new user request is independent. Historical local receipt state
        # is not a balance/position check and must not block another request.
        # The account lease still serializes all native input; the adapter checks
        # the actual current window, account and order fields before submission.
        # Fresh account data is observable, not a local trading strategy or an
        # incomplete-OCR resource gate. The broker still validates resources.
        if self.live and self.config.get("pretrade_account", True):
            self._account_snapshot()
        quote = None if manual_price is not None else self._quote(symbol)
        price = str(manual_price) if quote is None else quote.price(side)
        signal = explicit_signal or Signal.parse({"signal_id": signal_id, "account": self.account, "symbol": symbol,
                               "side": side, "quantity": quantity, "limit_price": price,
                               "expires_at": (utc_now() + timedelta(seconds=30)).isoformat()})
        if not self.live:
            result = self.engine.execute(signal.payload())
            self._log_order(result)
            return result
        plan = build_plan(signal, instrument, self.fee_reserve if side == "buy" else Decimal("0"))
        self.store.accept(signal, self.mode)
        self.store.claim(signal_id)
        self.store.transition(signal_id, "PREPARING", expected=("PREPARING",), plan=plan.payload(), detail={"reason": "用户在本地测试台发起，计划已记录"})
        try:
            self.adapter.connect()
            if timestamp(signal.expires_at) <= utc_now():
                return self.store.transition(signal_id, "EXPIRED", expected=("PREPARING",), detail={"reason": "账户查询期间信号过期；未填写委托"})
            self.adapter.prepare(plan, instrument)
            if stop is not None and stop.is_set():
                raise ValidationError("用户停止；尚未点击提交")
            if timestamp(signal.expires_at) <= utc_now():
                raise ValidationError("准备期间请求过期")
            if quote is not None:
                # Refresh after potentially slow OCR/model initialization. Refill is never automatic.
                current = self._quote(symbol)
                fresh_price = Decimal(current.price(side))
                if (side == "buy" and fresh_price > Decimal(price)) or (side == "sell" and fresh_price < Decimal(price)):
                    raise ValidationError("盘口变动后原限价已不能对手成交；尚未提交，可重新点击取得新盘口")
            if (stop is not None and stop.is_set()) or timestamp(signal.expires_at) <= utc_now():
                raise ValidationError("已停止或请求已过期；尚未提交")
            self.store.transition(signal_id, "PREPARED", expected=("PREPARING",), detail={"reason": "字段回读通过"})
        except Exception as error:
            try:
                if self.adapter.clear_draft() is not True:
                    raise ValidationError("无法确认草稿清空")
                status = "PREPARATION_FAILED"
            except Exception:
                status = "UNKNOWN"
            result = self.store.transition(signal_id, status, expected=("PREPARING",), detail={"reason": str(error), "resubmit_allowed": status != "UNKNOWN"})
            self._log_order(result)
            return result
        self.store.transition(signal_id, "SUBMITTING", expected=("PREPARED",), detail={"reason": "即将调用一次原生提交；失败不补发"})
        try:
            self.adapter.submit_prepared(plan, instrument, lambda receipt: self.store.record_receipt(signal_id, receipt))
        except Exception as error:
            order = self.store.get(signal_id)
            if order["status"] not in TERMINAL:
                self.store.transition(signal_id, "UNKNOWN", expected=(order["status"],), detail={"reason": str(error), "resubmit_allowed": False})
        result = self.store.get(signal_id)
        self._log_order(result)
        return result

    def _log_order(self, order):
        detail = order.get("detail") or {}
        self.log(f"{order['signal_id']}：{order['status']}；委托号={order['broker_order_id'] or '未取得'}；累计成交={order['filled_quantity']}；{detail.get('reason', '')}")

    def query(self, signal_id, broker_id=None):
        with session_lease(self.lock_path):
            self._recover()
            return self._query(signal_id, broker_id)

    def refresh_receipts(self):
        """Refresh known pending orders; never prepare, submit, resume or cancel."""
        with session_lease(self.lock_path):
            self._recover()
            for order in self.store.list_orders():
                if order["signal"]["account"] != self.account or order["status"] in TERMINAL:
                    continue
                if self.live and order["mode"] not in {"ths-user", "ths-draft"}:
                    continue
                if self.live and not (order["broker_order_id"] or (order["detail"] or {}).get("broker_order_id_hint")):
                    self.log(f"{order['signal_id']}尚无委托号，保留原状态；可选中后补录完整编号查询")
                    continue
                try:
                    self._query(order["signal_id"])
                except Exception as error:
                    from .diagnostics import record_action
                    import traceback
                    record_action(self.root, "fill_query_error", signal_id=order["signal_id"],
                                  error=str(error), traceback=traceback.format_exc())
                    self.log(f"{order['signal_id']}回执刷新失败：{error}；保留原状态，不影响新的买卖指令")
            return [order for order in self.store.list_orders() if order["signal"]["account"] == self.account]

    def broker_orders_snapshot(self):
        """Display broker orders independently of the local request ledger."""
        import json
        with session_lease(self.lock_path):
            self._recover()
            if self.live:
                table = self.adapter.read_orders()
                snapshot = {"account": self.account, "observed_at": table.observed_at, "source": "ths_client_ocr",
                            "rows": table.rows, "complete": table.complete, "incomplete_reason": table.incomplete_reason,
                            "read_warnings": table.warnings, "layout": table.layout}
                # Bind only exact existing requests. Broker-only rows stay visible
                # without fabricating local signals or guessing by price/time.
                for order in self.store.list_orders():
                    if (order["signal"]["account"] != self.account or order["mode"] not in {"ths-user", "ths-draft"}
                            or order["status"] in TERMINAL or not order["plan"]):
                        continue
                    broker_id = order["broker_order_id"] or (order["detail"] or {}).get("broker_order_id_hint")
                    if not broker_id:
                        continue
                    try:
                        receipt = self.adapter.receipt_from_snapshot(order, broker_id, table)
                        self.store.record_receipt(order["signal_id"], receipt)
                    except Exception as error:
                        self.log(f"{order['signal_id']}本地回执尚未同步：{error}；券商委托表仍正常展示")
            else:
                rows = []
                for order in self.store.list_orders():
                    if order["signal"]["account"] != self.account or not order["plan"]:
                        continue
                    plan = order["plan"]
                    rows.append({"symbol": plan["symbol"], "side": plan["side"], "quantity": plan["quantity"],
                                 "limit_price": plan["limit_price"], "order_id": order["broker_order_id"] or "—",
                                 "status": order["status"], "filled_quantity": order["filled_quantity"],
                                 "average_price": order["average_price"]})
                snapshot = {"account": self.account, "observed_at": utc_now().isoformat(), "source": "paper",
                            "rows": rows, "complete": True, "incomplete_reason": None}
            path = self.root / ("runtime/console-orders-live.json" if self.live else "runtime/console-orders-paper.json")
            path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            self.log(f"委托快照已更新：{len(snapshot['rows'])}条；{snapshot['observed_at']}")
            return snapshot

    def resume_confirmation(self, signal_id):
        with session_lease(self.lock_path):
            self._recover()
            order = self.store.get(signal_id)
            if (not self.live or order["mode"] != "ths-user" or order["status"] != "UNKNOWN"
                    or order["broker_order_id"] or not order["plan"]
                    or order["signal"]["account"] != self.account or order["signal"]["symbol"] not in self.instruments
                    or order["plan"]["quantity"] <= 0):
                raise ValidationError("只可继续本测试台尚未取得委托号的UNKNOWN原确认；不会创建新单")
            plan = OrderPlan(**order["plan"])
            self.store.transition(signal_id, "UNKNOWN", expected=("UNKNOWN",),
                detail={"reason": "用户选择按原限价继续现有委托确认；未重新点击买卖提交按钮", "resubmit_allowed": False})
            try:
                def hint(broker_id):
                    self.store.transition(signal_id, "UNKNOWN", expected=("UNKNOWN",), detail={
                        "reason": "读取成功提示中的合同编号，尚待委托表逐字段核对",
                        "broker_order_id_hint": broker_id, "receipt_binding_verified": False,
                        "resubmit_allowed": False})
                self.adapter.resume_confirmation(plan, lambda receipt: self.store.record_receipt(signal_id, receipt),
                                                 created_at=order["created_at"], persist_hint=hint)
            except Exception as error:
                current = self.store.get(signal_id)
                if current["status"] not in TERMINAL:
                    self.store.transition(signal_id, "UNKNOWN", expected=(current["status"],),
                        detail={**(current["detail"] or {}), "reason": str(error), "resubmit_allowed": False})
            result = self.store.get(signal_id)
            self._log_order(result)
            return result

    def _query(self, signal_id, broker_id=None):
        order = self.store.get(signal_id)
        if order["signal"]["account"] != self.account:
            raise ValidationError("订单账户与当前会话不匹配")
        if self.live and order["mode"] not in {"ths-user", "ths-draft"}:
            raise ValidationError("不能查询其他模式的订单")
        if order["status"] in TERMINAL:
            self._log_order(order)
            return order
        if not self.live:
            return self.engine.reconcile_paper(signal_id)
        broker_id = broker_id or order["broker_order_id"] or (order["detail"] or {}).get("broker_order_id_hint")
        receipt = self.adapter.lookup_receipt(order, broker_id)
        result = self.store.record_receipt(signal_id, receipt)
        self._log_order(result)
        return result

    def cancel(self, signal_id):
        with session_lease(self.lock_path):
            self._recover()
            order = self.store.get(signal_id)
            if order["signal"]["account"] != self.account or order["signal"]["symbol"] not in self.instruments:
                raise ValidationError("撤单目标账户或标的不属于本测试台")
            if order["status"] in TERMINAL:
                raise ValidationError(f"订单已经是{order['status']}，无需发送撤单")
            if not self.live:
                raise ValidationError("当前模拟买卖立即成交，没有可撤数量")
            # A read immediately before cancellation rejects terminal/stale/ambiguous records.
            order = self._query(signal_id)
            if order["status"] not in {"BROKER_ACCEPTED", "PARTIALLY_FILLED"}:
                raise ValidationError("没有可核对的活动委托")
            def persist_intent():
                self.store.transition(signal_id, "UNKNOWN", expected=(order["status"],), detail={"reason": "撤单流程已启动；未核对结果前禁止重发"})
            self.adapter.cancel(order, persist_intent)
            return self._query(signal_id)

    def discard_unsubmitted(self, signal_id):
        with session_lease(self.lock_path):
            self._recover()
            order = self.store.get(signal_id)
            if order["signal"]["account"] != self.account:
                raise ValidationError("旧草稿账户与当前会话不匹配")
            if not self.live or order["mode"] != "ths-draft" or order["status"] != "READY_FOR_MANUAL_SUBMIT" or order["broker_order_id"]:
                raise ValidationError("仅可处理用户明确确认从未提交的旧草稿；UNKNOWN不得这样清除")
            self.adapter.connect()
            self.adapter._page(self.config["ths_profile"]["pages"][order["plan"]["side"]])
            values = [self.adapter._read_edit(i) for i in (1032, 1033, 1034)]
            if any(values) and (values[0] != order["plan"]["symbol"][:6] or values[2] != str(order["plan"]["quantity"])
                                or not values[1] or Decimal(values[1]) != Decimal(order["plan"]["limit_price"])):
                raise ValidationError("当前字段与旧草稿不同；保留现场")
            if self.adapter.clear_draft() is not True:
                raise ValidationError("未确认草稿清空")
            return self.store.transition(signal_id, "PREPARATION_FAILED", expected=("READY_FOR_MANUAL_SUBMIT",), detail={"reason": "用户明确确认从未提交，原生草稿已清空"})

    def resolve_unsubmitted(self, signal_id, *, user_declared_no_confirmation=False):
        if user_declared_no_confirmation is not True:
            raise ValidationError("只有用户明确声明这笔从未点击确认提交，才可解除UNKNOWN；不能根据空表自动推断")
        with session_lease(self.lock_path):
            self._recover()
            order = self.store.get(signal_id)
            if (not self.live or order["mode"] != "ths-user" or order["status"] != "UNKNOWN"
                    or order["broker_order_id"] or order["filled_quantity"] or not order["plan"]
                    or (order["detail"] or {}).get("broker_order_id_hint")
                    or order["signal"]["account"] != self.account or order["signal"]["symbol"] not in self.instruments):
                raise ValidationError("仅可核对未取得委托号的本测试台UNKNOWN记录；已受理订单必须查询回执")
            observation = self.adapter.verify_unsubmitted_resolution(OrderPlan(**order["plan"]))
            if observation.get("account_verified") is not True or observation.get("unblocked") is not True:
                raise ValidationError("客户端账户或无弹窗现场未核对成功，保留UNKNOWN")
            result = self.store.transition(signal_id, "PREPARATION_FAILED", expected=("UNKNOWN",), detail={
                "reason": "用户明确声明从未确认提交；账户及无弹窗现场已核对，本地解除记录",
                "resolution": "user_declared_not_submitted", "broker_absence_proven": False,
                "cancel_sent": False, "submitted": False})
            self._log_order(result)
            return result
