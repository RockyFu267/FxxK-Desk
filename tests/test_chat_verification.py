import unittest
from unittest.mock import Mock
from types import SimpleNamespace
import numpy as np
from PIL import Image
from fxxk_desk.chat import ChatDesktop


class ChatVerificationTests(unittest.TestCase):
    def test_hover_tint_and_pointer_do_not_discard_timestamp(self):
        import tempfile
        from pathlib import Path

        backend = ChatDesktop.__new__(ChatDesktop)
        backend.np = np
        backend.target = SimpleNamespace(message_rect=(0, 0, 200, 180), chat='Source')
        backend.chat_is_open = Mock(return_value=True)
        rect = SimpleNamespace(left=0, top=0)
        before = np.full((180, 200, 3), 246, dtype=np.uint8)
        before[80:125, 50:170] = 224
        before[92:109, 65:130] = 45
        after = before.copy()
        after[80:125, 50:170] = (195, 221, 247)
        after[92:109, 65:130] = 50
        after[90:113, 154:168] = 20
        backend.calibrated_frame = Mock(side_effect=[(Image.fromarray(before), rect),
                                                     (Image.fromarray(after), rect)])
        backend.mouse = SimpleNamespace(click=Mock(side_effect=lambda coords, observe: observe()))
        backend.recognize = Mock(return_value='11/24 14:25:22')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'before.png'
            Image.fromarray(before).save(path)
            timestamp = backend.reveal_message_time(SimpleNamespace(screenshot=path), (50, 80, 170, 125))
        self.assertEqual(timestamp, '11/24 14:25:22')
        self.assertEqual(backend.mouse.click.call_args.args[0], (163, 96))

    def test_multiline_editor_fallback_checks_each_row_in_order(self):
        backend = ChatDesktop.__new__(ChatDesktop)
        backend.np = np
        backend.text_matches = Mock(return_value=False)
        pixels = np.full((60, 100, 3), 255, dtype=np.uint8)
        pixels[6:16, 8:85] = 0
        pixels[26:36, 8:85] = 0
        image = Image.fromarray(pixels)
        backend.recognize = Mock(side_effect=['原文 #61 2/8', '原文 #61 2/8', 'M0.00', 'M0.00'])
        self.assertTrue(backend.editor_text_matches(image, '原文 #61 2/8\nM0.00'))
        backend.recognize = Mock(side_effect=['原文 #61 2/8', '原文 #61 2/8', 'M0.01', 'M0.01'])
        self.assertFalse(backend.editor_text_matches(image, '原文 #61 2/8\nM0.00'))

    def test_floating_history_button_is_not_a_message(self):
        import tempfile
        from pathlib import Path
        backend = ChatDesktop.__new__(ChatDesktop)
        backend.np = np
        pixels = np.full((600, 800, 3), 246, dtype=np.uint8)
        pixels[100:200, 80:400] = (228, 231, 235)
        pixels[540:585, 600:780] = 255
        def box(x, y, width):
            return np.array([[x,y],[x+width,y],[x+width,y+20],[x,y+20]])
        backend.ocr = Mock(return_value=([(box(100, 130, 100), '正文内容', .99),
                                          (box(620, 550, 130), '回到最新位置', .99)], None))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'screen.png'
            Image.fromarray(pixels).save(path)
            messages = backend.extract_local_messages(path)
        self.assertEqual([message['text'] for message in messages], ['正文内容'])

    def test_only_windows_reported_owned_caret_is_masked(self):
        backend = ChatDesktop.__new__(ChatDesktop)
        backend.target = SimpleNamespace(editor_rect=(0, 0, 100, 100))
        backend.window = SimpleNamespace(pid=42)
        def caret_info(thread, pointer):
            info = pointer._obj
            info.caret = 123
            info.rect.left, info.rect.top = 10, 10
            info.rect.right, info.rect.bottom = 11, 35
            return 1
        def owner(hwnd, pointer):
            pointer._obj.value = 42
        def to_screen(hwnd, pointer):
            pointer._obj.x += 10
            pointer._obj.y += 10
            return 1
        u = SimpleNamespace(GetGUIThreadInfo=Mock(side_effect=caret_info),
                            GetWindowThreadProcessId=Mock(side_effect=owner),
                            ClientToScreen=Mock(side_effect=to_screen))
        backend.desktop = SimpleNamespace(u=u)
        pixels = np.full((100, 100, 3), 255, dtype=np.uint8)
        pixels[10:35, 10] = 0
        pixels[10:35, 20] = 0
        image = Image.fromarray(pixels)
        frame = SimpleNamespace(left=10, top=10)
        masked = np.array(backend.editor_image(image, frame))
        self.assertTrue((masked[10:35, 10] == 255).all())
        self.assertTrue((masked[10:35, 20] == 0).all())
        backend.window.pid = 99
        self.assertTrue((np.array(backend.editor_image(image, frame))[10:35, 10] == 0).all())

    def test_editor_text_uses_larger_ocr_when_line_end_hyphen_was_dropped(self):
        backend = ChatDesktop.__new__(ChatDesktop)
        backend.recognize = Mock(side_effect=['消息FDabcdef1234-1', '消息FD-abcdef1234-1'])
        image = Image.new('RGB', (200, 100), 'white')
        self.assertTrue(backend.text_matches(image, '消息 FD-abcdef1234-1'))
        enlarged = backend.recognize.call_args.args[0]
        self.assertEqual(enlarged.size, (600, 300))
        backend.recognize = Mock(return_value='消息 FD-abcdef1234-2')
        self.assertFalse(backend.text_matches(image, '消息 FD-abcdef1234-1'))
    def test_group_count_suffix_is_accepted_without_matching_similar_group(self):
        backend = ChatDesktop.__new__(ChatDesktop)
        rect = SimpleNamespace(left=-2143, top=-673, width=lambda: 1632, height=lambda: 1645)
        backend.regions = Mock(return_value=(rect, [('示例通知 (3)', 420, 179), ('示例通知二群', 420, 250)]))
        backend.target = SimpleNamespace(message_rect=(602, 96, 1384, 1236))
        backend.guard = Mock()
        backend.frame = Mock(return_value=(None, rect))
        backend.mouse = Mock()
        self.assertTrue(backend.open_exact_result('示例通知'))
        backend.mouse.click.assert_called_once_with(coords=(-1723, -494))

    def test_high_dpi_caret_is_empty_but_extra_text_pixels_are_not(self):
        backend = ChatDesktop.__new__(ChatDesktop)
        backend.np = np
        pixels = np.full((250, 724, 3), 255, dtype=np.uint8)
        pixels[11:36, 14] = 0
        backend.calibrated_frame = Mock(return_value=(Image.fromarray(pixels), None))
        self.assertTrue(backend.editor_empty((0, 0, 724, 250)))
        pixels[15:24, 25:33] = 0
        backend.calibrated_frame.return_value = (Image.fromarray(pixels), None)
        self.assertFalse(backend.editor_empty((0, 0, 724, 250)))
