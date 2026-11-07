"""CloudBase verification flows and short-lived browser-bound challenges."""

from __future__ import annotations

import re
import secrets
from urllib.parse import parse_qs, urlsplit

from psycopg.types.json import Jsonb

from ontology.db import connect
from . import auth

PROVIDERS = frozenset({"sms", "wechat"})
SMS_COOKIE = "__Host-onto_sms"
WECHAT_COOKIE = "__Host-onto_wechat"
WECHAT_CALLBACK = "/auth/wechat/callback"
CHALLENGE_TTL = 600
SMS_COOLDOWN = 60
SMS_PER_PHONE_DAY = 5
SMS_PER_MINUTE = 10
SMS_PER_DAY = 100
PHONE = re.compile(r"(?:\+86)?(1[0-9]{10})")
CODE = re.compile(r"[0-9]{6}")


def phone_number(value: object) -> str:
    if isinstance(value, str) and len(value) <= 32:
        match = PHONE.fullmatch(re.sub(r"[ -]", "", value.strip()))
        if match:
            return f"+86 {match[1]}"
    raise auth.LoginError(400, "请输入有效的中国大陆手机号。")


def reserve_sms(phone: str) -> None:
    """Count attempts before contacting the provider, across all app instances."""
    recipient = auth.digest(phone)
    with connect() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('onto_research.sms_quota'))")
        conn.execute("DELETE FROM onto_research.web_sms_requests WHERE sent_at <= now() - interval '1 day'")
        counts = conn.execute(
            """SELECT count(*) AS day_total, count(*) FILTER (WHERE recipient_hash = %s) AS phone_day,
                   count(*) FILTER (WHERE sent_at > now() - interval '1 minute') AS minute_total,
                   count(*) FILTER (WHERE recipient_hash = %s AND sent_at > now() - %s * interval '1 second') AS recent_phone
               FROM onto_research.web_sms_requests""",
            (recipient, recipient, SMS_COOLDOWN),
        ).fetchone()
        if (counts["recent_phone"] or counts["phone_day"] >= SMS_PER_PHONE_DAY
                or counts["minute_total"] >= SMS_PER_MINUTE or counts["day_total"] >= SMS_PER_DAY):
            raise auth.LoginError(429, "短信发送次数已达上限，请稍后重试。")
        conn.execute("INSERT INTO onto_research.web_sms_requests (recipient_hash) VALUES (%s)", (recipient,))


def create_challenge(provider: str, data: dict, ttl: int) -> str:
    token = secrets.token_urlsafe(32)
    with connect() as conn, conn.transaction():
        conn.execute("DELETE FROM onto_research.web_auth_challenges WHERE expires_at <= now()")
        conn.execute(
            """INSERT INTO onto_research.web_auth_challenges (token_hash, provider, payload, expires_at)
               VALUES (%s, %s, %s, now() + %s * interval '1 second')""",
            (auth.digest(token), provider, Jsonb(data), ttl),
        )
    return token


def sms_attempt(token: str) -> dict:
    row = None
    if auth.SESSION_TOKEN.fullmatch(token):
        with connect() as conn:
            row = conn.execute(
                """UPDATE onto_research.web_auth_challenges SET attempts = attempts + 1
                   WHERE token_hash = %s AND provider = 'sms' AND expires_at > now() AND attempts < 5
                   RETURNING payload""",
                (auth.digest(token),),
            ).fetchone()
    if row is None:
        raise auth.LoginError(400, "验证码已失效或尝试次数过多，请重新发送。")
    return row["payload"]


def consume_challenge(token: str, provider: str, state: str | None = None) -> dict:
    row = None
    if auth.SESSION_TOKEN.fullmatch(token):
        with connect() as conn:
            row = conn.execute(
                """DELETE FROM onto_research.web_auth_challenges
                   WHERE token_hash = %s AND provider = %s AND expires_at > now()
                         AND (%s::text IS NULL OR payload->>'state' = %s)
                   RETURNING payload""",
                (auth.digest(token), provider, state, state),
            ).fetchone()
    if row is None:
        raise auth.LoginError(400, "验证已失效，请重新开始。")
    return row["payload"]


async def send_sms(env_id: str, phone: str) -> tuple[dict, int]:
    device = secrets.token_hex(16)
    try:
        result = await auth.cloudbase_request(env_id, "verification", device=device,
                                              data={"target": "ANY", "phone_number": phone})
    except auth.CloudBaseError as error:
        if error.status == 429 or error.code == "captcha_required":
            raise auth.LoginError(429, "短信通道暂时限制发送，请稍后重试。") from None
        raise auth.LoginError(503, "暂时无法发送验证码，请稍后重试。") from None
    verification_id = result.get("verification_id")
    ttl = result.get("expires_in", CHALLENGE_TTL)
    if not isinstance(verification_id, str) or not verification_id or type(ttl) is not int or ttl <= 0:
        raise auth.LoginError(503, "短信服务返回了无效的验证信息。")
    return {"verification_id": verification_id, "phone_hash": auth.digest(phone), "device": device,
            "existing": result.get("is_user") is True}, min(ttl, CHALLENGE_TTL)


async def verify_sms(env_id: str, challenge: dict, code: str) -> str:
    try:
        result = await auth.cloudbase_request(
            env_id, "verification/verify", device=challenge["device"],
            data={"verification_id": challenge["verification_id"], "verification_code": code},
        )
    except auth.CloudBaseError as error:
        if error.status in (400, 401, 403, 404):
            raise auth.LoginError(400, "验证码错误或已过期。") from None
        raise auth.LoginError(503, "验证码服务暂时不可用。") from None
    token = result.get("verification_token")
    if not isinstance(token, str) or not token:
        raise auth.LoginError(503, "短信服务返回了无效的验证信息。")
    return token


async def sms_identity(env_id: str, challenge: dict, phone: str, verification_token: str) -> tuple[str, int]:
    data = {"verification_token": verification_token}
    path = "signin" if challenge["existing"] else "signup"
    if path == "signup":
        data["phone_number"] = phone
    try:
        result = await auth.cloudbase_request(env_id, path, device=challenge["device"], data=data)
    except auth.CloudBaseError:
        raise auth.LoginError(400, "手机号验证未完成，请重新发送验证码后再试。") from None
    return auth.verified_identity(result)


async def wechat_uri(env_id: str, callback: str, state: str, device: str) -> str:
    try:
        result = await auth.cloudbase_request(
            env_id, "provider/uri", device=device,
            query={"provider_id": "wx_open", "redirect_uri": callback, "state": state},
        )
        uri = result.get("uri", "")
        parsed = urlsplit(uri)
        query = parse_qs(parsed.query)
        if (parsed.scheme == "https" and parsed.netloc == "open.weixin.qq.com"
                and parsed.path == "/connect/qrconnect"
                and query.get("state") == [state] and query.get("redirect_uri") == [callback]):
            return uri
    except (auth.CloudBaseError, ValueError, TypeError, AttributeError):
        pass
    raise auth.LoginError(503, "微信登录暂时不可用，请检查身份源和回调域名配置。")


async def wechat_identity(env_id: str, callback: str, code: str, device: str) -> tuple[str, int]:
    try:
        result = await auth.cloudbase_request(
            env_id, "provider/token", device=device,
            data={"provider_id": "wx_open", "provider_redirect_uri": callback, "provider_code": code},
        )
        if "sub" not in result:
            token = result.get("provider_token")
            if not isinstance(token, str) or not token:
                raise auth.LoginError(503, "微信身份源返回了无效的验证信息。")
            result = await auth.cloudbase_request(
                env_id, "signin/with/provider", device=device,
                data={"provider_id": "wx_open", "provider_token": token, "force_disable_sign_up": False},
            )
    except auth.CloudBaseError as error:
        if error.code in ("not_found", "provider_not_found", "unimplemented", "login_type_disabled"):
            raise auth.LoginError(503, "请确认 CloudBase 已开启微信身份源和自动注册。") from None
        raise auth.LoginError(400, "微信授权未完成，请重新扫码。") from None
    return auth.verified_identity(result)
