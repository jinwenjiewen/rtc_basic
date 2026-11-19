"""RTC token interoperability, identity binding and bounded credential tests."""

import base64
import hashlib
import hmac
from io import BytesIO
import struct
import unittest
from unittest.mock import patch

from backend_harness_loop.rtc_token import issue_rtc_token, parse_rtc_token, validate_static_rtc_token


APP_ID = "0123456789abcdef01234567"
APP_KEY = "test-key-for-token-signing-only"


class RTCClientTokenTests(unittest.TestCase):
    def issue(self, **overrides):
        values = {"app_id": APP_ID, "app_key": APP_KEY, "room_id": "room_fixture", "user_id": "user_fixture", "now": 1700000000, "nonce": 123456789}
        values.update(overrides)
        return issue_rtc_token(**values)

    def test_matches_independent_official_javascript_fixture(self):
        # Original v001 fixture was produced with the official token.js generator.
        # Its expiry was changed to 600 seconds and re-signed independently with
        # Node's crypto.createHmac, without calling the Python encoder.
        expected = "0010123456789abcdef01234567SAAVzVsHAPFTZVjzU2UMAHJvb21fZml4dHVyZQwAdXNlcl9maXh0dXJlBQAAAAAAAAABAAAAAAACAAAAAAADAAAAAAAEAAAAAAAgAOwjgOffTG7PNW6f/sq6pdY2ydUIFUPcmaZWaIGbuRg8"
        issued = self.issue()
        self.assertEqual(issued.value, expected)
        self.assertEqual(issued.expires_at, 1700000600)
        self.assertNotIn(expected, repr(issued))

    def test_wire_format_binds_room_user_and_global_expiry(self):
        issued = self.issue()
        content = BytesIO(base64.b64decode(issued.value[27:], validate=True))

        def sized(stream):
            length = struct.unpack("<H", stream.read(2))[0]
            return stream.read(length)

        message, signature = sized(content), sized(content)
        self.assertEqual(content.read(), b"")
        self.assertTrue(hmac.compare_digest(signature, hmac.new(APP_KEY.encode(), message, hashlib.sha256).digest()))
        stream = BytesIO(message)
        self.assertEqual(struct.unpack("<III", stream.read(12)), (123456789, 1700000000, 1700000600))
        self.assertEqual(sized(stream), b"room_fixture")
        self.assertEqual(sized(stream), b"user_fixture")
        count = struct.unpack("<H", stream.read(2))[0]
        self.assertEqual(count, 5)
        self.assertEqual([struct.unpack("<HI", stream.read(6)) for _ in range(count)], [(key, 0) for key in range(5)])
        self.assertEqual(stream.read(), b"")
        self.assertNotEqual(self.issue(room_id="another_room").value, issued.value)
        self.assertNotEqual(self.issue(user_id="another_user").value, issued.value)
        self.assertNotEqual(self.issue(app_key="different-fixture-key").value, issued.value)

    def test_defaults_use_current_time_and_secure_nonce(self):
        with patch("backend_harness_loop.rtc_token.time.time", return_value=1700000100.75), patch("backend_harness_loop.rtc_token.secrets.randbits", return_value=42) as random_bits:
            issued = issue_rtc_token(APP_ID, APP_KEY, "room_fixture", "user_fixture")
        random_bits.assert_called_once_with(32)
        self.assertEqual(issued.expires_at, 1700000700)
        self.assertEqual(issued, self.issue(now=1700000100, nonce=42))

    def test_static_console_token_is_never_treated_as_application_key(self):
        console_token = self.issue().value
        with self.assertRaises(ValueError) as error:
            self.issue(app_key=console_token)
        self.assertNotIn(console_token, str(error.exception))
        self.assertIn("application key", str(error.exception))

    def test_invalid_identity_key_ttl_and_integer_bounds_are_rejected_safely(self):
        cases = (
            {"app_id": "not-an-app"}, {"app_id": "汉" * 24},
            {"app_key": ""}, {"app_key": "key with spaces"},
            {"room_id": ""}, {"room_id": "../room"}, {"room_id": "r" * 129},
            {"user_id": "user\nname"}, {"user_id": "用户"},
            {"lifetime_seconds": 0}, {"lifetime_seconds": 59},
            {"lifetime_seconds": 901}, {"lifetime_seconds": True},
            {"lifetime_seconds": 600.0}, {"now": -1}, {"now": True},
            {"now": 0xFFFFFFFF}, {"nonce": -1}, {"nonce": 0x100000000},
        )
        for values in cases:
            with self.subTest(fields=list(values)), self.assertRaises(ValueError) as error:
                self.issue(**values)
            self.assertNotIn(APP_KEY, str(error.exception))
        self.assertEqual(self.issue(lifetime_seconds=60).expires_at, 1700000060)
        self.assertEqual(self.issue(lifetime_seconds=900).expires_at, 1700000900)


def static_fixture(*, expires=1700000600, privileges=None, room=b"fixture_room", user=b"fixture_user", signature=b"S" * 32, message_suffix=b"", content_suffix=b""):
    """Unsigned independent wire fixture: tests do not assert cloud acceptance."""
    if privileges is None:
        privileges = [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (5, 0)]

    def sized(value):
        return struct.pack("<H", len(value)) + value

    message = (struct.pack("<III", 42, 1700000000, expires)
               + sized(room) + sized(user) + struct.pack("<H", len(privileges))
               + b"".join(struct.pack("<HI", key, expiry) for key, expiry in privileges)
               + message_suffix)
    content = sized(message) + sized(signature) + content_suffix
    return "001" + APP_ID + base64.b64encode(content).decode("ascii")


class RTCStaticTokenTests(unittest.TestCase):
    def validate(self, token=None, **overrides):
        values = {"token": static_fixture() if token is None else token, "app_id": APP_ID, "room_id": "fixture_room", "user_id": "fixture_user", "now": 1700000100}
        values.update(overrides)
        return validate_static_rtc_token(**values)

    def test_parse_accepts_six_privileges_without_claiming_signature_verification(self):
        raw = static_fixture()
        metadata = parse_rtc_token(raw)
        self.assertEqual((metadata.app_id, metadata.room_id, metadata.user_id), (APP_ID, "fixture_room", "fixture_user"))
        self.assertEqual((metadata.issued_at, metadata.expires_at), (1700000000, 1700000600))
        self.assertEqual(metadata.privileges, {key: 0 for key in range(6)})
        self.assertFalse(metadata.signature_verified)
        issued = self.validate(raw)
        self.assertEqual(issued.value, raw)
        self.assertEqual(issued.expires_at, 1700000600)
        self.assertNotIn(raw, repr(issued))

    def test_generated_token_can_be_parsed_and_validated_without_resigning(self):
        issued = issue_rtc_token(APP_ID, APP_KEY, "fixture_room", "fixture_user", now=1700000000, nonce=42)
        accepted = self.validate(issued.value)
        self.assertEqual(accepted, issued)
        self.assertFalse(parse_rtc_token(accepted.value).signature_verified)

    def test_wrong_binding_is_rejected_without_echoing_token(self):
        raw = static_fixture()
        for field, value in (("app_id", "987654321098765432109876"), ("room_id", "other_room"), ("user_id", "other_user")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "binding mismatch") as error:
                self.validate(raw, **{field: value})
            self.assertNotIn(raw, str(error.exception))

    def test_token_publish_and_subscribe_expiry_are_each_enforced(self):
        self.assertEqual(self.validate(static_fixture(expires=1700000900, privileges=[(0, 1700000500), (4, 1700000700)])).expires_at, 1700000500)
        for raw in (
            static_fixture(expires=1700000100),
            static_fixture(expires=1700000099),
            static_fixture(privileges=[(0, 1700000100), (4, 0)]),
            static_fixture(privileges=[(0, 0), (4, 1700000099)]),
        ):
            with self.assertRaisesRegex(ValueError, "expired"):
                self.validate(raw)
        self.assertEqual(self.validate(static_fixture(expires=0, privileges=[(0, 0), (4, 0)])).expires_at, 0)
        self.assertEqual(self.validate(static_fixture(expires=0, privileges=[(0, 0), (4, 1700000800)])).expires_at, 1700000800)
        # Unused privilege expiry does not invalidate required publish/subscribe.
        self.validate(static_fixture(privileges=[(0, 0), (4, 0), (5, 1)]))

    def test_missing_required_privilege_is_not_unlimited_permission(self):
        for privileges in ([], [(0, 0)], [(4, 0)], [(1, 0), (2, 0), (3, 0), (4, 0)]):
            with self.subTest(keys=[key for key, _ in privileges]), self.assertRaisesRegex(ValueError, "requires publish and subscribe"):
                self.validate(static_fixture(privileges=privileges))

    def test_malformed_envelopes_and_duplicate_privileges_are_rejected(self):
        raw = static_fixture()
        invalid = (
            "", "002" + raw[3:], raw + "=", raw[:40] + "\n" + raw[40:],
            "001" + APP_ID + "not-base64", raw[:-1],
            static_fixture(signature=b"S" * 31), static_fixture(signature=b"S" * 33),
            static_fixture(message_suffix=b"extra"), static_fixture(content_suffix=b"extra"),
            static_fixture(privileges=[(0, 0), (4, 0), (0, 0)]),
            static_fixture(room=b""), static_fixture(user=b"\xff"),
        )
        for index, value in enumerate(invalid):
            with self.subTest(case=index), self.assertRaisesRegex(ValueError, "v001 format"):
                parse_rtc_token(value)

    def test_every_payload_truncation_fails_even_when_base64_is_well_formed(self):
        raw = static_fixture()
        content = base64.b64decode(raw[27:])
        for length in range(len(content)):
            truncated = raw[:27] + base64.b64encode(content[:length]).decode("ascii")
            with self.subTest(length=length), self.assertRaises(ValueError):
                parse_rtc_token(truncated)

    def test_invalid_validation_time_is_rejected(self):
        for value in (True, -1, float("nan"), float("inf"), "1700000100", 10**1000):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
                self.validate(now=value)


if __name__ == "__main__":
    unittest.main()
