"""Screen capture backends. Modular: the detector only ever sees a BGR frame.

Backends:
  "quartz"    - Direct CoreGraphics window capture of the iPhone Mirroring
                window (CGWindowListCreateImage, kCGWindowListOptionIncludingWindow).
                Captures the window itself even if briefly occluded. Preferred.
  "mss"       - Region grab of the game rectangle via the `mss` package.
                Fallback if Quartz window capture is unavailable.
  "synthetic" - Generates fake game frames (random orange target) for testing
                the pipeline on any OS. NEVER used in live mode.

Public interface:
    capture = make_capture(cfg, backend="quartz")
    frame = capture.capture_game_region()   # HxWx3 BGR uint8, hot path
    frame = capture.capture_frame()         # full mirroring window (calibration)

Coordinate spaces:
    - Window bounds / game_region: macOS Quartz DISPLAY POINTS (origin top-left).
    - Returned frames: device pixels. scale = frame_width_px / region_width_pt.
    - Button centers in config: capture pixels relative to game_region origin.
"""

import sys

import numpy as np

IS_MACOS = sys.platform == "darwin"

MIRRORING_OWNER_HINTS = ("iPhone Mirroring",)


# ---------------------------------------------------------------------------
# Window discovery (macOS)
# ---------------------------------------------------------------------------

def list_windows():
    """Return on-screen window infos (macOS only)."""
    if not IS_MACOS:
        raise RuntimeError("list_windows() requires macOS")
    from Quartz import (CGWindowListCopyWindowInfo,
                        kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
    return CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                      kCGNullWindowID)


def find_iphone_mirroring_window(verbose=False):
    """Find the iPhone Mirroring window. Returns dict or None."""
    if not IS_MACOS:
        return None
    best = None
    for w in list_windows():
        owner = w.get("kCGWindowOwnerName", "") or ""
        name = w.get("kCGWindowName", "") or ""
        if not any(hint.lower() in owner.lower() for hint in MIRRORING_OWNER_HINTS):
            continue
        if w.get("kCGWindowLayer", 0) != 0:
            continue
        bounds = w.get("kCGWindowBounds", {})
        area = bounds.get("Width", 0) * bounds.get("Height", 0)
        if verbose:
            print(f"  candidate: id={w.get('kCGWindowNumber')} owner={owner!r} "
                  f"name={name!r} bounds={bounds}")
        # iPhone Mirroring is a tall portrait window; pick the largest match.
        if best is None or area > best["_area"]:
            best = {"id": w["kCGWindowNumber"], "owner": owner, "name": name,
                    "bounds": {"x": bounds.get("X", 0), "y": bounds.get("Y", 0),
                               "width": bounds.get("Width", 0),
                               "height": bounds.get("Height", 0)},
                    "_area": area}
    if best is not None:
        best.pop("_area", None)
    return best


# ---------------------------------------------------------------------------
# Permissions (macOS)
# ---------------------------------------------------------------------------

def check_screen_recording_permission():
    """True/False, or None if it cannot be determined on this OS."""
    if not IS_MACOS:
        return None
    try:
        from Quartz import CGPreflightScreenCaptureAccess
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return None


def request_screen_recording_permission():
    """Pops the system Screen Recording prompt (macOS 11+). Returns granted?"""
    if not IS_MACOS:
        return False
    try:
        from Quartz import CGRequestScreenCaptureAccess
        return bool(CGRequestScreenCaptureAccess())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Pixel-buffer decoding (pure function: unit-testable on any OS)
# ---------------------------------------------------------------------------

def decode_frame_buffer(buf, width, height, row_bytes, bytes_per_pixel, order):
    """Convert a raw image byte buffer to a contiguous BGR uint8 frame.

    buf:             bytes-like of length >= row_bytes * height.
    width/height:    image dimensions in pixels.
    row_bytes:       ACTUAL bytes per row (stride). May exceed
                     width * bytes_per_pixel due to row padding — this is
                     the value CoreGraphics reports, never assumed.
    bytes_per_pixel: 3 or 4.
    order:           'bgra' | 'rgba' | 'argb' | 'bgr' | 'rgb'.

    Returns (height, width, 3) contiguous BGR uint8 for OpenCV.
    """
    raw = np.frombuffer(buf, dtype=np.uint8, count=row_bytes * height)
    rows = raw.reshape(height, row_bytes)          # strided view, no copy
    px = rows[:, :width * bytes_per_pixel].reshape(
        height, width, bytes_per_pixel)            # strip row padding
    if order == "bgra":
        bgr = px[:, :, :3]
    elif order == "rgba":
        bgr = px[:, :, [2, 1, 0]]
    elif order == "argb":
        bgr = px[:, :, [3, 2, 1]]
    elif order == "bgr":
        bgr = px
    elif order == "rgb":
        bgr = px[:, :, ::-1]
    else:
        raise ValueError(f"unknown pixel order: {order!r}")
    return np.ascontiguousarray(bgr)               # single required copy


def pixel_order_from_bitmap_info(bitmap_info, bytes_per_pixel):
    """Map a CGImage bitmapInfo value to a decode_frame_buffer order string.

    NOTE: kCGBitmapByteOrderDefault is treated as little-endian (correct for
    Intel/Apple Silicon Macs, which is every Mac this bot can run on). The
    one-time capture diagnostics print the raw bitmapInfo hex so a wrong
    assumption here is immediately visible.
    """
    if bytes_per_pixel == 3:
        return "bgr"  # 24-bit window captures are byte-order BGR in practice
    if bytes_per_pixel != 4:
        raise ValueError(f"unsupported bytes_per_pixel={bytes_per_pixel}")
    # Imported lazily so this stays importable off macOS for tests.
    try:
        from Quartz import (kCGBitmapAlphaInfoMask, kCGImageAlphaFirst,
                            kCGImageAlphaPremultipliedFirst,
                            kCGImageAlphaNoneSkipFirst, kCGBitmapByteOrderMask,
                            kCGBitmapByteOrder32Big)
        alpha = bitmap_info & kCGBitmapAlphaInfoMask
        order32 = bitmap_info & kCGBitmapByteOrderMask
        big = (order32 == kCGBitmapByteOrder32Big)
        alpha_first = alpha in (kCGImageAlphaFirst,
                                kCGImageAlphaPremultipliedFirst,
                                kCGImageAlphaNoneSkipFirst)
    except Exception:
        # Non-macOS (tests): assume the common BGRA layout.
        return "bgra"
    if big:
        return "argb" if alpha_first else "rgba"
    return "bgra" if alpha_first else "rgba"


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class QuartzWindowCapture:
    """Direct CoreGraphics capture of the iPhone Mirroring window."""

    def __init__(self, cfg):
        if not IS_MACOS:
            raise RuntimeError("QuartzWindowCapture requires macOS")
        from Quartz import CGWindowListCreateImage  # noqa: F401  (import check)
        self.cfg = cfg
        self.window = find_iphone_mirroring_window()
        if self.window is None:
            raise RuntimeError(
                "Could not find the iPhone Mirroring window.\n"
                "Make sure iPhone Mirroring is open and the iPhone is connected.")
        self._scale = None  # measured lazily from the first frame
        self._format_logged = False

    # -- low-level ---------------------------------------------------------
    def _capture_window_pixels(self):
        from Quartz import (CGWindowListCreateImage, CGRectNull,
                            kCGWindowListOptionIncludingWindow,
                            kCGWindowImageDefault, CGImageGetWidth,
                            CGImageGetHeight, CGImageGetBytesPerRow,
                            CGImageGetBitsPerPixel, CGImageGetBitsPerComponent,
                            CGImageGetBitmapInfo, CGImageGetDataProvider,
                            CGDataProviderCopyData)
        cgimg = CGWindowListCreateImage(
            CGRectNull, kCGWindowListOptionIncludingWindow,
            self.window["id"], kCGWindowImageDefault)
        if cgimg is None:
            return None

        # NEVER assume bytes_per_row == width * 4. Read everything from the
        # CGImage itself.
        width = CGImageGetWidth(cgimg)
        height = CGImageGetHeight(cgimg)
        row_bytes = CGImageGetBytesPerRow(cgimg)
        bits_per_pixel = CGImageGetBitsPerPixel(cgimg)
        bits_per_component = CGImageGetBitsPerComponent(cgimg)
        bitmap_info = CGImageGetBitmapInfo(cgimg)
        bytes_per_pixel = bits_per_pixel // 8

        data = CGDataProviderCopyData(CGImageGetDataProvider(cgimg))
        if data is None or len(data) < row_bytes * height:
            return None

        order = pixel_order_from_bitmap_info(bitmap_info, bytes_per_pixel)
        frame = decode_frame_buffer(bytes(data), width, height, row_bytes,
                                    bytes_per_pixel, order)

        if not self._format_logged:
            self._format_logged = True
            print("Capture:")
            print(f"  width = {width}")
            print(f"  height = {height}")
            print(f"  bytes_per_row = {row_bytes}")
            print(f"  bytes_per_pixel = {bytes_per_pixel}")
            print(f"  bits_per_component = {bits_per_component}")
            print(f"  bitmap_info = 0x{bitmap_info:08x}")
            print(f"  pixel_order = {order}")
            print(f"  numpy_shape = {frame.shape}")
            print(f"  dtype = {frame.dtype}")
            print(f"  contiguous = {frame.flags['C_CONTIGUOUS']}")
            if row_bytes != width * bytes_per_pixel:
                print(f"  note: row stride has {row_bytes - width * bytes_per_pixel} "
                      f"padding bytes per row (handled, not assumed away)")
        return frame

    # -- public ------------------------------------------------------------
    @property
    def scale(self):
        """Device pixels per display point, measured from the window image."""
        if self._scale is None:
            frame = self._capture_window_pixels()
            if frame is None:
                raise RuntimeError("Window capture returned no image "
                                   "(Screen Recording permission?)")
            bw = self.window["bounds"]["width"]
            self._scale = frame.shape[1] / bw if bw else 2.0
        return self._scale

    def capture_frame(self):
        """Full mirroring-window frame (BGR). Used for calibration/debug."""
        return self._capture_window_pixels()

    def capture_game_region(self):
        """Hot path: only the calibrated game region (BGR)."""
        full = self._capture_window_pixels()
        if full is None:
            return None
        gr = self.cfg["game_region"]
        wb = self.window["bounds"]
        s = full.shape[1] / wb["width"] if wb["width"] else self.scale
        # game_region is in global points; convert to window-image pixels.
        x = int(round((gr["x"] - wb["x"]) * s))
        y = int(round((gr["y"] - wb["y"]) * s))
        w = int(round(gr["width"] * s))
        h = int(round(gr["height"] * s))
        x = max(0, min(x, full.shape[1] - 1))
        y = max(0, min(y, full.shape[0] - 1))
        w = max(1, min(w, full.shape[1] - x))
        h = max(1, min(h, full.shape[0] - y))
        return full[y:y + h, x:x + w].copy()


class MSSCapture:
    """Fallback: mss region grab of the game rectangle."""

    def __init__(self, cfg):
        if not IS_MACOS:
            raise RuntimeError("MSSCapture requires macOS")
        import mss  # noqa: F401
        self.cfg = cfg
        import mss as _mss
        self._mss = _mss.mss()
        self.window = find_iphone_mirroring_window()

    @property
    def scale(self):
        gr = self.cfg["game_region"]
        shot = self._grab(gr)
        return shot.shape[1] / gr["width"] if gr["width"] else 2.0

    def _grab(self, rect_points):
        mon = {"left": int(rect_points["x"]), "top": int(rect_points["y"]),
               "width": int(rect_points["width"]),
               "height": int(rect_points["height"])}
        shot = self._mss.grab(mon)
        # mss raw bytes are BGRA; dropping alpha already yields BGR.
        return np.ascontiguousarray(np.asarray(shot)[:, :, :3])

    def capture_frame(self):
        win = find_iphone_mirroring_window()
        if win is None:
            return None
        return self._grab(win["bounds"])

    def capture_game_region(self):
        return self._grab(self.cfg["game_region"])


class SyntheticCapture:
    """Fake game frames for pipeline testing on any OS. NEVER live."""

    def __init__(self, cfg, width=360, height=640, seed=0):
        self.cfg = cfg
        self.rng = np.random.default_rng(seed)
        self.width, self.height = width, height
        self.target = 1

    def _render(self):
        import cv2
        frame = np.full((self.height, self.width, 3), (128, 30, 180),
                        dtype=np.uint8)  # purple background
        r = 34
        for i in range(1, 7):
            b = self.cfg["buttons"][str(i)]
            color = (0, 140, 255) if i == self.target else (150, 40, 200)
            cv2.circle(frame, (int(b["x"]), int(b["y"])), r, color, -1)
            cv2.circle(frame, (int(b["x"]), int(b["y"])), r, (255, 255, 255), 2)
        return frame

    def capture_game_region(self):
        # Randomly move the target sometimes, like a live game would.
        if self.rng.random() < 0.5:
            self.target = int(self.rng.integers(1, 7))
        return self._render()

    def capture_frame(self):
        return self._render()


def make_capture(cfg, backend="quartz", **kwargs):
    if backend == "quartz":
        return QuartzWindowCapture(cfg)
    if backend == "mss":
        return MSSCapture(cfg)
    if backend == "synthetic":
        return SyntheticCapture(cfg, **kwargs)
    raise ValueError(f"unknown capture backend: {backend}")
