"""M3 read-only provider boundaries, provenance and conservative result budgets."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from backend_harness_loop.knowledge import (KnowledgeConfig, KnowledgeError, KnowledgeHit,
    KnowledgeSearch, MAX_RESPONSE_BYTES, SEARCH_PATH, create_knowledge_backend, register_knowledge_adapter)
from backend_harness_loop.config import Settings
from backend_harness_loop.reporting import write_report
from backend_harness_loop.store import RuntimeStore, StoreError
from backend_harness_loop.tools import ToolRegistry
from backend_harness_loop.tests.test_store_tools import call_message


CONFIG = KnowledgeConfig("https://knowledge.example", "fixture_collection", "fixture_private_key")


def payload(text="合成课程甲：费用为1200元，不提供就业保证。", *, source="fixture_doc", point="fixture_point"):
    return {"code": 0, "data": {"result_list": [{"content": text, "point_id": point,
        "score": .8, "doc_info": {"doc_id": source, "title": "合成资料"}}]}}


class KnowledgeConfigTests(unittest.TestCase):
    def config_file(self, folder, **overrides):
        value = {"schema_version": 1, "adapter": "viking_knowledge", "base_url": "https://knowledge.example",
                 "api_key_env": "SEPARATE_KNOWLEDGE_KEY", "resource": {"project": "new_project", "collection": "new_collection"}}
        value.update(overrides)
        path = Path(folder) / "knowledge.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_explicit_legacy_key_is_independent_and_defaults_to_https(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / "knowledge.env"
            path.write_text("KNOWLEDGE_BASE_API_KEY=fixture-file-key\nARK_API_KEY=unused-model-key\n", encoding="utf-8")
            config = KnowledgeConfig.load(path)
            self.assertTrue(config.base_url.startswith("https://"))
            self.assertEqual(config.api_key, "fixture-file-key")
            self.assertEqual(os.environ, {})
            self.assertNotIn(config.api_key, repr(config))
            self.assertNotIn(config.collection, json.dumps(config.inventory()))
            self.assertEqual(config.timeout_seconds, 1.5)
            overridden = KnowledgeConfig.load(path, environ={"KNOWLEDGE_BASE_API_KEY": "fixture-env-key"})
            self.assertEqual(overridden.api_key, "fixture-env-key")

    def test_unconfigured_capability_is_absent_and_explicit_errors_fail_closed(self):
        self.assertIsNone(KnowledgeConfig.load(None, environ={"ARK_API_KEY": "model-only"}))
        with self.assertRaisesRegex(KnowledgeError, "knowledge_environment_file_missing"):
            KnowledgeConfig.load(Path("missing-knowledge-fixture.env"), environ={})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "empty.env"
            path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(KnowledgeError, "invalid_knowledge_configuration"):
                KnowledgeConfig.load(path, environ={})

    def test_endpoint_credentials_and_expanded_budgets_are_rejected(self):
        for value in ("http://knowledge.example", "https://user:secret@knowledge.example",
                      "https://knowledge.example/?key=secret", "https://knowledge.example/path",
                      "https://knowledge.example:8080", "https://knowledge.example/#fragment"):
            with self.subTest(value=value), self.assertRaisesRegex(KnowledgeError, "invalid_knowledge_endpoint"):
                replace(CONFIG, base_url=value)
        for options in ({"limit": 6}, {"limit": True}, {"timeout_seconds": 2}, {"timeout_seconds": float("nan")}):
            with self.subTest(options=options), self.assertRaisesRegex(KnowledgeError, "invalid_knowledge_budget"):
                replace(CONFIG, **options)

    def test_json_resource_and_endpoint_are_separate_from_referenced_env_key(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.config_file(folder)
            env = Path(folder) / "keys.env"
            env.write_text("SEPARATE_KNOWLEDGE_KEY=knowledge-file-key\nARK_API_KEY=unused-model-key\nKNOWLEDGE_BASE_COLLECTION=ignored-old-resource\n", encoding="utf-8")
            config = KnowledgeConfig.load_file(path, env, environ={"SEPARATE_KNOWLEDGE_KEY": "knowledge-env-key"})
            self.assertEqual(config.collection, "new_collection")
            self.assertEqual(config.project, "new_project")
            self.assertEqual(config.api_key, "knowledge-env-key")
            self.assertNotIn(config.api_key, path.read_text("utf-8"))
            self.assertEqual(config.adapter, "viking_knowledge")

    def test_unknown_adapter_literal_credentials_and_missing_referenced_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            for overrides, code in (({"adapter": "not_registered"}, "unknown_knowledge_adapter"),
                                    ({"adapter": []}, "unknown_knowledge_adapter"),
                                    ({"api_key": "literal-key"}, "invalid_knowledge_config_file"),
                                    ({"api_key_env": "a literal secret"}, "invalid_knowledge_config_file"),
                                    ({}, "invalid_knowledge_configuration")):
                with self.subTest(code=code), self.assertRaisesRegex(KnowledgeError, code):
                    KnowledgeConfig.load_file(self.config_file(folder, **overrides), environ={"ARK_API_KEY": "model-key-only"})

    def test_reports_redact_new_knowledge_key_endpoint_and_resource(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            settings = Settings(knowledge_config=CONFIG)
            report = write_report(path, [{"status": "blocked", "evidence": [CONFIG.api_key, CONFIG.collection, CONFIG.base_url]}], settings, milestone="M3")
            for value in (CONFIG.api_key, CONFIG.collection, CONFIG.base_url):
                self.assertNotIn(value, path.read_text("utf-8"))
            self.assertEqual(report["results"][0]["evidence"], ["[REDACTED]"] * 3)
            self.assertFalse(report["m3_complete"])


class KnowledgeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, handler, query="合成课程甲", limit=3, config=CONFIG):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = KnowledgeSearch(config, client=client)
            try:
                return await service.search(query, limit)
            finally:
                await service.aclose()
                self.assertFalse(client.is_closed)  # Borrowed transport stays caller-owned.

    async def test_native_request_keeps_scope_and_disables_extra_model_processing(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=payload())
        hits = await self.request(handler)
        request = seen[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, SEARCH_PATH)
        self.assertEqual(request.headers["authorization"], "Bearer " + CONFIG.api_key)
        body = json.loads(request.content)
        self.assertEqual(body["name"], CONFIG.collection)
        self.assertEqual(body["project"], CONFIG.project)
        self.assertEqual(body["pre_processing"], {"rewrite": False, "need_instruction": False})
        self.assertEqual(body["post_processing"], {"rerank_switch": False, "get_attachment_link": False})
        self.assertNotIn("messages", body["pre_processing"])
        self.assertEqual(hits[0].source_id, "fixture_doc")
        self.assertNotIn(hits[0].text, repr(hits[0]))
        self.assertEqual(len(seen), 1)

    async def test_http_failures_and_redirects_are_not_empty_results_or_retried(self):
        for status in (302, 400, 401, 429, 500):
            requests = []
            def handler(request):
                requests.append(request)
                return httpx.Response(status, headers={"location": "https://other.example"},
                                      json={"message": "private body " + CONFIG.api_key})
            with self.subTest(status=status), self.assertRaises(KnowledgeError) as error:
                await self.request(handler)
            self.assertEqual(error.exception.code, "knowledge_http_error")
            self.assertEqual(error.exception.http_status, status)
            self.assertNotIn(CONFIG.api_key, str(error.exception))
            self.assertEqual(len(requests), 1)

    async def test_provider_error_and_invalid_json_have_distinct_codes(self):
        for response, code in (
            (httpx.Response(200, json={"code": 1000003, "message": "private"}), "knowledge_provider_error"),
            (httpx.Response(200, content=b"invalid json"), "knowledge_invalid_response"),
            (httpx.Response(200, json={"code": 0, "data": {}}), "knowledge_invalid_response"),
            (httpx.Response(200, json={"code": False, "data": {"result_list": []}}), "knowledge_invalid_response"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(KnowledgeError, code):
                await self.request(lambda request: response)

    async def test_empty_success_does_not_hide_malformed_hits(self):
        self.assertEqual(await self.request(lambda _: httpx.Response(200, json={"code": 0, "data": {"result_list": []}})), [])
        for value, code in ((payload(text=""), "knowledge_invalid_response"),
                            (payload(source=None, point=None), "knowledge_source_missing")):
            with self.subTest(code=code), self.assertRaisesRegex(KnowledgeError, code):
                await self.request(lambda _: httpx.Response(200, json=value))

    async def test_large_body_and_excess_hit_counts_are_bounded(self):
        with self.assertRaisesRegex(KnowledgeError, "knowledge_response_limit"):
            await self.request(lambda _: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)))
        value = payload()
        value["data"]["result_list"] *= 4
        with self.assertRaisesRegex(KnowledgeError, "knowledge_result_count_exceeded"):
            await self.request(lambda _: httpx.Response(200, json=value))

    async def test_invalid_arguments_never_dispatch(self):
        def fail(_):
            self.fail("invalid arguments reached provider")
        for query, limit in ((" ", 1), ("x" * 501, 1), ("ok", True), ("ok", 4)):
            with self.subTest(query=query[:10]), self.assertRaisesRegex(KnowledgeError, "invalid_tool_arguments"):
                await self.request(fail, query, limit)

    async def test_network_timeout_and_total_time_limit_are_observable(self):
        def timeout(request):
            raise httpx.ReadTimeout("private URL", request=request)
        with self.assertRaisesRegex(KnowledgeError, "knowledge_timeout"):
            await self.request(timeout)
        cancelled = asyncio.Event()
        async def slow(_):
            try:
                await asyncio.sleep(5)
            finally:
                cancelled.set()
        with self.assertRaisesRegex(KnowledgeError, "knowledge_timeout"):
            await self.request(slow, config=replace(CONFIG, timeout_seconds=.02))
        self.assertTrue(cancelled.is_set())


class KnowledgeToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = RuntimeStore(":memory:")
        self.store.create_session("session", "run", "flash")
        self.target = self.store.admit_turn("session", "round", "合成输入", "conn")
        self.requests = []
        def handler(request):
            self.requests.append(request)
            return httpx.Response(200, json=payload(('合成正文 "\\\n' * 1000)))
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.registry = ToolRegistry(self.store, KnowledgeSearch(CONFIG, client=self.client))

    async def asyncTearDown(self):
        await self.client.aclose()
        self.store.close()

    def prepared(self, call_id="search"):
        self.store.prepare_tool_calls(self.target, call_message((call_id, "knowledge_search", {"query": "合成"})))
        self.store.mark_tool_dispatched(self.target, call_id)

    async def test_disabled_registry_excludes_unavailable_business_capabilities(self):
        disabled = ToolRegistry(self.store)
        self.assertNotIn("knowledge_search", {item["function"]["name"] for item in disabled.schemas()})
        result = await disabled.execute("knowledge_search", {"query": "合成"}, self.target)
        self.assertEqual(result["error"]["code"], "unknown_tool")
        contract = self.registry.knowledge_contract()
        self.assertEqual(contract["effect_type"], "read_only")
        self.assertLessEqual(contract["timeout_ms"], 1500)

    async def test_alternative_adapter_reuses_same_tool_and_provenance_contract(self):
        class OtherBackend:
            def __init__(self, config):
                self.config = config
            async def search(self, query, limit):
                return [KnowledgeHit("另一服务的合成资料", "other_document")]
            async def aclose(self):
                pass
        with patch.dict("backend_harness_loop.knowledge._ADAPTERS"):
            register_knowledge_adapter("other_fixture", OtherBackend)
            with self.assertRaisesRegex(KnowledgeError, "knowledge_adapter_already_registered"):
                register_knowledge_adapter("other_fixture", OtherBackend)
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "other.json"
                path.write_text(json.dumps({"schema_version": 1, "adapter": "other_fixture", "base_url": "https://other.example",
                    "api_key_env": "OTHER_KEY", "resource": {"collection": "other_resource"}}), encoding="utf-8")
                backend = create_knowledge_backend(KnowledgeConfig.load_file(path, environ={"OTHER_KEY": "other-key"}))
            registry = ToolRegistry(self.store, backend)
            self.prepared()
            result = await registry.execute("knowledge_search", {"query": "合成"}, self.target, tool_call_id="search")
            self.assertEqual(result["data"]["hits"][0]["source_id"], "other_document")
            self.store.complete_tool_call(self.target, "search", result)
            self.assertTrue(self.store.knowledge_refs_exist(self.target, [result["data"]["hits"][0]["reference"]]))
            self.assertEqual(self.requests, [])

    async def test_result_preserves_source_and_fits_json_escaped_utf8_quota(self):
        result = await self.registry.execute("knowledge_search", {"query": "合成"}, self.target,
                                             tool_call_id="search", result_budget_bytes=800)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 800)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["truncated"])
        hit = result["data"]["hits"][0]
        self.assertTrue(hit["text_truncated"])
        self.assertEqual(hit["source_id"], "fixture_doc")
        self.assertRegex(hit["reference"], r"^kb:[a-f0-9]{32}$")
        self.assertNotIn(CONFIG.api_key, json.dumps(result))

    async def test_provenance_commits_with_result_and_is_session_scoped(self):
        self.prepared()
        result = await self.registry.execute("knowledge_search", {"query": "合成"}, self.target, tool_call_id="search")
        ref = result["data"]["hits"][0]["reference"]
        self.assertFalse(self.store.knowledge_refs_exist(self.target, [ref]))
        self.assertTrue(self.store.complete_tool_call(self.target, "search", result))
        self.assertTrue(self.store.knowledge_refs_exist(self.target, [ref]))
        self.assertTrue(self.store.complete_tool_call(self.target, "search", result))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM knowledge_refs").fetchone()[0], 1)
        self.assertFalse(self.store.knowledge_refs_exist(self.target, ["kb:" + "f" * 32]))
        self.store.create_session("other", "other_run", "pro")
        other = self.store.admit_turn("other", "r", "其它会话", "c")
        self.assertFalse(self.store.knowledge_refs_exist(other, [ref]))

    async def test_cancelled_source_and_late_result_cannot_supply_evidence(self):
        self.prepared()
        result = await self.registry.execute("knowledge_search", {"query": "合成"}, self.target, tool_call_id="search")
        ref = result["data"]["hits"][0]["reference"]
        self.store.cancel_round("session", "round", "test_interrupt")
        self.assertFalse(self.store.complete_tool_call(self.target, "search", result))
        next_turn = self.store.admit_turn("session", "new_round", "新输入", "new_conn")
        self.assertFalse(self.store.knowledge_refs_exist(next_turn, [ref]))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM knowledge_refs").fetchone()[0], 0)

    async def test_invalid_tool_arguments_never_reach_external_service(self):
        for args in ({"query": "合成", "collection": "other"}, {"query": ""}, {"query": "合成", "limit": True}):
            result = await self.registry.execute("knowledge_search", args, self.target)
            self.assertEqual(result["error"]["code"], "invalid_tool_arguments")
        self.assertEqual(self.requests, [])

    async def test_invalid_evidence_result_rolls_back_terminal_and_index(self):
        self.prepared()
        with self.assertRaisesRegex(StoreError, "invalid_knowledge_reference"):
            self.store.complete_tool_call(self.target, "search", {"status": "success", "data": {"hits": [{"reference": "invented"}]}})
        state = self.store.db.execute("SELECT state FROM tool_calls").fetchone()[0]
        self.assertEqual(state, "dispatched")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM knowledge_refs").fetchone()[0], 0)

    async def test_provenance_survives_sqlite_reopen_for_followup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "persistent.sqlite3"
            store = RuntimeStore(path)
            try:
                store.create_session("persisted", "persisted_run", "pro")
                first = store.admit_turn("persisted", "first", "合成", "c1")
                registry = ToolRegistry(store, self.registry.knowledge)
                store.prepare_tool_calls(first, call_message(("search", "knowledge_search", {"query": "合成"})))
                store.mark_tool_dispatched(first, "search")
                result = await registry.execute("knowledge_search", {"query": "合成"}, first, tool_call_id="search")
                reference = result["data"]["hits"][0]["reference"]
                store.complete_tool_call(first, "search", result)
                store.complete_turn(first, "generation_completed", "合成答复")
                store.close()
                store = RuntimeStore(path)
                second = store.admit_turn("persisted", "second", "它多少钱", "c2")
                self.assertTrue(store.knowledge_refs_exist(second, [reference]))
                self.assertEqual(store.get_tool_result(first, "search"), result)
            finally:
                store.close()

    async def test_success_from_a_subsequently_cancelled_turn_is_not_evidence(self):
        self.prepared()
        result = await self.registry.execute("knowledge_search", {"query": "合成"}, self.target, tool_call_id="search")
        self.store.complete_tool_call(self.target, "search", result)
        reference = result["data"]["hits"][0]["reference"]
        self.store.cancel_round("session", "round", "test_cancel_after_tool")
        second = self.store.admit_turn("session", "new_round", "新输入", "new_conn")
        self.assertFalse(self.store.knowledge_refs_exist(second, [reference]))
        # Audit remains intact, even though this evidence is ineligible.
        self.assertEqual(self.store.get_tool_result(self.target, "search")["status"], "success")
