"""M4 authenticated product transport and same-origin React hosting.

Developer nonce routes are deliberately not installed in this application.
The raw ledger, loop budgets, model choice and callback protocol stay shared.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from .capture import create_capture_app
from .client import ClientSessions
from .models import ModelError
from .product_auth import AuthContext, AuthService, AuthSettings, install_auth_routes
from .protocol import encode_sse_chunk, encode_sse_end
from .rtc_contract import ContractError, extract_interrupt, validate_callback
from .rtc_token import issue_rtc_token
from .runtime import AgentRuntime, RuntimeFault
from .store import RuntimeStore, StoreError
from .text import TextSessions, _body, _round, _sse, _text


class ProductRuntime(AgentRuntime):
    owner_live = None

    def _live(self, session, handle):
        return super()._live(session, handle) and (self.owner_live is None or self.owner_live(session.session_id))


def create_product_app(settings, profiles, database_path: Path, output_path: Path, *,
                       auth: AuthService, public_url: str = "", build_dir: Path = Path("build"),
                       runtime: ProductRuntime | None = None):
    owns_store = runtime is None
    if runtime is None:
        runtime = ProductRuntime(RuntimeStore(database_path), profiles,
            model_trust_env=settings.trust_env, max_steps=settings.m1_max_steps,
            max_tool_calls=settings.m1_max_tool_calls, wall_seconds=settings.m1_wall_seconds,
            first_output_ms=settings.m1_first_output_deadline_ms,
            **settings.context_options(), **settings.business_tool_options())
    text = TextSessions(runtime, max_sessions=128)
    owners: dict[str, AuthContext] = {}
    revoked: set[str] = set()
    cleanup: set[asyncio.Task] = set()
    rtc = None
    # Product voice always signs fresh, isolated room/user credentials. A console
    # token shared by the validation page is never used as an account credential.
    if (public_url and settings.rtc_app_key and settings.rtc_legacy_voice_config and settings.callback_secret
            and settings.volc_access_key and settings.volc_secret_key):
        try:
            issue_rtc_token(settings.rtc_app_id, settings.rtc_app_key, "capability_check", "capability_user")
            rtc = ClientSessions(replace(settings, rtc_static_token=""), output_path, public_url, runtime,
                max_active=4, max_sessions=128, service_name="backend_harness_loop.product")
        except ValueError:
            rtc = None

    def live(session_id):
        owner = owners.get(session_id)
        return bool(owner and owner.login_id not in revoked and owner.expires_at > time.time())

    runtime.owner_live = live

    def schedule(coroutine):
        task = asyncio.create_task(coroutine)
        cleanup.add(task)
        def finished(done):
            cleanup.discard(done)
            # Observe bounded cloud cleanup errors without logging credentials.
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)

    async def close_session(session_id):
        await runtime.close_session(session_id)
        if session_id in text.sessions:
            await text.stop(text.sessions[session_id])
        if rtc and session_id in rtc.sessions:
            schedule(rtc.stop(rtc.sessions[session_id]))

    async def revoke(login_id):
        # Invalidate speech/tool ownership before awaiting any provider cleanup.
        revoked.add(login_id)
        for session_id, owner in list(owners.items()):
            if owner.login_id == login_id:
                await close_session(session_id)

    async def check_owner(context, session_id):
        owner = owners.get(session_id)
        if not owner or (owner.user_id, owner.login_id) != (context.user_id, context.login_id):
            raise HTTPException(404, "会话不存在。")
        if not live(session_id):
            await close_session(session_id)
            raise HTTPException(410, "会话已结束，请重新开始。")
        return owner

    async def bind(context, result):
        session_id = result["session_id"]
        owners[session_id] = context
        runtime.store.bind_product_owner(session_id, context.user_id, context.login_id, context.expires_at)
        # Login can be revoked while a concurrent create awaits capacity/network.
        if context.login_id in revoked or not await auth.call(auth.active, context.login_id, context.user_id):
            await close_session(session_id)
            raise HTTPException(401, "登录已过期，请重新登录。")
        return result

    async def callback_owner(run_id):
        binding = runtime.binding(run_id)
        if rtc is None or binding.session_id not in rtc.sessions:
            raise HTTPException(404, "语音会话不存在。")
        owner = owners.get(binding.session_id)
        if not owner or not live(binding.session_id):
            raise HTTPException(410, "会话已结束。")
        if not await auth.call(auth.active, owner.login_id, owner.user_id):
            await revoke(owner.login_id)
            raise HTTPException(401, "会话授权已撤销。")
        return binding

    async def chat(request, payload):
        run_id = request.query_params.get("rtc_run_id", "")
        await callback_owner(run_id)
        round_id, user_text = validate_callback(payload)
        handle = await runtime.accept(run_id, round_id, user_text)
        created, stream_id = int(time.time()), "product_" + handle.target.turn_id

        async def stream():
            try:
                async for delta in runtime.speech(handle):
                    if not live(handle.target.session_id):
                        break
                    yield encode_sse_chunk(delta, stream_id=stream_id, created=created, model="rtc-agent-loop")
                if handle.valid and live(handle.target.session_id):
                    yield encode_sse_end(stream_id=stream_id, created=created, model="rtc-agent-loop")
            finally:
                if not handle.consumer_finished:
                    await runtime.disconnect(handle)
        return StreamingResponse(stream(), media_type="text/event-stream")

    async def event(request, tag, payload):
        binding = await callback_owner(request.query_params.get("rtc_run_id", ""))
        round_id = extract_interrupt(tag, payload, binding.task_id, binding.user_id)
        if round_id is not None and not binding.closed:
            await runtime.interrupt(binding.run_id, round_id)

    app = create_capture_app(replace(settings, callback_secret=settings.callback_secret or secrets.token_urlsafe(32)),
        output_path, chat_handler=chat, event_handler=event)
    # Keep the established callback capture boundary but publish product identity.
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", "") != "/healthz"]
    app.state.agent_runtime, app.state.auth = runtime, auth
    app.state.text_sessions, app.state.client_sessions = text, rtc
    app.state.revoke_login = revoke
    app.add_middleware(CORSMiddleware, allow_origins=list(auth.settings.allowed_origins),
        allow_credentials=True, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type", "X-CSRF-Token"])
    install_auth_routes(app, auth, revoke)
    browser_hosts = {urlsplit(origin).netloc.lower() for origin in auth.settings.allowed_origins}

    for error_type in (RuntimeFault, StoreError, ModelError, ContractError):
        async def runtime_error(request, error):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": {"code": error.code}}, status_code=getattr(error, "status", 409))
        app.add_exception_handler(error_type, runtime_error)

    @app.middleware("http")
    async def headers(request, call_next):
        # An ad-hoc RTC callback tunnel must not also publish the account and
        # frontend endpoints. Public browser origins require explicit config.
        if (request.headers.get("host", "").lower() not in browser_hosts
                and request.url.path not in {"/healthz", "/api/chat_callback", "/api/rtc/events"}):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "入口不存在。"}, status_code=404, headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "frame-ancestors 'none'"})
        return response

    @app.get("/healthz")
    async def health():
        return {"service": "backend_harness_loop.product", "mode": "agent_loop", "instance_id": settings.capture_instance_id,
            "transports": ["text"] + (["rtc"] if rtc else [])}

    def model_menu():
        return {"default_profile": profiles.default_profile_id, "profiles": [
            {"id": p.profile_id, "name": p.name, "configured": bool(p.api_key)}
            for p in (profiles.get(key) for key in profiles.profile_ids)], "rtc_available": rtc is not None}

    @app.get("/api/agent/models")
    async def models(request: Request):
        await auth.authorize(request)
        return model_menu()

    @app.post("/getScenes")
    async def scenes(request: Request):
        await auth.authorize(request)
        if await _body(request) != {}:
            raise HTTPException(400, "不支持的场景参数。")
        return {"ResponseMetadata": {"Action": "getScenes"}, "Result": {"scenes": [
            {"rtc": {"AppId": "", "RoomId": "", "UserId": "", "Token": ""},
             "scene": {"id": item["id"], "name": item["name"], "botName": "", "questions": ["介绍一下课程", "我想了解怎么报名"],
                "isVision": False, "isScreenMode": False, "isInterruptMode": True, "isAvatarScene": False,
                "avatarBgUrl": "", "runtimeMode": "agent_loop", "rtcAvailable": rtc is not None}}
            for item in model_menu()["profiles"] if item["configured"]]}}

    @app.post("/api/agent/{transport}/sessions")
    async def create(request: Request, transport: str):
        context = await auth.authorize(request)
        payload = await _body(request)
        if set(payload) - {"profile_id"} or ("profile_id" in payload and not isinstance(payload["profile_id"], str)):
            raise HTTPException(400, "不支持的会话参数。")
        # Recheck after receiving the body; logout must not admit a delayed request.
        context = await auth.authorize(request)
        if transport == "text":
            result = await text.create(payload.get("profile_id"))
        elif transport == "rtc" and rtc:
            result = await rtc.create(payload.get("profile_id"))
        elif transport == "rtc":
            raise HTTPException(503, "语音暂不可用，请使用文字对话。")
        else:
            raise HTTPException(404, "入口不存在。")
        try:
            return await bind(context, result)
        except BaseException:
            await close_session(result["session_id"])
            raise

    @app.get("/api/agent/{transport}/sessions/{session_id}")
    async def status(request: Request, transport: str, session_id: str):
        context = await auth.authorize(request)
        await check_owner(context, session_id)
        if transport == "text" and session_id in text.sessions:
            return await text.snapshot(await text.get(session_id, active=False))
        if transport == "rtc" and rtc and session_id in rtc.sessions:
            item = rtc.get(session_id)
            return {"session_id": session_id, "state": item.state, "expires_at": item.expires_at}
        raise HTTPException(404, "会话不存在。")

    @app.post("/api/agent/text/sessions/{session_id}/turns")
    async def turn(request: Request, session_id: str):
        await auth.authorize(request)
        payload = await _body(request)
        if set(payload) != {"round_id", "text"}:
            raise HTTPException(400, "不支持的输入参数。")
        round_id, user_text = _round(payload["round_id"]), _text(payload["text"])
        context = await auth.authorize(request)
        await check_owner(context, session_id)
        item = await text.get(session_id)
        handle = await runtime.accept(item.run_id, round_id, user_text)

        async def stream():
            try:
                yield _sse("turn", {"round_id": round_id, "turn_id": handle.target.turn_id, "epoch": handle.target.epoch})
                async for delta in runtime.speech(handle):
                    if not live(session_id):
                        break
                    yield _sse("delta", {"round_id": round_id, "text": delta})
                known = runtime.store.lookup_round(session_id, round_id)
                yield _sse("terminal", {"round_id": round_id, "state": known["status"] if known else "unknown",
                    "error_code": handle.failure_code})
            finally:
                if not handle.consumer_finished:
                    await runtime.disconnect(handle)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/agent/{transport}/sessions/{session_id}/{action}")
    async def action(request: Request, transport: str, session_id: str, action: str):
        await auth.authorize(request)
        payload = await _body(request)
        context = await auth.authorize(request)
        await check_owner(context, session_id)
        if transport == "text":
            item = await text.get(session_id, active=action != "stop")
            if action == "interrupt" and set(payload) == {"round_id"}:
                return {"interrupted": bool(await runtime.interrupt(item.run_id, _round(payload["round_id"])))}
            if payload:
                raise HTTPException(400, "不支持的操作参数。")
            if action == "heartbeat":
                return await text.heartbeat(item)
            if action == "stop":
                await text.stop(item)
                return {"stopped": True}
        elif transport == "rtc" and rtc:
            item = rtc.get(session_id)
            if payload:
                raise HTTPException(400, "不支持的操作参数。")
            if action == "start":
                result = await rtc.start(item)
                if not live(session_id) or not await auth.call(auth.active, context.login_id, context.user_id):
                    await revoke(context.login_id)
                    raise HTTPException(401, "登录已过期，请重新登录。")
                return {"running": result["running"]}
            if action == "stop":
                await runtime.close_session(session_id)
                result = await rtc.stop(item)
                return {"stopped": result["stopped"]}
            if action == "heartbeat" and item.state in {"created", "running"} and time.monotonic() < item.deadline:
                item.last_heartbeat = time.monotonic()
                return {"alive": True, "expires_at": item.expires_at}
        raise HTTPException(404, "操作不存在或会话已结束。")

    build = build_dir.resolve()

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith(("api/", "m0", "m1", "proxy")):
            raise HTTPException(404, "入口不存在。")
        target = (build / path).resolve()
        if target != build and build not in target.parents:
            raise HTTPException(404, "文件不存在。")
        if target.is_file():
            # Windows registry MIME associations can label .js as text/plain.
            # With nosniff enabled a browser then refuses the entire React app.
            media = {".js": "application/javascript", ".mjs": "application/javascript",
                ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml"}.get(target.suffix.lower())
            return FileResponse(target, media_type=media)
        if path not in {"", "login", "chat"} or not (build / "index.html").is_file():
            raise HTTPException(404, "页面不存在，请先构建 React 前端。")
        return FileResponse(build / "index.html")

    @asynccontextmanager
    async def lifespan(_app):
        await auth.call(auth.check_schema)
        if rtc:
            await rtc.close()  # Recover exact previous cloud identities before admission.

        async def reaper():
            while True:
                await asyncio.sleep(2)
                await text.reap()
                active_logins = {(o.login_id, o.user_id) for sid, o in owners.items()
                    if not runtime.sessions[sid].closed and o.login_id not in revoked}
                for login_id, user_id in active_logins:
                    try:
                        valid = await auth.call(auth.active, login_id, user_id)
                    except HTTPException:
                        valid = False
                    if not valid:
                        await revoke(login_id)
                if rtc:
                    await rtc.reap()

        task = asyncio.create_task(reaper())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await text.close()
            if rtc:
                await rtc.close()
            if cleanup:
                await asyncio.gather(*cleanup, return_exceptions=True)
            await runtime.aclose()
            if owns_store and not runtime.tasks and not runtime.control_tasks:
                runtime.store.close()
            auth.engine.dispose()

    app.router.lifespan_context = lifespan
    return app
