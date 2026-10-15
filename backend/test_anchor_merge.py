"""Anchor-save layouts survive persistence without globally promoting child rows."""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from backend.service import DEFAULT_TRACK_LABELS, FILENAMES, ProjectService, validate_annotations


ROOT = Path(__file__).resolve().parents[1]


def interval(ident, label, start, end, lane=0):
    return {"id": ident, "label": label, "kind": "interval", "start_ms": start,
            "end_ms": end, "lane": lane}


def layout(records):
    return sorted((item["id"], item["label"], item["start_ms"], item["end_ms"], item["lane"])
                  for item in records)


class AnchorMergePersistenceTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="anchor-merge-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.source = self.root / "collection"
        self.source.mkdir()
        video = self.source / "clip_20261014120000.mp4"
        video.write_bytes(b"synthetic anchor merge fixture")
        self.probe = patch.object(ProjectService, "probe", return_value={
            "duration_ms": 30000, "codec": "h264", "pixel_format": "yuv420p", "audio_codecs": []})
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.service = ProjectService(self.root, enable_remote=False)
        self.addCleanup(self.service.previews.close)
        self.project = self.service.create([video], "锚点局部合并", self.source)
        self.project["track_labels"] = copy.deepcopy(DEFAULT_TRACK_LABELS)
        self.service.save(self.project)
        self.actor = {"id": "anchor-editor", "display_name": "锚点标注员"}

    def save_rows(self, rows, previous, *, actor=None, axis="posture"):
        annotations = copy.deepcopy(previous["annotations"])
        annotations[axis] = copy.deepcopy(rows)
        return self.service.update_draft(previous["id"], annotations, previous["revision"],
                                         actor=actor or self.actor)

    def assert_roundtrip(self, saved):
        expected = saved["annotations"]
        self.assertEqual(self.service.public(self.service.load(saved["id"]))["annotations"], expected)
        with zipfile.ZipFile(io.BytesIO(self.service.export_zip(saved["id"]))) as archive:
            documents = {axis: json.loads(archive.read(filename)) for axis, filename in FILENAMES.items()}
        for axis, records in expected.items():
            exported = documents[axis]["segments"]
            self.assertEqual(layout(exported), layout(records))
            for record, segment in zip(records, exported):
                self.assertEqual({key: segment[key] for key in record}, record)
        self.assertEqual(documents["scene"]["segments"], [])
        self.service.writeback(saved["id"])
        fresh = ProjectService(self.root / f"fresh-{saved['revision']}", enable_remote=False)
        try:
            reopened = fresh.open_path(str(self.source))
            self.assertEqual(reopened["annotations"], expected)
        finally:
            fresh.previews.close()

    def test_partial_child_merge_result_preserves_split_lanes_and_blank_tail(self):
        rows = [interval("standing", "站", 0, 10000),
                interval("sitting", "坐", 10000, 15000),
                interval("child-standing", "站", 10000, 15000, 1)]
        saved = self.save_rows(rows, self.project)
        self.assertEqual(layout(saved["annotations"]["posture"]), layout(rows))
        self.assertTrue(all(item["created_by"] == self.actor["id"] for item in saved["annotations"]["posture"]))
        self.assertEqual(max(item["end_ms"] for item in saved["annotations"]["posture"]), 15000)
        self.assert_roundtrip(saved)

    def test_undo_restores_child_above_blank_main_and_deletion_keeps_that_blank(self):
        # An ordinary child stays on its saved lane until an explicit frontend
        # anchor save supplies a different layout. Undo supplies this old layout.
        before_rows = [interval("standing", "站", 0, 5000),
                       interval("sitting", "坐", 10000, 15000),
                       interval("child-standing", "站", 5000, 15000, 1)]
        before = self.save_rows(before_rows, self.project, actor={"id": "first-editor", "display_name": "原标注员"})
        before_snapshot = copy.deepcopy(before["annotations"])
        after_rows = [interval("standing", "站", 0, 10000),
                      interval("sitting", "坐", 10000, 15000),
                      interval("child-standing", "站", 10000, 15000, 1)]
        after = self.save_rows(after_rows, before)
        restored = self.service.update_draft(before["id"], before_snapshot, after["revision"], actor=self.actor)
        self.assertEqual(layout(restored["annotations"]["posture"]), layout(before_rows))
        self.assertEqual(before["annotations"], before_snapshot, "saving and undo must not mutate the old snapshot")
        canonical = validate_annotations(restored["annotations"], restored["duration_ms"], final=True)
        self.assertEqual(layout(canonical["posture"]), layout(before_rows))
        self.assert_roundtrip(restored)
        current = self.service.public(self.service.load(before["id"]))
        deleted = self.save_rows([item for item in current["annotations"]["posture"] if item["id"] != "standing"], current)
        child = next(item for item in deleted["annotations"]["posture"] if item["id"] == "child-standing")
        self.assertEqual((child["start_ms"], child["end_ms"], child["lane"]), (5000, 15000, 1))
        self.assertEqual([(item["start_ms"], item["end_ms"]) for item in deleted["annotations"]["posture"] if item["lane"] == 0], [(10000, 15000)])

    def test_main_fragments_coalesce_after_anchor_save_without_absorbing_child(self):
        before = self.save_rows([interval("standing", "站", 0, 5000),
                                 interval("sitting", "坐", 10000, 15000),
                                 interval("child-standing", "站", 5000, 15000, 1)], self.project,
                                actor={"id": "first-editor", "display_name": "原标注员"})
        fragments = [interval("standing", "站", 0, 5000),
                     interval("promoted-fragment", "站", 5000, 10000),
                     interval("sitting", "坐", 10000, 15000),
                     interval("child-standing", "站", 10000, 15000, 1)]
        saved = self.save_rows(fragments, before)
        expected = [interval("standing", "站", 0, 10000),
                    interval("sitting", "坐", 10000, 15000),
                    interval("child-standing", "站", 10000, 15000, 1)]
        self.assertEqual(layout(saved["annotations"]["posture"]), layout(expected))
        main = next(item for item in saved["annotations"]["posture"] if item["id"] == "standing")
        self.assertEqual((main["created_by"], main["updated_by"]), ("first-editor", "anchor-editor"))
        events = [item for item in self.service.edit_history(saved["id"]) if item["revision"] == saved["revision"]]
        changed = {item["segment_id"]: item for item in events}
        self.assertEqual((changed["standing"]["before"]["end_ms"], changed["standing"]["after"]["end_ms"]), (5000, 10000))
        self.assertEqual((changed["child-standing"]["before"]["start_ms"], changed["child-standing"]["after"]["start_ms"]), (5000, 10000))
        self.assert_roundtrip(saved)

    def test_split_fragments_with_different_notes_do_not_merge(self):
        rows = [interval("standing-supported", "站（扶栏杆）", 0, 5000),
                interval("standing-free", "站（未扶栏杆）", 5000, 10000),
                interval("child-supported", "站（扶栏杆）", 5000, 10000, 1)]
        saved = self.save_rows(rows, self.project)
        self.assertEqual(layout(saved["annotations"]["posture"]), layout(rows))
        self.assert_roundtrip(saved)

    def test_same_label_child_extension_reuses_id_and_undo_preserves_main_and_audit(self):
        before_rows = [interval("main-work", "工作学习", 0, 30000),
                       interval("child-meal", "饮食", 16000, 21000, 1)]
        first_actor = {"id": "first-editor", "display_name": "原标注员"}
        with patch("backend.service.now", return_value="2026-10-14T10:00:00Z"):
            before = self.save_rows(before_rows, self.project, actor=first_actor, axis="category")
        before_snapshot = copy.deepcopy(before["annotations"])
        rows = copy.deepcopy(before["annotations"]["category"])
        next(item for item in rows if item["id"] == "child-meal")["end_ms"] = 23000
        with patch("backend.service.now", return_value="2026-10-14T10:01:00Z"):
            saved = self.save_rows(rows, before, axis="category")
        records = {item["id"]: item for item in saved["annotations"]["category"]}
        originals = {item["id"]: item for item in before_snapshot["category"]}
        self.assertEqual(set(records), {"main-work", "child-meal"})
        self.assertEqual(records["main-work"], originals["main-work"])
        self.assertEqual((records["child-meal"]["start_ms"], records["child-meal"]["end_ms"], records["child-meal"]["lane"]), (16000, 23000, 1))
        self.assertEqual((records["child-meal"]["created_by"], records["child-meal"]["created_at"]),
                         (first_actor["id"], "2026-10-14T10:00:00Z"))
        self.assertEqual((records["child-meal"]["updated_by"], records["child-meal"]["updated_at"]),
                         (self.actor["id"], "2026-10-14T10:01:00Z"))
        changes = [item for item in self.service.edit_history(saved["id"]) if item["revision"] == saved["revision"]]
        self.assertEqual(len(changes), 1)
        self.assertEqual((changes[0]["action"], changes[0]["segment_id"]), ("update", "child-meal"))
        self.assertEqual(changes[0]["before"], originals["child-meal"])
        self.assertEqual(changes[0]["after"], records["child-meal"])
        self.assert_roundtrip(saved)
        current = self.service.load(saved["id"])
        with patch("backend.service.now", return_value="2026-10-14T10:02:00Z"):
            restored = self.service.update_draft(saved["id"], before_snapshot, current["revision"], actor=self.actor)
        undo_records = {item["id"]: item for item in restored["annotations"]["category"]}
        self.assertEqual(layout(restored["annotations"]["category"]), layout(before_rows))
        self.assertEqual(undo_records["main-work"], originals["main-work"])
        self.assertEqual(undo_records["child-meal"]["created_by"], first_actor["id"])
        self.assertEqual(undo_records["child-meal"]["updated_at"], "2026-10-14T10:02:00Z")
        undo_events = [item for item in self.service.edit_history(saved["id"]) if item["revision"] == restored["revision"]]
        self.assertEqual([(item["action"], item["segment_id"], item["before"]["end_ms"], item["after"]["end_ms"])
                          for item in undo_events], [("update", "child-meal", 23000, 21000)])
        self.assertEqual(before["annotations"], before_snapshot)
        self.assert_roundtrip(restored)

    def test_anchor_fragments_roundtrip_on_two_existing_child_lanes(self):
        before_rows = [interval("main-work", "工作学习", 0, 30000),
                       interval("meal-before", "饮食", 16000, 21000, 1),
                       interval("first-child-busy", "社交", 21000, 22000, 1),
                       interval("second-child-existing", "休息", 0, 5000, 2)]
        before = self.save_rows(before_rows, self.project, axis="category")
        # The frontend places the 20–23 second anchor range piece by piece:
        # 20–21 is already meal on child 1, 21–22 uses child 2, 22–23 child 1.
        fragments = [*before["annotations"]["category"],
                     interval("meal-middle", "饮食", 21000, 22000, 2),
                     interval("meal-tail", "饮食", 22000, 23000, 1)]
        saved = self.save_rows(fragments, before, axis="category")
        records = {item["id"]: item for item in saved["annotations"]["category"]}
        self.assertEqual(set(item["lane"] for item in records.values()), {0, 1, 2})
        for old in before["annotations"]["category"]:
            self.assertEqual(records[old["id"]], old)
        self.assertEqual(layout(saved["annotations"]["category"]), layout(fragments))
        changes = [item for item in self.service.edit_history(saved["id"]) if item["revision"] == saved["revision"]]
        self.assertEqual({(item["action"], item["segment_id"]) for item in changes},
                         {("create", "meal-middle"), ("create", "meal-tail")})
        self.assert_roundtrip(saved)


if __name__ == "__main__":
    unittest.main()
