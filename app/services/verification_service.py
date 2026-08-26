from __future__ import annotations

import json
import re
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


VerificationMode = Literal["rules_only", "semantic_optional", "semantic_required"]
RuleStatus = Literal["passed", "failed"]
SemanticStatus = Literal["passed", "failed", "skipped", "error"]
VerificationVerdict = Literal["passed", "rules_passed", "failed"]

_PUBLIC_REASON_LIMIT = 300
_PUBLIC_INSTRUCTION_LIMIT = 300
_MAX_PUBLIC_INSTRUCTIONS = 12
_MAX_EVIDENCE_ITEMS = 100

_FORMAT_ALIASES = {
    "doc": "docx",
    "word": "docx",
    "powerpoint": "pptx",
    "ppt": "pptx",
    "excel": "xlsx",
    "markdown": "md",
    "htm": "html",
}


def _compact_public_text(value: Any, *, limit: int) -> str:
    """Return a single-line, bounded message safe for a public status event."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        raise ValueError("public text cannot be empty")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _normalise_format(value: str | None) -> str | None:
    if value is None:
        return None
    normalised = value.strip().lower().lstrip(".")
    if not normalised:
        return None
    return _FORMAT_ALIASES.get(normalised, normalised)


def _normalise_string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{field_name} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must contain non-empty strings")
        text = item.strip()
        if text not in result:
            result.append(text)
    if len(result) > _MAX_EVIDENCE_ITEMS:
        raise ValueError(f"{field_name} contains too many items")
    return result


def _validate_json_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be an object")
    result = dict(value)
    if not all(isinstance(key, str) and key.strip() for key in result):
        raise ValueError(f"{field_name} keys must be non-empty strings")
    try:
        json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain JSON-compatible values") from exc
    return result


def _same_json_value(left: Any, right: Any) -> bool:
    """Compare evidence exactly, including JSON scalar types."""

    return json.dumps(
        left, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _short_labels(values: list[str], *, maximum: int = 6) -> str:
    shown = values[:maximum]
    suffix = f"等 {len(values)} 项" if len(values) > maximum else ""
    return "、".join(shown) + suffix


class _StrictPublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactOutput(_StrictPublicModel):
    """Publicly inspectable metadata and bounded extracted text for one artifact."""

    id: str = Field(default="", max_length=160)
    deliverable_id: str = Field(default="", max_length=160)
    task_id: str = Field(default="", max_length=160)
    run_id: str = Field(default="", max_length=160)
    name: str = Field(default="", max_length=500)
    kind: str = Field(default="", max_length=80)
    mime_type: str = Field(default="", max_length=240)
    size: int = Field(default=0, ge=0)
    version: int = Field(default=0, ge=0)
    download_url: str = Field(default="", max_length=2_000)
    content_text: str = Field(default="", max_length=500_000)
    size_bytes: int = Field(default=0, ge=0)
    sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    exists: bool = False
    readable: bool = False
    download_ready: bool = False

    @field_validator("kind")
    @classmethod
    def normalise_kind(cls, value: str) -> str:
        return _normalise_format(value) or ""


class CandidateOutput(_StrictPublicModel):
    """A final-answer candidate; it intentionally contains no model working trace."""

    answer: str = Field(default="", max_length=500_000)
    artifacts: list[ArtifactOutput] = Field(default_factory=list, max_length=100)


class DeliverableRequirement(_StrictPublicModel):
    """Machine-checkable requirements for one concrete GoalSpec deliverable."""

    deliverable_id: str = Field(..., min_length=1, max_length=160)
    kind: Literal["answer", "artifact"] = "answer"
    format: str = Field(default="text", min_length=1, max_length=80)
    filename: str = Field(default="", max_length=500)
    required: bool = True
    download_required: bool = False
    required_sections: list[str] = Field(default_factory=list, max_length=100)
    must_include: list[str] = Field(default_factory=list, max_length=100)
    source_markers: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("format")
    @classmethod
    def normalise_format(cls, value: str) -> str:
        return _normalise_format(value) or "text"

    @field_validator("required_sections", "must_include", "source_markers", mode="before")
    @classmethod
    def validate_requirements(cls, value: Any, info: Any) -> list[str]:
        return _normalise_string_list(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_shape(self) -> "DeliverableRequirement":
        if self.download_required and self.kind != "artifact":
            raise ValueError("download_required is only valid for artifact deliverables")
        return self


class EvidenceBundle(_StrictPublicModel):
    """Machine-checkable requirements and observed evidence for one candidate."""

    objective: str = Field(..., min_length=1, max_length=100_000)
    task_id: str = Field(default="", max_length=160)
    run_id: str = Field(default="", max_length=160)
    require_answer: bool = True
    deliverables: list[DeliverableRequirement] = Field(default_factory=list, max_length=100)
    expected_format: str | None = Field(default=None, max_length=80)
    requires_download: bool = False
    must_include: list[str] = Field(default_factory=list, max_length=_MAX_EVIDENCE_ITEMS)
    must_not_include: list[str] = Field(default_factory=list, max_length=_MAX_EVIDENCE_ITEMS)
    case_sensitive: bool = False
    required_tool_facts: dict[str, Any] = Field(default_factory=dict)
    tool_facts: dict[str, Any] = Field(default_factory=dict)
    goal_parameters: dict[str, Any] = Field(default_factory=dict)
    goal_parameter_evidence: dict[str, Any] = Field(default_factory=dict)
    unsupported_blocking_criteria: list[str] = Field(
        default_factory=list, max_length=_MAX_EVIDENCE_ITEMS
    )
    allow_additional_artifacts: bool = False

    @field_validator("expected_format")
    @classmethod
    def normalise_expected_format(cls, value: str | None) -> str | None:
        return _normalise_format(value)

    @field_validator("must_include", "must_not_include", mode="before")
    @classmethod
    def validate_text_requirements(cls, value: Any, info: Any) -> list[str]:
        return _normalise_string_list(value, field_name=info.field_name)

    @field_validator("unsupported_blocking_criteria", mode="before")
    @classmethod
    def validate_unsupported_criteria(cls, value: Any) -> list[str]:
        return _normalise_string_list(
            value, field_name="unsupported_blocking_criteria"
        )

    @field_validator(
        "required_tool_facts",
        "tool_facts",
        "goal_parameters",
        "goal_parameter_evidence",
        mode="before",
    )
    @classmethod
    def validate_structured_evidence(cls, value: Any, info: Any) -> dict[str, Any]:
        return _validate_json_mapping(value, field_name=info.field_name)


class RuleResult(_StrictPublicModel):
    id: str = Field(..., min_length=1, max_length=160)
    title: str = Field(..., min_length=1, max_length=200)
    status: RuleStatus
    public_reason: str = Field(..., min_length=1, max_length=_PUBLIC_REASON_LIMIT)
    repair_instruction: str | None = Field(
        default=None, max_length=_PUBLIC_INSTRUCTION_LIMIT
    )

    @field_validator("title", "public_reason", "repair_instruction", mode="before")
    @classmethod
    def compact_public_fields(cls, value: Any, info: Any) -> str | None:
        if value is None and info.field_name == "repair_instruction":
            return None
        limit = 200 if info.field_name == "title" else (
            _PUBLIC_REASON_LIMIT
            if info.field_name == "public_reason"
            else _PUBLIC_INSTRUCTION_LIMIT
        )
        return _compact_public_text(value, limit=limit)

    @model_validator(mode="after")
    def validate_repair_state(self) -> "RuleResult":
        if self.status == "passed" and self.repair_instruction is not None:
            raise ValueError("a passed rule cannot request a repair")
        return self


class SemanticResult(_StrictPublicModel):
    status: SemanticStatus
    public_reason: str = Field(..., min_length=1, max_length=_PUBLIC_REASON_LIMIT)
    repair_instructions: list[str] = Field(
        default_factory=list, max_length=_MAX_PUBLIC_INSTRUCTIONS
    )

    @field_validator("public_reason", mode="before")
    @classmethod
    def compact_reason(cls, value: Any) -> str:
        return _compact_public_text(value, limit=_PUBLIC_REASON_LIMIT)

    @field_validator("repair_instructions", mode="before")
    @classmethod
    def compact_instructions(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise TypeError("repair_instructions must be a list")
        result: list[str] = []
        for item in value:
            text = _compact_public_text(item, limit=_PUBLIC_INSTRUCTION_LIMIT)
            if text not in result:
                result.append(text)
        if len(result) > _MAX_PUBLIC_INSTRUCTIONS:
            raise ValueError("repair_instructions contains too many items")
        return result

    @model_validator(mode="after")
    def validate_status(self) -> "SemanticResult":
        if self.status == "passed" and self.repair_instructions:
            raise ValueError("a passed semantic result cannot request a repair")
        return self


class VerificationReport(_StrictPublicModel):
    schema_version: Literal[1] = 1
    mode: VerificationMode
    verdict: VerificationVerdict
    passed: bool
    coverage: Literal["rules_only", "rules_and_semantic"]
    semantic_attempted: bool = False
    semantic_verified: bool
    public_reason: str = Field(..., min_length=1, max_length=_PUBLIC_REASON_LIMIT)
    rules: list[RuleResult] = Field(..., min_length=1, max_length=100)
    semantic: SemanticResult
    repair_instructions: list[str] = Field(
        default_factory=list, max_length=_MAX_PUBLIC_INSTRUCTIONS
    )

    @field_validator("public_reason", mode="before")
    @classmethod
    def compact_reason(cls, value: Any) -> str:
        return _compact_public_text(value, limit=_PUBLIC_REASON_LIMIT)

    @field_validator("repair_instructions", mode="before")
    @classmethod
    def compact_instructions(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise TypeError("repair_instructions must be a list")
        result: list[str] = []
        for item in value:
            text = _compact_public_text(item, limit=_PUBLIC_INSTRUCTION_LIMIT)
            if text not in result:
                result.append(text)
        if len(result) > _MAX_PUBLIC_INSTRUCTIONS:
            raise ValueError("repair_instructions contains too many items")
        return result

    @model_validator(mode="after")
    def validate_consistency(self) -> "VerificationReport":
        rules_passed = all(item.status == "passed" for item in self.rules)
        semantic_status = self.semantic.status
        semantic_attempted = semantic_status in {"passed", "failed", "error"}
        semantic_verified = semantic_status in {"passed", "failed"}

        # VerificationService never invokes a semantic judge in rules-only mode,
        # nor after a deterministic rule has failed.  Reject reports that claim
        # otherwise instead of merely checking whether their duplicated flags
        # agree with the forged semantic status.
        if self.mode == "rules_only":
            if semantic_status != "skipped":
                raise ValueError("rules_only requires skipped semantic verification")
            if self.coverage != "rules_only":
                raise ValueError("rules_only requires rules_only coverage")
        if not rules_passed and semantic_status != "skipped":
            raise ValueError("failed rules require skipped semantic verification")

        if self.semantic_attempted != semantic_attempted:
            raise ValueError("semantic_attempted must reflect the semantic status")
        if self.semantic_verified != semantic_verified:
            raise ValueError("semantic_verified must reflect a completed semantic verdict")
        expected_coverage = "rules_and_semantic" if semantic_verified else "rules_only"
        if self.coverage != expected_coverage:
            raise ValueError("coverage must reflect completed semantic verification")

        if not rules_passed:
            expected_verdict: VerificationVerdict = "failed"
        elif semantic_status == "passed":
            expected_verdict = "passed"
        elif semantic_status == "failed":
            expected_verdict = "failed"
        elif self.mode == "semantic_required":
            # A skipped or unavailable required judge fails closed.
            expected_verdict = "failed"
        else:
            # Optional skipped/error semantics preserve only the deterministic
            # rules verdict.  rules_only reaches here with semantic=skipped.
            expected_verdict = "rules_passed"

        if self.verdict != expected_verdict:
            raise ValueError(
                "verdict does not match rule, semantic and verification-mode state"
            )
        expected_passed = expected_verdict in {"passed", "rules_passed"}
        if self.passed != expected_passed:
            raise ValueError("passed must reflect the verdict")
        if self.passed and self.repair_instructions:
            raise ValueError("a passed report cannot request a repair")
        return self


class SemanticJudge(Protocol):
    """A separately injected judge that returns public structured fields only."""

    async def evaluate(
        self,
        *,
        candidate: CandidateOutput,
        evidence: EvidenceBundle,
        rule_results: tuple[RuleResult, ...],
    ) -> SemanticResult | Mapping[str, Any]:
        ...


class VerificationService:
    """Run deterministic delivery checks and an optional independent judge.

    The service never constructs or persists a model prompt and never accepts a
    reasoning trace in its report schema.  A provider-specific judge may use a
    model internally, but only its strictly validated public result crosses this
    boundary.
    """

    def __init__(self, semantic_judge: SemanticJudge | None = None) -> None:
        self._semantic_judge = semantic_judge

    @property
    def has_semantic_judge(self) -> bool:
        """Whether this verifier can produce an independent semantic verdict."""

        return self._semantic_judge is not None

    async def verify(
        self,
        candidate: CandidateOutput | Mapping[str, Any],
        evidence: EvidenceBundle | Mapping[str, Any],
        *,
        mode: VerificationMode = "rules_only",
    ) -> VerificationReport:
        candidate_model = CandidateOutput.model_validate(candidate)
        evidence_model = EvidenceBundle.model_validate(evidence)
        rule_results = self._evaluate_rules(candidate_model, evidence_model)
        failed_rules = [item for item in rule_results if item.status == "failed"]

        if failed_rules:
            semantic = SemanticResult(
                status="skipped",
                public_reason="确定性规则尚未通过，本轮未执行语义复核。",
            )
            instructions = self._collect_repairs(failed_rules, semantic=None)
            return VerificationReport(
                mode=mode,
                verdict="failed",
                passed=False,
                coverage="rules_only",
                semantic_attempted=False,
                semantic_verified=False,
                public_reason=f"规则验收未通过：{len(failed_rules)} 项需要修复。",
                rules=rule_results,
                semantic=semantic,
                repair_instructions=instructions,
            )

        semantic = await self._evaluate_semantics(
            candidate_model, evidence_model, tuple(rule_results), mode
        )
        semantic_passed = semantic.status == "passed"
        semantic_blocks = semantic.status == "failed" or (
            mode == "semantic_required" and not semantic_passed
        )
        if semantic_blocks:
            instructions = self._collect_repairs([], semantic=semantic)
            if not instructions and mode == "semantic_required":
                instructions = ["配置可用的语义验收器后重新执行最终验收。"]
            return VerificationReport(
                mode=mode,
                verdict="failed",
                passed=False,
                coverage=(
                    "rules_and_semantic"
                    if semantic.status in {"passed", "failed"}
                    else "rules_only"
                ),
                semantic_attempted=semantic.status in {"passed", "failed", "error"},
                semantic_verified=semantic.status in {"passed", "failed"},
                public_reason=(
                    "规则验收通过，但语义复核未通过。"
                    if semantic.status == "failed"
                    else "规则验收通过，但必需的语义复核未完成。"
                ),
                rules=rule_results,
                semantic=semantic,
                repair_instructions=instructions,
            )

        if semantic_passed:
            return VerificationReport(
                mode=mode,
                verdict="passed",
                passed=True,
                coverage="rules_and_semantic",
                semantic_attempted=True,
                semantic_verified=True,
                public_reason="确定性规则和语义复核均已通过。",
                rules=rule_results,
                semantic=semantic,
            )

        return VerificationReport(
            mode=mode,
            verdict="rules_passed",
            passed=True,
            coverage="rules_only",
            semantic_attempted=semantic.status == "error",
            semantic_verified=False,
            public_reason=(
                "确定性规则已通过；当前模式未执行语义复核。"
                if mode == "rules_only"
                else "确定性规则已通过；语义复核未完成，结果仅覆盖规则验收。"
            ),
            rules=rule_results,
            semantic=semantic,
        )

    async def _evaluate_semantics(
        self,
        candidate: CandidateOutput,
        evidence: EvidenceBundle,
        rule_results: tuple[RuleResult, ...],
        mode: VerificationMode,
    ) -> SemanticResult:
        if mode == "rules_only":
            return SemanticResult(
                status="skipped",
                public_reason="当前使用仅规则验收模式，未执行语义复核。",
            )
        if self._semantic_judge is None:
            return SemanticResult(
                status="skipped",
                public_reason="当前未配置语义验收器。",
                repair_instructions=(
                    ["配置可用的语义验收器后重新执行最终验收。"]
                    if mode == "semantic_required"
                    else []
                ),
            )
        try:
            raw_result = await self._semantic_judge.evaluate(
                candidate=candidate.model_copy(deep=True),
                evidence=evidence.model_copy(deep=True),
                rule_results=tuple(item.model_copy(deep=True) for item in rule_results),
            )
            return SemanticResult.model_validate(raw_result)
        except Exception:
            # Never expose provider exceptions: they can contain prompts,
            # credentials, raw model output, or hidden reasoning fields.
            return SemanticResult(
                status="error",
                public_reason="语义复核暂时不可用，未公开内部诊断信息。",
                repair_instructions=(
                    ["检查语义验收器配置后重试。"]
                    if mode == "semantic_required"
                    else []
                ),
            )

    @classmethod
    def _evaluate_rules(
        cls, candidate: CandidateOutput, evidence: EvidenceBundle
    ) -> list[RuleResult]:
        results: list[RuleResult] = []

        has_required_output = bool(candidate.answer.strip()) if evidence.require_answer else bool(
            candidate.answer.strip() or candidate.artifacts
        )
        results.append(
            cls._rule(
                "candidate.non_empty",
                "最终交付不为空",
                has_required_output,
                "已生成非空的最终答复。" if evidence.require_answer else "已生成非空交付。",
                "最终答复为空，不能交付。" if evidence.require_answer else "没有可交付的答复或文件。",
                "生成一份非空、可直接交付的最终答复。",
            )
        )

        if evidence.unsupported_blocking_criteria:
            results.append(
                cls._rule(
                    "contract.supported",
                    "所有阻断级验收标准均可执行",
                    False,
                    "所有阻断级验收标准均有对应验证器。",
                    "尚不支持验收标准："
                    + _short_labels(evidence.unsupported_blocking_criteria),
                    "为这些阻断级标准配置可执行验证器："
                    + _short_labels(evidence.unsupported_blocking_criteria),
                )
            )

        if evidence.deliverables:
            results.extend(cls._evaluate_deliverables(candidate, evidence))
            if not evidence.allow_additional_artifacts:
                artifact_requirements = {
                    item.deliverable_id
                    for item in evidence.deliverables
                    if item.kind == "artifact"
                }
                unexpected = [
                    item.id or item.name or "未登记文件"
                    for item in candidate.artifacts
                    if not item.deliverable_id
                    or item.deliverable_id not in artifact_requirements
                ]
                results.append(
                    cls._rule(
                        "delivery.no_unexpected_artifacts",
                        "没有未绑定交付物的额外文件",
                        not unexpected,
                        "所有候选文件均绑定到已确认交付物。",
                        "发现未绑定交付物的文件：" + _short_labels(unexpected),
                        "移除额外文件，或先将其加入 GoalSpec 交付物。",
                    )
                )

        expected_format = evidence.expected_format
        matching_artifacts = list(candidate.artifacts)
        if not evidence.deliverables and expected_format and expected_format != "text":
            matching_artifacts = [
                item
                for item in candidate.artifacts
                if cls._artifact_format(item) == expected_format
            ]
            actual_formats = sorted(
                {
                    cls._artifact_format(item)
                    for item in candidate.artifacts
                    if cls._artifact_format(item)
                }
            )
            results.append(
                cls._rule(
                    "delivery.format",
                    f"交付格式为 {expected_format.upper()}",
                    bool(matching_artifacts),
                    f"已检测到 {len(matching_artifacts)} 个 {expected_format.upper()} 文件。",
                    "未检测到要求的交付格式；实际格式："
                    + ("、".join(actual_formats) if actual_formats else "无文件"),
                    f"生成并登记 {expected_format.upper()} 格式的交付文件。",
                )
            )

        if not evidence.deliverables and evidence.requires_download:
            download_candidates = (
                matching_artifacts
                if expected_format and expected_format != "text"
                else list(candidate.artifacts)
            )
            downloadable = [item for item in download_candidates if item.download_url.strip()]
            results.append(
                cls._rule(
                    "delivery.download",
                    "交付文件可以下载",
                    bool(downloadable),
                    f"已登记 {len(downloadable)} 个可下载文件。",
                    "要求下载交付，但没有匹配文件的下载地址。",
                    "为要求的交付文件登记可用下载地址。",
                )
            )

        searchable_text = "\n".join(
            [candidate.answer, *(item.content_text for item in candidate.artifacts)]
        )
        haystack = searchable_text if evidence.case_sensitive else searchable_text.casefold()
        if evidence.must_include:
            missing = [
                item
                for item in evidence.must_include
                if (item if evidence.case_sensitive else item.casefold()) not in haystack
            ]
            results.append(
                cls._rule(
                    "content.must_include",
                    "包含全部必需内容",
                    not missing,
                    f"全部 {len(evidence.must_include)} 项必需内容均已找到。",
                    "缺少必需内容：" + _short_labels(missing),
                    "补充缺少的必需内容：" + _short_labels(missing),
                )
            )

        if evidence.must_not_include:
            present = [
                item
                for item in evidence.must_not_include
                if (item if evidence.case_sensitive else item.casefold()) in haystack
            ]
            results.append(
                cls._rule(
                    "content.must_not_include",
                    "不包含禁止内容",
                    not present,
                    f"已确认 {len(evidence.must_not_include)} 项禁止内容均未出现。",
                    "发现禁止内容：" + _short_labels(present),
                    "移除禁止出现的内容：" + _short_labels(present),
                )
            )

        if evidence.required_tool_facts:
            tool_fact_issues = cls._exact_evidence_issues(
                evidence.required_tool_facts, evidence.tool_facts
            )
            results.append(
                cls._rule(
                    "evidence.tool_facts",
                    "工具事实证据精确匹配",
                    not tool_fact_issues,
                    f"已精确核对 {len(evidence.required_tool_facts)} 项工具事实。",
                    "工具事实缺失或不一致：" + _short_labels(tool_fact_issues),
                    "补充或纠正这些工具事实证据：" + _short_labels(tool_fact_issues),
                )
            )

        if evidence.goal_parameters:
            parameter_issues = cls._exact_evidence_issues(
                evidence.goal_parameters, evidence.goal_parameter_evidence
            )
            results.append(
                cls._rule(
                    "evidence.goal_parameters",
                    "目标参数证据精确匹配",
                    not parameter_issues,
                    f"已精确核对 {len(evidence.goal_parameters)} 项目标参数。",
                    "目标参数证据缺失或不一致：" + _short_labels(parameter_issues),
                    "补充或纠正这些目标参数证据：" + _short_labels(parameter_issues),
                )
            )

        return results

    @classmethod
    def _evaluate_deliverables(
        cls, candidate: CandidateOutput, evidence: EvidenceBundle
    ) -> list[RuleResult]:
        results: list[RuleResult] = []
        used_artifacts: set[int] = set()

        def _normalise_source_marker(value: str) -> str:
            """Compare source facts across Markdown and office renderers.

            Markdown attachments often sample a list item as ``- text`` while
            DOCX/PDF/XLSX generators deliberately render the same item as a
            plain paragraph or cell.  Removing only the presentation marker
            keeps the wording and punctuation strict without weakening normal
            required-content checks.
            """

            value = re.sub(
                r"^\s*(?:[-*+]\s+|#{1,6}\s+|\d+[.)]\s+)",
                "",
                str(value).strip(),
            )
            return re.sub(r"\s+", " ", value).strip()

        def contains_all(
            text: str, required: list[str], *, source_markers: bool = False
        ) -> list[str]:
            if source_markers:
                haystack = _normalise_source_marker(text)
                values = [_normalise_source_marker(item) for item in required]
            else:
                haystack = text if evidence.case_sensitive else text.casefold()
                values = required
            return [
                item
                for item, value in zip(required, values)
                if (
                    value if source_markers or evidence.case_sensitive else value.casefold()
                ) not in (
                    haystack if source_markers or evidence.case_sensitive else haystack.casefold()
                )
            ]

        for requirement in evidence.deliverables:
            prefix = f"deliverable.{requirement.deliverable_id}"
            if requirement.kind == "answer":
                answer = candidate.answer
                present = bool(answer.strip())
                if requirement.required:
                    results.append(
                        cls._rule(
                            f"{prefix}.present",
                            f"交付“{requirement.deliverable_id}”已生成",
                            present,
                            "已生成要求的文字答复。",
                            "要求的文字答复为空。",
                            "生成并保留要求的文字答复。",
                        )
                    )
                if not present and not requirement.required:
                    continue
                for suffix, title, values in (
                    ("content", "答复包含全部必需内容", requirement.must_include),
                    ("sections", "答复包含全部指定章节", requirement.required_sections),
                    ("source", "答复保留全部来源标记", requirement.source_markers),
                ):
                    if not values:
                        continue
                    missing = contains_all(answer, values)
                    results.append(
                        cls._rule(
                            f"{prefix}.{suffix}",
                            title,
                            not missing,
                            f"已在答复中找到全部 {len(values)} 项要求。",
                            "答复中缺少：" + _short_labels(missing),
                            "在答复中补充：" + _short_labels(missing),
                        )
                    )
                continue

            explicit = [
                (index, artifact)
                for index, artifact in enumerate(candidate.artifacts)
                if index not in used_artifacts
                and artifact.deliverable_id == requirement.deliverable_id
            ]
            candidates = explicit
            if not candidates:
                candidates = [
                    (index, artifact)
                    for index, artifact in enumerate(candidate.artifacts)
                    if index not in used_artifacts
                    and cls._artifact_format(artifact) == requirement.format
                    and (not requirement.filename or artifact.name == requirement.filename)
                ]
            selected = candidates[0] if candidates else None
            artifact = selected[1] if selected else None
            if selected:
                used_artifacts.add(selected[0])

            # Optional deliverables represent a requested capability that may
            # be unavailable on the current host (for example PPTX without
            # the optional Artifact Tool).  Their absence is reported by the
            # runtime, but must not block required deliverables in the same
            # request.  If an optional artifact is present, all normal checks
            # below still apply to it.
            if artifact is None and not requirement.required:
                continue

            format_matches = bool(
                artifact and cls._artifact_format(artifact) == requirement.format
            )
            results.append(
                cls._rule(
                    f"{prefix}.format",
                    f"交付格式为 {requirement.format.upper()}",
                    format_matches,
                    f"已匹配 {requirement.format.upper()} 交付文件。",
                    f"没有匹配交付“{requirement.deliverable_id}”的 {requirement.format.upper()} 文件。",
                    f"生成并绑定 {requirement.format.upper()} 文件到交付“{requirement.deliverable_id}”。",
                )
            )

            ownership_matches = bool(
                artifact
                and (not evidence.task_id or artifact.task_id == evidence.task_id)
                and (not evidence.run_id or artifact.run_id == evidence.run_id)
            )
            integrity_passed = bool(
                artifact
                and artifact.exists
                and artifact.readable
                and artifact.size_bytes > 0
                and len(artifact.sha256) == 64
                and ownership_matches
            )
            results.append(
                cls._rule(
                    f"{prefix}.integrity",
                    "交付文件存在、可读且摘要有效",
                    integrity_passed,
                    "文件归属正确，且已完成存在性、可读性和 SHA-256 检查。",
                    "文件缺失、不可读、归属不符或缺少有效 SHA-256 摘要。",
                    "重新生成文件并完成归属、可读性和 SHA-256 检查。",
                )
            )

            if requirement.filename:
                filename_matches = bool(artifact and artifact.name == requirement.filename)
                results.append(
                    cls._rule(
                        f"{prefix}.filename",
                        f"文件名为 {requirement.filename}",
                        filename_matches,
                        "文件名与已确认目标一致。",
                        f"实际文件名为 {artifact.name if artifact else '无文件'}。",
                        f"将交付文件名修正为 {requirement.filename}。",
                    )
                )

            if requirement.download_required:
                downloadable = bool(
                    artifact
                    and artifact.download_ready
                    and artifact.download_url.strip()
                    and artifact.exists
                )
                results.append(
                    cls._rule(
                        f"{prefix}.download",
                        "交付文件可以下载",
                        downloadable,
                        "文件已通过校验并登记可用下载地址。",
                        "文件尚未通过校验或下载地址不可用。",
                        "在文件校验通过后登记可用下载地址。",
                    )
                )

            artifact_text = artifact.content_text if artifact else ""
            for suffix, title, values in (
                ("content", "文件包含全部必需内容", requirement.must_include),
                ("sections", "文件包含全部指定章节", requirement.required_sections),
                ("source", "文件保留全部来源标记", requirement.source_markers),
            ):
                if not values:
                    continue
                missing = contains_all(
                    artifact_text,
                    values,
                    source_markers=suffix == "source",
                )
                results.append(
                    cls._rule(
                        f"{prefix}.{suffix}",
                        title,
                        not missing,
                        f"已在目标文件中找到全部 {len(values)} 项要求。",
                        "目标文件中缺少：" + _short_labels(missing),
                        "在目标文件中补充：" + _short_labels(missing),
                    )
                )

        return results

    @staticmethod
    def _artifact_format(artifact: ArtifactOutput) -> str:
        if artifact.kind:
            return _normalise_format(artifact.kind) or ""
        name = artifact.name.rsplit("/", 1)[-1]
        if "." not in name:
            return ""
        return _normalise_format(name.rsplit(".", 1)[-1]) or ""

    @staticmethod
    def _exact_evidence_issues(
        required: Mapping[str, Any], observed: Mapping[str, Any]
    ) -> list[str]:
        issues: list[str] = []
        for key, expected in required.items():
            if key not in observed:
                issues.append(f"{key}（缺失）")
            elif not _same_json_value(expected, observed[key]):
                issues.append(f"{key}（不一致）")
        return issues

    @staticmethod
    def _rule(
        rule_id: str,
        title: str,
        passed: bool,
        passed_reason: str,
        failed_reason: str,
        repair_instruction: str,
    ) -> RuleResult:
        return RuleResult(
            id=rule_id,
            title=title,
            status="passed" if passed else "failed",
            public_reason=passed_reason if passed else failed_reason,
            repair_instruction=None if passed else repair_instruction,
        )

    @staticmethod
    def _collect_repairs(
        failed_rules: list[RuleResult], *, semantic: SemanticResult | None
    ) -> list[str]:
        result: list[str] = []
        for rule in failed_rules:
            if rule.repair_instruction and rule.repair_instruction not in result:
                result.append(rule.repair_instruction)
        if semantic is not None:
            for instruction in semantic.repair_instructions:
                if instruction not in result:
                    result.append(instruction)
        return result[:_MAX_PUBLIC_INSTRUCTIONS]


__all__ = [
    "ArtifactOutput",
    "CandidateOutput",
    "DeliverableRequirement",
    "EvidenceBundle",
    "RuleResult",
    "SemanticJudge",
    "SemanticResult",
    "VerificationMode",
    "VerificationReport",
    "VerificationService",
]
