"""Delayed-mode tests for bot_delayed.py (all existing scripts untouched).

Covers: endless by design, no tap limit / no --taps, default 150-200 ms,
presets, custom range, min<=max validation, delay bounds, fresh random
delay per click, delay-after-detection-before-click ordering, zero delay,
re-arm protection, Ctrl+C shutdown, statistics, and performance logging.
"""

import inspect
import io
import os
import random
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import types

import bot_delayed
from benchmark import Benchmark, TapRecord, now_ns, NS_PER_MS
from test_live_loop import ScriptedCapture, make_cfg, CLICKS

EVENTS = []  # ("sleep", seconds) / ("click", button)


def install_delayed_stubs(script, raise_at=None):
    CLICKS.clear()
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
    fake_clicker.click_button = lambda n, pts: (
        CLICKS.append(n), EVENTS.append(("click", n)))

    sys.modules["capture"] = fake_capture
    sys.modules["clicker"] = fake_clicker


def rapid_script(rounds):
    script, expected = [], []
    for r in range(rounds):
        t = (r % 6) + 1
        expected.append(t)
        script += [t, t, None]
    return script, expected


def run_game(script, budget=6000, min_delay_ms=150.0, max_delay_ms=200.0,
             raise_at=None, no_sleep=False):
    install_delayed_stubs(script, raise_at=raise_at)
    stats = {}
    buf = io.StringIO()
    sleep_patch = (patch("time.sleep",
                         side_effect=lambda s: EVENTS.append(("sleep", s)))
                   if no_sleep else _nullcontext())
    with sleep_patch:
        with redirect_stdout(buf):
            rc = bot_delayed.run_delayed(
                make_cfg(), min_delay_ms=min_delay_ms,
                max_delay_ms=max_delay_ms, frame_budget=budget,
                stats=stats)
    assert rc == 0
    return list(CLICKS), stats, buf.getvalue()


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestEndlessByDesign(unittest.TestCase):
    def test_no_tap_limit_parameter(self):
        params = inspect.signature(bot_delayed.run_delayed).parameters
        self.assertNotIn("tap_limit", params)
        self.assertNotIn("taps", params)
        self.assertNotIn("endless", params)

    def test_no_taps_option(self):
        actions = bot_delayed.build_parser()._actions
        flags = [o for a in actions for o in a.option_strings]
        self.assertNotIn("--taps", flags)
        self.assertNotIn("--endless", flags)
        self.assertNotIn("--min-interval", flags)
        with self.assertRaises(SystemExit):
            bot_delayed.build_parser().parse_args(["--taps", "5"])

    def test_runs_past_ten_without_stopping(self):
        script, expected = rapid_script(15)
        with patch.object(bot_delayed.random, "uniform",
                          return_value=1.0):
            clicks, stats, _ = run_game(script, budget=2000)
        self.assertEqual(clicks, expected)
        self.assertEqual(stats["taps"], 15)


class TestDelayMenu(unittest.TestCase):
    def run_menu(self, inputs):
        buf = io.StringIO()
        with patch("builtins.input", side_effect=list(inputs)):
            with redirect_stdout(buf):
                result = bot_delayed.run_delay_menu()
        return result, buf.getvalue()

    def test_default_delay_150_200(self):
        result, out = self.run_menu(["", ""])
        self.assertEqual(result, (150.0, 200.0, "150\u2013200 ms"))
        self.assertIn("Delayed Mode", out)
        self.assertIn("Mode: ENDLESS", out)
        self.assertIn("Delay after detection: 150\u2013200 ms", out)

    def test_preset_ranges(self):
        for choice, lo, hi in (("1", 150.0, 200.0), ("2", 150.0, 250.0),
                               ("3", 200.0, 250.0)):
            result, _ = self.run_menu([choice, "y"])
            self.assertEqual(result[:2], (lo, hi))

    def test_custom_range(self):
        result, out = self.run_menu(["4", "120", "180", "y"])
        self.assertEqual(result[:2], (120.0, 180.0))
        self.assertIn("Delay after detection: 120\u2013180 ms", out)

    def test_min_must_not_exceed_max(self):
        result, out = self.run_menu(["4", "300", "100",
                                     "100", "300", "y"])
        self.assertEqual(result[:2], (100.0, 300.0))
        self.assertIn("minimum delay must be <= maximum delay", out)

    def test_zero_delay_option(self):
        result, out = self.run_menu(["5", "y"])
        self.assertEqual(result, (0.0, 0.0, "No delay"))
        self.assertIn("Delay after detection: No delay", out)

    def test_decline_exits(self):
        result, _ = self.run_menu(["1", "n"])
        self.assertIsNone(result)

    def test_ctrl_c_exits_cleanly(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                bot_delayed.run_delay_menu()

    def test_no_tap_questions_in_menu(self):
        _, out = self.run_menu(["1", "y"])
        self.assertNotIn("taps", out.lower().replace("delayed", ""))


class TestDelayBehavior(unittest.TestCase):
    def test_generated_delay_inside_range(self):
        random.seed(42)
        for _ in range(200):
            d = bot_delayed.generate_delay_ms(150.0, 200.0)
            self.assertGreaterEqual(d, 150.0)
            self.assertLessEqual(d, 200.0)

    def test_exact_delay_when_min_equals_max(self):
        for _ in range(20):
            self.assertEqual(bot_delayed.generate_delay_ms(200.0, 200.0),
                             200.0)

    def test_zero_delay_generates_zero(self):
        self.assertEqual(bot_delayed.generate_delay_ms(0.0, 0.0), 0.0)
        self.assertEqual(bot_delayed.wait_delay_ms(0.0), 0.0)

    def test_fresh_random_delay_per_click(self):
        script, _ = rapid_script(3)
        with patch.object(bot_delayed.random, "uniform",
                          side_effect=[163.0, 194.0, 151.0]) as m:
            clicks, stats, _ = run_game(script, budget=500)
        self.assertEqual(len(clicks), 3)
        self.assertEqual(m.call_count, 3)
        self.assertEqual(stats["delays"], [163.0, 194.0, 151.0])

    def test_delay_occurs_after_detection_before_click(self):
        script, _ = rapid_script(4)
        with patch.object(bot_delayed.random, "uniform",
                          return_value=175.0):
            clicks, stats, _ = run_game(script, budget=800, no_sleep=True)
        self.assertEqual(len(clicks), 4)
        # Every sleep is immediately followed by its click: the wait sits
        # strictly between detection and the click dispatch.
        self.assertEqual(len(EVENTS), 8)
        for i in range(0, 8, 2):
            kind, val = EVENTS[i]
            self.assertEqual(kind, "sleep")
            self.assertAlmostEqual(val, 0.175)
            self.assertEqual(EVENTS[i + 1][0], "click")

    def test_no_delay_means_no_sleep(self):
        script, _ = rapid_script(3)
        clicks, stats, _ = run_game(script, budget=500, min_delay_ms=0.0,
                                    max_delay_ms=0.0, no_sleep=True)
        self.assertEqual(len(clicks), 3)
        self.assertEqual(stats["delays"], [0.0, 0.0, 0.0])
        self.assertEqual([e for e in EVENTS if e[0] == "sleep"], [])

    def test_rearm_protection_active(self):
        script = [None] * 2
        expected = []
        for r in range(12):
            t = (r * 2 % 6) + 1
            expected.append(t)
            script += [t] * 8 + [None]
        with patch.object(bot_delayed.random, "uniform", return_value=1.0):
            clicks, _, _ = run_game(script, budget=3000)
        self.assertEqual(clicks, expected)


class TestShutdownAndStats(unittest.TestCase):
    def test_ctrl_c_exits_cleanly(self):
        script, _ = rapid_script(15)
        buf = io.StringIO()
        with patch.object(bot_delayed.random, "uniform", return_value=1.0):
            install_delayed_stubs(script, raise_at=25)
            stats = {}
            with redirect_stdout(buf):
                rc = bot_delayed.run_delayed(make_cfg(), frame_budget=6000,
                                             stats=stats)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertGreater(stats["taps"], 0)
        self.assertLess(stats["taps"], 15)
        self.assertIn("ENDLESS DELAYED BOT STOPPED", stats["report"])
        self.assertIn("Stopped by user (Ctrl+C).", out)
        self.assertNotIn("Traceback", out)

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

    def test_statistics_correct(self):
        delays = [163.0, 194.0, 151.0, 181.0]
        report = bot_delayed.format_delayed_report(self.make_bench(4),
                                                   delays, "150\u2013200 ms")
        for needle in ("ENDLESS DELAYED BOT STOPPED",
                       "Total taps:\n4",
                       "Total runtime:",
                       "Average delay after detection:\n172.2 ms",
                       "Minimum generated delay:\n151.0 ms",
                       "Maximum generated delay:\n194.0 ms",
                       "Total intentional delay:\n0.689 seconds",
                       "Average detection time:",
                       "Total detection + click processing:"):
            self.assertIn(needle, report)

    def test_performance_logging(self):
        script, _ = rapid_script(3)
        install_delayed_stubs(script)
        with patch.object(bot_delayed.random, "uniform",
                          side_effect=[163.0, 194.0, 151.0]):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "delayed.log")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = bot_delayed.run_delayed(
                        make_cfg(), perf_log_path=p, frame_budget=500)
                self.assertEqual(rc, 0)
                text = open(p).read()
        self.assertIn("ENDLESS DELAYED BOT STOPPED", text)
        self.assertIn("delay_ms", text)
        self.assertIn("163.000", text)
        self.assertIn("detect_to_click_ms", text)

    def test_diagnostic_flags_skip_bot(self):
        buf = io.StringIO()
        with patch("builtins.input",
                   side_effect=AssertionError("menu shown!")):
            with patch.object(bot_delayed, "load_config",
                              return_value=make_cfg()):
                with patch.object(bot_delayed.bot_impl, "run_test",
                                  return_value=0) as rt:
                    with redirect_stdout(buf):
                        rc = bot_delayed.main(["--test"])
        self.assertEqual(rc, 0)
        rt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
