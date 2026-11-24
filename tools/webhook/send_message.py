"""Send custom UTF-8 text through a locally configured group webhook (stdlib only)."""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


class SendError(RuntimeError):
    pass


def load_url(config=None):
    path = Path(config) if config else HERE / 'webhook.local.json'
    if path.exists():
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        url = value.get('webhook_url', '')
    elif config:
        raise SendError('找不到指定的本地配置文件')
    else:
        url = os.environ.get('SOURCE_WEBHOOK_URL', '')
        env_file = ROOT / '.env'
        if not url and env_file.exists():
            for line in env_file.read_text(encoding='utf-8-sig').splitlines():
                key, separator, value = line.strip().partition('=')
                if separator and key.strip() == 'SOURCE_WEBHOOK_URL':
                    url = value.strip().strip('\"\'')
                    break
    if not isinstance(url, str):
        raise SendError('本地 webhook_url 配置无效')
    parts = urllib.parse.urlsplit(url.strip())
    keys = urllib.parse.parse_qs(parts.query).get('key', [])
    if (parts.scheme != 'https' or parts.hostname != 'qyapi.weixin.qq.com'
            or parts.path != '/cgi-bin/webhook/send' or parts.username or parts.password
            or parts.fragment or len(keys) != 1 or not keys[0]):
        raise SendError('请在 webhook.local.json 中配置有效的群机器人 webhook_url')
    return url.strip()


def build_payload(message):
    if not message.strip():
        raise SendError('消息不能为空')
    size = len(message.encode('utf-8'))
    if size > 2048:
        raise SendError(f'消息为 {size} 个 UTF-8 字节，本工具限 2048 字节；请缩短后发送')
    return {'msgtype': 'text', 'text': {'content': message}}


def send(url, payload, timeout=15, proxy=None, opener=None):
    if not 1 <= timeout <= 120:
        raise SendError('超时应为 1～120 秒')
    if proxy and urllib.parse.urlsplit(proxy).scheme not in ('http', 'https'):
        raise SendError('代理地址必须使用 http:// 或 https://')
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(url, body, headers={'Content-Type': 'application/json; charset=utf-8'}, method='POST')
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy})) if proxy else urllib.request.build_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise SendError(f'接口返回 HTTP {error.code}，请检查网络或配置') from None
    except (OSError, urllib.error.URLError):
        raise SendError('网络异常，发送结果无法确认；请先查看群，本工具不会自动重试') from None
    except (ValueError, UnicodeError):
        raise SendError('接口响应无法解析，发送结果无法确认；请先查看群') from None
    if not isinstance(result, dict) or type(result.get('errcode')) is not int:
        raise SendError('接口响应缺少结果码，发送结果无法确认；请先查看群')
    if result['errcode'] != 0:
        raise SendError(f"机器人拒绝消息，错误码 {result['errcode']}；请检查本地 webhook 配置")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='向已配置的群机器人 webhook 发送自定义文本消息')
    parser.add_argument('message', nargs='?', help='消息内容；省略时交互输入')
    parser.add_argument('--file', type=Path, help='读取 UTF-8 文本文件，支持多行')
    parser.add_argument('--config', type=Path, help='本地 JSON 配置文件，默认脚本旁的 webhook.local.json')
    parser.add_argument('--proxy', help='可选代理地址，如 http://127.0.0.1:1099')
    parser.add_argument('--timeout', type=int, default=15)
    parser.add_argument('--preview', action='store_true', help='只预览消息与 UTF-8 请求体，不发送')
    args = parser.parse_args(argv)
    try:
        if args.message is not None and args.file is not None:
            raise SendError('只能指定一种消息输入方式')
        message = args.file.read_text(encoding='utf-8-sig') if args.file else args.message
        if message is None:
            message = input('请输入要发送到已配置群聊的消息：')
        payload = build_payload(message)
        if args.preview:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            print(f"仅预览，未发送。UTF-8 字节数：{len(message.encode('utf-8'))}")
            return 0
        send(load_url(args.config), payload, args.timeout, args.proxy)
        print('发送成功：群机器人接口已接受消息（errcode=0）。')
        return 0
    except (SendError, OSError, ValueError, EOFError) as error:
        # Never echo a URL or transport exception containing the secret key.
        detail = str(error) if isinstance(error, SendError) else '读取消息或本地配置失败，请检查文件路径和 UTF-8 编码'
        print('发送失败：'+detail, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\n已中断；若请求已发出，请先查看群确认结果。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
