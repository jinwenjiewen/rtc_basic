"""Bounded real-HTTP M2 compaction check using synthetic dialogue only.

Run against a local text service with background maintenance disabled when a
repeatable foreground-only sample is wanted. This probe never changes policy,
calls a model directly, writes SQLite, or stops sessions created by someone else.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import secrets
import sqlite3
import time

import httpx

from .text import (DEFAULT_PROFILES, SAFE_CODE, ProbeFailure, _base_url, _cleanup,
                   _create, _failure, _hash, _heartbeat, _page_key,
                   _session_metadata, _turn)


MAX_PROFILES = 2
MAX_TURNS = 16
REQUIRED_COMPACTIONS = 2
PHASES = {"decision", "final", "compaction", "compact", "compact_only", "maintenance", "context_compact", "background_compact"}


def _failure_metadata(exc: Exception) -> dict:
    value = _failure(exc)
    if value["code"].startswith("text_probe_"):
        value["code"] = value["code"].replace("text_probe_", "context_probe_", 1)
    return value


def _filler(index: int, size: int) -> str:
    prefix = f"合成压力材料第{index}段。下文只含临时讨论，可概括为主题引用，不是新的偏好、约束或执行结果；不要写工作笔记。\n"
    suffix = "\n本轮只回复“收到”。"
    sentence = "这是可合并的临时讨论材料，内容重复且不影响之前的有效预算、排除条件或尚未确认的事项。"
    room = size - len((prefix + suffix).encode("utf-8"))
    body = (sentence * (max(0, room) // len(sentence.encode("utf-8")) + 1)).encode("utf-8")[:max(0, room)]
    return prefix + body.decode("utf-8", errors="ignore") + suffix


def _json_reply(text: str) -> dict | None:
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(candidate)
    except (ValueError, TypeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _semantic_checks(text: str) -> dict:
    value = _json_reply(text)
    return {
        "valid_json_object": value is not None,
        "corrected_budget_retained": value is not None and type(value.get("budget")) is int and value["budget"] == 3000,
        "weekend_negation_retained": value is not None and value.get("weekend_allowed") is False,
        "unconfirmed_action_not_promoted_to_done": value is not None and value.get("order_submitted") is False,
    }


def _history_checks(text: str, first_user_seq: int) -> dict:
    value = _json_reply(text)
    return {
        "valid_json_object": value is not None,
        "original_budget_recovered": value is not None and type(value.get("initial_budget")) is int and value["initial_budget"] == 5000,
        "original_user_reference_matches": value is not None and value.get("source_ref") == f"seq:{first_user_seq}",
    }


def _metrics(rows: list[tuple]) -> tuple[list[dict], bool]:
    observations = []
    valid = bool(rows)
    names = ("before_estimated_tokens", "after_estimated_tokens", "reduction_estimated_tokens",
             "budget_tokens", "pressure_before", "pressure_after", "target_met", "maintenance_estimated_tokens")
    for row in rows:
        item = dict(zip(names, row))
        integers_ok = all(type(item[name]) is int and 0 <= item[name] <= 10**8 for name in (
            "before_estimated_tokens", "after_estimated_tokens", "reduction_estimated_tokens", "budget_tokens", "maintenance_estimated_tokens"))
        ratios_ok = all(isinstance(item[name], (int, float)) and not isinstance(item[name], bool)
                        and math.isfinite(item[name]) and 0 <= item[name] <= 10**6
                        for name in ("pressure_before", "pressure_after"))
        measured = integers_ok and ratios_ok and type(item["target_met"]) is int and item["target_met"] in {0, 1}
        if measured:
            valid = valid and (item["before_estimated_tokens"] > item["after_estimated_tokens"]
                and item["reduction_estimated_tokens"] == item["before_estimated_tokens"] - item["after_estimated_tokens"]
                and item["after_estimated_tokens"] <= item["budget_tokens"]
                and item["maintenance_estimated_tokens"] <= item["budget_tokens"]
                and item["pressure_after"] < item["pressure_before"])
            item["target_met"] = bool(item["target_met"])
            observations.append(item)
        else:
            valid = False
            observations.append({"metrics_available": False})
    return observations, bool(valid)


def _read_evidence(path: Path, session_id: str, profile: str, first_turn_id: str = "") -> dict:
    """Query booleans/counts only; summary and conversation text never leave SQL."""
    try:
        with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=.5)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            session = db.execute("SELECT state,notes_version,context_version,profile_id=? FROM sessions WHERE session_id=?", (profile, session_id)).fetchone()
            if session is None:
                raise ProbeFailure("context_probe_session_not_in_database")
            rows = db.execute("SELECT context_version,compacted_through_seq FROM context_snapshots WHERE session_id=? ORDER BY context_version", (session_id,)).fetchall()
            metrics, metrics_valid = _metrics(db.execute("""SELECT
                json_extract(input_ref_json,'$.estimate.before_tokens'),json_extract(input_ref_json,'$.estimate.after_tokens'),
                json_extract(input_ref_json,'$.estimate.reduction_tokens'),json_extract(input_ref_json,'$.estimate.budget_tokens'),
                json_extract(input_ref_json,'$.estimate.pressure_before'),json_extract(input_ref_json,'$.estimate.pressure'),
                json_extract(input_ref_json,'$.estimate.target_met'),json_extract(input_ref_json,'$.estimate.maintenance_tokens')
                FROM context_snapshots WHERE session_id=? ORDER BY context_version""", (session_id,)).fetchall())
            latest = db.execute("""SELECT instr(summary_json,'3000')>0,
                (instr(summary_json,'周末')>0 OR instr(lower(summary_json),'weekend')>0),
                (instr(summary_json,'不')>0 OR instr(lower(summary_json),'false')>0 OR instr(lower(summary_json),'excluded')>0 OR instr(lower(summary_json),'forbidden')>0),
                COALESCE(json_array_length(json_extract(summary_json,'$.pending_actions')),0)>0
                FROM context_snapshots WHERE session_id=? ORDER BY context_version DESC LIMIT 1""", (session_id,)).fetchone()
            source = db.execute("""SELECT MIN(seq),COUNT(*),MAX(instr(COALESCE(json_extract(payload_json,'$.content'),''),'5000')>0)
                FROM ledger WHERE session_id=? AND turn_id=? AND kind='message' AND role='user'""", (session_id, first_turn_id)).fetchone()
            user_count = db.execute("SELECT COUNT(*) FROM ledger WHERE session_id=? AND kind='message' AND role='user'", (session_id,)).fetchone()[0]
            compact_events = db.execute("SELECT COUNT(*) FROM ledger WHERE session_id=? AND kind='context.compacted'", (session_id,)).fetchone()[0]
            model_count, mismatches = db.execute("""SELECT COUNT(*),COALESCE(SUM(json_extract(payload_json,'$.profile_id') IS NOT ?),0)
                FROM ledger WHERE session_id=? AND kind='model.started'""", (profile, session_id)).fetchone()
            per_turn = db.execute("""SELECT COALESCE(MAX(n),0) FROM (SELECT COUNT(*) AS n FROM ledger
                WHERE session_id=? AND kind='model.started' GROUP BY turn_id)""", (session_id,)).fetchone()[0]
            usage = db.execute("""SELECT COALESCE(SUM(json_extract(payload_json,'$.usage.prompt_tokens')),0),
                COALESCE(SUM(json_extract(payload_json,'$.usage.completion_tokens')),0),
                COALESCE(SUM(json_extract(payload_json,'$.usage.total_tokens')),0)
                FROM ledger WHERE session_id=? AND kind='model.completed'""", (session_id,)).fetchone()
            phase_counts = {}
            for phase, count in db.execute("SELECT json_extract(payload_json,'$.phase'),COUNT(*) FROM ledger WHERE session_id=? AND kind='model.started' GROUP BY 1", (session_id,)):
                selected = phase if phase in PHASES else "other"
                phase_counts[selected] = phase_counts.get(selected, 0) + count
            history_count = db.execute("""SELECT COUNT(*) FROM tool_calls c JOIN turns t ON t.turn_id=c.turn_id
                WHERE t.session_id=? AND c.name IN ('context_read_history','context_search_history') AND c.state='success'""", (session_id,)).fetchone()[0]
            refs = db.execute("""SELECT COUNT(*),COALESCE(SUM(l.seq IS NULL),0)
                FROM context_snapshots s,json_tree(s.summary_json) r,json_tree(s.summary_json) p
                LEFT JOIN ledger l ON l.session_id=s.session_id AND 'seq:'||l.seq=r.value
                WHERE s.session_id=? AND r.parent=p.id AND p.key='source_refs' AND r.type='text'""", (session_id,)).fetchone()
            errors = {}
            for code, count in db.execute("SELECT json_extract(payload_json,'$.code'),COUNT(*) FROM ledger WHERE session_id=? AND kind='runtime.error' GROUP BY 1", (session_id,)):
                selected = code if isinstance(code, str) and SAFE_CODE.fullmatch(code) else "other"
                errors[selected] = errors.get(selected, 0) + count
            db.rollback()
        versions = [row[0] for row in rows]
        watermarks = [row[1] for row in rows]
        return {
            "session_closed": session[0] == "closed", "notes_version": session[1],
            "context_version": session[2], "profile_matches": bool(session[3]),
            "snapshot_count": len(rows), "compaction_event_count": compact_events,
            "snapshot_event_count_matches": len(rows) == compact_events,
            "compaction_metrics": metrics, "compaction_metrics_show_bounded_reduction": metrics_valid,
            "context_versions": versions, "coverage_watermarks": watermarks,
            "coverage_strictly_advances": all(b > a for a, b in zip([0, *watermarks[:-1]], watermarks)),
            "snapshot_versions_strictly_advance": all(b > a for a, b in zip([0, *versions[:-1]], versions)),
            "summary_literal_checks": {"corrected_budget_text_present": bool(latest and latest[0]),
                "weekend_term_present": bool(latest and latest[1]), "negation_term_present": bool(latest and latest[2]),
                "pending_actions_present": bool(latest and latest[3])},
            "original_first_user_record_retained": bool(source[1] == 1 and source[2]),
            "user_message_count": user_count, "source_ref_count": refs[0], "invalid_source_ref_count": refs[1],
            "model_call_count": model_count, "model_profile_matches": mismatches == 0,
            "max_observed_model_calls_per_turn": per_turn,
            "model_reported_usage_totals": dict(zip(("prompt_tokens", "completion_tokens", "total_tokens"), usage)),
            "model_phase_counts": phase_counts, "history_tool_success_count": history_count,
            "runtime_error_counts": errors,
            # Internal only; removed before the observation enters any report.
            "_first_user_seq": source[0],
        }
    except ProbeFailure:
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise ProbeFailure("context_probe_database_read_failed") from None


def _public_evidence(value: dict) -> dict:
    return {key: item for key, item in value.items() if not key.startswith("_")}


def _history_target_evidence(path: Path, session_id: str, turn_id: str, first_user_seq: int) -> dict:
    """Check this turn's actual tool result, not merely a successful tool call.

    History tools can return grouped messages, a bounded fragment list, or one
    directly requested record. Only the original user record containing the
    original budget qualifies; returned text is inspected wholly inside SQL.
    """
    if type(first_user_seq) is not int or first_user_seq < 1:
        raise ProbeFailure("context_probe_original_source_unavailable")
    try:
        with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=.5)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            bound = db.execute("SELECT COUNT(*) FROM turns WHERE session_id=? AND turn_id=?", (session_id, turn_id)).fetchone()[0] == 1
            calls = db.execute("""SELECT COUNT(*),COALESCE(SUM(c.state='success'),0)
                FROM tool_calls c JOIN turns t ON t.turn_id=c.turn_id
                WHERE t.session_id=? AND t.turn_id=?
                AND c.name IN ('context_read_history','context_search_history')""", (session_id, turn_id)).fetchone()
            matched = db.execute("""WITH successful AS (
                SELECT c.tool_call_id,c.result_json FROM tool_calls c JOIN turns t ON t.turn_id=c.turn_id
                WHERE t.session_id=? AND t.turn_id=? AND c.state='success'
                AND c.name IN ('context_read_history','context_search_history')
            ), returned AS (
                SELECT c.tool_call_id,json_extract(m.value,'$.seq') AS seq,
                    json_extract(m.value,'$.role') AS role,json_extract(m.value,'$.content') AS content
                FROM successful c,json_each(c.result_json,'$.data.messages') m
                WHERE json_type(c.result_json,'$.data.messages')='array' AND m.type='object'
                UNION ALL
                SELECT c.tool_call_id,json_extract(m.value,'$.seq'),
                    json_extract(m.value,'$.role'),json_extract(m.value,'$.content')
                FROM successful c,json_each(c.result_json,'$.data.records') m
                WHERE json_type(c.result_json,'$.data.records')='array' AND m.type='object'
                UNION ALL
                SELECT c.tool_call_id,json_extract(c.result_json,'$.data.seq'),
                    json_extract(c.result_json,'$.data.role'),json_extract(c.result_json,'$.data.content')
                FROM successful c WHERE json_type(c.result_json,'$.data')='object'
            ) SELECT COUNT(*),COUNT(DISTINCT tool_call_id) FROM returned
              WHERE typeof(seq)='integer' AND seq=? AND role='user'
                AND typeof(content)='text' AND instr(content,'5000')>0""", (session_id, turn_id, first_user_seq)).fetchone()
            db.rollback()
        return {"history_turn_bound_to_session": bound,
            "history_tool_calls_this_turn": calls[0], "successful_history_tool_calls_this_turn": calls[1],
            "target_user_record_return_count": matched[0],
            "successful_history_tool_calls_returning_target": matched[1],
            "returned_original_user_record_with_budget": bound and matched[0] > 0 and matched[1] > 0}
    except (OSError, sqlite3.Error, ValueError):
        raise ProbeFailure("context_probe_history_result_read_failed") from None


async def _profile_case(client: httpx.AsyncClient, database: Path, profile: str, index: int,
                        *, filler_bytes: int, max_turns: int) -> dict:
    report = {"profile_index": index, "profile_sha256": _hash(profile), "passed": False, "rounds": []}
    session_id, first_turn_id = None, ""
    marker = "M2-" + secrets.token_hex(8)
    try:
        session_id, data = await _create(client, profile)
        report["session_sha256"] = _hash(session_id)
        _session_metadata(data, profile)

        async def send(purpose: str, prompt: str):
            nonlocal first_turn_id
            await _heartbeat(client, session_id)
            turn = await _turn(client, session_id, prompt)
            if not first_turn_id:
                first_turn_id = turn.turn_id
            evidence = _read_evidence(database, session_id, profile, first_turn_id)
            report["rounds"].append({"purpose": purpose, **turn.metadata(),
                "snapshot_count": evidence["snapshot_count"], "context_version": evidence["context_version"],
                "notes_version": evidence["notes_version"], "user_message_count": evidence["user_message_count"]})
            if turn.state != "generation_completed" or turn.error_code:
                raise ProbeFailure(turn.error_code or "context_probe_round_incomplete")
            if evidence["notes_version"] != 0:
                raise ProbeFailure("context_probe_notes_would_mask_summary")
            if evidence["user_message_count"] != len(report["rounds"]) or not evidence["original_first_user_record_retained"]:
                raise ProbeFailure("context_probe_original_history_missing")
            return turn, evidence

        await send("initial_constraints", f"合成会话标记{marker}。我的课程预算是5000元，不接受周末课程。订单需要我明确确认后才能提交，现在尚未确认也尚未提交。请仅在会话上下文中保留这些条件，不要调用context_update_notes，也不要写工作笔记。只回复收到。")
        _, evidence = await send("corrected_constraints", "修正：预算从之前金额改为3000元。不要周末的限制继续有效，订单仍待我确认，未提交。不要写工作笔记。只回复收到。")
        fill_count = 0
        while evidence["snapshot_count"] < REQUIRED_COMPACTIONS and len(report["rounds"]) < max_turns - 2:
            fill_count += 1
            _, evidence = await send("pressure_material", _filler(fill_count, filler_bytes))
        report["pressure_turn_count"] = fill_count
        report["two_compactions_observed"] = evidence["snapshot_count"] >= REQUIRED_COMPACTIONS
        if not report["two_compactions_observed"]:
            raise ProbeFailure("context_probe_two_compactions_not_observed")

        semantic, _ = await send("semantic_recall_after_two_compactions", "根据此前会话，只输出JSON对象，字段为budget（最新预算数字）、weekend_allowed（是否接受周末课程，布尔）、order_submitted（订单是否已提交，布尔）。不要写工作笔记，不要增加其它字段或解释。")
        report["semantic_checks"] = _semantic_checks(semantic.text)
        before_history = _read_evidence(database, session_id, profile, first_turn_id)
        first_user_seq = before_history["_first_user_seq"]
        history, evidence = await send("original_history_after_two_compactions", f"请明确调用context_read_history或context_search_history，读回标记{marker}对应的最早一条用户预算原话。不要仅凭摘要猜测。只输出JSON对象，字段initial_budget（最初预算数字）、source_ref（该原始用户消息的seq:N引用）。不要写工作笔记。")
        report["history_checks"] = _history_checks(history.text, first_user_seq)
        report["history_tool_success_count_added"] = evidence["history_tool_success_count"] - before_history["history_tool_success_count"]
        report["history_tool_evidence"] = _history_target_evidence(database, session_id, history.turn_id, first_user_seq)
        report["database"] = _public_evidence(evidence)
        report["passed"] = bool(all(report["semantic_checks"].values()) and all(report["history_checks"].values())
            and report["history_tool_success_count_added"] > 0 and evidence["notes_version"] == 0
            and report["history_tool_evidence"]["returned_original_user_record_with_budget"]
            and evidence["snapshot_count"] >= REQUIRED_COMPACTIONS and evidence["snapshot_event_count_matches"]
            and evidence["compaction_metrics_show_bounded_reduction"]
            and evidence["coverage_strictly_advances"] and evidence["snapshot_versions_strictly_advance"]
            and evidence["source_ref_count"] > 0 and evidence["invalid_source_ref_count"] == 0
            and evidence["model_profile_matches"] and evidence["profile_matches"]
            and not evidence["runtime_error_counts"])
        if not report["passed"]:
            report["failure"] = {"code": "context_probe_expectation_failed"}
    except Exception as exc:
        report["failure"] = _failure_metadata(exc)
        if session_id is not None:
            try:
                report["database"] = _public_evidence(_read_evidence(database, session_id, profile, first_turn_id))
            except Exception as read_failure:
                report["evidence_failure"] = _failure_metadata(read_failure)
    finally:
        if session_id is not None:
            report["cleanup"] = await _cleanup(client, database, session_id, profile)
            report["passed"] = report["passed"] and report["cleanup"]["passed"]
    return report


async def run_probe(base_url: str, database: Path, profiles: list[str], *, filler_bytes: int = 1800,
                    max_turns: int = MAX_TURNS) -> dict:
    started = time.perf_counter()
    report = {"schema_version": 1, "probe": "m2_context_http", "passed": False,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {"synthetic_dialogue_only": True, "sqlite_read_only": True, "rtc_used": False,
            "notes_must_remain_empty": True, "literal_checks_are_not_semantic_proof": True,
            "http_text_timing_is_not_audio_latency": True},
        "limits": {"max_profiles": MAX_PROFILES, "max_turns_per_profile": max_turns,
            "filler_utf8_bytes": filler_bytes, "required_compactions": REQUIRED_COMPACTIONS,
            "automatic_retries": 0, "profile_fallback": False, "policy_changed_by_probe": False}, "profiles": []}
    try:
        base_url = _base_url(base_url)
        if (not 1 <= len(profiles) <= MAX_PROFILES or len(set(profiles)) != len(profiles)
                or any(not isinstance(profile, str) or not SAFE_CODE.fullmatch(profile) for profile in profiles)):
            raise ProbeFailure("invalid_probe_profiles")
        if not isinstance(filler_bytes, int) or isinstance(filler_bytes, bool) or not 400 <= filler_bytes <= 6000:
            raise ProbeFailure("invalid_context_probe_filler_size")
        if not isinstance(max_turns, int) or isinstance(max_turns, bool) or not 8 <= max_turns <= MAX_TURNS:
            raise ProbeFailure("invalid_context_probe_turn_limit")
        if not database.is_file():
            raise ProbeFailure("database_unavailable")
        report["loopback_origin_sha256"] = _hash(base_url)
        async with httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=45,
                                     follow_redirects=False, limits=httpx.Limits(max_connections=4)) as client:
            client.headers["X-M0-Client-Key"] = await _page_key(client)
            for index, profile in enumerate(profiles):
                report["profiles"].append(await _profile_case(client, database, profile, index,
                    filler_bytes=filler_bytes, max_turns=max_turns))
        report["cleanup_confirmed"] = all(item.get("cleanup", {}).get("passed") is True for item in report["profiles"])
        report["passed"] = report["cleanup_confirmed"] and all(item["passed"] for item in report["profiles"])
    except Exception as exc:
        report["failure"] = _failure_metadata(exc)
        report.setdefault("cleanup_confirmed", False)
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded real text verification of model-driven M2 context compaction")
    parser.add_argument("--base-url", default="http://127.0.0.1:3102")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", action="append")
    parser.add_argument("--filler-bytes", type=int, default=1800)
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS)
    args = parser.parse_args()
    try:
        report = asyncio.run(run_probe(args.base_url, args.database, args.profile or list(DEFAULT_PROFILES),
            filler_bytes=args.filler_bytes, max_turns=args.max_turns))
    except KeyboardInterrupt:
        report = {"schema_version": 1, "probe": "m2_context_http", "passed": False,
                  "failure": {"code": "context_probe_interrupted"}}
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        print("context_probe_report_write_failed")
        return 2
    print("context_probe_passed" if report["passed"] else "context_probe_failed")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
