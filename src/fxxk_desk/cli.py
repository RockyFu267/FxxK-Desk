import argparse
import json
from pathlib import Path
import sys
import time

from .windows import Desktop
from .interference import UserInterference


def main():
    parser = argparse.ArgumentParser(description="Observe and open desktop applications")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("windows", help="List visible windows")
    sub.add_parser('status', help='Read verified task state and heartbeat')
    p = sub.add_parser('region', help='Visually move/resize a monitor-independent capture region')
    p.add_argument('--region-file', default='artifacts/capture-region.local.json')
    p.add_argument('--min-width', type=int, default=640)
    p.add_argument('--min-height', type=int, default=480)
    p.add_argument('--status', action='store_true', help='Open the saved-region status panel')
    p = sub.add_parser('observe-region', help='Capture the saved visual region')
    p.add_argument('--region-file', default='artifacts/capture-region.local.json')
    p.add_argument('--output', default='artifacts/region.png')
    p.add_argument('--analyze', action='store_true')
    for name in ("focus", "observe"):
        p = sub.add_parser(name)
        p.add_argument("--title", required=True)
        if name == "observe":
            p.add_argument("--output", default="artifacts/window.png")
            p.add_argument("--analyze", action="store_true", help="Send captured window to configured gateway")
    p = sub.add_parser("launch")
    p.add_argument("--exe", required=True, help="Explicit executable path")
    p = sub.add_parser("open", help="Open a configured application and chat")
    p.add_argument("target", help="Target name in configuration")
    p.add_argument("--config", default="config.local.toml")
    p = sub.add_parser("chat", help="Collect visible history, draft or send a reply")
    p.add_argument("action", choices=("collect", "history", "draft", "send", "watch"))
    p.add_argument("target")
    p.add_argument("--config", default="config.local.toml")
    p.add_argument("--database", default="artifacts/chat.sqlite3")
    p.add_argument("--reply-id", type=int)
    p.add_argument("--send", action="store_true", help="Explicitly enable automatic sending in watch mode")
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--instruction", default="你是群聊助手。用简洁中文回复新消息。对告警只确认收到和概括，不声称已排障；对测试消息说明收到测试。")
    p = sub.add_parser("relay", help="Relay source chat messages to another chat")
    p.add_argument("action", choices=("once", "watch", "simulate", "recover"))
    p.add_argument("route")
    p.add_argument("--config", default="config.local.toml")
    p.add_argument("--database", default="artifacts/chat.sqlite3")
    p.add_argument("--send", action="store_true")
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--relay-id", type=int)
    p.add_argument("--confirm-not-sent", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == 'status':
            from .runtime_status import read_status
            print(json.dumps(read_status(), ensure_ascii=False, indent=2))
            return 0
        if args.command == 'region':
            from .region import edit_region
            edit_region(args.region_file, min_width=args.min_width, min_height=args.min_height, status_only=args.status)
            return 0
        if args.command == 'observe-region':
            from .capture_area import capture_region
            from .lease import desktop_lease
            from .runtime_status import RuntimeStatus, utc_now
            with desktop_lease():
                with RuntimeStatus('region_once') as status:
                    status.update('capturing')
                    result = capture_region(args.region_file, args.output)
                    status.update('completed', capture_count=1, last_capture_at=utc_now(), last_check_at=utc_now())
            if args.analyze:
                from .gateway import Gateway
                from PIL import Image
                image = Image.open(args.output)
                image.thumbnail((1024, 1024))
                preview = Path(args.output).with_name(Path(args.output).stem + '.model.png')
                image.save(preview)
                result['analysis'] = Gateway().describe(preview)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "relay":
            from .relay import run
            run(args)
            return 0
        if args.command == "chat":
            if args.interval < 2 or args.interval > 3600:
                raise ValueError("Polling interval must be between 2 and 3600 seconds")
            if args.action == "send" and args.reply_id is None:
                raise ValueError("send requires --reply-id")
            if args.send and args.action != "watch":
                raise ValueError("--send is only valid for watch")
            from .targets import load_target
            from .chat import ChatDesktop
            from .gateway import Gateway
            from .workflow import Workflow
            target = load_target(args.config, args.target)
            if args.send and not target.auto_reply:
                raise ValueError("Set auto_reply=true for this target before enabling automatic sending")
            workflow = Workflow(args.target, target,
                                None if args.action == "history" else ChatDesktop(),
                                None if args.action == "history" else Gateway(), args.database)
            runtime = None
            if args.action != 'history':
                from .runtime_status import RuntimeStatus, utc_now
                runtime = RuntimeStatus('chat', task=args.target)
                runtime.__enter__()
            try:
                while True:
                    try:
                        if runtime:
                            runtime.update('sending' if args.action == 'send' else 'observing')
                        if args.action == "history":
                            result = workflow.history()
                        elif args.action == "send":
                            result = workflow.send(args.reply_id)
                        else:
                            result = workflow.collect()
                            if runtime:
                                runtime.update('watching', last_check_at=utc_now(), capture_count=runtime.value['capture_count'] + 1,
                                               new_messages_total=runtime.value['new_messages_total'] + result.get('new_messages', 0))
                            if args.action in ("draft", "watch"):
                                if runtime:
                                    runtime.update('summarizing')
                                result["reply"] = workflow.draft(args.instruction)
                                blocked = workflow.db.execute("SELECT COUNT(*) FROM outbox o JOIN messages m ON m.id=o.message_id WHERE m.scope=? AND o.state IN ('sending','unknown','blocked_input')", (workflow.scope,)).fetchone()[0]
                                if args.action == "watch" and args.send and not blocked and result["reply"].get("state") == "draft":
                                    if runtime:
                                        runtime.update('sending')
                                    result["send"] = workflow.send(result["reply"]["id"])
                    except UserInterference:
                        if runtime:
                            runtime.update('paused_for_user')
                        workflow.record_interference('cycle')
                        print(json.dumps({'status': 'paused_for_user'}, ensure_ascii=False), flush=True)
                        if args.action != 'watch':
                            raise
                        workflow.backend.wait_for_user()
                        continue
                    if runtime:
                        sent = int(result.get('send', {}).get('state') == 'sent' or (args.action == 'send' and result.get('state') == 'sent'))
                        runtime.update('watching' if args.action == 'watch' else 'completed', sent_total=runtime.value['sent_total'] + sent)
                    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                    if args.action != "watch":
                        break
                    time.sleep(args.interval)
            finally:
                if runtime:
                    runtime.__exit__(*sys.exc_info())
                workflow.close()
                if workflow.backend is not None:
                    workflow.backend.close()
            return 0
        if args.command == "open":
            from .targets import load_target, open_target
            target = load_target(args.config, args.target)
            from .chat import ChatDesktop
            backend = ChatDesktop()
            try:
                result = open_target(target, backend)
            finally:
                backend.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        desktop = Desktop()
        if args.command == "windows":
            result = [w.to_dict() for w in desktop.windows()]
        elif args.command == "launch":
            desktop.launch(args.exe)
            result = {"status": "launch_requested", "executable": str(Path(args.exe))}
        else:
            window = desktop.select(args.title)
            if args.command == "focus":
                desktop.activate(window)
                result = {"status": "foreground_verified", "window": window.to_dict()}
            else:
                if args.analyze:
                    from .gateway import Gateway
                    gateway = Gateway()  # Validate configuration before capturing.
                result = desktop.capture(window, args.output)
                if args.analyze:
                    result["analysis"] = gateway.describe(args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except UserInterference as error:
        print(json.dumps({'status': 'paused_for_user', 'detail': str(error)}, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, KeyError, ImportError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
