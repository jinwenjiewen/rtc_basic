import copy
import importlib
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from config import settings
from services.intention import IntentionResult, TEXT_RESPONSE_INSTRUCTION


class ChatIntentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 导入应用不依赖开发者的本地密钥；所有服务调用均被替换。
        with patch.multiple(settings, ARK_API_KEY="test-main-key", ARK_BASE_URL="https://main.example/api/v3"):
            self.main = importlib.import_module("main")
        # Pipeline unit tests isolate authentication; test_auth.py covers its real boundaries.
        from auth import get_current_auth, require_rtc_callback
        original_overrides = self.main.app.dependency_overrides.copy()
        self.main.app.dependency_overrides[get_current_auth] = lambda: None
        self.main.app.dependency_overrides[require_rtc_callback] = lambda: None
        self.addCleanup(lambda: setattr(self.main.app, "dependency_overrides", original_overrides))
        self.result = IntentionResult(rewritten_query="Python 课程介绍图片", output_type="image")
        self.analyze = AsyncMock(return_value=self.result)
        self.retrieve = AsyncMock(return_value="课程资料")
        self.stream = Mock(side_effect=lambda *args, **kwargs: iter([
            {"choices": [{"delta": {"content": "暂不支持图片，可以用文字介绍。"}}]},
            {"choices": [], "usage": {"total_tokens": 10}},
        ]))
        for target, attribute, replacement in [
            (self.main.intention_service, "analyze", self.analyze),
            (self.main.rag_service, "retrieve", self.retrieve),
            (self.main.llm_service, "chat_stream", self.stream),
        ]:
            patcher = patch.object(target, attribute, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.main.app), base_url="http://testserver"
        )
        self.history = [{"role": "user", "content": "我想了解 Python 课程"}]
        self.question = "给我一张它的介绍图片"

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_debug_chat_uses_rewrite_and_preserves_original_conversation(self):
        response = await self.client.post("/debug/chat", json={
            "history": self.history, "question": self.question,
        })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("暂不支持图片", payload["text"])
        self.assertEqual(payload["usage"], {"total_tokens": 10})
        self.assertEqual(payload["intention"], self.result.model_dump())
        self.assert_pipeline(self.history + [{"role": "user", "content": self.question}])

    async def test_rtc_sse_keeps_protocol_and_uses_same_pipeline(self):
        messages = self.history + [{"role": "user", "content": self.question}]
        original = copy.deepcopy(messages)
        response = await self.client.post("/api/chat_callback", json={"messages": messages})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        events = [event for event in response.text.split("\n\n") if event]
        self.assertEqual(events[-1], "data: [DONE]")
        chunk = json.loads(events[0].removeprefix("data: "))
        self.assertIn("暂不支持图片", chunk["choices"][0]["delta"]["content"])
        self.assert_pipeline(original)
        self.assertEqual(messages, original)

    def assert_pipeline(self, messages):
        self.analyze.assert_awaited_once_with(self.question, self.history)
        self.retrieve.assert_awaited_once_with(self.result.rewritten_query)
        self.stream.assert_called_once_with(
            messages, "课程资料", response_instruction=TEXT_RESPONSE_INSTRUCTION
        )

    async def test_debug_intention_calls_only_small_model(self):
        response = await self.client.post("/debug/intention", json={
            "history": self.history, "question": self.question,
        })
        self.assertEqual(response.json(), self.result.model_dump())
        self.analyze.assert_awaited_once_with(self.question, self.history)
        self.retrieve.assert_not_awaited()
        self.stream.assert_not_called()

    async def test_rtc_empty_or_non_text_input_returns_done_without_model_calls(self):
        for messages in ([], [{"role": "user", "content": " "}], [{"role": "user", "content": []}]):
            response = await self.client.post("/api/chat_callback", json={"messages": messages})
            self.assertEqual(response.text, "data: [DONE]\n\n")
        self.analyze.assert_not_awaited()
        self.retrieve.assert_not_awaited()

    async def test_fallback_still_retrieves_original_question(self):
        self.analyze.return_value = IntentionResult(
            rewritten_query=self.question, output_type="text", used_fallback=True
        )
        response = await self.client.post("/debug/chat", json={"question": self.question})
        self.assertTrue(response.json()["intention"]["used_fallback"])
        self.retrieve.assert_awaited_once_with(self.question)
