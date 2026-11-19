"""Short-lived RTC v001 browser tokens, without importing the old backend.

Wire format and the supported publish/subscribe privileges follow the official
generator: https://github.com/volcengine/rtc-aigc-demo/blob/main/server/token.js
Publish privilege 0 expands to the generator's private subprivileges 1/2/3;
therefore this token must not be described as an audio-only permission grant.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
from io import BytesIO
import math
import re
import secrets
import struct
import time


@dataclass(frozen=True)
class RTCClientToken:
    value: str = field(repr=False)
    expires_at: int


@dataclass(frozen=True)
class RTCStaticTokenMetadata:
    """Unsigned token claims; only the RTC service verifies their signature."""

    app_id: str = field(repr=False)
    room_id: str = field(repr=False)
    user_id: str = field(repr=False)
    issued_at: int
    expires_at: int
    privileges: dict[int, int]
    signature_verified: bool = field(default=False, init=False)


def parse_rtc_token(raw: str) -> RTCStaticTokenMetadata:
    """Read the complete canonical v001 envelope without authenticating it.

    Unknown privilege keys are preserved; the wire format is not restricted to
    the five keys currently used by our generator. Duplicate keys, malformed
    lengths, extra bytes, nonstandard Base64 and truncated signatures fail.
    """
    try:
        if (
            not isinstance(raw, str)
            or not 27 < len(raw) <= 100_000
            or not raw.startswith("001")
            or not re.fullmatch(r"[A-Za-z0-9]{24}", raw[3:27])
        ):
            raise ValueError()
        encoded = raw[27:]
        content = base64.b64decode(encoded, validate=True)
        if base64.b64encode(content).decode("ascii") != encoded:
            raise ValueError()
        stream = BytesIO(content)

        def read_exact(size: int) -> bytes:
            value = stream.read(size)
            if len(value) != size:
                raise ValueError()
            return value

        def read_sized() -> bytes:
            return read_exact(struct.unpack("<H", read_exact(2))[0])

        message, signature = read_sized(), read_sized()
        if len(signature) != 32 or stream.read(1):
            raise ValueError()
        stream = BytesIO(message)
        _, issued_at, expires_at = struct.unpack("<III", read_exact(12))
        room_id, user_id = read_sized().decode("utf-8"), read_sized().decode("utf-8")
        count = struct.unpack("<H", read_exact(2))[0]
        privileges = {}
        for _ in range(count):
            key, expiry = struct.unpack("<HI", read_exact(6))
            if key in privileges:
                raise ValueError()
            privileges[key] = expiry
        if stream.read(1) or not room_id or not user_id:
            raise ValueError()
        return RTCStaticTokenMetadata(raw[3:27], room_id, user_id, issued_at, expires_at, privileges)
    except (ValueError, TypeError, UnicodeError, struct.error):
        raise ValueError("Invalid RTC static token v001 format") from None


def validate_static_rtc_token(
    token: str,
    app_id: str,
    room_id: str,
    user_id: str,
    *,
    now: float | None = None,
) -> RTCClientToken:
    """Check unsigned bindings and expiry; RTC cloud must verify the signature.

    Expiry zero means no limit at that layer. Both supported public privileges
    (publish 0 and subscribe 4) must be present; absence is not an unlimited
    permission. The returned expiry is the earliest nonzero relevant expiry,
    or zero when all three layers are unlimited. The client session still has
    its independent, bounded M0 lifetime.
    """
    metadata = parse_rtc_token(token)
    for name, expected in (("app_id", app_id), ("room_id", room_id), ("user_id", user_id)):
        if not isinstance(expected, str) or not expected or getattr(metadata, name) != expected:
            raise ValueError(f"RTC static token {name} binding mismatch")
    if 0 not in metadata.privileges or 4 not in metadata.privileges:
        raise ValueError("RTC static token requires publish and subscribe privileges")
    observed_now = time.time() if now is None else now
    if (
        not isinstance(observed_now, (int, float))
        or isinstance(observed_now, bool)
        or observed_now < 0
        or observed_now > 0xFFFFFFFF
        or not math.isfinite(observed_now)
    ):
        raise ValueError("Invalid RTC static token validation time")
    limits = [expiry for expiry in (metadata.expires_at, metadata.privileges[0], metadata.privileges[4]) if expiry]
    if any(expiry <= observed_now for expiry in limits):
        raise ValueError("RTC static token or required privilege has expired")
    return RTCClientToken(value=token, expires_at=min(limits) if limits else 0)


def issue_rtc_token(
    app_id: str,
    app_key: str,
    room_id: str,
    user_id: str,
    *,
    lifetime_seconds: int = 600,
    now: int | None = None,
    nonce: int | None = None,
) -> RTCClientToken:
    """Sign a fresh, bounded room/user token using an actual application key.

    This intentionally does not accept console tokens or arbitrary room/user
    encodings. M0 owns the generated ASCII identities. All failures name the
    invalid field and never include the supplied value.
    """
    if not isinstance(app_id, str) or not re.fullmatch(r"[A-Za-z0-9]{24}", app_id):
        raise ValueError("Invalid RTC app_id")
    if (
        not isinstance(app_key, str)
        or not app_key.strip()
        or len(app_key) > 1024
        or any(char.isspace() for char in app_key)
        or (app_key.startswith("001") and len(app_key) > 32)
    ):
        raise ValueError("RTC app_key must be an application key, not a console token")
    for name, value in (("room_id", room_id), ("user_id", user_id)):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError(f"Invalid RTC {name}")
    if (
        not isinstance(lifetime_seconds, int)
        or isinstance(lifetime_seconds, bool)
        or not 60 <= lifetime_seconds <= 900
    ):
        raise ValueError("RTC lifetime_seconds must be between 60 and 900")
    issued_at = int(time.time()) if now is None else now
    token_nonce = secrets.randbits(32) if nonce is None else nonce
    for name, value in (("now", issued_at), ("nonce", token_nonce)):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFFFFFF:
            raise ValueError(f"Invalid RTC token {name}")
    expires_at = issued_at + lifetime_seconds
    if expires_at > 0xFFFFFFFF:
        raise ValueError("RTC token expiry exceeds v001 format")

    def sized(value: bytes) -> bytes:
        return struct.pack("<H", len(value)) + value

    # Zero privilege expiry means the token-wide expiry remains authoritative,
    # matching the official generator's default publish/subscribe example.
    privileges = struct.pack("<H", 5) + b"".join(struct.pack("<HI", key, 0) for key in range(5))
    message = (
        struct.pack("<III", token_nonce, issued_at, expires_at)
        + sized(room_id.encode("ascii"))
        + sized(user_id.encode("ascii"))
        + privileges
    )
    signature = hmac.new(app_key.encode("utf-8"), message, hashlib.sha256).digest()
    value = "001" + app_id + base64.b64encode(sized(message) + sized(signature)).decode("ascii")
    return RTCClientToken(value=value, expires_at=expires_at)
