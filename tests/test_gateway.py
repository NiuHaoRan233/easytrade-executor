"""Shared model API and console execution path, fake native client only."""
import copy
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from easytrade.client import Client
from easytrade.domain import utc_now, ValidationError
from easytrade.service import create_server
from easytrade.store import ConflictError
from easytrade.user_session import UserSession
from test_user_console import CONFIG, FakeNative


def configuration():
    config = copy.deepcopy(CONFIG)
    config["default_symbol"] = "000001.SZ"
    config["instruments"] = {
        "000001.SZ": {"name": "平安银行", "lot_size": 100, "price_tick": "0.01"},
        "510300.SH": {"name": "沪深300", "lot_size": 200, "price_tick": "0.001"}}
    return config


def message(key="model-one", symbol="000001.SZ", side="buy", quantity=100, price="10.01"):
    return {"signal_id": key, "account": "test", "symbol": symbol, "side": side,
            "quantity": quantity, "limit_price": price, "expires_at": (utc_now() + timedelta(minutes=2)).isoformat()}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.native = FakeNative()
        self.session = UserSession(self.tmp.name, configuration(), live=True, adapter=self.native)

    def tearDown(self):
        self.tmp.cleanup()

    def test_multiple_symbols_bind_exact_name_tick_quantity_and_replay(self):
        raw = message()
        self.assertEqual(self.session.execute(raw)["status"], "BROKER_ACCEPTED")
        self.assertTrue(self.session.execute(raw)["replayed"])
        self.session.execute(message("etf-sell", "510300.SH", "sell", 400, "3.500"))
        self.assertEqual([(p.symbol, p.side, p.quantity, p.limit_price) for p in self.native.submitted],
            [("000001.SZ", "buy", 100, "10.01"), ("510300.SH", "sell", 400, "3.5")])
        self.assertIsNotNone(self.session.last_account_snapshot)
        with self.assertRaises(ConflictError):
            self.session.execute({**raw, "quantity": 200})
        self.assertEqual(len(self.native.submitted), 2)

    def test_invalid_input_account_and_expiry_never_submit(self):
        for raw in [message(symbol="not-a-code"), message(quantity=0), message(price="0"),
                    message(quantity=True), {**message(), "account": "other"}]:
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                self.session.execute(raw)
        raw = {**message(), "expires_at": (utc_now() - timedelta(seconds=1)).isoformat()}
        self.native.read_account = lambda: self.fail("expired signal must not query client")
        self.assertEqual(self.session.execute(raw)["status"], "EXPIRED")
        self.assertFalse(self.native.submitted)

    def test_model_code_quantity_and_price_need_no_security_preconfiguration(self):
        config = configuration()
        config["instruments"] = {}
        config.pop("default_symbol", None)
        config.pop("pretrade_account", None)
        self.native.read_account = lambda: self.fail("explicit execution must not add account OCR")
        session = UserSession(self.tmp.name, config, live=True, adapter=self.native)
        for index, (code, quantity, price) in enumerate((("000967", 101, "6.931"), ("300672.SZ", 7, "137.02"))):
            raw = message("unconfigured-" + str(index), code, quantity=quantity, price=price)
            self.assertEqual(session.execute(raw)["status"], "BROKER_ACCEPTED")
            self.assertTrue(session.execute(raw)["replayed"])
        self.assertEqual([(p.symbol, p.quantity, p.limit_price) for p in self.native.submitted],
                         [("000967", 101, "6.931"), ("300672.SZ", 7, "137.02")])

    def test_pretrade_account_is_fresh_before_each_new_order_but_not_replay(self):
        calls = []
        original = self.native.read_account
        def read():
            calls.append("account")
            return original()
        self.native.read_account = read
        self.native.on_prepare = lambda: self.assertEqual(calls[-1], "account")
        raw = message()
        self.session.execute(raw)
        self.session.execute(raw)
        self.session.execute(message("two", side="sell"))
        self.assertEqual(calls, ["account", "account"])

    def test_failed_pretrade_or_wrong_account_cannot_reach_submit(self):
        original = self.native.read_account
        self.native.read_account = lambda: {**original(), "account": "wrong"}
        with self.assertRaises(ValidationError):
            self.session.execute(message())
        self.assertFalse(self.native.prepared)
        self.assertFalse(self.native.submitted)

    def test_client_check_reports_layout_and_warnings_without_any_trading(self):
        from easytrade.tables import TableSnapshot
        original = self.native.read_account
        def account():
            value = original()
            value["positions_read_warnings"] = ["市价列被截断"]
            value["positions_layout"] = {"window_size": [1200, 800], "dpi": 144}
            return value
        self.native.read_account = account
        self.native.read_orders = lambda: TableSnapshot("orders", [], "now", "ocr", complete=True)
        report = self.session.check_client()
        self.assertFalse(report["submitted"])
        self.assertTrue(report["manual_review_required"])
        self.assertEqual(report["layout"]["dpi"], 144)
        self.assertEqual(next(row for row in report["checks"] if row["item"] == "持仓市价与市值")["status"], "需调整")
        self.assertTrue((Path(self.tmp.name) / "runtime/client-check-live.json").is_file())
        self.assertFalse(self.native.prepared or self.native.submitted or self.native.cancelled or self.native.resumed)

    def test_client_check_keeps_errors_and_never_claims_an_empty_account_passed(self):
        self.native.read_account = lambda: (_ for _ in ()).throw(ValidationError("missing table"))
        self.native.read_orders = lambda: (_ for _ in ()).throw(ValidationError("identity mismatch"))
        report = self.session.check_client()
        self.assertIsNone(report["account_snapshot"])
        self.assertEqual([r["status"] for r in report["checks"][:2]], ["读取失败", "读取失败"])
        self.assertTrue(report["manual_review_required"])
        self.assertFalse(self.native.submitted)

    def test_signal_expiring_during_account_read_does_not_fill_fields(self):
        raw = message()
        future = utc_now() + timedelta(minutes=3)
        original = self.native.read_account
        def read():
            self.clock = unittest.mock.patch("easytrade.user_session.utc_now", return_value=future)
            self.clock.start()
            return original()
        self.native.read_account = read
        try:
            self.assertEqual(self.session.execute(raw)["status"], "EXPIRED")
        finally:
            self.clock.stop()
        self.assertFalse(self.native.prepared)
        self.assertFalse(self.native.submitted)

    def test_generic_paper_configuration_never_constructs_native(self):
        config = configuration()
        del config["ths_profile"]
        config["account"] = "test"
        config["paper_quotes"] = {"510300.SH": {"bid":"3.499", "ask":"3.500"}}
        with unittest.mock.patch("easytrade.user_native.ThsUserAdapter", side_effect=AssertionError("native forbidden")):
            paper = UserSession(self.tmp.name, config)
            self.assertEqual(paper.execute(message(price="1.00"))["status"], "FILLED")
            self.assertEqual(paper.account_snapshot()["positions"][0]["symbol"], "000001.SZ")
            self.assertEqual(paper.quote("510300.SH").symbol, "510300.SH")

    def test_budget_and_paper_fee_use_the_same_reserve(self):
        config = configuration()
        paper = UserSession(self.tmp.name, config)
        raw = message(price="1.00")
        del raw["quantity"]
        raw["budget"] = "205.00"
        result = paper.execute(raw)
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["plan"]["quantity"], 200)
        self.assertEqual(Decimal(paper.account_snapshot()["balances"]["available_cash"]), Decimal("795"))

    def test_unknown_http_replay_never_resubmits_but_new_intent_can_submit(self):
        self.native.timeout = True
        raw = message()
        self.assertEqual(self.session.execute(raw)["status"], "UNKNOWN")
        self.assertTrue(self.session.execute(raw)["replayed"])
        self.assertEqual(len(self.native.submitted), 1)
        self.native.timeout = False
        self.assertEqual(self.session.execute(message("another", side="sell"))["status"], "BROKER_ACCEPTED")
        self.assertEqual(len(self.native.submitted), 2)

    def test_web_quote_uses_configured_market_and_rejects_wrong_response(self):
        from easytrade.quotes import fetch_quote
        from unittest.mock import MagicMock
        fields = ["0"] * 31
        fields[2], fields[3], fields[9], fields[19], fields[30] = "510300", "3.5", "3.499", "3.500", "20261016140000"
        response = MagicMock()
        response.__enter__.return_value.read.return_value = ('v_sh510300="' + "~".join(fields) + '";').encode("gbk")
        with patch("easytrade.quotes.urlopen", return_value=response) as opened:
            self.assertEqual(fetch_quote("510300.SH").symbol, "510300.SH")
            self.assertEqual(opened.call_args.args[0].full_url, "https://qt.gtimg.cn/q=sh510300")
            with self.assertRaises(ValidationError):
                fetch_quote("000001.SZ")

    def test_model_example_outbox_survives_concurrent_retries_and_windows_names(self):
        import importlib.util
        from concurrent.futures import ThreadPoolExecutor
        spec = importlib.util.spec_from_file_location("model_example", Path(__file__).resolve().parents[1] / "examples/model_client.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        desired = message("CON:a")
        desired.pop("expires_at")
        outbox = Path(self.tmp.name) / "outbox"
        with ThreadPoolExecutor(max_workers=4) as pool:
            payloads = list(pool.map(lambda _: module.persist_signal(outbox, desired), range(8)))
        self.assertTrue(all(payload == payloads[0] for payload in payloads))
        self.assertEqual(len(list(outbox.iterdir())), 1)
        with self.assertRaises(ValueError):
            module.persist_signal(outbox, {**desired, "quantity": 200})

    def test_http_model_receipt_and_account_routes_share_session(self):
        token = "test-token-never-a-real-credential-1234"
        server = create_server(self.session, token, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = Client(token, f"http://127.0.0.1:{server.server_port}")
        try:
            self.assertTrue(client.request("/health")["real_submit_supported"])
            self.assertEqual(set(client.instruments()["instruments"]), set(configuration()["instruments"]))
            raw = message()
            self.assertEqual(client.signal(raw)["status"], "BROKER_ACCEPTED")
            self.assertTrue(client.signal(raw)["replayed"])
            self.assertEqual(client.query(raw["signal_id"])["status"], "FILLED")
            self.assertEqual(client.account()["account"], "test")
            with self.assertRaisesRegex(RuntimeError, "409"):
                client.signal({**raw, "quantity": 200})
            with self.assertRaisesRegex(RuntimeError, "401"):
                Client("wrong", client.base_url).account()
            self.assertEqual(len(self.native.submitted), 1)
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()
