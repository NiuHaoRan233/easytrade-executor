from __future__ import annotations

import json
import hashlib
from decimal import Decimal
from typing import Protocol

from .domain import Instrument, OrderPlan, ValidationError, utc_now
from .store import Store


class PreparationError(RuntimeError):
    pass


class ForegroundChangedError(PreparationError):
    """The verified client lost foreground ownership during an operation."""


class BrokerRejectedError(PreparationError):
    """A specifically recognized rejection bound to the current submission."""

    def __init__(self, message, *, code, source="win32"):
        super().__init__(message)
        self.code, self.source = code, source


class DraftCleanupError(PreparationError):
    """Draft state cannot be verified after preparation fails."""


class Adapter(Protocol):
    mode: str

    def prepare(self, plan: OrderPlan, instrument: Instrument) -> dict: ...


class PaperAdapter:
    mode = "paper"

    def __init__(self, store: Store, accounts: dict):
        self.store = store
        with store.connect() as db:
            for name, data in accounts.items():
                cash = str(Decimal(data["cash"]))
                db.execute("INSERT OR IGNORE INTO paper_accounts VALUES (?,?,?)", (name, cash, json.dumps(data.get("holdings", {}))))

    def prepare(self, plan: OrderPlan, instrument: Instrument) -> dict:
        return {"simulated": True, "verified": {"symbol": plan.symbol, "side": plan.side,
                                                "quantity": plan.quantity, "limit_price": plan.limit_price}}

    def execute(self, plan: OrderPlan, scenario: str = "fill") -> list[dict]:
        if scenario not in {"fill", "partial", "reject", "timeout_after_submit"}:
            raise ValidationError("未知模拟场景")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT result FROM paper_orders WHERE signal_id=?", (plan.signal_id,)).fetchone()
            if old:
                return json.loads(old[0])
            row = db.execute("SELECT * FROM paper_accounts WHERE account=?", (plan.account,)).fetchone()
            if row is None:
                raise PreparationError("没有配置模拟账户")
            cash, holdings = Decimal(row["cash"]), json.loads(row["holdings"])
            available = int(holdings.get(plan.symbol, 0))
            required_cash = Decimal(plan.notional) + Decimal(plan.fee_reserve)
            rejection = scenario == "reject" or (plan.side == "buy" and cash < required_cash) or (plan.side == "sell" and available < plan.quantity)
            now = utc_now().isoformat()
            order_id = "PAPER-" + hashlib.sha256(plan.signal_id.encode()).hexdigest()[:20]
            base = {"account": plan.account, "symbol": plan.symbol, "side": plan.side,
                    "broker_order_id": order_id, "observed_at": now, "source": "paper"}
            if rejection:
                receipts = [{**base, "receipt_id": order_id + ":reject", "status": "REJECTED", "filled_quantity": 0, "average_price": "0"}]
            else:
                filled = max(1, plan.quantity // 2) if scenario == "partial" else plan.quantity
                value = Decimal(plan.limit_price) * filled
                if plan.side == "buy":
                    cash -= value + Decimal(plan.fee_reserve)
                    holdings[plan.symbol] = available + filled
                else:
                    cash += value
                    holdings[plan.symbol] = available - filled
                db.execute("UPDATE paper_accounts SET cash=?,holdings=? WHERE account=?", (str(cash), json.dumps(holdings), plan.account))
                receipts = [
                    {**base, "receipt_id": order_id + ":accepted", "status": "BROKER_ACCEPTED", "filled_quantity": 0, "average_price": "0"},
                    {**base, "receipt_id": order_id + ":fill", "status": "PARTIALLY_FILLED" if filled < plan.quantity else "FILLED",
                     "filled_quantity": filled, "average_price": plan.limit_price},
                ]
            db.execute("INSERT INTO paper_orders VALUES (?,?)", (plan.signal_id, json.dumps(receipts)))
        if scenario == "timeout_after_submit":
            raise TimeoutError("模拟已提交，但调用方超时")
        return receipts

    def lookup(self, signal_id: str) -> list[dict]:
        with self.store.connect() as db:
            row = db.execute("SELECT result FROM paper_orders WHERE signal_id=?", (signal_id,)).fetchone()
            return json.loads(row[0]) if row else []
