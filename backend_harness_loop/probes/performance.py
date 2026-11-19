"""Default-budget text/maintenance performance, using only an owned service.

Long dialogue is real HTTP/model traffic. Background-only/preemption cases
import explicitly scripted closed history; summaries always use real models.
No retries, profile fallback, budget extension, or fallback-as-first-text pass.
"""

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import time

from ..config import Settings
from ..models import ModelProfiles, load_model_environment
from ._text_server import owned_text_service
from .context import _filler, _semantic_checks, _history_checks, _history_target_evidence
from .text import BASE, DEFAULT_PROFILES, ProbeFailure, _cleanup, _create, _failure, _heartbeat, _json, _turn


BACKGROUND_FIXTURE_BYTES = 850


def _evidence(store, session_id, profile):
    db = store.db
    firsts = [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM ledger WHERE session_id=? AND kind='model.first_output' ORDER BY seq", (session_id,))]
    maintenance = []
    for kind, payload in db.execute("SELECT kind,payload_json FROM ledger WHERE session_id=? AND kind LIKE 'context.maintenance_%' ORDER BY seq", (session_id,)):
        data = json.loads(payload)
        maintenance.append({"kind": kind, **{key: data[key] for key in ("code", "elapsed_ms", "planning_ms", "staged", "max_model_steps", "wall_seconds", "output_tokens", "stage", "exception_type", "validation_detail") if key in data}})
    model_calls, wrong_profiles = db.execute("SELECT COUNT(*),COALESCE(SUM(json_extract(payload_json,'$.profile_id') IS NOT ?),0) FROM ledger WHERE session_id=? AND kind='model.started'", (profile, session_id)).fetchone()
    phases = [{"phase": phase, "count": count} for phase,count in db.execute("SELECT json_extract(payload_json,'$.phase'),COUNT(*) FROM ledger WHERE session_id=? AND kind='model.started' GROUP BY 1", (session_id,))]
    errors = [{"code": code, "count": count} for code,count in db.execute("SELECT json_extract(payload_json,'$.code'),COUNT(*) FROM ledger WHERE session_id=? AND kind='runtime.error' GROUP BY 1", (session_id,))]
    metrics = [dict(zip(("before_estimate", "after_estimate", "budget", "target_met"), row)) for row in db.execute("SELECT json_extract(input_ref_json,'$.estimate.before_tokens'),json_extract(input_ref_json,'$.estimate.after_tokens'),json_extract(input_ref_json,'$.estimate.budget_tokens'),json_extract(input_ref_json,'$.estimate.target_met') FROM context_snapshots WHERE session_id=? ORDER BY context_version", (session_id,))]
    return {"model_first_outputs": firsts, "model_profile_matches": wrong_profiles == 0, "model_calls": model_calls,
        "completed_model_phases": [{"phase": row[0], "elapsed_ms": row[1]} for row in db.execute(
            "SELECT json_extract(payload_json,'$.phase'),json_extract(payload_json,'$.elapsed_ms') FROM ledger WHERE session_id=? AND kind='model.completed' ORDER BY seq", (session_id,))],
        "max_model_calls_per_turn": db.execute("SELECT MAX(n) FROM (SELECT COUNT(*) n FROM ledger WHERE session_id=? AND kind='model.started' GROUP BY turn_id)", (session_id,)).fetchone()[0] or 0,
        "max_foreground_model_calls_per_turn": db.execute("SELECT MAX(n) FROM (SELECT COUNT(*) n FROM ledger WHERE session_id=? AND kind='model.started' AND json_extract(payload_json,'$.phase')!='background_compact' GROUP BY turn_id)", (session_id,)).fetchone()[0] or 0,
        "max_background_model_calls_per_turn": db.execute("SELECT MAX(n) FROM (SELECT COUNT(*) n FROM ledger WHERE session_id=? AND kind='model.started' AND json_extract(payload_json,'$.phase')='background_compact' GROUP BY turn_id)", (session_id,)).fetchone()[0] or 0,
        "phase_counts": phases, "runtime_errors": errors, "maintenance": maintenance,
        "candidate_observations": [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM ledger WHERE session_id=? AND kind='context.candidate_observed' ORDER BY seq", (session_id,))],
        "snapshot_count": len(metrics), "snapshot_metrics": metrics,
        "notes_version": store.get_notes(session_id)["version"],
        "context_edit_count": db.execute("SELECT COUNT(*) FROM context_edits WHERE session_id=?", (session_id,)).fetchone()[0],
        "user_records": db.execute("SELECT COUNT(*) FROM ledger WHERE session_id=? AND kind='message' AND role='user'", (session_id,)).fetchone()[0]}


async def _settle(runtime, session_id):
    started = time.perf_counter()
    session = runtime.sessions[session_id]
    task = session.maintenance
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), runtime.background_context_seconds + 1)
        except asyncio.CancelledError:
            pass
    return round((time.perf_counter()-started)*1000, 3)


def _first_output(store, turn_id):
    row = store.db.execute("SELECT payload_json FROM ledger WHERE turn_id=? AND kind='model.first_output' LIMIT 1", (turn_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _working_view_checks(store, session_id, semantic_turn_id):
    """A correct JSON answer cannot mask a summary miss with other storage."""
    history_calls = store.db.execute("SELECT COUNT(*) FROM tool_calls WHERE turn_id=? AND name IN ('context_read_history','context_search_history')",
        (semantic_turn_id,)).fetchone()[0]
    edits = store.db.execute("SELECT COUNT(*) FROM context_edits WHERE session_id=?", (session_id,)).fetchone()[0]
    return {"semantic_without_history_tools": history_calls == 0,
        "no_notes_substitute": store.get_notes(session_id)["version"] == 0, "no_context_edit_substitute": edits == 0}


async def _send(client, runtime, session_id, report, purpose, text, *, after_turn=None):
    await _heartbeat(client, session_id)
    result = await _turn(client, session_id, text, after_turn=after_turn)
    first = _first_output(runtime.store, result.turn_id)
    report["rounds"].append({"purpose": purpose, **result.metadata(), "model_first_output": first,
        "model_deadline_met": bool(first and first["elapsed_ms"] <= 9000),
        "fallback_not_counted": first is None and result.delta_count > 0})
    return result


async def _long_case(client, runtime, database, profile, turns):
    report = {"scenario": "continuous_long_dialogue", "profile_id": profile, "passed": False, "rounds": [],
              "cadence": "Wait for each scheduled idle maintenance to finish, capped by its unchanged budget; no model retries"}
    session_id = None
    try:
        session_id, _ = await _create(client, profile)
        marker = "M2P-" + secrets.token_hex(8)
        initial = await _send(client, runtime, session_id, report, "initial_constraints",
            f"合成标记{marker}。预算5000元，不接受周末，订单尚未确认未提交。不要调用context_update或context_update_notes预写摘要；只回复收到。")
        first_user = runtime.store.db.execute("SELECT seq FROM ledger WHERE turn_id=? AND role='user'", (initial.turn_id,)).fetchone()[0]
        await _settle(runtime, session_id)
        await _send(client, runtime, session_id, report, "corrected_constraints", "预算修正为3000元，不要周末，订单仍未确认未提交。不要调用context_update或context_update_notes；只回复收到。")
        await _settle(runtime, session_id)
        for index in range(turns-4):
            result = await _send(client, runtime, session_id, report, "pressure_material", _filler(index, 1800) + "不要调用context_update或context_update_notes预写摘要。")
            report["rounds"][-1]["idle_wait_ms"] = await _settle(runtime, session_id)
            if (index+1) % 5 == 0:
                print(json.dumps({"profile": profile, "scenario": "long", "real_turns": len(report["rounds"])}), flush=True)
            if sum(r["state"] != "generation_completed" for r in report["rounds"]) >= 3:
                raise ProbeFailure("performance_long_failure_limit")
        semantic = await _send(client, runtime, session_id, report, "semantic_recall",
            "只输出JSON对象，字段budget（最新预算整数）、weekend_allowed（布尔）、order_submitted（布尔）。不要修改工作视图或写笔记。")
        report["semantic_checks"] = _semantic_checks(semantic.text)
        await _settle(runtime, session_id)
        history = await _send(client, runtime, session_id, report, "original_history",
            f"请调用context_read_history按record_seq={first_user}读回最初用户预算原话。仅输出JSON对象，字段initial_budget（最初预算整数）、source_ref（原始用户seq:N）。不要修改工作视图或写笔记。")
        report["history_checks"] = _history_checks(history.text, first_user)
        report["history_tool_evidence"] = _history_target_evidence(database, session_id, history.turn_id, first_user)
        await _settle(runtime, session_id)
        evidence = _evidence(runtime.store, session_id, profile)
        report["database"] = evidence
        report["passed"] = bool(all(r["state"] == "generation_completed" and not r["error_code"] and r["model_deadline_met"] for r in report["rounds"])
            and len(report["rounds"]) == turns and evidence["snapshot_count"] >= 2
            and all(report["semantic_checks"].values()) and all(report["history_checks"].values())
            and report["history_tool_evidence"]["returned_original_user_record_with_budget"]
            and evidence["notes_version"] == evidence["context_edit_count"] == 0
            and evidence["model_profile_matches"] and evidence["max_foreground_model_calls_per_turn"] <= 4
            and evidence["max_background_model_calls_per_turn"] <= 1)
        if not report["passed"]:
            report["failure"] = {"code": "performance_long_expectation_failed"}
    except Exception as exc:
        report["failure"] = _failure(exc)
    finally:
        if session_id is not None:
            report["cleanup"] = await _cleanup(client, database, session_id, profile)
            await _settle(runtime, session_id)
            report["database"] = _evidence(runtime.store, session_id, profile)
            report["passed"] = report["passed"] and report["cleanup"]["passed"]
    return report


def _seed_background(store, session_id):
    for index in range(14):
        if index == 0:
            text = "合成原始预算5000元，不要周末，订单未确认未提交。"
        elif index == 1:
            text = "修正预算3000元，不要周末，订单未确认未提交。"
        else:
            text = _filler(index, BACKGROUND_FIXTURE_BYTES)
        owner = store.admit_turn(session_id, "fixture-"+str(index), text, "fixture-c"+str(index))
        store.complete_turn(owner, "generation_completed", "收到")
        store.record_delivery(owner, "收到", "delta")
        store.record_delivery(owner, "", "completed")


async def _background_case(client, runtime, database, profile, action):
    report = {"scenario": "background_"+action, "profile_id": profile, "passed": False, "rounds": [],
              "scripted_closed_fixture_turns": 14, "scripted_pressure_filler_bytes": BACKGROUND_FIXTURE_BYTES}
    session_id = None
    try:
        session_id, _ = await _create(client, profile)
        _seed_background(runtime.store, session_id)
        trigger = await _send(client, runtime, session_id, report, "trigger_idle", "只回复收到，不修改摘要或笔记。")
        session = runtime.sessions[session_id]
        old = session.current
        task = session.maintenance
        report["foreground_trigger_succeeded"] = trigger.state == "generation_completed" and report["rounds"][-1]["model_deadline_met"]
        if task is None:
            raise ProbeFailure("performance_background_not_scheduled")
        if action == "idle":
            report["idle_wait_ms"] = await _settle(runtime, session_id)
            committed = runtime.store.snapshot(session_id)["context_compaction_count"] == 1
            if committed:
                semantic = await _send(client, runtime, session_id, report, "semantic_after_background",
                    "只用已给工作视图输出JSON对象，字段budget（最新预算整数）、weekend_allowed（布尔）、order_submitted（布尔）。不要回查历史、修改摘要或写笔记。")
                report["semantic_checks"] = _semantic_checks(semantic.text)
                first_user = runtime.store.db.execute("SELECT MIN(seq) FROM ledger WHERE session_id=? AND role='user'", (session_id,)).fetchone()[0]
                history = await _send(client, runtime, session_id, report, "history_after_background",
                    f"请调用context_read_history按record_seq={first_user}读取最初预算原话。只输出JSON对象，initial_budget为最初预算整数、source_ref为原始用户seq:N。不要修改摘要或笔记。")
                report["history_checks"] = _history_checks(history.text, first_user)
                report["history_tool_evidence"] = _history_target_evidence(database, session_id, history.turn_id, first_user)
                report["working_view_checks"] = _working_view_checks(runtime.store, session_id, semantic.turn_id)
            report["passed"] = bool(report["foreground_trigger_succeeded"] and committed
                and all(report.get("semantic_checks", {}).values()) and report.get("semantic_checks")
                and all(report.get("history_checks", {}).values()) and report.get("history_checks")
                and report.get("history_tool_evidence", {}).get("returned_original_user_record_with_budget")
                and report.get("working_view_checks") and all(report["working_view_checks"].values())
                and all(r["state"] == "generation_completed" and r["model_deadline_met"] for r in report["rounds"]))
        else:
            for _ in range(200):
                entered = runtime.store.db.execute("SELECT COUNT(*) FROM ledger WHERE turn_id=? AND kind='model.started' AND json_extract(payload_json,'$.phase')='background_compact'", (trigger.turn_id,)).fetchone()[0]
                if entered or task.done():
                    break
                await asyncio.sleep(.01)
            if not entered or task.done():
                raise ProbeFailure("performance_background_not_inflight")
            started = time.perf_counter()
            if action == "preempt":
                async def admitted():
                    report["admission_ack_ms"] = round((time.perf_counter()-started)*1000, 3)
                await _send(client, runtime, session_id, report, "new_input_preempts", "这是新输入，只回复收到，不修改摘要或笔记。", after_turn=admitted)
            else:
                endpoint = "interrupt" if action == "interrupt" else "stop"
                await _json(client, BASE+"/"+session_id+"/"+endpoint, {"round_id": trigger.round_id} if endpoint == "interrupt" else {})
                report["admission_ack_ms"] = round((time.perf_counter()-started)*1000, 3)
            try:
                await asyncio.wait_for(asyncio.shield(task), 1)
            except asyncio.CancelledError:
                pass
            old_commits = runtime.store.db.execute("SELECT COUNT(*) FROM context_snapshots WHERE session_id=? AND owner_turn_id=?", (session_id, trigger.turn_id)).fetchone()[0]
            cancelled = runtime.store.db.execute("SELECT COUNT(*) FROM ledger WHERE turn_id=? AND kind='context.maintenance_cancelled'", (trigger.turn_id,)).fetchone()[0]
            report["old_candidate_commits"] = old_commits
            report["old_maintenance_cancelled"] = cancelled > 0
            report["old_handle_invalidated"] = not old.valid
            report["passed"] = bool(report["foreground_trigger_succeeded"] and report["admission_ack_ms"] < 1000
                and old_commits == 0 and cancelled > 0 and not old.valid
                and all(r["state"] == "generation_completed" and r["model_deadline_met"] for r in report["rounds"]))
        if not report["passed"]:
            report["failure"] = {"code": "performance_background_expectation_failed"}
    except Exception as exc:
        report["failure"] = _failure(exc)
    finally:
        if session_id is not None:
            report["cleanup"] = await _cleanup(client, database, session_id, profile)
            await _settle(runtime, session_id)
            report["database"] = _evidence(runtime.store, session_id, profile)
            report["passed"] = report["passed"] and report["cleanup"]["passed"]
    return report


def acceptance(cases):
    """Keep foreground latency, routine maintenance, and semantic gates separate."""
    long_cases = [c for c in cases if c["scenario"] == "continuous_long_dialogue"]
    idle_cases = [c for c in cases if c["scenario"] == "background_idle"]
    routine = [c for c in cases if c["scenario"] in {"continuous_long_dialogue", "background_idle"}]
    failures = [event for c in routine for event in c.get("database", {}).get("maintenance", [])
                if event["kind"] == "context.maintenance_failed"]
    completions = [event for c in routine for event in c.get("database", {}).get("maintenance", [])
                   if event["kind"] == "context.maintenance_completed"]
    budget_met = all(e.get("elapsed_ms", 5001)+e.get("planning_ms", 0) <= 5000 for e in completions)
    return {"foreground_all_completed_with_model_first_text_within_9s": bool(cases) and all(c["rounds"] for c in cases) and all(
                r["state"] == "generation_completed" and not r["error_code"] and r["model_deadline_met"]
                and r.get("model_first_output") is not None and r["model_first_output"]["elapsed_ms"] <= 9000
                for c in cases for r in c["rounds"]),
            "long_cases_passed": all(c["passed"] for c in long_cases) if long_cases else None,
            "idle_cases_passed": all(c["passed"] for c in idle_cases) if idle_cases else None,
            "routine_background_failure_count": len(failures), "routine_background_completed_count": len(completions),
            "completed_maintenance_including_planning_within_5s": budget_met,
            "routine_background_all_succeeded": bool(completions) and not failures and budget_met,
            "production_slo_passed": False}


async def run_probe(settings, profiles, database, *, selected=DEFAULT_PROFILES, port=3103, turns=28, scenario="all"):
    report = {"schema_version": 1, "probe": "m2_default_budget_performance", "passed": False,
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "cases": [],
        "configuration": {"first_output_deadline_ms": settings.m1_first_output_deadline_ms, "input_cap_estimated_tokens": settings.m2_context_input_tokens,
            "background_enabled": settings.m2_context_background, "background_seconds": settings.m2_context_background_seconds,
            "background_output_tokens": settings.m2_context_background_tokens, "max_model_steps": settings.m1_max_steps,
            "max_native_tools": settings.m1_max_tool_calls, "turn_wall_seconds": settings.m1_wall_seconds},
        "limits": {"max_profiles": 2, "long_real_turns_per_profile": turns, "background_cases_per_profile": 4,
            "automatic_retries": 0, "profile_fallback": False, "max_failed_long_turns": 3},
        "scope": {"long_dialogue_turns_real": True, "background_cases_import_scripted_history": True,
            "all_compaction_summaries_real": True, "only_owned_tcp_service_and_new_database": True,
            "fallback_not_valid_model_first_output": True, "rtc_used": False, "production_slo_claim": False}}
    report["source_sha256"] = {name: hashlib.sha256((Path(__file__).resolve().parents[1]/name).read_bytes()).hexdigest()
        for name in ("store.py", "context.py", "context_edits.py", "runtime.py", "models.py", "text.py", "app.py", "probes/performance.py", "probes/_text_server.py")}
    started = time.perf_counter()
    try:
        if not 1 <= len(selected) <= 2 or len(set(selected)) != len(selected) or not 20 <= turns <= 36:
            raise ProbeFailure("invalid_performance_probe_options")
        for profile in selected:
            profiles.get(profile).validate_ready()
        if (settings.m1_first_output_deadline_ms != 9000 or settings.m2_context_background_seconds != 5
            or settings.m2_context_input_tokens != 24576 or not settings.m2_context_background
            or settings.m2_context_background_tokens != 1500 or settings.m1_max_steps != 4
            or settings.m1_max_tool_calls != 8 or settings.m1_wall_seconds != 30):
            raise ProbeFailure("performance_probe_requires_default_deadlines")
        async with owned_text_service(settings, profiles, database, port=port, allow_background=True) as (runtime, client):
            for profile in selected:
                if scenario in {"all", "long"}:
                    result = await _long_case(client, runtime, database, profile, turns)
                    report["cases"].append(result)
                    print(json.dumps({"profile": profile, "scenario": "long", "passed": result["passed"], "failure": result.get("failure")}), flush=True)
                if scenario in {"all", "background"}:
                    for action in ("idle", "preempt", "interrupt", "close"):
                        result = await _background_case(client, runtime, database, profile, action)
                        report["cases"].append(result)
                        print(json.dumps({"profile": profile, "scenario": action, "passed": result["passed"], "failure": result.get("failure")}), flush=True)
        report["owned_service_stopped"] = True
        report["cleanup_confirmed"] = bool(report["cases"]) and all(c.get("cleanup", {}).get("passed") for c in report["cases"])
        report["acceptance"] = acceptance(report["cases"])
        report["passed"] = report["cleanup_confirmed"] and all(c["passed"] for c in report["cases"]) and report["acceptance"]["routine_background_all_succeeded"]
    except Exception as exc:
        report["failure"] = _failure(exc)
    report["elapsed_ms"] = round((time.perf_counter()-started)*1000, 3)
    return report


def main():
    parser = argparse.ArgumentParser(description="Default 9s text + 5s background performance probe")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models-file", type=Path, default=Path("backend_harness_loop/model_profiles.json"))
    parser.add_argument("--model-env-file", type=Path, default=Path("backend_harness_loop/.env"))
    parser.add_argument("--port", type=int, default=3103)
    parser.add_argument("--turns", type=int, default=28)
    parser.add_argument("--profile", action="append")
    parser.add_argument("--scenario", choices=("all", "long", "background"), default="all")
    args = parser.parse_args()
    if args.output.exists():
        print("performance_probe_requires_new_output")
        return 2
    try:
        profiles = ModelProfiles.load(args.models_file, environ=load_model_environment(args.model_env_file))
        settings = Settings.load(None)
        report = asyncio.run(run_probe(settings, profiles, args.database, selected=args.profile or DEFAULT_PROFILES,
            port=args.port, turns=args.turns, scenario=args.scenario))
    except Exception as exc:
        report = {"schema_version": 1, "probe": "m2_default_budget_performance", "passed": False, "failure": _failure(exc)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print("performance_probe_passed" if report["passed"] else "performance_probe_failed")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
