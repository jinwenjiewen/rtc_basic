import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend_harness_loop.live_evidence import LiveEvidenceError, summarize_m1_live


def sha(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def identifier(field, kind, value):
    return {"field": field, "kind": kind, "sha256": sha(value)}


def capture(run, round_id, *, tag=None, stage=None, data=None):
    ids = [identifier("query.rtc_run_id", "run", run)]
    if round_id is not None:
        ids.append(identifier("RoundID", "round", round_id))
    for index, (user_id, item_round) in enumerate(data or []):
        ids.extend([identifier(f"data[{index}].userId", "user", user_id),
                    identifier(f"data[{index}].roundId", "round", item_round)])
    result = {"source": "custom_llm" if tag is None else "rts_event",
              "correlation": {"identifiers": ids}}
    if tag is not None:
        result["tlv_tag"] = tag
    if stage is not None:
        result["correlation"]["stage_code"] = stage
    return result


class LiveEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.database = base / "runtime.sqlite3"
        self.capture = base / "capture.jsonl"
        self.client = base / "client.jsonl"
        self.ledger = base / "capture.sessions.json"
        self.fixture = "KNOWN PRIVATE TEST PHRASE"
        self.user_a, self.user_b = "PRIVATE-RTC-USER-A", "PRIVATE-RTC-USER-B"
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript("""
                CREATE TABLE sessions(session_id TEXT,run_id TEXT,profile_id TEXT,state TEXT,epoch INTEGER,notes_version INTEGER,notes_text TEXT,created_at REAL);
                CREATE TABLE turns(turn_id TEXT,session_id TEXT,run_id TEXT,round_id TEXT,state TEXT,delivery_status TEXT,user_text TEXT,assistant_text TEXT,delivery_text TEXT);
                CREATE TABLE ledger(session_id TEXT,turn_id TEXT,kind TEXT,payload_json TEXT);
                CREATE TABLE tool_calls(turn_id TEXT,name TEXT,state TEXT,arguments TEXT,result_json TEXT);
            """)
            db.executemany("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)", [
                ("session-a", "run-a", "pro", "closed", 2, 1, self.fixture, 1),
                ("session-b", "run-b", "flash", "active", 1, 0, "PRIVATE NOTES WITHOUT FIXTURE", 2),
                ("old-asgi-session", "old-asgi-run", "pro", "closed", 1, 0, "PRIVATE OLD NOTES", 0),
            ])
            db.executemany("INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?)", [
                ("turn-a0", "session-a", "run-a", "0", "cancelled", "interrupted", self.fixture, "PRIVATE ANSWER", "PRIVATE DELTA"),
                ("turn-a1", "session-a", "run-a", "1", "generation_completed", "completed", "PRIVATE TEXT INPUT", "PRIVATE ANSWER", "PRIVATE ANSWER"),
                ("turn-b0", "session-b", "run-b", "0", "running", "unknown", "PRIVATE VOICE INPUT", "PRIVATE ANSWER", "PRIVATE DELTA"),
                ("old-turn", "old-asgi-session", "old-asgi-run", "0", "completed", "completed", "PRIVATE OLD TEXT", "PRIVATE OLD ANSWER", "PRIVATE OLD ANSWER"),
            ])
            for session, turn in (("session-a", "turn-a0"), ("session-b", "turn-b0")):
                db.executemany("INSERT INTO ledger VALUES(?,?,?,?)", [
                    (session, turn, "model.started", json.dumps({"phase": "decision", "profile_id": "pro" if session == "session-a" else "flash", "content": "PRIVATE MODEL TEXT"})),
                    (session, turn, "model.completed", json.dumps({"usage": {"total_tokens": 10}, "content": "PRIVATE MODEL TEXT"})),
                ])
            db.executemany("INSERT INTO ledger VALUES(?,?,?,?)", [
                ("session-a", "turn-a0", "execution.cancelled", json.dumps({"reason": "PRIVATE CANCELLATION"})),
                ("session-a", "turn-a0", "runtime.error", json.dumps({"code": "model_request_failed", "detail": "PRIVATE ERROR DETAILS", "api_key": "PRIVATE API KEY"})),
                ("session-b", "turn-b0", "message", json.dumps({"role": "user", "content": "PRIVATE MESSAGE"})),
            ])
            db.execute("INSERT INTO tool_calls VALUES(?,?,?,?,?)", ("turn-a0", "history_search", "completed", "PRIVATE TOOL ARGUMENTS", "PRIVATE TOOL RESULTS"))
            db.commit()
        entries = []
        for name, user, stopped in (("a", self.user_a, True), ("b", self.user_b, False)):
            entries.append({"session_id": "session-" + name, "room_id": "PRIVATE ROOM " + name,
                            "task_id": "PRIVATE TASK " + name, "user_id": user,
                            "state": "stopped" if stopped else "running", "start_attempted": True,
                            "cleanup_rounds": 1 if stopped else 0, "provider": [
                                {"action": "StartVoiceChat", "ok": True, "http_status": 200,
                                 "credentials": "PRIVATE PROVIDER CREDENTIALS"},
                                *([{"action": "StopVoiceChat", "ok": True, "http_status": 200}] if stopped else []),
                            ]})
        self.ledger.write_text(json.dumps({"schema_version": 1, "sessions": entries}), encoding="utf-8")
        self.captures = [
            capture("run-a", "0"), capture("run-a", "0", tag="conv", stage=4),
            # The accepted round belongs to the agent. The user belongs to another
            # data element and another round; a cartesian product would be wrong.
            capture("run-a", None, tag="subv", data=[("agent-a", "0"), (self.user_a, "999")]),
            capture("run-b", "0"), capture("run-b", "0", tag="conv", stage=2),
            capture("run-b", None, tag="subv", data=[("agent-b", "8"), (self.user_b, "0")]),
            capture("wrong-run", "0", tag="conv", stage=4),
            capture("old-asgi-run", "0"),
        ]
        self.clients = [self.event("a", "text_sent"), self.event("a", "text_sent"),
                        self.event("a", "interrupt_sent"), self.event("b", "mic_started"),
                        self.event("b", "mic_level_active"), self.event("b", "speech_input_confirmed", scope="human_report"),
                        self.event("b", "heard_confirmed", scope="human_report"),
                        self.event("b", "stop_confirmed", scope="browser_sdk_observation"),
                        self.event("a", "sdk_error", code=1001),
                        self.event("a", "mic_started", run="wrong-run"),
                        self.event("b", "text_sent", session="session-a")]
        self.write_inputs()

    def event(self, name, kind, *, scope="browser_sdk_observation", run=None, session=None, **extra):
        return {"session_sha256": sha(session or "session-" + name), "run_sha256": sha(run or "run-" + name),
                "kind": kind, "scope": scope, "elapsed_ms": 100, **extra}

    def write_inputs(self):
        self.capture.write_text("\n".join(json.dumps(item) for item in self.captures) + "\n", encoding="utf-8")
        self.client.write_text("\n".join(json.dumps(item) for item in self.clients) + "\n", encoding="utf-8")

    def summarize(self, **kwargs):
        return summarize_m1_live(self.capture, self.client, self.database, self.ledger, **kwargs)

    def test_authoritative_run_and_session_isolation_with_reused_round_zero(self):
        result = self.summarize()
        self.assertEqual([item["profile"] for item in result["sessions"]], ["pro", "flash"])
        a, b = result["sessions"]
        self.assertEqual(a["run_sha256"], sha("run-a"))
        self.assertEqual(a["backend"]["accepted_round_count"], 2)
        self.assertEqual(b["backend"]["accepted_round_count"], 1)
        self.assertEqual(a["correlation"]["accepted_callback_round_count"], 1)
        self.assertEqual(b["correlation"]["accepted_callback_round_count"], 1)
        self.assertEqual(a["correlation"]["accepted_stage4_round_count"], 1)
        self.assertEqual(b["correlation"]["accepted_stage4_round_count"], 0)
        self.assertEqual(result["exclusions"]["database_sessions_not_in_client_ledger"], 1)
        self.assertEqual(result["exclusions"]["capture_records_unmatched_run"], 2)
        self.assertEqual(result["exclusions"]["client_events_unmatched_session_run"], 2)

    def test_user_asr_requires_same_subtitle_element_as_accepted_round(self):
        a, b = self.summarize()["sessions"]
        self.assertEqual(a["correlation"]["accepted_subv_round_count"], 1)
        self.assertEqual(a["correlation"]["accepted_user_asr_round_count"], 0)
        self.assertEqual(a["correlation"]["accepted_user_asr_model_round_count"], 0)
        self.assertEqual(b["correlation"]["accepted_user_asr_round_count"], 1)
        self.assertEqual(b["correlation"]["accepted_user_asr_model_round_count"], 1)
        self.captures.append(capture("run-a", None, tag="subv", data=[("agent-a", "9"), (self.user_a, "0")]))
        self.write_inputs()
        self.assertEqual(self.summarize()["sessions"][0]["correlation"]["accepted_user_asr_model_round_count"], 1)

    def test_m1_does_not_require_capture_stream_or_callback_response_status(self):
        a, b = self.summarize()["sessions"]
        self.assertEqual(a["capture"]["callback_response_status_counts"], {})
        self.assertNotIn("capture_stream", b["capture"]["source_counts"])
        self.assertEqual(b["observed_failure_codes"], {})
        self.assertEqual(b["backend"]["model_completed_round_count"], 1)
        self.assertNotIn("passed", b)

    def test_generation_state_is_distinct_from_delivery_completion(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.executemany("INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?)", [
                ("turn-b1", "session-b", "run-b", "1", "waiting_tool", "unknown", "PRIVATE TOOL INPUT", "", ""),
                ("turn-b2", "session-b", "run-b", "2", "responding", "delta", "PRIVATE STREAM INPUT", "", "PRIVATE DELTA"),
            ])
            db.commit()
        a, b = self.summarize()["sessions"]
        self.assertEqual(a["backend"]["turn_state_counts"], {"cancelled": 1, "generation_completed": 1})
        self.assertEqual(a["backend"]["delivery_status_counts"], {"completed": 1, "interrupted": 1})
        self.assertEqual(b["backend"]["turn_state_counts"], {"responding": 1, "running": 1, "waiting_tool": 1})
        self.assertEqual(b["backend"]["delivery_status_counts"], {"delta": 1, "unknown": 2})
        self.assertNotIn("other", a["backend"]["turn_state_counts"])
        self.assertNotIn("other", b["backend"]["turn_state_counts"])

    def test_text_input_is_counted_separately_and_human_scope_is_preserved(self):
        a, b = self.summarize()["sessions"]
        self.assertEqual(a["client"]["text_sent_count"], 2)
        self.assertNotIn("mic_started", a["client"]["event_counts"])
        self.assertEqual(b["client"]["text_sent_count"], 0)
        self.assertTrue(b["client"]["human_confirmations"]["speech_input_confirmed"])
        self.assertTrue(b["client"]["human_confirmations"]["heard_confirmed"])
        self.assertFalse(b["client"]["human_confirmations"]["stop_confirmed"])
        self.assertEqual(a["observed_failure_codes"], {"runtime:model_request_failed": 1, "sdk:1001": 1})

    def test_fixture_checks_only_return_booleans_and_never_leak_private_content(self):
        result = self.summarize(fixture=self.fixture)
        a, b = result["sessions"]
        self.assertEqual(a["backend"]["fixture_matches"], {"user_text": True, "notes_text": True})
        self.assertEqual(b["backend"]["fixture_matches"], {"user_text": False, "notes_text": False})
        serialized = json.dumps(result)
        for private in ("PRIVATE", self.fixture, self.user_a, self.user_b, sha(self.fixture), "session-a", "run-a", "turn-a0", "agent-a", "old-asgi-run"):
            self.assertNotIn(private, serialized)
        self.assertNotIn("fixture_matches", self.summarize()["sessions"][0]["backend"])

    def test_cleanup_distinguishes_local_state_and_provider_stop_acceptance(self):
        a, b = self.summarize()["sessions"]
        self.assertTrue(a["cleanup"]["local_stopped"])
        self.assertTrue(a["cleanup"]["stop_accepted"])
        self.assertEqual(a["cleanup"]["stop_http_status_counts"], {"200": 1})
        self.assertFalse(b["cleanup"]["local_stopped"])
        self.assertFalse(b["cleanup"]["stop_accepted"])
        self.assertNotIn("room_id", a["cleanup"])
        self.assertNotIn("user_id", a["cleanup"])
        self.assertNotIn("task_id", a["cleanup"])

    def test_reporting_is_read_only_and_does_not_recover_running_turn(self):
        before = self.database.read_bytes()
        self.summarize(fixture=self.fixture)
        self.assertEqual(before, self.database.read_bytes())
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT state FROM turns WHERE turn_id='turn-b0'").fetchone()[0], "running")

    def test_missing_optional_observations_and_malformed_lines_are_explicit(self):
        with self.capture.open("a", encoding="utf-8") as stream:
            stream.write("not json with PRIVATE INPUT\n[]\n")
        self.assertEqual(self.summarize()["exclusions"]["invalid_capture_records"], 2)
        self.capture.unlink()
        result = self.summarize()
        self.assertFalse(result["inputs"]["capture_present"])
        self.assertEqual(result["sessions"][1]["correlation"]["accepted_user_asr_round_count"], 0)

    def test_missing_database_and_bad_ledger_fail_with_safe_machine_codes(self):
        with self.assertRaisesRegex(LiveEvidenceError, "^database_metadata_read_failed$"):
            summarize_m1_live(self.capture, self.client, self.database.with_name("PRIVATE-MISSING.sqlite3"), self.ledger)
        self.ledger.write_text("PRIVATE MALFORMED LEDGER", encoding="utf-8")
        with self.assertRaisesRegex(LiveEvidenceError, "^invalid_session_ledger$"):
            self.summarize()


if __name__ == "__main__":
    unittest.main()
