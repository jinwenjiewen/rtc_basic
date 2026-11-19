"""M3 operator policy is independent of retrieval and cannot be chosen by input."""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend_harness_loop.business import BusinessPolicy
from backend_harness_loop.config import Settings
from backend_harness_loop.reporting import write_report
from backend_harness_loop.tests.test_knowledge import CONFIG
from backend_harness_loop.probes.enrollment import checks


class BusinessPolicyTests(unittest.TestCase):
    def test_default_no_quote_and_no_invented_entry_are_explicit(self):
        policy = BusinessPolicy()
        self.assertEqual(policy.pricing_mode, "defer_to_checkout")
        self.assertFalse(policy.inventory()["checkout_link_configured"])
        self.assertFalse(policy.inventory()["enrollment_contact_configured"])
        text = policy.instruction()
        for rule in ("当前不对外报价", "价格字段使用null", "用户自己的预算", "当前尚无已配置的报名入口", "不能编造链接"):
            self.assertIn(rule, text)
        self.assertNotIn("https://", text)

    def test_policy_file_has_no_provider_or_credential_dependency(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "business.json"
            path.write_text(json.dumps({"schema_version": 1, "pricing_mode": "defer_to_checkout",
                "checkout_url": "https://enroll.example/course", "enrollment_contact": "Actual business contact fixture"}), encoding="utf-8")
            policy = BusinessPolicy.load(path)
            self.assertTrue(policy.inventory()["checkout_link_configured"])
            self.assertTrue(policy.inventory()["enrollment_contact_configured"])
            self.assertIn(policy.checkout_url, policy.instruction())
            self.assertNotIn(policy.checkout_url, repr(policy))
            with self.assertRaises(FrozenInstanceError):
                policy.pricing_mode = "evidence_only"

    def test_invalid_policy_is_rejected_with_stable_diagnostics(self):
        for values in ({"pricing_mode": []}, {"pricing_mode": "user_override"}, {"checkout_url": 123},
                       {"checkout_url": "http://enroll.example"}, {"checkout_url": "https://user:key@enroll.example"},
                       {"enrollment_contact": 123}, {"enrollment_contact": "\n"}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "invalid_business_"):
                BusinessPolicy(**values)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.json"
            path.write_text('{"schema_version":1,"api_key":"unrelated-private-key"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid_business_policy_file"):
                BusinessPolicy.load(path)

    def test_business_policy_survives_knowledge_adapter_replacement(self):
        policy = BusinessPolicy()
        sentinel = object()
        with patch("backend_harness_loop.knowledge.create_knowledge_backend", return_value=sentinel):
            first = Settings(knowledge_config=CONFIG, business_policy=policy).business_tool_options()
            self.assertIs(first["knowledge"], sentinel)
            self.assertIs(first["business_policy"], policy)
            default = Settings(knowledge_config=CONFIG).business_tool_options()
            self.assertEqual(default["business_policy"].pricing_mode, "defer_to_checkout")
            unconfigured = Settings().business_tool_options()
            self.assertIsNone(unconfigured["business_policy"])

    def test_report_excludes_actual_enrollment_entry_and_contact(self):
        policy = BusinessPolicy(checkout_url="https://private-enroll.example/course", enrollment_contact="private-contact-fixture")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            result = write_report(path, [{"evidence": [policy.checkout_url, policy.enrollment_contact]}],
                                  Settings(business_policy=policy), milestone="M3")
            self.assertEqual(result["results"][0]["evidence"], ["[REDACTED]"] * 2)
            self.assertNotIn(policy.checkout_url, path.read_text("utf-8"))
            self.assertNotIn(policy.enrollment_contact, path.read_text("utf-8"))

    def test_probe_rejects_disclosed_amount_and_fake_order_even_if_price_field_null(self):
        observation = {"tools": []}
        reply = json.dumps({"price": None, "enrollment_intent": True, "order_submitted": True,
                            "next_step": "enrollment_method_pending", "reply": "课程只需1元，已经下单。"}, ensure_ascii=False)
        result = checks("enrollment_order", reply, observation, [])
        self.assertFalse(result["no_order_claim"])
        self.assertFalse(result["no_currency_amount_in_guidance"])

    def test_probe_retains_user_budget_without_treating_it_as_course_price(self):
        reply = json.dumps({"course": "星河 SQL 基础课", "budget": 900, "weekend_allowed": False,
                            "enrollment_intent": False, "order_submitted": False, "reply": "先不报名，保留你的最新条件。"}, ensure_ascii=False)
        self.assertTrue(all(checks("enrollment_correction", reply, {"tools": []}, []).values()))
