"""Measurement API fixtures: no RTC requests, microphone or audio output."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from backend_harness_loop.client import create_client_app
from backend_harness_loop.config import Settings


class ClientMeasurementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "capture.jsonl"
        self.app = create_client_app(Settings(rtc_app_id="012345678901234567890123", rtc_app_key="fixture-key", callback_secret="fixture-secret"), self.path, "https://m0.example.com")
        self.manager = self.app.state.client_sessions
        self.session = await self.manager.create()
        self.id = self.session["session_id"]
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:3101")
        self.headers = {"X-M0-Client-Key": self.app.state.client_key}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.folder.cleanup()

    async def post(self, action, payload):
        if action == "measurements":
            await self.client.post(f"/api/m0/sessions/{self.id}/measurement-starts", json={key: payload[key] for key in ("sample_id", "kind", "started_ms")}, headers=self.headers)
        return await self.client.post(f"/api/m0/sessions/{self.id}/{action}", json=payload, headers=self.headers)

    def sample(self, number=1, kind="interrupt_silence"):
        return {"sample_id": f"m0_sample_{number:032x}", "kind": kind, "state": "completed",
                "started_ms": 1000, "trigger_ms": 1500, "observed_ms": 2200,
                "confirmed_ms": 3300, "round_id": "fixture-round"}

    async def test_retries_are_idempotent_but_changed_terminal_samples_conflict(self):
        payload = self.sample()
        first = await self.post("measurements", payload)
        retry = await self.post("measurements", payload)
        changed = await self.post("measurements", {**payload, "observed_ms": 2300})
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(retry.json()["duplicate"])
        self.assertEqual(changed.status_code, 409)
        records = [json.loads(line) for line in self.manager.measurement_path.read_text("utf-8").splitlines()]
        self.assertEqual([record["phase"] for record in records], ["started", "terminal"])
        self.assertNotIn("fixture-round", self.manager.measurement_path.read_text("utf-8"))

    async def test_completed_speech_requires_mic_start_energy_and_uninterrupted_capture(self):
        sample = self.sample(kind="speech_first_audio")
        self.assertEqual((await self.post("measurements", sample)).status_code, 409)
        await self.post("events", {"kind": "mic_started", "elapsed_ms": 500})
        self.assertEqual((await self.post("measurements", sample)).status_code, 409)
        await self.post("events", {"kind": "mic_level_active", "elapsed_ms": 600})
        self.assertEqual((await self.post("measurements", sample)).status_code, 409)
        await self.post("events", {"kind": "mic_level_active", "elapsed_ms": 1200})
        self.assertEqual((await self.post("measurements", sample)).status_code, 200)
        await self.post("events", {"kind": "mic_ended", "elapsed_ms": 2000})
        self.assertEqual((await self.post("measurements", self.sample(2, kind="speech_first_audio"))).status_code, 409)

    async def test_failed_attempts_and_unsafe_payloads_are_not_silently_counted(self):
        invalid = {"sample_id": "m0_sample_" + "b" * 32, "kind": "speech_first_audio", "state": "invalid", "started_ms": 10, "reason": "timeout"}
        self.assertEqual((await self.post("measurements", invalid)).status_code, 200)
        self.assertEqual((await self.post("measurements", {**self.sample(), "text": "PRIVATE SPEECH"})).status_code, 400)
        status = (await self.client.get(f"/api/m0/sessions/{self.id}", headers=self.headers)).json()
        self.assertEqual(status["measurements_summary"]["invalid_count"], 1)
        self.assertFalse(status["measurements_summary"]["ready"])
        self.assertFalse(status["m0_complete"])

    async def test_only_same_run_observed_callback_rounds_enter_summary(self):
        await self.post("measurements", self.sample())
        route = f"/api/m0/sessions/{self.id}"
        status = (await self.client.get(route, headers=self.headers)).json()
        self.assertEqual(status["measurements_summary"]["unmatched_count"], 1)
        run = self.manager.get(self.id).run_id
        def captured(run_id):
            return {"source": "custom_llm", "correlation": {"identifiers": [
                {"kind": "run", "sha256": hashlib.sha256(run_id.encode()).hexdigest()},
                {"kind": "round", "sha256": hashlib.sha256(b"fixture-round").hexdigest()},
            ]}}
        self.path.write_text(json.dumps(captured("different-run")) + "\n", encoding="utf-8")
        status = (await self.client.get(route, headers=self.headers)).json()
        self.assertEqual(status["measurements_summary"]["interrupt_silence"]["count"], 0)
        self.path.write_text(json.dumps(captured(run)) + "\n", encoding="utf-8")
        status = (await self.client.get(route, headers=self.headers)).json()
        self.assertEqual(status["measurements_summary"]["interrupt_silence"]["count"], 1)
        self.assertFalse(status["measurements_summary"]["interrupt_silence"]["ready"])

    async def test_started_but_unfinished_attempt_remains_visible_and_terminal_requires_start(self):
        sample = self.sample()
        response = await self.client.post(f"/api/m0/sessions/{self.id}/measurements", json=sample, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        begin = {key: sample[key] for key in ("sample_id", "kind", "started_ms")}
        first = await self.post("measurement-starts", begin)
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue((await self.post("measurement-starts", begin)).json()["duplicate"])
        self.assertEqual((await self.post("measurement-starts", {**begin, "started_ms": 1001})).status_code, 409)
        status = (await self.client.get(f"/api/m0/sessions/{self.id}", headers=self.headers)).json()
        self.assertEqual(status["measurements_summary"]["incomplete_count"], 1)
        self.assertFalse(status["measurements_summary"]["ready"])

    async def test_malformed_event_types_do_not_poison_later_measurement_comparisons(self):
        for payload in ({"kind": "mic_started", "elapsed_ms": None},
                        {"kind": "mic_started", "elapsed_ms": 10**1000},
                        {"kind": "mic_started", "elapsed_ms": 1, "duration_ms": None},
                        {"kind": ["mic_started"], "elapsed_ms": 1}):
            self.assertEqual((await self.post("events", payload)).status_code, 400)
        self.assertEqual(self.manager.get(self.id).events, [])


if __name__ == "__main__":
    unittest.main()
