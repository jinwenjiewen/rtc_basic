"""Single-worker M1/M2 ledger and context state in a local SQLite file.

Transactions are short and never span model/tool network waits. A persisted
epoch/connection fence protects semantic writes; delivery/audit records may
still describe an old turn, without touching the current working context.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
import uuid


ACTIVE = frozenset({"running", "waiting_tool", "responding"})
TERMINAL = frozenset({"generation_completed", "cancelled", "failed"})
TOOL_TERMINAL = frozenset({"success", "no_results", "timeout", "error", "cancelled", "unknown_outcome"})
_HIDDEN_FIELDS = frozenset({"reasoning_content", "reasoning", "reasoning_details", "analysis", "thinking", "chain_of_thought"})
_SUMMARY_SECTIONS = frozenset({"goals", "items", "open_questions", "pending_actions", "topic_index"})
_SOURCE_REF = re.compile(r"seq:([1-9][0-9]{0,18})\Z")


class StoreError(ValueError):
    """Safe errors contain only stable codes, never dialogue or credentials."""
    code = "store_error"

    def __init__(self, code: str | None = None):
        super().__init__(code or self.code)


class StaleTurn(StoreError):
    code = "stale_turn"


class ContextConflict(StoreError):
    code = "context_version_conflict"


class RoundConflict(StoreError):
    code = "round_content_conflict"


class RoundCancelled(StoreError):
    code = "round_cancelled"


class DuplicateTurn(StoreError):
    code = "duplicate_round"

    def __init__(self, target: TurnIdentity, status: str):
        super().__init__()
        self.target, self.status = target, status


def _context_deadline(deadline, code="context_compaction_timeout"):
    if deadline is None:
        return
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise StoreError("invalid_context_deadline")
    if time.monotonic() >= deadline:
        raise StoreError(code)


@dataclass(frozen=True)
class TurnIdentity:
    session_id: str
    run_id: str
    turn_id: str
    epoch: int
    connection_id: str
    round_id: str


def _identifier(value: Any, name: str, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise StoreError("invalid_" + name)
    return value


def _round(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise StoreError("invalid_round_id")
    return _identifier(str(value), "round_id", 128)


def _persistable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _persistable(item) for key, item in value.items() if key not in _HIDDEN_FIELDS}
    if isinstance(value, (list, tuple)):
        return [_persistable(item) for item in value]
    return value


def _json(value: Any, *, maximum: int = 131072) -> str:
    try:
        encoded = json.dumps(_persistable(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        raise StoreError("invalid_json_value") from None
    if len(encoded.encode("utf-8")) > maximum:
        raise StoreError("ledger_payload_limit")
    return encoded


def _native(message: Any) -> dict:
    if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool"}:
        raise StoreError("invalid_native_message")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise StoreError("text_only_message_required")
    # Preserve the provider's object in the live caller; only a sanitized copy
    # crosses the persistence boundary. Opaque reasoning stays in turn memory.
    clean = {key: value for key, value in message.items() if key in {"role", "content", "tool_calls", "tool_call_id", "name"}}
    if message["role"] == "tool":
        _identifier(message.get("tool_call_id"), "tool_call_id")
    return json.loads(_json(clean))


class RuntimeStore:
    def __init__(self, path: Path | str):
        self.path = Path(path) if str(path) != ":memory:" else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=.25, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                profile_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
                epoch INTEGER NOT NULL DEFAULT 0, notes_version INTEGER NOT NULL DEFAULT 0,
                notes_text TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turns (
                turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
                run_id TEXT NOT NULL, round_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                connection_id TEXT NOT NULL, state TEXT NOT NULL, user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL DEFAULT '', delivery_text TEXT NOT NULL DEFAULT '',
                delivery_status TEXT NOT NULL DEFAULT 'unknown', created_at REAL NOT NULL,
                completed_at REAL, UNIQUE(session_id,round_id), UNIQUE(session_id,epoch)
            );
            CREATE TABLE IF NOT EXISTS product_owners (
                session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
                user_id TEXT NOT NULL, login_id TEXT NOT NULL, expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ledger (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(session_id), turn_id TEXT,
                epoch INTEGER NOT NULL, kind TEXT NOT NULL, role TEXT,
                payload_json TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ledger_session ON ledger(session_id,seq);
            CREATE INDEX IF NOT EXISTS ledger_session_turn ON ledger(session_id,turn_id,seq);
            CREATE TABLE IF NOT EXISTS tool_calls (
                turn_id TEXT NOT NULL REFERENCES turns(turn_id), tool_call_id TEXT NOT NULL,
                name TEXT NOT NULL, arguments TEXT NOT NULL, state TEXT NOT NULL,
                assistant_seq INTEGER NOT NULL, result_json TEXT, PRIMARY KEY(turn_id,tool_call_id)
            );
            CREATE TABLE IF NOT EXISTS knowledge_refs (
                reference TEXT NOT NULL, turn_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                PRIMARY KEY(reference,turn_id,tool_call_id),
                FOREIGN KEY(turn_id,tool_call_id) REFERENCES tool_calls(turn_id,tool_call_id)
            );
            CREATE TABLE IF NOT EXISTS cancelled_rounds (
                session_id TEXT NOT NULL REFERENCES sessions(session_id), round_id TEXT NOT NULL,
                reason TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(session_id,round_id)
            );
            CREATE TABLE IF NOT EXISTS context_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                context_version INTEGER NOT NULL, base_version INTEGER NOT NULL,
                compacted_through_seq INTEGER NOT NULL,
                summary_json TEXT NOT NULL, selected_refs_json TEXT NOT NULL,
                input_ref_json TEXT NOT NULL, created_at REAL NOT NULL,
                owner_turn_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL,
                UNIQUE(session_id,context_version)
            );
            CREATE TABLE IF NOT EXISTS context_candidates (
                candidate_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                parent_id TEXT REFERENCES context_candidates(candidate_id),
                base_version INTEGER NOT NULL, notes_version INTEGER NOT NULL,
                previous_compacted_through_seq INTEGER NOT NULL,
                segment_after_seq INTEGER NOT NULL, covered_through_seq INTEGER NOT NULL,
                summary_json TEXT NOT NULL, selected_refs_json TEXT NOT NULL,
                input_ref_json TEXT NOT NULL, prefix_fingerprint TEXT NOT NULL,
                recovery_key TEXT NOT NULL, created_at REAL NOT NULL,
                owner_turn_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS context_candidate_session
                ON context_candidates(session_id,base_version,created_at);
            CREATE TABLE IF NOT EXISTS context_edits (
                revision_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                context_version INTEGER NOT NULL, base_version INTEGER NOT NULL,
                operation TEXT NOT NULL, compacted_through_seq INTEGER NOT NULL,
                summary_json TEXT NOT NULL, selected_refs_json TEXT NOT NULL,
                selection_purpose TEXT NOT NULL, input_ref_json TEXT NOT NULL,
                created_at REAL NOT NULL, owner_turn_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL,
                UNIQUE(session_id,context_version)
            );
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(sessions)")}
        if "context_version" not in columns:
            self.db.execute("ALTER TABLE sessions ADD COLUMN context_version INTEGER NOT NULL DEFAULT 0")
        self._recover()

    @contextmanager
    def _transaction(self):
        if self.db.in_transaction:
            savepoint = "store_" + uuid.uuid4().hex
            self.db.execute("SAVEPOINT " + savepoint)
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK TO " + savepoint)
                self.db.execute("RELEASE " + savepoint)
                raise
            else:
                self.db.execute("RELEASE " + savepoint)
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def bind_product_owner(self, session_id: str, user_id: str, login_id: str, expires_at: float):
        with self._transaction():
            existing = self.product_owner(session_id)
            owner = {"session_id": session_id, "user_id": user_id, "login_id": login_id, "expires_at": expires_at}
            if existing and existing != owner:
                raise StoreError("product_owner_conflict")
            if not existing:
                self.db.execute("INSERT INTO product_owners VALUES(?,?,?,?)", (session_id, user_id, login_id, expires_at))

    def product_owner(self, session_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM product_owners WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def _event(self, session_id: str, turn_id: str | None, epoch: int, kind: str, payload: Any, role: str | None = None) -> int:
        result = self.db.execute(
            "INSERT INTO ledger(session_id,turn_id,epoch,kind,role,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (session_id, turn_id, epoch, kind, role, _json(payload), time.time()),
        )
        return int(result.lastrowid)

    def _target(self, row: sqlite3.Row) -> TurnIdentity:
        return TurnIdentity(row["session_id"], row["run_id"], row["turn_id"], row["epoch"], row["connection_id"], row["round_id"])

    def _matches(self, target: TurnIdentity) -> sqlite3.Row | None:
        row = self.db.execute("SELECT * FROM turns WHERE turn_id=?", (target.turn_id,)).fetchone()
        return row if row is not None and self._target(row) == target else None

    def _current(self, target: TurnIdentity) -> bool:
        row = self.db.execute("""SELECT t.state,s.epoch,s.state AS session_state
            FROM turns t JOIN sessions s ON t.session_id=s.session_id
            WHERE t.turn_id=? AND t.session_id=? AND t.run_id=? AND t.epoch=? AND t.connection_id=? AND t.round_id=?""",
            (target.turn_id, target.session_id, target.run_id, target.epoch, target.connection_id, target.round_id)).fetchone()
        return bool(row and row["state"] in ACTIVE and row["epoch"] == target.epoch and row["session_state"] == "active")

    def _require_current(self, target: TurnIdentity):
        if not self._current(target):
            raise StaleTurn()

    def _close_tools(self, target: TurnIdentity, code: str, status: str):
        rows = self.db.execute("SELECT tool_call_id FROM tool_calls WHERE turn_id=? AND state IN ('prepared','dispatched') ORDER BY assistant_seq,rowid", (target.turn_id,)).fetchall()
        for row in rows:
            result = {"status": status, "data": None, "error": {"code": code}, "truncated": False}
            self._finish_tool(target, row["tool_call_id"], result)

    def _cancel(self, row: sqlite3.Row, reason: str):
        if row["state"] not in ACTIVE:
            raise StaleTurn()
        target = self._target(row)
        self._close_tools(target, reason, "cancelled")
        self.db.execute("UPDATE turns SET state='cancelled',delivery_status='interrupted',completed_at=? WHERE turn_id=?", (time.time(), target.turn_id))
        if row["delivery_text"] and not row["assistant_text"]:
            self._event(target.session_id, target.turn_id, target.epoch, "message", {"role": "assistant", "content": row["delivery_text"]}, "assistant")
        self._event(target.session_id, target.turn_id, target.epoch, "turn.cancelled", {"reason": reason})

    def _interrupt_playback(self, row: sqlite3.Row, reason: str):
        """Playback may stop after generation has already reached its final state."""
        target = self._target(row)
        if row["delivery_status"] != "completed":
            self.record_delivery(target, "", "interrupted")
        self._event(target.session_id, target.turn_id, target.epoch, "playback.interrupted", {
            "reason": reason, "generation_status": row["state"],
            "delivery_status": "completed" if row["delivery_status"] == "completed" else "interrupted",
        })

    def _recover(self):
        with self._transaction():
            interrupted = self.db.execute("SELECT * FROM turns WHERE state IN ('running','waiting_tool','responding')").fetchall()
            sessions = set()
            for row in interrupted:
                target = self._target(row)
                self._close_tools(target, "runtime_restarted", "error")
                self.db.execute("UPDATE turns SET state='failed',delivery_status='unknown',completed_at=? WHERE turn_id=?", (time.time(), target.turn_id))
                if row["delivery_text"] and not row["assistant_text"]:
                    self._event(target.session_id, target.turn_id, target.epoch, "message", {"role": "assistant", "content": row["delivery_text"]}, "assistant")
                self._event(target.session_id, target.turn_id, target.epoch, "turn.recovered", {"status": "failed", "code": "runtime_restarted", "auto_replay": False})
                sessions.add(target.session_id)
            for session_id in sessions:
                self.db.execute("UPDATE sessions SET epoch=epoch+1 WHERE session_id=?", (session_id,))

    def create_session(self, session_id: str, run_id: str, profile_id: str) -> dict:
        for name, value in (("session_id", session_id), ("run_id", run_id), ("profile_id", profile_id)):
            _identifier(value, name)
        with self._transaction():
            current = self.db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if current:
                if current["run_id"] != run_id or current["profile_id"] != profile_id or current["state"] != "active":
                    raise StoreError("session_binding_conflict")
            else:
                try:
                    self.db.execute("INSERT INTO sessions(session_id,run_id,profile_id,created_at) VALUES(?,?,?,?)", (session_id, run_id, profile_id, time.time()))
                except sqlite3.IntegrityError:
                    raise StoreError("run_binding_conflict") from None
                self._event(session_id, None, 0, "session.created", {"profile_id": profile_id})
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict:
        row = self.db.execute("SELECT session_id,run_id,profile_id,state,epoch,notes_version,context_version,created_at FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise StoreError("unknown_session")
        return dict(row)

    def get_profile_id(self, session_id: str) -> str:
        return self.get_session(session_id)["profile_id"]

    def admit_turn(self, session_id: str, provider_round_id: str | int, user_text: str, connection_id: str) -> TurnIdentity:
        round_id = _round(provider_round_id)
        _identifier(connection_id, "connection_id")
        if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > 32000:
            raise StoreError("invalid_user_text")
        with self._transaction():
            session = self.get_session(session_id)
            if session["state"] != "active":
                raise StoreError("session_closed")
            prior = self.db.execute("SELECT * FROM turns WHERE session_id=? AND round_id=?", (session_id, round_id)).fetchone()
            if prior:
                if prior["user_text"] != user_text:
                    raise RoundConflict()
                raise DuplicateTurn(self._target(prior), prior["state"])
            if self.db.execute("SELECT 1 FROM cancelled_rounds WHERE session_id=? AND round_id=?", (session_id, round_id)).fetchone():
                raise RoundCancelled()
            for row in self.db.execute("SELECT * FROM turns WHERE session_id=? AND state IN ('running','waiting_tool','responding')", (session_id,)).fetchall():
                self._cancel(row, "superseded_by_input")
            epoch = session["epoch"] + 1
            target = TurnIdentity(session_id, session["run_id"], "turn_" + uuid.uuid4().hex, epoch, connection_id, round_id)
            self.db.execute("UPDATE sessions SET epoch=? WHERE session_id=?", (epoch, session_id))
            self.db.execute("INSERT INTO turns(turn_id,session_id,run_id,round_id,epoch,connection_id,state,user_text,created_at) VALUES(?,?,?,?,?,?,'running',?,?)",
                            (target.turn_id, session_id, target.run_id, round_id, epoch, connection_id, user_text, time.time()))
            self._event(session_id, target.turn_id, epoch, "turn.admitted", {"round_id": round_id, "connection_id": connection_id})
            self._event(session_id, target.turn_id, epoch, "message", {"role": "user", "content": user_text}, "user")
        return target

    def lookup_round(self, session_id: str, round_id: str | int) -> dict | None:
        row = self.db.execute("SELECT * FROM turns WHERE session_id=? AND round_id=?", (session_id, _round(round_id))).fetchone()
        return {"target": self._target(row), "status": row["state"]} if row else None

    def is_current(self, target: TurnIdentity) -> bool:
        return self._current(target)

    def set_turn_status(self, target: TurnIdentity, status: str) -> bool:
        if status not in ACTIVE:
            raise StoreError("invalid_active_turn_status")
        with self._transaction():
            if not self._current(target):
                return False
            self.db.execute("UPDATE turns SET state=? WHERE turn_id=?", (status, target.turn_id))
        return True

    def interrupt(self, session_id: str, *, turn_id: str | None = None, epoch: int | None = None,
                  round_id: str | int | None = None, connection_id: str | None = None) -> bool:
        if epoch is not None and (not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1):
            return False
        if (turn_id is not None and not isinstance(turn_id, str)) or (connection_id is not None and not isinstance(connection_id, str)):
            return False
        if round_id is None and (turn_id is None or epoch is None):
            return False
        with self._transaction():
            session = self.get_session(session_id)
            row = self.db.execute("SELECT * FROM turns WHERE session_id=? AND epoch=?", (session_id, session["epoch"])).fetchone()
            if not row or session["state"] != "active" or row["state"] == "cancelled":
                return False
            if ((turn_id is not None and row["turn_id"] != turn_id) or (epoch is not None and row["epoch"] != epoch)
                or (round_id is not None and row["round_id"] != _round(round_id))
                or (connection_id is not None and row["connection_id"] != connection_id)):
                return False
            self.db.execute("INSERT OR IGNORE INTO cancelled_rounds VALUES(?,?,?,?)", (session_id, row["round_id"], "user_interrupt", time.time()))
            if row["state"] in ACTIVE:
                self._cancel(row, "user_interrupt")
            else:
                self._interrupt_playback(row, "user_interrupt")
            self.db.execute("UPDATE sessions SET epoch=epoch+1 WHERE session_id=?", (session_id,))
        return True

    def cancel_round(self, session_id: str, round_id: str | int, reason: str = "rtc_interrupt") -> bool:
        round_id = _round(round_id)
        _identifier(reason, "cancellation_reason", 80)
        with self._transaction():
            session = self.get_session(session_id)
            result = self.db.execute("INSERT OR IGNORE INTO cancelled_rounds VALUES(?,?,?,?)", (session_id, round_id, reason, time.time()))
            row = self.db.execute("SELECT * FROM turns WHERE session_id=? AND round_id=?", (session_id, round_id)).fetchone()
            if row:
                current = row["epoch"] == session["epoch"] and session["state"] == "active"
                if current and row["state"] in ACTIVE:
                    self._cancel(row, reason)
                elif row["state"] in {"generation_completed", "failed"}:
                    self._interrupt_playback(row, reason)
                if current and row["state"] != "cancelled":
                    self.db.execute("UPDATE sessions SET epoch=epoch+1 WHERE session_id=?", (session_id,))
            self._event(session_id, row["turn_id"] if row else None, row["epoch"] if row else session["epoch"], "round.cancelled", {"round_id": round_id, "reason": reason})
        return result.rowcount > 0

    def append_event(self, target: TurnIdentity, kind: str, payload: dict | None = None) -> int:
        _identifier(kind, "event_kind", 80)
        if self._matches(target) is None:
            raise StoreError("unknown_turn")
        return self._event(target.session_id, target.turn_id, target.epoch, kind, payload or {})

    def save_message(self, target: TurnIdentity, message: dict) -> int:
        message = _native(message)
        if message["role"] == "tool" or message.get("tool_calls"):
            raise StoreError("use_tool_lifecycle_for_native_calls")
        with self._transaction():
            self._require_current(target)
            return self._event(target.session_id, target.turn_id, target.epoch, "message", message, message["role"])

    def prepare_tool_calls(self, target: TurnIdentity, assistant_message: dict) -> list[dict]:
        message = _native(assistant_message)
        calls = message.get("tool_calls")
        if message["role"] != "assistant" or not isinstance(calls, list) or not 1 <= len(calls) <= 8:
            raise StoreError("invalid_tool_call_batch")
        identifiers = set()
        for call in calls:
            if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("function"), dict):
                raise StoreError("invalid_tool_call")
            identifier = _identifier(call.get("id"), "tool_call_id")
            name = _identifier(call["function"].get("name"), "tool_name", 100)
            arguments = call["function"].get("arguments")
            if identifier in identifiers or not isinstance(arguments, str) or len(arguments) > 16000:
                raise StoreError("invalid_tool_arguments")
            identifiers.add(identifier)
        with self._transaction():
            self._require_current(target)
            existing = [self.db.execute("SELECT * FROM tool_calls WHERE turn_id=? AND tool_call_id=?", (target.turn_id, call["id"])).fetchone() for call in calls]
            if any(existing):
                if not all(existing) or any(row["name"] != call["function"]["name"] or row["arguments"] != call["function"]["arguments"] for row, call in zip(existing, calls)):
                    raise StoreError("tool_call_identity_conflict")
                return [dict(row) for row in existing]
            seq = self._event(target.session_id, target.turn_id, target.epoch, "message", message, "assistant")
            for call in calls:
                self.db.execute("INSERT INTO tool_calls(turn_id,tool_call_id,name,arguments,state,assistant_seq) VALUES(?,?,?,?,'prepared',?)",
                                (target.turn_id, call["id"], call["function"]["name"], call["function"]["arguments"], seq))
            self._event(target.session_id, target.turn_id, target.epoch, "tools.prepared", {"count": len(calls), "tool_call_ids": [call["id"] for call in calls]})
        return [dict(self.db.execute("SELECT * FROM tool_calls WHERE turn_id=? AND tool_call_id=?", (target.turn_id, call["id"])).fetchone()) for call in calls]

    def mark_tool_dispatched(self, target: TurnIdentity, tool_call_id: str) -> bool:
        with self._transaction():
            if not self._current(target):
                return False
            row = self.db.execute("SELECT state FROM tool_calls WHERE turn_id=? AND tool_call_id=?", (target.turn_id, tool_call_id)).fetchone()
            if not row:
                raise StoreError("tool_call_not_prepared")
            if row["state"] != "prepared":
                return False
            self.db.execute("UPDATE tool_calls SET state='dispatched' WHERE turn_id=? AND tool_call_id=?", (target.turn_id, tool_call_id))
            self._event(target.session_id, target.turn_id, target.epoch, "tool.dispatched", {"tool_call_id": tool_call_id})
        return True

    def _finish_tool(self, target: TurnIdentity, tool_call_id: str, result: dict) -> bool:
        if not isinstance(result, dict) or not isinstance(result.get("status"), str) or result["status"] not in TOOL_TERMINAL:
            raise StoreError("invalid_tool_result")
        encoded = _json(result, maximum=32768)
        row = self.db.execute("SELECT state,result_json,name FROM tool_calls WHERE turn_id=? AND tool_call_id=?", (target.turn_id, tool_call_id)).fetchone()
        if not row:
            raise StoreError("tool_call_not_prepared")
        if row["state"] in TOOL_TERMINAL:
            if row["result_json"] != encoded:
                raise StoreError("tool_result_conflict")
            return True
        self.db.execute("UPDATE tool_calls SET state=?,result_json=? WHERE turn_id=? AND tool_call_id=?", (result["status"], encoded, target.turn_id, tool_call_id))
        if row["name"] == "knowledge_search" and result["status"] == "success":
            data = result.get("data")
            hits = data.get("hits") if isinstance(data, dict) else None
            if not isinstance(hits, list) or not 1 <= len(hits) <= 5:
                raise StoreError("invalid_knowledge_result")
            for hit in hits:
                reference = hit.get("reference") if isinstance(hit, dict) else None
                if not isinstance(reference, str) or not re.fullmatch(r"kb:[a-f0-9]{32}", reference):
                    raise StoreError("invalid_knowledge_reference")
                self.db.execute("INSERT INTO knowledge_refs(reference,turn_id,tool_call_id) VALUES(?,?,?)",
                                (reference, target.turn_id, tool_call_id))
        self._event(target.session_id, target.turn_id, target.epoch, "message", {"role": "tool", "tool_call_id": tool_call_id, "content": encoded}, "tool")
        self._event(target.session_id, target.turn_id, target.epoch, "tool.completed", {"tool_call_id": tool_call_id, "status": result["status"]})
        return True

    def complete_tool_call(self, target: TurnIdentity, tool_call_id: str, result: dict) -> bool:
        with self._transaction():
            if not self._current(target):
                return False
            return self._finish_tool(target, tool_call_id, result)

    def get_tool_result(self, target: TurnIdentity, tool_call_id: str) -> dict | None:
        if self._matches(target) is None:
            raise StoreError("unknown_turn")
        row = self.db.execute("SELECT result_json FROM tool_calls WHERE turn_id=? AND tool_call_id=?", (target.turn_id, tool_call_id)).fetchone()
        return json.loads(row["result_json"]) if row and row["result_json"] else None

    def knowledge_refs_exist(self, target: TurnIdentity, references: list[str]) -> bool:
        """Source identity check only, not a proof of the model's claims.

        Index entries are committed with successful native results. Cancelled,
        failed and other sessions' turns cannot supply evidence to a new turn.
        Non-kb references retain the existing context tool contract.
        """
        selected = {ref for ref in references if ref.startswith("kb:")}
        if not selected:
            return True
        self._require_current(target)
        if any(not re.fullmatch(r"kb:[a-f0-9]{32}", ref) for ref in selected):
            return False
        for reference in selected:
            found = self.db.execute("""SELECT 1 FROM knowledge_refs r
                JOIN turns t ON t.turn_id=r.turn_id
                JOIN tool_calls c ON c.turn_id=r.turn_id AND c.tool_call_id=r.tool_call_id
                WHERE r.reference=? AND t.session_id=? AND c.state='success'
                AND (t.turn_id=? OR t.state='generation_completed') LIMIT 1""",
                (reference, target.session_id, target.turn_id)).fetchone()
            if found is None:
                return False
        return True

    def save_tool_exchange(self, target: TurnIdentity, assistant_message: dict, tool_messages: list[dict]):
        normalized = []
        for message in tool_messages:
            native = _native(message)
            try:
                result = json.loads(native["content"])
            except (TypeError, ValueError):
                raise StoreError("invalid_tool_result") from None
            normalized.append((native["tool_call_id"], result))
        expected = {call["id"] for call in assistant_message.get("tool_calls", [])}
        if len(normalized) != len(expected) or {identifier for identifier, _ in normalized} != expected:
            raise StoreError("incomplete_tool_exchange")
        with self._transaction():
            self.prepare_tool_calls(target, assistant_message)
            for identifier, result in normalized:
                self.complete_tool_call(target, identifier, result)

    def complete_turn(self, target: TurnIdentity, status: str, assistant_text: str = "") -> bool:
        if status not in TERMINAL or not isinstance(assistant_text, str) or len(assistant_text) > 64000:
            raise StoreError("invalid_turn_completion")
        with self._transaction():
            if not self._current(target):
                return False
            self._close_tools(target, "turn_ended_before_tool_result", "cancelled" if status == "cancelled" else "error")
            self.db.execute("UPDATE turns SET state=?,assistant_text=?,completed_at=? WHERE turn_id=?", (status, assistant_text, time.time(), target.turn_id))
            if assistant_text:
                self._event(target.session_id, target.turn_id, target.epoch, "message", {"role": "assistant", "content": assistant_text}, "assistant")
            self._event(target.session_id, target.turn_id, target.epoch, "turn." + status, {"status": status})
        return True

    def record_delivery(self, target: TurnIdentity, text: str, status: str = "delta") -> bool:
        if not isinstance(text, str) or len(text) > 64000 or status not in {"delta", "completed", "interrupted", "unknown"}:
            raise StoreError("invalid_delivery_record")
        with self._transaction():
            row = self._matches(target)
            if row is None:
                return False
            accumulated = row["delivery_text"] + text if status == "delta" else row["delivery_text"]
            if len(accumulated) > 64000:
                raise StoreError("delivery_text_limit")
            delivery_status = "streamed_unconfirmed" if status == "delta" else status
            self.db.execute("UPDATE turns SET delivery_text=?,delivery_status=? WHERE turn_id=?", (accumulated, delivery_status, target.turn_id))
            if accumulated and row["state"] in {"cancelled", "failed"}:
                final_message = self.db.execute("SELECT 1 FROM ledger WHERE turn_id=? AND role='assistant' AND json_extract(payload_json,'$.tool_calls') IS NULL LIMIT 1", (target.turn_id,)).fetchone()
                if not final_message:
                    self._event(target.session_id, target.turn_id, target.epoch, "message", {"role": "assistant", "content": accumulated}, "assistant")
            self._event(target.session_id, target.turn_id, target.epoch, "delivery." + status, {"characters": len(text), "status": delivery_status})
        return True

    def get_notes(self, session_id: str) -> dict:
        row = self.db.execute("SELECT notes_version,notes_text FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if not row:
            raise StoreError("unknown_session")
        return {"version": row["notes_version"], "text": row["notes_text"]}

    def update_notes(self, target: TurnIdentity, expected_version: int, text: str, *, tool_call_id: str | None = None) -> dict:
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0 or not isinstance(text, str) or len(text) > 4000:
            raise StoreError("invalid_notes_update")
        with self._transaction():
            self._require_current(target)
            if tool_call_id is not None:
                action = self.db.execute("SELECT name,state,arguments,result_json FROM tool_calls WHERE turn_id=? AND tool_call_id=?", (target.turn_id, tool_call_id)).fetchone()
                if not action or action["name"] != "context_update_notes":
                    raise StoreError("notes_tool_not_prepared")
                try:
                    prepared = json.loads(action["arguments"])
                except ValueError:
                    raise StoreError("invalid_prepared_notes_arguments") from None
                if prepared != {"expected_version": expected_version, "text": text}:
                    raise StoreError("notes_tool_arguments_conflict")
                if action["state"] == "success":
                    return json.loads(action["result_json"])["data"]
                if action["state"] not in {"prepared", "dispatched"}:
                    raise StoreError("notes_tool_not_prepared")
            current = self.get_notes(target.session_id)
            if current["version"] != expected_version:
                raise ContextConflict()
            result = {"version": expected_version + 1, "text": text}
            self.db.execute("UPDATE sessions SET notes_version=?,notes_text=?,context_version=context_version+1 WHERE session_id=?", (result["version"], text, target.session_id))
            self._event(target.session_id, target.turn_id, target.epoch, "context.notes_updated", {"version": result["version"]})
            if tool_call_id is not None:
                self._finish_tool(target, tool_call_id, {"status": "success", "data": result, "error": None, "truncated": False})
        return result

    def get_context(self, session_id: str) -> dict:
        """Private runtime view. Public snapshots deliberately omit its content."""
        session = self.db.execute("SELECT context_version FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if session is None:
            raise StoreError("unknown_session")
        row = self.db.execute("SELECT * FROM context_snapshots WHERE session_id=? ORDER BY context_version DESC LIMIT 1", (session_id,)).fetchone()
        if row is None:
            result = {"context_version": session["context_version"], "snapshot_version": 0,
                    "compacted_through_seq": 0, "summary": {key: [] for key in sorted(_SUMMARY_SECTIONS)},
                    "selected_refs": [], "input_ref": {}, "snapshot_id": None}
        else:
            result = {"context_version": session["context_version"], "snapshot_version": row["context_version"],
                "compacted_through_seq": row["compacted_through_seq"], "summary": json.loads(row["summary_json"]),
                "selected_refs": json.loads(row["selected_refs_json"]), "input_ref": json.loads(row["input_ref_json"]),
                "snapshot_id": row["snapshot_id"]}
        edit = self.db.execute("SELECT * FROM context_edits WHERE session_id=? ORDER BY context_version DESC LIMIT 1", (session_id,)).fetchone()
        result.update({"revision_id": None, "view_version": result["snapshot_version"], "selection_purpose": ""})
        if edit and edit["context_version"] > result["snapshot_version"]:
            result.update({"revision_id": edit["revision_id"], "view_version": edit["context_version"],
                "compacted_through_seq": edit["compacted_through_seq"], "summary": json.loads(edit["summary_json"]),
                "selected_refs": json.loads(edit["selected_refs_json"]), "selection_purpose": edit["selection_purpose"],
                "input_ref": json.loads(edit["input_ref_json"])})
        return result

    def user_reference(self, target: TurnIdentity, user_text: str) -> str:
        row = self.db.execute("SELECT seq,payload_json FROM ledger WHERE session_id=? AND turn_id=? AND kind='message' AND role='user' ORDER BY seq LIMIT 1",
                              (target.session_id, target.turn_id)).fetchone()
        if not row or json.loads(row["payload_json"]).get("content") != user_text:
            raise StoreError("context_user_input_changed")
        return "seq:" + str(row["seq"])

    def commit_context_edit(self, target: TurnIdentity, operation: str, arguments: dict,
                            summary: dict, selected_refs: list[str], selection_purpose: str, *,
                            input_ref: dict, metrics: dict, tool_call_id: str | None = None, deadline: float | None = None) -> dict:
        """CAS an entire edited view and native result without moving coverage."""
        if operation not in {"context_update", "context_select"}:
            raise StoreError("invalid_context_edit")
        expected = arguments.get("expected_context_version")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise StoreError("invalid_context_edit")
        if (not isinstance(summary, dict) or set(summary) != _SUMMARY_SECTIONS
            or any(not isinstance(entries, list) for entries in summary.values())):
            raise StoreError("invalid_context_summary")
        refs = self._summary_refs(summary) if any(summary.values()) else set()
        if (not isinstance(selected_refs, list) or len(selected_refs) > 64
            or any(not isinstance(ref, str) for ref in selected_refs) or len(set(selected_refs)) != len(selected_refs)
            or not isinstance(selection_purpose, str) or len(selection_purpose) > 120):
            raise StoreError("invalid_context_selection")
        for ref in selected_refs:
            self._source_ref(ref)
        if (not isinstance(input_ref, dict) or input_ref.get("expected_context_version") != expected
            or not isinstance(input_ref.get("visible_refs"), list) or len(input_ref["visible_refs"]) > 4096):
            raise StoreError("invalid_context_input_ref")
        visible = set(input_ref["visible_refs"])
        for ref in visible:
            self._source_ref(ref)
        if not (refs | set(selected_refs)) <= visible:
            raise StoreError("context_unseen_source_reference")
        summary_json, selected_json = _json(summary, maximum=32768), _json(selected_refs)
        frozen_json = _json(input_ref)
        with self._transaction():
            self._require_current(target)
            if tool_call_id is not None:
                action = self.db.execute("SELECT * FROM tool_calls WHERE turn_id=? AND tool_call_id=?", (target.turn_id, tool_call_id)).fetchone()
                if (not action or action["name"] != operation or json.loads(action["arguments"]) != arguments):
                    raise StoreError("context_edit_tool_arguments_conflict")
                if action["state"] == "success":
                    return json.loads(action["result_json"])["data"]
                if action["state"] not in {"prepared", "dispatched"}:
                    raise StoreError("context_edit_tool_not_prepared")
            session, current = self.get_session(target.session_id), self.get_context(target.session_id)
            if session["context_version"] != expected or input_ref.get("expected_notes_version") != session["notes_version"]:
                raise ContextConflict()
            if (operation == "context_select" and summary != current["summary"]
                or operation == "context_update" and (selected_refs != current["selected_refs"] or selection_purpose != current["selection_purpose"])):
                raise StoreError("context_edit_scope_violation")
            upper = input_ref.get("ledger_through_seq")
            if not isinstance(upper, int) or isinstance(upper, bool) or upper < 0:
                raise StoreError("invalid_context_input_ref")
            material = self._context_material(target.session_id, target.turn_id, snapshot_seq=upper)
            if (material["ledger_through_seq"] != upper or input_ref.get("material_fingerprint") != material["material_fingerprint"]
                or input_ref.get("owner_user_ref") != self.user_reference(target, self._matches(target)["user_text"])):
                raise StoreError("context_material_changed")
            for ref in visible:
                seq = self._source_ref(ref)
                source = self.db.execute("SELECT session_id,kind FROM ledger WHERE seq=?", (seq,)).fetchone()
                if not source or source["session_id"] != target.session_id or source["kind"] != "message" or seq > upper:
                    raise StoreError("invalid_context_source_reference")
            _context_deadline(deadline, "context_edit_timeout")
            revision = "revision_" + uuid.uuid4().hex
            result = {"context_version": expected + 1, "compacted_through_seq": current["compacted_through_seq"],
                "revision_id": revision, "operation": operation, "selected_ref_count": len(selected_refs),
                "input_tokens_estimate": metrics["input_tokens_estimate"]}
            self.db.execute("""INSERT INTO context_edits(revision_id,session_id,context_version,base_version,operation,
                compacted_through_seq,summary_json,selected_refs_json,selection_purpose,input_ref_json,created_at,owner_turn_id,owner_epoch)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (revision, target.session_id, expected + 1, expected, operation,
                    current["compacted_through_seq"], summary_json, selected_json, selection_purpose, frozen_json,
                    time.time(), target.turn_id, target.epoch))
            self.db.execute("UPDATE sessions SET context_version=? WHERE session_id=?", (expected + 1, target.session_id))
            self._event(target.session_id, target.turn_id, target.epoch, "context.updated" if operation == "context_update" else "context.selected", {
                "context_version": expected + 1, "base_version": expected, "compacted_through_seq": current["compacted_through_seq"],
                "revision_id": revision, "selected_ref_count": len(selected_refs), "input_tokens_estimate": metrics["input_tokens_estimate"]})
            if tool_call_id is not None:
                self._finish_tool(target, tool_call_id, {"status": "success", "data": result, "error": None, "truncated": False})
            _context_deadline(deadline, "context_edit_timeout")
            return result

    def _context_material(self, session_id: str, exclude_turn_id: str | None = None, *, snapshot_seq: int | None = None) -> dict:
        self.get_session(session_id)
        through = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM ledger WHERE session_id=?", (session_id,)).fetchone()[0]
        if snapshot_seq is not None:
            through = min(through, snapshot_seq)
        turns = self.db.execute("""SELECT t.*,MIN(l.seq) AS first_seq FROM turns t
            JOIN ledger l ON l.turn_id=t.turn_id AND l.session_id=t.session_id
            WHERE t.session_id=? AND l.seq<=? GROUP BY t.turn_id ORDER BY first_seq""", (session_id, through)).fetchall()
        # Load the whole frozen ledger in one ordered scan, retaining late
        # messages and every closed/open group. No per-turn query or tail trim.
        upper_by_turn = {turn["turn_id"]: turns[index+1]["first_seq"]-1 if index+1 < len(turns) else through
                         for index, turn in enumerate(turns)}
        boundaries, message_rows = {}, {}
        for row in self.db.execute("""SELECT seq,turn_id,kind,created_at,
            CASE WHEN kind='message' THEN payload_json END AS payload_json
            FROM ledger WHERE session_id=? AND seq<=? ORDER BY seq""", (session_id, through)):
            turn_id = row["turn_id"]
            if turn_id not in upper_by_turn:
                continue
            if row["seq"] <= upper_by_turn[turn_id]:
                boundaries[turn_id] = row["seq"]
            if row["kind"] == "message":
                message_rows.setdefault(turn_id, []).append(row)
        open_by_turn = {row["turn_id"]: row["n"] for row in self.db.execute("""SELECT c.turn_id,COUNT(*) AS n
            FROM tool_calls c JOIN turns t ON t.turn_id=c.turn_id
            WHERE t.session_id=? AND c.state IN ('prepared','dispatched') GROUP BY c.turn_id""", (session_id,))}
        groups = []
        for turn in turns:
            if turn["turn_id"] == exclude_turn_id:
                continue
            upper = upper_by_turn[turn["turn_id"]]
            boundary = boundaries.get(turn["turn_id"])
            rows = message_rows.get(turn["turn_id"], [])
            messages = []
            for row in rows:
                message = json.loads(row["payload_json"])
                if message["role"] == "assistant" and not message.get("tool_calls") and turn["delivery_status"] != "completed":
                    if message.get("content") != turn["delivery_text"]:
                        message["generated_content"] = message.get("content")
                    message["content"] = turn["delivery_text"]
                messages.append({"seq": row["seq"], "reference": "seq:" + str(row["seq"]), "created_at": row["created_at"],
                    "turn_status": turn["state"], "delivery_status": turn["delivery_status"],
                    "audience": "internal_tool_protocol" if message["role"] == "tool" or message.get("tool_calls") else "conversation",
                    "source_type": "user_statement" if message["role"] == "user" else "tool_evidence" if message["role"] == "tool" else "assistant_claim",
                    **message})
            open_tools = open_by_turn.get(turn["turn_id"], 0)
            late = [row["seq"] for row in rows if row["seq"] > upper]
            groups.append({"turn_id": turn["turn_id"], "round_id": turn["round_id"], "epoch": turn["epoch"],
                "state": turn["state"], "delivery_status": turn["delivery_status"],
                "delivery_text": turn["delivery_text"], "assistant_text": turn["assistant_text"],
                "first_seq": turn["first_seq"], "through_seq": boundary, "messages": messages,
                "open_tools": open_tools, "late_message_seqs": late,
                "blocked": turn["state"] not in TERMINAL or bool(open_tools or late)})
        # Plain playback/audit arrivals need not invalidate a candidate. Changes
        # to the actual visible history, its delivery state or native protocol do.
        fingerprint = hashlib.sha256(json.dumps(groups, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        return {"groups": groups, "ledger_through_seq": through,
                "business_through_seq": max((message["seq"] for group in groups for message in group["messages"]), default=0),
                "blocked_turn_ids": [group["turn_id"] for group in groups if group["blocked"]],
                "material_fingerprint": fingerprint}

    def context_material(self, session_id: str, exclude_turn_id: str | None = None) -> dict:
        """All source groups, without a recent-turn truncation or ledger deletion."""
        with self._transaction():
            return self._context_material(session_id, exclude_turn_id)

    @staticmethod
    def _source_ref(ref: Any) -> int:
        match = _SOURCE_REF.fullmatch(ref) if isinstance(ref, str) else None
        if not match or int(match[1]) >= 2**63:
            raise StoreError("invalid_context_reference")
        return int(match[1])

    @staticmethod
    def _summary_refs(summary: Any) -> set[str]:
        if not isinstance(summary, dict) or set(summary) != _SUMMARY_SECTIONS:
            raise StoreError("invalid_context_summary")
        refs = set()
        for entries in summary.values():
            if not isinstance(entries, list) or len(entries) > 64:
                raise StoreError("invalid_context_summary")
            for entry in entries:
                if (not isinstance(entry, dict) or not any(key in entry for key in ("text", "value", "topic"))
                    or not isinstance(entry.get("source_refs"), list) or not 1 <= len(entry["source_refs"]) <= 32):
                    raise StoreError("invalid_context_summary")
                for ref in entry["source_refs"]:
                    RuntimeStore._source_ref(ref)
                    refs.add(ref)
        if not refs:
            raise StoreError("context_summary_has_no_sources")
        _json(summary, maximum=32768)
        return refs

    @staticmethod
    def _prefix_fingerprint(material: dict, previous: int, boundary: int) -> str:
        groups = [g for g in material["groups"] if previous < g["through_seq"] <= boundary]
        encoded = json.dumps(groups, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _candidate_chain(self, session_id: str, candidate_id: str, recovery_key: str,
                         material: dict, context: dict) -> dict:
        """Validate receipts in order; a maximum processed seq is never coverage.

        Completed receipts may be reused by a new owner only after this check.
        They confer coverage of their contiguous raw segments, not permission
        for the next model to cite text it has never received.
        """
        session = self.get_session(session_id)
        chain, seen = [], set()
        identifier = candidate_id
        while identifier is not None:
            if identifier in seen or len(chain) >= 128:
                raise StoreError("context_candidate_chain_limit")
            seen.add(identifier)
            row = self.db.execute("SELECT * FROM context_candidates WHERE candidate_id=? AND session_id=?",
                                  (identifier, session_id)).fetchone()
            if row is None:
                raise StoreError("context_candidate_parent_missing")
            chain.append(dict(row))
            identifier = row["parent_id"]
        previous = context["compacted_through_seq"]
        boundary = previous
        for row in reversed(chain):
            if (row["base_version"] != context["context_version"] or row["notes_version"] != session["notes_version"]
                or row["previous_compacted_through_seq"] != previous or row["recovery_key"] != recovery_key):
                raise StoreError("context_candidate_base_changed")
            if row["segment_after_seq"] != boundary or row["covered_through_seq"] <= boundary:
                raise StoreError("context_candidate_coverage_gap")
            boundary = row["covered_through_seq"]
            groups = [g for g in material["groups"] if previous < g["through_seq"] <= boundary]
            if (not groups or groups[-1]["through_seq"] != boundary or any(g["blocked"] for g in groups)
                or row["prefix_fingerprint"] != self._prefix_fingerprint(material, previous, boundary)):
                raise StoreError("context_candidate_material_changed")
            frozen = json.loads(row["input_ref_json"])
            visible = set(frozen["visible_refs"])
            segment_refs = {m["reference"] for g in groups if g["through_seq"] > row["segment_after_seq"] for m in g["messages"]}
            if not segment_refs <= visible:
                raise StoreError("context_candidate_unseen_segment")
            if not (self._summary_refs(json.loads(row["summary_json"])) | set(json.loads(row["selected_refs_json"]))) <= visible:
                raise StoreError("context_candidate_unseen_source")
        latest = chain[0]
        return {"candidate_id": latest["candidate_id"], "covered_through_seq": boundary,
                "summary": json.loads(latest["summary_json"]), "selected_refs": json.loads(latest["selected_refs_json"]),
                "stage_count": len(chain), "recovery_key": recovery_key}

    def get_context_candidate(self, session_id: str, recovery_key: str, *, material: dict | None = None) -> dict | None:
        """Private staging state. Never included in get_context or model history."""
        context = self.get_context(session_id)
        if self.get_session(session_id)["state"] != "active":
            return None
        row = self.db.execute("""SELECT candidate_id FROM context_candidates
            WHERE session_id=? AND base_version=? AND recovery_key=? ORDER BY rowid DESC LIMIT 1""",
            (session_id, context["context_version"], recovery_key)).fetchone()
        if row is None:
            return None
        try:
            return self._candidate_chain(session_id, row["candidate_id"], recovery_key,
                                         material or self.context_material(session_id), context)
        except StoreError:
            # Preserve invalid receipts for audit; start again from the active
            # snapshot rather than guessing which later seq might be covered.
            return None

    def stage_context(self, target: TurnIdentity, expected_context_version: int,
                      covered_through_seq: int, summary: dict, keep_refs: list[str], *,
                      input_ref: dict, tool_call_id: str | None = None, allow_completed: bool = False, deadline: float | None = None) -> dict:
        return self._write_context(target, expected_context_version, covered_through_seq, summary, keep_refs,
            input_ref=input_ref, tool_call_id=tool_call_id, allow_completed=allow_completed, staged=True, deadline=deadline)

    def commit_context(self, target: TurnIdentity, expected_context_version: int,
                       covered_through_seq: int, summary: dict, keep_refs: list[str], *,
                       input_ref: dict, tool_call_id: str | None = None,
                       allow_completed: bool = False, deadline: float | None = None) -> dict:
        return self._write_context(target, expected_context_version, covered_through_seq, summary, keep_refs,
            input_ref=input_ref, tool_call_id=tool_call_id, allow_completed=allow_completed, deadline=deadline)

    def _write_context(self, target: TurnIdentity, expected_context_version: int,
                       covered_through_seq: int, summary: dict, keep_refs: list[str], *,
                       input_ref: dict, tool_call_id: str | None, allow_completed: bool,
                       staged: bool = False, deadline: float | None = None) -> dict:
        """Commit a model proposal and its native success result in one transaction.

        Frozen input metadata is created by the Harness, never accepted from
        model tool arguments. ``allow_completed`` is only for idle maintenance;
        its completed owner remains fenced by the exact live session epoch.
        """
        if (not isinstance(expected_context_version, int) or isinstance(expected_context_version, bool) or expected_context_version < 0
            or not isinstance(covered_through_seq, int) or isinstance(covered_through_seq, bool) or covered_through_seq <= 0
            or not isinstance(allow_completed, bool)):
            raise StoreError("invalid_context_commit")
        summary_refs = self._summary_refs(summary)
        if not isinstance(keep_refs, list) or len(keep_refs) > 64:
            raise StoreError("invalid_context_keep_refs")
        for ref in keep_refs:
            self._source_ref(ref)
        if len(set(keep_refs)) != len(keep_refs):
            raise StoreError("invalid_context_keep_refs")
        if not isinstance(input_ref, dict) or not isinstance(input_ref.get("visible_refs"), list) or len(input_ref["visible_refs"]) > 4096:
            raise StoreError("invalid_context_input_ref")
        visible_refs = set()
        for ref in input_ref["visible_refs"]:
            self._source_ref(ref)
            visible_refs.add(ref)
        for name in ("ledger_through_seq", "eligible_through_seq", "previous_compacted_through_seq", "expected_notes_version"):
            if not isinstance(input_ref.get(name), int) or isinstance(input_ref[name], bool) or input_ref[name] < 0:
                raise StoreError("invalid_context_input_ref")
        if input_ref["eligible_through_seq"] != covered_through_seq or not (summary_refs | set(keep_refs)) <= visible_refs:
            raise StoreError("context_unseen_source_reference")
        summary_json, refs_json, input_json = _json(summary, maximum=32768), _json(keep_refs), _json(input_ref, maximum=131072)
        with self._transaction():
            owner = self._matches(target)
            session = self.get_session(target.session_id)
            if (owner is None or session["state"] != "active" or session["epoch"] != target.epoch
                or (owner["state"] not in ACTIVE and not (allow_completed and owner["state"] in TERMINAL))):
                raise StaleTurn()
            if allow_completed and self.db.execute("SELECT 1 FROM tool_calls WHERE turn_id=? AND state IN ('prepared','dispatched')", (target.turn_id,)).fetchone():
                raise StoreError("context_owner_has_open_tools")
            if tool_call_id is not None:
                action = self.db.execute("SELECT name,state,arguments,result_json FROM tool_calls WHERE turn_id=? AND tool_call_id=?", (target.turn_id, tool_call_id)).fetchone()
                if not action or action["name"] != "context_compact":
                    raise StoreError("context_tool_not_prepared")
                try:
                    prepared = json.loads(action["arguments"])
                except ValueError:
                    raise StoreError("invalid_prepared_context_arguments") from None
                expected = {"expected_context_version": expected_context_version, "covered_through_seq": covered_through_seq,
                            "summary": summary, "keep_refs": keep_refs}
                if {key: prepared.get(key) for key in expected} != expected:
                    raise StoreError("context_tool_arguments_conflict")
                if action["state"] == "success":
                    return json.loads(action["result_json"])["data"]
                if action["state"] not in {"prepared", "dispatched"}:
                    raise StoreError("context_tool_not_prepared")
            context = self.get_context(target.session_id)
            if context["context_version"] != expected_context_version or input_ref["expected_notes_version"] != session["notes_version"]:
                raise ContextConflict()
            previous = context["compacted_through_seq"]
            if input_ref["previous_compacted_through_seq"] != previous or covered_through_seq <= previous:
                raise StoreError("context_coverage_not_advanced")
            upper = input_ref["ledger_through_seq"]
            actual_upper = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM ledger WHERE session_id=?", (target.session_id,)).fetchone()[0]
            if covered_through_seq > upper or upper > actual_upper:
                raise StoreError("context_coverage_outside_snapshot")
            owner_in_material = input_ref.get("owner_in_material", False)
            if not isinstance(owner_in_material, bool) or owner_in_material and (not allow_completed or owner["state"] not in TERMINAL):
                raise StoreError("invalid_context_owner_material")
            excluded = None if owner_in_material else target.turn_id
            material = self._context_material(target.session_id, excluded, snapshot_seq=upper)
            if input_ref.get("material_fingerprint") is not None and input_ref["material_fingerprint"] != material["material_fingerprint"]:
                raise StoreError("context_material_changed")
            segment_after, stage_count = previous, 0
            parent_id = input_ref.get("stage_parent_id")
            recovery_key = input_ref.get("recovery_key")
            if parent_id is not None:
                if not isinstance(parent_id, str) or not isinstance(recovery_key, str):
                    raise StoreError("invalid_context_candidate_parent")
                parent = self._candidate_chain(target.session_id, parent_id, recovery_key, material, context)
                segment_after, stage_count = parent["covered_through_seq"], parent["stage_count"]
            if staged or parent_id is not None:
                if (not isinstance(recovery_key, str) or not re.fullmatch(r"[a-f0-9]{64}", recovery_key)
                    or input_ref.get("segment_after_seq") != segment_after or covered_through_seq <= segment_after
                    or stage_count >= 128):
                    raise StoreError("context_candidate_coverage_gap")
            groups = material["groups"]
            if not any(group["through_seq"] == covered_through_seq for group in groups):
                raise StoreError("context_coverage_not_turn_boundary")
            covered = [group for group in groups if group["first_seq"] <= covered_through_seq and group["through_seq"] > previous]
            if not covered or any(group["blocked"] for group in covered):
                raise StoreError("context_coverage_not_closed_prefix")
            covered_refs = {message["reference"] for group in covered for message in group["messages"]
                            if segment_after < message["seq"] <= covered_through_seq}
            if not covered_refs <= visible_refs:
                raise StoreError("context_coverage_contains_unseen_material")
            if any(group["first_seq"] <= covered_through_seq and any(seq > previous for seq in group["late_message_seqs"]) for group in groups):
                raise StoreError("context_coverage_has_late_message")
            protected = input_ref.get("protected_refs", [])
            if not isinstance(protected, list):
                raise StoreError("invalid_context_protected_refs")
            for ref in protected:
                seq = self._source_ref(ref)
                if seq <= covered_through_seq:
                    raise StoreError("context_coverage_contains_protected_source")
                source = self.db.execute("SELECT session_id,kind FROM ledger WHERE seq=?", (seq,)).fetchone()
                if source is None or source["session_id"] != target.session_id or source["kind"] != "message" or seq > upper:
                    raise StoreError("invalid_context_source_reference")
            # Validate visible source provenance as well as the subset used by
            # the summary, so a forged future/cross-session whitelist is refused.
            for ref in visible_refs:
                seq = self._source_ref(ref)
                source = self.db.execute("SELECT session_id,kind FROM ledger WHERE seq=?", (seq,)).fetchone()
                if source is None or source["session_id"] != target.session_id or source["kind"] != "message" or seq > upper:
                    raise StoreError("invalid_context_source_reference")
            _context_deadline(deadline)
            if staged:
                candidate_id = "candidate_" + uuid.uuid4().hex
                result = {"staged": True, "candidate_id": candidate_id, "stage_count": stage_count + 1,
                          "covered_through_seq": covered_through_seq, "context_version": expected_context_version,
                          "compacted_through_seq": previous}
                self.db.execute("""INSERT INTO context_candidates(candidate_id,session_id,parent_id,base_version,
                    notes_version,previous_compacted_through_seq,segment_after_seq,covered_through_seq,
                    summary_json,selected_refs_json,input_ref_json,prefix_fingerprint,recovery_key,created_at,
                    owner_turn_id,owner_epoch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (candidate_id, target.session_id, parent_id, expected_context_version, session["notes_version"], previous,
                     segment_after, covered_through_seq, summary_json, refs_json, input_json,
                     self._prefix_fingerprint(material, previous, covered_through_seq), recovery_key, time.time(), target.turn_id, target.epoch))
                self._event(target.session_id, target.turn_id, target.epoch, "context.staged", {
                    "stage_count": stage_count + 1, "segment_after_seq": segment_after,
                    "covered_through_seq": covered_through_seq, "active_compacted_through_seq": previous,
                    "summary_bytes": len(summary_json.encode("utf-8"))})
                if tool_call_id is not None:
                    self._finish_tool(target, tool_call_id, {"status": "success", "data": result, "error": None, "truncated": False})
                _context_deadline(deadline)
                return result
            snapshot_id = "context_" + uuid.uuid4().hex
            result = {"context_version": expected_context_version + 1, "compacted_through_seq": covered_through_seq,
                      "snapshot_id": snapshot_id, "summary_present": True, "kept_refs": keep_refs}
            self.db.execute("""INSERT INTO context_snapshots(snapshot_id,session_id,context_version,base_version,
                compacted_through_seq,summary_json,selected_refs_json,input_ref_json,created_at,owner_turn_id,owner_epoch)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (snapshot_id, target.session_id, result["context_version"], expected_context_version,
                covered_through_seq, summary_json, refs_json, input_json, time.time(), target.turn_id, target.epoch))
            self.db.execute("UPDATE sessions SET context_version=? WHERE session_id=?", (result["context_version"], target.session_id))
            self._event(target.session_id, target.turn_id, target.epoch, "context.compacted", {
                "context_version": result["context_version"], "base_version": expected_context_version,
                "compacted_through_seq": covered_through_seq, "snapshot_id": snapshot_id,
                "kept_ref_count": len(keep_refs), "summary_bytes": len(summary_json.encode("utf-8")),
            })
            if tool_call_id is not None:
                self._finish_tool(target, tool_call_id, {"status": "success", "data": result, "error": None, "truncated": False})
            _context_deadline(deadline)
        return result

    def _history(self, session_id: str, limit: int, before_seq: int | None, exclude_turn_id: str | None, query: str | None = None) -> dict:
        self.get_session(session_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise StoreError("invalid_history_limit")
        if before_seq is not None and (not isinstance(before_seq, int) or isinstance(before_seq, bool) or before_seq <= 0):
            raise StoreError("invalid_history_cursor")
        params: list[Any] = [session_id]
        clauses = ["session_id=?", "role='user'", "kind='message'"]
        if before_seq is not None:
            clauses.append("seq<?")
            params.append(before_seq)
        if exclude_turn_id is not None:
            clauses.append("turn_id<>?")
            params.append(exclude_turn_id)
        if query is not None:
            # instr is literal substring matching; SQL wildcard characters have no special meaning.
            clauses.append("turn_id IN (SELECT turn_id FROM ledger WHERE session_id=? AND kind='message' AND instr(lower(COALESCE(json_extract(payload_json,'$.content'),'')),lower(?))>0)")
            params.extend([session_id, query])
        candidates = self.db.execute("SELECT seq,turn_id FROM ledger WHERE " + " AND ".join(clauses) + " ORDER BY seq DESC LIMIT ?", (*params, limit + 1)).fetchall()
        truncated = len(candidates) > limit
        selected = list(reversed(candidates[:limit]))
        messages = []
        for selected_turn in selected:
            rows = self.db.execute("""SELECT l.*,t.state AS turn_state,t.delivery_text,t.delivery_status,t.assistant_text
                FROM ledger l JOIN turns t ON l.turn_id=t.turn_id
                WHERE l.session_id=? AND l.turn_id=? AND l.kind='message' ORDER BY l.seq""", (session_id, selected_turn["turn_id"])).fetchall()
            for row in rows:
                message = json.loads(row["payload_json"])
                if message["role"] == "assistant" and not message.get("tool_calls") and row["delivery_status"] != "completed":
                    if not row["delivery_text"]:
                        continue
                    message["content"] = row["delivery_text"]
                messages.append({"seq": row["seq"], "turn_id": row["turn_id"], "turn_status": row["turn_state"], "delivery_status": row["delivery_status"],
                                 "audience": "internal_tool_protocol" if message["role"] == "tool" or message.get("tool_calls") else "conversation", **message})
        snapshot_seq = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM ledger WHERE session_id=?", (session_id,)).fetchone()[0]
        return {"messages": messages, "truncated": truncated,
                "next_before_seq": selected[0]["seq"] if truncated and selected else None, "snapshot_seq": snapshot_seq}

    def get_history(self, session_id: str, limit: int = 20, before_seq: int | None = None, exclude_turn_id: str | None = None) -> dict:
        return self._history(session_id, limit, before_seq, exclude_turn_id)

    def search_history(self, session_id: str, query: str, limit: int = 5, before_seq: int | None = None, exclude_turn_id: str | None = None) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise StoreError("invalid_history_query")
        return self._history(session_id, limit, before_seq, exclude_turn_id, query)

    def read_message(self, session_id: str, record_seq: int, offset: int = 0, max_chars: int = 2000) -> dict:
        self.get_session(session_id)
        if not isinstance(record_seq, int) or isinstance(record_seq, bool) or record_seq <= 0:
            raise StoreError("invalid_record_reference")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 1 <= max_chars <= 3000:
            raise StoreError("invalid_record_slice")
        row = self.db.execute("""SELECT l.*,t.state AS turn_state,t.delivery_text,t.delivery_status
            FROM ledger l JOIN turns t ON l.turn_id=t.turn_id
            WHERE l.session_id=? AND l.seq=? AND l.kind='message'""", (session_id, record_seq)).fetchone()
        if not row:
            raise StoreError("record_not_found")
        message = json.loads(row["payload_json"])
        text = message.get("content") if isinstance(message.get("content"), str) else _json(message)
        if message["role"] == "assistant" and not message.get("tool_calls") and row["delivery_status"] != "completed":
            text = row["delivery_text"]
        end = min(len(text), offset + max_chars)
        return {"seq": record_seq, "reference": "seq:" + str(record_seq), "turn_id": row["turn_id"],
                "role": message["role"], "turn_status": row["turn_state"], "delivery_status": row["delivery_status"],
                "content": text[offset:end], "offset": offset, "total_chars": len(text),
                "truncated": end < len(text), "next_offset": end if end < len(text) else None}

    def get_turn_messages(self, target: TurnIdentity) -> list[dict]:
        if self._matches(target) is None:
            raise StoreError("unknown_turn")
        return [json.loads(row["payload_json"]) for row in self.db.execute("SELECT payload_json FROM ledger WHERE turn_id=? AND kind='message' ORDER BY seq", (target.turn_id,))]

    def snapshot(self, session_id: str) -> dict:
        session = self.get_session(session_id)
        context = self.get_context(session_id)
        turns = [dict(row) for row in self.db.execute("SELECT turn_id,round_id,epoch,connection_id,state,delivery_status,created_at,completed_at FROM turns WHERE session_id=? ORDER BY epoch DESC LIMIT 20", (session_id,))]
        return {**session, "compacted_through_seq": context["compacted_through_seq"],
                "context_snapshot_version": context["snapshot_version"], "context_summary_present": any(bool(v) for v in context["summary"].values()),
                "context_edit_count": self.db.execute("SELECT COUNT(*) FROM context_edits WHERE session_id=?", (session_id,)).fetchone()[0],
                "context_compaction_count": self.db.execute("SELECT COUNT(*) FROM context_snapshots WHERE session_id=?", (session_id,)).fetchone()[0],
                "context_candidate_count": self.db.execute("SELECT COUNT(*) FROM context_candidates WHERE session_id=?", (session_id,)).fetchone()[0],
                "turns": turns, "ledger_seq": self.db.execute("SELECT COALESCE(MAX(seq),0) FROM ledger WHERE session_id=?", (session_id,)).fetchone()[0],
                "message_count": self.db.execute("SELECT COUNT(*) FROM ledger WHERE session_id=? AND kind='message'", (session_id,)).fetchone()[0]}

    def close_session(self, session_id: str):
        with self._transaction():
            session = self.get_session(session_id)
            if session["state"] == "closed":
                return
            for row in self.db.execute("SELECT * FROM turns WHERE session_id=? AND state IN ('running','waiting_tool','responding')", (session_id,)).fetchall():
                self._cancel(row, "session_closed")
            self.db.execute("UPDATE sessions SET state='closed',epoch=epoch+1 WHERE session_id=?", (session_id,))
            self._event(session_id, None, session["epoch"] + 1, "session.closed", {})

    def close(self):
        self.db.close()
