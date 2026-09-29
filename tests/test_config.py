"""Config load/save/validate tests."""

import json
import os
import tempfile
import unittest

import config


def valid_cfg():
    cfg = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in config.DEFAULT_CONFIG.items()}
    cfg["game_region"] = {"x": 100, "y": 100, "width": 900, "height": 800}
    cfg["window_bounds_at_calibration"] = {"x": 100, "y": 100,
                                           "width": 900, "height": 800}
    # Spacing 120 > default roi_size 105: the six ROIs must not overlap.
    for i in range(1, 7):
        cfg["buttons"][str(i)] = {"x": 100.0 + (i - 1) * 120, "y": 200.0}
    return cfg


class TestConfig(unittest.TestCase):
    def test_roundtrip(self):
        cfg = valid_cfg()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            config.save_config(cfg, p)
            loaded = config.load_config(p)
            self.assertEqual(loaded["buttons"]["3"]["x"], 340.0)
            self.assertEqual(loaded["roi_size"], 105)

    def test_rejects_zero_region(self):
        cfg = valid_cfg()
        cfg["game_region"]["width"] = 0
        with self.assertRaises(config.ConfigError):
            config.validate_config(cfg)

    def test_rejects_missing_file(self):
        with self.assertRaises(config.ConfigError):
            config.load_config("/nonexistent/config.json")

    def test_rejects_placeholder_buttons(self):
        cfg = valid_cfg()
        for i in range(1, 7):
            cfg["buttons"][str(i)] = {"x": 0, "y": 0}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w") as f:
                json.dump(cfg, f)
            with self.assertRaises(config.ConfigError):
                config.load_config(p)

    def test_rejects_bad_hsv_range(self):
        cfg = valid_cfg()
        cfg["orange_hsv"]["h_min"] = 40
        cfg["orange_hsv"]["h_max"] = 20
        with self.assertRaises(config.ConfigError):
            config.validate_config(cfg)

    def test_rejects_bad_threshold(self):
        cfg = valid_cfg()
        cfg["min_score"] = 1.5
        with self.assertRaises(config.ConfigError):
            config.validate_config(cfg)

    def test_rejects_overlapping_rois(self):
        cfg = valid_cfg()
        cfg["roi_size"] = 200  # spacing is 120 -> ROIs overlap
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(cfg)
        self.assertIn("overlap", str(ctx.exception).lower())

    def test_accepts_default_roi_size(self):
        self.assertEqual(config.DEFAULT_CONFIG["roi_size"], 105)
        cfg = valid_cfg()  # uses the default 105
        self.assertTrue(config.validate_config(cfg))


if __name__ == "__main__":
    unittest.main()
