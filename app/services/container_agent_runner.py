from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping

from app import db
from app.services.event_bus import emit
from app.services.mcp_gateway import ARTIFACT_DIR
from app.services.execution_engine_service import (
    ExecutionEngineError,
    configured_model_name,
    get_engine_row,
    normalize_engine_id,
    require_engine_ready,
    resolve_runtime_env,
)
from app.services.workspace_path_manager import (
    WorkspacePathManager,
    WorkspacePaths,
    default_path_manager,
)

logger = logging.getLogger(__name__)


def get_runner_idle_seconds() -> int:
    try:
        from app import db
        row = db.query_one("SELECT value FROM system_settings WHERE key = 'runner_idle_seconds'")
        if row and str(row.get("value") or "").strip():
            return max(0, int(str(row["value"]).strip()))
    except Exception:
        pass
    return int(os.getenv("APP_RUNNER_IDLE_SECONDS", "300"))


def set_runner_idle_seconds(seconds: int) -> int:
    val = max(0, int(seconds))
    try:
        from app import db
        now = db.utc_now()
        db.execute(
            """
            INSERT INTO system_settings(key, value, updated_at) VALUES ('runner_idle_seconds', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (str(val), now),
        )
    except Exception as e:
        logger.warning("更新 system_settings runner_idle_seconds 失败: %s", e)
    return val


@dataclass
class ContainerRunnerConfig:
    image_name: str = field(
        default_factory=lambda: os.getenv("APP_RUNNER_IMAGE", "agentnexus-runner:latest")
    )
    cpu_limit: str = field(
        default_factory=lambda: os.getenv("APP_RUNNER_CPUS", "2.0")
    )
    memory_limit: str = field(
        default_factory=lambda: os.getenv("APP_RUNNER_MEMORY", "4g")
    )
    pids_limit: int = 256
    default_timeout: int = 600
    container_workspace: str = "/workspace"
    container_home: str = "/home/node"
    idle_timeout: int = field(default_factory=get_runner_idle_seconds)


@dataclass
class ContainerExecutionResult:
    exit_code: int
    status: str
    stdout: str
    stderr: str
    summary: str
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    duration: float = 0.0
    session_id: str = ""


class ContainerAgentRunner:
    def __init__(
        self,
        config: ContainerRunnerConfig | None = None,
        path_manager: WorkspacePathManager | None = None,
    ) -> None:
        self.config = config or ContainerRunnerConfig()
        self.path_manager = path_manager or default_path_manager
        self._running_containers: dict[str, asyncio.subprocess.Process] = {}
        self._warm_containers: dict[str, float] = {}
        self._active_tasks_count: dict[str, int] = {}
        self._reaper_task: asyncio.Task | None = None

    async def check_docker_available(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return (await proc.wait()) == 0
        except (OSError, FileNotFoundError):
            return False

    def build_engine_command(
        self,
        engine: str,
        prompt: str,
        *,
        command_override: str | list[str] | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        if command_override:
            if isinstance(command_override, list):
                return command_override
            return ["/bin/bash", "-c", command_override]

        engine_id = normalize_engine_id(engine)
        model_name = ""
        row = get_engine_row(engine_id) if engine_id in {"codex", "claude"} else None
        if row:
            model_name = configured_model_name(row)
        if engine_id == "codex":
            if resume_session_id:
                command = [
                    "codex",
                    "exec",
                    "resume",
                    resume_session_id,
                    "--skip-git-repo-check",
                    "--dangerously-bypass-approvals-and-sandbox",
                ]
            else:
                command = [
                    "codex",
                    "exec",
                    "--skip-git-repo-check",
                    "--dangerously-bypass-approvals-and-sandbox",
                ]
            if row:
                base_url = str(row.get("base_url") or "").strip()
                if base_url:
                    command.extend([
                        "-c", 'model_provider="custom"',
                        "-c", 'model_providers.custom.name="custom"',
                        "-c", f'model_providers.custom.base_url="{base_url}"',
                        "-c", 'model_providers.custom.env_key="OPENAI_API_KEY"',
                        "-c", 'model_providers.custom.wire_api="responses"',
                        "-c", 'model_providers.custom.requires_openai_auth=false',
                    ])
            if model_name:
                command.extend(["-m", model_name])
            command.append(prompt)
            return command
        if engine_id == "claude":
            command = ["claude", "-p", prompt, "--dangerously-skip-permissions"]
            if resume_session_id:
                command.extend(["--resume", resume_session_id])
            if model_name:
                command.extend(["--model", model_name])
            return command
        return ["/bin/bash", "-c", prompt]

    def build_docker_run_args(
        self,
        container_name: str,
        paths: WorkspacePaths,
        env_vars: Mapping[str, str],
        engine_cmd: list[str],
        *,
        daemon: bool = False,
    ) -> list[str]:
        uid = os.getuid()
        gid = os.getgid()

        args = ["docker", "run"]
        if daemon:
            args.append("-d")
        else:
            args.append("--rm")
        args.extend([
            "--name", container_name,
            f"--user={uid}:{gid}",
            f"--cpus={self.config.cpu_limit}",
            f"--memory={self.config.memory_limit}",
            f"--pids-limit={self.config.pids_limit}",
            f"--workdir={self.config.container_workspace}",
        ])

        allow_outbound = os.getenv("APP_ALLOW_OUTBOUND_NETWORK", "").lower() in ("true", "1", "yes")
        if not allow_outbound:
            args.append("--network=none")
        else:
            args.extend(["--add-host", "host.docker.internal:host-gateway"])
            for proxy_var in ("http_proxy", "https_proxy", "no_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"):
                proxy_val = os.getenv(proxy_var)
                if proxy_val:
                    rewritten = (
                        proxy_val.replace("127.0.0.1", "host.docker.internal")
                        .replace("localhost", "host.docker.internal")
                    )
                    args.extend(["-e", f"{proxy_var}={rewritten}"])

        mounts = self.path_manager.get_docker_mounts(
            paths,
            container_workspace=self.config.container_workspace,
            container_home=self.config.container_home,
        )
        for mount in mounts:
            args.extend(["-v", mount])

        for key, val in env_vars.items():
            if val:
                args.extend(["-e", f"{key}={val}"])

        args.extend([
            "-e", f"HOME={self.config.container_home}",
            "-e", "TERM=xterm-256color",
            "-e", "CI=true",
            "-e", "PYTHONUSERBASE=/workspace/.python-user",
            "-e", "PIP_CACHE_DIR=/workspace/.cache/pip",
            "-e", "NPM_CONFIG_CACHE=/workspace/.npm",
            "-e", "NPM_CONFIG_PREFIX=/workspace/.npm-global",
            self.config.image_name,
        ])
        args.extend(engine_cmd)
        return args

    async def is_container_running(self, container_name: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "inspect", "-f", "{{.State.Running}}", container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            return proc.returncode == 0 and out.decode("utf-8").strip().lower() == "true"
        except Exception:
            return False

    async def ensure_warm_container(
        self,
        container_name: str,
        paths: WorkspacePaths,
        env_vars: Mapping[str, str],
    ) -> None:
        if await self.is_container_running(container_name):
            return
        try:
            rm_proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await rm_proc.wait()
        except Exception:
            pass

        daemon_args = self.build_docker_run_args(
            container_name,
            paths,
            env_vars,
            engine_cmd=["sleep", "infinity"],
            daemon=True,
        )
        proc = await asyncio.create_subprocess_exec(
            *daemon_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"启动常驻沙箱容器失败: {err.decode('utf-8', errors='replace')}")

    def _start_reaper_if_needed(self) -> None:
        if not self._reaper_task or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            now = time.time()
            for c_name, last_act in list(self._warm_containers.items()):
                if self._active_tasks_count.get(c_name, 0) == 0:
                    idle_sec = get_runner_idle_seconds()
                    if now - last_act > idle_sec:
                        self._warm_containers.pop(c_name, None)
                        try:
                            rm = await asyncio.create_subprocess_exec(
                                "docker", "rm", "-f", c_name,
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL,
                            )
                            await rm.wait()
                            logger.info("已自动回收空闲超过 %s 秒的容器: %s", self.config.idle_timeout, c_name)
                        except Exception as e:
                            logger.warning("回收空闲容器 %s 失败: %s", c_name, e)

    async def cancel_container(self, container_name: str) -> None:
        try:
            stop_proc = await asyncio.create_subprocess_exec(
                "docker", "stop", "-t", "3", container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await stop_proc.wait()
            rm_proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await rm_proc.wait()
        except Exception as err:
            logger.warning("停止并销毁容器失败 %s: %s", container_name, err)

    async def cleanup_stale_containers(self, *, prefix: str = "nexus-run-") -> int:
        cleaned = 0
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "ps", "-a", "--filter", f"name={prefix}", "--format", "{{.ID}} {{.Names}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0 and stdout:
                for line in stdout.decode("utf-8").strip().split("\n"):
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        cid, name = parts[0], parts[1]
                        if name not in self._running_containers:
                            rm_proc = await asyncio.create_subprocess_exec(
                                "docker", "rm", "-f", cid,
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL,
                            )
                            await rm_proc.wait()
                            cleaned += 1
        except Exception as err:
            logger.warning("清理残留容器失败: %s", err)
        return cleaned

    def _register_artifacts(
        self,
        task_id: str,
        run_id: str,
        workspace_id: str,
        paths: WorkspacePaths,
        start_time: float,
    ) -> list[dict[str, Any]]:
        discovered_files = self.path_manager.collect_new_artifacts(paths, since_timestamp=start_time)
        registered_artifacts: list[dict[str, Any]] = []

        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)

        for file_path in discovered_files:
            try:
                stat = file_path.stat()
                size = stat.st_size
                name = file_path.name
                suffix = file_path.suffix.lstrip(".").lower()
                kind = suffix if suffix else "file"
                mime_type, _ = mimetypes.guess_type(file_path.name)
                mime_type = mime_type or "application/octet-stream"

                sha256 = hashlib.sha256(file_path.read_bytes()).hexdigest()
                artifact_id = f"art_{uuid.uuid4().hex[:16]}"

                dest_path = ARTIFACT_DIR / f"{artifact_id}_{name}"
                shutil.copy2(file_path, dest_path)

                relative_path = str(file_path.relative_to(paths.code_dir)) if paths.code_dir in file_path.parents else name

                now = db.utc_now()
                db.execute(
                    """
                    INSERT INTO artifacts(
                        id, task_id, run_id, workspace_id, name, kind, path,
                        relative_path, mime_type, size, sha256, version,
                        metadata_json, delivery_status, verification_id, published_at,
                        created_at, tool_effect_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        task_id,
                        run_id,
                        workspace_id,
                        name,
                        kind,
                        str(dest_path),
                        relative_path,
                        mime_type,
                        size,
                        sha256,
                        1,
                        json.dumps({"source": "container_runner", "workspace": workspace_id}),
                        "published",
                        "",
                        now,
                        now,
                        "",
                    ),
                )
                registered_artifacts.append({
                    "id": artifact_id,
                    "name": name,
                    "size": size,
                    "kind": kind,
                    "relative_path": relative_path,
                })
            except Exception as err:
                logger.error("登记产物失败 %s: %s", file_path, err)

        return registered_artifacts

    async def execute_task(
        self,
        task_id: str,
        run_id: str,
        prompt: str,
        *,
        engine: str = "codex",
        organization_id: str = "local-org",
        user_id: str = "local-user",
        workspace_id: str = "default",
        env_vars: Mapping[str, str] | None = None,
        command_override: str | list[str] | None = None,
        timeout_seconds: int | None = None,
        is_cancel_requested: Callable[[], bool] | None = None,
        resume_session_id: str | None = None,
    ) -> ContainerExecutionResult:
        start_time = time.time()
        timeout = timeout_seconds or self.config.default_timeout

        paths = self.path_manager.get_paths(organization_id, user_id, workspace_id)
        self.path_manager.ensure_workspace(paths)

        container_name = f"nexus-run-{task_id[:12]}-{run_id[:8]}"
        try:
            require_engine_ready(engine)
        except ExecutionEngineError as err:
            return ContainerExecutionResult(
                exit_code=-1,
                status="failed",
                stdout="",
                stderr=str(err),
                summary=str(err),
                duration=time.time() - start_time,
            )

        engine_id_norm = normalize_engine_id(engine)
        if engine_id_norm == "codex":
            engine_row = get_engine_row("codex")
            if engine_row:
                c_base_url = str(engine_row.get("base_url") or "").strip()
                c_model = configured_model_name(engine_row) or "gpt-5.2"
                if c_base_url:
                    codex_dir = paths.state_dir / ".codex"
                    codex_dir.mkdir(parents=True, exist_ok=True)
                    config_toml = (
                        f'model = "{c_model}"\n'
                        'model_provider = "custom"\n\n'
                        '[model_providers.custom]\n'
                        'name = "custom"\n'
                        f'base_url = "{c_base_url}"\n'
                        'env_key = "OPENAI_API_KEY"\n'
                        'wire_api = "responses"\n'
                        'requires_openai_auth = false\n\n'
                        '[projects."/workspace"]\n'
                        'trust_level = "trusted"\n'
                    )
                    try:
                        (codex_dir / "config.toml").write_text(config_toml, encoding="utf-8")
                    except Exception as err:
                        logger.warning("写入 codex config.toml 失败: %s", err)

        engine_cmd = self.build_engine_command(
            engine,
            prompt,
            command_override=command_override,
            resume_session_id=resume_session_id,
        )

        active_envs = resolve_runtime_env(engine, env_vars)

        idle_sec = get_runner_idle_seconds()
        use_warm = idle_sec > 0
        if use_warm:
            clean_org = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', organization_id)
            clean_usr = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', user_id)
            clean_ws = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', workspace_id)
            clean_eng = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', engine)
            container_name = f"nexus-warm-{clean_org[:8]}-{clean_usr[:8]}-{clean_ws[:12]}-{clean_eng[:8]}"
            await self.ensure_warm_container(container_name, paths, active_envs)
            self._start_reaper_if_needed()
            self._active_tasks_count[container_name] = self._active_tasks_count.get(container_name, 0) + 1

            exec_args = [
                "docker", "exec", "-i",
                f"--user={os.getuid()}:{os.getgid()}",
                f"--workdir={self.config.container_workspace}",
            ]
            for k, v in active_envs.items():
                if v:
                    exec_args.extend(["-e", f"{k}={v}"])
            exec_args.append(container_name)
            exec_args.extend(engine_cmd)
            run_cmd_args = exec_args
            container_mode_desc = f"正在项目 {workspace_id} 的保持容器 ({container_name}) 中执行任务..."
        else:
            container_name = f"nexus-run-{task_id[:12]}-{run_id[:8]}"
            run_cmd_args = self.build_docker_run_args(container_name, paths, active_envs, engine_cmd)
            container_mode_desc = f"正在为项目 {workspace_id} 启动 {engine} 隔离执行容器..."

        emit(
            task_id,
            "stage_started",
            "启动沙箱容器",
            container_mode_desc,
            {
                "engine": engine,
                "workspace_id": workspace_id,
                "container_name": container_name,
                "warm": use_warm,
            },
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        try:
            proc = await asyncio.create_subprocess_exec(
                *run_cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._running_containers[container_name] = proc
        except Exception as err:
            emit(
                task_id,
                "stage_failed",
                "容器启动失败",
                f"创建容器进程失败: {err}",
                {"error": str(err)},
            )
            return ContainerExecutionResult(
                exit_code=-1,
                status="failed",
                stdout="",
                stderr=str(err),
                summary=f"容器启动失败: {err}",
                duration=time.time() - start_time,
            )

        async def read_stream(stream: asyncio.StreamReader, accumulator: list[str], stream_name: str) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
                accumulator.append(decoded)
                emit(
                    task_id,
                    "execution_progress",
                    "执行日志",
                    decoded,
                    {"engine": engine, "stream": stream_name},
                )

        read_stdout_task = asyncio.create_task(read_stream(proc.stdout, stdout_lines, "stdout"))
        read_stderr_task = asyncio.create_task(read_stream(proc.stderr, stderr_lines, "stderr"))

        cancelled = False
        timed_out = False

        while proc.returncode is None:
            if is_cancel_requested and is_cancel_requested():
                cancelled = True
                await self.cancel_container(container_name)
                break
            if time.time() - start_time > timeout:
                timed_out = True
                await self.cancel_container(container_name)
                break
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=0.5)
            except asyncio.TimeoutError:
                pass

        await asyncio.gather(read_stdout_task, read_stderr_task, return_exceptions=True)
        self._running_containers.pop(container_name, None)
        if use_warm:
            self._active_tasks_count[container_name] = max(0, self._active_tasks_count.get(container_name, 1) - 1)
            self._warm_containers[container_name] = time.time()

        exit_code = proc.returncode if proc.returncode is not None else -1
        duration = time.time() - start_time

        stdout_full = "\n".join(stdout_lines)
        stderr_full = "\n".join(stderr_lines)

        if cancelled:
            status = "cancelled"
            summary = f"任务已被用户取消，容器 {container_name} 已终止。"
        elif timed_out:
            status = "timeout"
            summary = f"任务超时（超过 {timeout} 秒），容器 {container_name} 已强制终止。"
        elif exit_code == 0:
            status = "completed"
            summary = stdout_full.strip() if stdout_full.strip() else f"{engine} 引擎执行完成。"
        else:
            status = "failed"
            summary = f"{engine} 执行异常退出 (退出码 {exit_code})。"

        session_id = ""
        match = re.search(r"session id:\s*([a-f0-9\-]+)", stderr_full)
        if match:
            session_id = match.group(1)
        elif resume_session_id:
            session_id = resume_session_id

        artifacts = self._register_artifacts(
            task_id=task_id,
            run_id=run_id,
            workspace_id=workspace_id,
            paths=paths,
            start_time=start_time,
        )

        if status == "completed":
            emit(
                task_id,
                "stage_completed",
                "执行完成",
                summary,
                {
                    "engine": engine,
                    "exit_code": exit_code,
                    "duration": duration,
                    "artifact_count": len(artifacts),
                },
            )
        else:
            emit(
                task_id,
                "stage_failed",
                "执行未完成",
                summary,
                {
                    "engine": engine,
                    "status": status,
                    "exit_code": exit_code,
                    "duration": duration,
                },
            )

        return ContainerExecutionResult(
            exit_code=exit_code,
            status=status,
            stdout=stdout_full,
            stderr=stderr_full,
            summary=summary,
            artifacts=artifacts,
            duration=duration,
            session_id=session_id,
        )


default_container_runner = ContainerAgentRunner()
