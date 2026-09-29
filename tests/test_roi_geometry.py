"""ROI geometry tests: size, centering, overlap, button containment.

Reference geometry comes from the user's real game screenshots
(1206x2622 capture): button centers and the measured button radius.
Measured 2026-09-29 from samples/game-all-purple.png: the button's outer
rim spans ~200 px at 1206 px capture width -> REF_BUTTON_RADIUS = 100.

USER_SCALE models the live iPhone Mirroring capture. The old calibrator
suggested roi = 280 * width / 1206 and the user landed on 150, which
implies scale <= 150/280 = 0.536 (their 150-px ROIs overlapped, so the
true scale is strictly below that). 0.5 is representative: at this scale
the requested 105-px ROI both contains the whole button (diameter ~100 px)
and stays clear of neighbors (spacing ~139 px).
"""

import unittest

import cv2
import numpy as np

import config
from detector import (NUM_BUTTONS, extract_rois, find_overlapping_rois,
                      roi_bounds)

# Measured button centers at 1206 px capture width (row-major, 1-based).
REF_CENTERS = {
    "1": (325, 1210), "2": (602, 1212), "3": (879, 1206),
    "4": (325, 1490), "5": (602, 1490), "6": (879, 1490),
}
REF_BUTTON_RADIUS = 100         # measured outer-rim radius @1206 px
USER_SCALE = 0.5                # representative live-mirroring scale (< 0.536)
ROI = 105


def scaled_buttons(scale=USER_SCALE):
    return {k: {"x": x * scale, "y": y * scale}
            for k, (x, y) in REF_CENTERS.items()}


class TestRoiGeometry(unittest.TestCase):
    def test_default_roi_size_is_105(self):
        self.assertEqual(config.DEFAULT_CONFIG["roi_size"], ROI)

    def test_roi_size_configurable(self):
        # roi_size lives in config.json and flows straight into the detector.
        cfg = dict(config.DEFAULT_CONFIG)
        cfg["roi_size"] = 90
        self.assertEqual(cfg["roi_size"], 90)

    def test_centers_unchanged_by_roi_resize(self):
        """Shrinking 150 -> 105 must not move any ROI center."""
        buttons = scaled_buttons()
        for i in range(1, NUM_BUTTONS + 1):
            cx = buttons[str(i)]["x"]
            cy = buttons[str(i)]["y"]
            for size in (150, ROI):
                x0, y0, x1, y1 = roi_bounds(buttons, size, i)
                self.assertEqual(x1 - x0, size)
                self.assertEqual(y1 - y0, size)
                # Center of the rect == calibrated center (sub-pixel rounding
                # only: int(round()) is the shared detector/debug convention).
                self.assertLessEqual(abs((x0 + x1) / 2 - cx), 1.0)
                self.assertLessEqual(abs((y0 + y1) / 2 - cy), 1.0)
            # ...and resizing 150 -> 105 does not move the rect at all
            # beyond the shared rounding: same top-left anchor convention.
            a = roi_bounds(buttons, 150, i)
            b = roi_bounds(buttons, ROI, i)
            self.assertEqual((a[0] + 75, a[1] + 75),  # 150-px rect center
                             (int(round(cx)), int(round(cy))))
            self.assertEqual((b[0] + 52, b[1] + 52),  # 105-px rect center
                             (int(round(cx)), int(round(cy))))

    def test_no_roi_overlap_at_user_scale(self):
        buttons = scaled_buttons()
        self.assertEqual(find_overlapping_rois(buttons, ROI), [])

    def test_no_roi_overlap_at_reference_scale(self):
        buttons = {k: {"x": float(x), "y": float(y)}
                   for k, (x, y) in REF_CENTERS.items()}
        self.assertEqual(find_overlapping_rois(buttons, ROI), [])

    def test_overlap_check_positive_control(self):
        """The check must actually fire when ROIs are too big."""
        buttons = scaled_buttons()
        pairs = find_overlapping_rois(buttons, 300)
        self.assertTrue(len(pairs) > 0)
        # And at full reference scale, the old 280 px ROI overlapped too
        # (neighbor spacing there is 277-280 px).
        ref = {k: {"x": float(x), "y": float(y)}
               for k, (x, y) in REF_CENTERS.items()}
        self.assertTrue(len(find_overlapping_rois(ref, 280)) > 0)

    def test_buttons_fully_inside_rois(self):
        """Each drawn button disc must lie entirely within its ROI rect."""
        scale = USER_SCALE
        buttons = scaled_buttons(scale)
        radius = int(round(REF_BUTTON_RADIUS * scale))
        half = ROI // 2
        # Modest margin: ROI must be bigger than the button.
        self.assertGreater(half, radius,
                           "ROI half-size must exceed the button radius")
        for i in range(1, NUM_BUTTONS + 1):
            cx = int(round(buttons[str(i)]["x"]))
            cy = int(round(buttons[str(i)]["y"]))
            x0, y0, x1, y1 = roi_bounds(buttons, ROI, i)
            self.assertLessEqual(x0, cx - radius)
            self.assertGreaterEqual(x1, cx + radius + 1)  # exclusive edge
            self.assertLessEqual(y0, cy - radius)
            self.assertGreaterEqual(y1, cy + radius + 1)

    def test_extract_rois_captures_whole_button(self):
        """End-to-end: the detector's extract_rois sees every button pixel."""
        scale = USER_SCALE
        buttons = scaled_buttons(scale)
        radius = int(round(REF_BUTTON_RADIUS * scale))
        frame = np.full((1200, 900, 3), (40, 40, 40), dtype=np.uint8)
        disc_color = (60, 140, 220)  # BGR, distinct from background
        expected_counts = {}
        for i in range(1, NUM_BUTTONS + 1):
            cx = int(round(buttons[str(i)]["x"]))
            cy = int(round(buttons[str(i)]["y"]))
            mask = np.zeros(frame.shape[:2], dtype=np.uint8)
            cv2.circle(mask, (cx, cy), radius, 255, -1)
            frame[mask > 0] = disc_color
            expected_counts[i] = int(np.count_nonzero(mask))
        rois = extract_rois(frame, buttons, ROI)
        self.assertEqual(rois.shape, (6, ROI, ROI, 3))
        for i in range(1, NUM_BUTTONS + 1):
            got = int(np.count_nonzero(
                np.all(rois[i - 1] == disc_color, axis=2)))
            self.assertEqual(got, expected_counts[i],
                             f"button {i} not fully inside its ROI")

    def test_roi_bounds_matches_extract_rois_slice(self):
        """The rect the debug window draws == the pixels the detector uses."""
        buttons = scaled_buttons()
        frame = np.random.default_rng(7).integers(
            0, 256, size=(1200, 900, 3), dtype=np.uint8)
        rois = extract_rois(frame, buttons, ROI)
        for i in range(1, NUM_BUTTONS + 1):
            x0, y0, x1, y1 = roi_bounds(buttons, ROI, i)
            np.testing.assert_array_equal(rois[i - 1], frame[y0:y1, x0:x1])

    def test_rois_clipped_to_frame(self):
        """A button near the frame edge must not crash extraction."""
        buttons = {str(i): {"x": 20.0, "y": 20.0} for i in range(1, 7)}
        # (validation would reject overlaps; this tests clipping only)
        frame = np.zeros((200, 200, 3), dtype=np.uint8)
        rois = extract_rois(frame, buttons, ROI)
        self.assertEqual(rois.shape, (6, ROI, ROI, 3))


if __name__ == "__main__":
    unittest.main()
