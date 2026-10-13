"""Main/child lane compatibility, empty exports and durable annotation identity."""
from __future__ import annotations

import io
import json
import unittest
import zipfile

from fastapi import HTTPException

from backend import test_service
from backend.local_reports import validate_report
from backend.service import AXES, FILENAMES, ProjectService, empty_annotations, validate_annotations


def segment(ident, start, end, lane=None, label="室内", kind="interval"):
    return {"id": ident, "label": label, "kind": kind, "start_ms": start, "end_ms": end,
            **({"lane": lane} if lane is not None else {})}


class LaneValidationTests(unittest.TestCase):
    def test_blank_and_partial_axes_are_valid_in_draft_and_final(self):
        self.assertEqual(validate_annotations(empty_annotations(), 1000, final=True), empty_annotations())
        labels = {"scene": "室内", "posture": "坐", "category": "专注", "habit": "喝水"}
        annotations = {axis: [segment(axis, 300, 600, 0, label)] for axis, label in labels.items()}
        self.assertEqual(validate_annotations(annotations, 1000), validate_annotations(annotations, 1000, final=True))
        for records in validate_annotations(annotations, 1000, final=True).values():
            self.assertEqual([(item["start_ms"], item["end_ms"]) for item in records], [(300, 600)])

    def test_every_axis_accepts_same_label_on_arbitrarily_numbered_children(self):
        labels = {"scene": "室内", "posture": "坐", "category": "专注", "habit": "喝水", "weather": "晴", "event": "声音"}
        tracks = [{"id": "weather", "name": "天气", "mode": "state", "labels": ["晴"]},
                  {"id": "event", "name": "事件", "mode": "event", "labels": []}]
        annotations = {axis: [segment("main", 100, 800, 0, label), segment("child", 100, 800, 12345, label)]
                       for axis, label in labels.items()}
        result = validate_annotations(annotations, 1000, final=True, custom_tracks=tracks)
        self.assertEqual({axis: {item["lane"] for item in records} for axis, records in result.items()},
                         {axis: {0, 12345} for axis in labels})

    def test_legacy_assignment_reserves_future_explicit_lanes_and_reuses_gaps(self):
        annotations = empty_annotations()
        annotations["habit"] = [segment("later", 500, 800, 0), segment("legacy-long", 0, 600),
                                segment("legacy-short", 0, 100), segment("same-time", 0, 100),
                                segment("reuse", 100, 200), segment("child-kept", 700, 1000, 7)]
        result = validate_annotations(annotations, 1000)["habit"]
        self.assertEqual({item["id"]: item["lane"] for item in result},
                         {"later": 0, "legacy-long": 2, "legacy-short": 0, "same-time": 1, "child-kept": 7})
        self.assertEqual(next(item["end_ms"] for item in result if item["id"] == "legacy-short"), 200)
        self.assertNotIn("lane", annotations["habit"][1])
        annotations["habit"].reverse()
        self.assertEqual(validate_annotations(annotations, 1000)["habit"], result)

    def test_touching_intervals_share_lane_but_points_and_pending_events_reserve_time(self):
        annotations = empty_annotations()
        annotations["habit"] = [segment("left", 0, 500, 0), segment("right", 500, 1000, 0),
                                segment("point", 500, 500, kind="point"), segment("pending", 500, None)]
        result = validate_annotations(annotations, 1000)["habit"]
        self.assertEqual({item["id"]: item["lane"] for item in result}, {"left": 0, "point": 1, "pending": 2})
        self.assertEqual(result[0]["end_ms"], 1000)
        with self.assertRaises(HTTPException):
            validate_annotations(annotations, 1000, final=True)
        annotations["habit"] = [segment("point-a", 500, 500, 1, kind="point"), segment("point-b", 500, 500, 1, kind="point")]
        with self.assertRaises(HTTPException):
            validate_annotations(annotations, 1000)

    def test_invalid_or_colliding_explicit_lanes_rejected_without_reassigning(self):
        for lane in (-1, True, 0.5, "1", None, 9007199254740992):
            annotations = empty_annotations()
            annotations["scene"] = [{**segment("s", 0, 500), "lane": lane}]
            with self.subTest(lane=lane), self.assertRaises(HTTPException):
                validate_annotations(annotations, 1000)
        for axis, label in (("scene", "室内"), ("posture", "坐"), ("category", "专注"), ("habit", "喝水")):
            annotations = empty_annotations()
            annotations[axis] = [segment("a", 0, 800, 4, label), segment("b", 500, 900, 4, label)]
            with self.subTest(axis=axis), self.assertRaises(HTTPException):
                validate_annotations(annotations, 1000)

    def test_normalization_merges_only_same_lane_and_keeps_explicit_seam_blank(self):
        videos = test_service.ValidationTests().seam_videos()
        annotations = empty_annotations()
        annotations["scene"] = [segment("left-main", 19165033, 19764766, 0),
                                segment("left-child", 19165033, 19764766, 1),
                                segment("right-main", 19766146, 20364767, 0),
                                segment("right-child", 19766146, 20364767, 1)]
        result = validate_annotations(annotations, 20364767, videos=videos)
        self.assertEqual(result["scene"], sorted(annotations["scene"], key=lambda item: (item["start_ms"], item["id"])))
        annotations["scene"] = [segment("main", 0, 500, 0), segment("child", 0, 1000, 1), segment("main-next", 500, 1000, 0)]
        result = validate_annotations(annotations, 1000)["scene"]
        self.assertEqual([(item["id"], item["end_ms"], item["lane"]) for item in result], [("child", 1000, 1), ("main", 1000, 0)])
        annotations["scene"] = [{**segment("a", 0, 500, 0), "created_by_name": "原名"},
                                {**segment("b", 500, 1000, 0), "created_by_name": "新名"}]
        merged = validate_annotations(annotations, 1000)["scene"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["created_by_name"], "原名")

    def test_supplement_split_keeps_each_lane_and_leaves_new_frames_empty(self):
        annotations = empty_annotations()
        annotations["scene"] = [segment("main", 0, 2000, 0), segment("child", 500, 1500, 2)]
        old = {"duration_ms": 2000, "videos": [{"id": "a", "name": "a.mp4", "start_ms": 0, "end_ms": 1000}, {"id": "b", "name": "b.mp4", "start_ms": 1000, "end_ms": 2000}], "annotations": annotations}
        videos = [{"id": "a", "name": "a.mp4", "start_ms": 0, "end_ms": 1000}, {"id": "new", "name": "new.mp4", "start_ms": 1000, "end_ms": 2000}, {"id": "b", "name": "b.mp4", "start_ms": 2000, "end_ms": 3000}]
        result, _ = ProjectService.remap_annotations(old, videos, 3000)
        self.assertEqual(sorted((item["start_ms"], item["end_ms"], item["lane"]) for item in result["scene"]),
                         [(0, 1000, 0), (500, 1000, 2), (2000, 2500, 2), (2000, 3000, 0)])


class LanePersistenceTests(unittest.TestCase):
    setUp = test_service.PersistenceTests.setUp
    tearDown = test_service.PersistenceTests.tearDown

    def test_draft_export_writeback_reopen_and_report_preserve_children_and_blanks(self):
        project = self.service.open_path(str(self.source))
        annotations = empty_annotations()
        annotations["scene"] = [segment("main", 100, 1500, 0), segment("child", 200, 900, 1), segment("reused", 900, 1300, 1, "室外")]
        updated = self.service.update_draft(project["id"], annotations, project["revision"])
        expected = updated["annotations"]
        self.assertEqual(self.service.load(project["id"])["annotations"], expected)
        with zipfile.ZipFile(io.BytesIO(self.service.export_zip(project["id"]))) as archive:
            scene = json.loads(archive.read(FILENAMES["scene"]))
            self.assertEqual(scene["schema_version"], 3)
            self.assertEqual([item["lane"] for item in scene["segments"]], [0, 1, 1])
            self.assertEqual(json.loads(archive.read(FILENAMES["posture"]))["segments"], [])
        self.service.writeback(project["id"])
        fresh = ProjectService(self.root / "fresh-subaxes")
        try:
            self.assertEqual(fresh.open_path(str(self.source))["annotations"], expected)
        finally:
            fresh.previews.close()
        stored = self.service.load(project["id"])
        public = self.service.public(stored)
        snapshot = {key: public[key] for key in ("id", "name", "revision", "duration_ms", "videos", "fixed_tracks", "track_labels", "custom_tracks", "selected_tracks", "track_names", "annotations")}
        documents = {axis: (self.source / "timeline" / FILENAMES[axis]).read_text(encoding="utf-8") for axis in AXES}
        self.assertEqual(validate_report(snapshot, documents)["annotations"], expected)

    def test_lane_changes_are_audited_and_legacy_normalization_is_not_a_fake_edit(self):
        project = self.service.open_path(str(self.source))
        stored = self.service.load(project["id"])
        stored["annotations"]["scene"] = [segment("old", 100, 500)]
        self.service.save(stored)
        actor = {"id": "worker", "display_name": "标注员"}
        public = self.service.public(stored)
        saved = self.service.update_draft(project["id"], public["annotations"], project["revision"], actor=actor)
        self.assertEqual(self.service.edit_history(project["id"]), [])
        saved["annotations"]["scene"][0]["lane"] = 3
        saved = self.service.update_draft(project["id"], saved["annotations"], saved["revision"], actor=actor)
        history = self.service.edit_history(project["id"])
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["before"]["lane"], history[0]["after"]["lane"]), (0, 3))
        self.assertEqual(saved["annotations"]["scene"][0]["updated_by"], actor["id"])

    def test_legacy_report_documents_without_lane_still_match_normalized_snapshot(self):
        project = self.service.load(self.service.open_path(str(self.source))["id"])
        project["annotations"]["scene"] = [segment("a", 0, 500), segment("b", 200, 600, label="室外")]
        _, exported = self.service.export_documents(project)
        documents = {}
        for axis, raw in exported.items():
            document = json.loads(raw)
            for item in document["segments"]:
                item.pop("lane")
            documents[axis] = json.dumps(document)
        public = self.service.public(project)
        snapshot = {key: public[key] for key in ("id", "name", "revision", "duration_ms", "videos", "fixed_tracks", "track_labels", "custom_tracks", "selected_tracks", "track_names", "annotations")}
        self.assertEqual(validate_report(snapshot, documents)["annotations"], public["annotations"])


if __name__ == "__main__":
    unittest.main()
