"""Offline quote geometry and source routing; never contact a trading client."""
import copy
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from easytrade.domain import ValidationError
from easytrade.native_quotes import parse_panel
from easytrade.quotes import Quote, QuoteUnavailable
from easytrade.user_session import UserSession
from test_user_console import CONFIG


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 16, 14, 0, 1, tzinfo=timezone(timedelta(hours=8)))
        self.cells = [("14:00:00", 140, 10), ("最新", 20, 35), ("1.365", 105, 35),
                      ("涨幅", 170, 35), ("2.094%", 245, 35)]
        for side, first, step, price in (("卖", 145, -20, "1.366"), ("买", 175, 20, "1.365")):
            for level in range(1, 6):
                y = first + (level - 1) * step
                self.cells += [(f"{side}{level}", 20, y), (price, 140, y), (str(100 * level), 260, y)]

    def parse(self, scores=None):
        result = SimpleNamespace(txts=[t for t, x, y in self.cells],
            scores=scores or [.99] * len(self.cells),
            boxes=[[(x-4, y-4), (x+4, y-4), (x+4, y+4), (x-4, y+4)] for t, x, y in self.cells])
        return parse_panel(result, 300, 300, "159570.SZ", self.now)

    def test_five_levels_source_and_client_clock(self):
        quote = self.parse()
        self.assertEqual(quote.source, "ths_client")
        self.assertEqual(quote.time_kind, "client_display_time")
        self.assertEqual(quote.captured_at, self.now)
        self.assertEqual(quote.last, Decimal("1.365"))
        self.assertEqual(quote.change_percent, Decimal("2.094"))
        self.assertEqual(len(quote.bids), 5)
        self.assertEqual(quote.asks[4].quantity, 500)
        self.assertEqual(quote.price("buy", now=self.now), "1.366")
        self.assertEqual(quote.payload()["bids"][0]["price"], "1.365")

    def test_missing_and_low_confidence_top_cells_never_become_executable(self):
        self.cells = [cell for cell in self.cells if cell != ("1.366", 140, 145)]
        quote = self.parse()
        self.assertIsNone(quote.asks[0].price)
        with self.assertRaises(QuoteUnavailable):
            quote.price("buy", now=self.now)
        self.cells = [cell for cell in self.cells if cell != ("100", 260, 175)]
        quote = self.parse()
        self.assertIsNone(quote.bids[0].quantity)
        self.assertIn("买—（未识别）", quote.describe(now=self.now))

    def test_ambiguous_label_clock_or_latest_fails(self):
        original = self.cells[:]
        for extra in (("买1", 20, 175), ("14:00:00", 170, 10), ("1.364", 110, 35)):
            self.cells = original + [extra]
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                self.parse()
        self.cells = [c for c in original if c[0] != "14:00:00"]
        with self.assertRaises(ValidationError):
            self.parse()

    def test_low_confidence_label_and_cell_are_not_trusted(self):
        scores = [.99] * len(self.cells)
        scores[next(i for i,c in enumerate(self.cells) if c[0] == "买1")] = .5
        with self.assertRaises(ValidationError):
            self.parse(scores)
        scores = [.99] * len(self.cells)
        scores[self.cells.index(("1.366", 140, 145))] = .5
        self.assertIsNone(self.parse(scores).asks[0].price)


class SourceTests(unittest.TestCase):
    def test_native_default_explicit_web_and_no_automatic_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            native_quote = Quote("159570.SZ", Decimal("1.3"), Decimal("1.4"), datetime.now(timezone.utc), source="ths_client")
            native = Mock(read_quote=Mock(return_value=native_quote))
            session = UserSession(directory, copy.deepcopy(CONFIG), live=True, adapter=native)
            with patch("easytrade.user_session.fetch_quote", return_value=native_quote) as web:
                self.assertEqual(session.quote_source, "ths_client")
                self.assertIs(session.quote(), native_quote)
                web.assert_not_called()
                native.read_quote.side_effect = ValidationError("bad panel")
                with self.assertRaisesRegex(ValidationError, "bad panel"):
                    session.quote()
                web.assert_not_called()
                session.set_quote_source("tencent_web")
                session.quote()
                web.assert_called_once_with("159570.SZ")
            with self.assertRaises(ValidationError):
                session.set_quote_source("unknown")

    def test_wrong_native_symbol_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            native = Mock(read_quote=Mock(return_value=Quote("000967.SZ", Decimal("1"), Decimal("2"), datetime.now(timezone.utc))))
            session = UserSession(directory, copy.deepcopy(CONFIG), live=True, adapter=native)
            with self.assertRaisesRegex(ValidationError, "标的"):
                session.quote()
