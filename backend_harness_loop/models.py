"""Provider-neutral M1 chat-completions profiles and bounded streaming transport.

Provider endpoints refer to credential environment-variable names; model
profiles refer to providers. Neither a model name nor its key is hardcoded in
the agent runtime. Hidden provider reasoning is never a speech delta.
"""

from __future__ import annotations

import asyncio
import codecs
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
from typing import Any, AsyncIterator, Callable, Mapping
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values


MAX_FRAME_BYTES = 131_072
MAX_STREAM_BYTES = 2_097_152
MAX_REQUEST_BYTES = 1_048_576
MAX_TEXT_CHARS = 32_768
MAX_REASONING_CHARS = 65_536
MAX_TOOL_ARGUMENT_CHARS = 16_384
MAX_TOOLS = 8
_CONTROL_MARKERS = tuple("<" + pipe + "DSML" + pipe for pipe in ("｜", "｜｜", "|", "||"))
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_CORE_FIELDS = frozenset({"model", "messages", "tools", "tool_choice", "stream", "stream_options", "max_tokens", "max_completion_tokens", "n"})
_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")


class ModelError(RuntimeError):
    """Machine diagnostics only: never retain a provider body, key or prompt."""

    def __init__(self, code: str, *, status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class ModelConfigurationError(ModelError):
    pass


class ModelTransportError(ModelError):
    pass


class ModelProtocolError(ModelError):
    pass


class _FinalTextGuard:
    """Keep split native control markers out of speech without parsing tools."""
    def __init__(self):
        self.pending = ""

    def push(self, text):
        value = self.pending + text
        if any(marker in value for marker in _CONTROL_MARKERS):
            raise ModelProtocolError("model_control_text_in_final")
        for length in range(min(len(value), max(map(len, _CONTROL_MARKERS))-1), 0, -1):
            if any(value.endswith(marker[:length]) for marker in _CONTROL_MARKERS):
                self.pending = value[-length:]
                return value[:-length]
        self.pending = ""
        return value

    def finish(self):
        # A truncated reserved control prefix is not a successful answer.
        # Ordinary literal brackets remain intact (for example "a<b<").
        if re.match(r"^<[|｜]{1,2}D", self.pending):
            raise ModelProtocolError("model_control_text_in_final")
        value, self.pending = self.pending, ""
        return value


def load_model_environment(env_file: Path | None = None, overlay_file: Path | None = None) -> dict[str, str]:
    """Read explicitly selected dotenv files without changing os.environ."""
    result: dict[str, str] = {}
    for path in (env_file, overlay_file):
        if path is None:
            continue
        try:
            selected = Path(path)
            if not selected.is_file():
                raise OSError()
            result.update({name: str(value) for name, value in dotenv_values(selected, interpolate=False).items() if value is not None})
        except (OSError, UnicodeError, ValueError):
            raise ModelConfigurationError("model_environment_file_unreadable") from None
    result.update(os.environ)
    return result


def _endpoint(base: str) -> str:
    if not isinstance(base, str) or len(base) > 2048 or any(char.isspace() for char in base):
        raise ModelConfigurationError("invalid_model_base_url")
    try:
        parsed = urlsplit(base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or "?" in base or "#" in base:
            raise ValueError()
        # Force port validation, including malformed bracket/port syntax.
        _ = parsed.port
    except ValueError:
        raise ModelConfigurationError("invalid_model_base_url") from None
    base = base.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _bounded_integer(value: Any, lower: int, upper: int, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
        raise ModelConfigurationError(code)
    return value


def _extra_body(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) & _CORE_FIELDS:
        raise ModelConfigurationError("invalid_model_extra_body")
    try:
        if len(json.dumps(value, allow_nan=False).encode("utf-8")) > 16_384:
            raise ValueError()
    except (TypeError, ValueError, RecursionError):
        raise ModelConfigurationError("invalid_model_extra_body") from None
    # Credential values belong in environment variables, never the model body.
    def check(item, depth=0):
        if depth > 16:
            raise ModelConfigurationError("invalid_model_extra_body")
        if isinstance(item, dict):
            for key, child in item.items():
                if re.search(r"authorization|api.?key|password|secret|credential", str(key), re.I):
                    raise ModelConfigurationError("credentials_in_model_extra_body")
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
    check(value)
    return deepcopy(value)


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    provider_id: str
    base_url: str
    model: str
    api_key_env: str
    api_key: str = field(default="", repr=False)
    adapter: str = "openai_chat"
    extra_body: dict = field(default_factory=dict, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    max_tokens: int = 1024
    timeout_seconds: float = 15
    include_usage: bool = True
    decision_tool_choice: str = "auto"
    final_tool_schema: str = "omit"

    @property
    def name(self) -> str:
        return self.profile_id

    @property
    def endpoint(self) -> str:
        return _endpoint(self.base_url)

    def validate_ready(self) -> None:
        _endpoint(self.base_url)
        if self.adapter != "openai_chat":
            raise ModelConfigurationError("unsupported_model_adapter")
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ModelConfigurationError("model_api_key_missing")
        if "\r" in self.api_key or "\n" in self.api_key:
            raise ModelConfigurationError("invalid_model_api_key")
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 200:
            raise ModelConfigurationError("invalid_model_identifier")
        _bounded_integer(self.max_tokens, 1, 8192, "invalid_model_max_tokens")
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(self.timeout_seconds, bool) or not 0 < self.timeout_seconds <= 120 or not math.isfinite(self.timeout_seconds):
            raise ModelConfigurationError("invalid_model_timeout")
        if not isinstance(self.include_usage, bool):
            raise ModelConfigurationError("invalid_model_include_usage")
        if not isinstance(self.decision_tool_choice, str) or self.decision_tool_choice not in {"auto", "required"}:
            raise ModelConfigurationError("invalid_model_decision_tool_choice")
        if not isinstance(self.final_tool_schema, str) or self.final_tool_schema not in {"omit", "empty"}:
            raise ModelConfigurationError("invalid_model_final_tool_schema")

    def public_inventory(self) -> dict:
        return {"id": self.profile_id, "provider_id": self.provider_id, "adapter": self.adapter,
                "name": self.name, "base_url": self.base_url, "model": self.model, "api_key_env": self.api_key_env,
                "configured": bool(self.api_key.strip()), "max_tokens": self.max_tokens,
                "timeout_seconds": self.timeout_seconds, "decision_tool_choice": self.decision_tool_choice,
                "final_tool_schema": self.final_tool_schema}


class ModelProfiles:
    def __init__(self, profiles: Mapping[str, ModelProfile], default_profile_id: str):
        self._profiles = dict(profiles)
        if default_profile_id not in self._profiles:
            raise ModelConfigurationError("unknown_default_model_profile")
        self.default_profile_id = default_profile_id

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def get(self, profile_id: str | None = None) -> ModelProfile:
        selected = self.default_profile_id if profile_id is None else profile_id
        if not isinstance(selected, str) or selected not in self._profiles:
            raise ModelConfigurationError("unknown_model_profile")
        return self._profiles[selected]

    def public_inventory(self) -> dict:
        return {"default_profile": self.default_profile_id, "profiles": [profile.public_inventory() for profile in self._profiles.values()]}

    def secret_values(self) -> tuple[str, ...]:
        return tuple({value for profile in self._profiles.values() for value in (profile.api_key, *profile.headers.values()) if value})

    @classmethod
    def load(cls, path: Path, environ: Mapping[str, str] | None = None) -> ModelProfiles:
        values = os.environ if environ is None else environ
        try:
            source = Path(path)
            if source.stat().st_size > 262_144:
                raise ValueError()
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            data = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=unique)
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
            raise ModelConfigurationError("model_profiles_file_invalid") from None
        if not isinstance(data, dict) or set(data) != {"schema_version", "default_profile", "providers", "profiles"} or type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ModelConfigurationError("model_profiles_schema_invalid")
        providers, definitions = data["providers"], data["profiles"]
        if not isinstance(providers, dict) or not 1 <= len(providers) <= 16 or not isinstance(definitions, dict) or not 1 <= len(definitions) <= 32:
            raise ModelConfigurationError("model_profiles_schema_invalid")
        selected_providers = {}
        for provider_id, provider in providers.items():
            if not isinstance(provider_id, str) or not _IDENTIFIER.fullmatch(provider_id) or not isinstance(provider, dict) or set(provider) - {"base_url", "api_key_env", "adapter", "headers_env"}:
                raise ModelConfigurationError("invalid_model_provider")
            base_url, key_env = provider.get("base_url"), provider.get("api_key_env")
            _endpoint(base_url)
            if not isinstance(key_env, str) or not _ENV_NAME.fullmatch(key_env) or provider.get("adapter", "openai_chat") != "openai_chat":
                raise ModelConfigurationError("invalid_model_provider")
            header_vars = provider.get("headers_env", {})
            if not isinstance(header_vars, dict) or len(header_vars) > 8:
                raise ModelConfigurationError("invalid_model_provider_headers")
            headers = {}
            for header, variable in header_vars.items():
                if (not isinstance(header, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", header)
                    or header.lower() in {"authorization", "host", "content-length", "content-type"}
                    or not isinstance(variable, str) or not _ENV_NAME.fullmatch(variable)):
                    raise ModelConfigurationError("invalid_model_provider_headers")
                value = str(values.get(variable) or "").strip()
                if "\r" in value or "\n" in value:
                    raise ModelConfigurationError("invalid_model_provider_headers")
                if value:
                    headers[header] = value
            key = str(values.get(key_env) or "").strip()
            if "\r" in key or "\n" in key:
                raise ModelConfigurationError("invalid_model_api_key")
            selected_providers[provider_id] = (base_url, key_env, key, headers)
        profiles = {}
        for profile_id, definition in definitions.items():
            if not isinstance(profile_id, str) or not _IDENTIFIER.fullmatch(profile_id) or not isinstance(definition, dict) or set(definition) - {"provider", "model", "extra_body", "max_tokens", "timeout_seconds", "include_usage", "decision_tool_choice", "final_tool_schema"}:
                raise ModelConfigurationError("invalid_model_profile")
            provider_id = definition.get("provider")
            if not isinstance(provider_id, str) or provider_id not in selected_providers:
                raise ModelConfigurationError("unknown_model_provider")
            model = definition.get("model")
            if not isinstance(model, str) or not model.strip() or len(model) > 200:
                raise ModelConfigurationError("invalid_model_identifier")
            maximum = _bounded_integer(definition.get("max_tokens", 1024), 1, 8192, "invalid_model_max_tokens")
            timeout = definition.get("timeout_seconds", 15)
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not .1 <= timeout <= 120 or not math.isfinite(timeout):
                raise ModelConfigurationError("invalid_model_timeout")
            include_usage = definition.get("include_usage", True)
            if not isinstance(include_usage, bool):
                raise ModelConfigurationError("invalid_model_include_usage")
            decision_choice = definition.get("decision_tool_choice", "auto")
            if not isinstance(decision_choice, str) or decision_choice not in {"auto", "required"}:
                raise ModelConfigurationError("invalid_model_decision_tool_choice")
            final_schema = definition.get("final_tool_schema", "omit")
            if not isinstance(final_schema, str) or final_schema not in {"omit", "empty"}:
                raise ModelConfigurationError("invalid_model_final_tool_schema")
            base, key_env, key, headers = selected_providers[provider_id]
            profiles[profile_id] = ModelProfile(profile_id, provider_id, base, model.strip(), key_env, key,
                                                extra_body=_extra_body(definition.get("extra_body", {})),
                                                headers=headers, max_tokens=maximum,
                                                timeout_seconds=float(timeout), include_usage=include_usage, decision_tool_choice=decision_choice,
                                                final_tool_schema=final_schema)
        default = data["default_profile"]
        if not isinstance(default, str):
            raise ModelConfigurationError("unknown_default_model_profile")
        return cls(profiles, default)


@dataclass
class ModelResponse:
    content: str = field(default="", repr=False)
    tool_calls: list[dict[str, Any]] = field(default_factory=list, repr=False)
    usage: dict[str, int] = field(default_factory=dict)
    assistant_message: dict[str, Any] = field(default_factory=dict, repr=False)
    finish_reason: str | None = None
    reported_model: str | None = None


async def _frames(response: httpx.Response) -> AsyncIterator[dict | str]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    data: list[str] = []
    frame_size = total = 0

    def consume(line: str):
        nonlocal frame_size
        if not line:
            if not data:
                return None
            payload = "\n".join(data)
            data.clear()
            frame_size = 0
            if payload == "[DONE]":
                return "[DONE]"
            try:
                value = json.loads(payload)
            except (ValueError, TypeError, RecursionError):
                raise ModelProtocolError("invalid_model_stream_json") from None
            if not isinstance(value, dict):
                raise ModelProtocolError("invalid_model_stream_object")
            if value.get("error"):
                raise ModelProtocolError("model_stream_error")
            return value
        if line.startswith("data:"):
            value = line[5:]
            value = value[1:] if value.startswith(" ") else value
            frame_size += len(value.encode("utf-8"))
            if frame_size > MAX_FRAME_BYTES:
                raise ModelProtocolError("model_stream_frame_too_large")
            data.append(value)
        return None

    try:
        async for raw in response.aiter_bytes():
            total += len(raw)
            if total > MAX_STREAM_BYTES:
                raise ModelProtocolError("model_stream_too_large")
            buffer += decoder.decode(raw)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                value = consume(line.removesuffix("\r"))
                if value is not None:
                    yield value
                    if value == "[DONE]":
                        return
            if len(buffer.encode("utf-8")) > MAX_FRAME_BYTES:
                raise ModelProtocolError("model_stream_frame_too_large")
        buffer += decoder.decode(b"", final=True)
        if buffer:
            value = consume(buffer.removesuffix("\r"))
            if value is not None:
                yield value
        value = consume("")
        if value is not None:
            yield value
    except UnicodeError:
        raise ModelProtocolError("invalid_model_stream_encoding") from None


def _usage(frame: dict) -> dict[str, int]:
    value = frame.get("usage")
    return {key: value[key] for key in _USAGE_FIELDS
            if isinstance(value, dict) and isinstance(value.get(key), int) and not isinstance(value[key], bool) and 0 <= value[key] <= 10**9}


def _delta(frame: dict) -> tuple[dict, str | None]:
    choices = frame.get("choices")
    if choices is None:
        choices = []
    if not isinstance(choices, list) or len(choices) > 1:
        raise ModelProtocolError("unexpected_model_choices")
    if not choices:
        return {}, None
    choice = choices[0]
    if not isinstance(choice, dict) or type(choice.get("index", 0)) is not int or choice.get("index", 0) != 0:
        raise ModelProtocolError("unexpected_model_choice")
    delta = choice.get("delta")
    if delta is None:
        delta = {}
    if not isinstance(delta, dict):
        raise ModelProtocolError("invalid_model_delta")
    reason = choice.get("finish_reason")
    if reason is not None and not isinstance(reason, str):
        raise ModelProtocolError("invalid_model_finish_reason")
    return delta, reason


class ModelClientPool:
    """Reuse transport only; model usage/stream/decision state stays per lease."""
    def __init__(self, *, trust_env=False, client_factory=None):
        self.trust_env = trust_env
        self.client_factory = client_factory or (lambda profile: httpx.AsyncClient(
            timeout=profile.timeout_seconds, follow_redirects=False, trust_env=trust_env))
        self.clients = {}
        self.closed = False

    def model(self, profile):
        if self.closed:
            raise ModelConfigurationError("model_client_pool_closed")
        profile.validate_ready()
        cached = self.clients.get(profile.profile_id)
        if cached is not None and cached[0] != profile:
            raise ModelConfigurationError("model_profile_changed_during_runtime")
        if cached is None:
            cached = (profile, self.client_factory(profile))
            self.clients[profile.profile_id] = cached
        return ChatCompletionsModel(profile, client=cached[1])

    async def aclose(self):
        if not self.closed:
            self.closed = True
            await asyncio.gather(*(client.aclose() for _,client in self.clients.values()))


class ChatCompletionsModel:
    def __init__(self, profile: ModelProfile, client: httpx.AsyncClient | None = None, *, trust_env: bool = False):
        self.profile = profile
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=profile.timeout_seconds, follow_redirects=False, trust_env=trust_env)
        self.last_usage: dict[str, int] = {}

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    def _body(self, messages: list[dict], tools: list[dict] | None, tool_choice: str | dict, max_tokens: int | None) -> dict:
        self.profile.validate_ready()
        if not isinstance(messages, list) or not messages or len(messages) > 128 or not all(isinstance(item, dict) for item in messages):
            raise ModelProtocolError("invalid_model_messages")
        if tools is not None and (not isinstance(tools, list) or len(tools) > 32 or not all(isinstance(tool, dict) for tool in tools)):
            raise ModelProtocolError("invalid_model_tools")
        if not isinstance(tool_choice, (str, dict)) or isinstance(tool_choice, str) and tool_choice not in {"auto", "none", "required"}:
            raise ModelProtocolError("invalid_model_tool_choice")
        maximum = self.profile.max_tokens if max_tokens is None else _bounded_integer(max_tokens, 1, 8192, "invalid_model_max_tokens")
        body = {**_extra_body(self.profile.extra_body), "model": self.profile.model,
                "messages": deepcopy(messages), "stream": True, "max_tokens": maximum}
        if tools:
            body["tools"] = deepcopy(tools)
            body["tool_choice"] = deepcopy(tool_choice)
        elif tool_choice == "none":
            body["tool_choice"] = "none"
            if self.profile.final_tool_schema == "empty":
                body["tools"] = []
        if self.profile.include_usage:
            body["stream_options"] = {"include_usage": True}
        try:
            if len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_REQUEST_BYTES:
                raise ValueError()
        except (TypeError, ValueError, RecursionError):
            raise ModelProtocolError("invalid_or_oversized_model_request") from None
        return body

    async def _request_frames(self, body: dict) -> AsyncIterator[dict | str]:
        headers = {**self.profile.headers, "Authorization": "Bearer " + self.profile.api_key,
                   "Content-Type": "application/json", "Accept": "text/event-stream"}
        response = None
        frames = None
        deadline = asyncio.get_running_loop().time() + self.profile.timeout_seconds
        try:
            request = self._client.build_request("POST", self.profile.endpoint, headers=headers,
                                                json=body, timeout=self.profile.timeout_seconds)
            async with asyncio.timeout(self.profile.timeout_seconds):
                response = await self._client.send(request, stream=True, follow_redirects=False)
            if response.status_code != 200:
                raise ModelTransportError("model_http_error", status_code=response.status_code)
            frames = _frames(response)
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError()
                try:
                    # Never keep a cancellation timeout open across yield: it
                    # could otherwise cancel unrelated consumer/backpressure
                    # work while the HTTP generator is suspended.
                    async with asyncio.timeout(remaining):
                        frame = await anext(frames)
                except StopAsyncIteration:
                    break
                yield frame
        except (TimeoutError, httpx.TimeoutException):
            raise ModelTransportError("model_timeout") from None
        except httpx.HTTPError:
            raise ModelTransportError("model_network_error") from None
        finally:
            if frames is not None:
                await frames.aclose()
            if response is not None:
                await response.aclose()

    async def complete(self, messages: list[dict], tools: list[dict] | None = None,
                       tool_choice: str | dict = "auto", max_tokens: int | None = None) -> ModelResponse:
        body = self._body(messages, tools, tool_choice, max_tokens)
        response = ModelResponse()
        calls: dict[int, dict] = {}
        reasoning = ""
        done = False
        frames = self._request_frames(body)
        try:
            async for frame in frames:
                if frame == "[DONE]":
                    done = True
                    break
                response.usage.update(_usage(frame))
                reported = frame.get("model")
                if isinstance(reported, str) and len(reported) <= 200:
                    response.reported_model = reported
                delta, reason = _delta(frame)
                if response.finish_reason is not None and any(delta.get(name) for name in ("content", "reasoning_content", "tool_calls")):
                    raise ModelProtocolError("model_data_after_finish")
                if reason is not None:
                    if response.finish_reason is not None and response.finish_reason != reason:
                        raise ModelProtocolError("conflicting_model_finish_reason")
                    response.finish_reason = reason
                for name in ("content", "reasoning_content"):
                    piece = delta.get(name)
                    if piece is not None and not isinstance(piece, str):
                        raise ModelProtocolError("non_text_model_delta")
                    if name == "content":
                        response.content += piece or ""
                        if len(response.content) > MAX_TEXT_CHARS:
                            raise ModelProtocolError("model_text_too_large")
                    else:
                        reasoning += piece or ""
                        if len(reasoning) > MAX_REASONING_CHARS:
                            raise ModelProtocolError("model_reasoning_too_large")
                fragments = delta.get("tool_calls") or []
                if not isinstance(fragments, list):
                    raise ModelProtocolError("invalid_model_tool_fragments")
                for fragment in fragments:
                    if not isinstance(fragment, dict):
                        raise ModelProtocolError("invalid_model_tool_fragment")
                    index = fragment.get("index")
                    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < MAX_TOOLS:
                        raise ModelProtocolError("invalid_model_tool_index")
                    if fragment.get("type", "function") != "function":
                        raise ModelProtocolError("unsupported_model_tool_type")
                    call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    piece = fragment.get("id")
                    if piece is not None:
                        if not isinstance(piece, str):
                            raise ModelProtocolError("invalid_model_tool_id")
                        if piece != call["id"]:
                            call["id"] += piece
                        if len(call["id"]) > 256:
                            raise ModelProtocolError("model_tool_id_too_large")
                    function = fragment.get("function") or {}
                    if not isinstance(function, dict):
                        raise ModelProtocolError("invalid_model_tool_function")
                    for name in ("name", "arguments"):
                        piece = function.get(name)
                        if piece is not None:
                            if not isinstance(piece, str):
                                raise ModelProtocolError("invalid_model_tool_field")
                            if name != "name" or piece != call["function"][name]:
                                call["function"][name] += piece
                            bound = 80 if name == "name" else MAX_TOOL_ARGUMENT_CHARS
                            if len(call["function"][name]) > bound:
                                raise ModelProtocolError("model_tool_field_too_large")
        finally:
            await frames.aclose()
        if not done:
            raise ModelProtocolError("model_done_marker_missing")
        if response.finish_reason not in {"stop", "tool_calls"}:
            raise ModelProtocolError("incomplete_model_generation")
        if bool(calls) != (response.finish_reason == "tool_calls"):
            raise ModelProtocolError("model_tool_finish_mismatch")
        if list(sorted(calls)) != list(range(len(calls))):
            raise ModelProtocolError("model_tool_index_gap")
        response.tool_calls = [calls[index] for index in sorted(calls)]
        identifiers = set()
        for call in response.tool_calls:
            if not call["id"] or not call["function"]["name"] or not _IDENTIFIER.fullmatch(call["function"]["name"]) or call["id"] in identifiers:
                raise ModelProtocolError("incomplete_or_duplicate_model_tool")
            identifiers.add(call["id"])
        response.assistant_message = {"role": "assistant", "content": response.content or None}
        if response.tool_calls:
            response.assistant_message["tool_calls"] = deepcopy(response.tool_calls)
        # Some reasoning-enabled providers require this opaque field in later
        # tool requests. It stays in the native message, never in speech text.
        if reasoning:
            response.assistant_message["reasoning_content"] = reasoning
        self.last_usage = dict(response.usage)
        return response

    async def stream_final(self, messages: list[dict], max_tokens: int | None = None,
                           on_usage: Callable[[dict[str, int]], None] | None = None) -> AsyncIterator[str]:
        body = self._body(messages, None, "none", max_tokens)
        finish = None
        done = False
        count = 0
        visible = False
        usage = {}
        guard = _FinalTextGuard()
        frames = self._request_frames(body)
        try:
            async for frame in frames:
                if frame == "[DONE]":
                    done = True
                    break
                updates = _usage(frame)
                if updates:
                    usage.update(updates)
                    if on_usage is not None:
                        on_usage(dict(usage))
                delta, reason = _delta(frame)
                if "tool_calls" in delta and delta["tool_calls"] is not None and not isinstance(delta["tool_calls"], list):
                    raise ModelProtocolError("invalid_model_tool_fragments")
                if delta.get("tool_calls"):
                    raise ModelProtocolError("model_tools_in_final_phase")
                if "reasoning_content" in delta and delta["reasoning_content"] is not None and not isinstance(delta["reasoning_content"], str):
                    raise ModelProtocolError("non_text_model_delta")
                if delta.get("reasoning_content"):
                    raise ModelProtocolError("model_reasoning_in_final_phase")
                text = delta.get("content")
                if text is not None and not isinstance(text, str):
                    raise ModelProtocolError("non_text_model_delta")
                if finish is not None and text:
                    raise ModelProtocolError("model_data_after_finish")
                if reason is not None:
                    if finish is not None and finish != reason:
                        raise ModelProtocolError("conflicting_model_finish_reason")
                    finish = reason
                if text:
                    count += len(text)
                    visible = visible or bool(text.strip())
                    if count > MAX_TEXT_CHARS:
                        raise ModelProtocolError("model_text_too_large")
                    safe_text = guard.push(text)
                    if safe_text:
                        yield safe_text
        finally:
            await frames.aclose()
        self.last_usage = dict(usage)
        if not done:
            raise ModelProtocolError("model_done_marker_missing")
        if finish != "stop":
            raise ModelProtocolError("incomplete_model_generation")
        if not visible:
            raise ModelProtocolError("empty_model_final_response")
        pending = guard.finish()
        if pending:
            yield pending
