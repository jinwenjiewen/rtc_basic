"""The acceptance report keeps failed, fallback and cancelled samples honest."""

from copy import deepcopy
import unittest

from backend_harness_loop.models import ModelProfile, ModelProfiles
from backend_harness_loop.probes.performance import acceptance, _seed_background, _working_view_checks
from backend_harness_loop.runtime import AgentRuntime
from backend_harness_loop.store import RuntimeStore


def case():
    return {"scenario": "continuous_long_dialogue", "passed": True,
        "rounds": [{"state": "generation_completed", "error_code": None,
            "model_deadline_met": True, "model_first_output": {"elapsed_ms": 8999}}],
        "database": {"maintenance": [{"kind": "context.maintenance_completed", "elapsed_ms": 4900, "planning_ms": 50}]}}


class PerformanceProbeTests(unittest.TestCase):
    def test_working_view_oracle_rejects_history_or_notes_substitution(self):
        store = RuntimeStore(":memory:")
        store.create_session("session", "run", "fixture")
        try:
            owner = store.admit_turn("session", "semantic", "constraints?", "semantic")
            self.assertTrue(all(_working_view_checks(store, "session", owner.turn_id).values()))
            store.prepare_tool_calls(owner, {"role": "assistant", "content": None, "tool_calls": [
                {"id": "read", "type": "function", "function": {"name": "context_read_history", "arguments": "{}"}}]})
            self.assertFalse(_working_view_checks(store, "session", owner.turn_id)["semantic_without_history_tools"])
            store.update_notes(owner, 0, "substitute")
            self.assertFalse(_working_view_checks(store, "session", owner.turn_id)["no_notes_substitute"])
        finally:
            store.close()

    def test_background_fixture_reaches_warn_without_foreground_auto_compaction(self):
        store = RuntimeStore(":memory:")
        profile = ModelProfile("fixture", "fixture", "https://fixture.example", "fixture", "KEY", "unused",
            decision_tool_choice="required", extra_body={"thinking": {"type": "disabled"}})
        runtime = AgentRuntime(store, ModelProfiles({"fixture": profile}, "fixture"), first_output_ms=9000)
        runtime.register_session("session", "run", "fixture", "task", "user")
        try:
            _seed_background(store, "session")
            text = "只回复收到，不修改摘要或笔记。"
            owner = store.admit_turn("session", "trigger", text, "trigger")
            session = runtime.binding("run")
            before = runtime._view(session, owner, text, runtime._schemas())
            self.assertTrue(before.metrics["warning"])
            self.assertFalse(before.metrics["needs_compaction"])
            store.complete_turn(owner, "generation_completed", "收到")
            store.record_delivery(owner, "收到", "delta")
            store.record_delivery(owner, "", "completed")
            after = runtime._view(session, owner, text, runtime._schemas(), allow_completed=True)
            self.assertTrue(after.metrics["warning"])
            self.assertIsNotNone(runtime._plan(session, owner, text, runtime._schemas(), force=True,
                allow_completed=True, staged=True, view=after))
        finally:
            store.close()

    def test_failed_round_is_retained_in_latency_gate(self):
        good = case()
        bad = deepcopy(good["rounds"][0])
        bad.update(state="failed", error_code="first_output_or_turn_timeout", model_deadline_met=False, model_first_output=None)
        good["rounds"].insert(0, bad)
        self.assertFalse(acceptance([good])["foreground_all_completed_with_model_first_text_within_9s"])

    def test_fallback_and_empty_cases_never_count_as_model_first_text(self):
        sample = case()
        sample["rounds"][0].update(model_first_output=None, fallback_not_counted=True)
        self.assertFalse(acceptance([sample])["foreground_all_completed_with_model_first_text_within_9s"])
        sample["rounds"].clear()
        self.assertFalse(acceptance([sample])["foreground_all_completed_with_model_first_text_within_9s"])
        self.assertFalse(acceptance([])["foreground_all_completed_with_model_first_text_within_9s"])

    def test_background_failure_does_not_turn_into_a_pass_after_later_success(self):
        sample = case()
        sample["database"]["maintenance"].insert(0, {"kind": "context.maintenance_failed", "code": "context_maintenance_timeout"})
        result = acceptance([sample])
        self.assertTrue(result["foreground_all_completed_with_model_first_text_within_9s"])
        self.assertEqual(result["routine_background_failure_count"], 1)
        self.assertFalse(result["routine_background_all_succeeded"])

    def test_planning_counts_towards_the_same_five_second_budget(self):
        sample = case()
        sample["database"]["maintenance"][0]["planning_ms"] = 101
        result = acceptance([sample])
        self.assertFalse(result["completed_maintenance_including_planning_within_5s"])
        self.assertFalse(result["routine_background_all_succeeded"])

    def test_deliberate_preemption_is_separate_from_idle_maintenance_failure(self):
        cancelled = case()
        cancelled.update(scenario="background_preempt")
        cancelled["database"]["maintenance"] = [{"kind": "context.maintenance_cancelled", "elapsed_ms": 12}]
        result = acceptance([case(), cancelled])
        self.assertTrue(result["routine_background_all_succeeded"])
        self.assertEqual(result["routine_background_completed_count"], 1)
        self.assertFalse(result["production_slo_passed"])


if __name__ == "__main__":
    unittest.main()
