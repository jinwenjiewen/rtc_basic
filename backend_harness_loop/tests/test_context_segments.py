"""Durable, inactive segment receipts and contiguous recovery over real SQLite."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend_harness_loop.context import ContextEngine, ContextError, ContextPolicy, SUMMARY_KEYS
from backend_harness_loop.store import RuntimeStore, StaleTurn, StoreError


class SegmentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "segments.sqlite3"
        self.store = RuntimeStore(self.path)
        self.store.create_session("session", "run", "flash")
        self.engine = ContextEngine(self.store, ContextPolicy(input_cap_tokens=16384))
        self.targets = []
        for index in range(44):
            turn = self.store.admit_turn("session", "seed-" + str(index), "fact:" + str(index) + ":" + "x" * 900, "c" + str(index))
            self.store.complete_turn(turn, "generation_completed", "heard")
            self.store.record_delivery(turn, "heard", "delta")
            self.store.record_delivery(turn, "", "completed")
            self.targets.append(turn)
        self.owner = self.store.admit_turn("session", "current", "continue", "current")

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def plan(self, **options):
        return self.engine.prepare(self.owner, "continue", "Fixed rules", [], force=True, staged=True, **options)

    def args(self, plan):
        source = json.loads(plan.messages[1]["content"].split("\n", 1)[1])
        summary = deepcopy(source["previous_summary"])
        summary["items"].append({"text": "fact:" + str(plan.covered_through_seq),
                                 "source_refs": ["seq:" + str(source["closed_prefix"][0]["messages"][0]["seq"])]})
        return {"expected_context_version": plan.expected_context_version, "covered_through_seq": plan.covered_through_seq,
                "summary": summary, "keep_refs": [], "target_tokens": 1000}

    def stage(self):
        plan = self.plan()
        result = self.engine.commit(self.owner, plan, self.args(plan))
        self.assertTrue(result["staged"])
        return plan, result

    def test_staging_does_not_change_active_view_pressure_or_raw_history(self):
        before = self.engine.build(self.owner, "continue", "Fixed rules", [])
        plan, result = self.stage()
        after = self.engine.build(self.owner, "continue", "Fixed rules", [])
        self.assertEqual(self.store.get_context("session")["context_version"], 0)
        self.assertEqual(self.store.get_context("session")["compacted_through_seq"], 0)
        self.assertEqual([m for m in before.messages if m["role"] != "system"],
                         [m for m in after.messages if m["role"] != "system"])
        self.assertEqual(before.metrics["input_tokens_estimate"], after.metrics["input_tokens_estimate"])
        self.assertEqual(before.metrics["pressure"], after.metrics["pressure"])
        self.assertTrue(after.metrics["hard_limit_exceeded"])
        self.assertEqual(after.metrics["recent_turn_count"], 44)
        self.assertEqual(result["compacted_through_seq"], 0)
        self.assertEqual(self.store.snapshot("session")["context_candidate_count"], 1)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)
        protected = plan.before_view.material["groups"][-4:]
        self.assertLess(plan.covered_through_seq, protected[0]["first_seq"])

    def test_contiguous_stages_commit_once_only_when_whole_view_fits(self):
        plan, staged = self.stage()
        initial_refs = [item["reference"] for group in plan.before_view.material["groups"]
                        if group["through_seq"] <= plan.covered_through_seq for item in group["messages"]]
        for _ in range(20):
            next_plan = self.plan()
            self.assertEqual(next_plan.input_ref["segment_after_seq"], staged["covered_through_seq"])
            # Omitted old raw records are NOT in the next model's source whitelist.
            self.assertNotIn(initial_refs[-1], next_plan.input_ref["visible_refs"])
            self.assertNotIn("fact:0:" + "x" * 30, json.dumps(next_plan.messages))
            result = self.engine.commit(self.owner, next_plan, self.args(next_plan))
            if not result.get("staged"):
                break
            staged = result
            self.assertEqual(self.store.get_context("session")["compacted_through_seq"], 0)
        else:
            self.fail("No bounded final commit")
        active = self.store.get_context("session")
        self.assertEqual(active["context_version"], 1)
        self.assertGreater(active["compacted_through_seq"], plan.covered_through_seq)
        self.assertIsNotNone(active["input_ref"]["stage_parent_id"])
        final = self.engine.build(self.owner, "continue", "Fixed rules", [])
        self.assertFalse(final.metrics["hard_limit_exceeded"])
        self.assertGreaterEqual(final.metrics["recent_turn_count"], 4)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 1)
        self.assertEqual(self.store.read_message("session", int(initial_refs[0][4:]))["content"], "fact:0:" + "x" * 900)

    def test_reopen_and_new_epoch_revalidate_receipt_but_reject_old_proposal(self):
        _, result = self.stage()
        late = self.plan()
        self.store.close()
        self.store = RuntimeStore(self.path)  # recovery fails the old active owner
        self.engine = ContextEngine(self.store, self.engine.policy)
        old_owner = self.owner
        self.owner = self.store.admit_turn("session", "after-restart", "continue", "new")
        resumed = self.plan()
        self.assertEqual(resumed.parent_candidate["candidate_id"], result["candidate_id"])
        self.assertEqual(resumed.input_ref["segment_after_seq"], result["covered_through_seq"])
        with self.assertRaises(StaleTurn):
            self.engine.commit(old_owner, late, self.args(late))
        self.engine.commit(self.owner, resumed, self.args(resumed))

    def test_interrupt_preserves_finished_receipt_and_fences_late_model(self):
        _, first = self.stage()
        late = self.plan()
        old = self.owner
        self.store.cancel_round("session", "current")
        self.owner = self.store.admit_turn("session", "after-interrupt", "continue", "new")
        with self.assertRaises(StaleTurn):
            self.engine.commit(old, late, self.args(late))
        self.assertEqual(self.plan().parent_candidate["candidate_id"], first["candidate_id"])
        self.assertEqual(self.store.snapshot("session")["context_candidate_count"], 1)

    def test_changed_prefix_delivery_invalidates_parent_without_erasing_audit(self):
        plan, result = self.stage()
        late = self.plan()
        self.store.record_delivery(self.targets[0], "changed", "delta")
        self.assertIsNone(self.plan().parent_candidate)
        with self.assertRaisesRegex(StoreError, "context_material_changed"):
            self.engine.commit(self.owner, late, self.args(late))
        self.assertIsNone(self.store.get_context_candidate("session", plan.input_ref["recovery_key"]))
        self.assertEqual(self.store.snapshot("session")["context_candidate_count"], 1)

    def test_notes_and_policy_changes_restart_at_active_snapshot(self):
        self.stage()
        changed = ContextEngine(self.store, replace(self.engine.policy, input_cap_tokens=17000))
        plan = changed.prepare(self.owner, "continue", "Fixed rules", [], force=True, staged=True)
        self.assertIsNone(plan.parent_candidate)
        self.store.update_notes(self.owner, 0, "new notes")
        self.assertIsNone(self.plan().parent_candidate)
        self.assertEqual(self.plan().expected_context_version, 1)

    def test_seen_in_old_segment_is_not_citation_permission_for_next_model(self):
        first, _ = self.stage()
        omitted = first.before_view.material["groups"][1]["messages"][0]["reference"]
        next_plan = self.plan()
        self.assertNotIn(omitted, next_plan.input_ref["visible_refs"])
        args = self.args(next_plan)
        args["summary"]["items"].append({"text": "unseen", "source_refs": [omitted]})
        with self.assertRaisesRegex(ContextError, "context_reference_not_visible"):
            self.engine.commit(self.owner, next_plan, args)

    def test_missing_parent_gap_and_cross_session_parent_cannot_stage(self):
        _, result = self.stage()
        plan = self.plan()
        for key, value, code in (("stage_parent_id", "missing", "context_candidate_parent_missing"),
                                 ("segment_after_seq", result["covered_through_seq"] + 1, "context_candidate_coverage_gap")):
            broken = deepcopy(plan)
            broken.input_ref[key] = value
            with self.assertRaisesRegex(StoreError, code):
                self.engine.commit(self.owner, broken, self.args(broken))
        self.store.db.execute("UPDATE context_candidates SET parent_id=NULL,segment_after_seq=1 WHERE candidate_id=?", (result["candidate_id"],))
        self.assertIsNone(self.plan().parent_candidate)
        with self.assertRaisesRegex(StoreError, "context_candidate_coverage_gap"):
            self.engine.commit(self.owner, plan, self.args(plan))

    def test_each_new_segment_must_reduce_its_own_virtual_view(self):
        self.stage()
        plan = self.plan()
        args = self.args(plan)
        # Still smaller than ALL original history, but expands the next segment.
        args["summary"]["items"][0]["text"] = "y" * (plan.input_ref["estimate"]["replaced_visible_source_bytes"] + 1000)
        with self.assertRaisesRegex(ContextError, "context_no_meaningful_segment_reduction"):
            self.engine.commit(self.owner, plan, args)

    def test_stage_tool_result_and_receipt_are_atomic_and_idempotent(self):
        plan = self.plan()
        args = self.args(plan)
        call = {"id": "stage-call", "type": "function", "function": {"name": "context_compact", "arguments": json.dumps(args)}}
        self.store.prepare_tool_calls(self.owner, {"role": "assistant", "content": None, "tool_calls": [call]})
        self.store.mark_tool_dispatched(self.owner, "stage-call")
        self.store.db.execute("""CREATE TEMP TRIGGER reject_stage BEFORE INSERT ON context_candidates
            BEGIN SELECT RAISE(ABORT,'injected'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.commit(self.owner, plan, args, tool_call_id="stage-call")
        self.assertEqual(self.store.snapshot("session")["context_candidate_count"], 0)
        self.assertIsNone(self.store.get_tool_result(self.owner, "stage-call"))
        self.store.db.execute("DROP TRIGGER reject_stage")
        first = self.engine.commit(self.owner, plan, args, tool_call_id="stage-call")
        again = self.engine.commit(self.owner, plan, args, tool_call_id="stage-call")
        self.assertEqual(first["candidate_id"], again["candidate_id"])
        self.assertEqual(self.store.snapshot("session")["context_candidate_count"], 1)
        self.assertEqual(self.store.get_tool_result(self.owner, "stage-call")["data"]["compacted_through_seq"], 0)

    def test_closed_session_rejects_stage_and_never_exposes_receipt(self):
        self.stage()
        plan = self.plan()
        self.store.close_session("session")
        with self.assertRaises(StaleTurn):
            self.engine.commit(self.owner, plan, self.args(plan))
        self.assertIsNone(self.store.get_context_candidate("session", plan.input_ref["recovery_key"]))

    def test_foreign_receipt_does_not_confer_coverage(self):
        _, result = self.stage()
        plan = self.plan()
        self.store.create_session("foreign", "foreign-run", "flash")
        self.store.db.execute("UPDATE context_candidates SET session_id='foreign' WHERE candidate_id=?", (result["candidate_id"],))
        with self.assertRaisesRegex(StoreError, "context_candidate_parent_missing"):
            self.engine.commit(self.owner, plan, self.args(plan))

    def test_oversized_first_closed_turn_and_current_native_chain_fail_without_skipping(self):
        self.store.create_session("large", "large-run", "flash")
        for index in range(6):
            target = self.store.admit_turn("large", str(index), "z" * (18000 if index == 0 else 100), "large-" + str(index))
            self.store.complete_turn(target, "generation_completed", "ok")
            self.store.record_delivery(target, "ok", "delta")
            self.store.record_delivery(target, "", "completed")
        owner = self.store.admit_turn("large", "current", "continue", "large-current")
        with self.assertRaisesRegex(ContextError, "context_maintenance_input_too_large"):
            self.engine.prepare(owner, "continue", "Fixed rules", [], force=True, staged=True)
        self.assertEqual(self.store.snapshot("large")["context_candidate_count"], 0)
        self.assertEqual(self.store.get_context("large")["compacted_through_seq"], 0)
        chain = [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "protected", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "protected", "content": "t" * 20000}]
        with self.assertRaisesRegex(ContextError, "context_fixed_input_too_large"):
            self.engine.prepare(self.owner, "continue", "Fixed rules", [], chain, force=True, staged=True)

    def test_failed_final_transaction_preserves_all_inactive_receipts(self):
        self.stage()
        self.store.db.execute("""CREATE TEMP TRIGGER reject_final BEFORE INSERT ON context_snapshots
            BEGIN SELECT RAISE(ABORT,'injected'); END""")
        for _ in range(20):
            plan = self.plan()
            args = self.args(plan)
            try:
                self.engine.commit(self.owner, plan, args)
            except sqlite3.IntegrityError:
                break
        else:
            self.fail("No final transaction attempted")
        self.assertEqual(self.store.get_context("session")["context_version"], 0)
        self.assertEqual(self.store.get_context("session")["compacted_through_seq"], 0)
        self.assertIsNotNone(self.plan().parent_candidate)
        self.store.db.execute("DROP TRIGGER reject_final")
        self.engine.commit(self.owner, plan, args)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 1)


if __name__ == "__main__":
    unittest.main()
