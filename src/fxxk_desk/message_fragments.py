"""Join adjacent views of one clipped text bubble using unique line overlap."""
import re


def clipped(message):
    return bool(message.get('top_clipped') or message.get('bottom_clipped'))


def _lines(message):
    return [line for line in message.get('text', '').splitlines() if line.strip()]


def _with_views(result, upper, lower):
    result = dict(result)
    label = upper.get('visible_time_label') or lower.get('visible_time_label')
    if label:
        result['visible_time_label'] = label
    if upper.get('ocr_punctuation_disagreement') or lower.get('ocr_punctuation_disagreement'):
        result['ocr_punctuation_disagreement'] = True
    result['_views'] = {**upper.get('_views', {}), **lower.get('_views', {})}
    references = []
    for message in (upper, lower):
        for reference in message.get('fragment_screenshots', [message.get('_screenshot')]):
            if reference and reference not in references:
                references.append(reference)
    result['fragment_screenshots'] = references
    return result


def _character_overlap(upper, lower):
    """OCR can regroup physical lines across views; preserve all nonspace chars."""
    first, second = upper.get('text', ''), lower.get('text', '')
    a = re.sub(r'\s+', '', first)
    positions = [i for i, char in enumerate(second) if not char.isspace()]
    b = ''.join(second[i] for i in positions)
    def strong(pattern):
        if len(pattern) < 32 or a.count(pattern) != 1 or b.count(pattern) != 1:
            return False
        chunks = {pattern[i:i+16] for i in range(0, len(pattern)-15, 8)}
        return sum(a.count(chunk) == b.count(chunk) == 1 for chunk in chunks) >= 2
    if (upper.get('top_clipped') and lower.get('top_clipped') and not upper.get('bottom_clipped')
            and not lower.get('bottom_clipped') and b.endswith(a) and strong(a)):
        return _with_views(lower, upper, lower)
    if upper.get('bottom_clipped') and a and a in b and strong(a):
        offset = b.index(a)
        if ((upper.get('top_clipped') and lower.get('top_clipped')) or
                (not upper.get('top_clipped') and not lower.get('top_clipped') and offset == 0)):
            return _with_views(lower, upper, lower)
    if (not upper.get('bottom_clipped') and lower.get('top_clipped') and not lower.get('bottom_clipped')
            and a.endswith(b) and strong(b)):
        return _with_views(upper, upper, lower)
    if not upper.get('bottom_clipped') or not lower.get('top_clipped'):
        return None
    matches = [n for n in range(32, min(len(a),len(b))+1) if a[-n:] == b[:n] and strong(b[:n])]
    punctuation_disagreement = False
    if not matches:
        # A long table can give different OCR readings of the same dot.
        # Require unique exact text on both sides of a single disagreement.
        for n in range(128, min(len(a), len(b)) + 1):
            left, right = a[-n:], b[:n]
            if left[:64] != right[:64] or left[-64:] != right[-64:]:
                continue
            differences = [(x, y) for x, y in zip(left, right) if x != y]
            if (len(differences) == 1 and set(differences[0]) == {',', '.'}
                    and strong(right[:64]) and strong(right[-64:])):
                matches.append(n)
        punctuation_disagreement = bool(matches)
    if len(matches) != 1:
        return None
    text = first+second[positions[matches[0]-1]+1:]
    if len(text)>10000:
        raise ValueError('long_message_character_budget_exhausted')
    result = dict(upper, text=text, sender=upper.get('sender') or lower.get('sender'),
                  bottom_clipped=lower.get('bottom_clipped', False), source='stitched_local_ocr',
                  ocr_reliable=upper.get('ocr_reliable', False) and lower.get('ocr_reliable', False))
    result['complete'] = not clipped(result) and result['ocr_reliable']
    result['geometrically_complete'] = not clipped(result)
    if punctuation_disagreement or upper.get('ocr_punctuation_disagreement') or lower.get('ocr_punctuation_disagreement'):
        result['ocr_punctuation_disagreement'] = True
    return _with_views(result, upper, lower)


def stitch_bubble(upper, lower):
    if upper.get('direction') != lower.get('direction') or upper.get('direction') not in ('incoming', 'outgoing'):
        raise ValueError('fragment_direction_conflict')
    if upper.get('sender') and lower.get('sender') and upper['sender'] != lower['sender']:
        raise ValueError('fragment_sender_conflict')
    for field in ('bubble_left', 'bubble_right'):
        if field not in upper or field not in lower or abs(upper[field]-lower[field]) > 12:
            raise ValueError('fragment_geometry_conflict')
    first, second = _lines(upper), _lines(lower)
    normalize = lambda line: re.sub(r'\s+', '', line)
    a, b = list(map(normalize, first)), list(map(normalize, second))
    unique_lines = lambda pattern: sum(a.count(line) == 1 and b.count(line) == 1 for line in set(pattern)) >= 2
    if not upper.get('bottom_clipped'):
        # Both views can include the same bubble bottom while the older view
        # exposes more of its upper body (common between two tall messages).
        if (lower.get('top_clipped') and not lower.get('bottom_clipped') and len(b) >= 2
                and a[-len(b):] == b and unique_lines(b) and sum(map(len,b)) >= 12):
            result = dict(upper)
            references = []
            for message in (upper, lower):
                for reference in message.get('fragment_screenshots', [message.get('_screenshot')]):
                    if reference and reference not in references:
                        references.append(reference)
            result['fragment_screenshots'] = references
            return result
        fallback = _character_overlap(upper, lower)
        if fallback:
            return fallback
        raise ValueError('fragment_edges_do_not_join')
    # Returning upward after reading the lower half can revisit a page already
    # contained in the assembled bubble. Do not duplicate or reverse that text.
    positions = [i for i in range(len(b)-len(a)+1) if b[i:i+len(a)] == a]
    if (len(a) >= 2 and len(positions) == 1 and sum(map(len, a)) >= 12 and unique_lines(a)
            and ((upper.get('top_clipped') and lower.get('top_clipped'))
                 or (not upper.get('top_clipped') and not lower.get('top_clipped') and positions[0] == 0))):
        result = dict(lower)
        references = list(result.get('fragment_screenshots', [result.get('_screenshot')]))
        for reference in upper.get('fragment_screenshots', [upper.get('_screenshot')]):
            if reference and reference not in references:
                references.append(reference)
        result['fragment_screenshots'] = [reference for reference in references if reference]
        return result
    if not lower.get('top_clipped'):
        fallback = _character_overlap(upper, lower)
        if fallback:
            return fallback
        raise ValueError('fragment_edges_do_not_join')
    lengths = [n for n in range(2, min(len(a), len(b))+1)
               if a[-n:] == b[:n] and len(set(a[-n:])) >= 2
               and sum(map(len, a[-n:])) >= 12
               and unique_lines(a[-n:])
               and sum(a[i:i+n] == a[-n:] for i in range(len(a)-n+1)) == 1
               and sum(b[i:i+n] == a[-n:] for i in range(len(b)-n+1)) == 1]
    if len(lengths) != 1:
        fallback = _character_overlap(upper, lower)
        if fallback:
            return fallback
        raise ValueError('ambiguous_fragment_overlap' if lengths else 'fragment_overlap_missing')
    text = '\n'.join(first+second[lengths[0]:])
    if len(text) > 10000:
        raise ValueError('long_message_character_budget_exhausted')
    result = dict(upper)
    result.update(text=text, sender=upper.get('sender') or lower.get('sender'),
                  top_clipped=bool(upper.get('top_clipped')),
                  bottom_clipped=bool(lower.get('bottom_clipped')),
                  ocr_reliable=upper.get('ocr_reliable', False) and lower.get('ocr_reliable', False),
                  source='stitched_local_ocr')
    result['complete'] = not clipped(result) and result['ocr_reliable']
    result['geometrically_complete'] = not clipped(result)
    references = []
    for message in (upper, lower):
        for reference in message.get('fragment_screenshots', [message.get('_screenshot')]):
            if reference and reference not in references:
                references.append(reference)
    result['fragment_screenshots'] = references
    return result


def merge_fragment_pages(older, newer, key, older_view=None, newer_view=None):
    # The overlap can contain whole messages before the split bubble, e.g.
    # [A, B, bubble-head] with [B, assembled-bubble]. Align the entire suffix.
    candidates = []
    last_error = None
    displacement = None
    if older_view and newer_view:
        import cv2
        from pathlib import Path
        from .scrolling import ScrollFeedbackUncertain, vertical_motion
        if Path(str(older_view)).is_file() and Path(str(newer_view)).is_file():
            before = cv2.imread(str(newer_view), cv2.IMREAD_GRAYSCALE)
            after = cv2.imread(str(older_view), cv2.IMREAD_GRAYSCALE)
            if before is not None and after is not None and before.shape == after.shape:
                width = int(before.shape[1] * .72)
                try:
                    displacement = vertical_motion(before[:, :width], after[:, :width], 'up')
                except ScrollFeedbackUncertain:
                    pass
    for length in range(1, min(len(older),len(newer))+1):
        joined = []
        # Geometry establishes which bubble instances overlap before OCR
        # matching, so two identical adjacent bubbles cannot be interchanged.
        references = {newer_view}
        for message in newer[:length]:
            references.update(message.get('_views', {}))
        geometry_scores = []
        direct_conflict = False
        for reference in references:
            offsets = []
            for upper, lower in zip(older[-length:], newer[:length]):
                a = upper.get('_views', {}).get(older_view)
                b = lower.get('_views', {}).get(reference)
                if a and b:
                    if reference == newer_view and displacement is not None:
                        top = max(a['rect'][1] - displacement, b['rect'][1])
                        bottom = min(a['rect'][3] - displacement, b['rect'][3])
                        if bottom <= top:
                            direct_conflict = True
                    if not a['top_clipped'] and not b['top_clipped']:
                        offsets.append(b['rect'][1]-a['rect'][1])
                    if not a['bottom_clipped'] and not b['bottom_clipped']:
                        offsets.append(b['rect'][3]-a['rect'][3])
            # The older viewport puts a shared edge at or below its position
            # in the newer viewport. An opposite displacement is not evidence.
            consistent = offsets and max(offsets) <= 32 and max(offsets)-min(offsets)<=32
            if reference == newer_view and offsets and not consistent:
                direct_conflict = True
            if (reference == newer_view and displacement is not None
                    and any(abs(offset + displacement) > 32 for offset in offsets)):
                direct_conflict = True
            if consistent:
                geometry_scores.append(len(offsets))
        if direct_conflict:
            continue
        for upper, lower in zip(older[-length:], newer[:length]):
            upper_time, lower_time = upper.get('visible_time_label'), lower.get('visible_time_label')
            if upper_time and lower_time and upper_time != lower_time:
                last_error = 'fragment_time_label_conflict'
                break
            compatible_sender = not upper.get('sender') or not lower.get('sender') or upper['sender'] == lower['sender']
            same_body = (re.sub(r'\s+', '', upper.get('text', '')) == re.sub(r'\s+', '', lower.get('text', ''))
                         and upper.get('direction') == lower.get('direction'))
            if upper.get('complete') and lower.get('complete') and compatible_sender and same_body:
                joined.append(_with_views(dict(lower, sender=upper.get('sender') or lower.get('sender')), upper, lower))
            else:
                try:
                    joined.append(_with_views(stitch_bubble(upper, lower), upper, lower))
                except ValueError as error:
                    last_error = str(error)
                    break
        else:
            candidates.append((max(geometry_scores, default=0), older[:-length]+joined+newer[length:]))
    if len(candidates) > 1:
        best = max(score for score, _ in candidates)
        candidates = [candidate for candidate in candidates if candidate[0] == best] if best else candidates
    if len(candidates) != 1:
        raise ValueError('ambiguous_page_overlap' if candidates else last_error or 'page_overlap_missing')
    return candidates[0][1]
