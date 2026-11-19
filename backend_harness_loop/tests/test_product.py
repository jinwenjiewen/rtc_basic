"""M4 security/transport smoke: isolated auth DB, real loop, scripted model.

These tests do not create real accounts or invoke RTC/model/knowledge services.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from backend_harness_loop.config import Settings
from backend_harness_loop.models import ModelProfiles
from backend_harness_loop.product import ProductRuntime, create_product_app
from backend_harness_loop.product_auth import AuthService, AuthSettings, LoginSession, User, utcnow
from backend_harness_loop.rtc_token import parse_rtc_token
from backend_harness_loop.store import RuntimeStore
from backend_harness_loop.tests.test_text import TextModels, events


class ProductTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        model_path = self.root / "models.json"
        model_path.write_text(json.dumps({"schema_version": 1, "default_profile": "flash",
            "providers": {"fake": {"base_url": "https://fixture.example/v1", "api_key_env": "FIXTURE_KEY"}},
            "profiles": {"flash": {"provider": "fake", "model": "flash"}, "pro": {"provider": "fake", "model": "pro"}}}))
        self.profiles = ModelProfiles.load(model_path, environ={"FIXTURE_KEY": "PRIVATE MODEL KEY"})
        self.models = TextModels()
        self.store = RuntimeStore(self.root / "runtime.sqlite3")
        self.runtime = ProductRuntime(self.store, self.profiles, model_factory=self.models, first_output_ms=9000,
            background_context_enabled=False)
        self.auth = AuthService(AuthSettings(f"sqlite+pysqlite:///{self.root / 'auth.sqlite3'}", "fixture jwt secret with over thirty two characters",
            allowed_origins=("http://testserver",)), initialize_test_database=True)
        with self.auth.database() as db:
            password_hash = self.auth.hasher.hash("fixture-password-1234")
            db.add_all([User(username="alice", password_hash=password_hash), User(username="bob", password_hash=password_hash)])
            db.commit()
        self.settings = Settings(rtc_app_id="012345678901234567890123", rtc_app_key="fixture-app-key",
            callback_secret="fixture-callback", rtc_legacy_voice_config={"fixture": True},
            volc_access_key="fixture-ak", volc_secret_key="fixture-sk")
        self.app = create_product_app(self.settings, self.profiles, self.root / "unused.sqlite3", self.root / "capture.jsonl",
            auth=self.auth, public_url="https://fixture.example", runtime=self.runtime, build_dir=self.root / "build")
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = self.new_client()
        self.clients = [self.client]
        await self.login(self.client)

    def new_client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")

    async def login(self, client, username="alice"):
        response = await client.post("/api/auth/login", json={"username": username, "password": "fixture-password-1234", "remember": True},
            headers={"Origin": "http://testserver"})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers.update({"X-CSRF-Token": client.cookies["rtc_csrf"], "Origin": "http://testserver"})
        return response

    async def create(self, transport="text", profile="flash", client=None):
        response = await (client or self.client).post(f"/api/agent/{transport}/sessions", json={"profile_id": profile})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def action(self, session, action, body=None, client=None, transport="text"):
        return await (client or self.client).post(f'/api/agent/{transport}/sessions/{session["session_id"]}/{action}', json=body or {})

    async def asyncTearDown(self):
        for model in self.models.instances:
            model.release.set()
        await self.lifespan.__aexit__(None, None, None)
        for client in self.clients:
            await client.aclose()
        self.store.close()
        self.folder.cleanup()

    async def test_cookie_login_refresh_logout_and_revocation(self):
        before = self.client.cookies["rtc_refresh"]
        self.assertEqual((await self.client.get("/api/auth/me")).json()["user"]["username"], "alice")
        response = await self.client.post("/api/auth/refresh")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(before, self.client.cookies["rtc_refresh"])
        cookies = response.headers.get_list("set-cookie")
        self.assertTrue(all("SameSite=lax" in value for value in cookies))
        self.assertTrue(any("rtc_access=" in value and "HttpOnly" in value for value in cookies))
        self.assertTrue(any("rtc_refresh=" in value and "Path=/api/auth" in value for value in cookies))
        self.assertEqual((await self.client.post("/api/auth/logout")).status_code, 204)
        self.assertEqual((await self.client.get("/api/auth/me")).status_code, 401)
        self.assertNotIn("rtc_access", self.client.cookies)

    async def test_cross_user_and_same_user_other_login_cannot_access_sessions(self):
        session = await self.create()
        for username in ("bob", "alice"):
            other = self.new_client()
            self.clients.append(other)
            await self.login(other, username)
            for action, body in [("turns", {"round_id": "x", "text": "foreign"}), ("interrupt", {"round_id": "x"}), ("stop", {}), ("heartbeat", {})]:
                self.assertEqual((await self.action(session, action, body, other)).status_code, 404)
            self.assertEqual((await other.get(f'/api/agent/text/sessions/{session["session_id"]}')).status_code, 404)
        owner = self.store.product_owner(session["session_id"])
        self.assertEqual(owner["user_id"], (await self.client.get("/api/auth/me")).json()["user"]["id"])
        self.assertEqual(self.models.instances, [])

    async def test_csrf_origin_and_unauthenticated_admission_denied(self):
        for headers in ({"X-CSRF-Token": ""}, {"Origin": "https://evil.example"}):
            response = await self.client.post("/api/agent/text/sessions", json={}, headers=headers)
            self.assertEqual(response.status_code, 403)
        anonymous = self.new_client()
        self.clients.append(anonymous)
        self.assertEqual((await anonymous.get("/api/agent/models")).status_code, 401)
        self.assertEqual((await anonymous.post("/api/agent/text/sessions", json={})).status_code, 401)
        self.assertEqual(self.app.state.text_sessions.sessions, {})

    async def test_model_pinned_final_only_and_browser_cannot_override_owner_or_provider(self):
        for field in ("owner_id", "user_id", "base_url", "api_key", "system"):
            response = await self.client.post("/api/agent/text/sessions", json={field: "PRIVATE INJECTION"})
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("PRIVATE INJECTION", response.text)
        session = await self.create(profile="pro")
        response = await self.action(session, "turns", {"round_id": "one", "text": "介绍课程"})
        self.assertEqual(response.status_code, 200, response.text)
        frames = events(response)
        self.assertEqual("".join(p["text"] for event, p in frames if event == "delta"), "真正的文字回复。")
        self.assertEqual(frames[-1][1]["state"], "generation_completed")
        self.assertNotIn("PRIVATE", response.text)
        self.assertEqual([model.profile.profile_id for model in self.models.instances], ["pro"])
        response = await self.action(session, "turns", {"round_id": "two", "text": "继续", "profile_id": "flash"})
        self.assertEqual(response.status_code, 400)
        inventory = (await self.client.get("/api/agent/models")).text
        self.assertNotIn("api_key", inventory)
        self.assertNotIn("base_url", inventory)

    async def test_logout_cancels_inflight_model_and_stops_own_leases_only(self):
        other = self.new_client()
        self.clients.append(other)
        await self.login(other, "bob")
        unaffected = await self.create(client=other)
        self.models.plans.append("blocked")
        session = await self.create()
        pending = asyncio.create_task(self.action(session, "turns", {"round_id": "blocked", "text": "hold"}))
        model = await asyncio.wait_for(self.models.started.get(), 2)
        response = await self.client.post("/api/auth/logout")
        self.assertEqual(response.status_code, 204)
        await asyncio.wait_for(model.cancelled.wait(), 2)
        result = await asyncio.wait_for(pending, 2)
        self.assertNotIn("event: delta", result.text)
        self.assertTrue(self.runtime.sessions[session["session_id"]].closed)
        self.assertFalse(self.runtime.sessions[unaffected["session_id"]].closed)

    async def test_rotated_refresh_reuse_revokes_bound_sessions(self):
        session = await self.create()
        old_refresh = self.client.cookies["rtc_refresh"]
        self.assertEqual((await self.client.post("/api/auth/refresh")).status_code, 200)
        self.client.cookies.clear()
        self.client.cookies.set("rtc_refresh", old_refresh, path="/api/auth")
        self.assertEqual((await self.client.post("/api/auth/refresh")).status_code, 401)
        self.assertTrue(self.runtime.sessions[session["session_id"]].closed)

    async def test_rtc_room_user_tokens_isolated_and_callback_revoked_after_logout(self):
        first, second = await self.create("rtc"), await self.create("rtc")
        self.assertNotEqual(first["room_id"], second["room_id"])
        self.assertNotEqual(first["user_id"], second["user_id"])
        token = parse_rtc_token(first["token"])
        self.assertEqual((token.room_id, token.user_id), (first["room_id"], first["user_id"]))
        binding = self.runtime.sessions[first["session_id"]]
        path = f"/api/chat_callback?rtc_run_id={binding.run_id}"
        body = {"round_id": 1, "messages": [{"role": "user", "content": "hello"}]}
        self.assertEqual((await self.client.post(path, json=body)).status_code, 401)
        response = await self.client.post(path, headers={"x-rtc-callback-token": "fixture-callback"}, json=body)
        self.assertEqual(response.status_code, 200)
        self.assertIn("真正的", response.text)
        await self.client.post("/api/auth/logout")
        response = await self.client.post(path, headers={"x-rtc-callback-token": "fixture-callback"}, json=body)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(len(self.models.instances), 1)

    async def test_external_revocation_and_expiry_deny_callback(self):
        session = await self.create("rtc")
        binding = self.runtime.sessions[session["session_id"]]
        owner = self.store.product_owner(session["session_id"])
        import uuid
        with self.auth.database() as db:
            row = db.get(LoginSession, uuid.UUID(owner["login_id"]))
            row.expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
        response = await self.client.post(f"/api/chat_callback?rtc_run_id={binding.run_id}",
            headers={"x-rtc-callback-token": "fixture-callback"},
            json={"round_id": 0, "messages": [{"role": "user", "content": "expired"}]})
        self.assertEqual(response.status_code, 401)
        self.assertTrue(binding.closed)
        self.assertEqual(self.models.instances, [])

    async def test_developer_routes_and_proxy_unavailable_and_static_files_contained(self):
        build = self.root / "build"
        build.mkdir()
        (build / "index.html").write_text("<html>React fixture</html>")
        (build / "main.js").write_text("console.log('React fixture')")
        script = await self.client.get("/main.js")
        self.assertEqual(script.headers["content-type"], "application/javascript")
        self.assertEqual(script.headers["x-content-type-options"], "nosniff")
        for path in ("/", "/chat", "/login"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("React fixture", response.text)
        for path in ("/m0", "/m1/text", "/api/m1/models", "/api/m0/sessions", "/proxy", "/..%2Fauth.sqlite3"):
            self.assertEqual((await self.client.get(path)).status_code, 404)
        self.assertEqual((await self.client.post("/proxy", json={"Action": "StartVoiceChat"})).status_code, 405)
        for path in ("/login", "/api/auth/login", "/api/agent/models"):
            self.assertEqual((await self.client.get(path, headers={"Host": "callback-tunnel.example"})).status_code, 404)
        self.assertEqual((await self.client.get("/healthz", headers={"Host": "callback-tunnel.example"})).status_code, 200)

    async def test_invalid_login_does_not_echo_password_and_auth_throttle_is_persistent(self):
        response = await self.client.post("/api/auth/login", json={"username": "alice", "password": "PRIVATE" * 30})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("PRIVATE", response.text)
        for _ in range(9):
            response = await self.client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
            self.assertEqual(response.status_code, 401)
        self.assertEqual((await self.client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})).status_code, 429)

    async def test_static_validation_token_not_accepted_as_product_voice_signer(self):
        # Even if an old console token is present, a product without a real
        # AppKey stays text-only. It does not hand the shared identity to users.
        settings = replace(self.settings, rtc_app_key="001" + "x" * 100, rtc_static_token="PRIVATE STATIC TOKEN")
        app = create_product_app(settings, self.profiles, self.root / "unused2.sqlite3", self.root / "capture2.jsonl",
            auth=self.auth, public_url="https://fixture.example", runtime=self.runtime)
        self.assertIsNone(app.state.client_sessions)
