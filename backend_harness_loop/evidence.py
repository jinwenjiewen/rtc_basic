"""Derive bounded observations from metadata; never promote them to audio proof."""

from __future__ import annotations

from collections import Counter
import hashlib


def records_for_task(records: list[dict], task_id: str) -> list[dict]:
    task_hash = hashlib.sha256(task_id.encode()).hexdigest()
    runs = set()
    for record in records:
        ids = record.get("correlation", {}).get("identifiers", [])
        if any(item.get("kind") == "task" and item.get("sha256") == task_hash for item in ids):
            runs.update(item["sha256"] for item in ids if item.get("kind") == "run")
    return [record for record in records if any(item.get("kind") == "run" and item.get("sha256") in runs for item in record.get("correlation", {}).get("identifiers", []))]


def analyze_capture(records: list[dict]) -> dict:
    sources = Counter()
    rounds: dict[str, set[tuple[str, str]]] = {"custom_llm": set(), "conv": set(), "subv": set()}
    attempt_counts = Counter()
    stages = Counter()
    errors = Counter()
    responses = Counter()
    streams = Counter()
    for record in records:
        source = record.get("source", "unknown")
        sources[source] += 1
        metadata = record.get("correlation", {})
        identifiers = metadata.get("identifiers", [])
        run_ids = {item["sha256"] for item in identifiers if item.get("kind") == "run"}
        round_ids = {item["sha256"] for item in identifiers if item.get("kind") == "round"}
        pairs = {(run, round_id) for run in run_ids for round_id in round_ids}
        channel = "custom_llm" if source == "custom_llm" else record.get("tlv_tag")
        if channel in rounds:
            rounds[channel].update(pairs)
        if source == "custom_llm":
            for pair in pairs:
                attempt_counts[pair] += 1
            if "response_status" in record:
                responses[str(record["response_status"])] += 1
        if "stage_code" in metadata:
            stages[str(metadata["stage_code"])] += 1
        if metadata.get("error_code"):
            errors[str(metadata["error_code"])] += 1
        if source == "capture_stream":
            streams[record.get("state", record.get("stream_state", "unknown"))] += 1
    return {
        "record_count": len(records),
        "source_counts": dict(sources),
        "unique_round_counts": {name: len(items) for name, items in rounds.items()},
        "custom_rounds_matched_to_state": len(rounds["custom_llm"] & rounds["conv"]),
        "custom_rounds_matched_to_subtitle": len(rounds["custom_llm"] & rounds["subv"]),
        "observed_custom_attempt_counts_per_round": sorted(attempt_counts.values()),
        "stage_counts": dict(stages),
        "machine_error_codes": dict(errors),
        "callback_response_status_counts": dict(responses),
        "stream_terminal_counts": dict(streams),
        "scope": "Observed callback samples only; not a universal retry guarantee or real client/audio validation",
    }
