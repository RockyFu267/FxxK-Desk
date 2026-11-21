import unittest
from unittest.mock import Mock

import numpy as np
from PIL import Image, ImageDraw

from fxxk_desk.chat import ChatDesktop


class EditorTests(unittest.TestCase):
    def check(self, image):
        backend = object.__new__(ChatDesktop)
        backend.np = np
        backend.calibrated_frame = Mock(return_value=(image, None))
        return backend.editor_empty((0, 0, 80, 50))

    def test_blank_editor_is_empty(self):
        self.assertTrue(self.check(Image.new('RGB', (80, 50), 'white')))

    def test_steady_remote_caret_is_not_a_draft(self):
        image = Image.new('RGB', (80, 50), 'white')
        ImageDraw.Draw(image).line((10, 5, 10, 23), fill='black')
        self.assertTrue(self.check(image))

    def test_real_text_is_not_overwritten(self):
        image = Image.new('RGB', (80, 50), 'white')
        ImageDraw.Draw(image).text((10, 5), 'existing draft', fill='black')
        self.assertFalse(self.check(image))
