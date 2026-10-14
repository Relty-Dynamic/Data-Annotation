"""Touching annotation intervals coalesce without erasing lanes or audit provenance."""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from backend import test_service
from backend.service import empty_annotations, validate_annotations


def segment(ident, start, end, label="室内", lane=0, **extra):
    return {"id": ident, "label": label, "start_ms": start, "end_ms": end,
            "kind": "interval", "lane": lane, **extra}


class IntervalMergeValidationTests(unittest.TestCase):
    def test_all_axes_merge_saved_neighbors_per_lane_and_keep_latest_contributor(self):
        labels = {"scene": "室内", "posture": "坐", "category": "专注", "habit": "喝水", "weather": "晴", "sound": "响声"}
        tracks = [{"id": "weather", "name": "天气", "mode": "state", "labels": ["晴"]},
                  {"id": "sound", "name": "声音", "mode": "event", "labels": []}]
        first_actor = {"created_by": "a", "created_by_name": "甲", "created_at": "2026-10-13T10:00:00Z"}
        next_actor = {"created_by": "b", "created_by_name": "乙", "created_at": "2026-10-13T10:01:00Z"}
        annotations = {axis: [segment("first", 0, 500, label + "（备注）", **first_actor),
                              segment("child", 0, 1000, label + "（备注）", lane=1),
                              segment("next", 500, 1000, label + "（备注）", **next_actor)] for axis, label in labels.items()}
        original = copy.deepcopy(annotations)
        result = validate_annotations(annotations, 1000, custom_tracks=tracks)
        for axis, records in result.items():
            with self.subTest(axis=axis):
                self.assertEqual(len(records), 2)
                main = next(item for item in records if item["lane"] == 0)
                self.assertEqual((main["id"], main["start_ms"], main["end_ms"]), ("first", 0, 1000))
                self.assertEqual(main["created_by"], "a")
                self.assertEqual(main["updated_by"], "b")
                self.assertEqual(main["updated_at"], next_actor["created_at"])
        self.assertEqual(annotations, original)
        self.assertEqual(validate_annotations(result, 1000, custom_tracks=tracks), result)

    def test_points_notes_modes_lanes_and_blanks_remain_distinct(self):
        cases = [
            [segment("a", 0, 500), segment("b", 501, 1000)],
            [segment("a", 0, 500), segment("b", 500, 1000, lane=1)],
            [segment("a", 0, 500, "喝水（冷水）"), segment("b", 500, 1000, "喝水（热水）")],
            [segment("a", 500, 500, kind="point"), segment("b", 501, 501, kind="point")],
        ]
        for records in cases:
            with self.subTest(records=records):
                self.assertEqual(validate_annotations({**empty_annotations(), "habit": records}, 1000)["habit"], records)
        category = [segment("a", 0, 500, "专注", mode="state"), segment("b", 500, 1000, "专注", mode="overlay")]
        self.assertEqual(validate_annotations({**empty_annotations(), "category": category}, 1000)["category"], category)

    def test_latest_edit_wins_even_when_it_belongs_to_the_first_interval(self):
        rows = [segment("a", 0, 500, created_by="creator", created_at="2026-10-13T10:00:00Z",
                        updated_by="latest", updated_at="2026-10-13T12:00:00Z"),
                segment("b", 500, 1000, created_by="other", created_at="2026-10-13T11:00:00Z")]
        merged = validate_annotations({**empty_annotations(), "scene": rows}, 1000)["scene"][0]
        self.assertEqual(merged["updated_by"], "latest")
        self.assertEqual(merged["created_by"], "creator")


class IntervalMergePersistenceTests(unittest.TestCase):
    setUp = test_service.PersistenceTests.setUp
    tearDown = test_service.PersistenceTests.tearDown

    def test_delayed_save_merges_authenticated_rows_and_export_keeps_provenance(self):
        project = self.service.open_path(str(self.source))
        actor_a, actor_b = {"id": "a", "display_name": "甲"}, {"id": "b", "display_name": "乙"}
        annotations = {**empty_annotations(), "scene": [segment("first", 0, 1000)]}
        with patch("backend.service.now", return_value="2026-10-13T10:00:00Z"):
            saved = self.service.update_draft(project["id"], annotations, project["revision"], actor=actor_a)
        annotations = copy.deepcopy(saved["annotations"])
        annotations["scene"].append(segment("later", 1000, 2000, created_by="forged", updated_at="2099-01-01"))
        with patch("backend.service.now", return_value="2026-10-13T10:01:00Z"):
            saved = self.service.update_draft(project["id"], annotations, saved["revision"], actor=actor_b)
        merged = saved["annotations"]["scene"][0]
        self.assertEqual(len(saved["annotations"]["scene"]), 1)
        self.assertEqual((merged["id"], merged["start_ms"], merged["end_ms"]), ("first", 0, 2000))
        self.assertEqual((merged["created_by"], merged["updated_by"], merged["updated_at"]), ("a", "b", "2026-10-13T10:01:00Z"))
        self.assertEqual(self.service.load(project["id"])["annotations"], saved["annotations"])
        history = self.service.edit_history(project["id"])
        self.assertEqual(len(history), 2)
        update = next(item for item in history if item["action"] == "update")
        self.assertEqual(update["before"]["end_ms"], 1000)
        self.assertEqual(update["after"]["end_ms"], 2000)
        _, documents = self.service.export_documents(self.service.load(project["id"]))
        exported = json.loads(documents["scene"])["segments"][0]
        self.assertEqual((exported["created_by"], exported["updated_by"], exported["end_ms"]), ("a", "b", 2000))
        self.service.update_draft(project["id"], saved["annotations"], saved["revision"], actor=actor_b)
        self.assertEqual(len(self.service.edit_history(project["id"])), 2, "normalization remains idempotent on repeated autosave")

    def test_existing_original_rows_are_kept_in_audit_before_values_when_coalesced(self):
        public = self.service.open_path(str(self.source))
        stored = self.service.load(public["id"])
        originals = [segment("left", 0, 1000, created_by="a", created_at="2026-10-13T10:00:00Z"),
                     segment("right", 1000, 2000, created_by="b", created_at="2026-10-13T10:01:00Z")]
        stored["annotations"]["scene"] = originals
        self.service.save(stored)
        browser_annotations = self.service.public(stored)["annotations"]
        with patch("backend.service.now", return_value="2026-10-13T10:02:00Z"):
            saved = self.service.update_draft(public["id"], browser_annotations, public["revision"], actor={"id": "c", "display_name": "丙"})
        self.assertEqual(len(saved["annotations"]["scene"]), 1)
        self.assertEqual(saved["annotations"]["scene"][0]["updated_by"], "b")
        history = self.service.edit_history(public["id"])
        self.assertEqual({item["segment_id"]: item["before"] for item in history}, {item["id"]: item for item in originals})
        self.assertEqual({item["action"] for item in history}, {"update", "delete"})


if __name__ == "__main__":
    unittest.main()
