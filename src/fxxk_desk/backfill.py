"""Bounded visual history recovery using ordered message anchors.

No model calls or desktop implementation live here, making scrolling and
uncertainty testable with deterministic paginated transcripts.
"""
from dataclasses import dataclass, field
import time
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


def find_anchor(previous, current, max_length=3):
    """Longest unique suffix of the checkpoint in the observed sequence.

    One-message anchors are only used if the previous checkpoint itself has
    one message. Repeated suffixes never silently choose an occurrence.
    """
    minimum = min(2, len(previous))
    if minimum == 0:
        return None
    for length in range(min(max_length, len(previous)), minimum - 1, -1):
        anchor = previous[-length:]
        positions = [i for i in range(len(current) - length + 1) if current[i:i + length] == anchor]
        if len(positions) == 1:
            return positions[0] + length
    return None


def merge_older(older, newer, key):
    first, second = [key(m) for m in older], [key(m) for m in newer]
    lengths = [n for n in range(1, min(len(first), len(second)) + 1) if first[-n:] == second[:n]]
    if len(lengths) != 1:
        raise ValueError("ambiguous_page_overlap" if lengths else "page_overlap_missing")
    return older + newer[lengths[0]:]


class Backfill:
    def __init__(self, read_page, scroll, key, max_pages=30, max_seconds=45,
                 anchor_messages=3, max_messages=2000):
        self.read_page, self.scroll, self.key = read_page, scroll, key
        self.max_pages, self.max_seconds = max_pages, max_seconds
        self.anchor_messages, self.max_messages = anchor_messages, max_messages

    def recover(self, previous, initial=None, had_checkpoint=False):
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
            self.scroll(direction, fast)
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
