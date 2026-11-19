"""M1 loop integration: real controller/store/tools, scripted models, no network."""

import asyncio
from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from backend_harness_loop.models import ModelProfiles, ModelProtocolError, ModelResponse
from backend_harness_loop.runtime import AgentRuntime, RuntimeFault
from backend_harness_loop.store import DuplicateTurn, RoundCancelled, RuntimeStore, StaleTurn


def call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False) if not isinstance(arguments, str) else arguments}}


def decision(*calls, content=""):
    native = {"role": "assistant", "content": content or None}
    if calls:
        native["tool_calls"] = list(calls)
    return ModelResponse(content=content, tool_calls=list(calls), assistant_message=native,
                         finish_reason="tool_calls" if calls else "stop", usage={"total_tokens": 7})


def prepare(identifier="prepare_1"):
    return decision(call(identifier, "prepare_response", {"mode": "answer", "evidence_refs": []}))


class FakeModel:
    def __init__(self, decisions=None, chunks=("最终答复",), *, decision_action=None, final_action=None, final_error=None):
        self.decisions = deque(decisions if decisions is not None else [prepare()])
        self.chunks = chunks
        self.decision_action = decision_action
        self.final_action = final_action
        self.final_error = final_error
        self.requests = []
        self.entered = asyncio.Event()
        self.final_entered = asyncio.Event()
        self.closed = asyncio.Event()
        self.stream_closed = asyncio.Event()
        self.profile = None

    async def complete(self, messages, **kwargs):
        self.requests.append({"phase": "decision", "profile_id": self.profile.profile_id, "messages": deepcopy(messages), **deepcopy(kwargs)})
        self.entered.set()
        if self.decision_action is not None:
            return await self.decision_action(messages)
        if not self.decisions:
            raise AssertionError("Unexpected extra model decision call")
        return self.decisions.popleft()

    async def stream_final(self, messages, max_tokens=None, on_usage=None):
        self.requests.append({"phase": "final", "profile_id": self.profile.profile_id, "messages": deepcopy(messages)})
        self.final_entered.set()
        try:
            if self.final_action is not None:
                await self.final_action()
            for text in self.chunks:
                yield text
            if self.final_error is not None:
                raise self.final_error
            if on_usage is not None:
                on_usage({"total_tokens": 11})
        finally:
            self.stream_closed.set()

    async def aclose(self):
        self.closed.set()


class Factory:
    def __init__(self, *models):
        self.pending = deque(models)
        self.created = []

    def __call__(self, profile):
        model = self.pending.popleft()
        model.profile = profile
        self.created.append(model)
        return model


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Path(self.folder.name) / "runtime.sqlite3"
        self.store = RuntimeStore(self.database)
        profiles = Path(self.folder.name) / "profiles.json"
        profiles.write_text(json.dumps({
            "schema_version": 1, "default_profile": "flash",
            "providers": {"fake": {"adapter": "openai_chat", "base_url": "https://fixture.example/v1", "api_key_env": "FIXTURE_KEY"}},
            "profiles": {name: {"provider": "fake", "model": model} for name, model in (("flash", "deepseek-v4-flash"), ("pro", "deepseek-v4-pro"))},
        }), encoding="utf-8")
        self.profiles = ModelProfiles.load(profiles, environ={"FIXTURE_KEY": "fixture-key"})
        self.runtimes = []
        self.release_on_cleanup = []

    async def asyncTearDown(self):
        for event in self.release_on_cleanup:
            event.set()
        for runtime in self.runtimes:
            await runtime.aclose()
        self.store.close()
        self.folder.cleanup()

    def runtime(self, *models, selected="flash", **options):
        factory = Factory(*models)
        runtime = AgentRuntime(self.store, self.profiles, model_factory=factory, **options)
        runtime.register_session("session", "run", selected, "task", "user")
        self.runtimes.append(runtime)
        return runtime, factory

    def gate(self):
        event = asyncio.Event()
        self.release_on_cleanup.append(event)
        return event

    async def collect(self, runtime, handle):
        output = [text async for text in runtime.speech(handle)]
        await asyncio.wait_for(asyncio.shield(handle.task), 1)
        return output

    def events(self, handle):
        return [(row["kind"], json.loads(row["payload_json"])) for row in self.store.db.execute("SELECT kind,payload_json FROM ledger WHERE turn_id=? ORDER BY seq", (handle.target.turn_id,))]

    async def test_real_context_tool_prepare_and_final_use_one_selected_profile(self):
        model = FakeModel([decision(call("read_1", "context_read_history", {"limit": 4}), content="PRIVATE INTERNAL DRAFT"), prepare()], chunks=("确认", "你的条件"))
        runtime, factory = self.runtime(model, selected="pro")
        seed = self.store.admit_turn("session", "seed", "预算是一千二", "seed_connection")
        self.store.complete_turn(seed, "generation_completed", "已记录")
        handle = await runtime.accept("run", "round1", "继续刚才的条件")
        self.assertEqual(await self.collect(runtime, handle), ["确认", "你的条件"])
        self.assertEqual([request["phase"] for request in model.requests], ["decision", "decision", "final"])
        self.assertEqual({request["profile_id"] for request in model.requests}, {"pro"})
        native = self.store.get_turn_messages(handle.target)
        self.assertEqual([item["tool_call_id"] for item in native if item["role"] == "tool"], ["read_1", "prepare_1"])
        returned = json.loads(next(item["content"] for item in native if item.get("tool_call_id") == "read_1"))
        self.assertIn("预算是一千二", json.dumps(returned, ensure_ascii=False))
        self.assertEqual(returned["status"], "success")
        self.assertTrue(model.closed.is_set())
        self.assertEqual(len(factory.created), 1)

    async def test_plain_decision_draft_is_neither_speech_nor_history(self):
        model = FakeModel([decision(content="PRIVATE PLANNING DRAFT")], chunks=("实际回答",))
        runtime, _ = self.runtime(model)
        handle = await runtime.accept("run", "round1", "你好")
        self.assertEqual(await self.collect(runtime, handle), ["实际回答"])
        self.assertNotIn("PRIVATE PLANNING DRAFT", json.dumps(self.store.get_turn_messages(handle.target)))
        self.assertIn("decision.draft", [kind for kind, _ in self.events(handle)])
        self.assertEqual(len(model.requests), 2)

    async def test_context_notes_update_and_tool_result_are_closed_once(self):
        model = FakeModel([decision(call("notes_1", "context_update_notes", {"expected_version": 0, "text": "用户预算1200，周末可用"})), prepare()])
        runtime, _ = self.runtime(model)
        handle = await runtime.accept("run", "round1", "预算1200，周末有空")
        await self.collect(runtime, handle)
        self.assertEqual(self.store.get_notes("session"), {"version": 1, "text": "用户预算1200，周末可用"})
        rows = [item for item in self.store.get_turn_messages(handle.target) if item.get("tool_call_id") == "notes_1"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["content"])["status"], "success")
        self.assertEqual(self.store.get_tool_result(handle.target, "notes_1")["data"]["version"], 1)

    async def test_step_budget_always_reserves_final_generation(self):
        model = FakeModel([decision(call("read_1", "context_read_history", {}))])
        runtime, _ = self.runtime(model, max_steps=2)
        handle = await runtime.accept("run", "round1", "回查历史")
        await self.collect(runtime, handle)
        self.assertEqual([request["phase"] for request in model.requests], ["decision", "final"])
        self.assertEqual(self.store.get_tool_result(handle.target, "read_1")["status"], "no_results")

    async def test_tool_budget_closes_rejected_native_calls_without_dispatching_them(self):
        model = FakeModel([decision(call("read_1", "context_read_history", {}), call("read_2", "context_read_history", {}))])
        runtime, _ = self.runtime(model, max_tool_calls=1)
        original = runtime.tools.execute
        dispatched = []
        async def spy(name, args, target, **kwargs):
            dispatched.append(kwargs["tool_call_id"])
            return await original(name, args, target, **kwargs)
        runtime.tools.execute = spy
        handle = await runtime.accept("run", "round1", "查看上下文")
        await self.collect(runtime, handle)
        self.assertEqual(dispatched, ["read_1"])
        self.assertEqual(self.store.get_tool_result(handle.target, "read_2")["error"], "tool_budget_exhausted")
        self.assertEqual(model.requests[-1]["phase"], "final")

    async def test_same_text_new_round_is_new_input_but_callback_retries_never_replay(self):
        runtime, factory = self.runtime(FakeModel(), FakeModel())
        first = await runtime.accept("run", "round1", "同样的话")
        await self.collect(runtime, first)
        with self.assertRaises(DuplicateTurn):
            await runtime.accept("run", "round1", "同样的话")
        second = await runtime.accept("run", "round2", "同样的话")
        await self.collect(runtime, second)
        self.assertNotEqual(first.target.turn_id, second.target.turn_id)
        self.assertGreater(second.target.epoch, first.target.epoch)
        self.assertEqual(len(factory.created), 2)

    async def test_interrupt_returns_while_cancelled_model_cleanup_is_still_pending(self):
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), self.gate()
        async def blocked(messages):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return prepare()
        model = FakeModel(decision_action=blocked)
        runtime, _ = self.runtime(model)
        handle = await runtime.accept("run", "round1", "需要很久的请求")
        await asyncio.wait_for(entered.wait(), 1)
        self.assertTrue(await asyncio.wait_for(runtime.interrupt("run", "round1"), 1))
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertFalse(handle.valid)
        self.assertFalse(handle.task.done())
        self.assertIn(handle.task, runtime.tasks)
        self.assertEqual(await self.collect_cancelled(runtime, handle), [])
        release.set()
        await asyncio.wait_for(model.closed.wait(), 1)
        self.assertEqual(self.store.lookup_round("session", "round1")["status"], "cancelled")

    async def collect_cancelled(self, runtime, handle):
        return [text async for text in runtime.speech(handle)]

    async def test_cancelled_cleanup_still_occupies_task_capacity(self):
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), self.gate()
        async def blocked(messages):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return prepare()
        first_model, second_model = FakeModel(decision_action=blocked), FakeModel()
        runtime, factory = self.runtime(first_model, second_model, max_live_tasks=1)
        first = await runtime.accept("run", "round1", "第一个")
        await asyncio.wait_for(entered.wait(), 1)
        await runtime.interrupt("run", "round1")
        await asyncio.wait_for(cancelled.wait(), 1)
        with self.assertRaisesRegex(RuntimeFault, "model_task_capacity"):
            await runtime.accept("run", "round2", "第二个")
        self.assertEqual(len(factory.created), 1)
        release.set()
        await asyncio.wait_for(asyncio.shield(first.task), 1)
        second = await runtime.accept("run", "round2", "第二个")
        await self.collect(runtime, second)

    async def test_old_connection_or_round_interrupt_cannot_cancel_new_target(self):
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), self.gate()
        async def old_decision(messages):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return prepare()
        final_gate = self.gate()
        new_model = FakeModel(chunks=("新回答",), final_action=final_gate.wait)
        runtime, _ = self.runtime(FakeModel(decision_action=old_decision), new_model)
        old = await runtime.accept("run", "old_round", "旧条件", connection_id="old_connection")
        await asyncio.wait_for(entered.wait(), 1)
        current = await runtime.accept("run", "new_round", "新条件", connection_id="new_connection")
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(new_model.final_entered.wait(), 1)
        await runtime.disconnect(old)
        await runtime.interrupt("run", "old_round")
        self.assertTrue(current.valid)
        self.assertTrue(self.store.is_current(current.target))
        release.set(); final_gate.set()
        self.assertEqual(await self.collect(runtime, current), ["新回答"])
        await asyncio.wait_for(asyncio.shield(old.task), 1)

    async def test_consumer_disconnect_cancels_model_and_marks_delivery_unknown(self):
        after_first, release = asyncio.Event(), self.gate()
        model = FakeModel()
        async def streaming(messages, max_tokens=None, on_usage=None):
            model.final_entered.set()
            try:
                yield "首段"
                after_first.set()
                await release.wait()
                yield "不应再送达"
            finally:
                model.stream_closed.set()
        model.stream_final = streaming
        runtime, _ = self.runtime(model)
        handle = await runtime.accept("run", "round1", "开始回答")
        speech = runtime.speech(handle)
        self.assertEqual(await anext(speech), "首段")
        await asyncio.wait_for(after_first.wait(), 1)
        await speech.aclose()
        await asyncio.wait_for(model.closed.wait(), 1)
        self.assertFalse(handle.valid)
        row = self.store.db.execute("SELECT state,delivery_text,delivery_status FROM turns WHERE turn_id=?", (handle.target.turn_id,)).fetchone()
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(row["delivery_text"], "首段")
        self.assertEqual(row["delivery_status"], "unknown")
        self.assertTrue(model.stream_closed.is_set())

    async def test_late_cancelled_tool_cannot_write_notes_or_pollute_new_reply(self):
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), self.gate()
        old_model = FakeModel([decision(call("notes_old", "context_update_notes", {"expected_version": 0, "text": "过期笔记"}))])
        new_model = FakeModel(chunks=("只采用新条件",))
        runtime, _ = self.runtime(old_model, new_model)
        original = runtime.tools.execute
        async def slow(name, args, target, **kwargs):
            if kwargs.get("tool_call_id") == "notes_old":
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
            return await original(name, args, target, **kwargs)
        runtime.tools.execute = slow
        old = await runtime.accept("run", "old", "写旧笔记")
        await asyncio.wait_for(entered.wait(), 1)
        current = await runtime.accept("run", "new", "改用新条件")
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertEqual(await self.collect(runtime, current), ["只采用新条件"])
        release.set()
        await asyncio.wait_for(asyncio.shield(old.task), 1)
        self.assertEqual(self.store.get_notes("session"), {"version": 0, "text": ""})
        self.assertEqual(self.store.get_tool_result(old.target, "notes_old")["status"], "cancelled")
        self.assertNotIn("过期笔记", new_model.requests[0]["messages"][0]["content"])

    async def test_tool_timeout_is_native_observation_then_model_can_prepare_final(self):
        entered, release = asyncio.Event(), self.gate()
        model = FakeModel([decision(call("slow_read", "context_read_history", {})), prepare()])
        runtime, _ = self.runtime(model, tool_seconds=.01)
        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
        runtime.tools.execute = blocked
        handle = await runtime.accept("run", "round1", "读取历史")
        self.assertEqual(await self.collect(runtime, handle), ["最终答复"])
        self.assertTrue(entered.is_set())
        observed = next(item for item in model.requests[1]["messages"] if item.get("tool_call_id") == "slow_read")
        self.assertEqual(json.loads(observed["content"])["status"], "timeout")
        self.assertEqual(self.store.get_tool_result(handle.target, "slow_read")["status"], "timeout")

    async def test_failure_fallback_does_not_disclose_exception_or_private_provider_data(self):
        async def failed(messages):
            raise RuntimeError("PRIVATE API KEY AND PROVIDER BODY")
        runtime, _ = self.runtime(FakeModel(decision_action=failed))
        handle = await runtime.accept("run", "round1", "你好")
        output = await self.collect(runtime, handle)
        self.assertTrue("".join(output).strip())
        self.assertNotIn("PRIVATE", "".join(output))
        self.assertEqual(handle.failure_code, "agent_execution_failed")
        self.assertNotIn("PRIVATE API", json.dumps(self.events(handle), ensure_ascii=False))
        self.assertEqual(self.store.lookup_round("session", "round1")["status"], "failed")

    async def test_context_capacity_fails_explicitly_without_silent_history_loss_or_model_call(self):
        model = FakeModel()
        runtime, _ = self.runtime(model, max_context_chars=100)
        handle = await runtime.accept("run", "round1", "请保留这条原始信息")
        output = await self.collect(runtime, handle)
        self.assertEqual(handle.failure_code, "context_protected_input_too_large")
        self.assertIn("原始记录已保留", "".join(output))
        self.assertEqual(model.requests, [])
        self.assertTrue(any(message.get("content") == "请保留这条原始信息" for message in self.store.get_history("session")["messages"]))

    async def test_first_output_deadline_cancels_blocked_model_and_speaks_only_fallback(self):
        release = self.gate()
        async def blocked(messages):
            await release.wait()
            return prepare()
        model = FakeModel(decision_action=blocked)
        runtime, _ = self.runtime(model, first_output_ms=500, wall_seconds=1)
        handle = await runtime.accept("run", "round1", "等待超时")
        output = await asyncio.wait_for(self.collect(runtime, handle), 2)
        self.assertTrue(output)
        self.assertEqual(handle.failure_code, "first_output_or_turn_timeout")
        self.assertTrue(model.closed.is_set())

    async def test_pre_admission_interrupt_tombstone_blocks_late_callback(self):
        runtime, factory = self.runtime(FakeModel())
        await runtime.interrupt("run", "round_delayed")
        with self.assertRaises(RoundCancelled):
            await runtime.accept("run", "round_delayed", "迟到回调")
        self.assertEqual(factory.created, [])

    async def test_restart_marks_inflight_work_failed_and_never_replays_saved_input(self):
        # Simulate durable state left by a dead worker, not a second live worker.
        self.store.create_session("session", "run", "flash")
        target = self.store.admit_turn("session", "old_round", "崩溃前输入", "dead_connection")
        self.store.prepare_tool_calls(target, decision(call("read_old", "context_read_history", {})).assistant_message)
        self.store.close()
        self.store = RuntimeStore(self.database)
        model = FakeModel(chunks=("新轮回答",))
        runtime, factory = self.runtime(model)
        self.assertEqual(self.store.lookup_round("session", "old_round")["status"], "failed")
        self.assertEqual(factory.created, [])
        self.assertEqual(len(runtime.tasks), 0)
        with self.assertRaises(DuplicateTurn):
            await runtime.accept("run", "old_round", "崩溃前输入")
        self.assertEqual(factory.created, [])
        handle = await runtime.accept("run", "new_round", "重新提问")
        self.assertEqual(await self.collect(runtime, handle), ["新轮回答"])

    async def test_cancelled_admission_waiter_reclaims_only_its_queued_output_connection(self):
        release = self.gate()
        model = FakeModel(decision_action=lambda messages: self.blocked_decision(release))
        runtime, factory = self.runtime(model)
        controller = runtime.binding("run")
        # Hold the event consumer before admission, then cancel the HTTP waiter
        # after its exact request is in the mailbox. No scheduling sleeps needed.
        held = asyncio.create_task(release.wait())
        controller.runner = held
        queued = asyncio.Event()
        original = controller.inbox.put_nowait
        def record_enqueue(item):
            original(item)
            if item[2] == "admit":
                queued.set()
        controller.inbox.put_nowait = record_enqueue
        waiter = asyncio.create_task(runtime.accept("run", "abandoned", "已断开的请求"))
        await asyncio.wait_for(queued.wait(), 1)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        held.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await held
        controller.runner = asyncio.create_task(controller.run())
        await asyncio.wait_for(asyncio.gather(*list(runtime.control_tasks)), 1)
        if runtime.tasks:
            await asyncio.wait_for(asyncio.gather(*list(runtime.tasks), return_exceptions=True), 1)
        self.assertEqual(self.store.lookup_round("session", "abandoned")["status"], "cancelled")
        self.assertFalse(controller.current.valid)
        self.assertLessEqual(len(factory.created), 1)

    async def blocked_decision(self, release):
        await release.wait()
        return prepare()

    async def test_full_output_queue_does_not_keep_failed_producer_alive(self):
        model = FakeModel(chunks=("未被消费",) * 32, final_error=ModelProtocolError("fixture_stream_failure"))
        runtime, _ = self.runtime(model)
        handle = await runtime.accept("run", "round1", "模拟RTC背压")
        # Producer reaches a terminal error with all output slots occupied.
        # It must close without waiting for a consumer that may never return.
        await asyncio.wait_for(asyncio.shield(handle.task), 1)
        self.assertTrue(model.closed.is_set())
        self.assertEqual(handle.failure_code, "fixture_stream_failure")
        self.assertEqual(self.store.lookup_round("session", "round1")["status"], "failed")
        output = await self.collect(runtime, handle)
        self.assertTrue(output)
        self.assertNotIn("未被消费", "".join(output))

    async def test_generated_but_unconsumed_text_is_not_replayed_as_spoken_history(self):
        first_model, next_model = FakeModel(chunks=("尚未送给用户的答复",)), FakeModel()
        runtime, _ = self.runtime(first_model, next_model)
        prior = await runtime.accept("run", "round1", "第一个问题")
        await asyncio.wait_for(asyncio.shield(prior.task), 1)
        self.assertEqual(prior.yielded_text, "")
        current = await runtime.accept("run", "round2", "第二个问题")
        await self.collect(runtime, current)
        messages = next_model.requests[0]["messages"]
        self.assertNotIn("尚未送给用户的答复", json.dumps(messages, ensure_ascii=False))
        self.assertIn("尚未送给用户的答复", json.dumps(self.store.get_turn_messages(prior.target), ensure_ascii=False))

    async def test_partial_delivery_history_uses_only_yielded_prefix_and_marks_uncertainty(self):
        first_model, next_model = FakeModel(chunks=("已发送片段", "未发送后缀")), FakeModel()
        runtime, _ = self.runtime(first_model, next_model)
        prior = await runtime.accept("run", "round1", "第一个问题")
        await asyncio.wait_for(asyncio.shield(prior.task), 1)
        old_speech = runtime.speech(prior)
        self.assertEqual(await anext(old_speech), "已发送片段")
        current = await runtime.accept("run", "round2", "第二个问题")
        await self.collect(runtime, current)
        messages = next_model.requests[0]["messages"]
        self.assertNotIn("未发送后缀", json.dumps(messages, ensure_ascii=False))
        index = next(i for i, item in enumerate(messages) if item.get("content") == "已发送片段")
        self.assertEqual(messages[index - 1]["role"], "system")
        self.assertIn("交付未确认", messages[index - 1]["content"])
        await old_speech.aclose()

    async def test_required_opaque_reasoning_stays_in_memory_not_persisted_audit(self):
        native = decision(call("read_1", "context_read_history", {}))
        native.assistant_message["reasoning_content"] = "PRIVATE PROVIDER REASONING"
        model = FakeModel([native, prepare()])
        runtime, _ = self.runtime(model)
        handle = await runtime.accept("run", "round1", "查历史")
        await self.collect(runtime, handle)
        self.assertTrue(any(item.get("reasoning_content") == "PRIVATE PROVIDER REASONING" for item in model.requests[1]["messages"]))
        self.assertNotIn("PRIVATE PROVIDER REASONING", json.dumps(self.events(handle), ensure_ascii=False))

    async def test_control_tools_count_towards_total_budget_and_excess_batch_calls_are_closed(self):
        model = FakeModel([decision(call("read_1", "context_read_history", {})),
                           decision(call("prepare_allowed", "prepare_response", {"mode": "answer", "evidence_refs": []}),
                                    call("prepare_excess", "prepare_response", {"mode": "answer", "evidence_refs": []}))])
        runtime, _ = self.runtime(model, max_tool_calls=2)
        handle = await runtime.accept("run", "round1", "查看历史后回答")
        self.assertEqual(await self.collect(runtime, handle), ["最终答复"])
        self.assertEqual(self.store.get_tool_result(handle.target, "prepare_allowed")["status"], "success")
        self.assertEqual(self.store.get_tool_result(handle.target, "prepare_excess")["error"], "tool_budget_exhausted")
        dispatched = [value["tool_call_id"] for kind, value in self.events(handle) if kind == "tool.dispatched"]
        self.assertNotIn("prepare_excess", dispatched)
        self.assertEqual(len(dispatched), 2)

    async def test_invalid_tool_arguments_consume_budget_and_do_not_spin_an_extra_decision(self):
        model = FakeModel([decision(call("invalid_1", "context_read_history", "{NOT JSON"))])
        runtime, _ = self.runtime(model, max_tool_calls=1)
        handle = await runtime.accept("run", "round1", "读取历史")
        self.assertEqual(await self.collect(runtime, handle), ["最终答复"])
        self.assertEqual([request["phase"] for request in model.requests], ["decision", "final"])
        self.assertEqual(self.store.get_tool_result(handle.target, "invalid_1")["error"], "invalid_tool_arguments")

    async def test_whitespace_delta_cannot_bypass_first_meaningful_output_deadline(self):
        release = self.gate()
        model = FakeModel()
        async def whitespace_then_late(messages, max_tokens=None, on_usage=None):
            try:
                yield " \n  "
                await release.wait()
                yield "LATE ANSWER AFTER DEADLINE"
            finally:
                model.stream_closed.set()
        model.stream_final = whitespace_then_late
        runtime, _ = self.runtime(model, first_output_ms=500, wall_seconds=2)
        handle = await runtime.accept("run", "round1", "首输出门槛")
        timer = asyncio.get_running_loop().call_later(.7, release.set)
        self.addCleanup(timer.cancel)
        output = await asyncio.wait_for(self.collect(runtime, handle), 2)
        self.assertTrue("".join(output).strip())
        self.assertNotIn("LATE ANSWER", "".join(output))
        self.assertEqual(handle.generated_text, "")
        self.assertEqual(handle.failure_code, "first_output_or_turn_timeout")
        self.assertTrue(model.stream_closed.is_set())

    async def test_normal_mailbox_capacity_reserves_slots_for_priority_interrupt(self):
        runtime, factory = self.runtime(FakeModel())
        controller = runtime.binding("run")
        held = asyncio.create_task(self.gate().wait())
        controller.runner = held
        pending_interrupt = None
        try:
            loop = asyncio.get_running_loop()
            for serial in range(56):
                controller.inbox.put_nowait((1, serial + 1, "held_fixture", None, loop.create_future()))
            with self.assertRaises(RuntimeFault) as rejected:
                await asyncio.wait_for(controller.ask("admit", ("overflow", "fixture", "connection")), 1)
            self.assertEqual(rejected.exception.code, "session_event_capacity")
            self.assertEqual(rejected.exception.status, 429)
            self.assertEqual(controller.inbox.qsize(), 56)
            enqueued = asyncio.Event()
            original = controller.inbox.put_nowait
            def observe_control(item):
                original(item)
                if item[2] == "interrupt":
                    enqueued.set()
            controller.inbox.put_nowait = observe_control
            pending_interrupt = asyncio.create_task(controller.ask("interrupt", "round"))
            await asyncio.wait_for(enqueued.wait(), 1)
            self.assertEqual(controller.inbox.qsize(), 57)
            priority, _, kind, _, reply = controller.inbox.get_nowait()
            self.assertEqual((priority, kind), (0, "interrupt"))
            reply.set_result(True)
            self.assertTrue(await pending_interrupt)
            self.assertEqual(factory.created, [])
        finally:
            while not controller.inbox.empty():
                *_, reply = controller.inbox.get_nowait()
                if not reply.done():
                    reply.cancel()
            if pending_interrupt is not None:
                await asyncio.gather(pending_interrupt, return_exceptions=True)
            held.cancel()
            await asyncio.gather(held, return_exceptions=True)
            controller.runner = None


if __name__ == "__main__":
    unittest.main()
