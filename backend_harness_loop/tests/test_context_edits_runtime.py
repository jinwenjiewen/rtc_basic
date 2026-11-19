"""Exercise model-selected edits with real controller/native tool persistence."""

import asyncio
from copy import deepcopy
import json
from dataclasses import replace
import unittest

from backend_harness_loop.context import ContextPolicy
from backend_harness_loop.tests import test_context_runtime as helpers


def state(messages):
    return json.loads(next(m["content"].split("：", 1)[1] for m in messages
                           if m["content"] and m["content"].startswith("上下文状态（由Harness提供）：")))


class EditModel(helpers.Model):
    def __init__(self, actions, **options):
        super().__init__(**options)
        self.actions = list(actions)

    async def complete(self, messages, tools=None, **options):
        self.requests.append({"phase": "decision", "profile": self.profile.profile_id,
                              "messages": deepcopy(messages), "tools": deepcopy(tools), **options})
        self.entered.set()
        await self._wait()
        metadata = state(messages)
        action = self.actions.pop(0) if self.actions else "prepare"
        if action in {"update", "pair", "bad_reference"}:
            args = {"expected_context_version": metadata["context_version"], "upserts": [{"section": "items", "id": "budget",
                "kind": "constraint", "value": 3000, "source_type": "user_statement", "source_refs": metadata["current_input_refs"], "status": "active"}]}
            if action == "bad_reference":
                args["upserts"][0]["source_refs"] = ["seq:99999999"]
            result = helpers.response("update-" + str(len(self.requests)), "context_update", args)
            if action == "pair":
                selected = helpers.response("select-same-version", "context_select", {"expected_context_version": metadata["context_version"],
                    "keep_refs": metadata["current_input_refs"], "release_refs": [], "purpose": "下一轮核对"})
                result.tool_calls.extend(selected.tool_calls)
                result.assistant_message["tool_calls"] = result.tool_calls
            return result
        if action == "select":
            return helpers.response("select-" + str(len(self.requests)), "context_select", {"expected_context_version": metadata["context_version"],
                "keep_refs": metadata["current_input_refs"], "release_refs": [], "purpose": "下一轮核对"})
        if action == "release":
            return helpers.response("release-" + str(len(self.requests)), "context_select", {"expected_context_version": metadata["context_version"],
                "keep_refs": [], "release_refs": metadata["selected_refs"], "purpose": "已核对"})
        return helpers.response("prepare-" + str(len(self.requests)), "prepare_response", {"mode": "answer", "evidence_refs": []})


class ContextEditRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = helpers.ContextRuntimeTests.asyncSetUp
    asyncTearDown = helpers.ContextRuntimeTests.asyncTearDown
    runtime = helpers.ContextRuntimeTests.runtime
    collect = helpers.ContextRuntimeTests.collect
    gate = helpers.ContextRuntimeTests.gate
    events = helpers.ContextRuntimeTests.events

    async def test_update_select_and_final_share_profile_steps_and_closed_native_frames(self):
        model = EditModel(["update", "select", "prepare"])
        runtime, _ = self.runtime(model, selected="pro", context_policy=ContextPolicy(), background_context_enabled=False)
        handle = await runtime.accept("run", "edit", "预算3000，周末不要，订单未提交")
        self.assertEqual(await self.collect(runtime, handle), ["FINAL-ANSWER"])
        self.assertEqual([r["phase"] for r in model.requests], ["decision"] * 3 + ["final"])
        self.assertEqual({r["profile"] for r in model.requests}, {"pro"})
        context = self.store.get_context("session")
        self.assertEqual(context["context_version"], 2)
        self.assertEqual(context["compacted_through_seq"], 0)
        self.assertEqual(context["summary"]["items"][0]["value"], 3000)
        self.assertEqual(context["selected_refs"], [self.store.user_reference(handle.target, handle.user_text)])
        final = model.requests[-1]["messages"]
        calls = [call for m in final for call in m.get("tool_calls", [])]
        results = {m["tool_call_id"]: json.loads(m["content"]) for m in final if m["role"] == "tool"}
        self.assertEqual(set(results), {call["id"] for call in calls})
        self.assertTrue(all(results[call["id"]]["status"] == "success" for call in calls))
        self.assertEqual(self.store.snapshot("session")["context_edit_count"], 2)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)
        self.assertEqual(self.store.get_notes("session")["version"], 0)

    async def test_two_writes_in_same_decision_cannot_reuse_frozen_version(self):
        model = EditModel(["pair", "prepare"])
        runtime, _ = self.runtime(model, context_policy=ContextPolicy(), background_context_enabled=False)
        handle = await runtime.accept("run", "pair", "预算3000")
        await self.collect(runtime, handle)
        conflict = self.store.get_tool_result(handle.target, "select-same-version")
        self.assertEqual(conflict["status"], "error")
        self.assertEqual(conflict["error"]["code"], "context_version_conflict")
        self.assertEqual(conflict["data"]["current_context_version"], 1)
        self.assertEqual(self.store.snapshot("session")["context_edit_count"], 1)
        self.assertEqual(self.store.get_context("session")["selected_refs"], [])

    async def test_releasing_previous_selection_preserves_ledger_and_raw_tail(self):
        first, second = EditModel(["select", "prepare"]), EditModel(["release", "prepare"])
        runtime, _ = self.runtime(first, second, context_policy=ContextPolicy(), background_context_enabled=False)
        old = await runtime.accept("run", "keep", "精确合成条件")
        await self.collect(runtime, old)
        reference = self.store.get_context("session")["selected_refs"][0]
        new = await runtime.accept("run", "release", "已核对，取消显式保留")
        await self.collect(runtime, new)
        self.assertEqual(self.store.get_context("session")["selected_refs"], [])
        self.assertIn("精确合成条件", json.dumps(second.requests[-1]["messages"], ensure_ascii=False))
        self.assertEqual(self.store.read_message("session", int(reference[4:]))["content"], "精确合成条件")

    async def test_cancel_ignoring_late_model_cannot_apply_old_edit(self):
        gate = self.gate()
        old = EditModel(["update"], gate=gate, ignore_cancel=True)
        fresh = EditModel(["update", "prepare"])
        runtime, _ = self.runtime(old, fresh, context_policy=ContextPolicy(), background_context_enabled=False)
        pending = await runtime.accept("run", "old", "旧条件")
        await old.entered.wait()
        replacement = await runtime.accept("run", "new", "预算3000")
        await self.collect(runtime, replacement)
        gate.set()
        await asyncio.gather(pending.task, return_exceptions=True)
        self.assertFalse(pending.valid)
        self.assertEqual(self.store.snapshot("session")["context_edit_count"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM context_edits WHERE owner_turn_id=?", (pending.target.turn_id,)).fetchone()[0], 0)

    async def test_tool_budget_counts_context_writes_and_still_reserves_final(self):
        model = EditModel(["update", "select"])
        runtime, _ = self.runtime(model, max_tool_calls=1, context_policy=ContextPolicy(), background_context_enabled=False)
        handle = await runtime.accept("run", "bounded", "预算3000")
        await self.collect(runtime, handle)
        self.assertEqual([r["phase"] for r in model.requests], ["decision", "final"])
        self.assertEqual(self.store.snapshot("session")["context_edit_count"], 1)

    async def test_source_rejection_supplies_bounded_current_references_for_model_repair(self):
        model = EditModel(["bad_reference", "update", "prepare"])
        runtime, _ = self.runtime(model, context_policy=ContextPolicy(), background_context_enabled=False)
        handle = await runtime.accept("run", "repair", "预算3000")
        await self.collect(runtime, handle)
        result = self.store.get_tool_result(handle.target, "update-1")
        self.assertEqual(result["error"]["code"], "context_reference_not_visible")
        self.assertEqual(result["data"]["current_input_refs"], [self.store.user_reference(handle.target, handle.user_text)])
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 1024)
        self.assertEqual(self.store.snapshot("session")["context_edit_count"], 1)
        self.assertEqual(len(model.requests), 4)

    async def test_required_decision_mode_keeps_model_tool_choice_and_final_tools_disabled(self):
        self.profiles._profiles["pro"] = replace(self.profiles.get("pro"), decision_tool_choice="required")
        model = EditModel(["update", "select", "prepare"])
        runtime, _ = self.runtime(model, selected="pro", context_policy=ContextPolicy(), background_context_enabled=False)
        handle = await runtime.accept("run", "required", "预算3000")
        await self.collect(runtime, handle)
        self.assertTrue(all(request["tool_choice"] == "required" for request in model.requests if request["phase"] == "decision"))
        self.assertEqual(runtime._fields(runtime.binding("run"), tool_choice="none")["tool_choice"], "none")
        self.assertNotIn("tools", model.requests[-1])
        self.assertEqual(self.store.snapshot("session")["context_edit_count"], 2)

    async def test_required_mode_rejects_provider_text_instead_of_acknowledging_unperformed_action(self):
        self.profiles._profiles["flash"] = replace(self.profiles.get("flash"), decision_tool_choice="required")
        class NoAction(helpers.Model):
            async def complete(self, *args, **kwargs):
                return helpers.ModelResponse(content="收到", assistant_message={"role": "assistant", "content": "收到"})
        runtime, _ = self.runtime(NoAction(), context_policy=ContextPolicy(), background_context_enabled=False)
        handle = await runtime.accept("run", "no-action", "请修改摘要")
        await self.collect(runtime, handle)
        self.assertEqual(handle.failure_code, "model_action_required")
        self.assertEqual(self.store.snapshot("session")["context_edit_count"], 0)


if __name__ == "__main__":
    unittest.main()
