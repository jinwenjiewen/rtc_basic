"""Owned TCP text service + imported synthetic history + real segmented models.

Unlike the read-only context probe, this executable creates its own NEW database
and seeds closed fixture turns before model calls. It never attaches to another
service or database. Fixture acknowledgements are scripted; every maintenance,
decision and final request after import uses the selected real model profile.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import socket
import time

import httpx
import uvicorn

from ..config import Settings
from ..models import ModelProfiles, load_model_environment
from ..runtime import AgentRuntime, SYSTEM
from ..store import RuntimeStore
from ..text import create_text_app
from ..tools import ToolRegistry
from .context import (_filler, _history_checks, _history_target_evidence, _public_evidence,
                      _read_evidence, _semantic_checks)
from .text import (DEFAULT_PROFILES, ProbeFailure, _cleanup, _create, _failure, _hash,
                   _heartbeat, _page_key, _turn)


SEED_TURNS = 48
MAX_RECOVERY_TURNS = 6


def _seed(store: RuntimeStore, session_id: str, marker: str, filler_bytes: int) -> str:
    first_id = ""
    for index in range(SEED_TURNS):
        if index == 0:
            text = (f"合成会话标记{marker}。我的课程预算是5000元，不接受周末课程。"
                    "订单需要我明确确认后才能提交，现在尚未确认也尚未提交。不要写工作笔记。")
        elif index == 24:
            text = "修正：预算从之前金额改为3000元。不要周末的限制继续有效，订单仍待我确认，未提交。不要写工作笔记。"
        else:
            text = _filler(index, filler_bytes)
        target = store.admit_turn(session_id, "import-" + str(index), text, "fixture-" + str(index))
        store.complete_turn(target, "generation_completed", "收到")
        store.record_delivery(target, "收到", "delta")
        store.record_delivery(target, "", "completed")
        if not first_id:
            first_id = target.turn_id
    return first_id


def _staging_evidence(store: RuntimeStore, session_id: str) -> dict:
    db = store.db
    rows = db.execute("""SELECT candidate_id,parent_id,segment_after_seq,covered_through_seq,
        previous_compacted_through_seq,owner_epoch FROM context_candidates WHERE session_id=? ORDER BY rowid""", (session_id,)).fetchall()
    by_id = {row["candidate_id"]: row for row in rows}
    contiguous = all((row["segment_after_seq"] == (by_id[row["parent_id"]]["covered_through_seq"] if row["parent_id"] in by_id
                      else row["previous_compacted_through_seq"]) and row["covered_through_seq"] > row["segment_after_seq"]
                      and (row["parent_id"] is None or row["parent_id"] in by_id)) for row in rows)
    cross_epoch = any(row["parent_id"] in by_id and row["owner_epoch"] != by_id[row["parent_id"]]["owner_epoch"] for row in rows)
    active = store.get_context(session_id)
    return {"candidate_count": len(rows), "candidate_receipts_contiguous": contiguous,
            "completed_receipt_reused_in_new_epoch": cross_epoch,
            "active_context_version": active["context_version"], "active_compacted_through_seq": active["compacted_through_seq"],
            "active_snapshot_count": db.execute("SELECT COUNT(*) FROM context_snapshots WHERE session_id=?", (session_id,)).fetchone()[0],
            "staged_event_count": db.execute("SELECT COUNT(*) FROM ledger WHERE session_id=? AND kind='context.staged'", (session_id,)).fetchone()[0],
            "resume_event_count": db.execute("SELECT COUNT(*) FROM ledger WHERE session_id=? AND kind='context.stage_resumed'", (session_id,)).fetchone()[0]}


async def _case(client, runtime, database, profile, index, filler_bytes):
    report = {"profile_index": index, "profile_id": profile, "passed": False, "rounds": []}
    session_id, first_id = None, ""
    try:
        session_id, _ = await _create(client, profile)
        report["session_sha256"] = _hash(session_id)
        marker = "M2S-" + secrets.token_hex(8)
        first_id = _seed(runtime.store, session_id, marker, filler_bytes)
        session = runtime.sessions[session_id]
        last = runtime.store.lookup_round(session_id, "import-47")["target"]
        view = runtime._view(session, last, "", runtime._schemas(), allow_completed=True)
        report["import"] = {"scripted_closed_fixture_turns": SEED_TURNS,
            "scripted_fixture_acknowledgements": True, "filler_utf8_bytes": filler_bytes,
            "all_uncovered_input_tokens_estimate": view.metrics["input_tokens_estimate"],
            "input_budget_tokens": view.metrics["input_budget_tokens"],
            "hard_limit_exceeded_before_recovery": view.metrics["hard_limit_exceeded"]}

        async def send(purpose, text):
            await _heartbeat(client, session_id)
            result = await _turn(client, session_id, text)
            evidence = _staging_evidence(runtime.store, session_id)
            report["rounds"].append({"purpose": purpose, **result.metadata(), **evidence})
            return result, evidence

        for ordinal in range(MAX_RECOVERY_TURNS):
            result, stage = await send("recover_imported_history", "请继续整理当前会话上下文，保留之前的有效预算、否定和待确认状态。不要写工作笔记，完成后只回复收到。")
            if ordinal == 0:
                report["first_checkpoint"] = {**stage,
                    "unfinished_receipts_remained_inactive": stage["candidate_count"] > 0 and stage["active_snapshot_count"] == 0
                        and stage["active_context_version"] == 0 and stage["active_compacted_through_seq"] == 0,
                    "explicit_pending_terminal": result.error_code == "context_maintenance_pending"}
                if not report["first_checkpoint"]["unfinished_receipts_remained_inactive"]:
                    raise ProbeFailure("segment_probe_no_inactive_checkpoint")
                # All completed receipts are in SQLite. Discard the connection,
                # engine cache and tool registry while retaining HTTP ownership.
                # This is storage recovery, not a claim of product reconnect.
                for _ in range(300):
                    if not runtime.tasks:
                        break
                    await asyncio.sleep(.01)
                if runtime.tasks:
                    raise ProbeFailure("segment_probe_model_cleanup_pending")
                runtime.store.close()
                runtime.store = RuntimeStore(database)
                runtime.context_engines.clear()
                runtime.tools = ToolRegistry(runtime.store)
                report["storage_reopened_and_context_cache_cleared"] = True
            if result.state == "generation_completed" and not result.error_code:
                break
            if result.error_code != "context_maintenance_pending":
                raise ProbeFailure(result.error_code or "segment_probe_unexpected_terminal")
        else:
            raise ProbeFailure("segment_probe_recovery_turn_limit")

        semantic, _ = await send("semantic_after_segment_recovery",
            "根据此前会话，只输出JSON对象，字段为budget（最新预算数字）、weekend_allowed（是否接受周末课程，布尔）、order_submitted（订单是否已提交，布尔）。不要写工作笔记，不要增加其它字段或解释。")
        report["semantic_checks"] = _semantic_checks(semantic.text)
        original = _read_evidence(database, session_id, profile, first_id)
        history, _ = await send("original_history_after_segment_recovery",
            f"请明确调用context_read_history或context_search_history，读回标记{marker}对应的最早一条用户预算原话。不要仅凭摘要猜测。只输出JSON对象，字段initial_budget（最初预算数字）、source_ref（该原始用户消息的seq:N引用）。不要写工作笔记。")
        report["history_checks"] = _history_checks(history.text, original["_first_user_seq"])
        report["history_tool_evidence"] = _history_target_evidence(database, session_id, history.turn_id, original["_first_user_seq"])
        report["database"] = _public_evidence(_read_evidence(database, session_id, profile, first_id))
        report["staging"] = _staging_evidence(runtime.store, session_id)
        evidence, stage = report["database"], report["staging"]
        report["passed"] = bool(all(report["semantic_checks"].values()) and all(report["history_checks"].values())
            and semantic.state == history.state == "generation_completed"
            and report["history_tool_evidence"]["returned_original_user_record_with_budget"]
            and report["first_checkpoint"]["explicit_pending_terminal"] and report["storage_reopened_and_context_cache_cleared"]
            and stage["candidate_count"] >= 3 and stage["candidate_receipts_contiguous"]
            and stage["completed_receipt_reused_in_new_epoch"] and stage["candidate_count"] == stage["staged_event_count"]
            and evidence["notes_version"] == 0 and evidence["snapshot_count"] >= 1
            and evidence["snapshot_event_count_matches"] and evidence["compaction_metrics_show_bounded_reduction"]
            and evidence["invalid_source_ref_count"] == 0 and evidence["original_first_user_record_retained"]
            and evidence["model_profile_matches"] and evidence["profile_matches"]
            and evidence["max_observed_model_calls_per_turn"] <= runtime.max_steps
            and set(evidence["runtime_error_counts"]) <= {"context_maintenance_pending"})
        if not report["passed"]:
            report["failure"] = {"code": "segment_probe_expectation_failed"}
    except Exception as exc:
        report["failure"] = _failure(exc)
        if session_id is not None and first_id:
            report["database"] = _public_evidence(_read_evidence(database, session_id, profile, first_id))
    finally:
        if session_id is not None:
            report["staging"] = _staging_evidence(runtime.store, session_id)
            report["cleanup"] = await _cleanup(client, database, session_id, profile)
            report["passed"] = report["passed"] and report["cleanup"]["passed"]
    return report


async def run_probe(settings, profiles, database, *, port=3103, selected=DEFAULT_PROFILES, filler_bytes=1800):
    report = {"schema_version": 1, "probe": "m2_segmented_text_http", "passed": False,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {"synthetic_history_import": True, "fixture_acknowledgements_scripted": True,
            "post_import_models_real": True, "owned_tcp_service_only": True, "rtc_used": False,
            "storage_reopen_is_not_product_reconnect": True, "timings_are_not_audio_latency": True},
        "limits": {"max_profiles": 2, "max_recovery_turns_per_profile": MAX_RECOVERY_TURNS,
            "max_verification_turns_per_profile": 2, "profile_fallback": False, "automatic_retries": 0}, "profiles": []}
    started = time.perf_counter()
    runtime, server, task, listener = None, None, None, None
    try:
        if (not 1 <= len(selected) <= 2 or len(set(selected)) != len(selected)
            or not 400 <= filler_bytes <= 6000 or not 1024 <= port <= 65535):
            raise ProbeFailure("invalid_segment_probe_options")
        for profile in selected:
            profiles.get(profile).validate_ready()
        if database.exists():
            raise ProbeFailure("segment_probe_requires_new_database")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", port))
        listener.listen(32)
        listener.setblocking(False)
        database.parent.mkdir(parents=True, exist_ok=True)
        with database.open("xb"):
            pass
        runtime = AgentRuntime(RuntimeStore(database), profiles,
            max_steps=settings.m1_max_steps, max_tool_calls=settings.m1_max_tool_calls,
            wall_seconds=settings.m1_wall_seconds, first_output_ms=settings.m1_first_output_deadline_ms,
            **settings.context_options())
        if runtime.background_context_enabled:
            raise ProbeFailure("segment_probe_requires_background_disabled")
        report["configuration"] = {"input_cap_estimated_tokens": settings.m2_context_input_tokens,
            "first_output_deadline_ms": runtime.first_output_ms, "wall_seconds": runtime.wall_seconds,
            "max_model_calls_per_turn": runtime.max_steps, "max_native_tools_per_turn": runtime.max_tool_calls,
            "keep_recent_turns": settings.m2_context_keep_recent_turns, "background_enabled": False}
        app = create_text_app(settings, profiles, database, port=port, runtime=runtime)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=3))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        for _ in range(300):
            if server.started or task.done():
                break
            await asyncio.sleep(.01)
        if not server.started:
            raise ProbeFailure("segment_probe_service_start_failed")
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=45) as client:
            client.headers["X-M0-Client-Key"] = await _page_key(client)
            for index, profile in enumerate(selected):
                result = await _case(client, runtime, database, profile, index, filler_bytes)
                report["profiles"].append(result)
                print(json.dumps({"profile_index": index, "passed": result["passed"],
                    "failure": result.get("failure"), "rounds": len(result["rounds"])}), flush=True)
        report["cleanup_confirmed"] = all(r.get("cleanup", {}).get("passed") for r in report["profiles"])
        report["passed"] = report["cleanup_confirmed"] and all(r["passed"] for r in report["profiles"])
    except Exception as exc:
        report["failure"] = _failure(exc)
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
        report["owned_service_stopped"] = task is None or task.done()
    report["latency_observations"] = {
        "default_9s_configuration": settings.m1_first_output_deadline_ms == 9000,
        "production_latency_slo_passed": False,
        "fallback_deltas_are_not_model_first_output": True,
        "all_import_recovery_turns_generated_model_replies": bool(report["profiles"]) and all(
            turn["state"] == "generation_completed" and not turn["error_code"]
            for case in report["profiles"] for turn in case["rounds"] if turn["purpose"] == "recover_imported_history")}
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return report


def main():
    parser = argparse.ArgumentParser(description="Isolated real text test of segmented M2 recovery")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--models-file", type=Path, default=Path("backend_harness_loop/model_profiles.json"))
    parser.add_argument("--model-env-file", type=Path, default=Path("backend_harness_loop/.env"))
    parser.add_argument("--first-output-ms", type=int, default=20000)
    parser.add_argument("--port", type=int, default=3103)
    parser.add_argument("--profile", action="append")
    args = parser.parse_args()
    if args.output.exists():
        print("segment_probe_requires_new_output")
        return 2
    try:
        profiles = ModelProfiles.load(args.models_file, environ=load_model_environment(args.model_env_file))
        settings = replace(Settings.load(None), m1_first_output_deadline_ms=args.first_output_ms, m2_context_background=False)
        report = asyncio.run(run_probe(settings, profiles, args.database, port=args.port, selected=args.profile or DEFAULT_PROFILES))
    except Exception as exc:
        report = {"schema_version": 1, "probe": "m2_segmented_text_http", "passed": False, "failure": _failure(exc)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print("segment_probe_passed" if report["passed"] else "segment_probe_failed")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
