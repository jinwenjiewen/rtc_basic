"""M3 evidence cannot treat fabricated prices, fallbacks or larger budgets as passes."""

from dataclasses import replace
import json
from pathlib import Path
import unittest

from backend_harness_loop.config import Settings
from backend_harness_loop.probes.knowledge import _checks, _observations, run_probe
from backend_harness_loop.probes.text import ProbeFailure
from backend_harness_loop.store import RuntimeStore
from backend_harness_loop.tests.test_store_tools import call_message


class KnowledgeProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_nondefault_budgets_are_rejected_before_models_or_services(self):
        for options in ({"m1_first_output_deadline_ms": 20000}, {"m1_max_steps": 8}, {"m1_wall_seconds": 90}):
            with self.subTest(options=options), self.assertRaisesRegex(ProbeFailure, "knowledge_probe_requires_default_budgets"):
                await run_probe(replace(Settings(), **options), None, Path("unused.sqlite3"))

    async def test_price_claim_requires_actual_contiguous_quote_or_explicit_unknown(self):
        observation = {"tools": [{"name": "knowledge_search", "status": "success", "hit_count": 1}]}
        text = "合成课程：本期费用1280元。"
        good = {"course": "合成课程", "price_text": "1280元", "evidence_quote": "本期费用1280元。", "verified": True, "order_submitted": False}
        checks = _checks("real_followup_price", json.dumps(good), observation, [text], expected_course="合成课程")
        self.assertTrue(all(checks.values()))
        forged = {**good, "price_text": "1元", "evidence_quote": "本期费用1元。"}
        self.assertFalse(_checks("real_followup_price", json.dumps(forged), observation, [text], expected_course="合成课程")["price_quoted_or_explicitly_unconfirmed"])
        missing = {**good, "price_text": None, "evidence_quote": None, "verified": False}
        self.assertTrue(all(_checks("real_followup_price", json.dumps(missing), observation, [text], expected_course="合成课程").values()))

    async def test_successful_transport_does_not_prove_a_supported_course(self):
        observation = {"tools": [{"name": "knowledge_search", "status": "success", "hit_count": 1}]}
        checks = _checks("real_business", '{"course":"编造课程","verified":true,"order_submitted":false}', observation, ["不相关资料"])
        self.assertFalse(checks["course_supported_by_actual_text"])

    async def test_exported_observations_exclude_dialogue_arguments_and_business_text(self):
        store = RuntimeStore(":memory:")
        try:
            store.create_session("session", "run", "pro")
            target = store.admit_turn("session", "r", "PRIVATE_USER_INPUT", "c")
            store.prepare_tool_calls(target, call_message(("search", "knowledge_search", {"query": "PRIVATE_QUERY"})))
            store.mark_tool_dispatched(target, "search")
            store.complete_tool_call(target, "search", {"status": "success", "data": {"hits": [{"text": "PRIVATE_BUSINESS_TEXT",
                "source_id": "PRIVATE_DOCUMENT_ID", "reference": "kb:" + "a" * 32}]}, "error": None, "truncated": False})
            store.append_event(target, "model.started", {"profile_id": "pro", "phase": "decision"})
            class Runtime:
                pass
            runtime = Runtime()
            runtime.store = store
            metadata, texts = _observations(runtime, target.turn_id, "pro")
            encoded = json.dumps(metadata)
            for private in ("PRIVATE_USER_INPUT", "PRIVATE_QUERY", "PRIVATE_BUSINESS_TEXT", "PRIVATE_DOCUMENT_ID"):
                self.assertNotIn(private, encoded)
            self.assertEqual(texts, ["PRIVATE_BUSINESS_TEXT"])
            self.assertIsNone(metadata["model_first_output"])
            self.assertTrue(metadata["native_chain_closed"])
        finally:
            store.close()
