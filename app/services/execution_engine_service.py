from __future__ import annotations

import os
import re
from typing import Any, Mapping

from app import db
from app.services.secret_store import secret_store

ENGINE_CREDENTIAL_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "CODEX_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

BUILTIN_EXECUTION_ENGINES: dict[str, dict[str, Any]] = {
    "codex": {
        "name": "Codex",
        "kind": "codex",
        "description": "在独立容器中调用 Codex，适合代码修改和项目内生成。",
        "default_api_key_env": "OPENAI_API_KEY",
        "primary_key_envs": ("OPENAI_API_KEY", "CODEX_API_KEY"),
        "base_url_envs": ("OPENAI_BASE_URL", "OPENAI_API_BASE"),
        "fallback_env_keys": (
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
        ),
        "model_flag": "-m",
    },
    "claude": {
        "name": "Claude Code",
        "kind": "claude",
        "description": "在独立容器中调用 Claude Code，适合代码修改和项目内生成。",
        "default_api_key_env": "ANTHROPIC_API_KEY",
        "primary_key_envs": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"),
        "base_url_envs": ("ANTHROPIC_BASE_URL",),
        "fallback_env_keys": (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ),
        "model_flag": "--model",
    },
}

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ExecutionEngineError(ValueError):
    """Raised when an execution engine cannot be used or updated."""


def is_env_name(value: str) -> bool:
    return bool(_ENV_NAME_RE.fullmatch(value or ""))


def normalize_engine_id(engine: str | None) -> str:
    value = (engine or "builtin").strip().lower()
    if value in {"codex", "codex-cli"}:
        return "codex"
    if value in {"claude", "claudecode", "claude-code"}:
        return "claude"
    if value in {"container", "command"}:
        return "container"
    return "builtin"


def ensure_schema() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_engines (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            base_url TEXT NOT NULL DEFAULT '',
            api_key_env TEXT NOT NULL DEFAULT '',
            api_key_ciphertext TEXT NOT NULL DEFAULT '',
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    now = db.utc_now()
    for engine_id, spec in BUILTIN_EXECUTION_ENGINES.items():
        existing = db.query_one("SELECT id FROM execution_engines WHERE id = ?", (engine_id,))
        if existing:
            continue
        db.execute(
            """
            INSERT INTO execution_engines(
                id, name, kind, enabled, base_url, api_key_env, api_key_ciphertext,
                config_json, created_at, updated_at
            ) VALUES (?, ?, ?, 1, '', ?, '', '{}', ?, ?)
            """,
            (engine_id, spec["name"], spec["kind"], spec["default_api_key_env"], now, now),
        )


def _table_ready() -> bool:
    try:
        return bool(
            db.query_one(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'execution_engines'"
            )
        )
    except Exception:
        return False


def list_engine_rows() -> list[dict[str, Any]]:
    if not _table_ready():
        ensure_schema()
    rows = db.query_all("SELECT * FROM execution_engines ORDER BY name")
    by_id = {str(row["id"]): row for row in rows}
    ordered: list[dict[str, Any]] = []
    for engine_id, spec in BUILTIN_EXECUTION_ENGINES.items():
        if engine_id in by_id:
            ordered.append(by_id.pop(engine_id))
        else:
            now = db.utc_now()
            ordered.append(
                {
                    "id": engine_id,
                    "name": spec["name"],
                    "kind": spec["kind"],
                    "enabled": 1,
                    "base_url": "",
                    "api_key_env": spec["default_api_key_env"],
                    "api_key_ciphertext": "",
                    "config_json": "{}",
                    "created_at": now,
                    "updated_at": now,
                }
            )
    ordered.extend(by_id.values())
    return ordered


def get_engine_row(engine_id: str) -> dict[str, Any] | None:
    if not _table_ready():
        return None
    return db.query_one("SELECT * FROM execution_engines WHERE id = ?", (engine_id,))


def engine_to_api(row: Mapping[str, Any]) -> dict[str, Any]:
    engine_id = str(row.get("id") or "")
    spec = BUILTIN_EXECUTION_ENGINES.get(engine_id, {})
    api_key_env = str(row.get("api_key_env") or "").strip()
    if api_key_env and not is_env_name(api_key_env):
        api_key_env = ""
    has_direct_key = bool(row.get("api_key_ciphertext"))
    config = db.json_loads(row.get("config_json"), {})
    public = {
        "id": engine_id,
        "name": spec.get("name") or row.get("name") or engine_id,
        "kind": spec.get("kind") or row.get("kind") or engine_id,
        "description": spec.get("description") or "",
        "enabled": bool(row.get("enabled")),
        "base_url": str(row.get("base_url") or ""),
        "api_key_env": api_key_env,
        "api_key_mode": "direct" if has_direct_key else "env",
        "has_api_key": has_direct_key,
        "default_api_key_env": spec.get("default_api_key_env") or "",
        "config": config if isinstance(config, dict) else {},
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "managed": False,
        "allowed_roles": str(row.get("allowed_roles") or "admin,user"),
        "readiness": engine_readiness(row),
    }
    return public


def engine_readiness(row: Mapping[str, Any]) -> dict[str, Any]:
    engine_id = str(row.get("id") or "")
    spec = BUILTIN_EXECUTION_ENGINES.get(engine_id, {})
    if not row.get("enabled"):
        return {"state": "off", "label": "已停用", "detail": "启用后才会出现在工作台的执行引擎列表中。"}
    has_direct_key = bool(row.get("api_key_ciphertext"))
    api_key_env = str(row.get("api_key_env") or "").strip()
    host_fallback = any(os.getenv(key) for key in spec.get("fallback_env_keys", ()))
    if has_direct_key:
        return {"state": "ready", "label": "已配置密钥", "detail": "将使用本页保存的加密密钥启动该引擎。"}
    if api_key_env and os.getenv(api_key_env):
        return {"state": "ready", "label": "已配置环境变量", "detail": f"服务进程已检测到 {api_key_env}。"}
    if host_fallback:
        return {"state": "ready", "label": "已使用进程环境", "detail": "尚未在本页保存密钥，将回退使用服务进程中的现有环境变量。"}
    if api_key_env:
        return {
            "state": "needs_config",
            "label": "缺少密钥",
            "detail": f"服务进程当前未检测到 {api_key_env}，也可以改为直接填写密钥。",
        }
    return {"state": "needs_config", "label": "缺少密钥", "detail": "请选择环境变量或直接填写密钥后再运行该引擎。"}


def list_engines() -> list[dict[str, Any]]:
    return [engine_to_api(row) for row in list_engine_rows()]


def get_engine(engine_id: str) -> dict[str, Any] | None:
    row = get_engine_row(engine_id)
    return engine_to_api(row) if row else None


def update_engine(engine_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
    ensure_schema()
    current = get_engine_row(engine_id)
    if not current:
        raise LookupError("执行引擎不存在")
    if engine_id not in BUILTIN_EXECUTION_ENGINES:
        raise ExecutionEngineError("目前只能配置内置的 Codex 和 Claude Code")

    spec = BUILTIN_EXECUTION_ENGINES[engine_id]
    current_api = engine_to_api(current)
    enabled = current_api["enabled"] if changes.get("enabled") is None else bool(changes.get("enabled"))
    base_url = current_api["base_url"] if changes.get("base_url") is None else str(changes.get("base_url") or "").strip()
    mode = str(changes.get("api_key_mode") or current_api.get("api_key_mode") or "env")
    if mode not in {"env", "direct"}:
        raise ExecutionEngineError("密钥方式必须是环境变量或直接填写")

    encrypted = str(current.get("api_key_ciphertext") or "")
    api_key_env = str(current_api.get("api_key_env") or spec["default_api_key_env"])
    if mode == "env":
        encrypted = ""
        if changes.get("api_key_env") is not None:
            api_key_env = str(changes.get("api_key_env") or "").strip()
        if not is_env_name(api_key_env):
            raise ExecutionEngineError("环境变量模式必须填写合法变量名，例如 OPENAI_API_KEY")
    else:
        api_key_env = ""
        raw_key = changes.get("api_key")
        if raw_key:
            encrypted = secret_store.encrypt(str(raw_key))
        elif not encrypted:
            raise ExecutionEngineError("直接填写密钥时必须提供 API Key")

    config = dict(current_api.get("config") or {})
    if changes.get("config") is not None:
        incoming = changes.get("config") or {}
        if not isinstance(incoming, Mapping):
            raise ExecutionEngineError("高级配置必须是对象")
        config = dict(incoming)
    allowed_roles = str(changes.get("allowed_roles") or current_api.get("allowed_roles") or "admin,user").strip()

    if changes.get("model") is not None:
        model_name = str(changes.get("model") or "").strip()
        if model_name:
            config["model"] = model_name
        else:
            config.pop("model", None)

    db.execute(
        """
        UPDATE execution_engines
        SET enabled = ?, base_url = ?, api_key_env = ?, api_key_ciphertext = ?,
            config_json = ?, allowed_roles = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            1 if enabled else 0,
            base_url,
            api_key_env if mode == "env" else "",
            encrypted,
            db.json_dumps(config),
            allowed_roles,
            db.utc_now(),
            engine_id,
        ),
    )
    updated = get_engine_row(engine_id) or current
    return engine_to_api(updated)


def configured_model_name(row: Mapping[str, Any] | None) -> str:
    if not row:
        return ""
    config = db.json_loads(row.get("config_json"), {})
    if isinstance(config, dict):
        return str(config.get("model") or "").strip()
    return ""


def engine_is_enabled(engine: str) -> bool:
    engine_id = normalize_engine_id(engine)
    if engine_id in {"builtin", "container"}:
        return True
    row = get_engine_row(engine_id)
    if not row:
        return engine_id in BUILTIN_EXECUTION_ENGINES
    return bool(row.get("enabled"))


def resolve_runtime_env(engine: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Merge host env, admin-saved engine config, then explicit overrides."""
    env: dict[str, str] = {}
    for key in ENGINE_CREDENTIAL_ENV_KEYS:
        value = os.getenv(key)
        if value:
            env[key] = value

    engine_id = normalize_engine_id(engine)
    target_ids = ("codex", "claude") if engine_id == "container" else ((engine_id,) if engine_id in BUILTIN_EXECUTION_ENGINES else ())
    for target_id in target_ids:
        row = get_engine_row(target_id)
        if not row:
            continue
        _apply_saved_credentials(env, row)

    if extra:
        for key, value in extra.items():
            if key and value:
                env[str(key)] = str(value)
    return env


def _apply_saved_credentials(env: dict[str, str], row: Mapping[str, Any]) -> None:
    engine_id = str(row.get("id") or "")
    spec = BUILTIN_EXECUTION_ENGINES.get(engine_id) or {}
    ciphertext = str(row.get("api_key_ciphertext") or "")
    if ciphertext:
        secret = secret_store.decrypt(ciphertext)
        for key in spec.get("primary_key_envs", ()):
            env[key] = secret
    else:
        env_name = str(row.get("api_key_env") or "").strip()
        if is_env_name(env_name):
            value = os.getenv(env_name)
            if value:
                for key in spec.get("primary_key_envs", ()):
                    env[key] = value
    base_url = str(row.get("base_url") or "").strip()
    if base_url:
        for key in spec.get("base_url_envs", ()):
            env[key] = base_url


def require_engine_ready(engine: str) -> dict[str, Any] | None:
    engine_id = normalize_engine_id(engine)
    if engine_id not in BUILTIN_EXECUTION_ENGINES:
        return None
    row = get_engine_row(engine_id)
    if not row:
        return None
    if not row.get("enabled"):
        raise ExecutionEngineError("该执行引擎已停用，请由管理员在执行引擎页面启用")
    public = engine_to_api(row)
    if public["readiness"]["state"] == "needs_config":
        raise ExecutionEngineError(public["readiness"]["detail"])
    return row


async def test_engine_connection(engine_id: str) -> dict[str, Any]:
    import uuid
    engine_norm = normalize_engine_id(engine_id)
    row = get_engine_row(engine_norm)
    if not row:
        raise LookupError("执行引擎不存在")
    if engine_norm not in BUILTIN_EXECUTION_ENGINES:
        raise ExecutionEngineError("目前仅支持测试 Codex 和 Claude Code")

    from app.services.container_agent_runner import default_container_runner
    docker_ok = await default_container_runner.check_docker_available()
    if not docker_ok:
        raise ExecutionEngineError("Docker 守护进程不可用，请确保 Docker 服务已启动。")

    res = await default_container_runner.execute_task(
        task_id=f"test-conn-{uuid.uuid4().hex[:8]}",
        run_id=f"conn-{uuid.uuid4().hex[:6]}",
        prompt="连接测试：请简短回复 OK。",
        engine=engine_norm,
        timeout_seconds=40,
    )
    if res.status != "completed" or res.exit_code != 0:
        err_msg = res.stderr or res.summary or "执行失败"
        raise ExecutionEngineError(f"引擎测试失败 (退出码 {res.exit_code})：{err_msg[:600]}")

    return {
        "ok": True,
        "engine_id": engine_norm,
        "response": res.stdout.strip() or "连接成功",
        "duration": round(res.duration, 2),
    }
