from __future__ import annotations

import sqlite3
import unittest

from app.services.candidate_finalization_service import CandidateFinalizationService
from app.services.goal_spec_service import compile_draft, finalize
from app.services.task_state import TaskStateService
from app.services.verification_service import VerificationService


class CandidateFinalizationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.state = TaskStateService(self.conn)
        self.run = self.state.begin_run("task-finalize")
        draft = compile_draft(
            task_id="task-finalize",
            objective={"statement": "给出可靠性结论", "intent": "analysis"},
        )
        self.state.save_goal_spec(
            "task-finalize", self.run["id"], draft.model_dump(mode="json")
        )
        self.goal = finalize(draft)
        self.goal_record = self.state.save_goal_spec(
            "task-finalize", self.run["id"], self.goal.model_dump(mode="json")
        )

    async def asyncTearDown(self) -> None:
        self.conn.close()

    async def test_report_is_persisted_against_exact_candidate_and_goal(self) -> None:
        result = await CandidateFinalizationService(self.state).verify_and_persist(
            task_id="task-finalize",
            run_id=self.run["id"],
            goal_spec_id=self.goal_record["id"],
            goal_spec=self.goal,
            candidate={"answer": "可靠性结论已生成。", "artifacts": []},
            evidence={"objective": "给出可靠性结论", "task_id": "task-finalize", "run_id": self.run["id"]},
            verification_service=VerificationService(),
            mode="rules_only",
        )
        self.assertTrue(result.report.passed)
        self.assertEqual(len(result.candidate_sha256), 64)
        self.assertEqual(result.persisted["candidate_sha256"], result.candidate_sha256)
        self.assertEqual(
            result.persisted["public_report"], result.report.model_dump(mode="json")
        )

    async def test_failed_report_is_still_persisted_before_publication_decision(self) -> None:
        result = await CandidateFinalizationService(self.state).verify_and_persist(
            task_id="task-finalize",
            run_id=self.run["id"],
            goal_spec_id=self.goal_record["id"],
            goal_spec=self.goal,
            candidate={"answer": "", "artifacts": []},
            evidence={"objective": "给出可靠性结论", "task_id": "task-finalize", "run_id": self.run["id"]},
            verification_service=VerificationService(),
            mode="rules_only",
        )
        self.assertFalse(result.report.passed)
        self.assertEqual(result.persisted["status"], "failed")

    async def test_same_candidate_with_different_evidence_or_mode_is_not_reused(self) -> None:
        service = CandidateFinalizationService(self.state)
        common = {
            "task_id": "task-finalize",
            "run_id": self.run["id"],
            "goal_spec_id": self.goal_record["id"],
            "goal_spec": self.goal,
            "candidate": {"answer": "可靠性结论已生成。", "artifacts": []},
            "verification_service": VerificationService(),
        }
        first = await service.verify_and_persist(
            **common,
            evidence={
                "objective": "给出可靠性结论",
                "task_id": "task-finalize",
                "run_id": self.run["id"],
            },
            mode="rules_only",
        )
        second = await service.verify_and_persist(
            **common,
            evidence={
                "objective": "给出可靠性结论",
                "task_id": "task-finalize",
                "run_id": self.run["id"],
                "must_not_include": ["不存在的禁止词"],
            },
            mode="semantic_optional",
        )

        self.assertNotEqual(first.persisted["id"], second.persisted["id"])
        self.assertNotEqual(first.evidence_sha256, second.evidence_sha256)
        self.assertEqual(second.persisted["attempt"], first.persisted["attempt"] + 1)
        self.assertEqual(second.persisted["mode"], "semantic_optional")

    async def test_superseded_goal_cannot_be_used_for_final_publication(self) -> None:
        newer = finalize(self.goal)
        self.state.save_goal_spec(
            "task-finalize", self.run["id"], newer.model_dump(mode="json")
        )

        with self.assertRaisesRegex(RuntimeError, "最新 GoalSpec"):
            await CandidateFinalizationService(self.state).verify_and_persist(
                task_id="task-finalize",
                run_id=self.run["id"],
                goal_spec_id=self.goal_record["id"],
                goal_spec=self.goal,
                candidate={"answer": "旧目标候选。", "artifacts": []},
                evidence={
                    "objective": "给出可靠性结论",
                    "task_id": "task-finalize",
                    "run_id": self.run["id"],
                },
                verification_service=VerificationService(),
                mode="rules_only",
            )


if __name__ == "__main__":
    unittest.main()
