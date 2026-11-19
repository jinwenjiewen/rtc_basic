from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from backend_harness_loop.probes.model import (
    ProbeProtocolError,
    collect_stream,
    run_model_probe,
    visible_final_text,
)


class FragmentedBody(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self.body = body

    async def __aiter__(self):
        for offset in range(0, len(self.body), 7):
            yield self.body[offset : offset + 7]


def sse(*values, done=True):
    body = "".join("data: " + json.dumps(value, ensure_ascii=False) + "\n\n" for value in values)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode("utf8")


def delta(value, reason=None):
    return {"choices": [{"index": 0, "delta": value, "finish_reason": reason}]}


def tool_stream(name, arguments, *, call_id="call_fixture", text=""):
    midpoint = len(arguments) // 2
    values = [
        delta({"content": text, "tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": arguments[:midpoint]}}]}),
        delta({"tool_calls": [{"index": 0, "function": {"arguments": arguments[midpoint:]}}]}),
        delta({}, "tool_calls"),
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]
    return sse(*values)


def text_stream(text):
    return sse(delta({"content": text, "reasoning_content": "DO_NOT_STORE_REASONING"}), delta({}, "stop"), {"choices": [], "usage": {"total_tokens": 7}})


def settings():
    return SimpleNamespace(
        ark_api_key="DO_NOT_PRINT_SECRET",
        ark_base_url="https://ark.example/api/v3",
        ark_model="fixture-model",
        model_timeout_seconds=1,
        first_output_deadline_ms=6000,
        trust_env=False,
    )


class ModelStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, body):
        async def handler(request):
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=FragmentedBody(body))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async with client.stream("GET", "https://fixture.example") as response:
                return await collect_stream(response, started_at=time.perf_counter())

    async def test_fragmented_http_and_arguments_are_reconstructed(self):
        observed = await self.collect(tool_stream("m0_lookup_fixture", '{"query":"fixture_course"}'))
        self.assertEqual(json.loads(observed.tool_calls[0]["function"]["arguments"]), {"query": "fixture_course"})
        self.assertEqual(observed.argument_fragment_counts, {0: 2})
        self.assertEqual(observed.tool_calls[0]["id"], "call_fixture")
        self.assertEqual(observed.usage["total_tokens"], 15)

    async def test_mixed_text_and_tools_cannot_be_final_speech(self):
        observed = await self.collect(tool_stream("prepare_response", '{"mode":"answer","evidence_refs":[]}', text="内部草稿"))
        self.assertTrue(observed.mixed_text_and_tools)
        with self.assertRaisesRegex(ProbeProtocolError, "tool_or_control_output"):
            visible_final_text(observed)

    async def test_unicode_text_and_hidden_fields_are_separated(self):
        observed = await self.collect(text_stream("你好，欢迎咨询。"))
        self.assertEqual(visible_final_text(observed), "你好，欢迎咨询。")
        self.assertNotIn("DO_NOT_STORE_REASONING", repr(observed))

    async def test_missing_done_is_not_success(self):
        with self.assertRaisesRegex(ProbeProtocolError, "missing_done_marker"):
            await self.collect(sse(delta({"content": "partial"}), delta({}, "stop"), done=False))

    async def test_length_stop_is_incomplete(self):
        with self.assertRaisesRegex(ProbeProtocolError, "incomplete_model_generation"):
            await self.collect(sse(delta({"content": "partial"}), delta({}, "length")))

    async def test_provider_error_frame_is_sanitized(self):
        with self.assertRaisesRegex(ProbeProtocolError, "provider_stream_error") as caught:
            await self.collect(sse({"error": {"message": "DO_NOT_PRINT_SECRET"}}))
        self.assertNotIn("DO_NOT_PRINT_SECRET", str(caught.exception))

    async def test_invalid_json_is_rejected(self):
        with self.assertRaisesRegex(ProbeProtocolError, "invalid_stream_json"):
            await self.collect(b"data: {bad}\n\ndata: [DONE]\n\n")


class LiveModelProbeTests(unittest.IsolatedAsyncioTestCase):
    def client_factory(self, handler):
        return lambda *args, **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_model_probe_roundtrip_and_two_stage_contract(self):
        requests = []

        async def handler(request):
            value = json.loads(request.content)
            requests.append(value)
            self.assertNotIn("thinking", value)
            self.assertEqual(request.url.path, "/api/v3/chat/completions")
            if len(requests) == 1:
                body = tool_stream("m0_lookup_fixture", '{"query":"fixture_course"}')
            elif len(requests) == 2:
                tool = value["messages"][-1]
                self.assertEqual(tool["role"], "tool")
                self.assertEqual(tool["tool_call_id"], "call_fixture")
                self.assertIn("M0_RESULT_42", tool["content"])
                self.assertNotIn("tools", value)
                body = text_stream("M0_RESULT_42")
            elif len(requests) == 3:
                self.assertEqual(value["tools"][0]["function"]["name"], "prepare_response")
                body = tool_stream("prepare_response", '{"mode":"answer","evidence_refs":[]}', call_id="call_prepare", text="未播报草稿")
            else:
                self.assertNotIn("tools", value)
                self.assertNotIn("tool_choice", value)
                self.assertEqual(value["messages"][-1]["tool_call_id"], "call_prepare")
                body = text_stream("你好！")
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=FragmentedBody(body))

        with patch("backend_harness_loop.probes.model._make_client", self.client_factory(handler)):
            result = await run_model_probe(settings(), samples=1)
        self.assertEqual(result["status"], "passed", result)
        evidence = result["evidence"]
        self.assertEqual(evidence["request_count"], 4)
        self.assertTrue(evidence["native_tool_roundtrip"]["multi_fragment_observed"])
        self.assertEqual(evidence["two_stage"]["prepare_response_samples"], 1)
        self.assertEqual(evidence["two_stage"]["first_text_latency"]["sample_count"], 1)
        self.assertFalse(evidence["two_stage"]["decision_text_forwarded_to_speech"])
        self.assertEqual(evidence["single_stage_gate"]["status"], "blocked")
        self.assertEqual(evidence["usage_total"]["total_tokens"], 44)
        self.assertNotIn("DO_NOT_PRINT_SECRET", json.dumps(result))
        self.assertNotIn("DO_NOT_STORE_REASONING", json.dumps(result))
        self.assertNotIn("未播报草稿", json.dumps(result, ensure_ascii=False))

    async def test_draft_decision_fallback_still_disables_final_tools(self):
        count = 0

        async def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                body = tool_stream("m0_lookup_fixture", '{"query":"fixture_course"}')
            elif count == 2:
                body = text_stream("M0_RESULT_42")
            elif count == 3:
                body = text_stream("DRAFT_NOT_SPEECH")
            else:
                self.assertNotIn("tools", json.loads(request.content))
                body = text_stream("你好")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

        with patch("backend_harness_loop.probes.model._make_client", self.client_factory(handler)):
            result = await run_model_probe(settings(), samples=1)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["evidence"]["two_stage"]["draft_fallback_samples"], 1)

    async def test_missing_configuration_blocks_without_network(self):
        config = settings()
        config.ark_api_key = ""
        result = await run_model_probe(config)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["evidence"]["request_count"], 0)

    async def test_three_samples_stay_within_eight_requests(self):
        count = 0

        async def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                body = tool_stream("m0_lookup_fixture", '{"query":"fixture_course"}')
            elif count == 2:
                body = text_stream("M0_RESULT_42")
            elif count % 2:
                body = tool_stream("prepare_response", '{"mode":"answer","evidence_refs":[]}', call_id="call_prepare")
            else:
                self.assertNotIn("tools", json.loads(request.content))
                body = text_stream("你好")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

        with patch("backend_harness_loop.probes.model._make_client", self.client_factory(handler)):
            result = await run_model_probe(settings(), samples=3)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(count, 8)
        self.assertEqual(result["evidence"]["two_stage"]["first_text_latency"]["sample_count"], 3)

    async def test_invalid_sample_count_makes_no_requests(self):
        result = await run_model_probe(settings(), samples=4)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["evidence"]["request_count"], 0)

    async def test_http_failure_never_records_response_body(self):
        async def handler(request):
            return httpx.Response(400, json={"error": {
                "code": "InvalidParameter",
                "type": "invalid_request_error",
                "param": "tool_choice",
                "message": "DO_NOT_PRINT_SECRET",
                "request_id": "DO_NOT_PRINT_SECRET",
            }})

        with patch("backend_harness_loop.probes.model._make_client", self.client_factory(handler)):
            result = await run_model_probe(settings(), samples=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["evidence"]["http_status"], 400)
        self.assertEqual(result["evidence"]["provider_error_code"], "InvalidParameter")
        self.assertEqual(result["evidence"]["provider_error_type"], "invalid_request_error")
        self.assertEqual(result["evidence"]["provider_error_param"], "tool_choice")
        self.assertNotIn("DO_NOT_PRINT_SECRET", json.dumps(result))

    async def test_explicit_thinking_mode_is_sent_on_every_phase(self):
        for thinking in ("disabled", "enabled"):
            with self.subTest(thinking=thinking):
                count = 0

                async def handler(request):
                    nonlocal count
                    count += 1
                    value = json.loads(request.content)
                    self.assertEqual(value["thinking"], {"type": thinking})
                    if count == 1:
                        body = tool_stream("m0_lookup_fixture", '{"query":"fixture_course"}')
                    elif count == 2:
                        body = text_stream("M0_RESULT_42")
                    elif count == 3:
                        body = tool_stream("prepare_response", '{"mode":"answer","evidence_refs":[]}', call_id="call_prepare")
                    else:
                        self.assertNotIn("tools", value)
                        body = text_stream("你好")
                    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

                config = settings()
                config.model_thinking = thinking
                with patch("backend_harness_loop.probes.model._make_client", self.client_factory(handler)):
                    result = await run_model_probe(config, samples=1)
                self.assertEqual(result["status"], "passed", result)
                self.assertEqual(count, 4)
                self.assertEqual(result["evidence"]["thinking"], thinking)

    async def test_unknown_provider_error_fields_are_not_reported(self):
        async def handler(request):
            return httpx.Response(400, json={"error": {
                "code": "DO_NOT_PRINT_SECRET", "type": "DO_NOT_PRINT_SECRET",
                "param": "DO_NOT_PRINT_SECRET", "message": "DO_NOT_PRINT_SECRET",
            }})

        with patch("backend_harness_loop.probes.model._make_client", self.client_factory(handler)):
            result = await run_model_probe(settings(), samples=1)
        self.assertNotIn("DO_NOT_PRINT_SECRET", json.dumps(result))
        self.assertNotIn("provider_error_code", result["evidence"])

    async def test_invalid_thinking_mode_is_rejected_without_network(self):
        config = settings()
        config.model_thinking = "unsupported"
        result = await run_model_probe(config, samples=1)
        self.assertEqual(result["evidence"]["error_code"], "invalid_model_thinking_configuration")
        self.assertEqual(result["evidence"]["request_count"], 0)

    async def test_timeout_is_bounded_and_sanitized(self):
        async def handler(request):
            raise httpx.ReadTimeout("DO_NOT_PRINT_SECRET", request=request)

        with patch("backend_harness_loop.probes.model._make_client", self.client_factory(handler)):
            result = await run_model_probe(settings(), samples=1)
        self.assertEqual(result["evidence"]["error_code"], "model_request_timeout")
        self.assertEqual(result["evidence"]["request_count"], 1)
        self.assertNotIn("DO_NOT_PRINT_SECRET", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
