"""Bounded RTC OpenAPI probes; never target an existing room or task.

Protocol references (checked 2026-11-08):
https://docs.volcengine.com/docs/real-time-communication/how-to-call-openapi-1
https://docs.volcengine.com/docs/real-time-communication/stop-voice-chat
https://docs.volcengine.com/docs/real-time-communication/configure-asr
https://docs.volcengine.com/docs/real-time-communication/configure-tts
https://docs.volcengine.com/docs/real-time-communication/start-voice-chat-ai-conversation-archived?lang=zh

A successful OpenAPI request does NOT prove RTC audio/callback correlation.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import re
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
import uuid

import httpx


OPENAPI_URL = "https://rtc.volcengineapi.com"
API_VERSION = "2025-06-01"
SUPPORTED_API_VERSIONS = {"2024-12-01", API_VERSION}
DEFAULT_VOICE = "zh_female_vv_uranus_bigtts"


class RTCProbeConfigurationError(ValueError):
    """Safe configuration failure: field identifiers only, never their values."""

    def __init__(self, code: str, fields: list[str]):
        super().__init__(code)
        self.code = code
        self.fields = fields


def _voice_config(settings: Any) -> dict[str, Any]:
    version = str(_setting(settings, "rtc_api_version", API_VERSION))
    if version == "2024-12-01":
        profile = _setting(settings, "rtc_legacy_voice_config", None)
        if not isinstance(profile, dict):
            raise RTCProbeConfigurationError("missing_config", ["rtc_legacy_voice_config"])
        if set(profile) != {"ASRConfig", "TTSConfig"}:
            raise RTCProbeConfigurationError("invalid_legacy_voice_config", ["ASRConfig", "TTSConfig"])
        invalid = []
        for name in ("ASRConfig", "TTSConfig"):
            config = profile[name]
            if (
                not isinstance(config, dict)
                or not isinstance(config.get("Provider"), str)
                or not config["Provider"].strip()
                or not isinstance(config.get("ProviderParams"), dict)
                or not config["ProviderParams"]
            ):
                invalid.append(name)
        if invalid:
            raise RTCProbeConfigurationError("invalid_legacy_voice_config", invalid)
        # No public-resource default is injected into the user's custom profile.
        return deepcopy(profile)
    if version != API_VERSION:
        raise RTCProbeConfigurationError("unsupported_api_version", ["rtc_api_version"])
    voice = str(_setting(settings, "rtc_tts_voice", DEFAULT_VOICE)) or DEFAULT_VOICE
    if voice.startswith("BV"):
        raise ValueError("legacy BV voices cannot be used with the public TTS 2.0 probe profile")
    return {
        "ASRConfig": {
            "Provider": "volcano",
            "ProviderParams": {
                "Mode": "bigmodel",
                "ApiResourceId": "volc.seedasr.sauc.duration",
                "StreamMode": 2,
            },
        },
        "TTSConfig": {
            "Provider": "volcano_bidirection",
            "ProviderParams": {
                "audio": {"voice_type": voice},
                "ResourceId": "seed-tts-2.0",
            },
        },
    }


def _setting(settings: Any, name: str, default: Any = "") -> Any:
    value = getattr(settings, name, default)
    return default if value is None else value


def _safe_code(value: Any) -> str | None:
    """Only carry machine codes, never provider messages or reflected requests."""
    if isinstance(value, (str, int)):
        code = str(value)
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", code):
            return code
    return None


def sign_request(
    settings: Any,
    action: str,
    body: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[str, dict[str, str], bytes]:
    """Serialize once, then sign and send exactly those bytes (official HMAC v4)."""
    if action not in {"StartVoiceChat", "StopVoiceChat", "UpdateVoiceChat"}:
        raise ValueError("unsupported RTC probe action")
    base = str(_setting(settings, "rtc_openapi_url", OPENAPI_URL)).rstrip("/")
    if base != OPENAPI_URL:
        raise ValueError("RTC probes require the official OpenAPI endpoint")
    access_key = str(_setting(settings, "volc_access_key"))
    secret_key = str(_setting(settings, "volc_secret_key"))
    if not access_key or not secret_key:
        raise ValueError("RTC signing credentials missing")
    version = str(_setting(settings, "rtc_api_version", API_VERSION))
    region = str(_setting(settings, "rtc_region", "cn-north-1"))
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    date_text = timestamp.strftime("%Y%m%dT%H%M%SZ")
    day = date_text[:8]
    content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body_hash = hashlib.sha256(content).hexdigest()
    query = "&".join(
        quote(key, safe="-_.~") + "=" + quote(value, safe="-_.~")
        for key, value in sorted({"Action": action, "Version": version}.items())
    )
    headers = {
        "Content-Type": "application/json",
        "Host": "rtc.volcengineapi.com",
        "X-Content-Sha256": body_hash,
        "X-Date": date_text,
    }
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = "\n".join(
        f"{key}:{headers[key_name]}"
        for key, key_name in (
            ("content-type", "Content-Type"),
            ("host", "Host"),
            ("x-content-sha256", "X-Content-Sha256"),
            ("x-date", "X-Date"),
        )
    )
    canonical = "\n".join(("POST", "/", query, canonical_headers, "", signed_headers, body_hash))
    scope = f"{day}/{region}/rtc/request"
    to_sign = "\n".join(("HMAC-SHA256", date_text, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    signing_key = secret_key.encode("utf-8")
    for component in (day, region, "rtc", "request"):
        signing_key = hmac.new(signing_key, component.encode(), hashlib.sha256).digest()
    signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return f"{base}/?{query}", headers, content


def classify_response(response: httpx.Response) -> dict[str, Any]:
    """Recognize provider failures even on HTTP 200, retaining only safe shape."""
    observation: dict[str, Any] = {
        "http_status": response.status_code,
        "ok": False,
        "response_shape": {},
    }
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        observation["error_code"] = "invalid_json"
        return observation
    if not isinstance(payload, dict):
        observation["error_code"] = "invalid_response_object"
        return observation
    metadata = payload.get("ResponseMetadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    error = metadata.get("Error") or payload.get("Error")
    observation["response_shape"] = {
        "top_level_keys": sorted(key for key in payload if key in {"ResponseMetadata", "Result", "Error"}),
        "metadata_present": "ResponseMetadata" in payload,
        "request_id_present": bool(metadata.get("RequestId")),
        "error_present": bool(error),
        "result_present": "Result" in payload,
        "result_type": type(payload["Result"]).__name__ if "Result" in payload else None,
    }
    if error:
        observation["error_code"] = _safe_code(error.get("Code")) if isinstance(error, dict) else None
        observation["error_code"] = observation["error_code"] or "provider_error"
    elif not response.is_success:
        observation["error_code"] = f"http_{response.status_code}"
    elif not metadata:
        observation["error_code"] = "missing_response_metadata"
    else:
        # Official examples allow success without Result and only ResponseMetadata.
        observation["ok"] = True
    return observation


async def call_openapi(
    client: httpx.AsyncClient, settings: Any, action: str, body: dict[str, Any]
) -> dict[str, Any]:
    began = time.perf_counter()
    url, headers, content = sign_request(settings, action, body)
    try:
        async with asyncio.timeout(float(_setting(settings, "rtc_timeout_seconds", 10.0))):
            response = await client.post(url, headers=headers, content=content)
            observation = classify_response(response)
    except (TimeoutError, httpx.TimeoutException):
        observation = {"ok": False, "error_code": "network_timeout", "outcome": "unknown"}
    except httpx.HTTPError:
        observation = {"ok": False, "error_code": "network_error", "outcome": "unknown"}
    observation["action"] = action
    observation["duration_ms"] = round((time.perf_counter() - began) * 1000, 2)
    return observation


def _valid_public_callback(url: str | None) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.fragment:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


def build_start_body(settings: Any, identity: dict[str, str], callback_url: str) -> dict[str, Any]:
    if not _valid_public_callback(callback_url):
        raise ValueError("a public HTTPS callback URL is required")
    voice_config = _voice_config(settings)
    suffix = identity.get("TaskId", "m0_task").removeprefix("m0_task_")
    parsed = urlsplit(callback_url)
    run_id = parse_qs(parsed.query).get("rtc_run_id", [""])[0]
    event_query = urlencode({"rtc_run_id": run_id}) if run_id else ""
    event_url = urlunsplit((parsed.scheme, parsed.netloc, "/api/rtc/events", event_query, ""))
    callback_secret = str(_setting(settings, "callback_secret"))
    if not callback_secret:
        raise ValueError("a run-scoped callback signature is required")
    return {
        **identity,
        "AgentConfig": {
            "TargetUserId": ["m0_user_" + suffix],
            "UserId": "m0_agent_" + suffix,
            "EnableConversationStateCallback": True,
            "ServerMessageURLForRTS": event_url,
            "ServerMessageSignatureForRTS": callback_secret,
        },
        "Config": {
            **voice_config,
            "LLMConfig": {
                "Mode": "CustomLLM",
                "Url": callback_url,
                "Prefill": False,
                **({"EnableRoundId": True} if str(_setting(settings, "rtc_api_version", API_VERSION)) == "2024-12-01" else {}),
            },
            "InterruptMode": 0,
        },
    }


async def run_rtc_probe(
    settings: Any, *, start: bool = False, callback_url: str | None = None
) -> dict[str, Any]:
    """Call only fresh m0_* tasks; public callback/client capture is a separate gate.

    Without start, twice stop a generated task that has never been started. This
    observes access and nonexistent-task behavior without touching existing RTC.
    With start, always attempt bounded cleanup, including after an unknown start.
    """
    evidence: dict[str, Any] = {
        "api_version": str(_setting(settings, "rtc_api_version", API_VERSION)),
        "scope": "fresh_m0_room_and_task_only",
        "live_start_requested": start,
        "query_capability": "query_unverified_no_documented_endpoint_selected",
        "round_correlation_gate": "blocked_requires_real_RTC_client_and_callback_capture",
        "end_to_end_interrupt_gate": "blocked_requires_real_RTC_client_and_audio_observation",
        "text_probe_requested": bool(_setting(settings, "rtc_text_probe", False)),
        "interrupt_only_probe_requested": bool(_setting(settings, "rtc_interrupt_only", False)),
        "voice_profile": "legacy_custom_resources" if str(_setting(settings, "rtc_api_version", API_VERSION)) == "2024-12-01" else "public_resources_2025",
    }
    if evidence["text_probe_requested"] and evidence["interrupt_only_probe_requested"]:
        evidence["error_code"] = "conflicting_probe_modes"
        return {"name": "rtc", "status": "blocked", "summary": "Text repetition and interrupt-only probes must be run separately.", "evidence": evidence}
    missing = [name for name in ("volc_access_key", "volc_secret_key", "rtc_app_id") if not _setting(settings, name)]
    if missing:
        evidence["missing_settings"] = missing
        return {"name": "rtc", "status": "blocked", "summary": "RTC credentials or AppId are not configured.", "evidence": evidence}
    if str(_setting(settings, "rtc_openapi_url", OPENAPI_URL)).rstrip("/") != OPENAPI_URL:
        return {"name": "rtc", "status": "blocked", "summary": "Only the official RTC endpoint is permitted.", "evidence": evidence}
    if evidence["api_version"] not in SUPPORTED_API_VERSIONS:
        return {"name": "rtc", "status": "blocked", "summary": "M0 probe supports API versions 2024-12-01 and 2025-06-01 only.", "evidence": evidence}
    if start and not _valid_public_callback(callback_url):
        return {"name": "rtc", "status": "blocked", "summary": "Start requires a verified public HTTPS callback for this M0 capture instance.", "evidence": evidence}

    hold_seconds = float(_setting(settings, "rtc_probe_hold_seconds", 3.0))
    if evidence["text_probe_requested"] and start and not 0 <= hold_seconds <= 7:
        return {"name": "rtc", "status": "blocked", "summary": "RTC text probe hold must be between 0 and 7 seconds; total deliberate wait is capped below 15 seconds.", "evidence": evidence}
    if evidence["interrupt_only_probe_requested"] and start and not 0.5 <= hold_seconds <= 7:
        return {"name": "rtc", "status": "blocked", "summary": "RTC interrupt-only hold must be between 0.5 and 7 seconds; total deliberate wait is capped at 14 seconds.", "evidence": evidence}

    suffix = uuid.uuid4().hex
    identity = {
        "AppId": str(_setting(settings, "rtc_app_id")),
        "RoomId": "m0_room_" + suffix,
        "TaskId": "m0_task_" + suffix,
    }
    # Only identifiers generated by this invocation may be written to cleanup evidence.
    evidence["probe_room_id"] = identity["RoomId"]
    evidence["probe_task_id"] = identity["TaskId"]
    evidence["probe_agent_id"] = "m0_agent_" + suffix
    evidence["probe_user_id"] = "m0_user_" + suffix
    timeout = float(_setting(settings, "rtc_timeout_seconds", 10.0))
    if not 0 < timeout <= 60:
        return {"name": "rtc", "status": "blocked", "summary": "RTC timeout must be between 0 and 60 seconds.", "evidence": evidence}
    start_body = None
    if start:
        try:
            start_body = build_start_body(settings, identity, callback_url or "")
        except RTCProbeConfigurationError as exc:
            evidence["error_code"] = exc.code
            evidence["configuration_fields"] = exc.fields
            return {"name": "rtc", "status": "blocked", "summary": "The selected RTC version requires an explicit valid voice profile.", "evidence": evidence}
        except ValueError:
            return {"name": "rtc", "status": "blocked", "summary": "The public callback or TTS probe profile is invalid.", "evidence": evidence}

    observations: list[dict[str, Any]] = []
    cleanup: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=bool(_setting(settings, "trust_env", False)),
    ) as client:
        if start:
            try:
                result = await call_openapi(client, settings, "StartVoiceChat", start_body or {})
                observations.append(result)
                evidence["start_outcome"] = "accepted" if result["ok"] else result.get("outcome", "rejected")
                if result["ok"] and (evidence["text_probe_requested"] or evidence["interrupt_only_probe_requested"]):
                    # These identifiers were generated in this invocation, never
                    # taken from an existing app/user room or persisted old task.
                    commands: list[dict[str, Any]] = []
                    interrupt_only = evidence["interrupt_only_probe_requested"]
                    for occurrence in ((1,) if interrupt_only else (1, 2)):
                        command_result = await call_openapi(
                            client,
                            settings,
                            "UpdateVoiceChat",
                            {
                                **identity,
                                "Command": "ExternalTextToLLM",
                                "Message": "这是M0验证，请简短回复。",
                                "InterruptMode": 1,
                            },
                        )
                        command_result["command"] = "ExternalTextToLLM"
                        command_result["occurrence"] = occurrence
                        commands.append(command_result)
                        await asyncio.sleep(hold_seconds)
                    interrupt_result = await call_openapi(
                        client,
                        settings,
                        "UpdateVoiceChat",
                        {**identity, "Command": "interrupt"},
                    )
                    interrupt_result["command"] = "interrupt"
                    commands.append(interrupt_result)
                    evidence["command_observations"] = commands
                    if interrupt_only:
                        evidence["interrupt_probe_input_occurrences"] = 1
                        evidence["configured_wait_budget_seconds"] = 2 * hold_seconds
                        evidence["interrupt_probe_gate"] = "commands_accepted_cancellation_observation_pending" if all(item["ok"] for item in commands) else "command_error_cancellation_observation_pending"
                        await asyncio.sleep(hold_seconds)
                    else:
                        evidence["text_probe_same_input_occurrences"] = 2
                        evidence["configured_wait_budget_seconds"] = 2 * hold_seconds + 0.25
                        evidence["text_probe_gate"] = "commands_accepted_callback_correlation_pending" if all(item["ok"] for item in commands) else "command_error_callback_correlation_pending"
                        await asyncio.sleep(0.25)
            finally:
                async def stop_created_task() -> None:
                    for _ in range(2):
                        result = await call_openapi(client, settings, "StopVoiceChat", identity)
                        cleanup.append(result)

                cleanup_task = asyncio.create_task(stop_created_task())
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    # Give cleanup a bounded chance even when the initiating request is cancelled.
                    await cleanup_task
                    raise
        else:
            for _ in range(2):
                observations.append(await call_openapi(client, settings, "StopVoiceChat", identity))

    evidence["observations"] = observations
    evidence["cleanup_observations"] = cleanup
    evidence["network_calls"] = len(observations) + len(cleanup) + len(evidence.get("command_observations", []))
    if start:
        cleanup_confirmed = any(result["ok"] for result in cleanup)
        evidence["cleanup_confirmed"] = cleanup_confirmed
        evidence["cleanup_required"] = not cleanup_confirmed
        lifecycle_ok = bool(observations and observations[0]["ok"] and cleanup_confirmed)
        evidence["live_start_stop_gate"] = "passed" if lifecycle_ok else "failed"
        status = "blocked" if lifecycle_ok else "failed"
        summary = "Start/Stop accepted; real client/callback/audio correlation is still unverified." if lifecycle_ok else "RTC Start or cleanup failed; inspect safe error codes and cleanup identifiers."
    else:
        evidence["nonexistent_task_stop_observed"] = True
        evidence["live_start_stop_gate"] = "blocked_start_not_requested"
        status = "blocked"
        summary = "Observed nonexistent-task Stop behavior; live Start/Stop and RTC correlation are not validated."
        if all(not result["ok"] for result in observations):
            summary = "Stop probe returned provider/network errors; live lifecycle and RTC correlation remain unverified."
    return {"name": "rtc", "status": status, "summary": summary, "evidence": evidence}
