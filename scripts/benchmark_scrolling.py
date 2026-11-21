"""A/B scrolling against an existing burst; never inject or send messages."""
import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time
from contextlib import ExitStack

from fxxk_desk.chat import ChatDesktop
from fxxk_desk.gateway import Gateway
from fxxk_desk.lease import desktop_lease
from fxxk_desk.relay import Relay, RelayModels, load_route
from fxxk_desk.workflow import Workflow, fingerprint
from fxxk_desk.interference import UserInterference


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', default='artifacts/scroll-test-manifest.json')
    parser.add_argument('--config', default='config.local.toml')
    parser.add_argument('--route', default='ops_to_test')
    parser.add_argument('--modes', nargs='+', choices=['stepwise', 'batch'], default=['stepwise', 'batch'])
    parser.add_argument('--attempts', type=int, default=1, help='Bounded retries, included in total time')
    args = parser.parse_args()
    if not 1 <= args.attempts <= 3:
        raise ValueError('Attempts must be between 1 and 3')
    manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    connection = sqlite3.connect(manifest['database'])
    try:
        first = connection.execute('SELECT messages FROM capture_pages ORDER BY id LIMIT 1').fetchone()
    finally:
        connection.close()
    if not first:
        raise RuntimeError('No archived pre-burst baseline available')
    anchor = [fingerprint(m) for m in json.loads(first[0]) if m.get('complete')]
    if not anchor:
        raise RuntimeError('Baseline has no complete anchor messages')
    route, source, destination = load_route(args.config, args.route)
    directory = Path('artifacts') / ('scroll-benchmark-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True)
    report = {'batch': manifest['batch'], 'runs': [], 'source_messages_injected': 0, 'destination_messages_sent': 0}
    with desktop_lease(), ExitStack() as stack:
        backend = stack.enter_context(ChatDesktop())
        gateway = Gateway()
        models = RelayModels(backend, gateway)
        for mode in args.modes:
            target = replace(source, scroll_mode=mode, backfill_max_seconds=120, backfill_max_pages=60)
            backend.ensure_app(target)
            if not backend.chat_is_open(target.chat):
                from fxxk_desk.targets import open_target
                open_target(target, backend)
            backend.scroll_chat('down', True)
            # Exclude OCR/model initialization and viewport setup equally.
            backend.scroll_profiles.clear()
            backend.metrics['frame_captures'] = backend.metrics['ocr_calls'] = 0
            backend.metrics['ocr_seconds'] = 0.0
            backend.metrics['scroll_actions'].clear()
            workflow = Workflow(route['source'], target, backend, models, str(directory / (mode + '.sqlite3')))
            try:
                # A fresh isolated database is seeded from the archived baseline.
                # The original test/production checkpoints are never modified.
                with workflow.db:
                    workflow.db.execute('INSERT INTO snapshots VALUES(?,?,?)', (workflow.scope, 'benchmark-seed', json.dumps(anchor)))
                relay = Relay(workflow, destination, backend, models, args.route)
                started = time.monotonic()
                attempts = []
                for number in range(args.attempts):
                    while True:
                        try:
                            result = relay.collect()
                            break
                        except UserInterference:
                            workflow.record_interference('benchmark')
                            print(json.dumps({'status': 'paused_for_user'}), flush=True)
                            backend.wait_for_user()
                    attempts.append(result)
                    if result.get('caught_up'):
                        break
                    print(json.dumps({'mode': mode, 'attempt': number + 1, 'recovery': result}, ensure_ascii=False), flush=True)
                elapsed = time.monotonic() - started
                texts = [''.join(json.loads(row[0])['text'].split()) for row in workflow.db.execute('SELECT payload FROM messages ORDER BY id')]
                counts = {marker: sum(marker in text for text in texts) for marker in manifest['accepted']}
                actions = list(backend.metrics['scroll_actions'])
                run = {'mode': mode, 'elapsed_seconds': round(elapsed, 3), 'recovery': result, 'attempts': attempts,
                       'frame_captures': backend.metrics['frame_captures'], 'ocr_calls': backend.metrics['ocr_calls'],
                       'ocr_seconds': round(backend.metrics['ocr_seconds'], 3),
                       'scroll_feedback_captures': sum(a['captures'] for a in actions),
                       'wheel_events': sum(a['wheel_events'] for a in actions), 'scroll_actions': actions,
                       'marker_counts': counts, 'passed': result.get('caught_up', False) and all(n == 1 for n in counts.values())}
                report['runs'].append(run)
                (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({k: v for k, v in run.items() if k != 'scroll_actions'}, ensure_ascii=False), flush=True)
            finally:
                workflow.close()
    by_mode = {run['mode']: run for run in report['runs']}
    if all(mode in by_mode and by_mode[mode]['passed'] for mode in ('stepwise', 'batch')):
        old, new = by_mode['stepwise'], by_mode['batch']
        report['comparison'] = {
            'elapsed_reduction_percent': round(100 * (1 - new['elapsed_seconds'] / old['elapsed_seconds']), 2),
            'capture_reduction_percent': round(100 * (1 - new['frame_captures'] / old['frame_captures']), 2),
            'both_complete_without_duplicates': True,
        }
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Report:', directory / 'report.json', flush=True)
    if not all(run['passed'] for run in report['runs']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
