import json
from pathlib import Path
import tempfile
import unittest

from backend_harness_loop.voice import load_voice_profile


class VoiceProfileTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "voice.json"
            path.write_text(text, encoding="utf-8")
            return load_voice_profile(path)

    def test_standalone_data_preserves_provider_fields_without_python_execution(self):
        profile = {"ASRConfig": {"Provider": "fixture", "ProviderParams": {"AppId": "asr", "Mode": "smallmodel"}},
            "TTSConfig": {"Provider": "other-fixture", "ProviderParams": {"audio": {"voice_type": "fixture-voice"}}}}
        self.assertEqual(self.load(json.dumps(profile)), profile)

    def test_voice_config_cannot_override_model_or_business_configuration(self):
        profile = {"ASRConfig": {"Provider": "v", "ProviderParams": {}}, "TTSConfig": {"Provider": "v", "ProviderParams": {}}}
        for field in ("LLMConfig", "APIKey", "BusinessPolicy", "model_profiles"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Invalid standalone ASR/TTS profile"):
                self.load(json.dumps({**profile, field: "PRIVATE VALUE"}))

    def test_duplicate_malformed_and_oversized_input_is_rejected_without_echo(self):
        for text in ('{"ASRConfig":{},"ASRConfig":{},"TTSConfig":{}}', '{"PRIVATE":', 'x' * 65_537):
            with self.subTest(length=len(text)), self.assertRaises(ValueError) as error:
                self.load(text)
            self.assertEqual(str(error.exception), "Invalid standalone ASR/TTS profile")

    def test_provider_shape_is_required(self):
        for value in ({}, {"Provider": "", "ProviderParams": {}}, {"Provider": "v", "ProviderParams": []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(json.dumps({"ASRConfig": value, "TTSConfig": value}))
