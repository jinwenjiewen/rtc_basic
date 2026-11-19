"""M1 RTC boundary: authenticated callbacks -> owned Agent Loop -> speech SSE."""

from __future__ import annotations

from pathlib import Path
import time

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from .client import create_client_app
from .config import Settings
from .protocol import encode_sse_chunk, encode_sse_end
from .rtc_contract import ContractError, extract_interrupt, validate_callback
from .runtime import AgentRuntime, RuntimeFault
from .store import RuntimeStore, StoreError


def create_agent_app(settings: Settings, profiles, database_path: Path, output_path: Path,
                     public_url: str, *, port: int = 3101, runtime: AgentRuntime | None = None):
    owns_store = runtime is None
    if runtime is None:
        runtime = AgentRuntime(
            RuntimeStore(database_path), profiles,
            model_trust_env=settings.trust_env,
            max_steps=settings.m1_max_steps, max_tool_calls=settings.m1_max_tool_calls,
            wall_seconds=settings.m1_wall_seconds, first_output_ms=settings.m1_first_output_deadline_ms,
            **settings.context_options(),
            **settings.business_tool_options(),
        )

    async def chat(request: Request, payload: dict):
        try:
            run_id = request.query_params.get("rtc_run_id", "")
            if not run_id:
                raise RuntimeFault("rtc_run_required", 400)
            # Never select a model, instructions or history from provider input.
            runtime.binding(run_id)
            round_id, user_text = validate_callback(payload)
            handle = await runtime.accept(run_id, round_id, user_text)
        except (ContractError, RuntimeFault) as exc:
            raise HTTPException(exc.status, {"code": exc.code}) from None
        except StoreError as exc:
            raise HTTPException(409, {"code": exc.code}) from None
        created = int(time.time())
        stream_id = "m1_" + handle.target.turn_id

        async def stream():
            try:
                async for text in runtime.speech(handle):
                    yield encode_sse_chunk(text, stream_id=stream_id, created=created, model="rtc-agent-loop")
                if handle.valid:
                    yield encode_sse_end(stream_id=stream_id, created=created, model="rtc-agent-loop")
            finally:
                if not handle.consumer_finished:
                    await runtime.disconnect(handle)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    async def event(request: Request, tag: str | None, payload: dict):
        run_id = request.query_params.get("rtc_run_id", "")
        try:
            binding = runtime.binding(run_id)
        except RuntimeFault:
            return
        round_id = extract_interrupt(tag, payload, binding.task_id, binding.user_id)
        if round_id is not None and not binding.closed:
            await runtime.interrupt(run_id, round_id)

    app = create_client_app(settings, output_path, public_url, port=port, runtime=runtime,
                            chat_handler=chat, event_handler=event)
    app.state.agent_runtime = runtime
    if owns_store:
        original_lifespan = app.router.lifespan_context
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(inner_app):
            try:
                async with original_lifespan(inner_app):
                    yield
            finally:
                if not runtime.tasks and not runtime.control_tasks:
                    runtime.store.close()
        app.router.lifespan_context = lifespan
    return app
