"""Bounded M0 experiments for Ark's chat-completions streaming interface.

Only synthetic fixture data is submitted. Results deliberately contain neither
credentials, full prompts, response text, nor model reasoning fields.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx


class ProbeProtocolError(ValueError):
    """A safe, fixed diagnostic code, never a provider response body."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class StreamObservation:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    elapsed_ms: float = 0
    first_event_ms: float | None = None
    first_text_ms: float | None = None
    argument_fragment_counts: dict[int, int] = field(default_factory=dict)

    @property
    def mixed_text_and_tools(self) -> bool:
        return bool(self.content and self.tool_calls)


async def collect_stream(
    response: httpx.Response, *, started_at: float
) -> StreamObservation:
    """Collect SSE, joining function arguments by their native call index.

    HTTP byte fragmentation is handled by aiter_lines; SSE multiline data frames
    are handled here. Native tool messages remain distinct from visible text.
    """
    observation = StreamObservation()
    calls: dict[int, dict[str, Any]] = {}
    data_lines: list[str] = []
    done = False

    def consume(payload: str) -> None:
        nonlocal done
        if payload == "[DONE]":
            done = True
            return
        if len(payload) > 131072:
            raise ProbeProtocolError("oversized_stream_frame")
        try:
            value = json.loads(payload)
        except (ValueError, TypeError):
            raise ProbeProtocolError("invalid_stream_json") from None
        if not isinstance(value, dict):
            raise ProbeProtocolError("invalid_stream_object")
        if value.get("error"):
            raise ProbeProtocolError("provider_stream_error")
        if observation.first_event_ms is None:
            observation.first_event_ms = (time.perf_counter() - started_at) * 1000
        usage = value.get("usage")
        if isinstance(usage, dict):
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                count = usage.get(name)
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    observation.usage[name] = count
        choices = value.get("choices") or []
        if not isinstance(choices, list):
            raise ProbeProtocolError("invalid_choices")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise ProbeProtocolError("unexpected_choice")
            reason = choice.get("finish_reason")
            if reason is not None:
                if not isinstance(reason, str):
                    raise ProbeProtocolError("invalid_finish_reason")
                observation.finish_reason = reason
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise ProbeProtocolError("invalid_delta")
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ProbeProtocolError("non_text_content_delta")
                if content and observation.first_text_ms is None:
                    observation.first_text_ms = (time.perf_counter() - started_at) * 1000
                observation.content += content
                if len(observation.content) > 32768:
                    raise ProbeProtocolError("oversized_response_text")
            for fragment in delta.get("tool_calls") or []:
                if not isinstance(fragment, dict):
                    raise ProbeProtocolError("invalid_tool_fragment")
                index = fragment.get("index")
                if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 8:
                    raise ProbeProtocolError("invalid_tool_index")
                call = calls.setdefault(
                    index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                call_id = fragment.get("id")
                if call_id:
                    if not isinstance(call_id, str):
                        raise ProbeProtocolError("invalid_tool_id")
                    if call_id != call["id"]:
                        call["id"] += call_id
                function = fragment.get("function") or {}
                if not isinstance(function, dict):
                    raise ProbeProtocolError("invalid_function_delta")
                for key in ("name", "arguments"):
                    piece = function.get(key)
                    if piece is not None:
                        if not isinstance(piece, str):
                            raise ProbeProtocolError("invalid_function_field")
                        call["function"][key] += piece
                        if key == "arguments" and piece:
                            observation.argument_fragment_counts[index] = (
                                observation.argument_fragment_counts.get(index, 0) + 1
                            )
                        if len(call["function"][key]) > 16384:
                            raise ProbeProtocolError("oversized_tool_arguments")
            # Intentionally never consume reasoning_content or other hidden fields.

    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                consume("\n".join(data_lines))
                data_lines.clear()
            if done:
                break
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        # Comments and SSE transport metadata are not model text.
    if data_lines and not done:
        consume("\n".join(data_lines))
    if not done:
        raise ProbeProtocolError("missing_done_marker")
    observation.tool_calls = [calls[index] for index in sorted(calls)]
    for call in observation.tool_calls:
        if not call["id"] or not call["function"]["name"]:
            raise ProbeProtocolError("incomplete_tool_call")
    if observation.finish_reason not in {"stop", "tool_calls"}:
        raise ProbeProtocolError("incomplete_model_generation")
    if observation.tool_calls and observation.finish_reason != "tool_calls":
        raise ProbeProtocolError("tool_finish_reason_mismatch")
    if not observation.tool_calls and observation.finish_reason == "tool_calls":
        raise ProbeProtocolError("missing_tool_calls")
    observation.elapsed_ms = (time.perf_counter() - started_at) * 1000
    return observation


def visible_final_text(observation: StreamObservation) -> str:
    """Only a completed, tool-free final phase is eligible for speech."""
    if observation.tool_calls or observation.finish_reason != "stop":
        raise ProbeProtocolError("tool_or_control_output_in_final_phase")
    if not observation.content.strip():
        raise ProbeProtocolError("empty_final_response")
    return observation.content


FIXTURE_TOOL = {
    "type": "function",
    "function": {
        "name": "m0_lookup_fixture",
        "description": "Read a synthetic M0 test fixture, not a real course or business API.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "enum": ["fixture_course"]}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

PREPARE_TOOL = {
    "type": "function",
    "function": {
        "name": "prepare_response",
        "description": "Choose the final speech phase; this does not execute a business action.",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["answer", "clarify"]},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["mode", "evidence_refs"],
            "additionalProperties": False,
        },
    },
}


def _make_client(timeout_seconds: float, *, trust_env: bool = False) -> httpx.AsyncClient:
    # httpx has no automatic request retries by default. No redirect can forward a key.
    return httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False, trust_env=trust_env)


def _endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ProbeProtocolError("invalid_model_base_url")
    base = base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _arguments(call: dict[str, Any], expected_name: str) -> dict[str, Any]:
    if call["function"]["name"] != expected_name:
        raise ProbeProtocolError("unexpected_tool_name")
    try:
        args = json.loads(call["function"]["arguments"])
    except (ValueError, TypeError):
        raise ProbeProtocolError("invalid_tool_arguments") from None
    if not isinstance(args, dict):
        raise ProbeProtocolError("invalid_tool_arguments")
    return args


def _assistant_message(observation: StreamObservation) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": observation.content or None}
    if observation.tool_calls:
        message["tool_calls"] = observation.tool_calls
    return message


def _timing(observation: StreamObservation) -> dict[str, Any]:
    return {
        "elapsed_ms": round(observation.elapsed_ms, 2),
        "first_event_ms": (
            round(observation.first_event_ms, 2) if observation.first_event_ms is not None else None
        ),
        "first_text_ms": (
            round(observation.first_text_ms, 2) if observation.first_text_ms is not None else None
        ),
        "mixed_text_and_tools": observation.mixed_text_and_tools,
        "tool_argument_fragment_counts": list(observation.argument_fragment_counts.values()),
        "usage": observation.usage,
    }


def _latency_summary(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    nearest_rank = lambda fraction: ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]
    return {
        "sample_count": len(values),
        "p50_ms": round(nearest_rank(0.50), 2),
        "p95_ms": round(nearest_rank(0.95), 2),
        "method": "empirical_nearest_rank",
        "interpretation": "Small synthetic sample only; not a production P95 estimate.",
    }


async def _safe_http_error(response: httpx.Response) -> dict[str, str]:
    """Return only known machine diagnostics; never return provider messages."""
    payload = bytearray()
    async for chunk in response.aiter_bytes():
        if len(payload) + len(chunk) > 16384:
            return {}
        payload.extend(chunk)
    try:
        value = json.loads(payload)
    except (ValueError, TypeError):
        return {}
    error = value.get("error") if isinstance(value, dict) else None
    if not isinstance(error, dict):
        return {}
    permitted = {
        "code": {
            "invalid_request_error", "authentication_error", "rate_limit_error",
            "server_error", "InvalidParameter", "InvalidParameter.Unsupported",
            "MissingParameter", "AuthenticationError", "PermissionDenied",
            "RateLimitExceeded", "ModelNotFound", "InsufficientQuota",
        },
        "type": {
            "invalid_request_error", "authentication_error", "permission_error",
            "rate_limit_error", "server_error", "api_error", "not_found_error",
        },
        "param": {
            "thinking", "thinking.type", "tool_choice", "tools", "messages",
            "model", "max_tokens", "temperature", "stream", "stream_options",
        },
    }
    return {
        "provider_error_" + key: error[key]
        for key, allowed in permitted.items()
        if isinstance(error.get(key), str) and error[key] in allowed
    }


async def run_model_probe(settings: Any, *, samples: int = 3) -> dict[str, Any]:
    """Run at most eight model calls: native tool roundtrip + 1-3 speech samples."""
    missing = [
        name
        for name in ("ark_api_key", "ark_base_url", "ark_model")
        if not str(getattr(settings, name, "") or "").strip()
    ]
    if missing:
        return {
            "name": "model",
            "status": "blocked",
            "summary": "Model credentials or endpoint are not configured.",
            "evidence": {"missing_settings": missing, "request_count": 0},
        }
    evidence: dict[str, Any] = {
        "fixture": "Synthetic local M0 data; no real course or business tool used.",
        "request_count": 0,
        "requested_samples": samples,
        "requests": [],
        "single_stage_gate": {
            "status": "blocked",
            "enabled": False,
            "reason": "Observed samples cannot establish an early text/tool branch commitment contract.",
        },
    }
    try:
        if not isinstance(samples, int) or isinstance(samples, bool) or not 1 <= samples <= 3:
            raise ProbeProtocolError("samples_must_be_between_one_and_three")
        timeout = float(settings.model_timeout_seconds)
        deadline_ms = float(settings.first_output_deadline_ms)
        thinking = getattr(settings, "model_thinking", "omit")
        if thinking not in {"disabled", "enabled", "omit"}:
            raise ProbeProtocolError("invalid_model_thinking_configuration")
        evidence["thinking"] = thinking
        if not 0 < timeout <= 60 or deadline_ms <= 0:
            raise ProbeProtocolError("invalid_model_probe_budget")
        endpoint = _endpoint(settings.ark_base_url)
        latencies: list[float] = []
        prepare_count = 0

        async with _make_client(timeout, trust_env=bool(getattr(settings, "trust_env", False))) as client:

            async def request(
                messages: list[dict[str, Any]],
                label: str,
                *,
                tools: list[dict[str, Any]] | None = None,
                tool_choice: Any = None,
            ) -> StreamObservation:
                if evidence["request_count"] >= 8:
                    raise ProbeProtocolError("request_budget_exhausted")
                body: dict[str, Any] = {
                    "model": settings.ark_model,
                    "messages": messages,
                    "temperature": 0,
                    "stream": True,
                    "max_tokens": 256,
                    "stream_options": {"include_usage": True},
                }
                if tools:
                    body["tools"] = tools
                    body["tool_choice"] = tool_choice or "auto"
                if thinking != "omit":
                    body["thinking"] = {"type": thinking}
                evidence["request_count"] += 1
                started = time.perf_counter()
                async with asyncio.timeout(timeout):
                    async with client.stream(
                        "POST",
                        endpoint,
                        headers={"Authorization": "Bearer " + settings.ark_api_key},
                        json=body,
                    ) as response:
                        if response.status_code != 200:
                            evidence["http_status"] = response.status_code
                            evidence.update(await _safe_http_error(response))
                            raise ProbeProtocolError("model_http_error")
                        if "text/event-stream" not in response.headers.get("content-type", ""):
                            raise ProbeProtocolError("non_sse_model_response")
                        observed = await collect_stream(response, started_at=started)
                evidence["requests"].append({"label": label, **_timing(observed)})
                return observed

            fixture_messages: list[dict[str, Any]] = [
                {"role": "system", "content": "This is a synthetic M0 protocol experiment. Never use real business data."},
                {"role": "user", "content": "Call m0_lookup_fixture once with query fixture_course. Then return the verification_token from its result verbatim."},
            ]
            fixture_decision = await request(
                fixture_messages,
                "native_tool_decision",
                tools=[FIXTURE_TOOL],
                tool_choice={"type": "function", "function": {"name": "m0_lookup_fixture"}},
            )
            if len(fixture_decision.tool_calls) != 1:
                raise ProbeProtocolError("expected_one_fixture_tool_call")
            fixture_call = fixture_decision.tool_calls[0]
            if _arguments(fixture_call, "m0_lookup_fixture") != {"query": "fixture_course"}:
                raise ProbeProtocolError("unexpected_fixture_arguments")
            fixture_messages.extend([
                _assistant_message(fixture_decision),
                {
                    "role": "tool",
                    "tool_call_id": fixture_call["id"],
                    "content": json.dumps({"synthetic": True, "verification_token": "M0_RESULT_42"}),
                },
            ])
            fixture_final = await request(fixture_messages, "native_tool_result_continuation")
            if "M0_RESULT_42" not in visible_final_text(fixture_final):
                raise ProbeProtocolError("tool_result_not_used")
            evidence["native_tool_roundtrip"] = {
                "passed": True,
                "argument_fragments_observed": fixture_decision.argument_fragment_counts.get(0, 0),
                "multi_fragment_observed": fixture_decision.argument_fragment_counts.get(0, 0) > 1,
                "result_token_verified": True,
            }

            for sample in range(samples):
                started = time.perf_counter()
                user_message = {"role": "user", "content": "请用中文简短地打个招呼。"}
                decision = await request(
                    [
                        {"role": "system", "content": "You are the RTC decision stage. Choose prepare_response with mode answer and empty evidence_refs to answer this greeting. Any text is an internal draft and will not be spoken."},
                        user_message,
                    ],
                    f"speech_{sample + 1}_decision",
                    tools=[PREPARE_TOOL],
                )
                final_messages: list[dict[str, Any]] = [
                    {"role": "system", "content": "You are the final RTC speech stage. Tools are disabled. Reply directly to the user in brief natural Chinese. Do not include internal decisions, tool JSON, or analysis."},
                    user_message,
                    _assistant_message(decision),
                ]
                if decision.tool_calls:
                    if len(decision.tool_calls) != 1:
                        raise ProbeProtocolError("expected_one_prepare_response")
                    call = decision.tool_calls[0]
                    args = _arguments(call, "prepare_response")
                    if args.get("mode") != "answer" or args.get("evidence_refs") != [] or set(args) != {"mode", "evidence_refs"}:
                        raise ProbeProtocolError("invalid_prepare_response_arguments")
                    prepare_count += 1
                    final_messages.append({"role": "tool", "tool_call_id": call["id"], "content": '{"phase":"final","tools_enabled":false}'})
                elif not decision.content.strip():
                    raise ProbeProtocolError("empty_decision")
                final = await request(final_messages, f"speech_{sample + 1}_final")
                visible_final_text(final)
                if final.first_text_ms is None:
                    raise ProbeProtocolError("missing_final_text_timing")
                # First final text is measured from the beginning of both phases.
                final_finished = time.perf_counter()
                latencies.append((final_finished - started) * 1000 - final.elapsed_ms + final.first_text_ms)

        evidence["two_stage"] = {
            "passed": True,
            "prepare_response_samples": prepare_count,
            "draft_fallback_samples": samples - prepare_count,
            "final_tools_disabled": True,
            "decision_text_forwarded_to_speech": False,
            "first_text_latency": _latency_summary(latencies),
            "first_output_deadline_ms": deadline_ms,
            "samples_within_deadline": sum(value <= deadline_ms for value in latencies),
            "latency_gate_passed": all(value <= deadline_ms for value in latencies),
            "measurement_boundary": "Backend first eligible final text; excludes RTC ASR, TTS, network playback and actual first audio.",
        }
        evidence["usage_total"] = {
            key: sum(entry["usage"].get(key, 0) for entry in evidence["requests"])
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        evidence["usage_requests_reported"] = sum(
            "total_tokens" in entry["usage"] for entry in evidence["requests"]
        )
        evidence["usage_scope"] = (
            "Sum of available provider usage fields; missing usage is not zero billing."
        )
        if not all(value <= deadline_ms for value in latencies):
            return {"name": "model", "status": "failed", "summary": "Synthetic protocol samples passed, but the configured backend first-text deadline was exceeded.", "evidence": evidence}
        return {"name": "model", "status": "passed", "summary": "Bounded synthetic native-tool and two-stage streaming samples passed; single-stage and RTC first-audio gates remain unverified.", "evidence": evidence}
    except ProbeProtocolError as exc:
        evidence["error_code"] = exc.code
    except (asyncio.TimeoutError, httpx.TimeoutException):
        evidence["error_code"] = "model_request_timeout"
    except httpx.HTTPError as exc:
        evidence["error_code"] = "model_transport_error"
        evidence["error_type"] = type(exc).__name__
    except (TypeError, ValueError, AttributeError):
        evidence["error_code"] = "invalid_probe_configuration"
    return {"name": "model", "status": "failed", "summary": "The bounded model experiment did not pass; see the sanitized diagnostic code.", "evidence": evidence}
