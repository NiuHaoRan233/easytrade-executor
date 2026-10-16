import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from easytrade.adapters import DraftCleanupError, PaperAdapter
from easytrade.domain import Instrument, Signal, ValidationError, build_plan, utc_now
from easytrade.engine import Executor
from easytrade.lease import session_lease
from easytrade.service import create_server
from easytrade.store import ConflictError, Store
from easytrade.tables import normalize, parse_tsv


CONFIG = {
    "accounts": {"guotai": {"cash": "100000", "holdings": {"000967.SZ": 1400}}},
    "instruments": {"000967.SZ": {"name": "盈峰环境", "lot_size": 100, "price_tick": "0.01"}},
    "buy_fee_reserve": "5",
}


def signal(signal_id="s1", side="buy", **updates):
    value = {"signal_id": signal_id, "account": "guotai", "symbol": "000967.SZ", "side": side,
             "limit_price": "7.42", "expires_at": (utc_now() + timedelta(minutes=10)).isoformat(), "quantity": 100}
    value.update(updates)
    return value


class DraftSpy:
    mode = "ths-draft"

    def __init__(self):
        self.prepared = 0
        self.cleared = 0

    def prepare(self, plan, instrument):
        self.prepared += 1
        return {"submitted": False}

    def connect(self):
        pass

    def clear_draft(self):
        self.cleared += 1


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.sqlite")
        self.adapter = PaperAdapter(self.store, CONFIG["accounts"])
        self.engine = Executor(self.store, self.adapter, copy.deepcopy(CONFIG))

    def tearDown(self):
        self.tmp.cleanup()

    def receipt(self, order, status, filled, price="7.42", receipt_id="r1", **updates):
        data = {"receipt_id": receipt_id, "account": "guotai", "symbol": "000967.SZ", "side": "buy",
                "broker_order_id": order["broker_order_id"] or "B123", "status": status,
                "filled_quantity": filled, "average_price": price if filled else "0",
                "observed_at": utc_now().isoformat(), "source": "paper"}
        data.update(updates)
        return data

    def test_budget_never_exceeds_budget_with_fee_reserve(self):
        raw = signal()
        del raw["quantity"]
        raw["budget"] = "10000"
        order = self.engine.execute(raw)
        self.assertEqual(order["plan"]["quantity"], 1300)
        self.assertLessEqual(Decimal(order["plan"]["notional"]) + Decimal(order["plan"]["fee_reserve"]), Decimal("10000"))
        self.assertEqual(order["status"], "FILLED")

    def test_buy_and_sell_update_only_paper_ledger(self):
        self.engine.execute(signal("buy"))
        self.engine.execute(signal("sell", "sell"))
        with self.store.connect() as db:
            holdings = json.loads(db.execute("SELECT holdings FROM paper_accounts").fetchone()[0])
        self.assertEqual(holdings["000967.SZ"], 1400)

    def test_repeated_signal_cannot_execute_twice(self):
        raw = signal()
        one = self.engine.execute(raw)
        two = self.engine.execute(raw)
        self.assertTrue(two["replayed"])
        self.assertEqual(one["broker_order_id"], two["broker_order_id"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM paper_orders").fetchone()[0], 1)

    def test_concurrent_duplicate_delivery_creates_one_trade(self):
        raw = signal()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.engine.execute(raw), range(40)))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM orders").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM paper_orders").fetchone()[0], 1)

    def test_changed_payload_same_id_rejected(self):
        raw = signal()
        self.engine.execute(raw)
        with self.assertRaises(ConflictError):
            self.engine.execute({**raw, "quantity": 200})

    def test_price_representation_canonicalized(self):
        raw = signal(limit_price="7.4200")
        self.engine.execute(raw)
        self.assertTrue(self.engine.execute({**raw, "limit_price": "7.42"})["replayed"])

    def test_expired_signal_does_not_reach_adapter(self):
        order = self.engine.execute(signal(expires_at=(utc_now() - timedelta(seconds=1)).isoformat()))
        self.assertEqual(order["status"], "EXPIRED")
        self.assertEqual(self.adapter.lookup("s1"), [])

    def test_invalid_tick_does_not_submit(self):
        order = self.engine.execute(signal(limit_price="7.421"))
        self.assertEqual(order["status"], "PREPARATION_FAILED")
        self.assertEqual(self.adapter.lookup("s1"), [])

    def test_budget_below_lot_rejected(self):
        raw = signal()
        del raw["quantity"]
        raw["budget"] = "10"
        self.assertEqual(self.engine.execute(raw)["status"], "PREPARATION_FAILED")

    def test_sell_more_than_available_is_rejected(self):
        self.assertEqual(self.engine.execute(signal(side="sell", quantity=1500))["status"], "REJECTED")

    def test_buy_insufficient_cash_is_rejected(self):
        self.assertEqual(self.engine.execute(signal(quantity=100000))["status"], "REJECTED")

    def test_timeout_after_submit_is_unknown_until_lookup(self):
        raw = signal()
        self.assertEqual(self.engine.execute(raw, scenario="timeout_after_submit")["status"], "UNKNOWN")
        self.assertEqual(self.engine.execute(raw)["status"], "UNKNOWN")
        self.assertEqual(self.engine.reconcile_paper("s1")["status"], "FILLED")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM paper_orders").fetchone()[0], 1)

    def test_restart_preserves_ledger_and_idempotency(self):
        raw = signal()
        self.engine.execute(raw)
        second_store = Store(self.store.path)
        second_adapter = PaperAdapter(second_store, CONFIG["accounts"])
        second_engine = Executor(second_store, second_adapter, CONFIG)
        self.assertTrue(second_engine.execute(raw)["replayed"])
        with second_store.connect() as db:
            holdings = json.loads(db.execute("SELECT holdings FROM paper_accounts").fetchone()[0])
        self.assertEqual(holdings["000967.SZ"], 1500)

    def test_interrupted_state_requires_reconciliation(self):
        raw = signal()
        self.store.accept(Signal.parse(raw), "paper")
        self.store.claim("s1")
        self.assertEqual(self.store.recover("paper"), 1)
        self.assertEqual(self.engine.execute(raw)["status"], "UNKNOWN")
        self.assertEqual(self.adapter.lookup("s1"), [])

    def test_partial_then_cancel_preserves_executed_quantity(self):
        order = self.engine.execute(signal(), scenario="partial")
        self.assertEqual(order["status"], "PARTIALLY_FILLED")
        receipt = self.receipt(order, "CANCELLED", 50)
        final = self.store.record_receipt("s1", receipt)
        self.assertEqual(final["filled_quantity"], 50)
        self.assertEqual(final["status"], "CANCELLED")

    def test_decreasing_fill_and_stale_receipt_are_rejected(self):
        order = self.engine.execute(signal(), scenario="partial")
        with self.assertRaises(ConflictError):
            self.store.record_receipt("s1", self.receipt(order, "PARTIALLY_FILLED", 40))
        with self.assertRaises(ConflictError):
            self.store.record_receipt("s1", self.receipt(order, "PARTIALLY_FILLED", 50, observed_at=(utc_now() - timedelta(days=1)).isoformat()))

    def test_receipt_must_match_order_and_price(self):
        order = self.engine.execute(signal(), scenario="partial")
        for updates in ({"symbol": "300672.SZ"}, {"side": "sell"}, {"broker_order_id": "WRONG"}, {"average_price": "7.43"}):
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                self.store.record_receipt("s1", self.receipt(order, "FILLED", 100, **updates))

    def test_same_receipt_deduplicated_and_changed_receipt_rejected(self):
        order = self.engine.execute(signal(), scenario="partial")
        receipt = self.receipt(order, "PARTIALLY_FILLED", 50)
        self.store.record_receipt("s1", receipt)
        self.store.record_receipt("s1", receipt)
        with self.assertRaises(ConflictError):
            self.store.record_receipt("s1", {**receipt, "average_price": "7.41"})

    def test_events_survive_restart_and_ack_does_not_trade(self):
        self.engine.execute(signal())
        events = Store(self.store.path).pending_events()
        self.assertTrue(events)
        self.store.acknowledge([event["event_id"] for event in events])
        self.assertEqual(self.store.pending_events(), [])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM paper_orders").fetchone()[0], 1)

    def test_one_manual_draft_at_a_time(self):
        spy = DraftSpy()
        engine = Executor(self.store, spy, CONFIG)
        first = engine.execute(signal("draft1"))
        second = engine.execute(signal("draft2"))
        self.assertEqual(first["status"], "READY_FOR_MANUAL_SUBMIT")
        self.assertEqual(second["status"], "PREPARATION_FAILED")
        self.assertEqual(spy.prepared, 1)
        engine.discard_draft("draft1")
        self.assertEqual(spy.cleared, 1)
        self.assertEqual(engine.execute(signal("draft3"))["status"], "READY_FOR_MANUAL_SUBMIT")

    def test_plan_survives_interruption_before_adapter_returns(self):
        spy = DraftSpy()
        def interrupted(plan, instrument):
            saved = Store(self.store.path).get(plan.signal_id)
            self.assertEqual(saved["plan"], plan.payload())
            raise KeyboardInterrupt()
        spy.prepare = interrupted
        engine = Executor(self.store, spy, CONFIG)
        with self.assertRaises(KeyboardInterrupt):
            engine.execute(signal("interrupted"))
        restarted = Store(self.store.path)
        self.assertEqual(restarted.recover("ths-draft"), 1)
        self.assertEqual(restarted.get("interrupted")["plan"]["quantity"], 100)
        next_spy = DraftSpy()
        result = Executor(restarted, next_spy, CONFIG).execute(signal("next"))
        self.assertEqual(result["status"], "PREPARATION_FAILED")
        self.assertEqual(next_spy.prepared, 0)

    def test_uncertain_cleanup_blocks_new_draft(self):
        spy = DraftSpy()
        def uncertain(plan, instrument):
            raise DraftCleanupError("无法确认清空")
        spy.prepare = uncertain
        engine = Executor(self.store, spy, CONFIG)
        self.assertEqual(engine.execute(signal("uncertain"))["status"], "UNKNOWN")
        spy.prepare = lambda plan, instrument: self.fail("must not overwrite uncertain draft")
        self.assertEqual(engine.execute(signal("next"))["status"], "PREPARATION_FAILED")

    def test_paper_receipts_cannot_confirm_live_draft(self):
        engine = Executor(self.store, DraftSpy(), CONFIG)
        order = engine.execute(signal())
        with self.assertRaises(ValidationError):
            self.store.record_receipt("s1", self.receipt(order, "FILLED", 100))

    def test_max_length_signal_id_has_valid_paper_receipt_ids(self):
        self.assertEqual(self.engine.execute(signal("x" * 128))["status"], "FILLED")

    def test_quantity_budget_floats_nonfinite_and_bad_dates_rejected(self):
        invalid = [signal(quantity=True), signal(quantity=-1), signal(limit_price=7.42), signal(limit_price="NaN"),
                   signal(limit_price="1e999999"), signal(expires_at="2026-10-14T12:00:00"), signal(budget="10000"),
                   signal(symbol="967.SZ"), signal(side="sell", quantity=None, budget="1000")]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.engine.execute(value)

    def test_session_lock_blocks_second_executor(self):
        path = Path(self.tmp.name) / "session.lock"
        with session_lease(path):
            with self.assertRaises(RuntimeError):
                with session_lease(path):
                    self.fail("lock should not be acquired")
        with session_lease(path):
            pass


class TableTests(unittest.TestCase):
    def test_preserves_code_and_alphanumeric_identifier(self):
        text = "证券代码\t操作\t合同编号\t成交数量\t成交均价\t成交编号\t\n000967\t证券买入\t1000015O33\t100\t7.420\t00000123\t\n"
        row = normalize(parse_tsv(text)[0], "trades")
        self.assertEqual(row["symbol"], "000967")
        self.assertEqual(row["order_id"], "1000015O33")
        self.assertEqual(row["trade_id"], "00000123")

    def test_empty_copy_duplicate_headers_and_bad_columns_fail(self):
        for text in ("", "证券代码\t证券代码\n000967\t000967", "证券代码\t数量\n000967", "证券代码\t\t数量\n000967\t\t100"):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_tsv(text)

    def test_valid_header_only_means_empty_table(self):
        self.assertEqual(parse_tsv("证券代码\t成交数量\n"), [])

    def test_positions_ignore_operation_icon(self):
        result = normalize({"证券代码": "000967", "操作": "≡", "可用余额": "100"}, "positions")
        self.assertEqual(result["available_quantity"], 100)

    def test_truncated_identifier_unknown_side_and_bad_code_fail(self):
        row = {"证券代码": "000967", "操作": "证券买入", "合同编号": "A123", "成交数量": "100", "成交均价": "7.42", "成交编号": "123"}
        for updates in ({"合同编号": "A1..."}, {"操作": "融资买入"}, {"证券代码": "967"}, {"成交均价": "NaN"}):
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                normalize({**row, **updates}, "trades")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "http.sqlite")
        self.executor = Executor(self.store, PaperAdapter(self.store, CONFIG["accounts"]), CONFIG)
        self.token = "local-test-token-01234567890123456789"
        self.server = create_server(self.executor, self.token, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, path, data=None, token=None):
        request = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={"Authorization": "Bearer " + (token or self.token), "Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_api_signal_query_events_and_ack(self):
        raw = signal("api")
        self.assertEqual(self.request("/signals", raw)["status"], "FILLED")
        self.assertTrue(self.request("/signals", raw)["replayed"])
        self.assertEqual(self.request("/orders/api")["status"], "FILLED")
        events = self.request("/events")["events"]
        self.request("/events/ack", {"event_ids": [x["event_id"] for x in events]})
        self.assertEqual(self.request("/events")["events"], [])
        self.assertFalse(self.request("/health")["real_submit_supported"])

    def test_unauthorized_and_conflicting_signal(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/health", token="wrong")
        self.assertEqual(error.exception.code, 401)
        raw = signal("conflict")
        self.request("/signals", raw)
        with self.assertRaises(HTTPError) as error:
            self.request("/signals", {**raw, "quantity": 200})
        self.assertEqual(error.exception.code, 409)


if __name__ == "__main__":
    unittest.main()
