"""Read-only Viking knowledge search; independent of model and RTC credentials.

Only the selected main model supplies a query. This adapter does no intent
classification, query rewriting, generation, retries, or automatic fallback.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
from typing import Callable, Protocol
from urllib.parse import urlsplit

from dotenv import dotenv_values
import httpx


SEARCH_PATH = "/api/knowledge/collection/search_knowledge"
MAX_RESPONSE_BYTES = 262144
MAX_QUERY_CHARS = 500
MAX_HITS = 5


class KnowledgeError(ValueError):
    """Stable diagnostics only; never echo provider bodies, URLs or keys."""

    def __init__(self, code: str, *, http_status: int | None = None):
        self.code, self.http_status = code, http_status
        super().__init__(code)


@dataclass(frozen=True)
class KnowledgeConfig:
    base_url: str = field(repr=False)
    collection: str = field(repr=False)
    api_key: str = field(repr=False)
    project: str = field(default="default", repr=False)
    limit: int = 3
    timeout_seconds: float = 1.5
    adapter: str = "viking_knowledge"
    search_path: str = SEARCH_PATH

    def __post_init__(self):
        try:
            parsed = urlsplit(self.base_url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                    or parsed.port not in {None, 443}):
                raise ValueError()
            if any(c.isspace() for c in self.base_url):
                raise ValueError()
        except (TypeError, ValueError):
            raise KnowledgeError("invalid_knowledge_endpoint") from None
        for value in (self.collection, self.project, self.api_key):
            if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
                raise KnowledgeError("invalid_knowledge_configuration")
        if len(self.collection) > 256 or len(self.project) > 128:
            raise KnowledgeError("invalid_knowledge_configuration")
        if (not isinstance(self.adapter, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.adapter)
                or not isinstance(self.search_path, str) or not self.search_path.startswith("/")
                or self.search_path.startswith("//") or len(self.search_path) > 256
                or any(c.isspace() or ord(c) < 32 or c in "?#\\" for c in self.search_path)):
            raise KnowledgeError("invalid_knowledge_adapter_configuration")
        if (isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= MAX_HITS
                or isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 1.5):
            raise KnowledgeError("invalid_knowledge_budget")

    @classmethod
    def load(cls, env_file: Path | None, *, environ=None) -> KnowledgeConfig | None:
        """Explicit separate file; environment wins, without mutating os.environ.

        A missing key disables the capability only when no file was requested.
        An explicitly requested but incomplete configuration is a boot error.
        """
        if env_file is not None and not env_file.is_file():
            raise KnowledgeError("knowledge_environment_file_missing")
        values = {**(dotenv_values(env_file, interpolate=False) if env_file else {}),
                  **(os.environ if environ is None else environ)}
        def get(name, default=""):
            return str(values.get(name) or default).strip()
        key = get("KNOWLEDGE_BASE_API_KEY")
        if not key and env_file is None:
            return None
        domain = get("KNOWLEDGE_BASE_DOMAIN", "api-knowledgebase.mlp.cn-beijing.volces.com")
        base = domain if "://" in domain else "https://" + domain
        try:
            return cls(base.rstrip("/"), get("KNOWLEDGE_BASE_COLLECTION", "rtc_ai"), key,
                       get("KNOWLEDGE_BASE_PROJECT", "default"),
                       int(get("M3_KNOWLEDGE_LIMIT", "3")),
                       float(get("M3_KNOWLEDGE_TIMEOUT_SECONDS", "1.5")))
        except (TypeError, ValueError) as exc:
            if isinstance(exc, KnowledgeError):
                raise
            raise KnowledgeError("invalid_knowledge_configuration") from None

    def inventory(self):
        return {"enabled": True, "adapter": self.adapter, "effect_type": "read_only",
                "https": True, "limit": self.limit, "timeout_ms": round(self.timeout_seconds * 1000),
                "automatic_retries": 0, "provider_rewrite": False, "provider_rerank": False}

    @classmethod
    def load_file(cls, config_file: Path, env_file: Path | None = None, *, environ=None) -> KnowledgeConfig:
        """Endpoint/resource/adapter in JSON; only the referenced key in env."""
        try:
            if config_file.stat().st_size > 16384:
                raise ValueError()
            data = json.loads(config_file.read_text("utf-8"))
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise KnowledgeError("invalid_knowledge_config_file") from None
        if (not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1
                or set(data) - {"schema_version", "adapter", "base_url", "search_path", "api_key_env", "resource", "limits"}):
            raise KnowledgeError("invalid_knowledge_config_file")
        resource, limits, key_env = data.get("resource"), data.get("limits", {}), data.get("api_key_env")
        if (not isinstance(resource, dict) or set(resource) - {"collection", "project"}
                or not isinstance(limits, dict) or set(limits) - {"max_hits", "timeout_ms"}
                or not isinstance(key_env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", key_env)):
            raise KnowledgeError("invalid_knowledge_config_file")
        adapter = data.get("adapter")
        if not isinstance(adapter, str) or adapter not in _ADAPTERS:
            raise KnowledgeError("unknown_knowledge_adapter")
        if env_file is not None and not env_file.is_file():
            raise KnowledgeError("knowledge_environment_file_missing")
        values = {**(dotenv_values(env_file, interpolate=False) if env_file else {}),
                  **(os.environ if environ is None else environ)}
        timeout = limits.get("timeout_ms", 1500)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise KnowledgeError("invalid_knowledge_budget")
        return cls(base_url=data.get("base_url"), collection=resource.get("collection"),
            project=resource.get("project", "default"), api_key=str(values.get(key_env) or "").strip(),
            limit=limits.get("max_hits", 3), timeout_seconds=timeout / 1000,
            adapter=adapter, search_path=data.get("search_path", SEARCH_PATH))


@dataclass(frozen=True)
class KnowledgeHit:
    text: str = field(repr=False)
    source_id: str = field(repr=False)
    point_id: str | None = field(default=None, repr=False)
    title: str | None = field(default=None, repr=False)
    score: float | None = None


class KnowledgeBackend(Protocol):
    """Stable tool-facing port. New providers must return these bounded hits."""
    config: KnowledgeConfig

    async def search(self, query: str, limit: int) -> list[KnowledgeHit]: ...

    async def aclose(self) -> None: ...


class VikingKnowledgeSearch:
    def __init__(self, config: KnowledgeConfig, *, client: httpx.AsyncClient | None = None):
        self.config = config
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False,
            timeout=config.timeout_seconds, limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()

    async def search(self, query: str, limit: int) -> list[KnowledgeHit]:
        if (not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.config.limit):
            raise KnowledgeError("invalid_tool_arguments")
        body = {"project": self.config.project, "name": self.config.collection,
                "query": query.strip(), "limit": limit,
                "pre_processing": {"rewrite": False, "need_instruction": False},
                "post_processing": {"rerank_switch": False, "get_attachment_link": False}}
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                async with self.client.stream("POST", self.config.base_url.rstrip("/") + self.config.search_path,
                        headers={"Authorization": "Bearer " + self.config.api_key, "Accept": "application/json"},
                        json=body, timeout=self.config.timeout_seconds, follow_redirects=False) as response:
                    if response.status_code != 200:
                        raise KnowledgeError("knowledge_http_error", http_status=response.status_code)
                    raw = bytearray()
                    async for part in response.aiter_bytes(chunk_size=16384):
                        if len(raw) + len(part) > MAX_RESPONSE_BYTES:
                            raise KnowledgeError("knowledge_response_limit")
                        raw.extend(part)
            payload = json.loads(raw)
        except KnowledgeError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise KnowledgeError("knowledge_timeout") from None
        except httpx.HTTPError:
            raise KnowledgeError("knowledge_transport_error") from None
        except (ValueError, UnicodeError, RecursionError):
            raise KnowledgeError("knowledge_invalid_response") from None
        return self._parse(payload, limit)

    @staticmethod
    def _parse(payload, limit):
        if not isinstance(payload, dict) or type(payload.get("code")) not in {int, str}:
            raise KnowledgeError("knowledge_invalid_response")
        if payload["code"] not in (0, "0"):
            raise KnowledgeError("knowledge_provider_error")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("result_list"), list):
            raise KnowledgeError("knowledge_invalid_response")
        rows = data["result_list"]
        if len(rows) > limit:
            raise KnowledgeError("knowledge_result_count_exceeded")
        hits, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise KnowledgeError("knowledge_invalid_response")
            text = row.get("content")
            doc = row.get("doc_info") or {}
            if not isinstance(text, str) or not text.strip() or not isinstance(doc, dict):
                raise KnowledgeError("knowledge_invalid_response")
            point_id = row.get("point_id", row.get("id"))
            source = doc.get("doc_id") or point_id
            if not isinstance(source, str) or not source or len(source) > 256 or any(ord(c) < 32 for c in source):
                raise KnowledgeError("knowledge_source_missing")
            if point_id is not None and (not isinstance(point_id, str) or len(point_id) > 256
                                         or any(ord(c) < 32 for c in point_id)):
                raise KnowledgeError("knowledge_invalid_response")
            score = row.get("score")
            if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score)):
                raise KnowledgeError("knowledge_invalid_response")
            title = doc.get("title") or doc.get("doc_name")
            if title is not None and not isinstance(title, str):
                raise KnowledgeError("knowledge_invalid_response")
            key = (source, point_id, text)
            if key in seen:
                continue
            seen.add(key)
            hits.append(KnowledgeHit(text, source, point_id, title[:200] if title else None, score))
        return hits


# Existing callers can still construct this provider explicitly. Runtime and
# tools depend only on KnowledgeBackend; normal startup uses the factory.
KnowledgeSearch = VikingKnowledgeSearch
_ADAPTERS: dict[str, Callable[[KnowledgeConfig], KnowledgeBackend]] = {"viking_knowledge": VikingKnowledgeSearch}


def register_knowledge_adapter(name: str, factory: Callable[[KnowledgeConfig], KnowledgeBackend]):
    """Application extension point, never callable by a model or request body."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or not callable(factory):
        raise KnowledgeError("invalid_knowledge_adapter_configuration")
    if name in _ADAPTERS:
        raise KnowledgeError("knowledge_adapter_already_registered")
    _ADAPTERS[name] = factory


def create_knowledge_backend(config: KnowledgeConfig) -> KnowledgeBackend:
    factory = _ADAPTERS.get(config.adapter)
    if factory is None:
        raise KnowledgeError("unknown_knowledge_adapter")
    return factory(config)
