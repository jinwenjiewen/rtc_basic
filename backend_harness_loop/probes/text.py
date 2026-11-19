"""Bounded HTTP verification of the local M1 text transport.

Run this module against an already running loopback service. Only synthetic
fixtures are submitted. The report contains hashes, booleans, counts, timings,
and fixed diagnostics; prompts, replies, credentials, and resource IDs stay in
memory. SQLite is opened read-only and scoped to sessions created by this run.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Awaitable, Callable
from urllib.parse import quote, urlsplit

import httpx


DEFAULT_PROFILES = ("deepseek_v4_flash", "deepseek_v4_pro")
ROUND_SECONDS = 45
REQUEST_SECONDS = 15
MAX_STREAM_CHARS = 262_144
MAX_REPLY_CHARS = 65_536
SAFE_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
STATES = {"running", "waiting_tool", "responding", "generation_completed", "cancelled", "failed"}
DELIVERY = {"unknown", "streamed_unconfirmed", "completed", "interrupted"}
TOOLS = {"context_read_history", "context_search_history", "context_update_notes", "prepare_response"}
TOOL_STATES = {"prepared", "dispatched", "success", "no_results", "timeout", "error", "cancelled", "unknown_outcome"}
BASE = "/api/m1/text/sessions"


class ProbeFailure(ValueError):
    """Only a bounded diagnostic is ever rendered, never exception text."""

    def __init__(self, code: str, status: int | None = None):
        self.code = code if isinstance(code, str) and SAFE_CODE.fullmatch(code) else "unsafe_error_code"
        self.status = status
        super().__init__(self.code)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError()
    except ValueError:
        raise ProbeFailure("loopback_http_url_required") from None
    return value.rstrip("/")


class _PageKey(HTMLParser):
    def __init__(self):
        super().__init__()
        self.keys: list[str] = []

    def handle_starttag(self, tag, attrs):
        selected = dict(attrs)
        if tag.lower() == "meta" and selected.get("name") == "m0-client-key":
            self.keys.append(selected.get("content", ""))


async def _error_response(response: httpx.Response):
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        raw.extend(chunk)
        if len(raw) > 65_536:
            raise ProbeFailure("http_error_body_limit", response.status_code)
    try:
        data = json.loads(raw)
        code = data.get("detail", {}).get("code")
    except (ValueError, TypeError, AttributeError):
        code = None
    raise ProbeFailure(code if isinstance(code, str) and SAFE_CODE.fullmatch(code)
                       else "http_request_failed", response.status_code)


async def _json(client: httpx.AsyncClient, path: str, body: dict | None = None) -> dict:
    async with asyncio.timeout(REQUEST_SECONDS):
        async with client.stream("GET" if body is None else "POST", path, json=body) as response:
            if response.status_code != 200:
                await _error_response(response)
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 65_536:
                    raise ProbeFailure("http_json_body_limit")
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ProbeFailure("invalid_http_json") from None
            if not isinstance(data, dict):
                raise ProbeFailure("invalid_http_object")
            return data


async def _page_key(client: httpx.AsyncClient) -> str:
    async with asyncio.timeout(REQUEST_SECONDS):
        async with client.stream("GET", "/m1/text") as response:
            if response.status_code != 200:
                await _error_response(response)
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 131_072:
                    raise ProbeFailure("text_page_limit")
    parser = _PageKey()
    try:
        parser.feed(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ProbeFailure("invalid_text_page") from None
    if (len(parser.keys) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", parser.keys[0])
            or parser.keys[0] == "__M0_CLIENT_KEY__"):
        raise ProbeFailure("missing_text_client_credential")
    return parser.keys[0]


async def _events(response: httpx.Response):
    """Parse SSE lines without depending on HTTP chunk boundaries."""
    event, data, total, frame = "message", [], 0, 0
    async for line in response.aiter_lines():
        total += len(line)
        if total > MAX_STREAM_CHARS or len(line) > 65_536:
            raise ProbeFailure("text_stream_limit")
        if not line:
            if data:
                try:
                    payload = json.loads("\n".join(data))
                except (ValueError, UnicodeError):
                    raise ProbeFailure("invalid_sse_json") from None
                if not isinstance(payload, dict):
                    raise ProbeFailure("invalid_sse_object")
                yield event, payload
            event, data, frame = "message", [], 0
        elif line.startswith(":"):
            continue
        else:
            name, separator, value = line.partition(":")
            if not separator:
                value = ""
            if value.startswith(" "):
                value = value[1:]
            if name == "event":
                event = value
            elif name == "data":
                frame += len(value)
                if frame > 65_536:
                    raise ProbeFailure("text_sse_event_limit")
                data.append(value)
    # This transport always terminates complete events with an empty line.
    if data:
        raise ProbeFailure("incomplete_sse_event")


@dataclass
class TurnResult:
    round_id: str = field(repr=False)
    turn_id: str = field(default="", repr=False)
    text: str = field(default="", repr=False)
    state: str = "unknown"
    delivery_status: str = "unknown"
    error_code: str | None = None
    first_delta_ms: float | None = None
    elapsed_ms: float = 0
    delta_count: int = 0
    turn_seen: bool = False
    terminal_seen: bool = False

    def metadata(self, fixture: str | None = None) -> dict:
        return {"round_sha256": _hash(self.round_id),
                "turn_sha256": _hash(self.turn_id) if self.turn_id else None,
                "state": self.state, "delivery_status": self.delivery_status,
                "error_code": self.error_code, "http_first_delta_ms": self.first_delta_ms,
                "elapsed_ms": self.elapsed_ms, "delta_count": self.delta_count,
                "reply_characters": len(self.text), "turn_seen": self.turn_seen,
                "terminal_seen": self.terminal_seen,
                "fixture_in_reply": fixture in self.text if fixture else None}


async def _turn(client: httpx.AsyncClient, session_id: str, text: str, *,
                after_turn: Callable[[], Awaitable[None]] | None = None,
                round_id: str | None = None) -> TurnResult:
    selected = round_id or "probe_" + secrets.token_hex(12)
    result = TurnResult(selected)
    started = time.perf_counter()
    try:
        async with asyncio.timeout(ROUND_SECONDS):
            async with client.stream("POST", BASE + "/" + quote(session_id, safe="") + "/turns",
                                     json={"round_id": selected, "text": text}) as response:
                if response.status_code != 200:
                    await _error_response(response)
                if not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
                    raise ProbeFailure("expected_text_event_stream")
                async for event, payload in _events(response):
                    if payload.get("round_id") != selected or result.terminal_seen:
                        raise ProbeFailure("sse_round_or_order_mismatch")
                    if event == "turn":
                        identifier, epoch = payload.get("turn_id"), payload.get("epoch")
                        if (result.turn_seen or not isinstance(identifier, str)
                                or not re.fullmatch(r"turn_[a-f0-9]{32}", identifier)
                                or not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1):
                            raise ProbeFailure("invalid_sse_turn")
                        result.turn_seen, result.turn_id = True, identifier
                        if after_turn is not None:
                            await after_turn()
                    elif event == "delta":
                        delta = payload.get("text")
                        if not result.turn_seen or not isinstance(delta, str):
                            raise ProbeFailure("invalid_sse_delta")
                        if delta.strip() and result.first_delta_ms is None:
                            result.first_delta_ms = _elapsed(started)
                        result.text += delta
                        result.delta_count += 1
                        if len(result.text) > MAX_REPLY_CHARS:
                            raise ProbeFailure("text_reply_limit")
                    elif event == "terminal":
                        if (not result.turn_seen or payload.get("state") not in {"generation_completed", "cancelled", "failed"}
                                or payload.get("delivery_status") not in DELIVERY):
                            raise ProbeFailure("invalid_sse_terminal")
                        error = payload.get("error_code")
                        if error is not None and (not isinstance(error, str) or not SAFE_CODE.fullmatch(error)):
                            raise ProbeFailure("invalid_terminal_error_code")
                        result.state, result.delivery_status = payload["state"], payload["delivery_status"]
                        result.error_code, result.terminal_seen = error, True
                    else:
                        raise ProbeFailure("unexpected_sse_event")
                if not result.terminal_seen:
                    raise ProbeFailure("stream_closed_before_terminal")
    finally:
        result.elapsed_ms = _elapsed(started)
    return result


def _database(path: Path, session_id: str, profile: str, fixture: str = "", foreign: str = "") -> dict:
    """Return aggregate evidence only; never materialize prompts or notes."""
    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=.25)) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute("""SELECT state,profile_id=?,notes_version,
                CASE WHEN ?='' THEN NULL ELSE instr(notes_text,?)>0 END,
                CASE WHEN ?='' THEN NULL ELSE instr(notes_text,?)>0 END
                FROM sessions WHERE session_id=?""",
                (profile, fixture, fixture, foreign, foreign, session_id)).fetchone()
            if row is None:
                raise ProbeFailure("probe_session_not_in_database")
            tools: dict[str, dict[str, int]] = {}
            for name, state, count in db.execute("""SELECT c.name,c.state,COUNT(*) FROM tool_calls c
                JOIN turns t ON c.turn_id=t.turn_id WHERE t.session_id=? GROUP BY c.name,c.state""", (session_id,)):
                name = name if name in TOOLS else "other"
                state = state if state in TOOL_STATES else "other"
                bucket = tools.setdefault(name, {})
                bucket[state] = bucket.get(state, 0) + count
            turns = {state if state in STATES else "other": count for state, count in
                     db.execute("SELECT state,COUNT(*) FROM turns WHERE session_id=? GROUP BY state", (session_id,))}
            messages = db.execute("SELECT COUNT(*) FROM ledger WHERE session_id=? AND kind='message'", (session_id,)).fetchone()[0]
            model = db.execute("""SELECT COUNT(*),COALESCE(SUM(json_extract(payload_json,'$.profile_id') IS NOT ?),0)
                FROM ledger WHERE session_id=? AND kind='model.started'""", (profile, session_id)).fetchone()
            per_turn = db.execute("""SELECT COALESCE(MAX(n),0) FROM (SELECT COUNT(*) AS n FROM ledger
                WHERE session_id=? AND kind='model.started' GROUP BY turn_id)""", (session_id,)).fetchone()[0]
            return {"session_closed": row[0] == "closed", "profile_matches": bool(row[1]),
                    "notes_version": row[2], "fixture_in_notes": bool(row[3]) if row[3] is not None else None,
                    "foreign_fixture_in_notes": bool(row[4]) if row[4] is not None else None,
                    "tool_counts": tools, "turn_state_counts": turns, "message_count": messages,
                    "model_call_count": model[0], "model_profile_matches": model[1] == 0,
                    "max_observed_model_calls_per_turn": per_turn}
    except (OSError, sqlite3.Error, ValueError):
        raise ProbeFailure("database_read_failed") from None


def _failure(exc: Exception) -> dict:
    if isinstance(exc, ProbeFailure):
        return {"code": exc.code, **({"http_status": exc.status} if exc.status is not None else {})}
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return {"code": "text_probe_timeout"}
    if isinstance(exc, httpx.HTTPError):
        return {"code": "text_probe_network_error"}
    return {"code": "text_probe_internal_error"}


def _tool_success(database: dict, names: set[str]) -> int:
    return sum(database.get("tool_counts", {}).get(name, {}).get("success", 0) for name in names)


async def _create(client: httpx.AsyncClient, profile: str) -> tuple[str, dict]:
    data = await _json(client, BASE, {"profile_id": profile})
    identifier = data.get("session_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"text_session_[a-f0-9]{32}", identifier):
        raise ProbeFailure("invalid_text_session_response")
    return identifier, data


def _session_metadata(data: dict, profile: str):
    if data.get("profile_id") != profile or data.get("mode") != "agent_loop" or data.get("transport") != "text":
        raise ProbeFailure("invalid_text_session_response")


async def _heartbeat(client: httpx.AsyncClient, session_id: str):
    if (await _json(client, BASE + "/" + session_id + "/heartbeat", {})).get("alive") is not True:
        raise ProbeFailure("text_heartbeat_unconfirmed")


async def _cleanup(client: httpx.AsyncClient, database: Path, session_id: str, profile: str) -> dict:
    result = {"stop_confirmed": False, "database_closed": False, "passed": False}
    try:
        result["stop_confirmed"] = (await _json(client, BASE + "/" + session_id + "/stop", {})).get("stopped") is True
        result["database_closed"] = _database(database, session_id, profile)["session_closed"]
        result["passed"] = result["stop_confirmed"] and result["database_closed"]
    except Exception as exc:
        result["failure"] = _failure(exc)
    return result


async def _profile_case(client: httpx.AsyncClient, database: Path, profile: str, fixture: str,
                        foreign: str, index: int) -> dict:
    report = {"profile_index": index, "profile_sha256": _hash(profile), "fixture_sha256": _hash(fixture),
              "passed": False, "rounds": []}
    session_id = None
    try:
        session_id, session_data = await _create(client, profile)
        report["session_sha256"] = _hash(session_id)
        _session_metadata(session_data, profile)
        first = await _turn(client, session_id,
            f"这是一条合成测试信息。请把我的专属代号“{fixture}”原样写入工作笔记，调用上下文工具保存。完成后只简短确认。")
        report["rounds"].append({"purpose": "write_notes", **first.metadata()})
        await _heartbeat(client, session_id)
        recalled = await _turn(client, session_id, "请从当前会话的工作笔记回忆我刚才保存的完整专属代号，只回答该代号。")
        report["rounds"].append({"purpose": "recall_notes", **recalled.metadata(fixture)})
        before_history = _database(database, session_id, profile, fixture, foreign)
        history_required = "flash" in profile.lower()
        report["history_tool_check_required"] = history_required
        history_ok = True
        if history_required:
            await _heartbeat(client, session_id)
            checked = await _turn(client, session_id,
                "请明确调用 context_read_history 或 context_search_history 工具核对本会话此前的专属代号，再只回答完整代号。")
            report["rounds"].append({"purpose": "verify_history_tool", **checked.metadata(fixture)})
            after_history = _database(database, session_id, profile, fixture, foreign)
            report["history_tool_success_count_added"] = (_tool_success(after_history, {"context_read_history", "context_search_history"})
                - _tool_success(before_history, {"context_read_history", "context_search_history"}))
            history_ok = (checked.state == "generation_completed" and fixture in checked.text
                          and report["history_tool_success_count_added"] > 0)
        evidence = _database(database, session_id, profile, fixture, foreign)
        report["database"] = evidence
        report["passed"] = bool(first.state == recalled.state == "generation_completed"
            and fixture in recalled.text and evidence["fixture_in_notes"] and not evidence["foreign_fixture_in_notes"]
            and evidence["profile_matches"] and evidence["model_profile_matches"]
            and evidence["model_call_count"] > 0 and _tool_success(evidence, {"context_update_notes"}) > 0 and history_ok)
        if not report["passed"]:
            report["failure"] = {"code": "model_fixture_expectation_failed"}
    except Exception as exc:
        report["failure"] = _failure(exc)
    finally:
        if session_id is not None:
            report["cleanup"] = await _cleanup(client, database, session_id, profile)
            report["passed"] = report["passed"] and report["cleanup"]["passed"]
    return report


async def _control_case(client: httpx.AsyncClient, database: Path, profile: str) -> dict:
    report = {"profile_sha256": _hash(profile), "passed": False, "rounds": []}
    session_id = None
    try:
        session_id, session_data = await _create(client, profile)
        report["session_sha256"] = _hash(session_id)
        _session_metadata(session_data, profile)
        old_round = "probe_" + secrets.token_hex(12)

        async def early_interrupt():
            response = await _json(client, BASE + "/" + session_id + "/interrupt", {"round_id": old_round})
            report["early_interrupt_acknowledged"] = response.get("interrupted") is True and response.get("round_id") == old_round

        cancelled = await _turn(client, session_id, "请简要介绍三个适合下雨天的室内活动。",
                                round_id=old_round, after_turn=early_interrupt)
        report["rounds"].append({"purpose": "early_interrupt", **cancelled.metadata()})
        await _heartbeat(client, session_id)

        async def stale_interrupt():
            response = await _json(client, BASE + "/" + session_id + "/interrupt", {"round_id": old_round})
            report["old_interrupt_reported_unchanged"] = response.get("interrupted") is False and response.get("round_id") == old_round

        next_text = "这是一条新的合成测试输入。请用一句话说明雨后为什么常有清新气味。"
        current = await _turn(client, session_id, next_text, after_turn=stale_interrupt)
        report["rounds"].append({"purpose": "new_round_survives_old_interrupt", **current.metadata()})
        before = _database(database, session_id, profile)
        duplicate_code = None
        try:
            await _json(client, BASE + "/" + session_id + "/turns", {"round_id": current.round_id, "text": next_text})
        except ProbeFailure as exc:
            report["duplicate_http_status"], duplicate_code = exc.status, exc.code
        report["duplicate_error_code"] = duplicate_code
        after = _database(database, session_id, profile)
        report["duplicate_message_count_unchanged"] = before["message_count"] == after["message_count"]
        report["duplicate_model_call_count_unchanged"] = before["model_call_count"] == after["model_call_count"]
        report["database"] = after
        report["passed"] = bool(report.get("early_interrupt_acknowledged") and cancelled.state == "cancelled"
            and report.get("old_interrupt_reported_unchanged") and current.state == "generation_completed"
            and current.text.strip() and report.get("duplicate_http_status") == 409
            and report["duplicate_message_count_unchanged"] and report["duplicate_model_call_count_unchanged"])
        if not report["passed"]:
            report["failure"] = {"code": "control_expectation_failed"}
    except Exception as exc:
        report["failure"] = _failure(exc)
    finally:
        if session_id is not None:
            report["cleanup"] = await _cleanup(client, database, session_id, profile)
            report["passed"] = report["passed"] and report["cleanup"]["passed"]
    return report


async def run_probe(base_url: str, database: Path, profiles: list[str]) -> dict:
    """No model retries, profile fallback, RTC activity, or SQLite writes."""
    started = time.perf_counter()
    report = {"schema_version": 1, "probe": "m1_text_http", "passed": False,
              "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "limits": {"round_seconds": ROUND_SECONDS, "request_seconds": REQUEST_SECONDS,
                         "max_profiles": 2, "max_turns": 8, "model_budget_owner": "runtime",
                         "automatic_retries": 0, "profile_fallback": False},
              "scope": {"synthetic_fixtures_only": True, "sqlite_read_only": True,
                        "first_delta_is_http_text_not_audio": True, "rtc_used": False},
              "profiles": []}
    try:
        base_url = _base_url(base_url)
        if (not 1 <= len(profiles) <= 2 or len(set(profiles)) != len(profiles)
                or any(not SAFE_CODE.fullmatch(profile) for profile in profiles)):
            raise ProbeFailure("invalid_probe_profiles")
        if not database.is_file():
            raise ProbeFailure("database_unavailable")
        report["limits"]["planned_turns"] = 2 * len(profiles) + sum("flash" in profile.lower() for profile in profiles) + 2
        report["loopback_origin_sha256"] = _hash(base_url)
        fixtures = [("蓝色" if index == 0 else "绿色") + "代号-" + secrets.token_hex(8) for index in range(len(profiles))]
        async with httpx.AsyncClient(base_url=base_url, timeout=ROUND_SECONDS, trust_env=False,
                                     follow_redirects=False, limits=httpx.Limits(max_connections=4)) as client:
            client.headers["X-M0-Client-Key"] = await _page_key(client)
            for index, profile in enumerate(profiles):
                other = fixtures[1 - index] if len(fixtures) == 2 else ""
                report["profiles"].append(await _profile_case(client, database, profile, fixtures[index], other, index))
            report["control"] = await _control_case(client, database, profiles[0])
        report["cleanup_confirmed"] = all(item.get("cleanup", {}).get("passed") is True
            for item in [*report["profiles"], report["control"]])
        report["passed"] = report["cleanup_confirmed"] and all(item["passed"] for item in report["profiles"]) and report["control"]["passed"]
    except Exception as exc:
        report["failure"] = _failure(exc)
        report.setdefault("cleanup_confirmed", False)
    report["elapsed_ms"] = _elapsed(started)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded metadata-only self-test of an already running local M1 text service")
    parser.add_argument("--base-url", default="http://127.0.0.1:3101")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", action="append", help="Repeat at most twice; defaults to Flash and Pro")
    args = parser.parse_args()
    try:
        report = asyncio.run(run_probe(args.base_url, args.database, args.profile or list(DEFAULT_PROFILES)))
    except KeyboardInterrupt:
        report = {"schema_version": 1, "probe": "m1_text_http", "passed": False,
                  "failure": {"code": "text_probe_interrupted"}}
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        print("text_probe_report_write_failed")
        return 2
    print("text_probe_passed" if report["passed"] else "text_probe_failed")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
