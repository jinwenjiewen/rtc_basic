"""M3 uses the actual controller, SQLite, native frames and HTTP adapter."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from backend_harness_loop.knowledge import KnowledgeSearch
from backend_harness_loop.models import ModelProfiles
from backend_harness_loop.runtime import AgentRuntime, FINAL_SYSTEM
from backend_harness_loop.store import RuntimeStore
from backend_harness_loop.text import create_text_app
from backend_harness_loop.config import Settings
from backend_harness_loop.tests.test_knowledge import CONFIG, payload
from backend_harness_loop.tests.test_runtime import FakeModel, Factory, call, decision, prepare
from backend_harness_loop.tests.test_text import events


class ObservingModel(FakeModel):
    """Scripted decisions may depend on genuine observations, never on a timer."""
    def __init__(self, choose, **kwargs):
        super().__init__(**kwargs)
        self.choose = choose

    async def complete(self, messages, **kwargs):
        self.requests.append({"phase": "decision", "profile_id": self.profile.profile_id, "messages": messages})
        return self.choose(messages, len(self.requests))


def last_hit(messages):
    for message in reversed(messages):
        if message["role"] == "tool":
            value = json.loads(message["content"])
            if isinstance(value.get("data"), dict) and value["data"].get("hits"):
                return value["data"]["hits"][0]
    raise AssertionError("no real knowledge observation")


class KnowledgeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        path = self.root / "models.json"
        path.write_text(json.dumps({"schema_version": 1, "default_profile": "flash",
            "providers": {"fake": {"base_url": "https://model.example/v1", "api_key_env": "MODEL_KEY"}},
            "profiles": {name: {"provider": "fake", "model": name} for name in ("flash", "pro")}}), encoding="utf-8")
        self.profiles = ModelProfiles.load(path, environ={"MODEL_KEY": "fixture-model-key"})
        self.store = RuntimeStore(self.root / "runtime.sqlite3")
        self.handler = lambda _: httpx.Response(200, json=payload())
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: self.handler(request)))
        self.runtime = None
        self.releases = []

    async def asyncTearDown(self):
        for event in self.releases:
            event.set()
        if self.runtime:
            await self.runtime.aclose()
        await self.client.aclose()
        self.store.close()
        self.folder.cleanup()

    def start(self, *models, **options):
        self.runtime = AgentRuntime(self.store, self.profiles, model_factory=Factory(*models),
            knowledge=KnowledgeSearch(CONFIG, client=self.client), background_context_enabled=False,
            first_output_ms=9000, **options)
        self.runtime.register_session("session", "run", "pro", "task", "user")
        return self.runtime

    async def collect(self, handle):
        result = [text async for text in self.runtime.speech(handle)]
        await asyncio.wait_for(asyncio.shield(handle.task), 1)
        return result

    async def test_two_dependent_searches_share_profile_and_commit_before_dispatch(self):
        observed = []
        def handler(request):
            rows = self.store.db.execute("SELECT state FROM tool_calls WHERE name='knowledge_search' ORDER BY rowid").fetchall()
            self.assertEqual(rows[-1]["state"], "dispatched")
            self.assertFalse(self.store.db.in_transaction)
            query = json.loads(request.content)["query"]
            observed.append(query)
            return httpx.Response(200, json=payload("合成课程甲，编码K17。" if len(observed) == 1 else "K17费用1200元，不保证就业。"))
        self.handler = handler
        def choose(messages, step):
            if step == 1:
                return decision(call("first", "knowledge_search", {"query": "合成课程甲 编码"}))
            if step == 2:
                self.assertIn("K17", last_hit(messages)["text"])
                return decision(call("second", "knowledge_search", {"query": "K17 费用"}))
            ref = last_hit(messages)["reference"]
            return decision(call("prepare", "prepare_response", {"mode": "answer", "evidence_refs": [ref]}))
        model = ObservingModel(choose, chunks=("合成费用1200元。",))
        runtime = self.start(model)
        handle = await runtime.accept("run", "r", "查费用")
        self.assertEqual(await self.collect(handle), ["合成费用1200元。"])
        self.assertIsNone(handle.failure_code)
        self.assertEqual(observed, ["合成课程甲 编码", "K17 费用"])
        self.assertEqual([r["phase"] for r in model.requests], ["decision"] * 3 + ["final"])
        self.assertEqual({r["profile_id"] for r in model.requests}, {"pro"})
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM knowledge_refs").fetchone()[0], 2)
        messages = self.store.get_turn_messages(handle.target)
        self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], ["first", "second", "prepare"])

    async def test_final_uses_closed_phase_policy_and_keeps_all_native_observations(self):
        model = FakeModel([decision(call("search", "knowledge_search", {"query": "合成"})), prepare()])
        self.start(model)
        handle = await self.runtime.accept("run", "r", "查课程")
        await self.collect(handle)
        first, final = model.requests[0]["messages"], model.requests[-1]["messages"]
        self.assertIn("在prepare_response.evidence_refs", first[0]["content"])
        self.assertTrue(final[0]["content"].startswith(FINAL_SYSTEM))
        self.assertIn("当前不对外报价", final[0]["content"])
        self.assertNotIn("在prepare_response.evidence_refs", final[0]["content"])
        self.assertFalse(any(m.get("content", "").startswith("Harness本轮剩余预算")
                             for m in final if m.get("role") == "system"))
        self.assertEqual([m["tool_call_id"] for m in final if m.get("role") == "tool"], ["search", "prepare_1"])
        self.assertEqual([m["id"] for item in final for m in item.get("tool_calls", [])], ["search", "prepare_1"])
        self.assertTrue(any(m["content"] == "查课程" for m in final if m.get("role") == "user"))

    async def test_forced_final_after_last_search_has_no_pending_control_operation(self):
        model = FakeModel([decision(call("search", "knowledge_search", {"query": "合成"}))])
        self.start(model, max_steps=2)
        handle = await self.runtime.accept("run", "r", "查课程")
        await self.collect(handle)
        self.assertTrue(model.requests[-1]["messages"][0]["content"].startswith(FINAL_SYSTEM))
        final_event = self.store.db.execute("SELECT payload_json FROM ledger WHERE turn_id=? AND kind='model.started' ORDER BY seq DESC LIMIT 1", (handle.target.turn_id,)).fetchone()
        data = json.loads(final_event[0])
        self.assertEqual(data["final_cause"], "model_call_limit")
        self.assertTrue(data["decision_phase_closed"])
        self.assertEqual(len(model.requests), 2)

    async def test_greeting_has_no_forced_business_request(self):
        def unexpected(_):
            self.fail("greeting was forced to search")
        self.handler = unexpected
        self.start(FakeModel([prepare()], chunks=("你好。",)))
        handle = await self.runtime.accept("run", "r", "你好")
        self.assertEqual(await self.collect(handle), ["你好。"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM knowledge_refs").fetchone()[0], 0)

    async def test_no_results_allows_model_to_rewrite_and_search_again(self):
        queries = []
        def handler(request):
            queries.append(json.loads(request.content)["query"])
            value = {"code": 0, "data": {"result_list": []}} if len(queries) == 1 else payload()
            return httpx.Response(200, json=value)
        self.handler = handler
        model = FakeModel([decision(call("empty", "knowledge_search", {"query": "合成别名"})),
            decision(call("retry", "knowledge_search", {"query": "合成课程甲"})), prepare()])
        self.start(model)
        handle = await self.runtime.accept("run", "r", "找课程")
        await self.collect(handle)
        self.assertEqual(self.store.get_tool_result(handle.target, "empty")["status"], "no_results")
        self.assertEqual(self.store.get_tool_result(handle.target, "retry")["status"], "success")
        self.assertEqual(queries, ["合成别名", "合成课程甲"])

    async def test_tool_timeout_is_closed_and_model_can_explain_failure(self):
        cancelled = asyncio.Event()
        async def handler(_):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        # MockTransport detects an async function; avoid the setup's sync wrapper.
        await self.client.aclose()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model = FakeModel([decision(call("slow", "knowledge_search", {"query": "合成"})), prepare()], chunks=("暂时无法确认。",))
        self.start(model, tool_seconds=.02)
        handle = await self.runtime.accept("run", "r", "查课程")
        self.assertEqual(await self.collect(handle), ["暂时无法确认。"])
        self.assertEqual(self.store.get_tool_result(handle.target, "slow")["status"], "timeout")
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(handle.failure_code)
        budgets = [json.loads(next(m["content"].split("：", 1)[1] for m in request["messages"]
            if m.get("role") == "system" and m.get("content", "").startswith("Harness本轮剩余预算"))) for request in model.requests[:2]]
        self.assertLess(budgets[1]["first_output_remaining_ms"], budgets[0]["first_output_remaining_ms"])
        self.assertEqual(budgets[1]["model_calls_remaining_including_final"], 3)
        self.assertEqual(budgets[1]["native_calls_remaining"], 7)

    async def test_superseding_input_fences_provider_that_returns_after_cancellation(self):
        entered, released, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.releases.append(released)
        async def handler(_):
            entered.set()
            try:
                await released.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await released.wait()
            return httpx.Response(200, json=payload())
        await self.client.aclose()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.start(FakeModel([decision(call("old", "knowledge_search", {"query": "合成"}))]), FakeModel(chunks=("新轮次",)))
        old = await self.runtime.accept("run", "old_r", "旧输入")
        await asyncio.wait_for(entered.wait(), 1)
        new = await self.runtime.accept("run", "new_r", "改为新输入")
        await asyncio.wait_for(cancelled.wait(), 1)
        released.set()
        self.assertEqual(await self.collect(new), ["新轮次"])
        await asyncio.wait_for(asyncio.shield(old.task), 1)
        self.assertEqual(self.store.get_tool_result(old.target, "old")["status"], "cancelled")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM knowledge_refs").fetchone()[0], 0)
        self.assertEqual(old.generated_text, "")

    async def test_unbacked_business_reference_is_rejected_before_final(self):
        self.start(FakeModel([decision(call("bad", "prepare_response", {"mode": "answer", "evidence_refs": ["kb:" + "a" * 32]})), prepare()]))
        handle = await self.runtime.accept("run", "r", "测试引用")
        await self.collect(handle)
        self.assertEqual(self.store.get_tool_result(handle.target, "bad")["error"]["code"], "knowledge_reference_not_found")
        self.assertEqual(self.store.get_tool_result(handle.target, "prepare_1")["status"], "success")

    async def test_close_cancels_external_wait_without_committing_late_evidence(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def handler(_):
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        await self.client.aclose()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.start(FakeModel([decision(call("pending", "knowledge_search", {"query": "合成"}))]))
        handle = await self.runtime.accept("run", "r", "查课程")
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(self.runtime.close_session("session"), 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.gather(handle.task, return_exceptions=True)
        self.assertEqual(self.store.get_tool_result(handle.target, "pending")["status"], "cancelled")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM knowledge_refs").fetchone()[0], 0)
        self.assertEqual(handle.generated_text, "")

    async def test_two_call_limit_still_reserves_final_after_business_search(self):
        model = FakeModel([decision(call("search", "knowledge_search", {"query": "合成"}))])
        self.start(model, max_steps=2)
        handle = await self.runtime.accept("run", "r", "查课程")
        await self.collect(handle)
        self.assertIsNone(handle.failure_code)
        self.assertEqual([r["phase"] for r in model.requests], ["decision", "final"])
        self.assertEqual(self.store.get_tool_result(handle.target, "search")["status"], "success")

    async def test_large_business_observation_fits_next_request_and_preserves_final_capacity(self):
        value = payload("价格与否定条件为合成测试材料。" * 1500)
        value["data"]["result_list"] += [payload("另一份合成材料。" * 1000, point=str(i))["data"]["result_list"][0] for i in (2, 3)]
        self.handler = lambda _: httpx.Response(200, json=value)
        self.start(FakeModel([decision(call("large", "knowledge_search", {"query": "合成"})), prepare()]))
        handle = await self.runtime.accept("run", "r", "查资料")
        await self.collect(handle)
        self.assertIsNone(handle.failure_code)
        result = self.store.get_tool_result(handle.target, "large")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 12000)

    async def test_business_loop_uses_the_text_http_boundary_without_rtc(self):
        model = FakeModel([decision(call("search", "knowledge_search", {"query": "合成"})), prepare()], chunks=("文字业务答复。",))
        self.start(model)
        app = create_text_app(Settings(), self.profiles, self.root / "unused.db", runtime=self.runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3101") as client:
            headers = {"X-M0-Client-Key": app.state.client_key}
            created = await client.post("/api/m1/text/sessions", headers=headers, json={"profile_id": "pro"})
            session = created.json()["session_id"]
            response = await client.post(f"/api/m1/text/sessions/{session}/turns", headers=headers,
                                         json={"round_id": "r", "text": "查合成课程"})
            parsed = events(response)
            self.assertEqual("".join(p["text"] for e, p in parsed if e == "delta"), "文字业务答复。")
            self.assertEqual(parsed[-1][1]["state"], "generation_completed")
            self.assertEqual((await client.post(f"/api/m1/text/sessions/{session}/stop", headers=headers, json={})).status_code, 200)
        await app.state.text_sessions.close()
