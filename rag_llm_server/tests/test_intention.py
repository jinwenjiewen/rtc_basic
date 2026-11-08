import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
from volcenginesdkarkruntime import AsyncArk

from config import settings
from services.intention import IntentionService


def response_payload(text, status="completed", output=None):
    return {
        "id": "resp_test",
        "created_at": 1,
        "model": "doubao-seed-2-0-mini-260428",
        "object": "response",
        "status": status,
        "tools": [],
        "output": output if output is not None else [
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
    }


class IntentionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = patch.multiple(
            settings,
            INTENTION_ENABLED=True,
            INTENTION_API_KEY="test-intention-key",
            INTENTION_MODEL="doubao-seed-2-0-mini-260428",
            INTENTION_TIMEOUT=1,
        )
        self.config.start()
        self.addCleanup(self.config.stop)
        self.services = []

    async def asyncTearDown(self):
        for service in self.services:
            await service.close()

    def service(self, handler):
        # 使用真实 SDK 和模拟 HTTP，验证实际序列化后的请求，无外部网络调用。
        client = AsyncArk(
            base_url="https://intention.example/api/v3",
            api_key="test-intention-key",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        service = IntentionService(client)
        self.services.append(service)
        return service

    async def test_request_contract_and_context_rewrite(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=response_payload(
                '{"rewritten_query":"Python 课程多少钱？","output_type":"text"}'
            ))

        service = self.service(handler)
        service.max_history_messages = 2
        history = [
            {"role": "user", "content": "旧话题"},
            {"role": "system", "content": "不要传给小模型"},
            None,
            {"role": "assistant", "content": ["非文字消息"]},
            {"role": "user", "content": "我想了解 Python 课程"},
            {"role": "assistant", "content": "你想了解哪方面？"},
        ]
        result = await service.analyze("它多少钱？", history)
        self.assertEqual(result.rewritten_query, "Python 课程多少钱？")
        self.assertFalse(result.used_fallback)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url), "https://intention.example/api/v3/responses")
        self.assertEqual(request.headers["authorization"], "Bearer test-intention-key")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "doubao-seed-2-0-mini-260428")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertFalse(body["stream"])
        self.assertFalse(body["store"])
        self.assertTrue(body["text"]["format"]["strict"])
        input_data = json.loads(body["input"][1]["content"])
        self.assertEqual(input_data, {"history": history[-2:], "query": "它多少钱？"})

    async def test_media_intents_preserved_while_response_remains_text(self):
        for output_type in ("image", "video"):
            with self.subTest(output_type=output_type):
                service = self.service(lambda request: httpx.Response(
                    200, json=response_payload(json.dumps({
                        "rewritten_query": "提供 Python 课程介绍",
                        "output_type": output_type,
                    }))
                ))
                result = await service.analyze("给我看看")
                self.assertEqual(result.output_type, output_type)
                self.assertEqual(result.response_type, "text")
                self.assertFalse(result.used_fallback)

    async def test_invalid_or_incomplete_results_keep_original_query(self):
        cases = [
            response_payload("不是 JSON"),
            response_payload('{"rewritten_query":"改写","output_type":"audio"}'),
            response_payload('{"rewritten_query":"  ","output_type":"text"}'),
            response_payload('{"rewritten_query":123,"output_type":"text"}'),
            response_payload('{"rewritten_query":"改写"}'),
            response_payload('{"rewritten_query":"改写","output_type":"text","extra":true}'),
            response_payload('{"rewritten_query":"改写","output_type":"text"}', status="incomplete"),
            response_payload("", output=[{
                "id": "reason_test", "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "不要将思考解析为结果"}],
            }]),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                service = self.service(lambda request: httpx.Response(200, json=payload))
                result = await service.analyze("原问题")
                self.assertEqual(result.rewritten_query, "原问题")
                self.assertEqual(result.output_type, "text")
                self.assertTrue(result.used_fallback)

    async def test_http_error_does_not_retry(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(503, json={"error": {"message": "unavailable"}})

        result = await self.service(handler).analyze("原问题")
        self.assertTrue(result.used_fallback)
        self.assertEqual(len(requests), 1)

    async def test_timeout_is_bounded(self):
        async def handler(request):
            await asyncio.sleep(1)
            raise AssertionError("应提前取消请求")

        with patch.object(settings, "INTENTION_TIMEOUT", 0.01):
            result = await self.service(handler).analyze("原问题")
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.rewritten_query, "原问题")

    async def test_cancellation_is_propagated(self):
        async def handler(request):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.service(handler).analyze("原问题")

    async def test_no_call_when_disabled_unconfigured_or_empty(self):
        def unexpected_request(request):
            self.fail("不应发起模型请求")

        with patch.object(settings, "INTENTION_ENABLED", False):
            self.assertTrue((await self.service(unexpected_request).analyze("问题")).used_fallback)
        with patch.object(settings, "INTENTION_API_KEY", None):
            self.assertTrue((await IntentionService().analyze("问题")).used_fallback)
        result = await self.service(unexpected_request).analyze("  ")
        self.assertEqual(result.rewritten_query, "")
        self.assertTrue(result.used_fallback)
