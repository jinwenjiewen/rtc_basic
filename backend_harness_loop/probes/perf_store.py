"""Bounded local scaling measurements; imported archive/summary are scripted.

No cloud models or existing databases. Every sample is retained, including the
first. Trace callbacks count SQL statements without recording their contents.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import statistics
import time

from ..models import ChatCompletionsModel, ModelClientPool, ModelProfile, ModelProfiles
from ..runtime import AgentRuntime, SYSTEM
from ..store import RuntimeStore
from .text import ProbeFailure


def _measure(store, function, samples):
    timings, counts = [], []
    for _ in range(samples):
        count = [0]
        store.db.set_trace_callback(lambda statement: count.__setitem__(0, count[0] + 1))
        started = time.perf_counter()
        try:
            function()
        finally:
            timings.append(round((time.perf_counter() - started) * 1000, 3))
            counts.append(count[0])
            store.db.set_trace_callback(None)
    return {"samples_ms": timings, "sql_statements": counts, "median_ms": round(statistics.median(timings), 3), "max_ms": max(timings)}


async def _transport_setup(samples):
    profile = ModelProfile("fixture", "fixture", "https://fixture.example", "fixture", "KEY", "unused")
    unpooled, pooled = [], []
    for _ in range(samples):
        started = time.perf_counter()
        model = ChatCompletionsModel(profile, trust_env=False)
        unpooled.append(round((time.perf_counter()-started)*1000, 3))
        await model.aclose()
    pool = ModelClientPool(trust_env=False)
    try:
        for _ in range(samples):
            started = time.perf_counter()
            model = pool.model(profile)
            pooled.append(round((time.perf_counter()-started)*1000, 3))
            await model.aclose()
    finally:
        await pool.aclose()
    return {"unpooled_setup_ms": unpooled, "pooled_lease_ms": pooled, "first_pool_setup_included": True, "network_requests": 0}


def run_case(path, turns, samples):
    if path.exists():
        raise ProbeFailure("perf_store_requires_new_database")
    store = RuntimeStore(path)
    profile = ModelProfile("fixture", "fixture", "https://fixture.example", "deepseek-v4-pro", "UNUSED_KEY", "unused-fixture-key",
        extra_body={"thinking": {"type": "disabled"}}, decision_tool_choice="required")
    runtime = AgentRuntime(store, ModelProfiles({"fixture": profile}, "fixture"), background_context_enabled=False, first_output_ms=9000)
    runtime.register_session("fixture-session", "fixture-run", "fixture", "fixture-task", "fixture-user")
    started = time.perf_counter()
    try:
        with store._transaction():
            for index in range(turns):
                owner = store.admit_turn("fixture-session", str(index), "预算3000，周末不要，订单未确认；合成临时讨论。" + "x" * 300, "fixture-" + str(index))
                store.complete_turn(owner, "generation_completed", "收到")
                store.record_delivery(owner, "收到", "delta")
                store.record_delivery(owner, "", "completed")
        text = "请回顾已有约束。"
        owner = store.admit_turn("fixture-session", "current", text, "fixture-current")
        material = store.context_material("fixture-session", owner.turn_id)
        boundary = material["groups"][-5]["through_seq"]
        source = material["groups"][0]["messages"][0]["reference"]
        summary = {key: [] for key in ("goals", "items", "open_questions", "pending_actions", "topic_index")}
        summary["items"] = [{"id": "constraint", "value": "预算3000，周末不要，订单未确认", "source_refs": [source]}]
        frozen = {"ledger_through_seq": material["ledger_through_seq"], "eligible_through_seq": boundary,
            "previous_compacted_through_seq": 0, "expected_notes_version": 0, "material_fingerprint": material["material_fingerprint"],
            "visible_refs": [m["reference"] for g in material["groups"] for m in g["messages"]], "protected_refs": []}
        store.commit_context(owner, 0, boundary, summary, [], input_ref=frozen)
        session = runtime.binding("fixture-run")
        engine, schemas = runtime._engine(session), runtime._schemas()
        setup_ms = round((time.perf_counter()-started)*1000, 3)

        def preflight():
            view = runtime._view(session, owner, text, schemas)
            plan, actual_tools, actual, edit = runtime._decision_context(session, owner, text, [], view)
            runtime._check_request(session, actual.messages, actual_tools)

        result = {"scripted_closed_turns": turns, "setup_ms_excluded_from_samples": setup_ms,
            "scripted_archival_snapshot": True, "cloud_model_calls": 0,
            "material": _measure(store, lambda: store.context_material("fixture-session", owner.turn_id), samples),
            "view": _measure(store, lambda: runtime._view(session, owner, text, schemas), samples),
            "preflight": _measure(store, preflight, samples)}
        view = runtime._view(session, owner, text, schemas)
        result["invariants"] = {"complete_material_groups": len(view.material["groups"]),
            "uncovered_raw_turns": view.metrics["recent_turn_count"], "input_tokens_estimate": view.metrics["input_tokens_estimate"],
            "input_budget_tokens": view.metrics["input_budget_tokens"], "hard_limit_exceeded": view.metrics["hard_limit_exceeded"],
            "raw_user_records_retained": store.db.execute("SELECT COUNT(*) FROM ledger WHERE session_id='fixture-session' AND role='user'").fetchone()[0] == turns+1}
        return result
    finally:
        store.close_session("fixture-session")
        store.close()


def main():
    parser = argparse.ArgumentParser(description="Local M2 long-ledger scaling benchmark without cloud models")
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--turns", type=int, action="append")
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()
    counts = args.turns or [100, 500, 1000]
    if args.output.exists() or not 1 <= args.samples <= 5 or len(counts) > 3 or any(not 12 <= n <= 1800 for n in counts):
        print("invalid_perf_store_options")
        return 2
    args.directory.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "probe": "m2_local_store_performance", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(), "scope": "Scripted ledger + archival snapshot, no semantic/real-model or audio claim",
        "samples_per_operation": args.samples, "warmup_discarded": False, "cases": [], "passed": False}
    try:
        for count in counts:
            result = run_case(args.directory/f"turns-{count}.sqlite3", count, args.samples)
            report["cases"].append(result)
            print(json.dumps({"turns": count, "preflight": result["preflight"]}), flush=True)
        report["passed"] = all(c["invariants"]["uncovered_raw_turns"] == 4 and c["invariants"]["raw_user_records_retained"]
            and not c["invariants"]["hard_limit_exceeded"] for c in report["cases"])
        report["transport_setup"] = asyncio.run(_transport_setup(args.samples))
    except Exception:
        report["failure"] = {"code": "perf_store_measurement_failed"}
    report["source_sha256"] = {name: hashlib.sha256((Path("backend_harness_loop")/name).read_bytes()).hexdigest()
        for name in ("store.py", "context.py", "runtime.py", "models.py", "probes/perf_store.py")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
