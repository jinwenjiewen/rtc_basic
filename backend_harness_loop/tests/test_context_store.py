"""M2 snapshot transactions, source visibility and closed-prefix fences."""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend_harness_loop.store import ContextConflict, RuntimeStore, StaleTurn, StoreError


def summary(ref, text="预算保留为3000，不接受周末；动作仍待确认"):
    result = {name: [] for name in ("goals", "items", "open_questions", "pending_actions", "topic_index")}
    result["items"] = [{"id": "constraint-1", "kind": "constraint", "value": text,
                        "source_type": "user_statement", "source_refs": [ref], "status": "active"}]
    return result


def native(identifier, name, arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": identifier,
            "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


class ContextStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "context.sqlite3"
        self.store = RuntimeStore(self.path)
        self.store.create_session("session", "run", "flash")
        self.store.create_session("foreign", "other-run", "pro")
        self.ordinal = 0

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def admit(self, text="合成用户条件", session="session"):
        self.ordinal += 1
        return self.store.admit_turn(session, self.ordinal, text, "connection-" + str(self.ordinal))

    def closed(self, text="预算3000，不接受周末", reply="已记录，尚未执行"):
        target = self.admit(text)
        self.store.complete_turn(target, "generation_completed", reply)
        self.store.record_delivery(target, reply, "delta")
        self.store.record_delivery(target, "", "completed")
        return target

    def proposal(self, owner, index=0, *, allow_completed=False, owner_in_material=False):
        material = self.store.context_material(owner.session_id, None if owner_in_material else owner.turn_id)
        context = self.store.get_context(owner.session_id)
        selected = material["groups"][index]
        refs = [message["reference"] for group in material["groups"] for message in group["messages"]]
        proposal = {"ledger_through_seq": material["ledger_through_seq"],
                    "eligible_through_seq": selected["through_seq"],
                    "previous_compacted_through_seq": context["compacted_through_seq"],
                    "expected_notes_version": self.store.get_notes(owner.session_id)["version"],
                    "visible_refs": refs, "protected_refs": [],
                    "material_fingerprint": material["material_fingerprint"],
                    "owner_in_material": owner_in_material,
                    "prompt_version": "context-compact-v1", "input_fingerprint": "synthetic-input"}
        return context["context_version"], selected["through_seq"], summary(selected["messages"][0]["reference"]), [], proposal

    def commit(self, owner, proposal=None, **options):
        version, covered, candidate, kept, frozen = proposal or self.proposal(owner)
        return self.store.commit_context(owner, version, covered, candidate, kept, input_ref=frozen, **options)

    def test_material_includes_all_history_without_twenty_turn_truncation(self):
        for index in range(25):
            self.closed("第" + str(index) + "轮精确条件")
        owner = self.admit("继续")
        material = self.store.context_material("session", owner.turn_id)
        self.assertEqual(len(material["groups"]), 25)
        self.assertEqual(sum(len(group["messages"]) for group in material["groups"]), 50)
        self.assertFalse(material["blocked_turn_ids"])
        self.assertTrue(all(left["through_seq"] < right["first_seq"]
                            for left, right in zip(material["groups"], material["groups"][1:])))
        self.assertEqual(material["groups"][0]["messages"][0]["content"], "第0轮精确条件")

    def test_commit_preserves_raw_history_and_public_status_omits_summary(self):
        self.closed()
        owner = self.admit("继续")
        proposal = self.proposal(owner)
        source_seq = int(proposal[2]["items"][0]["source_refs"][0][4:])
        before = self.store.db.execute("SELECT COUNT(*) FROM ledger WHERE kind='message'").fetchone()[0]
        result = self.commit(owner, proposal)
        self.assertEqual(result["context_version"], 1)
        self.assertEqual(self.store.get_context("session")["summary"], proposal[2])
        self.assertEqual(self.store.read_message("session", source_seq)["content"], "预算3000，不接受周末")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM ledger WHERE kind='message'").fetchone()[0], before)
        public = self.store.snapshot("session")
        self.assertEqual(public["context_compaction_count"], 1)
        self.assertEqual(public["compacted_through_seq"], proposal[1])
        self.assertTrue(public["context_summary_present"])
        self.assertNotIn("summary", public)
        self.assertNotIn("预算", json.dumps(public, ensure_ascii=False))

    def test_notes_update_advances_shared_context_version_and_fences_candidate(self):
        self.closed()
        owner = self.admit("现在改成不超过2500")
        proposal = self.proposal(owner)
        self.store.update_notes(owner, 0, "新约束2500")
        self.assertEqual(self.store.get_notes("session")["version"], 1)
        self.assertEqual(self.store.get_context("session")["context_version"], 1)
        with self.assertRaises(ContextConflict):
            self.commit(owner, proposal)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)

    def test_new_input_or_interrupt_rejects_old_epoch_without_context_version_change(self):
        self.closed()
        owner = self.admit()
        proposal = self.proposal(owner)
        next_owner = self.admit("替换输入")
        with self.assertRaises(StaleTurn):
            self.commit(owner, proposal)
        self.assertTrue(self.store.is_current(next_owner))
        fresh = self.proposal(next_owner)
        self.store.interrupt("session", round_id=next_owner.round_id)
        with self.assertRaises(StaleTurn):
            self.commit(next_owner, fresh)
        self.assertEqual(self.store.get_context("session")["context_version"], 0)

    def test_rejects_foreign_unseen_and_partial_source_coverage(self):
        self.closed()
        foreign = self.admit("另一会话机密", "foreign")
        foreign_seq = self.store.db.execute("SELECT seq FROM ledger WHERE turn_id=? AND role='user'", (foreign.turn_id,)).fetchone()[0]
        owner = self.admit()
        for mode in ("foreign", "unseen", "missing-prefix-message"):
            with self.subTest(mode=mode):
                proposal = list(deepcopy(self.proposal(owner)))
                if mode == "foreign":
                    ref = "seq:" + str(foreign_seq)
                    proposal[2] = summary(ref)
                    proposal[4]["visible_refs"].append(ref)
                elif mode == "unseen":
                    proposal[4]["visible_refs"].remove(proposal[2]["items"][0]["source_refs"][0])
                else:
                    proposal[4]["visible_refs"].pop()
                with self.assertRaises(StoreError):
                    self.commit(owner, proposal)
        self.assertFalse(self.store.get_context("session")["snapshot_id"])

    def test_rejects_partial_boundary_future_sources_and_protected_prefix(self):
        self.closed()
        self.closed("第二轮材料")
        owner = self.admit()
        for mode in ("partial-boundary", "future-source", "protected"):
            with self.subTest(mode=mode):
                proposal = list(deepcopy(self.proposal(owner)))
                if mode == "partial-boundary":
                    proposal[1] = int(proposal[2]["items"][0]["source_refs"][0][4:])
                    proposal[4]["eligible_through_seq"] = proposal[1]
                elif mode == "future-source":
                    proposal[4]["ledger_through_seq"] = proposal[1]
                else:
                    proposal[4]["protected_refs"] = proposal[2]["items"][0]["source_refs"]
                with self.assertRaises(StoreError):
                    self.commit(owner, proposal)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)

    def test_closed_prefix_cannot_cross_active_or_open_protocol_group(self):
        first = self.closed()
        self.closed("第二轮材料")
        owner = self.admit()
        self.store.db.execute("UPDATE turns SET state='running' WHERE turn_id=?", (first.turn_id,))
        with self.assertRaises(StoreError):
            self.commit(owner, self.proposal(owner, 1))
        self.store.db.execute("UPDATE turns SET state='generation_completed' WHERE turn_id=?", (first.turn_id,))
        source_seq = self.store.db.execute("SELECT seq FROM ledger WHERE turn_id=? AND role='assistant'", (first.turn_id,)).fetchone()[0]
        self.store.db.execute("INSERT INTO tool_calls(turn_id,tool_call_id,name,arguments,state,assistant_seq) VALUES(?,?,'context_read_history','{}','prepared',?)",
                              (first.turn_id, "unclosed", source_seq))
        with self.assertRaises(StoreError):
            self.commit(owner, self.proposal(owner, 1))
        self.assertEqual(self.store.get_context("session")["context_version"], 0)

    def test_snapshot_versions_and_coverage_only_advance_and_old_rows_are_immutable(self):
        self.closed()
        self.closed("后续精确条件")
        owner = self.admit()
        first = self.commit(owner)
        old = dict(self.store.db.execute("SELECT * FROM context_snapshots WHERE snapshot_id=?", (first["snapshot_id"],)).fetchone())
        second = self.commit(owner, self.proposal(owner, 1))
        self.assertGreater(second["compacted_through_seq"], first["compacted_through_seq"])
        self.assertEqual(second["context_version"], 2)
        self.assertEqual(dict(self.store.db.execute("SELECT * FROM context_snapshots WHERE snapshot_id=?", (first["snapshot_id"],)).fetchone()), old)
        with self.assertRaises(StoreError):
            self.commit(owner, self.proposal(owner, 0))
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 2)

    def test_background_commit_keeps_epoch_guard_and_requires_completed_owner(self):
        self.closed()
        owner = self.closed("当前已回答的轮次")
        proposal = self.proposal(owner, owner_in_material=True)
        with self.assertRaises(StaleTurn):
            self.commit(owner, proposal)
        # Maintenance audit events after the frozen bound are not new material.
        self.store.append_event(owner, "model.started", {"phase": "compact_only"})
        result = self.commit(owner, proposal, allow_completed=True)
        self.assertEqual(result["context_version"], 1)
        self.admit("新输入抢占")
        with self.assertRaises(StaleTurn):
            self.commit(owner, self.proposal(owner, 1, owner_in_material=True), allow_completed=True)

    def test_native_compaction_result_snapshot_and_event_commit_once(self):
        self.closed()
        owner = self.admit()
        proposal = self.proposal(owner)
        args = {"expected_context_version": proposal[0], "covered_through_seq": proposal[1],
                "summary": proposal[2], "keep_refs": proposal[3]}
        self.store.prepare_tool_calls(owner, native("compact", "context_compact", args))
        self.store.mark_tool_dispatched(owner, "compact")
        proposal = self.proposal(owner)
        result = self.commit(owner, proposal, tool_call_id="compact")
        self.assertEqual(self.store.get_tool_result(owner, "compact")["data"], result)
        self.assertEqual(self.commit(owner, proposal, tool_call_id="compact"), result)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM ledger WHERE kind='context.compacted'").fetchone()[0], 1)
        self.assertEqual(sum(message.get("tool_call_id") == "compact" for message in self.store.get_turn_messages(owner)), 1)

    def test_failed_native_result_rolls_back_snapshot_version_and_event(self):
        self.closed()
        owner = self.admit()
        proposal = self.proposal(owner)
        args = {"expected_context_version": proposal[0], "covered_through_seq": proposal[1],
                "summary": proposal[2], "keep_refs": proposal[3]}
        self.store.prepare_tool_calls(owner, native("compact", "context_compact", args))
        self.store.mark_tool_dispatched(owner, "compact")
        proposal = self.proposal(owner)
        original = self.store._finish_tool
        def fail(*args):
            raise sqlite3.IntegrityError("synthetic commit failure")
        self.store._finish_tool = fail
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self.commit(owner, proposal, tool_call_id="compact")
        finally:
            self.store._finish_tool = original
        self.assertEqual(self.store.get_context("session")["context_version"], 0)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM ledger WHERE kind='context.compacted'").fetchone()[0], 0)
        self.assertIsNone(self.store.get_tool_result(owner, "compact"))

    def test_late_nonmessage_audit_does_not_move_boundary_or_invalidate_material(self):
        old = self.closed()
        owner = self.admit()
        proposal = self.proposal(owner)
        self.store.append_event(old, "playback.observed", {"state": "silent"})
        material = self.store.context_material("session", owner.turn_id)
        self.assertEqual(material["groups"][0]["through_seq"], proposal[1])
        self.assertEqual(material["material_fingerprint"], proposal[4]["material_fingerprint"])
        self.assertEqual(self.commit(owner, proposal)["context_version"], 1)

    def test_late_delivered_message_is_explicit_and_cannot_be_silently_covered(self):
        old = self.admit("未完成的问题")
        self.store.complete_turn(old, "failed", "")
        owner = self.admit()
        proposal = self.proposal(owner)
        self.store.record_delivery(old, "晚到但确实交付的片段", "delta")
        material = self.store.context_material("session", owner.turn_id)
        self.assertTrue(material["groups"][0]["late_message_seqs"])
        self.assertTrue(material["groups"][0]["blocked"])
        with self.assertRaises(StoreError):
            self.commit(owner, proposal)
        self.assertFalse(self.store.get_context("session")["snapshot_id"])

    def test_material_marks_unheard_generation_separately_from_delivery(self):
        old = self.admit()
        self.store.complete_turn(old, "generation_completed", "全部生成内容")
        self.store.record_delivery(old, "只交付了前半", "delta")
        owner = self.admit()
        last = self.store.context_material("session", owner.turn_id)["groups"][0]["messages"][-1]
        self.assertEqual(last["content"], "只交付了前半")
        self.assertEqual(last["generated_content"], "全部生成内容")
        self.assertEqual(last["delivery_status"], "streamed_unconfirmed")

    def test_snapshot_survives_reopen_and_old_schema_migrates(self):
        self.closed()
        owner = self.admit()
        committed = self.commit(owner)
        private = self.store.get_context("session")
        self.store.close()
        self.store = RuntimeStore(self.path)
        self.assertEqual(self.store.get_context("session"), private)
        self.assertEqual(self.store.snapshot("session")["compacted_through_seq"], committed["compacted_through_seq"])
        old_path = Path(self.folder.name) / "m1.sqlite3"
        with sqlite3.connect(old_path) as old:
            old.execute("""CREATE TABLE sessions(session_id TEXT PRIMARY KEY,run_id TEXT UNIQUE NOT NULL,
                profile_id TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active',epoch INTEGER NOT NULL DEFAULT 0,
                notes_version INTEGER NOT NULL DEFAULT 0,notes_text TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL)""")
            old.execute("INSERT INTO sessions(session_id,run_id,profile_id,created_at) VALUES('old','old-run','flash',1)")
        old.close()
        migrated = RuntimeStore(old_path)
        try:
            self.assertEqual(migrated.get_context("old")["context_version"], 0)
            self.assertEqual(migrated.snapshot("old")["context_compaction_count"], 0)
        finally:
            migrated.close()

    def test_invalid_refs_and_summary_are_safe_errors_before_any_write(self):
        self.closed()
        owner = self.admit()
        for bad_ref in (None, {}, 0, "seq:0", "seq:01", "seq:9223372036854775808", "other:1"):
            with self.subTest(ref_type=type(bad_ref).__name__):
                proposal = list(deepcopy(self.proposal(owner)))
                proposal[3] = [bad_ref]
                with self.assertRaises(StoreError):
                    self.commit(owner, proposal)
        proposal = list(self.proposal(owner))
        proposal[2] = {"items": [{"value": "没有来源"}]}
        with self.assertRaises(StoreError):
            self.commit(owner, proposal)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)


if __name__ == "__main__":
    unittest.main()
