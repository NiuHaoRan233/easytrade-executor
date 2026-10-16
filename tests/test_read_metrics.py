import unittest
from unittest.mock import patch

from easytrade.read_metrics import read_operation, read_step


class Reader:
    profile = {"console_diagnostics": "runtime/diagnostics.jsonl"}

    @read_operation("account")
    def read(self, fail=False):
        return self.nested(fail)

    @read_operation("nested")
    @read_step("ocr")
    def nested(self, fail):
        if fail:
            raise ValueError("read failed")
        return "fresh"


class TimingTests(unittest.TestCase):
    def test_nested_calls_emit_one_timing_and_restore_state_even_on_failure(self):
        for fail in (False, True):
            reader = Reader()
            with patch("easytrade.diagnostics.record_action") as record:
                if fail:
                    with self.assertRaisesRegex(ValueError, "read failed"):
                        reader.read(True)
                else:
                    self.assertEqual(reader.read(), "fresh")
            record.assert_called_once()
            fields = record.call_args.kwargs
            self.assertEqual(fields["operation"], "account")
            self.assertEqual(fields["success"], not fail)
            self.assertEqual(fields["steps"]["ocr"]["count"], 1)
            self.assertIsNone(reader._read_metrics)

    def test_unwritable_optional_timing_does_not_mask_result_or_read_error(self):
        with patch("easytrade.diagnostics.record_action", side_effect=OSError("disk unavailable")):
            self.assertEqual(Reader().read(), "fresh")
            with self.assertRaisesRegex(ValueError, "read failed"):
                Reader().read(True)
