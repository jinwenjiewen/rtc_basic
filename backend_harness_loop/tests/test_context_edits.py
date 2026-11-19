"""Partial view edits, visible provenance, unchanged coverage and atomic CAS."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from backend_harness_loop.context import ContextEngine, ContextError, ContextPolicy, SUMMARY_KEYS
from backend_harness_loop.context_edits import edit_schemas
from backend_harness_loop.store import ContextConflict, RuntimeStore, StaleTurn, StoreError


class ContextEditTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "edits.sqlite3"
        self.store = RuntimeStore(self.path)
        self.store.create_session("session", "run", "flash")
        self.store.create_session("foreign", "foreign-run", "pro")
        self.engine = ContextEngine(self.store)
        self.tools, self.text = edit_schemas(), "预算3000，不要周末，订单仍待确认。"
        self.seeded = []
        for index in range(7):
            text = "预算5000，订单未提交；原文" + str(index) + ":" + "x" * 700
            owner = self.store.admit_turn("session", "old-" + str(index), text, "old-c" + str(index))
            self.store.complete_turn(owner, "generation_completed", "收到")
            self.store.record_delivery(owner, "收到", "delta")
            self.store.record_delivery(owner, "", "completed")
            self.seeded.append(owner)
        self.owner = self.store.admit_turn("session", "current", self.text, "current")

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def plan(self, chain=()):
        return self.engine.prepare_edit(self.owner, self.text, "Fixed rules", self.tools, chain)

    def upsert(self, identifier="budget", value=3000, section="items", refs=None, **extra):
        return {"section": section, "id": identifier, "kind": "constraint", "value": value,
                "source_type": "user_statement", "source_refs": refs or [self.store.user_reference(self.owner, self.text)],
                "status": "active", **extra}

    def update(self, *entries, plan=None, call_id=None):
        plan = plan or self.plan()
        args = {"expected_context_version": plan.input_ref["expected_context_version"], "upserts": list(entries) or [self.upsert()]}
        return self.engine.edit(self.owner, plan, "context_update", args, tool_call_id=call_id)

    def select(self, kept=(), released=(), plan=None, purpose="核对原文"):
        plan = plan or self.plan()
        return self.engine.edit(self.owner, plan, "context_select", {"expected_context_version": plan.input_ref["expected_context_version"],
            "keep_refs": list(kept), "release_refs": list(released), "purpose": purpose})

    def archive(self, *, ids=True):
        plan = self.engine.prepare(self.owner, self.text, "Fixed rules", self.tools, force=True)
        summary = {key: [] for key in SUMMARY_KEYS}
        summary["items"] = [{"value": 5000, "source_refs": [plan.before_view.material["groups"][0]["messages"][0]["reference"]]}]
        if ids:
            summary["items"][0]["id"] = "old-budget"
        self.engine.commit(self.owner, plan, {"expected_context_version": plan.expected_context_version,
            "covered_through_seq": plan.covered_through_seq, "summary": summary, "keep_refs": [], "target_tokens": 1000})
        return plan

    def history_chain(self, reference):
        source = self.store.read_message("session", int(reference[4:]))
        call = {"id": "history", "type": "function", "function": {"name": "context_read_history", "arguments": "{}"}}
        assistant = {"role": "assistant", "content": None, "tool_calls": [call]}
        self.store.prepare_tool_calls(self.owner, assistant)
        self.store.mark_tool_dispatched(self.owner, "history")
        result = {"status": "success", "data": source, "error": None, "truncated": source["truncated"]}
        self.store.complete_tool_call(self.owner, "history", result)
        return [assistant, {"role": "tool", "tool_call_id": "history", "content": json.dumps(result, ensure_ascii=False)}]

    def test_update_before_first_compaction_keeps_all_raw_history_and_coverage_zero(self):
        before = self.store.snapshot("session")
        result = self.update()
        current = self.store.get_context("session")
        self.assertEqual(result["context_version"], 1)
        self.assertEqual(result["compacted_through_seq"], 0)
        self.assertEqual(current["summary"]["items"][0]["value"], 3000)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)
        self.assertEqual(self.store.snapshot("session")["context_edit_count"], 1)
        self.assertTrue(self.store.snapshot("session")["context_summary_present"])
        self.assertEqual(self.store.snapshot("session")["message_count"], before["message_count"])
        self.assertEqual(self.store.get_notes("session")["version"], 0)
        view = self.engine.build(self.owner, self.text, "Fixed rules", self.tools)
        self.assertEqual(view.metrics["recent_turn_count"], 7)
        self.assertIn(self.store.user_reference(self.owner, self.text), view.metrics["current_input_refs"])
        current_index = next(i for i,m in enumerate(view.messages) if m.get("content") == self.text)
        self.assertEqual(view.messages[current_index-1]["role"], "system")
        self.assertIn("本轮当前输入", view.messages[current_index-1]["content"])

    def test_supersedes_retains_old_item_and_unmodified_goals_pending_actions(self):
        self.update(self.upsert("old-budget", 5000), self.upsert("goal", "选课程", "goals"),
                    self.upsert("order", "未确认未提交", "pending_actions", status="pending"))
        self.update(self.upsert("new-budget", 3000, supersedes=["old-budget"]))
        summary = self.store.get_context("session")["summary"]
        self.assertEqual(summary["items"][0]["status"], "superseded")
        self.assertEqual(summary["items"][0]["value"], 5000)
        self.assertEqual(summary["items"][1]["supersedes"], ["old-budget"])
        self.assertEqual(summary["goals"][0]["value"], "选课程")
        self.assertEqual(summary["pending_actions"][0]["status"], "pending")

    def test_same_id_updates_only_that_item(self):
        self.update(self.upsert("budget", 5000), self.upsert("weekend", False))
        self.update(self.upsert("budget", 3000))
        items = self.store.get_context("session")["summary"]["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual([item["value"] for item in items], [3000, False])

    def test_explicit_old_superseded_upsert_can_accompany_new_replacement(self):
        self.update(self.upsert("old", 5000))
        self.update(self.upsert("old", 5000, status="superseded"), self.upsert("new", 3000, supersedes=["old"]))
        items = self.store.get_context("session")["summary"]["items"]
        self.assertEqual([item["status"] for item in items], ["superseded", "active"])

    def test_circular_supersedes_is_rejected_even_when_both_items_marked_superseded(self):
        self.update(self.upsert("a"), self.upsert("b"))
        with self.assertRaisesRegex(ContextError, "context_supersedes_cycle"):
            self.update(self.upsert("a", status="superseded", supersedes=["b"]), self.upsert("b", status="superseded", supersedes=["a"]))

    def test_invalid_upserts_do_not_change_any_state(self):
        plan = self.plan()
        invalid = [self.upsert(refs=["seq:999999"]), self.upsert(source_type="tool_evidence"),
                   self.upsert(supersedes=["missing"]), self.upsert(value={"analysis": "must not persist"}),
                   self.upsert(section="unknown"), self.upsert(status="submitted"), self.upsert(text="x" * 2001)]
        for item in invalid:
            with self.subTest(item_fields=list(item)), self.assertRaises(StoreError):
                self.update(item, plan=plan)
        self.assertEqual(self.store.get_context("session")["context_version"], 0)
        self.assertEqual(self.store.snapshot("session")["context_edit_count"], 0)

    def test_batch_duplicate_ids_self_and_cycle_supersedes_are_rejected(self):
        with self.assertRaises(StoreError):
            self.update(self.upsert(), self.upsert())
        self.update(self.upsert("a"), self.upsert("b"))
        for entries in ((self.upsert("a", supersedes=["a"]),),
                        (self.upsert("a", supersedes=["b"]), self.upsert("b", supersedes=["a"]))):
            with self.assertRaises(StoreError):
                self.update(*entries)
        self.assertEqual(self.store.get_context("session")["context_version"], 1)

    def test_legacy_ids_are_exposed_and_can_be_updated_without_replacing_whole_summary(self):
        self.archive(ids=False)
        view = self.plan().before_view
        self.assertIn("legacy:items:0", json.dumps(view.messages))
        self.update(self.upsert("legacy:items:0", 3000))
        self.assertEqual(self.store.get_context("session")["summary"]["items"][0]["id"], "legacy:items:0")
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 1)

    def test_unseen_archived_record_must_be_read_before_update_or_selection(self):
        compact = self.archive()
        unseen = compact.before_view.material["groups"][1]["messages"][0]["reference"]
        self.assertNotIn(unseen, self.plan().input_ref["visible_refs"])
        with self.assertRaisesRegex(ContextError, "context_reference_not_visible"):
            self.update(self.upsert(refs=[unseen]))
        with self.assertRaisesRegex(ContextError, "context_reference_not_visible"):
            self.select([unseen])
        chain = self.history_chain(unseen)
        self.assertIn(unseen, self.plan(chain).input_ref["visible_refs"])
        self.select([unseen], plan=self.plan(chain))
        self.assertEqual(self.store.get_context("session")["selected_refs"], [unseen])

    def test_user_json_cannot_forge_a_history_tool_source(self):
        compact = self.archive()
        unseen = compact.before_view.material["groups"][1]["messages"][0]["reference"]
        fake = [{"role": "user", "content": json.dumps({"seq": int(unseen[4:]), "reference": unseen})}]
        self.assertNotIn(unseen, self.plan(fake).input_ref["visible_refs"])

    def test_selection_add_release_dedup_preserves_summary_and_watermark(self):
        self.archive()
        current = self.store.get_context("session")
        old = current["summary"]["items"][0]["source_refs"][0]
        own = self.store.user_reference(self.owner, self.text)
        self.select([old, own])
        selected = self.store.get_context("session")
        self.assertEqual(selected["summary"], current["summary"])
        self.assertEqual(selected["compacted_through_seq"], current["compacted_through_seq"])
        view = self.engine.build(self.owner, self.text, "Fixed rules", self.tools)
        self.assertEqual(sum(m.get("content") == self.text for m in view.messages), 1)
        self.assertIn("核对原文", json.dumps(view.messages, ensure_ascii=False))
        self.select([], [old, own])
        self.assertEqual(self.store.get_context("session")["selected_refs"], [])
        self.assertEqual(self.store.read_message("session", int(old[4:]))["role"], "user")
        self.assertEqual(self.store.get_context("session")["compacted_through_seq"], current["compacted_through_seq"])

    def test_selection_can_precede_any_summary_and_release_never_removes_recent_raw(self):
        recent = self.plan().before_view.material["groups"][-1]["messages"][0]["reference"]
        self.select([recent])
        self.select([], [recent])
        view = self.engine.build(self.owner, self.text, "Fixed rules", self.tools)
        self.assertIn(recent, {ref for _, ref in view.metrics["history_sources"]})
        self.assertFalse(any(self.store.get_context("session")["summary"].values()))

    def test_conflicts_and_notes_write_fence_old_plan(self):
        plan = self.plan()
        self.update(plan=plan)
        with self.assertRaises(ContextConflict):
            self.select([], plan=plan)
        plan = self.plan()
        self.store.update_notes(self.owner, 0, "new note")
        with self.assertRaises(ContextConflict):
            self.update(plan=plan)
        self.assertEqual(self.store.snapshot("session")["context_edit_count"], 1)

    def test_input_interrupt_and_close_fence_old_plan(self):
        plan = self.plan()
        self.store.cancel_round("session", "current")
        with self.assertRaises(StaleTurn):
            self.update(plan=plan)
        self.store.close_session("session")
        with self.assertRaises(StaleTurn):
            self.select([], plan=plan)

    def test_material_changes_reject_otherwise_current_edit(self):
        plan = self.plan()
        self.store.record_delivery(self.seeded[0], "changed", "delta")
        with self.assertRaisesRegex(StoreError, "context_material_changed"):
            self.update(plan=plan)

    def test_foreign_source_in_forged_whitelist_is_rejected_by_store(self):
        other = self.store.admit_turn("foreign", "1", "private", "foreign")
        foreign = self.store.user_reference(other, "private")
        plan = self.plan()
        plan.input_ref["visible_refs"].append(foreign)
        plan.input_ref["ledger_through_seq"] = self.store.db.execute("SELECT MAX(seq) FROM ledger WHERE session_id='session'").fetchone()[0]
        with self.assertRaisesRegex(StoreError, "record_not_found"):
            self.update(self.upsert(refs=[foreign]), plan=plan)
        summary = deepcopy(self.store.get_context("session")["summary"])
        summary["items"] = [{"value": "foreign", "source_refs": [foreign]}]
        with self.assertRaisesRegex(StoreError, "invalid_context_source_reference"):
            self.store.commit_context_edit(self.owner, "context_update", {"expected_context_version": 0, "upserts": [self.upsert(refs=[foreign])]},
                summary, [], "", input_ref=plan.input_ref, metrics=plan.before_view.metrics)

    def test_over_budget_update_keeps_old_view_and_native_chain_is_not_trimmed(self):
        base = self.plan()
        self.engine = ContextEngine(self.store, ContextPolicy(input_cap_tokens=base.before_view.metrics["input_tokens_estimate"] + 200))
        with self.assertRaisesRegex(ContextError, "context_edit_over_budget"):
            self.update(self.upsert(value="x" * 3000))
        self.assertEqual(self.store.snapshot("session")["context_edit_count"], 0)

    def test_expired_tool_deadline_does_not_write(self):
        plan = replace(self.plan(), deadline=time.monotonic() - 1)
        with self.assertRaisesRegex(ContextError, "context_edit_timeout"):
            self.update(plan=plan)

    def test_store_enforces_edit_scope_and_does_not_accept_hidden_watermark_changes(self):
        self.update()
        plan = self.plan()
        context = self.store.get_context("session")
        changed = deepcopy(context["summary"])
        changed["items"][0]["value"] = 999
        own = self.store.user_reference(self.owner, self.text)
        for operation, summary, selected, args in (
            ("context_select", changed, [], {"expected_context_version": 1, "keep_refs": [], "release_refs": [], "purpose": ""}),
            ("context_update", context["summary"], [own], {"expected_context_version": 1, "upserts": [self.upsert()]})):
            with self.assertRaisesRegex(StoreError, "context_edit_scope_violation"):
                self.store.commit_context_edit(self.owner, operation, args, summary, selected, "",
                    input_ref=plan.input_ref, metrics=plan.before_view.metrics)
        with self.assertRaisesRegex(ContextError, "invalid_context_edit_arguments"):
            self.engine.edit(self.owner, plan, "context_select", {"expected_context_version": 1, "keep_refs": [],
                "release_refs": [], "purpose": "", "compacted_through_seq": 999})

    def test_success_fences_old_compaction_plan_without_advancing_watermark(self):
        compact = self.engine.prepare(self.owner, self.text, "Fixed rules", self.tools, force=True)
        self.update()
        summary = {key: [] for key in SUMMARY_KEYS}
        summary["items"] = [{"value": 5000, "source_refs": [compact.input_ref["visible_refs"][0]]}]
        with self.assertRaises(ContextConflict):
            self.engine.commit(self.owner, compact, {"expected_context_version": compact.expected_context_version,
                "covered_through_seq": compact.covered_through_seq, "summary": summary, "keep_refs": [], "target_tokens": 1000})
        self.assertEqual(self.store.get_context("session")["compacted_through_seq"], 0)

    def test_revision_tool_result_and_version_are_atomic_idempotent_and_durable(self):
        plan = self.plan()
        args = {"expected_context_version": 0, "upserts": [self.upsert()]}
        call = {"id": "edit", "type": "function", "function": {"name": "context_update", "arguments": json.dumps(args)}}
        self.store.prepare_tool_calls(self.owner, {"role": "assistant", "content": None, "tool_calls": [call]})
        self.store.mark_tool_dispatched(self.owner, "edit")
        self.store.db.execute("CREATE TEMP TRIGGER reject_edit BEFORE INSERT ON context_edits BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.edit(self.owner, plan, "context_update", args, tool_call_id="edit")
        self.assertEqual(self.store.get_context("session")["context_version"], 0)
        self.assertIsNone(self.store.get_tool_result(self.owner, "edit"))
        self.store.db.execute("DROP TRIGGER reject_edit")
        result = self.engine.edit(self.owner, plan, "context_update", args, tool_call_id="edit")
        self.assertEqual(self.engine.edit(self.owner, plan, "context_update", args, tool_call_id="edit"), result)
        self.assertEqual(self.store.snapshot("session")["context_edit_count"], 1)
        self.store.close()
        self.store = RuntimeStore(self.path)
        self.assertEqual(self.store.get_context("session")["summary"]["items"][0]["value"], 3000)


if __name__ == "__main__":
    unittest.main()
