"""M0 callback capture: metadata only, fixed synthetic response, no agent/model.

Run with access logging disabled: callback query strings can contain credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import anyio

from .protocol import MAX_PAYLOAD_BYTES, decode_rts_message, encode_sse_chunk, encode_sse_end

_SENSITIVE = re.compile(r"token|authorization|secret|signature|api.?key|password|credential", re.I)
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_ID_FIELDS = {
    "TaskId": "task", "TaskID": "task", "task_id": "task", "rtc_run_id": "run",
    "conversation_id": "conversation", "RoomId": "room", "RoomID": "room",
    "AppId": "app", "UserId": "user", "UserID": "user", "user_id": "user", "userId": "user",
    "RequestId": "request", "RequestID": "request", "request_id": "request",
    "RoundID": "round", "RoundId": "round", "roundId": "round", "round_id": "round", "turn_id": "turn",
    "id": "generic_id",
}


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "number" if isinstance(value, (int, float)) else "unknown"


def _safe_name(key: Any) -> str:
    if not isinstance(key, str) or not _FIELD_NAME.fullmatch(key):
        return "<non_identifier>"
    return "<sensitive_field>" if _SENSITIVE.search(key) else key


def payload_shape(payload: dict) -> dict[str, str]:
    """Field names and types only; no scalar field value is retained."""
    shape: dict[str, str] = {}

    def visit(value: Any, path: str, depth: int) -> None:
        if len(shape) >= 100:
            return
        shape[path[:180]] = _kind(value)
        if depth >= 4:
            return
        if isinstance(value, dict):
            for key, item in list(value.items())[:30]:
                field = _safe_name(key)
                if field == "<sensitive_field>":
                    shape[(path + "." + field)[:180]] = "redacted"
                    continue
                visit(item, path + "." + field, depth + 1)
        elif isinstance(value, list):
            for item in value[:2]:
                visit(item, path + "[]", depth + 1)

    visit(payload, "$", 0)
    return shape


def correlation_metadata(payload: dict, *, query: dict | None = None, headers: dict | None = None) -> dict:
    identifiers: list[dict] = []
    for key, kind in _ID_FIELDS.items():
        value = payload.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            identifiers.append({"field": key, "kind": kind, "sha256": hashlib.sha256(str(value).encode()).hexdigest()})
    # Subtitles carry their round reference inside data[], never retain text.
    data = payload.get("data")
    if isinstance(data, list):
        for index, item in enumerate(data[:10]):
            if isinstance(item, dict):
                for key, kind in _ID_FIELDS.items():
                    value = item.get(key)
                    if isinstance(value, (str, int)) and not isinstance(value, bool):
                        identifiers.append({"field": f"data[{index}].{key}", "kind": kind, "sha256": hashlib.sha256(str(value).encode()).hexdigest()})
    for key in ("rtc_run_id", "conversation_id"):
        value = (query or {}).get(key)
        if isinstance(value, str) and value:
            identifiers.append({"field": "query." + key, "kind": _ID_FIELDS[key], "sha256": hashlib.sha256(value.encode()).hexdigest()})
    for key in ("x-request-id", "x-tt-logid"):
        value = (headers or {}).get(key)
        if isinstance(value, str) and value:
            identifiers.append({"field": "header." + key, "kind": "request_candidate", "sha256": hashlib.sha256(value.encode()).hexdigest()})
    result: dict = {"identifiers": identifiers, "stable_round_association": "not_verified"}
    stage = payload.get("Stage")
    if isinstance(stage, dict) and isinstance(stage.get("Code"), int) and not isinstance(stage["Code"], bool) and 0 <= stage["Code"] <= 5:
        result["stage_code"] = stage["Code"]
    event_time = payload.get("EventTime")
    if isinstance(event_time, int) and not isinstance(event_time, bool) and 0 <= event_time <= 10**15:
        result["event_time"] = event_time
    error_info = payload.get("ErrorInfo")
    if isinstance(error_info, dict):
        code = error_info.get("ErrorCode")
        if code is None:
            code = error_info.get("Code")
        if isinstance(code, (str, int)) and not isinstance(code, bool) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", str(code)):
            result["error_code"] = str(code)
    return result


async def _json_body(request: Request) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_PAYLOAD_BYTES:
            raise HTTPException(413, "M0 request exceeds capture size limit")
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPException(400, "Invalid JSON request") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "JSON request must be an object")
    return value


def create_capture_app(settings: Any, output_path: Path, *, chat_handler=None, event_handler=None) -> FastAPI:
    secret = getattr(settings, "callback_secret", "")
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("A nonempty callback_secret is required for capture")
    instance_id = getattr(settings, "capture_instance_id", "local")
    fail_first = getattr(settings, "capture_fail_first", False)
    chunk_count = getattr(settings, "capture_chunk_count", 1)
    try:
        chunk_delay = float(getattr(settings, "capture_chunk_delay_seconds", 0))
    except (TypeError, ValueError):
        raise ValueError("Invalid capture chunk delay") from None
    if not isinstance(fail_first, bool):
        raise ValueError("capture_fail_first must be a boolean")
    if not isinstance(chunk_count, int) or isinstance(chunk_count, bool) or not 1 <= chunk_count <= 8:
        raise ValueError("capture_chunk_count must be between 1 and 8")
    if not math.isfinite(chunk_delay) or not 0 <= chunk_delay <= 2:
        raise ValueError("capture_chunk_delay_seconds must be between 0 and 2")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    lock = asyncio.Lock()
    output_path = Path(output_path)
    app.state.capture_count = 0
    app.state.authenticated_chat_count = 0

    def authorize(request: Request, payload: dict | None = None) -> None:
        supplied = request.headers.get("x-rtc-callback-token") or request.query_params.get("token")
        if supplied is None and payload is not None:
            supplied = payload.get("signature")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), secret.encode()):
            raise HTTPException(401, "Invalid M0 callback credential")

    async def record(source: str, payload: dict, request: Request, *, tag: str | None = None, extra: dict | None = None) -> None:
        metadata = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "schema": payload_shape(payload),
            "correlation": correlation_metadata(payload, query=dict(request.query_params), headers=dict(request.headers)),
            "evidence_scope": "observed_metadata_only_not_end_to_end_verification",
        }
        if tag is not None:
            metadata["tlv_tag"] = tag
        # Fault diagnostics are a closed contract, never arbitrary payload values.
        if extra:
            allowed = {"response_status", "state", "duration_ms", "chunks_sent"}
            if set(extra) - allowed:
                raise ValueError("Unsupported capture diagnostic field")
            if "response_status" in extra and extra["response_status"] not in {200, 503}:
                raise ValueError("Unsupported capture response status")
            if "state" in extra and extra["state"] not in {"completed", "cancelled", "closed"}:
                raise ValueError("Unsupported capture stream state")
            if "chunks_sent" in extra and (
                not isinstance(extra["chunks_sent"], int)
                or isinstance(extra["chunks_sent"], bool)
                or not 0 <= extra["chunks_sent"] <= 8
            ):
                raise ValueError("Invalid capture chunk count")
            if "duration_ms" in extra and (
                not isinstance(extra["duration_ms"], (int, float))
                or isinstance(extra["duration_ms"], bool)
                or not math.isfinite(extra["duration_ms"])
                or extra["duration_ms"] < 0
            ):
                raise ValueError("Invalid capture duration")
            metadata.update(extra)
        line = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + "\n"

        def append() -> None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)

        async with lock:
            await asyncio.to_thread(append)
            app.state.capture_count += 1

    @app.get("/healthz")
    async def healthz():
        return {"service": "backend_harness_loop.m0", "instance_id": instance_id, "mode": "agent_loop" if chat_handler else "capture_only", "capture_count": app.state.capture_count}

    @app.post("/api/chat_callback")
    async def chat_callback(request: Request):
        authorize(request)
        payload = await _json_body(request)
        async with lock:
            app.state.authenticated_chat_count += 1
            inject_fault = fail_first and app.state.authenticated_chat_count == 1
        if chat_handler is not None:
            # Arrival metadata is not evidence that the runtime accepted a
            # duplicate/cancelled round or that its HTTP response was 200.
            await record("custom_llm", payload, request)
            return await chat_handler(request, payload)
        await record("custom_llm", payload, request, extra={"response_status": 503 if inject_fault else 200})
        if inject_fault:
            return JSONResponse(
                {"Error": {"Code": "M0SyntheticUnavailable", "Message": "Synthetic M0 retry experiment"}},
                status_code=503,
            )
        stream_id = "m0_" + uuid.uuid4().hex
        created = int(time.time())

        async def stream():
            began = time.perf_counter()
            chunks_sent = 0
            state = "closed"
            try:
                for index in range(chunk_count):
                    if index and chunk_delay:
                        await asyncio.sleep(chunk_delay)
                    # Counts mean yielded to the server transport, not audio heard.
                    chunks_sent += 1
                    yield encode_sse_chunk("这是 RTC 接口验证回复。", stream_id=stream_id, created=created)
                yield encode_sse_end(stream_id=stream_id, created=created)
                state = "completed"
            except asyncio.CancelledError:
                state = "cancelled"
                raise
            finally:
                # Starlette cancels streams inside an AnyIO cancellation scope.
                # Preserve the short metadata append before propagating cancellation.
                with anyio.CancelScope(shield=True):
                    await record(
                        "capture_stream", payload, request,
                        extra={
                            "response_status": 200,
                            "state": state,
                            "duration_ms": round((time.perf_counter() - began) * 1000, 2),
                            "chunks_sent": chunks_sent,
                        },
                    )

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/rtc/events")
    async def event_health(request: Request):
        authorize(request)
        return {"service": "backend_harness_loop.m0", "status": "ready"}

    @app.post("/api/rtc/events")
    async def events(request: Request):
        payload = await _json_body(request)
        authorize(request, payload)
        if "message" in payload:
            if "binary" in payload and payload["binary"] is not True:
                raise HTTPException(400, "M0 expects binary RTS envelope")
            try:
                tag, decoded = decode_rts_message(payload["message"])
            except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
                raise HTTPException(400, "Invalid bounded RTS TLV envelope") from exc
            await record("rts_event", decoded, request, tag=tag)
            if event_handler is not None:
                await event_handler(request, tag, decoded)
        else:
            await record("rtc_event", payload, request)
            if event_handler is not None:
                await event_handler(request, None, payload)
        return {"status": "recorded", "stable_round_association": "not_verified"}

    return app
