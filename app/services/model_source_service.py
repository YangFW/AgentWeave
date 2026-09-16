from __future__ import annotations

import logging
import os
import time
from typing import Any
import httpx

from app import db
from app.services.secret_store import secret_store

logger = logging.getLogger(__name__)


def _source_to_api(row: dict[str, Any]) -> dict[str, Any]:
    models = db.json_loads(row.get("models_json"), [])
    config = db.json_loads(row.get("config_json"), {})
    has_key = bool(row.get("api_key_ciphertext") or row.get("api_key_env"))
    return {
        "id": row["id"],
        "name": row["name"],
        "provider": row.get("provider") or "openai_compatible",
        "base_url": row.get("base_url") or "",
        "api_key_env": row.get("api_key_env") or "",
        "has_api_key": has_key,
        "models": models,
        "default_model": row.get("default_model") or (models[0]["id"] if models else ""),
        "enabled": bool(row.get("enabled", 1)),
        "allowed_roles": row.get("allowed_roles") or "admin,user",
        "config": config,
        "last_test_status": row.get("last_test_status") or "",
        "last_test_message": row.get("last_test_message") or "",
        "last_test_at": row.get("last_test_at") or "",
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


def list_sources(user_role: str = "user") -> list[dict[str, Any]]:
    rows = db.query_all("SELECT * FROM model_sources ORDER BY created_at DESC")
    result = []
    for r in rows:
        allowed = [role.strip() for role in (r.get("allowed_roles") or "admin,user").split(",") if role.strip()]
        if user_role == "admin" or user_role in allowed:
            result.append(_source_to_api(r))
    return result


def get_source(source_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM model_sources WHERE id = ?", (source_id,))
    if not row:
        return None
    return _source_to_api(row)


def sync_source_to_model_configs(source_id: str) -> None:
    source = db.query_one("SELECT * FROM model_sources WHERE id = ?", (source_id,))
    if not source:
        return
    models = db.json_loads(source.get("models_json"), [])
    now = db.utc_now()
    src_enabled = bool(source.get("enabled", 1))
    src_roles = source.get("allowed_roles") or "admin,user"
    base_url = source.get("base_url") or ""
    ciphertext = source.get("api_key_ciphertext") or ""
    api_key_env = source.get("api_key_env") or ""
    provider = source.get("provider") or "openai_compatible"
    config_json = source.get("config_json") or "{}"

    valid_comp_ids = set()
    for m in models:
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        valid_comp_ids.add(f"{source_id}::{mid}")
        mname = str(m.get("name") or mid).strip()
        m_enabled = 1 if (src_enabled and m.get("enabled", True)) else 0
        display_name = f"[{source['name']}] {mname}"

        # 1. Composite ID e.g. "source-01::gpt-5.5"
        comp_id = f"{source_id}::{mid}"
        existing = db.query_one("SELECT id FROM model_configs WHERE id = ?", (comp_id,))
        if existing:
            db.execute(
                """
                UPDATE model_configs
                SET name = ?, provider = ?, model = ?, base_url = ?,
                    api_key_env = ?, api_key_ciphertext = ?, enabled = ?,
                    allowed_roles = ?, config_json = ?, source_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (display_name, provider, mid, base_url, api_key_env, ciphertext, m_enabled, src_roles, config_json, source_id, now, comp_id),
            )
        else:
            db.execute(
                """
                INSERT INTO model_configs(
                    id, name, provider, model, base_url, api_key_env, api_key_ciphertext,
                    enabled, allowed_roles, config_json, source_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (comp_id, display_name, provider, mid, base_url, api_key_env, ciphertext, m_enabled, src_roles, config_json, source_id, now, now),
            )

        # 2. Keep bare legacy ID aligned if it belongs to this source or matches
        bare_row = db.query_one("SELECT id, source_id FROM model_configs WHERE id = ?", (mid,))
        if bare_row and bare_row.get("source_id") in ("", source_id):
            db.execute(
                """
                UPDATE model_configs
                SET name = ?, provider = ?, model = ?, base_url = ?,
                    api_key_env = ?, api_key_ciphertext = ?, enabled = ?,
                    allowed_roles = ?, config_json = ?, source_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (display_name, provider, mid, base_url, api_key_env, ciphertext, m_enabled, src_roles, config_json, source_id, now, mid),
            )

    # 3. Clean up deleted composite models for this source
    if valid_comp_ids:
        placeholders = ",".join("?" for _ in valid_comp_ids)
        db.execute(
            f"DELETE FROM model_configs WHERE source_id = ? AND id LIKE ? AND id NOT IN ({placeholders})",
            (source_id, f"{source_id}::%", *valid_comp_ids),
        )
    else:
        db.execute(
            "DELETE FROM model_configs WHERE source_id = ? AND id LIKE ?",
            (source_id, f"{source_id}::%"),
        )


def create_source(payload: dict[str, Any]) -> dict[str, Any]:
    import uuid
    source_id = str(payload.get("id") or "").strip() or f"source-{uuid.uuid4().hex[:8]}"
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("模型源名称不能为空")
    provider = str(payload.get("provider") or "openai_compatible").strip()
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("Base URL 不能为空")
    api_key_env = str(payload.get("api_key_env") or "").strip()
    api_key_plain = str(payload.get("api_key") or "").strip()
    ciphertext = ""
    if api_key_plain:
        ciphertext = secret_store.encrypt(api_key_plain)

    models = payload.get("models") or []
    clean_models = []
    for m in models:
        if isinstance(m, str) and m.strip():
            clean_models.append({"id": m.strip(), "name": m.strip(), "enabled": True, "is_default": False})
        elif isinstance(m, dict) and m.get("id"):
            clean_models.append({
                "id": str(m["id"]).strip(),
                "name": str(m.get("name") or m["id"]).strip(),
                "enabled": bool(m.get("enabled", True)),
                "is_default": bool(m.get("is_default", False)),
            })
    models = clean_models
    default_model = str(payload.get("default_model") or "").strip()
    if not default_model and models:
        default_model = models[0]["id"]

    enabled = 1 if payload.get("enabled", True) else 0
    allowed_roles = str(payload.get("allowed_roles") or "admin,user").strip()
    config = payload.get("config") or {}
    now = db.utc_now()

    db.execute(
        """
        INSERT INTO model_sources(
            id, name, provider, base_url, api_key_env, api_key_ciphertext,
            models_json, default_model, enabled, allowed_roles, config_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            name,
            provider,
            base_url,
            api_key_env,
            ciphertext,
            db.json_dumps(models),
            default_model,
            enabled,
            allowed_roles,
            db.json_dumps(config),
            now,
            now,
        ),
    )
    sync_source_to_model_configs(source_id)
    return get_source(source_id) or {}


def update_source(source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = db.query_one("SELECT * FROM model_sources WHERE id = ?", (source_id,))
    if not existing:
        raise LookupError("模型源不存在")

    name = str(payload.get("name") or existing.get("name") or "").strip()
    if not name:
        raise ValueError("模型源名称不能为空")
    provider = str(payload.get("provider") or existing.get("provider") or "openai_compatible").strip()
    base_url = str(payload.get("base_url") or existing.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("Base URL 不能为空")

    api_key_env = payload.get("api_key_env")
    if api_key_env is None:
        api_key_env = existing.get("api_key_env") or ""
    else:
        api_key_env = str(api_key_env).strip()

    ciphertext = existing.get("api_key_ciphertext") or ""
    if "api_key" in payload:
        new_key = str(payload["api_key"] or "").strip()
        if new_key:
            ciphertext = secret_store.encrypt(new_key)

    models = payload.get("models")
    if models is None:
        models = db.json_loads(existing.get("models_json"), [])
    else:
        clean_models = []
        for m in models:
            if isinstance(m, str) and m.strip():
                clean_models.append({"id": m.strip(), "name": m.strip(), "enabled": True, "is_default": False})
            elif isinstance(m, dict) and m.get("id"):
                clean_models.append({
                    "id": str(m["id"]).strip(),
                    "name": str(m.get("name") or m["id"]).strip(),
                    "enabled": bool(m.get("enabled", True)),
                    "is_default": bool(m.get("is_default", False)),
                })
        models = clean_models

    default_model = str(payload.get("default_model") or existing.get("default_model") or "").strip()
    if not default_model and models:
        default_model = models[0]["id"]

    enabled = 1 if payload.get("enabled", bool(existing.get("enabled", 1))) else 0
    allowed_roles = str(payload.get("allowed_roles") or existing.get("allowed_roles") or "admin,user").strip()
    config = payload.get("config") if "config" in payload else db.json_loads(existing.get("config_json"), {})
    now = db.utc_now()

    db.execute(
        """
        UPDATE model_sources
        SET name = ?, provider = ?, base_url = ?, api_key_env = ?,
            api_key_ciphertext = ?, models_json = ?, default_model = ?,
            enabled = ?, allowed_roles = ?, config_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            name,
            provider,
            base_url,
            api_key_env,
            ciphertext,
            db.json_dumps(models),
            default_model,
            enabled,
            allowed_roles,
            db.json_dumps(config),
            now,
            source_id,
        ),
    )
    sync_source_to_model_configs(source_id)
    return get_source(source_id) or {}


def delete_source(source_id: str) -> None:
    db.execute("DELETE FROM model_sources WHERE id = ?", (source_id,))
    db.execute("DELETE FROM model_configs WHERE source_id = ? AND id LIKE ?", (source_id, f"{source_id}::%"))
    db.execute("UPDATE model_configs SET enabled = 0 WHERE source_id = ?", (source_id,))


async def discover_source_models(
    base_url: str,
    api_key: str | None = None,
    api_key_env: str | None = None,
    source_id: str | None = None,
) -> list[dict[str, Any]]:
    if not api_key:
        if api_key_env and os.getenv(api_key_env):
            api_key = os.getenv(api_key_env, "")
        elif source_id:
            source = db.query_one("SELECT api_key_ciphertext, api_key_env FROM model_sources WHERE id = ?", (source_id,))
            if source:
                if source.get("api_key_ciphertext"):
                    api_key = secret_store.decrypt(source["api_key_ciphertext"])
                elif source.get("api_key_env") and os.getenv(source["api_key_env"]):
                    api_key = os.getenv(source["api_key_env"], "")

    clean_url = (base_url or "").strip().rstrip("/")
    if not clean_url:
        raise ValueError("请提供有效的接口 Base URL")

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    urls_to_try = [f"{clean_url}/models"]
    if not clean_url.endswith("/v1"):
        urls_to_try.append(f"{clean_url}/v1/models")

    last_error: Exception | None = None
    data = None
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for target_url in urls_to_try:
            try:
                resp = await client.get(target_url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    break
                elif resp.status_code in {401, 403}:
                    raise RuntimeError(f"认证失败 (HTTP {resp.status_code})：请检查 API Key 是否填写正确或具有访问权限")
                else:
                    last_error = RuntimeError(f"请求 {target_url} 响应 HTTP {resp.status_code}: {resp.text[:150]}")
            except httpx.RequestError as err:
                last_error = RuntimeError(f"无法连接目标地址 ({target_url})：{err}")

    if data is None:
        if last_error:
            raise last_error
        raise RuntimeError("未获取到模型列表")

    raw_list = data.get("data") if isinstance(data, dict) else None
    if raw_list is None and isinstance(data, dict):
        raw_list = data.get("models")
    if raw_list is None and isinstance(data, list):
        raw_list = data
    if not isinstance(raw_list, list):
        raw_list = []

    result = []
    seen = set()
    for item in raw_list:
        mid = ""
        mname = ""
        if isinstance(item, dict):
            mid = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
            mname = str(item.get("name") or item.get("id") or mid).strip()
        elif isinstance(item, str):
            mid = item.strip()
            mname = mid
        if mid and mid not in seen:
            seen.add(mid)
            result.append({"id": mid, "name": mname})

    result.sort(key=lambda x: x["id"].lower())
    return result


async def test_source_connection_live(
    base_url: str,
    api_key: str | None = None,
    api_key_env: str | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    if source_id and not base_url:
        source = db.query_one("SELECT * FROM model_sources WHERE id = ?", (source_id,))
        if not source:
            raise LookupError("模型源不存在")
        base_url = source.get("base_url") or ""
        if not api_key:
            ciphertext = source.get("api_key_ciphertext") or ""
            api_key = secret_store.decrypt(ciphertext) if ciphertext else ""
        if not api_key_env:
            api_key_env = source.get("api_key_env") or ""

    start = time.time()
    try:
        models = await discover_source_models(
            base_url=base_url,
            api_key=api_key,
            api_key_env=api_key_env,
            source_id=source_id,
        )
        duration = round(time.time() - start, 2)
        msg = f"连接成功，发现 {len(models)} 个可用模型（耗时 {duration} 秒）"
        if source_id:
            now = db.utc_now()
            db.execute(
                "UPDATE model_sources SET last_test_status = 'pass', last_test_message = ?, last_test_at = ?, updated_at = ? WHERE id = ?",
                (msg, now, now, source_id),
            )
        return {"ok": True, "message": msg, "model_count": len(models), "duration": duration}
    except Exception as exc:
        duration = round(time.time() - start, 2)
        msg = f"连接失败：{exc}"
        if source_id:
            now = db.utc_now()
            db.execute(
                "UPDATE model_sources SET last_test_status = 'error', last_test_message = ?, last_test_at = ?, updated_at = ? WHERE id = ?",
                (msg, now, now, source_id),
            )
        return {"ok": False, "message": msg, "duration": duration}


async def test_source_connection(source_id: str) -> dict[str, Any]:
    return await test_source_connection_live(base_url="", source_id=source_id)
