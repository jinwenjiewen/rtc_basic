"""Full probe path on the real text adapter, with explicitly scripted models."""

import json
from pathlib import Path
import tempfile
import unittest

import httpx

from backend_harness_loop.config import Settings
from backend_harness_loop.models import ModelProfile, ModelProfiles
from backend_harness_loop.probes import edits
from backend_harness_loop.probes.text import _page_key
from backend_harness_loop.runtime import AgentRuntime
from backend_harness_loop.store import RuntimeStore
from backend_harness_loop.tests import test_context_runtime as helpers
from backend_harness_loop.tests.test_context_edits_runtime import state
from backend_harness_loop.text import create_text_app


class ProbeModel:
    def __init__(self, profile, observation, *, skip_release=False):
        self.profile, self.observation, self.skip_release = profile, observation, skip_release
        self.serial = 0

    observe = edits.ObservedModel.observe

    def operation(self, name, args):
        self.serial += 1
        return helpers.response("probe-" + str(self.serial), name, args)

    async def complete(self, messages, tools=None, **options):
        self.observe(messages, "decision")
        meta = state(messages)
        current = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        latest = json.loads(messages[-1]["content"]) if messages[-1]["role"] == "tool" else None
        version = meta["context_version"]

        def item(identifier, value, section="items", **extra):
            return {"section": section, "id": identifier, "kind": "constraint", "value": value,
                "source_type": "user_statement", "source_refs": meta["current_input_refs"], "status": "active", **extra}

        if latest and latest["status"] == "success":
            if latest.get("data", {}).get("role") == "user":
                reference = latest["data"]["reference"]
                return self.operation("context_select", {"expected_context_version": version, "keep_refs": [reference], "release_refs": [], "purpose": "budget-audit"})
            return self.operation("prepare_response", {"mode": "answer", "evidence_refs": []})
        if "建立items中的budget_v1" in current:
            return self.operation("context_update", {"expected_context_version": version, "upserts": [item("budget_v1", 5000),
                item("weekend", False), item("order_pending", "未确认未提交", "pending_actions", status="pending")]})
        if "预算修正为3000元" in current:
            return self.operation("context_update", {"expected_context_version": version, "upserts": [item("budget_v2", 3000, supersedes=["budget_v1"])]})
        if "请明确调用context_compact" in current:
            data = json.loads(next(m["content"].split("\n", 1)[1] for m in messages if m["role"] == "user" and "working_context_data" in m["content"]))
            schema = next(tool for tool in tools if tool["function"]["name"] == "context_compact")["function"]["parameters"]["properties"]
            return self.operation("context_compact", {"expected_context_version": version,
                "covered_through_seq": schema["covered_through_seq"]["enum"][0], "summary": data["summary"], "keep_refs": [], "target_tokens": 1000})
        if "请先实际调用context_read_history" in current:
            target = self.observation[self.profile.profile_id]["seq"]
            return self.operation("context_read_history", {"record_seq": target, "offset": 0, "max_chars": 2000})
        if "只取消显式加载" in current and not self.skip_release:
            return self.operation("context_select", {"expected_context_version": version, "keep_refs": [], "release_refs": meta["selected_refs"], "purpose": "已核对"})
        if "预算进一步修正为3200元" in current:
            return self.operation("context_update", {"expected_context_version": version, "upserts": [item("budget_v3", 3200, supersedes=["budget_v2"])]})
        return self.operation("prepare_response", {"mode": "answer", "evidence_refs": []})

    async def stream_final(self, messages, **options):
        self.observe(messages, "final")
        current = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if "只输出JSON对象" in current:
            data = json.loads(next(m["content"].split("\n", 1)[1] for m in messages if m["role"] == "user" and "working_context_data" in m["content"]))
            budget = 3200 if any(item.get("id") == "budget_v3" for item in data["summary"]["items"]) else 3000
            yield json.dumps({"budget": budget, "weekend_allowed": False, "order_submitted": False})
        else:
            yield "收到"

    async def aclose(self):
        pass


class EditProbeTests(unittest.IsolatedAsyncioTestCase):
    async def case(self, skip_release):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "edits.sqlite3"
            profile = ModelProfile("flash", "fixture", "https://fixture.example", "fixture", "KEY", "private-fixture-key")
            profiles = ModelProfiles({"flash": profile}, "flash")
            observation = {"flash": {"requests": []}}
            runtime = AgentRuntime(RuntimeStore(database), profiles,
                model_factory=lambda p: ProbeModel(p, observation, skip_release=skip_release),
                background_context_enabled=False, first_output_ms=20000)
            app = create_text_app(Settings(), profiles, database, port=3103, runtime=runtime)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 8)), base_url="http://127.0.0.1:3103") as client:
                    client.headers["X-M0-Client-Key"] = await _page_key(client)
                    result = await edits._case(client, runtime, database, "flash", 0, observation)
                return result
            finally:
                await runtime.aclose()
                runtime.store.close()

    async def test_full_report_checks_real_tool_effects_and_closes_owned_session(self):
        result = await self.case(False)
        self.assertTrue(result["passed"], result)
        self.assertEqual(len(result["rounds"]), 8)
        self.assertEqual(result["database"]["update_count"], 3)
        self.assertEqual(result["database"]["select_count"], 2)
        encoded = json.dumps(result)
        self.assertNotIn("private-fixture-key", encoded)
        self.assertNotIn("M2E-", encoded)
        self.assertNotIn("text_session_", encoded)

    async def test_reply_without_release_tool_is_not_counted_as_success(self):
        result = await self.case(True)
        self.assertFalse(result["passed"])
        self.assertFalse(result["release_checks"]["selection_empty"])
        self.assertTrue(result["cleanup"]["passed"])


if __name__ == "__main__":
    unittest.main()
