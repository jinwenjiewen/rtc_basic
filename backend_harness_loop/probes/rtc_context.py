"""Explicit owned RTC validation app; never imported by the normal server.

Seeds scripted closed history, not a summary. Real RTC callbacks, compaction,
tools, final text and TTS use the selected session-pinned profile. Extra routes
reuse the browser's complete loopback/Host/Origin/nonce boundary. No credentials
or conversation text are returned by validation controls.
"""

import argparse
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi import HTTPException, Request

from ..app import create_agent_app
from ..capture import _json_body
from ..config import Settings
from ..legacy import load_legacy_voice_profile
from ..models import ModelProfiles, load_model_environment
from ..runtime import AgentRuntime
from ..store import RuntimeStore
from .context import _filler
from .performance import _seed_background
from .rtc import call_openapi


class RTCValidationRuntime(AgentRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixture_sessions = set()
        self.pressure_appended = set()

    def register_session(self, session_id, run_id, profile_id, task_id, user_id):
        super().register_session(session_id, run_id, profile_id, task_id, user_id)
        if session_id.startswith("m0_session_") and session_id not in self.fixture_sessions:
            if self.store.snapshot(session_id)["message_count"]:
                raise ValueError("rtc_probe_requires_empty_owned_session")
            _seed_background(self.store, session_id)
            self.fixture_sessions.add(session_id)

    def append_pressure(self, session_id):
        if session_id not in self.fixture_sessions:
            raise HTTPException(409, "Not an owned RTC fixture session")
        if session_id in self.pressure_appended:
            raise HTTPException(409, "Pressure fixture was already appended")
        session = self.sessions[session_id]
        current = session.current
        if (session.closed or not session.inbox.empty() or current is None
            or not current.consumer_finished or current.task is None or not current.task.done()
            or session.maintenance is not None and not session.maintenance.done()):
            raise HTTPException(409, "Wait for the owned reply and maintenance to settle")
        # Synchronous, same runtime writer, no model or tool awaits. Next real
        # callback acquires a fresh epoch. The original RTC round is retained.
        for index in range(4):
            owner = self.store.admit_turn(session_id, "rtc-fixture-tail-"+str(index), _filler(index, 1800), "rtc-fixture-tail-"+str(index))
            self.store.complete_turn(owner, "generation_completed", "收到")
            self.store.record_delivery(owner, "收到", "delta")
            self.store.record_delivery(owner, "", "completed")
        self.pressure_appended.add(session_id)
        return {"scripted_closed_turns_appended": 4, "summary_imported": False}


def create_rtc_context_app(settings, profiles, database, output, public_url, *, port=3101, runtime=None):
    owns_store = runtime is None
    runtime = runtime or RTCValidationRuntime(RuntimeStore(database), profiles,
        model_trust_env=settings.trust_env, max_steps=settings.m1_max_steps,
        max_tool_calls=settings.m1_max_tool_calls, wall_seconds=settings.m1_wall_seconds,
        first_output_ms=settings.m1_first_output_deadline_ms, **settings.context_options())
    app = create_agent_app(settings, profiles, database, output, public_url, port=port, runtime=runtime)
    if owns_store:
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(inner_app):
            try:
                async with original_lifespan(inner_app):
                    yield
            finally:
                if not runtime.tasks and not runtime.control_tasks:
                    runtime.store.close()
        app.router.lifespan_context = lifespan

    @app.post("/api/m2/rtc-validation/{session_id}")
    async def control(request: Request, session_id: str):
        app.state.authorize_browser(request)
        payload = await _json_body(request)
        if (set(payload) != {"step"} or not isinstance(payload["step"], str)
            or payload["step"] not in {"pressure_tail", "recall", "history", "long_reply", "new_input"}):
            raise HTTPException(400, "Unknown bounded RTC validation step")
        session = app.state.client_sessions.get(session_id)
        if session.state != "running" or session_id not in runtime.fixture_sessions:
            raise HTTPException(409, "Owned RTC validation session is not running")
        if payload["step"] == "pressure_tail":
            return runtime.append_pressure(session_id)
        first_user = runtime.store.db.execute("SELECT MIN(seq) FROM ledger WHERE session_id=? AND role='user'", (session_id,)).fetchone()[0]
        messages = {
            "recall": "请只输出JSON对象，budget为最新预算整数、weekend_allowed为布尔、order_submitted为布尔。不要修改摘要或笔记。",
            "history": f"请实际调用context_read_history按record_seq={first_user}读回最初用户预算原话。只输出JSON对象，initial_budget为最初预算整数，source_ref为原始用户seq:N。不要改摘要或笔记。",
            "long_reply": "请连续用约三百字解释为什么学习需要循序渐进，只说说明正文，不要修改既有预算等约束。",
            "new_input": "最新预算修正为3200元，仍不要周末，订单未确认未提交。请简短确认，不要修改工作笔记。",
        }
        async with httpx.AsyncClient(timeout=settings.rtc_timeout_seconds, trust_env=settings.trust_env, follow_redirects=False) as client:
            result = await call_openapi(client, settings, "UpdateVoiceChat", {
                **session.identity, "Command": "ExternalTextToLLM", "Message": messages[payload["step"]], "InterruptMode": 1})
        return {"accepted": bool(result.get("ok")), "provider": result, "step": payload["step"]}

    return app


def main():
    parser = argparse.ArgumentParser(description="Owned M2 RTC compaction validation service")
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--port", default=3101, type=int)
    parser.add_argument("--env-file", default=Path("rag_llm_server/.env"), type=Path)
    parser.add_argument("--overlay-env-file", default=Path("backend_harness_loop/.local/client-credentials.env"), type=Path)
    parser.add_argument("--models-file", default=Path("backend_harness_loop/model_profiles.json"), type=Path)
    parser.add_argument("--model-env-file", default=Path("backend_harness_loop/.env"), type=Path)
    args = parser.parse_args()
    if args.database.exists() or any(p.exists() for p in (args.output, args.output.with_suffix(".sessions.json"))):
        print("rtc_validation_requires_new_artifacts")
        return 2
    settings = Settings.load(args.env_file, overlay_file=args.overlay_env_file)
    settings = replace(settings, rtc_legacy_voice_config=load_legacy_voice_profile(Path("rag_llm_server/main.py")))
    if (settings.m1_first_output_deadline_ms != 9000 or settings.m2_context_input_tokens != 24576
        or settings.m2_context_background_seconds != 5 or not settings.m2_context_background):
        print("rtc_validation_requires_default_budgets")
        return 2
    profiles = ModelProfiles.load(args.models_file, environ=load_model_environment(args.model_env_file))
    app = create_rtc_context_app(settings, profiles, args.database, args.output, args.public_url, port=args.port)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
