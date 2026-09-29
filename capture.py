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

    # -- low-level ---------------------------------------------------------
    def _capture_window_pixels(self):
        from Quartz import (CGWindowListCreateImage, CGRectNull,
                            kCGWindowListOptionIncludingWindow,
                            kCGWindowImageDefault, CGImageGetWidth,
                            CGImageGetHeight, CGImageGetDataProvider,
                            CGDataProviderCopyData)
        cgimg = CGWindowListCreateImage(
            CGRectNull, kCGWindowListOptionIncludingWindow,
            self.window["id"], kCGWindowImageDefault)
        if cgimg is None:
            return None
        w = CGImageGetWidth(cgimg)
        h = CGImageGetHeight(cgimg)
        data = CGDataProviderCopyData(CGImageGetDataProvider(cgimg))
        # CGWindowListCreateImage yields 32-bit BGRA (premultiplied-first,
        # little-endian) -> bytes are B,G,R,A. Take BGR for OpenCV.
        buf = np.frombuffer(data, dtype=np.uint8)
        expected = w * h * 4
        if buf.size < expected:
            return None
        bgra = buf[:expected].reshape(h, w, 4)
        return bgra[:, :, :3].copy()  # BGR, contiguous

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
        import cv2
        mon = {"left": int(rect_points["x"]), "top": int(rect_points["y"]),
               "width": int(rect_points["width"]),
               "height": int(rect_points["height"])}
        shot = self._mss.grab(mon)
        frame = np.asarray(shot)[:, :, :3]  # BGRA -> BGR
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

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
