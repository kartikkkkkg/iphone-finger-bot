"""Regression test against REAL game screenshots.

Looks for samples/game-*.png (git-ignored; present on the dev machine).
Skips gracefully when they are absent so CI stays green.

Reference geometry measured from the 1206x2622 screenshots:
  row 1: (325,1210) (602,1212) (879,1206)
  row 2: (325,1490) (602,1490) (879,1490)
"""

import glob
import os
import unittest

import cv2

import detector

SAMPLES_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "samples")

CENTERS = [(325, 1210), (602, 1212), (879, 1206),
           (325, 1490), (602, 1490), (879, 1490)]

EXPECTED = {
    "game-orange-4a.png": 4,
    "game-orange-4b.png": 4,
    "game-all-purple.png": None,
}


def make_cfg():
    return {
        "game_region": {"x": 0, "y": 0, "width": 1206, "height": 2622},
        "buttons": {str(i + 1): {"x": float(x), "y": float(y)}
                    for i, (x, y) in enumerate(CENTERS)},
        "roi_size": 280,
        "orange_hsv": {"h_min": 10, "h_max": 24, "s_min": 90, "v_min": 90},
        "min_score": 0.22,
        "ambiguity_margin": 0.08,
    }


class TestRealScreenshots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = sorted(glob.glob(os.path.join(SAMPLES_DIR, "game-*.png"))
                           )
        if not cls.files:
            raise unittest.SkipTest(
                "no samples/game-*.png screenshots present")

    def test_real_screenshots(self):
        cfg = make_cfg()
        for path in self.files:
            name = os.path.basename(path)
            if name not in EXPECTED:
                continue
            frame = cv2.imread(path)
            self.assertIsNotNone(frame, f"could not read {name}")
            target, scores, info = detector.detect(frame, cfg)
            self.assertEqual(target, EXPECTED[name],
                             f"{name}: got {target}, scores="
                             f"{[f'{s:.3f}' for s in scores]}")


if __name__ == "__main__":
    unittest.main()
