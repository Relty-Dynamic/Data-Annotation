from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.service import ProjectService, empty_annotations
from backend.testing_annotations import complete_annotations

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / ".tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
FFPROBE = FFMPEG.with_name("ffprobe.exe")


@unittest.skipUnless(FFMPEG.is_file() and FFPROBE.is_file(), "Project-local FFmpeg not installed")
class ActualMediaTests(unittest.TestCase):
    def test_fast_preview_samples_correct_source_times_and_builds_without_original(self):
        with tempfile.TemporaryDirectory(prefix="fast-media-test-", dir=ROOT / ".tmp") as folder:
            root = Path(folder)
            original = root / "20260914120000.mp4"
            args = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=red:s=320x180:r=30:d=4",
                    "-f", "lavfi", "-i", "color=blue:s=320x180:r=30:d=4",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
                    "-c:v", "libx264", "-threads", "1", str(original)]
            result = subprocess.run(args, capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            service = ProjectService(root)
            service.tool = lambda name: FFMPEG if name == "ffmpeg" else FFPROBE
            project = service.create([original], "Fast preview", None)
            service.save(project)
            spec = service.preview_spec(project, "v0001")
            spec.target.write_bytes(original.read_bytes())
            original.unlink()  # Only this synthetic test original, simulating an unplugged card.
            service.render_fast_preview(spec, lambda value: None, threading.Event())
            fast = service.cached_fast_preview(spec)
            self.assertIsNotNone(fast)
            self.assertAlmostEqual(service.probe(fast)["duration_ms"], 400, delta=34)
            self.assertEqual(service.probe(spec.target)["duration_ms"], 8000)
            for seconds, channel in [(0.1, 0), (0.3, 2)]:
                pixel = subprocess.run([str(FFMPEG), "-v", "error", "-ss", str(seconds), "-i", str(fast),
                                        "-frames:v", "1", "-vf", "scale=1:1,format=rgb24", "-f", "rawvideo", "pipe:1"],
                                       capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(pixel.returncode, 0, pixel.stderr)
                self.assertEqual(len(pixel.stdout), 3)
                self.assertGreater(pixel.stdout[channel], 180)
                self.assertLess(pixel.stdout[2 if channel == 0 else 0], 60)
            with patch("backend.service.run_ffmpeg") as encode:
                service.render_fast_preview(spec, lambda value: None, threading.Event())
                encode.assert_not_called()
            self.assertEqual(service.load(project["id"])["annotations"], project["annotations"])
            service.previews.close()

    def prepare(self, service, ident, video_id):
        service.prepare_preview(ident, video_id, prefetch=False)
        spec = service.preview_spec(service.load(ident), video_id)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = service.previews.status(spec)
            if status["state"] == "ready":
                service.previews.close()
                return service.media(ident, video_id)
            if status["state"] == "error":
                self.fail(status["detail"])
            time.sleep(0.05)
        service.previews.close()
        self.fail("Actual preview conversion timed out")

    def test_nonzero_video_pts_normalized_with_audio(self):
        with tempfile.TemporaryDirectory(prefix="media-offset-test-", dir=ROOT / ".tmp") as folder:
            root = Path(folder)
            self.assertEqual(root.resolve().parent, (ROOT / ".tmp").resolve())
            path = root / "A09999_20260914100000_0001.mp4"
            result = subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "1", "-c:v", "libx264", "-c:a", "aac", "-threads", "1", "-output_ts_offset", "5", str(path)], capture_output=True, text=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            service = ProjectService(root)
            service.tool = lambda name: FFMPEG if name == "ffmpeg" else FFPROBE
            original = service.probe(path)
            self.assertAlmostEqual(original["media_start_seconds"], 5, places=3)
            project = service.create([path], "offset", None)
            service.save(project)
            self.assertEqual(project["videos"][0]["original_media_start_ms"], 5000)
            preview = self.prepare(service, project["id"], "v0001")
            self.assertNotEqual(preview, path)
            normalized = service.probe(preview)
            self.assertAlmostEqual(normalized["media_start_seconds"], 0, places=3)
            self.assertEqual(normalized["duration_ms"], original["duration_ms"])
            self.assertEqual(normalized["audio_codecs"], ["aac"])

    def test_real_offline_preview_can_serve_range_and_rebuild_thumbnail_after_restart(self):
        with tempfile.TemporaryDirectory(prefix="media-offline-test-", dir=ROOT / ".tmp") as folder:
            root = Path(folder)
            path = root / "A09999_20260914120000_0001.avi"
            result = subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-t", "1", "-an", "-c:v", "mjpeg", "-threads", "1", str(path)], capture_output=True, text=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            service = ProjectService(root)
            service.tool = lambda name: FFMPEG if name == "ffmpeg" else FFPROBE
            try:
                project = service.create([path], "Offline AVI", None)
                service.save(project)
                preview = self.prepare(service, project["id"], "v0001")
                thumbnail = service.media(project["id"], "v0001", thumbnail=True)
                expected_bytes = preview.read_bytes()[:32]
                # Only temporary synthetic originals are removed to simulate ejecting a card.
                self.assertEqual(path.resolve().parent, root.resolve())
                path.unlink()
                thumbnail.unlink()
                application = create_app(root)
                application.state.service.tool = service.tool
                with TestClient(application, base_url="http://127.0.0.1") as client:
                    url = f"/api/previews/{project['id']}/v0001"
                    self.assertEqual(client.get(url).json()["state"], "ready")
                    response = client.get(project["videos"][0]["url"], headers={"Range": "bytes=0-31"})
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(response.content, expected_bytes)
                    response = client.get(project["videos"][0]["thumbnail_url"])
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.content.startswith(b"\xff\xd8"))
                    self.assertTrue(response.content.endswith(b"\xff\xd9"))
                    status = client.post(f"/api/projects/{project['id']}/prepare").json()
                    self.assertEqual((status["state"], status["ready"], status["failed"]), ("ready", 1, 0))
                    self.assertEqual(application.state.service.probe(preview)["duration_ms"], 1000)
            finally:
                service.previews.close()

    def test_multipage_storyboard_builds_from_offline_cache_and_reads_both_sheets(self):
        with tempfile.TemporaryDirectory(prefix="media-storyboard-test-", dir=ROOT / ".tmp") as folder:
            root = Path(folder)
            path = root / "A09999_20260914120000_0001.avi"
            result = subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:size=160x90:rate=2", "-vf", "drawbox=color=blue:t=fill:enable='gte(t,100)'", "-t", "101.5", "-an", "-c:v", "mjpeg", "-threads", "1", str(path)], capture_output=True, text=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            service = ProjectService(root)
            service.tool = lambda name: FFMPEG if name == "ffmpeg" else FFPROBE
            try:
                project = service.create([path], "Offline storyboards", None)
                service.save(project)
                spec = service.preview_spec(project, "v0001")
                # Simulate the old complete video+thumbnail cache, which predates storyboards.
                service.render_video(spec, lambda value: None, threading.Event())
                service.render_thumbnail(spec)
                service.render_fast_preview(spec, lambda value: None, threading.Event())
                self.assertEqual(service.preparation_status(project["id"])["ready"], 0)
                self.assertEqual(path.resolve().parent, root.resolve())
                path.unlink()
                application = create_app(root)
                application.state.service.tool = service.tool
                with patch("backend.service.run_ffmpeg") as encode:
                    with TestClient(application, base_url="http://127.0.0.1") as client:
                        url = f"/api/storyboards/{project['id']}/v0001"
                        self.assertEqual(client.get(url).json()["state"], "idle")
                        client.post(f"/api/projects/{project['id']}/prepare")
                        deadline = time.monotonic() + 30
                        while time.monotonic() < deadline:
                            status = client.get(f"/api/projects/{project['id']}/prepare").json()
                            if status["state"] in {"ready", "partial"}:
                                break
                            time.sleep(0.05)
                        self.assertEqual((status["state"], status["ready"], status["failed"]), ("ready", 1, 0), status)
                        encode.assert_not_called()
                        manifest = client.get(url).json()
                        self.assertEqual((manifest["interval_ms"], manifest["frame_count"]), (1000, 102))
                        self.assertEqual(len(manifest["sheets"]), 2)
                        for index, sheet_url in enumerate(manifest["sheets"]):
                            response = client.get(sheet_url)
                            self.assertEqual(response.status_code, 200)
                            self.assertTrue(response.content.startswith(b"\xff\xd8"))
                            sheet = application.state.service.storyboard_sheet(project["id"], "v0001", index)
                            info = json.loads(service.command([str(FFPROBE), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(sheet)]).stdout)["streams"][0]
                            self.assertEqual((info["width"], info["height"]), (1600, 900))
                            # First tile is red at t=0; the second sheet begins blue at t=100.
                            sample = subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-i", str(sheet), "-vf", "format=rgb24,crop=1:1:80:45", "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
                            self.assertEqual(sample.returncode, 0, sample.stderr)
                            red, green, blue = sample.stdout
                            self.assertGreater(red if index == 0 else blue, 200)
                            self.assertLess(blue if index == 0 else red, 50)
                            self.assertLess(green, 50)
            finally:
                service.previews.close()

    def test_mp4_avi_preview_thumbnail_range_and_recording_gap(self):
        with tempfile.TemporaryDirectory(prefix="media-test-", dir=ROOT / ".tmp") as folder:
            root = Path(folder)
            self.assertEqual(root.resolve().parent, (ROOT / ".tmp").resolve())
            source = root / "collection" / "FPV"
            source.mkdir(parents=True)
            mp4 = source / "A09999_20260914090000_0001.mp4"
            avi = source / "A09999_20260914090011_0002.avi"
            flags = subprocess.CREATE_NO_WINDOW
            for path, codec in [(mp4, "libx264"), (avi, "mjpeg")]:
                result = subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-t", "1", "-an", "-c:v", codec, "-threads", "1", str(path)], capture_output=True, text=True, timeout=60, creationflags=flags)
                self.assertEqual(result.returncode, 0, result.stderr)
            service = ProjectService(root)
            service.tool = lambda name: FFMPEG if name == "ffmpeg" else FFPROBE
            project = service.open_path(str(source.parent))
            self.assertEqual(project["duration_ms"], 12000)
            self.assertEqual(project["gaps"], [{"start_ms": 1000, "end_ms": 11000}])
            self.assertEqual(service.media(project["id"], "v0001"), mp4)
            preview = self.prepare(service, project["id"], "v0002")
            self.assertEqual(preview.suffix, ".mp4")
            self.assertEqual(service.probe(preview)["codec"], "h264")
            self.assertEqual(service.probe(preview)["duration_ms"], 1000)
            for checked in [avi, preview]:
                info = json.loads(service.command([str(FFPROBE), "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames,avg_frame_rate,width,height", "-of", "json", str(checked)]).stdout)["streams"][0]
                self.assertEqual(int(info["nb_read_frames"]), 25)
                self.assertEqual(info["avg_frame_rate"], "25/1")
                self.assertEqual((info["width"], info["height"]), (320, 180))
            thumbnail = service.media(project["id"], "v0002", thumbnail=True)
            self.assertEqual(thumbnail.read_bytes()[:2], b"\xff\xd8")
            self.assertEqual(service.media(project["id"], "v0002"), preview)
            application = create_app(root)
            application.state.service.tool = service.tool
            with TestClient(application, base_url="http://127.0.0.1") as client:
                response = client.get(project["videos"][1]["url"], headers={"Range": "bytes=0-31"})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, preview.read_bytes()[:32])
                self.assertEqual(client.get(project["videos"][0]["thumbnail_url"]).status_code, 200)
            annotations = empty_annotations()
            annotations["habit"] = [{"id": "event", "label": "咳嗽", "kind": "point", "start_ms": 11500, "end_ms": 11500}]
            service.update_draft(project["id"], complete_annotations(project, annotations), 0)
            service.writeback(project["id"])
            exported = json.loads((source.parent / "timeline" / "habit.timeline.json").read_bytes())
            self.assertEqual(exported["timebase"]["origin"], "recording_datetime")
            self.assertEqual(exported["segments"][0]["start_ms"], 11500)


if __name__ == "__main__":
    unittest.main()
