"""Bounded visual history recovery using ordered message anchors.

No model calls or desktop implementation live here, making scrolling and
uncertainty testable with deterministic paginated transcripts.
"""
from dataclasses import dataclass, field
import time
import json
import re
from .interference import UserInterference


@dataclass
class Page:
    messages: list
    screenshot: str
    image_hash: str


@dataclass
class Recovery:
    messages: list = field(default_factory=list)
    checkpoint: list = field(default_factory=list)
    image_hash: str = ""
    status: str = "gap"
    reason: str = ""
    pages: list = field(default_factory=list)
    scrolls: int = 0
    caught_up: bool = False
    anchor: dict | None = None


def find_anchor(previous, current, max_length=5):
    """Find the full ordered checkpoint once, without guessing among repeats."""
    anchor = previous[-max_length:]
    if not anchor:
        return None
    size = len(anchor)
    ends = [i + size for i in range(len(current) - size + 1)
            if current[i:i + size] == anchor]
    if ends:
        return ends[0] if len(ends) == 1 else None
    # A previous screenshot can miss the untimed twin directly above a
    # timestamped bubble. Allow that one extra old bubble only when all
    # checkpoint entries still match and two timestamps confirm the suffix.
    if size < 2 or len(current) <= size:
        return None
    try:
        parsed = [json.loads(value) for value in anchor]
        if not all(isinstance(value, dict) and value.get('visible_time_label') for value in parsed[-2:]):
            return None
    except (TypeError, ValueError):
        return None
    matches = set()
    for index, value in enumerate(parsed[:-1]):
        if not isinstance(value, dict) or not value.get('visible_time_label'):
            continue
        twin = dict(value)
        twin.pop('visible_time_label')
        twin_key = json.dumps(twin, sort_keys=True, ensure_ascii=False)
        expanded = anchor[:index] + [twin_key] + anchor[index:]
        matches.update(find_expanded_anchor_ends(expanded, current))
    return matches.pop() if len(matches) == 1 else None


def find_expanded_anchor_ends(anchor, current):
    return [i + len(anchor) for i in range(len(current) - len(anchor) + 1)
            if current[i:i + len(anchor)] == anchor]


def find_identified_anchor(previous, current):
    """Allow one anchor only when its explicit event identifier is unique."""
    if not previous:
        return None
    try:
        text = json.loads(previous[-1]).get('text', '')
    except (ValueError, TypeError, AttributeError):
        return None
    if not re.search(r'(?<![A-Za-z0-9])(?:FD-[a-f0-9]{10}(?:-\d+)?|SCROLL-[a-f0-9]{6,}-\d+)(?![A-Za-z0-9])', text):
        return None
    if previous.count(previous[-1]) != 1 or current.count(previous[-1]) != 1:
        return None
    return current.index(previous[-1])+1


def merge_older(older, newer, key):
    first, second = [key(m) for m in older], [key(m) for m in newer]
    lengths = [n for n in range(1, min(len(first), len(second)) + 1) if first[-n:] == second[:n]]
    if len(lengths) != 1:
        raise ValueError("ambiguous_page_overlap" if lengths else "page_overlap_missing")
    return older + newer[lengths[0]:]


class Backfill:
    def __init__(self, read_page, scroll, key, max_pages=30, max_seconds=45,
                 anchor_messages=5, max_messages=2000):
        self.read_page, self.scroll, self.key = read_page, scroll, key
        self.max_pages, self.max_seconds = max_pages, max_seconds
        self.anchor_messages, self.max_messages = anchor_messages, max_messages

    def recover(self, previous, initial=None, had_checkpoint=False, passive=False):
        result = Recovery()
        started = time.monotonic()
        interrupted = False

        def read():
            if len(result.pages) >= self.max_pages or time.monotonic() - started > self.max_seconds:
                raise ValueError("recovery_budget_exhausted")
            page = self.read_page()
            result.pages.append(page)
            return page

        def move(direction, fast=False):
            if len(result.pages) >= self.max_pages or time.monotonic() - started > self.max_seconds:
                raise ValueError("recovery_budget_exhausted")
            from .scrolling import ScrollFeedbackUncertain
            try:
                self.scroll(direction, fast)
            except ScrollFeedbackUncertain:
                # A speed-estimation failure is not a failed message capture.
                # Read the actual new page and prove continuity with OCR.
                pass
            result.scrolls += 1
            return read()

        def complete(page):
            return [dict(m, _screenshot=page.screenshot) for m in page.messages if m.get("complete")]

        def latest(page):
            # Two equal observations surrounding a downward scroll establish a
            # stable visible tail. A moving tail is bounded by the same budget.
            while True:
                after = move("down", True)
                if after.image_hash == page.image_hash:
                    return after
                page = after

        try:
            page = initial or read()
            if initial is not None:
                result.pages.append(initial)
            if passive:
                sequence = complete(page)
                end = find_anchor(previous, [self.key(m) for m in sequence], self.anchor_messages)
                if end is None:
                    result.reason = 'waiting_for_idle_backfill_or_baseline'
                    return result
                result.messages = sequence[end:]
                result.checkpoint = sequence
                result.image_hash = page.image_hash
                result.status = 'overlap'
                result.reason = 'visible_prefix_only_waiting_for_tail_verification'
                return result
            # Read the visible page first. Proven new messages must not wait
            # behind an unrelated trip to the history bottom.
            if previous:
                visible = page.messages
                keys = [self.key(m) for m in visible]
                end = find_anchor(previous, keys, self.anchor_messages)
                if (end is not None and end == len(visible)
                        and page.messages[-1].get('complete')):
                    result.status = 'unchanged'
                    result.caught_up = True
                    result.reason = 'visible_tail_matches_checkpoint'
                    return result
                prefix_end = end
                while prefix_end is not None and prefix_end < len(visible) and visible[prefix_end].get('complete'):
                    prefix_end += 1
                if end is not None and prefix_end > end:
                    prefix = [dict(m, _screenshot=page.screenshot) for m in visible[:prefix_end] if m.get('complete')]
                    result.messages = prefix[-(prefix_end-end):]
                    result.checkpoint = prefix
                    result.image_hash = page.image_hash
                    result.status = 'overlap'
                    result.reason = 'visible_prefix_collected_before_backfill'
                    return result
            from .message_fragments import clipped, merge_fragment_pages
            if any(clipped(message) for message in page.messages):
                def decorated(fragment_page):
                    messages = []
                    for message in fragment_page.messages:
                        copy = dict(message, _screenshot=fragment_page.screenshot)
                        if message.get('bubble_rect'):
                            copy['_views'] = {fragment_page.screenshot: {'rect': message['bubble_rect'],
                                'top_clipped': message.get('top_clipped'), 'bottom_clipped': message.get('bottom_clipped')}}
                        messages.append(copy)
                    return messages
                sequence = decorated(page)
                # First find the lower edge without skipping the middle of a
                # bubble. Every movement retains overlapping text lines.
                while sequence and sequence[-1].get('bottom_clipped'):
                    newer = move('down')
                    if newer.image_hash == page.image_hash:
                        raise ValueError('long_message_bottom_not_reached')
                    sequence = merge_fragment_pages(sequence, decorated(newer), self.key, page.screenshot, newer.screenshot)
                    page = newer
                tail_page = page
                while True:
                    keys = [self.key(message) for message in sequence]
                    end = find_anchor(previous, keys, self.anchor_messages) if previous else None
                    baseline_ready = not previous and sequence and not clipped(sequence[0])
                    if end is not None or baseline_ready:
                        offset = end if end is not None else 0
                        if any(not message.get('complete') for message in sequence[offset:]):
                            raise ValueError('long_message_incomplete_or_ocr_uncertain')
                        if len(sequence) > self.max_messages:
                            raise ValueError('message_budget_exhausted')
                        result.status = 'recovered' if previous or had_checkpoint else 'baseline'
                        result.messages = sequence[offset:] if previous or had_checkpoint else []
                        result.checkpoint = sequence
                        result.image_hash = tail_page.image_hash
                        result.reason = 'long_message_assembled_before_tail_verification'
                        # Send the proven complete message before optional tail
                        # restoration; the workflow retains a deferred-tail flag.
                        return result
                    older = move('up')
                    if older.image_hash == page.image_hash:
                        raise ValueError('history_boundary_before_long_message_head_or_anchor')
                    sequence = merge_fragment_pages(decorated(older), sequence, self.key, older.screenshot, page.screenshot)
                    page = older
            if not previous:
                page = latest(page)
            tail_page = page
            sequence = complete(page)
            if not previous:
                if page.messages and not sequence:
                    raise ValueError("no_complete_messages_on_page")
                result.status, result.caught_up = ("overlap" if had_checkpoint else "baseline"), True
                if had_checkpoint:
                    result.messages = sequence
                result.checkpoint = sequence
                result.image_hash = tail_page.image_hash
                return result
            end = find_anchor(previous, [self.key(m) for m in sequence], self.anchor_messages)
            while end is None:
                older = move("up")
                if older.image_hash == page.image_hash:
                    raise ValueError("history_boundary_before_anchor")
                older_messages = complete(older)
                if not older_messages:
                    raise ValueError("no_complete_messages_on_page")
                sequence = merge_older(older_messages, sequence, self.key)
                if len(sequence) > self.max_messages:
                    raise ValueError("message_budget_exhausted")
                page = older
                end = find_anchor(previous, [self.key(m) for m in sequence], self.anchor_messages)
            # Preserve all messages through the original tail, in order, before
            # returning downward. New arrivals during recovery are checked again.
            result.messages = sequence[end:]
            result.checkpoint = sequence
            result.image_hash = tail_page.image_hash
            result.status = "recovered" if result.scrolls > 1 else "overlap"
            returned = latest(page)
            now = complete(returned)
            joined = find_anchor([self.key(m) for m in sequence], [self.key(m) for m in now], self.anchor_messages)
            if joined is not None:
                if len(result.messages) + len(now[joined:]) > self.max_messages:
                    raise ValueError("message_budget_exhausted")
                result.messages.extend(now[joined:])
                result.checkpoint = now
                result.image_hash = returned.image_hash
                result.caught_up = True
            else:
                # Commit the proven prefix but retain its checkpoint. A later
                # cycle must backfill the newly arrived tail; never skip to it.
                result.reason = "tail_advanced_during_recovery"
            return result
        except UserInterference:
            interrupted = True
            raise
        except (ValueError, RuntimeError, OSError) as error:
            result.reason = str(error)
            # A failed search never replaces the old durable checkpoint.
            if not result.checkpoint:
                result.status = "gap"
                result.messages = []
            return result
        finally:
            # Restoration must not turn a proven prefix into a false latest
            # checkpoint. One bounded downward movement also runs after failure.
            if not interrupted and not result.caught_up and result.scrolls:
                try:
                    self.scroll("down", True)
                except UserInterference:
                    raise
                except (OSError, RuntimeError):
                    pass
