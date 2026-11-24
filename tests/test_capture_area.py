import json
from pathlib import Path
import tempfile
import unittest

from fxxk_desk.capture_area import Rect, Monitor, Limits, fit_rect, resize_rect, monitor_at, save_region, load_region


class CaptureAreaTests(unittest.TestCase):
    def setUp(self):
        self.left = Monitor('left', Rect(-2160, -815, 0, 3025))
        self.main = Monitor('main', Rect(0, 0, 3840, 2160), True)
        self.right = Monitor('right', Rect(3840, -846, 6000, 2994))
        self.monitors = [self.left, self.main, self.right]
        self.limits = Limits()

    def test_negative_coordinate_monitor_is_selected(self):
        self.assertEqual(monitor_at(self.monitors, -1500, -500), self.left)

    def test_move_across_screens_refits_inside_destination(self):
        before = Rect(-1700, -600, -400, 560)
        after = fit_rect(Rect(5300, 2500, 6600, 3660), self.right, self.limits)
        self.assertTrue(self.right.bounds.contains(after))
        self.assertEqual((after.width, after.height), (before.width, before.height))

    def test_resize_never_crosses_minimum_size(self):
        initial = Rect(100, 100, 1400, 1260)
        result = resize_rect(initial, 'se', -2000, -2000, self.main, self.limits)
        self.assertEqual((result.width, result.height), (640, 480))
        self.assertEqual((result.left, result.top), (100, 100))

    def test_resize_west_preserves_opposite_edge_at_monitor_boundary(self):
        initial = Rect(100, 100, 1400, 1260)
        result = resize_rect(initial, 'w', -10000, 0, self.main, self.limits)
        self.assertEqual((result.left, result.right), (0, 1400))

    def test_portrait_screen_can_be_fully_selected(self):
        self.assertEqual(fit_rect(self.left.bounds, self.left, self.limits), self.left.bounds)

    def test_pixel_budget_does_not_break_minimum_dimensions(self):
        limits = Limits(640, 480, 640 * 480)
        wide = Monitor('huge', Rect(0, 0, 10000, 10000))
        w, h = limits.size(641, 9000, wide)
        self.assertGreaterEqual(w, 640)
        self.assertGreaterEqual(h, 480)
        self.assertLessEqual(w * h, limits.max_pixels)

    def test_single_monitor_constraint_avoids_virtual_desktop_gaps(self):
        result = fit_rect(Rect(-100, -700, 1000, 100), self.main, self.limits)
        self.assertTrue(self.main.bounds.contains(result))

    def test_local_save_roundtrip_keeps_negative_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.local.json'
            rect = Rect(-1730, -695, -430, 465)
            save_region(path, rect, self.left, self.limits)
            self.assertEqual(load_region(path), rect)
            self.assertTrue(json.loads(path.read_text(encoding='utf-8'))['layout_calibration_required'])

    def test_invalid_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.local.json'
            path.write_text(json.dumps({'coordinate_space': 'logical', 'left': 0, 'top': 0, 'right': 100, 'bottom': 100}))
            with self.assertRaises(ValueError):
                load_region(path)

    def test_oversized_save_does_not_replace_existing_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.local.json'
            path.write_text('original', encoding='utf-8')
            with self.assertRaises(ValueError):
                save_region(path, Rect(-100, -100, 5000, 5000), self.main, self.limits)
            self.assertEqual(path.read_text(encoding='utf-8'), 'original')
