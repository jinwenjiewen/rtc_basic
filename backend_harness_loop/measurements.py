"""Validate and summarize M0 browser SDK estimates, never physical audio timing.

All timestamps in one sample use the same page's monotonic clock. The server
can check their shape and association, but does not authenticate their accuracy.
Only metadata is accepted; round identifiers are hashed at this boundary.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any


KINDS = frozenset({"speech_first_audio", "interrupt_silence"})
INVALID_REASONS = frozenset({
    "remote_not_silent", "mic_not_started", "old_audio", "resumed_speech",
    "overlapping_rounds", "missing_round", "command_failed", "timeout",
    "autoplay_failed", "track_ended", "mic_stopped", "session_closed",
    "page_hidden", "connection_lost", "cancelled", "clock_regressed",
    # Earlier callers used these equivalent diagnostic labels.
    "overlapping_audio", "speech_resumed", "autoplay_blocked",
})
TIME_FIELDS = ("started_ms", "trigger_ms", "observed_ms", "confirmed_ms")
FIELDS = frozenset({"sample_id", "kind", "state", "round_id", "reason", *TIME_FIELDS})
SOURCE = "browser_sdk_estimate"
SCOPE = (
    "Browser SDK estimates from matched, unique rounds only; not physical audio "
    "output or verified acoustic speech endpoints. Small-sample empirical "
    "percentiles do not establish a production SLO."
)


def _time(value: Any, name: str) -> int | float:
    # Range checking first handles arbitrary-size integers without float overflow.
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 <= value <= 3_600_000
        or not math.isfinite(value)
    ):
        raise ValueError(f"Invalid measurement {name}")
    return value


def validate_measurement(payload: dict) -> dict:
    """Accept one immutable sample and derive its measured duration.

    Completed samples require four ordered timestamps and a provider round.
    Invalid samples retain bounded diagnostic timestamps without requiring
    order: clock regression is itself an allowed reason for invalidation.
    Failure messages contain field names only, never supplied values.
    """
    if not isinstance(payload, dict) or set(payload) - FIELDS:
        raise ValueError("Invalid measurement fields")
    sample_id = payload.get("sample_id")
    if not isinstance(sample_id, str) or not re.fullmatch(r"m0_sample_[a-f0-9]{32}", sample_id):
        raise ValueError("Invalid measurement sample_id")
    kind, state = payload.get("kind"), payload.get("state")
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("Invalid measurement kind")
    if not isinstance(state, str) or state not in {"completed", "invalid"}:
        raise ValueError("Invalid measurement state")
    result = {"sample_id": sample_id, "kind": kind, "state": state, "source": SOURCE, "metric_version": 1}
    if "started_ms" not in payload:
        raise ValueError("Missing measurement started_ms")
    for name in TIME_FIELDS:
        if name in payload:
            result[name] = _time(payload[name], name)
    if "round_id" in payload:
        round_id = payload["round_id"]
        if (
            not isinstance(round_id, (str, int))
            or isinstance(round_id, bool)
            or (isinstance(round_id, int) and abs(round_id) >= 10**128)
            or not 1 <= len(str(round_id)) <= 128
        ):
            raise ValueError("Invalid measurement round_id")
        result["round_sha256"] = hashlib.sha256(str(round_id).encode("utf-8")).hexdigest()

    if state == "invalid":
        reason = payload.get("reason")
        if not isinstance(reason, str) or reason not in INVALID_REASONS:
            raise ValueError("Invalid measurement reason")
        result["reason"] = reason
        return result

    if "reason" in payload:
        raise ValueError("Completed measurement cannot have reason")
    if not all(name in result for name in TIME_FIELDS) or "round_sha256" not in result:
        raise ValueError("Completed measurement requires timestamps and round_id")
    times = [result[name] for name in TIME_FIELDS]
    if any(later < earlier for earlier, later in zip(times, times[1:])):
        raise ValueError("Invalid measurement timestamp order")
    if times[-1] - times[0] > 60_000:
        raise ValueError("Invalid measurement total duration")
    result["duration_ms"] = result["observed_ms"] - result["trigger_ms"]
    return result


def validate_measurement_start(payload: dict) -> dict:
    """Persist an attempt before waiting for speech, audio or a terminal sample."""
    if not isinstance(payload, dict) or set(payload) != {"sample_id", "kind", "started_ms"}:
        raise ValueError("Invalid measurement start fields")
    sample_id, kind = payload["sample_id"], payload["kind"]
    if not isinstance(sample_id, str) or not re.fullmatch(r"m0_sample_[a-f0-9]{32}", sample_id):
        raise ValueError("Invalid measurement start sample_id")
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("Invalid measurement start kind")
    return {"sample_id": sample_id, "kind": kind, "started_ms": _time(payload["started_ms"], "started_ms"),
            "source": SOURCE, "metric_version": 1}


def summarize_measurements(
    samples: list[dict], observed_round_hashes: set[str], minimum_samples: int = 3,
) -> dict:
    """Reduce normalized samples returned by :func:`validate_measurement`.

    Retain the earliest started completed sample for each (kind, round), so
    reordered HTTP delivery does not select a later repeated measurement.
    Both percentiles use nearest rank, including P50 for even sample counts.
    Invalid/unknown-round/duplicate samples are counted and never hidden among
    successful durations. Each kind must independently reach minimum_samples.
    """
    if not isinstance(minimum_samples, int) or isinstance(minimum_samples, bool) or minimum_samples < 1:
        raise ValueError("Invalid measurement minimum_samples")
    durations: dict[str, list[int | float]] = {kind: [] for kind in sorted(KINDS)}
    invalid_count = unmatched_count = duplicate_count = 0
    invalid_reasons: dict[str, int] = {}
    seen = set()
    for sample in sorted(samples, key=lambda item: (item["started_ms"], item["sample_id"])):
        if sample["state"] == "invalid":
            invalid_count += 1
            reason = sample["reason"]
            invalid_reasons[reason] = invalid_reasons.get(reason, 0) + 1
            continue
        round_hash = sample["round_sha256"]
        if round_hash not in observed_round_hashes:
            unmatched_count += 1
            continue
        pair = (sample["kind"], round_hash)
        if pair in seen:
            duplicate_count += 1
            continue
        seen.add(pair)
        durations[sample["kind"]].append(sample["duration_ms"])

    metrics = {}
    for kind, values in durations.items():
        values.sort()
        count = len(values)
        metrics[kind] = {
            "count": count,
            "p50_ms": values[math.ceil(count * .5) - 1] if count else None,
            "p95_ms": values[math.ceil(count * .95) - 1] if count else None,
            "ready": count >= minimum_samples,
        }
    return {
        **metrics, "invalid_count": invalid_count, "invalid_reasons": dict(sorted(invalid_reasons.items())),
        "unmatched_count": unmatched_count, "duplicate_count": duplicate_count,
        "minimum_samples": minimum_samples, "ready": all(metric["ready"] for metric in metrics.values()),
        "percentile_method": "nearest_rank", "scope": SCOPE,
    }
