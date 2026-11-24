"""Recover chat history using a timestamp revealed on the message itself."""

from datetime import date, time as clock_time
import re
import time

from .backfill import Recovery
from .message_fragments import clipped, merge_fragment_pages, stitch_bubble
from .scrolling import ScrollFeedbackUncertain


def parse_message_time(text):
    """Accept the client's month/day and seconds display, never a minute label."""
    pattern = r'(?<!\d)(\d{1,2})\s*[/／]\s*(\d{1,2})\s*(\d{1,2}):(\d{2}):(\d{2})(?!\d)'
    matches = set()
    for match in re.finditer(pattern, text.replace('：', ':')):
        month, day, hour, minute, second = map(int, match.groups())
        try:
            date(2000, month, day)
            clock_time(hour, minute, second)
        except ValueError:
            continue
        matches.add(f'{month:02d}/{day:02d} {hour:02d}:{minute:02d}:{second:02d}')
    return matches.pop() if len(matches) == 1 else None


def occurrence_key(message):
    return (message.get('direction'), ''.join(message.get('text', '').split()))


def timestamp_order(value):
    # The client omits the year. Chat timestamps after today's month/day
    # therefore refer to the preceding calendar year.
    return (value[:5] <= date.today().strftime('%m/%d'), value)


def matches_legacy_time(label, visible_label, timestamp):
    if not timestamp:
        return False
    if parse_message_time(label):
        return parse_message_time(label) == timestamp
    match = re.fullmatch(r'([^\d]*|\d{1,2}月\d{1,2}日)(\d{1,2}):(\d{2})', label)
    if not match:
        return False
    prefix, hour, minute = match.groups()
    if timestamp[6:11] != f'{int(hour):02d}:{minute}':
        return False
    if not prefix:
        return True
    dated = re.fullmatch(r'(\d{1,2})月(\d{1,2})日', prefix)
    if dated:
        month, day = map(int, dated.groups())
        return timestamp.startswith(f'{month:02d}/{day:02d} ')
    return visible_label == label


def _observed(page):
    messages = []
    for message in page.messages:
        item = dict(message, _screenshot=page.screenshot)
        if item.get('bubble_rect'):
            item['_views'] = {page.screenshot: {
                'rect': item['bubble_rect'], 'top_clipped': item.get('top_clipped'),
                'bottom_clipped': item.get('bottom_clipped'),
            }}
        messages.append(item)
    return messages


def locate_latest_bubble_head(read_page, scroll_up, max_pages, max_seconds):
    """Follow only the latest bubble from its visible tail back to its header."""
    deadline = time.monotonic() + max_seconds
    page = read_page()
    moves = 0
    if not page.messages or page.messages[-1].get('bottom_clipped'):
        return None, None, moves
    tail = page.messages[-1]
    def has_header(item):
        rect = item.get('bubble_rect')
        return not item.get('top_clipped') and rect is not None and rect[1] >= 32

    while not has_header(tail):
        if moves >= max_pages - 1 or time.monotonic() >= deadline:
            return None, None, moves
        try:
            scroll_up()
        except ScrollFeedbackUncertain:
            pass
        moves += 1
        older = read_page()
        if older.image_hash == page.image_hash or not older.messages:
            return None, None, moves
        candidate = older.messages[-1]
        if not clipped(candidate) and not clipped(tail):
            before, after = tail.get('bubble_rect'), candidate.get('bubble_rect')
            if (not before or not after or occurrence_key(candidate) != occurrence_key(tail)
                    or abs(before[0]-after[0]) > 12 or after[1] <= before[1]):
                return None, None, moves
        else:
            try:
                stitch_bubble(candidate, tail)
            except ValueError:
                return None, None, moves
        page, tail = older, candidate
    return page, tail, moves


class TimestampBackfill:
    def __init__(self, read_page, scroll, read_message_time, key, max_pages, max_seconds):
        self.read_page = read_page
        self.scroll = scroll
        self.read_message_time = read_message_time
        self.key = key
        self.max_pages = max_pages
        self.max_seconds = max_seconds

    def recover(self, initial, anchor, legacy_anchor=None, *, had_checkpoint=False, passive=False):
        result = Recovery(pages=[initial], image_hash=initial.image_hash)
        if passive:
            result.reason = 'message_timestamp_requires_interactive_observation'
            return result
        if not anchor and legacy_anchor and not legacy_anchor.get('visible_time_label'):
            result.reason = 'legacy_anchor_missing_time'
            return result

        deadline = time.monotonic() + self.max_seconds
        timestamps = {}

        def stamp(item, page):
            view = item.get('_views', {}).get(page.screenshot)
            if not view or view['top_clipped'] or time.monotonic() >= deadline:
                return None
            ref = (page.screenshot, tuple(view['rect']))
            if ref not in timestamps:
                timestamps[ref] = self.read_message_time(page, view['rect'])
            return timestamps[ref]

        def saved_stamp(item):
            for screenshot, view in item.get('_views', {}).items():
                timestamp = timestamps.get((screenshot, tuple(view['rect'])))
                if timestamp:
                    return timestamp
            return None

        def advance(page, direction):
            if len(result.pages) >= self.max_pages or time.monotonic() >= deadline:
                return None
            try:
                self.scroll(direction)
            except ScrollFeedbackUncertain:
                pass
            result.scrolls += 1
            observed = self.read_page()
            result.pages.append(observed)
            return observed if observed.image_hash != page.image_hash else None

        def merge(older, newer, older_page, newer_page):
            return merge_fragment_pages(older, newer, self.key, older_page.screenshot, newer_page.screenshot)

        page = initial
        sequence = _observed(page)
        # If the user left the viewport in the middle of a bubble, first
        # collect the lower portion so that the newest end is in the sequence.
        while sequence and sequence[-1].get('bottom_clipped'):
            newer = advance(page, 'down')
            if newer is None:
                result.reason = 'message_tail_not_visible'
                return result
            try:
                sequence = merge(sequence, _observed(newer), page, newer)
            except ValueError as error:
                result.reason = str(error)
                return result
            page = newer
        tail_page = page
        result.image_hash = page.image_hash
        if not sequence and not anchor and not legacy_anchor:
            result.status = 'baseline'
            result.caught_up = True
            return result

        tail_timestamp = None
        connected = True
        while True:
            reference = anchor or legacy_anchor
            body_matches = {}
            if reference:
                candidates = []
                wanted = occurrence_key(reference)
                for index, item in enumerate(sequence):
                    if not item.get('complete') or occurrence_key(item) != wanted:
                        continue
                    if reference.get('sender') and item.get('sender') and reference['sender'] != item['sender']:
                        continue
                    timestamp = stamp(item, page)
                    body_matches[index] = timestamp
                    same_time = (timestamp == anchor['timestamp'] if anchor else
                                 matches_legacy_time(legacy_anchor['visible_time_label'],
                                                     item.get('visible_time_label'), timestamp))
                    if same_time:
                        candidates.append(index)
                if len(candidates) > 1:
                    result.reason = 'message_timestamp_anchor_ambiguous'
                    return result
                end = candidates[0] + 1 if candidates else None
            else:
                end = 0

            if end is not None and connected and end == len(sequence) and anchor:
                result.status = 'unchanged'
                result.caught_up = True
                return result

            if connected and sequence and tail_timestamp is None:
                tail_timestamp = stamp(sequence[-1], page)

            timed_index, verified_time = len(sequence)-1, tail_timestamp
            if not verified_time:
                for index in range(len(sequence)-1, -1, -1):
                    verified_time = saved_stamp(sequence[index])
                    if verified_time:
                        timed_index = index
                        break

            boundary_index = None
            if anchor and end is None:
                visible = [(index, item) for index, item in enumerate(sequence)
                           if page.screenshot in item.get('_views', {})]
                # A same-day anchor cannot have a future-date separator above
                # it. For older anchors require a timed tail on the same date.
                anchor_day = anchor['timestamp'][:5]
                if (anchor_day == date.today().strftime('%m/%d')
                        or (verified_time and verified_time[:5] == anchor_day)):
                    earlier_markers = [index for index, item in visible
                                       if re.fullmatch(r'\d{2}:\d{2}', item.get('visible_time_label') or '')
                                       and item['visible_time_label'] < anchor['timestamp'][6:11]]
                    if earlier_markers:
                        boundary_index = max(earlier_markers)
                if boundary_index is None:
                    # With the long bubble's head on screen, one adjacent
                    # earlier message can tell us whether we scrolled past it.
                    edge = min(body_matches, default=len(sequence)-1)
                    probes = [(index, item) for index, item in visible if index < edge and item.get('complete')]
                    if probes:
                        index, probe = probes[-1] if body_matches else probes[0]
                        timestamp = stamp(probe, page)
                        if timestamp and timestamp_order(timestamp) < timestamp_order(anchor['timestamp']):
                            boundary_index = index
                if boundary_index is not None:
                    # The old bubble is between an earlier visible time and
                    # a verified newer tail. Require one matching complete
                    # body in this connected sequence before using it.
                    matches = [index for index, timestamp in body_matches.items()
                               if timestamp is None and boundary_index < index < timed_index
                               and all(item.get('complete') for item in sequence[boundary_index:index])]
                    if (connected and len(body_matches) == len(matches) == 1 and verified_time
                            and timestamp_order(verified_time) > timestamp_order(anchor['timestamp'])):
                        end = matches[0] + 1

            if (end is not None and connected and sequence and verified_time
                    and timed_index >= end - (0 if anchor else 1)
                    and sequence[timed_index].get('complete')):
                following = sequence[end:timed_index+1]
                if not had_checkpoint or all(item.get('complete') for item in following):
                    direction, body = occurrence_key(sequence[timed_index])
                    result.anchor = {'direction': direction, 'text': body, 'timestamp': verified_time,
                                     'sender': sequence[timed_index].get('sender')}
                    result.checkpoint = [item for item in sequence[:timed_index+1] if item.get('complete')]
                    result.messages = following if had_checkpoint else []
                    result.status = 'recovered' if had_checkpoint else 'baseline'
                    result.caught_up = page is tail_page and timed_index == len(sequence)-1
                    result.reason = ('message_anchor_verified_between_timestamps' if boundary_index is not None
                                     else 'message_timestamp_anchor_verified')
                    return result
            if end is not None and connected and sequence and not clipped(sequence[-1]) and not tail_timestamp:
                result.reason = 'message_timestamp_unreadable'
                return result
            if boundary_index is not None:
                result.reason = 'older_timestamp_reached_anchor_unverified'
                return result

            older = advance(page, 'up')
            if older is None:
                if not result.reason:
                    result.reason = ('message_timestamp_unreadable' if end is not None else
                                     'message_timestamp_anchor_not_found')
                return result
            items = _observed(older)
            try:
                sequence = merge(items, sequence, older, page)
            except ValueError as error:
                # Keep scanning for diagnostic evidence. A disconnected page
                # cannot prove which messages bridge it to the current tail.
                result.reason = str(error)
                connected = False
                sequence = items
            page = older
