"""Exercise model-style explicit instructions through the real Tk worker, offline."""
import json
import tempfile
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

from easytrade.console import Console


def main():
    with tempfile.TemporaryDirectory(prefix="easytrade-console-smoke-") as directory:
        project = Path(directory)
        (project / "config").mkdir()
        config = {"account": "demo", "accounts": {"demo": {"cash": "100000", "holdings": {}}},
                  "instruments": {}, "buy_fee_reserve": "0"}
        (project / "config/paper.example.json").write_text(json.dumps(config), encoding="utf-8")
        root = tk.Tk()
        root.withdraw()
        with patch("easytrade.user_native.ThsUserAdapter", side_effect=AssertionError("native adapter forbidden in smoke")):
            console = Console(root, project)
            def wait_done():
                deadline = time.monotonic() + 10
                while console.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.02)
                assert not console.busy, "GUI worker timed out"
                root.update()
            try:
                wait_done()
                assert not console.rows
                assert console.symbol.get() == ""
                assert tuple(console.symbol_box.cget("values")) == ()
                assert console.manual_price.get() == ""
                assert console.quantity.get() == ""
                assert not hasattr(console, "configure_symbol_button")
                console.symbol.set("159570")
                console.quantity.set("100")
                console.manual_price.set("1.365")
                console.start("buy")
                wait_done()
                assert len(console.rows) == 1
                first = next(iter(console.rows.values()))
                assert first["plan"]["symbol"] == "159570"
                assert first["plan"]["quantity"] == 100
                assert first["status"] == "FILLED"
                console.manual_price.set("1.371")
                console.start("sell")
                wait_done()
                assert len(console.rows) == 2
                assert all(row["status"] == "FILLED" for row in console.rows.values())
                console.symbol.set("000967.SZ")
                console.quantity.set("7")
                console.manual_price.set("6.931")
                console.start("buy")
                wait_done()
                assert any(row["plan"]["symbol"] == "000967.SZ" and row["plan"]["quantity"] == 7
                           and row["plan"]["limit_price"] == "6.931" for row in console.rows.values())
                console.start("account")
                wait_done()
                assert len(console.positions_table.get_children()) == 2
                assert console.tabs.select() == str(console.account_tab)
                console.positions_table.selection_set(console.positions_table.get_children()[-1])
                console.select_position()
                assert console.symbol.get() == "000967.SZ"
                assert "000967.SZ" in console.symbol_box.cget("values")
                before = len(console.rows)
                console.quantity.set("0")
                console.start("sell")
                assert not console.busy and len(console.rows) == before
                console.symbol.set("invalid")
                console.start("buy")
                assert not console.busy and len(console.rows) == before
                console.start("refresh")
                wait_done()
                assert len(console.broker_table.get_children()) == 3
                assert not (project / "config/paper.local.json").exists()
                assert json.loads((project / "config/paper.example.json").read_text())["instruments"] == {}
                print(json.dumps({"gui":"Tk", "startup_orders":0, "preconfigured_symbols":0,
                                  "explicit_buy_sell":True, "arbitrary_code_quantity_price":True,
                                  "account_and_orders":True, "native_adapter_constructed":False}))
            finally:
                console.close()


if __name__ == "__main__":
    main()
