"""Owned RTC fixtures and extra controls never bypass normal local fencing."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import httpx

from backend_harness_loop.config import Settings
from backend_harness_loop.models import ModelProfile, ModelProfiles
from backend_harness_loop.probes.rtc_context import RTCValidationRuntime, create_rtc_context_app
from backend_harness_loop.store import RuntimeStore
from backend_harness_loop.tests.test_context_runtime import Model


class RTCContextProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(":memory:")
        profile = ModelProfile("fixture", "fixture", "https://fixture.example", "fixture-model", "KEY", "unused")
        self.profiles = ModelProfiles({"fixture": profile}, "fixture")
        self.gate = None
        self.created = []

        def factory(selected):
            model = Model(final_gate=self.gate)
            model.profile = selected
            self.created.append(model)
            return model
        self.runtime = RTCValidationRuntime(self.store, self.profiles, first_output_ms=9000,
            model_factory=factory, background_context_enabled=False)
        self.runtime.register_session("m0_session_fixture", "run", "fixture", "task", "user")
        self.app = create_rtc_context_app(Settings(callback_secret="fixture-key"), self.profiles,
            Path(self.folder.name)/"unused.sqlite3", Path(self.folder.name)/"capture.jsonl", "https://callback.example.com", runtime=self.runtime)
        self.app.state.client_sessions.sessions["m0_session_fixture"] = SimpleNamespace(state="running", identity={})
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:3101",
            headers={"X-M0-Client-Key": self.app.state.client_key})

    async def asyncTearDown(self):
        if self.gate:
            self.gate.set()
        await self.client.aclose()
        await self.runtime.aclose()
        self.store.close()
        self.folder.cleanup()

    async def test_fixture_is_raw_closed_history_and_has_no_model_or_summary(self):
        self.assertEqual(self.created, [])
        self.assertEqual(self.store.snapshot("m0_session_fixture")["message_count"], 28)
        self.assertEqual(self.store.get_context("m0_session_fixture")["compacted_through_seq"], 0)
        self.runtime.register_session("m0_session_fixture", "run", "fixture", "task", "user")
        self.assertEqual(self.store.snapshot("m0_session_fixture")["message_count"], 28)

    async def test_extra_controls_reuse_nonce_host_origin_and_forwarded_fences(self):
        url = "/api/m2/rtc-validation/m0_session_fixture"
        for headers, expected in (({"X-M0-Client-Key": "bad"}, 401), ({"Host": "public.example"}, 403),
            ({"Origin": "https://public.example"}, 403), ({"X-Forwarded-For": "127.0.0.1"}, 403)):
            response = await self.client.post(url, json={"step": "pressure_tail"}, headers=headers)
            self.assertEqual(response.status_code, expected)
        response = await self.client.post(url, json={"step": []})
        self.assertEqual(response.status_code, 400)

    async def test_pressure_is_single_use_after_a_completed_reply_and_preserves_archive(self):
        handle = await self.runtime.accept("run", "real", "hello")
        self.assertEqual([s async for s in self.runtime.speech(handle)], ["FINAL-ANSWER"])
        await handle.task
        response = await self.client.post("/api/m2/rtc-validation/m0_session_fixture", json={"step": "pressure_tail"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scripted_closed_turns_appended"], 4)
        material = self.store.context_material("m0_session_fixture")
        self.assertEqual(len(material["groups"]), 19)
        self.assertEqual(self.store.get_context("m0_session_fixture")["compacted_through_seq"], 0)
        response = await self.client.post("/api/m2/rtc-validation/m0_session_fixture", json={"step": "pressure_tail"})
        self.assertEqual(response.status_code, 409)

    async def test_pressure_cannot_write_during_an_active_reply(self):
        self.gate = asyncio.Event()
        handle = await self.runtime.accept("run", "active", "hello")
        response = await self.client.post("/api/m2/rtc-validation/m0_session_fixture", json={"step": "pressure_tail"})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("m0_session_fixture", self.runtime.pressure_appended)
        self.assertEqual(len(self.store.context_material("m0_session_fixture")["groups"]), 15)


if __name__ == "__main__":
    unittest.main()
