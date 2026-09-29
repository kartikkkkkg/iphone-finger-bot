"""Coordinate-mapping tests (Retina scaling + window moves)."""

import unittest

from clicker import (capture_px_to_screen_point, screen_point_to_capture_px,
                     compute_click_points)


def base_cfg():
    return {
        "game_region": {"x": 100, "y": 200, "width": 400, "height": 800},
        "window_bounds_at_calibration": {"x": 100, "y": 200,
                                         "width": 400, "height": 800},
        "buttons": {str(i): {"x": 200.0, "y": 300.0} for i in range(1, 7)},
        "roi_size": 120,
    }


class TestCoordinates(unittest.TestCase):
    def test_px_to_point_retina_2x(self):
        x, y = capture_px_to_screen_point((100, 200), 200.0, 300.0, scale=2.0)
        self.assertAlmostEqual(x, 200.0)   # 100 + 200/2
        self.assertAlmostEqual(y, 350.0)   # 200 + 300/2

    def test_px_to_point_1x(self):
        x, y = capture_px_to_screen_point((100, 200), 200.0, 300.0, scale=1.0)
        self.assertAlmostEqual(x, 300.0)
        self.assertAlmostEqual(y, 500.0)

    def test_roundtrip(self):
        origin = (100, 200)
        px, py = 321.0, 654.0
        x, y = capture_px_to_screen_point(origin, px, py, scale=2.0)
        px2, py2 = screen_point_to_capture_px(origin, x, y, scale=2.0)
        self.assertAlmostEqual(px, px2)
        self.assertAlmostEqual(py, py2)

    def test_click_points_no_window_move(self):
        cfg = base_cfg()
        now = {"x": 100, "y": 200, "width": 400, "height": 800}
        pts = compute_click_points(cfg, now, scale=2.0)
        self.assertAlmostEqual(pts[1][0], 200.0)
        self.assertAlmostEqual(pts[1][1], 350.0)

    def test_click_points_window_moved(self):
        cfg = base_cfg()
        now = {"x": 300, "y": 500, "width": 400, "height": 800}  # +200,+300
        pts = compute_click_points(cfg, now, scale=2.0)
        self.assertAlmostEqual(pts[1][0], 400.0)   # shifted by +200
        self.assertAlmostEqual(pts[1][1], 650.0)   # shifted by +300

    def test_click_points_window_resized(self):
        cfg = base_cfg()
        now = {"x": 100, "y": 200, "width": 800, "height": 1600}  # 2x size
        pts = compute_click_points(cfg, now, scale=2.0)
        # button px doubles, then /2 scale -> same point offset doubles
        self.assertAlmostEqual(pts[1][0], 300.0)
        self.assertAlmostEqual(pts[1][1], 500.0)

    def test_click_points_no_current_bounds(self):
        cfg = base_cfg()
        pts = compute_click_points(cfg, None, scale=2.0)
        self.assertAlmostEqual(pts[4][0], 200.0)


if __name__ == "__main__":
    unittest.main()
