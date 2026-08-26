from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.services.goal_spec_service import GoalSpec, ensure_goal_spec
from app.services.runtime_contract_service import canonical_json_hash
from app.services.verification_service import (
    CandidateOutput,
    EvidenceBundle,
    VerificationMode,
    VerificationReport,
    VerificationService,
)


@dataclass(frozen=True)
class FinalizationResult:
    candidate: CandidateOutput
    evidence: EvidenceBundle
    report: VerificationReport
    persisted: dict[str, Any]
    candidate_sha256: str
    evidence_sha256: str


class CandidateFinalizationService:
    """Persist the authoritative verification before a candidate can be published."""

    def __init__(self, task_state: Any) -> None:
        self.task_state = task_state

    async def verify_and_persist(
        self,
        *,
        task_id: str,
        run_id: str,
        goal_spec_id: str,
        goal_spec: GoalSpec | Mapping[str, Any],
        candidate: CandidateOutput | Mapping[str, Any],
        evidence: EvidenceBundle | Mapping[str, Any],
        verification_service: VerificationService,
        mode: VerificationMode,
        intake_generation: int = 0,
        verifier_model_id: str = "",
        repaired_from_id: str = "",
    ) -> FinalizationResult:
        spec = ensure_goal_spec(goal_spec)
        if spec.task_id != task_id or spec.status != "confirmed":
            raise RuntimeError("最终验收只能引用当前任务已确认的 GoalSpec")
        stored_goal = self.task_state.get_goal_spec(goal_spec_id)
        if not stored_goal:
            raise RuntimeError("最终验收引用的 GoalSpec 尚未持久化")
        if (
            stored_goal.get("task_id") != task_id
            or stored_goal.get("spec_hash") != spec.spec_hash
            or int(stored_goal.get("version") or 0) != spec.version
        ):
            raise RuntimeError("最终验收引用的 GoalSpec 版本或 Hash 不一致")
        active_goal = self.task_state.latest_goal_spec(run_id=run_id)
        if not active_goal or active_goal.get("id") != goal_spec_id:
            raise RuntimeError("最终验收必须引用当前运行的最新 GoalSpec")

        candidate_model = CandidateOutput.model_validate(candidate)
        evidence_model = EvidenceBundle.model_validate(evidence)
        if evidence_model.task_id != task_id:
            raise RuntimeError("验收证据不属于当前任务")
        if evidence_model.run_id != run_id:
            raise RuntimeError("验收证据不属于当前运行")
        if evidence_model.objective != spec.objective.statement:
            raise RuntimeError("验收证据目标与当前 GoalSpec 不一致")
        candidate_sha256 = canonical_json_hash(candidate_model.model_dump(mode="json"))
        evidence_sha256 = canonical_json_hash(evidence_model.model_dump(mode="json"))
        report = await verification_service.verify(
            candidate_model, evidence_model, mode=mode
        )
        public_report = report.model_dump(mode="json")
        persisted = self.task_state.save_verification_report(
            task_id,
            run_id,
            goal_spec_id,
            public_report,
            public_report=public_report,
            verifier_model_id=verifier_model_id,
            candidate_sha256=candidate_sha256,
            evidence_sha256=evidence_sha256,
            intake_generation=intake_generation,
            repaired_from_id=repaired_from_id,
        )
        if (
            persisted.get("candidate_sha256") != candidate_sha256
            or persisted.get("evidence_sha256") != evidence_sha256
            or persisted.get("mode") != mode
            or persisted.get("verifier_model_id") != verifier_model_id
            or persisted.get("public_report") != public_report
            or int(persisted.get("intake_generation") or 0)
            != int(intake_generation)
        ):
            raise RuntimeError("持久化的最终验收记录与本轮验收不一致")
        return FinalizationResult(
            candidate=candidate_model,
            evidence=evidence_model,
            report=report,
            persisted=persisted,
            candidate_sha256=candidate_sha256,
            evidence_sha256=evidence_sha256,
        )


__all__ = ["CandidateFinalizationService", "FinalizationResult"]
