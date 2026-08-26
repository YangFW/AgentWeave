from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from app.services.goal_spec_service import (
    CapabilityBindings,
    GoalSpec,
    SkillBinding,
    ToolArgumentConstraint,
    ToolBinding,
    ensure_goal_spec,
)


class ContractViolation(RuntimeError):
    """Raised when execution no longer matches the confirmed GoalSpec."""


def canonical_json_hash(value: Any) -> str:
    """Return a stable SHA-256 for JSON-compatible contract material."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractViolation("能力定义包含无法固化的非 JSON 数据") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _same_json_value(left: Any, right: Any) -> bool:
    try:
        return json.dumps(
            left,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) == json.dumps(
            right,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return False


class RuntimeContractService:
    """Freeze Skill/tool capabilities and enforce them at the invocation boundary.

    The service intentionally has no planning or model responsibilities.  It
    snapshots the concrete capability definitions selected by a confirmed goal
    and later proves that a call still uses the same server, tool, schema and
    goal-bound parameter values.
    """

    def __init__(self, skill_registry: Any, mcp_gateway: Any) -> None:
        self.skill_registry = skill_registry
        self.mcp_gateway = mcp_gateway

    @staticmethod
    def schema_hash(schema: Mapping[str, Any]) -> str:
        value = dict(schema)
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            raise ContractViolation("工具参数 Schema 无效，已停止执行") from exc
        return canonical_json_hash(value)

    def tool_definition(self, server_id: str, tool_name: str) -> dict[str, Any]:
        getter = getattr(self.mcp_gateway, "get_tool_definition", None)
        definition = getter(server_id, tool_name) if callable(getter) else None
        if not isinstance(definition, Mapping):
            lister = getattr(self.mcp_gateway, "list_tools", None)
            listed = lister() if callable(lister) else []
            definition = next(
                (
                    item
                    for item in listed
                    if isinstance(item, Mapping)
                    and str(item.get("server_id") or "") == server_id
                    and str(item.get("name") or "") == tool_name
                ),
                None,
            )
        if not isinstance(definition, Mapping):
            raise ContractViolation(f"已确认的工具不存在或不可用：{server_id}.{tool_name}")
        result = dict(definition)
        schema = result.get("input_schema")
        if not isinstance(schema, Mapping):
            raise ContractViolation(f"工具 {server_id}.{tool_name} 缺少有效参数 Schema")
        self.schema_hash(schema)
        return result

    def snapshot_bindings(
        self,
        *,
        skills: Sequence[Mapping[str, Any]],
        tools: Sequence[tuple[str, str]],
        argument_constraints: Mapping[
            tuple[str, str], Sequence[Mapping[str, Any] | ToolArgumentConstraint]
        ] | None = None,
        network_access: str = "none",
        allowed_network_hosts: Sequence[str] = (),
    ) -> CapabilityBindings:
        skill_bindings: list[SkillBinding] = []
        for skill in skills:
            skill_id = str(skill.get("id") or "").strip()
            if not skill_id:
                raise ContractViolation("已选择的 Skill 缺少稳定 ID")
            if hasattr(self.skill_registry, "package_hash"):
                content_hash = str(self.skill_registry.package_hash(skill_id))
            else:
                runtime_content = str(
                    self.skill_registry.runtime_content(skill_id)
                    if hasattr(self.skill_registry, "runtime_content")
                    else skill.get("content") or ""
                )
                content_hash = hashlib.sha256(runtime_content.encode("utf-8")).hexdigest()
            skill_bindings.append(
                SkillBinding(
                    skill_id=skill_id,
                    version=str(skill.get("version") or "0.0.0"),
                    content_hash=content_hash,
                    name=str(skill.get("name") or skill_id),
                    purpose=str(skill.get("description") or "")[:500],
                )
            )

        tool_bindings: list[ToolBinding] = []
        constraint_map = dict(argument_constraints or {})
        seen: set[tuple[str, str]] = set()
        for raw_server_id, raw_tool_name in tools:
            server_id = str(raw_server_id).strip()
            tool_name = str(raw_tool_name).strip()
            key = (server_id, tool_name)
            if not server_id or not tool_name or key in seen:
                continue
            seen.add(key)
            definition = self.tool_definition(server_id, tool_name)
            schema = dict(definition.get("input_schema") or {})
            effect = str(definition.get("effect") or "unknown").lower()
            if effect not in {"read", "write", "external", "unknown"}:
                effect = "unknown"
            tool_bindings.append(
                ToolBinding(
                    server_id=server_id,
                    tool_name=tool_name,
                    schema_hash=self.schema_hash(schema),
                    effect=effect,
                    purpose=str(definition.get("description") or "")[:500],
                    argument_constraints=tuple(
                        ToolArgumentConstraint.model_validate(item)
                        for item in constraint_map.get(key, ())
                    ),
                )
            )

        return CapabilityBindings(
            skills=tuple(skill_bindings),
            tools=tuple(tool_bindings),
            network_access=network_access,
            allowed_network_hosts=tuple(str(item) for item in allowed_network_hosts),
        )

    def validate_tool_call(
        self,
        goal_spec: GoalSpec | Mapping[str, Any],
        server_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> None:
        spec = ensure_goal_spec(goal_spec)
        if spec.status != "confirmed":
            raise ContractViolation("当前目标尚未确认，不能调用工具")
        binding = next(
            (
                item
                for item in spec.capability_bindings.tools
                if item.server_id == server_id and item.tool_name == tool_name
            ),
            None,
        )
        if binding is None:
            raise ContractViolation(f"已阻止 GoalSpec 未授权的工具调用：{server_id}.{tool_name}")

        definition = self.tool_definition(server_id, tool_name)
        schema = dict(definition.get("input_schema") or {})
        current_hash = self.schema_hash(schema)
        if current_hash != binding.schema_hash:
            raise ContractViolation(
                f"工具 {server_id}.{tool_name} 的参数 Schema 已变化，请重新确认目标和计划"
            )

        try:
            Draft202012Validator(schema).validate(dict(arguments))
        except ValidationError as exc:
            location = ".".join(str(item) for item in exc.absolute_path)
            suffix = f"（字段 {location}）" if location else ""
            raise ContractViolation(
                f"工具 {server_id}.{tool_name} 参数不符合 Schema{suffix}"
            ) from exc

        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        supplied = dict(arguments)
        inputs_by_key = {item.key: item for item in spec.inputs}
        for constraint in binding.argument_constraints:
            found, actual = self._argument_value(supplied, constraint.argument_path)
            if constraint.operator == "present":
                if not found:
                    raise ContractViolation(
                        f"工具参数 {constraint.argument_path} 是已确认目标的必填约束"
                    )
                continue
            expected = constraint.expected
            if constraint.source_input_key:
                source = inputs_by_key.get(constraint.source_input_key)
                if source is None or source.status not in {"provided", "defaulted"}:
                    raise ContractViolation(
                        f"GoalSpec 缺少参数约束来源 {constraint.source_input_key}"
                    )
                expected = source.value
            if not found:
                raise ContractViolation(
                    f"工具参数缺少已确认约束 {constraint.argument_path}"
                )
            if constraint.operator == "equals" and not _same_json_value(expected, actual):
                raise ContractViolation(
                    f"工具参数 {constraint.argument_path} 与已确认目标不一致，已停止调用"
                )
            if constraint.operator == "in":
                allowed = expected if isinstance(expected, list) else []
                if not any(_same_json_value(item, actual) for item in allowed):
                    raise ContractViolation(
                        f"工具参数 {constraint.argument_path} 不在已确认范围内，已停止调用"
                    )
            if constraint.operator == "matches" and not re.search(str(expected), str(actual)):
                raise ContractViolation(
                    f"工具参数 {constraint.argument_path} 不符合已确认格式，已停止调用"
                )

        for item in spec.inputs:
            if item.status not in {"provided", "defaulted"} or item.key not in properties:
                continue
            if item.key not in supplied:
                # JSON Schema decides whether an omitted value is legal.  A
                # goal-bound value cannot be silently replaced when supplied.
                continue
            if not _same_json_value(item.value, supplied[item.key]):
                raise ContractViolation(
                    f"工具参数 {item.key} 与已确认目标不一致，已停止调用"
                )

    def validate_capability_snapshot(
        self, goal_spec: GoalSpec | Mapping[str, Any]
    ) -> None:
        spec = ensure_goal_spec(goal_spec)
        if spec.status != "confirmed":
            raise ContractViolation("能力快照只能校验已确认的 GoalSpec")
        for binding in spec.capability_bindings.skills:
            if hasattr(self.skill_registry, "package_hash"):
                current_hash = str(self.skill_registry.package_hash(binding.skill_id))
            else:
                current = str(self.skill_registry.runtime_content(binding.skill_id))
                current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
            current_skill = (
                self.skill_registry.get_skill(binding.skill_id)
                if hasattr(self.skill_registry, "get_skill")
                else None
            )
            if not isinstance(current_skill, Mapping):
                raise ContractViolation(f"已确认的 Skill 不存在：{binding.skill_id}")
            if (
                current_hash != binding.content_hash
                or str(current_skill.get("version") or "0.0.0") != binding.version
            ):
                raise ContractViolation(
                    f"Skill {binding.skill_id} 的版本或内容已变化，请重新确认目标"
                )
        for binding in spec.capability_bindings.tools:
            definition = self.tool_definition(binding.server_id, binding.tool_name)
            current_hash = self.schema_hash(dict(definition.get("input_schema") or {}))
            if current_hash != binding.schema_hash:
                raise ContractViolation(
                    f"工具 {binding.qualified_name} 的参数 Schema 已变化，请重新确认目标"
                )

    @staticmethod
    def _argument_value(arguments: Mapping[str, Any], path: str) -> tuple[bool, Any]:
        current: Any = arguments
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return False, None
            current = current[part]
        return True, current


__all__ = [
    "ContractViolation",
    "RuntimeContractService",
    "canonical_json_hash",
]
