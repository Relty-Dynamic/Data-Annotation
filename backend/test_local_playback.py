import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from backend.local_playback import render_local_fast, render_local_thumbnail


class LocalPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.normal = self.directory / 'normal.mp4'
        self.normal.write_bytes(b'normal')
        self.service = SimpleNamespace(tool=Mock(return_value=Path('ffmpeg.exe')),
                                       probe=Mock(return_value={'duration_ms': 100, 'media_start_seconds': 0}),
                                       valid_thumbnail=Mock(return_value=True))
        self.stopping = threading.Event()

    @staticmethod
    def render_file(args, *unused, **kwargs):
        Path(args[-1]).write_bytes(b'completed')

    def test_fast_commits_only_valid_timing(self):
        target = self.directory / 'fast.mp4'
        target.write_bytes(b'old')
        with patch('backend.local_playback.run_ffmpeg', side_effect=self.render_file) as run:
            render_local_fast(self.service, self.normal, target, 2000, lambda _: None, self.stopping)
        self.assertEqual(target.read_bytes(), b'completed')
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('-i') + 1], str(self.normal))
        self.assertFalse(target.with_suffix('.partial.mp4').exists())

    def test_bad_timestamps_keep_previous_published_file(self):
        target = self.directory / 'fast.mp4'
        target.write_bytes(b'old')
        self.service.probe.return_value = {'duration_ms': 100, 'media_start_seconds': 2}
        with patch('backend.local_playback.run_ffmpeg', side_effect=self.render_file):
            with self.assertRaises(HTTPException):
                render_local_fast(self.service, self.normal, target, 2000, lambda _: None, self.stopping)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertFalse(target.with_suffix('.partial.mp4').exists())

    def test_cancellation_before_publish_keeps_previous_file(self):
        target = self.directory / 'fast.mp4'
        target.write_bytes(b'old')
        def finish_then_cancel(args, *unused, **kwargs):
            self.render_file(args)
            self.stopping.set()
        with patch('backend.local_playback.run_ffmpeg', side_effect=finish_then_cancel):
            with self.assertRaises(HTTPException):
                render_local_fast(self.service, self.normal, target, 2000, lambda _: None, self.stopping)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertFalse(target.with_suffix('.partial.mp4').exists())

    def test_invalid_cover_cannot_replace_old_cover(self):
        target = self.directory / 'cover.jpg'
        target.write_bytes(b'old')
        self.service.valid_thumbnail.return_value = False
        with patch('backend.local_playback.run_ffmpeg', side_effect=self.render_file):
            with self.assertRaises(HTTPException):
                render_local_thumbnail(self.service, self.normal, target, 2000, self.stopping)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertFalse(target.with_suffix('.partial.jpg').exists())

    def test_stopped_job_never_starts_encoder(self):
        self.stopping.set()
        with patch('backend.local_playback.run_ffmpeg') as run:
            with self.assertRaises(HTTPException):
                render_local_thumbnail(self.service, self.normal, self.directory / 'cover.jpg', 2000, self.stopping)
        run.assert_not_called()
