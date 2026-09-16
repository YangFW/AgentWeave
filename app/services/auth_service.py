"""小规模内网部署用的轻量会话认证。

账号和会话保存在 SQLite；环境变量仅在初始化时创建尚不存在的账号。
"""
from __future__ import annotations

import hashlib
import base64
from app import db
import hmac
import os
import secrets
import time
from contextlib import closing
from contextvars import ContextVar
from typing import Any

SESSION_COOKIE = "agentnexus_session"
current_identity: ContextVar[dict[str, Any] | None] = ContextVar("current_identity", default=None)


def init_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user', enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS user_sessions (
        token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at REAL NOT NULL,
        created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS upload_owners (
        upload_id TEXT PRIMARY KEY, user_id TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS workspace_members (
        workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('member','viewer')),
        PRIMARY KEY(workspace_id,user_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL DEFAULT '',
        method TEXT NOT NULL, route TEXT NOT NULL DEFAULT '', status_code INTEGER,
        started_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS login_limits (
        bucket TEXT PRIMARY KEY, window_start REAL NOT NULL, attempts INTEGER NOT NULL)""")
    now = db.utc_now()
    for username, (password, role) in _accounts().items():
        if password:
            existing = db.query_one("SELECT id FROM users WHERE username = ?", (username,))
            if not existing:
                db.execute("INSERT INTO users(id, username, password_hash, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (f"user_{secrets.token_hex(8)}", username, _hash_password(password), role, now, now))
                db.execute("INSERT INTO workspace_members(workspace_id,user_id,role) SELECT 'default',id,'member' FROM users WHERE username=?", (username,))


def enabled() -> bool:
    return os.getenv("APP_AUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def validate_deployment_admin() -> None:
    if not enabled():
        return
    admins = db.query_all("SELECT username,password_hash FROM users WHERE role='admin' AND enabled=1")
    if not admins:
        raise RuntimeError('已启用认证，但没有可用管理员；首次启动请配置 APP_ADMIN_PASSWORD')
    for admin in admins:
        if admin['username'] == 'admin' and _verify_password('secret', admin['password_hash']):
            raise RuntimeError('检测到早期测试管理员凭据，请先按修复说明重置账号后再启用多人部署')


def _accounts() -> dict[str, tuple[str, str]]:
    return {
        os.getenv("APP_ADMIN_USERNAME", "admin"): (os.getenv("APP_ADMIN_PASSWORD", ""), "admin"),
        os.getenv("APP_USER_USERNAME", "user"): (os.getenv("APP_USER_PASSWORD", ""), "user"),
    }


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return "pbkdf2$240000$" + base64.b64encode(salt + digest).decode()


def _verify_password(password: str, encoded: str) -> bool:
    try:
        rounds = int(encoded.split("$")[1])
        raw = base64.b64decode(encoded.split("$", 2)[2])
        expected = hashlib.pbkdf2_hmac("sha256", password.encode(), raw[:16], rounds)
        return hmac.compare_digest(expected, raw[16:])
    except (ValueError, IndexError):
        return False


def login(username: str, password: str) -> tuple[str, dict[str, Any]] | None:
    account = db.query_one("SELECT id, username, password_hash, role FROM users WHERE username = ? AND enabled = 1", (username,))
    if not account or not _verify_password(password, str(account["password_hash"])):
        return None
    token = secrets.token_urlsafe(32)
    session = {"username": username, "role": account["role"], "user_id": account["id"], "expires_at": time.time() + 86400}
    db.execute("INSERT INTO user_sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
               (hashlib.sha256(token.encode()).hexdigest(), account["id"], session["expires_at"], db.utc_now()))
    return token, session


def reserve_login_attempt(username: str, peer: str, *, now: float | None = None) -> bool:
    timestamp = time.time() if now is None else now
    # 不保存原始来源地址；账号桶防止换来源绕过，来源桶限制批量猜账号。
    buckets = [("user:" + username.strip(), 10), ("peer:" + peer, 60)]
    with closing(db.get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM login_limits WHERE window_start<=?", (timestamp - 60,))
        keys = [(hashlib.sha256(name.encode()).hexdigest(), limit) for name, limit in buckets]
        for key, limit in keys:
            row = conn.execute("SELECT attempts FROM login_limits WHERE bucket=?", (key,)).fetchone()
            if row and row[0] >= limit:
                return False
        for key, _ in keys:
            conn.execute("INSERT INTO login_limits(bucket,window_start,attempts) VALUES(?,?,1) ON CONFLICT(bucket) DO UPDATE SET attempts=attempts+1", (key, timestamp))
    return True


def get_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    # 每次读取数据库，确保停用账号、修改角色和跨进程注销立即生效。
    return db.query_one(
        "SELECT u.username, u.role, u.id AS user_id, s.expires_at "
        "FROM user_sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=? AND u.enabled=1 AND s.expires_at>?",
        (hashlib.sha256(token.encode()).hexdigest(), time.time()),
    )


def logout(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM user_sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))


def start_audit(identity: dict[str, Any] | None, method: str) -> int:
    return db.execute_returning_id(
        "INSERT INTO audit_events(user_id,method,started_at) VALUES(?,?,?)",
        ((identity or {}).get("user_id", ""), method, db.utc_now()),
    )


def finish_audit(audit_id: int, route: str, status_code: int, user_id: str | None = None) -> None:
    db.execute("UPDATE audit_events SET route=?,status_code=?,finished_at=?,user_id=COALESCE(?,user_id) WHERE id=?", (route, status_code, db.utc_now(), user_id, audit_id))


def list_users() -> list[dict[str, Any]]:
    return db.query_all("SELECT id, username, role, enabled, created_at, updated_at FROM users ORDER BY username")


def create_user(username: str, password: str, role: str = "user") -> dict[str, Any]:
    now = db.utc_now()
    user_id = f"user_{secrets.token_hex(8)}"
    db.execute(
        "INSERT INTO users(id,username,password_hash,role,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (user_id, username, _hash_password(password), role, now, now),
    )
    db.execute("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES('default',?,'member')", (user_id,))
    return next(user for user in list_users() if user["id"] == user_id)


def workspace_access(workspace_id: str, identity: dict[str, Any]) -> str | None:
    workspace = db.query_one("SELECT owner_user_id,enabled FROM workspaces WHERE id=? AND organization_id='local-org'", (workspace_id,))
    if not workspace:
        return None
    if identity["role"] == "admin" or workspace["owner_user_id"] == identity["user_id"]:
        return "owner"
    if not workspace["enabled"]:
        return None
    member = db.query_one("SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?", (workspace_id, identity["user_id"]))
    return member["role"] if member else None


def agent_access(agent_id: str, identity: dict[str, Any], workspace_id: str) -> bool:
    agent = db.query_one('SELECT * FROM agents WHERE id=?', (agent_id,))
    if not agent or not agent.get('enabled', True):
        return False
    if identity['role'] == 'admin':
        return True
    visibility = agent.get('visibility') or 'organization'
    if visibility == 'public':
        return True
    if agent.get('organization_id', 'local-org') != 'local-org':
        return False
    if visibility == 'organization':
        return True
    if agent.get('workspace_id', 'default') != workspace_id:
        return False
    if not workspace_access(workspace_id, identity):
        return False
    return visibility == 'workspace' or agent.get('owner_user_id') == identity['user_id']


def update_user(user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    new_hash = _hash_password(changes["password"]) if changes.get("password") is not None else None
    with closing(db.get_conn()) as conn, conn:
        # 管理员数量检查和状态修改必须在同一写事务内完成。
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise LookupError("用户不存在")
        role = changes.get("role") or row["role"]
        active = row["enabled"] if changes.get("enabled") is None else int(changes["enabled"])
        if row["role"] == "admin" and row["enabled"] and (role != "admin" or not active):
            count = conn.execute("SELECT count(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
            if count <= 1:
                raise ValueError("不能停用或降级最后一个管理员")
        conn.execute(
            "UPDATE users SET role=?, enabled=?, password_hash=?, updated_at=? WHERE id=?",
            (role, active, new_hash or row["password_hash"], db.utc_now(), user_id),
        )
        if new_hash is not None or not active or role != row["role"]:
            conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
    return next(user for user in list_users() if user["id"] == user_id)
