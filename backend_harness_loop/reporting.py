"""Persist evidence atomically, with a final secret-redaction boundary."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings


def write_report(path: Path, results: list[dict], settings: Settings, *, milestone: str = "M0", extra_secrets: tuple[str, ...] = ()) -> dict:
    report = {
        "schema_version": 1,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "milestone": milestone,
        "configuration": settings.inventory(),
        "results": results,
        "m0_complete": False,
        "completion_note": "Individual probes do not prove live RTC round correlation, retry or end-to-end barge-in.",
    }
    if milestone == "M1":
        report["m1_complete"] = False
        report["completion_note"] = "M1 vertical-slice evidence; product authentication, migration and full audio performance are separate acceptance items."
    if milestone == "M3":
        report["m3_complete"] = False
        report["completion_note"] = "M3 read-only business tool evidence; scripted knowledge cannot establish real business retrieval acceptance."
    knowledge_secrets = ((settings.knowledge_config.api_key, settings.knowledge_config.base_url,
                          settings.knowledge_config.collection) if settings.knowledge_config else ())
    business_private = ((settings.business_policy.checkout_url, settings.business_policy.enrollment_contact)
                        if settings.business_policy else ())
    secrets = tuple(value for value in (
        settings.ark_api_key, settings.volc_access_key, settings.volc_secret_key,
        settings.rtc_app_id, settings.rtc_app_key, settings.callback_secret, settings.server_url,
        settings.rtc_static_token, settings.rtc_static_room_id, settings.rtc_static_user_id, *knowledge_secrets, *business_private, *extra_secrets,
    ) if value)

    def redact(value):
        if isinstance(value, str):
            for secret in sorted(secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {redact(key): redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    sanitized = redact(report)
    serialized = json.dumps(sanitized, ensure_ascii=False, indent=2)
    # Round correlation can only be signed off against captured cloud/client evidence.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(serialized + "\n", encoding="utf-8")
    temporary.replace(path)
    return sanitized
