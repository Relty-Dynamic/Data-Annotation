"""Local account, session and project access storage. No credentials leave .local."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
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
                    email: str | None = None) -> dict:
        username, display_name = username.strip(), display_name.strip()
        email = email.strip().lower() if email else None
        if (not USERNAME.fullmatch(username) or not 1 <= len(display_name) <= 80 or role not in {"admin", "annotator"}
                or (email is not None and (len(email) > 254 or not EMAIL.fullmatch(email)))):
            raise HTTPException(422, "账号、姓名或角色格式不正确。")
        try:
            encoded = _password_hash(password)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        ident = uuid.uuid4().hex
        with self.connection() as con:
            try:
                con.execute("INSERT INTO users (id,username,display_name,email,role,password_hash,active,can_upload) VALUES (?,?,?,?,?,?,1,0)",
                            (ident, username, display_name, email, role, encoded))
            except sqlite3.IntegrityError as error:
                raise HTTPException(409, "账号或邮箱已存在。") from error
        return {"id": ident, "username": username, "display_name": display_name, "email": email, "role": role,
                "active": True, "can_upload": role == "admin"}

    def list_users(self) -> list[dict]:
        with self.connection() as con:
            rows = con.execute("SELECT id,username,display_name,email,role,active,can_upload FROM users ORDER BY username").fetchall()
        return [{**dict(row), "can_upload": bool(row["can_upload"]) or row["role"] == "admin"} for row in rows]

    def set_upload_permission(self, user_id: str, allowed: bool) -> None:
        with self.connection() as con:
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

    def set_active(self, user_id: str, active: bool) -> None:
        with self.connection() as con:
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
