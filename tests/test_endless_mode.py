"""Endless-mode tests: CLI flag, uncapped loop, re-arm guard, Ctrl+C stats.

Reuses the stubbed capture/clicker harness from test_live_loop (fake
modules injected into sys.modules; no macOS APIs touched).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bot
from benchmark import Benchmark, TapRecord, NS_PER_MS
from test_live_loop import install_stubs, make_cfg, CLICKS


class TestEndlessCLI(unittest.TestCase):
    def test_flag_defaults_off(self):
        self.assertFalse(bot.build_parser().parse_args([]).endless)

    def test_flag_parses(self):
        args = bot.build_parser().parse_args(["--endless"])
        self.assertTrue(args.endless)

    def test_flag_combines_with_debug_and_perf_log(self):
        args = bot.build_parser().parse_args(
            ["--endless", "--debug", "--perf-log", "p.log"])
        self.assertTrue(args.endless and args.debug)
        self.assertEqual(args.perf_log, "p.log")


class TestEndlessLoop(unittest.TestCase):
    def run_game(self, script, budget, endless):
        install_stubs(script)
        rc = bot.run_live(make_cfg(), debug=False, perf_log_path=None,
                          backend="quartz", frame_budget=budget,
                          endless=endless)
        self.assertEqual(rc, 0)
        return list(CLICKS)

    def fifteen_round_script(self):
        script = [None] * 3
        expected = []
        for r in range(15):
            t = (r % 6) + 1
            expected.append(t)
            script += [t, t, None]
        return script, expected

    def test_endless_clicks_past_ten(self):
        script, expected = self.fifteen_round_script()
        clicks = self.run_game(script, budget=5000, endless=True)
        self.assertEqual(clicks, expected)
        self.assertEqual(len(clicks), 15)

    def test_endless_rearm_guard(self):
        # Target held for 8 frames per round: still one click per round.
        script = [None] * 2
        expected = []
        for r in range(15):
            t = (r * 3 % 6) + 1
            expected.append(t)
            script += [t] * 8 + [None]
        clicks = self.run_game(script, budget=5000, endless=True)
        self.assertEqual(clicks, expected)

    def test_default_mode_still_caps_at_ten(self):
        # The 10-tap safety limit is untouched in default mode.
        script, _ = self.fifteen_round_script()
        clicks = self.run_game(script, budget=5000, endless=False)
        self.assertEqual(len(clicks), 10)

    def test_endless_never_clicks_without_orange(self):
        clicks = self.run_game([None] * 60, budget=50, endless=True)
        self.assertEqual(clicks, [])


class TestEndlessReport(unittest.TestCase):
    def make_bench(self, n=5):
        b = Benchmark()
        base = 1_000_000_000
        step = 200 * NS_PER_MS
        for i in range(n):
            t_cap = base + i * step
            t_det0 = t_cap + 1 * NS_PER_MS
            t_det1 = t_det0 + 2 * NS_PER_MS      # 2 ms detection
            t_click = t_det1 + int(0.4 * NS_PER_MS)  # 0.4 ms loop overhead
            t_done = t_click + int(0.1 * NS_PER_MS)  # 0.1 ms dispatch
            b.add_tap(TapRecord(i + 1, t_cap, t_det0, t_det1,
                                t_click, t_done))
        b.mark_run_start(base - 500 * NS_PER_MS)
        b.mark_run_end(base + n * step)
        return b

    def test_summary_stats(self):
        s = self.make_bench(5).summary()
        self.assertEqual(s["taps"], 5)
        self.assertAlmostEqual(s["avg_detect_ms"], 2.0, places=6)
        self.assertAlmostEqual(s["min_detect_to_click_ms"], 0.4, places=6)
        self.assertAlmostEqual(s["total_detect_ms"], 10.0, places=6)
        self.assertAlmostEqual(s["total_click_dispatch_ms"], 0.5, places=6)
        self.assertAlmostEqual(s["total_detect_plus_click_ms"], 10.5,
                               places=6)
        self.assertAlmostEqual(s["avg_interval_ms"], 200.0, places=6)
        self.assertAlmostEqual(s["min_interval_ms"], 200.0, places=6)
        self.assertAlmostEqual(s["max_interval_ms"], 200.0, places=6)
        self.assertAlmostEqual(s["total_runtime_s"], 1.5, places=6)

    def test_report_contains_all_ctrl_c_items(self):
        report = self.make_bench(5).format_endless_report()
        for needle in ("Total taps:",
                       "Total runtime:",
                       "Average interval:",
                       "Minimum interval:",
                       "Maximum interval:",
                       "Average detection time:",
                       "Fastest detection-to-click:",
                       "Total detection + click processing:"):
            self.assertIn(needle, report)

    def test_default_report_unchanged(self):
        # Default mode keeps its original report format.
        report = self.make_bench(5).format_report()
        self.assertIn("GAME COMPLETE", report)
        self.assertNotIn("ENDLESS MODE", report)

    def test_perf_log_endless_compatible(self):
        b = self.make_bench(3)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "performance.log")
            b.write_log(p, report=b.format_endless_report())
            text = open(p).read()
        self.assertIn("ENDLESS MODE STOPPED", text)
        self.assertIn("t_click_done_ns", text)
        self.assertIn("detect_to_click_ms", text)


if __name__ == "__main__":
    unittest.main()
