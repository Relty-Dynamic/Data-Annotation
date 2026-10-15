"""Individual JSON downloads retain the ZIP export's data and validation rules."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.service import FILENAMES, digest

ROOT = Path(__file__).resolve().parents[1]


class ExportDocumentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="export-documents-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.app = create_app(self.root, auth_required=False)
        self.service = self.app.state.service
        self.source = self.root / "originals"
        self.source.mkdir()
        video = self.source / "A09999_20260916120000_0000.avi"
        video.write_bytes(b"export fixture")
        self.service.probe = lambda path: {
            "duration_ms": 1000, "codec": "mjpeg", "pixel_format": "yuvj420p",
            "audio_codecs": [], "media_start_seconds": 0,
        }
        project = self.service.create([video], "导出测试", self.source)
        self.service.save(project)
        self.service.add_custom_track(project["id"], "饮食", "event", [], 0, json_name="food")
        self.project = self.service.load(project["id"])
        self.project["selected_tracks"] = ["posture"]
        self.project["annotations"]["scene"] = [
            {"id": "scene", "label": "室内", "kind": "interval", "lane": 0,
             "start_ms": 100, "end_ms": 500},
        ]
        self.project["annotations"]["food"] = [
            {"id": "meal", "label": "喝水", "kind": "point", "lane": 2,
             "start_ms": 700, "end_ms": 700},
        ]
        self.service.save(self.project)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.endpoint = f'/api/projects/{project["id"]}/export-documents'

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.assertEqual(self.root.resolve().parent, (ROOT / ".tmp").resolve())
        self.temp.cleanup()

    def test_exports_every_axis_as_named_json_without_modifying_project_or_source(self):
        before = self.service.load(self.project["id"])
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 200, response.text)
        documents = response.json()["documents"]
        expected = {name for axis, name in FILENAMES.items() if axis != "habit"} | {"food.timeline.json"}
        self.assertEqual({item["name"] for item in documents}, expected)
        self.assertTrue(all(isinstance(item["content"], str) for item in documents))
        decoded = {item["name"]: json.loads(item["content"]) for item in documents}
        self.assertEqual(len({item["save_id"] for item in decoded.values()}), 1)
        self.assertEqual(decoded["scene.timeline.json"]["segments"][0]["label"], "室内")
        self.assertEqual(decoded["food.timeline.json"]["segments"][0]["lane"], 2)
        self.assertEqual(decoded["posture.timeline.json"]["segments"], [])
        zipped = self.client.get(f'/api/projects/{self.project["id"]}/export')
        self.assertEqual(zipped.status_code, 200, zipped.text)
        with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
            self.assertEqual(set(archive.namelist()), set(decoded) | {"habit.timeline.json"})
            for name, document in decoded.items():
                zipped_document = json.loads(archive.read(name))
                for generated in ("save_id", "saved_at"):
                    document.pop(generated)
                    zipped_document.pop(generated)
                self.assertEqual(document, zipped_document)
        self.assertEqual(self.service.load(self.project["id"]), before)
        self.assertFalse((self.source / "timeline").exists())

    def test_retired_unfinished_habit_does_not_block_new_export_or_change_legacy_data(self):
        project = self.service.load(self.project["id"])
        project["selected_tracks"] = ["posture", "habit"]
        project["annotations"]["habit"] = [
            {"id": "unfinished", "label": "散步", "kind": "interval", "lane": 0,
             "start_ms": 200, "end_ms": None},
        ]
        self.service.save(project)
        before = self.service.load(project["id"])
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("habit.timeline.json", [item["name"] for item in response.json()["documents"]])
        for item in response.json()["documents"]:
            self.assertEqual(json.loads(item["content"])["timebase"]["selected_tracks"], ["posture"])
        self.assertEqual(self.client.get(f'/api/projects/{project["id"]}/export').status_code, 422)
        self.assertEqual(self.service.load(project["id"]), before)

    def test_unselected_custom_open_event_still_blocks_export(self):
        project = self.service.load(self.project["id"])
        project["annotations"]["food"] = [
            {"id": "unfinished", "label": "喝水", "kind": "interval", "lane": 0,
             "start_ms": 200, "end_ms": None},
        ]
        self.service.save(project)
        before = self.service.load(project["id"])
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn("documents", response.json())
        self.assertEqual(self.service.load(project["id"]), before)

    def test_cloud_writeback_ignores_retired_selection_and_preserves_existing_habit_file(self):
        project = self.service.load(self.project["id"])
        _, historical = self.service.export_documents(project)
        folder = self.source / "timeline"
        folder.mkdir()
        habit_file = folder / "habit.timeline.json"
        habit_file.write_bytes(historical["habit"])
        project["_external_hashes"]["habit"] = digest(historical["habit"])
        project["selected_tracks"] = ["posture", "habit"]
        project["annotations"]["habit"] = [
            {"id": "unfinished", "label": "散步", "kind": "interval", "lane": 0,
             "start_ms": 200, "end_ms": None},
        ]
        self.service.save(project)
        response = self.client.post(f'/api/projects/{project["id"]}/writeback', json={"expected_revision": project["revision"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([Path(path).name for path in response.json()["paths"]], ["posture.timeline.json"])
        self.assertEqual(habit_file.read_bytes(), historical["habit"])
        self.assertEqual(self.service.load(project["id"])["annotations"]["habit"], project["annotations"]["habit"])

    def test_unknown_project_has_no_export(self):
        response = self.client.get('/api/projects/' + 'f' * 32 + '/export-documents')
        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
