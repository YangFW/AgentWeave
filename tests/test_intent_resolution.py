from __future__ import annotations

import unittest

from app.services.model_gateway import ModelGateway


class IntentResolutionTests(unittest.TestCase):
    def test_extracts_json_from_fenced_model_output(self) -> None:
        gateway = ModelGateway()
        value = gateway._json_object('```json\n{"standalone_request":"整理季度复盘","parameters":{"department":"产品部","format":"docx"}}\n```')
        self.assertEqual(value["parameters"]["department"], "产品部")
        self.assertEqual(value["parameters"]["format"], "docx")

    def test_rejects_non_json_intent_output(self) -> None:
        with self.assertRaises(RuntimeError):
            ModelGateway()._json_object("我认为这是一个后续问题")

    def test_normalizes_common_tool_parameter_aliases(self) -> None:
        value = ModelGateway()._normalize_parameters(
            {"location": "宁波", "time": "明天", "audience": "管理层", "output_format": "pdf"},
            "查询宁波明天是否下雨",
        )
        self.assertEqual(value["city"], "宁波")
        self.assertEqual(value["day"], "tomorrow")
        self.assertEqual(value["audience"], "管理层")
        self.assertEqual(value["format"], "pdf")

    def test_removes_parser_output_instruction_leak(self) -> None:
        gateway = ModelGateway()
        value = gateway._sanitize_standalone_request(
            "制定一个三阶段内测方案。只输出一个 JSON 对象，不要 Markdown。",
            "制定一个三阶段内测方案。",
        )
        self.assertEqual(value, "制定一个三阶段内测方案。")

    def test_preserves_json_format_when_user_requested_it(self) -> None:
        gateway = ModelGateway()
        value = gateway._sanitize_standalone_request(
            "制定一个三阶段内测方案，只输出 JSON。",
            "制定一个三阶段内测方案，只输出 JSON。",
        )
        self.assertIn("JSON", value)

    def test_removes_verbose_parser_schema_leak_from_expert_task(self) -> None:
        gateway = ModelGateway()
        value = gateway._sanitize_standalone_request(
            "作为可靠性专家评估平台闭环并列出证据与风险。"
            "输出必须为一个 JSON 对象，字段为 standalone_request、intent、"
            "parameters、missing_information、is_follow_up，不要使用 Markdown。",
            "作为可靠性专家评估平台闭环并列出证据与风险。",
        )
        self.assertEqual(value, "作为可靠性专家评估平台闭环并列出证据与风险。")
        self.assertNotIn("standalone_request", value)

    def test_parser_json_leak_is_removed_when_user_requested_markdown(self) -> None:
        gateway = ModelGateway()
        value = gateway._sanitize_standalone_request(
            "用 Markdown 总结平台。返回 JSON 对象，字段为 standalone_request、"
            "intent、parameters、missing_information、is_follow_up。",
            "用 Markdown 总结平台。",
        )
        self.assertEqual(value, "用 Markdown 总结平台。")

    def test_json_input_does_not_preserve_parser_schema_leak(self) -> None:
        gateway = ModelGateway()
        value = gateway._sanitize_standalone_request(
            "分析附件 JSON 数据并给出风险。只输出一个 JSON 对象，不要 Markdown。"
            "字段必须为 standalone_request、intent、parameters、"
            "missing_information、is_follow_up。",
            "分析附件 JSON 数据并给出风险。",
        )
        self.assertEqual(value, "分析附件 JSON 数据并给出风险。")

    def test_json_output_request_does_not_preserve_unrequested_parser_fields(self) -> None:
        gateway = ModelGateway()
        value = gateway._sanitize_standalone_request(
            "分析附件并用 JSON 输出风险列表。字段必须为 standalone_request、intent、"
            "parameters、missing_information、is_follow_up。",
            "分析附件并用 JSON 输出风险列表。",
        )
        self.assertEqual(value, "分析附件并用 JSON 输出风险列表。")
        self.assertNotIn("standalone_request", value)

    def test_preserves_parser_fields_explicitly_requested_by_user(self) -> None:
        gateway = ModelGateway()
        original = (
            "请输出 JSON，字段为 standalone_request、intent、parameters、"
            "missing_information、is_follow_up。"
        )
        value = gateway._sanitize_standalone_request(original, original)
        self.assertEqual(value, original)


if __name__ == "__main__":
    unittest.main()
