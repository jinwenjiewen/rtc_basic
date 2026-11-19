"""Crash recovery and cancellation cleanup without real cloud side effects."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend_harness_loop.client import ClientSessions, create_client_app
from backend_harness_loop.config import Settings


PUBLIC_URL = "https://m0.example.com"


class ClientRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "capture.jsonl"
        self.settings = Settings(
            rtc_app_id="012345678901234567890123", rtc_app_key="fixture-app-key",
            callback_secret="fixture-callback-secret", volc_access_key="fixture-ak",
            volc_secret_key="fixture-sk", rtc_legacy_voice_config={
                "ASRConfig": {"Provider": "volcano", "ProviderParams": {"Mode": "smallmodel"}},
                "TTSConfig": {"Provider": "volcano", "ProviderParams": {"audio": {"voice_type": "BV001_streaming"}}},
            },
        )

    async def crash_ledger(self):
        manager = ClientSessions(self.settings, self.path, PUBLIC_URL)
        data = await manager.create()
        session = manager.get(data["session_id"])
        # This is the durable state left by a process dying after cloud Start.
        session.start_attempted = True
        session.state = "running"
        manager._persist_ledger()
        return data, manager.ledger_path

    async def test_startup_cleans_recovered_task_before_accepting_new_session(self):
        old, ledger_path = await self.crash_ledger()
        app = create_client_app(self.settings, self.path, PUBLIC_URL)
        manager = app.state.client_sessions
        recovered = manager.get(old["session_id"])
        self.assertLess(recovered.deadline, time.monotonic())
        with self.assertRaises(HTTPException) as blocked:
            await manager.create()
        self.assertEqual(blocked.exception.status_code, 409)
        cloud = AsyncMock(return_value={"ok": True})
        with patch("backend_harness_loop.client.call_openapi", cloud):
            async with app.router.lifespan_context(app):
                self.assertEqual(recovered.state, "stopped")
                self.assertEqual([call.args[2] for call in cloud.await_args_list], ["StopVoiceChat", "StopVoiceChat"])
                for call in cloud.await_args_list:
                    self.assertEqual(call.args[3], {"AppId": self.settings.rtc_app_id, "RoomId": old["room_id"], "TaskId": old["session_id"].replace("m0_session_", "m0_task_")})
                new = await manager.create()
                self.assertNotEqual(new["room_id"], old["room_id"])
        entries = json.loads(ledger_path.read_text("utf-8"))["sessions"]
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(entry["state"] == "stopped" for entry in entries))
        self.assertEqual(cloud.await_count, 2)  # The new unstarted session needs no Stop.

    async def test_unknown_startup_cleanup_preserves_identity_and_blocks_new_task(self):
        old, ledger_path = await self.crash_ledger()
        app = create_client_app(self.settings, self.path, PUBLIC_URL)
        manager = app.state.client_sessions
        cloud = AsyncMock(return_value={"ok": False, "outcome": "unknown"})
        with patch("backend_harness_loop.client.call_openapi", cloud):
            async with app.router.lifespan_context(app):
                self.assertEqual(manager.get(old["session_id"]).state, "stop_pending")
                with self.assertRaises(HTTPException) as blocked:
                    await manager.create()
                self.assertEqual(blocked.exception.status_code, 409)
                entries = json.loads(ledger_path.read_text("utf-8"))["sessions"]
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["room_id"], old["room_id"])
                self.assertEqual(entries[0]["state"], "stop_pending")
        self.assertEqual(manager.get(old["session_id"]).state, "stop_pending")
        self.assertEqual(cloud.await_count, 4)  # Startup and shutdown each make two attempts.

    async def test_repeated_start_cancellation_holds_lock_until_cleanup_finishes(self):
        manager = ClientSessions(self.settings, self.path, PUBLIC_URL)
        data = await manager.create()
        session = manager.get(data["session_id"])
        start_began, cleanup_began, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        actions = []

        async def invoke(client, settings, action, body):
            actions.append(action)
            if action == "StartVoiceChat":
                start_began.set()
                await asyncio.Event().wait()
            cleanup_began.set()
            await release_cleanup.wait()
            return {"ok": True}

        start_task = stop_task = None
        with patch.object(manager, "_public_health", AsyncMock(return_value=True)), patch("backend_harness_loop.client.call_openapi", side_effect=invoke):
            try:
                async with asyncio.timeout(5):
                    start_task = asyncio.create_task(manager.start(session))
                    await start_began.wait()
                    start_task.cancel()
                    await cleanup_began.wait()
                    start_task.cancel()
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    self.assertTrue(session.lock.locked())
                    self.assertFalse(start_task.done())
                    stop_task = asyncio.create_task(manager.stop(session))
                    await asyncio.sleep(0)
                    self.assertFalse(stop_task.done())
                    self.assertEqual(actions, ["StartVoiceChat", "StopVoiceChat"])
                    release_cleanup.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await start_task
                    self.assertTrue((await stop_task)["stopped"])
            finally:
                release_cleanup.set()
                tasks = [task for task in (start_task, stop_task) if task is not None]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(session.state, "stopped")
        self.assertFalse(session.lock.locked())
        self.assertEqual(actions, ["StartVoiceChat", "StopVoiceChat", "StopVoiceChat"])

    async def test_lifespan_shutdown_stops_a_running_browser_task(self):
        app = create_client_app(self.settings, self.path, PUBLIC_URL)
        manager = app.state.client_sessions
        cloud = AsyncMock(return_value={"ok": True})
        with patch.object(manager, "_public_health", AsyncMock(return_value=True)), patch("backend_harness_loop.client.call_openapi", cloud):
            async with app.router.lifespan_context(app):
                data = await manager.create()
                session = manager.get(data["session_id"])
                self.assertTrue((await manager.start(session))["running"])
                self.assertEqual(session.state, "running")
            self.assertEqual(session.state, "stopped")
        self.assertEqual([call.args[2] for call in cloud.await_args_list], ["StartVoiceChat", "StopVoiceChat", "StopVoiceChat"])
        self.assertTrue(all(call.args[3]["RoomId"] == data["room_id"] for call in cloud.await_args_list))
        self.assertEqual(json.loads(manager.ledger_path.read_text("utf-8"))["sessions"][0]["state"], "stopped")

    async def test_wrong_app_ledger_is_preserved_and_never_targets_cloud(self):
        _, ledger_path = await self.crash_ledger()
        original = ledger_path.read_bytes()
        other_app = replace(self.settings, rtc_app_id="987654321098765432109876")
        with patch("backend_harness_loop.client.call_openapi", new_callable=AsyncMock) as cloud:
            with self.assertRaisesRegex(ValueError, "recovery ledger"):
                create_client_app(other_app, self.path, PUBLIC_URL)
            cloud.assert_not_awaited()
        self.assertEqual(ledger_path.read_bytes(), original)

    async def test_tampered_room_task_or_session_identity_is_rejected_without_overwrite(self):
        _, ledger_path = await self.crash_ledger()
        valid = json.loads(ledger_path.read_text("utf-8"))
        for field, value in (("room_id", "existing_product_room"), ("task_id", "m0_task_" + "a" * 32), ("session_id", "product_session")):
            with self.subTest(field=field):
                corrupted = json.loads(json.dumps(valid))
                corrupted["sessions"][0][field] = value
                serialized = json.dumps(corrupted)
                ledger_path.write_text(serialized, encoding="utf-8")
                with patch("backend_harness_loop.client.call_openapi", new_callable=AsyncMock) as cloud:
                    with self.assertRaisesRegex(ValueError, "recovery ledger"):
                        create_client_app(self.settings, self.path, PUBLIC_URL)
                    cloud.assert_not_awaited()
                self.assertEqual(ledger_path.read_text("utf-8"), serialized)

    async def test_duplicate_or_inconsistent_recovery_entries_are_rejected(self):
        _, ledger_path = await self.crash_ledger()
        valid = json.loads(ledger_path.read_text("utf-8"))
        invalid_entries = [
            [dict(valid["sessions"][0]), dict(valid["sessions"][0])],
            [{**valid["sessions"][0], "start_attempted": False}],
            [{**valid["sessions"][0], "expires_at": True}],
            [{**valid["sessions"][0], "expires_at": "1700000600"}],
            [{**valid["sessions"][0], "expires_at": -1}],
        ]
        for index, entries in enumerate(invalid_entries):
            with self.subTest(case=index):
                serialized = json.dumps({**valid, "sessions": entries})
                ledger_path.write_text(serialized, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "recovery ledger"):
                    create_client_app(self.settings, self.path, PUBLIC_URL)
                self.assertEqual(ledger_path.read_text("utf-8"), serialized)


if __name__ == "__main__":
    unittest.main()
