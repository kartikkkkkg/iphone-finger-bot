"""Native macOS mouse clicks via Quartz CGEvent (no PyAutoGUI).

Coordinate helpers are pure functions (unit-tested on any OS):
    capture_px_to_screen_point(region_origin_pt, px, py, scale) -> (x, y) points
    screen_point_to_capture_px(region_origin_pt, x, y, scale)  -> (px, py)

The actual click path (macOS only):
    click_at(x_pt, y_pt)            # CGEventCreateMouseEvent + CGEventPost
    click_button(n, click_points)   # click_points: {1: (x, y), ...} in points
"""

import sys

IS_MACOS = sys.platform == "darwin"


# ---------------------------------------------------------------------------
# Pure coordinate helpers (platform-independent)
# ---------------------------------------------------------------------------

def capture_px_to_screen_point(region_origin_pt, px, py, scale):
    """Capture pixels (relative to game_region origin) -> Quartz points."""
    ox, oy = region_origin_pt
    return (ox + px / scale, oy + py / scale)


def screen_point_to_capture_px(region_origin_pt, x, y, scale):
    """Quartz points -> capture pixels relative to game_region origin."""
    ox, oy = region_origin_pt
    return ((x - ox) * scale, (y - oy) * scale)


def compute_click_points(cfg, window_bounds_now, scale):
    """Map calibrated button centers to current screen points.

    Handles the iPhone Mirroring window having MOVED since calibration:
    the stored window_bounds_at_calibration is compared with the current
    bounds; the button geometry is shifted by the origin delta and scaled
    by the size ratio (normally 1.0).

    Returns {1: (x_pt, y_pt), ...}.
    """
    gr = cfg["game_region"]
    wb0 = cfg["window_bounds_at_calibration"]
    ox, oy = gr["x"], gr["y"]

    if wb0["width"] > 0 and window_bounds_now:
        dx = window_bounds_now["x"] - wb0["x"]
        dy = window_bounds_now["y"] - wb0["y"]
        sx = (window_bounds_now["width"] / wb0["width"]
              if wb0["width"] else 1.0)
        sy = (window_bounds_now["height"] / wb0["height"]
              if wb0["height"] else 1.0)
        ox += dx
        oy += dy
    else:
        sx = sy = 1.0

    points = {}
    for i in range(1, 7):
        b = cfg["buttons"][str(i)]
        px = b["x"] * sx
        py = b["y"] * sy
        points[i] = capture_px_to_screen_point((ox, oy), px, py, scale)
    return points


# ---------------------------------------------------------------------------
# macOS native click
# ---------------------------------------------------------------------------

def _require_macos():
    if not IS_MACOS:
        raise RuntimeError("Clicking requires macOS (Quartz CGEvent)")


def click_at(x_pt, y_pt):
    """Post a native left-click at Quartz display points. No sleeps."""
    _require_macos()
    from Quartz import (CGEventCreateMouseEvent, CGEventPost,
                        kCGEventLeftMouseDown, kCGEventLeftMouseUp,
                        kCGMouseButtonLeft, kCGHIDEventTap, CGPointMake)
    pt = CGPointMake(x_pt, y_pt)
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, pt,
                                   kCGMouseButtonLeft)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, pt,
                                 kCGMouseButtonLeft)
    # Down+up back-to-back: lowest possible dispatch latency. iPhone Mirroring
    # registers this as a tap; no artificial delay is inserted.
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)


def click_button(button_number, click_points):
    """Click button 1..6 using precomputed screen points."""
    if button_number not in click_points:
        raise ValueError(f"unknown button: {button_number}")
    x, y = click_points[button_number]
    click_at(x, y)


def get_mouse_position():
    """Current mouse position in Quartz points (for calibration)."""
    _require_macos()
    from Quartz import CGEventCreate, CGEventGetLocation
    loc = CGEventGetLocation(CGEventCreate(None))
    return (loc.x, loc.y)


def move_mouse(x_pt, y_pt):
    """Move (no click) the cursor - used by --verify-coords."""
    _require_macos()
    from Quartz import (CGEventCreateMouseEvent, CGEventPost,
                        kCGEventMouseMoved, kCGHIDEventTap, CGPointMake)
    ev = CGEventCreateMouseEvent(None, kCGEventMouseMoved,
                                 CGPointMake(x_pt, y_pt), 0)
    CGEventPost(kCGHIDEventTap, ev)


# ---------------------------------------------------------------------------
# Accessibility permission
# ---------------------------------------------------------------------------

def check_accessibility_permission():
    """True/False, or None if it cannot be determined."""
    if not IS_MACOS:
        return None
    try:
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    except Exception:
        return None


def accessibility_instructions():
    return (
        "ACCESSIBILITY PERMISSION REQUIRED for synthetic clicks.\n"
        "Enable it here:\n"
        "  System Settings -> Privacy & Security -> Accessibility\n"
        "  -> click '+' -> add your Terminal app (or the Python interpreter)\n"
        "     and toggle it ON.\n"
        "Then re-run the bot. The bot will NOT silently fail: it checks this\n"
        "permission before the game loop starts."
    )


def open_accessibility_settings():
    import subprocess
    subprocess.run(["open",
                    "x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_Accessibility"], check=False)
