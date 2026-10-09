"""The offline Ubuntu move keeps identities and refuses changed originals."""

import json
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.auth import AuthStore
from backend.service import ProjectService, empty_annotations, now
from deploy.migrate_projects import apply_bundle, current_source_fingerprint, export_bundle, load_mappings, map_source, sha256_file


class ProjectMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="datamark-move-")
        self.root = Path(self.temp.name)
        self.old = self.root / "old"
        self.new = self.root / "new"
        self.old.mkdir()
        self.new.mkdir()
        self.video = self.old / "CAM_20260930120000_0000.mp4"
        self.video.write_bytes(b"fixture-video-content")
        self.service_root = self.root / "source-install"
        service = ProjectService(self.service_root, enable_remote=False)
        stamp = service.source_stamp(self.video)
        self.project_id = "a" * 32
        project = {"id": self.project_id, "name": "原有项目", "updated_at": now(),
                   "source_dir": str(self.old), "_source_key": str(self.old),
                   "_storage_version": 3, "source_fingerprint": "b" * 64,
                   "videos": [{"id": "v0001", "name": self.video.name, "relative_path": self.video.name,
                               "duration_ms": 1000, "start_ms": 0, "end_ms": 1000}],
                   "_sources": {"v0001": {"path": str(self.video), "stamp": stamp, "duration_ms": 1000}},
                   "annotations": {**empty_annotations(), "habit": [
                       {"id": "h1", "label": "喝水", "kind": "point", "start_ms": 100, "end_ms": 100,
                        "created_by": "existing-user"}]},
                   "_external_hashes": {axis: None for axis in ("scene", "posture", "category", "habit")}}
        service.save(project)
        self.auth = AuthStore(self.service_root)
        self.admin = self.auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
        self.auth.login("admin", "correct horse battery staple", "local")
        self.bundle = self.root / "bundle"
        self.mapping = self.root / "mappings.json"
        self.mapping.write_text(json.dumps([{"from": str(self.old), "to": str(self.new)}]))

    def tearDown(self):
        self.temp.cleanup()

    def test_verified_move_preserves_draft_accounts_and_project_id(self):
        self.assertEqual(export_bundle(self.service_root / ".local", self.bundle), {"projects": 1, "videos": 1, "needs_admin": False})
        shutil.copy2(self.video, self.new / self.video.name)
        target = self.root / "ubuntu-state"
        self.assertEqual(apply_bundle(self.bundle, target, self.mapping), {"projects": 1, "videos": 1, "needs_admin": False})
        moved = ProjectService(self.root / "ubuntu-app", enable_remote=False)
        # Load the transferred database without modifying the original fixture.
        moved.sessions.close()
        shutil.copy2(target / "annotations.sqlite3", moved.db_path)
        project = moved.load(self.project_id)
        self.assertEqual(project["_sources"]["v0001"]["path"], str((self.new / self.video.name).resolve()))
        self.assertEqual(project["annotations"]["habit"][0]["created_by"], "existing-user")
        self.assertEqual(project["source_fingerprint"], "b" * 64)
        self.assertTrue(project["_source_fingerprint_aliases"])
        self.assertEqual(current_source_fingerprint(project), ProjectService.current_source_fingerprint(project))
        imported = AuthStore(self.root / "ubuntu-auth")
        shutil.copy2(target / "auth.sqlite3", imported.path)
        self.assertTrue(imported.has_admin())
        with imported.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_changed_source_keeps_destination_empty(self):
        export_bundle(self.service_root / ".local", self.bundle)
        (self.new / self.video.name).write_bytes(b"changed-video")
        target = self.root / "ubuntu-state"
        with self.assertRaisesRegex(ValueError, "视频"):
            apply_bundle(self.bundle, target, self.mapping)
        self.assertFalse(target.exists())

    def test_custom_axis_and_its_written_file_survive_move(self):
        axis = "custom_" + "c" * 32
        service = ProjectService(self.service_root, enable_remote=False)
        project = service.load(self.project_id)
        project["custom_tracks"] = [{"id": axis, "name": "环境", "mode": "state", "labels": ["安静"]}]
        project["annotations"][axis] = []
        old_timeline = self.old / "timeline"
        new_timeline = self.new / "timeline"
        old_timeline.mkdir()
        new_timeline.mkdir()
        for fixed_axis in ("scene", "posture", "category", "habit"):
            fixed_name = f"{fixed_axis}.timeline.json"
            (old_timeline / fixed_name).write_text('{"sample":"fixed-axis"}', encoding="utf-8")
            project["_external_hashes"][fixed_axis] = sha256_file(old_timeline / fixed_name)
        filename = f"{axis}.timeline.json"
        (old_timeline / filename).write_text('{"sample":"custom-axis"}', encoding="utf-8")
        project["_external_hashes"][axis] = sha256_file(old_timeline / filename)
        service.save(project)
        export_bundle(self.service_root / ".local", self.bundle)
        shutil.copy2(self.video, self.new / self.video.name)
        for original in old_timeline.iterdir():
            shutil.copy2(original, new_timeline / original.name)
        target = self.root / "ubuntu-custom-state"
        apply_bundle(self.bundle, target, self.mapping)
        with sqlite3.connect(target / "annotations.sqlite3") as con:
            moved = json.loads(con.execute("SELECT document FROM projects WHERE id=?", (self.project_id,)).fetchone()[0])
        self.assertEqual(moved["custom_tracks"], project["custom_tracks"])
        self.assertEqual(moved["_external_hashes"][axis], project["_external_hashes"][axis])

    def test_legacy_install_without_accounts_moves_drafts_and_requires_admin_setup(self):
        (self.service_root / ".local" / "auth.sqlite3").unlink()
        self.assertTrue(export_bundle(self.service_root / ".local", self.bundle)["needs_admin"])
        shutil.copy2(self.video, self.new / self.video.name)
        target = self.root / "ubuntu-state"
        self.assertTrue(apply_bundle(self.bundle, target, self.mapping)["needs_admin"])
        self.assertTrue(target.joinpath("annotations.sqlite3").is_file())
        deployed = self.root / "deployed"
        (deployed / ".local").mkdir(parents=True)
        shutil.copy2(target / "auth.sqlite3", deployed / ".local" / "auth.sqlite3")
        auth = AuthStore(deployed)
        self.assertFalse(auth.has_admin())
        auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
        self.assertTrue(auth.has_admin())

    def test_windows_unc_mapping_is_case_insensitive_and_confined(self):
        shutil.copy2(self.video, self.new / self.video.name)
        self.mapping.write_text(json.dumps([{"from": r"\\Relty\homes", "to": str(self.new)}]))
        mappings = load_mappings(self.mapping)
        self.assertEqual(map_source(r"\\relty\HOMES\CAM_20260930120000_0000.mp4", mappings),
                         (self.new / self.video.name).resolve())
        with self.assertRaisesRegex(ValueError, "映射"):
            map_source(r"\\relty\datasets\CAM_20260930120000_0000.mp4", mappings)


if __name__ == "__main__":
    unittest.main()
