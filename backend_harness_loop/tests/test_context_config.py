"""Service-owned context budgets stay independent from model credentials."""

import os
import unittest
from unittest.mock import patch

from backend_harness_loop.config import Settings


class ContextConfigurationTests(unittest.TestCase):
    def test_explicit_context_budget_is_forwarded_without_model_or_rtc_keys(self):
        values = {"M2_CONTEXT_INPUT_TOKENS": "12000", "M2_CONTEXT_WINDOW_TOKENS": "20000",
                  "M2_CONTEXT_OUTPUT_RESERVE_TOKENS": "1800", "M2_CONTEXT_SAFETY_TOKENS": "700",
                  "M2_CONTEXT_KEEP_RECENT_TURNS": "5", "M2_CONTEXT_WARN_RATIO": ".65",
                  "M2_CONTEXT_AUTO_RATIO": ".80", "M2_CONTEXT_TARGET_RATIO": ".45",
                  "M2_CONTEXT_BACKGROUND": "false", "M2_CONTEXT_BACKGROUND_SECONDS": "3",
                  "M2_CONTEXT_BACKGROUND_TOKENS": "1200"}
        with patch.dict(os.environ, values, clear=True):
            settings = Settings.load(None)
        options = settings.context_options()
        policy = options["context_policy"]
        self.assertEqual(policy.input_budget_tokens, 12000)
        self.assertEqual(policy.keep_recent_turns, 5)
        self.assertEqual((policy.target_ratio, policy.warn_ratio, policy.auto_ratio), (.45, .65, .80))
        self.assertFalse(options["background_context_enabled"])
        self.assertEqual((options["background_context_seconds"], options["background_context_tokens"]), (3, 1200))
        self.assertFalse(settings.ark_api_key or settings.callback_secret)

    def test_inconsistent_thresholds_or_unprotected_tail_are_rejected(self):
        for values in ({"M2_CONTEXT_WARN_RATIO": ".9"}, {"M2_CONTEXT_KEEP_RECENT_TURNS": "3"},
                       {"M2_CONTEXT_WINDOW_TOKENS": "1000"}):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(ValueError):
                    Settings.load(None).context_options()
