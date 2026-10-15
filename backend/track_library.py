"""Account libraries and shared defaults, separate from project snapshots."""
from __future__ import annotations

import json
import time
import uuid

from fastapi import HTTPException

from .service import AXES, AXIS_NAMES, DEFAULT_TRACK_LABELS, valid_custom_axis, validate_project_name


class TrackLibrary:
    DEFAULT_SCOPE = "*"

    def __init__(self, auth):
        self.auth = auth
        with auth.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS track_library (
                    scope TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                    mode TEXT NOT NULL, labels_json TEXT NOT NULL,
                    PRIMARY KEY(scope,id)
                );
                CREATE TABLE IF NOT EXISTS track_library_settings (key TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS track_library_events (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, track_id TEXT NOT NULL,
                    actor_id TEXT, recorded_at INTEGER NOT NULL, before_json TEXT, after_json TEXT
                );
            """)
            if not con.execute("SELECT 1 FROM track_library_settings WHERE key='initialized'").fetchone():
                for axis in ("scene", "posture", "category"):
                    con.execute("INSERT INTO track_library VALUES (?,?,?,?,?)",
                                (self.DEFAULT_SCOPE, axis, AXIS_NAMES[axis], "state",
                                 json.dumps(DEFAULT_TRACK_LABELS[axis], ensure_ascii=False)))
                # Preserve personal templates without renaming any existing axes.
                con.execute("INSERT OR IGNORE INTO track_library SELECT user_id,json_name,name,'state',labels_json FROM track_templates")
                con.execute("INSERT INTO track_library_settings VALUES ('initialized')")

    @staticmethod
    def track(row):
        return {"id": row["id"], "name": row["name"], "mode": row["mode"],
                "labels": json.loads(row["labels_json"]), "is_default": row["scope"] == "*"}

    def defaults(self):
        return [track for track in self.list(None) if track["is_default"]]

    def list(self, user_id):
        with self.auth.connection() as con:
            rows = con.execute("SELECT * FROM track_library WHERE scope IN (?,?) ORDER BY CASE scope WHEN '*' THEN 0 ELSE 1 END,rowid",
                               (self.DEFAULT_SCOPE, user_id or "local")).fetchall()
        # Keep stored legacy definitions recoverable without offering the retired built-in axis.
        return [self.track(row) for row in rows if row["id"] != "habit"]

    def save(self, user_id, track_id, name, mode, labels, *, default=False, create=False):
        if track_id == "habit":
            raise HTTPException(410, "内置习惯轴已停用；请创建自定义事件轴。")
        if (not isinstance(track_id, str) or not (track_id in AXES or valid_custom_axis(track_id))
                or (create and track_id.startswith("custom_"))):
            raise HTTPException(422, "JSON 名须以小写字母开头，只能包含小写字母、数字、下划线或连字符。")
        if track_id in AXES and not default and create:
            raise HTTPException(403, "预置时间轴只能由管理员在默认库中维护。")
        if mode not in ("state", "event") or (track_id in AXES and mode != ("event" if track_id == "habit" else "state")):
            raise HTTPException(422, "时间轴类型无效。")
        name = validate_project_name(name)
        if not isinstance(labels, list) or not 1 <= len(labels) <= 32:
            raise HTTPException(422, "每条时间轴须设置 1–32 个标签。")
        labels = [validate_project_name(label) for label in labels]
        if len(set(labels)) != len(labels):
            raise HTTPException(422, "同一时间轴不能有重复标签。")
        scope = self.DEFAULT_SCOPE if default else user_id or "local"
        after = {"id": track_id, "name": name, "mode": mode, "labels": labels, "is_default": default}
        with self.auth.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM track_library WHERE scope=? AND id=?", (scope, track_id)).fetchone()
            if create and row:
                raise HTTPException(409, "这个 JSON 名已在时间轴库中使用。")
            if not create and not row:
                raise HTTPException(404, "时间轴不存在。")
            conflict = con.execute("SELECT 1 FROM track_library WHERE id=? AND scope!=? AND (scope='*' OR ?='*')",
                                   (track_id, scope, scope)).fetchone()
            if conflict:
                raise HTTPException(409, "默认库与个人库不能使用相同的 JSON 名。")
            if create and con.execute("SELECT COUNT(*) FROM track_library WHERE scope=?", (scope,)).fetchone()[0] >= 32:
                raise HTTPException(422, "每个时间轴库最多 32 条。")
            if create and default and track_id not in AXES:
                custom_count = con.execute("SELECT COUNT(*) FROM track_library WHERE scope='*' AND id NOT IN ('scene','posture','category','habit')").fetchone()[0]
                if custom_count >= 16:
                    raise HTTPException(422, "默认库最多包含 16 条自定义轴，以便新项目全部启用。")
            before = self.track(row) if row else None
            if before and before["mode"] != mode:
                raise HTTPException(422, "已有时间轴的类型不能修改，请创建新的时间轴。")
            con.execute("INSERT INTO track_library VALUES (?,?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET name=excluded.name,mode=excluded.mode,labels_json=excluded.labels_json",
                        (scope, track_id, name, mode, json.dumps(labels, ensure_ascii=False)))
            self._record(con, scope, track_id, user_id, before, after)
        return after

    def set_default(self, user_id, track_id, default):
        if track_id == "habit":
            raise HTTPException(410, "内置习惯轴已停用；请创建自定义事件轴。")
        personal_scope = user_id or "local"
        source = personal_scope if default else self.DEFAULT_SCOPE
        target = self.DEFAULT_SCOPE if default else personal_scope
        with self.auth.connection() as con:
            # Serialize the conflict checks and move so concurrent changes cannot
            # replace another account's definition or exceed library limits.
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM track_library WHERE scope=? AND id=?", (source, track_id)).fetchone()
            if not row:
                raise HTTPException(404, "时间轴不存在或分类已变更，请刷新时间轴库。")
            conflict = con.execute("SELECT 1 FROM track_library WHERE id=? AND scope!=?" if default
                                   else "SELECT 1 FROM track_library WHERE id=? AND scope=?",
                                   (track_id, source if default else target)).fetchone()
            if conflict:
                raise HTTPException(409, "这个 JSON 名已被其他个人时间轴使用，无法增加为默认时间轴。" if default
                                    else "个人库中已有相同 JSON 名，无法移除默认身份。")
            if con.execute("SELECT COUNT(*) FROM track_library WHERE scope=?", (target,)).fetchone()[0] >= 32:
                raise HTTPException(422, "目标时间轴库已达到 32 条上限。")
            if default and track_id not in AXES:
                count = con.execute("SELECT COUNT(*) FROM track_library WHERE scope='*' AND id NOT IN ('scene','posture','category','habit')").fetchone()[0]
                if count >= 16:
                    raise HTTPException(422, "默认库最多包含 16 条自定义轴，以便新项目全部启用。")
            before = self.track(row)
            after = {**before, "is_default": default}
            con.execute("UPDATE track_library SET scope=? WHERE scope=? AND id=?", (target, source, track_id))
            self._record(con, source, track_id, user_id, before, None)
            self._record(con, target, track_id, user_id, None, after)
        return after

    def delete(self, user_id, track_id, *, default=False):
        scope = self.DEFAULT_SCOPE if default else user_id or "local"
        with self.auth.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM track_library WHERE scope=? AND id=?", (scope, track_id)).fetchone()
            if not row:
                raise HTTPException(404, "时间轴不存在或无权删除默认时间轴。")
            self._record(con, scope, track_id, user_id, self.track(row), None)
            con.execute("DELETE FROM track_library WHERE scope=? AND id=?", (scope, track_id))

    @staticmethod
    def _record(con, scope, track_id, actor_id, before, after):
        con.execute("INSERT INTO track_library_events VALUES (?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, scope, track_id, actor_id, int(time.time()),
                     json.dumps(before, ensure_ascii=False) if before else None,
                     json.dumps(after, ensure_ascii=False) if after else None))
