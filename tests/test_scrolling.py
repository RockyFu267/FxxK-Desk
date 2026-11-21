import time
import unittest

import numpy as np

from fxxk_desk.scrolling import PixelScroller, ScrollProfile, vertical_motion


class FakePixelDesktop:
    def __init__(self, step=41, position=1600, direction='up'):
        rng = np.random.default_rng(7)
        self.canvas = (rng.random((4000, 200)) > 0.94).astype(np.float32)
        self.step, self.position, self.direction = step, position, direction
        self.commands = []

    def capture(self):
        return self.canvas[self.position:self.position + 836].copy()

    def wheel(self, count):
        self.commands.append(count)
        delta = self.step * count * (-1 if self.direction == 'up' else 1)
        self.position = min(3164, max(0, self.position + delta))


class PixelScrollTests(unittest.TestCase):
    def test_repeated_layout_with_unique_markers_keeps_actual_large_distance(self):
        rng = np.random.default_rng(12)
        tile = (rng.random((246, 200)) > 0.94).astype(np.float32)
        canvas = np.tile(tile, (15, 1))
        for i in range(15):
            canvas[i * 246:i * 246 + 15, :80] = (rng.random((15, 80)) > 0.8).astype(np.float32)
        self.assertEqual(vertical_motion(canvas[1000:1836], canvas[672:1508], 'up'), 328)

    def run_scroll(self, desktop, mode='batch', profile=None):
        return PixelScroller(desktop.capture, desktop.wheel, desktop.direction, 836,
                             profile, mode).run(desktop.capture(), 334.4)

    def test_calibrated_batch_reduces_captures_with_same_actual_distance(self):
        stepwise, batch = FakePixelDesktop(), FakePixelDesktop()
        original = self.run_scroll(stepwise, 'stepwise')
        improved = self.run_scroll(batch)
        self.assertEqual(stepwise.position, batch.position)
        self.assertLess(improved.captures, original.captures)
        self.assertLessEqual(improved.captures, 4)
        self.assertAlmostEqual(improved.pixels_per_event, 41, delta=1)
        self.assertLess(abs(improved.measured_pixels - 334.4), 21)

    def test_warm_calibration_can_scroll_a_page_with_one_feedback_capture(self):
        desktop = FakePixelDesktop()
        profile = ScrollProfile(41, time.monotonic())
        result = self.run_scroll(desktop, profile=profile)
        self.assertEqual(result.captures, 2)  # Initial image + one verification.

    def test_conservative_two_event_batches_still_reduce_captures(self):
        old, new = FakePixelDesktop(), FakePixelDesktop()
        original = self.run_scroll(old, 'stepwise')
        improved = self.run_scroll(new, profile=ScrollProfile(max_batch_events=2))
        self.assertEqual(old.position, new.position)
        self.assertLess(improved.captures, original.captures)
        self.assertLessEqual(max(new.commands), 2)

    def test_unreliable_pixels_clear_the_cached_speed(self):
        desktop = FakePixelDesktop()
        profile = ScrollProfile(41, time.monotonic())
        unrelated = np.random.default_rng(42).random((836, 200)).astype(np.float32)
        controller = PixelScroller(lambda: unrelated, desktop.wheel, 'up', 836, profile)
        with self.assertRaisesRegex(RuntimeError, 'calibration invalidated'):
            controller.run(desktop.capture(), 334.4)
        self.assertIsNone(profile.pixels_per_event)
        self.assertGreater(profile.updated_at, 0)
        self.assertLess(profile.max_batch_events, 12)

    def test_boundary_stops_without_guessing_a_pixel_speed(self):
        result = self.run_scroll(FakePixelDesktop(position=0))
        self.assertTrue(result.stopped_at_boundary)
        self.assertEqual(result.wheel_events, 1)
        self.assertIsNone(result.pixels_per_event)

    def test_verified_speed_change_shrinks_batch_and_recalibrates(self):
        desktop = FakePixelDesktop(step=20)
        profile = ScrollProfile(41, time.monotonic())
        result = self.run_scroll(desktop, profile=profile)
        self.assertGreater(result.recalibrations, 0)
        self.assertAlmostEqual(profile.pixels_per_event, 20, delta=1)
        self.assertLess(profile.max_batch_events, 12)

    def test_short_final_batch_is_verified_as_boundary(self):
        desktop = FakePixelDesktop(position=100)
        profile = ScrollProfile(41, time.monotonic())
        result = self.run_scroll(desktop, profile=profile)
        self.assertTrue(result.stopped_at_boundary)
        self.assertAlmostEqual(result.measured_pixels, 100, delta=1)
        self.assertGreater(profile.pixels_per_event, 0)

    def test_expired_profile_recalibrates_with_one_event(self):
        desktop = FakePixelDesktop()
        self.run_scroll(desktop, profile=ScrollProfile(80, time.monotonic() - 61))
        self.assertEqual(desktop.commands[0], 1)

    def test_downward_scroll_has_correct_signed_displacement(self):
        result = self.run_scroll(FakePixelDesktop(position=800, direction='down'))
        self.assertAlmostEqual(result.pixels_per_event, 41, delta=1)
        self.assertGreater(result.measured_pixels, 300)
