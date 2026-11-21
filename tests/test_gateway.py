import unittest
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from fxxk_desk.gateway import Gateway, parse_result


class ResponseTests(unittest.TestCase):
    def test_project_key_overrides_unrelated_machine_key(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / '.env'
            config.write_text('OPENAI_BASE_URL=http://localhost/v1\nOPENAI_API_KEY=project-test-key\n', encoding='utf-8')
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'unrelated-machine-key'}, clear=True):
                with patch('fxxk_desk.gateway.Path', return_value=config):
                    gateway = Gateway()
                self.assertEqual(gateway.key, 'project-test-key')

    def test_reasoning_is_not_content(self):
        self.assertEqual(parse_result({"choices": [{"finish_reason": "stop", "message": {
            "content": "visible button", "reasoning_content": "private reasoning"}}]}), "visible button")

    def test_truncated_response_rejected(self):
        with self.assertRaises(ValueError):
            parse_result({"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]})

    def test_empty_content_rejected(self):
        with self.assertRaises(ValueError):
            parse_result({"choices": [{"message": {"content": None}}]})
