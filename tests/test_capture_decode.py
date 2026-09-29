"""Capture buffer decoding tests: stride/padding and pixel-order handling.

Regression test for the corrupted-capture bug: CoreGraphics row stride
(bytes_per_row) is NOT assumed equal to width * bytes_per_pixel.
"""

import unittest

import numpy as np

from capture import decode_frame_buffer


def make_padded_buffer(width, height, bytes_per_pixel, pad_per_row, order):
    """Build a raw buffer with known pixels + garbage row padding."""
    rng = np.random.default_rng(42)
    row_bytes = width * bytes_per_pixel + pad_per_row
    buf = bytearray(row_bytes * height)
    pixels = rng.integers(0, 256, size=(height, width, bytes_per_pixel),
                          dtype=np.uint8)
    for y in range(height):
        off = y * row_bytes
        buf[off:off + width * bytes_per_pixel] = pixels[y].tobytes()
        # garbage padding (must never leak into the decoded frame)
        for p in range(width * bytes_per_pixel, row_bytes):
            buf[off + p] = 0xFF
    return bytes(buf), row_bytes, pixels


class TestDecodeFrameBuffer(unittest.TestCase):
    def test_bgra_with_row_padding(self):
        w, h, bpp, pad = 5, 3, 4, 7
        buf, row_bytes, pixels = make_padded_buffer(w, h, bpp, pad, "bgra")
        frame = decode_frame_buffer(buf, w, h, row_bytes, bpp, "bgra")
        self.assertEqual(frame.shape, (h, w, 3))
        self.assertTrue(frame.flags["C_CONTIGUOUS"])
        self.assertEqual(frame.dtype, np.uint8)
        np.testing.assert_array_equal(frame, pixels[:, :, :3])

    def test_rgba_channel_swap(self):
        w, h, bpp, pad = 4, 2, 4, 3
        buf, row_bytes, pixels = make_padded_buffer(w, h, bpp, pad, "rgba")
        frame = decode_frame_buffer(buf, w, h, row_bytes, bpp, "rgba")
        np.testing.assert_array_equal(frame, pixels[:, :, [2, 1, 0]])

    def test_argb_channel_swap(self):
        w, h, bpp, pad = 4, 2, 4, 5
        buf, row_bytes, pixels = make_padded_buffer(w, h, bpp, pad, "argb")
        frame = decode_frame_buffer(buf, w, h, row_bytes, bpp, "argb")
        np.testing.assert_array_equal(frame, pixels[:, :, [3, 2, 1]])

    def test_no_padding_still_works(self):
        w, h, bpp, pad = 6, 4, 4, 0
        buf, row_bytes, pixels = make_padded_buffer(w, h, bpp, pad, "bgra")
        frame = decode_frame_buffer(buf, w, h, row_bytes, bpp, "bgra")
        np.testing.assert_array_equal(frame, pixels[:, :, :3])

    def test_padding_never_leaks_into_pixels(self):
        # All padding bytes are 0xFF; no decoded pixel may come from padding.
        w, h, bpp, pad = 3, 5, 4, 13
        buf, row_bytes, _ = make_padded_buffer(w, h, bpp, pad, "bgra")
        frame = decode_frame_buffer(buf, w, h, row_bytes, bpp, "bgra")
        self.assertEqual(frame.shape, (h, w, 3))
        # Re-decode with zeroed padding: identical result proves no leakage.
        raw = bytearray(buf)
        for y in range(h):
            off = y * row_bytes + w * bpp
            for p in range(pad):
                raw[off + p] = 0x00
        frame2 = decode_frame_buffer(bytes(raw), w, h, row_bytes, bpp, "bgra")
        np.testing.assert_array_equal(frame, frame2)

    def test_unknown_order_raises(self):
        with self.assertRaises(ValueError):
            decode_frame_buffer(b"\x00" * 64, 4, 4, 16, 4, "yuv420")


if __name__ == "__main__":
    unittest.main()
