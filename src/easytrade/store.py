from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .domain import Signal, ValidationError, utc_now, validate_receipt


class ConflictError(ValidationError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS orders (
                    signal_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    signal TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
                    plan TEXT, detail TEXT, broker_order_id TEXT,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    average_price TEXT NOT NULL DEFAULT '0', observed_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT NOT NULL, body TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    signal_id TEXT NOT NULL, receipt_id TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(signal_id, receipt_id)
                );
                CREATE TABLE IF NOT EXISTS paper_orders (
                    signal_id TEXT PRIMARY KEY, result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    account TEXT PRIMARY KEY, cash TEXT NOT NULL, holdings TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def unpack(row) -> dict:
        if row is None:
            raise KeyError("订单不存在")
        result = dict(row)
        for name in ("signal", "plan", "detail"):
            result[name] = json.loads(result[name]) if result[name] else None
        return result

    @staticmethod
    def emit(db, order: dict):
        body = {"signal_id": order["signal_id"], "mode": order["mode"],
                "status": order["status"], "plan": order["plan"],
                "broker_order_id": order["broker_order_id"],
                "filled_quantity": order["filled_quantity"], "average_price": order["average_price"],
                "observed_at": order["observed_at"], "updated_at": order["updated_at"],
                "detail": order["detail"], "simulated": order["mode"] == "paper"}
        db.execute("INSERT INTO events(signal_id,body) VALUES (?,?)", (order["signal_id"], json.dumps(body, ensure_ascii=False)))

    def accept(self, signal: Signal, mode: str) -> tuple[dict, bool]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM orders WHERE signal_id=?", (signal.signal_id,)).fetchone()
            if row:
                if row["digest"] != signal.digest() or row["mode"] != mode:
                    raise ConflictError("同一signal_id已用于不同信号或不同执行模式")
                return self.unpack(row), False
            now = utc_now().isoformat()
            db.execute("INSERT INTO orders(signal_id,digest,signal,mode,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                       (signal.signal_id, signal.digest(), json.dumps(signal.payload()), mode, "LOCAL_ACCEPTED", now, now))
            order = self.unpack(db.execute("SELECT * FROM orders WHERE signal_id=?", (signal.signal_id,)).fetchone())
            self.emit(db, order)
            return order, True

    def get(self, signal_id: str) -> dict:
        with self.connect() as db:
            return self.unpack(db.execute("SELECT * FROM orders WHERE signal_id=?", (signal_id,)).fetchone())

    def pending_draft(self, account: str, *, exclude_signal_id: str = "") -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM orders WHERE mode<>'paper' AND status IN ('PREPARING','PREPARED','SUBMITTING','READY_FOR_MANUAL_SUBMIT','UNKNOWN') AND json_extract(signal,'$.account')=? AND signal_id<>?", (account, exclude_signal_id)).fetchone()
            return self.unpack(row) if row else None

    def list_orders(self, limit: int = 100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit必须为1至1000")
        with self.connect() as db:
            return [self.unpack(row) for row in db.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,))]

    def claim(self, signal_id: str) -> bool:
        with self.connect() as db:
            changed = db.execute("UPDATE orders SET status='PREPARING',updated_at=? WHERE signal_id=? AND status='LOCAL_ACCEPTED'",
                                 (utc_now().isoformat(), signal_id)).rowcount
            if changed:
                self.emit(db, self.unpack(db.execute("SELECT * FROM orders WHERE signal_id=?", (signal_id,)).fetchone()))
            return bool(changed)

    def transition(self, signal_id: str, status: str, *, plan=None, detail=None, expected: tuple[str, ...]) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order = self.unpack(db.execute("SELECT * FROM orders WHERE signal_id=?", (signal_id,)).fetchone())
            if order["status"] not in expected:
                raise ConflictError(f"状态已变化: {order['status']}")
            plan = plan if plan is not None else order["plan"]
            db.execute("UPDATE orders SET status=?,plan=?,detail=?,updated_at=? WHERE signal_id=?",
                       (status, json.dumps(plan) if plan is not None else None, json.dumps(detail), utc_now().isoformat(), signal_id))
            result = self.unpack(db.execute("SELECT * FROM orders WHERE signal_id=?", (signal_id,)).fetchone())
            self.emit(db, result)
            return result

    def record_receipt(self, signal_id: str, data: dict) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order = self.unpack(db.execute("SELECT * FROM orders WHERE signal_id=?", (signal_id,)).fetchone())
            data = validate_receipt(order, data)
            body = json.dumps(data, sort_keys=True)
            old = db.execute("SELECT body FROM receipts WHERE signal_id=? AND receipt_id=?", (signal_id, data["receipt_id"])).fetchone()
            if old:
                if old["body"] != body:
                    raise ConflictError("同一receipt_id内容发生变化")
                return order
            if order["broker_order_id"] and order["broker_order_id"] != data["broker_order_id"]:
                raise ConflictError("委托编号与已有回执不匹配")
            if order["status"] not in {"SUBMITTING", "UNKNOWN", "READY_FOR_MANUAL_SUBMIT", "BROKER_ACCEPTED", "PARTIALLY_FILLED"}:
                raise ConflictError(f"当前状态不接受新的回执: {order['status']}")
            if data["filled_quantity"] < order["filled_quantity"]:
                raise ConflictError("累计成交数量回退，拒绝旧回执")
            if order["observed_at"] and data["observed_at"] < order["observed_at"]:
                raise ConflictError("拒绝时间早于上一条的回执")
            db.execute("INSERT INTO receipts VALUES (?,?,?)", (signal_id, data["receipt_id"], body))
            db.execute("UPDATE orders SET status=?,broker_order_id=?,filled_quantity=?,average_price=?,observed_at=?,detail=?,updated_at=? WHERE signal_id=?",
                       (data["status"], data["broker_order_id"], data["filled_quantity"], data["average_price"], data["observed_at"],
                        json.dumps({"source": data["source"], "receipt_id": data["receipt_id"]}), utc_now().isoformat(), signal_id))
            result = self.unpack(db.execute("SELECT * FROM orders WHERE signal_id=?", (signal_id,)).fetchone())
            self.emit(db, result)
            return result

    def recover(self, mode: str) -> int:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT signal_id FROM orders WHERE mode=? AND status IN ('PREPARING','PREPARED','SUBMITTING')", (mode,)).fetchall()
            for row in rows:
                db.execute("UPDATE orders SET status='UNKNOWN',detail=?,updated_at=? WHERE signal_id=?",
                           (json.dumps({"reason": "进程中断；必须核对，禁止自动重新提交"}), utc_now().isoformat(), row[0]))
                self.emit(db, self.unpack(db.execute("SELECT * FROM orders WHERE signal_id=?", (row[0],)).fetchone()))
            return len(rows)

    def pending_events(self, after: int = 0, limit: int = 100) -> list[dict]:
        if after < 0 or not 1 <= limit <= 1000:
            raise ValidationError("事件游标或limit不合法")
        with self.connect() as db:
            return [{"event_id": row["event_id"], **json.loads(row["body"])} for row in db.execute(
                "SELECT * FROM events WHERE acknowledged=0 AND event_id>? ORDER BY event_id LIMIT ?", (after, limit))]

    def acknowledge(self, event_ids: list[int]):
        if not isinstance(event_ids, list) or len(event_ids) > 1000 or any(type(x) is not int or x <= 0 for x in event_ids):
            raise ValidationError("event_ids必须是最多1000个正整数")
        with self.connect() as db:
            db.executemany("UPDATE events SET acknowledged=1 WHERE event_id=?", [(x,) for x in event_ids])
