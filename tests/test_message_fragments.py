import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from fxxk_desk.backfill import Backfill, Page
from fxxk_desk.message_fragments import stitch_bubble
from fxxk_desk.targets import Target
from fxxk_desk.workflow import Workflow, fingerprint
from fxxk_desk.relay import Relay
from fxxk_desk.interference import UserInterference


def message(name, lines=1):
    return {'text': '\n'.join(f'{name} unique row {i:03}' for i in range(lines)),
            'sender': 'Sender', 'direction': 'incoming', 'complete': True}


class FragmentTranscript:
    supports_backfill = True

    def __init__(self, items, directory=None, height=12, step=5):
        self.items, self.height, self.step = items, height, step
        self.directory, self.moves = directory, []
        self.position = max(0, self.length()-height)

    def length(self):
        return sum(len(item['text'].splitlines()) for item in self.items)

    def read(self):
        start, result = 0, []
        for item in self.items:
            lines = item['text'].splitlines()
            end = start+len(lines)
            lo, hi = max(start, self.position), min(end, self.position+self.height)
            if lo < hi:
                top, bottom = start < self.position, end > self.position+self.height
                result.append(dict(item, text='\n'.join(lines[lo-start:hi-start]),
                                   sender=None if top else item['sender'], complete=not top and not bottom,
                                   top_clipped=top, bottom_clipped=bottom, bubble_left=80, bubble_right=500,
                                   bubble_rect=[80,(lo-self.position)*33,500,(hi-self.position)*33],
                                   ocr_reliable=True))
            start = end
        digest = hashlib.sha256(json.dumps(result).encode()).hexdigest()
        return Page(result, 'page-'+str(self.position), digest)

    def scroll(self, direction, fast=False):
        self.moves.append(direction)
        if direction == 'up':
            self.position = max(0, self.position-self.step)
        else:
            self.position = max(0, self.length()-self.height) if fast else min(max(0, self.length()-self.height), self.position+self.step)

    scroll_chat = scroll

    def ensure_app(self, target):
        pass

    def chat_is_open(self, chat):
        return True

    def message_snapshot(self, chat):
        page = self.read()
        path = Path(self.directory)/(page.image_hash+'.json')
        path.write_text(json.dumps(page.messages), encoding='utf-8')
        return path


class Reader:
    extractor_version = 'fragment-test-v1'

    def extract_messages(self, path):
        return json.loads(Path(path).read_text(encoding='utf-8'))


class MessageFragmentTests(unittest.TestCase):
    def test_message_spanning_many_screens_is_one_complete_ordered_message(self):
        old = [message('old A'), message('old B')]
        long = message('long message', 67)
        following = [message('after A'), message('after B')]
        desktop = FragmentTranscript(old+[long]+following)
        result = Backfill(desktop.read, desktop.scroll, fingerprint).recover(
            [fingerprint(item) for item in old], desktop.read(), had_checkpoint=True)
        self.assertEqual(result.status, 'recovered', result.reason)
        self.assertEqual([item['text'] for item in result.messages], [long['text']]+[item['text'] for item in following])
        assembled = result.messages[0]
        self.assertTrue(assembled['complete'])
        self.assertEqual(assembled['sender'], 'Sender')
        self.assertGreater(len(assembled['fragment_screenshots']), 4)

    def test_initial_observation_assembles_but_does_not_forward_history(self):
        desktop = FragmentTranscript([message('long baseline', 48)])
        result = Backfill(desktop.read, desktop.scroll, fingerprint).recover([], desktop.read())
        self.assertEqual(result.status, 'baseline', result.reason)
        self.assertEqual(result.messages, [])
        self.assertEqual(result.checkpoint[0]['text'], desktop.items[0]['text'])

    def test_adjacent_long_messages_remain_two_messages(self):
        old = [message('old A'), message('old B')]
        first, second = message('long A', 43), message('long B', 47)
        desktop = FragmentTranscript(old+[first,second])
        result = Backfill(desktop.read, desktop.scroll, fingerprint).recover(
            [fingerprint(item) for item in old], desktop.read(), had_checkpoint=True)
        self.assertEqual([item['text'] for item in result.messages], [first['text'],second['text']], result.reason)

    def test_adjacent_identical_tables_with_different_ocr_line_wrapping(self):
        class Reflow(FragmentTranscript):
            def read(self):
                page = super().read()
                for item in page.messages:
                    if item.get('top_clipped') or item.get('bottom_clipped'):
                        stream = ''.join(item['text'].split())
                        width = 47+self.position%9
                        item['text'] = '\n'.join(stream[i:i+width] for i in range(0,len(stream),width))
                return page
        old = [message('old A'),message('old B')]
        table = message('table unique data', 43)
        desktop = Reflow(old+[dict(table),dict(table)])
        result = Backfill(desktop.read,desktop.scroll,fingerprint).recover(
            [fingerprint(item) for item in old],desktop.read(),had_checkpoint=True)
        self.assertEqual(len(result.messages),2,result.reason)
        for item in result.messages:
            self.assertEqual(''.join(item['text'].split()),''.join(table['text'].split()))
            self.assertTrue(item['complete'])

    def test_fragment_failure_latches_scroll_until_task_restart(self):
        from unittest.mock import patch
        from fxxk_desk.backfill import Recovery
        with tempfile.TemporaryDirectory() as directory:
            old=[message('old A'),message('old B')]
            desktop=FragmentTranscript(list(old),directory)
            workflow=Workflow('source',Target('app.exe','App','Source'),desktop,Reader(),str(Path(directory)/'chat.db'))
            try:
                workflow.collect()
                desktop.items.append(message('long incoming',50))
                desktop.position=desktop.length()-desktop.height
                with patch('fxxk_desk.backfill.Backfill.recover',return_value=Recovery(reason='fragment_edges_do_not_join')):
                    workflow.collect()
                moves=len(desktop.moves)
                result=workflow.collect()
                self.assertEqual(len(desktop.moves),moves)
                self.assertIn('history_retry_paused',result['gap_reason'])
            finally:
                workflow.close()

    def test_geometry_mismatch_and_missing_overlap_never_join(self):
        first = dict(message('item', 8), bottom_clipped=True, top_clipped=False,
                     bubble_left=80, bubble_right=500, ocr_reliable=True)
        second = dict(first, text='\n'.join(first['text'].splitlines()[4:]),
                      top_clipped=True, bottom_clipped=False, bubble_right=650)
        with self.assertRaisesRegex(ValueError, 'geometry'):
            stitch_bubble(first, second)
        second.update(bubble_right=500, text='no common row alpha\nno common row beta')
        with self.assertRaisesRegex(ValueError, 'overlap'):
            stitch_bubble(first, second)

    def test_starting_in_middle_collects_lower_and_upper_edges(self):
        old = [message('old A'), message('old B')]
        long = message('long middle', 40)
        desktop = FragmentTranscript(old+[long])
        desktop.position = 15
        result = Backfill(desktop.read, desktop.scroll, fingerprint).recover(
            [fingerprint(item) for item in old], desktop.read(), had_checkpoint=True)
        self.assertEqual([item['text'] for item in result.messages], [long['text']], result.reason)
        self.assertIn('up', desktop.moves)
        self.assertIn('down', desktop.moves)

    def test_budget_exhaustion_never_commits_partial_message(self):
        old = [message('old A'), message('old B')]
        desktop = FragmentTranscript(old+[message('very long', 100)])
        result = Backfill(desktop.read, desktop.scroll, fingerprint, max_pages=4).recover(
            [fingerprint(item) for item in old], desktop.read(), had_checkpoint=True)
        self.assertEqual(result.status, 'gap')
        self.assertFalse(result.messages)
        self.assertFalse(result.checkpoint)

    def test_human_interruption_during_stitching_does_not_restore_scroll(self):
        desktop = FragmentTranscript([message('old A'), message('old B'), message('long', 50)])
        def interrupt(direction, fast=False):
            desktop.moves.append(direction)
            raise UserInterference('user took over')
        with self.assertRaises(UserInterference):
            Backfill(desktop.read, interrupt, fingerprint).recover(
                [fingerprint(message('old A')), fingerprint(message('old B'))], desktop.read(), had_checkpoint=True)
        self.assertEqual(desktop.moves, ['up'])

    def test_repeated_or_conflicting_fragments_are_not_guessed(self):
        first = dict(message('repeat', 1), text='repeated line A\nrepeated line B\n'*4,
                     bottom_clipped=True, top_clipped=False, bubble_left=80, bubble_right=500, ocr_reliable=True)
        second = dict(first, top_clipped=True, bottom_clipped=False)
        with self.assertRaises(ValueError):
            stitch_bubble(first, second)
        second.update(text='different line\nnot overlapping', sender='Another sender')
        with self.assertRaises(ValueError):
            stitch_bubble(first, second)

    def test_workflow_queues_long_message_once_and_reuses_assembled_view(self):
        with tempfile.TemporaryDirectory() as directory:
            old = [message('old A'), message('old B')]
            desktop = FragmentTranscript(list(old), directory)
            workflow = Workflow('source', Target('app.exe', 'App', 'Source'), desktop, Reader(), str(Path(directory)/'chat.db'))
            relay = Relay(workflow, Target('app.exe', 'App', 'Destination'), desktop, Mock(), 'route')
            try:
                relay.collect()
                long = message('long incoming', 52)
                desktop.items.append(long)
                desktop.position = max(0, desktop.length()-desktop.height)
                result = relay.collect()
                self.assertEqual(result['relay_queued'], 1, result)
                payload = json.loads(workflow.db.execute('SELECT m.payload FROM messages m JOIN relay_outbox o ON o.source_id=m.id').fetchone()[0])
                self.assertEqual(payload['text'], long['text'])
                moves = len(desktop.moves)
                self.assertEqual(relay.collect()['relay_queued'], 0)
                self.assertEqual(len(desktop.moves), moves)
                self.assertEqual(workflow.db.execute('SELECT COUNT(*) FROM relay_outbox').fetchone()[0], 1)
            finally:
                workflow.close()
