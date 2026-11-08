"""Password authentication, short-lived JWTs and rotating refresh sessions."""
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from pwdlib import PasswordHash
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import LoginSession, LoginThrottle, User

router = APIRouter(prefix="/api/auth", tags=["auth"])
password_hasher = PasswordHash.recommended()
dummy_hash = password_hasher.hash(secrets.token_urlsafe(32))
ACCESS_COOKIE = "rtc_access"
REFRESH_COOKIE = "rtc_refresh"
CSRF_COOKIE = "rtc_csrf"
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")


def utcnow():
    return datetime.now(timezone.utc)


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def create_user(db: Session, username: str, password: str):
    username = username.strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("账号需为 3–32 位字母、数字、下划线、短横线或点。")
    if not 12 <= len(password) <= 128:
        raise ValueError("新账号密码需为 12–128 位字符。")
    if db.scalar(select(User).where(User.username == username)):
        raise ValueError("账号已存在。")
    user = User(username=username, password_hash=password_hasher.hash(password))
    db.add(user)
    db.commit()
    return user


def require_origin(request: Request):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in settings.AUTH_ALLOWED_ORIGINS:
        raise HTTPException(403, "请求来源不受信任。")


def require_csrf(request: Request, session: LoginSession):
    require_origin(request)
    token = request.headers.get("x-csrf-token", "")
    if not token or not hmac.compare_digest(digest(token), session.csrf_hash):
        raise HTTPException(403, "请求校验失败，请刷新页面后重试。")


def clear_cookies(response: Response):
    for name, path in [(ACCESS_COOKIE, "/"), (CSRF_COOKIE, "/"), (REFRESH_COOKIE, "/api/auth")]:
        response.delete_cookie(name, path=path, secure=settings.AUTH_COOKIE_SECURE,
                               httponly=name != CSRF_COOKIE, samesite="lax")
    response.headers["Cache-Control"] = "no-store"


def unauthorized():
    return HTTPException(401, "登录已过期，请重新登录。", headers={"Cache-Control": "no-store"})


def public_user(user: User):
    return {"id": str(user.id), "username": user.username}


def issue_cookies(response: Response, user: User, session: LoginSession,
                  refresh: str, csrf: str):
    now = utcnow()
    access_seconds = settings.ACCESS_TOKEN_MINUTES * 60
    token = jwt.encode({
        "sub": str(user.id), "sid": str(session.id), "type": "access",
        "iss": settings.JWT_ISSUER, "aud": settings.JWT_AUDIENCE,
        "iat": now, "exp": now + timedelta(seconds=access_seconds),
        "jti": str(uuid.uuid4()),
    }, settings.JWT_SECRET_KEY, algorithm="HS256")
    remaining = max(0, int((session.expires_at - now).total_seconds()))
    common = {"secure": settings.AUTH_COOKIE_SECURE, "samesite": "lax"}
    response.set_cookie(ACCESS_COOKIE, token, httponly=True, path="/",
                        max_age=access_seconds if session.persistent else None, **common)
    response.set_cookie(REFRESH_COOKIE, f"{session.id}.{refresh}", httponly=True,
                        path="/api/auth", max_age=remaining if session.persistent else None, **common)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, path="/",
                        max_age=remaining if session.persistent else None, **common)
    response.headers["Cache-Control"] = "no-store"


@dataclass
class AuthContext:
    user: User
    session: LoginSession


def get_current_auth(request: Request, db: Session = Depends(get_db)):
    authorization = request.headers.get("authorization", "")
    bearer = authorization.lower().startswith("bearer ")
    token = authorization[7:] if bearer else request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise unauthorized()
    try:
        claims = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=["HS256"],
            issuer=settings.JWT_ISSUER, audience=settings.JWT_AUDIENCE,
            options={"require": ["sub", "sid", "exp", "iat", "iss", "aud", "jti", "type"]})
        if claims["type"] != "access":
            raise ValueError("Invalid token type")
        user_id, session_id = uuid.UUID(claims["sub"]), uuid.UUID(claims["sid"])
    except (jwt.InvalidTokenError, ValueError, TypeError, AttributeError):
        raise unauthorized()
    session = db.get(LoginSession, session_id)
    if not session or session.user_id != user_id or session.revoked_at or session.expires_at <= utcnow():
        raise unauthorized()
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise unauthorized()
    if not bearer and request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_csrf(request, session)
    return AuthContext(user=user, session=session)


def require_rtc_callback(request: Request):
    # RTC is a server-to-server caller and cannot use the browser's login cookies.
    token = request.headers.get("x-rtc-callback-token") or request.query_params.get("token", "")
    if not settings.RTC_CALLBACK_SECRET or not hmac.compare_digest(token.encode(), settings.RTC_CALLBACK_SECRET.encode()):
        raise HTTPException(401, "无效的回调凭证。")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)
    remember: bool = False


def check_login_throttles(db: Session, username: str, ip: str):
    now = utcnow()
    # Stored in PostgreSQL, so limits also hold across worker processes/restarts.
    db.execute(delete(LoginThrottle).where(LoginThrottle.window_start < now - timedelta(days=1)))
    keys = [(digest(f"account:{username}"), 10), (digest(f"ip:{ip}"), 40)]
    counters = []
    for key, maximum in sorted(keys):
        db.execute(insert(LoginThrottle).values(key=key, attempts=0, window_start=now)
                   .on_conflict_do_nothing(index_elements=[LoginThrottle.key]))
        counter = db.scalar(select(LoginThrottle).where(LoginThrottle.key == key).with_for_update())
        if counter.window_start <= now - timedelta(minutes=15):
            counter.window_start, counter.attempts = now, 0
        if counter.attempts >= maximum:
            retry = max(1, int((counter.window_start + timedelta(minutes=15) - now).total_seconds()))
            db.commit()
            raise HTTPException(429, "尝试次数过多，请稍后再试。", headers={"Retry-After": str(retry)})
        counters.append(counter)
    for counter in counters:
        counter.attempts += 1
    db.commit()


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    require_origin(request)
    username = body.username.strip().lower()
    check_login_throttles(db, username, request.client.host if request.client else "unknown")
    user = db.scalar(select(User).where(User.username == username))
    # Always perform a password hash check, including for nonexistent accounts.
    valid = password_hasher.verify(body.password, user.password_hash if user else dummy_hash)
    if not user or not valid or not user.is_active:
        raise HTTPException(401, "账号或密码错误。", headers={"Cache-Control": "no-store"})
    now = utcnow()
    db.execute(delete(LoginSession).where(LoginSession.expires_at < now))
    refresh, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    session = LoginSession(user_id=user.id, refresh_hash=digest(refresh), csrf_hash=digest(csrf),
        persistent=body.remember, expires_at=now + timedelta(days=settings.REFRESH_TOKEN_DAYS)
        if body.remember else now + timedelta(hours=12))
    db.add(session)
    db.commit()
    issue_cookies(response, user, session, refresh, csrf)
    return {"user": public_user(user)}


def get_refresh_session(request: Request, db: Session):
    cookie = request.cookies.get(REFRESH_COOKIE, "")
    try:
        session_id, refresh = cookie.split(".", 1)
        session_id = uuid.UUID(session_id)
        if not refresh:
            raise ValueError()
    except (ValueError, AttributeError):
        raise unauthorized()
    session = db.scalar(select(LoginSession).where(LoginSession.id == session_id).with_for_update())
    if not session or session.revoked_at or session.expires_at <= utcnow():
        raise unauthorized()
    require_csrf(request, session)
    if not hmac.compare_digest(session.refresh_hash, digest(refresh)):
        # Reuse of an already rotated token revokes that entire login session.
        session.revoked_at = utcnow()
        db.commit()
        raise unauthorized()
    return session


@router.post("/refresh")
def refresh_login(request: Request, response: Response, db: Session = Depends(get_db)):
    require_origin(request)
    session = get_refresh_session(request, db)
    user = db.get(User, session.user_id)
    if not user or not user.is_active:
        raise unauthorized()
    refresh = secrets.token_urlsafe(48)
    session.refresh_hash = digest(refresh)
    db.commit()
    # Keep CSRF stable for the session so concurrent business requests remain valid.
    csrf = request.headers["x-csrf-token"]
    issue_cookies(response, user, session, refresh, csrf)
    return {"user": public_user(user)}


@router.get("/me")
def current_user(response: Response, auth: AuthContext = Depends(get_current_auth)):
    response.headers["Cache-Control"] = "no-store"
    return {"user": public_user(auth.user)}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    require_origin(request)
    if request.cookies.get(REFRESH_COOKIE):
        session = get_refresh_session(request, db)
        session.revoked_at = utcnow()
        db.commit()
    clear_cookies(response)


