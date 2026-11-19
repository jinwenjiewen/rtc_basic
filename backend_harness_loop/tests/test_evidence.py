import unittest
import hashlib

from backend_harness_loop.evidence import analyze_capture, records_for_task


def record(source, run, round_id, tag=None):
    result = {"source": source, "correlation": {"identifiers": [{"kind": "run", "sha256": run}, {"kind": "round", "sha256": round_id}]}}
    if tag:
        result["tlv_tag"] = tag
    return result


class EvidenceTests(unittest.TestCase):
    def test_matching_round_number_in_different_run_is_not_a_match(self):
        result = analyze_capture([record("custom_llm", "run-a", "round-1"), record("rts_event", "run-b", "round-1", "conv")])
        self.assertEqual(result["custom_rounds_matched_to_state"], 0)

    def test_same_run_two_rounds_and_repeated_attempts_are_counted_separately(self):
        result = analyze_capture([record("custom_llm", "run-a", "round-1"), record("custom_llm", "run-a", "round-1"), record("custom_llm", "run-a", "round-2"), record("rts_event", "run-a", "round-1", "conv"), record("rts_event", "run-a", "round-2", "subv")])
        self.assertEqual(result["unique_round_counts"]["custom_llm"], 2)
        self.assertEqual(result["observed_custom_attempt_counts_per_round"], [1, 2])
        self.assertEqual(result["custom_rounds_matched_to_state"], 1)
        self.assertEqual(result["custom_rounds_matched_to_subtitle"], 1)

    def test_task_filter_and_stream_terminal_state_use_the_recorded_contract(self):
        task = record("rts_event", "run-a", "round-1", "conv")
        task["correlation"]["identifiers"].append({"kind": "task", "sha256": hashlib.sha256(b"fixture-task").hexdigest()})
        stream = record("capture_stream", "run-a", "round-1")
        stream["state"] = "cancelled"
        records = [task, stream, record("custom_llm", "run-b", "round-1")]
        selected = records_for_task(records, "fixture-task")
        self.assertEqual(len(selected), 2)
        self.assertEqual(analyze_capture(selected)["stream_terminal_counts"], {"cancelled": 1})
