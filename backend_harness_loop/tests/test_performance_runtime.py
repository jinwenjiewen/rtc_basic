"""Deadline metrics and idle headroom are execution rules, not model semantics."""

import asyncio
from dataclasses import replace
import unittest

from backend_harness_loop.context import ContextEngine, ContextPolicy, SUMMARY_KEYS
from backend_harness_loop.models import ModelProfile, ModelProfiles
from backend_harness_loop.runtime import AgentRuntime, SYSTEM, TurnHandle
from backend_harness_loop.store import RuntimeStore, StoreError
from backend_harness_loop.tests import test_context_runtime as helpers


class PerformanceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = helpers.ContextRuntimeTests.asyncSetUp
    asyncTearDown = helpers.ContextRuntimeTests.asyncTearDown
    runtime = helpers.ContextRuntimeTests.runtime
    seed = helpers.ContextRuntimeTests.seed
    collect = helpers.ContextRuntimeTests.collect
    events = helpers.ContextRuntimeTests.events

    async def test_first_output_records_only_nonblank_model_content(self):
        class Spaced(helpers.Model):
            async def stream_final(self, messages, **options):
                yield " \n"
                yield "answer"
        runtime, _ = self.runtime(Spaced(), background_context_enabled=False)
        handle = await runtime.accept("run", "first", "hello")
        self.assertEqual(await self.collect(runtime, handle), ["answer"])
        first = self.events("model.first_output")
        self.assertEqual(len(first), 1)
        self.assertLessEqual(first[0]["elapsed_ms"], runtime.first_output_ms)
        self.assertEqual(first[0]["deadline_ms"], runtime.first_output_ms)

    async def test_fallback_never_creates_a_model_first_output_measurement(self):
        runtime, _ = self.runtime(helpers.Model(final_text=""), background_context_enabled=False)
        handle = await runtime.accept("run", "empty", "hello")
        self.assertTrue(await self.collect(runtime, handle))
        self.assertEqual(handle.failure_code, "empty_final_response")
        self.assertEqual(self.events("model.first_output"), [])

    async def test_provider_swallowing_final_timeout_cannot_emit_a_late_first_frame(self):
        class Late(helpers.Model):
            async def stream_final(self, messages, **options):
                try:
                    await asyncio.sleep(2)
                except asyncio.CancelledError:
                    yield "late-private-output"
        runtime, _ = self.runtime(Late(), first_output_ms=500, background_context_enabled=False)
        handle = await runtime.accept("run", "late", "hello")
        parts = await self.collect(runtime, handle)
        self.assertNotIn("late-private-output", parts)
        self.assertEqual(handle.failure_code, "first_output_or_turn_timeout")
        self.assertEqual(self.events("model.first_output"), [])

    async def test_planning_already_using_5s_prevents_starting_background_model(self):
        runtime, factory = self.runtime(helpers.Model())
        targets = self.seed(10, 140)
        session = runtime.binding("run")
        handle = TurnHandle(targets[-1])
        session.current = handle
        plan = runtime._plan(session, handle.target, "", runtime._schemas(), force=True, allow_completed=True, staged=True)
        await runtime._maintain(session, handle, plan, planning_ms=5100)
        self.assertEqual(factory.created, [])
        self.assertEqual(self.events("context.maintenance_failed")[-1]["code"], "context_maintenance_timeout")
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)

    async def test_protected_tail_cannot_offer_an_impossible_compact_call(self):
        model = helpers.Model()
        runtime, _ = self.runtime(model, context_policy=ContextPolicy(), background_context_enabled=False)
        self.seed(4, 1800)
        handle = await runtime.accept("run", "protected", "请只回答已有约束")
        self.assertEqual(await self.collect(runtime, handle), ["FINAL-ANSWER"])
        decision = model.requests[0]
        names = {tool["function"]["name"] for tool in decision["tools"]}
        self.assertNotIn("context_compact", names)
        self.assertTrue({"context_update", "context_select", "context_read_history", "prepare_response"} <= names)
        self.assertEqual(self.store.snapshot("session")["message_count"], 12)

    async def test_background_malformed_arguments_fail_without_retry_or_snapshot(self):
        class Malformed(helpers.Model):
            async def complete(self, messages, tools=None, **options):
                result = await super().complete(messages, tools=tools, **options)
                result.tool_calls[0]["function"]["arguments"] = '{"summary":'
                return result
        model = Malformed()
        runtime, factory = self.runtime(model, context_policy=ContextPolicy())
        targets = self.seed(10, 500)
        session = runtime.binding("run")
        handle = TurnHandle(targets[-1])
        session.current = handle
        plan = runtime._plan(session, handle.target, "", runtime._schemas(), force=True, allow_completed=True, staged=True)
        await runtime._maintain(session, handle, plan)
        self.assertEqual(len(factory.created), 1)
        self.assertEqual(self.events("context.maintenance_failed")[-1]["code"], "invalid_compact_arguments")
        self.assertEqual(self.events("context.maintenance_failed")[-1]["stage"], "arguments_decode")
        observed = self.events("context.candidate_observed")[-1]
        self.assertEqual(len(observed["arguments_sha256"]), 64)
        self.assertNotIn("summary", observed)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)
        self.assertEqual(self.store.snapshot("session")["context_candidate_count"], 0)

    async def test_rejected_background_reports_field_location_without_candidate_content(self):
        class Invalid(helpers.Model):
            async def complete(self, messages, tools=None, **options):
                import json
                result = await super().complete(messages, tools=tools, **options)
                args = json.loads(result.tool_calls[0]["function"]["arguments"])
                args["summary"]["items"][0]["source_refs"] = []
                result.tool_calls[0]["function"]["arguments"] = json.dumps(args)
                return result
        runtime, _ = self.runtime(Invalid(), context_policy=ContextPolicy())
        targets = self.seed(10, 500)
        session = runtime.binding("run")
        handle = TurnHandle(targets[-1])
        session.current = handle
        plan = runtime._plan(session, handle.target, "", runtime._schemas(), force=True, allow_completed=True, staged=True)
        await runtime._maintain(session, handle, plan)
        failed = self.events("context.maintenance_failed")[-1]
        self.assertEqual(failed["validation_detail"], {"reason": "summary_source_refs_count", "section": "items", "entry_index": 0})
        self.assertEqual(failed["code"], "invalid_context_summary")
        self.assertEqual(failed["stage"], "candidate_commit")
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)


class BackgroundHeadroomTests(unittest.TestCase):
    def setUp(self):
        self.store = RuntimeStore(":memory:")
        profile = ModelProfile("fixture", "fixture", "https://fixture.example", "fixture", "KEY", "unused")
        self.runtime = AgentRuntime(self.store, ModelProfiles({"fixture": profile}, "fixture"), model_factory=lambda p: None)
        self.runtime.register_session("session", "run", "fixture", "task", "user")
        for index in range(8):
            self.text = "x" * (1800 if index == 7 else 700)
            self.owner = self.store.admit_turn("session", str(index), self.text, "c"+str(index))
            self.store.complete_turn(self.owner, "generation_completed", "heard")
            self.store.record_delivery(self.owner, "heard", "delta")
            self.store.record_delivery(self.owner, "", "completed")
        self.session = self.runtime.binding("run")
        self.tools = self.runtime._schemas()
        self.engine = self.runtime._engine(self.session)

    def tearDown(self):
        self.store.close()

    def proposal(self, plan):
        summary = {name: [] for name in SUMMARY_KEYS}
        summary["items"] = [{"value": 3000, "source_refs": [plan.input_ref["visible_refs"][0]]}]
        return {"expected_context_version": plan.expected_context_version, "covered_through_seq": plan.covered_through_seq,
            "summary": summary, "keep_refs": [], "target_tokens": 1000}

    def plan(self):
        return self.engine.prepare(self.owner, self.text, SYSTEM, self.tools,
            force=True, allow_completed=True, staged=True, request_fields=self.runtime._fields(self.session))

    def test_idle_candidate_fit_alone_is_rejected_if_next_input_room_is_missing(self):
        plan = self.plan()
        args = self.proposal(plan)
        candidate = {**plan.before_view.context, "summary": args["summary"], "selected_refs": [],
            "context_version": plan.expected_context_version+1, "compacted_through_seq": plan.covered_through_seq}
        after = self.engine._assemble(self.owner, self.text, SYSTEM, self.tools, [], plan.before_view.material,
            candidate, plan.before_view.notes, plan.request_fields, include_owner=True)
        reserve = plan.input_ref["estimate"]["next_input_reserve_tokens_estimate"]
        self.assertEqual(reserve, len(self.text.encode("utf-8"))+512)
        self.engine = ContextEngine(self.store, replace(self.engine.policy,
            input_cap_tokens=after.metrics["input_tokens_estimate"]+reserve//4))
        plan = self.plan()
        with self.assertRaisesRegex(StoreError, "context_background_headroom_insufficient"):
            self.engine.commit(self.owner, plan, self.proposal(plan), allow_completed=True)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)
        self.assertEqual(self.store.snapshot("session")["context_candidate_count"], 0)

    def test_sufficient_room_commits_without_changing_recent_four_or_raw_ledger(self):
        plan = self.plan()
        result = self.engine.commit(self.owner, plan, self.proposal(plan), allow_completed=True)
        self.assertFalse(result.get("staged", False))
        view = self.engine.build(self.owner, self.text, SYSTEM, self.tools, allow_completed=True)
        self.assertGreaterEqual(view.metrics["recent_turn_count"], 4)
        self.assertEqual(self.store.snapshot("session")["message_count"], 16)

    def test_optional_compact_definition_cannot_overfill_an_otherwise_valid_request(self):
        owner = self.store.admit_turn("session", "current", "now", "current")
        fields = self.runtime._fields(self.session)
        base = self.engine.build(owner, "now", SYSTEM, self.tools, request_fields=fields)
        plan = self.engine.prepare(owner, "now", SYSTEM, self.tools, force=True, request_fields=fields, view=base)
        full = self.engine.build(owner, "now", SYSTEM, self.runtime._schemas(plan), request_fields=fields)
        cap = (base.metrics["input_tokens_estimate"] + full.metrics["input_tokens_estimate"]) // 2
        self.runtime.context_engines[self.session.profile_id] = ContextEngine(self.store, replace(self.engine.policy, input_cap_tokens=cap))
        self.engine = self.runtime._engine(self.session)
        base = self.runtime._view(self.session, owner, "now", self.tools)
        self.assertIsNotNone(self.engine.prepare(owner, "now", SYSTEM, self.tools, force=True, request_fields=fields, view=base))
        plan, tools, actual, edit = self.runtime._decision_context(self.session, owner, "now", [], base)
        self.assertIsNone(plan)
        self.assertFalse(actual.metrics["hard_limit_exceeded"])
        self.assertEqual(actual.material, base.material)
        self.assertNotIn("context_compact", [tool["function"]["name"] for tool in tools])


if __name__ == "__main__":
    unittest.main()
