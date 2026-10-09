"""Names remain metadata across imports, edits, migrations and concurrent clients."""
from __future__ import annotations

import copy
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import test_service
from backend.app import create_app
from backend.project_storage import import_name
from backend.service import ProjectService, validate_project_name
from backend.testing_annotations import complete_project


class ProjectNameValidationTests(unittest.TestCase):
    def test_unicode_length_trim_and_filesystem_punctuation(self):
        name = "采集 / 日期:参与者\\样本*?<>|" + chr(34) + "😀"
        self.assertEqual(validate_project_name("  " + name + "　"), name)
        self.assertEqual(validate_project_name("😀" * 80), "😀" * 80)
        with self.assertRaises(HTTPException):
            validate_project_name("😀" * 81)
        self.assertIsNone(validate_project_name("　 ", allow_empty=True))
        self.assertIsNone(validate_project_name(None, allow_empty=True))

    def test_blank_controls_and_invalid_types_are_rejected_for_rename(self):
        for value in (None, "", "  ", 1, True, [], "first\nsecond", "line\n", "\tname", "a\x00b", "a\x7fb", "a\x85b", chr(0xD800)):
            with self.subTest(value=repr(value)), self.assertRaises(HTTPException) as error:
                validate_project_name(value)
            self.assertEqual(error.exception.status_code, 422)
        for value in ("\n", "\t", " a\n", "x" * 81):
            with self.subTest(import_name=repr(value)), self.assertRaises(HTTPException):
                validate_project_name(value, allow_empty=True)


class ProjectNamingTests(unittest.TestCase):
    setUp = test_service.PersistenceTests.setUp
    tearDown = test_service.PersistenceTests.tearDown

    def test_internal_create_still_uses_import_time_not_legacy_positional_name(self):
        project = self.service.create(list(self.fpv.glob("*.mp4")), "Directory label must not become name", self.source,
                                      imported_at="2026-09-14T12:32:00+00:00")
        self.assertEqual(project["name"], "0914-20-32")
        self.assertNotIn("_custom_name", project)

    def test_directory_default_uses_selected_folder_and_survives_migration(self):
        project = self.service.open_path(str(self.source), name="　 ")
        self.assertEqual(project["name"], self.source.name)
        self.assertTrue(self.service.load(project["id"])["_custom_name"])
        stored = self.service.load(project["id"])
        stored["_storage_version"] = 0
        self.service.save(stored)
        with patch.object(self.service, "attach_source_caches"), patch.object(self.service, "cleanup_relinked_sources"):
            self.service.migrate_legacy_projects()
        self.assertEqual(self.service.load(project["id"])["name"], self.source.name)

    def test_fpv_directory_default_uses_selected_fpv_folder(self):
        project = self.service.open_path(str(self.fpv))
        self.assertEqual(project["name"], "FPV")
        self.assertEqual(self.service.open_path(str(self.source))["name"], "FPV")

    def test_directory_and_files_import_trim_explicit_names_and_default_blank(self):
        directory = self.service.open_path(str(self.source), name="  命名目录项目  ")
        self.assertEqual(directory["name"], "命名目录项目")
        paths = [str(path) for path in self.fpv.glob("*.mp4")]
        files = self.service.open_files(paths, name=" 文件选择 / 名称:😀 ")
        self.assertEqual(files["name"], "文件选择 / 名称:😀")
        default = self.service.open_files(paths, name="　 ")
        self.assertEqual(default["name"], import_name(default["imported_at"]))
        self.assertNotEqual(files["id"], directory["id"])
        self.assertEqual(self.service.load(directory["id"])["name"], "命名目录项目")
        self.assertNotIn("_custom_name", files)  # internal migration marker stays private

    def test_rename_only_changes_name_revision_updated_time_and_internal_name_marker(self):
        public = self.service.open_path(str(self.source))
        stored = self.service.load(public["id"])
        stored["draft_dirty"] = True
        stored["annotations"]["habit"] = [{"id": "draft", "label": "喝水", "kind": "point", "start_ms": 100, "end_ms": 100}]
        self.service.save(stored)
        before = self.service.load(public["id"])
        cache_before = self.service.preview_spec(before, before["videos"][0]["id"], check_source=False)
        with patch.object(self.service.sessions, "invalidate") as invalidate:
            renamed = self.service.rename_project(public["id"], "  新项目名 / 😀  ", public["revision"])
        invalidate.assert_not_called()
        self.assertEqual(renamed["name"], "新项目名 / 😀")
        self.assertEqual(renamed["revision"], public["revision"] + 1)
        after = self.service.load(public["id"])
        self.assertEqual({key: value for key, value in before.items() if key not in {"name", "revision", "updated_at", "_custom_name"}},
                         {key: value for key, value in after.items() if key not in {"name", "revision", "updated_at", "_custom_name"}})
        cache_after = self.service.preview_spec(after, after["videos"][0]["id"], check_source=False)
        self.assertEqual((cache_after.key, cache_after.target), (cache_before.key, cache_before.target))
        self.assertEqual(self.service.storage.directory(after["id"]).name, after["id"])
        self.assertTrue(after["draft_dirty"])
        self.assertTrue(after["_custom_name"])

    def test_same_trimmed_name_is_noop_but_stale_revision_still_conflicts(self):
        project = self.service.open_path(str(self.source), name="保持名称")
        before = self.service.load(project["id"])
        with patch.object(self.service, "save", wraps=self.service.save) as save:
            same = self.service.rename_project(project["id"], " 保持名称 ", project["revision"])
        save.assert_not_called()
        self.assertEqual(same["revision"], project["revision"])
        self.assertEqual(self.service.load(project["id"]), before)
        updated = self.service.rename_project(project["id"], "新名称", project["revision"])
        with self.assertRaises(HTTPException) as error:
            self.service.rename_project(project["id"], "新名称", project["revision"])
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.service.load(project["id"])["revision"], updated["revision"])

    def test_reopen_directory_with_new_requested_name_preserves_original_name_and_revision(self):
        project = self.service.open_path(str(self.source), name="已保存的名称")
        reopened = self.service.open_path(str(self.source), name="新建表单中的不同名称")
        self.assertEqual((reopened["id"], reopened["name"], reopened["revision"]),
                         (project["id"], project["name"], project["revision"]))

    def test_supplement_and_supplemented_reopen_preserve_renamed_project(self):
        project = self.service.open_path(str(self.source), name="开始名称")
        renamed = self.service.rename_project(project["id"], "追加素材的项目", project["revision"])
        extra = self.root / "clip_20260914000005.mp4"
        extra.write_bytes(b"synthetic new source")
        after = self.service.supplement(project["id"], [extra], renamed["revision"])["project"]
        self.assertEqual(after["name"], "追加素材的项目")
        reopened = self.service.open_path(str(self.source), name="不得覆盖补导入名称")
        self.assertEqual((reopened["id"], reopened["name"]), (project["id"], after["name"]))

    def test_names_survive_restart_listing_export_and_legacy_migration(self):
        project = self.service.open_path(str(self.source), name="重启后保留")
        stored = self.service.load(project["id"])
        stored["_storage_version"] = 0
        self.service.save(stored)
        with patch.object(self.service, "attach_source_caches"), patch.object(self.service, "cleanup_relinked_sources"):
            self.service.migrate_legacy_projects()
        self.assertEqual(self.service.load(project["id"])["name"], "重启后保留")
        reopened_service = ProjectService(self.root)
        try:
            self.assertEqual(reopened_service.projects()[0]["name"], "重启后保留")
            _, documents = reopened_service.export_documents(complete_project(reopened_service.load(project["id"])))
            import json
            self.assertTrue(all(json.loads(raw)["collection_name"] == "重启后保留" for raw in documents.values()))
        finally:
            reopened_service.sessions.close()
            reopened_service.previews.close()

    def test_renaming_legacy_draft_before_migration_preserves_user_name(self):
        project = self.service.open_path(str(self.source))
        stored = self.service.load(project["id"])
        stored.pop("_storage_version", None)
        stored.pop("imported_at", None)
        stored["name"] = "VIDEO"
        self.service.save(stored)
        renamed = self.service.rename_project(project["id"], "用户已重命名", project["revision"])
        with patch.object(self.service, "attach_source_caches"), patch.object(self.service, "cleanup_relinked_sources"):
            self.service.migrate_legacy_projects()
        self.assertEqual(self.service.load(project["id"])["name"], renamed["name"])
        self.assertEqual(self.service.load(project["id"])["revision"], renamed["revision"])

    def test_two_clients_renaming_same_revision_have_one_winner(self):
        project = self.service.open_path(str(self.source))
        barrier = threading.Barrier(2)
        results = []
        def rename(name):
            barrier.wait(timeout=3)
            try:
                results.append(self.service.rename_project(project["id"], name, project["revision"])["name"])
            except HTTPException as error:
                results.append(error.status_code)
        threads = [threading.Thread(target=rename, args=(name,)) for name in ("甲", "乙")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results.count(409), 1)
        self.assertEqual(self.service.load(project["id"])["revision"], project["revision"] + 1)
        self.assertIn(self.service.load(project["id"])["name"], ("甲", "乙"))

    def test_rename_rejects_missing_deleting_and_invalid_without_mutation(self):
        project = self.service.open_path(str(self.source))
        before = self.service.load(project["id"])
        with self.assertRaises(HTTPException) as error:
            self.service.rename_project("0" * 32, "不存在", 0)
        self.assertEqual(error.exception.status_code, 404)
        for name in ("", "x" * 81, "换行\n"):
            with self.assertRaises(HTTPException):
                self.service.rename_project(project["id"], name, project["revision"])
            self.assertEqual(self.service.load(project["id"]), before)
        before["_deleting"] = True
        self.service.save(before)
        with self.assertRaises(HTTPException) as error:
            self.service.rename_project(project["id"], "删除中的项目", project["revision"])
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.service.load(project["id"], allow_deleting=True), before)

    def test_draft_concurrency_uses_rename_revision_and_preserves_clean_flag(self):
        project = self.service.open_path(str(self.source))
        renamed = self.service.rename_project(project["id"], "已经重命名", project["revision"])
        self.assertFalse(renamed["draft_dirty"])
        with self.assertRaises(HTTPException) as error:
            self.service.update_draft(project["id"], project["annotations"], project["revision"])
        self.assertEqual(error.exception.status_code, 409)
        updated = self.service.update_draft(project["id"], project["annotations"], renamed["revision"])
        self.assertEqual(updated["name"], renamed["name"])

    def test_api_new_name_routes_rename_validation_origin_guard_and_capability(self):
        with TestClient(create_app(self.root / "naming-api-runtime", auth_required=False), base_url="http://127.0.0.1") as client:
            self.assertIn("project-naming", client.get("/api/health").json()["capabilities"])
            response = client.post("/api/projects/open", json={"path": str(self.source), "name": " API目录名 "})
            self.assertEqual(response.status_code, 200, response.text)
            directory = response.json()
            self.assertEqual(directory["name"], "API目录名")
            first = next(self.fpv.glob("*.mp4"))
            for endpoint, payload in (("/api/projects/files", {"paths": [str(first)]}), ("/api/projects/open", {"path": str(first)})):
                response = client.post(endpoint, json={**payload, "name": "API文件名😀"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["name"], "API文件名😀")
            endpoint = f'/api/projects/{directory["id"]}/name'
            response = client.patch(endpoint, json={"name": "API新名称", "expected_revision": directory["revision"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["revision"], directory["revision"] + 1)
            for payload in ({"name": "", "expected_revision": 1}, {"name": "x" * 81, "expected_revision": 1},
                            {"name": "换行\n", "expected_revision": 1}, {"name": 123, "expected_revision": 1},
                            {"name": "合法", "expected_revision": True}, {"name": "合法", "expected_revision": -1}):
                self.assertEqual(client.patch(endpoint, json=payload).status_code, 422, repr(payload))
            self.assertEqual(client.patch(endpoint, json={"name": "过期", "expected_revision": 0}).status_code, 409)
            self.assertEqual(client.patch(endpoint, json={"name": "站外请求", "expected_revision": 1}, headers={"Origin": "https://outside.example"}).status_code, 403)
            self.assertEqual(client.get(f'/api/projects/{directory["id"]}').json()["name"], "API新名称")
            for endpoint, payload in (("/api/projects/files", {"paths": [str(first)]}), ("/api/projects/open", {"path": str(self.source)})):
                self.assertEqual(client.post(endpoint, json={**payload, "name": "bad\x00name"}).status_code, 422)
