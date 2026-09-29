from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.testing_annotations import complete_annotations, complete_project
from backend.service import AXES, FILENAMES, ProjectService, empty_annotations, natural_key, parse_recording_start, recording_layout, validate_annotations

ROOT = Path(__file__).resolve().parents[1]


class ValidationTests(unittest.TestCase):
    def test_numeric_filename_sort(self):
        self.assertEqual(sorted(["clip10.mp4", "clip2.mp4", "clip1.mp4"], key=natural_key), ["clip1.mp4", "clip2.mp4", "clip10.mp4"])

    def test_recording_filename_patterns_and_missing_timestamp(self):
        for name in ("A09999_20260909161720_0004.avi", "20260909_161720.mp4", "2026-09-09_16-17-20.mp4"):
            self.assertEqual(parse_recording_start(name).isoformat(), "2026-09-09T16:17:20+08:00")
        self.assertEqual(parse_recording_start("20260909161720123.mp4").microsecond, 123000)
        for name in ("clip1.mp4", "20261309161720.mp4", "20260909161720_20260909161721.mp4"):
            with self.assertRaises(HTTPException):
                parse_recording_start(name)

    def test_scene_overlap_rejected_habit_overlap_allowed(self):
        annotations = empty_annotations()
        annotations["scene"] = [{"id": "a", "label": "室内", "start_ms": 0, "end_ms": 700}, {"id": "b", "label": "室外", "start_ms": 500, "end_ms": 1000}]
        with self.assertRaises(HTTPException):
            validate_annotations(annotations, 1000)
        annotations["scene"] = []
        annotations["habit"] = [{"id": "a", "label": "吃饭", "start_ms": 0, "end_ms": 700}, {"id": "b", "label": "看手机", "start_ms": 500, "end_ms": 1000}]
        self.assertEqual(len(validate_annotations(annotations, 1000)["habit"]), 2)

    def test_open_habit_only_allowed_in_draft(self):
        annotations = empty_annotations()
        annotations["habit"] = [{"id": "a", "label": "喝水", "start_ms": 50, "end_ms": None}]
        validate_annotations(annotations, 1000)
        with self.assertRaises(HTTPException):
            validate_annotations(annotations, 1000, final=True)

    def test_point_event_boundaries_and_invalid_kinds(self):
        annotations = empty_annotations()
        annotations["habit"] = [{"id": "point-start", "label": "眨眼", "kind": "point", "start_ms": 0, "end_ms": 0}, {"id": "point-end", "label": "咳嗽", "kind": "point", "start_ms": 1000, "end_ms": 1000}]
        annotations = complete_annotations({"duration_ms": 1000}, annotations)
        validated = validate_annotations(annotations, 1000, final=True)
        self.assertEqual([item["kind"] for item in validated["habit"]], ["point", "point"])
        annotations["habit"][0]["end_ms"] = 1
        with self.assertRaises(HTTPException):
            validate_annotations(annotations, 1000)
        annotations = empty_annotations()
        annotations["scene"] = [{"id": "a", "label": "室内", "kind": "point", "start_ms": 0, "end_ms": 0}]
        with self.assertRaises(HTTPException):
            validate_annotations(annotations, 1000)

    def test_annotations_cannot_cover_recording_gaps(self):
        videos = [{"start_ms": 0, "end_ms": 1000}, {"start_ms": 11000, "end_ms": 12000}]
        for point in [0, 1000, 11000, 12000]:
            annotations = empty_annotations()
            annotations["habit"] = [{"id": "p", "label": "咳嗽", "kind": "point", "start_ms": point, "end_ms": point}]
            validate_annotations(annotations, 12000, videos=videos)
        for kind, start, end in [("point", 6000, 6000), ("interval", 500, 11500), ("interval", 6000, None), ("interval", 1000, 11000)]:
            annotations = empty_annotations()
            annotations["habit"] = [{"id": "gap", "label": "吃饭", "kind": kind, "start_ms": start, "end_ms": end}]
            with self.assertRaises(HTTPException):
                validate_annotations(annotations, 12000, videos=videos)
        annotations = empty_annotations()
        annotations["habit"] = [{"id": "ok", "label": "吃饭", "start_ms": 200, "end_ms": 1200}]
        validate_annotations(annotations, 2000, videos=[{"start_ms": 0, "end_ms": 1000}, {"start_ms": 1000, "end_ms": 2000}])

    def seam_videos(self):
        return [
            {"id": "v24", "name": "A09999_20260911183100_0023.avi", "start_ms": 19165033, "end_ms": 19764766, "duration_ms": 599733, "alignment_offset_ms": 33},
            {"id": "v25", "name": "A09999_20260911184100_0024.avi", "start_ms": 19765000, "end_ms": 20364767, "duration_ms": 599767},
        ]

    def test_recording_runs_bridge_only_qualified_camera_rounding(self):
        videos = self.seam_videos()
        runs, bridges, gaps = recording_layout(videos)
        self.assertEqual(runs, [{"start_ms": 19165033, "end_ms": 20364767, "video_ids": ["v24", "v25"]}])
        self.assertEqual(gaps, [])
        self.assertEqual((bridges[0]["start_ms"], bridges[0]["end_ms"], bridges[0]["raw_boundary_error_ms"]), (19764766, 19765000, -267))
        for name, start, offset in [
            ("B09999_20260911184100_0024.avi", 19765000, 0),
            ("A09999_20260911184100_0025.avi", 19765000, 0),
            ("A09999_20260911184100000_0024.avi", 19765000, 0),
            ("A09999_20260911184101_0024.avi", 19765000, 0),
            ("A09999_20260911184100_0024.avi", 19765766, 0),
            ("A09999_20260911184100_0024.avi", 19765000, 3001),
        ]:
            with self.subTest(name=name, start=start, offset=offset):
                changed = [dict(videos[0]), {**videos[1], "name": name, "start_ms": start, "alignment_offset_ms": offset}]
                rejected_runs, rejected_bridges, rejected_gaps = recording_layout(changed)
                self.assertEqual(len(rejected_runs), 2)
                self.assertEqual(rejected_bridges, [])
                self.assertEqual(rejected_gaps, [{"start_ms": 19764766, "end_ms": start}])

    def test_camera_seam_alignment_limit_includes_3000_without_widening_gaps(self):
        videos = self.seam_videos()
        videos[1]["alignment_offset_ms"] = 3000
        runs, bridges, gaps = recording_layout(videos)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(bridges), 1)
        self.assertEqual(gaps, [])
        videos[1]["start_ms"] = videos[0]["end_ms"] + 1000
        runs, bridges, gaps = recording_layout(videos)
        self.assertEqual(len(runs), 2)
        self.assertEqual(bridges, [])
        self.assertEqual(gaps, [{"start_ms": videos[0]["end_ms"], "end_ms": videos[1]["start_ms"]}])

    def test_legacy_state_split_is_repaired_only_at_a_qualified_seam(self):
        videos = self.seam_videos()
        annotations = empty_annotations()
        annotations["scene"] = [
            {"id": "left", "label": "室内", "start_ms": 19165033, "end_ms": 19764766},
            {"id": "right", "label": "室内", "start_ms": 19766146, "end_ms": 20364767},
        ]
        annotations["posture"] = [
            {"id": "standing", "label": "站", "start_ms": 19165033, "end_ms": 19764766},
            {"id": "sitting", "label": "坐", "start_ms": 19766146, "end_ms": 20364767},
        ]
        annotations["habit"] = [{**item, "label": "抽烟"} for item in annotations["scene"]]
        result = validate_annotations(annotations, 20364767, videos=videos)
        self.assertEqual(result["scene"], [{"id": "left", "label": "室内", "kind": "interval", "start_ms": 19165033, "end_ms": 20364767}])
        self.assertEqual(len(result["posture"]), 2)
        self.assertEqual(len(result["habit"]), 2)
        self.assertEqual(annotations["scene"][0]["end_ms"], 19764766)
        annotations["scene"][0]["end_ms"] -= 1
        self.assertEqual(len(validate_annotations(annotations, 20364767, videos=videos)["scene"]), 2)

    def test_touching_equal_states_merge_but_behavior_events_keep_boundaries(self):
        annotations = empty_annotations()
        annotations["scene"] = [
            {"id": "left", "label": "室内", "start_ms": 0, "end_ms": 1000},
            {"id": "right", "label": "室内", "start_ms": 1000, "end_ms": 2000},
        ]
        annotations["habit"] = [{**item, "label": "抽烟"} for item in annotations["scene"]]
        result = validate_annotations(annotations, 2000)
        self.assertEqual(result["scene"], [{"id": "left", "label": "室内", "kind": "interval", "start_ms": 0, "end_ms": 2000}])
        self.assertEqual(len(result["habit"]), 2)

    def test_continuous_annotation_crosses_camera_seam_but_not_real_gap(self):
        videos = self.seam_videos()
        annotations = empty_annotations()
        annotations["habit"] = [{"id": "continuous", "label": "抽烟", "start_ms": 19764000, "end_ms": 19767000}]
        validate_annotations(annotations, 20364767, videos=videos)
        annotations["scene"] = [{"id": "state", "label": "室内", "start_ms": 19764000, "end_ms": 19767000}]
        validate_annotations(annotations, 20364767, videos=videos)
        videos[1]["name"] = "B09999_20260911184100_0024.avi"
        with self.assertRaises(HTTPException):
            validate_annotations(annotations, 20364767, videos=videos)

    def test_invalid_time_bounds_and_boolean_rejected(self):
        for start, end in [(False, 30), (30, 30), (-1, 30), (0, 1001), (0.5, 30), (1000, None)]:
            with self.subTest(start=start, end=end):
                annotations = empty_annotations()
                annotations["habit"] = [{"id": "a", "label": "喝水", "start_ms": start, "end_ms": end}]
                with self.assertRaises(HTTPException):
                    validate_annotations(annotations, 1000)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="backend-tests-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.service = ProjectService(self.root)
        self.source = self.root / "collection"
        self.fpv = self.source / "FPV"
        self.fpv.mkdir(parents=True)
        for name in ["clip10_20260914000002.mp4", "clip2_20260914000001.mp4", "clip1_20260914000000.mp4"]:
            (self.fpv / name).write_bytes(b"synthetic-media-" + name.encode())
        self.probe = patch.object(ProjectService, "probe", return_value={"duration_ms": 1000, "codec": "h264", "pixel_format": "yuv420p", "audio_codecs": []})
        self.probe.start()

    def tearDown(self):
        self.probe.stop()
        # TemporaryDirectory was allocated directly under the project .tmp root.
        self.assertEqual(self.root.resolve().parent, (ROOT / ".tmp").resolve())
        self.temp.cleanup()

    def annotated_project(self):
        project = self.service.open_path(str(self.source))
        annotations = empty_annotations()
        annotations["scene"] = [{"id": "scene-1", "label": "室内", "start_ms": 0, "end_ms": 1500}, {"id": "scene-2", "label": "室外", "start_ms": 1500, "end_ms": 3000}]
        annotations["posture"] = [{"id": "posture-1", "label": "坐", "start_ms": 0, "end_ms": 3000}]
        annotations["habit"] = [{"id": "habit-2", "label": "看手机", "start_ms": 1200, "end_ms": 2300}, {"id": "habit-1", "label": "吃饭", "start_ms": 500, "end_ms": 2000}]
        return self.service.update_draft(project["id"], complete_annotations(project, annotations), 0)

    def test_all_scene_options_save_export_and_reopen(self):
        project = self.service.open_path(str(self.source))
        labels = ["室内", "室外", "车内", "其他"]
        annotations = empty_annotations()
        annotations["scene"] = [{"id": f"scene-{i}", "label": label, "start_ms": i * 750, "end_ms": (i + 1) * 750} for i, label in enumerate(labels)]
        saved = self.service.update_draft(project["id"], complete_annotations(project, annotations), 0)
        _, documents = self.service.export_documents(self.service.load(project["id"]))
        exported = json.loads(documents["scene"])
        self.assertEqual([s["label_id"] for s in exported["segments"]], ["indoor", "outdoor", "in_vehicle", "other"])
        self.service.writeback(project["id"])
        fresh = self.service.create(list(self.fpv.glob("*.mp4")), "Round trip", self.source)
        restored, _ = self.service.read_external(fresh)
        self.assertEqual(restored["scene"], saved["annotations"]["scene"])

    def camera_timing_fixture(self, rows, folder="camera-timing"):
        source = self.root / folder
        fpv = source / "FPV"
        fpv.mkdir(parents=True)
        durations = {}
        paths = []
        for filename, duration in rows:
            path = fpv / filename
            path.write_bytes(b"synthetic camera source " + filename.encode())
            paths.append(path)
            durations[filename] = duration
        def probe(path):
            return {"duration_ms": durations[path.name], "codec": "mjpeg", "pixel_format": "yuvj420p", "audio_codecs": []}
        return source, paths, probe

    def test_camera_alignment_preserves_full_duration_gap_export_and_conflicts(self):
        rows = [
            ("A09999_20260911131135_0000.avi", 600533),
            ("A09999_20260911132135_0001.avi", 600533),
            ("A09999_20260911140000_0002.avi", 1000),
        ]
        source, paths, probe = self.camera_timing_fixture(rows)
        original_bytes = [path.read_bytes() for path in paths]
        with patch.object(ProjectService, "probe", side_effect=probe):
            project = self.service.open_path(str(source))
            first, second, third = project["videos"]
            self.assertEqual([video["duration_ms"] for video in project["videos"]], [600533, 600533, 1000])
            self.assertTrue(all(video["end_ms"] - video["start_ms"] == video["duration_ms"] for video in project["videos"]))
            self.assertEqual((second["filename_start_ms"], second["start_ms"], second["alignment_offset_ms"]), (600000, 600533, 533))
            self.assertEqual(second["filename_recording_start"], "2026-09-11T13:21:35.000+08:00")
            self.assertEqual(second["recording_start"], "2026-09-11T13:21:35.533+08:00")
            self.assertEqual(project["gaps"], [{"start_ms": 1201066, "end_ms": 2905000}])
            self.assertEqual(third["start_ms"], 2905000)
            for video in (first, third):
                self.assertNotIn("alignment_offset_ms", video)
                self.assertNotIn("filename_recording_start", video)
                self.assertNotIn("filename_start_ms", video)
            stored = self.service.load(project["id"])
            self.assertEqual(stored["_sources"][second["id"]]["duration_ms"], 600533)
            self.assertEqual(stored["_sources"][second["id"]]["path"], str(paths[1].resolve()))
            annotations = empty_annotations()
            point_time = second["start_ms"] + 123
            annotations["habit"] = [{"id": "aligned-event", "label": "喝水", "kind": "point", "start_ms": point_time, "end_ms": point_time}]
            updated = self.service.update_draft(project["id"], complete_annotations(project, annotations), project["revision"])
            self.service.writeback(project["id"])
            for axis in AXES:
                document = json.loads((source / "timeline" / FILENAMES[axis]).read_bytes())
                policy = document["timebase"]["alignment_policy"]
                self.assertEqual(policy["name"], "adjacent_camera_second_precision_v2")
                self.assertEqual(policy["maximum_raw_overlap_inclusive_ms"], 3000)
                self.assertEqual(policy["maximum_cumulative_offset_inclusive_ms"], 3000)
                self.assertEqual(policy["maximum_nominal_gap_exclusive_ms"], 1000)
                self.assertTrue(policy["preserves_full_video_duration"])
                mapping = document["timebase"]["videos"][1]
                self.assertEqual(mapping["alignment_offset_ms"], 533)
                self.assertEqual(mapping["filename_start_ms"], 600000)
                self.assertEqual(mapping["filename_recording_start"], second["filename_recording_start"])
                self.assertNotIn("alignment_offset_ms", document["timebase"]["videos"][0])
                if axis == "habit":
                    self.assertEqual(document["segments"][0]["start_ms"] - mapping["start_ms"], 123)
                    self.assertEqual(document["segments"][0]["start_time"], "132135")
            reloaded = ProjectService(self.root / "aligned-reload-runtime").open_path(str(source))
            self.assertEqual(reloaded["annotations"], updated["annotations"])
            self.assertEqual(reloaded["source_fingerprint"], project["source_fingerprint"])
            self.assertIn("533", self.service.load(project["id"])["warnings"][0])
            local = json.loads(json.dumps(updated["annotations"]))
            local["habit"][0]["label"] = "咳嗽"
            changed = self.service.update_draft(project["id"], local, updated["revision"])
            external = source / "timeline" / FILENAMES["scene"]
            external.write_bytes(external.read_bytes() + b"\n")
            reopened = self.service.open_path(str(source))
            self.assertEqual(reopened["annotations"], changed["annotations"])
            self.assertTrue(any("最大累计偏移 533" in warning for warning in reopened["warnings"]))
            self.assertTrue(any("原目录 JSON 已被修改" in warning for warning in reopened["warnings"]))
            with self.assertRaises(HTTPException) as context:
                self.service.writeback(project["id"])
            self.assertEqual(context.exception.status_code, 409)
        self.assertEqual([path.read_bytes() for path in paths], original_bytes)

    def test_camera_seam_public_update_export_and_legacy_json_keep_coordinates(self):
        rows = [
            ("A09999_20260911183100_0023.avi", 599733),
            ("A09999_20260911184100_0024.avi", 599767),
            ("A09999_20260911185200_0025.avi", 1000),
        ]
        source, paths, probe = self.camera_timing_fixture(rows)
        with patch.object(ProjectService, "probe", side_effect=probe):
            legacy = self.service.create(paths, "legacy seams", source)
            original_fingerprint = legacy["source_fingerprint"]
            original_videos = json.loads(json.dumps(legacy["videos"]))
            old_annotations = empty_annotations()
            old_annotations["scene"] = [
                {"id": "left", "label": "室内", "kind": "interval", "start_ms": 0, "end_ms": 599733},
                {"id": "right", "label": "室内", "kind": "interval", "start_ms": 601146, "end_ms": 1199767},
                {"id": "after-real-gap", "label": "室内", "kind": "interval", "start_ms": 1260000, "end_ms": 1261000},
            ]
            old_annotations["habit"] = [{"id": "point", "label": "喝水", "kind": "point", "start_ms": 601500, "end_ms": 601500}]
            complete = complete_annotations(legacy, old_annotations)
            old_annotations["posture"] = complete["posture"]
            old_annotations["category"] = complete["category"]
            legacy["annotations"] = old_annotations
            self.service.save(legacy)
            before = self.service.load(legacy["id"])
            public = self.service.public(before)
            self.assertEqual(self.service.load(legacy["id"]), before)
            self.assertEqual(public["revision"], 0)
            self.assertEqual(public["videos"], original_videos)
            self.assertEqual(public["source_fingerprint"], original_fingerprint)
            self.assertEqual(public["gaps"], [{"start_ms": 1199767, "end_ms": 1260000}])
            self.assertEqual(len(public["recording_runs"]), 2)
            self.assertEqual(len(public["annotations"]["scene"]), 2)
            self.assertEqual(public["annotations"]["habit"], old_annotations["habit"])
            _, documents = self.service.export_documents(before)
            exported = json.loads(documents["scene"])
            self.assertEqual(exported["timebase"]["gaps"], public["gaps"])
            self.assertEqual(exported["timebase"]["continuity_bridges"], public["continuity_bridges"])
            policy = exported["timebase"]["continuity_policy"]
            self.assertEqual(policy["name"], "adjacent_camera_second_precision_v2")
            self.assertEqual(policy["maximum_seam_exclusive_ms"], 1000)
            self.assertEqual(policy["maximum_raw_boundary_error_exclusive_ms"], 1000)
            self.assertEqual(policy["maximum_alignment_offset_inclusive_ms"], 3000)
            self.assertEqual(exported["timebase"]["source_fingerprint"], original_fingerprint)
            self.assertEqual(exported["segments"][0]["end_ms"], 1199767)
            self.assertEqual(json.loads(documents["habit"])["segments"][0]["start_ms"], 601500)
            # A pre-upgrade export has the original false gap and split records.
            # Its unchanged fingerprint/timebase remains readable, without retiming.
            for axis in AXES:
                document = json.loads(documents[axis])
                for key in ("recording_runs", "continuity_bridges", "continuity_policy"):
                    document["timebase"].pop(key, None)
                document["timebase"]["gaps"] = legacy["gaps"]
                if axis == "scene":
                    document["segments"] = [{**item, "label_id": "indoor"} for item in old_annotations[axis]]
                (source / FILENAMES[axis]).write_text(json.dumps(document), encoding="utf-8")
            reloaded = ProjectService(self.root / "legacy-reader").open_path(str(source))
            self.assertEqual(reloaded["annotations"], public["annotations"])
            self.assertEqual(reloaded["source_fingerprint"], original_fingerprint)
            # An old open window can still save; server normalization prevents
            # putting the same false state split back into the stored draft.
            updated = self.service.update_draft(legacy["id"], old_annotations, 0)
            self.assertEqual(updated["annotations"], public["annotations"])
            self.assertEqual(self.service.load(legacy["id"])["annotations"], public["annotations"])
            self.assertEqual(updated["revision"], 1)
            with self.assertRaises(HTTPException) as context:
                self.service.update_draft(legacy["id"], old_annotations, 0)
            self.assertEqual(context.exception.status_code, 409)

    def test_camera_alignment_chain_accepts_traced_1033_ms_cumulative_offset(self):
        rows = [
            ("A09999_20260911131135_0000.avi", 600533),
            ("A09999_20260911132135_0001.avi", 599767),
            ("A09999_20260911133135_0002.avi", 600733),
            ("A09999_20260911134135_0003.avi", 1000),
        ]
        source, paths, probe = self.camera_timing_fixture(rows)
        with patch.object(ProjectService, "probe", side_effect=probe):
            project = self.service.create(paths, "chain", source)
        self.assertEqual([video.get("alignment_offset_ms", 0) for video in project["videos"]], [0, 533, 300, 1033])
        self.assertEqual([video["start_ms"] for video in project["videos"]], [0, 600533, 1200300, 1801033])
        self.assertEqual([video["duration_ms"] for video in project["videos"]], [row[1] for row in rows])
        self.assertEqual(project["gaps"], [])
        self.assertEqual(project["duration_ms"], sum(row[1] for row in rows))
        self.assertIn("3 处", project["warnings"][0])
        self.assertIn("最大累计偏移 1033", project["warnings"][0])
        _, documents = self.service.export_documents(complete_project(project))
        policy = json.loads(documents["habit"])["timebase"]["alignment_policy"]
        self.assertEqual(policy["maximum_applied_offset_ms"], 1033)
        self.assertEqual(policy["shifted_video_count"], 3)

    def test_camera_alignment_rejects_unqualified_and_large_original_overlaps(self):
        first = "A09999_20260911131135_0000.avi"
        cases = [
            ("skipped-index", first, "A09999_20260911131136_0002.avi", 1500),
            ("other-camera", first, "B09999_20260911131136_0001.avi", 1500),
            ("explicit-milliseconds", "A09999_20260911131135000_0000.avi", "A09999_20260911131136000_0001.avi", 1500),
            ("same-nominal-time", first, "A09999_20260911131135_0001.avi", 500),
            ("original-overlap-over-limit", first, "A09999_20260911131136_0001.avi", 4001),
            ("original-overlap-four-seconds", first, "A09999_20260911131136_0001.avi", 5000),
        ]
        for case, first_name, second_name, duration in cases:
            with self.subTest(case=case):
                source, paths, probe = self.camera_timing_fixture([(first_name, duration), (second_name, 1000)], folder=case)
                with patch.object(ProjectService, "probe", side_effect=probe):
                    with self.assertRaises(HTTPException) as context:
                        self.service.create(paths, case, source)
                self.assertEqual(context.exception.status_code, 422)

    def test_camera_alignment_accepts_1500_and_inclusive_3000_ms_overlap(self):
        for overlap in (1000, 1500, 2000, 2999, 3000):
            with self.subTest(overlap=overlap):
                rows = [
                    ("A09999_20260915175325_0016.avi", 600000 + overlap),
                    ("A09999_20260915180325_0017.avi", 600000),
                ]
                source, paths, probe = self.camera_timing_fixture(rows, folder=f"overlap-{overlap}")
                with patch.object(ProjectService, "probe", side_effect=probe):
                    project = self.service.create(paths, "inclusive overlap", source)
                first, second = project["videos"]
                self.assertEqual(second["start_ms"], first["end_ms"])
                self.assertEqual(second["alignment_offset_ms"], overlap)
                self.assertEqual(second["filename_start_ms"], 600000)
                self.assertEqual(second["filename_recording_start"], "2026-09-15T18:03:25.000+08:00")
                self.assertEqual(project["duration_ms"], sum(row[1] for row in rows))
                self.assertEqual(project["gaps"], [])
                self.assertIn("不超过 3000 毫秒", project["warnings"][0])
                self.assertTrue(all(v["end_ms"] - v["start_ms"] == v["duration_ms"] for v in project["videos"]))
                _, documents = self.service.export_documents(complete_project(project))
                for axis in AXES:
                    timebase = json.loads(documents[axis])["timebase"]
                    self.assertEqual(timebase["videos"][1]["alignment_offset_ms"], overlap)
                    self.assertEqual(timebase["alignment_policy"]["maximum_applied_offset_ms"], overlap)

    def test_camera_alignment_cumulative_limit_includes_3000_but_rejects_3001(self):
        for cumulative in (2000, 2999, 3000, 3001):
            with self.subTest(cumulative=cumulative):
                rows = [
                    ("A09999_20260911131135_0000.avi", 2000),
                    ("A09999_20260911131136_0001.avi", 2000),
                    ("A09999_20260911131137_0002.avi", cumulative - 1000),
                    ("A09999_20260911131138_0003.avi", 1000),
                ]
                source, paths, probe = self.camera_timing_fixture(rows, folder=f"cumulative-{cumulative}")
                with patch.object(ProjectService, "probe", side_effect=probe):
                    if cumulative > 3000:
                        with self.assertRaises(HTTPException) as context:
                            self.service.create(paths, "cumulative limit", source)
                        self.assertIn("重叠 3001 毫秒", context.exception.detail)
                        self.assertIn("原始边界误差 1001 毫秒", context.exception.detail)
                        self.assertIn("累计偏移不超过 3000 毫秒", context.exception.detail)
                    else:
                        project = self.service.create(paths, "cumulative limit", source)
                        self.assertEqual([v.get("alignment_offset_ms", 0) for v in project["videos"]], [0, 1000, 2000, cumulative])
                        self.assertEqual(project["duration_ms"], sum(row[1] for row in rows))
                        self.assertEqual(project["gaps"], [])

    def test_camera_alignment_does_not_consume_one_second_nominal_gap(self):
        rows = [
            ("A09999_20260911131135_0000.avi", 1900),
            ("A09999_20260911131136_0001.avi", 1900),
            ("A09999_20260911131137_0002.avi", 1000),
            ("A09999_20260911131139_0003.avi", 1000),
        ]
        source, paths, probe = self.camera_timing_fixture(rows)
        with patch.object(ProjectService, "probe", side_effect=probe):
            with self.assertRaises(HTTPException) as context:
                self.service.create(paths, "gap guard", source)
        self.assertIn("原始边界误差 -1000 毫秒", context.exception.detail)

    def test_unshifted_camera_project_retains_existing_fingerprint_and_export(self):
        rows = [("A09999_20260911131135_0000.avi", 2000), ("A09999_20260911131137_0001.avi", 2000)]
        _, paths, probe = self.camera_timing_fixture(rows)
        with patch.object(ProjectService, "probe", side_effect=probe), patch.object(ProjectService, "source_stamp", return_value={"size": 123, "mtime_ns": 456, "sample_sha256": "fixture"}):
            project = self.service.create(paths, "legacy fingerprint", None)
        # Recorded from the prior strict implementation before adding alignment.
        self.assertEqual(project["source_fingerprint"], "2acb48ec6a46dfd2ccee72f2d8a46f2832ab196aa80f3b700785f01941fbee61")
        self.assertEqual(project["warnings"], [])
        self.assertTrue(all("alignment_offset_ms" not in video and "filename_start_ms" not in video and "filename_recording_start" not in video for video in project["videos"]))
        self.service.save(project)
        reloaded = ProjectService(self.root).load(project["id"])
        self.assertEqual(reloaded["source_fingerprint"], project["source_fingerprint"])
        _, documents = self.service.export_documents(complete_project(reloaded))
        timebase = json.loads(documents["scene"])["timebase"]
        self.assertNotIn("alignment_policy", timebase)
        self.assertTrue(all("alignment_offset_ms" not in video for video in timebase["videos"]))

    def test_recording_order_persistence_and_revision_conflict(self):
        project = self.annotated_project()
        self.assertEqual([v["name"] for v in project["videos"]], ["clip1_20260914000000.mp4", "clip2_20260914000001.mp4", "clip10_20260914000002.mp4"])
        self.assertEqual([v["start_ms"] for v in project["videos"]], [0, 1000, 2000])
        reopened = ProjectService(self.root).load(project["id"])
        self.assertEqual(reopened["annotations"], project["annotations"])
        with self.assertRaises(HTTPException) as context:
            self.service.update_draft(project["id"], empty_annotations(), 0)
        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(self.service.open_path(str(self.fpv))["id"], project["id"])

    def test_ten_second_recording_gap_preserved_and_overlap_rejected(self):
        paths = [self.fpv / "part1_20260914120000.mp4", self.fpv / "part2_20260914120011.mp4"]
        for path in paths:
            path.write_bytes(b"synthetic gap source")
        project = self.service.create(paths, "gapped", self.source)
        self.assertEqual(project["gaps"], [{"start_ms": 1000, "end_ms": 11000}])
        self.assertEqual(project["duration_ms"], 12000)
        self.assertEqual(project["videos"][1]["start_ms"], 11000)
        self.assertEqual(project["recording_start"], "2026-09-14T12:00:00.000+08:00")
        _, documents = self.service.export_documents(complete_project(project))
        timebase = json.loads(documents["habit"])["timebase"]
        self.assertEqual(timebase["origin"], "recording_datetime")
        self.assertEqual(timebase["gaps"], project["gaps"])
        overlap = self.fpv / "part2_20260914120000500.mp4"
        overlap.write_bytes(b"synthetic overlap source")
        with self.assertRaises(HTTPException) as context:
            self.service.create([paths[0], overlap], "overlap", self.source)
        self.assertIn("重叠", context.exception.detail)

    def test_export_zip_three_files_common_timebase_and_batch(self):
        project = self.annotated_project()
        with zipfile.ZipFile(io.BytesIO(self.service.export_zip(project["id"]))) as archive:
            self.assertEqual(set(archive.namelist()), set(FILENAMES.values()))
            docs = [json.loads(archive.read(FILENAMES[axis])) for axis in AXES]
        self.assertEqual(len({doc["save_id"] for doc in docs}), 1)
        self.assertTrue(all(doc["timebase"] == docs[0]["timebase"] for doc in docs))
        self.assertEqual(docs[3]["segments"][0]["label"], "吃饭")
        self.assertEqual(docs[0]["timebase"]["videos"][2]["relative_path"], "FPV/clip10_20260914000002.mp4")
        self.assertTrue(all("label_id" in item for doc in docs for item in doc["segments"]))

    def test_clock_export_all_axes_user_example_and_gap_roundtrip(self):
        source = self.root / "clock-gap-collection"
        fpv = source / "FPV"
        fpv.mkdir(parents=True)
        for filename in ("camera_20260914150445250.mp4", "camera_20260914150456250.mp4"):
            (fpv / filename).write_bytes(b"clock gap fixture")
        project = self.service.open_path(str(source))
        self.assertEqual(project["gaps"], [{"start_ms": 1000, "end_ms": 11000}])
        annotations = empty_annotations()
        annotations["scene"] = [{"id": "scene-clock", "label": "室内", "start_ms": 11250, "end_ms": 11999}]
        annotations["posture"] = [{"id": "posture-clock", "label": "坐", "start_ms": 11250, "end_ms": 11999}]
        annotations["category"] = [{"id": "category-clock", "label": "专注", "start_ms": 11250, "end_ms": 11999}]
        annotations["habit"] = [{"id": "drink-water", "label": "喝水", "kind": "point", "start_ms": 11250, "end_ms": 11250}]
        updated = self.service.update_draft(project["id"], complete_annotations(project, annotations), 0)
        self.service.writeback(project["id"])
        for axis in AXES:
            with self.subTest(axis=axis):
                document = json.loads((source / "timeline" / FILENAMES[axis]).read_bytes())
                self.assertEqual(document["timebase"]["real_time_format"], "HHMMSS")
                self.assertEqual(document["timebase"]["timezone"], "Asia/Shanghai")
                self.assertEqual(document["timebase"]["gaps"], project["gaps"])
                segment = next(item for item in document["segments"] if item["start_ms"] == 11250)
                # The 10-second recording gap remains part of elapsed time: this
                # must be 15:04:56, not 15:04:46 after compacting video durations.
                self.assertEqual(segment["start_time"], "150456")
                self.assertEqual(segment["start_date"], "2026-09-14")
                self.assertEqual(segment["start_ms"], 11250)
                self.assertEqual(segment["end_date"], "2026-09-14")
                if axis == "habit":
                    self.assertEqual(segment["label"], "喝水")
                    self.assertEqual(segment["end_time"], "150456")
                    self.assertEqual(segment["end_ms"], 11250)
                    self.assertEqual(segment["kind"], "point")
                else:
                    self.assertEqual(segment["end_time"], "150457")
                    self.assertEqual(segment["end_ms"], 11999)
        imported = ProjectService(self.root / "clock-roundtrip-runtime").open_path(str(source))
        self.assertEqual(imported["annotations"], updated["annotations"])
        _, exported_again = self.service.export_documents(imported)
        point = json.loads(exported_again["habit"])["segments"][0]
        self.assertEqual((point["start_time"], point["end_time"]), ("150456", "150456"))

    def test_clock_export_leading_zero_and_midnight_fractional_truncation(self):
        cases = [
            ("20260914040506500", "040506", "040507", "2026-09-14"),
            ("20260914235959500", "235959", "000000", "2026-09-15"),
        ]
        for timestamp, expected_start, expected_end, expected_end_date in cases:
            with self.subTest(timestamp=timestamp):
                path = self.root / ("camera_" + timestamp + ".mp4")
                path.write_bytes(b"fractional clock fixture")
                project = self.service.create([path], "fractional clock", None)
                project["annotations"]["habit"] = [{"id": "fraction", "label": "喝水", "start_ms": 499, "end_ms": 999}]
                _, documents = self.service.export_documents(complete_project(project))
                segment = json.loads(documents["habit"])["segments"][0]
                # Start is xx:xx:xx.999: HHMMSS truncates subsecond precision;
                # original millisecond offsets remain available without loss.
                self.assertEqual(segment["start_time"], expected_start)
                self.assertEqual(segment["end_time"], expected_end)
                self.assertEqual(segment["start_date"], "2026-09-14")
                self.assertEqual(segment["end_date"], expected_end_date)
                self.assertEqual((segment["start_ms"], segment["end_ms"]), (499, 999))
                self.assertRegex(segment["start_time"], r"^\d{6}$")
                self.assertRegex(segment["end_time"], r"^\d{6}$")

    def test_clock_export_point_equal_times_after_midnight(self):
        path = self.root / "camera_20260914235959500.mp4"
        path.write_bytes(b"midnight point fixture")
        project = self.service.create([path], "midnight point", None)
        project["annotations"]["habit"] = [{"id": "midnight-point", "label": "咳嗽", "kind": "point", "start_ms": 999, "end_ms": 999}]
        _, documents = self.service.export_documents(complete_project(project))
        segment = json.loads(documents["habit"])["segments"][0]
        self.assertEqual((segment["start_time"], segment["end_time"]), ("000000", "000000"))
        self.assertEqual((segment["start_date"], segment["end_date"]), ("2026-09-15", "2026-09-15"))
        self.assertEqual((segment["start_ms"], segment["end_ms"]), (999, 999))
        self.assertEqual(segment["kind"], "point")

    def test_writeback_reload_backups_and_external_conflict(self):
        project = self.annotated_project()
        first = self.service.writeback(project["id"])
        for path in first["paths"]:
            self.assertTrue(Path(path).is_file())
        self.assertFalse(self.service.load(project["id"])["draft_dirty"])
        second = self.service.writeback(project["id"])
        self.assertTrue((self.source / ".annotation-backups" / second["save_id"] / FILENAMES["scene"]).is_file())
        # A new local database imports the original results without attaching any
        # local IDs to the source identity fingerprint.
        other = ProjectService(self.root / "another-local-runtime")
        imported = other.open_path(str(self.source))
        self.assertEqual(imported["annotations"], project["annotations"])
        path = self.source / "timeline" / FILENAMES["scene"]
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(HTTPException) as context:
            self.service.writeback(project["id"])
        self.assertEqual(context.exception.status_code, 409)

    def test_point_and_interval_export_roundtrip(self):
        project = self.annotated_project()
        annotations = project["annotations"]
        annotations["habit"].extend([
            {"id": "point-a", "label": "咳嗽", "kind": "point", "start_ms": 500, "end_ms": 500},
            {"id": "point-end", "label": "眨眼", "kind": "point", "start_ms": 3000, "end_ms": 3000},
        ])
        updated = self.service.update_draft(project["id"], complete_annotations(project, annotations), project["revision"])
        self.service.writeback(project["id"])
        document = json.loads((self.source / "timeline" / FILENAMES["habit"]).read_bytes())
        point = next(item for item in document["segments"] if item["id"] == "point-end")
        self.assertEqual((point["kind"], point["start_ms"], point["end_ms"]), ("point", 3000, 3000))
        self.assertEqual([item["start_ms"] for item in document["segments"]], sorted(item["start_ms"] for item in document["segments"]))
        imported = ProjectService(self.root / "points-runtime").open_path(str(self.source))
        self.assertEqual(imported["annotations"], updated["annotations"])

    def test_source_change_rejected_without_altering_draft(self):
        project = self.annotated_project()
        (self.fpv / "clip2_20260914000001.mp4").write_bytes(b"changed")
        with self.assertRaises(HTTPException) as context:
            self.service.open_path(str(self.source))
        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(self.service.load(project["id"])["annotations"], project["annotations"])

    def test_partial_write_failure_restores_all_originals(self):
        project = self.annotated_project()
        self.service.writeback(project["id"])
        before = {axis: (self.source / "timeline" / FILENAMES[axis]).read_bytes() for axis in AXES}
        original_replace = os.replace
        count = 0
        def failing_replace(source, target):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated network failure")
            return original_replace(source, target)
        with patch("backend.service.os.replace", side_effect=failing_replace):
            with self.assertRaises(HTTPException) as context:
                self.service.writeback(project["id"])
        self.assertEqual(context.exception.status_code, 500)
        self.assertEqual(before, {axis: (self.source / "timeline" / FILENAMES[axis]).read_bytes() for axis in AXES})

    def test_mismatched_export_batch_and_fingerprint_rejected(self):
        project = self.annotated_project()
        self.service.writeback(project["id"])
        path = self.source / "timeline" / FILENAMES["scene"]
        doc = json.loads(path.read_bytes())
        doc["save_id"] = "different-batch"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(HTTPException):
            self.service.open_path(str(self.source))
        doc["timebase"]["source_fingerprint"] = "wrong-source"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(HTTPException) as context:
            ProjectService(self.root / "fresh-runtime").open_path(str(self.source))
        self.assertEqual(context.exception.status_code, 409)

    def test_api_path_files_range_origin_guard_and_validation(self):
        first = self.fpv / "clip2_20260914000001.mp4"
        first.write_bytes(b"abcdefghij")
        last = self.fpv / "clip10_20260914000002.mp4"
        last.write_bytes(b"1234567890")
        with TestClient(create_app(self.root / "http-runtime"), base_url="http://127.0.0.1") as client:
            response = client.post("/api/projects/files", json={"paths": [str(last), str(first)]})
            self.assertEqual(response.status_code, 200, response.text)
            project = response.json()
            self.assertEqual(project["videos"][0]["name"], first.name)
            ranged = client.get(project["videos"][0]["url"], headers={"Range": "bytes=2-5"})
            self.assertEqual(ranged.status_code, 206)
            self.assertEqual(ranged.content, b"cdef")
            blocked = client.put(f'/api/projects/{project["id"]}/draft', json={"annotations": empty_annotations(), "expected_revision": 0}, headers={"Origin": "https://outside.example"})
            self.assertEqual(blocked.status_code, 403)
            self.assertEqual(client.post("/api/projects/files", json={"paths": []}).status_code, 422)
            self.assertEqual(client.post("/api/projects/upload", files=[("files", (first.name, b"1", "video/mp4"))]).status_code, 410)
            self.assertEqual(client.get("/api/does-not-exist").status_code, 404)
            self.assertEqual(first.read_bytes(), b"abcdefghij")


if __name__ == "__main__":
    unittest.main()
