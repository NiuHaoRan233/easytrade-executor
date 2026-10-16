"""Offline tests only: fake native adapter, temporary databases and public quote fixtures."""
import copy
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from easytrade.domain import Instrument, Signal, ValidationError, build_plan, utc_now
from easytrade.quotes import Quote, QuoteUnavailable, parse_tencent
from easytrade.tables import TableSnapshot, normalize
from easytrade.user_native import ThsUserAdapter, confirmation_matches, matching_row, positive_button_id
from easytrade.user_session import UserSession

CONFIG = {"session_lock": "runtime/live.lock", "buy_fee_reserve": "5", "accounts": {"test": {}},
          "instruments": {"159570.SZ": {"name": "港股通创新药ETF", "lot_size": 100, "price_tick": "0.001"}},
          "ths_profile": {"account_alias": "test", "pages": {"buy": ["buy"], "sell": ["sell"]}}}


def signal(signal_id="one", side="buy"):
    return Signal.parse({"signal_id": signal_id, "account": "test", "symbol": "159570.SZ", "side": side,
                         "quantity": 100, "limit_price": "1.401", "expires_at": (utc_now() + timedelta(minutes=1)).isoformat()})


class FakeNative:
    def __init__(self):
        self.submitted, self.prepared, self.cancelled = [], [], []
        self.resumed = []
        self.timeout = False
        self.fail_prepare = False
        self.cleanup = True
        self.query_status = "FILLED"
        self.on_prepare = lambda: None

    def connect(self):
        pass

    def prepare(self, plan, instrument):
        self.prepared.append(plan)
        self.on_prepare()
        if self.fail_prepare:
            raise ValueError("bad field")

    def clear_draft(self):
        return self.cleanup

    def submit_prepared(self, plan, instrument, persist_receipt):
        self.submitted.append(plan)
        if self.timeout:
            raise TimeoutError("response lost after click")
        persist_receipt({"receipt_id": uuid4().hex, "account": plan.account, "symbol": plan.symbol, "side": plan.side,
                         "broker_order_id": str(len(self.submitted)), "status": "BROKER_ACCEPTED", "filled_quantity": 0,
                         "average_price": "0", "observed_at": utc_now().isoformat(), "source": "win32"})

    def lookup_receipt(self, order, broker_id=None):
        plan = order["plan"]
        filled = plan["quantity"] if self.query_status == "FILLED" else plan["quantity"] // 2 if self.query_status == "PARTIALLY_FILLED" else 0
        return {"receipt_id": uuid4().hex, "account": plan["account"], "symbol": plan["symbol"], "side": plan["side"],
                "broker_order_id": broker_id or order["broker_order_id"], "status": self.query_status,
                "filled_quantity": filled, "average_price": plan["limit_price"] if filled else "0",
                "observed_at": utc_now().isoformat(), "source": "ocr"}

    def cancel(self, order, persist_intent):
        persist_intent()
        self.cancelled.append(order["broker_order_id"])
        self.query_status = "CANCELLED"

    def resume_confirmation(self, plan, persist_receipt, **kwargs):
        self.resumed.append(plan)
        persist_receipt({"receipt_id": uuid4().hex, "account": plan.account, "symbol": plan.symbol, "side": plan.side,
                         "broker_order_id": "resumed1", "status": "BROKER_ACCEPTED", "filled_quantity": 0,
                         "average_price": "0", "observed_at": utc_now().isoformat(), "source": "win32"})

    def verify_unsubmitted_resolution(self, plan):
        return {"account_verified": True, "unblocked": True, "broker_absence_proven": False}

    def read_account(self):
        return {"account": "test", "observed_at": utc_now().isoformat(), "source": "win32+ocr",
                "balances": {"available_cash": "876.33", "frozen_cash": "0", "stock_value": "23000"},
                "positions": [{"symbol": "159570", "name": "ETF", "position_quantity": 0,
                               "available_quantity": 0, "current_price": "1.371", "market_value": "0"}],
                "positions_complete": False, "positions_scope": "visible_rows"}


class QuoteTests(unittest.TestCase):
    def quote(self, **updates):
        values = dict(symbol="159570.SZ", bid=Decimal("1.400"), ask=Decimal("1.401"), observed_at=utc_now())
        values.update(updates)
        return Quote(**values)

    def test_book_side_and_stale_halted_future(self):
        self.assertEqual(self.quote().price("buy"), "1.401")
        self.assertEqual(self.quote().price("sell"), "1.400")
        for updates in ({"observed_at": utc_now() - timedelta(seconds=6)},
                        {"observed_at": utc_now() + timedelta(seconds=2)},
                        {"bid": Decimal("0")}, {"bid": Decimal("2")}):
            with self.assertRaises(ValidationError):
                self.quote(**updates).price("buy")

    def test_provider_fields_and_symbol_are_bound(self):
        fields = [""] * 31
        fields[2], fields[9], fields[10], fields[19], fields[20], fields[30] = "159570", "1.400", "100", "1.401", "200", "20261016133327"
        text = 'v_sz159570="' + "~".join(fields) + '";'
        quote = parse_tencent(text)
        self.assertEqual(str(quote.ask), "1.401")
        self.assertEqual(quote.observed_at.hour, 13)
        for corrupt in (text.replace("v_sz159570", "v_sz159571"), text.replace('~159570~', '~159571~'), text + "\n" + text):
            with self.assertRaises(ValidationError):
                parse_tencent(corrupt)

    def provider_snapshot(self, *, moment="20261016150354", bid="0", ask="0", bid_qty="0", ask_qty="0"):
        fields = [""] * 31
        fields[2], fields[3], fields[9], fields[10], fields[19], fields[20], fields[30] = (
            "159570", "1.365", bid, bid_qty, ask, ask_qty, moment)
        return 'v_sz159570="' + "~".join(fields) + '";'

    def test_after_close_empty_book_is_displayable_but_not_an_order_price(self):
        quote = parse_tencent(self.provider_snapshot())
        self.assertEqual(quote.session, "已收盘")
        text = quote.describe(now=quote.observed_at)
        self.assertIn("已收盘", text)
        self.assertIn("最新成交1.365（仅供查看）", text)
        for side in ("buy", "sell"):
            with self.assertRaisesRegex(QuoteUnavailable, "已收盘"):
                quote.price(side, now=quote.observed_at)

    def test_intraday_empty_book_and_one_sided_volume_are_not_fabricated(self):
        quote = parse_tencent(self.provider_snapshot(moment="20261016140354"))
        self.assertIn("盘口不可用", quote.describe(now=quote.observed_at))
        with self.assertRaises(QuoteUnavailable):
            quote.price("sell", now=quote.observed_at)
        quote = parse_tencent(self.provider_snapshot(moment="20261016140354", bid="1.364", ask="1.365", bid_qty="100"))
        with self.assertRaisesRegex(QuoteUnavailable, "卖一无挂单数量"):
            quote.price("buy", now=quote.observed_at)
        self.assertEqual(quote.price("sell", now=quote.observed_at), "1.364")

    def test_invalid_book_data_is_still_rejected(self):
        for kwargs in ({"bid": "-1"}, {"ask": "NaN"}, {"bid_qty": "-1"}, {"ask_qty": "bad"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                parse_tencent(self.provider_snapshot(**kwargs))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.native = FakeNative()
        self.logs = []
        self.session = UserSession(self.tmp.name, copy.deepcopy(CONFIG), live=True, adapter=self.native,
                                   quote_provider=lambda: Quote("159570.SZ", Decimal("1.400"), Decimal("1.401"), utc_now()), log=self.logs.append)

    def tearDown(self):
        self.tmp.cleanup()

    def test_broker_only_rows_displayed_and_exact_existing_order_reconciled(self):
        local = self.session.submit("buy", signal_id="local")
        row = {"order_id": local["broker_order_id"], "symbol": "159570", "side": "buy", "quantity": 100,
               "limit_price": "1.401", "filled_quantity": 100, "average_price": "1.401", "status": "FILLED"}
        manual = {**row, "order_id": "manual", "symbol": "000967"}
        self.native.read_orders = lambda: TableSnapshot("orders", [row, manual], utc_now().isoformat(), "ocr", complete=True)
        checked = ThsUserAdapter({"account_alias": "test"})
        self.native.receipt_from_snapshot = checked.receipt_from_snapshot
        snapshot = self.session.broker_orders_snapshot()
        self.assertEqual(snapshot["rows"], [row, manual])
        self.assertTrue(snapshot["complete"])
        self.assertEqual(self.session.store.get("local")["status"], "FILLED")
        self.assertEqual(len(self.session.store.list_orders()), 1)
        self.assertTrue((Path(self.tmp.name) / "runtime/console-orders-live.json").is_file())
        self.assertEqual(len(self.native.submitted), 1)

    def test_broker_snapshot_survives_local_mismatch_without_false_fill(self):
        local = self.session.submit("buy", signal_id="local")
        row = {"order_id": local["broker_order_id"], "symbol": "159570", "side": "sell", "quantity": 100,
               "limit_price": "1.401", "filled_quantity": 100, "average_price": "1.401", "status": "FILLED"}
        self.native.read_orders = lambda: TableSnapshot("orders", [row], utc_now().isoformat(), "ocr")
        self.native.receipt_from_snapshot = ThsUserAdapter({"account_alias": "test"}).receipt_from_snapshot
        self.assertEqual(self.session.broker_orders_snapshot()["rows"], [row])
        self.assertEqual(self.session.store.get("local")["status"], "BROKER_ACCEPTED")
        self.assertTrue(any("尚未同步" in message for message in self.logs))

    def test_same_request_deduplicated_but_new_request_is_independent(self):
        self.native.timeout = True
        result = self.session.submit("buy", signal_id="lost")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.session.submit("buy", signal_id="lost")["status"], "UNKNOWN")
        self.assertEqual(len(self.native.submitted), 1)
        self.native.timeout = False
        self.assertEqual(self.session.submit("sell", signal_id="other")["status"], "BROKER_ACCEPTED")
        self.assertEqual(len(self.native.prepared), 2)
        self.assertEqual(len(self.native.submitted), 2)
        self.assertEqual(self.session.store.get("lost")["status"], "UNKNOWN")

    def test_accepted_and_partial_orders_do_not_block_new_buy_or_sell(self):
        accepted = self.session.submit("buy", signal_id="accepted")
        self.assertEqual(accepted["status"], "BROKER_ACCEPTED")
        self.native.query_status = "PARTIALLY_FILLED"
        self.session.query("accepted")
        sell = self.session.submit("sell", signal_id="new-sell")
        buy = self.session.submit("buy", signal_id="new-buy")
        self.assertEqual([sell["status"], buy["status"]], ["BROKER_ACCEPTED"] * 2)
        self.assertEqual(self.session.store.get("accepted")["filled_quantity"], 50)
        self.assertEqual([p.side for p in self.native.submitted], ["buy", "sell", "buy"])

    def test_requested_quantity_passed_to_native_and_receipt(self):
        result = self.session.submit_and_follow("buy", quantity=300, signal_id="three-lots")
        self.assertEqual(result["filled_quantity"], 300)
        self.assertEqual(self.native.prepared[0].quantity, 300)
        self.assertEqual(self.native.submitted[0].quantity, 300)
        with self.assertRaises(ValidationError):
            self.session.submit("buy", quantity=100, signal_id="three-lots")
        self.assertEqual(len(self.native.submitted), 1)

    def test_invalid_quantity_does_not_create_or_prepare_order(self):
        for quantity in (0, -100, 101, True, "100"):
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError):
                self.session.submit("buy", quantity=quantity)
        self.assertEqual(self.native.prepared, [])
        self.assertEqual(self.session.orders(), [])

    def test_refresh_updates_legacy_acceptance_without_new_native_submission(self):
        self.session.submit("buy", signal_id="old-accepted")
        rows = self.session.refresh_receipts()
        self.assertEqual(rows[0]["status"], "FILLED")
        self.assertEqual(rows[0]["filled_quantity"], 100)
        self.assertEqual(len(self.native.prepared), 1)
        self.assertEqual(len(self.native.submitted), 1)
        self.assertEqual(self.native.cancelled, [])

    def test_refresh_failure_keeps_state_and_new_instruction_still_works(self):
        self.session.submit("buy", signal_id="old-accepted")
        self.native.lookup_receipt = lambda *args: (_ for _ in ()).throw(ValidationError("bad OCR"))
        self.assertEqual(self.session.refresh_receipts()[0]["status"], "BROKER_ACCEPTED")
        self.assertTrue(any("bad OCR" in message for message in self.logs))
        self.assertEqual(self.session.submit("sell", signal_id="independent-sell")["status"], "BROKER_ACCEPTED")

    def test_refresh_unknown_without_id_does_not_guess_or_resubmit(self):
        self.native.timeout = True
        self.session.submit("buy", signal_id="unknown")
        self.native.lookup_receipt = lambda *args: self.fail("must not guess a missing ID")
        self.assertEqual(self.session.refresh_receipts()[0]["status"], "UNKNOWN")
        self.assertEqual(len(self.native.submitted), 1)

    def test_single_order_follow_gets_fill_without_resubmitting(self):
        result = self.session.submit_and_follow("sell", signal_id="follow")
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["filled_quantity"], 100)
        self.assertEqual(len(self.native.submitted), 1)
        self.session.submit_and_follow("sell", signal_id="follow")
        self.assertEqual(len(self.native.submitted), 1)

    def test_fill_follow_timeout_and_read_failure_preserve_known_acceptance(self):
        result = self.session.submit_and_follow("buy", signal_id="follow-timeout", wait_seconds=0)
        self.assertEqual(result["status"], "BROKER_ACCEPTED")
        self.assertEqual(len(self.native.submitted), 1)
        def bad_query(*args):
            raise ValidationError("bad OCR")
        self.native.lookup_receipt = bad_query
        # Same request resumes read-only observation, never another submit.
        result = self.session.submit_and_follow("buy", signal_id="follow-timeout")
        self.assertEqual(result["status"], "BROKER_ACCEPTED")
        self.assertTrue(any("bad OCR" in message for message in self.logs))
        self.assertEqual(len(self.native.submitted), 1)

    def test_account_snapshot_keeps_total_available_and_scope_separate(self):
        snapshot = self.session.account_snapshot()
        self.assertEqual(snapshot["balances"]["available_cash"], "876.33")
        self.assertEqual(snapshot["positions"][0]["position_quantity"], 0)
        self.assertFalse(snapshot["positions_complete"])
        saved = json.loads((Path(self.tmp.name) / "runtime/console-account-live.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, snapshot)
        self.assertEqual(self.native.submitted, [])

    def test_manual_broker_binding_recovers_unknown_without_resubmit(self):
        self.native.timeout = True
        self.session.submit("buy", signal_id="lost")
        result = self.session.query("lost", "12345")
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["broker_order_id"], "12345")
        self.assertEqual(len(self.native.submitted), 1)

    def test_user_resume_keeps_original_signal_and_never_submits_another_order(self):
        self.native.timeout = True
        self.session.submit("buy", signal_id="pending-confirm")
        result = self.session.resume_confirmation("pending-confirm")
        self.assertEqual(result["status"], "BROKER_ACCEPTED")
        self.assertEqual(result["signal_id"], "pending-confirm")
        self.assertEqual(len(self.session.orders()), 1)
        self.assertEqual(len(self.native.submitted), 1)
        self.assertEqual(len(self.native.prepared), 1)
        self.assertEqual(len(self.native.resumed), 1)
        self.assertEqual(self.native.resumed[0].limit_price, "1.401")
        with self.assertRaises(ValidationError):
            self.session.resume_confirmation("pending-confirm")
        self.assertEqual(len(self.native.resumed), 1)

    def test_resume_failure_stays_unknown_and_does_not_buy_again(self):
        self.native.timeout = True
        self.session.submit("buy", signal_id="pending-confirm")
        def failed(*args):
            raise ValidationError("no matching confirmation")
        self.native.resume_confirmation = failed
        self.assertEqual(self.session.resume_confirmation("pending-confirm")["status"], "UNKNOWN")
        self.assertEqual(len(self.native.submitted), 1)

    def test_success_hint_survives_lookup_failure_and_cannot_be_declared_unsubmitted(self):
        self.native.timeout = True
        self.session.submit("buy", signal_id="success-popup")
        def resume(plan, persist, *, created_at=None, persist_hint=None):
            persist_hint("12345")
            raise ValidationError("visible table not ready")
        self.native.resume_confirmation = resume
        result = self.session.resume_confirmation("success-popup")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["broker_order_id"])
        self.assertEqual(result["detail"]["broker_order_id_hint"], "12345")
        with self.assertRaises(ValidationError):
            self.session.resolve_unsubmitted("success-popup", user_declared_no_confirmation=True)
        result = self.session.query("success-popup")
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["broker_order_id"], "12345")
        self.assertEqual(len(self.native.submitted), 1)

    def test_local_resolution_needs_user_declaration_not_an_empty_table(self):
        self.native.timeout = True
        self.session.submit("buy", signal_id="closed-confirm")
        with self.assertRaises(ValidationError):
            self.session.resolve_unsubmitted("closed-confirm")
        self.assertEqual(self.session.store.get("closed-confirm")["status"], "UNKNOWN")
        result = self.session.resolve_unsubmitted("closed-confirm", user_declared_no_confirmation=True)
        self.assertEqual(result["status"], "PREPARATION_FAILED")
        self.assertEqual(result["detail"]["resolution"], "user_declared_not_submitted")
        self.assertFalse(result["detail"]["broker_absence_proven"])
        self.assertFalse(result["detail"]["cancel_sent"])
        self.assertEqual(len(self.native.submitted), 1)
        self.assertEqual(self.native.cancelled, [])
        self.assertEqual(len(self.session.orders()), 1)

    def test_local_resolution_refuses_scene_failure_and_known_broker_order(self):
        self.native.timeout = True
        self.session.submit("buy", signal_id="blocked")
        def blocked(plan):
            raise ValidationError("modal still visible")
        self.native.verify_unsubmitted_resolution = blocked
        with self.assertRaises(ValidationError):
            self.session.resolve_unsubmitted("blocked", user_declared_no_confirmation=True)
        self.assertEqual(self.session.store.get("blocked")["status"], "UNKNOWN")
        self.session.query("blocked", "12345")
        with self.assertRaises(ValidationError):
            self.session.resolve_unsubmitted("blocked", user_declared_no_confirmation=True)

    def test_historical_manual_draft_does_not_block_explicit_new_request(self):
        self.session.store.accept(signal("old"), "ths-draft")
        self.session.store.claim("old")
        self.session.store.transition("old", "READY_FOR_MANUAL_SUBMIT", expected=("PREPARING",))
        self.assertEqual(self.session.submit("buy")["status"], "BROKER_ACCEPTED")
        self.assertEqual(len(self.native.prepared), 1)
        self.assertEqual(self.session.store.get("old")["status"], "READY_FOR_MANUAL_SUBMIT")

    def test_restart_mid_submit_becomes_unknown(self):
        self.session.store.accept(signal("mid"), "ths-user")
        self.session.store.claim("mid")
        self.session.store.transition("mid", "SUBMITTING", expected=("PREPARING",))
        self.assertEqual(self.session.orders()[0]["status"], "UNKNOWN")
        self.assertEqual(self.session.submit("buy")["status"], "BROKER_ACCEPTED")
        self.assertEqual(self.session.store.get("mid")["status"], "UNKNOWN")

    def test_explicit_manual_price_does_not_fetch_public_quote(self):
        self.session.quote_provider = lambda: self.fail("manual price must not fetch quotes")
        self.assertEqual(self.session.submit("buy", manual_price="1.401")["status"], "BROKER_ACCEPTED")

    def test_unavailable_book_does_not_create_order_and_manual_price_is_independent(self):
        self.session.quote_provider = lambda: Quote("159570.SZ", Decimal("0"), Decimal("0"), utc_now(),
                                                    last=Decimal("1.365"), session="已收盘")
        with self.assertRaisesRegex(QuoteUnavailable, "已收盘"):
            self.session.submit("buy", signal_id="no-book")
        self.assertEqual(self.session.orders(), [])
        self.assertEqual(self.native.prepared, [])
        self.assertEqual(self.session.submit("buy", manual_price="1.365", signal_id="manual")["status"], "BROKER_ACCEPTED")
        self.assertEqual(self.native.submitted[0].limit_price, "1.365")

    def test_moved_book_does_not_submit_and_clears_fields(self):
        calls = iter(["1.401", "1.402"])
        self.session.quote_provider = lambda: Quote("159570.SZ", Decimal("1.400"), Decimal(next(calls)), utc_now())
        result = self.session.submit("buy")
        self.assertEqual(result["status"], "PREPARATION_FAILED")
        self.assertEqual(self.native.submitted, [])

    def test_cleanup_uncertainty_preserved_without_blocking_explicit_new_order(self):
        self.native.fail_prepare, self.native.cleanup = True, False
        old = self.session.submit("buy")
        self.assertEqual(old["status"], "UNKNOWN")
        self.native.fail_prepare, self.native.cleanup = False, True
        self.assertEqual(self.session.submit("sell")["status"], "BROKER_ACCEPTED")
        self.assertEqual(self.session.store.get(old["signal_id"])["status"], "UNKNOWN")

    def test_stop_during_prepare_does_not_click(self):
        stop = threading.Event()
        self.native.on_prepare = stop.set
        self.assertEqual(self.session.submit("buy", stop=stop)["status"], "PREPARATION_FAILED")
        self.assertEqual(self.native.submitted, [])

    def test_stop_during_quote_refresh_does_not_click(self):
        stop = threading.Event()
        calls = []
        def quote():
            calls.append(1)
            if len(calls) == 2:
                stop.set()
            return Quote("159570.SZ", Decimal("1.400"), Decimal("1.401"), utc_now())
        self.session.quote_provider = quote
        self.assertEqual(self.session.submit("buy", stop=stop)["status"], "PREPARATION_FAILED")
        self.assertEqual(self.native.submitted, [])

    def test_cross_account_receipt_cannot_be_bound(self):
        other = signal("foreign").payload()
        other["account"] = "other"
        self.session.store.accept(Signal.parse(other), "ths-user")
        with self.assertRaises(ValidationError):
            self.session.query("foreign", "123")

    def test_follow_does_not_create_a_sell_for_accepted_or_partial_buy(self):
        for state in ("BROKER_ACCEPTED", "PARTIALLY_FILLED"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                adapter = FakeNative()
                adapter.query_status = state
                session = UserSession(directory, CONFIG, live=True, adapter=adapter)
                # wait_seconds=0 checks no polling is necessary for timeout protection.
                result = session.submit_and_follow("buy", manual_price="1.401", wait_seconds=0)
                self.assertEqual(result["status"], "BROKER_ACCEPTED")
                self.assertEqual([p.side for p in adapter.submitted], ["buy"])

    def test_explicit_buy_and_sell_replay_are_independent(self):
        self.session.submit_and_follow("buy", signal_id="buy-one")
        self.assertEqual([p.side for p in self.native.submitted], ["buy"])
        self.session.submit_and_follow("sell", signal_id="sell-one")
        self.session.submit_and_follow("sell", signal_id="sell-one")
        self.assertEqual([p.side for p in self.native.submitted], ["buy", "sell"])

    def test_cancel_records_uncertainty_before_action_and_queries_result(self):
        order = self.session.submit("buy")
        self.native.query_status = "BROKER_ACCEPTED"
        original = self.native.cancel
        def cancel(order, intent):
            def durable():
                intent()
                self.assertEqual(self.session.store.get(order["signal_id"])["status"], "UNKNOWN")
            original(order, durable)
        self.native.cancel = cancel
        result = self.session.cancel(order["signal_id"])
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(self.native.cancelled, [order["broker_order_id"]])

    def test_pending_user_order_blocks_original_draft_executor(self):
        self.native.timeout = True
        self.session.submit("buy", signal_id="pending")
        self.assertEqual(self.session.store.pending_draft("test")["signal_id"], "pending")

    def test_independent_paper_orders_do_not_construct_native_adapter(self):
        with patch("easytrade.user_native.ThsUserAdapter", side_effect=AssertionError("native forbidden")):
            paper = UserSession(self.tmp.name, CONFIG)
            result = [paper.submit("buy", signal_id="paper-buy"), paper.submit("sell", signal_id="paper-sell")]
            self.assertEqual([r["status"] for r in result], ["FILLED", "FILLED"])
            self.assertTrue(all(r["mode"] == "paper" for r in result))
            with paper.store.connect() as db:
                row = db.execute("SELECT * FROM paper_accounts").fetchone()
                self.assertEqual(json.loads(row["holdings"])["159570.SZ"], 0)
                self.assertEqual(Decimal(row["cash"]), Decimal("994.900"))


class NativeContractTests(unittest.TestCase):
    def setUp(self):
        self.plan = build_plan(signal(), Instrument.parse(CONFIG["instruments"]["159570.SZ"]), Decimal("5"))
        self.text = "买入确认\n证券代码：159570\n买入价格：1.401\n买入数量：100"

    def test_confirmation_requires_all_labelled_fields_and_direction(self):
        self.assertTrue(confirmation_matches(self.text, self.plan))
        for text in (self.text.replace("100", "1000"), self.text.replace("1.401", "1.402"),
                     self.text.replace("159570", "1595708"), self.text.replace("买入", "卖出"),
                     "买入确认159570 1.401 100", self.text + "\n买入数量：200"):
            with self.subTest(text=text):
                self.assertFalse(confirmation_matches(text, self.plan))

    def test_observed_rich_static_confirmation_checks_exact_formatted_price(self):
        raw = ('资金帐号：测试账户\n证券代码：159570(港股通创新药ETF汇添?\n'
               '买入价格：<font  color=blue>1.401 (卖一)</font>\n买入数量：100\n'
               '预估金额：140.142\n您是否确定以上买入委托？\n委托确认')
        self.assertTrue(confirmation_matches(raw, self.plan))
        for value in (raw.replace("1.401", "1.402"), raw.replace("1.401", "1,401"),
                      raw.replace("<font  color=blue>", "<unknown>"), raw.replace("100", "1000")):
            self.assertFalse(confirmation_matches(value, self.plan))

    def test_only_unique_known_positive_button_is_allowed(self):
        for label in ("是(&Y)", "是(Y)", "是（Y）", "确定", "确认"):
            self.assertEqual(positive_button_id([{"id": 6, "text": label}, {"id": 7, "text": "否"}]), 6)
        for buttons in ([{"id": 99, "text": "确定"}], [{"id": 1, "text": "撤全部"}], [],
                        [{"id": 1, "text": "确定"}, {"id": 6, "text": "是"}]):
            with self.assertRaises(ValidationError):
                positive_button_id(buttons)

    def test_observed_id2_is_only_allowed_for_success_information(self):
        buttons = [{"id": 2, "text": "确定"}, {"id": 1008, "text": "查询当前委托"},
                   {"id": 2392, "text": "点击此处不再提示"}]
        self.assertEqual(positive_button_id(buttons, kind="ORDER_ACCEPTED"), 2)
        for kind in (None, "ORDER_CONFIRMATION", "CANCEL_CONFIRMATION", "UNKNOWN"):
            with self.assertRaises(ValidationError):
                positive_button_id(buttons, kind=kind)

    def test_dialog_must_be_owned_by_exact_main_and_foreground(self):
        native = ThsUserAdapter({})
        native.main = SimpleNamespace(handle=10, is_enabled=lambda: True)
        native._is_foreground = lambda: True
        helper = {"handle": 99, "owner": 0, "kind": "UNKNOWN"}
        modal = {"handle": 20, "owner": 10, "kind": "CAPTCHA"}
        win32 = SimpleNamespace(GetForegroundWindow=lambda: 20, GetAncestor=lambda handle, flag: handle,
                                GetWindow=lambda handle, flag: 0)
        native.observe_dialogs = lambda: {"dialogs": [helper, modal]}
        with patch.dict("sys.modules", {"win32gui": win32}):
            self.assertEqual(native._active_dialog(), modal)
            win32.GetForegroundWindow = lambda: 30
            with self.assertRaises(ValidationError):
                native._active_dialog()
            win32.GetForegroundWindow = lambda: 10
            native.observe_dialogs = lambda: {"dialogs": [helper]}
            self.assertIsNone(native._active_dialog())

    def test_observed_tooltip_does_not_count_as_second_modal_but_unknown_windows_do(self):
        native = ThsUserAdapter({})
        native.main = SimpleNamespace(handle=10, is_enabled=lambda: False)
        native._is_foreground = lambda: False
        modal = {"handle": 20, "owner": 10, "window_class": "#32770", "kind": "ORDER_CONFIRMATION"}
        tooltip = {"handle": 30, "owner": 10, "window_class": "CPPToolTip", "kind": "UNKNOWN", "buttons": []}
        helper = {"handle": 40, "owner": 0, "window_class": "#32770", "kind": "UNKNOWN"}
        dialogs = [modal, tooltip, helper]
        native.observe_dialogs = lambda: {"dialogs": dialogs}
        win32 = SimpleNamespace(GetForegroundWindow=lambda: 20, GetAncestor=lambda handle, flag: handle,
                                GetWindow=lambda handle, flag: 0)
        with patch.dict("sys.modules", {"win32gui": win32}):
            self.assertEqual(native._active_dialog(), modal)
            for changed in ({"window_class": "#32770"}, {"buttons": [{"id": 1, "text": "确定"}]},
                            {"read_error": "RuntimeError"}):
                dialogs[:] = [modal, {**tooltip, **changed}, helper]
                with self.assertRaises(ValidationError):
                    native._active_dialog()
            dialogs[:] = [modal, tooltip, helper]
            win32.GetForegroundWindow = lambda: 30
            with self.assertRaises(ValidationError):
                native._active_dialog()

    def test_no_modal_can_activate_main_for_inspection_without_clicking_trade(self):
        native = ThsUserAdapter({})
        actions = []
        focused = [False]
        def focus():
            focused[0] = True
            actions.append("focus-main")
        native.main = SimpleNamespace(handle=10, is_enabled=lambda: True,
                                      restore=lambda: actions.append("restore"), set_focus=focus)
        native._is_foreground = lambda: focused[0]
        native.observe_dialogs = lambda: {"dialogs": [{"handle": 40, "owner": 0, "window_class": "#32770"}]}
        win32 = SimpleNamespace(GetForegroundWindow=lambda: 30, GetAncestor=lambda h, f: h,
                                GetWindow=lambda h, f: 0)
        with patch.dict("sys.modules", {"win32gui": win32}):
            self.assertIsNone(native._active_dialog(activate=True))
        self.assertEqual(actions, ["restore", "focus-main"])

    def test_resume_without_modal_explains_resolution_and_never_submits(self):
        native = ThsUserAdapter({"account_alias": "test"})
        native._attach = lambda: None
        native._active_dialog = lambda **kwargs: None
        with self.assertRaisesRegex(ValidationError, "原委托确认框已经不在"):
            native.resume_confirmation(self.plan, lambda receipt: self.fail("unexpected receipt"))

    def test_cancellation_selects_exact_ocr_id_and_persists_before_doubleclick(self):
        from PIL import Image
        import numpy as np
        native = ThsUserAdapter({"pages": {}, "timeout_seconds": 0.1, "account_alias": "test"})
        image = Image.new("RGB", (200, 100), "white")
        row = {"order_id": "123", "symbol": "159570", "side": "buy", "quantity": 100,
               "limit_price": "1.401", "filled_quantity": 0, "status": "BROKER_ACCEPTED"}
        order = {"broker_order_id": "123", "plan": self.plan.payload()}
        events = []
        grid = SimpleNamespace(capture_as_image=lambda: image.copy(), double_click_input=lambda **kwargs: events.append("doubleclick"))
        native.connect = lambda: None
        native._page = lambda path: None
        native._assert_identity = lambda: None
        native._control = lambda *args: grid
        native.reader = SimpleNamespace(confidence=0.85,
            read=lambda image, role, now: TableSnapshot(role, [row], now, "ocr"),
            _recognize=lambda image: SimpleNamespace(txts=["123"], scores=[0.99],
                boxes=[np.array([[10, 20], [30, 20], [30, 40], [10, 40]])]))
        dialogs = iter([{"kind": "CANCEL_CONFIRMATION", "text": "委托编号：123"}, {"kind": "CANCEL_REQUEST_ACCEPTED"}])
        native._active_dialog = lambda: next(dialogs)
        native._click_positive = lambda item: events.append(item["kind"])
        native.cancel(order, lambda: events.append("durable-intent"))
        self.assertEqual(events, ["durable-intent", "doubleclick", "CANCEL_CONFIRMATION", "CANCEL_REQUEST_ACCEPTED"])
        # Duplicate ID locations must stop before either persistence or input.
        native.reader._recognize = lambda image: SimpleNamespace(txts=["123", "123"], scores=[0.99, 0.99], boxes=[[], []])
        events.clear()
        with self.assertRaises(ValidationError):
            native.cancel(order, lambda: events.append("durable-intent"))
        self.assertEqual(events, [])

    def native(self, dialogs):
        native = ThsUserAdapter({"pages": {"buy": ["buy"]}, "timeout_seconds": 0.1})
        native._assert_identity = lambda: None
        values = {1032: "159570", 1033: "1.401", 1034: "100"}
        native._read_edit = lambda key: values[key]
        self.clicks, self.events = [], []
        def control(key, cls):
            if key == 129:
                return SimpleNamespace(get_item=lambda path: SimpleNamespace(is_selected=lambda: True))
            if key == 1036:
                return SimpleNamespace(window_text=lambda: "港股通创新药ETF")
            if key == 1006:
                return SimpleNamespace(window_text=lambda: "买入", click=lambda: self.clicks.append("submit"))
            raise AssertionError(key)
        native._control = control
        iterator = iter(dialogs)
        native._active_dialog = lambda **kwargs: next(iterator)
        native._click_positive = lambda item: self.events.append("close-" + item["kind"])
        return native

    def test_success_id_is_persisted_before_dismissing_popup(self):
        native = self.native([{"kind": "ORDER_CONFIRMATION", "text": self.text},
                              {"kind": "ORDER_ACCEPTED", "text": "", "broker_order_id": "123", "source": "win32"}])
        receipts = []
        def persist(receipt):
            receipts.append(receipt)
            self.events.append("persist")
        native.submit_prepared(self.plan, SimpleNamespace(name="港股通创新药ETF"), persist)
        self.assertEqual(self.clicks, ["submit"])
        self.assertEqual(self.events, ["close-ORDER_CONFIRMATION", "persist", "close-ORDER_ACCEPTED"])
        self.assertEqual(receipts[0]["filled_quantity"], 0)

    def test_resume_validates_account_and_original_modal_without_submit_button(self):
        confirm = {"handle": 20, "kind": "ORDER_CONFIRMATION", "text": self.text}
        success = {"handle": 21, "kind": "ORDER_ACCEPTED", "text": "", "broker_order_id": "123", "source": "win32"}
        native = self.native([confirm, confirm, confirm, success])
        native.profile["account_alias"] = "test"
        native.main = SimpleNamespace(is_visible=lambda: True)
        native._attach = lambda: None
        native._assert_caption_identity = lambda: self.events.append("identity")
        receipts = []
        native.resume_confirmation(self.plan, receipts.append)
        self.assertEqual(self.clicks, [])
        self.assertEqual(self.events, ["identity", "close-ORDER_CONFIRMATION", "close-ORDER_ACCEPTED"])
        self.assertEqual(receipts[0]["broker_order_id"], "123")

    def test_resume_rejects_a_different_order_before_any_confirmation_click(self):
        native = self.native([{"kind": "ORDER_CONFIRMATION", "text": self.text.replace("100", "200")}])
        native.profile["account_alias"] = "test"
        native._attach = lambda: None
        with self.assertRaises(ValidationError):
            native.resume_confirmation(self.plan, lambda receipt: self.fail("unexpected receipt"))
        self.assertEqual(self.clicks, [])
        self.assertEqual(self.events, [])

    def test_success_popup_recovery_requires_exact_table_reconciliation_and_durable_hint(self):
        success = {"handle": 21, "kind": "ORDER_ACCEPTED", "text": "已成功提交", "broker_order_id": "12345"}
        native = self.native([success])
        native.profile["account_alias"] = "test"
        native.main = SimpleNamespace(is_visible=lambda: True)
        native._attach = lambda: None
        native._assert_caption_identity = lambda: self.events.append("identity")
        receipt = {"broker_order_id": "12345", "status": "FILLED", "filled_quantity": 100}
        def lookup(order, broker_id):
            self.assertEqual(order["plan"], self.plan.payload())
            self.assertEqual(broker_id, "12345")
            self.events.append("table-reconciliation")
            return receipt
        native.lookup_receipt = lookup
        received = []
        native.resume_confirmation(self.plan, received.append, created_at=utc_now().isoformat(),
            persist_hint=lambda broker_id: self.events.append("hint-" + broker_id))
        self.assertEqual(self.events, ["identity", "hint-12345", "close-ORDER_ACCEPTED", "table-reconciliation"])
        self.assertEqual(received, [receipt])
        self.assertEqual(self.clicks, [])

    def test_unknown_captcha_wrong_fields_and_repeated_confirmation_never_auto_confirm(self):
        for dialogs in ([{"kind": "CAPTCHA", "text": "验证码"}],
                        [{"kind": "UNKNOWN", "text": "未知"}],
                        [{"kind": "ORDER_CONFIRMATION", "text": self.text.replace("100", "200")}],
                        [{"kind": "ORDER_CONFIRMATION", "text": self.text}] * 2):
            with self.subTest(dialogs=dialogs):
                native = self.native(dialogs)
                with self.assertRaises(ValidationError):
                    native.submit_prepared(self.plan, SimpleNamespace(name="港股通创新药ETF"), lambda receipt: self.fail("unexpected receipt"))
                self.assertEqual(self.clicks, ["submit"])
                self.assertLessEqual(len(self.events), 1)

    def test_query_binding_needs_exact_id_code_side_quantity_and_price(self):
        row = {"order_id": "123", "symbol": "159570", "side": "buy", "quantity": 100, "limit_price": "1.401"}
        def snapshot(rows):
            return TableSnapshot("orders", rows, utc_now().isoformat(), "ocr")
        self.assertEqual(matching_row(snapshot([row]), self.plan.payload(), "123"), row)
        for rows in ([], [row, row], [{**row, "symbol": "159571"}], [{**row, "quantity": 200}],
                     [{**row, "side": "sell"}], [{**row, "limit_price": "1.400"}]):
            with self.assertRaises(ValidationError):
                matching_row(snapshot(rows), self.plan.payload(), "123")

    def test_read_only_lookup_retries_only_focus_interruption_once(self):
        from easytrade.adapters import ForegroundChangedError
        native = ThsUserAdapter({})
        native.submit_prepared = lambda *args: self.fail("query must never submit")
        native.cancel = lambda *args: self.fail("query must never cancel")
        receipt = {"status": "FILLED"}
        with patch.object(native, "_lookup_receipt_once", side_effect=[ForegroundChangedError("focus"), receipt]) as lookup:
            self.assertEqual(native.lookup_receipt({}, "123"), receipt)
            self.assertEqual(lookup.call_count, 2)
        with patch.object(native, "_lookup_receipt_once", side_effect=ForegroundChangedError("focus")) as lookup:
            with self.assertRaisesRegex(ForegroundChangedError, "已只读重试一次"):
                native.lookup_receipt({}, "123")
            self.assertEqual(lookup.call_count, 2)
        with patch.object(native, "_lookup_receipt_once", side_effect=ValidationError("bad OCR")) as lookup:
            with self.assertRaisesRegex(ValidationError, "bad OCR"):
                native.lookup_receipt({}, "123")
            self.assertEqual(lookup.call_count, 1)

    def test_broker_table_refresh_retries_only_focus_interruption(self):
        from easytrade.adapters import ForegroundChangedError
        native = ThsUserAdapter({})
        snapshot = TableSnapshot("orders", [], utc_now().isoformat(), "ocr", complete=True)
        with patch.object(native, "_read_orders_once", side_effect=[ForegroundChangedError("focus"), snapshot]) as read:
            self.assertIs(native.read_orders(), snapshot)
            self.assertEqual(read.call_count, 2)
        with patch.object(native, "_read_orders_once", side_effect=ForegroundChangedError("focus")) as read:
            with self.assertRaisesRegex(ForegroundChangedError, "已只读重试一次"):
                native.read_orders()
            self.assertEqual(read.call_count, 2)
        with patch.object(native, "_read_orders_once", side_effect=ValidationError("OCR")) as read:
            with self.assertRaisesRegex(ValidationError, "OCR"):
                native.read_orders()
            self.assertEqual(read.call_count, 1)

    def test_queued_order_is_not_a_fill(self):
        row = normalize({"证券代码": "159570", "操作": "买入", "委托编号": "123", "委托数量": "100",
                         "成交数量": "0", "委托价格": "1.401", "状态": "待报"}, "orders")
        self.assertEqual(row["status"], "BROKER_ACCEPTED")
        self.assertEqual(row["raw_status"], "待报")
        self.assertEqual(row["filled_quantity"], 0)

    def test_account_reader_preserves_quantities_and_reads_balances_without_submission(self):
        native = ThsUserAdapter({"account_alias": "test"})
        native.connect = lambda: None
        native._assert_identity = lambda: None
        row = {"symbol": "159570", "position_quantity": 100, "available_quantity": 0}
        native.read_positions = lambda: TableSnapshot("positions", [row], utc_now().isoformat(), "ocr")
        values = {1012: "1000", 1013: "100", 1016: "900", 1014: "140", 1015: "1140"}
        native._control = lambda key, cls: SimpleNamespace(window_text=lambda: values[key])
        snapshot = native.read_account()
        self.assertEqual(snapshot["balances"]["available_cash"], "900")
        self.assertEqual(snapshot["positions"][0]["position_quantity"], 100)
        self.assertEqual(snapshot["positions"][0]["available_quantity"], 0)
        self.assertFalse(snapshot["positions_complete"])
        row.pop("position_quantity")
        with self.assertRaises(ValidationError):
            native.read_account()


if __name__ == "__main__":
    unittest.main()
