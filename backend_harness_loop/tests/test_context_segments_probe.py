"""Exercise the probe's complete report/cleanup path through the text adapter.

Models here are scripted. This verifies measurement plumbing, not real-model
semantic quality or latency; real TCP/cloud results are recorded separately.
"""

import json
from pathlib import Path
import tempfile
import unittest

import httpx

from backend_harness_loop.config import Settings
from backend_harness_loop.models import ModelProfile, ModelProfiles
from backend_harness_loop.probes import segments
from backend_harness_loop.runtime import AgentRuntime
from backend_harness_loop.store import RuntimeStore
from backend_harness_loop.tests import test_context_runtime as helpers
from backend_harness_loop.tests.test_context_segments_runtime import SegmentModel
from backend_harness_loop.text import create_text_app


class ProbeModel(SegmentModel):
    async def complete(self, messages, tools=None, **options):
        if len(tools or []) == 1:
            return await super().complete(messages, tools, **options)
        prompt = "\n".join(m.get("content", "") or "" for m in messages if m["role"] == "user")
        if "initial_budget（最初预算数字）" in prompt and not any(m["role"] == "tool" for m in messages):
            return helpers.response("read-original", "context_read_history", {"record_seq": 3, "offset": 0, "max_chars": 1000})
        return await super().complete(messages, tools, **options)

    async def stream_final(self, messages, **options):
        prompt = "\n".join(m.get("content", "") or "" for m in messages if m["role"] == "user")
        if "initial_budget（最初预算数字）" in prompt:
            yield '{"initial_budget":5000,"source_ref":"seq:3"}'
        elif "weekend_allowed（是否接受周末课程" in prompt:
            yield '{"budget":3000,"weekend_allowed":false,"order_submitted":false}'
        else:
            yield "收到"


class SegmentProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_report_uses_actual_counter_fields_and_closes_only_owned_session(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "probe.sqlite3"
            store = RuntimeStore(database)
            profile = ModelProfile("flash", "fixture", "https://fixture.example", "fixture-model", "KEY", "private-fixture-key")
            profiles = ModelProfiles({"flash": profile}, "flash")

            def factory(selected):
                model = ProbeModel()
                model.profile = selected
                return model

            runtime = AgentRuntime(store, profiles, model_factory=factory, background_context_enabled=False, first_output_ms=20000)
            app = create_text_app(Settings(), profiles, database, port=3103, runtime=runtime)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 8)),
                                             base_url="http://127.0.0.1:3103") as client:
                    client.headers["X-M0-Client-Key"] = await segments._page_key(client)
                    result = await segments._case(client, runtime, database, "flash", 0, 1800)
                self.assertTrue(result["passed"], result)
                self.assertTrue(result["cleanup"]["passed"])
                self.assertTrue(result["first_checkpoint"]["unfinished_receipts_remained_inactive"])
                self.assertTrue(result["storage_reopened_and_context_cache_cleared"])
                self.assertGreaterEqual(result["staging"]["candidate_count"], 3)
                encoded = json.dumps(result)
                self.assertNotIn("private-fixture-key", encoded)
                self.assertNotIn("合成压力材料", encoded)
                self.assertNotIn("text_session_", encoded)
                self.assertNotIn("summary_json", encoded)
            finally:
                await runtime.aclose()
                runtime.store.close()


if __name__ == "__main__":
    unittest.main()
