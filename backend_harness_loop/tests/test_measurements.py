import hashlib
import unittest

from backend_harness_loop.measurements import INVALID_REASONS, summarize_measurements, validate_measurement


def raw(index=1, *, kind="speech_first_audio", round_id=1, duration=500, started=100):
    return {"sample_id": "m0_sample_" + f"{index:032x}", "kind": kind, "state": "completed",
            "started_ms": started, "trigger_ms": started + 100, "observed_ms": started + 100 + duration,
            "confirmed_ms": started + 200 + duration, "round_id": round_id}


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


class MeasurementTests(unittest.TestCase):
    def test_normalizes_round_and_derives_duration_without_accepting_claimed_duration(self):
        sample = validate_measurement(raw(round_id="PRIVATE-ROUND"))
        self.assertEqual(sample["round_sha256"], digest("PRIVATE-ROUND"))
        self.assertEqual(sample["source"], "browser_sdk_estimate")
        self.assertEqual(sample["duration_ms"], 500)
        self.assertNotIn("round_id", sample)
        self.assertNotIn("PRIVATE-ROUND", str(sample))
        self.assertEqual(validate_measurement(raw(round_id=42))["round_sha256"], validate_measurement(raw(round_id="42"))["round_sha256"])
        for field in ("duration_ms", "source", "round_sha256", "text"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_measurement({**raw(), field: "PRIVATE VALUE"})

    def test_requires_valid_sample_identity_kind_and_state_without_reflecting_values(self):
        for payload in (None, [], "PRIVATE VALUE", {**raw(), "sample_id": "PRIVATE VALUE"},
                        {**raw(), "kind": []}, {**raw(), "kind": "PRIVATE VALUE"},
                        {**raw(), "state": {}}, {**raw(), "state": "PRIVATE VALUE"}):
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(ValueError) as failure:
                validate_measurement(payload)
            self.assertNotIn("PRIVATE VALUE", str(failure.exception))

    def test_rejects_nonfinite_boolean_unbounded_and_missing_timestamps(self):
        for field in ("started_ms", "trigger_ms", "observed_ms", "confirmed_ms"):
            for value in (True, False, -1, 3_600_001, 10**1000, "100", None, float("nan"), float("inf"), -float("inf")):
                with self.subTest(field=field, value_type=type(value).__name__), self.assertRaises(ValueError):
                    validate_measurement({**raw(), field: value})
            payload = raw()
            del payload[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                validate_measurement(payload)

    def test_completed_order_and_inclusive_duration_boundaries(self):
        for updates in ({"trigger_ms": 99}, {"observed_ms": 199}, {"confirmed_ms": 699}):
            with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, "order"):
                validate_measurement({**raw(), **updates})
        allowed = {**raw(), "started_ms": 3_540_000, "trigger_ms": 3_540_000,
                   "observed_ms": 3_600_000, "confirmed_ms": 3_600_000}
        self.assertEqual(validate_measurement(allowed)["duration_ms"], 60_000)
        with self.assertRaisesRegex(ValueError, "total duration"):
            validate_measurement({**raw(), "confirmed_ms": 60_101})
        self.assertEqual(validate_measurement({**raw(), "trigger_ms": 100, "observed_ms": 100, "confirmed_ms": 100})["duration_ms"], 0)

    def test_round_shape_and_completed_reason_are_rejected(self):
        for value in (None, True, False, [], {}, 1.5, "", "x" * 129, 10**128, 10**10000):
            with self.subTest(round_type=type(value).__name__), self.assertRaises(ValueError):
                validate_measurement({**raw(), "round_id": value})
        missing = raw()
        del missing["round_id"]
        with self.assertRaises(ValueError):
            validate_measurement(missing)
        with self.assertRaises(ValueError):
            validate_measurement({**raw(), "reason": "timeout"})

    def test_invalid_samples_allow_partial_or_regressed_timing_but_no_duration(self):
        base = {"sample_id": raw()["sample_id"], "kind": "speech_first_audio", "state": "invalid", "started_ms": 100}
        for reason in INVALID_REASONS:
            with self.subTest(reason=reason):
                result = validate_measurement({**base, "reason": reason})
                self.assertEqual(result["reason"], reason)
                self.assertNotIn("duration_ms", result)
        regressed = validate_measurement({**base, "reason": "clock_regressed", "trigger_ms": 99, "observed_ms": 1})
        self.assertEqual(regressed["observed_ms"], 1)
        for updates in ({}, {"reason": "PRIVATE VALUE"}, {"reason": []}, {"reason": "timeout", "confirmed_ms": float("nan")}):
            with self.subTest(updates_type=type(updates).__name__), self.assertRaises(ValueError):
                validate_measurement({**base, **updates})

    def test_summary_filters_invalid_unmatched_and_duplicate_rounds_per_kind(self):
        samples = [validate_measurement(raw(1)), validate_measurement(raw(2, started=500, duration=100)),
                   validate_measurement(raw(3, round_id=2)),
                   validate_measurement(raw(4, kind="interrupt_silence")),
                   validate_measurement({"sample_id": raw(5)["sample_id"], "kind": "speech_first_audio",
                                         "state": "invalid", "started_ms": 10, "reason": "timeout"})]
        result = summarize_measurements(list(reversed(samples)), {digest(1)})
        self.assertEqual(result["speech_first_audio"]["count"], 1)
        self.assertEqual(result["speech_first_audio"]["p50_ms"], 500)
        self.assertEqual(result["interrupt_silence"]["count"], 1)
        self.assertEqual(result["invalid_count"], 1)
        self.assertEqual(result["invalid_reasons"], {"timeout": 1})
        self.assertEqual(result["unmatched_count"], 1)
        self.assertEqual(result["duplicate_count"], 1)
        self.assertFalse(result["ready"])
        self.assertEqual(result, summarize_measurements(samples, {digest(1)}))

    def test_nearest_rank_percentiles_and_independent_minimum_sample_gates(self):
        samples = [validate_measurement(raw(index, round_id=index, duration=duration))
                   for index, duration in enumerate((400, 100, 300, 200), start=1)]
        observed = {digest(index) for index in range(1, 5)}
        result = summarize_measurements(samples, observed)
        self.assertEqual(result["speech_first_audio"], {"count": 4, "p50_ms": 200, "p95_ms": 400, "ready": True})
        self.assertEqual(result["interrupt_silence"], {"count": 0, "p50_ms": None, "p95_ms": None, "ready": False})
        self.assertEqual(result["percentile_method"], "nearest_rank")
        self.assertFalse(result["ready"])
        interrupts = [validate_measurement(raw(index + 10, kind="interrupt_silence", round_id=index, duration=200)) for index in range(1, 4)]
        self.assertTrue(summarize_measurements(samples + interrupts, observed)["ready"])
        small = summarize_measurements(samples[:2], observed)
        self.assertEqual(small["speech_first_audio"]["p95_ms"], 400)
        self.assertFalse(small["speech_first_audio"]["ready"])
        self.assertIn("not physical audio", small["scope"])

    def test_minimum_must_be_positive_integer(self):
        for minimum in (0, -1, True, "3", 3.0):
            with self.subTest(minimum_type=type(minimum).__name__), self.assertRaises(ValueError):
                summarize_measurements([], set(), minimum)


if __name__ == "__main__":
    unittest.main()
