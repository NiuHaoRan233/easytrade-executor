"""Synthetic grids isolate geometry/column checks without downloading an OCR model."""
import unittest
from types import SimpleNamespace
import sys
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

from easytrade.tables import OcrTableReader
from easytrade.domain import ValidationError


def sample(scale=1, order=None):
    names = ["证券代码", "证券名称", "股票余额", "可用余额", "市价", "市值"]
    values = ["000001", "样例证券", "100", "100", "12.34", "1234.00"]
    order = order or list(range(6))
    width, height = 100, 40
    image = Image.new("RGB", (width * 6 + 1, height * 2 + 1), "white")
    draw = ImageDraw.Draw(image)
    for x in range(0, image.width, width):
        draw.line((x, 0, x, image.height - 1), fill="gray")
    for y in range(0, image.height, height):
        draw.line((0, y, image.width - 1, y), fill="gray")
    boxes, texts = [], []
    for row, entries in enumerate((names, values)):
        for column, index in enumerate(order):
            x, y = column * width + 15, row * height + 12
            boxes.append(np.array([[x,y],[x+65,y],[x+65,y+16],[x,y+16]], dtype=float) * scale)
            texts.append(entries[index])
    if scale != 1:
        image = image.resize((image.width * scale, image.height * scale))
    result = SimpleNamespace(txts=texts, scores=[.99] * len(texts), boxes=boxes)
    return image, result


class TableLayoutTests(unittest.TestCase):
    def test_configured_detection_expansion_keeps_boundary_checks(self):
        image, result = sample()
        engine = unittest.mock.Mock(return_value=result)
        factory = unittest.mock.Mock(return_value=engine)
        reader = OcrTableReader(unclip_ratio=1.2)
        with patch.dict(sys.modules, {"rapidocr": SimpleNamespace(RapidOCR=factory)}):
            self.assertEqual(reader.read(image, "positions", "now").rows[0]["position_quantity"], 100)
            result.boxes[8][1:3, 0] = 310
            with self.assertRaisesRegex(ValidationError, "跨列"):
                reader.read(image, "positions", "now")
        factory.assert_called_once_with(params={"Det.unclip_ratio": 1.2})

    def test_detection_expansion_rejects_invalid_configuration(self):
        for value in (True, "1.2", 0.9, 2.1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                OcrTableReader(unclip_ratio=value)

    def read(self, image, result):
        reader = OcrTableReader()
        with patch.object(reader, "_recognize", return_value=result):
            return reader.read(image, "positions", "now")

    def test_rescaled_and_reordered_columns_keep_exact_prices(self):
        for scale in (1, 2, 3):
            for order in (None, [4, 1, 0, 5, 2, 3]):
                with self.subTest(scale=scale, order=order):
                    snapshot = self.read(*sample(scale, order))
                    self.assertEqual(snapshot.rows[0]["current_price"], "12.34")
                    self.assertEqual(snapshot.rows[0]["market_value"], "1234.00")
                    self.assertEqual(snapshot.rows[0]["position_quantity"], 100)
                    self.assertFalse(snapshot.warnings)

    def test_optional_price_missing_clipped_low_confidence_or_merged_is_omitted(self):
        for scenario in ("missing-header", "low-score", "cross-column", "empty", "invalid", "cropped-viewport"):
            with self.subTest(scenario=scenario):
                image, result = sample()
                if scenario == "missing-header":
                    result.txts[4] = "市"
                elif scenario == "low-score":
                    result.scores[10] = .1
                elif scenario == "cross-column":
                    result.boxes[10][1:3, 0] = 510
                elif scenario == "empty":
                    result.txts.pop(10)
                    result.scores.pop(10)
                    result.boxes.pop(10)
                elif scenario == "invalid":
                    result.txts[10] = "12.3?"
                elif scenario == "cropped-viewport":
                    image = image.crop((0, 0, 450, image.height))
                snapshot = self.read(image, result)
                self.assertNotIn("current_price", snapshot.rows[0])
                if scenario != "cropped-viewport":
                    self.assertEqual(snapshot.rows[0]["market_value"], "1234.00")
                self.assertEqual(snapshot.rows[0]["position_quantity"], 100)
                self.assertTrue(snapshot.warnings)

    def test_lost_decimal_point_is_not_shown_as_a_valid_price(self):
        image, result = sample()
        result.txts[10] = "1234"
        snapshot = self.read(image, result)
        self.assertNotIn("current_price", snapshot.rows[0])
        self.assertNotIn("market_value", snapshot.rows[0])
        self.assertIn("不一致", snapshot.warnings[0])

    def test_display_rounding_and_zero_position_are_allowed(self):
        for qty, price, value in (("100", "12.34", "1234.30"), ("0", "12.34", "0.00")):
            image, result = sample()
            result.txts[8], result.txts[9], result.txts[10], result.txts[11] = qty, qty, price, value
            snapshot = self.read(image, result)
            self.assertEqual(snapshot.rows[0]["current_price"], price)
            self.assertFalse(snapshot.warnings)

    def test_critical_quantity_crossing_a_column_rejects_the_snapshot(self):
        image, result = sample()
        result.boxes[8][1:3, 0] = 310
        with self.assertRaisesRegex(ValidationError, "跨列"):
            self.read(image, result)
