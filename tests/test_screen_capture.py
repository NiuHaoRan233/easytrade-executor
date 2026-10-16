"""GDI lifecycle tests with fake DCs; never read the real desktop."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from easytrade.screen_capture import capture_region, _bitmap_pixels


class CaptureTests(unittest.TestCase):
    def test_bitmap_reader_converts_pywin32_handles_and_rejects_partial_copy(self):
        import struct
        class Handle:
            def __int__(self):
                return 99
        for rows in (2, 1):
            read = Mock(return_value=rows)
            with patch("ctypes.WinDLL", return_value=SimpleNamespace(GetDIBits=read), create=True):
                if rows == 2:
                    self.assertEqual(len(_bitmap_pixels(43, Handle(), 3, 2)), 24)
                else:
                    with self.assertRaises(OSError):
                        _bitmap_pixels(43, Handle(), 3, 2)
            args = read.call_args.args
            self.assertEqual(args[:4], (43, 99, 0, 2))
            self.assertEqual(struct.unpack("<IiiHHIIiiII", args[5].raw[:40])[:5], (40, 3, -2, 1, 32))

    def test_bounded_capture_and_cleanup_on_success_or_copy_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                gui = Mock()
                gui.GetDC.return_value = 42
                gui.CreateCompatibleDC.return_value = 43
                gui.CreateCompatibleBitmap.return_value = 99
                gui.SelectObject.return_value = "previous"
                if fail:
                    gui.BitBlt.side_effect = RuntimeError("copy failed")
                with patch.dict("sys.modules", {"win32gui": gui, "win32con": SimpleNamespace(SRCCOPY=7)}), patch("easytrade.screen_capture._bitmap_pixels", return_value=b"\x03\x02\x01\x00" * 6):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "copy failed"):
                            capture_region((-10, -5, -7, -3))
                    else:
                        image = capture_region((-10, -5, -7, -3))
                        self.assertEqual(image.size, (3, 2))
                        self.assertEqual(image.getpixel((0, 0)), (1, 2, 3))
                gui.BitBlt.assert_called_once_with(43, 0, 0, 3, 2, 42, -10, -5, 7)
                self.assertEqual(gui.SelectObject.call_args_list[-1].args, (43, "previous"))
                gui.DeleteObject.assert_called_once_with(99)
                gui.DeleteDC.assert_called_once_with(43)
                gui.ReleaseDC.assert_called_once_with(0, 42)

    def test_invalid_region_is_rejected_before_allocating_dc(self):
        gui = Mock()
        with patch.dict("sys.modules", {"win32gui": gui, "win32ui": Mock(), "win32con": Mock()}):
            with self.assertRaises(ValueError):
                capture_region((3, 0, 2, 1))
        gui.GetDC.assert_not_called()
