import time
import struct
import hmac
import hashlib
import base64
import secrets
from dataclasses import dataclass
from io import BytesIO

VERSION = "001"

PRIVILEGES = {
    "PrivPublishStream": 0,
    "privPublishAudioStream": 1,
    "privPublishVideoStream": 2,
    "privPublishDataStream": 3,
    "PrivSubscribeStream": 4,
}

class ByteBuf:
    def __init__(self, data=None):
        self.buffer = BytesIO(data) if data else BytesIO()
    def pack(self): return self.buffer.getvalue()
    def put_uint16(self, v): self.buffer.write(struct.pack('<H', v)); return self
    def put_uint32(self, v): self.buffer.write(struct.pack('<I', v)); return self
    def put_bytes(self, b): self.put_uint16(len(b)); self.buffer.write(b); return self
    def put_string(self, s): return self.put_bytes(s.encode('utf-8'))
    def put_tree_map_uint32(self, m):
        if not m: self.put_uint16(0); return self
        self.put_uint16(len(m))
        for k, v in sorted(m.items(), key=lambda item: int(item[0])): self.put_uint16(int(k)); self.put_uint32(int(v))
        return self

class AccessToken:
    def __init__(self, app_id, app_key, room_id, user_id):
        self.app_id = app_id
        self.app_key = app_key
        self.room_id = room_id
        self.user_id = user_id
        self.issued_at = int(time.time())
        self.nonce = secrets.randbits(32)
        self.expire_at = 0
        self.privileges = {}

    def add_privilege(self, privilege, expire_timestamp):
        self.privileges[privilege] = expire_timestamp
        if privilege == PRIVILEGES["PrivPublishStream"]:
            self.privileges[PRIVILEGES["privPublishVideoStream"]] = expire_timestamp
            self.privileges[PRIVILEGES["privPublishAudioStream"]] = expire_timestamp
            self.privileges[PRIVILEGES["privPublishDataStream"]] = expire_timestamp

    def expire_time(self, expire_timestamp):
        self.expire_at = expire_timestamp

    def pack_msg(self):
        buf = ByteBuf()
        buf.put_uint32(self.nonce); buf.put_uint32(self.issued_at); buf.put_uint32(self.expire_at)
        buf.put_string(self.room_id); buf.put_string(self.user_id); buf.put_tree_map_uint32(self.privileges)
        return buf.pack()

    def serialize(self):
        msg = self.pack_msg()
        signature = hmac.new(self.app_key.encode('utf-8'), msg, hashlib.sha256).digest()
        content = ByteBuf().put_bytes(msg).put_bytes(signature).pack()
        return VERSION + self.app_id + base64.b64encode(content).decode('utf-8')


@dataclass(frozen=True)
class TokenMetadata:
    """Unsigned metadata only. RTC cloud still verifies the token's signature."""
    app_id: str
    room_id: str
    user_id: str
    issued_at: int
    expire_at: int
    privileges: dict[int, int]

    def is_expired(self, now: float):
        expirations = [self.expire_at, self.privileges.get(0, 0), self.privileges.get(4, 0)]
        return any(expiry > 0 and expiry <= now for expiry in expirations)


def parse_token(raw: str) -> TokenMetadata:
    """Parse the official v001 wire format without treating it as an AppKey."""
    try:
        if not raw.startswith(VERSION) or len(raw) <= 27:
            raise ValueError()
        content = base64.b64decode(raw[27:], validate=True)
        stream = BytesIO(content)

        def read_exact(size):
            value = stream.read(size)
            if len(value) != size:
                raise ValueError()
            return value

        def read_string():
            size = struct.unpack('<H', read_exact(2))[0]
            return read_exact(size)

        message, signature = read_string(), read_string()
        if len(signature) != 32 or stream.read(1):
            raise ValueError()
        stream = BytesIO(message)
        _, issued, expires = struct.unpack('<III', read_exact(12))
        room, user = read_string().decode('utf-8'), read_string().decode('utf-8')
        count = struct.unpack('<H', read_exact(2))[0]
        privileges = {}
        for _ in range(count):
            key, expiry = struct.unpack('<HI', read_exact(6))
            if key in privileges:
                raise ValueError()
            privileges[key] = expiry
        if stream.read(1) or not room or not user:
            raise ValueError()
        return TokenMetadata(raw[3:27], room, user, issued, expires, privileges)
    except (ValueError, TypeError, UnicodeError, struct.error):
        raise ValueError("RTC 临时 Token 格式无效，请重新从控制台复制完整 Token。") from None
