"""Four-axis semantics, legacy compatibility and atomic export regressions."""
from __future__ import annotations

import copy
import io
import json
import os
import unittest
from pathlib import Path
import zipfile
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.service import AXES, LEGACY_AXES, FILENAMES, FIXED_LABELS, ProjectService, digest, empty_annotations, filename_for_axis, validate_annotations
from backend import test_service
from backend.testing_annotations import complete_annotations


def interval(ident, label, start, end, **extra):
    return {"id": ident, "label": label, "kind": "interval", "start_ms": start, "end_ms": end, **extra}


class FourAxisValidationTests(unittest.TestCase):
    def full(self, duration=1000):
        return {"scene": [interval("s", "室内", 0, duration)],
                "posture": [interval("p", "坐", 0, duration)],
                "category": [interval("c", "专注", 0, duration)], "habit": []}

    def test_category_overlap_preserves_modes(self):
        annotations = self.full()
        annotations["category"] = [interval("a", "专注", 0, 700), interval("b", "用餐", 300, 1000, mode="overlay")]
        result = validate_annotations(annotations, 1000, final=True)
        self.assertEqual([record["mode"] for record in result["category"]], ["state", "overlay"])
        self.assertEqual(len(result["category"]), 2)
        annotations["scene"].append(interval("bad", "室外", 300, 600))
        with self.assertRaises(HTTPException):
            validate_annotations(annotations, 1000)

    def test_every_required_axis_rejects_first_middle_last_or_total_gap_only_at_export(self):
        for axis in ("scene", "posture"):
            for spans in ([], [(1, 1000)], [(0, 999)], [(0, 400), (401, 1000)]):
                with self.subTest(axis=axis, spans=spans):
                    annotations = self.full()
                    label = annotations[axis][0]["label"]
                    annotations[axis] = [interval(str(i), label, start, end) for i, (start, end) in enumerate(spans)]
                    validate_annotations(annotations, 1000)
                    with self.assertRaises(HTTPException) as error:
                        validate_annotations(annotations, 1000, final=True)
                    self.assertEqual(error.exception.status_code, 422)

    def test_optional_category_allows_first_middle_last_or_total_gaps_without_filling(self):
        for spans in ([], [(1, 1000)], [(0, 999)], [(0, 400), (401, 1000)]):
            with self.subTest(spans=spans):
                annotations = self.full()
                annotations["category"] = [interval(str(i), "专注", start, end, mode="state")
                                           for i, (start, end) in enumerate(spans)]
                original = copy.deepcopy(annotations)
                result = validate_annotations(annotations, 1000, final=True)
                self.assertEqual(result, original)
                self.assertEqual(annotations, original)

    def test_coverage_ignores_real_video_gaps_but_category_cannot_cross_them(self):
        videos = [{"start_ms": 0, "end_ms": 1000}, {"start_ms": 2000, "end_ms": 3000}]
        annotations = self.full(3000)
        for axis in ("scene", "posture", "category"):
            label = annotations[axis][0]["label"]
            annotations[axis] = [interval("a", label, 0, 1000), interval("b", label, 2000, 3000)]
        annotations["category"].append(interval("overlay", "用餐", 300, 700, mode="overlay"))
        validate_annotations(annotations, 3000, final=True, videos=videos)
        annotations["category"].append(interval("cross-gap", "活动", 500, 2500, mode="overlay"))
        with self.assertRaises(HTTPException):
            validate_annotations(annotations, 3000, videos=videos)

    def test_fixed_category_labels_and_closed_intervals_only(self):
        self.assertEqual(set(FIXED_LABELS["category"]), {"专注", "活动", "用餐", "通勤", "社交", "放松", "休息", "其他"})
        for label in FIXED_LABELS["category"]:
            annotations = self.full()
            annotations["category"][0]["label"] = label
            validate_annotations(annotations, 1000, final=True)
        for changes in ({"label": "invalid"}, {"mode": "invalid"}, {"mode": None}, {"end_ms": None},
                        {"kind": "point", "end_ms": 0}, {"start_ms": -1}, {"end_ms": 1001},
                        {"start_ms": 500, "end_ms": 500}):
            annotations = self.full()
            annotations["category"][0].update(changes)
            for final in (False, True):
                with self.subTest(changes=changes, final=final), self.assertRaises(HTTPException) as error:
                    validate_annotations(annotations, 1000, final=final)
                self.assertEqual(error.exception.status_code, 422)

    def test_legacy_axes_do_not_get_default_labels(self):
        legacy = {axis: [] for axis in LEGACY_AXES}
        legacy["scene"] = [interval("vehicle", "车内", 100, 700), interval("other", "其他", 700, 1000)]
        original = copy.deepcopy(legacy)
        result = validate_annotations(legacy, 1000)
        self.assertEqual(legacy, original)
        self.assertEqual(result["category"], [])
        self.assertEqual(result["scene"], legacy["scene"])
        with self.assertRaises(HTTPException):
            validate_annotations(legacy, 1000, final=True)

    def test_adjacent_category_records_preserve_distinct_modes_and_ids(self):
        annotations = self.full()
        annotations["category"] = [interval("a", "专注", 0, 500), interval("b", "专注", 500, 1000, mode="overlay")]
        self.assertEqual([r["id"] for r in validate_annotations(annotations, 1000, final=True)["category"]], ["a", "b"])

    def test_touching_state_records_keep_distinct_authors(self):
        annotations = self.full()
        annotations["scene"] = [interval("a", "室内", 0, 500, created_by="user-a", created_at="2026-09-30T00:00:00Z"),
                                interval("b", "室内", 500, 1000, created_by="user-b", created_at="2026-09-30T01:00:00Z")]
        self.assertEqual([item["id"] for item in validate_annotations(annotations, 1000)["scene"]], ["a", "b"])

    def test_supplement_splits_category_state_and_overlay_without_labelling_added_frames(self):
        old = {"duration_ms": 2000, "videos": [{"id": "a", "name": "a.mp4", "start_ms": 0, "end_ms": 1000}, {"id": "b", "name": "b.mp4", "start_ms": 1000, "end_ms": 2000}], "annotations": empty_annotations()}
        old["annotations"]["category"] = [interval("base", "专注", 0, 2000), interval("extra", "用餐", 500, 1500, mode="overlay")]
        videos = [{"id": "a", "name": "a.mp4", "start_ms": 0, "end_ms": 1000}, {"id": "added", "name": "added.mp4", "start_ms": 1000, "end_ms": 2000}, {"id": "b", "name": "b.mp4", "start_ms": 2000, "end_ms": 3000}]
        result, warnings = ProjectService.remap_annotations(old, videos, 3000)
        self.assertEqual(warnings, [])
        records = result["category"]
        self.assertEqual(sorted((r["start_ms"], r["end_ms"], r["mode"]) for r in records), [(0, 1000, "state"), (500, 1000, "overlay"), (2000, 2500, "overlay"), (2000, 3000, "state")])
        self.assertEqual(len({r["id"] for r in records}), 4)
        self.assertEqual([r["id"] for r in records[:2]], ["base", "extra"])


class FourAxisPersistenceTests(unittest.TestCase):
    setUp = test_service.PersistenceTests.setUp
    tearDown = test_service.PersistenceTests.tearDown
    annotated_project = test_service.PersistenceTests.annotated_project

    def test_fixed_tracks_can_all_be_removed_and_restored_with_empty_json(self):
        project = self.service.open_path(str(self.source))
        annotations = empty_annotations()
        annotations["scene"] = [interval("scene", "室内", 0, project["duration_ms"])]
        annotations["posture"] = [interval("posture", "坐", 0, project["duration_ms"])]
        saved = self.service.update_draft(project["id"], annotations, project["revision"])
        for axis in AXES:
            saved = self.service.set_fixed_track(project["id"], axis, False, saved["revision"])
        self.assertEqual(saved["fixed_tracks"], [])
        self.assertTrue(all(saved["annotations"][axis] == [] for axis in AXES))
        with self.assertRaises(HTTPException):
            self.service.update_draft(project["id"], {**saved["annotations"], "posture": annotations["posture"]}, saved["revision"])
        self.service.writeback(project["id"])
        for axis in AXES:
            document = json.loads((self.source / "timeline" / FILENAMES[axis]).read_bytes())
            self.assertEqual(document["segments"], [])
            self.assertEqual(document["timebase"]["fixed_tracks"], [])
        fresh = self.service.create(list(self.fpv.glob("*.mp4")), "Round trip", self.source)
        restored, _, _, enabled, _ = self.service.read_external(fresh, include_tracks=True)
        self.assertEqual(enabled, [])
        self.assertTrue(all(restored[axis] == [] for axis in AXES))
        added = self.service.set_fixed_track(project["id"], "posture", True, saved["revision"])
        self.assertEqual(added["fixed_tracks"], ["posture"])
        with self.assertRaises(HTTPException):
            self.service.export_documents(self.service.load(project["id"]), for_writeback=True)

    def test_track_labels_delete_annotations_and_round_trip_with_stable_json(self):
        project = self.annotated_project()
        annotations = project["annotations"]
        annotations["category"] = [interval("category", "专注", 0, project["duration_ms"])]
        saved = self.service.update_draft(project["id"], annotations, project["revision"])
        labels = [value for value in saved["track_labels"]["category"] if value != "专注"] + ["阅读"]
        changed = self.service.set_track_labels(project["id"], "category", labels, saved["revision"])
        self.assertEqual(changed["annotations"]["category"], [])
        self.assertEqual(changed["track_labels"]["category"], labels)
        with self.assertRaises(HTTPException) as stale:
            self.service.set_track_labels(project["id"], "category", labels, saved["revision"])
        self.assertEqual(stale.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            self.service.update_draft(project["id"], {**changed["annotations"], "category": annotations["category"]}, changed["revision"])
        relabeled = self.service.update_draft(project["id"], {**changed["annotations"], "category": [interval("new", "阅读", 0, project["duration_ms"])]}, changed["revision"])
        save_id, documents = self.service.export_documents(self.service.load(project["id"]), for_writeback=True)
        self.assertTrue(save_id)
        category = json.loads(documents["category"])
        self.assertEqual([item["label"] for item in relabeled["annotations"]["category"]], ["阅读"])
        self.assertEqual([item["label"] for item in category["segments"]], ["阅读"])
        self.assertEqual(category["timebase"]["track_labels"]["category"], labels)
        self.assertIn("阅读", [item["name"] for item in category["labels"]])
        self.service.writeback(project["id"])
        fresh = self.service.create(list(self.fpv.glob("*.mp4")), "Round trip", self.source)
        imported, _, _, _, imported_labels = self.service.read_external(fresh, include_tracks=True)
        self.assertEqual([item["label"] for item in imported["category"]], ["阅读"])
        self.assertEqual(imported_labels["category"], labels)

    def test_removing_used_posture_requires_coverage_before_writeback(self):
        project = self.annotated_project()
        changed = self.service.set_track_labels(project["id"], "posture", ["动", "站", "躺", "蹲"], project["revision"])
        self.assertEqual(changed["annotations"]["posture"], [])
        with self.assertRaises(HTTPException):
            self.service.export_documents(self.service.load(project["id"]), for_writeback=True)

    def test_custom_event_shortcut_labels_can_be_added_and_removed(self):
        project = self.annotated_project()
        created = self.service.add_custom_track(project["id"], "事件", "event", [], project["revision"])
        axis = created["custom_tracks"][0]["id"]
        changed = self.service.set_track_labels(project["id"], axis, ["喝水", "看手机"], created["revision"])
        self.assertEqual(changed["custom_tracks"][0]["labels"], ["喝水", "看手机"])
        self.assertEqual(self.service.set_track_labels(project["id"], axis, [], changed["revision"])["custom_tracks"][0]["labels"], [])

    def test_custom_state_last_button_can_be_deleted_with_its_annotations(self):
        project = self.annotated_project()
        created = self.service.add_custom_track(project["id"], "环境", "state", ["安静"], project["revision"])
        axis = created["custom_tracks"][0]["id"]
        annotations = {**created["annotations"], axis: [interval("one", "安静", 0, project["duration_ms"])]}
        saved = self.service.update_draft(project["id"], annotations, created["revision"])
        changed = self.service.set_track_labels(project["id"], axis, [], saved["revision"])
        self.assertEqual(changed["custom_tracks"][0]["labels"], [])
        self.assertEqual(changed["annotations"][axis], [])
        self.assertEqual(json.loads(self.service.export_documents(self.service.load(project["id"]))[1][axis])["segments"], [])

    def test_custom_track_api_adds_to_project_and_rejects_stale_revision(self):
        project = self.annotated_project()
        with TestClient(create_app(self.root, auth_required=False), base_url="http://127.0.0.1") as client:
            path = f"/api/projects/{project['id']}/custom-tracks"
            body = {"name": "环境", "mode": "state", "labels": ["安静", "嘈杂"],
                    "expected_revision": project["revision"]}
            response = client.post(path, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            added = response.json()
            axis = added["custom_tracks"][0]["id"]
            self.assertEqual(added["annotations"][axis], [])
            self.assertEqual(client.get(f"/api/projects/{project['id']}").json()["custom_tracks"], added["custom_tracks"])
            self.assertEqual(client.post(path, json=body).status_code, 409)
            removed = client.request("DELETE", f"{path}/{axis}", json={"confirmed": True, "expected_revision": added["revision"]})
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertEqual(removed.json()["custom_tracks"], [])
            self.assertEqual(client.request("DELETE", f"{path}/{axis}", json={"confirmed": True, "expected_revision": removed.json()["revision"]}).status_code, 404)

    def test_project_custom_state_and_event_tracks_export_writeback_and_reopen(self):
        project = self.annotated_project()
        state = self.service.add_custom_track(project["id"], "环境", "state", ["安静", "嘈杂"], project["revision"])
        event = self.service.add_custom_track(project["id"], "干扰事件", "event", [], state["revision"])
        state_id, event_id = (track["id"] for track in event["custom_tracks"])
        annotations = copy.deepcopy(event["annotations"])
        annotations[state_id] = [interval("quiet", "安静", 500, 1800)]
        annotations[event_id] = [interval("noise", "噪声", 600, 900),
                                 interval("alarm", "闹钟", 750, 750, kind="point")]
        saved = self.service.update_draft(project["id"], annotations, event["revision"])
        with zipfile.ZipFile(io.BytesIO(self.service.export_zip(project["id"]))) as archive:
            self.assertEqual(set(archive.namelist()), set(FILENAMES.values()) |
                             {filename_for_axis(state_id), filename_for_axis(event_id)})
            for track in saved["custom_tracks"]:
                document = json.loads(archive.read(filename_for_axis(track["id"])))
                self.assertEqual(document["axis_name"], track["name"])
                self.assertEqual(document["axis_mode"], track["mode"])
        self.service.writeback(project["id"])
        fresh = ProjectService(self.root / "custom-fresh")
        try:
            reopened = fresh.open_path(str(self.source))
            self.assertEqual(reopened["custom_tracks"], saved["custom_tracks"])
            self.assertEqual(reopened["annotations"][state_id], saved["annotations"][state_id])
            self.assertEqual(reopened["annotations"][event_id], saved["annotations"][event_id])
        finally:
            fresh.previews.close()

    def test_delete_custom_track_removes_its_json_on_next_writeback(self):
        project = self.annotated_project()
        added = self.service.add_custom_track(project["id"], "环境", "state", ["安静"], project["revision"])
        axis = added["custom_tracks"][0]["id"]
        annotations = copy.deepcopy(added["annotations"])
        annotations[axis] = [interval("quiet", "安静", 0, 1000)]
        saved = self.service.update_draft(project["id"], annotations, added["revision"])
        self.service.writeback(project["id"])
        path = self.source / "timeline" / filename_for_axis(axis)
        original = path.read_bytes()
        deleted = self.service.delete_custom_track(project["id"], axis, saved["revision"])
        self.assertEqual(deleted["custom_tracks"], [])
        self.assertNotIn(axis, deleted["annotations"])
        self.assertEqual(path.read_bytes(), original, "the external file remains until writeback")
        self.assertEqual(self.service.open_path(str(self.source))["custom_tracks"], [],
                         "reopening before writeback must keep the deleted local draft")
        with zipfile.ZipFile(io.BytesIO(self.service.export_zip(project["id"]))) as archive:
            self.assertEqual(set(archive.namelist()), set(FILENAMES.values()))
        result = self.service.writeback(project["id"])
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), result["paths"])
        self.assertEqual((self.source / ".annotation-backups" / result["save_id"] / path.name).read_bytes(), original)
        fresh = ProjectService(self.root / "deleted-custom-fresh")
        try:
            self.assertEqual(fresh.open_path(str(self.source))["custom_tracks"], [])
        finally:
            fresh.previews.close()

    def test_delete_custom_track_stops_if_external_file_changed(self):
        project = self.annotated_project()
        added = self.service.add_custom_track(project["id"], "环境", "event", [], project["revision"])
        axis = added["custom_tracks"][0]["id"]
        self.service.writeback(project["id"])
        deleted = self.service.delete_custom_track(project["id"], axis, added["revision"])
        path = self.source / "timeline" / filename_for_axis(axis)
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(HTTPException) as error:
            self.service.writeback(project["id"])
        self.assertEqual(error.exception.status_code, 409)
        self.assertTrue(path.exists())
        self.assertEqual(deleted["custom_tracks"], [])

    def test_failed_writeback_restores_removed_custom_file(self):
        project = self.annotated_project()
        added = self.service.add_custom_track(project["id"], "环境", "event", [], project["revision"])
        axis = added["custom_tracks"][0]["id"]
        self.service.writeback(project["id"])
        path = self.source / "timeline" / filename_for_axis(axis)
        original = path.read_bytes()
        fixed_before = {name: (self.source / "timeline" / name).read_bytes() for name in FILENAMES.values()}
        self.service.delete_custom_track(project["id"], axis, added["revision"])
        original_hashes = self.service.current_external_hashes
        calls = 0
        def fail_verification(source):
            nonlocal calls
            calls += 1
            hashes = original_hashes(source)
            return {**hashes, "unexpected": "changed"} if calls == 3 else hashes
        with patch.object(self.service, "current_external_hashes", side_effect=fail_verification):
            with self.assertRaises(HTTPException) as error:
                self.service.writeback(project["id"])
        self.assertEqual(error.exception.status_code, 500)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual({name: (self.source / "timeline" / name).read_bytes() for name in FILENAMES.values()}, fixed_before)

    def test_custom_state_rejects_unknown_or_overlapping_labels(self):
        project = self.annotated_project()
        project = self.service.add_custom_track(project["id"], "环境", "state", ["安静"], project["revision"])
        axis = project["custom_tracks"][0]["id"]
        for segments in ([interval("bad", "嘈杂", 0, 500)],
                         [interval("a", "安静", 0, 500), interval("b", "安静", 400, 700)]):
            annotations = copy.deepcopy(project["annotations"])
            annotations[axis] = segments
            with self.assertRaises(HTTPException) as error:
                self.service.update_draft(project["id"], annotations, project["revision"])
            self.assertEqual(error.exception.status_code, 422)

    def test_supplement_remaps_custom_state_and_event_to_original_videos(self):
        state_id, event_id = "custom_" + "a" * 32, "custom_" + "b" * 32
        old = {"duration_ms": 2000, "videos": [
            {"id": "a", "name": "a.mp4", "start_ms": 0, "end_ms": 1000},
            {"id": "b", "name": "b.mp4", "start_ms": 1000, "end_ms": 2000}],
            "custom_tracks": [{"id": state_id, "name": "环境", "mode": "state", "labels": ["安静"]},
                              {"id": event_id, "name": "干扰", "mode": "event", "labels": []}],
            "annotations": {**empty_annotations(),
                            state_id: [interval("quiet", "安静", 0, 2000)],
                            event_id: [interval("alarm", "响铃", 1500, 1500, kind="point")]}}
        videos = [{"id": "a", "name": "a.mp4", "start_ms": 0, "end_ms": 1000},
                  {"id": "added", "name": "added.mp4", "start_ms": 1000, "end_ms": 2000},
                  {"id": "b", "name": "b.mp4", "start_ms": 2000, "end_ms": 3000}]
        result, warnings = ProjectService.remap_annotations(old, videos, 3000)
        self.assertEqual(warnings, [])
        self.assertEqual([(item["start_ms"], item["end_ms"]) for item in result[state_id]],
                         [(0, 1000), (2000, 3000)])
        self.assertEqual(result[event_id][0]["start_ms"], 2500)

    def legacy_files(self, project):
        stored = self.service.load(project["id"])
        _, documents = self.service.export_documents(stored)
        hashes = {}
        for axis in LEGACY_AXES:
            document = json.loads(documents[axis])
            document["schema_version"] = 1
            # Legacy files were allowed to contain incomplete axes.
            if axis == "posture":
                document["segments"] = []
            raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
            (self.source / FILENAMES[axis]).write_bytes(raw)
            hashes[axis] = digest(raw)
        stored["annotations"].pop("category")
        stored["annotations"]["posture"] = []
        stored["_external_hashes"] = hashes
        stored["draft_dirty"] = False
        self.service.save(stored)
        return stored

    def test_read_only_legacy_draft_and_reopen_do_not_rewrite_annotations_or_revision(self):
        stored = self.legacy_files(self.annotated_project())
        public = self.service.public(stored)
        self.assertEqual(public["annotations"]["category"], [])
        self.assertEqual(self.service.load(stored["id"]), stored)
        reopened = self.service.open_path(str(self.source))
        self.assertEqual(reopened["revision"], stored["revision"])
        self.assertEqual(reopened["annotations"]["category"], [])
        self.assertEqual(reopened["warnings"], [])
        after = self.service.load(stored["id"])
        self.assertEqual(after["annotations"], stored["annotations"])
        self.assertEqual(after["_external_hashes"], stored["_external_hashes"])
        fresh = ProjectService(self.root / "legacy-fresh")
        try:
            imported = fresh.open_path(str(self.source))
            self.assertEqual(imported["annotations"], public["annotations"])
        finally:
            fresh.previews.close()

    def test_legacy_client_save_preserves_existing_new_axis(self):
        project = self.annotated_project()
        annotations = {axis: project["annotations"][axis] for axis in LEGACY_AXES}
        updated = self.service.update_draft(project["id"], annotations, project["revision"])
        self.assertEqual(updated["annotations"]["category"], project["annotations"]["category"])

    def test_upgrade_old_three_hash_baseline_writes_four_same_batch_files(self):
        stored = self.legacy_files(self.annotated_project())
        self.service.update_draft(stored["id"], complete_annotations(stored), stored["revision"])
        saved = self.service.writeback(stored["id"])
        self.assertEqual(len(saved["paths"]), 4)
        self.assertEqual(set(self.service.load(stored["id"])["_external_hashes"]), set(AXES))
        for axis in AXES:
            document = json.loads((self.source / "timeline" / FILENAMES[axis]).read_bytes())
            self.assertEqual(document["schema_version"], 3)
            self.assertEqual(document["save_id"], saved["save_id"])
        self.assertEqual(json.loads((self.source / FILENAMES["scene"]).read_bytes())["schema_version"], 1)

    def test_four_postures_survive_save_export_writeback_and_reopen(self):
        project = self.annotated_project()
        annotations = copy.deepcopy(project["annotations"])
        annotations["posture"] = [interval(f"posture-{i}", label, i * 750, (i + 1) * 750)
                                  for i, label in enumerate(("动", "坐", "站", "躺"))]
        updated = self.service.update_draft(project["id"], annotations, project["revision"])
        with zipfile.ZipFile(io.BytesIO(self.service.export_zip(project["id"]))) as archive:
            document = json.loads(archive.read(FILENAMES["posture"]))
        self.assertEqual([record["label_id"] for record in document["segments"]],
                         ["moving", "sitting", "standing", "lying"])
        self.service.writeback(project["id"])
        result, _ = self.service.read_external(self.service.load(project["id"]))
        self.assertEqual(result, updated["annotations"])
        fresh = ProjectService(self.root / "posture-fresh")
        try:
            self.assertEqual(fresh.open_path(str(self.source))["annotations"], updated["annotations"])
        finally:
            fresh.previews.close()

    def test_v3_attribution_survives_export_writeback_and_reopen(self):
        project = self.annotated_project()
        annotations = copy.deepcopy(project["annotations"])
        annotations["habit"] = [interval("water", "喝水", 500, 600, created_by="forged")]
        actor = {"id": "user-1", "display_name": "标注甲"}
        saved = self.service.update_draft(project["id"], annotations, project["revision"], actor=actor)
        author = saved["annotations"]["habit"][0]
        self.assertEqual(author["created_by"], "user-1")
        self.assertEqual(author["created_by_name"], "标注甲")
        with zipfile.ZipFile(io.BytesIO(self.service.export_zip(project["id"]))) as archive:
            exported = json.loads(archive.read(FILENAMES["habit"]))
        self.assertEqual(exported["schema_version"], 3)
        self.assertEqual(exported["segments"][0]["created_by"], "user-1")
        self.service.writeback(project["id"])
        imported, _ = self.service.read_external(self.service.load(project["id"]))
        self.assertEqual(imported["habit"][0], author)

    def test_category_state_and_overlay_survive_save_export_import(self):
        project = self.annotated_project()
        annotations = project["annotations"]
        annotations["category"] = [interval("base", "专注", 0, 3000), interval("extra", "用餐", 500, 1500, mode="overlay")]
        updated = self.service.update_draft(project["id"], annotations, project["revision"])
        self.service.writeback(project["id"])
        result, _ = self.service.read_external(self.service.load(project["id"]))
        self.assertEqual(result, updated["annotations"])
        self.assertEqual([r["mode"] for r in result["category"]], ["state", "overlay"])

    def assert_optional_category_roundtrip(self, category):
        project = self.annotated_project()
        annotations = copy.deepcopy(project["annotations"])
        annotations["category"] = copy.deepcopy(category)
        updated = self.service.update_draft(project["id"], annotations, project["revision"])
        expected = updated["annotations"]
        self.assertEqual(expected["category"], category)
        with zipfile.ZipFile(io.BytesIO(self.service.export_zip(project["id"]))) as archive:
            self.assertEqual(set(archive.namelist()), set(FILENAMES.values()))
            documents = {axis: json.loads(archive.read(FILENAMES[axis])) for axis in AXES}
        self.assertEqual({document["schema_version"] for document in documents.values()}, {3})
        self.assertEqual(len({document["save_id"] for document in documents.values()}), 1)
        category_fields = ("id", "label", "kind", "start_ms", "end_ms", "mode")
        self.assertEqual([{key: record[key] for key in category_fields}
                          for record in documents["category"]["segments"]], category)
        self.assertEqual(self.service.load(project["id"])["annotations"], expected)
        saved = self.service.writeback(project["id"])
        self.assertEqual(len(saved["paths"]), 4)
        stored = self.service.load(project["id"])
        result, hashes = self.service.read_external(stored)
        self.assertEqual(result, expected)
        self.assertEqual(hashes, stored["_external_hashes"])
        self.assertEqual(stored["annotations"], expected)
        for axis in AXES:
            document = json.loads((self.source / "timeline" / FILENAMES[axis]).read_bytes())
            self.assertEqual(document["schema_version"], 3)
            self.assertEqual(document["save_id"], saved["save_id"])
        fresh = ProjectService(self.root / "optional-category-fresh")
        try:
            reopened = fresh.open_path(str(self.source))
            self.assertEqual(reopened["annotations"], expected)
        finally:
            fresh.previews.close()

    def test_empty_category_exports_writes_and_reopens_as_empty_fourth_axis(self):
        self.assert_optional_category_roundtrip([])

    def test_partial_overlapping_category_exports_writes_and_reopens_without_filling(self):
        self.assert_optional_category_roundtrip([
            interval("base", "专注", 500, 1500, mode="state"),
            interval("extra", "用餐", 800, 2000, mode="overlay"),
        ])

    def test_missing_posture_still_blocks_export_and_writeback_with_empty_category(self):
        project = self.annotated_project()
        revision = project["revision"]
        for partial in (False, True):
            with self.subTest(partial=partial):
                annotations = copy.deepcopy(project["annotations"])
                annotations["category"] = []
                label = annotations["posture"][0]["label"]
                annotations["posture"] = [interval("partial", label, 100, 3000)] if partial else []
                updated = self.service.update_draft(project["id"], annotations, revision)
                revision = updated["revision"]
                for operation in (self.service.export_zip, self.service.writeback):
                    with self.assertRaises(HTTPException) as error:
                        operation(project["id"])
                    self.assertEqual(error.exception.status_code, 422)
                    self.assertIn("姿势轴尚未覆盖", error.exception.detail)
                self.assertFalse(any((self.source / "timeline" / filename).exists() for filename in FILENAMES.values()))

    def test_incomplete_scene_blocks_export_but_writes_back_and_reopens_without_filling(self):
        project = self.annotated_project()
        for scene in ([], [interval("partial", "室内", 100, 1500)]):
            with self.subTest(scene=scene):
                annotations = copy.deepcopy(project["annotations"])
                annotations["scene"] = scene
                updated = self.service.update_draft(project["id"], annotations, project["revision"])
                project = updated
                with self.assertRaises(HTTPException) as error:
                    self.service.export_zip(project["id"])
                self.assertEqual(error.exception.status_code, 422)
                self.assertIn("场景轴尚未覆盖", error.exception.detail)
                self.service.writeback(project["id"])
                document = json.loads((self.source / "timeline" / FILENAMES["scene"]).read_bytes())
                self.assertEqual([(item["start_ms"], item["end_ms"]) for item in document["segments"]],
                                 [(item["start_ms"], item["end_ms"]) for item in scene])
                imported, _ = self.service.read_external(self.service.load(project["id"]))
                self.assertEqual(imported["scene"], scene)
                fresh = ProjectService(self.root / "partial-scene-fresh")
                try:
                    self.assertEqual(fresh.open_path(str(self.source))["annotations"]["scene"], scene)
                finally:
                    fresh.previews.close()
                project = self.service.load(project["id"])

    def test_deleted_scene_first_segment_remains_saved_draft_and_can_write_back(self):
        project = self.annotated_project()
        annotations = project["annotations"]
        annotations["scene"] = annotations["scene"][1:]
        annotations["category"] = []
        updated = self.service.update_draft(project["id"], annotations, project["revision"])
        self.assertEqual(updated["annotations"]["scene"][0]["start_ms"], 1500)
        self.assertEqual(updated["annotations"]["category"], [])
        with self.assertRaises(HTTPException):
            self.service.export_zip(project["id"])
        self.service.writeback(project["id"])
        self.assertTrue(all((self.source / "timeline" / filename).exists() for filename in FILENAMES.values()))

    def test_missing_new_category_file_or_mixed_version_batch_is_rejected(self):
        project = self.annotated_project()
        self.service.writeback(project["id"])
        path = self.source / "timeline" / FILENAMES["category"]
        raw = path.read_bytes()
        path.unlink()
        with self.assertRaises(HTTPException) as error:
            self.service.read_external(self.service.load(project["id"]))
        self.assertEqual(error.exception.status_code, 409)
        document = json.loads(raw)
        for changes in ({"schema_version": 1}, {"save_id": "different"}):
            path.write_text(json.dumps({**document, **changes}), encoding="utf-8")
            with self.assertRaises(HTTPException) as error:
                self.service.read_external(self.service.load(project["id"]))
            self.assertEqual(error.exception.status_code, 409)

    def test_external_category_creation_conflicts_with_old_hash_baseline(self):
        stored = self.legacy_files(self.annotated_project())
        self.service.update_draft(stored["id"], complete_annotations(stored), stored["revision"])
        (self.source / FILENAMES["category"]).write_text("external work", encoding="utf-8")
        with self.assertRaises(HTTPException) as error:
            self.service.writeback(stored["id"])
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual((self.source / FILENAMES["category"]).read_text(encoding="utf-8"), "external work")

    def test_fourth_file_failure_rolls_back_category_and_other_three_files(self):
        project = self.annotated_project()
        self.service.writeback(project["id"])
        originals = {axis: (self.source / "timeline" / FILENAMES[axis]).read_bytes() for axis in AXES}
        replace = os.replace
        calls = 0
        def fail_last(source, target):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError("simulated fourth-file failure")
            return replace(source, target)
        with patch("backend.service.os.replace", side_effect=fail_last), self.assertRaises(HTTPException) as error:
            self.service.writeback(project["id"])
        self.assertEqual(error.exception.status_code, 500)
        self.assertEqual(originals, {axis: (self.source / "timeline" / FILENAMES[axis]).read_bytes() for axis in AXES})

    def test_scene_export_only_advertises_current_options_and_used_legacy_ids(self):
        project = self.annotated_project()
        stored = self.service.load(project["id"])
        _, documents = self.service.export_documents(stored)
        self.assertEqual(json.loads(documents["scene"])["labels"],
                         [{"id": "indoor", "name": "室内"}, {"id": "outdoor", "name": "室外"}])
        for legacy_label, legacy_id in (("车内", "in_vehicle"), ("其他", "other")):
            with self.subTest(label=legacy_label):
                stored["annotations"]["scene"] = [interval("legacy", legacy_label, 0, stored["duration_ms"])]
                _, documents = self.service.export_documents(stored)
                document = json.loads(documents["scene"])
                self.assertEqual(document["labels"], [{"id": "indoor", "name": "室内"}, {"id": "outdoor", "name": "室外"},
                                                       {"id": legacy_id, "name": legacy_label}])
                self.assertEqual(document["segments"][0]["label_id"], legacy_id)

    def test_health_advertises_four_axis_support(self):
        with TestClient(create_app(self.root / "four-axis-health", auth_required=False), base_url="http://127.0.0.1") as client:
            self.assertIn("four-axis-annotations", client.get("/api/health").json()["capabilities"])

    def test_writeback_creates_timeline_directory_and_keeps_root_clear(self):
        project = self.annotated_project()
        saved = self.service.writeback(project["id"])
        self.assertEqual({Path(p).parent for p in saved["paths"]}, {self.source / "timeline"})
        self.assertEqual({p.name for p in (self.source / "timeline").iterdir()}, set(FILENAMES.values()))
        self.assertFalse(any((self.source / name).exists() for name in FILENAMES.values()))

    def test_failed_first_subfolder_write_preserves_legacy_files_and_can_retry(self):
        stored = self.legacy_files(self.annotated_project())
        originals = {axis: (self.source / FILENAMES[axis]).read_bytes() for axis in LEGACY_AXES}
        self.service.update_draft(stored["id"], complete_annotations(stored), stored["revision"])
        replace = os.replace
        calls = 0
        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated migration write failure")
            return replace(source, target)
        with patch("backend.service.os.replace", side_effect=fail_second), self.assertRaises(HTTPException):
            self.service.writeback(stored["id"])
        self.assertEqual(originals, {axis: (self.source / FILENAMES[axis]).read_bytes() for axis in LEGACY_AXES})
        self.assertEqual(list((self.source / "timeline").iterdir()), [])
        self.assertEqual(self.service.current_external_hashes(self.source), self.service.expected_external_hashes(stored))
        self.service.writeback(stored["id"])
        self.assertEqual(originals, {axis: (self.source / FILENAMES[axis]).read_bytes() for axis in LEGACY_AXES})
        result, _ = self.service.read_external(self.service.load(stored["id"]))
        self.assertEqual(result, self.service.load(stored["id"])["annotations"])
        (self.source / "timeline" / FILENAMES["category"]).unlink()
        with self.assertRaises(HTTPException) as error:
            self.service.read_external(stored)
        self.assertEqual(error.exception.status_code, 409)

    def test_same_named_file_blocks_writeback_without_changing_it(self):
        project = self.annotated_project()
        target = self.source / "timeline"
        target.write_bytes(b"user file")
        with self.assertRaises(HTTPException) as error:
            self.service.writeback(project["id"])
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(target.read_bytes(), b"user file")
