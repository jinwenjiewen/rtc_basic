"""Probe acceptance logic only; no network, real model, or private source data."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from backend_harness_loop.probes import context
from backend_harness_loop.probes.text import ProbeFailure, TurnResult


def evidence(turns=0, *, snapshots=0, notes=0, history=0):
    return {
        "session_closed": False, "notes_version": notes, "context_version": snapshots,
        "profile_matches": True, "snapshot_count": snapshots, "compaction_event_count": snapshots,
        "snapshot_event_count_matches": True, "context_versions": list(range(1, snapshots + 1)),
        "compaction_metrics_show_bounded_reduction": snapshots > 0,
        "coverage_watermarks": [20 * i for i in range(1, snapshots + 1)],
        "coverage_strictly_advances": True, "snapshot_versions_strictly_advance": True,
        "summary_literal_checks": {key: True for key in (
            "corrected_budget_text_present", "weekend_term_present", "negation_term_present", "pending_actions_present")},
        "original_first_user_record_retained": True, "user_message_count": turns,
        "source_ref_count": snapshots, "invalid_source_ref_count": 0,
        "model_call_count": turns * 2 + snapshots, "model_profile_matches": True,
        "model_phase_counts": {"decision": turns, "final": turns, "compact_only": snapshots},
        "history_tool_success_count": history, "runtime_error_counts": {}, "_first_user_seq": 10,
    }


def reply(number, text="收到"):
    return TurnResult(f"round_{number}", f"turn_{number:032x}", text=text,
                      state="generation_completed", delivery_status="completed",
                      delta_count=1, terminal_seen=True, turn_seen=True)


class ContextProbeValuesTests(unittest.TestCase):
    def test_semantics_require_actual_latest_number_and_json_booleans(self):
        good = '{"budget":3000,"weekend_allowed":false,"order_submitted":false}'
        self.assertTrue(all(context._semantic_checks(good).values()))
        self.assertTrue(all(context._semantic_checks("```json\n" + good + "\n```").values()))
        bad = '{"budget":5000,"weekend_allowed":"false","order_submitted":true}'
        result = context._semantic_checks(bad)
        self.assertFalse(result["corrected_budget_retained"])
        self.assertFalse(result["weekend_negation_retained"])
        self.assertFalse(result["unconfirmed_action_not_promoted_to_done"])
        self.assertFalse(context._semantic_checks('{"budget":true}')["corrected_budget_retained"])

    def test_original_history_answer_requires_the_exact_original_reference(self):
        self.assertTrue(all(context._history_checks('{"initial_budget":5000,"source_ref":"seq:10"}', 10).values()))
        result = context._history_checks('{"initial_budget":5000,"source_ref":"seq:999"}', 10)
        self.assertTrue(result["original_budget_recovered"])
        self.assertFalse(result["original_user_reference_matches"])
        self.assertFalse(context._history_checks("预算可能是5000", 10)["valid_json_object"])

    def test_filler_is_bounded_utf8_and_does_not_reveal_answers_to_later_questions(self):
        for size in (400, 1200, 6000):
            value = context._filler(3, size)
            self.assertLessEqual(len(value.encode("utf-8")), size)
            self.assertGreaterEqual(len(value.encode("utf-8")), size - 2)
            self.assertNotIn("5000", value)
            self.assertNotIn("3000", value)


class ContextProbeDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "probe.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.executescript("""
            CREATE TABLE sessions(session_id TEXT,state TEXT,notes_version INTEGER,context_version INTEGER,profile_id TEXT);
            CREATE TABLE context_snapshots(session_id TEXT,context_version INTEGER,compacted_through_seq INTEGER,summary_json TEXT,input_ref_json TEXT DEFAULT '{}');
            CREATE TABLE ledger(seq INTEGER PRIMARY KEY,session_id TEXT,turn_id TEXT,kind TEXT,role TEXT,payload_json TEXT);
            CREATE TABLE turns(turn_id TEXT,session_id TEXT);
            CREATE TABLE tool_calls(turn_id TEXT,name TEXT,state TEXT,tool_call_id TEXT,result_json TEXT);
        """)
        self.db.execute("INSERT INTO sessions VALUES('own-session','active',0,2,'flash')")
        self.db.execute("INSERT INTO sessions VALUES('foreign-session','active',9,9,'pro')")
        summary = {"goals": [], "items": [{"value": "PRIVATE_SUMMARY：预算3000，不要周末", "source_refs": ["seq:10"]}],
                   "open_questions": [], "pending_actions": [{"text": "未确认，未提交订单", "source_refs": ["seq:10"]}], "topic_index": []}
        measured = {"estimate": {"before_tokens": 15000, "after_tokens": 8000, "reduction_tokens": 7000,
            "budget_tokens": 16384, "pressure_before": .95, "pressure": .60, "target_met": False, "maintenance_tokens": 10000}}
        for version, through in ((1, 10), (2, 20)):
            self.db.execute("INSERT INTO context_snapshots VALUES('own-session',?,?,?,?)", (version, through, json.dumps(summary, ensure_ascii=False), json.dumps(measured)))
        self.db.execute("INSERT INTO context_snapshots VALUES('foreign-session',99,999,'{}','{}')")
        rows = [
            (10, "own-session", "first-turn", "message", "user", {"content": "PRIVATE_USER：最初预算5000"}),
            (11, "own-session", "first-turn", "context.compacted", None, {}),
            (12, "foreign-session", "foreign-turn", "message", "user", {"content": "PRIVATE_FOREIGN"}),
            (15, "own-session", "second-turn", "message", "user", {"content": "预算改3000"}),
            (21, "own-session", "second-turn", "context.compacted", None, {}),
            (22, "own-session", "first-turn", "model.started", None, {"profile_id": "flash", "phase": "compact_only"}),
            (23, "own-session", "second-turn", "model.started", None, {"profile_id": "flash", "phase": "final"}),
        ]
        for seq, session, turn, kind, role, payload in rows:
            self.db.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?)", (seq, session, turn, kind, role, json.dumps(payload, ensure_ascii=False)))
        self.db.execute("INSERT INTO turns VALUES('first-turn','own-session')")
        self.db.execute("INSERT INTO turns VALUES('foreign-turn','foreign-session')")
        self.db.execute("INSERT INTO tool_calls VALUES('first-turn','context_read_history','success','earlier-call','{}')")
        self.db.execute("INSERT INTO tool_calls VALUES('foreign-turn','context_read_history','success','foreign-call','{}')")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def test_sql_observations_are_session_scoped_content_free_and_read_only(self):
        before = self.path.read_bytes()
        result = context._read_evidence(self.path, "own-session", "flash", "first-turn")
        self.assertEqual(result["snapshot_count"], 2)
        self.assertEqual(result["coverage_watermarks"], [10, 20])
        self.assertEqual(result["history_tool_success_count"], 1)
        self.assertEqual(result["source_ref_count"], 4)
        self.assertEqual(result["invalid_source_ref_count"], 0)
        self.assertTrue(result["original_first_user_record_retained"])
        self.assertTrue(all(result["summary_literal_checks"].values()))
        self.assertTrue(result["compaction_metrics_show_bounded_reduction"])
        self.assertFalse(result["compaction_metrics"][0]["target_met"])
        public = context._public_evidence(result)
        self.assertNotIn("_first_user_seq", public)
        rendered = json.dumps(public)
        for original in ("PRIVATE_SUMMARY", "PRIVATE_USER", "PRIVATE_FOREIGN", "own-session", "foreign-session", "first-turn"):
            self.assertNotIn(original, rendered)
        self.assertEqual(before, self.path.read_bytes())

    def test_source_refs_cannot_borrow_another_session_and_missing_refs_are_counted(self):
        bad = {"items": [{"source_refs": ["seq:12", "seq:999"]}]}
        self.db.execute("UPDATE context_snapshots SET summary_json=? WHERE session_id='own-session' AND context_version=2", (json.dumps(bad),))
        self.db.commit()
        result = context._read_evidence(self.path, "own-session", "flash", "first-turn")
        self.assertEqual(result["invalid_source_ref_count"], 2)

    def test_missing_original_or_wrong_profile_cannot_be_presented_as_preserved(self):
        result = context._read_evidence(self.path, "own-session", "wrong-profile", "wrong-turn")
        self.assertFalse(result["profile_matches"])
        self.assertFalse(result["model_profile_matches"])
        self.assertFalse(result["original_first_user_record_retained"])

    def test_missing_or_nonshrinking_metrics_do_not_count_as_valid_compaction_gain(self):
        self.db.execute("UPDATE context_snapshots SET input_ref_json='{}' WHERE session_id='own-session'")
        self.db.commit()
        result = context._read_evidence(self.path, "own-session", "flash", "first-turn")
        self.assertFalse(result["compaction_metrics_show_bounded_reduction"])
        self.assertEqual(result["compaction_metrics"], [{"metrics_available": False}, {"metrics_available": False}])

    def test_history_result_checks_grouped_messages_fragment_lists_and_direct_records(self):
        self.db.execute("INSERT INTO turns VALUES('history-turn','own-session')")
        original = {"seq": 10, "role": "user", "content": "PRIVATE_ORIGINAL：预算5000"}
        for index, data in enumerate(({"messages": [original]}, {"records": [original]}, original)):
            self.db.execute("INSERT INTO tool_calls VALUES('history-turn','context_read_history','success',?,?)",
                (f"history-{index}", json.dumps({"status": "success", "data": data}, ensure_ascii=False)))
        self.db.commit()
        result = context._history_target_evidence(self.path, "own-session", "history-turn", 10)
        self.assertTrue(result["returned_original_user_record_with_budget"])
        self.assertEqual(result["target_user_record_return_count"], 3)
        self.assertEqual(result["successful_history_tool_calls_returning_target"], 3)
        self.assertNotIn("PRIVATE_ORIGINAL", json.dumps(result))

    def test_broad_successful_search_or_a_previous_turn_target_does_not_qualify(self):
        self.db.execute("INSERT INTO turns VALUES('history-turn','own-session')")
        filler = {"seq": 15, "role": "user", "content": "PRIVATE_FILLER：预算改为3000"}
        original = {"seq": 10, "role": "user", "content": "PRIVATE_ORIGINAL：预算5000"}
        self.db.execute("INSERT INTO tool_calls VALUES('history-turn','context_search_history','success','broad',?)",
            (json.dumps({"status": "success", "data": {"messages": [filler]}}),))
        self.db.execute("UPDATE tool_calls SET result_json=? WHERE tool_call_id='earlier-call'",
            (json.dumps({"status": "success", "data": {"messages": [original]}}),))
        self.db.commit()
        result = context._history_target_evidence(self.path, "own-session", "history-turn", 10)
        self.assertEqual(result["successful_history_tool_calls_this_turn"], 1)
        self.assertFalse(result["returned_original_user_record_with_budget"])
        self.assertEqual(result["successful_history_tool_calls_returning_target"], 0)


class ContextProbeWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, *, notes=0, compact=True, bad_answer=False, failure=None, literal=True, returned_target=True):
        turns = [reply(i) for i in range(1, 17)]
        if compact:
            turns[4].text = ('{"budget":5000,"weekend_allowed":true,"order_submitted":true}' if bad_answer else
                             '{"budget":3000,"weekend_allowed":false,"order_submitted":false}')
            turns[5].text = '{"initial_budget":5000,"source_ref":"seq:10"}'
        sent = AsyncMock(side_effect=failure if failure else turns)
        clean = AsyncMock(return_value={"passed": True, "stop_confirmed": True, "database_closed": True})

        def observed(*_):
            n = sent.await_count
            snaps = min(2, max(0, n - 2)) if compact else 0
            result = evidence(n, snapshots=snaps, notes=notes, history=int(n >= 6))
            if not literal:
                result["summary_literal_checks"] = {key: False for key in result["summary_literal_checks"]}
            return result

        with (
            patch.object(context, "_create", AsyncMock(return_value=("text_session_" + "a" * 32,
                {"profile_id": "flash", "mode": "agent_loop", "transport": "text"}))),
            patch.object(context, "_heartbeat", AsyncMock()), patch.object(context, "_turn", sent),
            patch.object(context, "_read_evidence", side_effect=observed), patch.object(context, "_cleanup", clean),
            patch.object(context, "_history_target_evidence", return_value={
                "history_turn_bound_to_session": True, "history_tool_calls_this_turn": 1,
                "successful_history_tool_calls_this_turn": 1, "target_user_record_return_count": int(returned_target),
                "successful_history_tool_calls_returning_target": int(returned_target),
                "returned_original_user_record_with_budget": returned_target}),
        ):
            result = await context._profile_case(None, Path("unread.sqlite3"), "flash", 0,
                                                  filler_bytes=1200, max_turns=8)
        return result, sent, clean

    async def test_two_snapshots_semantics_and_original_tool_read_are_all_required(self):
        result, sent, clean = await self.run_case()
        self.assertTrue(result["passed"])
        self.assertEqual(sent.await_count, 6)
        self.assertEqual(result["history_tool_success_count_added"], 1)
        self.assertTrue(all(result["semantic_checks"].values()))
        self.assertTrue(all(result["history_checks"].values()))
        clean.assert_awaited_once()
        self.assertNotIn("_first_user_seq", json.dumps(result))
        self.assertNotIn("text_session_" + "a" * 32, json.dumps(result))

    async def test_working_notes_cannot_hide_a_failed_compaction_test(self):
        result, sent, clean = await self.run_case(notes=1)
        self.assertFalse(result["passed"])
        self.assertEqual(result["failure"]["code"], "context_probe_notes_would_mask_summary")
        self.assertEqual(sent.await_count, 1)
        clean.assert_awaited_once()

    async def test_missing_second_compaction_stops_at_bound_without_retrying_or_leaking(self):
        result, sent, clean = await self.run_case(compact=False)
        self.assertFalse(result["passed"])
        self.assertEqual(result["failure"]["code"], "context_probe_two_compactions_not_observed")
        self.assertEqual(sent.await_count, 6)
        self.assertEqual(result["pressure_turn_count"], 4)
        clean.assert_awaited_once()

    async def test_valid_snapshots_alone_do_not_prove_semantic_fidelity(self):
        result, _, clean = await self.run_case(bad_answer=True)
        self.assertFalse(result["passed"])
        self.assertFalse(result["semantic_checks"]["corrected_budget_retained"])
        self.assertFalse(result["semantic_checks"]["weekend_negation_retained"])
        clean.assert_awaited_once()

    async def test_literal_summary_observations_do_not_override_correct_semantic_and_history_behavior(self):
        result, _, clean = await self.run_case(literal=False)
        self.assertTrue(result["passed"])
        self.assertFalse(any(result["database"]["summary_literal_checks"].values()))
        self.assertTrue(all(result["semantic_checks"].values()))
        self.assertTrue(all(result["history_checks"].values()))
        clean.assert_awaited_once()

    async def test_successful_broad_history_tool_and_correct_answer_still_require_the_actual_target_record(self):
        result, _, clean = await self.run_case(returned_target=False)
        self.assertFalse(result["passed"])
        self.assertTrue(all(result["semantic_checks"].values()))
        self.assertTrue(all(result["history_checks"].values()))
        self.assertGreater(result["history_tool_success_count_added"], 0)
        self.assertFalse(result["history_tool_evidence"]["returned_original_user_record_with_budget"])
        clean.assert_awaited_once()

    async def test_model_failure_is_kept_and_owned_session_is_still_cleaned(self):
        result, sent, clean = await self.run_case(failure=ProbeFailure("first_output_or_turn_timeout"))
        self.assertFalse(result["passed"])
        self.assertEqual(result["failure"]["code"], "first_output_or_turn_timeout")
        self.assertEqual(sent.await_count, 1)
        clean.assert_awaited_once()

    async def test_invalid_probe_destination_and_bounds_never_start_http(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exists.sqlite3"
            path.touch()
            with patch.object(context.httpx, "AsyncClient", side_effect=AssertionError("No HTTP")) as network:
                for url, profiles, size, limit in (
                    ("https://remote.example", ["flash"], 1200, 16),
                    ("http://127.0.0.1:3102", ["flash", "pro", "third"], 1200, 16),
                    ("http://127.0.0.1:3102", ["flash"], 9000, 16),
                    ("http://127.0.0.1:3102", ["flash"], 1200, 17),
                ):
                    result = await context.run_probe(url, path, profiles, filler_bytes=size, max_turns=limit)
                    self.assertFalse(result["passed"])
                    self.assertIn("failure", result)
                network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
