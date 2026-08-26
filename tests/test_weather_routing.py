from __future__ import annotations

import unittest

from app.services.agent_runtime import AgentRuntime


class WeatherRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = object.__new__(AgentRuntime)

    def test_unrelated_message_is_not_polluted_by_old_weather_history(self) -> None:
        history = [
            {"role": "user", "content": "今天天气怎么样"},
            {"role": "assistant", "content": "请告诉我需要查询的城市或地区。"},
            {"role": "user", "content": "宁波"},
            {"role": "assistant", "content": "宁波今天预计有雷暴。"},
        ]
        request = self.runtime._weather_request("我有个亲戚带了两个小朋友来玩，要怎么招待他", history)
        self.assertIsNone(request)

    def test_city_is_accepted_only_as_immediate_weather_follow_up(self) -> None:
        history = [
            {"role": "user", "content": "今天天气怎么样"},
            {"role": "assistant", "content": "请告诉我需要查询的城市或地区，例如宁波。"},
        ]
        self.assertEqual(self.runtime._weather_request("宁波", history), {"city": "宁波", "day": "today"})

    def test_short_date_follow_up_reuses_city_after_completed_forecast(self) -> None:
        history = [
            {"role": "user", "content": "今天天气怎么样"},
            {"role": "user", "content": "宁波"},
            {"role": "assistant", "content": "宁波今天（2026-08-21）天气：雷暴。气温 26～31℃，降水概率 100%。"},
        ]
        self.assertEqual(
            self.runtime._weather_request("明天呢", history),
            {"city": "宁波", "day": "tomorrow"},
        )

    def test_intent_weather_route_accepts_short_date_follow_up(self) -> None:
        history = [
            {"role": "user", "content": "宁波今天天气怎么样"},
            {"role": "assistant", "content": "宁波今天（2026-08-21）天气：雷暴。气温 26～31℃，降水概率 100%。"},
        ]
        self.assertEqual(
            self.runtime._weather_request_for_intent(
                "明天呢",
                "明天呢",
                history,
                {"intent": "general", "parameters": {}, "source": "model"},
            ),
            {"city": "宁波", "day": "tomorrow"},
        )

    def test_document_request_replaces_pending_weather_clarification(self) -> None:
        history = [
            {"role": "user", "content": "今天天气怎么样"},
            {"role": "assistant", "content": "请告诉我需要查询的城市或地区。"},
        ]
        message = "把前面的结果整理成 Word 文档"
        self.assertIsNone(self.runtime._weather_request(message, history))
        self.assertIsNone(self.runtime._weather_request_for_intent(
            message,
            message,
            history,
            {"intent": "weather_query", "source": "model"},
        ))

    def test_non_city_acknowledgement_does_not_continue_weather(self) -> None:
        history = [
            {"role": "user", "content": "今天天气怎么样"},
            {"role": "assistant", "content": "请告诉我需要查询的城市或地区。"},
        ]
        for message in ("好的", "继续", "谢谢", "算了", "不用了"):
            with self.subTest(message=message):
                self.assertIsNone(self.runtime._weather_request(message, history))

    def test_weather_day_is_not_hard_coded_to_tomorrow(self) -> None:
        self.assertEqual(self.runtime._weather_request("宁波今天天气怎么样", []), {"city": "宁波", "day": "today"})
        self.assertEqual(self.runtime._weather_request("宁波明天天气怎么样", []), {"city": "宁波", "day": "tomorrow"})
        self.assertEqual(self.runtime._weather_request("宁波后天天气怎么样", []), {"city": "宁波", "day": "day_after_tomorrow"})

    def test_negated_weather_request_does_not_call_weather_tool(self) -> None:
        self.assertIsNone(self.runtime._weather_request("我在宁波，今天先不用查天气", []))

    def test_negated_weather_word_does_not_turn_execution_plan_into_weather(self) -> None:
        plan = self.runtime._build_execution_plan(
            {
                "message": "请说明自动化任务和无限循环的区别，不要查询天气。",
                "resolved_message": "请说明自动化任务和无限循环的区别，不要查询天气。",
                "intent_resolution": {
                    "standalone_request": "请说明自动化任务和无限循环的区别，不要查询天气。",
                    "intent": "weather_query",
                    "parameters": {},
                    "source": "model",
                },
                "attachments_json": "[]",
            },
            [{"id": "general_task", "name": "通用任务处理 Skill", "required_mcps": []}],
            "",
            False,
        )
        self.assertNotIn("weather", plan["allowed_servers"])
        self.assertEqual(plan["nodes"][0]["title"], "确认当前目标与约束")
        self.assertEqual(plan["nodes"][-2]["title"], "生成任务结果")

    def test_rain_question_extracts_city(self) -> None:
        self.assertEqual(self.runtime._weather_request("查询宁波明天是否下雨", []), {"city": "宁波", "day": "tomorrow"})

    def test_model_weather_label_alone_cannot_override_document_goal(self) -> None:
        message = "把杭州行程整理成 Word，包含天气不好时的室内推荐"
        plan = self.runtime._build_execution_plan(
            {
                "message": message,
                "resolved_message": message,
                "intent_resolution": {
                    "standalone_request": message,
                    "intent": "weather_query",
                    "parameters": {"city": "杭州", "format": "docx"},
                    "source": "model",
                },
            },
            [{"id": "bad-weather-match", "name": "误匹配 Skill", "required_mcps": ["weather"]}],
            "docx",
            False,
        )
        self.assertEqual(plan["allowed_servers"], ["report"])
        self.assertFalse(plan["weather_lookup_required"])

    def test_formatting_previous_forecast_does_not_requery_weather(self) -> None:
        message = "把刚才的天气预报整理成 Word 文档"
        plan = self.runtime._build_execution_plan(
            {
                "message": message,
                "resolved_message": message,
                "intent_resolution": {
                    "standalone_request": message,
                    "intent": "create_document",
                    "parameters": {"format": "docx"},
                },
            },
            [],
            "docx",
            False,
        )
        self.assertEqual(plan["allowed_servers"], ["report"])
        self.assertFalse(plan["weather_lookup_required"])

    def test_explicit_weather_lookup_can_feed_requested_document(self) -> None:
        message = "查询宁波明天天气并生成 Word 文档"
        plan = self.runtime._build_execution_plan(
            {
                "message": message,
                "resolved_message": message,
                "intent_resolution": {
                    "standalone_request": message,
                    "intent": "weather_document",
                    "parameters": {"city": "宁波", "day": "tomorrow", "format": "docx"},
                },
            },
            [],
            "docx",
            False,
        )
        self.assertEqual(plan["allowed_servers"], ["report", "weather"])
        self.assertTrue(plan["weather_lookup_required"])


if __name__ == "__main__":
    unittest.main()
