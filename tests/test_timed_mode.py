"""Timed-mode tests for bot_timed.py (standalone; bot.py untouched).

Covers: minimum-interval enforcement, exactly 10 taps in default mode,
endless mode, Ctrl+C clean shutdown, timing statistics, and the CLI.
Uses the stubbed capture/clicker harness from test_live_loop.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import types

import bot_timed
from benchmark import Benchmark, TapRecord, now_ns, NS_PER_MS
from test_live_loop import (ScriptedCapture, install_stubs, make_cfg,
                            CLICKS)

CLICK_TIMES = []


def install_timed_stubs(script, raise_at=None):
    """Like test_live_loop.install_stubs but records click timestamps and
    optionally raises KeyboardInterrupt after `raise_at` frames."""
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


class TestWaitHelper(unittest.TestCase):
    def test_no_wait_for_first_tap(self):
        self.assertEqual(
            bot_timed.wait_for_min_interval(None, 275_000_000), 0)

    def test_no_wait_when_already_slow(self):
        # Previous click was 10 s ago: must return immediately, no sleep.
        t0 = now_ns()
        waited = bot_timed.wait_for_min_interval(t0 - 10_000_000_000,
                                                 275_000_000)
        self.assertEqual(waited, 0)
        self.assertLess((now_ns() - t0) / NS_PER_MS, 50)

    def test_waits_remaining_time_only(self):
        # Previous click 200 ms ago, minimum 275 ms -> waits ~75 ms.
        t_last = now_ns() - 200_000_000
        t0 = now_ns()
        waited = bot_timed.wait_for_min_interval(t_last, 275_000_000)
        elapsed_ms = (now_ns() - t_last) / NS_PER_MS
        self.assertGreaterEqual(elapsed_ms, 275)
        self.assertLess((now_ns() - t0) / NS_PER_MS, 200)
        self.assertGreater(waited, 0)

    def test_full_wait_from_scratch(self):
        t_last = now_ns()
        waited = bot_timed.wait_for_min_interval(t_last, 80_000_000)
        self.assertGreaterEqual((now_ns() - t_last) / NS_PER_MS, 80)
        self.assertGreaterEqual(waited / NS_PER_MS, 75)


class TestTimedLoop(unittest.TestCase):
    def run_game(self, script, budget=5000, endless=False,
                 min_interval_ms=60, raise_at=None):
        install_timed_stubs(script, raise_at=raise_at)
        stats = {}
        rc = bot_timed.run_timed(
            make_cfg(), perf_log_path=None, backend="quartz",
            frame_budget=budget, endless=endless,
            min_interval_ms=min_interval_ms, stats=stats)
        self.assertEqual(rc, 0)
        return list(CLICKS), stats

    def rapid_script(self, rounds):
        # Targets appear back-to-back: no idle frames between rounds.
        script, expected = [], []
        for r in range(rounds):
            t = (r % 6) + 1
            expected.append(t)
            script += [t, t, None]
        return script, expected

    def test_min_interval_enforced(self):
        script, _ = self.rapid_script(10)
        clicks, stats = self.run_game(script, min_interval_ms=60)
        self.assertEqual(len(clicks), 10)
        for iv in intervals_ms():
            self.assertGreaterEqual(
                iv, 55, "every click-to-click interval must honor the floor")
        # 9 intervals x 60 ms floor.
        self.assertGreaterEqual(stats["wait_ns"] / NS_PER_MS, 9 * 55)

    def test_exactly_ten_taps_default(self):
        script, _ = self.rapid_script(15)
        clicks, stats = self.run_game(script, endless=False,
                                      min_interval_ms=20)
        self.assertEqual(len(clicks), 10)
        self.assertEqual(stats["taps"], 10)

    def test_endless_mode(self):
        script, expected = self.rapid_script(15)
        clicks, stats = self.run_game(script, endless=True,
                                      min_interval_ms=20)
        self.assertEqual(clicks, expected)
        self.assertEqual(stats["taps"], 15)

    def test_endless_rearm_guard(self):
        script = [None] * 2
        expected = []
        for r in range(12):
            t = (r * 2 % 6) + 1
            expected.append(t)
            script += [t] * 8 + [None]
        clicks, _ = self.run_game(script, endless=True, min_interval_ms=20)
        self.assertEqual(clicks, expected)

    def test_no_wait_when_game_slower_than_cadence(self):
        # 60 idle frames between rounds >> 25 ms cadence: zero enforced wait.
        script, expected = [], []
        for r in range(5):
            t = (r % 6) + 1
            expected.append(t)
            script += [None] * 60 + [t, t, None]
        clicks, stats = self.run_game(script, endless=True,
                                      min_interval_ms=25)
        self.assertEqual(clicks, expected)
        self.assertEqual(stats["wait_ns"], 0)

    def test_ctrl_c_clean_shutdown(self):
        script, _ = self.rapid_script(15)
        clicks, stats = self.run_game(script, endless=True,
                                      min_interval_ms=20, raise_at=25)
        # Clean exit (rc 0), partial progress kept, report generated.
        self.assertGreater(len(clicks), 0)
        self.assertLess(len(clicks), 15)
        self.assertEqual(stats["taps"], len(clicks))
        self.assertIn("Total taps:", stats["report"])


class TestTimedReport(unittest.TestCase):
    def make_bench(self, n=10):
        b = Benchmark()
        base = 1_000_000_000
        step = 300 * NS_PER_MS
        for i in range(n):
            t_cap = base + i * step
            t_det0 = t_cap + NS_PER_MS
            t_det1 = t_det0 + 2 * NS_PER_MS
            t_click = t_det1
            t_done = t_click + int(0.1 * NS_PER_MS)
            b.add_tap(TapRecord(i + 1, t_cap, t_det0, t_det1,
                                t_click, t_done))
        b.mark_run_start(base - 100 * NS_PER_MS)
        b.mark_run_end(base + n * step)
        return b

    def test_report_contains_all_items(self):
        report = bot_timed.format_timed_report(
            self.make_bench(10), total_wait_ns=1_650_000_000,
            endless=False)
        for needle in ("Total taps:",
                       "Total runtime:",
                       "Average interval:",
                       "Minimum interval:",
                       "Maximum interval:",
                       "Average detection time:",
                       "Total detection + click processing:",
                       "Total enforced timing wait:",
                       "First-click -> tenth-click duration:"):
            self.assertIn(needle, report)
        self.assertIn("TIMED MODE COMPLETE", report)

    def test_endless_report_variant(self):
        report = bot_timed.format_timed_report(
            self.make_bench(7), total_wait_ns=900_000_000, endless=True)
        self.assertIn("TIMED ENDLESS MODE STOPPED", report)
        self.assertIn("First-click -> last-click duration:", report)
        self.assertIn("Total taps: 7", report)

    def test_wait_reported_accurately(self):
        report = bot_timed.format_timed_report(
            self.make_bench(10), total_wait_ns=2_475_000_000)
        self.assertIn("Total enforced timing wait: 2475.0 ms", report)

    def test_perf_log_compatible(self):
        b = self.make_bench(4)
        report = bot_timed.format_timed_report(b, total_wait_ns=0)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "timed.log")
            b.write_log(p, report=report)
            text = open(p).read()
        self.assertIn("TIMED MODE COMPLETE", text)
        self.assertIn("t_click_done_ns", text)


class TestTimedCLI(unittest.TestCase):
    def test_defaults(self):
        args = bot_timed.build_parser().parse_args([])
        self.assertFalse(args.endless)
        self.assertIsNone(args.perf_log)

    def test_endless_and_perf_log(self):
        args = bot_timed.build_parser().parse_args(
            ["--endless", "--perf-log", "timed.log"])
        self.assertTrue(args.endless)
        self.assertEqual(args.perf_log, "timed.log")


if __name__ == "__main__":
    unittest.main()
