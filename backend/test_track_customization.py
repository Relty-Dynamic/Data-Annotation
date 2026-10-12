"""New track defaults, stable custom filenames, and label-note round trips."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from backend.service import ProjectService, empty_annotations


ROOT = Path(__file__).resolve().parents[1]


class TrackCustomizationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="track-customization-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.source = self.root / "collection"
        self.fpv = self.source / "FPV"
        self.fpv.mkdir(parents=True)
        self.video = self.fpv / "A09999_20260917120000_0001.avi"
        self.video.write_bytes(b"synthetic video")
        self.service = ProjectService(self.root / "app")
        self.service.probe = lambda path: {"duration_ms": 1000, "codec": "mjpeg", "pixel_format": "yuv420p", "audio_codecs": []}

    def tearDown(self):
        self.service.sessions.close()
        self.service.previews.close()
        self.temp.cleanup()

    def test_new_directory_and_file_projects_default_to_scene_and_posture(self):
        directory = self.service.open_path(str(self.source))
        self.assertEqual(directory["fixed_tracks"], ["scene", "posture"])
        other = self.root / "other" / self.video.name
        other.parent.mkdir()
        other.write_bytes(b"another synthetic video")
        files = self.service.open_files([str(other)])
        self.assertEqual(files["fixed_tracks"], ["scene", "posture"])

    def test_named_custom_json_survives_writeback_and_reopen(self):
        project = self.service.open_path(str(self.source))
        created = self.service.add_custom_track(project["id"], "饮食", "state", ["进食"], 0, json_name="food")
        self.assertEqual(created["custom_tracks"][0]["id"], "food")
        annotations = empty_annotations()
        annotations["scene"] = [{"id": "scene", "label": "室内", "start_ms": 0, "end_ms": 1000}]
        annotations["posture"] = [{"id": "posture", "label": "坐", "start_ms": 0, "end_ms": 1000}]
        annotations["food"] = [{"id": "meal", "label": "进食（午餐）", "start_ms": 100, "end_ms": 300}]
        saved = self.service.update_draft(project["id"], annotations, created["revision"])
        self.service.writeback(project["id"])
        path = self.source / "timeline" / "food.timeline.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["segments"][0]["label"], "进食（午餐）")
        fresh = ProjectService(self.root / "other-app")
        fresh.probe = self.service.probe
        try:
            reopened = fresh.open_path(str(self.source))
            self.assertEqual(reopened["custom_tracks"], saved["custom_tracks"])
            self.assertEqual(reopened["annotations"]["food"], saved["annotations"]["food"])
        finally:
            fresh.sessions.close()
            fresh.previews.close()

    def test_note_changes_only_selected_segment_and_json_label(self):
        project = self.service.open_path(str(self.source))
        annotations = empty_annotations()
        annotations["scene"] = [{"id": "first", "label": "室内（餐厅）", "start_ms": 0, "end_ms": 400},
                                {"id": "middle", "label": "室外", "start_ms": 400, "end_ms": 600},
                                {"id": "last", "label": "室内", "start_ms": 600, "end_ms": 1000}]
        annotations["posture"] = [{"id": "posture", "label": "坐", "start_ms": 0, "end_ms": 1000}]
        saved = self.service.update_draft(project["id"], annotations, 0)
        self.assertEqual([item["label"] for item in saved["annotations"]["scene"]],
                         ["室内（餐厅）", "室外", "室内"])
        self.service.writeback(project["id"])
        document = json.loads((self.source / "timeline" / "scene.timeline.json").read_text(encoding="utf-8"))
        segment = document["segments"][0]
        self.assertEqual(segment["label"], "室内（餐厅）")
        self.assertNotIn("note", segment)
        self.assertIn("室内（餐厅）", [item["name"] for item in document["labels"]])
        self.assertEqual([item["label"] for item in self.service.open_path(str(self.source))["annotations"]["scene"]],
                         ["室内（餐厅）", "室外", "室内"])

    def test_deleting_base_label_also_removes_segments_with_notes(self):
        project = self.service.open_path(str(self.source))
        annotations = empty_annotations()
        annotations["scene"] = [{"id": "noted", "label": "室内（餐厅）", "start_ms": 0, "end_ms": 400},
                                {"id": "plain", "label": "室内", "start_ms": 600, "end_ms": 1000}]
        saved = self.service.update_draft(project["id"], annotations, 0)
        updated = self.service.set_track_labels(project["id"], "scene", ["室外"], saved["revision"])
        self.assertEqual(updated["annotations"]["scene"], [])

    def test_named_json_rejects_reserved_and_path_names(self):
        project = self.service.open_path(str(self.source))
        for name in ("scene", "../food", "Food", "custom_abcd"):
            with self.subTest(name=name), self.assertRaises(HTTPException):
                self.service.add_custom_track(project["id"], "饮食", "state", ["进食"], 0, json_name=name)


if __name__ == "__main__":
    unittest.main()
