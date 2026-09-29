"""Unified-mode tests for bot_unified.py (bot.py/bot_timed.py untouched).

Covers: default 10 taps, --taps N, --endless, mutual exclusion, default and
custom min-interval, --min-interval 0, interval floor, no extra wait on slow
games, first-click never waits, Ctrl+C shutdown, re-arm protection,
perf logging, 10-tap safety, and CLI help.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import types

import bot_unified
from benchmark import Benchmark, TapRecord, now_ns, NS_PER_MS
from test_live_loop import ScriptedCapture, make_cfg, CLICKS

CLICK_TIMES = []


def install_unified_stubs(script, raise_at=None):
    """Stubbed capture/clicker: records click dispatch timestamps and can
    raise KeyboardInterrupt after `raise_at` frames."""
    CLICKS.clear()
    CLICK_TIMES.clear()

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

    def _click(n, pts):
        CLICKS.append(n)
        CLICK_TIMES.append(now_ns())

    fake_clicker.click_button = _click

    sys.modules["capture"] = fake_capture
    sys.modules["clicker"] = fake_clicker


def intervals_ms():
    return [(b - a) / NS_PER_MS
            for a, b in zip(CLICK_TIMES, CLICK_TIMES[1:])]


def rapid_script(rounds):
    script, expected = [], []
    for r in range(rounds):
        t = (r % 6) + 1
        expected.append(t)
        script += [t, t, None]
    return script, expected


def run_game(script, budget=6000, tap_limit=10, endless=False,
             min_interval_ms=275, raise_at=None):
    install_unified_stubs(script, raise_at=raise_at)
    stats = {}
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = bot_unified.run_unified(
            make_cfg(), tap_limit=tap_limit, endless=endless,
            min_interval_ms=min_interval_ms, frame_budget=budget,
            stats=stats)
    assert rc == 0
    return list(CLICKS), stats, buf.getvalue()


class TestTapModes(unittest.TestCase):
    def test_default_exactly_ten_taps(self):
        parser = bot_unified.build_parser()
        args = parser.parse_args([])
        label, limit, endless = bot_unified.resolve_mode(args, parser)
        self.assertEqual((label, limit, endless), ("10 taps", 10, False))
        script, _ = rapid_script(15)
        clicks, stats, _ = run_game(script, tap_limit=limit,
                                    endless=endless, min_interval_ms=20)
        self.assertEqual(len(clicks), 10)
        self.assertEqual(stats["taps"], 10)

    def test_taps_25(self):
        parser = bot_unified.build_parser()
        args = parser.parse_args(["--taps", "25"])
        label, limit, endless = bot_unified.resolve_mode(args, parser)
        self.assertEqual((label, limit, endless), ("25 taps", 25, False))
        script, _ = rapid_script(30)
        clicks, stats, _ = run_game(script, tap_limit=limit,
                                    endless=endless, min_interval_ms=20)
        self.assertEqual(len(clicks), 25)
        self.assertEqual(stats["taps"], 25)

    def test_endless_does_not_stop_at_ten(self):
        parser = bot_unified.build_parser()
        args = parser.parse_args(["--endless"])
        label, limit, endless = bot_unified.resolve_mode(args, parser)
        self.assertEqual((label, limit, endless), ("ENDLESS", None, True))
        script, expected = rapid_script(15)
        clicks, stats, _ = run_game(script, tap_limit=limit,
                                    endless=endless, min_interval_ms=20)
        self.assertEqual(clicks, expected)
        self.assertEqual(stats["taps"], 15)

    def test_taps_and_endless_mutually_exclusive(self):
        parser = bot_unified.build_parser()
        args = parser.parse_args(["--taps", "25", "--endless"])
        with self.assertRaises(SystemExit) as cm:
            bot_unified.resolve_mode(args, parser)
        self.assertEqual(cm.exception.code, 2)

    def test_taps_must_be_positive(self):
        parser = bot_unified.build_parser()
        for bad in ("0", "-3"):
            args = parser.parse_args(["--taps", bad])
            with self.assertRaises(SystemExit):
                bot_unified.resolve_mode(args, parser)

    def test_existing_ten_tap_safety_intact(self):
        # Targets keep coming past round 10; default mode must stop at 10.
        script, _ = rapid_script(20)
        clicks, stats, out = run_game(script, tap_limit=10, endless=False,
                                      min_interval_ms=20)
        self.assertEqual(len(clicks), 10)
        self.assertIn("Mode:\n10 taps", stats["report"])
        self.assertNotIn("Tap 11/", out)


class TestMinInterval(unittest.TestCase):
    def test_default_min_interval_275(self):
        # No flag -> sentinel None; effective default resolves to 275.
        args = bot_unified.build_parser().parse_args([])
        self.assertIsNone(args.min_interval)
        self.assertEqual(bot_unified.effective_min_interval_ms(args), 275)
        explicit = bot_unified.build_parser().parse_args(
            ["--min-interval", "0"])
        self.assertEqual(bot_unified.effective_min_interval_ms(explicit), 0)
        self.assertTrue(bot_unified.mode_flags_explicit(explicit))
        self.assertFalse(bot_unified.mode_flags_explicit(args))

    def test_negative_min_interval_rejected(self):
        parser = bot_unified.build_parser()
        args = parser.parse_args(["--min-interval", "-5"])
        with self.assertRaises(SystemExit):
            bot_unified.resolve_mode(args, parser)

    def test_min_interval_zero_disables_enforcement(self):
        script, _ = rapid_script(10)
        clicks, stats, _ = run_game(script, min_interval_ms=0)
        self.assertEqual(len(clicks), 10)
        self.assertEqual(stats["wait_ns"], 0)

    def test_custom_interval_works(self):
        for mi in (300, 500):
            parser = bot_unified.build_parser()
            args = parser.parse_args(["--min-interval", str(mi)])
            label, _, _ = bot_unified.resolve_mode(args, parser)
            self.assertEqual(label, "10 taps")
            self.assertEqual(args.min_interval, mi)

    def test_back_to_back_targets_respect_floor(self):
        script, _ = rapid_script(10)
        clicks, stats, _ = run_game(script, min_interval_ms=60)
        self.assertEqual(len(clicks), 10)
        for iv in intervals_ms():
            self.assertGreaterEqual(iv, 55)
        self.assertGreaterEqual(stats["wait_ns"] / NS_PER_MS, 9 * 55)

    def test_slow_game_gets_no_extra_wait(self):
        script, expected = [], []
        for r in range(5):
            t = (r % 6) + 1
            expected.append(t)
            script += [None] * 60 + [t, t, None]
        clicks, stats, _ = run_game(script, endless=True,
                                    min_interval_ms=25)
        self.assertEqual(clicks, expected)
        self.assertEqual(stats["wait_ns"], 0)

    def test_first_click_has_zero_enforced_wait(self):
        script, _ = rapid_script(1)
        clicks, stats, _ = run_game(script, tap_limit=1,
                                    min_interval_ms=275)
        self.assertEqual(len(clicks), 1)
        self.assertEqual(stats["wait_ns"], 0)


class TestShutdownAndRearm(unittest.TestCase):
    def test_ctrl_c_exits_cleanly_with_partial_stats(self):
        script, _ = rapid_script(15)
        clicks, stats, out = run_game(script, endless=True,
                                      min_interval_ms=20, raise_at=25)
        self.assertGreater(len(clicks), 0)
        self.assertLess(len(clicks), 15)
        self.assertEqual(stats["taps"], len(clicks))
        self.assertIn("ENDLESS MODE STOPPED", stats["report"])
        self.assertIn("Stopped by user (Ctrl+C).", out)

    def test_ctrl_c_limited_mode_report(self):
        script, _ = rapid_script(15)
        clicks, stats, _ = run_game(script, tap_limit=10, endless=False,
                                    min_interval_ms=20, raise_at=25)
        self.assertIn("RUN COMPLETE", stats["report"])
        self.assertIn("Mode:\n10 taps", stats["report"])

    def test_rearm_protection(self):
        script = [None] * 2
        expected = []
        for r in range(12):
            t = (r * 2 % 6) + 1
            expected.append(t)
            script += [t] * 8 + [None]
        clicks, _, _ = run_game(script, endless=True, min_interval_ms=20)
        self.assertEqual(clicks, expected)


class TestReportAndLog(unittest.TestCase):
    def make_bench(self, n=10):
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

    def test_report_shape(self):
        report = bot_unified.format_unified_report(
            self.make_bench(10), "10 taps", 275, 1_650_000_000,
            endless=False)
        for needle in ("RUN COMPLETE",
                       "Mode:\n10 taps",
                       "Minimum click interval:\n275 ms",
                       "Total taps:\n10",
                       "Total runtime:",
                       "First click \u2192 last click:",
                       "Average interval:",
                       "Minimum interval:",
                       "Maximum interval:",
                       "Average detection time:",
                       "Min detection:",
                       "Max detection:",
                       "Fastest detection-to-click:",
                       "Total detection + click processing:",
                       "Total enforced timing wait:\n1.650 seconds"):
            self.assertIn(needle, report)

    def test_endless_report_shape(self):
        report = bot_unified.format_unified_report(
            self.make_bench(7), "ENDLESS", 300, 900_000_000, endless=True)
        self.assertIn("ENDLESS MODE STOPPED", report)
        self.assertIn("Mode:\nENDLESS", report)
        self.assertIn("Minimum click interval:\n300 ms", report)
        self.assertIn("Total taps:\n7", report)

    def test_zero_tap_report(self):
        report = bot_unified.format_unified_report(
            Benchmark(), "10 taps", 275, 0, endless=False)
        self.assertIn("Total taps:\n0", report)

    def test_performance_logging(self):
        script, _ = rapid_script(5)
        install_unified_stubs(script)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "unified.log")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = bot_unified.run_unified(
                    make_cfg(), tap_limit=5, endless=False,
                    min_interval_ms=20, perf_log_path=p,
                    frame_budget=200)
            self.assertEqual(rc, 0)
            text = open(p).read()
        # Existing log format preserved: report + per-tap CSV.
        self.assertIn("RUN COMPLETE", text)
        self.assertIn("t_capture_ns", text)
        self.assertIn("detect_to_click_ms", text)
        # New fields travel inside the report section.
        self.assertIn("Mode:\n5 taps", text)
        self.assertIn("Minimum click interval:\n20 ms", text)
        self.assertIn("Total enforced timing wait:", text)


class TestCLIHelp(unittest.TestCase):
    def test_help_exposes_all_options(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                bot_unified.build_parser().parse_args(["--help"])
        self.assertEqual(cm.exception.code, 0)
        text = buf.getvalue()
        for needle in ("--taps", "--endless", "--min-interval",
                       "--perf-log", "--verify-coords", "--debug",
                       "--test", "--calibrate", "--list-windows",
                       "--save-capture", "--backend"):
            self.assertIn(needle, text)
        self.assertIn("10 taps", text)


if __name__ == "__main__":
    unittest.main()
