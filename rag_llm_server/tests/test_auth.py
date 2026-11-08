"""Integration checks against real PostgreSQL in a disposable test schema.

Defaults to DATABASE_URL's database name + '_test'. AUTH_TEST_DATABASE_URL can
override it, but the database name must end in '_test'. No dev tables are used.
"""
import importlib
import io
import os
from pathlib import Path
import unittest
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
import jwt
import psycopg
from psycopg import sql
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from auth import ACCESS_COOKIE, CSRF_COOKIE, REFRESH_COOKIE, create_user, digest, utcnow
from config import settings
from database import get_db
from models import LoginSession, LoginThrottle, User


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not settings.DATABASE_URL:
            raise unittest.SkipTest("配置 PostgreSQL DATABASE_URL 后运行账号集成测试。")
        configured = make_url(settings.DATABASE_URL)
        cls.url = make_url(os.getenv("AUTH_TEST_DATABASE_URL", "")) if os.getenv("AUTH_TEST_DATABASE_URL") else configured.set(database=f"{configured.database}_test")
        if not cls.url.database.endswith("_test"):
            raise RuntimeError("AUTH_TEST_DATABASE_URL 的数据库名必须以 _test 结尾。")
        with psycopg.connect(host=cls.url.host, port=cls.url.port or 5432,
            user=cls.url.username, password=cls.url.password, dbname="postgres", autocommit=True) as connection:
            if not connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (cls.url.database,)).fetchone():
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.url.database)))
        cls.schema = f"auth_test_{uuid.uuid4().hex}"
        cls.engine = create_engine(cls.url, connect_args={"options": f"-csearch_path={cls.schema}"})
        with cls.engine.begin() as connection:
            connection.execute(CreateSchema(cls.schema))
        migration_config = AlembicConfig(str(Path(__file__).parents[1] / "alembic.ini"))
        with cls.engine.connect() as connection:
            migration_config.attributes["connection"] = connection
            command.upgrade(migration_config, "head")
        with patch.multiple(settings, ARK_API_KEY="unit-test", ARK_BASE_URL="https://test.invalid/api/v3"):
            cls.main = importlib.import_module("main")

    @classmethod
    def tearDownClass(cls):
        if not cls.schema.startswith("auth_test_") or not cls.url.database.endswith("_test"):
            raise RuntimeError("拒绝清理非测试数据库。")
        with cls.engine.begin() as connection:
            connection.execute(DropSchema(cls.schema, cascade=True))
        cls.engine.dispose()

    def setUp(self):
        self.patch_settings = patch.multiple(settings,
            JWT_SECRET_KEY="test-only-signing-secret-with-at-least-32-bytes",
            RTC_CALLBACK_SECRET="test-only-callback-secret-with-at-least-32-bytes",
            AUTH_ALLOWED_ORIGINS=["http://testserver"], AUTH_COOKIE_SECURE=False,
            RTC_STATIC_TOKEN="", RTC_ROOM_ID="", RTC_USER_ID="", RTC_TASK_ID="ChatTask01",
            SERVER_URL="https://callback.example.com",
            RTC_APP_ID="test-app", RTC_APP_KEY="0" * 32)
        self.patch_settings.start()
        self.addCleanup(self.patch_settings.stop)
        with Session(self.engine) as db:
            for model in [LoginSession, LoginThrottle, User]:
                db.execute(delete(model))
            db.commit()
            self.user = create_user(db, "alice", "a-long-test-password")
            self.user_id = self.user.id

        def test_db():
            with Session(self.engine, expire_on_commit=False) as db:
                yield db

        self.original_overrides = self.main.app.dependency_overrides.copy()
        self.main.app.dependency_overrides[get_db] = test_db
        self.addCleanup(lambda: setattr(self.main.app, "dependency_overrides", self.original_overrides))
        self.client = TestClient(self.main.app, headers={"Origin": "http://testserver"})
        self.addCleanup(self.client.close)

    def login(self, remember=True, username="alice", password="a-long-test-password", client=None):
        client = client or self.client
        response = client.post("/api/auth/login", json={"username": username, "password": password, "remember": remember})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE]
        return response

    def test_migrations_and_guest_interception(self):
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT version_num FROM alembic_version")), "0001_auth")
        for path in ["/getScenes", "/proxy?Action=StartVoiceChat", "/debug/chat", "/debug/intention"]:
            self.assertEqual(self.client.post(path, json={"question": "hello"}).status_code, 401, path)
        self.assertEqual(self.client.get("/debug/rag?query=hello").status_code, 401)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_login_hashes_credentials_and_uses_httponly_cookies(self):
        response = self.login(username="ALICE")
        self.assertEqual(response.json()["user"]["username"], "alice")
        self.assertNotIn("token", response.json())
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        cookies = response.headers.get_list("set-cookie")
        for name in [ACCESS_COOKIE, REFRESH_COOKIE]:
            cookie = next(value for value in cookies if value.startswith(f"{name}="))
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=lax", cookie)
            self.assertIn("Max-Age=", cookie)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        with Session(self.engine) as db:
            self.assertTrue(db.get(User, self.user_id).password_hash.startswith("$argon2"))
            session = db.scalar(select(LoginSession))
            self.assertEqual(session.refresh_hash, digest(self.client.cookies[REFRESH_COOKIE].split(".", 1)[1]))

    def test_session_cookies_when_remember_is_unchecked(self):
        response = self.login(remember=False)
        self.assertTrue(all("Max-Age=" not in cookie for cookie in response.headers.get_list("set-cookie")))
        with Session(self.engine) as db:
            session = db.scalar(select(LoginSession))
            self.assertFalse(session.persistent)
            self.assertLess(session.expires_at, utcnow() + timedelta(hours=13))

    def test_invalid_credentials_have_same_generic_response(self):
        wrong = self.client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
        unknown = self.client.post("/api/auth/login", json={"username": "unknown", "password": "wrong"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.json(), unknown.json())

    def test_login_throttling_persists_in_database(self):
        for _ in range(10):
            response = self.client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
            self.assertEqual(response.status_code, 401)
        response = self.client.post("/api/auth/login", json={"username": "alice", "password": "a-long-test-password"})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers["Retry-After"]), 0)

    def test_csrf_and_untrusted_origins_are_blocked(self):
        self.login()
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual(self.client.post("/getScenes").status_code, 403)
        self.assertEqual(self.client.post("/api/auth/refresh").status_code, 403)
        self.client.headers["X-CSRF-Token"] = self.client.cookies[CSRF_COOKIE]
        self.client.headers["Origin"] = "https://untrusted.invalid"
        self.assertEqual(self.client.post("/getScenes").status_code, 403)
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 403)
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "alice", "password": "a-long-test-password"}).status_code, 403)

    def test_expired_access_can_refresh_and_refresh_rotates(self):
        self.login()
        old_refresh = self.client.cookies[REFRESH_COOKIE]
        claims = jwt.decode(self.client.cookies[ACCESS_COOKIE], options={"verify_signature": False})
        claims["exp"] = int((utcnow() - timedelta(seconds=10)).timestamp())
        expired = jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm="HS256")
        self.client.cookies.set(ACCESS_COOKIE, expired, domain="testserver.local", path="/")
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.client.cookies[REFRESH_COOKIE], old_refresh)
        self.assertEqual(self.client.post("/api/auth/refresh").status_code, 200)
        self.assertNotEqual(self.client.cookies[REFRESH_COOKIE], old_refresh)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

    def test_refresh_reuse_revokes_entire_session(self):
        self.login()
        original_refresh = self.client.cookies[REFRESH_COOKIE]
        self.assertEqual(self.client.post("/api/auth/refresh").status_code, 200)
        access = self.client.cookies[ACCESS_COOKIE]
        self.client.cookies.set(REFRESH_COOKIE, original_refresh, domain="testserver.local", path="/api/auth")
        self.assertEqual(self.client.post("/api/auth/refresh").status_code, 401)
        self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"}).status_code, 401)

    def test_logout_immediately_revokes_saved_access_and_refresh(self):
        self.login()
        access = self.client.cookies[ACCESS_COOKIE]
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 204)
        self.assertFalse(self.client.cookies.get(REFRESH_COOKIE))
        self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"}).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/refresh").status_code, 401)

    def test_forged_wrong_audience_and_missing_exp_are_rejected(self):
        self.login()
        token = self.client.cookies[ACCESS_COOKIE]
        self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token[:-5]}abcde"}).status_code, 401)
        for changes in [{"aud": "somewhere-else"}, {"type": "refresh"}, {"iss": "another-service"}]:
            claims = jwt.decode(token, options={"verify_signature": False})
            claims.update(changes)
            bad = jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm="HS256")
            self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {bad}"}).status_code, 401)
        claims.pop("exp")
        bad = jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm="HS256")
        self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {bad}"}).status_code, 401)

    def test_disabled_user_and_expired_session_are_rejected(self):
        self.login()
        with Session(self.engine) as db:
            db.get(User, self.user_id).is_active = False
            db.commit()
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/refresh").status_code, 401)
        with Session(self.engine) as db:
            db.get(User, self.user_id).is_active = True
            db.commit()
        self.login()
        with Session(self.engine) as db:
            for session in db.scalars(select(LoginSession)):
                session.expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_rtc_rooms_are_isolated_and_application_key_is_never_returned(self):
        self.login()
        first = self.client.post("/getScenes").json()["Result"]["scenes"][0]["rtc"]
        self.assertNotEqual(first["Token"], settings.RTC_APP_KEY)
        self.assertEqual(self.client.post("/getScenes").json()["Result"]["scenes"][0]["rtc"]["RoomId"], first["RoomId"])
        self.assertEqual(self.client.post("/proxy?Action=DeleteApp", json={}).status_code, 400)
        other = TestClient(self.main.app, headers={"Origin": "http://testserver"})
        self.addCleanup(other.close)
        self.login(client=other)
        second = other.post("/getScenes").json()["Result"]["scenes"][0]["rtc"]
        self.assertNotEqual(first["RoomId"], second["RoomId"])

    def test_callback_uses_separate_service_credential(self):
        self.assertEqual(self.client.post("/api/chat_callback", json={"messages": []}).status_code, 401)
        self.assertEqual(self.client.post("/api/chat_callback", params={"token": "无效凭证"}, json={"messages": []}).status_code, 401)
        response = self.client.post("/api/chat_callback", params={"token": settings.RTC_CALLBACK_SECRET}, json={"messages": []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "data: [DONE]\n\n")

    def test_proxy_ignores_client_room_and_includes_callback_credential(self):
        self.login()
        rtc = self.client.post("/getScenes").json()["Result"]["scenes"][0]["rtc"]
        upstream = AsyncMock()
        upstream.post.return_value = Mock(json=lambda: {"ResponseMetadata": {"Action": "StartVoiceChat"}, "Result": {}})
        context = AsyncMock()
        context.__aenter__.return_value = upstream
        with patch.object(self.main.httpx, "AsyncClient", return_value=context):
            response = self.client.post("/proxy?Action=StartVoiceChat", json={"RoomId": "someone-elses-room"})
        self.assertEqual(response.status_code, 200)
        payload = upstream.post.call_args.kwargs["json"]
        self.assertEqual(payload["RoomId"], rtc["RoomId"])
        self.assertEqual(payload["AgentConfig"]["TargetUserId"], [rtc["UserId"]])
        self.assertIn(settings.RTC_CALLBACK_SECRET, payload["Config"]["LLMConfig"]["Url"])

    def test_password_reset_revokes_existing_sessions(self):
        import manage
        self.login()
        saved_access = self.client.cookies[ACCESS_COOKIE]
        new_password = "a-new-long-password"
        with patch("sys.argv", ["manage.py", "set-password", "alice"]), \
             patch.object(manage, "get_engine", return_value=self.engine), \
             patch.object(manage.getpass, "getpass", return_value=new_password), \
             patch("sys.stdout", new_callable=io.StringIO):
            manage.main()
        self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {saved_access}"}).status_code, 401)
        self.login(password=new_password)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

    def test_static_token_identity_matches_voice_chat_request(self):
        from services.token_build import AccessToken, PRIVILEGES
        self.login()
        builder = AccessToken(settings.RTC_APP_ID, "fixture-key", "ChatRoom01", "Huoshan01")
        builder.add_privilege(PRIVILEGES["PrivPublishStream"] , 0)
        builder.expire_time(int((utcnow() + timedelta(hours=1)).timestamp()))
        token = builder.serialize()
        # The v001 token format includes an exactly 24-character AppId.
        with patch.multiple(settings, RTC_APP_ID="0123456789abcdef01234567"):
            builder.app_id = settings.RTC_APP_ID
            token = builder.serialize()
            with patch.object(settings, "RTC_APP_KEY", token):
                rtc = self.client.post("/getScenes").json()["Result"]["scenes"][0]["rtc"]
                self.assertEqual(rtc["Token"], token)
                upstream = AsyncMock()
                upstream.post.return_value = Mock(json=lambda: {"ResponseMetadata": {"Action": "StartVoiceChat"}, "Result": {}})
                context = AsyncMock()
                context.__aenter__.return_value = upstream
                with patch.object(self.main.httpx, "AsyncClient", return_value=context):
                    response = self.client.post("/proxy?Action=StartVoiceChat", json={})
                self.assertEqual(response.status_code, 200)
                payload = upstream.post.call_args.kwargs["json"]
                self.assertEqual(payload["RoomId"], rtc["RoomId"])
                self.assertEqual(payload["AgentConfig"]["TargetUserId"], [rtc["UserId"]])
                self.assertEqual(payload["TaskId"], "ChatTask01")
                self.assertEqual(payload["Config"]["LLMConfig"]["Mode"], "CustomLLM")

    def test_callback_misconfiguration_returns_clear_error_without_calling_rtc(self):
        self.login()
        with patch.object(settings, "SERVER_URL", ""), patch.object(self.main.httpx, "AsyncClient") as upstream:
            response = self.client.post("/proxy?Action=StartVoiceChat", json={})
        self.assertEqual(response.status_code, 503)
        self.assertIn("回调地址", response.json()["detail"])
        upstream.assert_not_called()
