import tempfile
import unittest
from pathlib import Path

from backend_harness_loop.legacy import load_legacy_voice_profile


class LegacyProfileTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "main.py"
            path.write_text(text, encoding="utf-8")
            return load_legacy_voice_profile(path)

    def test_only_static_voice_fields_are_loaded_without_executing_module(self):
        result = self.load('raise RuntimeError("must not execute")\nconfig={"ASRConfig":{"Provider":"volcano","ProviderParams":{"AppId":"fixture-asr"}},"TTSConfig":{"Provider":"volcano","ProviderParams":{"audio":{"voice_type":"BV001_streaming"}}},"APIKey":"do-not-copy"}')
        self.assertEqual(set(result), {"ASRConfig", "TTSConfig"})
        self.assertEqual(result["ASRConfig"]["ProviderParams"]["AppId"], "fixture-asr")
        self.assertNotIn("do-not-copy", str(result))

    def test_dynamic_configuration_is_rejected_without_running_expression(self):
        with self.assertRaises(ValueError):
            self.load('config={"ASRConfig":__import__("missing_side_effect_module"),"TTSConfig":{}}')

    def test_ambiguous_or_missing_configuration_is_rejected(self):
        for text in ('config={}', 'config={"ASRConfig":{},"TTSConfig":{}}', 'a={"ASRConfig":{"Provider":"v","ProviderParams":{}},"TTSConfig":{"Provider":"v","ProviderParams":{}}}\nb=a.copy()\nc={"ASRConfig":{"Provider":"v","ProviderParams":{}},"TTSConfig":{"Provider":"v","ProviderParams":{}}}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)
