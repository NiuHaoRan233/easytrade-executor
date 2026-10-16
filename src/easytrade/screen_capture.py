"""Capture fresh screen pixels for a bounded region, including negative monitors.

Pillow's Windows ImageGrab captures the entire virtual desktop before cropping.
Identity checks need just a small caption; copy only those pixels with GDI.
This is a screen capture, so foreground/occlusion checks remain the caller's job.
"""


def _bitmap_pixels(dc, bitmap, width, height):
    import ctypes
    import struct
    from ctypes import wintypes

    read = ctypes.WinDLL("gdi32", use_last_error=True).GetDIBits
    read.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                     ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    read.restype = ctypes.c_int
    # Negative height requests top-down, 32-bit BGRX pixels without row padding.
    header = ctypes.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0))
    pixels = ctypes.create_string_buffer(width * height * 4)
    if read(int(dc), int(bitmap), 0, height, pixels, header, 0) != height:
        raise OSError("标题区域截图未完整取得")
    return pixels.raw


def capture_region(box):
    import win32con
    import win32gui
    from PIL import Image

    left, top, right, bottom = box
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ValueError("截图范围必须有正宽高")
    screen_handle = win32gui.GetDC(0)
    try:
        memory = win32gui.CreateCompatibleDC(screen_handle)
        try:
            bitmap = win32gui.CreateCompatibleBitmap(screen_handle, width, height)
            try:
                previous = win32gui.SelectObject(memory, bitmap)
                try:
                    win32gui.BitBlt(memory, 0, 0, width, height, screen_handle, left, top, win32con.SRCCOPY)
                finally:
                    win32gui.SelectObject(memory, previous)
                # GetDIBits requires the bitmap to be deselected from the DC.
                return Image.frombytes("RGB", (width, height), _bitmap_pixels(memory, bitmap, width, height), "raw", "BGRX")
            finally:
                win32gui.DeleteObject(bitmap)
        finally:
            win32gui.DeleteDC(memory)
    finally:
        win32gui.ReleaseDC(0, screen_handle)
