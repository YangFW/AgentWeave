from __future__ import annotations

import json
import re
from typing import Any

from app.services.verification_service import (
    CandidateOutput,
    EvidenceBundle,
    RuleResult,
    SemanticResult,
)


class ModelSemanticJudge:
    """Independent, structured semantic review backed by a configured model.

    The adapter deliberately performs a separate model request from generation.
    It returns only the public verdict contract; raw prompts, provider errors and
    model working text never become part of a VerificationReport.
    """

    MAX_ARTIFACT_TEXT = 30_000
    MAX_ANSWER_TEXT = 60_000

    def __init__(self, model_gateway: Any, model_id: str) -> None:
        model_id = str(model_id or "").strip()
        if not model_id or model_id == "deterministic":
            raise ValueError("语义验收器必须使用已配置的非确定性模型")
        self.model_gateway = model_gateway
        self.model_id = model_id

    async def evaluate(
        self,
        *,
        candidate: CandidateOutput,
        evidence: EvidenceBundle,
        rule_results: tuple[RuleResult, ...],
    ) -> SemanticResult | dict[str, Any]:
        payload = {
            "objective": evidence.objective,
            "goal_parameters": evidence.goal_parameters,
            "candidate": {
                "answer": candidate.answer[: self.MAX_ANSWER_TEXT],
                "artifacts": [
                    {
                        "name": item.name,
                        "kind": item.kind,
                        "download_url": item.download_url,
                        "content_text": item.content_text[: self.MAX_ARTIFACT_TEXT],
                    }
                    for item in candidate.artifacts
                ],
            },
            "deterministic_rule_results": [
                {
                    "id": item.id,
                    "status": item.status,
                    "public_reason": item.public_reason,
                }
                for item in rule_results
            ],
        }
        prompt = (
            "请独立验收候选结果是否真正完成当前目标。重点核对主题、实体、时间、"
            "参数、交付物和上下文指代是否一致；非空但跑题必须判定 failed。\n"
            "只输出一个 JSON 对象，不要 Markdown，不要分析过程。对象只能包含："
            "status（passed 或 failed）、public_reason（面向用户的一句话）、"
            "repair_instructions（失败时给出简短可执行修改项数组，通过时为空数组）。\n\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        last_error: Exception | None = None
        validated: SemanticResult | None = None
        for _attempt in range(2):
            try:
                raw = await self.model_gateway.summarize(
                    prompt,
                    {
                        "system_prompt": (
                            "你是 AgentNexus 的独立最终验收器，不参与生成任务。"
                            "只做目标一致性判定，禁止输出思维链或额外字段。"
                        )
                    },
                    model_config_id=self.model_id,
                )
                parsed = self._json_object(str(raw or ""))
                if parsed.get("status") not in {"passed", "failed"}:
                    raise ValueError("语义验收器必须返回 passed 或 failed")
                # Validate the public shape before accepting the response so
                # one malformed provider response can be retried just like a
                # malformed JSON response. Unknown fields remain forbidden.
                validated = SemanticResult.model_validate(parsed)
                break
            except Exception as exc:
                last_error = exc
        if validated is None:
            raise ValueError("语义验收器连续两次未返回可校验结果") from last_error
        return validated.model_dump(mode="json")

    @staticmethod
    def _json_object(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("语义验收器未返回 JSON 对象")
        value = json.loads(text[start : end + 1])
        if not isinstance(value, dict):
            raise ValueError("语义验收结果必须是 JSON 对象")
        # OpenAI-compatible providers occasionally collapse a one-item array
        # into a string even when the contract asks for an array.  Normalize
        # only this public field; unknown fields remain untouched and are still
        # rejected by SemanticResult's strict schema.
        repairs = value.get("repair_instructions")
        if isinstance(repairs, str):
            value["repair_instructions"] = [repairs] if repairs.strip() else []
        elif repairs is None:
            value["repair_instructions"] = []
        status = value.get("status")
        if isinstance(status, str):
            status_aliases = {
                "pass": "passed",
                "success": "passed",
                "通过": "passed",
                "fail": "failed",
                "failure": "failed",
                "未通过": "failed",
            }
            value["status"] = status_aliases.get(status.strip().lower(), status.strip().lower())
        return value


__all__ = ["ModelSemanticJudge"]
