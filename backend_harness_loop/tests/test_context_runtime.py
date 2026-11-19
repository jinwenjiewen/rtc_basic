"""Controller-level M2 maintenance with real SQLite/engine and scripted models."""

import asyncio
from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from backend_harness_loop.context import ContextPolicy, SUMMARY_KEYS
from backend_harness_loop.models import ModelProfile, ModelProfiles, ModelResponse
from backend_harness_loop.runtime import AgentRuntime, FINAL_INSTRUCTION, RuntimeFault, SYSTEM
from backend_harness_loop.store import RuntimeStore


def response(identifier, name, arguments):
    call = {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
    native = {"role": "assistant", "content": None, "tool_calls": [call]}
    return ModelResponse(tool_calls=[call], assistant_message=native,
                         usage={"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
                         finish_reason="tool_calls")


def compact_response(tools, messages, serial, *, invalid=False):
    schema = tools[0]["function"]["parameters"]["properties"]
    source = json.loads(messages[1]["content"].split("\n", 1)[1])
    first = source["closed_prefix"][0]["messages"][0]["seq"]
    summary = {name: [] for name in SUMMARY_KEYS}
    summary["items"] = [{"id": "preserved", "text": "SUMMARY-PRIVATE budget=1200; no Friday; action pending",
                         "source_type": "user_statement", "status": "active",
                         "source_refs": ["seq:999999999" if invalid else "seq:" + str(first)]}]
    return response("compact-" + str(serial), "context_compact", {
        "expected_context_version": schema["expected_context_version"]["enum"][0],
        "covered_through_seq": schema["covered_through_seq"]["enum"][0],
        "summary": summary, "keep_refs": [], "target_tokens": 1000})


class Model:
    def __init__(self, *, invalid=False, gate=None, ignore_cancel=False, update_notes=False,
                 final_gate=None, final_text="FINAL-ANSWER"):
        self.invalid, self.gate, self.ignore_cancel = invalid, gate, ignore_cancel
        self.update_notes, self.final_gate, self.final_text = update_notes, final_gate, final_text
        self.requests = []
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.closed = asyncio.Event()
        self.profile = None
        self.decisions = 0

    async def _wait(self):
        while self.gate is not None and not self.gate.is_set():
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                if not self.ignore_cancel:
                    raise

    async def complete(self, messages, tools=None, **options):
        compact = len(tools or []) == 1 and tools[0]["function"]["name"] == "context_compact"
        self.requests.append({"phase": "compact" if compact else "decision", "profile": self.profile.profile_id,
                              "messages": deepcopy(messages), "tools": deepcopy(tools), **deepcopy(options)})
        self.entered.set()
        await self._wait()
        if compact:
            return compact_response(tools, messages, len(self.requests), invalid=self.invalid)
        self.decisions += 1
        if self.update_notes and self.decisions == 1:
            return response("note-update", "context_update_notes", {"expected_version": 0, "text": "NOTE-REBUILT exact latest 2500"})
        return response("prepare-" + str(self.decisions), "prepare_response", {"mode": "answer", "evidence_refs": []})

    async def stream_final(self, messages, on_usage=None, **options):
        self.requests.append({"phase": "final", "profile": self.profile.profile_id,
                              "messages": deepcopy(messages), **deepcopy(options)})
        if self.final_gate is not None:
            await self.final_gate.wait()
        yield self.final_text
        if on_usage:
            on_usage({"total_tokens": 5})

    async def aclose(self):
        self.closed.set()


class Factory:
    def __init__(self, *models):
        self.pending, self.created = deque(models), []

    def __call__(self, profile):
        if not self.pending:
            raise AssertionError("Unexpected model instance")
        model = self.pending.popleft()
        model.profile = profile
        self.created.append(model)
        return model


class ContextRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.folder.name) / "runtime.sqlite3")
        profiles = {name: ModelProfile(name, "fixture", "https://fixture.example/v1", "fixture-model-" + name,
                                      "FIXTURE_KEY", "fixture-key") for name in ("flash", "pro")}
        self.profiles = ModelProfiles(profiles, "flash")
        self.runtimes, self.gates = [], []

    async def asyncTearDown(self):
        for gate in self.gates:
            gate.set()
        for runtime in self.runtimes:
            await runtime.aclose()
        await asyncio.sleep(0)
        self.store.close()
        self.folder.cleanup()

    def gate(self):
        gate = asyncio.Event()
        self.gates.append(gate)
        return gate

    def runtime(self, *models, selected="flash", **options):
        factory = Factory(*models)
        options.setdefault("context_policy", ContextPolicy(input_cap_tokens=16384))
        runtime = AgentRuntime(self.store, self.profiles, model_factory=factory, **options)
        runtime.register_session("session", "run", selected, "task", "user")
        self.runtimes.append(runtime)
        return runtime, factory

    def seed(self, count=14, length=500):
        targets = []
        for index in range(count):
            text = "budget=1200; no Friday; action pending; fact-" + str(index) + ":" + "x" * length
            target = self.store.admit_turn("session", "seed-" + str(index), text, "seed-c-" + str(index))
            self.store.complete_turn(target, "generation_completed", "heard")
            self.store.record_delivery(target, "heard", "delta")
            self.store.record_delivery(target, "", "completed")
            targets.append(target)
        return targets

    async def collect(self, runtime, handle):
        parts = [part async for part in runtime.speech(handle)]
        await asyncio.wait_for(asyncio.shield(handle.task), 2)
        return parts

    def events(self, kind=None):
        rows = self.store.db.execute("SELECT kind,payload_json FROM ledger WHERE session_id='session' ORDER BY seq").fetchall()
        return [json.loads(row["payload_json"]) for row in rows if kind is None or row["kind"] == kind]

    async def test_short_turn_has_two_model_calls_without_fixed_context_planner(self):
        model = Model()
        runtime, factory = self.runtime(model)
        handle = await runtime.accept("run", "short", "你好")
        self.assertEqual(await self.collect(runtime, handle), ["FINAL-ANSWER"])
        self.assertEqual([request["phase"] for request in model.requests], ["decision", "final"])
        self.assertEqual(len(factory.created), 1)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)

    async def test_foreground_compaction_shares_profile_budget_and_only_final_text_is_visible(self):
        model = Model()
        runtime, factory = self.runtime(model, selected="pro", background_context_enabled=False, context_policy=ContextPolicy())
        original = self.seed(18, 700)
        first_seq = self.store.db.execute("SELECT seq FROM ledger WHERE turn_id=? AND role='user'", (original[0].turn_id,)).fetchone()[0]
        handle = await runtime.accept("run", "long", "继续按原预算和否定条件回答")
        self.assertEqual(await self.collect(runtime, handle), ["FINAL-ANSWER"])
        self.assertEqual([request["phase"] for request in model.requests], ["compact", "decision", "final"])
        self.assertEqual({request["profile"] for request in model.requests}, {"pro"})
        self.assertEqual(len(factory.created), 1)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 1)
        self.assertIn("budget=1200", self.store.read_message("session", first_seq)["content"])
        self.assertIn("SUMMARY-PRIVATE", json.dumps(model.requests[-1]["messages"]))
        calls = self.store.db.execute("SELECT state FROM tool_calls WHERE name='context_compact'").fetchall()
        self.assertEqual([row[0] for row in calls], ["success"])

    async def test_compaction_consumes_a_step_and_final_is_reserved_at_two_step_limit(self):
        model = Model()
        runtime, _ = self.runtime(model, max_steps=2, background_context_enabled=False)
        self.seed()
        handle = await runtime.accept("run", "bounded", "继续")
        self.assertEqual(await self.collect(runtime, handle), ["FINAL-ANSWER"])
        self.assertEqual([request["phase"] for request in model.requests], ["compact", "final"])
        self.assertEqual(max(event["step"] for event in self.events("model.started")), 2)

    async def test_model_requested_compaction_keeps_its_closed_native_chain_for_final(self):
        class Voluntary(Model):
            runtime = None
            initial_pressure = None
            eligible_source_bytes = None

            async def complete(self, messages, tools=None, **options):
                if not self.requests:
                    self.requests.append({"phase": "decision", "profile": self.profile.profile_id,
                                          "messages": deepcopy(messages), "tools": deepcopy(tools), **deepcopy(options)})
                    session = self.runtime.binding("run")
                    self.initial_pressure = session.context_metrics["pressure"]
                    plan = session.current.compact_plan
                    source = json.loads(plan.messages[1]["content"].split("\n", 1)[1])
                    self.eligible_source_bytes = sum(len(message.get("content", "").encode("utf-8"))
                        for group in source["closed_prefix"] for message in group["messages"]
                        if isinstance(message.get("content"), str))
                    # Script a correctly sourced proposal from the actual frozen
                    # plan; this validates dispatch/commit, not LLM discovery.
                    return compact_response([plan.tool_schema], plan.messages, 1)
                return await super().complete(messages, tools=tools, **options)

        model = Voluntary()
        runtime, _ = self.runtime(model, background_context_enabled=False, context_policy=ContextPolicy(input_cap_tokens=24576))
        model.runtime = runtime
        self.seed(9, 500)
        handle = await runtime.accept("run", "voluntary", "请整理旧信息再回答")
        self.assertEqual(await self.collect(runtime, handle), ["FINAL-ANSWER"])
        self.assertLess(model.initial_pressure, runtime.context_policy.auto_ratio)
        self.assertGreaterEqual(model.eligible_source_bytes, 1024)
        self.assertGreater(len(model.requests[0]["tools"]), 1)
        self.assertEqual([request["phase"] for request in model.requests], ["decision", "decision", "final"])
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 1)
        final = model.requests[-1]["messages"]
        calls = [call for message in final for call in message.get("tool_calls", [])]
        self.assertEqual([call["function"]["name"] for call in calls], ["context_compact", "prepare_response"])
        compact_result = next(message for message in final if message.get("tool_call_id") == calls[0]["id"])
        self.assertEqual(json.loads(compact_result["content"]), self.store.get_tool_result(handle.target, calls[0]["id"]))

    async def test_notes_are_rebuilt_after_compaction_and_native_note_write(self):
        model = Model(update_notes=True)
        runtime, _ = self.runtime(model, background_context_enabled=False, context_policy=ContextPolicy())
        self.seed(18, 700)  # Trigger compaction at the unchanged service default cap.
        handle = await runtime.accept("run", "rebuild", "继续并更新笔记")
        self.assertEqual(await self.collect(runtime, handle), ["FINAL-ANSWER"])
        self.assertEqual([request["phase"] for request in model.requests], ["compact", "decision", "decision", "final"])
        self.assertIn("NOTE-REBUILT", json.dumps(model.requests[-1]["messages"]))
        self.assertEqual(self.store.get_notes("session")["version"], 1)
        self.assertEqual(self.store.get_context("session")["context_version"], 2)

    async def test_rejected_proposals_have_one_correction_attempt_and_keep_old_view(self):
        model = Model(invalid=True)
        runtime, _ = self.runtime(model, background_context_enabled=False)
        self.seed()
        old = self.store.get_context("session")
        handle = await runtime.accept("run", "bad-candidate", "继续")
        visible = "".join(await self.collect(runtime, handle))
        self.assertNotIn("SUMMARY-PRIVATE", visible)
        self.assertEqual(self.store.get_context("session"), old)
        self.assertEqual([request["phase"] for request in model.requests], ["compact", "compact"])
        self.assertNotIn("上次压缩校验反馈", json.dumps(model.requests[0]["messages"], ensure_ascii=False))
        self.assertIn("上次压缩校验反馈", json.dumps(model.requests[1]["messages"], ensure_ascii=False))
        self.assertIn("context_reference_not_visible", json.dumps(model.requests[1]["messages"], ensure_ascii=False))
        self.assertEqual(handle.failure_code, "context_compaction_rejected")
        self.assertEqual(len(self.events("context.compact_rejected")), 2)
        self.assertEqual([row[0] for row in self.store.db.execute("SELECT state FROM tool_calls WHERE name='context_compact'")], ["error", "error"])

    async def test_foreground_maintenance_uses_the_original_first_output_deadline(self):
        model = Model(gate=self.gate())
        runtime, _ = self.runtime(model, background_context_enabled=False, first_output_ms=500, wall_seconds=2)
        self.seed()
        old = self.store.get_context("session")
        handle = await runtime.accept("run", "deadline", "继续")
        await self.collect(runtime, handle)
        self.assertEqual(handle.failure_code, "first_output_or_turn_timeout")
        self.assertEqual(self.store.get_context("session"), old)
        self.assertEqual([request["phase"] for request in model.requests], ["compact"])
        self.assertTrue(model.closed.is_set())

    async def test_background_compaction_after_final_uses_one_request_and_default_budget(self):
        foreground, background = Model(), Model()
        runtime, factory = self.runtime(foreground, background)
        # Fewer base schemas need more fixture text to keep warn-only pressure.
        self.seed(10, 380)
        handle = await runtime.accept("run", "normal-before-idle", "继续")
        self.assertEqual(await self.collect(runtime, handle), ["FINAL-ANSWER"])
        await asyncio.wait_for(background.closed.wait(), 2)
        self.assertEqual([request["phase"] for request in foreground.requests], ["decision", "final"])
        self.assertEqual([request["phase"] for request in background.requests], ["compact"])
        self.assertEqual(background.requests[0]["max_tokens"], 1500)
        started = self.events("context.maintenance_started")
        self.assertEqual(started[-1]["max_model_steps"], 1)
        self.assertEqual(started[-1]["wall_seconds"], 5)
        self.assertEqual(self.store.lookup_round("session", handle.target.round_id)["status"], "generation_completed")
        self.assertEqual(self.store.get_context("session")["context_version"], 1)
        self.assertEqual({model.profile.profile_id for model in factory.created}, {"flash"})

    async def test_background_timeout_has_no_retry_or_candidate_commit(self):
        foreground, background = Model(), Model(gate=self.gate())
        runtime, factory = self.runtime(foreground, background, background_context_seconds=.05)
        self.seed(10, 380)
        handle = await runtime.accept("run", "background-timeout", "继续")
        await self.collect(runtime, handle)
        await asyncio.wait_for(background.closed.wait(), 1)
        self.assertEqual(len(background.requests), 1)
        self.assertEqual(len(factory.created), 2)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)
        self.assertEqual(self.events("context.maintenance_failed")[-1]["code"], "context_maintenance_timeout")

    async def test_new_user_input_preempts_background_without_waiting_for_model(self):
        background = Model(gate=self.gate())
        foreground, fresh = Model(), Model(final_text="FRESH-ANSWER")
        runtime, _ = self.runtime(foreground, background, fresh)
        self.seed(10, 380)
        old = await runtime.accept("run", "before-barge-in", "继续")
        await self.collect(runtime, old)
        await asyncio.wait_for(background.entered.wait(), 1)
        new = await asyncio.wait_for(runtime.accept("run", "new-input", "新的输入"), .5)
        runtime.background_context_enabled = False
        self.assertEqual(await self.collect(runtime, new), ["FRESH-ANSWER"])
        await asyncio.wait_for(background.closed.wait(), 1)
        self.assertFalse(old.valid)
        self.assertTrue(new.valid)
        self.assertTrue(background.cancelled.is_set())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM context_snapshots WHERE owner_turn_id=?",
                                              (old.target.turn_id,)).fetchone()[0], 0)

    async def test_interrupt_completed_reply_cancels_background_and_retains_generation_state(self):
        foreground, background = Model(), Model(gate=self.gate())
        runtime, _ = self.runtime(foreground, background)
        self.seed(10, 380)
        old = await runtime.accept("run", "interrupt-idle", "继续")
        await self.collect(runtime, old)
        await asyncio.wait_for(background.entered.wait(), 1)
        await runtime.interrupt("run", old.target.round_id)
        await asyncio.wait_for(background.closed.wait(), 1)
        self.assertTrue(background.cancelled.is_set())
        self.assertEqual(self.store.lookup_round("session", old.target.round_id)["status"], "generation_completed")
        self.assertEqual(self.store.get_context("session")["context_version"], 0)

    async def test_cancel_ignoring_background_keeps_slot_and_late_proposal_cannot_commit(self):
        release_old, release_new = self.gate(), self.gate()
        foreground = Model()
        background = Model(gate=release_old, ignore_cancel=True)
        fresh = Model(gate=release_new, final_text="NEW-ANSWER")
        runtime, _ = self.runtime(foreground, background, fresh, max_live_tasks=2)
        self.seed(10, 380)
        old = await runtime.accept("run", "ignore-cancel", "继续")
        await self.collect(runtime, old)
        await asyncio.wait_for(background.entered.wait(), 1)
        pending_background = runtime.binding("run").maintenance
        new = await runtime.accept("run", "new-pending", "新任务")
        runtime.background_context_enabled = False
        await asyncio.wait_for(fresh.entered.wait(), 1)
        await asyncio.wait_for(background.cancelled.wait(), 1)
        self.assertIn(pending_background, runtime.tasks)
        with self.assertRaises(RuntimeFault) as overloaded:
            await runtime.accept("run", "must-not-steal-slot", "又一个输入")
        self.assertEqual(overloaded.exception.code, "model_task_capacity")
        self.assertTrue(new.valid)
        release_old.set()
        await asyncio.wait_for(asyncio.shield(pending_background), 1)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)
        release_new.set()
        self.assertEqual(await self.collect(runtime, new), ["NEW-ANSWER"])
        await runtime.aclose()
        await asyncio.sleep(0)
        self.assertFalse(runtime.tasks)
        self.assertTrue(all(model.closed.is_set() for model in (foreground, background, fresh)))

    def archive_long_source(self, runtime):
        """Keep a genuine long ledger record outside the small active view."""
        text = "BROAD-HISTORY budget=1200; no Friday; action pending; " + "x" * 7000
        source = self.store.admit_turn("session", "large-source", text, "large-source-c")
        self.store.complete_turn(source, "generation_completed", "recorded")
        self.store.record_delivery(source, "recorded", "delta")
        self.store.record_delivery(source, "", "completed")
        for index in range(4):
            target = self.store.admit_turn("session", "small-tail-" + str(index), "recent " + str(index), "small-tail-c-" + str(index))
            self.store.complete_turn(target, "generation_completed", "heard")
            self.store.record_delivery(target, "heard", "delta")
            self.store.record_delivery(target, "", "completed")
        owner = self.store.admit_turn("session", "archive-owner", "保留原始记录", "archive-owner-c")
        session = runtime.binding("run")
        plan = runtime._plan(session, owner, "保留原始记录", runtime._schemas(), force=True)
        candidate = compact_response([plan.tool_schema], plan.messages, 1)
        runtime._engine(session).commit(owner, plan, json.loads(candidate.tool_calls[0]["function"]["arguments"]))
        self.store.complete_turn(owner, "generation_completed", "context ready")
        self.store.record_delivery(owner, "context ready", "delta")
        self.store.record_delivery(owner, "", "completed")
        return text

    def assert_actual_requests_fit(self, runtime, model):
        session = runtime.binding("run")
        for request in model.requests:
            tools = request.get("tools") or []
            fields = runtime._fields(session, max_tokens=request.get("max_tokens"),
                tool_choice=request.get("tool_choice", "auto" if tools else "none"))
            metrics = runtime._engine(session).check_request(request["messages"], tools, request_fields=fields)
            self.assertLessEqual(metrics["input_tokens_estimate"], metrics["input_budget_tokens"])

    async def test_large_history_result_uses_remaining_room_and_keeps_a_readable_cursor(self):
        class HistoryModel(Model):
            fragment = None
            continued = None

            async def complete(self, messages, tools=None, **options):
                self.requests.append({"phase": "decision", "profile": self.profile.profile_id,
                                      "messages": deepcopy(messages), "tools": deepcopy(tools), **deepcopy(options)})
                if len(self.requests) == 1:
                    return response("broad-search", "context_search_history", {"query": "BROAD-HISTORY", "limit": 10})
                latest = json.loads(next(message["content"] for message in reversed(messages) if message["role"] == "tool"))
                if len(self.requests) == 2:
                    self.fragment = latest
                    record = latest["data"]["records"][0]
                    return response("cursor-read", "context_read_history", {
                        "record_seq": record["seq"], "offset": record["next_offset"], "max_chars": 100})
                self.continued = latest
                return response("prepare-history", "prepare_response", {"mode": "answer", "evidence_refs": []})

        model = HistoryModel()
        runtime, _ = self.runtime(model, background_context_enabled=False, context_policy=ContextPolicy())
        original = self.archive_long_source(runtime)
        calls = []
        execute = runtime.tools.execute

        async def observed(name, args, target, **options):
            result = await execute(name, args, target, **options)
            calls.append((name, options.get("result_budget_bytes"), deepcopy(result)))
            return result

        runtime.tools.execute = observed
        handle = await runtime.accept("run", "read-archive", "请核对那条较长原始材料")
        self.assertEqual(await self.collect(runtime, handle), ["FINAL-ANSWER"])
        self.assertEqual([request["phase"] for request in model.requests], ["decision", "decision", "decision", "final"])
        self.assertEqual([name for name, _, _ in calls], ["context_search_history", "context_read_history"])
        self.assertLess(calls[0][1], len(original.encode("utf-8")))
        for _, budget, result in calls:
            self.assertGreaterEqual(budget, 1024)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")), budget)
        self.assertEqual(model.fragment["status"], "success")
        self.assertTrue(model.fragment["truncated"])
        self.assertEqual(model.fragment["data"]["source_type"], "conversation_ledger")
        record = model.fragment["data"]["records"][0]
        self.assertEqual(record["reference"], "seq:" + str(record["seq"]))
        self.assertIsInstance(record["next_offset"], int)
        self.assertIn("BROAD-HISTORY", record["content"])
        self.assertNotIn("SUMMARY-PRIVATE", record["content"])
        self.assertEqual(model.continued["data"]["content"], original[record["next_offset"]:record["next_offset"] + 100])
        self.assert_actual_requests_fit(runtime, model)

    async def test_final_reserved_room_reads_real_history_before_closing_tools(self):
        class CrowdedModel(Model):
            async def complete(self, messages, tools=None, **options):
                self.requests.append({"phase": "decision", "profile": self.profile.profile_id,
                                      "messages": deepcopy(messages), "tools": deepcopy(tools), **deepcopy(options)})
                reply = response("no-room-search", "context_search_history", {"query": "BROAD-HISTORY", "limit": 10})
                reply.content = "PRIVATE_DECISION_DRAFT:" + "q" * 7000
                reply.assistant_message["content"] = reply.content
                return reply

        model = CrowdedModel()
        runtime, _ = self.runtime(model, background_context_enabled=False)
        original = self.archive_long_source(runtime)
        compacted = self.store.snapshot("session")["context_compaction_count"]
        dispatched = []
        execute = runtime.tools.execute

        async def observed(name, args, target, **options):
            dispatched.append(name)
            return await execute(name, args, target, **options)

        runtime.tools.execute = observed
        handle = await runtime.accept("run", "no-result-room", "请核对历史材料")
        self.assertEqual(await self.collect(runtime, handle), ["FINAL-ANSWER"])
        self.assertEqual([request["phase"] for request in model.requests], ["decision", "final"])
        self.assertEqual(dispatched, ["context_search_history"])
        self.assertTrue(handle.capacity_force_final)
        self.assertTrue(handle.final_capacity_reserved)
        result = self.store.get_tool_result(handle.target, "no-room-search")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["source_type"], "conversation_ledger")
        self.assertTrue(result["truncated"])
        record = result["data"]["records"][0]
        self.assertEqual(record["content"], original[record["offset"]:record["next_offset"]])
        self.assertIn("BROAD-HISTORY", record["content"])
        self.assertNotIn("SUMMARY-PRIVATE", record["content"])
        self.assertNotIn("tools", model.requests[-1])
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], compacted)
        self.assert_actual_requests_fit(runtime, model)

    async def test_even_final_room_too_small_returns_a_bounded_error_without_dispatch(self):
        class FullModel(Model):
            runtime = None
            final_room = None

            async def complete(self, messages, tools=None, **options):
                self.requests.append({"phase": "decision", "profile": self.profile.profile_id,
                                      "messages": deepcopy(messages), "tools": deepcopy(tools), **deepcopy(options)})
                reply = response("zero-room-search", "context_search_history", {"query": "BROAD-HISTORY", "limit": 10})
                reply.content = ""
                reply.assistant_message["content"] = ""
                session = self.runtime.binding("run")
                handle = session.current
                engine = self.runtime._engine(session)
                fields = self.runtime._fields(session, tool_choice="none")
                empty = engine.build(handle.target, handle.user_text, self.runtime.final_system, [],
                    [reply.assistant_message, FINAL_INSTRUCTION], request_fields=fields)
                # This adversarial native frame leaves room for the complete
                # error envelope, but never enough for a 512-byte read quota.
                length = empty.metrics["input_budget_tokens"] - empty.metrics["input_tokens_estimate"] - 450
                if length <= 0:
                    raise AssertionError("Fixture cannot reserve the minimum error envelope")
                reply.content = "q" * length
                reply.assistant_message["content"] = reply.content
                full = engine.build(handle.target, handle.user_text, self.runtime.final_system, [],
                    [reply.assistant_message, FINAL_INSTRUCTION], request_fields=fields)
                self.final_room = full.metrics["input_budget_tokens"] - full.metrics["input_tokens_estimate"]
                return reply

        model = FullModel()
        runtime, _ = self.runtime(model, background_context_enabled=False)
        model.runtime = runtime
        self.archive_long_source(runtime)
        compacted = self.store.snapshot("session")["context_compaction_count"]
        dispatched = []
        execute = runtime.tools.execute

        async def observed(name, args, target, **options):
            dispatched.append(name)
            return await execute(name, args, target, **options)

        runtime.tools.execute = observed
        handle = await runtime.accept("run", "no-final-room", "请核对历史材料")
        self.assertEqual(await self.collect(runtime, handle), ["FINAL-ANSWER"])
        self.assertEqual([request["phase"] for request in model.requests], ["decision", "final"])
        self.assertGreaterEqual(model.final_room, 200)
        self.assertLessEqual(model.final_room, 500)
        self.assertEqual(dispatched, [])
        self.assertTrue(handle.capacity_force_final)
        self.assertFalse(handle.final_capacity_reserved)
        result = self.store.get_tool_result(handle.target, "zero-room-search")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "context_result_budget_exhausted")
        self.assertIsNone(result["data"])
        self.assertNotIn("tools", model.requests[-1])
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], compacted)
        self.assert_actual_requests_fit(runtime, model)


if __name__ == "__main__":
    unittest.main()
