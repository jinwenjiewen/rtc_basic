import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend_harness_loop.client import create_client_app
from backend_harness_loop.config import Settings
from backend_harness_loop.rtc_token import issue_rtc_token


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "capture.jsonl"
        self.settings = Settings(
            rtc_app_id="012345678901234567890123", rtc_app_key="fixture-app-key",
            callback_secret="fixture-callback-secret", volc_access_key="fixture-ak",
            volc_secret_key="fixture-sk", rtc_legacy_voice_config={
                "ASRConfig": {"Provider": "volcano", "ProviderParams": {"Mode": "smallmodel"}},
                "TTSConfig": {"Provider": "volcano", "ProviderParams": {"audio": {"voice_type": "BV001_streaming"}}},
            },
        )
        self.app = create_client_app(self.settings, self.path, "https://m0.example.com")
        self.manager = self.app.state.client_sessions
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:3101")
        self.headers = {"X-M0-Client-Key": self.app.state.client_key}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.folder.cleanup()

    async def create(self):
        response = await self.client.post("/api/m0/sessions", headers=self.headers, json={})
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def post(self, session, action, payload=None):
        return await self.client.post(f'/api/m0/sessions/{session["session_id"]}/{action}', headers=self.headers, json=payload or {})

    async def test_local_page_and_management_are_inaccessible_through_tunnel_or_foreign_origin(self):
        denied = await self.client.get("/m0", headers={"Host": "m0.example.com"})
        self.assertEqual(denied.status_code, 403)
        forwarded = await self.client.get("/m0", headers={"X-Forwarded-For": "127.0.0.1"})
        self.assertEqual(forwarded.status_code, 403)
        denied = await self.client.post("/api/m0/sessions", json={})
        self.assertEqual(denied.status_code, 401)
        denied = await self.client.post("/api/m0/sessions", headers={**self.headers, "Origin": "https://other.example.com"}, json={})
        self.assertEqual(denied.status_code, 403)
        page = await self.client.get("/m0")
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.app.state.client_key, page.text)
        self.assertNotIn(self.settings.rtc_app_key, page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        health = await self.client.get("/healthz", headers={"Host": "m0.example.com", "X-Forwarded-For": "cloud"})
        self.assertEqual(health.status_code, 200)

    async def test_single_fresh_session_and_unstarted_cleanup_never_calls_cloud(self):
        with patch("backend_harness_loop.client.call_openapi", new_callable=AsyncMock) as cloud:
            session = await self.create()
            second = await self.client.post("/api/m0/sessions", headers=self.headers, json={})
            self.assertEqual(second.status_code, 409)
            self.assertTrue((await self.post(session, "stop")).json()["stopped"])
            self.assertTrue((await self.post(session, "stop")).json()["stopped"])
            cloud.assert_not_awaited()
            new = await self.create()
            self.assertNotEqual(new["room_id"], session["room_id"])
        ledger = self.manager.ledger_path.read_text("utf-8")
        for value in (session["token"], self.settings.rtc_app_key, self.settings.callback_secret):
            self.assertNotIn(value, ledger)

    async def test_unknown_start_is_cleaned_twice_with_exact_generated_identity(self):
        session = await self.create()
        cloud = AsyncMock(side_effect=[{"ok": False, "outcome": "unknown"}, {"ok": True}, {"ok": True}])
        with patch.object(self.manager, "_public_health", AsyncMock(return_value=True)), patch("backend_harness_loop.client.call_openapi", cloud):
            response = await self.post(session, "start")
        self.assertFalse(response.json()["running"])
        self.assertTrue(response.json()["stopped"])
        self.assertEqual([call.args[2] for call in cloud.await_args_list], ["StartVoiceChat", "StopVoiceChat", "StopVoiceChat"])
        for call in cloud.await_args_list:
            self.assertEqual(call.args[3]["RoomId"], session["room_id"])

    async def test_start_health_failure_prevents_cloud_task(self):
        session = await self.create()
        with patch.object(self.manager, "_public_health", AsyncMock(return_value=False)), patch("backend_harness_loop.client.call_openapi", new_callable=AsyncMock) as cloud:
            self.assertEqual((await self.post(session, "start")).status_code, 503)
            cloud.assert_not_awaited()

    async def test_start_cancellation_still_cleans_same_task(self):
        session = await self.create()
        async def call(client, settings, action, body):
            if action == "StartVoiceChat":
                raise asyncio.CancelledError()
            return {"ok": True}
        cloud = AsyncMock(side_effect=call)
        with patch.object(self.manager, "_public_health", AsyncMock(return_value=True)), patch("backend_harness_loop.client.call_openapi", cloud):
            with self.assertRaises(asyncio.CancelledError):
                await self.manager.start(self.manager.get(session["session_id"]))
        self.assertEqual(len(cloud.await_args_list), 3)
        self.assertEqual(self.manager.get(session["session_id"]).state, "stopped")

    async def test_heartbeat_expiry_and_unknown_cleanup_keep_state_truthful(self):
        session = await self.create()
        item = self.manager.get(session["session_id"])
        item.start_attempted = True
        item.state = "running"
        item.last_heartbeat = time.monotonic() - 61
        with patch("backend_harness_loop.client.call_openapi", AsyncMock(return_value={"ok": False, "outcome": "unknown"})) as cloud:
            await self.manager.reap()
            self.assertEqual(item.state, "stop_pending")
            self.assertEqual(cloud.await_count, 2)
        with patch("backend_harness_loop.client.call_openapi", AsyncMock(return_value={"ok": True})):
            await self.manager.reap()
        self.assertEqual(item.state, "stopped")
        self.assertEqual((await self.post(session, "heartbeat")).status_code, 409)

    async def test_events_record_only_metrics_and_do_not_auto_complete_m0(self):
        session = await self.create()
        for payload in ({"kind": "sdk_error", "elapsed_ms": 1, "text": "PRIVATE"}, {"kind": "sdk_error", "elapsed_ms": -1}, {"kind": "sdk_error", "elapsed_ms": 1, "code": "PRIVATE"}):
            self.assertEqual((await self.post(session, "events", payload)).status_code, 400)
        self.assertEqual((await self.post(session, "events", {"kind": "stop_confirmed", "elapsed_ms": 1})).status_code, 409)
        for kind in ("joined", "mic_started", "interrupt_sent", "heard_confirmed", "speech_input_confirmed", "stop_confirmed"):
            self.assertEqual((await self.post(session, "events", {"kind": kind, "elapsed_ms": 10})).status_code, 200)
        status = (await self.client.get(f'/api/m0/sessions/{session["session_id"]}', headers=self.headers)).json()
        self.assertTrue(all(status["manual_confirmations"].values()))
        self.assertFalse(status["m0_complete"])
        records = [json.loads(line) for line in self.manager.event_path.read_text("utf-8").splitlines()]
        self.assertEqual(records[-1]["scope"], "human_report")
        self.assertNotIn(session["token"], self.manager.event_path.read_text("utf-8"))

    async def test_explicit_static_token_uses_its_bound_room_user_and_unique_m0_task(self):
        token = issue_rtc_token(self.settings.rtc_app_id, "fixture-key", "UserSuppliedRoom", "UserSuppliedUser", lifetime_seconds=120)
        settings = replace(self.settings, rtc_app_key="", rtc_static_token=token.value,
                           rtc_static_room_id="UserSuppliedRoom", rtc_static_user_id="UserSuppliedUser")
        app = create_client_app(settings, self.path, "https://m0.example.com")
        manager = app.state.client_sessions
        session = await manager.create()
        self.assertEqual(session["room_id"], "UserSuppliedRoom")
        self.assertEqual(session["user_id"], "UserSuppliedUser")
        self.assertEqual(session["token"], token.value)
        self.assertLessEqual(session["expires_at"], token.expires_at)
        item = manager.get(session["session_id"])
        self.assertTrue(item.identity["TaskId"].startswith("m0_task_"))
        cloud = AsyncMock(return_value={"ok": True})
        with patch.object(manager, "_public_health", AsyncMock(return_value=True)), patch("backend_harness_loop.client.call_openapi", cloud):
            await manager.start(item)
            self.assertEqual(cloud.await_args.args[3]["AgentConfig"]["TargetUserId"], ["UserSuppliedUser"])
            await manager.stop(item)
        restored = create_client_app(settings, self.path, "https://m0.example.com").state.client_sessions
        self.assertEqual(restored.get(session["session_id"]).identity["RoomId"], "UserSuppliedRoom")
        self.assertNotIn(token.value, manager.ledger_path.read_text("utf-8"))

    async def test_explicit_static_token_wrong_binding_fails_without_creating_a_task(self):
        token = issue_rtc_token(self.settings.rtc_app_id, "fixture-key", "AnotherRoom", "BoundUser")
        settings = replace(self.settings, rtc_static_token=token.value,
                           rtc_static_room_id="WrongRoom", rtc_static_user_id="BoundUser")
        manager = create_client_app(settings, self.path, "https://m0.example.com").state.client_sessions
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as failure:
            await manager.create()
        self.assertEqual(failure.exception.detail, {"code": "rtc_static_token_invalid"})
        self.assertFalse(manager.sessions)
        self.assertFalse(manager.ledger_path.exists())

    async def test_stopped_provider_observations_survive_restart_and_next_session_persistence(self):
        data = await self.create()
        item = self.manager.get(data["session_id"])
        observations = [
            {"action": "StartVoiceChat", "ok": True, "http_status": 200, "duration_ms": 12.5,
             "response_shape": {"metadata_present": True, "request_id_present": True}},
            {"action": "StopVoiceChat", "ok": True, "http_status": 200, "duration_ms": 8.25},
            {"action": "StopVoiceChat", "ok": True, "http_status": 200, "duration_ms": 9.75},
        ]
        with patch.object(self.manager, "_public_health", AsyncMock(return_value=True)), patch("backend_harness_loop.client.call_openapi", AsyncMock(side_effect=observations)):
            self.assertTrue((await self.manager.start(item))["running"])
            self.assertTrue((await self.manager.stop(item))["stopped"])
        saved = json.loads(self.manager.ledger_path.read_text("utf-8"))["sessions"][0]
        with patch("backend_harness_loop.client.call_openapi", new_callable=AsyncMock) as cloud:
            restored = create_client_app(self.settings, self.path, "https://m0.example.com").state.client_sessions
            recovered = restored.get(data["session_id"])
            self.assertEqual(recovered.state, "stopped")
            self.assertEqual(recovered.cleanup_rounds, 1)
            self.assertEqual(recovered.provider, observations)
            # A new session rewrites every ledger entry: the old observations must survive.
            await restored.create()
            rewritten = json.loads(restored.ledger_path.read_text("utf-8"))["sessions"][0]
            self.assertEqual(rewritten, saved)
            again = create_client_app(self.settings, self.path, "https://m0.example.com").state.client_sessions
            self.assertEqual(again.get(data["session_id"]).provider, observations)
            self.assertEqual(again.get(data["session_id"]).cleanup_rounds, 1)
            cloud.assert_not_awaited()

    async def test_recovered_pending_cleanup_keeps_retry_budget_and_appends_provider_observations(self):
        data = await self.create()
        item = self.manager.get(data["session_id"])
        prior = [
            {"action": "StartVoiceChat", "ok": True, "http_status": 200},
            *[{"action": "StopVoiceChat", "ok": False, "outcome": "unknown"} for _ in range(4)],
        ]
        item.state = "stop_pending"
        item.start_attempted = True
        item.cleanup_rounds = 2
        item.provider = prior
        self.manager._persist_ledger()
        restored = create_client_app(self.settings, self.path, "https://m0.example.com").state.client_sessions
        recovered = restored.get(data["session_id"])
        new_observation = {"action": "StopVoiceChat", "ok": False, "outcome": "unknown", "error_code": "network_timeout"}
        with patch("backend_harness_loop.client.call_openapi", AsyncMock(return_value=new_observation)) as cloud:
            await restored.reap()
            self.assertEqual(recovered.state, "stop_pending")
            self.assertEqual(recovered.cleanup_rounds, 3)
            self.assertEqual(recovered.provider, prior + [new_observation, new_observation])
            self.assertEqual([call.args[2] for call in cloud.await_args_list], ["StopVoiceChat", "StopVoiceChat"])
            self.assertTrue(all(call.args[3] == item.identity for call in cloud.await_args_list))
        # Restart must not reset the three-round automatic cleanup limit.
        again = create_client_app(self.settings, self.path, "https://m0.example.com").state.client_sessions
        with patch("backend_harness_loop.client.call_openapi", new_callable=AsyncMock) as cloud:
            await again.reap()
            cloud.assert_not_awaited()
        self.assertEqual(again.get(data["session_id"]).cleanup_rounds, 3)
        self.assertEqual(again.get(data["session_id"]).provider, recovered.provider)

    async def test_legacy_recovery_ledger_without_observation_fields_remains_compatible(self):
        data = await self.create()
        await self.manager.stop(self.manager.get(data["session_id"]))
        ledger = json.loads(self.manager.ledger_path.read_text("utf-8"))
        for field in ("cleanup_rounds", "provider"):
            del ledger["sessions"][0][field]
        self.manager.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        with patch("backend_harness_loop.client.call_openapi", new_callable=AsyncMock) as cloud:
            restored = create_client_app(self.settings, self.path, "https://m0.example.com").state.client_sessions
            recovered = restored.get(data["session_id"])
            self.assertEqual(recovered.cleanup_rounds, 0)
            self.assertEqual(recovered.provider, [])
            self.assertEqual(recovered.identity, self.manager.get(data["session_id"]).identity)
            await restored.create()
            cloud.assert_not_awaited()
        rewritten = json.loads(restored.ledger_path.read_text("utf-8"))["sessions"][0]
        self.assertEqual(rewritten["cleanup_rounds"], 0)
        self.assertEqual(rewritten["provider"], [])


if __name__ == "__main__":
    unittest.main()
