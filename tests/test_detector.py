"""Detector unit tests with synthetic game frames.

Fixture: 480x360 BGR frame, purple background, six circles in a 3x2 grid,
one orange target. Button centers are stored in the config under test.
"""

import unittest

import cv2
import numpy as np

import detector

W, H = 480, 360
RADIUS = 40
PURPLE = (150, 40, 200)   # BGR
ORANGE = (0, 140, 255)    # BGR
ORANGE_DIM = (0, 90, 170)  # darker orange (brightness variation)
BG = (128, 30, 180)

CENTERS = [(90, 90), (240, 90), (390, 90),
           (90, 250), (240, 250), (390, 250)]


def make_cfg(**over):
    cfg = {
        "game_region": {"x": 0, "y": 0, "width": W, "height": H},
        "buttons": {str(i + 1): {"x": float(x), "y": float(y)}
                    for i, (x, y) in enumerate(CENTERS)},
        "roi_size": 100,
        "orange_hsv": {"h_min": 8, "h_max": 26, "s_min": 70, "v_min": 70},
        "min_score": 0.22,
        "ambiguity_margin": 0.08,
    }
    cfg.update(over)
    return cfg


def render(target=None, target_color=ORANGE, extra_orange_outside=False,
           weak=False, second_target=None):
    frame = np.full((H, W, 3), BG, dtype=np.uint8)
    for i, (x, y) in enumerate(CENTERS, start=1):
        color = PURPLE
        if i == target:
            color = target_color
        if weak and i == target:
            # mostly purple with a small orange patch -> below threshold
            cv2.circle(frame, (x, y), RADIUS, PURPLE, -1)
            cv2.circle(frame, (x, y), 8, ORANGE, -1)
            continue
        if i == second_target:
            color = ORANGE
        cv2.circle(frame, (x, y), RADIUS, color, -1)
        cv2.circle(frame, (x, y), RADIUS, (255, 255, 255), 2)
    if extra_orange_outside:
        # Big orange banner far from every ROI: must be ignored.
        cv2.rectangle(frame, (0, 320), (W, 360), ORANGE, -1)
    return frame


class TestDetector(unittest.TestCase):
    def test_all_six_targets(self):
        cfg = make_cfg()
        for t in range(1, 7):
            target, scores, _ = detector.detect(render(target=t), cfg)
            self.assertEqual(target, t, f"failed for target {t}")
            self.assertGreater(scores[t - 1], 0.3)

    def test_no_orange_target(self):
        cfg = make_cfg()
        target, scores, info = detector.detect(render(target=None), cfg)
        self.assertIsNone(target)
        self.assertTrue(all(s < cfg["min_score"] for s in scores),
                        f"scores={scores}")

    def test_weak_orange_below_threshold(self):
        cfg = make_cfg()
        target, scores, info = detector.detect(render(target=4, weak=True), cfg)
        self.assertIsNone(target, f"weak orange must not trigger: {scores}")

    def test_orange_ui_outside_rois_is_ignored(self):
        cfg = make_cfg()
        target, scores, _ = detector.detect(
            render(target=2, extra_orange_outside=True), cfg)
        self.assertEqual(target, 2)
        # The outside-ROI orange must not leak into any ROI score.
        for i, s in enumerate(scores):
            if i != 1:
                self.assertLess(s, cfg["min_score"], f"ROI {i + 1} leaked: {s}")

    def test_two_ambiguous_targets_no_click(self):
        cfg = make_cfg()
        target, scores, info = detector.detect(
            render(target=1, second_target=5), cfg)
        self.assertIsNone(target, "ambiguous targets must not click")
        self.assertIn("ambiguous", info["reason"])

    def test_brightness_variation(self):
        cfg = make_cfg()
        target, scores, _ = detector.detect(
            render(target=6, target_color=ORANGE_DIM), cfg)
        self.assertEqual(target, 6, f"dim orange missed: {scores}")

    def test_scores_are_normalized(self):
        cfg = make_cfg()
        _, scores, _ = detector.detect(render(target=3), cfg)
        self.assertTrue(all(0.0 <= s <= 1.0 for s in scores))

    def test_roi_clamp_at_frame_edge(self):
        cfg = make_cfg()
        cfg["buttons"]["1"] = {"x": 10.0, "y": 10.0}  # partly outside
        rois = detector.extract_rois(render(target=3), cfg["buttons"],
                                     cfg["roi_size"])
        self.assertEqual(rois.shape, (6, 100, 100, 3))

    def test_purple_never_scores(self):
        cfg = make_cfg()
        _, scores, _ = detector.detect(render(target=None), cfg)
        self.assertLess(max(scores), 0.05,
                        f"purple buttons scored orange: {scores}")


if __name__ == "__main__":
    unittest.main()
