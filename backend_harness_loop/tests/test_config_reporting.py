import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend_harness_loop.config import Settings
from backend_harness_loop.reporting import write_report


class ConfigReportingTests(unittest.TestCase):
    def test_explicit_file_and_environment_precedence_without_mutation(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / "fixture.env"
            path.write_text("ARK_API_KEY=file-secret\nARK_ENDPOINT_ID=file-model\n", encoding="utf-8")
            os.environ["ARK_ENDPOINT_ID"] = "environment-model"
            settings = Settings.load(path)
            self.assertEqual(settings.ark_api_key, "file-secret")
            self.assertEqual(settings.ark_model, "environment-model")
            self.assertNotIn("ARK_API_KEY", os.environ)
            self.assertNotIn("file-secret", repr(settings))
            self.assertFalse(settings.trust_env)
            self.assertEqual(settings.model_thinking, "disabled")

    def test_missing_file_is_not_silently_ignored(self):
        with self.assertRaises(ValueError):
            Settings.load(Path("does-not-exist-m0.env"))

    def test_rtc_app_key_override_is_secret_and_does_not_mutate_environment(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / "fixture.env"
            path.write_text("RTC_APP_KEY=file-app-key\n", encoding="utf-8")
            loaded = Settings.load(path)
            self.assertEqual(loaded.rtc_app_key, "file-app-key")
            self.assertNotIn("file-app-key", repr(loaded))
            self.assertTrue(loaded.inventory()["configured"]["rtc_app_key"])
            os.environ["M0_RTC_APP_KEY"] = "m0-app-key"
            self.assertEqual(Settings.load(path).rtc_app_key, "m0-app-key")
            self.assertNotIn("RTC_APP_KEY", os.environ)

    def test_explicit_overlay_precedence_and_static_configuration_do_not_mutate_environment(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            base = Path(folder) / "base.env"
            overlay = Path(folder) / "overlay.env"
            base.write_text("ARK_API_KEY=base-key\nARK_ENDPOINT_ID=base-model\nM0_RTC_STATIC_TOKEN=base-token\n", encoding="utf-8")
            overlay.write_text("ARK_API_KEY=overlay-key\nM0_RTC_STATIC_TOKEN=overlay-token-${NOT_EXPANDED}\nM0_RTC_STATIC_ROOM_ID=overlay-room\nM0_RTC_STATIC_USER_ID=overlay-user\n", encoding="utf-8")
            loaded = Settings.load(base, overlay_file=overlay)
            self.assertEqual(loaded.ark_api_key, "overlay-key")
            self.assertEqual(loaded.ark_model, "base-model")
            self.assertEqual(loaded.rtc_static_token, "overlay-token-${NOT_EXPANDED}")
            self.assertEqual((loaded.rtc_static_room_id, loaded.rtc_static_user_id), ("overlay-room", "overlay-user"))
            os.environ["M0_RTC_STATIC_TOKEN"] = "environment-token"
            loaded = Settings.load(base, overlay_file=overlay)
            self.assertEqual(loaded.rtc_static_token, "environment-token")
            self.assertEqual(dict(os.environ), {"M0_RTC_STATIC_TOKEN": "environment-token"})
            for value in ("environment-token", "overlay-room", "overlay-user"):
                self.assertNotIn(value, repr(loaded))
                self.assertNotIn(value, json.dumps(loaded.inventory()))
            for key in ("rtc_static_token", "rtc_static_room_id", "rtc_static_user_id"):
                self.assertTrue(loaded.inventory()["configured"][key])

    def test_static_mode_never_guesses_from_legacy_token_or_app_key_fields(self):
        with patch.dict(os.environ, {"RTC_APP_KEY": "001-console-like-token", "RTC_STATIC_TOKEN": "legacy-token", "RTC_ROOM_ID": "legacy-room", "RTC_USER_ID": "legacy-user"}, clear=True):
            loaded = Settings.load(None)
            self.assertEqual(loaded.rtc_app_key, "001-console-like-token")
            self.assertFalse(loaded.rtc_static_token)
            self.assertFalse(loaded.rtc_static_room_id)
            self.assertFalse(loaded.rtc_static_user_id)

    def test_missing_explicit_overlay_is_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, "overlay"):
            Settings.load(None, overlay_file=Path("does-not-exist-m0-overlay.env"))

    def test_static_credentials_and_bindings_are_redacted_from_report(self):
        loaded = Settings(rtc_static_token="fixture-static-token", rtc_static_room_id="fixture-static-room", rtc_static_user_id="fixture-static-user")
        values = [loaded.rtc_static_token, loaded.rtc_static_room_id, loaded.rtc_static_user_id]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            report = write_report(path, [{"name": "static", "status": "blocked", "evidence": values}], loaded)
            self.assertEqual(report["results"][0]["evidence"], ["[REDACTED]"] * 3)
            for value in values:
                self.assertNotIn(value, path.read_text("utf-8"))

    def test_report_redacts_credentials_and_never_promotes_probe_to_m0_complete(self):
        settings = Settings(ark_api_key="fixture-api-key", callback_secret="fixture-callback-key", rtc_app_key="fixture-app-key")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            report = write_report(path, [{"name": "fixture", "status": "passed", "evidence": {"accidental": "fixture-api-key fixture-callback-key fixture-app-key"}}], settings)
            self.assertFalse(report["m0_complete"])
            self.assertNotIn("fixture-api-key", path.read_text("utf-8"))
            self.assertNotIn("fixture-callback-key", path.read_text("utf-8"))
            self.assertNotIn("fixture-app-key", path.read_text("utf-8"))
            self.assertEqual(report, json.loads(path.read_text("utf-8")))

    def test_json_escaped_secret_is_redacted_before_serialization(self):
        settings = Settings(callback_secret='fixture-"quote\\slash')
        with tempfile.TemporaryDirectory() as folder:
            report = write_report(Path(folder) / "report.json", [{"name": "fixture", "status": "passed", "evidence": [settings.callback_secret]}], settings)
            self.assertEqual(report["results"][0]["evidence"], ["[REDACTED]"])

    def test_model_secrets_are_independent_and_redacted_in_m1_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "m1.json"
            report = write_report(path, [{"name": "fixture", "status": "passed", "evidence": "private-model-key"}], Settings(), milestone="M1", extra_secrets=("private-model-key",))
            self.assertEqual(report["milestone"], "M1")
            self.assertFalse(report["m1_complete"])
            self.assertNotIn("private-model-key", path.read_text("utf-8"))

    def test_m1_budget_configuration_does_not_change_m0_budget(self):
        with patch.dict(os.environ, {"M1_FIRST_OUTPUT_DEADLINE_MS": "8500", "M1_MAX_MODEL_STEPS": "3"}, clear=True):
            settings = Settings.load(None)
            self.assertEqual(settings.m1_first_output_deadline_ms, 8500)
            self.assertEqual(settings.first_output_deadline_ms, 6000)
            self.assertEqual(settings.m1_max_steps, 3)


if __name__ == "__main__":
    unittest.main()
