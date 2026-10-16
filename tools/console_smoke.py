"""Exercise the actual Tk worker and buttons in simulation, in a temporary folder."""
from __future__ import annotations

import json
import tempfile
import time
import tkinter as tk
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from easytrade.console import Console, ROOT
from easytrade.quotes import Quote
from easytrade.domain import utc_now


def main():
    with tempfile.TemporaryDirectory(prefix="easytrade-console-smoke-") as directory:
        project = Path(directory)
        (project / "config").mkdir()
        (project / "config/ths.example.json").write_text(
            (ROOT / "config/ths.example.json").read_text(encoding="utf-8"), encoding="utf-8")
        (project / "config/paper.example.json").write_text(
            (ROOT / "config/paper.example.json").read_text(encoding="utf-8"), encoding="utf-8")
        root = tk.Tk()
        root.withdraw()
        with patch("easytrade.user_native.ThsUserAdapter", side_effect=AssertionError("native adapter forbidden in smoke")):
            console = Console(root, project)
            def wait_done():
                deadline = time.monotonic() + 10
                while console.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.02)
                if console.busy:
                    raise AssertionError("GUI worker timed out")
            try:
                wait_done()
                assert not console.rows, "startup must not trade"
                assert console.mode.get().startswith("模拟")
                console.start("quote")
                wait_done()
                assert "模拟固定样本" in console.quote_text.get()
                assert "roundtrip" not in console.action_buttons
                assert len(console.symbol_box.cget("values")) >= 2
                console.start("buy")
                wait_done()
                assert len(console.rows) == 1, "a buy must never create an automatic sell"
                console.start("sell")
                wait_done()
                orders = list(console.rows.values())
                assert len(orders) == 2
                assert all(order["mode"] == "paper" and order["status"] == "FILLED" for order in orders)
                assert sorted(order["plan"]["side"] for order in orders) == ["buy", "sell"]
                assert all(order["filled_quantity"] == 100 for order in orders)
                console.start("account")
                wait_done()
                assert "99999" in console.account_text.get()
                assert "paper" in console.account_text.get()
                assert len(console.positions_table.get_children()) == 1
                assert console.tabs.select() == str(console.account_tab)
                # GUI quantity must reach the real worker and simulated ledger.
                console.quantity.set("300")
                console.start("buy")
                wait_done()
                assert any(o["plan"]["quantity"] == 300 and o["filled_quantity"] == 300 for o in console.rows.values())
                count = len(console.rows)
                console.quantity.set("101")
                console.start("sell")
                wait_done()
                assert len(console.rows) == count
                console.quantity.set("100")
                console.symbol.set("510300.SH")
                console.change_symbol()
                console.quantity.set("200")
                console.start("buy")
                wait_done()
                assert any(o['plan']['symbol'] == '510300.SH' and o['plan']['quantity'] == 200 for o in console.rows.values())
                # Closed-market snapshots must be visible, with no real session.
                (project / "config/ths.local.json").write_text(
                    (project / "config/ths.example.json").read_text(encoding="utf-8"), encoding="utf-8")
                closed = Quote("000001.SZ", Decimal("0"), Decimal("0"), utc_now(),
                               last=Decimal("1.365"), session="已收盘")
                with patch("easytrade.console.UserSession", return_value=SimpleNamespace(quote=lambda symbol: closed, orders=lambda: [], set_quote_source=lambda source: None)):
                    console.mode.set("实盘（测试中）")
                    console.start("quote")
                    wait_done()
                assert "已收盘" in console.quote_text.get()
                assert "最新成交1.365" in console.quote_text.get()
                console.mode.set("模拟（不连接客户端）")
                console.start("list")
                wait_done()
                console.start("refresh")
                wait_done()
                assert len(console.broker_table.get_children()) == len(console.rows)
                assert console.tabs.select() == str(console.broker_tab)
                assert "paper" in console.broker_text.get()
                # Exercise the visible recovery guidance with in-memory rows only.
                # Setting the StringVar does not dispatch a live action/session.
                pending = {**orders[0], "signal_id": "old-draft", "mode": "ths-draft",
                           "status": "READY_FOR_MANUAL_SUBMIT", "broker_order_id": None}
                console.rows["old-draft"] = pending
                console.table.insert("", "end", iid="old-draft")
                selected = console.table.selection()
                if selected:
                    console.table.selection_remove(*selected)
                console.mode.set("实盘（本机提交尚待验证）")
                console.update_pending_hint()
                assert "核对未提交" in console.pending_hint.get()
                assert console.table.selection() == ("old-draft",)
                assert all(str(button.cget("state")) == "normal" for button in console.trade_buttons)
                console.update_recovery_buttons()
                assert str(console.action_buttons["discard"].cget("state")) == "normal"
                assert str(console.action_buttons["cancel"].cget("state")) == "disabled"
                assert str(console.action_buttons["resume"].cget("state")) == "disabled"
                pending["mode"], pending["status"] = "ths-user", "UNKNOWN"
                console.update_pending_hint()
                assert all(str(button.cget("state")) == "normal" for button in console.trade_buttons)
                console.update_recovery_buttons()
                assert str(console.action_buttons["resume"].cget("state")) == "normal"
                assert str(console.action_buttons["query"].cget("state")) == "disabled"
                console.broker_id.set("12345")
                assert str(console.action_buttons["query"].cget("state")) == "normal"
                pending["status"], pending["broker_order_id"] = "BROKER_ACCEPTED", "12345"
                console.update_pending_hint()
                assert all(str(button.cget("state")) == "normal" for button in console.trade_buttons)
                console.set_busy(True)
                assert all(str(button.cget("state")) == "disabled" for button in console.trade_buttons)
                console.set_busy(False)
                assert all(str(button.cget("state")) == "normal" for button in console.trade_buttons)
                console.mode.set("模拟（不连接客户端）")
                console.update_pending_hint()
                assert all(str(button.cget("state")) == "normal" for button in console.trade_buttons)
                journal = [json.loads(line) for line in (project / "runtime/console-actions.jsonl").read_text(encoding="utf-8").splitlines()]
                assert any(entry["event"] == "action_started" and entry["action"] == "buy" for entry in journal)
                assert not any(entry.get("action") == "roundtrip" for entry in journal)
                assert any(entry["event"] == "action_finished" and "elapsed_ms" in entry for entry in journal)
                assert any(entry["event"] == "order_snapshot" and len(entry["orders"]) == 2 for entry in journal)
                print(json.dumps({"gui": "Tk 8.6", "startup_orders": 0, "independent_orders": len(orders),
                                  "all_filled": True, "native_adapter_constructed": False,
                                  "multiple_symbols": True, "no_automatic_roundtrip": True,
                                  "historical_orders_do_not_disable_new_requests": True,
                                  "quantity_and_account_tab": True,
                                  "recovery_button_states_and_action_journal": True,
                                  "database": "temporary and removed"}, ensure_ascii=False))
            finally:
                console.close()


if __name__ == "__main__":
    main()
