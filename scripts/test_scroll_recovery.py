"""Explicit real-desktop test. Injects synthetic messages ONLY with --inject.

Run from the repository with PYTHONPATH=src. It never sends to the destination.
The run manifest permits capture-only retries without injecting another batch.
"""
import argparse
import json
from pathlib import Path
import time
import uuid
from dataclasses import replace
from contextlib import ExitStack

from fxxk_desk.chat import ChatDesktop
from fxxk_desk.gateway import Gateway
from fxxk_desk.lease import desktop_lease
from fxxk_desk.relay import Relay, RelayModels, load_route, post_webhook_text
from fxxk_desk.workflow import Workflow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.local.toml')
    parser.add_argument('--route', default='ops_to_test')
    parser.add_argument('--manifest', default='artifacts/scroll-test-manifest.json')
    parser.add_argument('--inject', action='store_true')
    parser.add_argument('--count', type=int, default=8)
    parser.add_argument('--max-seconds', type=float, default=120)
    args = parser.parse_args()
    if not 2 <= args.count <= 10:
        raise ValueError('Count must be between 2 and 10')
    if not 2 <= args.max_seconds <= 120:
        raise ValueError('Recovery budget must be between 2 and 120 seconds')
    route, source, destination = load_route(args.config, args.route)
    source = replace(source, backfill_max_seconds=args.max_seconds, backfill_max_pages=60)
    manifest_path = Path(args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    gateway = Gateway()  # Loads project-only credentials; never logs them.
    with desktop_lease(), ExitStack() as stack:
        backend = stack.enter_context(ChatDesktop())
        models = RelayModels(backend, gateway)
        if args.inject:
            if manifest_path.exists():
                raise RuntimeError('Manifest already exists; use capture-only retry or a new manifest path')
            batch = 'SCROLL-' + uuid.uuid4().hex[:6]
            manifest = {'batch': batch, 'count': args.count, 'accepted': [],
                        'database': f'artifacts/{batch}.sqlite3'}
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        else:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        workflow = Workflow(route['source'], source, backend, models, manifest['database'])
        try:
            relay = Relay(workflow, destination, backend, models, args.route)
            if args.inject:
                baseline = relay.collect()
                print(json.dumps({'baseline': baseline}, ensure_ascii=False), flush=True)
                if not baseline.get('caught_up'):
                    raise RuntimeError('Baseline did not reach latest; no test messages injected')
                for number in range(1, manifest['count'] + 1):
                    marker = f"{manifest['batch']}-{number:02d}"
                    content = (f"刷屏补采测试 {marker}\n序号：{number:02d}/{manifest['count']:02d}\n"
                               f"测试主机：demo-{number:02d}\n模拟指标：CPU使用率\n"
                               "指标数值：95%\n持续时间：5分钟\n状态：模拟告警\n"
                               "此消息为自动化测试数据\n无需人工处理")
                    post_webhook_text(route.get('webhook_env', 'SOURCE_WEBHOOK_URL'), content)
                    manifest['accepted'].append(marker)
                    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
                    time.sleep(0.3)
                time.sleep(1)
            # At most three bounded recovery cycles; never resend test messages.
            attempts = []
            for _ in range(3):
                attempt_started = time.monotonic()
                result = relay.collect()
                result['elapsed_seconds'] = round(time.monotonic() - attempt_started, 2)
                attempts.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if result.get('caught_up'):
                    break
            rows = workflow.db.execute('SELECT payload FROM messages WHERE continuity_verified=1 ORDER BY id').fetchall()
            texts = [''.join(json.loads(row[0])['text'].split()) for row in rows]
            counts = {marker: sum(marker in text for text in texts) for marker in manifest['accepted']}
            manifest['result'] = {'attempts': attempts, 'marker_counts': counts,
                                  'passed': len(counts) == manifest['count'] and all(count == 1 for count in counts.values()),
                                  'destination_messages_sent': 0}
            manifest.setdefault('runs', []).append(manifest['result'])
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(manifest['result'], ensure_ascii=False, indent=2), flush=True)
            if not manifest['result']['passed']:
                raise SystemExit(1)
        finally:
            workflow.close()


if __name__ == '__main__':
    main()
