from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.testing_annotations import complete_annotations, complete_project
from backend.service import AXES, FILENAMES, empty_annotations

ROOT = Path(__file__).resolve().parents[1]


class SupplementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="supplement-tests-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.app = create_app(self.root)
        self.service = self.app.state.service
        self.source = self.root / "originals"
        self.source.mkdir()
        self.durations = {}
        self.probed = []
        def probe(path):
            self.probed.append(path.name)
            return {"duration_ms": self.durations.get(path.name, 1000), "codec": "mjpeg", "pixel_format": "yuvj420p", "audio_codecs": [], "media_start_seconds": 0}
        self.service.probe = probe

    def tearDown(self):
        self.service.previews.close()
        self.assertEqual(self.root.resolve().parent, (ROOT / ".tmp").resolve())
        self.temp.cleanup()

    def video(self, second, index, duration=1000):
        name = f"A09999_202609161200{second:02d}_{index:04d}.avi"
        path = self.source / name
        path.write_bytes(b"synthetic-video-" + name.encode())
        self.durations[name] = duration
        return path

    def project(self, paths, annotations=None, directory=False):
        project = self.service.create(paths, "fixture", self.source if directory else None,
                                      imported_at="2026-09-16T06:17:00+00:00")
        if annotations:
            project["annotations"] = annotations
        self.service.save(project)
        return project

    def test_middle_insert_keeps_project_annotations_ids_and_offline_cache(self):
        first, last = self.video(0, 0), self.video(2, 2)
        ann = empty_annotations()
        ann["scene"] = [{"id": "left", "label": "室内", "start_ms": 0, "end_ms": 1000},
                        {"id": "right", "label": "室外", "start_ms": 2000, "end_ms": 3000}]
        ann["habit"] = [{"id": "water", "label": "喝水", "kind": "point", "start_ms": 2250, "end_ms": 2250}]
        before = self.project([last, first], ann)
        old_spec = self.service.preview_spec(before, "v0002")
        old_spec.target.write_bytes(b"existing-preview")
        first.unlink()
        last.unlink()
        added = self.video(1, 1)
        self.probed.clear()
        result = self.service.supplement(before["id"], [added], 0)
        after = result["project"]
        self.assertEqual((result["added_count"], result["skipped_names"]), (1, []))
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["name"], before["name"])
        self.assertEqual(after["imported_at"], before["imported_at"])
        self.assertEqual(after["revision"], 1)
        self.assertEqual([v["name"] for v in after["videos"]], [first.name, added.name, last.name])
        self.assertEqual([v["id"] for v in after["videos"]], ["v0001", "v0003", "v0002"])
        self.assertEqual(after["annotations"]["habit"], ann["habit"])
        self.assertEqual([(v["start_ms"], v["end_ms"]) for v in after["annotations"]["scene"]], [(0, 1000), (2000, 3000)])
        self.assertEqual(after["gaps"], [])
        stored = self.service.load(before["id"])
        for ident in before["_sources"]:
            self.assertEqual(stored["_sources"][ident], before["_sources"][ident])
        new_spec = self.service.preview_spec(stored, "v0002")
        self.assertEqual(new_spec.key, old_spec.key)
        self.assertEqual(new_spec.target, old_spec.target)
        self.assertEqual(self.service.media(before["id"], "v0002").read_bytes(), b"existing-preview")
        self.assertEqual(self.probed, [added.name])
        self.assertEqual(len(self.service.projects()), 1)

    def test_prepend_preserves_real_clock_and_source_local_annotation_position(self):
        later = self.video(2, 2)
        ann = empty_annotations()
        ann["habit"] = [{"id": "water", "label": "喝水", "kind": "point", "start_ms": 250, "end_ms": 250}]
        ann["scene"] = [{"id": "scene", "label": "室内", "start_ms": 0, "end_ms": 1000}]
        before = self.project([later], ann)
        _, old_docs = self.service.export_documents(complete_project(before))
        result = self.service.supplement(before["id"], [self.video(0, 0)], 0)
        after = result["project"]
        self.assertEqual(after["recording_start"], "2026-09-16T12:00:00.000+08:00")
        self.assertEqual(after["annotations"]["habit"][0]["start_ms"], 2250)
        self.assertEqual(after["annotations"]["scene"][0]["start_ms"], 2000)
        self.assertEqual(after["gaps"], [{"start_ms": 1000, "end_ms": 2000}])
        _, new_docs = self.service.export_documents(complete_project(self.service.load(before["id"])))
        old_event = json.loads(old_docs["habit"])["segments"][0]
        new_event = json.loads(new_docs["habit"])["segments"][0]
        self.assertEqual(new_event["start_time"], old_event["start_time"])
        self.assertEqual(new_event["start_date"], old_event["start_date"])

    def test_overlap_correction_moves_annotation_with_original_frame(self):
        before = self.project([self.video(0, 0), self.video(2, 2)])
        ann = empty_annotations()
        ann["habit"] = [{"id": "water", "label": "喝水", "kind": "point", "start_ms": 2250, "end_ms": 2250}]
        self.service.update_draft(before["id"], ann, 0)
        result = self.service.supplement(before["id"], [self.video(1, 1, 2500)], 1)
        after = result["project"]
        old_last = next(v for v in after["videos"] if v["id"] == "v0002")
        self.assertEqual(old_last["alignment_offset_ms"], 1500)
        self.assertEqual(after["annotations"]["habit"][0]["start_ms"] - old_last["start_ms"], 250)

    def test_inserted_time_remains_unlabelled_and_interval_end_uses_left_video(self):
        ann = empty_annotations()
        ann["scene"] = [{"id": "state", "label": "室内", "start_ms": 500, "end_ms": 2500}]
        ann["posture"] = [{"id": "pose", "label": "坐", "start_ms": 500, "end_ms": 2000}]
        ann["habit"] = [{"id": "cross", "label": "吃饭", "start_ms": 500, "end_ms": 2500},
                        {"id": "boundary", "label": "喝水", "kind": "point", "start_ms": 2000, "end_ms": 2000}]
        before = self.project([self.video(0, 0, 2000), self.video(2, 2)], ann)
        after = self.service.supplement(before["id"], [self.video(1, 1)], 0)["project"]
        self.assertEqual([(s["start_ms"], s["end_ms"]) for s in after["annotations"]["scene"]], [(500, 2000), (3000, 3500)])
        self.assertEqual([(s["start_ms"], s["end_ms"]) for s in after["annotations"]["posture"]], [(500, 2000)])
        self.assertEqual([(s["start_ms"], s["end_ms"]) for s in after["annotations"]["habit"] if s["label"] == "吃饭"], [(500, 2000), (3000, 3500)])
        self.assertEqual(next(s for s in after["annotations"]["habit"] if s["id"] == "boundary")["start_ms"], 3000)
        for axis in AXES:
            ids = [s["id"] for s in after["annotations"][axis]]
            self.assertEqual(len(ids), len(set(ids)))

    def test_duplicate_is_noop_and_stale_revision_is_rejected(self):
        first = self.video(0, 0)
        before = self.project([first])
        result = self.service.supplement(before["id"], [first], 0)
        self.assertEqual(result["added_count"], 0)
        self.assertEqual(result["skipped_names"], [first.name])
        self.assertEqual(self.service.load(before["id"]), before)
        self.service.update_draft(before["id"], empty_annotations(), 0)
        snapshot = self.service.load(before["id"])
        with self.assertRaises(HTTPException) as context:
            self.service.supplement(before["id"], [self.video(1, 1)], 0)
        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(self.service.load(before["id"]), snapshot)

    def test_invalid_overlap_and_probe_failure_leave_original_project_intact(self):
        before = self.project([self.video(0, 0), self.video(2, 2)])
        with self.assertRaises(HTTPException):
            self.service.supplement(before["id"], [self.video(1, 1, 5001)], 0)
        self.assertEqual(self.service.load(before["id"]), before)
        new = self.video(4, 4)
        with patch.object(self.service, "probe", side_effect=HTTPException(422, "invalid media")):
            with self.assertRaises(HTTPException):
                self.service.supplement(before["id"], [new], 0)
        self.assertEqual(self.service.load(before["id"]), before)

    def test_path_directory_skips_old_and_source_reopen_preserves_supplement(self):
        first = self.video(0, 0)
        before = self.project([first], directory=True)
        second = self.video(2, 2)
        response = self.service.supplement_path(before["id"], str(self.source), 0)
        after = response["project"]
        self.assertEqual(response["added_count"], 1)
        self.assertEqual(response["skipped_names"], [first.name])
        self.assertEqual(after["source_dir"], str(self.source))
        self.assertEqual(self.service.open_path(str(self.source))["id"], before["id"])
        self.assertEqual(len(self.service.open_path(str(self.source))["videos"]), 2)
        after = self.service.update_draft(before["id"], complete_annotations(after), after["revision"])
        self.service.writeback(before["id"])
        for axis in AXES:
            document = json.loads((self.source / "timeline" / FILENAMES[axis]).read_bytes())
            self.assertEqual(len(document["timebase"]["videos"]), 2)
        self.assertTrue(second.is_file())

    def test_supplement_rejects_blank_path_without_changing_project(self):
        before = self.project([self.video(0, 0)])
        with self.assertRaises(HTTPException) as context:
            self.service.supplement_path(before["id"], "   ", 0)
        self.assertEqual(context.exception.status_code, 422)
        self.assertEqual(self.service.load(before["id"]), before)

    def test_supplemented_source_reopen_still_detects_changed_original(self):
        first = self.video(0, 0)
        before = self.project([first], directory=True)
        self.service.supplement(before["id"], [self.video(2, 2)], 0)
        snapshot = self.service.load(before["id"])
        stat = first.stat()
        import os
        os.utime(first, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10000000))
        with self.assertRaises(HTTPException) as context:
            self.service.open_path(str(self.source))
        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(self.service.load(before["id"]), snapshot)

    def test_middle_supplement_writeback_reopens_in_a_fresh_database(self):
        first, last = self.video(0, 0), self.video(2, 2)
        ann = empty_annotations()
        ann["habit"] = [{"id": "water", "label": "喝水", "kind": "point", "start_ms": 2250, "end_ms": 2250}]
        before = self.project([first, last], ann, directory=True)
        after = self.service.supplement(before["id"], [self.video(1, 1)], 0)["project"]
        after = self.service.update_draft(before["id"], complete_annotations(after), after["revision"])
        self.service.writeback(before["id"])
        from backend.service import ProjectService
        fresh = ProjectService(self.root / "fresh-runtime")
        fresh.probe = self.service.probe
        try:
            reopened = fresh.open_path(str(self.source))
            self.assertEqual(reopened["annotations"], after["annotations"])
            self.assertEqual(reopened["source_fingerprint"], after["source_fingerprint"])
        finally:
            fresh.previews.close()

    def test_pending_behavior_does_not_automatically_label_appended_video(self):
        ann = empty_annotations()
        ann["habit"] = [{"id": "ongoing", "label": "吃饭", "start_ms": 500, "end_ms": None}]
        before = self.project([self.video(0, 0)], ann)
        after = self.service.supplement(before["id"], [self.video(1, 1)], 0)["project"]
        self.assertEqual([(s["start_ms"], s["end_ms"]) for s in after["annotations"]["habit"]], [(500, 1000)])
        self.assertTrue(after["warnings"])

    def test_path_api_adds_to_current_project_without_copying_and_preserves_failed_batch(self):
        before = self.project([self.video(0, 0)])
        good = self.video(2, 2)
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            endpoint = f'/api/projects/{before["id"]}/videos/files'
            response = client.post(endpoint, json={"expected_revision": 0, "paths": [str(good)]})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result["project"]["id"], before["id"])
            self.assertEqual(result["added_count"], 1)
            snapshot = self.service.load(before["id"])
            new_source = Path(snapshot["_sources"][result["project"]["videos"][-1]["id"]]["path"])
            self.assertEqual(new_source, good.resolve())
            directory = self.service.storage.directory(before["id"])
            self.assertFalse((directory / "imports").exists())
            invalid = self.source / "invalid.avi"
            invalid.write_bytes(b"invalid")
            response = client.post(endpoint, json={"expected_revision": 1, "paths": [str(invalid)]})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.service.load(before["id"]), snapshot)
            self.assertTrue(invalid.exists())
            blocked = client.post(endpoint, json={"expected_revision": 1, "paths": [str(good)]}, headers={"Origin": "https://outside.example"})
            self.assertEqual(blocked.status_code, 403)
            stale = client.post(endpoint, json={"expected_revision": 0, "paths": [str(good)]})
            self.assertEqual(stale.status_code, 409, stale.text)
            duplicate = client.post(endpoint, json={"expected_revision": 1, "paths": [str(good)]})
            self.assertEqual(duplicate.status_code, 200, duplicate.text)
            self.assertEqual(duplicate.json()["added_count"], 0)
            self.assertEqual(new_source.read_bytes(), good.read_bytes())


if __name__ == "__main__":
    unittest.main()
