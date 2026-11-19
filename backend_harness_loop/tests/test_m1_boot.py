"""In-process M1 boot/doctor checks; never open a listening socket or cloud task."""

from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend_harness_loop.__main__ import main
from backend_harness_loop.app import create_agent_app
from backend_harness_loop.config import Settings
from backend_harness_loop.models import ModelProfiles


FLASH_KEY = "boot-fixture-flash-credential-value"
PRO_KEY = "boot-fixture-pro-credential-value"


def fixture_files(folder: Path, *, credentials=True):
    config = folder / "model-profiles.json"
    key_file = folder / "model-credentials.env"
    config.write_text(json.dumps({
        "schema_version": 1, "default_profile": "deepseek_v4_flash",
        "providers": {
            "flash_gateway": {"base_url": "https://flash.example/v1", "api_key_env": "M1_BOOT_FLASH_KEY"},
            "pro_gateway": {"base_url": "https://pro.example/v1", "api_key_env": "M1_BOOT_PRO_KEY"},
        },
        "profiles": {
            "deepseek_v4_flash": {"provider": "flash_gateway", "model": "deepseek-v4-flash"},
            "deepseek_v4_pro": {"provider": "pro_gateway", "model": "deepseek-v4-pro"},
        },
    }), encoding="utf-8")
    key_file.write_text(
        f"M1_BOOT_FLASH_KEY={FLASH_KEY if credentials else ''}\nM1_BOOT_PRO_KEY={PRO_KEY if credentials else ''}\n",
        encoding="utf-8",
    )
    return config, key_file


class M1OwnedStoreBootTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_store_lifespan_serves_local_configuration_then_closes_session_and_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _ = fixture_files(root)
            profiles = ModelProfiles.load(config, environ={"M1_BOOT_FLASH_KEY": FLASH_KEY, "M1_BOOT_PRO_KEY": PRO_KEY})
            database = root / "runtime.sqlite3"
            settings = Settings(callback_secret="boot-fixture-callback-secret")
            with (
                patch("backend_harness_loop.models.ChatCompletionsModel", side_effect=AssertionError("No model request during boot")) as model,
                patch("backend_harness_loop.client.call_openapi", new=AsyncMock(side_effect=AssertionError("No RTC request during boot"))) as rtc,
            ):
                app = create_agent_app(settings, profiles, database, root / "capture.jsonl", "https://callback.example.com")
                runtime = app.state.agent_runtime
                async with app.router.lifespan_context(app):
                    runtime.register_session("boot-session", "boot-run", "deepseek_v4_flash", "boot-task", "boot-user")
                    self.assertEqual(runtime.store.get_session("boot-session")["state"], "active")
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3101") as client:
                        health = await client.get("/healthz")
                        self.assertEqual(health.status_code, 200)
                        self.assertEqual(health.json()["mode"], "agent_loop")
                        page = await client.get("/m0")
                        self.assertEqual(page.status_code, 200)
                        self.assertNotIn("__M0_CLIENT_KEY__", page.text)
                        match = re.search(r'name="m0-client-key" content="([A-Za-z0-9_-]{30,})"', page.text)
                        self.assertIsNotNone(match)
                        denied = await client.get("/api/m1/models")
                        self.assertEqual(denied.status_code, 401)
                        inventory = await client.get("/api/m1/models", headers={"X-M0-Client-Key": match[1]})
                        self.assertEqual(inventory.status_code, 200)
                        self.assertEqual(inventory.json()["mode"], "agent_loop")
                        self.assertEqual({p["id"] for p in inventory.json()["profiles"]}, {"deepseek_v4_flash", "deepseek_v4_pro"})
                        for secret in (FLASH_KEY, PRO_KEY, settings.callback_secret):
                            self.assertNotIn(secret, inventory.text)
                            self.assertNotIn(secret, page.text)
                self.assertTrue(runtime.sessions["boot-session"].closed)
                self.assertEqual(runtime.tasks, set())
                with self.assertRaises(sqlite3.ProgrammingError):
                    runtime.store.db.execute("SELECT 1")
                with closing(sqlite3.connect(database)) as saved:
                    self.assertEqual(saved.execute("SELECT state FROM sessions WHERE session_id=?", ("boot-session",)).fetchone()[0], "closed")
                model.assert_not_called()
                rtc.assert_not_awaited()

    async def test_separate_boots_issue_distinct_local_client_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _ = fixture_files(root)
            profiles = ModelProfiles.load(config, environ={"M1_BOOT_FLASH_KEY": FLASH_KEY, "M1_BOOT_PRO_KEY": PRO_KEY})
            keys = []
            for index in range(2):
                app = create_agent_app(Settings(callback_secret="boot-fixture-callback-secret"), profiles,
                                       root / f"runtime-{index}.sqlite3", root / f"capture-{index}.jsonl", "https://callback.example.com")
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3101") as client:
                        page = await client.get("/m0")
                        key = re.search(r'name="m0-client-key" content="([^"]+)"', page.text)[1]
                        keys.append(key)
                        if index:
                            old_key = await client.get("/api/m1/models", headers={"X-M0-Client-Key": keys[0]})
                            self.assertEqual(old_key.status_code, 401)
            self.assertNotEqual(*keys)


class M1DoctorBootTests(unittest.TestCase):
    def run_doctor(self, config: Path, key_file: Path):
        output = io.StringIO()
        args = ["backend_harness_loop", "m1-doctor", "--models-file", str(config), "--model-env-file", str(key_file)]
        with (
            patch.dict(os.environ, {}, clear=True), patch("sys.argv", args), redirect_stdout(output),
            patch("httpx.AsyncClient", side_effect=AssertionError("Doctor must not connect")),
        ):
            code = main()
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_doctor_resolves_separate_environment_keys_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            config, key_file = fixture_files(Path(directory))
            for secret in (FLASH_KEY, PRO_KEY):
                self.assertNotIn(secret, config.read_text(encoding="utf-8"))
            code, payload, printed = self.run_doctor(config, key_file)
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "configured")
            self.assertEqual(payload["runtime_mode"], "agent_loop")
            self.assertTrue(all(profile["configured"] for profile in payload["profiles"]))
            self.assertEqual({profile["api_key_env"] for profile in payload["profiles"]}, {"M1_BOOT_FLASH_KEY", "M1_BOOT_PRO_KEY"})
            for secret in (FLASH_KEY, PRO_KEY):
                self.assertNotIn(secret, printed)

    def test_doctor_missing_credentials_returns_blocked_exit_two_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            config, key_file = fixture_files(Path(directory), credentials=False)
            code, payload, printed = self.run_doctor(config, key_file)
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "blocked")
            self.assertTrue(all(not profile["configured"] for profile in payload["profiles"]))
            for secret in (FLASH_KEY, PRO_KEY):
                self.assertNotIn(secret, printed)


if __name__ == "__main__":
    unittest.main()
