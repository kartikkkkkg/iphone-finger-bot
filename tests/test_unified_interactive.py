"""Interactive-menu tests for bot_unified.py.

Covers: interactive default selection, limited tap selection, custom tap
count, endless selection, all interval presets, custom interval, zero
interval, invalid input retry, confirmation decline, Ctrl+C/EOF during
prompts, and explicit CLI arguments bypassing the menu.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bot_unified
from test_live_loop import make_cfg


def run_menu(inputs):
    """Drive run_interactive_menu with scripted stdin; returns
    (result, printed_output)."""
    buf = io.StringIO()
    with patch("builtins.input", side_effect=list(inputs)):
        with redirect_stdout(buf):
            result = bot_unified.run_interactive_menu()
    return result, buf.getvalue()


class TestInteractiveMenu(unittest.TestCase):
    def test_interactive_default_selection(self):
        # All Enters: Limited, 10 taps, 275 ms, start.
        result, out = run_menu(["", "", "", ""])
        self.assertEqual(result, ("10 taps", 10, False, 275.0))
        self.assertIn("iPhone Orange Button Bot", out)
        self.assertIn("Mode: Limited", out)
        self.assertIn("Taps: 10", out)
        self.assertIn("Minimum click interval: 275 ms", out)

    def test_limited_tap_selection(self):
        result, _ = run_menu(["1", "10", "1", "y"])
        self.assertEqual(result, ("10 taps", 10, False, 275.0))

    def test_custom_tap_count(self):
        result, out = run_menu(["1", "25", "1", "Y"])
        self.assertEqual(result, ("25 taps", 25, False, 275.0))
        self.assertIn("Taps: 25", out)

    def test_endless_selection(self):
        result, out = run_menu(["2", "1", ""])
        self.assertEqual(result, ("ENDLESS", None, True, 275.0))
        self.assertIn("Mode: Endless", out)
        self.assertIn("Taps: Endless", out)

    def test_interval_presets(self):
        for choice, expected in (("1", 275.0), ("2", 300.0),
                                 ("3", 350.0), ("4", 500.0),
                                 ("6", 0.0)):
            result, out = run_menu(["1", "10", choice, "y"])
            self.assertEqual(result[3], expected)
            self.assertIn(f"Minimum click interval: {expected:g} ms", out)

    def test_custom_interval(self):
        result, out = run_menu(["1", "10", "5", "412.5", "y"])
        self.assertEqual(result[3], 412.5)
        self.assertIn("Minimum click interval: 412.5 ms", out)

    def test_zero_interval_preset(self):
        result, _ = run_menu(["2", "6", "y"])
        self.assertEqual(result, ("ENDLESS", None, True, 0.0))

    def test_invalid_input_retry(self):
        # Bad mode, bad taps, bad interval, bad confirm -> all retried,
        # nothing silently chosen.
        result, out = run_menu(["3", "banana", "2",     # mode
                                "0", "-5", "x", "7",    # taps
                                "9", "0", "5", "abc", "-1", "350",  # interval
                                "maybe", "n"])          # confirm -> decline
        self.assertIsNone(result)  # declined at confirmation
        self.assertGreater(out.count("Invalid input"), 5)

    def test_confirmation_decline(self):
        result, out = run_menu(["1", "10", "1", "n"])
        self.assertIsNone(result)
        self.assertIn("Configuration", out)

    def test_ctrl_c_during_prompts_propagates(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                bot_unified.run_interactive_menu()

    def test_eof_during_prompts_propagates(self):
        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(EOFError):
                bot_unified.run_interactive_menu()


class TestMenuBypass(unittest.TestCase):
    def run_main(self, argv, menu_inputs=None):
        """Run main() with load_config/run_unified stubbed. menu_inputs=None
        means input() must never be called."""
        input_mock = (patch("builtins.input",
                            side_effect=AssertionError("menu shown!"))
                      if menu_inputs is None
                      else patch("builtins.input", side_effect=menu_inputs))
        buf = io.StringIO()
        with input_mock:
            with patch.object(bot_unified, "load_config",
                              return_value=make_cfg()):
                with patch.object(bot_unified, "run_unified",
                                  return_value=0) as ru:
                    with redirect_stdout(buf):
                        rc = bot_unified.main(argv)
        return rc, ru, buf.getvalue()

    def test_endless_flag_bypasses_menu(self):
        rc, ru, _ = self.run_main(["--endless"])
        self.assertEqual(rc, 0)
        kw = ru.call_args.kwargs
        self.assertEqual((kw["tap_limit"], kw["endless"],
                          kw["min_interval_ms"]), (None, True, 275))

    def test_taps_flag_bypasses_menu(self):
        rc, ru, _ = self.run_main(["--taps", "25"])
        self.assertEqual(rc, 0)
        kw = ru.call_args.kwargs
        self.assertEqual((kw["tap_limit"], kw["endless"],
                          kw["min_interval_ms"]), (25, False, 275))

    def test_min_interval_flag_bypasses_menu(self):
        rc, ru, _ = self.run_main(["--min-interval", "300"])
        self.assertEqual(rc, 0)
        kw = ru.call_args.kwargs
        self.assertEqual((kw["tap_limit"], kw["endless"],
                          kw["min_interval_ms"]), (10, False, 300.0))

    def test_endless_plus_interval_bypass(self):
        rc, ru, _ = self.run_main(["--endless", "--min-interval", "300"])
        self.assertEqual(rc, 0)
        kw = ru.call_args.kwargs
        self.assertEqual((kw["tap_limit"], kw["endless"],
                          kw["min_interval_ms"]), (None, True, 300.0))

    def test_zero_interval_bypass(self):
        rc, ru, _ = self.run_main(["--min-interval", "0"])
        self.assertEqual(rc, 0)
        self.assertEqual(ru.call_args.kwargs["min_interval_ms"], 0.0)

    def test_no_flags_shows_menu_and_runs(self):
        rc, ru, out = self.run_main([], menu_inputs=["", "", "", ""])
        self.assertEqual(rc, 0)
        kw = ru.call_args.kwargs
        self.assertEqual((kw["tap_limit"], kw["endless"],
                          kw["min_interval_ms"]), (10, False, 275.0))
        self.assertIn("iPhone Orange Button Bot", out)

    def test_menu_decline_exits_without_running(self):
        rc, ru, _ = self.run_main([], menu_inputs=["1", "10", "1", "n"])
        self.assertEqual(rc, 0)
        ru.assert_not_called()

    def test_ctrl_c_in_menu_exits_cleanly(self):
        buf = io.StringIO()
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with patch.object(bot_unified, "load_config") as lc:
                with patch.object(bot_unified, "run_unified") as ru:
                    with redirect_stdout(buf):
                        rc = bot_unified.main([])
        self.assertEqual(rc, 0)
        lc.assert_not_called()
        ru.assert_not_called()
        self.assertIn("Cancelled", buf.getvalue())

    def test_eof_in_menu_exits_cleanly(self):
        with patch("builtins.input", side_effect=EOFError):
            with patch.object(bot_unified, "load_config") as lc:
                with patch.object(bot_unified, "run_unified") as ru:
                    rc = bot_unified.main([])
        self.assertEqual(rc, 0)
        lc.assert_not_called()
        ru.assert_not_called()

    def test_test_flag_skips_menu(self):
        buf = io.StringIO()
        with patch("builtins.input",
                   side_effect=AssertionError("menu shown!")):
            with patch.object(bot_unified, "load_config",
                              return_value=make_cfg()):
                with patch.object(bot_unified.bot_impl, "run_test",
                                  return_value=0) as rt:
                    with redirect_stdout(buf):
                        rc = bot_unified.main(["--test"])
        self.assertEqual(rc, 0)
        rt.assert_called_once()

    def test_verify_coords_flag_skips_menu(self):
        with patch("builtins.input",
                   side_effect=AssertionError("menu shown!")):
            with patch.object(bot_unified, "load_config",
                              return_value=make_cfg()):
                with patch("calibrate.run_verify_coords") as vc:
                    rc = bot_unified.main(["--verify-coords"])
        self.assertEqual(rc, 0)
        vc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
