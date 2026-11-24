from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

spec = importlib.util.spec_from_file_location('webhook_tool', Path(__file__).resolve().parents[1]/'tools/webhook/send_message.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class WebhookToolTests(unittest.TestCase):
    def test_chinese_multiline_and_quotes_are_utf8_not_question_marks(self):
        message = '中文测试：他说“收到”\nCPU 95% "测试"'
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'{"errcode":0}')
        tool.send('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=example', tool.build_payload(message), opener=opener)
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data.decode('utf-8'))['text']['content'], message)
        opener.open.assert_called_once()

    def test_empty_and_utf8_byte_limit(self):
        for text in ('', ' \n', '中'*683):
            with self.assertRaises(tool.SendError):
                tool.build_payload(text)
        self.assertEqual(tool.build_payload('中'*682)['text']['content'], '中'*682)

    def test_network_error_is_redacted_and_not_retried(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError('secret-key-in-url')
        with self.assertRaises(tool.SendError) as error:
            tool.send('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret', tool.build_payload('test'), opener=opener)
        self.assertNotIn('secret', str(error.exception))
        opener.open.assert_called_once()

    def test_api_error_is_not_reported_as_success(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'{"errcode":93000}')
        with self.assertRaises(tool.SendError):
            tool.send('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=example', tool.build_payload('test'), opener=opener)

    def test_chinese_and_quotes_preview_does_not_send(self):
        message = '这是一条带 "引号" 的中文消息'
        output = io.StringIO()
        with patch.object(tool, 'send') as send, redirect_stdout(output):
            self.assertEqual(tool.main([message, '--preview']), 0)
            send.assert_not_called()
        self.assertIn('中文消息', output.getvalue())
        self.assertIn('\\"引号\\"', output.getvalue())

    def test_utf8_bom_file_and_private_url_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            message = root/'message.txt'
            message.write_text('中文第一行\n第二行', encoding='utf-8-sig')
            config = root/'webhook.local.json'
            url = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=example'
            config.write_text(json.dumps({'webhook_url':url}), encoding='utf-8')
            self.assertEqual(tool.load_url(config), url)
            with patch.object(tool, 'send') as send, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(tool.main(['--file', str(message), '--preview']), 0)
                send.assert_not_called()
            self.assertIn('中文第一行', output.getvalue())
