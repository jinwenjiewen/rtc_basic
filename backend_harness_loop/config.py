"""Explicit configuration without importing the existing backend or printing secrets."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from .knowledge import KnowledgeConfig
from .business import BusinessPolicy


@dataclass(frozen=True)
class Settings:
    ark_api_key: str = field(default="", repr=False)
    ark_base_url: str = ""
    ark_model: str = ""
    volc_access_key: str = field(default="", repr=False)
    volc_secret_key: str = field(default="", repr=False)
    rtc_app_id: str = field(default="", repr=False)
    rtc_app_key: str = field(default="", repr=False)
    rtc_static_token: str = field(default="", repr=False)
    rtc_static_room_id: str = field(default="", repr=False)
    rtc_static_user_id: str = field(default="", repr=False)
    callback_secret: str = field(default="", repr=False)
    server_url: str = field(default="", repr=False)
    rtc_api_version: str = "2024-12-01"
    rtc_region: str = "cn-north-1"
    rtc_openapi_url: str = "https://rtc.volcengineapi.com"
    rtc_timeout_seconds: float = 10.0
    rtc_text_probe: bool = False
    rtc_interrupt_only: bool = False
    rtc_probe_hold_seconds: float = 3.0
    rtc_asr_app_id: str = ""
    rtc_tts_app_id: str = ""
    rtc_legacy_voice_config: dict | None = field(default=None, repr=False)
    rtc_tts_voice: str = "zh_female_vv_uranus_bigtts"
    model_timeout_seconds: float = 15.0
    model_thinking: str = "disabled"
    first_output_deadline_ms: int = 6000
    trust_env: bool = False
    capture_instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    capture_fail_first: bool = False
    capture_chunk_delay_seconds: float = 0
    capture_chunk_count: int = 1
    m1_max_steps: int = 4
    m1_max_tool_calls: int = 8
    m1_wall_seconds: float = 30
    m1_first_output_deadline_ms: int = 9000
    m2_context_input_tokens: int = 24576
    m2_context_window_tokens: int = 32768
    m2_context_output_reserve: int = 1024
    m2_context_safety_tokens: int = 512
    m2_context_keep_recent_turns: int = 4
    m2_context_warn_ratio: float = .70
    m2_context_auto_ratio: float = .85
    m2_context_target_ratio: float = .50
    m2_context_background: bool = True
    m2_context_background_seconds: float = 5
    m2_context_background_tokens: int = 1500
    knowledge_config: KnowledgeConfig | None = field(default=None, repr=False)
    business_policy: BusinessPolicy | None = field(default=None, repr=False)

    @classmethod
    def load(cls, env_file: Path | None, *, overlay_file: Path | None = None, trust_env: bool = False) -> Settings:
        if env_file is not None and not env_file.is_file():
            raise ValueError("The explicit environment file does not exist")
        if overlay_file is not None and not overlay_file.is_file():
            raise ValueError("The explicit overlay environment file does not exist")
        values = {
            **(dotenv_values(env_file, interpolate=False) if env_file else {}),
            **(dotenv_values(overlay_file, interpolate=False) if overlay_file else {}),
            **os.environ,
        }

        def get(name: str, default: str = "") -> str:
            return str(values.get(name) or default).strip()

        return cls(
            ark_api_key=get("ARK_API_KEY"),
            ark_base_url=get("ARK_BASE_URL"),
            ark_model=get("ARK_ENDPOINT_ID"),
            volc_access_key=get("VOLC_ACCESS_KEY"),
            volc_secret_key=get("VOLC_SECRET_KEY"),
            rtc_app_id=get("M0_RTC_APP_ID", get("RTC_APP_ID")),
            rtc_app_key=get("M0_RTC_APP_KEY", get("RTC_APP_KEY")),
            rtc_static_token=get("M0_RTC_STATIC_TOKEN"),
            rtc_static_room_id=get("M0_RTC_STATIC_ROOM_ID"),
            rtc_static_user_id=get("M0_RTC_STATIC_USER_ID"),
            callback_secret=get("RTC_CALLBACK_SECRET"),
            server_url=get("SERVER_URL"),
            rtc_api_version=get("M0_RTC_API_VERSION", "2024-12-01"),
            rtc_region=get("M0_RTC_REGION", "cn-north-1"),
            rtc_timeout_seconds=float(get("M0_RTC_TIMEOUT_SECONDS", "10")),
            rtc_text_probe=get("M0_RTC_TEXT_PROBE", "false").lower() == "true",
            rtc_interrupt_only=get("M0_RTC_INTERRUPT_ONLY", "false").lower() == "true",
            rtc_probe_hold_seconds=float(get("M0_RTC_PROBE_HOLD_SECONDS", "3")),
            rtc_tts_voice=get("M0_RTC_TTS_VOICE", "zh_female_vv_uranus_bigtts"),
            model_timeout_seconds=float(get("M0_MODEL_TIMEOUT_SECONDS", "15")),
            model_thinking=get("M0_MODEL_THINKING", "disabled"),
            first_output_deadline_ms=int(get("M0_FIRST_OUTPUT_DEADLINE_MS", "6000")),
            trust_env=trust_env,
            capture_instance_id=get("M0_CAPTURE_INSTANCE_ID", uuid.uuid4().hex),
            capture_fail_first=get("M0_CAPTURE_FAIL_FIRST", "false").lower() == "true",
            capture_chunk_delay_seconds=float(get("M0_CAPTURE_DELAY_MS", "0")) / 1000,
            capture_chunk_count=int(get("M0_CAPTURE_CHUNKS", "1")),
            m1_max_steps=int(get("M1_MAX_MODEL_STEPS", "4")),
            m1_max_tool_calls=int(get("M1_MAX_TOOL_CALLS", "8")),
            m1_wall_seconds=float(get("M1_TURN_WALL_SECONDS", "30")),
            m1_first_output_deadline_ms=int(get("M1_FIRST_OUTPUT_DEADLINE_MS", "9000")),
            m2_context_input_tokens=int(get("M2_CONTEXT_INPUT_TOKENS", "24576")),
            m2_context_window_tokens=int(get("M2_CONTEXT_WINDOW_TOKENS", "32768")),
            m2_context_output_reserve=int(get("M2_CONTEXT_OUTPUT_RESERVE_TOKENS", "1024")),
            m2_context_safety_tokens=int(get("M2_CONTEXT_SAFETY_TOKENS", "512")),
            m2_context_keep_recent_turns=int(get("M2_CONTEXT_KEEP_RECENT_TURNS", "4")),
            m2_context_warn_ratio=float(get("M2_CONTEXT_WARN_RATIO", ".70")),
            m2_context_auto_ratio=float(get("M2_CONTEXT_AUTO_RATIO", ".85")),
            m2_context_target_ratio=float(get("M2_CONTEXT_TARGET_RATIO", ".50")),
            m2_context_background=get("M2_CONTEXT_BACKGROUND", "true").lower() == "true",
            m2_context_background_seconds=float(get("M2_CONTEXT_BACKGROUND_SECONDS", "5")),
            m2_context_background_tokens=int(get("M2_CONTEXT_BACKGROUND_TOKENS", "1500")),
        )

    def context_options(self) -> dict:
        from .context import ContextPolicy
        return {"context_policy": ContextPolicy(
            input_cap_tokens=self.m2_context_input_tokens,
            model_window_tokens=self.m2_context_window_tokens,
            output_reserve_tokens=self.m2_context_output_reserve,
            safety_margin_tokens=self.m2_context_safety_tokens,
            keep_recent_turns=self.m2_context_keep_recent_turns,
            warn_ratio=self.m2_context_warn_ratio,
            auto_ratio=self.m2_context_auto_ratio,
            target_ratio=self.m2_context_target_ratio,
        ), "background_context_enabled": self.m2_context_background,
            "background_context_seconds": self.m2_context_background_seconds,
            "background_context_tokens": self.m2_context_background_tokens}

    def business_tool_options(self) -> dict:
        from .knowledge import create_knowledge_backend
        return {"knowledge": create_knowledge_backend(self.knowledge_config) if self.knowledge_config is not None else None,
                "business_policy": self.business_policy or (BusinessPolicy() if self.knowledge_config else None)}

    def inventory(self) -> dict:
        return {
            "configured": {
                key: bool(getattr(self, key))
                for key in (
                    "ark_api_key", "ark_base_url", "ark_model", "volc_access_key",
                    "volc_secret_key", "rtc_app_id", "rtc_app_key", "callback_secret", "server_url",
                    "rtc_static_token", "rtc_static_room_id", "rtc_static_user_id",
                )
            },
            "rtc_api_version": self.rtc_api_version,
            "legacy_voice_profile_configured": bool(self.rtc_legacy_voice_config),
            "model_transport_https": urlsplit(self.ark_base_url).scheme == "https",
            "ambient_proxy_enabled": self.trust_env,
            "model_thinking": self.model_thinking,
            "knowledge": self.knowledge_config.inventory() if self.knowledge_config else {"enabled": False},
            "business": (self.business_policy or BusinessPolicy()).inventory() if self.business_policy or self.knowledge_config else {"enabled": False},
        }
