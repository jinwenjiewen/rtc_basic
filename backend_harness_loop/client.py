"""Local-only, bounded M0 browser sessions; not the product session API."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit
import uuid

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from .capture import _json_body, create_capture_app
from .config import Settings
from .evidence import analyze_capture
from .measurements import validate_measurement, validate_measurement_start, summarize_measurements
from .probes.rtc import build_start_body, call_openapi, _valid_public_callback
from .rtc_token import issue_rtc_token, validate_static_rtc_token


WEB = Path(__file__).with_name("web")
SDK = Path(__file__).resolve().parents[1] / "node_modules/@volcengine/rtc/index.min.js"
EVENTS = frozenset({
    "joined", "agent_joined", "agent_audio_published", "first_audio_decoded",
    "remote_level_active", "remote_level_silent", "mic_started", "mic_stopped",
    "mic_ended", "text_sent", "interrupt_sent", "autoplay_failed",
    "playback_resumed", "heard_confirmed", "speech_input_confirmed",
    "stop_confirmed", "sdk_error", "player_playing", "mic_level_active",
})
HUMAN_EVENTS = {"heard_confirmed", "speech_input_confirmed", "stop_confirmed"}


@dataclass
class ClientSession:
    session_id: str
    identity: dict = field(repr=False)
    run_id: str
    user_id: str
    agent_id: str
    expires_at: int
    deadline: float
    binding_mode: str = "app_key"
    last_heartbeat: float = field(default_factory=time.monotonic)
    state: str = "created"
    start_attempted: bool = False
    cleanup_rounds: int = 0
    events: list[dict] = field(default_factory=list)
    measurements: list[dict] = field(default_factory=list)
    measurement_starts: list[dict] = field(default_factory=list)
    provider: list[dict] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class ClientSessions:
    def __init__(self, settings: Settings, capture_path: Path, public_url: str, runtime=None,
                 *, max_active: int = 1, max_sessions: int = 10,
                 service_name: str = "backend_harness_loop.m0"):
        if not 1 <= max_active <= max_sessions <= 128:
            raise ValueError("Invalid RTC session capacity")
        self.max_active, self.max_sessions, self.service_name = max_active, max_sessions, service_name
        self.settings = settings
        self.runtime = runtime
        self.capture_path = Path(capture_path)
        self.event_path = self.capture_path.with_suffix(".client.jsonl")
        self.measurement_path = self.capture_path.with_suffix(".measurements.jsonl")
        self.ledger_path = self.capture_path.with_suffix(".sessions.json")
        self.public_url = public_url.rstrip("/")
        parsed = urlsplit(self.public_url)
        if not _valid_public_callback(self.public_url) or parsed.query or parsed.path not in {"", "/"}:
            raise ValueError("M0 client requires a clean public HTTPS base URL")
        if settings.rtc_api_version != "2024-12-01":
            raise ValueError("The browser M0 currently validates the selected 2024 RTC route only")
        if not 0 < settings.rtc_timeout_seconds <= 15:
            raise ValueError("M0 client RTC timeout must be between 0 and 15 seconds")
        self.sessions: dict[str, ClientSession] = {}
        self.create_lock = asyncio.Lock()
        self.file_lock = asyncio.Lock()
        self._restore_ledger()

    def _restore_ledger(self):
        if not self.ledger_path.exists():
            return
        try:
            if self.ledger_path.stat().st_size > 1_000_000:
                raise ValueError()
            data = json.loads(self.ledger_path.read_text("utf-8"))
            expected = hashlib.sha256(self.settings.rtc_app_id.encode()).hexdigest()
            if data.get("schema_version") != 1 or data.get("app_id_sha256") != expected:
                raise ValueError()
            entries = data["sessions"]
            if not isinstance(entries, list) or len(entries) > self.max_sessions:
                raise ValueError()
            for entry in entries:
                match = re.fullmatch(r"m0_session_([a-f0-9]{32})", entry["session_id"])
                if not match:
                    raise ValueError()
                suffix = match[1]
                mode = entry.get("binding_mode", "app_key")
                if mode not in {"app_key", "static_token"}:
                    raise ValueError()
                expected_room = self.settings.rtc_static_room_id if mode == "static_token" else "m0_room_" + suffix
                expected_user = self.settings.rtc_static_user_id if mode == "static_token" else "m0_user_" + suffix
                if not expected_room or not expected_user or entry["room_id"] != expected_room or entry["task_id"] != "m0_task_" + suffix:
                    raise ValueError()
                if entry.get("user_id", "m0_user_" + suffix) != expected_user:
                    raise ValueError()
                if entry["state"] not in {"created", "starting", "running", "stop_pending", "stopped"} or not isinstance(entry["start_attempted"], bool):
                    raise ValueError()
                if entry["session_id"] in self.sessions or (entry["state"] in {"starting", "running", "stop_pending"} and not entry["start_attempted"]):
                    raise ValueError()
                if not isinstance(entry["expires_at"], int) or isinstance(entry["expires_at"], bool) or not 0 < entry["expires_at"] <= 10**11:
                    raise ValueError()
                cleanup_rounds = entry.get("cleanup_rounds", 0)
                provider = entry.get("provider", [])
                if not isinstance(cleanup_rounds, int) or isinstance(cleanup_rounds, bool) or cleanup_rounds < 0:
                    raise ValueError()
                if not isinstance(provider, list) or any(not isinstance(observation, dict) for observation in provider):
                    raise ValueError()
                self.sessions[entry["session_id"]] = ClientSession(
                    session_id=entry["session_id"],
                    identity={"AppId": self.settings.rtc_app_id, "RoomId": entry["room_id"], "TaskId": entry["task_id"]},
                    run_id="m0_run_" + suffix, user_id=expected_user, agent_id="m0_agent_" + suffix,
                    expires_at=int(entry["expires_at"]), deadline=time.monotonic() - 1,
                    binding_mode=mode,
                    state=entry["state"], start_attempted=entry["start_attempted"],
                    cleanup_rounds=cleanup_rounds, provider=provider,
                )
        except (ValueError, KeyError, TypeError, OSError):
            raise ValueError("Invalid or different-app M0 recovery ledger; preserve it for inspection") from None

    def _persist_ledger(self):
        # Persist identifiers before Start, so forced process termination is recoverable.
        data = {"schema_version": 1, "m0_complete": False,
                "app_id_sha256": hashlib.sha256(self.settings.rtc_app_id.encode()).hexdigest(), "sessions": [
            {"session_id": s.session_id, "room_id": s.identity["RoomId"],
             "task_id": s.identity["TaskId"], "state": s.state,
             "user_id": s.user_id, "binding_mode": s.binding_mode,
             "start_attempted": s.start_attempted, "expires_at": s.expires_at,
             "cleanup_rounds": s.cleanup_rounds, "provider": s.provider}
            for s in self.sessions.values()
        ]}
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.ledger_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.ledger_path)

    def get(self, session_id: str) -> ClientSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "Unknown M0 session")
        return session

    async def create(self, profile_id: str | None = None) -> dict:
        async with self.create_lock:
            if self.runtime is not None:
                try:
                    profile_id = self.runtime.validate_profile(profile_id)
                except Exception as exc:
                    raise HTTPException(503, {"code": getattr(exc, "code", "model_configuration_unavailable")}) from None
            if sum(s.state != "stopped" for s in self.sessions.values()) >= self.max_active:
                raise HTTPException(409, "End the current M0 session before creating another")
            if len(self.sessions) >= self.max_sessions:
                raise HTTPException(409, "M0 instance session limit reached; restart after cleanup")
            suffix = uuid.uuid4().hex
            static = bool(self.settings.rtc_static_token)
            room_id = self.settings.rtc_static_room_id if static else "m0_room_" + suffix
            user_id = self.settings.rtc_static_user_id if static else "m0_user_" + suffix
            identity = {"AppId": self.settings.rtc_app_id, "RoomId": room_id, "TaskId": "m0_task_" + suffix}
            try:
                token = (validate_static_rtc_token(self.settings.rtc_static_token, self.settings.rtc_app_id, room_id, user_id)
                         if static else issue_rtc_token(self.settings.rtc_app_id, self.settings.rtc_app_key, room_id, user_id))
                remaining = min(600, token.expires_at - int(time.time())) if token.expires_at else 600
                if remaining < 30:
                    raise ValueError("RTC token expires too soon for M0")
            except ValueError:
                code = "rtc_static_token_invalid" if static else "rtc_app_key_required"
                raise HTTPException(503, {"code": code}) from None
            session = ClientSession(
                session_id="m0_session_" + suffix, identity=identity,
                run_id="m0_run_" + suffix, user_id=user_id, agent_id="m0_agent_" + suffix,
                expires_at=int(time.time()) + remaining, deadline=time.monotonic() + remaining,
                binding_mode="static_token" if static else "app_key",
            )
            self.sessions[session.session_id] = session
            self._persist_ledger()
            if self.runtime is not None:
                try:
                    self.runtime.register_session(session.session_id, session.run_id, profile_id, identity["TaskId"], user_id)
                except Exception:
                    session.state = "stopped"
                    self._persist_ledger()
                    raise HTTPException(503, {"code": "runtime_session_unavailable"}) from None
            return {"session_id": session.session_id, "app_id": self.settings.rtc_app_id,
                    "room_id": identity["RoomId"], "user_id": user_id,
                    "agent_id": session.agent_id, "token": token.value, "expires_at": session.expires_at,
                    **({"profile_id": profile_id, "runtime_mode": "agent_loop"} if self.runtime is not None else {})}

    async def _public_health(self, client: httpx.AsyncClient) -> bool:
        try:
            response = await client.get(self.public_url + "/healthz", headers={"ngrok-skip-browser-warning": "m0-validation"})
            data = response.json() if response.status_code == 200 else {}
            return data.get("service") == self.service_name and data.get("instance_id") == self.settings.capture_instance_id
        except (httpx.HTTPError, ValueError):
            return False

    async def start(self, session: ClientSession) -> dict:
        async with session.lock:
            if session.state == "running":
                return {"running": True, "provider": session.provider[-1:]}
            if session.state != "created" or time.monotonic() >= session.deadline:
                raise HTTPException(409, "M0 session cannot be started")
            async with httpx.AsyncClient(timeout=self.settings.rtc_timeout_seconds, trust_env=self.settings.trust_env, follow_redirects=False) as client:
                if not await self._public_health(client):
                    raise HTTPException(503, "Public URL does not reach this M0 instance")
                callback = self.public_url + "/api/chat_callback?" + urlencode({"token": self.settings.callback_secret, "rtc_run_id": session.run_id})
                try:
                    body = build_start_body(self.settings, session.identity, callback)
                    body["AgentConfig"]["TargetUserId"] = [session.user_id]
                except ValueError:
                    raise HTTPException(503, "M0 legacy voice profile is not configured") from None
                session.state = "starting"
                session.start_attempted = True
                self._persist_ledger()
                try:
                    observation = await call_openapi(client, self.settings, "StartVoiceChat", body)
                    session.provider.append(observation)
                    if observation["ok"]:
                        session.state = "running"
                        session.last_heartbeat = time.monotonic()
                        self._persist_ledger()
                        return {"running": True, "provider": [observation]}
                finally:
                    if session.state != "running":
                        # Unknown Start outcomes still need cleanup of this exact task.
                        await self._finish_stop(session)
                return {"running": False, "stopped": session.state == "stopped", "provider": session.provider}

    async def _stop_locked(self, session: ClientSession) -> dict:
        if self.runtime is not None:
            await self.runtime.close_session(session.session_id)
        if session.state == "stopped":
            return {"stopped": True, "provider": []}
        if not session.start_attempted:
            session.state = "stopped"
            self._persist_ledger()
            return {"stopped": True, "provider": []}
        session.state = "stop_pending"
        session.cleanup_rounds += 1
        self._persist_ledger()
        results = []
        async with httpx.AsyncClient(timeout=self.settings.rtc_timeout_seconds, trust_env=self.settings.trust_env, follow_redirects=False) as client:
            for _ in range(2):
                results.append(await call_openapi(client, self.settings, "StopVoiceChat", session.identity))
        session.provider.extend(results)
        if any(item.get("ok") for item in results):
            session.state = "stopped"
        self._persist_ledger()
        return {"stopped": session.state == "stopped", "provider": results}

    async def stop(self, session: ClientSession) -> dict:
        async with session.lock:
            return await self._finish_stop(session)

    async def _finish_stop(self, session: ClientSession) -> dict:
        # Retain the lock until cleanup finishes even if cancellation repeats.
        task = asyncio.create_task(self._stop_locked(session))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError()
        return result

    async def reap(self):
        now = time.monotonic()
        for session in list(self.sessions.values()):
            expired = now >= session.deadline or now - session.last_heartbeat >= 60
            if session.state != "stopped" and (expired or session.state == "stop_pending") and session.cleanup_rounds < 3:
                await self.stop(session)

    async def close(self):
        for session in list(self.sessions.values()):
            if session.state != "stopped":
                await self.stop(session)

    async def event(self, session: ClientSession, payload: dict):
        if set(payload) - {"kind", "elapsed_ms", "duration_ms", "code"} or not isinstance(payload.get("kind"), str) or payload["kind"] not in EVENTS:
            raise HTTPException(400, "Only bounded M0 observation fields are accepted")
        for key in ("elapsed_ms", "duration_ms"):
            value = payload.get(key)
            if key in payload and (not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 3_600_000 or not math.isfinite(value)):
                raise HTTPException(400, "Invalid M0 event timing")
        if "elapsed_ms" not in payload:
            raise HTTPException(400, "M0 event requires a relative timestamp")
        if "code" in payload and (not isinstance(payload["code"], int) or isinstance(payload["code"], bool) or not -10**9 <= payload["code"] <= 10**9):
            raise HTTPException(400, "Invalid SDK machine code")
        if len(session.events) >= 300:
            raise HTTPException(429, "M0 observation limit reached")
        if payload["kind"] == "stop_confirmed" and not any(e["kind"] == "interrupt_sent" for e in session.events):
            raise HTTPException(409, "Send an interrupt before confirming audible stop")
        if payload["kind"] == "speech_input_confirmed" and not any(e["kind"] == "mic_started" for e in session.events):
            raise HTTPException(409, "Enable the microphone before confirming voice input")
        event = {**payload, "recorded_at": int(time.time()), "scope": "human_report" if payload["kind"] in HUMAN_EVENTS else "browser_sdk_observation"}
        session.events.append(event)
        record = {"session_sha256": hashlib.sha256(session.session_id.encode()).hexdigest(), "run_sha256": hashlib.sha256(session.run_id.encode()).hexdigest(), **event}
        async with self.file_lock:
            self.event_path.parent.mkdir(parents=True, exist_ok=True)
            with self.event_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"recorded": True}

    def _append_measurement(self, session: ClientSession, phase: str, sample: dict):
        record = {"session_sha256": hashlib.sha256(session.session_id.encode()).hexdigest(),
                  "run_sha256": hashlib.sha256(session.run_id.encode()).hexdigest(),
                  "recorded_at": int(time.time()), "phase": phase, "sample": sample}
        self.measurement_path.parent.mkdir(parents=True, exist_ok=True)
        with self.measurement_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def measurement_start(self, session: ClientSession, payload: dict):
        try:
            sample = validate_measurement_start(payload)
        except ValueError:
            raise HTTPException(400, "Invalid bounded SDK measurement start") from None
        async with self.file_lock:
            previous = next((item for item in session.measurement_starts if item["sample_id"] == sample["sample_id"]), None)
            if previous is not None:
                if previous != sample:
                    raise HTTPException(409, "An M0 measurement start is immutable")
                return {"recorded": True, "duplicate": True}
            if len(session.measurement_starts) >= 20:
                raise HTTPException(429, "M0 measurement attempt limit reached")
            self._append_measurement(session, "started", sample)
            session.measurement_starts.append(sample)
        return {"recorded": True, "duplicate": False}

    async def measurement(self, session: ClientSession, payload: dict):
        try:
            sample = validate_measurement(payload)
        except ValueError:
            raise HTTPException(400, "Invalid bounded SDK measurement") from None
        async with self.file_lock:
            previous = next((item for item in session.measurements if item["sample_id"] == sample["sample_id"]), None)
            if previous is not None:
                if previous != sample:
                    raise HTTPException(409, "An M0 measurement is immutable after recording")
                return {"recorded": True, "duplicate": True}
            if len(session.measurements) >= 20:
                raise HTTPException(429, "M0 measurement limit reached")
            start = next((item for item in session.measurement_starts if item["sample_id"] == sample["sample_id"]), None)
            if start is None or any(start[key] != sample[key] for key in ("kind", "started_ms")):
                raise HTTPException(409, "Record a matching measurement start before its terminal result")
            if sample["kind"] == "speech_first_audio" and sample["state"] == "completed":
                start = sample["started_ms"]
                trigger = sample["trigger_ms"]
                mic_starts = [e["elapsed_ms"] for e in session.events if e["kind"] == "mic_started" and e["elapsed_ms"] <= start]
                if not mic_starts:
                    raise HTTPException(409, "Voice measurement requires a captured microphone start")
                last_start = max(mic_starts)
                if not any(e["kind"] == "mic_level_active" and start <= e["elapsed_ms"] <= trigger for e in session.events):
                    raise HTTPException(409, "Voice measurement requires observed local microphone energy")
                if any(e["kind"] in {"mic_stopped", "mic_ended"} and last_start <= e["elapsed_ms"] <= sample["observed_ms"] for e in session.events):
                    raise HTTPException(409, "Microphone ended during the voice measurement")
            self._append_measurement(session, "terminal", sample)
            session.measurements.append(sample)
        return {"recorded": True, "duplicate": False}

    async def _capture_records(self, session: ClientSession) -> list[dict]:
        def read():
            if not self.capture_path.exists() or self.capture_path.stat().st_size > 8_000_000:
                return []
            result = []
            run_hash = hashlib.sha256(session.run_id.encode()).hexdigest()
            for line in self.capture_path.read_text("utf-8").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if any(item.get("kind") == "run" and item.get("sha256") == run_hash for item in record.get("correlation", {}).get("identifiers", [])):
                    result.append(record)
            return result
        return await asyncio.to_thread(read)

    async def status(self, session: ClientSession) -> dict:
        records = await self._capture_records(session)
        analysis = analyze_capture(records)
        round_hashes = {identifier["sha256"] for record in records if record.get("source") == "custom_llm"
                        for identifier in record.get("correlation", {}).get("identifiers", []) if identifier.get("kind") == "round"}
        measurements_summary = summarize_measurements(session.measurements, round_hashes)
        terminal_ids = {sample["sample_id"] for sample in session.measurements}
        incomplete = sum(sample["sample_id"] not in terminal_ids for sample in session.measurement_starts)
        measurements_summary.update(started_count=len(session.measurement_starts), incomplete_count=incomplete)
        measurements_summary["ready"] = measurements_summary["ready"] and incomplete == 0
        return {"state": session.state, "expires_at": session.expires_at,
                "server_evidence": {"custom_llm_rounds": analysis["unique_round_counts"]["custom_llm"],
                                    "matched_rounds": analysis["custom_rounds_matched_to_state"],
                                    "rts_errors": analysis["machine_error_codes"]},
                "client_evidence": session.events[-100:],
                "client_event_counts": dict(Counter(item["kind"] for item in session.events)),
                "measurements": session.measurements,
                "measurements_summary": measurements_summary,
                "m0_complete": False,
                "manual_confirmations": {kind: any(e["kind"] == kind for e in session.events) for kind in HUMAN_EVENTS}}


def create_client_app(settings: Settings, output_path: Path, public_url: str, *, port: int = 3101, runtime=None, chat_handler=None, event_handler=None):
    app = create_capture_app(settings, output_path, chat_handler=chat_handler, event_handler=event_handler)
    manager = ClientSessions(settings, output_path, public_url, runtime=runtime)
    client_key = secrets.token_urlsafe(32)
    app.state.client_key = client_key
    app.state.client_sessions = manager
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def local(request: Request):
        if (request.headers.get("host", "").lower() not in hosts
            or request.client is None or request.client.host not in {"127.0.0.1", "::1"}
            or any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"))):
            raise HTTPException(403, "M0 browser management is loopback-only")
        origin = request.headers.get("origin")
        if origin and origin not in {"http://" + host for host in hosts}:
            raise HTTPException(403, "Invalid M0 client origin")

    def authorize(request: Request):
        local(request)
        supplied = request.headers.get("x-m0-client-key", "")
        if not hmac.compare_digest(supplied.encode(), client_key.encode()):
            raise HTTPException(401, "Invalid M0 browser credential")

    # Explicit validation apps can reuse this exact local/nonce boundary.
    # No extra route or capability is installed in the normal application.
    app.state.authorize_browser = authorize

    text_manager = None
    if runtime is not None:
        from .text import install_text_routes
        text_manager = install_text_routes(app, runtime, authorize, local)

    @asynccontextmanager
    async def lifespan(_app):
        # A previous process may have died after Start was accepted. Resolve
        # its ledger before allowing a new browser session in this process.
        await manager.close()
        async def reaper():
            while True:
                await asyncio.sleep(2)
                await manager.reap()
                if text_manager is not None:
                    await text_manager.reap()
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
            if text_manager is not None:
                await text_manager.close()
            if runtime is not None:
                await runtime.aclose()
    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

    @app.get("/m0", response_class=HTMLResponse)
    async def page(request: Request):
        local(request)
        html = (WEB / "m0.html").read_text("utf-8").replace("__M0_CLIENT_KEY__", client_key)
        return HTMLResponse(html.replace("__RUNTIME_MODE__", "agent_loop" if runtime is not None else "capture_only"))

    @app.get("/m0/assets/{asset}")
    async def asset(request: Request, asset: str):
        local(request)
        paths = {"rtc.js": SDK, "m0.js": WEB / "m0.js", "m0.css": WEB / "m0.css", "measurements.js": WEB / "measurements.js"}
        if asset not in paths or not paths[asset].is_file():
            raise HTTPException(404, "M0 asset not installed")
        return FileResponse(paths[asset], media_type="text/css" if asset.endswith(".css") else "application/javascript")

    @app.post("/api/m0/sessions")
    async def create(request: Request):
        authorize(request)
        payload = await _json_body(request)
        if runtime is None and payload != {}:
            raise HTTPException(400, "M0 sessions use server-generated identities only")
        if runtime is not None and (set(payload) - {"profile_id"} or ("profile_id" in payload and not isinstance(payload["profile_id"], str))):
            raise HTTPException(400, "Only a configured model profile may be selected")
        return await manager.create(payload.get("profile_id"))

    if runtime is not None:
        @app.get("/api/m1/models")
        async def models(request: Request):
            authorize(request)
            return {"mode": "agent_loop", **runtime.profiles.public_inventory()}

        @app.get("/api/m1/sessions/{session_id}")
        async def runtime_status(request: Request, session_id: str):
            authorize(request)
            manager.get(session_id)
            return runtime.snapshot(session_id)

    @app.get("/api/m0/sessions/{session_id}")
    async def status(request: Request, session_id: str):
        authorize(request)
        return await manager.status(manager.get(session_id))

    @app.post("/api/m0/sessions/{session_id}/{action}")
    async def action(request: Request, session_id: str, action: str):
        authorize(request)
        payload = await _json_body(request)
        session = manager.get(session_id)
        if action == "events":
            return await manager.event(session, payload)
        if action == "measurements":
            return await manager.measurement(session, payload)
        if action == "measurement-starts":
            return await manager.measurement_start(session, payload)
        if payload != {}:
            raise HTTPException(400, "Unexpected M0 action fields")
        if action == "start":
            return await manager.start(session)
        if action == "stop":
            return await manager.stop(session)
        if action == "heartbeat":
            if session.state not in {"created", "running"} or time.monotonic() >= session.deadline:
                raise HTTPException(409, "M0 session has ended or expired")
            session.last_heartbeat = time.monotonic()
            return {"alive": True, "expires_at": session.expires_at}
        raise HTTPException(404, "Unknown M0 action")

    return app
