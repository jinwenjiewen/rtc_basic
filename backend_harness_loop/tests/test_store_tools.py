"""Actual SQLite fences, native tool closure and model-selected context tools."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from backend_harness_loop.store import (
    ContextConflict, DuplicateTurn, RoundCancelled, RoundConflict, RuntimeStore,
    StaleTurn, StoreError,
)
from backend_harness_loop.tools import MAX_RESULT_BYTES, ToolRegistry


def call_message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
        for identifier, name, arguments in calls
    ]}


def success(data):
    return {"status": "success", "data": data, "error": None, "truncated": False}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "runtime.sqlite3"
        self.store = RuntimeStore(self.path)
        self.store.create_session("session-a", "run-a", "flash")
        self.store.create_session("session-b", "run-b", "pro")

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def turn(self, round_id=1, text="你好", session="session-a"):
        return self.store.admit_turn(session, round_id, text, "connection-" + str(round_id))

    def test_sessions_bind_immutable_run_and_profile(self):
        self.store.create_session("session-a", "run-a", "flash")
        for arguments in (("session-a", "run-new", "flash"), ("session-a", "run-a", "pro"), ("session-new", "run-a", "flash")):
            with self.subTest(arguments=arguments), self.assertRaises(StoreError):
                self.store.create_session(*arguments)
        self.assertEqual(self.store.get_profile_id("session-b"), "pro")

    def test_round_dedup_does_not_use_text_or_cancel_a_newer_turn(self):
        first = self.turn(0, "同样的话")
        second = self.turn(1, "同样的话")
        self.assertNotEqual(first.turn_id, second.turn_id)
        self.assertEqual(second.epoch, first.epoch + 1)
        with self.assertRaises(DuplicateTurn) as duplicate:
            self.store.admit_turn("session-a", 0, "同样的话", "retry-connection")
        self.assertEqual(duplicate.exception.target, first)
        self.assertEqual(duplicate.exception.status, "cancelled")
        with self.assertRaises(RoundConflict):
            self.store.admit_turn("session-a", 0, "不同的话", "retry-connection")
        self.assertTrue(self.store.is_current(second))
        self.assertEqual(self.store.get_session("session-a")["epoch"], second.epoch)

    def test_every_identity_component_fences_semantic_writes(self):
        turn = self.turn()
        for bad in (replace(turn, epoch=turn.epoch + 1), replace(turn, run_id="run-b"), replace(turn, session_id="session-b"), replace(turn, connection_id="old-connection")):
            with self.subTest(target=bad):
                self.assertFalse(self.store.is_current(bad))
                self.assertFalse(self.store.complete_turn(bad, "generation_completed", "stale output"))
                with self.assertRaises(StaleTurn):
                    self.store.update_notes(bad, 0, "stale note")
        self.assertEqual(self.store.get_notes("session-a"), {"version": 0, "text": ""})

    def test_interrupt_is_precise_and_cancel_tombstones_survive_before_admission(self):
        current = self.turn(2)
        self.assertFalse(self.store.interrupt("session-a"))
        self.assertFalse(self.store.interrupt("session-a", turn_id=current.turn_id, epoch=current.epoch + 1))
        self.assertTrue(self.store.cancel_round("session-a", 1, "rtc_interrupt"))
        self.assertTrue(self.store.is_current(current))
        with self.assertRaises(RoundCancelled):
            self.turn(1)
        self.assertTrue(self.store.interrupt("session-a", turn_id=current.turn_id, epoch=current.epoch))
        self.assertFalse(self.store.is_current(current))
        newer = self.turn(3)
        self.assertFalse(self.store.interrupt("session-a", round_id=2))
        self.assertTrue(self.store.is_current(newer))

    def test_new_input_closes_pending_native_tools_before_next_history(self):
        old = self.turn(1)
        message = call_message(("read-1", "context_read_history", {}), ("read-2", "context_search_history", {"query": "预算"}))
        self.store.prepare_tool_calls(old, message)
        self.assertTrue(self.store.mark_tool_dispatched(old, "read-1"))
        self.store.complete_tool_call(old, "read-1", success({"messages": []}))
        new = self.turn(2)
        messages = self.store.get_turn_messages(old)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool", "tool"])
        self.assertEqual({m["tool_call_id"] for m in messages if m["role"] == "tool"}, {"read-1", "read-2"})
        cancelled = self.store.get_tool_result(old, "read-2")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(self.store.complete_tool_call(old, "read-2", success("late")))
        self.assertTrue(self.store.is_current(new))
        with self.assertRaises(StoreError):
            self.store.get_tool_result(replace(old, session_id="session-b"), "read-1")

    def test_notes_cas_binds_prepared_tool_and_commits_result_atomically(self):
        turn = self.turn()
        arguments = {"expected_version": 0, "text": "用户预算是3000元，不接受周末。"}
        self.store.prepare_tool_calls(turn, call_message(("note-1", "context_update_notes", arguments)))
        with self.assertRaises(StoreError):
            self.store.update_notes(turn, 0, "different", tool_call_id="note-1")
        self.assertEqual(self.store.get_notes(turn.session_id)["version"], 0)
        self.assertIsNone(self.store.get_tool_result(turn, "note-1"))
        updated = self.store.update_notes(turn, 0, arguments["text"], tool_call_id="note-1")
        result = success(updated)
        self.assertEqual(self.store.get_tool_result(turn, "note-1"), result)
        self.assertTrue(self.store.complete_tool_call(turn, "note-1", result))
        self.assertEqual(self.store.update_notes(turn, 0, arguments["text"], tool_call_id="note-1"), updated)
        self.assertEqual(self.store.get_notes(turn.session_id)["version"], 1)
        self.assertEqual(sum(m["role"] == "tool" for m in self.store.get_turn_messages(turn)), 1)
        with self.assertRaises(ContextConflict):
            self.store.update_notes(turn, 0, "old version")
        newer = self.turn(2)
        with self.assertRaises(StaleTurn):
            self.store.update_notes(turn, 1, "late update")
        self.assertTrue(self.store.is_current(newer))

    def test_restart_closes_native_protocol_and_preserves_only_actual_partial_delivery(self):
        turn = self.turn()
        self.store.prepare_tool_calls(turn, call_message(("pending", "context_read_history", {})))
        self.store.mark_tool_dispatched(turn, "pending")
        self.store.record_delivery(turn, "实际发出的部分", "delta")
        self.store.close()
        self.store = RuntimeStore(self.path)
        self.assertFalse(self.store.is_current(turn))
        lookup = self.store.lookup_round("session-a", 1)
        self.assertEqual(lookup["status"], "failed")
        self.assertEqual(self.store.get_tool_result(turn, "pending")["error"]["code"], "runtime_restarted")
        history = self.store.get_history("session-a")["messages"]
        self.assertEqual(history[-1]["content"], "实际发出的部分")
        self.assertEqual(history[-1]["turn_status"], "failed")
        with self.assertRaises(DuplicateTurn):
            self.store.admit_turn("session-a", 1, "你好", "retry")

    def test_old_delivery_audit_cannot_mutate_current_context(self):
        old = self.turn(1)
        self.store.complete_turn(old, "generation_completed", "完整生成，但并非都已发送")
        self.store.record_delivery(old, "实际一半", "delta")
        self.assertTrue(self.store.interrupt("session-a", round_id=1))
        current = self.turn(2)
        self.store.record_delivery(old, "", "interrupted")
        self.store.append_event(old, "late.observation", {"machine_code": "test"})
        history = self.store.get_history("session-a", exclude_turn_id=current.turn_id)
        self.assertEqual(history["messages"][-1]["content"], "实际一半")
        self.assertEqual(history["messages"][-1]["turn_status"], "generation_completed")
        self.assertTrue(self.store.is_current(current))
        self.assertEqual(self.store.get_notes("session-a")["version"], 0)

    def test_stage4_after_generation_preserves_terminal_state_and_pending_delivery(self):
        for ordinal, partial in ((1, ""), (2, "已发出的半句")):
            with self.subTest(partial=bool(partial)):
                target = self.turn(ordinal)
                self.store.complete_turn(target, "generation_completed", "已发出的半句和仍在队列的后半句")
                if partial:
                    self.store.record_delivery(target, partial, "delta")
                before = self.store.db.execute("SELECT * FROM turns WHERE turn_id=?", (target.turn_id,)).fetchone()
                self.assertTrue(self.store.cancel_round("session-a", ordinal))
                after = self.store.db.execute("SELECT * FROM turns WHERE turn_id=?", (target.turn_id,)).fetchone()
                self.assertEqual(after["state"], "generation_completed")
                self.assertEqual(after["assistant_text"], before["assistant_text"])
                self.assertEqual(after["completed_at"], before["completed_at"])
                self.assertEqual(after["delivery_text"], partial)
                self.assertEqual(after["delivery_status"], "interrupted")
                kinds = [row[0] for row in self.store.db.execute("SELECT kind FROM ledger WHERE turn_id=?", (target.turn_id,))]
                self.assertIn("playback.interrupted", kinds)
                self.assertIn("delivery.interrupted", kinds)
                self.assertNotIn("turn.cancelled", kinds)
                self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM cancelled_rounds WHERE session_id=? AND round_id=?", ("session-a", str(ordinal))).fetchone())
                interrupted_epoch = self.store.get_session("session-a")["epoch"]
                self.assertFalse(self.store.cancel_round("session-a", ordinal))
                self.assertEqual(self.store.get_session("session-a")["epoch"], interrupted_epoch)
                with self.assertRaises(DuplicateTurn) as duplicate:
                    self.store.admit_turn("session-a", ordinal, "你好", "retry")
                self.assertEqual(duplicate.exception.status, "generation_completed")
                spoken = [m for m in self.store.get_history("session-a")["messages"] if m["turn_id"] == target.turn_id and m["role"] == "assistant"]
                self.assertEqual([m["content"] for m in spoken], [partial] if partial else [])

    def test_stage4_after_completed_delivery_records_playback_without_rewriting_delivery(self):
        target = self.turn()
        self.store.complete_turn(target, "generation_completed", "完整回答")
        self.store.record_delivery(target, "完整回答", "delta")
        self.store.record_delivery(target, "", "completed")
        before = self.store.db.execute("SELECT * FROM turns WHERE turn_id=?", (target.turn_id,)).fetchone()
        self.assertTrue(self.store.cancel_round("session-a", 1))
        after = self.store.db.execute("SELECT * FROM turns WHERE turn_id=?", (target.turn_id,)).fetchone()
        self.assertEqual(after["state"], "generation_completed")
        self.assertEqual(after["completed_at"], before["completed_at"])
        self.assertEqual(after["delivery_status"], "completed")
        playback = json.loads(self.store.db.execute("SELECT payload_json FROM ledger WHERE turn_id=? AND kind='playback.interrupted'", (target.turn_id,)).fetchone()[0])
        self.assertEqual(playback, {"reason": "rtc_interrupt", "generation_status": "generation_completed", "delivery_status": "completed"})
        self.assertEqual(self.store.get_history("session-a")["messages"][-1]["content"], "完整回答")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM ledger WHERE turn_id=? AND kind='turn.cancelled'", (target.turn_id,)).fetchone()[0], 0)

    def test_exact_disconnect_interrupt_preserves_failed_generation_terminal(self):
        target = self.turn()
        self.store.record_delivery(target, "失败前已发出的部分", "delta")
        self.store.complete_turn(target, "failed", "")
        completed_at = self.store.db.execute("SELECT completed_at FROM turns WHERE turn_id=?", (target.turn_id,)).fetchone()[0]
        self.assertTrue(self.store.interrupt("session-a", turn_id=target.turn_id, epoch=target.epoch, round_id=target.round_id, connection_id=target.connection_id))
        row = self.store.db.execute("SELECT * FROM turns WHERE turn_id=?", (target.turn_id,)).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["completed_at"], completed_at)
        self.assertEqual(row["delivery_status"], "interrupted")
        self.assertEqual(self.store.get_history("session-a")["messages"][-1]["content"], "失败前已发出的部分")
        current = self.turn(2)
        self.assertFalse(self.store.interrupt("session-a", turn_id=target.turn_id, epoch=target.epoch, connection_id=target.connection_id))
        self.assertTrue(self.store.is_current(current))

    def test_old_stage4_only_observes_old_terminal_round_and_leaves_new_context_current(self):
        old = self.turn(1)
        self.store.complete_turn(old, "generation_completed", "旧轮完整生成")
        self.store.record_delivery(old, "旧轮片段", "delta")
        current = self.turn(2)
        self.store.update_notes(current, 0, "新轮有效上下文")
        notes = self.store.get_notes("session-a")
        self.assertTrue(self.store.cancel_round("session-a", 1))
        self.assertEqual(self.store.lookup_round("session-a", 1)["status"], "generation_completed")
        self.assertEqual(self.store.get_session("session-a")["epoch"], current.epoch)
        self.assertTrue(self.store.is_current(current))
        self.assertEqual(self.store.get_notes("session-a"), notes)
        observations = self.store.db.execute("SELECT turn_id FROM ledger WHERE kind='playback.interrupted'").fetchall()
        self.assertEqual([row[0] for row in observations], [old.turn_id])
        self.assertEqual(self.store.db.execute("SELECT delivery_status FROM turns WHERE turn_id=?", (old.turn_id,)).fetchone()[0], "interrupted")

    def test_partial_delivery_survives_empty_terminal_record_without_duplicate_assistant(self):
        for ordinal, status in ((1, "cancelled"), (2, "failed")):
            with self.subTest(status=status):
                target = self.turn(ordinal)
                self.store.record_delivery(target, "只发出的片段", "delta")
                if status == "cancelled":
                    self.store.interrupt("session-a", turn_id=target.turn_id, epoch=target.epoch)
                else:
                    self.store.complete_turn(target, "failed", "")
                self.store.record_delivery(target, "", "interrupted" if status == "cancelled" else "unknown")
                self.store.record_delivery(target, "", "interrupted" if status == "cancelled" else "unknown")
                messages = [m for m in self.store.get_history("session-a")["messages"] if m["turn_id"] == target.turn_id and m["role"] == "assistant"]
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0]["content"], "只发出的片段")
                self.assertEqual(messages[0]["turn_status"], status)

    def test_history_pages_complete_turns_and_scope_isolated(self):
        turns = []
        for index in range(1, 4):
            turn = self.turn(index, f"第{index}轮")
            self.store.prepare_tool_calls(turn, call_message((f"call-{index}", "context_read_history", {})))
            self.store.complete_tool_call(turn, f"call-{index}", success({"value": index}))
            self.store.complete_turn(turn, "generation_completed", f"回答{index}")
            self.store.record_delivery(turn, f"回答{index}", "delta")
            self.store.record_delivery(turn, "", "completed")
            turns.append(turn)
        other = self.turn(1, "PRIVATE OTHER SESSION", session="session-b")
        first = self.store.get_history("session-a", limit=1)
        self.assertEqual({m["turn_id"] for m in first["messages"]}, {turns[-1].turn_id})
        self.assertEqual([m["role"] for m in first["messages"]], ["user", "assistant", "tool", "assistant"])
        self.assertTrue(first["truncated"])
        second = self.store.get_history("session-a", limit=1, before_seq=first["next_before_seq"])
        self.assertEqual(second["messages"][0]["content"], "第2轮")
        self.assertNotIn("PRIVATE OTHER SESSION", json.dumps(first, ensure_ascii=False))
        other_seq = self.store.get_history("session-b")["messages"][0]["seq"]
        with self.assertRaises(StoreError):
            self.store.read_message("session-a", other_seq)

    def test_tool_exchange_rolls_back_when_any_terminal_is_invalid(self):
        turn = self.turn()
        before = len(self.store.get_turn_messages(turn))
        assistant = call_message(("a", "context_read_history", {}), ("b", "context_read_history", {}))
        with self.assertRaises(StoreError):
            self.store.save_tool_exchange(turn, assistant, [
                {"role": "tool", "tool_call_id": "a", "content": json.dumps(success(1))},
                {"role": "tool", "tool_call_id": "b", "content": json.dumps({"status": "invalid"})},
            ])
        self.assertEqual(len(self.store.get_turn_messages(turn)), before)
        self.assertIsNone(self.store.get_tool_result(turn, "a"))

    def test_provider_hidden_reasoning_is_neither_persisted_nor_exposed_to_history_tools(self):
        target = self.turn()
        native = call_message(("reason-safe-call", "context_read_history", {}))
        native["reasoning_content"] = "PRIVATE OPAQUE REASONING"
        native["analysis"] = {"text": "PRIVATE ANALYSIS"}
        native["tool_calls"][0]["reasoning_details"] = ["PRIVATE NESTED REASONING"]
        self.store.prepare_tool_calls(target, native)
        self.store.append_event(target, "model.completed", {"usage": {"tokens": 3}, "reasoning": "PRIVATE TRACE REASONING", "nested": {"thinking": "PRIVATE THINKING"}})
        self.store.complete_tool_call(target, "reason-safe-call", success({"value": "actual result"}))
        self.store.complete_turn(target, "generation_completed", "用户可见回答")
        self.store.record_delivery(target, "用户可见回答", "delta")
        self.store.record_delivery(target, "", "completed")
        self.assertEqual(native["reasoning_content"], "PRIVATE OPAQUE REASONING")
        self.assertEqual(native["analysis"]["text"], "PRIVATE ANALYSIS")
        history = self.store.get_history("session-a")
        self.assertEqual([m["role"] for m in history["messages"]], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(history["messages"][1]["tool_calls"][0]["id"], "reason-safe-call")
        persisted = " ".join(row[0] for row in self.store.db.execute("SELECT payload_json FROM ledger"))
        self.assertNotIn("PRIVATE", persisted)
        self.assertNotIn("reasoning_content", json.dumps(history))
        self.assertNotIn("analysis", json.dumps(history))
        self.store.close()
        on_disk = self.path.read_bytes()
        self.assertNotIn(b"PRIVATE", on_disk)
        self.store = RuntimeStore(self.path)

    def test_generated_but_unconsumed_reply_never_appears_as_spoken_history(self):
        old = self.turn()
        self.store.complete_turn(old, "generation_completed", "已发出一半但后半句仍在队列")
        self.assertEqual([m["role"] for m in self.store.get_history("session-a")["messages"]], ["user"])
        generated_seq = self.store.db.execute("SELECT seq FROM ledger WHERE turn_id=? AND role='assistant'", (old.turn_id,)).fetchone()[0]
        self.assertEqual(self.store.read_message("session-a", generated_seq)["content"], "")
        self.store.record_delivery(old, "已发出一半", "delta")
        partial = self.store.get_history("session-a")["messages"][-1]
        self.assertEqual(partial["content"], "已发出一半")
        self.assertEqual(partial["turn_status"], "generation_completed")
        self.assertEqual(partial["delivery_status"], "streamed_unconfirmed")
        newer = self.turn(2)
        self.store.record_delivery(old, "", "interrupted")
        history = self.store.get_history("session-a", exclude_turn_id=newer.turn_id)
        self.assertEqual(history["messages"][-1]["content"], "已发出一半")
        self.assertEqual(history["messages"][-1]["delivery_status"], "interrupted")
        raw = json.loads(self.store.db.execute("SELECT payload_json FROM ledger WHERE seq=?", (generated_seq,)).fetchone()[0])
        self.assertEqual(raw["content"], "已发出一半但后半句仍在队列")
        self.assertTrue(self.store.is_current(newer))


class ContextToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = RuntimeStore(":memory:")
        self.store.create_session("a", "run-a", "flash")
        self.store.create_session("b", "run-b", "pro")
        self.registry = ToolRegistry(self.store)

    async def asyncTearDown(self):
        self.store.close()

    async def test_model_can_read_search_and_update_only_its_session(self):
        old = self.store.admit_turn("a", 1, "预算3000，不要周末", "c1")
        self.store.complete_turn(old, "generation_completed", "收到条件")
        other = self.store.admit_turn("b", 1, "另一用户PRIVATE秘密", "other")
        current = self.store.admit_turn("a", 2, "继续", "c2")
        read = await self.registry.execute("context_read_history", {}, current)
        search = await self.registry.execute("context_search_history", {"query": "预算"}, current)
        self.assertEqual(read["status"], "success")
        self.assertEqual(search["data"]["messages"][0]["content"], "预算3000，不要周末")
        self.assertNotIn("PRIVATE", json.dumps(read, ensure_ascii=False))
        result = await self.registry.execute("context_read_history", {"session_id": "b"}, current)
        self.assertEqual(result["error"]["code"], "invalid_tool_arguments")
        changed = await self.registry.execute("context_update_notes", {"expected_version": 0, "text": "保留预算3000和否定周末"}, current)
        self.assertEqual(changed["status"], "success")
        self.assertEqual(self.store.get_notes("b")["text"], "")
        self.assertTrue(self.store.is_current(other))

    async def test_parameters_errors_and_stale_turns_have_stable_codes(self):
        turn = self.store.admit_turn("a", 1, "你好", "c1")
        for name, arguments in (("unregistered", {}), ("context_read_history", {"limit": True}), ("context_read_history", {"offset": 2}), ("context_search_history", {"query": ""}), ("context_update_notes", {"expected_version": True, "text": "x"})):
            result = await self.registry.execute(name, arguments, turn)
            self.assertEqual(result["status"], "error")
        self.store.admit_turn("a", 2, "新输入", "c2")
        stale = await self.registry.execute("context_update_notes", {"expected_version": 0, "text": "不可提交"}, turn)
        self.assertEqual(stale["status"], "cancelled")
        self.assertEqual(self.store.get_notes("a")["version"], 0)

    async def test_large_original_is_bounded_and_resumable_by_exact_record_reference(self):
        text = "原文" * 7000 + "否定：不允许周末。"
        old = self.store.admit_turn("a", 1, text, "c1")
        self.store.complete_turn(old, "generation_completed", "收到")
        current = self.store.admit_turn("a", 2, "旧条件是什么", "c2")
        result = await self.registry.execute("context_search_history", {"query": "不允许周末"}, current)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), MAX_RESULT_BYTES)
        record = result["data"]["records"][0]
        self.assertIn("不允许周末", record["content"])
        first = await self.registry.execute("context_read_history", {"record_seq": record["seq"], "offset": 0, "max_chars": 2000}, current)
        second = await self.registry.execute("context_read_history", {"record_seq": record["seq"], "offset": first["data"]["next_offset"], "max_chars": 2000}, current)
        self.assertEqual(first["data"]["content"] + second["data"]["content"], text[:4000])
        self.assertEqual([tool["function"]["name"] for tool in self.registry.schemas()], ["context_read_history", "context_search_history", "context_update_notes"])

    async def test_oversized_newest_turn_does_not_return_an_unrelated_older_fragment(self):
        first = self.store.admit_turn("a", 1, "较旧的小段", "c1")
        self.store.complete_turn(first, "generation_completed", "未发送草稿")
        large_text = "最近条件" * 4000
        large = self.store.admit_turn("a", 2, large_text, "c2")
        self.store.complete_turn(large, "generation_completed", "未发送草稿")
        current = self.store.admit_turn("a", 3, "请回查", "c3")
        result = await self.registry.execute("context_read_history", {}, current)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["data"]["records"][0]["turn_id"], large.turn_id)
        self.assertTrue(result["data"]["records"][0]["content"].startswith("最近条件"))
        self.assertEqual(self.store.search_history("a", "role")["messages"], [])


if __name__ == "__main__":
    unittest.main()
