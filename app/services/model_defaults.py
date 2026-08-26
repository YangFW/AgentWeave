"""Resolve the platform's default model without hard-coding one provider.

The offline model remains the safe fallback for a fresh, unconfigured install.
Once the user has configured an enabled model, the newest configured model is
preferred for the default assistant and workspace.  Explicit per-task model
selection still wins at the API/UI boundary.
"""

from __future__ import annotations

import os
from typing import Any

from app import db


def configured_default_model_id(fallback: str = "deterministic") -> str:
    """Return the best enabled configured model, or the offline fallback.

    A successful connection test is preferred, followed by models with a
    configured credential.  This keeps a newly added but untested model from
    unexpectedly becoming the default when a known-good model exists.
    """

    try:
        rows = db.query_all(
            """
            SELECT id, api_key_env, api_key_ciphertext, last_test_status, updated_at
            FROM model_configs
            WHERE enabled = 1 AND id <> 'deterministic'
            ORDER BY
                CASE WHEN last_test_status = 'pass' THEN 0 ELSE 1 END,
                updated_at DESC,
                id
            """
        )
    except Exception:
        # Startup migrations and isolated unit tests may call this helper
        # before the model table exists.  The deterministic fallback is valid
        # in that state and avoids making startup depend on optional config.
        return fallback

    for row in rows:
        model_id = str(row.get("id") or "").strip()
        if not model_id:
            continue
        env_name = str(row.get("api_key_env") or "").strip()
        has_credential = bool(row.get("api_key_ciphertext")) or bool(
            env_name and os.getenv(env_name)
        )
        if row.get("last_test_status") == "pass" or has_credential:
            return model_id
    return fallback
