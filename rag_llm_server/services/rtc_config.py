"""Keep RTC identities bound to their credentials; validate callback configuration."""
import ipaddress
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

from config import settings
from services.token_build import AccessToken, PRIVILEGES, parse_token


class RTCConfigurationError(ValueError):
    pass


def static_token():
    if settings.RTC_STATIC_TOKEN:
        return settings.RTC_STATIC_TOKEN
    # Compatibility with earlier demos that stored a console token in RTC_APP_KEY.
    if settings.RTC_APP_KEY.startswith("001") and len(settings.RTC_APP_KEY) > 32:
        return settings.RTC_APP_KEY
    return ""


def static_metadata():
    try:
        metadata = parse_token(static_token())
    except ValueError as exc:
        raise RTCConfigurationError(str(exc)) from None
    if metadata.app_id != settings.RTC_APP_ID:
        raise RTCConfigurationError("临时 RTC Token 的 AppId 与 RTC_APP_ID 不一致。")
    if settings.RTC_ROOM_ID and settings.RTC_ROOM_ID != metadata.room_id:
        raise RTCConfigurationError("RTC_ROOM_ID 与临时 Token 绑定的房间号不一致。")
    if settings.RTC_USER_ID and settings.RTC_USER_ID != metadata.user_id:
        raise RTCConfigurationError("RTC_USER_ID 与临时 Token 绑定的用户 ID 不一致。")
    return metadata


def rtc_identity(auth):
    if static_token():
        metadata = static_metadata()
        return metadata.room_id, metadata.user_id, settings.RTC_TASK_ID or "ChatTask01"
    return f"room_{auth.session.id.hex}", f"user_{auth.user.id.hex}", f"task_{auth.session.id.hex}"


def rtc_credentials(auth):
    room_id, user_id, _ = rtc_identity(auth)
    if static_token():
        if static_metadata().is_expired(time.time()):
            raise RTCConfigurationError("临时 RTC Token 已过期，请在控制台重新生成并更新 RTC_STATIC_TOKEN。")
        token = static_token()
    else:
        if not settings.RTC_APP_ID or not settings.RTC_APP_KEY:
            raise RTCConfigurationError("请配置 RTC_APP_ID 和 RTC_APP_KEY，或配置有效的 RTC_STATIC_TOKEN。")
        builder = AccessToken(settings.RTC_APP_ID, settings.RTC_APP_KEY, room_id, user_id)
        expires = int(min(time.time() + 3600, auth.session.expires_at.timestamp()))
        builder.add_privilege(PRIVILEGES["PrivSubscribeStream"], 0)
        builder.add_privilege(PRIVILEGES["PrivPublishStream"], 0)
        builder.expire_time(expires)
        token = builder.serialize()
    return {"AppId": settings.RTC_APP_ID, "RoomId": room_id, "UserId": user_id, "Token": token}


def callback_url():
    if not settings.SERVER_URL:
        raise RTCConfigurationError(
            "当前仅配置了本地后端，尚未配置可供 RTC 云端访问的回调地址。"
            "本地模型与 RAG 接口可继续使用；云端语音对话需要部署后端并配置 SERVER_URL。"
        )
    try:
        parsed = urlsplit(settings.SERVER_URL)
        host = (parsed.hostname or "").lower().rstrip(".")
        local_host = host == "localhost" or host.endswith(".localhost") or host.endswith(".local")
        try:
            local_host = local_host or not ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        if parsed.scheme != "https" or not host or local_host or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
    except ValueError:
        raise RTCConfigurationError("SERVER_URL 需要填写 RTC 云端可访问的 HTTPS 地址，不能填写本机或内网地址。") from None
    path = parsed.path.rstrip("/") + "/api/chat_callback"
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode({"token": settings.RTC_CALLBACK_SECRET}), ""))
