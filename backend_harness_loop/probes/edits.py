"""Real-model HTTP verification of partial summary edits and evidence selection.

Only a NEW owned database/service is used. Four closed filler fixtures are
imported to make the first two real turns eligible for model-led compaction.
All updates, selection, compaction, history reads and final answers are real.
Reports contain checks/counts/timings, never prompts, summaries, IDs or secrets.
"""

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import time

from ..config import Settings
from ..models import ChatCompletionsModel, ModelProfiles, load_model_environment
from ._text_server import owned_text_service
from .context import _history_target_evidence
from .text import DEFAULT_PROFILES, ProbeFailure, _cleanup, _create, _failure, _hash, _heartbeat, _turn


class ObservedModel:
    """Inspect only the selected target presence; delegate every model request."""
    def __init__(self, profile, observation):
        self.inner, self.profile = ChatCompletionsModel(profile, trust_env=False), profile
        self.observation = observation

    def observe(self, messages, phase):
        target = self.observation.get(self.profile.profile_id, {})
        count = 0
        for message in messages:
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, str) or "\n" not in content:
                continue
            try:
                data = json.loads(content.split("\n", 1)[1])
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("source_type") == "working_context_data":
                count += sum(record.get("seq") == target.get("seq") and record.get("role") == "user"
                    and record.get("content") == target.get("text") for record in data.get("selected_records", []))
        target.setdefault("requests", []).append({"phase": phase, "selected_target_occurrences": count})

    async def complete(self, messages, **options):
        self.observe(messages, "decision")
        return await self.inner.complete(messages, **options)

    async def stream_final(self, messages, **options):
        self.observe(messages, "final")
        async for text in self.inner.stream_final(messages, **options):
            yield text

    async def aclose(self):
        await self.inner.aclose()


def _semantic(text, budget):
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        value = None
    valid = isinstance(value, dict) and set(value) == {"budget", "weekend_allowed", "order_submitted"}
    return {"valid_json_object": valid,
        "latest_budget": bool(valid and type(value["budget"]) is int and value["budget"] == budget),
        "weekend_negation": bool(valid and value["weekend_allowed"] is False),
        "pending_not_promoted_to_done": bool(valid and value["order_submitted"] is False)}


def _evidence(store, session_id, profile):
    db, context = store.db, store.get_context(session_id)
    rows = db.execute("SELECT operation,context_version,compacted_through_seq FROM context_edits WHERE session_id=? ORDER BY context_version", (session_id,)).fetchall()
    events = db.execute("SELECT COUNT(*) FROM ledger WHERE session_id=? AND kind IN ('context.updated','context.selected')", (session_id,)).fetchone()[0]
    calls = db.execute("""SELECT c.name,c.state,COUNT(*) FROM tool_calls c JOIN turns t ON t.turn_id=c.turn_id
        WHERE t.session_id=? GROUP BY c.name,c.state""", (session_id,)).fetchall()
    model = db.execute("""SELECT COUNT(*),COALESCE(SUM(json_extract(payload_json,'$.profile_id') IS NOT ?),0)
        FROM ledger WHERE session_id=? AND kind='model.started'""", (profile, session_id)).fetchone()
    max_calls = db.execute("SELECT MAX(n) FROM (SELECT COUNT(*) n FROM ledger WHERE session_id=? AND kind='model.started' GROUP BY turn_id)", (session_id,)).fetchone()[0]
    errors = db.execute("SELECT COUNT(*) FROM ledger WHERE session_id=? AND kind='runtime.error'", (session_id,)).fetchone()[0]
    return {"context_version": context["context_version"], "compacted_through_seq": context["compacted_through_seq"],
        "edit_count": len(rows), "update_count": sum(r["operation"] == "context_update" for r in rows),
        "select_count": sum(r["operation"] == "context_select" for r in rows), "edit_events_match": len(rows) == events,
        "versions_strictly_advance": all(a["context_version"] < b["context_version"] for a,b in zip(rows, rows[1:])),
        "snapshot_count": db.execute("SELECT COUNT(*) FROM context_snapshots WHERE session_id=?", (session_id,)).fetchone()[0],
        "selected_count": len(context["selected_refs"]), "notes_version": store.get_notes(session_id)["version"],
        "model_profile_matches": model[1] == 0, "model_call_count": model[0], "max_model_calls_per_turn": max_calls or 0,
        "runtime_errors": errors, "tool_states": [{"name": r[0] if r[0] in {"context_update", "context_select", "context_compact", "context_read_history", "context_search_history", "prepare_response", "context_update_notes"} else "other", "state": r[1], "count": r[2]} for r in calls],
        "provider_usage": dict(zip(("prompt_tokens", "completion_tokens", "total_tokens"), db.execute("""SELECT
            COALESCE(SUM(json_extract(payload_json,'$.usage.prompt_tokens')),0),
            COALESCE(SUM(json_extract(payload_json,'$.usage.completion_tokens')),0),
            COALESCE(SUM(json_extract(payload_json,'$.usage.total_tokens')),0)
            FROM ledger WHERE session_id=? AND kind='model.completed'""", (session_id,)).fetchone()))}


async def _case(client, runtime, database, profile, index, observation):
    report, session_id = {"profile_index": index, "profile_id": profile, "passed": False, "rounds": []}, None
    try:
        session_id, _ = await _create(client, profile)
        report["session_sha256"] = _hash(session_id)
        marker = "M2E-" + secrets.token_hex(8)

        async def send(purpose, prompt):
            await _heartbeat(client, session_id)
            result = await _turn(client, session_id, prompt)
            report["rounds"].append({"purpose": purpose, **result.metadata(), **_evidence(runtime.store, session_id, profile)})
            if result.error_code or result.state != "generation_completed":
                raise ProbeFailure(result.error_code or "edit_probe_incomplete_turn")
            return result

        extra = "以下合成重复材料只是使旧轮次具备可压缩体积，不改变任何约束：" + "临时讨论无需保留。" * 30
        original_text = (f"本次合成验收标记{marker}。课程预算5000元，不接受周末，订单尚未确认也未提交。请明确调用context_update，"
            "建立items中的budget_v1、weekend和pending_actions中的order_pending三个条目。预算value为5000，周末value为false，订单status为pending。"
            "各条引用本轮current_input_refs。不要写工作笔记，不压缩，完成后只回复收到。\n" + extra)
        original = await send("initial_structured_update", original_text)
        original_ref = runtime.store.user_reference(runtime.store.lookup_round(session_id, original.round_id)["target"], original_text)
        observation[profile].update({"seq": int(original_ref[4:]), "text": original_text})
        initial = runtime.store.get_context(session_id)
        report["initial_update_checks"] = {"version_one": initial["context_version"] == 1,
            "coverage_zero": initial["compacted_through_seq"] == 0,
            "three_expected_ids": {item.get("id") for section in initial["summary"].values() for item in section} == {"budget_v1", "weekend", "order_pending"},
            "actual_user_sources": all(item.get("source_refs") == [original_ref] for section in initial["summary"].values() for item in section)}
        await send("correct_budget_with_supersedes", "预算修正为3000元。请明确调用context_update，只新增items中的budget_v2，value为3000、supersedes=[budget_v1]，引用本轮current_input_refs。不要改weekend和order_pending，不要写笔记或压缩。只回复收到。\n" + extra)
        corrected = runtime.store.get_context(session_id)
        items = {item.get("id"): item for section in corrected["summary"].values() for item in section}
        old_items = {item.get("id"): item for section in initial["summary"].values() for item in section}
        report["supersedes_checks"] = {"old_marked_superseded": items.get("budget_v1", {}).get("status") == "superseded",
            "new_value_and_relation": items.get("budget_v2", {}).get("value") == 3000 and items.get("budget_v2", {}).get("supersedes") == ["budget_v1"],
            "unmodified_items_retained": all(items.get(key) == old_items.get(key) for key in ("weekend", "order_pending")),
            "coverage_not_advanced": corrected["compacted_through_seq"] == 0}
        # Imported filler makes exactly the two real old rounds eligible while
        # preserving four closed raw turns. No fabricated summary is imported.
        for ordinal in range(4):
            owner = runtime.store.admit_turn(session_id, "fixture-" + str(ordinal), "合成已结束的临时讨论，不改变预算和约束。", "fixture-c" + str(ordinal))
            runtime.store.complete_turn(owner, "generation_completed", "收到")
            runtime.store.record_delivery(owner, "收到", "delta")
            runtime.store.record_delivery(owner, "", "completed")
        await send("model_compaction_after_partial_updates", "请明确调用context_compact压缩允许的闭合前缀，保留既有budget_v2、weekend、order_pending及有效修正关系和原始来源，keep_refs=[]。不要调用context_update或写笔记。随后只回复收到。")
        compacted = runtime.store.get_context(session_id)
        watermark = compacted["compacted_through_seq"]
        report["compaction_checks"] = {"original_archived": watermark >= int(original_ref[4:]),
            "snapshot_committed": runtime.store.snapshot(session_id)["context_compaction_count"] >= 1,
            "not_selected_yet": original_ref not in compacted["selected_refs"]}
        selected = await send("read_archived_record_then_select", f"请先实际调用context_read_history按record_seq={original_ref[4:]}读回原始用户记录，再调用context_select，把该用户原文{original_ref}加入keep_refs，release_refs=[]，purpose=budget-audit。不要选择工具返回壳或助手记录，不修改摘要或压缩水位，不写工作笔记。完成后只回复收到。")
        report["history_tool_evidence"] = _history_target_evidence(database, session_id, selected.turn_id, int(original_ref[4:]))
        current = runtime.store.get_context(session_id)
        report["selection_checks"] = {"selected_original_only": current["selected_refs"] == [original_ref],
            "summary_unchanged": current["summary"] == compacted["summary"], "watermark_unchanged": current["compacted_through_seq"] == watermark}
        observe_start = len(observation[profile]["requests"])
        semantic = await send("recall_with_selected_old_budget", "根据此前会话，只输出JSON对象，字段budget（最新预算数字）、weekend_allowed（布尔）、order_submitted（布尔）。不要增加字段或解释，不修改工作视图，不写笔记。")
        report["semantic_with_selected_old_record"] = _semantic(semantic.text, 3000)
        seen = observation[profile]["requests"][observe_start:]
        report["selected_record_loaded_in_actual_model_requests"] = bool(seen) and all(r["selected_target_occurrences"] == 1 for r in seen)
        before_release = runtime.store.get_context(session_id)
        release_args = {"expected_context_version": before_release["context_version"], "keep_refs": [], "release_refs": [original_ref], "purpose": "已核对"}
        await send("release_original_evidence", "本轮唯一任务：实际执行一次context_select，取消当前证据保留。参数是下面的JSON，请作为工具调用参数执行：\n"
            + json.dumps(release_args, ensure_ascii=False) + "\n只取消显式加载，不改变摘要、预算或压缩水位，不写笔记。必须得到工具success后才能回复收到；未执行或失败时明确说明未完成。")
        released = runtime.store.get_context(session_id)
        report["release_checks"] = {"selection_empty": released["selected_refs"] == [],
            "summary_unchanged": released["summary"] == before_release["summary"],
            "watermark_unchanged": released["compacted_through_seq"] == before_release["compacted_through_seq"],
            "original_ledger_retained": runtime.store.read_message(session_id, int(original_ref[4:]))["content"] == original_text}
        await send("partial_update_after_compaction", "预算进一步修正为3200元。请明确调用context_update，仅新增items中的budget_v3，value=3200、supersedes=[budget_v2]，引用本轮current_input_refs，保留周末否定和订单待确认状态。不要写笔记或压缩。只回复收到。")
        current = runtime.store.get_context(session_id)
        by_id = {item.get("id"): item for section in current["summary"].values() for item in section}
        report["update_after_compaction_checks"] = {"new_budget_persisted": by_id.get("budget_v3", {}).get("value") == 3200,
            "old_budget_superseded": by_id.get("budget_v2", {}).get("status") == "superseded",
            "supersedes_link": by_id.get("budget_v3", {}).get("supersedes") == ["budget_v2"]}
        observe_start = len(observation[profile]["requests"])
        final = await send("recall_after_release_and_update", "根据此前会话，只输出JSON对象，字段budget（最新预算数字）、weekend_allowed（布尔）、order_submitted（布尔）。不要增加字段或解释，不修改工作视图，不写笔记。")
        report["semantic_after_release_and_update"] = _semantic(final.text, 3200)
        seen = observation[profile]["requests"][observe_start:]
        report["released_record_absent_from_actual_selected_records"] = bool(seen) and all(r["selected_target_occurrences"] == 0 for r in seen)
        report["database"] = _evidence(runtime.store, session_id, profile)
        checks = ("initial_update_checks", "supersedes_checks", "compaction_checks", "selection_checks", "release_checks",
                  "update_after_compaction_checks", "semantic_with_selected_old_record", "semantic_after_release_and_update")
        evidence = report["database"]
        report["passed"] = bool(all(all(report[key].values()) for key in checks)
            and report["history_tool_evidence"]["returned_original_user_record_with_budget"]
            and report["selected_record_loaded_in_actual_model_requests"] and report["released_record_absent_from_actual_selected_records"]
            and evidence["update_count"] == 3 and evidence["select_count"] == 2 and evidence["edit_events_match"]
            and evidence["versions_strictly_advance"] and evidence["notes_version"] == 0 and evidence["runtime_errors"] == 0
            and evidence["model_profile_matches"] and evidence["max_model_calls_per_turn"] <= runtime.max_steps)
        if not report["passed"]:
            report["failure"] = {"code": "edit_probe_expectation_failed"}
    except Exception as exc:
        report["failure"] = _failure(exc)
    finally:
        if session_id is not None:
            report["database"] = _evidence(runtime.store, session_id, profile)
            report["cleanup"] = await _cleanup(client, database, session_id, profile)
            report["passed"] = report["passed"] and report["cleanup"]["passed"]
    return report


async def run_probe(settings, profiles, database, *, port=3103, selected=DEFAULT_PROFILES):
    report = {"schema_version": 1, "probe": "m2_context_edits_text_http", "passed": False,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {"owned_tcp_service_and_new_database": True, "real_model_http_turns_per_profile": 8,
            "scripted_closed_filler_turns_per_profile": 4, "summary_fixture_imported": False,
            "all_edits_compaction_selection_history_reads_and_answers_real": True, "rtc_used": False,
            "http_timing_is_not_audio_latency": True},
        "configuration": {"input_cap_estimated_tokens": settings.m2_context_input_tokens,
            "first_output_deadline_ms": settings.m1_first_output_deadline_ms, "max_model_steps": settings.m1_max_steps,
            "max_native_tools": settings.m1_max_tool_calls, "wall_seconds": settings.m1_wall_seconds,
            "background_enabled": settings.m2_context_background},
        "limits": {"max_profiles": 2, "automatic_retries": 0, "profile_fallback": False, "max_http_turns_per_profile": 8}, "profiles": []}
    started, observation = time.perf_counter(), {}
    try:
        if not 1 <= len(selected) <= 2 or len(set(selected)) != len(selected):
            raise ProbeFailure("invalid_edit_probe_profiles")
        for profile in selected:
            profiles.get(profile).validate_ready()
            observation[profile] = {"requests": []}
        report["configuration"]["decision_tool_choice_by_profile"] = {p: profiles.get(p).decision_tool_choice for p in selected}
        async with owned_text_service(settings, profiles, database, port=port,
            model_factory=lambda profile: ObservedModel(profile, observation)) as (runtime, client):
            for index, profile in enumerate(selected):
                result = await _case(client, runtime, database, profile, index, observation)
                report["profiles"].append(result)
                print(json.dumps({"profile_index": index, "passed": result["passed"], "failure": result.get("failure"), "rounds": len(result["rounds"])}), flush=True)
        report["owned_service_stopped"] = True
        report["cleanup_confirmed"] = all(p.get("cleanup", {}).get("passed") for p in report["profiles"])
        report["passed"] = report["cleanup_confirmed"] and all(p["passed"] for p in report["profiles"])
    except Exception as exc:
        report["failure"] = _failure(exc)
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return report


def main():
    parser = argparse.ArgumentParser(description="Owned real text verification of M2 context_update/context_select")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--models-file", type=Path, default=Path("backend_harness_loop/model_profiles.json"))
    parser.add_argument("--model-env-file", type=Path, default=Path("backend_harness_loop/.env"))
    parser.add_argument("--first-output-ms", type=int, default=20000)
    parser.add_argument("--port", type=int, default=3103)
    parser.add_argument("--profile", action="append")
    args = parser.parse_args()
    if args.output.exists():
        print("edit_probe_requires_new_output")
        return 2
    try:
        profiles = ModelProfiles.load(args.models_file, environ=load_model_environment(args.model_env_file))
        settings = replace(Settings.load(None), m1_first_output_deadline_ms=args.first_output_ms, m2_context_background=False)
        report = asyncio.run(run_probe(settings, profiles, args.database, port=args.port, selected=args.profile or DEFAULT_PROFILES))
    except Exception as exc:
        report = {"schema_version": 1, "probe": "m2_context_edits_text_http", "passed": False, "failure": _failure(exc)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print("edit_probe_passed" if report["passed"] else "edit_probe_failed")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
