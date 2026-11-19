"""Read-only, content-free observations for one M1 RTC client ledger.

SQLite sessions supply the authoritative run IDs. A session must also occur in
the supplied RTC client ledger; this prevents unrelated synthetic DB sessions
and old captures from being folded into a live report. Counts are observations,
not an automatic claim that a person heard audio or that latency was measured.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


_HASH = re.compile(r"[a-f0-9]{64}\Z")
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
_PROFILE = re.compile(r"[A-Za-z0-9_./:-]{1,100}\Z")
_NESTED = re.compile(r"data\[(\d+)\]\.[A-Za-z_][A-Za-z_0-9]*\Z")
_CLIENT_KINDS = frozenset({
    "joined", "agent_joined", "agent_audio_published", "first_audio_decoded",
    "remote_level_active", "remote_level_silent", "mic_started", "mic_level_active",
    "mic_stopped", "mic_ended", "text_sent", "interrupt_sent", "autoplay_failed",
    "playback_resumed", "heard_confirmed", "speech_input_confirmed", "stop_confirmed",
    "sdk_error", "player_playing",
})
_HUMAN_KINDS = frozenset({"heard_confirmed", "speech_input_confirmed", "stop_confirmed"})
_STATES = frozenset({"active", "closed", "running", "completed", "generation_completed", "cancelled", "failed",
                     "waiting_tool", "responding", "unknown", "delta", "interrupted", "created", "starting", "stop_pending", "stopped"})


class LiveEvidenceError(ValueError):
    """A machine code only: input contents and paths are never reflected."""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe(value, pattern=_CODE) -> str:
    return value if isinstance(value, str) and pattern.fullmatch(value) else "other"


def _state(value) -> str:
    return value if isinstance(value, str) and value in _STATES else "other"


def _counts(values) -> dict:
    return dict(sorted(Counter(values).items()))


def _read_jsonl(path, label: str) -> tuple[list[dict], int, bool]:
    target = Path(path)
    if not target.exists():
        return [], 0, False
    records, invalid = [], 0
    try:
        if target.stat().st_size > 50_000_000:
            raise LiveEvidenceError(label + "_file_too_large")
        with target.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError()
                    records.append(record)
                except (ValueError, TypeError):
                    invalid += 1
    except (OSError, UnicodeError):
        raise LiveEvidenceError(label + "_read_failed") from None
    return records, invalid, True


def _identifiers(record: dict) -> list[dict]:
    correlation = record.get("correlation")
    if not isinstance(correlation, dict):
        return []
    identifiers = correlation.get("identifiers")
    if not isinstance(identifiers, list):
        return []
    return [item for item in identifiers if isinstance(item, dict)
            and isinstance(item.get("sha256"), str) and _HASH.fullmatch(item["sha256"])]


def _hashes(record: dict, kind: str) -> set[str]:
    return {item["sha256"] for item in _identifiers(record) if item.get("kind") == kind}


def _user_asr_rounds(record: dict, user_hash: str | None) -> set[str]:
    """Pair user and round only within the same subtitle data element."""
    if record.get("tlv_tag") != "subv" or user_hash is None:
        return set()
    elements = defaultdict(lambda: defaultdict(set))
    for item in _identifiers(record):
        field = item.get("field")
        match = _NESTED.fullmatch(field) if isinstance(field, str) else None
        if match and item.get("kind") in {"round", "user"}:
            elements[match[1]][item["kind"]].add(item["sha256"])
    rounds = set()
    for element in elements.values():
        if element["user"] == {user_hash} and len(element["round"]) == 1:
            rounds.update(element["round"])
    return rounds


def _cleanup(entry: dict) -> dict:
    observations = entry.get("provider", [])
    if not isinstance(observations, list):
        observations = []
    calls = [item for item in observations if isinstance(item, dict)
             and item.get("action") in {"StartVoiceChat", "StopVoiceChat"}]
    stops = [item for item in calls if item["action"] == "StopVoiceChat"]
    http_counts = Counter()
    for item in stops:
        status = item.get("http_status")
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            http_counts[str(status)] += 1
    rounds = entry.get("cleanup_rounds")
    return {
        "local_state": _state(entry.get("state")),
        "local_stopped": entry.get("state") == "stopped",
        "start_attempted": entry.get("start_attempted") is True,
        "cleanup_rounds": rounds if isinstance(rounds, int) and not isinstance(rounds, bool) and rounds >= 0 else 0,
        "provider_action_counts": _counts(item["action"] for item in calls),
        "stop_accepted": any(item.get("ok") is True for item in stops),
        "stop_http_status_counts": dict(sorted(http_counts.items())),
        "scope": "Local cleanup state and provider acceptance; not independent proof of remote shutdown",
    }


def _backend(db: sqlite3.Connection, session: sqlite3.Row, fixture: str | None) -> tuple[dict, set[str], set[str], dict]:
    session_id, run_id = session["session_id"], session["run_id"]
    turns = db.execute("SELECT round_id,state,delivery_status FROM turns WHERE session_id=? AND run_id=?", (session_id, run_id)).fetchall()
    accepted = {_sha(str(turn["round_id"])) for turn in turns}
    kinds = db.execute("SELECT kind,COUNT(*) AS n FROM ledger WHERE session_id=? GROUP BY kind", (session_id,)).fetchall()
    event_counts = Counter()
    for item in kinds:
        event_counts[_safe(item["kind"])] += item["n"]
    phases = db.execute("""SELECT json_extract(payload_json,'$.phase') AS phase,COUNT(*) AS n
        FROM ledger WHERE session_id=? AND kind='model.started' GROUP BY phase""", (session_id,)).fetchall()
    profiles = db.execute("""SELECT json_extract(payload_json,'$.profile_id') AS profile,COUNT(*) AS n
        FROM ledger WHERE session_id=? AND kind='model.started' GROUP BY profile""", (session_id,)).fetchall()
    model_rounds = {_sha(str(item["round_id"])) for item in db.execute("""SELECT DISTINCT t.round_id
        FROM ledger l JOIN turns t ON t.turn_id=l.turn_id
        WHERE l.session_id=? AND t.session_id=? AND t.run_id=? AND l.kind='model.completed'""", (session_id, session_id, run_id))}
    tools = db.execute("""SELECT c.name,c.state,COUNT(*) AS n FROM tool_calls c JOIN turns t ON t.turn_id=c.turn_id
        WHERE t.session_id=? AND t.run_id=? GROUP BY c.name,c.state""", (session_id, run_id)).fetchall()
    tool_names, tool_states = Counter(), Counter()
    for item in tools:
        tool_names[_safe(item["name"])] += item["n"]
        tool_states[_safe(item["state"])] += item["n"]
    errors = Counter()
    for item in db.execute("""SELECT json_extract(payload_json,'$.code') AS code,COUNT(*) AS n
        FROM ledger WHERE session_id=? AND kind='runtime.error' GROUP BY code""", (session_id,)):
        errors[_safe(item["code"])] += item["n"]
    result = {
        "state": _state(session["state"]), "epoch": session["epoch"], "notes_version": session["notes_version"],
        "accepted_round_count": len(accepted),
        "turn_state_counts": _counts(_state(item["state"]) for item in turns),
        "delivery_status_counts": _counts(_state(item["delivery_status"]) for item in turns),
        "ledger_event_counts": dict(sorted(event_counts.items())),
        "model_started_count": event_counts["model.started"],
        "model_completed_count": event_counts["model.completed"],
        "model_completed_round_count": len(model_rounds),
        "cancellation_count": event_counts["execution.cancelled"],
        "model_phase_counts": { _safe(item["phase"]): item["n"] for item in phases },
        "model_profile_counts": { _safe(item["profile"], _PROFILE): item["n"] for item in profiles },
        "tool_name_counts": dict(sorted(tool_names.items())),
        "tool_state_counts": dict(sorted(tool_states.items())),
    }
    if fixture is not None:
        # The matching text stays inside SQLite. Only booleans leave this query.
        found = db.execute("""SELECT EXISTS(SELECT 1 FROM turns WHERE session_id=? AND run_id=? AND instr(user_text,?)>0) AS user_match,
            instr(notes_text,?)>0 AS notes_match FROM sessions WHERE session_id=?""", (session_id, run_id, fixture, fixture, session_id)).fetchone()
        result["fixture_matches"] = {"user_text": bool(found["user_match"]), "notes_text": bool(found["notes_match"])}
    return result, accepted, model_rounds, dict(sorted(errors.items()))


def summarize_m1_live(capture_path, client_path, database_path, session_ledger_path, *, fixture=None) -> dict:
    """Return safe counts from local files, without starting RTC or opening a model.

    ``fixture`` is an optional known test phrase. It is checked inside SQLite and
    returned only as two booleans, never as text or a content hash. No write or
    RuntimeStore initialization is performed, including recovery mutations.
    """
    if fixture is not None and (not isinstance(fixture, str) or not 1 <= len(fixture) <= 2000):
        raise LiveEvidenceError("invalid_fixture")
    captures, capture_invalid, capture_present = _read_jsonl(capture_path, "capture")
    clients, client_invalid, client_present = _read_jsonl(client_path, "client")
    try:
        ledger = json.loads(Path(session_ledger_path).read_text("utf-8"))
        entries = ledger["sessions"]
        if ledger.get("schema_version") != 1 or not isinstance(entries, list) or any(not isinstance(item, dict) or not isinstance(item.get("session_id"), str) for item in entries):
            raise ValueError()
        entry_map = {item["session_id"]: item for item in entries}
        if len(entry_map) != len(entries):
            raise ValueError()
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise LiveEvidenceError("invalid_session_ledger") from None
    try:
        db = sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        sessions = db.execute("SELECT session_id,run_id,profile_id,state,epoch,notes_version FROM sessions ORDER BY created_at,session_id").fetchall()
        bound = [item for item in sessions if item["session_id"] in entry_map]
        by_run = defaultdict(list)
        for record in captures:
            runs = _hashes(record, "run")
            if len(runs) == 1:
                by_run[next(iter(runs))].append(record)
        result_sessions = []
        matched_capture_ids, matched_client_ids = set(), set()
        for session in bound:
            session_hash, run_hash = _sha(session["session_id"]), _sha(session["run_id"])
            entry = entry_map[session["session_id"]]
            user_id = entry.get("user_id")
            user_hash = _sha(user_id) if isinstance(user_id, str) and user_id else None
            capture_records = by_run[run_hash]
            matched_capture_ids.update(id(item) for item in capture_records)
            client_records = [item for item in clients if item.get("session_sha256") == session_hash and item.get("run_sha256") == run_hash]
            matched_client_ids.update(id(item) for item in client_records)
            backend, accepted, model_rounds, runtime_errors = _backend(db, session, fixture)
            channels = {"custom_llm": set(), "conv": set(), "subv": set()}
            asr_rounds, stage4_rounds, attempts = set(), set(), Counter()
            sources, stages, capture_errors, callback_status = Counter(), Counter(), Counter(), Counter()
            for record in capture_records:
                source = record.get("source")
                sources[source if source in {"custom_llm", "rts_event", "rtc_event", "capture_stream"} else "other"] += 1
                rounds = _hashes(record, "round")
                channel = "custom_llm" if source == "custom_llm" else record.get("tlv_tag")
                if channel in channels:
                    channels[channel].update(rounds)
                if source == "custom_llm":
                    attempts.update(rounds & accepted)
                    status = record.get("response_status")
                    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
                        callback_status[str(status)] += 1
                metadata = record.get("correlation", {})
                if not isinstance(metadata, dict):
                    continue
                stage = metadata.get("stage_code")
                if isinstance(stage, int) and not isinstance(stage, bool) and 0 <= stage <= 5:
                    stages[str(stage)] += 1
                    if stage == 4 and channel == "conv":
                        stage4_rounds.update(rounds)
                if metadata.get("error_code") is not None:
                    capture_errors[_safe(str(metadata["error_code"]))] += 1
                asr_rounds.update(_user_asr_rounds(record, user_hash))
            event_counts = _counts(item.get("kind") if item.get("kind") in _CLIENT_KINDS else "other" for item in client_records)
            sdk_errors = _counts(str(item["code"]) for item in client_records if item.get("kind") == "sdk_error" and isinstance(item.get("code"), int) and not isinstance(item["code"], bool))
            confirmations = {kind: any(item.get("kind") == kind and item.get("scope") == "human_report" for item in client_records) for kind in sorted(_HUMAN_KINDS)}
            failures = {"runtime:" + key: value for key, value in runtime_errors.items()}
            failures.update({"capture:" + key: value for key, value in capture_errors.items()})
            failures.update({"sdk:" + key: value for key, value in sdk_errors.items()})
            result_sessions.append({
                "session_sha256": session_hash, "run_sha256": run_hash,
                "profile": _safe(session["profile_id"], _PROFILE),
                "backend": backend,
                "correlation": {
                    "custom_llm_round_count": len(channels["custom_llm"]),
                    "accepted_callback_round_count": len(accepted & channels["custom_llm"]),
                    "accepted_conv_round_count": len(accepted & channels["conv"]),
                    "accepted_subv_round_count": len(accepted & channels["subv"]),
                    "accepted_user_asr_round_count": len(accepted & asr_rounds),
                    "accepted_user_asr_model_round_count": len(accepted & asr_rounds & model_rounds & channels["custom_llm"]),
                    "accepted_callback_model_round_count": len(accepted & model_rounds & channels["custom_llm"]),
                    "accepted_stage4_round_count": len(accepted & stage4_rounds),
                    "observed_callback_attempt_counts_per_accepted_round": sorted(attempts.values()),
                    "user_binding_available": user_hash is not None,
                },
                "capture": {"record_count": len(capture_records), "source_counts": dict(sorted(sources.items())),
                            "channel_round_counts": {key: len(value) for key, value in channels.items()},
                            "stage_counts": dict(sorted(stages.items())),
                            "callback_response_status_counts": dict(sorted(callback_status.items())),
                            "scope": "Same-run callback metadata; missing M0 capture_stream or response_status is normal for M1"},
                "client": {"record_count": len(client_records), "event_counts": event_counts,
                           "text_sent_count": event_counts.get("text_sent", 0), "human_confirmations": confirmations,
                           "scope_counts": _counts(item.get("scope") if item.get("scope") in {"human_report", "browser_sdk_observation"} else "other" for item in client_records),
                           "scope": "SDK observations and explicit human reports; text_sent is separate from voice and counts do not establish timing"},
                "observed_failure_codes": dict(sorted(failures.items())),
                "cleanup": _cleanup(entry),
            })
        db.rollback()
    except (sqlite3.Error, OSError, TypeError, ValueError):
        raise LiveEvidenceError("database_metadata_read_failed") from None
    finally:
        if "db" in locals():
            db.close()
    database_ids = {item["session_id"] for item in sessions}
    return {
        "schema_version": 1, "milestone": "M1",
        "scope": "Read-only observations for RTC-ledger-bound SQLite sessions; no automatic end-to-end or latency verdict",
        "inputs": {"capture_present": capture_present, "client_events_present": client_present},
        "exclusions": {"database_sessions_not_in_client_ledger": len(sessions) - len(bound),
                       "client_ledger_sessions_not_in_database": len(set(entry_map) - database_ids),
                       "capture_records_unmatched_run": len(captures) - len(matched_capture_ids),
                       "client_events_unmatched_session_run": len(clients) - len(matched_client_ids),
                       "invalid_capture_records": capture_invalid, "invalid_client_records": client_invalid},
        "sessions": result_sessions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--client", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--session-ledger", required=True)
    parser.add_argument("--fixture-env", help="Optional environment variable containing the test phrase; its contents are not printed")
    args = parser.parse_args()
    try:
        fixture = os.environ.get(args.fixture_env) if args.fixture_env else None
        if args.fixture_env and fixture is None:
            raise LiveEvidenceError("fixture_environment_missing")
        summary = summarize_m1_live(args.capture, args.client, args.database, args.session_ledger, fixture=fixture)
    except LiveEvidenceError as error:
        print(json.dumps({"error_code": str(error)}))
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
