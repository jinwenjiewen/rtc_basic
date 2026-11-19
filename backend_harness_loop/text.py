"""Local text transport for the same M1 Agent Loop, without RTC resources.

This is a bounded development entry point. Text sessions own a runtime binding,
not a VoiceChat task; final text is streamed without exposing decision drafts or
native tool messages. Model profiles and context remain runtime responsibilities.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hmac
import json
from pathlib import Path
import secrets
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse

from .models import ModelError
from .runtime import AgentRuntime, RuntimeFault
from .store import RuntimeStore, StoreError


WEB = Path(__file__).with_name("web")
MAX_REQUEST_BYTES = 65_536
MAX_TEXT_BYTES = 8192


def _error(code: str, status: int = 400):
    return HTTPException(status, {"code": code})


async def _body(request: Request) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST_BYTES:
            raise _error("text_request_too_large", 413)

    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError()
            result[name] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise _error("invalid_text_request") from None
    if not isinstance(value, dict):
        raise _error("invalid_text_request")
    return value


def _round(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _error("invalid_round_id")
    selected = str(value)
    if not selected or len(selected) > 128 or any(ord(char) < 32 for char in selected):
        raise _error("invalid_round_id")
    try:
        selected.encode("utf-8")
    except UnicodeError:
        raise _error("invalid_round_id") from None
    return selected


def _text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error("invalid_text")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeError:
        raise _error("invalid_text") from None
    if length > MAX_TEXT_BYTES:
        raise _error("text_input_too_large", 413)
    return value


def _sse(event: str, payload: dict) -> bytes:
    return ("event: " + event + "\ndata: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")


@dataclass
class TextSession:
    session_id: str
    run_id: str
    profile_id: str
    expires_at: int
    deadline: float
    last_heartbeat: float = field(default_factory=time.monotonic)
    closed: bool = False


class TextSessions:
    def __init__(self, runtime: AgentRuntime, *, lifetime_seconds: int = 600, heartbeat_seconds: int = 60,
                 max_active: int = 4, max_sessions: int = 32):
        if not 1 <= lifetime_seconds <= 3600 or not 1 <= heartbeat_seconds <= lifetime_seconds:
            raise ValueError("Invalid text session lease")
        if not 1 <= max_active <= max_sessions <= 128:
            raise ValueError("Invalid text session capacity")
        self.runtime = runtime
        self.lifetime_seconds, self.heartbeat_seconds = lifetime_seconds, heartbeat_seconds
        self.max_active, self.max_sessions = max_active, max_sessions
        self.sessions: dict[str, TextSession] = {}
        self.lock = asyncio.Lock()

    async def create(self, profile_id: str | None = None) -> dict:
        await self.reap()
        async with self.lock:
            if len(self.sessions) >= self.max_sessions:
                raise _error("text_session_instance_capacity", 429)
            if sum(not item.closed for item in self.sessions.values()) >= self.max_active:
                raise _error("text_session_capacity", 429)
            try:
                selected = self.runtime.validate_profile(profile_id)
                suffix = uuid.uuid4().hex
                session_id, run_id = "text_session_" + suffix, "text_run_" + suffix
                self.runtime.register_session(session_id, run_id, selected, "text_task_" + suffix, "text_user_" + suffix)
            except ModelError as exc:
                raise _error(exc.code, 503) from None
            except RuntimeFault as exc:
                raise _error(exc.code, exc.status) from None
            except StoreError as exc:
                raise _error(exc.code, 409) from None
            item = TextSession(session_id, run_id, selected, int(time.time()) + self.lifetime_seconds,
                               time.monotonic() + self.lifetime_seconds)
            self.sessions[item.session_id] = item
            return self._public(item)

    @staticmethod
    def _public(item: TextSession) -> dict:
        return {"session_id": item.session_id, "profile_id": item.profile_id,
                "transport": "text", "mode": "agent_loop", "expires_at": item.expires_at}

    async def get(self, session_id: str, *, active: bool = True) -> TextSession:
        item = self.sessions.get(session_id)
        if item is None:
            raise _error("unknown_text_session", 404)
        if not item.closed and (time.monotonic() >= item.deadline or
                                time.monotonic() - item.last_heartbeat >= self.heartbeat_seconds):
            await self.stop(item)
        if active and item.closed:
            raise _error("text_session_closed", 410)
        return item

    async def stop(self, item: TextSession):
        # Close admission before waiting for the control-loop acknowledgement.
        # The control event itself never waits for model or tool network I/O.
        item.closed = True
        await self.runtime.close_session(item.session_id)

    async def heartbeat(self, item: TextSession) -> dict:
        item = await self.get(item.session_id)
        item.last_heartbeat = time.monotonic()
        return {"alive": True, "expires_at": item.expires_at}

    async def snapshot(self, item: TextSession) -> dict:
        await self.get(item.session_id, active=False)
        return {**self.runtime.snapshot(item.session_id), **self._public(item), "alive": not item.closed}

    async def reap(self):
        for item in list(self.sessions.values()):
            if not item.closed and (time.monotonic() >= item.deadline or
                                    time.monotonic() - item.last_heartbeat >= self.heartbeat_seconds):
                await self.stop(item)

    async def close(self):
        for item in list(self.sessions.values()):
            await self.stop(item)


def install_text_routes(app: FastAPI, runtime: AgentRuntime, authorize, local) -> TextSessions:
    """Install routes only; the owning app calls manager.reap/close in lifespan."""
    if hasattr(app.state, "text_sessions"):
        raise ValueError("Text transport is already installed")
    manager = TextSessions(runtime)
    app.state.text_sessions = manager

    @app.get("/m1/text", response_class=HTMLResponse)
    async def page(request: Request):
        local(request)
        path = WEB / "text.html"
        if not path.is_file():
            raise _error("text_page_not_installed", 404)
        return HTMLResponse(path.read_text("utf-8").replace("__M0_CLIENT_KEY__", app.state.client_key))

    @app.get("/m1/text/assets/{asset}")
    async def asset(request: Request, asset: str):
        local(request)
        paths = {"text.js": WEB / "text.js", "text.css": WEB / "text.css"}
        if asset not in paths or not paths[asset].is_file():
            raise _error("text_asset_not_installed", 404)
        return FileResponse(paths[asset], media_type="text/css" if asset.endswith(".css") else "application/javascript")

    @app.post("/api/m1/text/sessions")
    async def create(request: Request):
        authorize(request)
        payload = await _body(request)
        if set(payload) - {"profile_id"} or ("profile_id" in payload and not isinstance(payload["profile_id"], str)):
            raise _error("invalid_text_session_fields")
        return await manager.create(payload.get("profile_id"))

    @app.get("/api/m1/text/sessions/{session_id}")
    async def status(request: Request, session_id: str):
        authorize(request)
        return await manager.snapshot(await manager.get(session_id, active=False))

    @app.post("/api/m1/text/sessions/{session_id}/turns")
    async def turn(request: Request, session_id: str):
        authorize(request)
        payload = await _body(request)
        if set(payload) != {"round_id", "text"}:
            raise _error("invalid_text_turn_fields")
        round_id, text = _round(payload["round_id"]), _text(payload["text"])
        item = await manager.get(session_id)
        try:
            handle = await runtime.accept(item.run_id, round_id, text)
        except RuntimeFault as exc:
            raise _error(exc.code, exc.status) from None
        except StoreError as exc:
            raise _error(exc.code, 409) from None

        async def stream():
            try:
                yield _sse("turn", {"round_id": round_id, "turn_id": handle.target.turn_id, "epoch": handle.target.epoch})
                async for delta in runtime.speech(handle):
                    yield _sse("delta", {"round_id": round_id, "text": delta})
                # Snapshot contains metadata only, never dialogue or tool JSON.
                snapshot = runtime.snapshot(item.session_id)
                metadata = next((entry for entry in snapshot["turns"] if entry["turn_id"] == handle.target.turn_id), {})
                known = runtime.store.lookup_round(item.session_id, round_id)
                yield _sse("terminal", {"round_id": round_id,
                    "state": known["status"] if known else "unknown",
                    "delivery_status": metadata.get("delivery_status", "unknown"),
                    "error_code": handle.failure_code})
            finally:
                if not handle.consumer_finished:
                    await runtime.disconnect(handle)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        })

    @app.post("/api/m1/text/sessions/{session_id}/interrupt")
    async def interrupt(request: Request, session_id: str):
        authorize(request)
        payload = await _body(request)
        if set(payload) != {"round_id"}:
            raise _error("invalid_text_interrupt_fields")
        round_id = _round(payload["round_id"])
        item = await manager.get(session_id)
        try:
            changed = await runtime.interrupt(item.run_id, round_id)
        except RuntimeFault as exc:
            raise _error(exc.code, exc.status) from None
        except StoreError as exc:
            raise _error(exc.code, 409) from None
        return {"interrupted": bool(changed), "round_id": round_id}

    @app.post("/api/m1/text/sessions/{session_id}/heartbeat")
    async def heartbeat(request: Request, session_id: str):
        authorize(request)
        if await _body(request) != {}:
            raise _error("unexpected_text_action_fields")
        return await manager.heartbeat(await manager.get(session_id))

    @app.post("/api/m1/text/sessions/{session_id}/stop")
    async def stop(request: Request, session_id: str):
        authorize(request)
        if await _body(request) != {}:
            raise _error("unexpected_text_action_fields")
        item = await manager.get(session_id, active=False)
        await manager.stop(item)
        return {"stopped": True}

    return manager


def create_text_app(settings, profiles, database_path: Path, *, port: int = 3101,
                    runtime: AgentRuntime | None = None) -> FastAPI:
    """Standalone local text app; no RTC credentials, SDK, tunnel, or task."""
    owns_store = runtime is None
    if runtime is None:
        runtime = AgentRuntime(RuntimeStore(database_path), profiles,
            model_trust_env=settings.trust_env,
            max_steps=settings.m1_max_steps, max_tool_calls=settings.m1_max_tool_calls,
            wall_seconds=settings.m1_wall_seconds, first_output_ms=settings.m1_first_output_deadline_ms,
            **settings.context_options(), **settings.business_tool_options())
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.agent_runtime = runtime
    app.state.client_key = secrets.token_urlsafe(32)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def local(request: Request):
        if (request.headers.get("host", "").lower() not in hosts or request.client is None
            or request.client.host not in {"127.0.0.1", "::1"}
            or any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"))):
            raise _error("text_management_loopback_only", 403)
        origin = request.headers.get("origin")
        if origin and origin not in {"http://" + host for host in hosts}:
            raise _error("invalid_text_origin", 403)

    def authorize(request: Request):
        local(request)
        supplied = request.headers.get("x-m0-client-key", "")
        if not hmac.compare_digest(supplied.encode(), app.state.client_key.encode()):
            raise _error("invalid_text_client_credential", 401)

    manager = install_text_routes(app, runtime, authorize, local)

    @asynccontextmanager
    async def lifespan(_app):
        async def reaper():
            while True:
                await asyncio.sleep(2)
                await manager.reap()
        task = asyncio.create_task(reaper())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await manager.close()
            await runtime.aclose()
            if owns_store and not runtime.tasks and not runtime.control_tasks:
                runtime.store.close()
    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

    @app.get("/")
    async def home(request: Request):
        local(request)
        return RedirectResponse("/m1/text")

    @app.get("/healthz")
    async def health():
        return {"service": "backend_harness_loop.text", "mode": "agent_loop", "transports": ["text"],
                "instance_id": settings.capture_instance_id}

    @app.get("/api/m1/models")
    async def models(request: Request):
        authorize(request)
        return {"mode": "agent_loop", **runtime.profiles.public_inventory()}

    return app
