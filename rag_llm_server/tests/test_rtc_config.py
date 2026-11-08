"""RTC identity binding and callback tests, using fixture credentials only."""
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from config import settings
from services.rtc_config import RTCConfigurationError, callback_url, rtc_credentials, rtc_identity
from services.token_build import AccessToken, PRIVILEGES, parse_token

APP_ID = "0123456789abcdef01234567"
APP_KEY = "test-key-for-token-signing-only"


def console_token(expires=None, room="ChatRoom01", user="Huoshan01"):
    builder = AccessToken(APP_ID, APP_KEY, room, user)
    builder.add_privilege(PRIVILEGES["PrivSubscribeStream"], 0)
    builder.add_privilege(PRIVILEGES["PrivPublishStream"], 0)
    builder.expire_time(expires if expires is not None else int(time.time()) + 3600)
    return builder.serialize()


class RTCConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = patch.multiple(settings, RTC_APP_ID=APP_ID, RTC_APP_KEY=APP_KEY,
            RTC_STATIC_TOKEN="", RTC_ROOM_ID="", RTC_USER_ID="", RTC_TASK_ID="ChatTask01",
            SERVER_URL="https://rtc-test.ngrok-free.dev", RTC_CALLBACK_SECRET="fixture-secret")
        self.config.start()
        self.addCleanup(self.config.stop)
        self.auth = SimpleNamespace(
            user=SimpleNamespace(id=uuid.uuid4()),
            session=SimpleNamespace(id=uuid.uuid4(), expires_at=datetime.now(timezone.utc) + timedelta(hours=2)))

    def test_console_token_is_preserved_with_its_bound_identity(self):
        settings.RTC_STATIC_TOKEN = console_token()
        config = rtc_credentials(self.auth)
        self.assertEqual(config["Token"], settings.RTC_STATIC_TOKEN)
        self.assertEqual((config["RoomId"], config["UserId"]), ("ChatRoom01", "Huoshan01"))
        self.assertEqual(rtc_identity(self.auth), ("ChatRoom01", "Huoshan01", "ChatTask01"))

    def test_dynamic_token_matches_official_javascript_generator(self):
        # Generated independently with volcengine/rtc-aigc-demo server/token.js.
        builder = AccessToken(APP_ID, APP_KEY, "room_fixture", "user_fixture")
        builder.nonce, builder.issued_at = 123456789, 1700000000
        builder.expire_time(1700003600)
        builder.add_privilege(PRIVILEGES["PrivSubscribeStream"], 0)
        builder.add_privilege(PRIVILEGES["PrivPublishStream"], 0)
        expected = "0010123456789abcdef01234567SAAVzVsHAPFTZRD/U2UMAHJvb21fZml4dHVyZQwAdXNlcl9maXh0dXJlBQAAAAAAAAABAAAAAAACAAAAAAADAAAAAAAEAAAAAAAgAAVsMNZpcxw/5yja50gKymGCTqOOhkPEwSLAMk+/xlmA"
        self.assertEqual(builder.serialize(), expected)

    def test_legacy_token_in_app_key_is_not_used_to_sign_another_token(self):
        settings.RTC_APP_KEY = console_token()
        config = rtc_credentials(self.auth)
        self.assertEqual(config["Token"], settings.RTC_APP_KEY)
        self.assertEqual(config["RoomId"], "ChatRoom01")

    def test_dynamic_credentials_remain_isolated_by_login_session(self):
        first = rtc_credentials(self.auth)
        metadata = parse_token(first["Token"])
        self.assertEqual((metadata.room_id, metadata.user_id), (first["RoomId"], first["UserId"]))
        self.assertFalse(metadata.is_expired(time.time()))
        self.auth.session.id = uuid.uuid4()
        self.assertNotEqual(rtc_credentials(self.auth)["RoomId"], first["RoomId"])

    def test_expired_token_rejected_but_identity_available_for_stop(self):
        settings.RTC_STATIC_TOKEN = console_token(int(time.time()) - 1)
        with self.assertRaisesRegex(RTCConfigurationError, "已过期"):
            rtc_credentials(self.auth)
        self.assertEqual(rtc_identity(self.auth)[0], "ChatRoom01")

    def test_wrong_app_room_or_user_is_rejected(self):
        settings.RTC_STATIC_TOKEN = console_token()
        for field, value in [("RTC_APP_ID", "another-app"), ("RTC_ROOM_ID", "other-room"), ("RTC_USER_ID", "other-user")]:
            with patch.object(settings, field, value):
                with self.assertRaisesRegex(RTCConfigurationError, "不一致"):
                    rtc_credentials(self.auth)

    def test_malformed_or_truncated_console_tokens_are_rejected(self):
        token = console_token()
        for invalid in ["", "001" + APP_ID + "not-base64", token[:-12], "002" + token[3:]]:
            with self.assertRaises(ValueError):
                parse_token(invalid)

    def test_ngrok_and_deployed_callback_hosts_are_supported(self):
        self.assertEqual(callback_url(), "https://rtc-test.ngrok-free.dev/api/chat_callback?token=fixture-secret")
        settings.SERVER_URL = "https://api.example.com/rtc/"
        self.assertEqual(callback_url(), "https://api.example.com/rtc/api/chat_callback?token=fixture-secret")

    def test_missing_or_local_callback_is_rejected_before_cloud_call(self):
        for url in ["", "http://localhost:3001", "https://127.0.0.1", "https://10.0.0.1", "https://[::1]", "https://example.com?key=value"]:
            with patch.object(settings, "SERVER_URL", url):
                with self.assertRaises(RTCConfigurationError):
                    callback_url()
