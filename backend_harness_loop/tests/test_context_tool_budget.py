"""Real SQLite history pages fit a caller's current UTF-8 result allocation."""

import json
import unittest

from backend_harness_loop.store import RuntimeStore, StoreError
from backend_harness_loop.tools import MAX_RESULT_BYTES, ToolRegistry


def byte_count(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


class ContextToolBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = RuntimeStore(":memory:")
        self.store.create_session("session", "run", "flash")
        self.tools = ToolRegistry(self.store)
        self.serial = 0

    async def asyncTearDown(self):
        self.store.close()

    def turn(self, text):
        self.serial += 1
        return self.store.admit_turn("session", str(self.serial), text, "c" + str(self.serial))

    def history(self, text, *, native=False):
        target = self.turn(text)
        if native:
            call_id = "source-tool-" + str(self.serial)
            self.store.prepare_tool_calls(target, {"role": "assistant", "content": None, "tool_calls": [{
                "id": call_id, "type": "function", "function": {"name": "context_search_history", "arguments": '{"query":"old"}'}}]})
            self.store.mark_tool_dispatched(target, call_id)
            self.store.complete_tool_call(target, call_id, {"status": "success", "data": {"evidence": "closed source"}, "error": None, "truncated": False})
        self.store.record_delivery(target, "收到")
        self.store.complete_turn(target, "generation_completed", "收到")
        self.store.record_delivery(target, "", "completed")
        return target

    def user_seq(self, target):
        return self.store.db.execute("SELECT seq FROM ledger WHERE turn_id=? AND role='user'", (target.turn_id,)).fetchone()[0]

    async def test_direct_chinese_emoji_record_pages_are_exact_and_utf8_bounded(self):
        text = "课程😀预算🧪" * 320 + "原始限制：不能周五。"
        old = self.history(text)
        current = self.turn("回查原文")
        reference, offset, chunks = self.user_seq(old), 0, []
        for _ in range(100):
            result = await self.tools.execute("context_read_history", {"record_seq": reference, "offset": offset, "max_chars": 2000},
                                              current, result_budget_bytes=1024)
            self.assertEqual(result["status"], "success")
            self.assertLessEqual(byte_count(result), 1024)
            record = result["data"]
            self.assertEqual(record["reference"], "seq:" + str(reference))
            self.assertEqual(record["content"], text[offset:offset + len(record["content"])])
            chunks.append(record["content"])
            if record["next_offset"] is None:
                self.assertFalse(record["truncated"])
                break
            self.assertTrue(record["truncated"])
            self.assertGreater(record["next_offset"], offset)
            self.assertEqual(record["next_offset"], offset + len(record["content"]))
            offset = record["next_offset"]
        else:
            self.fail("bounded record did not make progress")
        self.assertEqual("".join(chunks), text)
        self.assertEqual(self.store.read_message("session", reference, 0, 2000)["content"], text[:2000])

    async def test_complete_turn_pages_keep_native_call_result_pairs_and_older_cursor(self):
        old = [self.history("预算条件第" + str(index) + "轮，" + "x" * 80, native=True) for index in range(6)]
        current = self.turn("读取历史")
        cursor, observed, pages = None, set(), 0
        while True:
            args = {"limit": 10}
            if cursor is not None:
                args["before_seq"] = cursor
            result = await self.tools.execute("context_read_history", args, current, result_budget_bytes=2500)
            self.assertLessEqual(byte_count(result), 2500)
            if result["status"] == "no_results":
                break
            self.assertEqual(result["status"], "success")
            self.assertFalse(result["data"].get("records"))
            groups = {}
            for message in result["data"]["messages"]:
                groups.setdefault(message["turn_id"], []).append(message)
            for turn_id, messages in groups.items():
                self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool", "assistant"])
                calls = {call["id"] for m in messages for call in m.get("tool_calls", [])}
                results = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
                self.assertEqual(calls, results)
                self.assertNotIn(turn_id, observed)
                observed.add(turn_id)
            pages += 1
            if not result["truncated"]:
                break
            next_cursor = result["data"]["next_before_seq"]
            self.assertIsInstance(next_cursor, int)
            if cursor is not None:
                self.assertLess(next_cursor, cursor)
            cursor = next_cursor
            self.assertLess(pages, 10)
        self.assertGreater(pages, 1)
        self.assertEqual(observed, {target.turn_id for target in old})

    async def test_small_search_fragment_contains_hit_and_does_not_skip_older_matches(self):
        query = "只允许工作日"
        older = self.history("旧方案：" + query)
        text = "ß填😀" * 1200 + query + "，预算3900元。"
        newer = self.history(text)
        current = self.turn("查原文")
        result = await self.tools.execute("context_search_history", {"query": query, "limit": 5}, current, result_budget_bytes=1024)
        self.assertEqual(result["status"], "success")
        self.assertLessEqual(byte_count(result), 1024)
        self.assertTrue(result["truncated"])
        data, record = result["data"], result["data"]["records"][0]
        self.assertEqual(record["turn_id"], newer.turn_id)
        self.assertIn(query, record["content"])
        self.assertLessEqual(record["offset"], text.find(query))
        self.assertEqual(data["source_refs"], [record["reference"]])
        self.assertEqual(data["next_offset"], record["next_offset"])
        self.assertEqual(data["record_seq"], self.user_seq(newer))
        self.assertEqual(data["next_before_seq"], self.user_seq(newer))
        next_page = await self.tools.execute("context_search_history", {"query": query, "before_seq": data["next_before_seq"]},
                                            current, result_budget_bytes=1024)
        self.assertEqual(next_page["status"], "success")
        self.assertTrue(all(message["turn_id"] == older.turn_id for message in next_page["data"]["messages"]))

    async def test_budget_shortage_is_explicit_error_not_no_results_or_partial_json(self):
        self.history("真实存在的记录" * 100)
        current = self.turn("查历史")
        result = await self.tools.execute("context_read_history", {}, current, result_budget_bytes=128)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "context_result_budget_exhausted")
        self.assertLessEqual(byte_count(result), 128)
        with self.assertRaisesRegex(StoreError, "context_result_budget_exhausted"):
            await self.tools.execute("context_read_history", {}, current, result_budget_bytes=1)
        with self.assertRaisesRegex(StoreError, "invalid_tool_result_budget"):
            await self.tools.execute("context_read_history", {}, current, result_budget_bytes=MAX_RESULT_BYTES + 1)

    async def test_notes_success_payload_is_checked_before_atomic_write(self):
        current = self.turn("请保存")
        result = await self.tools.execute("context_update_notes", {"expected_version": 0, "text": "长中文笔记😀" * 400},
                                          current, result_budget_bytes=512)
        self.assertEqual(result["error"]["code"], "context_result_budget_exhausted")
        self.assertLessEqual(byte_count(result), 512)
        self.assertEqual(self.store.get_notes("session"), {"version": 0, "text": ""})
        self.assertEqual(self.store.get_context("session")["context_version"], 0)

    async def test_default_budget_preserves_requested_record_slice_and_true_no_results(self):
        current = self.turn("尚无历史")
        empty = await self.tools.execute("context_read_history", {}, current)
        self.assertEqual(empty["status"], "no_results")
        self.store.complete_turn(current, "generation_completed", "")
        text = "😀" * 2500
        old = self.history(text)
        current = self.turn("查旧记录")
        result = await self.tools.execute("context_read_history", {"record_seq": self.user_seq(old), "max_chars": 2000}, current)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["content"], text[:2000])
        self.assertEqual(result["data"]["next_offset"], 2000)
        self.assertLessEqual(byte_count(result), MAX_RESULT_BYTES)

