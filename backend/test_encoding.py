import unittest
from unittest.mock import patch

from backend.encoding import encoder_threads


class EncodingConfigurationTests(unittest.TestCase):
    def test_desktop_defaults_stay_unchanged(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(encoder_threads(4), '4')
            self.assertEqual(encoder_threads(2), '2')

    def test_server_override_is_bounded(self):
        with patch.dict('os.environ', {'DATAMARK_FFMPEG_ENCODER_THREADS': '2'}):
            self.assertEqual(encoder_threads(4), '2')
        for invalid in ('0', '-1', '9', 'many', ''):
            with self.subTest(value=invalid), patch.dict('os.environ', {'DATAMARK_FFMPEG_ENCODER_THREADS': invalid}):
                self.assertEqual(encoder_threads(4), '4')
