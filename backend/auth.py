"""Local account, session and project access storage. No credentials leave .local."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

from .service import AXES, validate_project_name


SESSION_SECONDS = 12 * 60 * 60
USERNAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._@+-]{2,119}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _password_hash(password: str, salt: bytes | None = None) -> str:
    minimum = 8 if password.isascii() and password.isdecimal() else 12
    if len(password) < minimum or len(password) > 1024:
        raise ValueError("纯数字密码至少 8 位；其他密码至少 12 位，最长 1024 位。")
    salt = salt or os.urandom(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt:32768:8:1:" + salt.hex() + ":" + key.hex()


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split(":")
        if algorithm != "scrypt" or (n, r, p) != ("32768", "8", "1"):
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, OverflowError):
        return False


class AuthStore:
    def __init__(self, root: Path):
        local = root / ".local"
        local.mkdir(parents=True, exist_ok=True)
        self.path = local / "auth.sqlite3"
        with self.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL, email TEXT,
                    role TEXT NOT NULL CHECK(role IN ('admin','annotator')),
                    password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    can_upload INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    csrf_hash TEXT NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    project_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id)
                );
                CREATE TABLE IF NOT EXISTS editing_sessions (
                    project_id TEXT NOT NULL, tab_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    touched_at INTEGER NOT NULL, PRIMARY KEY(project_id, tab_id)
                );
                CREATE TABLE IF NOT EXISTS track_templates (
                    user_id TEXT NOT NULL REFERENCES users(id), json_name TEXT NOT NULL,
                    name TEXT NOT NULL, labels_json TEXT NOT NULL, updated_at INTEGER NOT NULL,
                    PRIMARY KEY(user_id, json_name)
                );
                CREATE TABLE IF NOT EXISTS hidden_fixed_track_templates (
                    user_id TEXT NOT NULL REFERENCES users(id), track_id TEXT NOT NULL,
                    PRIMARY KEY(user_id, track_id)
                );
                CREATE TABLE IF NOT EXISTS deleted_accounts (
                    user_id TEXT PRIMARY KEY REFERENCES users(id),
                    deleted_at INTEGER NOT NULL, deleted_by TEXT NOT NULL REFERENCES users(id)
                );
            """)
            if "can_upload" not in {row[1] for row in con.execute("PRAGMA table_info(users)")}:
                con.execute("ALTER TABLE users ADD COLUMN can_upload INTEGER NOT NULL DEFAULT 0")
            if "email" not in {row[1] for row in con.execute("PRAGMA table_info(users)")}:
                con.execute("ALTER TABLE users ADD COLUMN email TEXT")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique ON users(email COLLATE NOCASE) WHERE email IS NOT NULL")
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA journal_mode=WAL")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def has_admin(self) -> bool:
        with self.connection() as con:
            return con.execute("SELECT 1 FROM users WHERE role='admin' AND active=1 LIMIT 1").fetchone() is not None

    def create_user(self, username: str, display_name: str, password: str, role: str = "annotator",
                    email: str | None = None, *, email_login: bool = False) -> dict:
        username, display_name = username.strip(), display_name.strip()
        email = email.strip().lower() if email else None
        valid_username = (username == email and len(username) <= 254 and EMAIL.fullmatch(username)) if email_login else USERNAME.fullmatch(username)
        if (not valid_username or not 1 <= len(display_name) <= 80 or role not in {"admin", "annotator"}
                or (email is not None and (len(email) > 254 or not EMAIL.fullmatch(email)))):
            raise HTTPException(422, "账号、姓名或角色格式不正确。")
        try:
            encoded = _password_hash(password)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        ident = uuid.uuid4().hex
        with self.connection() as con:
            try:
                if email_login and con.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE OR email=? COLLATE NOCASE", (email, email)).fetchone():
                    raise HTTPException(409, "账号或邮箱已存在。")
                con.execute("INSERT INTO users (id,username,display_name,email,role,password_hash,active,can_upload) VALUES (?,?,?,?,?,?,1,0)",
                            (ident, username, display_name, email, role, encoded))
            except sqlite3.IntegrityError as error:
                raise HTTPException(409, "账号或邮箱已存在。") from error
        return {"id": ident, "username": username, "display_name": display_name, "email": email, "role": role,
                "active": True, "can_upload": role == "admin"}

    def list_users(self, *, include_deleted: bool = False) -> list[dict]:
        with self.connection() as con:
            query = "SELECT id,username,display_name,email,role,active,can_upload FROM users"
            if not include_deleted:
                query += " WHERE id NOT IN (SELECT user_id FROM deleted_accounts)"
            rows = con.execute(query + " ORDER BY username").fetchall()
        return [{**dict(row), "can_upload": bool(row["can_upload"]) or row["role"] == "admin"} for row in rows]

    def delete_user(self, user_id: str, actor_id: str) -> None:
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            actor = con.execute("SELECT role,active FROM users WHERE id=?", (actor_id,)).fetchone()
            if not actor or actor['role'] != 'admin' or not actor['active']:
                raise HTTPException(403, "只有超级管理员可删除账号。")
            row = con.execute("SELECT role,active FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise HTTPException(404, "账号不存在。")
            if user_id == actor_id:
                raise HTTPException(409, "不能删除当前登录账号。")
            if con.execute("SELECT 1 FROM deleted_accounts WHERE user_id=?", (user_id,)).fetchone():
                return
            if row['role'] == 'admin' and row['active'] and con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                raise HTTPException(409, "不能删除最后一个管理员。")
            con.execute("INSERT INTO deleted_accounts VALUES (?,?,?)", (user_id, int(time.time()), actor_id))
            con.execute("UPDATE users SET active=0,can_upload=0 WHERE id=?", (user_id,))
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM editing_sessions WHERE user_id=?", (user_id,))

    def _require_not_deleted(self, con, user_id):
        if con.execute("SELECT 1 FROM deleted_accounts WHERE user_id=?", (user_id,)).fetchone():
            raise HTTPException(409, "账号已删除，不能修改或重新启用。")

    def set_upload_permission(self, user_id: str, allowed: bool) -> None:
        with self.connection() as con:
            self._require_not_deleted(con, user_id)
            row = con.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise HTTPException(404, "账号不存在。")
            if row["role"] != "annotator":
                raise HTTPException(422, "只能调整标注账号的上传权限。")
            con.execute("UPDATE users SET can_upload=? WHERE id=?", (int(allowed), user_id))

    def can_upload(self, user_id: str) -> bool:
        with self.connection() as con:
            row = con.execute("SELECT role,active,can_upload FROM users WHERE id=?", (user_id,)).fetchone()
        return bool(row and row["active"] and (row["role"] == "admin" or row["can_upload"]))

    def list_track_templates(self, user_id: str) -> list[dict]:
        with self.connection() as con:
            rows = con.execute("SELECT json_name,name,labels_json FROM track_templates WHERE user_id=? ORDER BY updated_at DESC,json_name",
                               (user_id,)).fetchall()
        return [{"json_name": row["json_name"], "name": row["name"], "labels": json.loads(row["labels_json"])} for row in rows]

    def visible_fixed_track_templates(self, user_id: str) -> list[str]:
        with self.connection() as con:
            hidden = {row[0] for row in con.execute(
                "SELECT track_id FROM hidden_fixed_track_templates WHERE user_id=?", (user_id,))}
        return [axis for axis in AXES if axis not in hidden]

    def set_fixed_track_template_visible(self, user_id: str, track_id: str, visible: bool) -> list[str]:
        if track_id not in AXES:
            raise HTTPException(404, "固定时间轴入口不存在。")
        with self.connection() as con:
            if visible:
                con.execute("DELETE FROM hidden_fixed_track_templates WHERE user_id=? AND track_id=?", (user_id, track_id))
            else:
                con.execute("INSERT OR IGNORE INTO hidden_fixed_track_templates VALUES (?,?)", (user_id, track_id))
        return self.visible_fixed_track_templates(user_id)

    def save_track_template(self, user_id: str, json_name: str, name: str, labels: list[str]) -> dict:
        if (not isinstance(json_name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", json_name)
                or json_name in {"scene", "posture", "category", "habit"} or json_name.startswith("custom_")
                or not isinstance(name, str)
                or not isinstance(labels, list) or len(labels) > 32
                or any(not isinstance(label, str) for label in labels)):
            raise HTTPException(422, "个人时间轴模板格式不正确。")
        name = validate_project_name(name)
        labels = [validate_project_name(label) for label in labels]
        if len(set(labels)) != len(labels):
            raise HTTPException(422, "个人时间轴模板中的标签不能重复。")
        with self.connection() as con:
            count = con.execute("SELECT COUNT(*) FROM track_templates WHERE user_id=?", (user_id,)).fetchone()[0]
            exists = con.execute("SELECT 1 FROM track_templates WHERE user_id=? AND json_name=?", (user_id, json_name)).fetchone()
            if count >= 32 and not exists:
                raise HTTPException(422, "每个账号最多保存 32 条时间轴模板。")
            con.execute("INSERT INTO track_templates VALUES (?,?,?,?,?) ON CONFLICT(user_id,json_name) DO UPDATE SET name=excluded.name,labels_json=excluded.labels_json,updated_at=excluded.updated_at",
                        (user_id, json_name, name, json.dumps(labels, ensure_ascii=False), int(time.time())))
        return {"json_name": json_name, "name": name, "labels": labels}

    def delete_track_template(self, user_id: str, json_name: str) -> None:
        with self.connection() as con:
            result = con.execute("DELETE FROM track_templates WHERE user_id=? AND json_name=?", (user_id, json_name))
            if result.rowcount == 0:
                raise HTTPException(404, "个人时间轴模板不存在。")

    def set_active(self, user_id: str, active: bool) -> None:
        with self.connection() as con:
            self._require_not_deleted(con, user_id)
            row = con.execute("SELECT role,active FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise HTTPException(404, "账号不存在。")
            if not active and row["role"] == "admin" and row["active"] and con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                raise HTTPException(409, "不能停用最后一个管理员。")
            con.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
            if not active:
                con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                con.execute("DELETE FROM assignments WHERE user_id=?", (user_id,))

    def reset_password(self, user_id: str, password: str) -> None:
        try:
            encoded = _password_hash(password)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        with self.connection() as con:
            self._require_not_deleted(con, user_id)
            if not con.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise HTTPException(404, "账号不存在。")
            con.execute("UPDATE users SET password_hash=? WHERE id=?", (encoded, user_id))
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def change_password(self, user_id: str, current: str, password: str) -> None:
        with self.connection() as con:
            row = con.execute("SELECT password_hash FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        if not row or not _verify_password(current, row[0]):
            raise HTTPException(403, "当前密码错误。")
        self.reset_password(user_id, password)

    def login(self, username: str, password: str, client_key: str) -> tuple[dict, str, str]:
        # Persistent, per account and client throttling also covers unknown accounts.
        keys = [hashlib.sha256(value.encode()).hexdigest() for value in
                ("client|" + client_key, "account|" + client_key + "|" + username.casefold())]
        failed = False
        with self.connection() as con:
            con.execute("CREATE TABLE IF NOT EXISTS login_attempts (key TEXT PRIMARY KEY, failures INTEGER NOT NULL, blocked_until INTEGER NOT NULL)")
            attempts = [con.execute("SELECT failures,blocked_until FROM login_attempts WHERE key=?", (key,)).fetchone() for key in keys]
            if any(attempt and attempt["blocked_until"] > int(time.time()) for attempt in attempts):
                raise HTTPException(429, "登录尝试过多，请稍后再试。")
            row = con.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (username,)).fetchone()
            if not row and "@" in username:
                row = con.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE AND active=1", (username,)).fetchone()
            if not row or not _verify_password(password, row["password_hash"]):
                for key, attempt in zip(keys, attempts):
                    failures = (attempt["failures"] if attempt else 0) + 1
                    con.execute("INSERT INTO login_attempts VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET failures=excluded.failures,blocked_until=excluded.blocked_until", (key, failures, int(time.time()) + 300 if failures >= 5 else 0))
                failed = True
            else:
                con.executemany("DELETE FROM login_attempts WHERE key=?", [(key,) for key in keys])
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                con.execute("INSERT INTO sessions VALUES (?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), row["id"], hashlib.sha256(csrf.encode()).hexdigest(), int(time.time()) + SESSION_SECONDS))
                result = self._public(row), token, csrf
        if failed:
            raise HTTPException(401, "账号或密码错误。")
        return result

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "username": row["username"], "display_name": row["display_name"], "email": row["email"],
                "role": row["role"], "can_upload": row["role"] == "admin" or bool(row["can_upload"])}

    def session(self, token: str | None) -> tuple[dict, str] | None:
        if not token:
            return None
        with self.connection() as con:
            row = con.execute("SELECT u.*,s.csrf_hash,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or not row["active"] or row["expires_at"] <= int(time.time()):
            return None
        return self._public(row), row["csrf_hash"]

    def logout(self, token: str | None) -> None:
        if token:
            with self.connection() as con:
                con.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def assign(self, project_id: str, user_id: str | None) -> None:
        with self.connection() as con:
            if user_id is None:
                con.execute("DELETE FROM assignments WHERE project_id=?", (project_id,))
                return
            row = con.execute("SELECT role,active FROM users WHERE id=?", (user_id,)).fetchone()
            if not row or row["role"] != "annotator" or not row["active"]:
                raise HTTPException(422, "请选择有效的标注账号。")
            con.execute("INSERT INTO assignments VALUES (?,?) ON CONFLICT(project_id) DO UPDATE SET user_id=excluded.user_id", (project_id, user_id))

    def claim(self, project_id: str, user_id: str) -> bool:
        """Give an unclaimed project to its first annotator without replacing an owner."""
        with self.connection() as con:
            row = con.execute("SELECT role,active FROM users WHERE id=?", (user_id,)).fetchone()
            if not row or row["role"] != "annotator" or not row["active"]:
                raise HTTPException(422, "请选择有效的标注账号。")
            con.execute("INSERT INTO assignments VALUES (?,?) ON CONFLICT(project_id) DO NOTHING", (project_id, user_id))
            owner = con.execute("SELECT user_id FROM assignments WHERE project_id=?", (project_id,)).fetchone()
            return owner is not None and owner[0] == user_id

    def assignment(self, project_id: str) -> str | None:
        with self.connection() as con:
            row = con.execute("SELECT user_id FROM assignments WHERE project_id=?", (project_id,)).fetchone()
        return row[0] if row else None

    def allowed(self, user: dict, project_id: str) -> bool:
        return user["role"] == "admin" or self.assignment(project_id) == user["id"]

    def allowed_project_ids(self, user: dict) -> set[str] | None:
        if user["role"] == "admin":
            return None
        with self.connection() as con:
            return {row[0] for row in con.execute("SELECT project_id FROM assignments WHERE user_id=?", (user["id"],))}

    def enter_editing(self, project_id: str, user_id: str, tab_id: str) -> list[dict]:
        current = int(time.time())
        with self.connection() as con:
            con.execute("DELETE FROM editing_sessions WHERE touched_at<?", (current - 45,))
            con.execute("INSERT INTO editing_sessions VALUES (?,?,?,?) ON CONFLICT(project_id,tab_id) DO UPDATE SET user_id=excluded.user_id,touched_at=excluded.touched_at",
                        (project_id, tab_id, user_id, current))
            rows = con.execute("SELECT DISTINCT u.id,u.display_name,u.role FROM editing_sessions e JOIN users u ON u.id=e.user_id WHERE e.project_id=? AND e.user_id!=? AND u.active=1",
                               (project_id, user_id)).fetchall()
            return [{"id": row[0], "display_name": row[1], "role": row[2]} for row in rows]

    def leave_editing(self, project_id: str, user_id: str, tab_id: str) -> None:
        with self.connection() as con:
            con.execute("DELETE FROM editing_sessions WHERE project_id=? AND tab_id=? AND user_id=?", (project_id, tab_id, user_id))


def main():
    parser = argparse.ArgumentParser(description="创建 DataMark 管理员账号")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check-admin", action="store_true")
    args = parser.parse_args()
    store = AuthStore(args.root)
    if args.check_admin:
        sys.exit(0 if store.has_admin() else 1)
    if store.has_admin():
        parser.error("管理员已存在，请在应用内管理其他账号。")
    username = input("管理员账号：").strip()
    display_name = input("显示姓名：").strip()
    password = getpass.getpass("密码（纯数字至少 8 位；其他至少 12 位）：")
    repeat = getpass.getpass("再次输入密码：")
    if password != repeat:
        parser.error("两次密码不一致。")
    store.create_user(username, display_name, password, "admin")
    print("管理员账号已创建。")


if __name__ == "__main__":
    main()
