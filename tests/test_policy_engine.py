from __future__ import annotations

import unittest
from typing import Any, Mapping

from app.services.policy_engine import (
    PolicyApprovalRequired,
    PolicyConfigurationError,
    PolicyDenied,
    PolicyEngine,
    PolicyRule,
    SUPPORTED_LIFECYCLE_EVENTS,
)


def builtin_rule(
    rule_id: str,
    decision: str,
    *,
    event: str = "tool.before",
    scope: str = "organization",
    scope_id: str | None = None,
    priority: int = 0,
    match: Mapping[str, Any] | None = None,
    **handler: Any,
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "name": rule_id.replace("-", " ").title(),
        "event": event,
        "scope": scope,
        "scope_id": scope_id,
        "priority": priority,
        "match": dict(match or {}),
        "handler": {"type": "builtin_rule", "decision": decision, **handler},
    }


class PolicyRuleValidationTests(unittest.TestCase):
    def test_declares_all_required_lifecycle_events(self) -> None:
        self.assertEqual(
            SUPPORTED_LIFECYCLE_EVENTS,
            {
                "task.created",
                "goal.resolved",
                "plan.created",
                "tool.before",
                "tool.after",
                "tool.failed",
                "approval.requested",
                "artifact.created",
                "output.before",
                "task.completed",
                "task.failed",
            },
        )
        rule = PolicyRule.from_dict(
            {
                "id": "all-events",
                "events": sorted(SUPPORTED_LIFECYCLE_EVENTS),
                "handler": {"type": "builtin_rule", "decision": "allow"},
            }
        )
        self.assertEqual(set(rule.events), SUPPORTED_LIFECYCLE_EVENTS)

    def test_rejects_unknown_event_decision_and_scope(self) -> None:
        invalid_values = [
            {**builtin_rule("bad-event", "allow"), "event": "task.started"},
            builtin_rule("bad-decision", "execute"),
            {**builtin_rule("bad-scope", "allow"), "scope": "session"},
        ]
        for value in invalid_values:
            with self.subTest(rule=value["id"]), self.assertRaises(PolicyConfigurationError):
                PolicyRule.from_dict(value)

    def test_rejects_shell_and_arbitrary_handler_types(self) -> None:
        for handler_type in ("shell", "command", "python", "javascript"):
            with self.subTest(handler_type=handler_type), self.assertRaises(PolicyConfigurationError):
                PolicyRule.from_dict(
                    {
                        "id": f"unsafe-{handler_type}",
                        "event": "tool.before",
                        "handler": {"type": handler_type, "command": "echo unsafe"},
                    }
                )

    def test_rule_ids_are_unique(self) -> None:
        with self.assertRaises(PolicyConfigurationError):
            PolicyEngine([builtin_rule("same", "allow"), builtin_rule("same", "deny")])

    def test_scope_aliases_are_normalized(self) -> None:
        self.assertEqual(PolicyRule.from_dict({**builtin_rule("org", "allow"), "scope": "tenant"}).scope, "organization")
        self.assertEqual(PolicyRule.from_dict({**builtin_rule("project", "allow"), "scope": "project"}).scope, "workspace")


class PolicyEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_matching_rule_defaults_to_allow(self) -> None:
        engine = PolicyEngine([builtin_rule("only-weather", "deny", match={"server": "weather"})])
        evaluation = await engine.evaluate(
            "tool.before",
            {"tool": {"server": "filesystem", "name": "read_file", "arguments": {"path": "notes.md"}}},
        )
        self.assertTrue(evaluation.allowed)
        self.assertEqual(evaluation.rules_considered, 1)
        self.assertEqual(evaluation.rules_matched, 0)
        self.assertEqual(evaluation.to_dict()["outcome"], "allow")

    async def test_non_tool_lifecycle_audit_omits_internal_missing_sentinel(self) -> None:
        evaluation = await PolicyEngine(
            [builtin_rule("goal-audit", "allow", event="goal.resolved")]
        ).evaluate(
            "goal.resolved", {"goal": {"standalone_request": "整理报告"}}
        )
        serialized = evaluation.to_dict()
        self.assertNotIn("server", serialized["decisions"][0]["match"])
        self.assertNotIn("tool", serialized["decisions"][0]["match"])

    async def test_tool_match_supports_server_tool_glob_and_argument_conditions(self) -> None:
        engine = PolicyEngine(
            [
                builtin_rule(
                    "protect-system-files",
                    "deny",
                    priority=100,
                    match={
                        "server": ["filesystem", "local-files"],
                        "tool": "write_*",
                        "arguments": {
                            "path": {"starts_with": "/etc/"},
                            "overwrite": {"eq": True},
                            "size": {"gte": 1024},
                        },
                    },
                    reason="系统目录禁止覆盖写入",
                )
            ]
        )
        evaluation = await engine.evaluate(
            "tool.before",
            {
                "task_id": "task-1",
                "tool": {
                    "server": "filesystem",
                    "name": "write_file",
                    "arguments": {"path": "/etc/hosts", "overwrite": True, "size": 2048},
                },
            },
        )
        self.assertTrue(evaluation.denied)
        self.assertEqual(evaluation.summary, "系统目录禁止覆盖写入")
        decision = evaluation.to_dict()["decisions"][0]
        self.assertEqual(decision["match"]["server"], "filesystem")
        self.assertEqual(decision["match"]["tool"], "write_file")
        self.assertEqual(decision["match"]["argument_condition_paths"], ["overwrite", "path", "size"])
        self.assertNotIn("/etc/hosts", str(decision["match"]))

    async def test_tool_argument_mismatch_does_not_apply_rule(self) -> None:
        engine = PolicyEngine(
            [
                builtin_rule(
                    "protect-system-files",
                    "deny",
                    match={"server": "filesystem", "tool": "write_file", "arguments": {"path": {"starts_with": "/etc/"}}},
                )
            ]
        )
        evaluation = await engine.evaluate(
            "tool.before",
            {"server_id": "filesystem", "tool_name": "write_file", "arguments": {"path": "/workspace/result.md"}},
        )
        self.assertTrue(evaluation.allowed)
        self.assertEqual(evaluation.rules_matched, 0)

    async def test_generic_conditions_match_nested_context_without_code(self) -> None:
        engine = PolicyEngine(
            [
                builtin_rule(
                    "external-writes-need-owner",
                    "require_approval",
                    event="artifact.created",
                    match={
                        "conditions": [
                            {"path": "artifact.visibility", "op": "eq", "value": "public"},
                            {"path": "artifact.size", "op": "gt", "value": 1_000_000},
                        ]
                    },
                    approval={"kind": "publish_artifact"},
                    reason="公开的大文件需要审批",
                )
            ]
        )
        evaluation = await engine.evaluate(
            "artifact.created",
            {"artifact": {"visibility": "public", "size": 2_000_000}},
        )
        self.assertTrue(evaluation.requires_approval)
        self.assertEqual(evaluation.approval_requests[0]["kind"], "publish_artifact")
        with self.assertRaises(PolicyApprovalRequired):
            evaluation.raise_for_outcome()

    async def test_organization_restriction_cannot_be_relaxed_by_lower_scope(self) -> None:
        engine = PolicyEngine(
            [
                builtin_rule("org-deny", "deny", scope="organization", priority=10, reason="组织禁止该操作"),
                builtin_rule("workspace-allow", "allow", scope="workspace", priority=999),
                builtin_rule("user-allow", "allow", scope="user", priority=9999),
            ]
        )
        evaluation = await engine.evaluate("tool.before", {})
        self.assertTrue(evaluation.denied)
        self.assertIn("组织禁止该操作", evaluation.summary)
        with self.assertRaises(PolicyDenied):
            evaluation.raise_for_outcome()

    async def test_lower_scope_can_make_an_organization_allow_more_restrictive(self) -> None:
        engine = PolicyEngine(
            [
                builtin_rule("org-allow", "allow", scope="organization", priority=10),
                builtin_rule("workspace-approval", "require_approval", scope="workspace", priority=10),
            ]
        )
        evaluation = await engine.evaluate("tool.before", {})
        self.assertTrue(evaluation.requires_approval)

    async def test_highest_priority_wins_within_a_scope(self) -> None:
        engine = PolicyEngine(
            [
                builtin_rule("old-deny", "deny", scope="workspace", priority=10),
                builtin_rule("new-allow", "allow", scope="workspace", priority=20),
            ]
        )
        evaluation = await engine.evaluate("tool.before", {})
        self.assertTrue(evaluation.allowed)
        by_rule = {item.rule_id: item for item in evaluation.decisions}
        self.assertFalse(by_rule["old-deny"].effective)
        self.assertTrue(by_rule["new-allow"].effective)

    async def test_ineffective_terminal_rule_cannot_smuggle_a_patch(self) -> None:
        engine = PolicyEngine(
            [
                builtin_rule(
                    "old-deny-with-patch",
                    "deny",
                    scope="workspace",
                    priority=10,
                    modifications={"answer": "被低优先级规则篡改"},
                ),
                builtin_rule(
                    "new-allow",
                    "allow",
                    scope="workspace",
                    priority=20,
                ),
            ]
        )
        evaluation = await engine.evaluate("output.before", {"answer": "原答案"})
        self.assertTrue(evaluation.allowed)
        self.assertEqual(evaluation.modifications, {})
        self.assertEqual(evaluation.apply({"answer": "原答案"})["answer"], "原答案")

    async def test_equal_priority_is_resolved_toward_restrictive_decision(self) -> None:
        engine = PolicyEngine(
            [
                builtin_rule("allow", "allow", priority=10),
                builtin_rule("approval", "require_approval", priority=10),
                builtin_rule("deny", "deny", priority=10),
            ]
        )
        evaluation = await engine.evaluate("tool.before", {})
        self.assertTrue(evaluation.denied)
        self.assertEqual([item.rule_id for item in evaluation.decisions if item.effective], ["deny"])

    async def test_scope_id_limits_rule_to_its_owner(self) -> None:
        engine = PolicyEngine(
            [builtin_rule("workspace-a", "deny", scope="workspace", scope_id="workspace-a")]
        )
        allowed = await engine.evaluate("tool.before", {"workspace_id": "workspace-b"})
        denied = await engine.evaluate("tool.before", {"scope": {"workspace_id": "workspace-a"}})
        self.assertTrue(allowed.allowed)
        self.assertTrue(denied.denied)

    async def test_modify_and_add_context_are_merged_with_hierarchical_precedence(self) -> None:
        engine = PolicyEngine(
            [
                builtin_rule(
                    "user-default",
                    "modify",
                    scope="user",
                    priority=100,
                    modifications={"tool": {"arguments": {"timeout": 20, "format": "text"}}},
                ),
                builtin_rule(
                    "org-cap",
                    "modify",
                    scope="organization",
                    priority=1,
                    modifications={"tool": {"arguments": {"timeout": 5}}},
                ),
                builtin_rule(
                    "workspace-context",
                    "add_context",
                    scope="workspace",
                    added_context={"policy_context": {"cost_center": "engineering"}},
                ),
                builtin_rule(
                    "org-context",
                    "add_context",
                    scope="organization",
                    added_context={"policy_context": {"classification": "internal"}},
                ),
            ]
        )
        original = {"tool": {"arguments": {"timeout": 60, "path": "report.md"}}}
        evaluation = await engine.evaluate("tool.before", original)
        patched = evaluation.apply(original)
        self.assertTrue(evaluation.allowed)
        self.assertEqual(evaluation.modifications["tool"]["arguments"]["timeout"], 5)
        self.assertEqual(patched["tool"]["arguments"]["format"], "text")
        self.assertEqual(patched["tool"]["arguments"]["path"], "report.md")
        self.assertEqual(patched["policy_context"], {"cost_center": "engineering", "classification": "internal"})
        self.assertEqual(original["tool"]["arguments"]["timeout"], 60)

    async def test_add_context_never_overwrites_existing_or_modified_values(self) -> None:
        engine = PolicyEngine(
            [
                builtin_rule("modify-timeout", "modify", modifications={"arguments": {"timeout": 5}}),
                builtin_rule(
                    "context-defaults",
                    "add_context",
                    added_context={"arguments": {"timeout": 120, "format": "json"}, "trace": {"enabled": True}},
                ),
            ]
        )
        evaluation = await engine.evaluate("tool.before", {})
        patched = evaluation.apply({"arguments": {"path": "report.md"}, "trace": {"enabled": False}})
        self.assertEqual(patched["arguments"], {"path": "report.md", "timeout": 5, "format": "json"})
        self.assertFalse(patched["trace"]["enabled"])

    async def test_lifecycle_patches_apply_to_goal_plan_output_and_approval_shapes(self) -> None:
        cases = [
            (
                "goal.resolved",
                {"goal": {"standalone_request": "原目标"}},
                {"goal": {"standalone_request": "受策略约束的目标"}},
                "goal.standalone_request",
                "受策略约束的目标",
            ),
            (
                "plan.created",
                {"plan": {"output_format": "text"}},
                {"plan": {"output_format": "md"}},
                "plan.output_format",
                "md",
            ),
            (
                "output.before",
                {"answer": "内部版本"},
                {"answer": "可公开版本"},
                "answer",
                "可公开版本",
            ),
            (
                "approval.requested",
                {"approval": {"message": "原审批说明"}},
                {"approval": {"message": "请核对影响范围"}},
                "approval.message",
                "请核对影响范围",
            ),
        ]
        for event, context, modifications, path, expected in cases:
            with self.subTest(event=event):
                engine = PolicyEngine(
                    [
                        builtin_rule(
                            f"modify-{event}",
                            "modify",
                            event=event,
                            modifications=modifications,
                        )
                    ]
                )
                evaluation = await engine.evaluate(event, context)
                applied = evaluation.apply(context)
                current: Any = applied
                for segment in path.split("."):
                    current = current[segment]
                self.assertEqual(current, expected)
                self.assertNotEqual(context, applied)

    async def test_apply_rejects_non_mapping_context(self) -> None:
        evaluation = await PolicyEngine().evaluate("output.before", {})
        with self.assertRaisesRegex(PolicyConfigurationError, "must be an object"):
            evaluation.apply([])  # type: ignore[arg-type]

    async def test_audit_serialization_redacts_policy_patches(self) -> None:
        engine = PolicyEngine(
            [builtin_rule("secret-patch", "modify", modifications={"headers": {"authorization": "Bearer secret"}})]
        )
        evaluation = await engine.evaluate("tool.before", {})
        self.assertEqual(
            evaluation.to_dict()["modifications"]["headers"]["authorization"],
            "[REDACTED]",
        )
        self.assertEqual(evaluation.modifications["headers"]["authorization"], "Bearer secret")

    async def test_invalid_operator_is_rejected_instead_of_silently_bypassed(self) -> None:
        engine = PolicyEngine(
            [builtin_rule("unsafe-operator", "deny", match={"arguments": {"path": {"op": "regex", "value": ".*"}}})]
        )
        with self.assertRaises(PolicyConfigurationError):
            await engine.evaluate("tool.before", {"arguments": {"path": "anything"}})

    async def test_unknown_event_is_rejected(self) -> None:
        with self.assertRaises(PolicyConfigurationError):
            await PolicyEngine().evaluate("tool.executing", {})


class HttpPolicyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def http_rule(
        *,
        url: str = "https://policy.example.com/hooks/tool",
        on_error: str | None = None,
    ) -> dict[str, Any]:
        handler: dict[str, Any] = {"type": "http", "url": url}
        if on_error:
            handler["on_error"] = on_error
        return {
            "id": "remote-policy",
            "event": "tool.before",
            "scope": "organization",
            "match": {"server": "filesystem"},
            "handler": handler,
        }

    async def test_http_is_disabled_by_default_and_fails_closed(self) -> None:
        called = False

        async def dispatcher(url: str, payload: dict[str, Any], timeout: float, headers: Mapping[str, str]) -> Mapping[str, Any]:
            nonlocal called
            called = True
            return {"decision": "allow"}

        engine = PolicyEngine([self.http_rule()], http_dispatcher=dispatcher)
        evaluation = await engine.evaluate("tool.before", {"server": "filesystem", "tool": "read_file"})
        self.assertTrue(evaluation.denied)
        self.assertFalse(called)
        self.assertEqual(evaluation.decisions[0].handler_status, "error")
        self.assertIn("disabled", evaluation.decisions[0].reason)

    async def test_allowlisted_http_handler_can_require_approval_and_redacts_secrets(self) -> None:
        calls: list[dict[str, Any]] = []

        async def dispatcher(url: str, payload: dict[str, Any], timeout: float, headers: Mapping[str, str]) -> Mapping[str, Any]:
            calls.append({"url": url, "payload": payload, "timeout": timeout, "headers": dict(headers)})
            return {
                "decision": "require_approval",
                "reason": "远程数据外发需要审批",
                "approval": {"kind": "external_data_transfer"},
                "metadata": {"policy_version": "2026-08"},
            }

        engine = PolicyEngine(
            [self.http_rule()],
            http_enabled=True,
            http_allowlist=["https://policy.example.com/hooks"],
            http_dispatcher=dispatcher,
        )
        evaluation = await engine.evaluate(
            "tool.before",
            {
                "server": "filesystem",
                "tool": "read_file",
                "api_key": "must-not-leak",
                "arguments": {"path": "report.md", "access_token": "also-secret"},
            },
        )
        self.assertTrue(evaluation.requires_approval)
        self.assertEqual(len(calls), 1)
        sent_context = calls[0]["payload"]["context"]
        self.assertEqual(sent_context["api_key"], "[REDACTED]")
        self.assertEqual(sent_context["arguments"]["access_token"], "[REDACTED]")
        self.assertEqual(sent_context["arguments"]["path"], "report.md")
        self.assertEqual(evaluation.approval_requests[0]["kind"], "external_data_transfer")

    async def test_allowlist_uses_origin_and_path_boundaries(self) -> None:
        calls = 0

        async def dispatcher(url: str, payload: dict[str, Any], timeout: float, headers: Mapping[str, str]) -> Mapping[str, Any]:
            nonlocal calls
            calls += 1
            return {"decision": "allow"}

        urls = [
            "https://policy.example.com.evil.test/hooks/tool",
            "https://policy.example.com/hooksmith/tool",
            "http://policy.example.com/hooks/tool",
            "https://policy.example.com/hooks/%252e%252e/admin",
        ]
        for url in urls:
            with self.subTest(url=url):
                engine = PolicyEngine(
                    [self.http_rule(url=url)],
                    http_enabled=True,
                    http_allowlist=["https://policy.example.com/hooks"],
                    http_dispatcher=dispatcher,
                )
                evaluation = await engine.evaluate("tool.before", {"server": "filesystem"})
                self.assertTrue(evaluation.denied)
        self.assertEqual(calls, 0)

    def test_http_url_rejects_query_secrets_and_fail_open_configuration(self) -> None:
        with self.assertRaises(PolicyConfigurationError):
            PolicyEngine([self.http_rule(url="https://policy.example.com/hooks?token=secret")])
        with self.assertRaises(PolicyConfigurationError):
            PolicyEngine([self.http_rule(on_error="allow")])

    async def test_http_can_return_modification_and_context_patch(self) -> None:
        async def dispatcher(url: str, payload: dict[str, Any], timeout: float, headers: Mapping[str, str]) -> Mapping[str, Any]:
            return {
                "decision": "modify",
                "modifications": {"arguments": {"timeout": 3}},
                "added_context": {"remote_policy": {"checked": True}},
                "reason": "限制远程调用超时",
            }

        engine = PolicyEngine(
            [self.http_rule()],
            http_enabled=True,
            http_allowlist=["policy.example.com"],
            http_dispatcher=dispatcher,
        )
        evaluation = await engine.evaluate(
            "tool.before",
            {"server": "filesystem", "arguments": {"timeout": 30, "path": "report.md"}},
        )
        self.assertTrue(evaluation.allowed)
        self.assertEqual(evaluation.apply({"arguments": {"timeout": 30}})["arguments"]["timeout"], 3)
        self.assertTrue(evaluation.added_context["remote_policy"]["checked"])

    async def test_invalid_http_response_fails_closed_with_auditable_error(self) -> None:
        async def dispatcher(url: str, payload: dict[str, Any], timeout: float, headers: Mapping[str, str]) -> Mapping[str, Any]:
            return {"unexpected": "shape"}

        engine = PolicyEngine(
            [self.http_rule()],
            http_enabled=True,
            http_allowlist=["policy.example.com"],
            http_dispatcher=dispatcher,
        )
        evaluation = await engine.evaluate("tool.before", {"server": "filesystem"})
        self.assertTrue(evaluation.denied)
        self.assertEqual(evaluation.decisions[0].handler_status, "error")
        self.assertEqual(evaluation.decisions[0].metadata["error_type"], "PolicyConfigurationError")

    async def test_http_error_can_explicitly_pause_for_approval(self) -> None:
        async def dispatcher(url: str, payload: dict[str, Any], timeout: float, headers: Mapping[str, str]) -> Mapping[str, Any]:
            raise TimeoutError("policy service timed out")

        engine = PolicyEngine(
            [self.http_rule(on_error="require_approval")],
            http_enabled=True,
            http_allowlist=["policy.example.com"],
            http_dispatcher=dispatcher,
        )
        evaluation = await engine.evaluate("tool.before", {"server": "filesystem"})
        self.assertTrue(evaluation.requires_approval)
        self.assertEqual(evaluation.decisions[0].handler_status, "error")


if __name__ == "__main__":
    unittest.main()
