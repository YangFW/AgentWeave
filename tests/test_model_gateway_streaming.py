from __future__ import annotations

import json
import unittest

import httpx

from app.services.model_gateway import ModelGateway


class ModelGatewayStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_json_response_falls_back_when_stream_is_ignored(self) -> None:
        captured_payload: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "普通 JSON 回答"}}
                    ]
                },
            )

        deltas: list[str] = []

        async def on_delta(text: str) -> None:
            deltas.append(text)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            message = await ModelGateway()._stream_completion(
                client,
                "https://model.example.test/v1/chat/completions",
                {"model": "compatible-model", "messages": []},
                {"Authorization": "Bearer test-key"},
                on_delta,
            )

        self.assertTrue(captured_payload["stream"])
        self.assertEqual(message["content"], "普通 JSON 回答")
        self.assertEqual(deltas, ["普通 JSON 回答"])

    async def test_plain_json_tool_call_is_preserved(self) -> None:
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "weather_lookup", "arguments": '{"city":"宁波"}'},
        }

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [tool_call],
                            }
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            message = await ModelGateway()._stream_completion(
                client,
                "https://model.example.test/v1/chat/completions",
                {"model": "compatible-model", "messages": []},
                {},
                None,
            )

        self.assertEqual(message["tool_calls"], [tool_call])

    async def test_sse_response_still_forwards_each_delta(self) -> None:
        body = "\n".join(
            (
                'data: {"choices":[{"delta":{"content":"实时"}}]}',
                'data: {"choices":[{"delta":{"content":"回答"}}]}',
                "data: [DONE]",
                "",
            )
        )

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        deltas: list[str] = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            message = await ModelGateway()._stream_completion(
                client,
                "https://model.example.test/v1/chat/completions",
                {"model": "compatible-model", "messages": []},
                {},
                deltas.append,
            )

        self.assertEqual(message["content"], "实时回答")
        self.assertEqual(deltas, ["实时", "回答"])


if __name__ == "__main__":
    unittest.main()
