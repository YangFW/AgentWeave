from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ResourceKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]


class _StrictModel(BaseModel):
    """Immutable, closed-schema base for persisted goal-contract values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        str_strip_whitespace=True,
    )


class ProvenanceRef(_StrictModel):
    source_type: Literal[
        "user_message",
        "conversation_event",
        "attachment",
        "memory",
        "policy",
        "default",
        "derived",
        "tool_result",
    ]
    source_id: ResourceKey
    field: str = Field(default="", max_length=240)
    excerpt: str = Field(default="", max_length=2_000)


class ObjectiveSpec(_StrictModel):
    statement: str = Field(min_length=1, max_length=100_000)
    intent: str = Field(default="general", min_length=1, max_length=160)
    in_scope: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    out_of_scope: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    constraints: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    provenance: tuple[ProvenanceRef, ...] = Field(default_factory=tuple, max_length=100)


class InputSpec(_StrictModel):
    key: ResourceKey
    label: str = Field(min_length=1, max_length=160)
    value: JsonValue | None = None
    required: bool = True
    status: Literal["provided", "defaulted", "missing"] = "provided"
    ask: str = Field(default="", max_length=500)
    default_policy: Literal["never", "allowed", "applied"] = "never"
    provenance: tuple[ProvenanceRef, ...] = Field(default_factory=tuple, max_length=100)

    @model_validator(mode="after")
    def validate_value_and_provenance(self) -> "InputSpec":
        if self.status == "missing":
            if self.value is not None:
                raise ValueError("missing input cannot carry a value")
            if self.default_policy == "applied":
                raise ValueError("missing input cannot have an applied default")
            return self
        if self.value is None:
            raise ValueError(f"{self.status} input must carry a value")
        if not self.provenance:
            raise ValueError(f"{self.status} input must declare provenance")
        if self.status == "defaulted" and self.default_policy != "applied":
            raise ValueError("defaulted input must use default_policy='applied'")
        return self


class DeliverableSpec(_StrictModel):
    id: ResourceKey
    kind: Literal["answer", "artifact", "action"] = "answer"
    format: str = Field(default="text", min_length=1, max_length=80)
    title: str = Field(default="", max_length=500)
    filename: str = Field(default="", max_length=500)
    sections: tuple[str, ...] = Field(default_factory=tuple, max_length=200)
    required: bool = True
    download_required: bool = False
    source_input_keys: tuple[str, ...] = Field(default_factory=tuple, max_length=200)

    @model_validator(mode="after")
    def validate_delivery_shape(self) -> "DeliverableSpec":
        if self.download_required and self.kind != "artifact":
            raise ValueError("download_required is only valid for artifact deliverables")
        return self


class SkillBinding(_StrictModel):
    skill_id: ResourceKey
    version: str = Field(min_length=1, max_length=80)
    content_hash: Sha256
    name: str = Field(default="", max_length=200)
    purpose: str = Field(default="", max_length=500)
    score: float | None = Field(default=None, ge=0)


class ToolArgumentConstraint(_StrictModel):
    argument_path: ResourceKey
    operator: Literal["equals", "in", "matches", "present"] = "equals"
    expected: JsonValue | None = None
    source_input_key: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def validate_constraint(self) -> "ToolArgumentConstraint":
        if self.operator == "present" and (self.expected is not None or self.source_input_key):
            raise ValueError("present constraints cannot carry an expected value")
        if self.operator == "matches" and not isinstance(self.expected, str):
            raise ValueError("matches constraints require a string pattern")
        if self.operator == "in" and not isinstance(self.expected, list):
            raise ValueError("in constraints require an array expected value")
        if self.operator == "equals" and self.expected is None and not self.source_input_key:
            raise ValueError("equals constraints require expected or source_input_key")
        return self


class ToolBinding(_StrictModel):
    server_id: ResourceKey
    tool_name: ResourceKey
    schema_hash: Sha256
    effect: Literal["read", "write", "external", "unknown"] = "unknown"
    purpose: str = Field(default="", max_length=500)
    argument_constraints: tuple[ToolArgumentConstraint, ...] = Field(
        default_factory=tuple, max_length=100
    )

    @model_validator(mode="after")
    def validate_unique_constraints(self) -> "ToolBinding":
        _ensure_unique(
            (item.argument_path for item in self.argument_constraints),
            "tool argument constraint",
        )
        return self

    @property
    def qualified_name(self) -> str:
        return f"{self.server_id}.{self.tool_name}"


class CapabilityBindings(_StrictModel):
    skills: tuple[SkillBinding, ...] = Field(default_factory=tuple, max_length=100)
    tools: tuple[ToolBinding, ...] = Field(default_factory=tuple, max_length=500)
    network_access: Literal["none", "restricted", "unrestricted"] = "none"
    allowed_network_hosts: tuple[str, ...] = Field(default_factory=tuple, max_length=500)

    @model_validator(mode="after")
    def validate_unique_bindings(self) -> "CapabilityBindings":
        _ensure_unique((item.skill_id for item in self.skills), "skill binding")
        _ensure_unique(
            (f"{item.server_id}.{item.tool_name}" for item in self.tools),
            "tool binding",
        )
        if self.network_access == "none" and self.allowed_network_hosts:
            raise ValueError("network_access='none' cannot carry allowed hosts")
        return self


class ContextRef(_StrictModel):
    kind: Literal[
        "conversation_task",
        "conversation_event",
        "attachment",
        "memory",
        "policy",
        "external_source",
    ]
    ref_id: ResourceKey
    role: str = Field(default="source", min_length=1, max_length=120)
    content_hash: Sha256 | None = None
    required: bool = False
    label: str = Field(default="", max_length=500)


class AcceptanceCriterion(_StrictModel):
    id: ResourceKey
    title: str = Field(min_length=1, max_length=500)
    kind: Literal[
        "semantic_match",
        "presence",
        "format",
        "artifact_content",
        "download",
        "filename",
        "sections",
        "source_consistency",
        "schema",
        "custom",
    ]
    target: str = Field(default="answer", min_length=1, max_length=240)
    operator: Literal[
        "present",
        "equals",
        "contains",
        "contains_all",
        "minimum",
        "valid",
        "semantic_equivalent",
    ] = "valid"
    expected: JsonValue | None = None
    severity: Literal["block", "warn"] = "block"
    evidence_required: bool = True


class ConfirmationSpec(_StrictModel):
    status: Literal[
        "unresolved",
        "needs_input",
        "auto_confirmed",
        "user_confirmed",
    ] = "unresolved"
    mode: Literal["none", "automatic", "user"] = "none"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguities: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    confirmation_ref: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def validate_confirmation_shape(self) -> "ConfirmationSpec":
        if self.status == "auto_confirmed" and self.mode != "automatic":
            raise ValueError("auto_confirmed requires mode='automatic'")
        if self.status == "user_confirmed" and self.mode != "user":
            raise ValueError("user_confirmed requires mode='user'")
        if self.status in {"auto_confirmed", "user_confirmed"} and self.ambiguities:
            raise ValueError("a confirmed goal cannot retain unresolved ambiguities")
        return self


class GoalSpecRef(_StrictModel):
    goal_id: ResourceKey
    version: int = Field(ge=1)
    spec_hash: Sha256


class GoalSpec(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    goal_id: ResourceKey
    task_id: ResourceKey
    conversation_id: str = Field(default="", max_length=240)
    version: int = Field(default=1, ge=1)
    status: Literal["draft", "needs_input", "confirmed"] = "draft"
    objective: ObjectiveSpec
    inputs: tuple[InputSpec, ...] = Field(default_factory=tuple, max_length=500)
    deliverables: tuple[DeliverableSpec, ...] = Field(min_length=1, max_length=200)
    capability_bindings: CapabilityBindings = Field(default_factory=CapabilityBindings)
    context_refs: tuple[ContextRef, ...] = Field(default_factory=tuple, max_length=1_000)
    acceptance: tuple[AcceptanceCriterion, ...] = Field(min_length=1, max_length=500)
    confirmation: ConfirmationSpec = Field(default_factory=ConfirmationSpec)
    supersedes: GoalSpecRef | None = None
    spec_hash: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")

    @model_validator(mode="after")
    def validate_and_seal(self) -> "GoalSpec":
        _ensure_unique((item.key for item in self.inputs), "input")
        _ensure_unique((item.id for item in self.deliverables), "deliverable")
        _ensure_unique(
            (f"{item.kind}:{item.ref_id}:{item.role}" for item in self.context_refs),
            "context reference",
        )
        _ensure_unique((item.id for item in self.acceptance), "acceptance criterion")

        missing = self.missing_required_inputs
        if missing and self.status != "needs_input":
            raise ValueError("a goal with missing required inputs must use status='needs_input'")
        if not missing and self.status == "needs_input":
            raise ValueError("status='needs_input' requires at least one missing required input")
        if self.status == "confirmed":
            if self.confirmation.status not in {"auto_confirmed", "user_confirmed"}:
                raise ValueError("confirmed goal requires a confirmed confirmation record")
        elif self.confirmation.status in {"auto_confirmed", "user_confirmed"}:
            raise ValueError("a confirmed confirmation record requires status='confirmed'")

        if self.version == 1 and self.supersedes is not None:
            raise ValueError("version 1 cannot supersede another GoalSpec")
        if self.version > 1:
            if self.supersedes is None:
                raise ValueError("version > 1 must reference the superseded GoalSpec")
            if self.supersedes.goal_id != self.goal_id:
                raise ValueError("superseded GoalSpec must belong to the same goal lineage")
            if self.supersedes.version >= self.version:
                raise ValueError("superseded version must be lower than the new version")

        expected_hash = _hash_payload(
            self.model_dump(mode="json", exclude={"spec_hash"})
        )
        if self.spec_hash and self.spec_hash != expected_hash:
            raise ValueError("GoalSpec hash does not match its canonical payload")
        object.__setattr__(self, "spec_hash", expected_hash)
        return self

    @property
    def missing_required_inputs(self) -> tuple[InputSpec, ...]:
        return tuple(
            item for item in self.inputs if item.required and item.status == "missing"
        )

    def public_summary(self) -> dict[str, Any]:
        return public_goal_summary(self)


class GoalSpecRevision(_StrictModel):
    objective: ObjectiveSpec | None = None
    inputs: tuple[InputSpec, ...] | None = None
    deliverables: tuple[DeliverableSpec, ...] | None = None
    capability_bindings: CapabilityBindings | None = None
    context_refs: tuple[ContextRef, ...] | None = None
    acceptance: tuple[AcceptanceCriterion, ...] | None = None
    confirmation: ConfirmationSpec | None = None

    @model_validator(mode="after")
    def require_a_change(self) -> "GoalSpecRevision":
        if not self.model_fields_set:
            raise ValueError("a GoalSpec revision must contain at least one change")
        return self


def _ensure_unique(values: Sequence[str] | Any, label: str) -> None:
    seen: set[str] = set()
    for raw in values:
        value = str(raw)
        if value in seen:
            raise ValueError(f"duplicate {label}: {value}")
        seen.add(value)


def _hash_payload(payload: Mapping[str, Any]) -> str:
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("GoalSpec must contain only canonical JSON values") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_goal_hash(value: GoalSpec | Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 without trusting a supplied hash field."""

    if isinstance(value, GoalSpec):
        payload = value.model_dump(mode="json", exclude={"spec_hash"})
    else:
        payload = dict(value)
        payload.pop("spec_hash", None)
    return _hash_payload(payload)


def ensure_goal_spec(value: GoalSpec | Mapping[str, Any]) -> GoalSpec:
    """Load either the typed model or its plain-dict serialization."""

    return value if isinstance(value, GoalSpec) else GoalSpec.model_validate(value)


def _default_deliverables() -> tuple[DeliverableSpec, ...]:
    return (DeliverableSpec(id="answer", kind="answer", format="text"),)


def _default_acceptance(
    deliverables: Sequence[DeliverableSpec],
) -> tuple[AcceptanceCriterion, ...]:
    criteria: list[AcceptanceCriterion] = [
        AcceptanceCriterion(
            id="goal_semantics",
            title="结果与当前目标语义一致",
            kind="semantic_match",
            target="answer_and_artifacts",
            operator="semantic_equivalent",
            evidence_required=True,
        ),
        AcceptanceCriterion(
            id="response_present",
            title="已生成可交付结果",
            kind="presence",
            target="answer_or_artifact",
            operator="present",
            evidence_required=True,
        ),
    ]
    for item in deliverables:
        if item.kind != "artifact" or not item.required:
            continue
        criteria.append(
            AcceptanceCriterion(
                id=f"format_{item.id}",
                title=f"已生成要求的 {item.format} 文件",
                kind="format",
                target=f"deliverable:{item.id}",
                operator="equals",
                expected=item.format,
            )
        )
        if item.download_required:
            criteria.append(
                AcceptanceCriterion(
                    id=f"download_{item.id}",
                    title="文件已注册并可下载",
                    kind="download",
                    target=f"deliverable:{item.id}",
                    operator="valid",
                )
            )
    return tuple(criteria)


def _lineage_ref(spec: GoalSpec) -> GoalSpecRef:
    return GoalSpecRef(
        goal_id=spec.goal_id,
        version=spec.version,
        spec_hash=spec.spec_hash,
    )


def _status_for(
    inputs: Sequence[InputSpec], confirmation: ConfirmationSpec
) -> Literal["draft", "needs_input", "confirmed"]:
    if any(item.required and item.status == "missing" for item in inputs):
        return "needs_input"
    if confirmation.status in {"auto_confirmed", "user_confirmed"}:
        return "confirmed"
    return "draft"


def compile_draft(
    *,
    task_id: str,
    objective: ObjectiveSpec | Mapping[str, Any],
    conversation_id: str = "",
    inputs: Sequence[InputSpec | Mapping[str, Any]] = (),
    deliverables: Sequence[DeliverableSpec | Mapping[str, Any]] | None = None,
    capability_bindings: CapabilityBindings | Mapping[str, Any] | None = None,
    context_refs: Sequence[ContextRef | Mapping[str, Any]] = (),
    acceptance: Sequence[AcceptanceCriterion | Mapping[str, Any]] | None = None,
    confirmation: ConfirmationSpec | Mapping[str, Any] | None = None,
    goal_id: str = "",
) -> GoalSpec:
    """Compile an immutable first revision from typed values or plain dictionaries."""

    task_id = str(task_id).strip()
    if not task_id:
        raise ValueError("task_id cannot be empty")
    objective_value = ObjectiveSpec.model_validate(objective)
    input_values = tuple(InputSpec.model_validate(item) for item in inputs)
    deliverable_values = tuple(
        DeliverableSpec.model_validate(item)
        for item in (deliverables if deliverables is not None else _default_deliverables())
    )
    binding_value = CapabilityBindings.model_validate(capability_bindings or {})
    context_values = tuple(ContextRef.model_validate(item) for item in context_refs)
    confirmation_value = ConfirmationSpec.model_validate(confirmation or {})
    acceptance_values = tuple(
        AcceptanceCriterion.model_validate(item)
        for item in (
            acceptance
            if acceptance is not None
            else _default_acceptance(deliverable_values)
        )
    )
    resolved_goal_id = goal_id.strip() or (
        "goal_" + hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
    )
    return GoalSpec.model_validate(
        {
            "goal_id": resolved_goal_id,
            "task_id": task_id,
            "conversation_id": conversation_id,
            "version": 1,
            "status": _status_for(input_values, confirmation_value),
            "objective": objective_value,
            "inputs": input_values,
            "deliverables": deliverable_values,
            "capability_bindings": binding_value,
            "context_refs": context_values,
            "acceptance": acceptance_values,
            "confirmation": confirmation_value,
        }
    )


def finalize(
    value: GoalSpec | Mapping[str, Any],
    *,
    confirmation: ConfirmationSpec | Mapping[str, Any] | None = None,
    capability_bindings: CapabilityBindings | Mapping[str, Any] | None = None,
    acceptance: Sequence[AcceptanceCriterion | Mapping[str, Any]] | None = None,
) -> GoalSpec:
    """Create a confirmed revision; never mutate or silently fill missing inputs."""

    current = ensure_goal_spec(value)
    if current.missing_required_inputs:
        missing = ", ".join(item.key for item in current.missing_required_inputs)
        raise ValueError(f"cannot finalize GoalSpec with missing inputs: {missing}")
    confirmation_value = ConfirmationSpec.model_validate(
        confirmation
        or {
            "status": "auto_confirmed",
            "mode": "automatic",
            "confidence": max(current.confirmation.confidence, 0.5),
        }
    )
    if confirmation_value.status not in {"auto_confirmed", "user_confirmed"}:
        raise ValueError("finalize requires auto_confirmed or user_confirmed confirmation")
    binding_value = (
        CapabilityBindings.model_validate(capability_bindings)
        if capability_bindings is not None
        else current.capability_bindings
    )
    acceptance_value = (
        tuple(AcceptanceCriterion.model_validate(item) for item in acceptance)
        if acceptance is not None
        else current.acceptance
    )
    payload = current.model_dump(mode="json", exclude={"spec_hash"})
    payload.update(
        {
            "version": current.version + 1,
            "status": "confirmed",
            "capability_bindings": binding_value,
            "acceptance": acceptance_value,
            "confirmation": confirmation_value,
            "supersedes": _lineage_ref(current),
        }
    )
    return GoalSpec.model_validate(payload)


def revise_for_steering(
    value: GoalSpec | Mapping[str, Any],
    changes: GoalSpecRevision | Mapping[str, Any],
    *,
    version: int | None = None,
) -> GoalSpec:
    """Create a new revision for a runtime user amendment.

    Material goal changes invalidate old Skill/tool bindings and generated
    acceptance criteria unless the caller supplies replacements explicitly.
    This prevents a steering message from inheriting a stale execution plan.
    """

    current = ensure_goal_spec(value)
    next_version = current.version + 1 if version is None else int(version)
    if next_version <= current.version:
        raise ValueError("a steering revision version must exceed its superseded version")
    revision = GoalSpecRevision.model_validate(changes)
    changed_fields = revision.model_fields_set
    material_fields = {"objective", "inputs", "deliverables", "context_refs"}
    materially_changed = bool(changed_fields.intersection(material_fields))

    payload = current.model_dump(
        mode="json", exclude={"spec_hash", "status", "version", "supersedes"}
    )
    for field_name in changed_fields:
        payload[field_name] = getattr(revision, field_name)

    if materially_changed and "capability_bindings" not in changed_fields:
        payload["capability_bindings"] = CapabilityBindings()

    deliverable_values = tuple(
        DeliverableSpec.model_validate(item) for item in payload["deliverables"]
    )
    if materially_changed and "acceptance" not in changed_fields:
        payload["acceptance"] = _default_acceptance(deliverable_values)

    input_values = tuple(InputSpec.model_validate(item) for item in payload["inputs"])
    if "confirmation" in changed_fields:
        confirmation_value = ConfirmationSpec.model_validate(payload["confirmation"])
    else:
        has_missing = any(
            item.required and item.status == "missing" for item in input_values
        )
        confirmation_value = ConfirmationSpec(
            status="needs_input" if has_missing else "unresolved",
            mode="none",
            confidence=0.0,
        )
        payload["confirmation"] = confirmation_value

    payload.update(
        {
            "version": next_version,
            "status": _status_for(input_values, confirmation_value),
            "supersedes": _lineage_ref(current),
        }
    )
    return GoalSpec.model_validate(payload)


def public_goal_summary(value: GoalSpec | Mapping[str, Any]) -> dict[str, Any]:
    """Return a user-facing contract summary without provenance excerpts or hidden work."""

    spec = ensure_goal_spec(value)
    return {
        "schema_version": spec.schema_version,
        "goal_id": spec.goal_id,
        "task_id": spec.task_id,
        "version": spec.version,
        "status": spec.status,
        "objective": {
            "statement": spec.objective.statement,
            "intent": spec.objective.intent,
            "in_scope": list(spec.objective.in_scope),
            "out_of_scope": list(spec.objective.out_of_scope),
            "constraints": list(spec.objective.constraints),
        },
        "missing_inputs": [
            {"key": item.key, "label": item.label, "ask": item.ask}
            for item in spec.missing_required_inputs
        ],
        "deliverables": [
            {
                "id": item.id,
                "kind": item.kind,
                "format": item.format,
                "title": item.title,
                "filename": item.filename,
                "required": item.required,
                "download_required": item.download_required,
            }
            for item in spec.deliverables
        ],
        "capabilities": {
            "skills": [
                {
                    "skill_id": item.skill_id,
                    "name": item.name,
                    "version": item.version,
                    "purpose": item.purpose,
                }
                for item in spec.capability_bindings.skills
            ],
            "tools": [
                {
                    "server_id": item.server_id,
                    "tool_name": item.tool_name,
                    "effect": item.effect,
                    "purpose": item.purpose,
                }
                for item in spec.capability_bindings.tools
            ],
            "network_access": spec.capability_bindings.network_access,
        },
        "context_ref_count": len(spec.context_refs),
        "acceptance": [
            {
                "id": item.id,
                "title": item.title,
                "kind": item.kind,
                "severity": item.severity,
            }
            for item in spec.acceptance
        ],
        "confirmation": {
            "status": spec.confirmation.status,
            "mode": spec.confirmation.mode,
            "confidence": spec.confirmation.confidence,
            "ambiguities": list(spec.confirmation.ambiguities),
        },
        "supersedes": (
            spec.supersedes.model_dump(mode="json") if spec.supersedes else None
        ),
        "spec_hash": spec.spec_hash,
    }


__all__ = [
    "AcceptanceCriterion",
    "CapabilityBindings",
    "ConfirmationSpec",
    "ContextRef",
    "DeliverableSpec",
    "GoalSpec",
    "GoalSpecRef",
    "GoalSpecRevision",
    "InputSpec",
    "ObjectiveSpec",
    "ProvenanceRef",
    "SkillBinding",
    "ToolBinding",
    "ToolArgumentConstraint",
    "canonical_goal_hash",
    "compile_draft",
    "ensure_goal_spec",
    "finalize",
    "public_goal_summary",
    "revise_for_steering",
]
