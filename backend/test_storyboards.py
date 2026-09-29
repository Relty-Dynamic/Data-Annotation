from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend.storyboards import StoryboardCache, keyframe_sampling_safe

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / ".tools" / "ffmpeg" / "bin" / "ffmpeg.exe"


class StoryboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="storyboard-test-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.cache = StoryboardCache(self.root)
        self.source = self.root / "cached.mp4"
        self.source.write_bytes(b"unchanged local preview")
        self.stopping = threading.Event()

    def tearDown(self):
        self.temp.cleanup()

    def fake_render(self, args, duration_ms, log_path, update, stopping):
        count = (duration_ms + 999) // 1000
        output = Path(args[-1]).parent
        for index in range((count + 99) // 100):
            (output / f"sheet-{index:05d}.jpg").write_bytes(b"\xff\xd8test\xff\xd9")
        self.assertIsNone(self.cache.read("p2_test", duration_ms))
        update(70)

    def render(self, duration=100001):
        return self.cache.render(self.source, "p2_test", duration, FFMPEG, lambda _: None, self.stopping)

    def test_completed_manifest_reuses_cache_without_source_or_encoder(self):
        with patch("backend.storyboards.run_ffmpeg", side_effect=self.fake_render) as run:
            manifest = self.render()
            self.assertEqual(manifest["frame_count"], 101)
            self.assertEqual(manifest["sheets"], ["sheet-00000.jpg", "sheet-00001.jpg"])
            self.source.unlink()
            self.assertEqual(self.render(), manifest)
            self.assertEqual(run.call_count, 1)
        self.assertEqual(self.cache.read("p2_test", 100001), manifest)
        self.assertTrue(self.cache.sheet_path("p2_test", "sheet-00001.jpg").is_file())

    def test_invalid_manifest_sheet_or_profile_is_not_ready(self):
        with patch("backend.storyboards.run_ffmpeg", side_effect=self.fake_render):
            original = self.render()
        directory = self.cache.directory("p2_test")
        marker = directory / "manifest.json"
        for overrides in ({"version": 2}, {"interval_ms": 2000}, {"frame_count": True},
                          {"sheets": ["../../secret.jpg"]}, {"sheets": ["sheet-00000.jpg"]}):
            marker.write_text(json.dumps({**original, **overrides}), encoding="utf-8")
            self.assertIsNone(self.cache.read("p2_test", 100001))
        marker.write_text(json.dumps(original), encoding="utf-8")
        self.assertIsNone(self.cache.read("p2_test", 99999))
        (directory / "sheet-00001.jpg").write_bytes(b"\xff\xd8truncated")
        self.assertIsNone(self.cache.read("p2_test", 100001))
        with self.assertRaises(HTTPException):
            self.cache.sheet_path("p2_test", "sheet-00000.jpg")

    def test_partial_render_and_cancel_never_publish(self):
        def incomplete(args, *rest):
            Path(args[-1]).parent.joinpath("sheet-00000.jpg").write_bytes(b"\xff\xd8partial")
        with patch("backend.storyboards.run_ffmpeg", side_effect=incomplete):
            with self.assertRaises(HTTPException):
                self.render()
        self.assertIsNone(self.cache.read("p2_test"))
        self.assertEqual(list(self.cache.directory("p2_test").glob(".partial-*")), [])
        self.stopping.set()
        with patch("backend.storyboards.run_ffmpeg") as run:
            with self.assertRaises(HTTPException):
                self.render()
            run.assert_not_called()
        self.assertEqual(self.source.read_bytes(), b"unchanged local preview")

    def test_encoder_failure_preserves_source_and_no_marker(self):
        with patch("backend.storyboards.run_ffmpeg", side_effect=HTTPException(422, "failure")):
            with self.assertRaises(HTTPException):
                self.render()
        self.assertIsNone(self.cache.read("p2_test"))
        self.assertFalse((self.cache.directory("p2_test") / "manifest.json").exists())
        self.assertEqual(self.source.read_bytes(), b"unchanged local preview")

    def test_rejects_path_traversal_and_invalid_duration(self):
        for key in ("..", "../anything", "C:\\test", "a/b", "a\\b"):
            with self.assertRaises(ValueError):
                self.cache.directory(key)
        for filename in ("../secret.jpg", "manifest.json", "sheet-00000.jpg/secret"):
            with self.assertRaises(HTTPException):
                self.cache.sheet_path("p2_test", filename)
        for duration in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                self.render(duration)

    def test_keyframe_fast_path_requires_every_sample_and_preserves_fallback(self):
        def probe(times, rate="30/1", start="0"):
            return {"streams": [{"avg_frame_rate": rate, "start_time": start}],
                    "frames": [{"best_effort_timestamp_time": str(value)} for value in times]}
        self.assertTrue(keyframe_sampling_safe(probe([0, 1, 2]), 2300))
        self.assertTrue(keyframe_sampling_safe(probe([0, 0.98, 1.98]), 2300))
        for data in (probe([0, 8.3]), probe([0, 1.01, 2.01]), probe([0, 0.9, 1.9]),
                     probe([1, 2, 3]), probe([0, 1, 2], "0/0"), probe([0, 2, 1]), {}):
            self.assertFalse(keyframe_sampling_safe(data, 2300))
        for allowed in (True, False):
            key = "p2_dense" if allowed else "p2_sparse"
            def fake(args, duration_ms, log_path, update, stopping):
                self.assertEqual("-skip_frame" in args, allowed)
                Path(args[-1]).parent.joinpath("sheet-00000.jpg").write_bytes(b"\xff\xd8image\xff\xd9")
            with patch("backend.storyboards.can_sample_keyframes", return_value=allowed), patch("backend.storyboards.run_ffmpeg", side_effect=fake):
                self.cache.render(self.source, key, 2300, FFMPEG, lambda _: None, self.stopping)

    @unittest.skipUnless(FFMPEG.is_file(), "Project-local FFmpeg not installed")
    def test_actual_ffmpeg_fractional_tail_and_letterbox(self):
        self.source.unlink()
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                                 "-f", "lavfi", "-i", "color=c=red:size=160x120:rate=10", "-t", "2.3",
                                 "-c:v", "libx264", "-threads", "1", str(self.source)],
                                capture_output=True, timeout=30, creationflags=flags)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.render(2300)
        self.assertEqual(manifest["frame_count"], 3)
        self.assertEqual(manifest["sheets"], ["sheet-00000.jpg"])
        sheet = self.cache.sheet_path("p2_test", manifest["sheets"][0])
        raw = subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-i", str(sheet),
                              "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                             capture_output=True, timeout=30, creationflags=flags)
        self.assertEqual(raw.returncode, 0, raw.stderr)
        self.assertEqual(len(raw.stdout), 1600 * 900 * 3)
        def pixel(x, y):
            offset = (y * 1600 + x) * 3
            return tuple(raw.stdout[offset:offset + 3])
        for index in range(3):
            color = pixel(index * 160 + 80, 45)
            self.assertGreater(color[0], 200)
            self.assertLess(color[1], 30)
            self.assertLess(color[2], 30)
            self.assertLess(max(pixel(index * 160 + 5, 45)), 20)
        self.assertLess(max(pixel(3 * 160 + 80, 45)), 20)
        self.assertEqual(self.cache.read("p2_test", 2300), manifest)


if __name__ == "__main__":
    unittest.main()
