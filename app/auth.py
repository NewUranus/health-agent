"""账号、可撤销会话和用户归属；数据库只保存密码与会话凭证的哈希。"""
import argparse
from contextlib import contextmanager
import getpass
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from dotenv import load_dotenv
from app.database import schema_lock_sync

ROOT = Path(__file__).resolve().parent.parent
COOKIE_NAME = "healthpilot_session"
SESSION_SECONDS = 12 * 60 * 60
ROLES = frozenset({"user", "knowledge_admin", "ops_admin"})
ADMIN_ROLES = frozenset({"knowledge_admin", "ops_admin"})
_KNOWLEDGE_ADMIN = frozenset({
    "documents", "document-pairs", "approvals", "knowledge-archives", "text-documents",
    "chunks", "search", "compare", "stats", "accounts", "model-usage",
})
_OPS_ADMIN = frozenset({"operations", "queue", "admission", "audit", "metrics", "logs", "traces"})


class AuthFailure(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=3,
                           maxmem=64 * 1024 * 1024).hex()
    return f"scrypt${salt}${value}"


def valid_username(value):
    return bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{2,63}", value))


def valid_role(value):
    return value in ROLES


def admin_scope(resource):
    """将 /admin/... 资源归为 knowledge、ops；无法识别时返回 None。"""
    if not resource or not (resource == "/admin" or resource.startswith("/admin/")):
        return None
    name = resource[len("/admin"):].strip("/").split("/", 1)[0]
    if not name:
        return None
    if name in _KNOWLEDGE_ADMIN:
        return "knowledge"
    if name in _OPS_ADMIN:
        return "ops"
    return None


def role_allows_admin(role, resource):
    scope = admin_scope(resource)
    if scope is None or role not in ADMIN_ROLES:
        return False
    return (role == "knowledge_admin" and scope == "knowledge") or (role == "ops_admin" and scope == "ops")


class AuthStore:
    """有 DATABASE_URL 时使用 PostgreSQL；本地使用独立 SQLite 文件。"""

    def __init__(self):
        self.url = os.getenv("DATABASE_URL", "")
        self.path = Path(os.getenv("HEALTH_AGENT_AUTH_DB", str(ROOT / "runs" / "auth.sqlite3")))
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))

    @contextmanager
    def connection(self):
        if self.url:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=5)
        else:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
        try:
            with conn:
                yield lambda sql, args=(): conn.execute(sql.replace("?", "%s") if self.url else sql, args)
        finally:
            conn.close()

    def setup(self):
        if not self.url:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            self.path.chmod(0o600)
        with schema_lock_sync(self.url), self.connection() as execute:
            for statement in (
                "CREATE TABLE IF NOT EXISTS hp_users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at BIGINT NOT NULL, role TEXT NOT NULL DEFAULT 'user')",
                "CREATE TABLE IF NOT EXISTS hp_sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES hp_users(id), expires_at BIGINT NOT NULL)",
                "CREATE INDEX IF NOT EXISTS hp_sessions_user ON hp_sessions(user_id)",
                "CREATE TABLE IF NOT EXISTS hp_threads (thread_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES hp_users(id))",
                "CREATE TABLE IF NOT EXISTS hp_login_limits (key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, until_at BIGINT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS hp_audit (id TEXT PRIMARY KEY, at BIGINT NOT NULL, actor TEXT, action TEXT NOT NULL, resource TEXT NOT NULL, request_id TEXT NOT NULL, status INTEGER NOT NULL)",
                "CREATE INDEX IF NOT EXISTS hp_audit_request ON hp_audit(request_id)",
                "CREATE TABLE IF NOT EXISTS hp_model_usage (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES hp_users(id), task_id TEXT, request_id TEXT NOT NULL, at BIGINT NOT NULL, scene TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, input_tokens BIGINT, output_tokens BIGINT, duration_ms BIGINT NOT NULL)",
                "CREATE INDEX IF NOT EXISTS hp_model_usage_at ON hp_model_usage(at)",
                "CREATE TABLE IF NOT EXISTS hp_model_costs (usage_id TEXT PRIMARY KEY REFERENCES hp_model_usage(id) ON DELETE CASCADE, cost_microusd BIGINT NOT NULL, pricing_version TEXT NOT NULL)",
            ):
                execute(statement)
            self._ensure_role_column(execute)

    def _ensure_role_column(self, execute):
        if self.url:
            execute("ALTER TABLE hp_users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user'")
            return
        columns = {row["name"] for row in execute("PRAGMA table_info(hp_users)").fetchall()}
        if "role" not in columns:
            execute("ALTER TABLE hp_users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")

    @staticmethod
    def _audit(execute, actor, action, resource, request_id, status):
        execute("INSERT INTO hp_audit VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, int(time.time()), actor, action, resource, request_id, status))

    def audit(self, actor, action, resource, request_id, status):
        with self.connection() as execute:
            self._audit(execute, actor, action, resource, request_id, status)

    def provision(self, username, password, *, reset=False, role=None):
        username = username.strip().lower()
        if not valid_username(username):
            raise ValueError("账号须为 3–64 位英文字母、数字、点、横线或下划线，并以字母或数字开头。")
        if not 12 <= len(password) <= 256:
            raise ValueError("密码须为 12–256 个字符。")
        if role is None:
            role = "user" if not reset else None
        if role is not None and not valid_role(role):
            raise ValueError("角色须为 user、knowledge_admin 或 ops_admin。")
        hashed = password_hash(password)
        with self.connection() as execute:
            user = execute("SELECT id, role FROM hp_users WHERE username = ?", (username,)).fetchone()
            if bool(user) != reset:
                raise ValueError("账号不存在。" if reset else "账号已存在。")
            user_id = user["id"] if user else uuid.uuid4().hex
            if reset:
                if role is None:
                    execute("UPDATE hp_users SET password_hash = ? WHERE id = ?", (hashed, user_id))
                    role = user["role"] if user["role"] in ROLES else "user"
                else:
                    execute("UPDATE hp_users SET password_hash = ?, role = ? WHERE id = ?", (hashed, role, user_id))
                execute("DELETE FROM hp_sessions WHERE user_id = ?", (user_id,))
            else:
                execute("INSERT INTO hp_users (id, username, password_hash, created_at, role) VALUES (?, ?, ?, ?, ?)",
                        (user_id, username, hashed, int(time.time()), role))
            self._audit(execute, "local-operator", "user.password_reset" if reset else "user.created", user_id, uuid.uuid4().hex, 200)
        return {"id": user_id, "username": username, "role": role}

    def _reserve_attempt(self, username, client):
        now = int(time.time())
        limited = False
        with self.connection() as execute:
            execute("DELETE FROM hp_login_limits WHERE until_at <= ?", (now,))
            for key, limit in ((digest("user:" + username), 8), (digest("client:" + client), 40)):
                row = execute("INSERT INTO hp_login_limits VALUES (?, 1, ?) ON CONFLICT (key) DO UPDATE SET attempts = hp_login_limits.attempts + 1 RETURNING attempts",
                              (key, now + 900)).fetchone()
                limited |= row["attempts"] > limit
        if limited:
            raise AuthFailure(429, "AUTH_RATE_LIMITED", "登录尝试过多，请 15 分钟后再试。")

    def login(self, username, password, client, previous_token, request_id):
        username = username.strip().lower()
        self._reserve_attempt(username, client)
        with self.connection() as execute:
            user = execute("SELECT * FROM hp_users WHERE username = ?", (username,)).fetchone()
        stored = user["password_hash"] if user else self.dummy_hash
        matched = hmac.compare_digest(password_hash(password, stored.split("$")[1]), stored)
        if not user or not matched:
            raise AuthFailure(401, "AUTH_INVALID_CREDENTIALS", "账号或密码错误。")
        token, expires = secrets.token_urlsafe(32), int(time.time()) + SESSION_SECONDS
        with self.connection() as execute:
            # 重置密码与登录并发时，不能使用旧密码签发新凭证。
            sql = "SELECT password_hash FROM hp_users WHERE id = ?" + (" FOR UPDATE" if self.url else "")
            current = execute(sql, (user["id"],)).fetchone()
            if not current or current["password_hash"] != stored:
                raise AuthFailure(401, "AUTH_INVALID_CREDENTIALS", "账号或密码错误。")
            execute("DELETE FROM hp_sessions WHERE expires_at <= ? OR token_hash = ?", (int(time.time()), digest(previous_token or "")))
            execute("INSERT INTO hp_sessions VALUES (?, ?, ?)", (digest(token), user["id"], expires))
            execute("DELETE FROM hp_login_limits WHERE key = ?", (digest("user:" + username),))
            self._audit(execute, user["id"], "auth.login", "/auth/login", request_id, 200)
        role = user["role"] if user["role"] in ROLES else "user"
        return token, {"id": user["id"], "username": user["username"], "role": role}, expires

    def resolve(self, token):
        if not token or len(token) > 128:
            return None
        with self.connection() as execute:
            row = execute("SELECT u.id, u.username, u.role, s.expires_at FROM hp_sessions s JOIN hp_users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?",
                          (digest(token), int(time.time()))).fetchone()
        if not row:
            return None
        data = dict(row)
        if data.get("role") not in ROLES:
            data["role"] = "user"
        return data

    def logout(self, token, actor, request_id):
        with self.connection() as execute:
            execute("DELETE FROM hp_sessions WHERE token_hash = ?", (digest(token or ""),))
            self._audit(execute, actor, "auth.logout", "/auth/logout", request_id, 200)

    def claim_thread(self, thread_id, user_id):
        key = digest(thread_id)
        with self.connection() as execute:
            execute("INSERT INTO hp_threads VALUES (?, ?) ON CONFLICT (thread_hash) DO NOTHING", (key, user_id))
            owner = execute("SELECT user_id FROM hp_threads WHERE thread_hash = ?", (key,)).fetchone()
        if owner["user_id"] != user_id:
            raise AuthFailure(404, "THREAD_NOT_FOUND", "会话不存在或无权访问，请新建会话。")
        # 登录会话与旧演示的检查点、轨迹文件不共用命名空间。
        return "auth-" + digest(user_id + ":" + thread_id)


def main():
    parser = argparse.ArgumentParser(description="本地管理 HealthPilot 账号；密码不会作为命令参数或输出。")
    parser.add_argument("action", choices=["create-user", "reset-password"])
    parser.add_argument("username")
    parser.add_argument("--role", choices=sorted(ROLES), help="账号角色；创建时默认 user，重置时仅在显式传入时更改。")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    password = getpass.getpass("密码（至少 12 个字符）：")
    if password != getpass.getpass("再次输入密码："):
        parser.exit(1, "两次密码不一致。\n")
    store = AuthStore()
    store.setup()
    try:
        store.provision(args.username, password, reset=args.action == "reset-password", role=args.role)
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    print("账号已创建。" if args.action == "create-user" else "密码已重置，旧登录会话已注销。")


if __name__ == "__main__":
    main()
