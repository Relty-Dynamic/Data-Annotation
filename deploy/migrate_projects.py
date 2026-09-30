"""Move a stopped DataMark installation to Ubuntu after verifying every source video."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath


DATABASES = ("annotations.sqlite3", "auth.sqlite3")
AXES = ("scene", "posture", "category", "habit")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(destination)) as copy:
        original.backup(copy)
    destination.chmod(0o600)


def records(database: Path) -> list[dict]:
    with closing(sqlite3.connect(database)) as con:
        return [json.loads(row[0]) for row in con.execute("SELECT document FROM projects ORDER BY id")]


def source_stamp(path: Path) -> dict:
    stat = path.stat()
    with path.open("rb") as handle:
        sample = handle.read(65536)
        if stat.st_size > 65536:
            handle.seek(max(65536, stat.st_size - 65536))
            sample += handle.read(65536)
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "sample_sha256": hashlib.sha256(sample).hexdigest()}


def export_bundle(state: Path, output: Path) -> dict:
    if output.exists():
        raise ValueError("迁移包目录已存在；不会覆盖。")
    repository = Path(__file__).resolve().parents[1]
    target = output.resolve()
    if target.is_relative_to(repository) and not target.is_relative_to(repository / ".local"):
        raise ValueError("迁移包包含账号和个人数据，请保存在仓库外或 .local 内。")
    if not (state / DATABASES[0]).is_file():
        raise ValueError("找不到标注数据库。")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".datamark-export-", dir=output.parent))
    try:
        snapshot(state / DATABASES[0], temporary / DATABASES[0])
        auth_uninitialized = not (state / DATABASES[1]).is_file()
        if auth_uninitialized:
            with closing(sqlite3.connect(temporary / DATABASES[1])):
                pass
            (temporary / DATABASES[1]).chmod(0o600)
        else:
            snapshot(state / DATABASES[1], temporary / DATABASES[1])
            with closing(sqlite3.connect(temporary / DATABASES[1])) as con:
                tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "users" not in tables:
                    if tables:
                        raise ValueError("旧账号库结构不受支持。")
                    auth_uninitialized = True
                elif not con.execute("SELECT 1 FROM users WHERE role='admin' AND active=1 LIMIT 1").fetchone():
                    if con.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                        raise ValueError("旧账号库没有有效管理员。")
                    auth_uninitialized = True
        projects = []
        for project in records(temporary / DATABASES[0]):
            videos = []
            for video in project["videos"]:
                source = project["_sources"][video["id"]]
                path = Path(source["path"])
                before = source_stamp(path)
                if (before["size"], before["sample_sha256"]) != (source["stamp"]["size"], source["stamp"]["sample_sha256"]):
                    raise ValueError(f"原视频已改变：{video['name']}")
                digest = sha256_file(path)
                if source_stamp(path) != before:
                    raise ValueError(f"校验期间原视频已改变：{video['name']}")
                videos.append({"id": video["id"], "path": source["path"], "size": before["size"], "sha256": digest})
            projects.append({"id": project["id"], "videos": videos})
        manifest = {"version": 1, "databases": {name: sha256_file(temporary / name) for name in DATABASES},
                    "auth_uninitialized": auth_uninitialized, "projects": projects}
        (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if output.exists():
            raise ValueError("迁移包目录已被其他进程创建；不会覆盖。")
        os.replace(temporary, output)
        return {"projects": len(projects), "videos": sum(len(item["videos"]) for item in projects),
                "needs_admin": auth_uninitialized}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _path_parts(raw: str):
    path = PureWindowsPath(raw) if raw.startswith("\\\\") or re.match(r"^[A-Za-z]:[\\/]", raw) else PurePosixPath(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("迁移路径必须是无上级跳转的绝对路径。")
    return path


def load_mappings(path: Path) -> list[tuple[object, Path]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, list) or not document:
        raise ValueError("路径映射必须是非空列表。")
    mappings = []
    for item in document:
        if not isinstance(item, dict) or set(item) != {"from", "to"}:
            raise ValueError("每条路径映射需要 from 和 to。")
        original = _path_parts(item["from"])
        destination = Path(item["to"])
        if not destination.is_absolute() or ".." in destination.parts or not destination.is_dir():
            raise ValueError("Ubuntu 映射目录必须是已挂载的绝对目录。")
        mappings.append((original, destination.resolve(strict=True)))
    mappings.sort(key=lambda item: len(item[0].parts), reverse=True)
    return mappings


def map_source(raw: str, mappings: list[tuple[object, Path]]) -> Path:
    candidate = _path_parts(raw)
    for original, destination in mappings:
        if type(candidate) is not type(original):
            continue
        try:
            relative = candidate.relative_to(original)
        except ValueError:
            continue
        result = destination.joinpath(*relative.parts).resolve(strict=True)
        if not result.is_relative_to(destination):
            raise ValueError("映射后的路径超出 NAS 目录。")
        return result
    raise ValueError(f"缺少路径映射：{raw}")


def external_hashes(source: Path) -> dict:
    timeline = source / "timeline"
    folder = timeline if any((timeline / f"{axis}.timeline.json").exists() for axis in AXES) else source
    return {axis: sha256_file(folder / f"{axis}.timeline.json") if (folder / f"{axis}.timeline.json").exists() else None
            for axis in AXES}


def current_source_fingerprint(project: dict) -> str:
    source_dir = Path(project["source_dir"]) if project.get("source_dir") else None
    items = []
    for index, video in enumerate(project["videos"]):
        source = project["_sources"][video["id"]]
        path = Path(source["path"])
        relative = str(path.relative_to(source_dir)).replace("\\", "/") if source_dir and path.is_relative_to(source_dir) else path.name
        items.append({**video, "id": f"v{index + 1:04d}", "name": path.name, "relative_path": relative,
                      "url": None, "thumbnail_url": None, "stamp": source["stamp"]})
    return hashlib.sha256((json.dumps(items, ensure_ascii=False, indent=2) + "\n").encode("utf-8")).hexdigest()


def apply_bundle(bundle: Path, state: Path, mappings_file: Path) -> dict:
    if state.exists():
        raise ValueError("Ubuntu 状态目录已存在；不会覆盖现有账号或草稿。")
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or set(manifest.get("databases", {})) != set(DATABASES):
        raise ValueError("迁移包格式不受支持。")
    if type(manifest.get("auth_uninitialized", False)) is not bool:
        raise ValueError("迁移包账号状态不受支持。")
    needs_admin = manifest.get("auth_uninitialized", False)
    for name in DATABASES:
        if sha256_file(bundle / name) != manifest["databases"][name]:
            raise ValueError(f"迁移包数据库校验失败：{name}")
    mappings = load_mappings(mappings_file)
    expected_projects = {item["id"]: item for item in manifest["projects"]}
    if len(expected_projects) != len(manifest["projects"]):
        raise ValueError("迁移包项目 ID 重复。")
    state.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".datamark-import-", dir=state.parent))
    try:
        for name in DATABASES:
            shutil.copy2(bundle / name, temporary / name)
            (temporary / name).chmod(0o600)
        migrated = 0
        with closing(sqlite3.connect(temporary / DATABASES[0])) as con:
            with con:
                actual = records(temporary / DATABASES[0])
                if {item["id"] for item in actual} != set(expected_projects):
                    raise ValueError("迁移包项目清单与数据库不一致。")
                for project in actual:
                    entries = {item["id"]: item for item in expected_projects[project["id"]]["videos"]}
                    if (len(entries) != len(expected_projects[project["id"]]["videos"])
                            or {item["id"] for item in project["videos"]} != set(entries)):
                        raise ValueError("迁移包视频清单与项目不一致。")
                    for video in project["videos"]:
                        source = project["_sources"][video["id"]]
                        entry = entries[video["id"]]
                        if entry["path"] != source["path"]:
                            raise ValueError("迁移包原视频路径与数据库不一致。")
                        target = map_source(entry["path"], mappings)
                        if not target.is_file() or target.name.casefold() != video["name"].casefold() or target.stat().st_size != entry["size"]:
                            raise ValueError(f"Ubuntu 原视频名称或大小不符：{video['name']}")
                        before = source_stamp(target)
                        if sha256_file(target) != entry["sha256"] or source_stamp(target) != before:
                            raise ValueError(f"Ubuntu 原视频内容不符：{video['name']}")
                        source.setdefault("cache_identity_path", source["path"])
                        source.setdefault("cache_identity_stamp", source["stamp"])
                        source["path"] = str(target)
                        source["stamp"] = before
                        source["cache_directory"] = str(target.parent / ".datamark-cache" / project["id"] / "preview")
                        migrated += 1
                    if project.get("source_dir"):
                        source_dir = map_source(project["source_dir"], mappings)
                        if not source_dir.is_dir() or external_hashes(source_dir) != {axis: project.get("_external_hashes", {}).get(axis) for axis in AXES}:
                            raise ValueError("原目录 timeline 文件与旧项目记录不一致；未迁移。")
                        project["source_dir"] = str(source_dir)
                        project["_source_key"] = os.path.normcase(str(source_dir))
                    else:
                        project["_source_key"] = None
                    project["_storage_version"] = 3
                    # The old fingerprint identifies already exported annotations;
                    # a verified Linux source identity is accepted when reopening.
                    fresh_fingerprint = current_source_fingerprint(project)
                    project["_source_fingerprint_aliases"] = list(dict.fromkeys(
                        project.get("_source_fingerprint_aliases", []) + [fresh_fingerprint]))
                    con.execute("UPDATE projects SET source_key=?, document=? WHERE id=?",
                                (project["_source_key"], json.dumps(project, ensure_ascii=False), project["id"]))
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with closing(sqlite3.connect(temporary / DATABASES[1])) as con:
            with con:
                if needs_admin:
                    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if "users" in tables and con.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                        raise ValueError("迁移包账号状态与账号库不一致。")
                else:
                    if not con.execute("SELECT 1 FROM users WHERE role='admin' AND active=1 LIMIT 1").fetchone():
                        raise ValueError("迁移账号库没有有效管理员。")
                    con.execute("DELETE FROM sessions")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if state.exists():
            raise ValueError("Ubuntu 状态目录在校验期间被创建；不会覆盖。")
        os.replace(temporary, state)
        return {"projects": len(actual), "videos": migrated, "needs_admin": needs_admin}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="在旧平台停止后生成校验迁移包")
    export.add_argument("--state", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    apply = commands.add_parser("apply", help="在 Ubuntu 上校验 NAS 原视频并迁入空状态目录")
    apply.add_argument("--bundle", type=Path, required=True)
    apply.add_argument("--state", type=Path, required=True)
    apply.add_argument("--mappings", type=Path, required=True)
    args = parser.parse_args()
    result = (export_bundle(args.state, args.output) if args.command == "export"
              else apply_bundle(args.bundle, args.state, args.mappings))
    print(f"已验证 {result['projects']} 个项目、{result['videos']} 段视频。")
    if result["needs_admin"]:
        print("旧平台尚无账号；迁移完成后须交互创建管理员，再启动网页服务。")


if __name__ == "__main__":
    main()
