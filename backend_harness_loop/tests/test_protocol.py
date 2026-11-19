import base64
import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from starlette.requests import Request

import httpx

from backend_harness_loop.capture import create_capture_app
from backend_harness_loop.protocol import (
    ExecutionTarget,
    OutputFence,
    decode_rts_message,
    decode_sse,
    encode_sse_chunk,
    encode_sse_end,
    run_protocol_probe,
)


def envelope(payload, tag=b"conv", length=None):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    size = len(raw) if length is None else length
    return base64.b64encode(tag + size.to_bytes(4, "big") + raw).decode()


class ProtocolTests(unittest.TestCase):
    def test_utf8_split_every_byte_and_done(self):
        wire = (encode_sse_chunk("中文🙂", stream_id="stream") + encode_sse_end(stream_id="stream")).replace("\n", "\r\n").encode()
        events = decode_sse(wire[i : i + 1] for i in range(len(wire)))
        self.assertEqual(events[0]["choices"][0]["delta"]["content"], "中文🙂")
        self.assertEqual(events[-1], "[DONE]")
        self.assertEqual(events[-2]["choices"][0]["finish_reason"], "stop")

    def test_sse_rejects_malformed_data_and_data_after_done(self):
        for stream in (b"data: not-json\n\n", b"data: [DONE]\n\ndata: {}\n\n", b"data: []\n\n"):
            with self.subTest(stream=stream), self.assertRaises(ValueError):
                decode_sse([stream])

    def test_tlv_validates_base64_length_tag_utf8_and_object(self):
        self.assertEqual(decode_rts_message(envelope({"RoundID": 3}))[1]["RoundID"], 3)
        self.assertEqual(decode_rts_message(envelope({"data": []}, tag=b"subv"))[0], "subv")
        for stream in ("not-base64", envelope({}, length=100), envelope({}, tag=b"tool"), base64.b64encode(b"conv\0\0\0\1\xff").decode(), envelope([])):
            with self.subTest(stream=stream), self.assertRaises((ValueError, UnicodeError)):
                decode_rts_message(stream)

    def test_stale_target_and_old_connection_cannot_cancel_new_turn(self):
        old, new = ExecutionTarget("run", "old", 1), ExecutionTarget("run", "new", 2)
        fence = OutputFence()
        fence.bind(old, "old-connection")
        fence.bind(new, "new-connection")
        self.assertFalse(fence.interrupt(old, "old-control"))
        self.assertFalse(fence.disconnected(old, "old-connection"))
        self.assertTrue(fence.can_emit(new, "new-connection"))
        with self.assertRaises(ValueError):
            fence.bind(old, "stale-bind")
        fence.bind(new, "replacement-connection")
        self.assertFalse(fence.disconnected(new, "new-connection"))
        self.assertTrue(fence.can_emit(new, "replacement-connection"))
        self.assertTrue(fence.interrupt(new, "new-control"))
        self.assertFalse(fence.interrupt(new, "new-control"))
        self.assertFalse(fence.can_emit(new, "new-connection"))
        with self.assertRaises(ValueError):
            fence.bind(new, "cancelled-bind")

    def test_probe_labels_synthetic_evidence(self):
        result = run_protocol_probe()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["evidence"]["synthetic"])
        self.assertEqual(result["evidence"]["cloud_round_association"], "not_verified")


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "capture.jsonl"
        self.app = create_capture_app(SimpleNamespace(callback_secret="private-callback-secret", capture_instance_id="fixture-instance"), self.path)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://capture.local")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_requires_nonempty_secret_and_auth(self):
        with self.assertRaises(ValueError):
            create_capture_app(SimpleNamespace(callback_secret=""), self.path)
        response = await self.client.post("/api/chat_callback", json={"messages": []})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.path.exists())
        health = (await self.client.get("/healthz")).json()
        self.assertEqual(health["service"], "backend_harness_loop.m0")
        self.assertEqual(health["instance_id"], "fixture-instance")

    async def test_chat_captures_only_shapes_and_hashed_identifiers(self):
        payload = {"messages": [{"role": "user", "content": "PRIVATE USER TEXT"}], "TaskId": "private-task", "RoundID": 4, "api_key": "private-api-key"}
        response = await self.client.post("/api/chat_callback?token=private-callback-secret&rtc_run_id=private-run", json=payload, headers={"Authorization": "Bearer private-auth", "X-Request-ID": "private-request"})
        self.assertEqual(response.status_code, 200)
        parsed = decode_sse([response.content])
        self.assertEqual(parsed[-1], "[DONE]")
        log = self.path.read_text("utf-8")
        for sensitive in ("PRIVATE USER TEXT", "private-task", "private-run", "private-request", "private-callback-secret", "private-api-key", "private-auth"):
            self.assertNotIn(sensitive, log)
        record = json.loads(log.splitlines()[0])
        self.assertIn("$.messages[].content", record["schema"])
        self.assertIn("$.<sensitive_field>", record["schema"])
        self.assertEqual(record["correlation"]["stable_round_association"], "not_verified")
        self.assertEqual(record["response_status"], 200)

    async def test_rts_accepts_no_content_type_and_signature(self):
        payload = {"signature": "private-callback-secret", "binary": True, "message": envelope({"TaskId": "private-task", "RoundID": 3, "Stage": {"Code": 4, "Description": "private-description"}, "EventTime": 1234})}
        response = await self.client.post("/api/rtc/events", content=json.dumps(payload).encode())
        self.assertEqual(response.status_code, 200)
        record = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(record["tlv_tag"], "conv")
        self.assertEqual(record["correlation"]["stage_code"], 4)
        self.assertNotIn("private-description", self.path.read_text("utf-8"))

    async def test_rejects_malformed_and_oversized_payload(self):
        headers = {"X-RTC-Callback-Token": "private-callback-secret"}
        bad = await self.client.post("/api/chat_callback", content=b"not-json", headers=headers)
        self.assertEqual(bad.status_code, 400)
        large = await self.client.post("/api/chat_callback", content=b"x" * 65_537, headers=headers)
        self.assertEqual(large.status_code, 413)
        rts = await self.client.post("/api/rtc/events", json={"signature": "private-callback-secret", "binary": True, "message": envelope({}, length=10)})
        self.assertEqual(rts.status_code, 400)
        self.assertFalse(self.path.exists())

    async def test_subtitle_without_binary_flag_keeps_nested_round_but_not_text(self):
        payload = {"signature": "private-callback-secret", "message": envelope({"data": [{"roundId": 3, "text": "PRIVATE SUBTITLE"}]}, tag=b"subv")}
        response = await self.client.post("/api/rtc/events", content=json.dumps(payload).encode())
        self.assertEqual(response.status_code, 200)
        record = json.loads(self.path.read_text("utf-8"))
        self.assertIn("data[0].roundId", [item["field"] for item in record["correlation"]["identifiers"]])
        self.assertNotIn("PRIVATE SUBTITLE", self.path.read_text("utf-8"))
        payload["binary"] = False
        rejected = await self.client.post("/api/rtc/events", content=json.dumps(payload).encode())
        self.assertEqual(rejected.status_code, 400)

    async def test_subtitle_camel_case_user_id_is_hashed_without_text_or_content_hash(self):
        payload = {"signature": "private-callback-secret", "message": envelope({"data": [
            {"roundId": 3, "userId": "private-subtitle-user", "text": "PRIVATE ASR SUBTITLE"}
        ]}, tag=b"subv")}
        response = await self.client.post("/api/rtc/events", json=payload)
        self.assertEqual(response.status_code, 200)
        log = self.path.read_text("utf-8")
        identifiers = json.loads(log)["correlation"]["identifiers"]
        self.assertIn({"field": "data[0].userId", "kind": "user", "sha256": hashlib.sha256(b"private-subtitle-user").hexdigest()}, identifiers)
        for sensitive in ("private-subtitle-user", "PRIVATE ASR SUBTITLE", hashlib.sha256(b"PRIVATE ASR SUBTITLE").hexdigest()):
            self.assertNotIn(sensitive, log)

    async def test_error_code_alias_retains_only_machine_code_without_reason(self):
        payload = {
            "signature": "private-callback-secret",
            "message": envelope({"TaskId": "private-task", "ErrorInfo": {"ErrorCode": 3001, "Reason": "PRIVATE ERROR REASON"}}),
        }
        response = await self.client.post("/api/rtc/events", json=payload)
        self.assertEqual(response.status_code, 200)
        log = self.path.read_text("utf-8")
        self.assertEqual(json.loads(log)["correlation"]["error_code"], "3001")
        self.assertNotIn("PRIVATE ERROR REASON", log)
        self.assertNotIn("private-task", log)

    async def test_fault_only_fails_first_authenticated_valid_chat(self):
        app = create_capture_app(SimpleNamespace(callback_secret="private-callback-secret", capture_fail_first=True), self.path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://capture.local") as client:
            denied = await client.post("/api/chat_callback", json={"messages": []})
            self.assertEqual(denied.status_code, 401)
            first = await client.post("/api/chat_callback", json={"messages": []}, headers={"X-RTC-Callback-Token": "private-callback-secret"})
            self.assertEqual(first.status_code, 503)
            self.assertEqual(first.json()["Error"]["Code"], "M0SyntheticUnavailable")
            second = await client.post("/api/chat_callback", json={"messages": []}, headers={"X-RTC-Callback-Token": "private-callback-secret"})
            self.assertEqual(second.status_code, 200)
            self.assertEqual(decode_sse([second.content])[-1], "[DONE]")
        records = [json.loads(line) for line in self.path.read_text("utf-8").splitlines()]
        chats = [item for item in records if item["source"] == "custom_llm"]
        self.assertEqual([item["response_status"] for item in chats], [503, 200])

    async def test_delayed_stream_completes_with_bounded_metrics(self):
        app = create_capture_app(SimpleNamespace(callback_secret="private-callback-secret", capture_chunk_count=3, capture_chunk_delay_seconds=0.01), self.path)
        began = time.perf_counter()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://capture.local") as client:
            response = await client.post("/api/chat_callback", json={"RoundID": 7}, headers={"X-RTC-Callback-Token": "private-callback-secret"})
        self.assertGreaterEqual(time.perf_counter() - began, 0.02)
        self.assertEqual(len(decode_sse([response.content])), 5)
        record = json.loads(self.path.read_text("utf-8").splitlines()[-1])
        self.assertEqual(record["source"], "capture_stream")
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["chunks_sent"], 3)
        self.assertGreaterEqual(record["duration_ms"], 20)

    async def test_stream_cancellation_is_recorded_and_propagates(self):
        app = create_capture_app(SimpleNamespace(callback_secret="private-callback-secret", capture_chunk_count=8, capture_chunk_delay_seconds=2), self.path)
        body = json.dumps({"RoundID": 7, "messages": [{"content": "PRIVATE CONTENT"}]}).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request({"type": "http", "method": "POST", "path": "/api/chat_callback", "headers": [(b"x-rtc-callback-token", b"private-callback-secret")], "query_string": b"rtc_run_id=private-run"}, receive)
        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/chat_callback")
        response = await endpoint(request)
        iterator = response.body_iterator
        began = time.perf_counter()
        first = await anext(iterator)
        self.assertLess(time.perf_counter() - began, 0.5)
        self.assertIn("接口验证", first)
        pending = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0.01)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        record = json.loads(self.path.read_text("utf-8").splitlines()[-1])
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["chunks_sent"], 1)
        ids = record["correlation"]["identifiers"]
        self.assertIn(hashlib.sha256(b"private-run").hexdigest(), [item["sha256"] for item in ids])
        self.assertNotIn("PRIVATE CONTENT", self.path.read_text("utf-8"))
        self.assertNotIn("private-run", self.path.read_text("utf-8"))

    async def test_explicitly_closed_stream_is_recorded(self):
        app = create_capture_app(SimpleNamespace(callback_secret="private-callback-secret", capture_chunk_count=2), self.path)

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        request = Request({"type": "http", "method": "POST", "path": "/api/chat_callback", "headers": [(b"x-rtc-callback-token", b"private-callback-secret")], "query_string": b""}, receive)
        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/chat_callback")
        response = await endpoint(request)
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        record = json.loads(self.path.read_text("utf-8").splitlines()[-1])
        self.assertEqual(record["state"], "closed")
        self.assertEqual(record["chunks_sent"], 1)

    def test_fault_options_are_bounded(self):
        for options in ({"capture_chunk_count": 9}, {"capture_chunk_count": True}, {"capture_chunk_delay_seconds": 2.1}, {"capture_chunk_delay_seconds": float("nan")}, {"capture_fail_first": "false"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                create_capture_app(SimpleNamespace(callback_secret="secret", **options), self.path)


if __name__ == "__main__":
    unittest.main()
