"""An isolated, owned TCP text service for bounded integration probes."""

import asyncio
from contextlib import asynccontextmanager
import socket

import httpx
import uvicorn

from ..runtime import AgentRuntime
from ..store import RuntimeStore
from ..text import create_text_app
from .text import ProbeFailure, _page_key


@asynccontextmanager
async def owned_text_service(settings, profiles, database, *, port=3103, model_factory=None, allow_background=False,
                             knowledge=None):
    if database.exists():
        raise ProbeFailure("probe_requires_new_database")
    if not 1024 <= port <= 65535 or settings.m2_context_background and not allow_background:
        raise ProbeFailure("invalid_owned_text_service_options")
    listener, runtime, server, task = None, None, None, None
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", port))
        listener.listen(32)
        listener.setblocking(False)
        database.parent.mkdir(parents=True, exist_ok=True)
        with database.open("xb"):
            pass
        business_options = {"knowledge": knowledge, "business_policy": settings.business_policy} if knowledge is not None else settings.business_tool_options()
        runtime = AgentRuntime(RuntimeStore(database), profiles, model_factory=model_factory,
            max_steps=settings.m1_max_steps, max_tool_calls=settings.m1_max_tool_calls,
            wall_seconds=settings.m1_wall_seconds, first_output_ms=settings.m1_first_output_deadline_ms,
            **settings.context_options(), **business_options)
        app = create_text_app(settings, profiles, database, port=port, runtime=runtime)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=3))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        for _ in range(300):
            if server.started or task.done():
                break
            await asyncio.sleep(.01)
        if not server.started:
            raise ProbeFailure("owned_text_service_start_failed")
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=45) as client:
            client.headers["X-M0-Client-Key"] = await _page_key(client)
            yield runtime, client
    finally:
        if server is not None:
            server.should_exit = True
        if task is not None:
            await asyncio.wait_for(task, 8)
        if listener is not None:
            listener.close()
        if runtime is not None:
            await runtime.aclose()
            if not runtime.tasks and not runtime.control_tasks:
                runtime.store.close()
