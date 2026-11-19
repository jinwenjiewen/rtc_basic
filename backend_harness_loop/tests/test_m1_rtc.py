"""M1 HTTP/RTC integration fixtures. No cloud, real model, microphone or audio."""

import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import httpx

from backend_harness_loop.app import create_agent_app
from backend_harness_loop.capture import create_capture_app
from backend_harness_loop.config import Settings
from backend_harness_loop.models import ModelProfiles, ModelProtocolError
from backend_harness_loop.protocol import decode_sse
from backend_harness_loop.runtime import AgentRuntime
from backend_harness_loop.store import RuntimeStore


def callback(round_id=0, text="你好，开始本地测试。"):
    return {"round_id": round_id, "messages": [{"role": "user", "content": text}]}


def rts(payload, tag="conv", *, binary=True):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    envelope = tag.encode("ascii") + len(body).to_bytes(4, "big") + body
    result = {"signature": "fixture-callback-key", "message": base64.b64encode(envelope).decode("ascii")}
    if binary is not None:
        result["binary"] = binary
    return result


def interrupted(round_id=0, *, task="task1", user="user1", stage=4):
    return {"RoundID": round_id, "TaskId": task, "UserID": user, "Stage": {"Code": stage}, "EventTime": 1234}


class FakeModel:
    def __init__(self, factory, profile, plan):
        self.factory, self.profile, self.plan = factory, profile, plan
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.decision_messages = None
        self.final_messages = None
        self.closed = False

    async def complete(self, messages, tools=None, tool_choice="auto", max_tokens=None):
        self.decision_messages = deepcopy(messages)
        self.started.set()
        self.factory.started.put_nowait(self)
        if self.plan in {"blocked", "ignore_cancel"}:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                if self.plan != "ignore_cancel":
                    raise
                await self.release.wait()
        calls = [{"id": "prepare_fixture", "type": "function", "function": {
            "name": "prepare_response", "arguments": '{"mode":"answer","evidence_refs":[]}',
        }}]
        return SimpleNamespace(content="PRIVATE DECISION DRAFT", tool_calls=calls,
                               usage={"total_tokens": 7},
                               assistant_message={"role": "assistant", "content": "PRIVATE DECISION DRAFT", "tool_calls": calls})

    async def stream_final(self, messages, max_tokens=None, on_usage=None):
        self.final_messages = deepcopy(messages)
        if on_usage:
            on_usage({"total_tokens": 5})
        yield "这是"
        if self.plan == "hold_stream":
            await self.release.wait()
        if self.plan == "partial_error":
            # Keep the error after actual consumer delivery, not merely after
            # putting a text chunk into a queue that has no consumer yet.
            await self.release.wait()
            raise ModelProtocolError("fixture_stream_error")
        yield "真实模型回复。"

    async def aclose(self):
        self.closed = True


class FakeModels:
    def __init__(self):
        self.instances = []
        self.plans = []
        self.started = asyncio.Queue()

    def __call__(self, profile):
        instance = FakeModel(self, profile, self.plans.pop(0) if self.plans else "normal")
        self.instances.append(instance)
        return instance


class M1RTCIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.capture_path = root / "capture.jsonl"
        config = root / "models.json"
        config.write_text(json.dumps({"schema_version": 1, "default_profile": "deepseek_v4_flash",
            "providers": {"test": {"base_url": "https://model.example/v1", "api_key_env": "FIXTURE_MODEL_KEY"}},
            "profiles": {
                "deepseek_v4_flash": {"provider": "test", "model": "deepseek-v4-flash"},
                "deepseek_v4_pro": {"provider": "test", "model": "deepseek-v4-pro"},
            }}), encoding="utf-8")
        self.profiles = ModelProfiles.load(config, environ={"FIXTURE_MODEL_KEY": "fixture-model-key"})
        self.store = RuntimeStore(":memory:")
        self.models = FakeModels()
        self.runtime = AgentRuntime(self.store, self.profiles, model_factory=self.models)
        self.runtime.register_session("session1", "run1", "deepseek_v4_flash", "task1", "user1")
        self.settings = Settings(callback_secret="fixture-callback-key")
        self.app = create_agent_app(self.settings, self.profiles, root / "unused.sqlite", self.capture_path,
                                    "https://callback.example.com", runtime=self.runtime)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:3101")
        self.local_headers = {"X-M0-Client-Key": self.app.state.client_key}

    async def asyncTearDown(self):
        for instance in self.models.instances:
            instance.release.set()
        await self.runtime.aclose()
        await self.client.aclose()
        self.store.close()
        self.folder.cleanup()

    async def post(self, payload, *, run="run1", token="fixture-callback-key", headers=None):
        return await self.client.post("/api/chat_callback", params={"rtc_run_id": run, "token": token}, json=payload, headers=headers)

    async def event(self, payload, *, run="run1", tag="conv", binary=True):
        return await self.client.post("/api/rtc/events", params={"rtc_run_id": run}, json=rts(payload, tag, binary=binary))

    async def test_callback_authentication_happens_before_model_or_input_acceptance(self):
        response = await self.post(callback(), token="wrong-token")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.models.instances, [])
        self.assertEqual(self.store.snapshot("session1")["turns"], [])
        response = await self.client.post("/api/chat_callback?rtc_run_id=run1", json=callback(),
                                          headers={"X-RTC-Callback-Token": "fixture-callback-key"})
        self.assertEqual(response.status_code, 200)

    async def test_missing_unknown_and_closed_run_cannot_start_a_model(self):
        missing = await self.post(callback(), run="")
        unknown = await self.post(callback(), run="another-run")
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(unknown.status_code, 404)
        await self.runtime.close_session("session1")
        closed = await self.post(callback())
        self.assertEqual(closed.status_code, 410)
        self.assertEqual(self.models.instances, [])

    async def test_final_user_and_round_validation_fail_before_model_execution(self):
        malformed = [callback(round_id=False), {"round_id": 1, "messages": [{"role": "user", "content": "old"}, {"role": "assistant", "content": "do not replay"}]}, callback(text="你" * 2731)]
        for payload, status in zip(malformed, (400, 400, 413)):
            self.assertEqual((await self.post(payload)).status_code, status)
        self.assertEqual(self.models.instances, [])

    async def test_one_stable_rtc_stream_contains_only_final_text_and_one_done(self):
        response = await self.post(callback())
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        frames = decode_sse([response.content])
        self.assertEqual(frames[-1], "[DONE]")
        self.assertEqual(sum(frame == "[DONE]" for frame in frames), 1)
        ids = {frame["id"] for frame in frames if isinstance(frame, dict)}
        self.assertEqual(len(ids), 1)
        self.assertTrue(next(iter(ids)).startswith("m1_"))
        text = "".join(frame["choices"][0]["delta"].get("content", "") for frame in frames if isinstance(frame, dict))
        self.assertEqual(text, "这是真实模型回复。")
        for hidden in ("PRIVATE DECISION DRAFT", "prepare_response", "evidence_refs", "fixture-model-key"):
            self.assertNotIn(hidden, response.text)
        self.assertTrue(self.models.instances[0].closed)

    async def test_provider_model_and_history_do_not_select_profile_or_duplicate_inputs(self):
        payload = callback(text="当前问题")
        payload["model"] = "deepseek-v4-pro"
        payload["messages"] = [{"role": "system", "content": "UNTRUSTED RTC SYSTEM"},
                               {"role": "user", "content": "REPLAY OLD USER"},
                               {"role": "assistant", "content": "UNTRUSTED RTC ASSISTANT"},
                               payload["messages"][-1]]
        self.assertEqual((await self.post(payload)).status_code, 200)
        instance = self.models.instances[0]
        self.assertEqual(instance.profile.profile_id, "deepseek_v4_flash")
        serialized = json.dumps(instance.decision_messages, ensure_ascii=False)
        self.assertNotIn("UNTRUSTED RTC", serialized)
        self.assertNotIn("REPLAY OLD USER", serialized)
        self.assertEqual([m["content"] for m in instance.decision_messages if m["role"] == "user"], ["当前问题"])
        history = self.store.get_history("session1", limit=20)
        self.assertEqual([m["content"] for m in history["messages"] if m["role"] == "user"], ["当前问题"])

    async def test_duplicate_round_is_rejected_but_identical_text_new_round_is_admitted(self):
        self.assertEqual((await self.post(callback(round_id=0))).status_code, 200)
        duplicate = await self.post(callback(round_id=0))
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(len(self.models.instances), 1)
        self.assertEqual((await self.post(callback(round_id=1))).status_code, 200)
        self.assertEqual(len(self.models.instances), 2)
        user_messages = [m for m in self.store.get_history("session1", limit=20)["messages"] if m["role"] == "user"]
        self.assertEqual(len(user_messages), 2)
        self.assertEqual(user_messages[0]["content"], user_messages[1]["content"])

    async def test_interruption_before_callback_leaves_a_cancel_tombstone(self):
        self.assertEqual((await self.event(interrupted(7))).status_code, 200)
        callback_response = await self.post(callback(round_id=7))
        self.assertEqual(callback_response.status_code, 409)
        self.assertEqual(callback_response.json()["detail"]["code"], "round_cancelled")
        self.assertEqual(self.models.instances, [])

    async def test_fast_interrupt_ends_connection_without_waiting_for_stubborn_model_cleanup(self):
        self.models.plans.append("ignore_cancel")
        request = asyncio.create_task(self.post(callback(round_id=8)))
        model = await asyncio.wait_for(self.models.started.get(), timeout=1)
        for payload in (interrupted(8, task="other"), interrupted(8, user="agent-not-client"), interrupted(8, stage=5), {"TaskId": "task1", "UserID": "user1", "Stage": {"Code": 4}}):
            self.assertEqual((await self.event(payload)).status_code, 200)
            self.assertTrue(self.runtime.binding("run1").current.valid)
        correct = await asyncio.wait_for(self.event(interrupted(8)), timeout=1)
        self.assertEqual(correct.status_code, 200)
        result = await asyncio.wait_for(request, timeout=1)
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("真实模型回复", result.text)
        self.assertFalse(model.release.is_set())
        self.assertTrue(model.cancelled.is_set())
        model.release.set()

    async def test_late_interrupt_for_old_round_does_not_cancel_new_model_request(self):
        self.assertEqual((await self.post(callback(round_id=1))).status_code, 200)
        self.models.plans.append("blocked")
        request = asyncio.create_task(self.post(callback(round_id=2)))
        model = self.models.instances[0]
        while model.plan != "blocked":
            model = await asyncio.wait_for(self.models.started.get(), timeout=1)
        self.assertEqual((await self.event(interrupted(1))).status_code, 200)
        self.assertTrue(self.runtime.binding("run1").current.valid)
        self.assertEqual(self.runtime.binding("run1").current.target.round_id, "2")
        model.release.set()
        self.assertIn("真实模型回复", (await asyncio.wait_for(request, timeout=1)).text)

    async def test_old_output_iterator_disconnect_cannot_cancel_newer_callback_turn(self):
        self.models.plans.append("hold_stream")
        old = await self.runtime.accept("run1", "old", "旧轮次", connection_id="old-connection")
        output = self.runtime.speech(old)
        self.assertEqual(await asyncio.wait_for(anext(output), timeout=1), "这是")
        self.models.plans.append("blocked")
        request = asyncio.create_task(self.post(callback(round_id="new")))
        model = self.models.instances[0]
        while model.plan != "blocked":
            model = await asyncio.wait_for(self.models.started.get(), timeout=1)
        await output.aclose()
        self.assertTrue(self.runtime.binding("run1").current.valid)
        self.assertEqual(self.runtime.binding("run1").current.target.round_id, "new")
        model.release.set()
        self.assertIn("真实模型回复", (await asyncio.wait_for(request, timeout=1)).text)

    async def test_rts_signature_and_tlv_are_strict_without_wildcard_cancellation(self):
        bad_auth = rts(interrupted())
        bad_auth["signature"] = "bad-secret"
        self.assertEqual((await self.client.post("/api/rtc/events?rtc_run_id=run1", json=bad_auth)).status_code, 401)
        self.assertEqual((await self.event(interrupted(), binary=False)).status_code, 400)
        self.assertEqual((await self.event(interrupted(), binary=None)).status_code, 200)
        self.assertEqual((await self.event(interrupted(), run="unknown-run")).status_code, 200)
        self.assertEqual((await self.client.post("/api/rtc/events?rtc_run_id=run1", json={"signature": "fixture-callback-key", "message": "malformed"})).status_code, 400)
        self.assertEqual(self.models.instances, [])

    async def test_partial_final_error_is_not_replayed_or_replaced_with_a_second_full_answer(self):
        self.models.plans.append("partial_error")
        request = asyncio.create_task(self.post(callback()))
        model = await asyncio.wait_for(self.models.started.get(), timeout=1)
        async def first_chunk_delivered():
            while True:
                handle = self.runtime.binding("run1").current
                if handle and handle.yielded_text == "这是":
                    return
                await asyncio.sleep(0)
        await asyncio.wait_for(first_chunk_delivered(), timeout=1)
        model.release.set()
        response = await asyncio.wait_for(request, timeout=1)
        text = "".join(frame["choices"][0]["delta"].get("content", "") for frame in decode_sse([response.content]) if isinstance(frame, dict))
        self.assertEqual(text, "这是")
        self.assertNotIn("请再说一次", response.text)
        turn = self.store.snapshot("session1")["turns"][0]
        self.assertEqual(turn["state"], "failed")

    async def test_model_inventory_is_local_and_never_returns_key_or_accepts_unknown_profile(self):
        self.assertEqual((await self.client.get("/api/m1/models")).status_code, 401)
        inventory = await self.client.get("/api/m1/models", headers=self.local_headers)
        self.assertEqual(inventory.status_code, 200)
        self.assertNotIn("fixture-model-key", inventory.text)
        public = await self.client.get("/api/m1/models", headers={**self.local_headers, "Host": "callback.example.com"})
        self.assertEqual(public.status_code, 403)
        unknown = await self.client.post("/api/m0/sessions", headers=self.local_headers, json={"profile_id": "unconfigured-profile"})
        self.assertEqual(unknown.status_code, 503)
        self.assertEqual(self.models.instances, [])

    async def test_explicit_pro_profile_selected_at_session_creation_remains_fixed_for_callback(self):
        settings = replace(self.settings, rtc_app_id="012345678901234567890123", rtc_app_key="fixture-rtc-key")
        app = create_agent_app(settings, self.profiles, Path(self.folder.name) / "unused-pro.sqlite",
                               Path(self.folder.name) / "pro-capture.jsonl", "https://callback.example.com", runtime=self.runtime)
        headers = {"X-M0-Client-Key": app.state.client_key}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3101") as client:
            created = await client.post("/api/m0/sessions", headers=headers, json={"profile_id": "deepseek_v4_pro"})
            self.assertEqual(created.status_code, 200)
            self.assertEqual(created.json()["profile_id"], "deepseek_v4_pro")
            session_id = created.json()["session_id"]
            run_id = app.state.client_sessions.get(session_id).run_id
            payload = callback()
            payload["model"] = "deepseek-v4-flash"
            response = await client.post("/api/chat_callback", params={"rtc_run_id": run_id, "token": "fixture-callback-key"}, json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.models.instances[0].profile.profile_id, "deepseek_v4_pro")
            stopped = await client.post(f"/api/m0/sessions/{session_id}/stop", headers=headers, json={})
            self.assertTrue(stopped.json()["stopped"])

    async def test_m0_without_agent_hooks_keeps_its_fixed_synthetic_reply(self):
        app = create_capture_app(self.settings, Path(self.folder.name) / "m0.jsonl")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m0.local") as client:
            response = await client.post("/api/chat_callback?token=fixture-callback-key", json={"messages": []})
        self.assertEqual(response.status_code, 200)
        self.assertIn("这是 RTC 接口验证回复。", response.text)
        self.assertEqual(self.models.instances, [])


if __name__ == "__main__":
    unittest.main()
