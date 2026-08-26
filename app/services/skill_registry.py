from __future__ import annotations

import re
import os
import hashlib
import mimetypes
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any

from app import db

BUILTIN_SKILLS_DIR = Path(__file__).resolve().parents[1] / "builtin_skills"


def parse_frontmatter(content: str) -> dict[str, Any]:
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    meta: dict[str, Any] = {}
    for raw_line in parts[1].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip().strip('"').strip("'")
        meta[key.strip()] = value
    return meta


def referenced_package_files(content: str) -> list[str]:
    """Find package-relative files a SKILL.md explicitly tells the runtime to read/run."""
    matches = re.findall(
        r"\b((?:scripts|references|rules|assets)/[A-Za-z0-9_./-]+\.(?:py|md|json|sh|js|ts|txt))\b",
        content,
        flags=re.IGNORECASE,
    )
    matches += re.findall(r"\b([A-Z][A-Z0-9_-]{2,}\.(?:md|txt|json))\b", content)
    cleaned: list[str] = []
    for raw in matches:
        value = raw.rstrip(".,;:)]}")
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned[:100]


def skill_to_api(row: dict[str, Any]) -> dict[str, Any]:
    file_rows = db.query_all("SELECT path FROM skill_files WHERE skill_id = ? ORDER BY path", (row["id"],))
    paths = [item["path"] for item in file_rows]
    existing_lower = {path.lower() for path in paths}
    missing = [path for path in referenced_package_files(row.get("content", "")) if path.lower() not in existing_lower]
    return {
        **row,
        "enabled": bool(row.get("enabled")),
        "required_mcps": db.json_loads(row.get("required_mcps"), []),
        "file_count": len(paths),
        "package_missing": missing,
    }


class SkillRegistry:
    @staticmethod
    def _content_payload(
        content: str,
        *,
        fallback_id: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        meta = parse_frontmatter(content)
        # Prefer package metadata because downloaded entry files often share
        # the generic name `SKILL.md`; fall back to a content hash when needed.
        raw_id = str(meta.get("id") or meta.get("name") or fallback_id).strip()
        skill_id = re.sub(r"[^A-Za-z0-9_]+", "_", raw_id.replace("-", "_")).strip("_")
        if skill_id.lower() in {"", "skill", "download", "downloaded_skill"}:
            skill_id = "skill_" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        if not re.fullmatch(r"[A-Za-z0-9_]{2,80}", skill_id):
            raise ValueError("SKILL.md 必须提供合法 id（字母、数字、下划线，2-80 位）")
        return {
            "id": skill_id,
            "name": meta.get("name") or skill_id,
            "description": meta.get("description") or "通过安装包导入的 Skill",
            "category": meta.get("category") or "installed",
            "version": meta.get("version") or "0.1.0",
            "content": content,
            "enabled": enabled,
            "required_mcps": [
                item.strip()
                for item in str(meta.get("required_mcps", "")).split(",")
                if item.strip()
            ],
        }

    @staticmethod
    def _skill_to_api_in_transaction(
        conn: Any, row: Any
    ) -> dict[str, Any]:
        value = dict(row)
        paths = [
            str(item[0])
            for item in conn.execute(
                "SELECT path FROM skill_files WHERE skill_id = ? ORDER BY path",
                (value["id"],),
            ).fetchall()
        ]
        existing_lower = {path.lower() for path in paths}
        missing = [
            path
            for path in referenced_package_files(str(value.get("content") or ""))
            if path.lower() not in existing_lower
        ]
        return {
            **value,
            "enabled": bool(value.get("enabled")),
            "required_mcps": db.json_loads(value.get("required_mcps"), []),
            "file_count": len(paths),
            "package_missing": missing,
        }

    def load_builtin_skills(self) -> None:
        if BUILTIN_SKILLS_DIR.exists():
            for skill_dir in sorted(BUILTIN_SKILLS_DIR.iterdir()):
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    continue
                content = skill_file.read_text(encoding="utf-8")
                meta = parse_frontmatter(content)
                skill_id = meta.get("id") or skill_dir.name
                exists = db.query_one("SELECT id FROM skills WHERE id = ?", (skill_id,))
                if not exists:
                    self.create_skill({
                        "id": skill_id,
                        "name": meta.get("name", skill_id),
                        "description": meta.get("description", ""),
                        "category": meta.get("category", "builtin"),
                        "version": meta.get("version", "0.1.0"),
                        "content": content,
                        "enabled": True,
                        "required_mcps": [x.strip() for x in meta.get("required_mcps", "").split(",") if x.strip()],
                    })
                for package_file in skill_dir.rglob("*"):
                    if not package_file.is_file():
                        continue
                    relative = package_file.relative_to(skill_dir).as_posix()
                    if not self.get_file(skill_id, relative):
                        self.put_file(skill_id, relative, package_file.read_bytes(), sync_skill=False)

        # Skills created by earlier platform versions only lived in the
        # `skills` table. Backfill their entry file so every installed Skill is
        # a real, inspectable/exportable package in the current UI.
        for row in db.query_all("SELECT id, content FROM skills"):
            if not self.get_file(row["id"], "SKILL.md"):
                self.put_file(row["id"], "SKILL.md", str(row.get("content") or "").encode("utf-8"), sync_skill=False)

    def list_skills(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM skills"
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY category, name"
        return [skill_to_api(r) for r in db.query_all(sql, params)]

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        row = db.query_one("SELECT * FROM skills WHERE id = ?", (skill_id,))
        return skill_to_api(row) if row else None

    def create_skill(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = db.utc_now()
        db.execute(
            """
            INSERT INTO skills(id, name, description, category, version, content, enabled, required_mcps, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["id"], payload["name"], payload.get("description", ""), payload.get("category", "custom"),
                payload.get("version", "0.1.0"), payload.get("content", ""), 1 if payload.get("enabled", True) else 0,
                db.json_dumps(payload.get("required_mcps", [])), now, now,
            ),
        )
        self.put_file(payload["id"], "SKILL.md", payload.get("content", "").encode("utf-8"), sync_skill=False)
        return self.get_skill(payload["id"]) or payload

    def install_content(self, content: str, fallback_id: str = "", enabled: bool = True) -> dict[str, Any]:
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            installed = self.install_content_in_transaction(
                conn,
                content,
                fallback_id=fallback_id,
                enabled=enabled,
            )
            conn.commit()
            return installed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def install_content_in_transaction(
        self,
        conn: Any,
        content: str,
        fallback_id: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Install one entry file on a caller-owned SQLite transaction.

        Agent platform commands use this path so the registry mutation and the
        Task/Run terminal publication share one linearization point.  The
        method deliberately never commits or opens another connection.
        """

        payload = self._content_payload(
            content, fallback_id=fallback_id, enabled=enabled
        )
        skill_id = str(payload["id"])
        now = db.utc_now()
        current = conn.execute(
            "SELECT id FROM skills WHERE id = ?", (skill_id,)
        ).fetchone()
        if current is None:
            conn.execute(
                """
                INSERT INTO skills(
                    id, name, description, category, version, content, enabled,
                    required_mcps, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_id,
                    payload["name"],
                    payload["description"],
                    payload["category"],
                    payload["version"],
                    content,
                    1 if enabled else 0,
                    db.json_dumps(payload["required_mcps"]),
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE skills
                SET name = ?, description = ?, category = ?, version = ?,
                    content = ?, enabled = ?, required_mcps = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload["name"],
                    payload["description"],
                    payload["category"],
                    payload["version"],
                    content,
                    1 if enabled else 0,
                    db.json_dumps(payload["required_mcps"]),
                    now,
                    skill_id,
                ),
            )
        raw = content.encode("utf-8")
        conn.execute(
            """
            INSERT INTO skill_files(
                skill_id, path, content, content_type, is_binary, size, updated_at
            ) VALUES (?, 'SKILL.md', ?, 'text/markdown', 0, ?, ?)
            ON CONFLICT(skill_id, path) DO UPDATE SET
                content = excluded.content,
                content_type = excluded.content_type,
                is_binary = excluded.is_binary,
                size = excluded.size,
                updated_at = excluded.updated_at
            """,
            (skill_id, raw, len(raw), now),
        )
        row = conn.execute(
            "SELECT * FROM skills WHERE id = ?", (skill_id,)
        ).fetchone()
        assert row is not None
        return self._skill_to_api_in_transaction(conn, row)

    def install_package(self, files: dict[str, bytes], fallback_id: str = "", enabled: bool = True) -> dict[str, Any]:
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            installed = self.install_package_in_transaction(
                conn,
                files,
                fallback_id=fallback_id,
                enabled=enabled,
            )
            conn.commit()
            return installed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def install_package_in_transaction(
        self,
        conn: Any,
        files: dict[str, bytes],
        fallback_id: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Install a complete package without escaping the caller transaction."""

        normalized = {self._safe_path(path): content for path, content in files.items()}
        skill_paths = [
            path for path in normalized if PurePosixPath(path).name == "SKILL.md"
        ]
        if len(skill_paths) != 1:
            raise ValueError("Skill 包必须且只能包含一个 SKILL.md")
        skill_path = skill_paths[0]
        root = PurePosixPath(skill_path).parent
        package: dict[str, bytes] = {}
        for path, raw in normalized.items():
            pure = PurePosixPath(path)
            try:
                relative = pure.relative_to(root) if str(root) != "." else pure
            except ValueError:
                continue
            safe = self._safe_path(relative.as_posix())
            package[safe] = raw
        if "SKILL.md" not in package:
            raise ValueError("Skill 包缺少 SKILL.md")
        if len(package) > 200:
            raise ValueError("Skill 包文件数不能超过 200")
        if sum(len(raw) for raw in package.values()) > 2 * 1024 * 1024:
            raise ValueError("Skill 包不能超过 2MB")
        if any(len(raw) > 1024 * 1024 for raw in package.values()):
            raise ValueError("单个 Skill 文件不能超过 1MB")
        try:
            content = package["SKILL.md"].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("SKILL.md 必须是 UTF-8 文本") from exc

        skill = self.install_content_in_transaction(
            conn,
            content,
            fallback_id=fallback_id or root.name,
            enabled=enabled,
        )
        skill_id = str(skill["id"])
        now = db.utc_now()
        conn.execute("DELETE FROM skill_files WHERE skill_id = ?", (skill_id,))
        for path, raw in package.items():
            try:
                raw.decode("utf-8")
                is_binary = 0
            except UnicodeDecodeError:
                is_binary = 1
            if path == "SKILL.md" and is_binary:
                raise ValueError("SKILL.md 必须是 UTF-8 文本")
            content_type = mimetypes.guess_type(path)[0] or (
                "application/octet-stream" if is_binary else "text/plain"
            )
            conn.execute(
                """
                INSERT INTO skill_files(
                    skill_id, path, content, content_type, is_binary, size, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_id,
                    path,
                    raw,
                    content_type,
                    is_binary,
                    len(raw),
                    now,
                ),
            )
        row = conn.execute(
            "SELECT * FROM skills WHERE id = ?", (skill_id,)
        ).fetchone()
        assert row is not None
        return self._skill_to_api_in_transaction(conn, row)

    def install_from_path(self, raw_path: str, enabled: bool = True) -> dict[str, Any]:
        roots = [Path(p).expanduser().resolve() for p in os.getenv("APP_SKILL_LOCAL_ROOTS", "").split(os.pathsep) if p]
        if not roots:
            raise ValueError("本地路径安装未启用，请配置 APP_SKILL_LOCAL_ROOTS")
        path = Path(raw_path).expanduser().resolve()
        if not any(path == root or root in path.parents for root in roots):
            raise ValueError("路径不在 APP_SKILL_LOCAL_ROOTS 允许范围内")
        skill_file = path / "SKILL.md" if path.is_dir() else path
        if skill_file.name != "SKILL.md" or not skill_file.is_file():
            raise ValueError("路径必须指向 SKILL.md 或包含它的目录")
        if skill_file.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("SKILL.md 不能超过 2MB")
        package_root = skill_file.parent
        files = {
            item.relative_to(package_root).as_posix(): item.read_bytes()
            for item in package_root.rglob("*")
            if item.is_file()
        }
        return self.install_package(files, fallback_id=skill_file.parent.name, enabled=enabled)

    def update_skill(self, skill_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_skill(skill_id)
        if not current:
            return None
        updated = {**current, **{k: v for k, v in payload.items() if v is not None}}
        db.execute(
            """
            UPDATE skills SET name = ?, description = ?, category = ?, version = ?, content = ?, enabled = ?, required_mcps = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                updated["name"], updated["description"], updated["category"], updated["version"], updated["content"],
                1 if updated.get("enabled") else 0, db.json_dumps(updated.get("required_mcps", [])), db.utc_now(), skill_id,
            ),
        )
        if "content" in payload and payload.get("content") is not None:
            self.put_file(skill_id, "SKILL.md", str(updated["content"]).encode("utf-8"), sync_skill=False)
        return self.get_skill(skill_id)

    def list_files(self, skill_id: str) -> list[dict[str, Any]]:
        rows = db.query_all(
            "SELECT path, content_type, is_binary, size, updated_at FROM skill_files WHERE skill_id = ? ORDER BY path",
            (skill_id,),
        )
        return [{**row, "is_binary": bool(row.get("is_binary"))} for row in rows]

    def runtime_content(self, skill_id: str, max_chars: int = 16000) -> str:
        skill = self.get_skill(skill_id)
        if not skill:
            return ""
        parts = [skill.get("content", "")]
        used = len(parts[0])
        for item in self.list_files(skill_id):
            if item["path"] == "SKILL.md" or item.get("is_binary") or used >= max_chars:
                continue
            file = self.get_file(skill_id, item["path"])
            content = str((file or {}).get("content") or "")
            remaining = max_chars - used
            excerpt = content[:remaining]
            parts.append(f"\n### Package file: {item['path']}\n{excerpt}")
            used += len(excerpt)
        return "\n".join(parts)

    def package_hash(self, skill_id: str) -> str:
        """Hash the complete installed package, including binary assets.

        Runtime prompt excerpts are intentionally bounded and therefore cannot
        serve as an immutable capability identity.  The length-prefixed package
        encoding avoids path/content boundary ambiguity and is stable across
        database reads.
        """

        if not self.get_skill(skill_id):
            raise ValueError("Skill not found")
        rows = db.query_all(
            "SELECT path, content FROM skill_files WHERE skill_id = ? ORDER BY path",
            (skill_id,),
        )
        digest = hashlib.sha256()
        for row in rows:
            path = str(row.get("path") or "").encode("utf-8")
            content = row.get("content") or b""
            if isinstance(content, str):
                content = content.encode("utf-8")
            digest.update(len(path).to_bytes(8, "big"))
            digest.update(path)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return digest.hexdigest()

    def get_file(self, skill_id: str, path: str) -> dict[str, Any] | None:
        safe = self._safe_path(path)
        row = db.query_one("SELECT * FROM skill_files WHERE skill_id = ? AND path = ?", (skill_id, safe))
        if not row:
            return None
        raw = row.get("content") or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return {
            **row,
            "is_binary": bool(row.get("is_binary")),
            "content": None if row.get("is_binary") else raw.decode("utf-8", errors="replace"),
        }

    def put_file(self, skill_id: str, path: str, content: bytes, sync_skill: bool = True) -> dict[str, Any]:
        if not self.get_skill(skill_id):
            raise ValueError("Skill not found")
        safe = self._safe_path(path)
        if len(content) > 1024 * 1024:
            raise ValueError("单个 Skill 文件不能超过 1MB")
        try:
            content.decode("utf-8")
            is_binary = 0
        except UnicodeDecodeError:
            is_binary = 1
        content_type = mimetypes.guess_type(safe)[0] or ("application/octet-stream" if is_binary else "text/plain")
        now = db.utc_now()
        db.execute(
            "INSERT INTO skill_files(skill_id, path, content, content_type, is_binary, size, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(skill_id, path) DO UPDATE SET content=excluded.content, content_type=excluded.content_type, is_binary=excluded.is_binary, size=excluded.size, updated_at=excluded.updated_at",
            (skill_id, safe, content, content_type, is_binary, len(content), now),
        )
        if safe == "SKILL.md" and sync_skill:
            if is_binary:
                raise ValueError("SKILL.md 必须是 UTF-8 文本")
            db.execute("UPDATE skills SET content = ?, updated_at = ? WHERE id = ?", (content.decode("utf-8"), now, skill_id))
        return self.get_file(skill_id, safe) or {"path": safe}

    def delete_file(self, skill_id: str, path: str) -> None:
        safe = self._safe_path(path)
        if safe == "SKILL.md":
            raise ValueError("SKILL.md 是 Skill 包入口文件，不能删除")
        if not self.get_file(skill_id, safe):
            raise ValueError("Skill file not found")
        db.execute("DELETE FROM skill_files WHERE skill_id = ? AND path = ?", (skill_id, safe))

    def replace_package(self, skill_id: str, files: dict[str, bytes]) -> None:
        if "SKILL.md" not in files:
            raise ValueError("Skill 包缺少 SKILL.md")
        if len(files) > 200:
            raise ValueError("Skill 包文件数不能超过 200")
        if sum(len(content) for content in files.values()) > 2 * 1024 * 1024:
            raise ValueError("Skill 包不能超过 2MB")
        db.execute("DELETE FROM skill_files WHERE skill_id = ?", (skill_id,))
        for path, content in files.items():
            self.put_file(skill_id, path, content, sync_skill=(path == "SKILL.md"))

    def delete_package(self, skill_id: str) -> None:
        db.execute("DELETE FROM skill_files WHERE skill_id = ?", (skill_id,))

    def _safe_path(self, path: str) -> str:
        value = str(path or "").replace("\\", "/").strip("/")
        pure = PurePosixPath(value)
        if not value or pure.is_absolute() or ".." in pure.parts or len(value) > 240:
            raise ValueError("Skill 文件路径不合法")
        return pure.as_posix()

    def score_skills(self, message: str, allowed_ids: list[str] | None = None) -> list[dict[str, Any]]:
        skills = self.list_skills(enabled_only=True)
        if allowed_ids:
            allowed = set(allowed_ids)
            skills = [s for s in skills if s["id"] in allowed]
        normalized = message.lower()
        # Negative constraints describe capabilities that must not be used;
        # treating their nouns as positive keywords inverts user intent (for
        # example “不要调用天气工具” used to select tool-backed Skills).
        normalized = re.sub(
            r"(?:不要|不用|无需|禁止|不必|别再|请勿)[^。！？!?；;\n]*",
            " ",
            normalized,
        )
        ascii_tokens = set(re.findall(r"[a-zA-Z0-9_\-]{2,}", normalized))
        chinese_chunks = re.findall(r"[\u4e00-\u9fff]+", normalized)
        stop_grams = {"这个", "那个", "一下", "帮我", "用户", "使用", "进行", "要求", "任务", "问题", "可以", "需要", "当前", "输出"}
        chinese_grams: dict[str, float] = {}
        for chunk in chinese_chunks:
            if 2 <= len(chunk) <= 8:
                chinese_grams[chunk] = 3.0
            for size, weight in ((2, 0.7), (3, 1.0), (4, 1.3)):
                for index in range(max(0, len(chunk) - size + 1)):
                    token = chunk[index:index + size]
                    if token not in stop_grams:
                        chinese_grams[token] = max(chinese_grams.get(token, 0.0), weight)
        results = []
        for skill in skills:
            text = " ".join([skill["id"], skill["name"], skill["description"], skill["content"][:2000]]).lower()
            score = 0.0
            for token in ascii_tokens:
                if token and token in text:
                    score += 1.0
            for token, weight in chinese_grams.items():
                if token in text:
                    score += weight
            bonus_map = {
                "report_generation": ["报告", "汇报", "总结", "markdown", "word", "pdf", "excel"],
                "general_task": ["分析", "查询", "帮我", "任务"],
                "word_document": ["word", "docx", "word文档"],
                "excel_workbook": ["excel", "xlsx", "电子表格"],
                "powerpoint_presentation": ["ppt", "pptx", "powerpoint", "幻灯片", "演示文稿"],
                "markdown_document": ["markdown", "md文件"],
                "html_document": ["html", "网页文档"],
            }
            for keyword in bonus_map.get(skill["id"], []):
                if keyword.lower() in normalized:
                    score += 5.0
            if skill["id"] == "report_generation":
                report_request = bool(re.search(
                    r"(?:生成|创建|制作|导出|下载|写|整理|做|出).{0,12}(?:报告|汇报|文档|pdf|word|docx|markdown|excel)"
                    r"|(?:报告|汇报|文档|pdf|word|docx|markdown|excel).{0,12}(?:生成|创建|制作|导出|下载|写|整理|做|出)",
                    normalized,
                ))
                if not report_request:
                    score = 0.0
            specialty_keyword_guards = {
                "mermaid_diagram": ["mermaid", "流程图", "时序图", "架构图", "状态图", "关系图"],
                "product_requirement_document": ["prd", "产品需求文档", "用户故事", "验收标准"],
                "research_report": [
                    "联网", "搜索", "最新", "调研", "研究报告", "竞品", "行业研究",
                    "来源", "引用", "参考链接", "新闻", "web search",
                ],
                "context_parameter_guard": [
                    "这个项目", "这个任务", "刚才", "前面", "之前", "按原来的",
                    "按刚才", "阈值", "把它导出", "继续生成", "继续导出",
                ],
                "data_analysis": [
                    "数据分析", "分析数据", "数据集", "表格", "excel", "xlsx",
                    "csv", "统计", "趋势", "指标", "图表", "可视化",
                ],
                "meeting_minutes": [
                    "会议纪要", "会议记录", "行动项", "待办事项", "参会人",
                    "会议录音", "会议内容", "议程",
                ],
                # Planning is an opt-in capability.  Its Skill declares a
                # sequential-thinking MCP, so matching it on generic words
                # such as “问题” or “方案” would unnecessarily bind a tool
                # that simple summaries and lookups do not need.  Keep the
                # trigger set focused on genuinely complex reasoning asks.
                "complex_problem_planning": [
                    "复杂问题", "复杂方案", "方案比较", "方案对比", "架构决策",
                    "架构设计", "根因排查", "故障排查", "多约束", "技术选型",
                    "决策分析", "权衡利弊", "trade-off", "tradeoff", "root cause",
                ],
            }
            if skill["id"] in specialty_keyword_guards and not any(
                keyword in normalized for keyword in specialty_keyword_guards[skill["id"]]
            ):
                score = 0.0
            format_guards = {
                "word_document": ["word", "docx", "word文档"],
                "docx": ["word", "docx", "word文档"],
                "excel_workbook": ["excel", "xlsx", "电子表格"],
                "xlsx": ["excel", "xlsx", "电子表格", "csv", "tsv"],
                "powerpoint_presentation": ["ppt", "pptx", "powerpoint", "幻灯片", "演示文稿"],
                "pptx": ["ppt", "pptx", "powerpoint", "幻灯片", "演示文稿"],
                "markdown_document": ["markdown", "md文件"],
                "html_document": ["html", "网页文档"],
                "pdf": ["pdf"],
            }
            if skill["id"] in format_guards and not any(k in normalized for k in format_guards[skill["id"]]):
                score = 0.0
            # A single shared Chinese bigram (0.7) is common across unrelated
            # skills and must not grant that Skill's MCP permissions.  Require
            # at least one stronger semantic signal or an explicit ASCII token.
            if score >= 1.0:
                results.append({"skill": skill, "score": round(score, 3)})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:5]
