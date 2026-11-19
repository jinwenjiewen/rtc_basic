"""Text HTTP transport against the real controller/store and scripted models."""

import asyncio
from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend_harness_loop.config import Settings
from backend_harness_loop.models import ModelProfiles, ModelProtocolError, ModelResponse
from backend_harness_loop.runtime import AgentRuntime
from backend_harness_loop.store import RuntimeStore
from backend_harness_loop.text import create_text_app


def tool(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class TextModel:
    def __init__(self, factory, profile, plan):
        self.factory, self.profile, self.plan = factory, profile, plan
        self.release, self.cancelled = asyncio.Event(), asyncio.Event()
        self.closed = False
        self.messages = []
        self.steps = 0

    async def complete(self, messages, **kwargs):
        self.messages.append(deepcopy(messages))
        self.steps += 1
        self.factory.started.put_nowait(self)
        if self.plan == "blocked":
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if self.plan == "notes" and self.steps == 1:
            calls = [tool("fixture_notes", "context_update_notes", {"expected_version": 0, "text": "PRIVATE NOTE FIXTURE"})]
        else:
            calls = [tool("fixture_prepare", "prepare_response", {"mode": "answer", "evidence_refs": []})]
        native = {"role": "assistant", "content": "PRIVATE DECISION DRAFT", "tool_calls": calls}
        return ModelResponse(content=native["content"], tool_calls=calls, assistant_message=native,
                             finish_reason="tool_calls", usage={"total_tokens": 7})

    async def stream_final(self, messages, max_tokens=None, on_usage=None):
        if self.plan == "error":
            raise ModelProtocolError("fixture_model_error")
        yield "真正的"
        yield "文字回复。"
        if on_usage:
            on_usage({"total_tokens": 5})

    async def aclose(self):
        self.closed = True


class TextModels:
    def __init__(self):
        self.plans = deque()
        self.instances = []
        self.started = asyncio.Queue()

    def __call__(self, profile):
        instance = TextModel(self, profile, self.plans.popleft() if self.plans else "normal")
        self.instances.append(instance)
        return instance


def events(response):
    result = []
    for frame in response.text.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.splitlines()
        result.append((next(line[7:] for line in lines if line.startswith("event: ")),
                       json.loads(next(line[6:] for line in lines if line.startswith("data: ")))))
    return result


class TextTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        config = self.root / "models.json"
        config.write_text(json.dumps({"schema_version": 1, "default_profile": "flash",
            "providers": {"fake": {"base_url": "https://fixture.example/v1", "api_key_env": "FIXTURE_KEY"}},
            "profiles": {"flash": {"provider": "fake", "model": "flash"},
                         "pro": {"provider": "fake", "model": "pro"}}}), encoding="utf-8")
        self.profiles = ModelProfiles.load(config, environ={"FIXTURE_KEY": "PRIVATE KEY FIXTURE"})
        self.models = TextModels()
        self.store = RuntimeStore(":memory:")
        self.runtime = AgentRuntime(self.store, self.profiles, model_factory=self.models)
        # All RTC settings remain empty: text must not require them.
        self.app = create_text_app(Settings(), self.profiles, self.root / "unused.sqlite3", runtime=self.runtime)
        self.manager = self.app.state.text_sessions
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:3101")
        self.headers = {"X-M0-Client-Key": self.app.state.client_key}

    async def asyncTearDown(self):
        for model in self.models.instances:
            model.release.set()
        await self.manager.close()
        await self.runtime.aclose()
        await self.client.aclose()
        self.store.close()
        self.folder.cleanup()

    async def create(self, profile=None):
        result = await self.client.post("/api/m1/text/sessions", json={} if profile is None else {"profile_id": profile}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    async def post(self, session, action, body):
        return await self.client.post(f'/api/m1/text/sessions/{session["session_id"]}/{action}', json=body, headers=self.headers)

    async def turn(self, session, round_id=0, text="这是文字输入"):
        return await self.post(session, "turns", {"round_id": round_id, "text": text})

    async def latest_started(self, plan):
        while True:
            model = await asyncio.wait_for(self.models.started.get(), timeout=1)
            if model.plan == plan:
                return model

    async def test_standalone_text_needs_no_rtc_credentials_or_cloud_calls(self):
        with patch("backend_harness_loop.probes.rtc.call_openapi", AsyncMock(side_effect=AssertionError("RTC must not be called"))) as rtc:
            health = (await self.client.get("/healthz")).json()
            self.assertEqual(health["transports"], ["text"])
            self.assertEqual(health["mode"], "agent_loop")
            inventory = (await self.client.get("/api/m1/models", headers=self.headers)).json()
            self.assertEqual(inventory["default_profile"], "flash")
            session = await self.create()
            self.assertEqual(session["transport"], "text")
            self.assertNotIn("token", session)
            result = await self.turn(session)
            self.assertEqual(result.status_code, 200)
            self.assertTrue((await self.post(session, "stop", {})).json()["stopped"])
            rtc.assert_not_awaited()

    async def test_authentication_and_local_boundary_precede_any_session_or_model(self):
        response = await self.client.post("/api/m1/text/sessions", content=b"not json")
        self.assertEqual(response.status_code, 401)
        for additions in ({"Host": "public.example"}, {"Origin": "https://evil.example"},
                          {"X-Forwarded-For": "127.0.0.1"}, {"Forwarded": "for=127.0.0.1"}):
            response = await self.client.post("/api/m1/text/sessions", json={}, headers={**self.headers, **additions})
            self.assertEqual(response.status_code, 403)
        remote = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=("192.0.2.1", 1)), base_url="http://127.0.0.1:3101")
        async with remote:
            self.assertEqual((await remote.post("/api/m1/text/sessions", json={}, headers=self.headers)).status_code, 403)
        self.assertEqual(self.manager.sessions, {})
        self.assertEqual(self.models.instances, [])

    async def test_page_assets_are_local_and_nonce_does_not_escape_in_public_requests(self):
        web = self.root / "web"
        web.mkdir()
        (web / "text.html").write_text('<meta name="m0-client-key" content="__M0_CLIENT_KEY__">', encoding="utf-8")
        (web / "text.js").write_text("'fixture';", encoding="utf-8")
        with patch("backend_harness_loop.text.WEB", web):
            page = await self.client.get("/m1/text")
            self.assertIn(self.app.state.client_key, page.text)
            self.assertEqual(page.headers["Cache-Control"], "no-store")
            self.assertEqual((await self.client.get("/m1/text/assets/text.js")).status_code, 200)
            self.assertEqual((await self.client.get("/m1/text/assets/not-allowed.js")).status_code, 404)
            denied = await self.client.get("/m1/text", headers={"Host": "public.example"})
            self.assertEqual(denied.status_code, 403)
            self.assertNotIn(self.app.state.client_key, denied.text)

    async def test_only_user_text_and_server_selected_profile_enter_loop(self):
        for payload in ({"model": "untrusted"}, {"profile_id": None}, {"session_id": "client-owned"}):
            result = await self.client.post("/api/m1/text/sessions", headers=self.headers, json=payload)
            self.assertEqual(result.status_code, 400)
        session = await self.create("pro")
        for field in ("model", "profile_id", "messages", "system", "run_id"):
            result = await self.post(session, "turns", {"round_id": 0, "text": "合法文字", field: "PRIVATE INJECTION"})
            self.assertEqual(result.status_code, 400)
            self.assertNotIn("PRIVATE INJECTION", result.text)
        response = await self.turn(session)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([model.profile.profile_id for model in self.models.instances], ["pro"])

    async def test_text_and_round_validation_use_utf8_bytes_and_reject_control_or_boolean_ids(self):
        session = await self.create()
        invalid = [({"round_id": False, "text": "好"}, 400), ({"round_id": "bad\nround", "text": "好"}, 400),
                   ({"round_id": 0, "text": "  "}, 400), ({"round_id": 0, "text": 1}, 400),
                   ({"round_id": 0, "text": "蓝" * 2731}, 413)]
        for payload, code in invalid:
            self.assertEqual((await self.post(session, "turns", payload)).status_code, code)
        self.assertEqual(self.models.instances, [])
        result = await self.turn(session, text="a" * 8192)
        self.assertEqual(result.status_code, 200)
        oversized = await self.client.post(f'/api/m1/text/sessions/{session["session_id"]}/turns',
            headers=self.headers, content=b"x" * 65537)
        self.assertEqual(oversized.status_code, 413)

    async def test_stream_exposes_only_final_text_with_actual_tools_and_terminal_state(self):
        self.models.plans.append("notes")
        session = await self.create()
        response = await self.turn(session)
        self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
        frames = events(response)
        self.assertEqual([kind for kind, _ in frames], ["turn", "delta", "delta", "terminal"])
        self.assertEqual("".join(data["text"] for kind, data in frames if kind == "delta"), "真正的文字回复。")
        self.assertEqual(frames[-1][1]["state"], "generation_completed")
        self.assertEqual(frames[-1][1]["delivery_status"], "completed")
        for private in ("PRIVATE DECISION DRAFT", "PRIVATE NOTE FIXTURE", "PRIVATE KEY FIXTURE", "tool_calls", "context_update_notes", "[DONE]"):
            self.assertNotIn(private, response.text)
        status = (await self.client.get(f'/api/m1/text/sessions/{session["session_id"]}', headers=self.headers)).json()
        self.assertEqual(status["notes_version"], 1)
        self.assertNotIn("PRIVATE NOTE FIXTURE", json.dumps(status))

    async def test_same_text_new_round_preserves_history_but_duplicate_round_starts_no_model(self):
        session = await self.create()
        self.assertEqual((await self.turn(session, 0, "保留这句历史" )).status_code, 200)
        duplicate = await self.turn(session, "0", "保留这句历史")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["detail"]["code"], "duplicate_round")
        self.assertEqual(len(self.models.instances), 1)
        self.assertEqual((await self.turn(session, 1, "保留这句历史")).status_code, 200)
        second_input = self.models.instances[1].messages[0]
        self.assertEqual(sum(m.get("role") == "user" and m.get("content") == "保留这句历史" for m in second_input), 2)
        self.assertNotIn("PRIVATE DECISION DRAFT", json.dumps(second_input, ensure_ascii=False))

    async def test_running_interrupt_cancels_model_and_finishes_owned_stream(self):
        self.models.plans.append("blocked")
        session = await self.create()
        request = asyncio.create_task(self.turn(session, "running"))
        model = await self.latest_started("blocked")
        result = await self.post(session, "interrupt", {"round_id": "running"})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["interrupted"])
        response = await asyncio.wait_for(request, 1)
        self.assertTrue(model.cancelled.is_set())
        frames = events(response)
        self.assertEqual([kind for kind, _ in frames], ["turn", "terminal"])
        self.assertEqual(frames[-1][1]["state"], "cancelled")
        self.assertEqual(frames[-1][1]["delivery_status"], "interrupted")

    async def test_late_old_interrupt_cannot_cancel_new_turn(self):
        session = await self.create()
        await self.turn(session, "old")
        self.models.plans.append("blocked")
        request = asyncio.create_task(self.turn(session, "new"))
        model = await self.latest_started("blocked")
        self.assertEqual((await self.post(session, "interrupt", {"round_id": "old"})).status_code, 200)
        current = self.runtime.binding(self.manager.sessions[session["session_id"]].run_id).current
        self.assertEqual(current.target.round_id, "new")
        self.assertTrue(current.valid)
        self.assertFalse(model.cancelled.is_set())
        model.release.set()
        self.assertEqual(events(await asyncio.wait_for(request, 1))[-1][1]["state"], "generation_completed")

    async def test_stop_closes_stream_admission_and_is_idempotent(self):
        self.models.plans.append("blocked")
        session = await self.create()
        request = asyncio.create_task(self.turn(session))
        model = await self.latest_started("blocked")
        self.assertTrue((await self.post(session, "stop", {})).json()["stopped"])
        self.assertEqual(events(await asyncio.wait_for(request, 1))[-1][1]["state"], "cancelled")
        self.assertTrue(model.cancelled.is_set())
        self.assertTrue((await self.post(session, "stop", {})).json()["stopped"])
        self.assertEqual((await self.post(session, "heartbeat", {})).status_code, 410)
        self.assertEqual((await self.turn(session, 1)).status_code, 410)
        status = (await self.client.get(f'/api/m1/text/sessions/{session["session_id"]}', headers=self.headers)).json()
        self.assertFalse(status["alive"])
        self.assertEqual(status["state"], "closed")

    async def test_http_waiter_disconnect_revokes_its_live_model(self):
        self.models.plans.append("blocked")
        session = await self.create()
        request = asyncio.create_task(self.turn(session))
        model = await self.latest_started("blocked")
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        await asyncio.wait_for(model.cancelled.wait(), 1)
        turns = self.runtime.snapshot(session["session_id"])["turns"]
        self.assertEqual(turns[0]["state"], "cancelled")
        self.assertNotEqual(turns[0]["delivery_status"], "completed")

    async def test_capacity_and_expired_leases_do_not_leave_live_runtime(self):
        self.manager.max_active, self.manager.max_sessions = 2, 3
        one, two = await self.create(), await self.create()
        self.assertEqual((await self.client.post("/api/m1/text/sessions", headers=self.headers, json={})).status_code, 429)
        self.manager.sessions[one["session_id"]].deadline = time.monotonic() - 1
        await self.manager.reap()
        self.assertEqual(self.runtime.snapshot(one["session_id"])["state"], "closed")
        three = await self.create()
        await self.post(two, "stop", {})
        await self.post(three, "stop", {})
        self.assertEqual((await self.client.post("/api/m1/text/sessions", headers=self.headers, json={})).json()["detail"]["code"], "text_session_instance_capacity")

    async def test_heartbeat_does_not_extend_absolute_deadline_and_inactive_timeout_closes(self):
        session = await self.create()
        item = self.manager.sessions[session["session_id"]]
        original = item.deadline
        item.last_heartbeat = time.monotonic() - 30
        self.assertTrue((await self.post(session, "heartbeat", {})).json()["alive"])
        self.assertEqual(item.deadline, original)
        item.last_heartbeat = time.monotonic() - 61
        self.assertEqual((await self.turn(session)).status_code, 410)
        self.assertEqual(self.runtime.snapshot(session["session_id"])["state"], "closed")

    async def test_model_failure_uses_safe_fallback_and_terminal_error(self):
        self.models.plans.append("error")
        session = await self.create()
        response = await self.turn(session)
        frames = events(response)
        self.assertEqual(frames[-1][1]["state"], "failed")
        self.assertEqual(frames[-1][1]["error_code"], "fixture_model_error")
        self.assertEqual([kind for kind, _ in frames], ["turn", "delta", "terminal"])
        self.assertNotIn("PRIVATE DECISION DRAFT", response.text)

    async def test_text_route_cannot_address_rtc_or_unknown_runtime_sessions(self):
        self.runtime.register_session("rtc_session", "rtc_run", "flash", "rtc_task", "rtc_user")
        for session_id in ("rtc_session", "text_session_unknown"):
            response = await self.client.post(f"/api/m1/text/sessions/{session_id}/turns", headers=self.headers,
                json={"round_id": 0, "text": "跨通道输入"})
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.models.instances, [])

    async def test_standalone_lifespan_closes_text_runtime_before_shutdown(self):
        async with self.app.router.lifespan_context(self.app):
            session = await self.create()
            self.models.plans.append("blocked")
            request = asyncio.create_task(self.turn(session))
            model = await self.latest_started("blocked")
        self.assertTrue(model.cancelled.is_set())
        self.assertEqual(events(await asyncio.wait_for(request, 1))[-1][1]["state"], "cancelled")
        self.assertEqual(self.runtime.snapshot(session["session_id"])["state"], "closed")


if __name__ == "__main__":
    unittest.main()
