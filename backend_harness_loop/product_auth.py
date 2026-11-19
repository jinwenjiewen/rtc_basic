"""Product Cookie authentication over the existing account schema.

No import of the legacy application's global settings, engine or RTC SDK.
PostgreSQL tables are reused without migrations or account/password changes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import uuid
from urllib.parse import urlsplit

from dotenv import dotenv_values
from fastapi import HTTPException, Request, Response
import jwt
from pwdlib import PasswordHash
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid, create_engine, delete, func, inspect, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def unauthorized():
    return HTTPException(401, "登录已过期，请重新登录。")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "auth_users"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoginSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("auth_users.id", ondelete="CASCADE"), index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64))
    csrf_hash: Mapped[str] = mapped_column(String(64))
    persistent: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoginThrottle(Base):
    __tablename__ = "auth_login_throttles"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


@dataclass(frozen=True)
class AuthSettings:
    database_url: str = field(repr=False)
    jwt_secret: str = field(repr=False)
    allowed_origins: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")
    cookie_secure: bool = False
    access_minutes: int = 15
    refresh_days: int = 7
    issuer: str = "rtc-basic"
    audience: str = "rtc-basic-web"

    def __post_init__(self):
        if len(self.jwt_secret.encode()) < 32 or not self.database_url.startswith(("postgresql+psycopg://", "sqlite+pysqlite:///")):
            raise ValueError("Invalid product authentication configuration")
        if not 1 <= self.access_minutes <= 60 or not 1 <= self.refresh_days <= 30:
            raise ValueError("Invalid authentication lifetime")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path
                    or parsed.query or parsed.fragment or parsed.username or parsed.password or "*" in origin):
                raise ValueError("An explicit allowed origin is required")
        if not self.allowed_origins:
            raise ValueError("Allowed origins are required")

    @classmethod
    def load(cls, path: Path):
        values = {**dotenv_values(path), **os.environ}
        return cls(values.get("DATABASE_URL", ""), values.get("JWT_SECRET_KEY", ""),
            tuple(item.strip().rstrip("/") for item in values.get("AUTH_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if item.strip()),
            values.get("AUTH_COOKIE_SECURE", "false").lower() in {"true", "1", "yes"},
            int(values.get("ACCESS_TOKEN_MINUTES", "15")), int(values.get("REFRESH_TOKEN_DAYS", "7")))


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    login_id: str
    username: str
    expires_at: float

    def public(self):
        return {"user": {"id": self.user_id, "username": self.username}}


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)
    remember: StrictBool = False


class AuthRevoked(HTTPException):
    def __init__(self, login_id: str):
        super().__init__(401, "登录已过期，请重新登录。")
        self.login_id = login_id


class AuthService:
    def __init__(self, settings: AuthSettings, *, initialize_test_database: bool = False):
        self.settings = settings
        options = ({"connect_args": {"check_same_thread": False, "timeout": 3}}
                   if settings.database_url.startswith("sqlite") else
                   {"connect_args": {"connect_timeout": 3, "options": "-c statement_timeout=3000 -c lock_timeout=2000"}, "pool_timeout": 3})
        self.engine = create_engine(settings.database_url, pool_pre_ping=True, **options)
        self.database = sessionmaker(self.engine, expire_on_commit=False)
        self.hasher = PasswordHash.recommended()
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))
        # SQLite is an explicit, isolated test facility, never an account fallback.
        if initialize_test_database:
            if self.engine.dialect.name != "sqlite":
                raise ValueError("Test schema initialization is SQLite only")
            Base.metadata.create_all(self.engine)

    def check_schema(self):
        inspector = inspect(self.engine)
        for table in Base.metadata.sorted_tables:
            columns = {item["name"] for item in inspector.get_columns(table.name)}
            if not set(table.columns.keys()) <= columns:
                raise ValueError("Existing auth schema is incomplete; run the account service migrations separately")

    async def call(self, function, *args):
        try:
            return await asyncio.to_thread(function, *args)
        except SQLAlchemyError:
            raise HTTPException(503, "登录服务暂不可用，请稍后重试。") from None

    def origin(self, request: Request):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in self.settings.allowed_origins:
            raise HTTPException(403, "请求来源不受信任。")

    def csrf(self, request: Request, session: LoginSession):
        self.origin(request)
        supplied = request.headers.get("x-csrf-token", "")
        if not supplied or not hmac.compare_digest(digest(supplied), session.csrf_hash):
            raise HTTPException(403, "请求校验失败，请刷新页面后重试。")

    @staticmethod
    def context(user: User, session: LoginSession):
        return AuthContext(str(user.id), str(session.id), user.username, aware(session.expires_at).timestamp())

    @staticmethod
    def valid(session):
        return session is not None and session.revoked_at is None and aware(session.expires_at) > utcnow()

    def _authenticate(self, request):
        # Browser endpoints deliberately use the existing HttpOnly Cookie contract.
        token = request.cookies.get("rtc_access")
        try:
            claims = jwt.decode(token, self.settings.jwt_secret, algorithms=["HS256"],
                issuer=self.settings.issuer, audience=self.settings.audience,
                options={"require": ["sub", "sid", "exp", "iat", "iss", "aud", "jti", "type"]})
            if claims["type"] != "access":
                raise ValueError()
            user_id, login_id = uuid.UUID(claims["sub"]), uuid.UUID(claims["sid"])
        except (jwt.InvalidTokenError, ValueError, TypeError, AttributeError):
            raise unauthorized() from None
        with self.database() as db:
            session, user = db.get(LoginSession, login_id), db.get(User, user_id)
            if not self.valid(session) or session.user_id != user_id or not user or not user.is_active:
                raise unauthorized()
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                self.csrf(request, session)
            return self.context(user, session)

    async def authorize(self, request):
        return await self.call(self._authenticate, request)

    def active(self, login_id, user_id):
        with self.database() as db:
            session, user = db.get(LoginSession, uuid.UUID(login_id)), db.get(User, uuid.UUID(user_id))
            return bool(self.valid(session) and str(session.user_id) == user_id and user and user.is_active)

    def _throttle(self, db, username, ip):
        now = utcnow()
        db.execute(delete(LoginThrottle).where(LoginThrottle.window_start < now - timedelta(days=1)))
        insert = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
        counters = []
        for key, maximum in sorted([(digest("account:" + username), 10), (digest("ip:" + ip), 40)]):
            db.execute(insert(LoginThrottle).values(key=key, attempts=0, window_start=now).on_conflict_do_nothing(index_elements=[LoginThrottle.key]))
            counter = db.scalar(select(LoginThrottle).where(LoginThrottle.key == key).with_for_update())
            if aware(counter.window_start) <= now - timedelta(minutes=15):
                counter.window_start, counter.attempts = now, 0
            if counter.attempts >= maximum:
                db.commit()
                raise HTTPException(429, "尝试次数过多，请稍后再试。", headers={"Retry-After": "900"})
            counters.append(counter)
        for counter in counters:
            counter.attempts += 1
        db.commit()

    def login(self, body, request, response):
        self.origin(request)
        username = body.username.strip().lower()
        with self.database() as db:
            self._throttle(db, username, request.client.host if request.client else "unknown")
            user = db.scalar(select(User).where(User.username == username))
            valid = self.hasher.verify(body.password, user.password_hash if user else self.dummy_hash)
            if not user or not valid or not user.is_active:
                raise HTTPException(401, "账号或密码错误。")
            refresh, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
            session = LoginSession(user_id=user.id, refresh_hash=digest(refresh), csrf_hash=digest(csrf),
                persistent=body.remember, expires_at=utcnow() + (timedelta(days=self.settings.refresh_days) if body.remember else timedelta(hours=12)))
            db.add(session)
            db.commit()
            self.cookies(response, user, session, refresh, csrf)
            return self.context(user, session)

    def rotate(self, request, response, logout=False):
        self.origin(request)
        try:
            login_id, refresh = request.cookies.get("rtc_refresh", "").split(".", 1)
            login_id = uuid.UUID(login_id)
            if not refresh:
                raise ValueError()
        except (ValueError, AttributeError):
            raise unauthorized() from None
        with self.database() as db:
            session = db.scalar(select(LoginSession).where(LoginSession.id == login_id).with_for_update())
            if not self.valid(session):
                raise unauthorized()
            self.csrf(request, session)
            if not hmac.compare_digest(digest(refresh), session.refresh_hash):
                session.revoked_at = utcnow()
                db.commit()
                raise AuthRevoked(str(login_id))
            user = db.get(User, session.user_id)
            if not user or not user.is_active:
                session.revoked_at = utcnow()
                db.commit()
                raise AuthRevoked(str(login_id))
            if logout:
                session.revoked_at = utcnow()
                db.commit()
            else:
                refresh = secrets.token_urlsafe(48)
                session.refresh_hash = digest(refresh)
                db.commit()
                self.cookies(response, user, session, refresh, request.headers["x-csrf-token"])
            return self.context(user, session)

    def cookies(self, response, user, session, refresh, csrf):
        now = utcnow()
        seconds = self.settings.access_minutes * 60
        token = jwt.encode({"sub": str(user.id), "sid": str(session.id), "type": "access",
            "iss": self.settings.issuer, "aud": self.settings.audience, "iat": now,
            "exp": now + timedelta(seconds=seconds), "jti": str(uuid.uuid4())}, self.settings.jwt_secret, algorithm="HS256")
        remaining = max(0, int((aware(session.expires_at) - now).total_seconds()))
        common = {"secure": self.settings.cookie_secure, "samesite": "lax"}
        for name, value, path, http_only, lifetime in [
            ("rtc_access", token, "/", True, seconds),
            ("rtc_refresh", f"{session.id}.{refresh}", "/api/auth", True, remaining),
            ("rtc_csrf", csrf, "/", False, remaining),
        ]:
            response.set_cookie(name, value, path=path, httponly=http_only,
                max_age=lifetime if session.persistent else None, **common)

    def clear(self, response):
        for name, path in [("rtc_access", "/"), ("rtc_csrf", "/"), ("rtc_refresh", "/api/auth")]:
            response.delete_cookie(name, path=path, httponly=name != "rtc_csrf", secure=self.settings.cookie_secure, samesite="lax")


def install_auth_routes(app, auth: AuthService, revoke):
    @app.post("/api/auth/login")
    async def login(request: Request, response: Response):
        from .text import _body
        try:
            body = LoginRequest.model_validate(await _body(request))
        except ValidationError:
            raise HTTPException(400, "请检查账号和密码格式。") from None
        return (await auth.call(auth.login, body, request, response)).public()

    @app.get("/api/auth/me")
    async def me(request: Request):
        return (await auth.authorize(request)).public()

    @app.post("/api/auth/refresh")
    async def refresh(request: Request, response: Response):
        return (await auth.call(auth.rotate, request, response)).public()

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request, response: Response):
        context = await auth.call(auth.rotate, request, response, True)
        await revoke(context.login_id)
        auth.clear(response)

    @app.exception_handler(HTTPException)
    async def auth_error(request, error):
        from fastapi.responses import JSONResponse
        if isinstance(error, AuthRevoked):
            await revoke(error.login_id)
        response = JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=error.headers)
        if error.status_code == 401 and request.url.path in {"/api/auth/logout", "/api/auth/refresh"}:
            auth.clear(response)
        return response
