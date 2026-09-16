from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


class WorkspacePathError(RuntimeError):
    pass


def _sanitize_path_segment(value: str, field_name: str) -> str:
    if not value or not isinstance(value, str):
        raise WorkspacePathError(f"{field_name} 不能为空")
    cleaned = value.strip()
    if not SAFE_ID_PATTERN.match(cleaned) or ".." in cleaned:
        raise WorkspacePathError(f"{field_name} 包含非法路径字符: {value}")
    return cleaned


@dataclass(frozen=True)
class WorkspacePaths:
    org_id: str
    user_id: str
    workspace_id: str
    root: Path
    code_dir: Path
    state_dir: Path
    artifacts_dir: Path
    logs_dir: Path
    codex_state_dir: Path
    claude_state_dir: Path
    claude_config_file: Path

    @property
    def venv_dir(self) -> Path:
        return self.code_dir / ".venv"

    @property
    def node_modules_dir(self) -> Path:
        return self.code_dir / "node_modules"


class WorkspacePathManager:
    def __init__(self, base_dir: Path | str | None = None) -> None:
        if base_dir is None:
            base_env = os.getenv("APP_WORKSPACES_ROOT")
            if base_env:
                self._base_dir = Path(base_env).resolve()
            else:
                self._base_dir = Path(__file__).resolve().parents[2] / "data" / "workspaces"
        else:
            self._base_dir = Path(base_dir).resolve()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def get_paths(
        self,
        org_id: str | None = "local-org",
        user_id: str | None = "local-user",
        workspace_id: str | None = "default",
    ) -> WorkspacePaths:
        clean_org = _sanitize_path_segment(org_id or "local-org", "org_id")
        clean_user = _sanitize_path_segment(user_id or "local-user", "user_id")
        clean_ws = _sanitize_path_segment(workspace_id or "default", "workspace_id")

        root = self._base_dir / clean_org / clean_user / clean_ws
        code_dir = root / "code"
        state_dir = root / "state"
        artifacts_dir = root / "artifacts"
        logs_dir = root / "logs"
        codex_state_dir = state_dir / ".codex"
        claude_state_dir = state_dir / ".claude"
        claude_config_file = state_dir / ".claude.json"

        return WorkspacePaths(
            org_id=clean_org,
            user_id=clean_user,
            workspace_id=clean_ws,
            root=root,
            code_dir=code_dir,
            state_dir=state_dir,
            artifacts_dir=artifacts_dir,
            logs_dir=logs_dir,
            codex_state_dir=codex_state_dir,
            claude_state_dir=claude_state_dir,
            claude_config_file=claude_config_file,
        )

    def ensure_workspace(self, paths: WorkspacePaths) -> WorkspacePaths:
        for directory in (
            paths.root,
            paths.code_dir,
            paths.state_dir,
            paths.artifacts_dir,
            paths.logs_dir,
            paths.codex_state_dir,
            paths.claude_state_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o750)

        if not paths.claude_config_file.exists():
            paths.claude_config_file.touch(mode=0o640, exist_ok=True)
            paths.claude_config_file.write_text("{}", encoding="utf-8")

        return paths

    def get_docker_mounts(
        self,
        paths: WorkspacePaths,
        *,
        container_workspace: str = "/workspace",
        container_home: str = "/home/node",
    ) -> list[str]:
        self.ensure_workspace(paths)
        return [
            f"{paths.code_dir}:{container_workspace}:rw",
            f"{paths.artifacts_dir}:{container_workspace}/artifacts:rw",
            f"{paths.codex_state_dir}:{container_home}/.codex:rw",
            f"{paths.claude_state_dir}:{container_home}/.claude:rw",
            f"{paths.claude_config_file}:{container_home}/.claude.json:rw",
        ]

    def clean_workspace(self, paths: WorkspacePaths, *, keep_code: bool = True) -> None:
        if not paths.root.exists():
            return
        if not keep_code:
            shutil.rmtree(paths.root, ignore_errors=True)
        else:
            shutil.rmtree(paths.logs_dir, ignore_errors=True)
            paths.logs_dir.mkdir(exist_ok=True, mode=0o750)

    def collect_new_artifacts(
        self,
        paths: WorkspacePaths,
        since_timestamp: float | None = None,
    ) -> list[Path]:
        artifacts: list[Path] = []
        scan_targets = [paths.artifacts_dir, paths.code_dir]
        for target_dir in scan_targets:
            if not target_dir.exists():
                continue
            for file_path in target_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                relative = file_path.relative_to(target_dir)
                parts = relative.parts
                if any(p.startswith(".") or p in ("node_modules", "__pycache__") for p in parts):
                    continue
                if since_timestamp is not None:
                    try:
                        if file_path.stat().st_mtime < since_timestamp:
                            continue
                    except OSError:
                        continue
                artifacts.append(file_path)
        return artifacts


default_path_manager = WorkspacePathManager()
