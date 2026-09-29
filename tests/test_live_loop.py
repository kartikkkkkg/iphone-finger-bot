"""End-to-end live-loop safety test with stubbed macOS modules.

Injects fake `capture`/`clicker` modules into sys.modules, then drives
bot.run_live() with a scripted frame sequence. Verifies:
  - exactly 10 clicks for 10 rounds, never an 11th
  - no click before the first orange target appears
  - no double-click when the same frame is seen twice (re-arm guard)
  - same button two rounds in a row still gets clicked (clear observed)

Runs on any OS: start_esc_watcher is a no-op off macOS.
"""

import sys
import types
import unittest

import cv2
import numpy as np

import bot
import detector as real_detector

W, H = 480, 360
CENTERS = [(90, 90), (240, 90), (390, 90), (90, 250), (240, 250), (390, 250)]

CLICKS = []


def render(target):
    frame = np.full((H, W, 3), (128, 30, 180), dtype=np.uint8)
    for i, (x, y) in enumerate(CENTERS, start=1):
        color = (0, 140, 255) if i == target else (150, 40, 200)
        cv2.circle(frame, (x, y), 40, color, -1)
    return frame


class ScriptedCapture:
    """Yields frames from a script of targets; None target = no orange."""

    def __init__(self, script):
        self.script = script
        self.i = 0
        self.window = {"bounds": {"x": 0, "y": 0, "width": W, "height": H}}

    @property
    def scale(self):
        return 1.0

    def capture_game_region(self):
        t = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return render(t)


def make_cfg():
    return {
        "game_region": {"x": 0, "y": 0, "width": W, "height": H},
        "window_bounds_at_calibration": {"x": 0, "y": 0,
                                         "width": W, "height": H},
        "buttons": {str(i + 1): {"x": float(x), "y": float(y)}
                    for i, (x, y) in enumerate(CENTERS)},
        "roi_size": 100,
        "orange_hsv": {"h_min": 10, "h_max": 24, "s_min": 90, "v_min": 90},
        "min_score": 0.22,
        "ambiguity_margin": 0.08,
    }


def install_stubs(script):
    CLICKS.clear()
    fake_capture = types.ModuleType("capture")
    fake_capture.check_screen_recording_permission = lambda: True
    fake_capture.make_capture = lambda cfg, backend="quartz": \
        ScriptedCapture(script)

    fake_clicker = types.ModuleType("clicker")
    fake_clicker.check_accessibility_permission = lambda: True
    fake_clicker.compute_click_points = lambda cfg, bounds, scale: \
        {i: (0.0, 0.0) for i in range(1, 7)}
    fake_clicker.click_button = lambda n, pts: CLICKS.append(n)

    sys.modules["capture"] = fake_capture
    sys.modules["clicker"] = fake_clicker


class TestLiveLoop(unittest.TestCase):
    def run_game(self, script, budget=5000):
        install_stubs(script)
        rc = bot.run_live(make_cfg(), debug=False, perf_log_path=None,
                          backend="quartz", frame_budget=budget)
        self.assertEqual(rc, 0)
        return list(CLICKS)

    def test_ten_rounds_ten_clicks(self):
        # 3 idle frames, then 10 rounds: target visible x2, clear x1.
        script = [None] * 3
        expected = []
        for r in range(10):
            t = (r % 6) + 1
            expected.append(t)
            script += [t, t, None]
        clicks = self.run_game(script)
        self.assertEqual(clicks, expected)
        self.assertEqual(len(clicks), 10, "must be exactly 10 clicks")

    def test_no_double_click_on_repeated_frame(self):
        # Each target stays visible for 8 frames: still exactly one click
        # per round, because re-arm requires the orange to clear first.
        script = [None] * 2
        expected = []
        for r in range(10):
            t = (r * 2 % 6) + 1
            expected.append(t)
            script += [t] * 8 + [None]
        clicks = self.run_game(script)
        self.assertEqual(clicks, expected)

    def test_same_button_twice_in_a_row(self):
        script = [None] * 2
        for _ in range(10):
            script += [4, 4, None]
        clicks = self.run_game(script)
        self.assertEqual(clicks, [4] * 10)

    def test_never_clicks_without_orange(self):
        clicks = self.run_game([None] * 60, budget=50)
        self.assertEqual(clicks, [])


if __name__ == "__main__":
    unittest.main()
