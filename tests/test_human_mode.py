"""Human-mode tests for bot_human.py (all existing scripts untouched).

Covers: reaction-delay bounds and freshness, jitter bounds and scatter,
hold range, slow-tap behavior, profile menu, decline, Ctrl+C, endless
loop integration (jittered press-and-hold clicks), re-arm protection,
Ctrl+C shutdown, report statistics, perf logging, and diagnostics.
"""

import inspect
import io
import math
import os
import random
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import types

import bot_human
from benchmark import Benchmark, TapRecord, now_ns, NS_PER_MS
from test_live_loop import ScriptedCapture, make_cfg, CLICKS

CLICK_CALLS = []  # (x_pt, y_pt, hold_ms)
SLEEPS = []
EVENTS = []  # interleaved ("sleep", s) / ("click", x, y, hold)


def test_profile(**over):
    p = dict(bot_human.PROFILES["natural"])
    p.update(over)
    return p


FAST_PROFILE = test_profile(delay_mean=1.0, delay_sd=0.1, delay_tau=0.1,
                            delay_floor=0.0, hold_min_ms=1.0,
                            hold_max_ms=2.0, slow_tap_p=0.0)


def install_human_stubs(script, raise_at=None):
    CLICKS.clear()
    CLICK_CALLS.clear()
    SLEEPS.clear()
    EVENTS.clear()

    class MaybeRaisingCapture(ScriptedCapture):
        def __init__(self, *a):
            super().__init__(*a)
            self.calls = 0

        def capture_game_region(self):
            self.calls += 1
            if raise_at is not None and self.calls > raise_at:
                raise KeyboardInterrupt()
            return super().capture_game_region()

    fake_capture = types.ModuleType("capture")
    fake_capture.check_screen_recording_permission = lambda: True
    fake_capture.make_capture = lambda cfg, backend="quartz": \
        MaybeRaisingCapture(script)

    fake_clicker = types.ModuleType("clicker")
    fake_clicker.check_accessibility_permission = lambda: True
    fake_clicker.compute_click_points = lambda cfg, bounds, scale: \
        {i: (0.0, 0.0) for i in range(1, 7)}

    sys.modules["capture"] = fake_capture
    sys.modules["clicker"] = fake_clicker


def rapid_script(rounds):
    script, expected = [], []
    for r in range(rounds):
        t = (r % 6) + 1
        expected.append(t)
        script += [t, t, None]
    return script, expected


def run_game(script, profile, budget=6000, raise_at=None):
    install_human_stubs(script, raise_at=raise_at)
    stats = {}
    buf = io.StringIO()

    def _click(x, y, h):
        CLICKS.append((x, y))
        CLICK_CALLS.append((x, y, h))
        EVENTS.append(("click", x, y, h))

    def _sleep(s):
        SLEEPS.append(s)
        EVENTS.append(("sleep", s))

    with patch.object(bot_human, "human_click_at", side_effect=_click):
        with patch("time.sleep", side_effect=_sleep):
            with redirect_stdout(buf):
                rc = bot_human.run_human(make_cfg(), profile,
                                         frame_budget=budget, stats=stats)
    assert rc == 0
    return stats, buf.getvalue()


class TestHumanPrimitives(unittest.TestCase):
    def test_reaction_delay_respects_floor(self):
        p = test_profile()
        random.seed(7)
        for _ in range(300):
            d = bot_human.sample_reaction_delay_ms(p)
            self.assertGreaterEqual(d, p["delay_floor"])

    def test_reaction_delay_is_right_skewed_and_fresh(self):
        p = test_profile()
        random.seed(7)
        ds = [bot_human.sample_reaction_delay_ms(p) for _ in range(300)]
        self.assertGreater(len(set(ds)), 290)
        mean = sum(ds) / len(ds)
        # ex-Gaussian mean ~= mean + tau, above the plain normal mean
        self.assertGreater(mean, p["delay_mean"])
        self.assertLess(mean, p["delay_mean"] + p["delay_tau"] + 60)

    def test_hold_in_range(self):
        p = test_profile()
        random.seed(11)
        for _ in range(100):
            h = bot_human.sample_hold_ms(p)
            self.assertGreaterEqual(h, p["hold_min_ms"])
            self.assertLessEqual(h, p["hold_max_ms"])

    def test_jitter_clamped_and_scattered(self):
        p = test_profile()
        max_r = 17.5
        random.seed(13)
        pts = [bot_human.sample_jitter_px(p, max_r) for _ in range(300)]
        for dx, dy in pts:
            self.assertLessEqual(math.hypot(dx, dy), max_r + 1e-9)
        # Real scatter: not all zeros, spread around the center.
        self.assertGreater(len({(round(x, 3), round(y, 3))
                                for x, y in pts}), 250)
        mx = sum(x for x, _ in pts) / len(pts)
        my = sum(y for _, y in pts) / len(pts)
        self.assertLess(abs(mx), 3)
        self.assertLess(abs(my), 3)

    def test_slow_tap_probability(self):
        p = test_profile(slow_tap_p=1.0)
        for _ in range(20):
            s = bot_human.sample_slow_extra_ms(p)
            self.assertGreater(s, 0)
            self.assertLessEqual(s, p["slow_max_ms"])
        p0 = test_profile(slow_tap_p=0.0)
        for _ in range(20):
            self.assertEqual(bot_human.sample_slow_extra_ms(p0), 0.0)


class TestHumanMenu(unittest.TestCase):
    def run_menu(self, inputs):
        buf = io.StringIO()
        with patch("builtins.input", side_effect=list(inputs)):
            with redirect_stdout(buf):
                result = bot_human.run_human_menu()
        return result, buf.getvalue()

    def test_default_profile_natural(self):
        result, out = self.run_menu(["", ""])
        name, profile = result
        self.assertEqual(name, "natural")
        self.assertEqual(profile["label"], "Natural (recommended)")
        self.assertIn("Human Mode", out)
        self.assertIn("Profile: Natural (recommended)", out)

    def test_profile_presets(self):
        for choice, key in (("1", "natural"), ("2", "brisk"),
                            ("3", "aggressive")):
            result, _ = self.run_menu([choice, "y"])
            self.assertEqual(result[0], key)
            self.assertEqual(result[1], bot_human.PROFILES[key])

    def test_custom_profile(self):
        result, _ = self.run_menu(["4", "280", "40", "50", "210",
                                   "12", "70", "140", "0.1", "400",
                                   "900", "y"])
        name, profile = result
        self.assertEqual(name, "custom")
        self.assertEqual(profile["delay_mean"], 280.0)
        self.assertEqual(profile["jitter_sigma_px"], 12.0)
        self.assertEqual(profile["slow_tap_p"], 0.1)

    def test_decline(self):
        result, _ = self.run_menu(["1", "n"])
        self.assertIsNone(result)

    def test_invalid_retry(self):
        result, out = self.run_menu(["9", "oops", "2", "maybe", "y"])
        self.assertEqual(result[0], "brisk")
        self.assertGreater(out.count("Invalid input"), 2)

    def test_ctrl_c_propagates(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                bot_human.run_human_menu()


class TestHumanLoop(unittest.TestCase):
    def test_endless_no_tap_limit(self):
        params = inspect.signature(bot_human.run_human).parameters
        self.assertNotIn("tap_limit", params)
        flags = [o for a in bot_human.build_parser()._actions
                 for o in a.option_strings]
        self.assertNotIn("--taps", flags)

    def test_jittered_press_hold_clicks(self):
        script, expected = rapid_script(6)
        stats, _ = run_game(script, FAST_PROFILE, budget=1500)
        self.assertEqual(stats["taps"], 6)
        self.assertEqual(len(CLICK_CALLS), 6)
        max_r = 0.35 * 100 / 2.0  # roi 100, scale 1.0
        for x, y, hold in CLICK_CALLS:
            # Clicks scatter around the (0,0) stub center, never exactly
            # on it, always inside the clamp circle.
            self.assertLessEqual(math.hypot(x, y), max_r + 1e-9)
            self.assertGreaterEqual(hold, 1.0)
            self.assertLessEqual(hold, 2.0)
        self.assertGreater(len({(round(x, 4), round(y, 4))
                                for x, y, _ in CLICK_CALLS}), 1)
        for t in stats["tap_stats"]:
            self.assertGreaterEqual(t["delay_ms"], 0.0)

    def test_wait_happens_before_click(self):
        script, _ = rapid_script(3)
        stats, _ = run_game(script, FAST_PROFILE, budget=800)
        # Strict per-tap ordering: reaction wait -> press&hold click.
        # (The hold itself is inside the patched human_click_at.)
        self.assertEqual(len(EVENTS), 6)
        for i in range(0, 6, 2):
            self.assertEqual(EVENTS[i][0], "sleep")
            self.assertGreater(EVENTS[i][1], 0)
            self.assertEqual(EVENTS[i + 1][0], "click")

    def test_rearm_protection(self):
        script = [None] * 2
        expected = []
        for r in range(8):
            t = (r * 2 % 6) + 1
            expected.append(t)
            script += [t] * 8 + [None]
        stats, _ = run_game(script, FAST_PROFILE, budget=3000)
        self.assertEqual(stats["taps"], len(expected))

    def test_ctrl_c_clean_shutdown(self):
        script, _ = rapid_script(15)
        install_human_stubs(script, raise_at=25)
        stats = {}
        buf = io.StringIO()
        with patch.object(bot_human, "human_click_at",
                          side_effect=lambda x, y, h: None):
            with redirect_stdout(buf):
                rc = bot_human.run_human(make_cfg(), FAST_PROFILE,
                                         frame_budget=6000, stats=stats)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertGreater(stats["taps"], 0)
        self.assertIn("HUMAN MODE STOPPED", stats["report"])
        self.assertIn("Stopped by user (Ctrl+C).", out)
        self.assertNotIn("Traceback", out)


class TestHumanReportAndLog(unittest.TestCase):
    def make_bench(self, n=4):
        b = Benchmark()
        base = 1_000_000_000
        step = 300 * NS_PER_MS
        for i in range(n):
            t_cap = base + i * step
            t_det0 = t_cap + NS_PER_MS
            t_det1 = t_det0 + 2 * NS_PER_MS
            b.add_tap(TapRecord(i + 1, t_cap, t_det0, t_det1, t_det1,
                                t_det1 + int(0.1 * NS_PER_MS)))
        b.mark_run_start(base - 100 * NS_PER_MS)
        b.mark_run_end(base + n * step)
        return b

    def tap_stats(self):
        return [
            {"delay_ms": 280.5, "hold_ms": 90.0, "jx_px": 5.0,
             "jy_px": -3.0, "slow_ms": 0.0},
            {"delay_ms": 310.2, "hold_ms": 110.0, "jx_px": -7.0,
             "jy_px": 2.0, "slow_ms": 450.0},
            {"delay_ms": 295.0, "hold_ms": 80.0, "jx_px": 1.0,
             "jy_px": 1.0, "slow_ms": 0.0},
            {"delay_ms": 340.8, "hold_ms": 100.0, "jx_px": 0.0,
             "jy_px": 6.0, "slow_ms": 0.0},
        ]

    def test_report_fields(self):
        report = bot_human.format_human_report(self.make_bench(4),
                                               self.tap_stats())
        for needle in ("HUMAN MODE STOPPED",
                       "Total taps:\n4",
                       "Total runtime:",
                       "Average reaction delay:",
                       "Minimum reaction delay:",
                       "Maximum reaction delay:",
                       "Average press-and-hold:",
                       "Average tap offset from center:",
                       "Slow taps:\n1",
                       "Total intentional delay:",
                       "Average detection time:",
                       "Total detection + click processing:"):
            self.assertIn(needle, report)

    def test_performance_logging(self):
        script, _ = rapid_script(3)
        install_human_stubs(script)
        with patch.object(bot_human, "human_click_at",
                          side_effect=lambda x, y, h: None):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "human.log")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = bot_human.run_human(
                        make_cfg(), FAST_PROFILE, perf_log_path=p,
                        frame_budget=500)
                self.assertEqual(rc, 0)
                with open(p) as fh:
                    text = fh.read()
        self.assertIn("HUMAN MODE STOPPED", text)
        for col in ("delay_ms", "hold_ms", "jitter_dx_px", "jitter_dy_px",
                    "slow_extra_ms", "detect_to_click_ms"):
            self.assertIn(col, text)

    def test_diagnostic_skips_menu(self):
        with patch("builtins.input",
                   side_effect=AssertionError("menu shown!")):
            with patch.object(bot_human, "load_config",
                              return_value=make_cfg()):
                with patch.object(bot_human.bot_impl, "run_test",
                                  return_value=0) as rt:
                    rc = bot_human.main(["--test"])
        self.assertEqual(rc, 0)
        rt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
