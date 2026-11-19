"""M1 model adapter tests use synthetic SSE and injected HTTP transports only."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from backend_harness_loop.models import (
    ChatCompletionsModel, ModelConfigurationError, ModelProfile, ModelProfiles,
    ModelProtocolError, ModelTransportError, load_model_environment,
)


def configuration():
    return {"schema_version": 1, "default_profile": "flash", "providers": {
        "pro_service": {"base_url": "https://pro.example/v1", "api_key_env": "PRO_KEY", "adapter": "openai_chat"},
        "flash_service": {"base_url": "https://flash.example/v9", "api_key_env": "FLASH_KEY", "adapter": "openai_chat"},
    }, "profiles": {
        "pro": {"provider": "pro_service", "model": "deepseek-v4-pro", "extra_body": {"thinking": {"type": "disabled"}}},
        "flash": {"provider": "flash_service", "model": "third-party-flash-alias", "include_usage": False},
    }}


def profile(**overrides):
    return ModelProfile(**({"profile_id": "flash", "provider_id": "gateway", "base_url": "https://gateway.example/v1", "model": "deepseek-v4-flash", "api_key_env": "FLASH_KEY", "api_key": "fixture-private-key"} | overrides))


def event(delta=None, finish=None, **extra):
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **extra}, ensure_ascii=False) + "\n\n"


def payload(*items, done=True):
    return ("".join(items) + ("data: [DONE]\n\n" if done else "")).encode("utf-8")


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks, gate=None):
        self.chunks = chunks
        self.gate = gate
        self.closed = False

    async def __aiter__(self):
        for index, part in enumerate(self.chunks):
            if index and self.gate is not None:
                await self.gate.wait()
            yield part

    async def aclose(self):
        self.closed = True


class ModelProfilesTests(unittest.TestCase):
    def load(self, data, env=None):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profiles.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return ModelProfiles.load(path, environ={} if env is None else env)

    def test_models_endpoints_and_keys_are_independently_replaceable(self):
        registry = self.load(configuration(), {"PRO_KEY": "fixture-pro-secret", "FLASH_KEY": "fixture-flash-secret"})
        self.assertEqual(registry.default_profile_id, "flash")
        self.assertEqual(registry.get().model, "third-party-flash-alias")
        self.assertEqual(registry.get("pro").endpoint, "https://pro.example/v1/chat/completions")
        self.assertEqual(registry.get("flash").endpoint, "https://flash.example/v9/chat/completions")
        self.assertEqual(registry.get("pro").api_key, "fixture-pro-secret")
        inventory = json.dumps(registry.public_inventory())
        self.assertNotIn("fixture-pro-secret", inventory)
        self.assertNotIn("fixture-flash-secret", inventory)
        self.assertNotIn("fixture-pro-secret", repr(registry.get("pro")))
        self.assertEqual(set(registry.secret_values()), {"fixture-pro-secret", "fixture-flash-secret"})

    def test_final_tool_schema_is_explicit_and_does_not_change_model_routing(self):
        data = configuration()
        data["profiles"]["pro"]["final_tool_schema"] = "empty"
        registry = self.load(data, {"PRO_KEY": "pro-key", "FLASH_KEY": "flash-key"})
        self.assertEqual(registry.get("pro").final_tool_schema, "empty")
        self.assertEqual(registry.get("flash").final_tool_schema, "omit")
        self.assertEqual(registry.get("pro").model, "deepseek-v4-pro")
        data["profiles"]["pro"]["final_tool_schema"] = "enabled"
        with self.assertRaisesRegex(ModelConfigurationError, "invalid_model_final_tool_schema"):
            self.load(data)

    def test_missing_keys_are_visible_without_reusing_an_unrelated_provider_key(self):
        registry = self.load(configuration(), {"ARK_API_KEY": "unrelated-secret"})
        self.assertFalse(registry.get().public_inventory()["configured"])
        with self.assertRaisesRegex(ModelConfigurationError, "model_api_key_missing"):
            registry.get().validate_ready()

    def test_direct_completion_endpoint_is_not_appended_twice(self):
        self.assertEqual(profile(base_url="https://proxy.example/api/chat/completions/").endpoint, "https://proxy.example/api/chat/completions")

    def test_decision_protocol_is_explicit_and_defaults_to_compatible_auto(self):
        data = configuration()
        self.assertEqual(self.load(data).get("pro").decision_tool_choice, "auto")
        data["profiles"]["pro"]["decision_tool_choice"] = "required"
        self.assertEqual(self.load(data).get("pro").decision_tool_choice, "required")
        for invalid in ("none", True, {}, "forced"):
            data["profiles"]["pro"]["decision_tool_choice"] = invalid
            with self.assertRaisesRegex(ModelConfigurationError, "invalid_model_decision_tool_choice"):
                self.load(data)

    def test_environment_overlay_is_explicit_without_interpolation_or_mutation(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"PRO_KEY": "os-secret"}, clear=True):
            base, overlay = Path(folder) / "base.env", Path(folder) / "overlay.env"
            base.write_text("PRO_KEY=base-secret\nFLASH_KEY=base-flash\n", encoding="utf-8")
            overlay.write_text("PRO_KEY=overlay-secret\nFLASH_KEY=overlay-flash\nLITERAL=${PRO_KEY}\n", encoding="utf-8")
            env = load_model_environment(base, overlay)
            self.assertEqual(env["PRO_KEY"], "os-secret")
            self.assertEqual(env["FLASH_KEY"], "overlay-flash")
            self.assertEqual(env["LITERAL"], "${PRO_KEY}")
            self.assertNotIn("FLASH_KEY", os.environ)
            with self.assertRaisesRegex(ModelConfigurationError, "model_environment_file_unreadable"):
                load_model_environment(Path(folder) / "missing.env")

    def test_configuration_rejects_credential_urls_inline_keys_and_reserved_body_fields(self):
        for url in ("http://gateway.example/v1", "https://u:secret@gateway.example", "https://gateway.example?a=secret", "https://gateway.example/#secret", "https://gateway.example:bad", "https://gateway.example?", " https://gateway.example"):
            data = configuration()
            data["providers"]["pro_service"]["base_url"] = url
            with self.subTest(case="url"), self.assertRaises(ModelConfigurationError):
                self.load(data)
        for name in ("messages", "model", "stream", "tools", "tool_choice", "max_tokens", "max_completion_tokens", "stream_options"):
            data = configuration()
            data["profiles"]["pro"]["extra_body"] = {name: "private-value"}
            with self.subTest(field=name), self.assertRaisesRegex(ModelConfigurationError, "invalid_model_extra_body"):
                self.load(data)
        data = configuration()
        data["providers"]["pro_service"]["api_key"] = "private-inline-key"
        with self.assertRaises(ModelConfigurationError) as failure:
            self.load(data)
        self.assertNotIn("private-inline-key", str(failure.exception))
        data = configuration()
        data["profiles"]["pro"]["extra_body"] = {"nested": {"api_key": "private-inline-key"}}
        with self.assertRaisesRegex(ModelConfigurationError, "credentials_in_model_extra_body"):
            self.load(data)

    def test_configuration_rejects_unsupported_adapters_unknown_profiles_and_numeric_edges(self):
        mutations = [
            lambda d: d["providers"]["pro_service"].update(adapter="unknown"),
            lambda d: d.update(default_profile="missing"),
            lambda d: d["profiles"]["pro"].update(provider="missing"),
            lambda d: d["profiles"]["pro"].update(timeout_seconds=float("inf")),
            lambda d: d["profiles"]["pro"].update(max_tokens=True),
            lambda d: d["profiles"]["pro"].update(include_usage="true"),
        ]
        for number, mutate in enumerate(mutations):
            data = configuration(); mutate(data)
            with self.subTest(case=number), self.assertRaises(ModelConfigurationError):
                self.load(data)
        with self.assertRaisesRegex(ModelConfigurationError, "unknown_model_profile"):
            self.load(configuration()).get([])

    def test_duplicate_configuration_keys_and_oversized_or_missing_files_fail_safely(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profiles.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(ModelConfigurationError, "model_profiles_file_invalid"):
                ModelProfiles.load(path)
            path.write_text(" " * 262_145, encoding="utf-8")
            with self.assertRaises(ModelConfigurationError):
                ModelProfiles.load(path)
            with self.assertRaises(ModelConfigurationError):
                ModelProfiles.load(Path(folder) / "missing.json")

    def test_additional_header_secrets_come_from_environment_and_are_hidden(self):
        data = configuration()
        data["providers"]["flash_service"]["headers_env"] = {"X-Gateway-Token": "GATEWAY_SECRET"}
        registry = self.load(data, {"FLASH_KEY": "key", "GATEWAY_SECRET": "fixture-gateway-secret"})
        self.assertEqual(registry.get().headers["X-Gateway-Token"], "fixture-gateway-secret")
        self.assertNotIn("fixture-gateway-secret", repr(registry.get()))
        self.assertNotIn("fixture-gateway-secret", json.dumps(registry.public_inventory()))
        data["providers"]["flash_service"]["headers_env"] = {"Authorization": "GATEWAY_SECRET"}
        with self.assertRaises(ModelConfigurationError):
            self.load(data)


class ModelTransportTests(unittest.IsolatedAsyncioTestCase):
    def make(self, raw, *, selected=None, status=200, stream=None):
        self.requests = []
        self.byte_stream = stream or ByteStream([raw])
        def handler(request):
            self.requests.append(request)
            return httpx.Response(status, stream=self.byte_stream)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return ChatCompletionsModel(selected or profile(), client=client)

    async def test_decision_collects_utf8_and_native_fragmented_tools_without_leaking_reasoning(self):
        raw = payload(
            event({"reasoning_content": "PRIVATE THINKING", "content": "内部草稿"}),
            event({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "read_", "arguments": '{"query":"'}}]}),
            event({"tool_calls": [{"index": 0, "function": {"name": "history", "arguments": '中文"}'}}]}, finish="tool_calls"),
            'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":7,"total_tokens":12}}\n\n',
        )
        stream = ByteStream([raw[i:i+3] for i in range(0, len(raw), 3)])
        model = self.make(raw, stream=stream)
        response = await model.complete([{"role": "user", "content": "fixture"}], tools=[{"type": "function", "function": {"name": "read_history"}}])
        self.assertEqual(response.content, "内部草稿")
        self.assertEqual(response.tool_calls[0]["function"], {"name": "read_history", "arguments": '{"query":"中文"}'})
        self.assertEqual(response.assistant_message["reasoning_content"], "PRIVATE THINKING")
        self.assertEqual(response.usage["total_tokens"], 12)
        self.assertNotIn("PRIVATE THINKING", repr(response))
        self.assertTrue(stream.closed)

    async def test_tool_results_are_sent_with_original_ids_and_same_selected_model(self):
        model = self.make(payload(event({"content": "answer"}, finish="stop")))
        messages = [{"role": "user", "content": "fixture"},
                    {"role": "assistant", "content": None, "tool_calls": [{"id": "native_77", "type": "function", "function": {"name": "read_history", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "native_77", "content": '{"status":"success"}'}]
        original = deepcopy(messages)
        await model.complete(messages)
        sent = json.loads(self.requests[0].content)
        self.assertEqual(sent["messages"], original)
        self.assertEqual(sent["model"], "deepseek-v4-flash")
        self.assertEqual(messages, original)
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer fixture-private-key")

    async def test_final_stream_yields_first_delta_before_network_stream_finishes(self):
        gate = asyncio.Event()
        stream = ByteStream([event({"content": "你好"}).encode(), payload(event({"content": "世界"}, finish="stop"))], gate=gate)
        model = self.make(b"", stream=stream)
        generated = model.stream_final([{"role": "user", "content": "fixture"}])
        self.assertEqual(await asyncio.wait_for(anext(generated), .5), "你好")
        gate.set()
        self.assertEqual(await anext(generated), "世界")
        with self.assertRaises(StopAsyncIteration):
            await anext(generated)
        sent = json.loads(self.requests[0].content)
        self.assertNotIn("tools", sent)
        self.assertEqual(sent["tool_choice"], "none")
        self.assertTrue(stream.closed)

    async def test_final_phase_rejects_tool_or_reasoning_before_yielding_same_frame_text(self):
        for delta in ({"content": "PRIVATE CONTROL", "tool_calls": [{"index": 0, "function": {"name": "write"}}]}, {"content": "PRIVATE CONTROL", "reasoning_content": "PRIVATE REASONING"}):
            model = self.make(payload(event(delta, finish="stop")))
            output = []
            with self.assertRaises(ModelProtocolError):
                async for text in model.stream_final([{"role": "user", "content": "fixture"}]):
                    output.append(text)
            self.assertEqual(output, [])
            self.assertTrue(self.byte_stream.closed)

    async def test_cancellation_closes_inflight_http_stream_and_is_not_reported_as_model_error(self):
        gate = asyncio.Event()
        stream = ByteStream([event({"role": "assistant"}).encode(), payload(event({"content": "late"}, finish="stop"))], gate=gate)
        model = self.make(b"", stream=stream)
        task = asyncio.create_task(model.complete([{"role": "user", "content": "fixture"}]))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream.closed)

    async def test_consumer_close_releases_final_stream_without_waiting_for_more_tokens(self):
        gate = asyncio.Event()
        stream = ByteStream([event({"content": "first"}).encode(), payload(event({"content": "late"}, finish="stop"))], gate=gate)
        model = self.make(b"", stream=stream)
        generated = model.stream_final([{"role": "user", "content": "fixture"}])
        self.assertEqual(await anext(generated), "first")
        await generated.aclose()
        self.assertTrue(stream.closed)

    async def test_usage_callback_is_request_local_and_final_content_contains_no_usage(self):
        model = self.make(payload(event({"content": "answer"}, finish="stop"), 'data: {"choices":[],"usage":{"total_tokens":9}}\n\n'))
        observations = []
        output = [text async for text in model.stream_final([{"role": "user", "content": "fixture"}], on_usage=observations.append)]
        self.assertEqual(output, ["answer"])
        self.assertEqual(observations, [{"total_tokens": 9}])

    async def test_http_errors_are_safe_and_redirects_never_forward_keys(self):
        for status in (302, 401, 429, 500):
            model = self.make(b"PRIVATE KEY AND PROVIDER BODY", status=status)
            with self.subTest(status=status), self.assertRaises(ModelTransportError) as failure:
                await model.complete([{"role": "user", "content": "fixture"}])
            self.assertEqual(failure.exception.status_code, status)
            self.assertEqual(str(failure.exception), "model_http_error")
            self.assertEqual(len(self.requests), 1)
            self.assertTrue(self.byte_stream.closed)

    async def test_overall_timeout_cancels_stream_and_closes_connection(self):
        stream = ByteStream([event({"role": "assistant"}).encode(), b""], gate=asyncio.Event())
        model = self.make(b"", selected=profile(timeout_seconds=.1), stream=stream)
        with self.assertRaisesRegex(ModelTransportError, "model_timeout"):
            await model.complete([{"role": "user", "content": "fixture"}])
        self.assertTrue(stream.closed)

    async def test_suspended_stream_timeout_does_not_cancel_consumer_work(self):
        stream = ByteStream([event({"content": "first"}).encode(), payload(event({"content": "late"}, finish="stop"))])
        model = self.make(b"", selected=profile(timeout_seconds=.1), stream=stream)
        generated = model.stream_final([{"role": "user", "content": "fixture"}])
        self.assertEqual(await anext(generated), "first")
        # A timeout that spans generator yields would cancel this consumer sleep.
        await asyncio.sleep(.15)
        with self.assertRaisesRegex(ModelTransportError, "model_timeout"):
            await anext(generated)
        self.assertTrue(stream.closed)

    async def test_missing_done_incomplete_finish_and_tool_finish_mismatch_are_protocol_errors(self):
        cases = [payload(event({"content": "partial"}, finish="stop"), done=False),
                 payload(event({"content": "partial"}, finish="length")),
                 payload(event({}, finish="tool_calls")),
                 payload(event({"tool_calls": [{"index": 0, "id": "call1", "function": {"name": "read_history", "arguments": "{}"}}]}, finish="stop"))]
        for index, raw in enumerate(cases):
            model = self.make(raw)
            with self.subTest(case=index), self.assertRaises(ModelProtocolError):
                await model.complete([{"role": "user", "content": "fixture"}])
            self.assertTrue(self.byte_stream.closed)

    async def test_oversized_arguments_frames_and_duplicate_native_ids_fail_closed(self):
        cases = [payload(event({"tool_calls": [{"index": 0, "id": "call1", "function": {"name": "read_history", "arguments": "x" * 16_385}}]}, finish="tool_calls")),
                 payload(event({"content": "x" * 131_073}, finish="stop")),
                 payload(event({"tool_calls": [{"index": n, "id": "same", "function": {"name": "read_history", "arguments": "{}"}} for n in (0, 1)]}, finish="tool_calls"))]
        for index, raw in enumerate(cases):
            model = self.make(raw)
            with self.subTest(case=index), self.assertRaises(ModelProtocolError):
                await model.complete([{"role": "user", "content": "fixture"}])

    async def test_invalid_request_or_missing_credentials_never_dispatches_http(self):
        model = self.make(b"", selected=profile(api_key=""))
        with self.assertRaises(ModelConfigurationError):
            await model.complete([{"role": "user", "content": "fixture"}])
        self.assertEqual(self.requests, [])
        model = self.make(b"")
        with self.assertRaises(ModelProtocolError):
            await model.complete([{"role": "user", "content": "x" * 1_048_577}])
        self.assertEqual(self.requests, [])

    async def test_malformed_empty_delta_fields_are_rejected_instead_of_becoming_text(self):
        cases = [event({"tool_calls": {}}), event({"reasoning_content": 0}),
                 'data: {"choices":[{"index":false,"delta":{},"finish_reason":"stop"}]}\n\n',
                 'data: {"choices":[{"index":0,"delta":[],"finish_reason":"stop"}]}\n\n']
        for index, raw in enumerate(cases):
            model = self.make(payload(raw))
            with self.subTest(case=index), self.assertRaises(ModelProtocolError):
                _ = [text async for text in model.stream_final([{"role": "user", "content": "fixture"}])]

    async def test_split_native_control_text_never_reaches_final_consumer(self):
        for pipe in ("｜", "｜｜", "|", "||"):
            marker = "<" + pipe + "DSML" + pipe + "tool_calls>"
            for boundary in range(1, len(marker)):
                model = self.make(payload(event({"content": marker[:boundary]}),
                    event({"content": marker[boundary:]}), event(finish="stop")))
                emitted = []
                with self.subTest(pipe=pipe, boundary=boundary), self.assertRaises(ModelProtocolError) as rejected:
                    async for text in model.stream_final([{"role": "user", "content": "fixture"}]):
                        emitted.append(text)
                self.assertEqual(rejected.exception.code, "model_control_text_in_final")
                self.assertEqual(emitted, [])

    async def test_literal_angle_brackets_are_preserved_after_bounded_holdback(self):
        model = self.make(payload(event({"content": "a<"}), event({"content": "b"}),
            event({"content": "<"}), event(finish="stop")))
        self.assertEqual("".join([s async for s in model.stream_final([{"role": "user", "content": "fixture"}])]), "a<b<")

    async def test_explicit_empty_final_schema_keeps_all_tools_disabled(self):
        model = self.make(payload(event({"content": "actual answer"}), event(finish="stop")),
                          selected=profile(final_tool_schema="empty"))
        self.assertEqual("".join([s async for s in model.stream_final([{"role": "user", "content": "fixture"}])]), "actual answer")
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["tools"], [])
        self.assertEqual(body["tool_choice"], "none")

    async def test_truncated_reserved_control_prefix_is_rejected_at_end_of_stream(self):
        for prefix in ("<|D", "<|DSML", "<｜DSML", "<||DSML", "<｜｜DSML"):
            model = self.make(payload(event({"content": prefix}), event(finish="stop")))
            emitted = []
            with self.subTest(prefix=prefix), self.assertRaises(ModelProtocolError) as rejected:
                async for text in model.stream_final([{"role": "user", "content": "fixture"}]):
                    emitted.append(text)
            self.assertEqual(rejected.exception.code, "model_control_text_in_final")
            self.assertEqual(emitted, [])


if __name__ == "__main__":
    unittest.main()
