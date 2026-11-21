"""Calibrated pixel-distance scrolling with bounded feedback batches."""
from dataclasses import dataclass, asdict, field
import math
import time


@dataclass
class ScrollProfile:
    pixels_per_event: float | None = None
    updated_at: float = 0
    max_batch_events: int = 12


@dataclass
class ScrollStats:
    mode: str
    direction: str
    target_pixels: float
    measured_pixels: float = 0
    wheel_events: int = 0
    captures: int = 1
    batches: int = 0
    stopped_at_boundary: bool = False
    pixels_per_event: float | None = None
    elapsed_seconds: float = 0
    error: str | None = None
    measurements: list = field(default_factory=list)
    recalibrations: int = 0

    def to_dict(self):
        return asdict(self)


def vertical_motion(before, after, direction):
    import cv2
    import numpy as np
    if np.array_equal(before, after):
        return 0.0
    limit = int(before.shape[0] * 0.45)
    template = before[:-limit] if direction == 'up' else before[limit:]
    if np.std(template) < 0.001:
        raise RuntimeError('Pixel scroll template has no recognizable content')
    # Match only the actual overlapping pixels, rather than periodic FFT
    # correlation. Repeated message layouts otherwise alias a large movement
    # to a shorter distance even when their unique marker lines are visible.
    scores = cv2.matchTemplate(after, template, cv2.TM_CCOEFF_NORMED)[:, 0]
    best = int(np.argmax(scores))
    confidence = float(scores[best])
    competing = scores.copy()
    competing[max(0, best - 3):best + 4] = -1
    if not math.isfinite(confidence) or confidence < 0.995 or float(np.max(competing)) >= confidence - 0.0001:
        raise RuntimeError('Pixel scroll feedback uncertain; calibration invalidated')
    return float(best if direction == 'up' else limit - best)


class PixelScroller:
    def __init__(self, capture, wheel, direction, height, profile=None, mode='batch'):
        if direction not in ('up', 'down') or mode not in ('batch', 'stepwise'):
            raise ValueError('Invalid scroll mode or direction')
        self.capture, self.wheel = capture, wheel
        self.direction, self.height = direction, height
        self.profile = profile if profile is not None else ScrollProfile()
        self.mode = mode

    def run(self, initial, goal, max_events=48):
        stats = ScrollStats(self.mode, self.direction, goal)
        self.last_stats = stats
        started = time.monotonic()
        previous = initial
        if started - self.profile.updated_at > 60:
            self.profile.pixels_per_event = None
        try:
            while stats.wheel_events < max_events:
                remaining = goal - stats.measured_pixels
                step = self.profile.pixels_per_event
                # Wheel input is quantized. Half a calibrated event is close
                # enough; do not overshoot a page just to cover a few pixels.
                tolerance = max(2, step * 0.5) if step else 0
                if remaining <= tolerance:
                    break
                count = 1
                if self.mode == 'batch' and step and step > 2:
                    safe_events = max(1, math.floor(self.height * 0.40 / step))
                    count = min(max(1, math.floor(remaining / step)), safe_events, self.profile.max_batch_events)
                count = min(count, max_events - stats.wheel_events)
                self.wheel(count)
                stats.wheel_events += count
                stats.batches += 1
                current = self.capture()
                stats.captures += 1
                distance = vertical_motion(previous, current, self.direction)
                stats.measurements.append({'events': count, 'distance': round(distance, 2)})
                if distance < 1:
                    stats.stopped_at_boundary = True
                    break
                # Unexpected changes invalidate the cached speed; no unverified
                # page is silently accepted by the recovery engine.
                observed_step = distance / count
                if count > 1 and step and not 0.55 <= observed_step / step <= 1.65:
                    # The measured overlap is trustworthy even if the speed
                    # prediction was wrong. Shrink the next batch and relearn;
                    # do not discard proven displacement or count it twice.
                    self.profile.max_batch_events = max(1, min(self.profile.max_batch_events, count // 2))
                    stats.recalibrations += 1
                self.profile.pixels_per_event = observed_step
                self.profile.updated_at = time.monotonic()
                stats.measured_pixels += distance
                previous = current
            stats.pixels_per_event = self.profile.pixels_per_event
            return stats
        except Exception as error:
            stats.error = str(error)
            if 'count' in locals() and count > 1:
                self.profile.max_batch_events = max(1, min(self.profile.max_batch_events, count // 2))
            self.profile.pixels_per_event = None
            # Retain the conservative batch cap long enough for the next
            # recovery attempt. A zero timestamp would immediately expire it
            # and accidentally retry the same failing batch size.
            self.profile.updated_at = time.monotonic()
            raise
        finally:
            stats.elapsed_seconds = round(time.monotonic() - started, 4)
