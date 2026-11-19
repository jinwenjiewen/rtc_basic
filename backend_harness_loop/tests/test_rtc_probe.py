"""Offline protocol and resource-cleanup tests; no credentials or cloud calls."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend_harness_loop.probes import rtc


def settings(**overrides):
    values = {
        "volc_access_key": "test-access-key",
        "volc_secret_key": "test-secret-key",
        "rtc_app_id": "test-app",
        "rtc_api_version": "2025-06-01",
        "rtc_region": "cn-north-1",
        "rtc_openapi_url": rtc.OPENAPI_URL,
        "rtc_timeout_seconds": 1,
        "rtc_tts_voice": rtc.DEFAULT_VOICE,
        "callback_secret": "test-callback-secret",
        "trust_env": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def success():
    return httpx.Response(200, json={"ResponseMetadata": {"RequestId": "test-request"}})


def legacy_profile():
    return {
        "ASRConfig": {
            "Provider": "volcano",
            "ProviderParams": {"Mode": "smallmodel", "AppId": "fixture-asr-app", "Cluster": "fixture-asr-cluster"},
        },
        "TTSConfig": {
            "Provider": "volcano",
            "ProviderParams": {
                "app": {"appid": "fixture-tts-app", "cluster": "fixture-tts-cluster"},
                "audio": {"voice_type": "BV001_streaming", "speed_ratio": 1},
            },
        },
    }


class SignatureAndShapeTests(unittest.TestCase):
    def test_signed_body_is_exact_utf8_json_and_canonical_query(self):
        config = settings()
        body = {"AppId": "test-app", "RoomId": "m0_中文", "TaskId": "m0_task"}
        url, headers, content = rtc.sign_request(
            config, "StopVoiceChat", body,
            now=datetime(2026, 11, 8, 6, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(url, "https://rtc.volcengineapi.com/?Action=StopVoiceChat&Version=2025-06-01")
        self.assertEqual(content, '{"AppId":"test-app","RoomId":"m0_中文","TaskId":"m0_task"}'.encode())
        self.assertEqual(headers["X-Content-Sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(headers["X-Date"], "20261108T063000Z")
        self.assertIn("/20261108/cn-north-1/rtc/request", headers["Authorization"])
        # Independent golden vector produced with the installed official
        # volcenginesdkcore.signv4.SignerV4 and a frozen UTC clock.
        self.assertTrue(headers["Authorization"].endswith(
            "Signature=e2899a9c80f0b9e753e1a1f020876c71734db6bfc53f591e76df3e64ab14bdeb"
        ))
        # Signing different actual bytes must produce a different signature.
        _, changed, _ = rtc.sign_request(config, "StopVoiceChat", {**body, "TaskId": "another"}, now=datetime(2026, 11, 8, 6, 30, tzinfo=timezone.utc))
        self.assertNotEqual(headers["Authorization"], changed["Authorization"])

    def test_official_metadata_only_success_and_empty_error_are_accepted(self):
        self.assertTrue(rtc.classify_response(success())["ok"])
        result = rtc.classify_response(httpx.Response(200, json={"ResponseMetadata": {"RequestId": "id", "Error": {}}}))
        self.assertTrue(result["ok"])
        self.assertFalse(result["response_shape"]["error_present"])
        self.assertFalse(result["response_shape"]["result_present"])

    def test_http_200_provider_error_and_http_error_are_failures(self):
        result = rtc.classify_response(httpx.Response(200, json={"ResponseMetadata": {"Error": {"Code": "AccessDenied", "Message": "do not retain this"}}}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "AccessDenied")
        self.assertNotIn("Message", json.dumps(result))
        self.assertFalse(rtc.classify_response(httpx.Response(503, json={"ResponseMetadata": {"RequestId": "id"}}))["ok"])

    def test_invalid_and_unrecognized_success_are_not_silent_passes(self):
        self.assertEqual(rtc.classify_response(httpx.Response(200, text="not json"))["error_code"], "invalid_json")
        self.assertFalse(rtc.classify_response(httpx.Response(200, json=[]))["ok"])
        self.assertFalse(rtc.classify_response(httpx.Response(200, json={}))["ok"])

    def test_official_host_only_and_unexpected_actions_rejected(self):
        with self.assertRaises(ValueError):
            rtc.sign_request(settings(rtc_openapi_url="https://attacker.invalid"), "StopVoiceChat", {})
        with self.assertRaises(ValueError):
            rtc.sign_request(settings(), "QueryVoiceChat", {})
        url, headers, content = rtc.sign_request(settings(), "UpdateVoiceChat", {"Command": "interrupt"})
        self.assertIn("Action=UpdateVoiceChat", url)
        self.assertEqual(headers["X-Content-Sha256"], hashlib.sha256(content).hexdigest())

    def test_public_profile_callback_and_events_are_bound(self):
        body = rtc.build_start_body(settings(), {"AppId": "a", "RoomId": "m0_room", "TaskId": "m0_task"}, "https://capture.example/api/chat_callback?token=secret&rtc_run_id=m0_run")
        self.assertEqual(body["AgentConfig"]["ServerMessageURLForRTS"], "https://capture.example/api/rtc/events?rtc_run_id=m0_run")
        self.assertEqual(body["AgentConfig"]["ServerMessageSignatureForRTS"], "test-callback-secret")
        self.assertFalse(body["Config"]["LLMConfig"]["Prefill"])
        self.assertNotIn("Method", body["Config"]["LLMConfig"])
        self.assertNotIn("ApiType", body["Config"]["LLMConfig"])
        self.assertEqual(body["Config"]["TTSConfig"]["ProviderParams"]["ResourceId"], "seed-tts-2.0")
        with self.assertRaises(ValueError):
            rtc.build_start_body(settings(rtc_tts_voice="BV001_streaming"), {}, "https://capture.example/callback")
        for url in ("http://capture.example/callback", "https://localhost/callback", "https://127.0.0.1/callback", "https://user:pass@capture.example/callback"):
            with self.assertRaises(ValueError):
                rtc.build_start_body(settings(), {}, url)

    def test_legacy_profile_preserves_custom_resources_without_mutating_source(self):
        profile = legacy_profile()
        body = rtc.build_start_body(
            settings(rtc_api_version="2024-12-01", rtc_legacy_voice_config=profile),
            {"AppId": "a", "RoomId": "m0_room", "TaskId": "m0_task"},
            "https://capture.example/api/chat_callback?token=secret&rtc_run_id=m0_run",
        )
        self.assertEqual(body["Config"]["ASRConfig"], profile["ASRConfig"])
        self.assertEqual(body["Config"]["TTSConfig"], profile["TTSConfig"])
        self.assertNotIn("ResourceId", body["Config"]["TTSConfig"]["ProviderParams"])
        self.assertNotIn("ApiResourceId", body["Config"]["ASRConfig"]["ProviderParams"])
        self.assertEqual(body["Config"]["LLMConfig"]["Mode"], "CustomLLM")
        self.assertFalse(body["Config"]["LLMConfig"]["Prefill"])
        self.assertTrue(body["Config"]["LLMConfig"]["EnableRoundId"])
        self.assertTrue(body["AgentConfig"]["EnableConversationStateCallback"])
        self.assertEqual(body["AgentConfig"]["UserId"], "m0_agent_m0_task")
        self.assertEqual(body["AgentConfig"]["TargetUserId"], ["m0_user_m0_task"])
        self.assertEqual(body["AgentConfig"]["ServerMessageURLForRTS"], "https://capture.example/api/rtc/events?rtc_run_id=m0_run")
        body["Config"]["TTSConfig"]["ProviderParams"]["audio"]["voice_type"] = "changed"
        self.assertEqual(profile["TTSConfig"]["ProviderParams"]["audio"]["voice_type"], "BV001_streaming")

    def test_legacy_signed_request_uses_selected_api_version(self):
        url, headers, content = rtc.sign_request(
            settings(rtc_api_version="2024-12-01"),
            "StartVoiceChat", {"AppId": "fixture", "TaskId": "m0_fixture"},
        )
        self.assertEqual(url.split("Version=")[1], "2024-12-01")
        self.assertEqual(headers["X-Content-Sha256"], hashlib.sha256(content).hexdigest())

    def test_legacy_missing_and_malformed_profiles_are_explicit_configuration_errors(self):
        for profile in (None, {}, {"ASRConfig": {}}, {"ASRConfig": {"Provider": "volcano", "ProviderParams": {}}, "TTSConfig": {"Provider": "volcano", "ProviderParams": {}}}):
            with self.subTest(profile=profile), self.assertRaises(rtc.RTCProbeConfigurationError):
                rtc.build_start_body(settings(rtc_api_version="2024-12-01", rtc_legacy_voice_config=profile), {}, "https://capture.example/callback")


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, config, handler, **kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(rtc.httpx, "AsyncClient", return_value=client):
            return await rtc.run_rtc_probe(config, **kwargs)

    async def test_default_probe_never_starts_and_only_stops_own_identifiers(self):
        seen = []

        def handler(request):
            self.assertEqual(request.url.params["Action"], "StopVoiceChat")
            body = json.loads(request.content)
            self.assertTrue(body["RoomId"].startswith("m0_room_"))
            self.assertTrue(body["TaskId"].startswith("m0_task_"))
            self.assertEqual(request.headers["x-content-sha256"], hashlib.sha256(request.content).hexdigest())
            seen.append(body)
            return success()

        result = await self.invoke(settings(), handler)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("query_unverified", result["evidence"]["query_capability"])
        self.assertEqual(result["evidence"]["live_start_stop_gate"], "blocked_start_not_requested")

    async def test_legacy_missing_profile_blocks_start_without_network(self):
        with patch.object(rtc.httpx, "AsyncClient") as client:
            result = await rtc.run_rtc_probe(settings(rtc_api_version="2024-12-01"), start=True, callback_url="https://capture.example/callback")
        client.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["evidence"]["error_code"], "missing_config")
        self.assertEqual(result["evidence"]["configuration_fields"], ["rtc_legacy_voice_config"])

    async def test_legacy_text_probe_keeps_version_and_resource_profile_on_all_calls(self):
        seen = []

        def handler(request):
            self.assertEqual(request.url.params["Version"], "2024-12-01")
            seen.append((request.url.params["Action"], json.loads(request.content)))
            return success()

        with patch.object(rtc.asyncio, "sleep", new_callable=AsyncMock):
            result = await self.invoke(
                settings(rtc_api_version="2024-12-01", rtc_legacy_voice_config=legacy_profile(), rtc_text_probe=True),
                handler, start=True, callback_url="https://capture.example/api/chat_callback?token=secret&rtc_run_id=m0_run",
            )
        self.assertEqual([item[0] for item in seen], ["StartVoiceChat", "UpdateVoiceChat", "UpdateVoiceChat", "UpdateVoiceChat", "StopVoiceChat", "StopVoiceChat"])
        self.assertEqual(seen[0][1]["Config"]["TTSConfig"], legacy_profile()["TTSConfig"])
        self.assertEqual(seen[1][1]["Message"], seen[2][1]["Message"])
        self.assertTrue(all(item[1]["TaskId"] == seen[0][1]["TaskId"] for item in seen))
        self.assertTrue(result["evidence"]["cleanup_confirmed"])
        self.assertEqual(result["evidence"]["voice_profile"], "legacy_custom_resources")
        self.assertEqual(result["status"], "blocked")

    async def test_start_success_always_stops_twice_but_does_not_pass_audio_gate(self):
        seen = []

        def handler(request):
            seen.append((request.url.params["Action"], json.loads(request.content)))
            return success()

        result = await self.invoke(settings(), handler, start=True, callback_url="https://capture.example/api/chat_callback?token=secret&rtc_run_id=run")
        self.assertEqual([action for action, _ in seen], ["StartVoiceChat", "StopVoiceChat", "StopVoiceChat"])
        self.assertEqual(seen[0][1]["TaskId"], seen[1][1]["TaskId"])
        self.assertEqual(seen[1][1], seen[2][1])
        self.assertTrue(result["evidence"]["cleanup_confirmed"])
        self.assertEqual(result["evidence"]["live_start_stop_gate"], "passed")
        self.assertEqual(result["status"], "blocked")
        rendered = json.dumps(result)
        for secret in ("test-access-key", "test-secret-key", "test-callback-secret", "https://capture.example", "token=secret"):
            self.assertNotIn(secret, rendered)

    async def test_unknown_start_timeout_still_cleans_same_task(self):
        seen = []

        def handler(request):
            seen.append((request.url.params["Action"], json.loads(request.content)))
            if seen[-1][0] == "StartVoiceChat":
                raise httpx.ReadTimeout("secret-containing-url-must-not-be-reported", request=request)
            return success()

        result = await self.invoke(settings(), handler, start=True, callback_url="https://capture.example/callback")
        self.assertEqual(result["evidence"]["start_outcome"], "unknown")
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[0][1]["TaskId"], seen[2][1]["TaskId"])
        self.assertTrue(result["evidence"]["cleanup_confirmed"])
        self.assertNotIn("secret-containing", json.dumps(result))

    async def test_text_probe_submits_two_identical_inputs_and_interrupt_to_only_fresh_task(self):
        seen = []

        def handler(request):
            seen.append((request.url.params["Action"], json.loads(request.content)))
            return success()

        with patch.object(rtc.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = await self.invoke(
                settings(rtc_text_probe=True, rtc_probe_hold_seconds=3),
                handler,
                start=True,
                callback_url="https://capture.example/api/chat_callback?token=secret&rtc_run_id=run",
            )
        self.assertEqual([item[0] for item in seen], ["StartVoiceChat", "UpdateVoiceChat", "UpdateVoiceChat", "UpdateVoiceChat", "StopVoiceChat", "StopVoiceChat"])
        self.assertEqual(seen[1][1], seen[2][1])
        self.assertEqual(seen[1][1]["Message"], "这是M0验证，请简短回复。")
        self.assertEqual(seen[1][1]["InterruptMode"], 1)
        self.assertEqual(seen[3][1]["Command"], "interrupt")
        self.assertEqual({body["TaskId"] for _, body in seen}, {seen[0][1]["TaskId"]})
        self.assertTrue(seen[0][1]["TaskId"].startswith("m0_task_"))
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [3.0, 3.0, 0.25])
        self.assertEqual(result["evidence"]["network_calls"], 6)
        self.assertEqual(result["evidence"]["text_probe_same_input_occurrences"], 2)
        self.assertEqual(result["evidence"]["text_probe_gate"], "commands_accepted_callback_correlation_pending")
        self.assertEqual(result["status"], "blocked")

    async def test_text_probe_not_run_if_start_rejected(self):
        actions = []

        def handler(request):
            actions.append(request.url.params["Action"])
            if actions[-1] == "StartVoiceChat":
                return httpx.Response(200, json={"ResponseMetadata": {"Error": {"Code": "InvalidAppId"}}})
            return success()

        with patch.object(rtc.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = await self.invoke(settings(rtc_text_probe=True), handler, start=True, callback_url="https://capture.example/callback")
        self.assertEqual(actions, ["StartVoiceChat", "StopVoiceChat", "StopVoiceChat"])
        sleep.assert_not_awaited()
        self.assertNotIn("command_observations", result["evidence"])

    async def test_interrupt_only_sends_one_input_then_interrupt_and_keeps_cleanup(self):
        seen = []

        def handler(request):
            self.assertEqual(request.url.params["Version"], "2024-12-01")
            seen.append((request.url.params["Action"], json.loads(request.content)))
            return success()

        with patch.object(rtc.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = await self.invoke(
                settings(rtc_api_version="2024-12-01", rtc_legacy_voice_config=legacy_profile(), rtc_interrupt_only=True, rtc_probe_hold_seconds=0.5),
                handler, start=True, callback_url="https://capture.example/callback",
            )
        self.assertEqual([item[0] for item in seen], ["StartVoiceChat", "UpdateVoiceChat", "UpdateVoiceChat", "StopVoiceChat", "StopVoiceChat"])
        self.assertEqual(seen[1][1]["Command"], "ExternalTextToLLM")
        self.assertEqual(seen[2][1]["Command"], "interrupt")
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.5, 0.5])
        self.assertEqual(result["evidence"]["interrupt_probe_input_occurrences"], 1)
        self.assertEqual(result["evidence"]["network_calls"], 5)
        self.assertEqual(result["evidence"]["configured_wait_budget_seconds"], 1)
        self.assertNotIn("text_probe_same_input_occurrences", result["evidence"])
        self.assertTrue(result["evidence"]["cleanup_confirmed"])
        self.assertEqual(result["status"], "blocked")

    async def test_conflicting_probe_modes_are_blocked_without_network(self):
        with patch.object(rtc.httpx, "AsyncClient") as client:
            result = await rtc.run_rtc_probe(settings(rtc_text_probe=True, rtc_interrupt_only=True), start=True, callback_url="https://capture.example/callback")
        client.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["evidence"]["error_code"], "conflicting_probe_modes")

    async def test_interrupt_only_invalid_hold_is_blocked_before_cloud_calls(self):
        with patch.object(rtc.httpx, "AsyncClient") as client:
            for hold in (0, 0.25, 7.1):
                result = await rtc.run_rtc_probe(settings(rtc_interrupt_only=True, rtc_probe_hold_seconds=hold), start=True, callback_url="https://capture.example/callback")
                self.assertEqual(result["status"], "blocked")
        client.assert_not_called()

    async def test_interrupt_only_does_not_send_commands_after_start_rejection(self):
        actions = []

        def handler(request):
            actions.append(request.url.params["Action"])
            return httpx.Response(200, json={"ResponseMetadata": {"Error": {"Code": "Rejected"}}}) if actions[-1] == "StartVoiceChat" else success()

        with patch.object(rtc.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = await self.invoke(settings(rtc_interrupt_only=True), handler, start=True, callback_url="https://capture.example/callback")
        self.assertEqual(actions, ["StartVoiceChat", "StopVoiceChat", "StopVoiceChat"])
        sleep.assert_not_awaited()
        self.assertNotIn("command_observations", result["evidence"])

    async def test_text_probe_deliberate_wait_budget_is_bounded_before_any_cloud_mutation(self):
        with patch.object(rtc.httpx, "AsyncClient") as client:
            result = await rtc.run_rtc_probe(settings(rtc_text_probe=True, rtc_probe_hold_seconds=8), start=True, callback_url="https://capture.example/callback")
        self.assertEqual(result["status"], "blocked")
        client.assert_not_called()

    async def test_cleanup_failure_retains_identifiers_and_unknown_outcome(self):
        def handler(request):
            if request.url.params["Action"] == "StartVoiceChat":
                return success()
            raise httpx.ConnectError("do not serialize exception", request=request)

        result = await self.invoke(settings(), handler, start=True, callback_url="https://capture.example/callback")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["evidence"]["cleanup_required"])
        self.assertEqual(len(result["evidence"]["cleanup_observations"]), 2)
        self.assertTrue(result["evidence"]["probe_task_id"].startswith("m0_task_"))

    async def test_cancellation_during_start_does_not_abandon_cleanup(self):
        started = asyncio.Event()
        actions = []

        async def handler(request):
            action = request.url.params["Action"]
            actions.append(action)
            if action == "StartVoiceChat":
                started.set()
                await asyncio.Event().wait()
            return success()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(rtc.httpx, "AsyncClient", return_value=client):
            task = asyncio.create_task(rtc.run_rtc_probe(settings(), start=True, callback_url="https://capture.example/callback"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(actions, ["StartVoiceChat", "StopVoiceChat", "StopVoiceChat"])

    async def test_missing_config_invalid_start_and_endpoint_make_no_network_calls(self):
        with patch.object(rtc.httpx, "AsyncClient") as client:
            for config, kwargs in (
                (SimpleNamespace(), {}),
                (settings(), {"start": True, "callback_url": "https://localhost/callback"}),
                (settings(rtc_api_version="unsupported-version"), {}),
                (settings(rtc_openapi_url="https://elsewhere.example"), {}),
            ):
                result = await rtc.run_rtc_probe(config, **kwargs)
                self.assertEqual(result["status"], "blocked")
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
