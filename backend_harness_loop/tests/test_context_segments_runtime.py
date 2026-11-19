"""Segmented maintenance obeys controller cancellation and per-turn budgets."""

import asyncio
from copy import deepcopy
import json
import unittest

from backend_harness_loop.context import ContextPolicy
from backend_harness_loop.tests import test_context_runtime as helpers


class SegmentModel(helpers.Model):
    def __init__(self, *, stop_at=None, stage_gate=None, **options):
        super().__init__(**options)
        self.stop_at, self.stage_gate = stop_at, stage_gate
        self.compactions = 0

    async def complete(self, messages, tools=None, **options):
        compact = len(tools or []) == 1 and tools[0]["function"]["name"] == "context_compact"
        if compact:
            self.compactions += 1
            if self.compactions == self.stop_at:
                self.entered.set()
                while not self.stage_gate.is_set():
                    try:
                        await self.stage_gate.wait()
                    except asyncio.CancelledError:
                        self.cancelled.set()
                        if not self.ignore_cancel:
                            raise
        result = await super().complete(messages, tools, **options)
        if compact:
            source = json.loads(messages[1]["content"].split("\n", 1)[1])
            args = json.loads(result.tool_calls[0]["function"]["arguments"])
            inherited = deepcopy(source["previous_summary"])
            inherited["items"].extend(args["summary"]["items"])
            args["summary"] = inherited
            return helpers.response(result.tool_calls[0]["id"], "context_compact", args)
        return result


class SegmentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = helpers.ContextRuntimeTests.asyncSetUp
    asyncTearDown = helpers.ContextRuntimeTests.asyncTearDown
    runtime = helpers.ContextRuntimeTests.runtime
    seed = helpers.ContextRuntimeTests.seed
    collect = helpers.ContextRuntimeTests.collect
    gate = helpers.ContextRuntimeTests.gate
    events = helpers.ContextRuntimeTests.events

    async def test_step_exhaustion_keeps_inactive_progress_next_text_input_resumes(self):
        models = [SegmentModel() for _ in range(6)]
        runtime, factory = self.runtime(*models, selected="pro", background_context_enabled=False,
            context_policy=ContextPolicy(input_cap_tokens=24576), first_output_ms=20000)
        self.seed(48, length=1200)
        first = await runtime.accept("run", "first", "continue")
        await self.collect(runtime, first)
        self.assertEqual(first.failure_code, "context_maintenance_pending")
        self.assertEqual(len(models[0].requests), 3)
        self.assertEqual([r["phase"] for r in models[0].requests], ["compact"] * 3)
        self.assertEqual(self.store.snapshot("session")["context_candidate_count"], 3)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)
        self.assertEqual(self.store.get_context("session")["compacted_through_seq"], 0)
        for index in range(1, 6):
            handle = await runtime.accept("run", "resume-" + str(index), "continue")
            parts = await self.collect(runtime, handle)
            if not handle.failure_code:
                self.assertEqual(parts, ["FINAL-ANSWER"])
                break
            self.assertEqual(handle.failure_code, "context_maintenance_pending")
        else:
            self.fail("No final recovery within bounded turns")
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 1)
        self.assertTrue(self.events("context.stage_resumed"))
        self.assertTrue(all(len(m.requests) <= 4 for m in factory.created))
        self.assertTrue(all(r["profile"] == "pro" for m in factory.created for r in m.requests))
        self.assertTrue(all(runtime._engine(runtime.binding("run")).estimate_request(r["messages"], r.get("tools"),
            runtime._fields(runtime.binding("run"), max_tokens=r.get("max_tokens"), tool_choice=r.get("tool_choice", "none"))) <= 24576
            for m in factory.created for r in m.requests))

    async def test_new_input_cancels_second_segment_and_reuses_first_receipt(self):
        gate = self.gate()
        initial = SegmentModel()
        old = SegmentModel(stop_at=1, stage_gate=gate, ignore_cancel=True)
        new = SegmentModel()
        runtime, _ = self.runtime(initial, old, new, max_steps=2, background_context_enabled=False,
            context_policy=ContextPolicy(input_cap_tokens=24576))
        self.seed(40, length=900)
        # One stage per turn makes the persisted first receipt deterministic.
        first = await runtime.accept("run", "first", "continue")
        await self.collect(runtime, first)
        self.assertEqual(self.store.snapshot("session")["context_candidate_count"], 1)
        runtime.max_steps = 4
        # Cancel a new owner while it is extending the persisted first receipt.
        pending = await runtime.accept("run", "pending", "continue")
        for _ in range(200):
            if old.compactions == 1:
                break
            await asyncio.sleep(.005)
        self.assertEqual(old.compactions, 1)
        replacement = await runtime.accept("run", "replacement", "latest correction")
        await self.collect(runtime, replacement)
        self.assertFalse(pending.valid)
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(pending.task), 2)
        self.assertTrue(old.cancelled.is_set())
        self.assertTrue(any(event["stage_count"] == 1 for event in self.events("context.stage_resumed")))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM context_candidates WHERE owner_turn_id=?", (pending.target.turn_id,)).fetchone()[0], 0)
        self.assertEqual(self.store.get_context("session")["context_version"], 1)

    async def test_background_uses_one_request_and_stages_without_activating(self):
        foreground, background = SegmentModel(), SegmentModel()
        runtime, _ = self.runtime(foreground, background, context_policy=ContextPolicy(input_cap_tokens=24576))
        targets = self.seed(40, length=1000)
        # Run the ordinary bounded idle worker on a closed owner, as after delivery.
        from backend_harness_loop.runtime import TurnHandle
        session = runtime.binding("run")
        handle = TurnHandle(targets[-1])
        session.current = handle
        plan = runtime._plan(session, handle.target, "", runtime._schemas(), force=True, allow_completed=True, staged=True)
        await runtime._maintain(session, handle, plan)
        self.assertEqual(len(foreground.requests), 1)
        self.assertEqual(self.store.snapshot("session")["context_candidate_count"], 1)
        self.assertEqual(self.store.snapshot("session")["context_compaction_count"], 0)
        self.assertTrue(self.events("context.maintenance_completed")[-1]["staged"])

    async def test_background_result_after_deadline_cannot_stage_even_if_model_swallows_cancel(self):
        gate = self.gate()
        model = SegmentModel(stop_at=1, stage_gate=gate, ignore_cancel=True)
        runtime, _ = self.runtime(model, background_context_seconds=.02, context_policy=ContextPolicy(input_cap_tokens=24576))
        targets = self.seed(40, length=1000)
        from backend_harness_loop.runtime import TurnHandle
        session = runtime.binding("run")
        handle = TurnHandle(targets[-1])
        session.current = handle
        plan = runtime._plan(session, handle.target, "", runtime._schemas(), force=True, allow_completed=True, staged=True)
        task = asyncio.create_task(runtime._maintain(session, handle, plan))
        await model.entered.wait()
        await asyncio.sleep(.05)
        gate.set()
        await task
        self.assertTrue(model.cancelled.is_set())
        self.assertEqual(self.store.snapshot("session")["context_candidate_count"], 0)
        self.assertEqual(self.events("context.maintenance_failed")[-1]["code"], "context_maintenance_timeout")


if __name__ == "__main__":
    unittest.main()
