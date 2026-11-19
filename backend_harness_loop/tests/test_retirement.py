import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class RetirementTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[2]

    @unittest.skipUnless((root / "rag_llm_server/main.py").is_file(), "Retired backend is excluded from the 3.0 release")
    def test_retired_script_and_asgi_import_are_blocked_before_any_sdk_or_database(self):
        environment = {**os.environ}
        environment.pop("RTC_LEGACY_RECOVERY", None)
        commands = [
            [sys.executable, "rag_llm_server/main.py"],
            [sys.executable, "-c", "import sys; sys.path.insert(0, 'rag_llm_server'); import main"],
        ]
        for command in commands:
            with self.subTest(command=command[-1]):
                result = subprocess.run(command, cwd=self.root, env=environment, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertIn("legacy_service_retired", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_product_rejects_legacy_source_before_loading_it(self):
        result = subprocess.run([sys.executable, "-m", "backend_harness_loop", "product", "--legacy-source", "must-not-be-read.py"],
            cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "blocked")
        self.assertNotIn("Traceback", result.stderr)
