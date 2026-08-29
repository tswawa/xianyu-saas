"""Short-lived server-side Xianyu QR login sessions.

The module deliberately keeps platform credentials in memory.  Callers expose
only the dictionaries returned by ``start`` and ``poll``; a confirmed Cookie
header is borrowed with ``begin_consume`` and destroyed only after the caller
reports a successful durable save with ``finish_consume``.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.cookiejar import Cookie
from typing import Callable
from urllib.parse import urlsplit

from account_storage import DEFAULT_ACCOUNT_ID, normalize_account_key


PASSPORT_HOST = "passport.goofish.com"
GENERATE_URL = "https://passport.goofish.com/newlogin/qrcode/generate.do"
QUERY_URL = "https://passport.goofish.com/newlogin/qrcode/query.do"
LOGIN_URL = "https://passport.goofish.com/login_token/login.do"
MTOP_URL = (
    "https://h5api.m.goofish.com/h5/"
    "mtop.taobao.idlemessage.pc.loginuser.get/1.0/"
)
APP_KEY = "34839810"
MAX_RESPONSE_BYTES = 256 * 1024
MAX_COOKIE_HEADER_BYTES = 32 * 1024
LOGIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,96}$")
COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
TOKEN_RE = re.compile(r"^[\x21-\x7e]{8,4096}$")


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


DEFAULT_TTL_SECONDS = _bounded_int("SAAS_XIANYU_LOGIN_TTL_SECONDS", 150, 120, 180)
DEFAULT_CAPACITY = _bounded_int("SAAS_XIANYU_LOGIN_CAPACITY", 64, 4, 256)
DEFAULT_COOLDOWN_SECONDS = _bounded_int(
    "SAAS_XIANYU_LOGIN_COOLDOWN_SECONDS", 5, 1, 30
)
DEFAULT_TIMEOUT_SECONDS = _bounded_int(
    "SAAS_XIANYU_LOGIN_REQUEST_TIMEOUT_SECONDS", 8, 3, 15
)
DEFAULT_POLL_INTERVAL_SECONDS = _bounded_int(
    "SAAS_XIANYU_LOGIN_POLL_INTERVAL_SECONDS", 1, 1, 5
)
DEFAULT_CONFIRMED_TTL_SECONDS = _bounded_int(
    "SAAS_XIANYU_LOGIN_CONFIRMED_TTL_SECONDS", 90, 60, 120
)
DEFAULT_UPSTREAM_CONCURRENCY = _bounded_int(
    "SAAS_XIANYU_LOGIN_UPSTREAM_CONCURRENCY", 4, 1, 16
)
_GLOBAL_UPSTREAM_GATE = threading.BoundedSemaphore(DEFAULT_UPSTREAM_CONCURRENCY)


ERROR_MESSAGES = {
    "invalid_request": "登录请求无效，请重新发起。",
    "login_not_found": "登录会话不存在，请重新发起。",
    "login_exists": "已有登录会话正在进行。",
    "login_cooldown": "操作过于频繁，请稍后再试。",
    "login_capacity": "当前登录人数较多，请稍后再试。",
    "login_busy": "正在检查登录状态，请稍后再试。",
    "login_not_confirmed": "尚未确认登录。",
    "login_consumed": "登录结果已使用，请重新发起。",
    "login_expired": "登录二维码已过期，请重新发起。",
    "qr_query_failed": "二维码状态确认失败，请刷新二维码重试。",
    "login_confirm_failed": "扫码确认成功，但闲鱼登录确认失败，请刷新二维码重试。",
    "mtop_context_failed": "扫码确认成功，但登录上下文初始化失败，请刷新二维码重试。",
    "qr_cookie_incomplete": "扫码确认成功，但登录信息不完整，请刷新二维码重试。",
    "platform_error": "闲鱼登录暂时不可用，请稍后再试。",
    "network_error": "暂时无法连接闲鱼，请稍后再试。",
}


class XianyuLoginError(RuntimeError):
    """A stable, browser-safe login failure without upstream response text."""

    def __init__(self, code: str):
        safe_code = code if code in ERROR_MESSAGES else "platform_error"
        self.code = safe_code
        self.message = ERROR_MESSAGES[safe_code]
        super().__init__(self.message)


@dataclass
class _Login:
    user_id: int
    login_id: str
    created_at: float
    expires_at: float
    session: object = field(repr=False)
    account_key: str = DEFAULT_ACCOUNT_ID
    status: str = "waiting"
    qr_url: str = field(default="", repr=False)
    qr_svg: bytes = field(default=b"", repr=False)
    query_token: str = field(default="", repr=False)
    query_ck: str = field(default="", repr=False)
    login_token: str = field(default="", repr=False)
    cookie_header: str = field(default="", repr=False)
    in_flight: bool = False
    error_code: str = ""
    consumed: bool = False
    consuming: bool = False
    last_poll_at: float = 0.0
    session_closed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _new_requests_session():
    import requests

    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.goofish.com",
            "Referer": "https://www.goofish.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0 Safari/537.36"
            ),
        }
    )
    return session


def _new_qr_svg(value: str) -> bytes:
    import qrcode
    import qrcode.image.svg

    code = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    code.add_data(value)
    code.make(fit=True)
    image = code.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    output = io.BytesIO()
    image.save(output)
    return output.getvalue()


def _safe_user_id(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise XianyuLoginError("invalid_request")
    return value


def _safe_login_id(value) -> str:
    if not isinstance(value, str) or not LOGIN_ID_RE.fullmatch(value):
        raise XianyuLoginError("invalid_request")
    return value


def _response_json(response, expected_url: str) -> dict:
    try:
        status_code = int(response.status_code)
        headers = response.headers
        response_url = str(getattr(response, "url", expected_url) or expected_url)
    except (AttributeError, TypeError, ValueError):
        raise XianyuLoginError("platform_error") from None
    if not 200 <= status_code < 300:
        raise XianyuLoginError("platform_error")
    expected = urlsplit(expected_url)
    actual = urlsplit(response_url)
    if (
        actual.scheme != "https"
        or actual.hostname != expected.hostname
        or actual.path != expected.path
        or actual.username is not None
        or actual.password is not None
        or actual.port not in (None, 443)
    ):
        raise XianyuLoginError("platform_error")
    try:
        content_length = int(headers.get("Content-Length", "0") or "0")
    except (TypeError, ValueError):
        raise XianyuLoginError("platform_error") from None
    if content_length < 0 or content_length > MAX_RESPONSE_BYTES:
        raise XianyuLoginError("platform_error")
    try:
        chunks = response.iter_content(chunk_size=16 * 1024, decode_unicode=False)
        raw_content = bytearray()
        for chunk in chunks:
            if not chunk:
                continue
            if not isinstance(chunk, (bytes, bytearray)):
                raise XianyuLoginError("platform_error")
            if len(raw_content) + len(chunk) > MAX_RESPONSE_BYTES:
                raise XianyuLoginError("platform_error")
            raw_content.extend(chunk)
        payload = json.loads(bytes(raw_content).decode("utf-8"))
    except XianyuLoginError:
        raise
    except Exception:
        raise XianyuLoginError("platform_error") from None
    if not isinstance(payload, dict):
        raise XianyuLoginError("platform_error")
    return payload


def _content_data(payload: dict) -> dict:
    content = payload.get("content")
    if not isinstance(content, dict) or content.get("success") is not True:
        raise XianyuLoginError("platform_error")
    data = content.get("data")
    if not isinstance(data, dict):
        raise XianyuLoginError("platform_error")
    return data


def _qr_url(value) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise XianyuLoginError("platform_error")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != PASSPORT_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path != "/qrcodeCheck.htm"
        or parsed.fragment
    ):
        raise XianyuLoginError("platform_error")
    return value


def _opaque_token(value) -> str:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise XianyuLoginError("platform_error")
    if any(character in value for character in ("\r", "\n", ";")):
        raise XianyuLoginError("platform_error")
    return value


def _query_token(value) -> str:
    token = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(token, str) or not re.fullmatch(r"[0-9]{8,32}", token):
        raise XianyuLoginError("platform_error")
    return token


def _login_token(data: dict) -> str:
    for key in ("token", "lgToken", "loginToken"):
        direct = data.get(key)
        if direct is not None:
            return _opaque_token(direct)
    extension = data.get("bizExt")
    if extension is None:
        return ""
    if isinstance(extension, str) and 2 <= len(extension) <= 8192:
        try:
            extension = json.loads(extension)
        except (TypeError, ValueError):
            raise XianyuLoginError("platform_error") from None
    if not isinstance(extension, dict):
        raise XianyuLoginError("platform_error")
    return _opaque_token(extension.get("loginToken"))


def _validate_svg(value) -> bytes:
    if not isinstance(value, (bytes, bytearray)):
        raise XianyuLoginError("platform_error")
    svg = bytes(value)
    if not 64 <= len(svg) <= 512 * 1024:
        raise XianyuLoginError("platform_error")
    lowered = svg.lower()
    if b"<svg" not in lowered[:512] or b"</svg>" not in lowered[-512:]:
        raise XianyuLoginError("platform_error")
    forbidden = (b"<script", b"javascript:", b"<foreignobject", b"onload=", b"onclick=")
    if any(marker in lowered for marker in forbidden):
        raise XianyuLoginError("platform_error")
    return svg


def _cookie_applies(cookie: Cookie, host: str, path: str, now: float) -> bool:
    domain = str(getattr(cookie, "domain", "") or "").lstrip(".").lower()
    if domain and host != domain and not host.endswith("." + domain):
        return False
    cookie_path = str(getattr(cookie, "path", "/") or "/")
    if not path.startswith(cookie_path.rstrip("/") + "/") and path != cookie_path:
        return False
    expires = getattr(cookie, "expires", None)
    return expires is None or float(expires) > now


def _cookie_header(session, now: float) -> str:
    target = urlsplit(MTOP_URL)
    selected: dict[str, tuple[int, str]] = {}
    try:
        cookies = list(session.cookies)
    except Exception:
        raise XianyuLoginError("platform_error") from None
    if len(cookies) > 256:
        raise XianyuLoginError("platform_error")
    for cookie in cookies:
        if not _cookie_applies(cookie, target.hostname or "", target.path, now):
            continue
        name = str(getattr(cookie, "name", "") or "")
        value = str(getattr(cookie, "value", "") or "")
        if not COOKIE_NAME_RE.fullmatch(name):
            continue
        if not value or len(value) > 4096 or not value.isascii():
            continue
        if any(ord(character) < 0x21 or ord(character) == 0x7F or character == ";" for character in value):
            continue
        score = len(str(getattr(cookie, "domain", "") or "")) + len(
            str(getattr(cookie, "path", "/") or "/")
        )
        previous = selected.get(name)
        if previous is None or score >= previous[0]:
            selected[name] = (score, value)
    unb = selected.get("unb", (0, ""))[1]
    mtop_token = selected.get("_m_h5_tk", (0, ""))[1]
    if not unb.isdigit() or len(unb) > 64 or "_" not in mtop_token:
        raise XianyuLoginError("platform_error")
    header = "; ".join(f"{name}={selected[name][1]}" for name in sorted(selected))
    if not header or len(header.encode("ascii")) > MAX_COOKIE_HEADER_BYTES:
        raise XianyuLoginError("platform_error")
    return header


class XianyuLoginManager:
    """Thread-safe, per-user registry for official Xianyu QR login flows."""

    def __init__(
        self,
        session_factory: Callable[[], object] = _new_requests_session,
        qr_factory: Callable[[str], bytes] = _new_qr_svg,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        capacity: int = DEFAULT_CAPACITY,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        request_timeout: int = DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        confirmed_ttl_seconds: int = DEFAULT_CONFIRMED_TTL_SECONDS,
        upstream_gate=None,
        sweep_interval_seconds: float = 1.0,
    ):
        self._session_factory = session_factory
        self._qr_factory = qr_factory
        self._clock = clock
        self._ttl = max(120, min(int(ttl_seconds), 180))
        self._capacity = max(1, min(int(capacity), 256))
        self._cooldown = max(0, min(int(cooldown_seconds), 30))
        self._timeout = max(1, min(int(request_timeout), 15))
        self._poll_interval = max(0.0, min(float(poll_interval_seconds), 5.0))
        self._confirmed_ttl = max(60, min(int(confirmed_ttl_seconds), 120))
        self._upstream_gate = upstream_gate or _GLOBAL_UPSTREAM_GATE
        self._sweep_interval = max(0.05, min(float(sweep_interval_seconds), 5.0))
        self._lock = threading.RLock()
        self._items: dict[str, _Login] = {}
        self._by_user: dict[tuple[int, str], str] = {}
        self._last_start: dict[tuple[int, str], float] = {}
        self._stop_sweeper = threading.Event()
        self._sweeper = threading.Thread(
            target=self._sweep_loop,
            name="xianyu-login-sweeper",
            daemon=True,
        )
        self._sweeper.start()

    @staticmethod
    def _close_session(item: _Login) -> None:
        if item.session_closed:
            return
        try:
            item.session.cookies.clear()
        except Exception:
            pass
        try:
            item.session.close()
        except Exception:
            pass
        item.session_closed = True

    @staticmethod
    def _cleanup(item: _Login) -> None:
        item.qr_url = ""
        item.qr_svg = b""
        item.query_token = ""
        item.query_ck = ""
        item.login_token = ""
        item.cookie_header = ""
        XianyuLoginManager._close_session(item)

    @staticmethod
    def _scope(user_id, account_key=DEFAULT_ACCOUNT_ID) -> tuple[int, str]:
        uid = _safe_user_id(user_id)
        try:
            key = normalize_account_key(account_key)
        except ValueError:
            raise XianyuLoginError("invalid_request") from None
        return uid, key

    def _expire(self, item: _Login, now: float) -> None:
        item.status = "expired"
        item.error_code = ""
        # A request owns the Session while in flight.  That request performs
        # the final cleanup before publishing its expired result.
        if not item.in_flight and not item.consuming:
            self._cleanup(item)
        scope = (item.user_id, item.account_key)
        if self._by_user.get(scope) == item.login_id:
            self._by_user.pop(scope, None)

    def _prune(self, now: float) -> None:
        for login_id, item in list(self._items.items()):
            if item.expires_at <= now:
                self._expire(item, now)
            if item.status == "expired" and now >= item.expires_at + 30:
                self._items.pop(login_id, None)
        for scope, started_at in list(self._last_start.items()):
            if scope not in self._by_user and now - started_at >= self._cooldown:
                self._last_start.pop(scope, None)

    def _sweep_loop(self) -> None:
        while not self._stop_sweeper.wait(self._sweep_interval):
            try:
                now = float(self._clock())
                with self._lock:
                    self._prune(now)
            except Exception:
                # Cleanup must never take down the API process.
                continue

    def _get(self, user_id, login_id, account_key=DEFAULT_ACCOUNT_ID) -> tuple[_Login, float]:
        uid, key = self._scope(user_id, account_key)
        identifier = _safe_login_id(login_id)
        now = float(self._clock())
        with self._lock:
            self._prune(now)
            item = self._items.get(identifier)
            if item is None or item.user_id != uid or item.account_key != key:
                raise XianyuLoginError("login_not_found")
            if item.expires_at <= now:
                self._expire(item, now)
            return item, now

    def _public(self, item: _Login, now: float) -> dict:
        payload = {
            "login_id": item.login_id,
            "status": item.status,
            "expires_in": max(0, int(math.ceil(item.expires_at - now))),
        }
        if item.status == "error":
            code = item.error_code if item.error_code in ERROR_MESSAGES else "platform_error"
            payload.update({"code": code, "message": ERROR_MESSAGES[code]})
        return payload

    def _call_json(self, item: _Login, method: str, url: str, **kwargs) -> dict:
        kwargs.update(
            {"timeout": self._timeout, "allow_redirects": False, "stream": True}
        )
        if not self._upstream_gate.acquire(blocking=False):
            raise XianyuLoginError("login_busy")
        response = None
        try:
            response = item.session.request(method, url, **kwargs)
            return _response_json(response, url)
        except XianyuLoginError:
            raise
        except Exception:
            raise XianyuLoginError("network_error") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            self._upstream_gate.release()

    def start(self, user_id, account_key=DEFAULT_ACCOUNT_ID) -> dict:
        uid, key = self._scope(user_id, account_key)
        now = float(self._clock())
        with self._lock:
            self._prune(now)
            scope = (uid, key)
            active_id = self._by_user.get(scope)
            if active_id and active_id in self._items:
                raise XianyuLoginError("login_exists")
            previous = self._last_start.get(scope)
            if previous is not None and now - previous < self._cooldown:
                raise XianyuLoginError("login_cooldown")
            active_count = sum(
                item.status not in {"expired", "error"} for item in self._items.values()
            )
            if active_count >= self._capacity:
                raise XianyuLoginError("login_capacity")
            login_id = secrets.token_urlsafe(32)
            try:
                session = self._session_factory()
            except Exception:
                raise XianyuLoginError("platform_error") from None
            item = _Login(uid, login_id, now, now + self._ttl, session, account_key=key)
            item.in_flight = True
            self._items[login_id] = item
            self._by_user[scope] = login_id
            self._last_start[scope] = now

        try:
            with item.lock:
                payload = self._call_json(
                    item,
                    "GET",
                    GENERATE_URL,
                    params={
                        "appName": "xianyu",
                        "appEntrance": "web",
                        "fromSite": "77",
                    },
                )
                data = _content_data(payload)
                qr_url = _qr_url(data.get("codeContent"))
                query_token = _query_token(data.get("t"))
                query_ck = _opaque_token(data.get("ck"))
                qr_svg = _validate_svg(self._qr_factory(qr_url))
        except XianyuLoginError:
            with self._lock:
                if self._items.get(login_id) is item:
                    self._items.pop(login_id, None)
                    self._by_user.pop((uid, key), None)
                self._cleanup(item)
            raise
        except Exception:
            with self._lock:
                if self._items.get(login_id) is item:
                    self._items.pop(login_id, None)
                    self._by_user.pop((uid, key), None)
                self._cleanup(item)
            raise XianyuLoginError("platform_error") from None

        with self._lock:
            current = self._items.get(login_id)
            current_now = float(self._clock())
            if current is not item or current_now >= item.expires_at:
                item.in_flight = False
                self._expire(item, current_now)
                raise XianyuLoginError("login_expired")
            item.qr_url = qr_url
            item.qr_svg = qr_svg
            item.query_token = query_token
            item.query_ck = query_ck
            item.in_flight = False
            return self._public(item, current_now)

    def qr_svg(self, user_id, login_id, account_key=DEFAULT_ACCOUNT_ID) -> bytes:
        item, _now = self._get(user_id, login_id, account_key)
        with self._lock:
            if item.status == "expired":
                raise XianyuLoginError("login_expired")
            if not item.qr_svg:
                raise XianyuLoginError("platform_error")
            return bytes(item.qr_svg)

    def _confirm(self, item: _Login, login_token: str) -> str:
        if login_token:
            try:
                payload = self._call_json(
                    item,
                    "POST",
                    LOGIN_URL,
                    params={
                        "appName": "xianyu",
                        "fromSite": "77",
                        "token": login_token,
                        "subFlow": "DIALOG_CHECK_LOGIN_RPC",
                        "nextCode": "0018",
                        "bizScene": "qrcode",
                        "confirm": "true",
                    },
                    data={"deviceId": ""},
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "https://passport.goofish.com",
                        "Referer": "https://passport.goofish.com/mini_login.htm",
                    },
                )
                _content_data(payload)
            except XianyuLoginError as error:
                if error.code in {"login_busy", "network_error"}:
                    raise
                raise XianyuLoginError("login_confirm_failed") from None
        try:
            self._call_json(
                item,
                "POST",
                MTOP_URL,
                params={
                    "jsv": "2.7.2",
                    "appKey": APP_KEY,
                    "t": str(int(time.time() * 1000)),
                    "sign": "",
                    "api": "mtop.taobao.idlemessage.pc.loginuser.get",
                    "v": "1.0",
                    "type": "originaljson",
                    "dataType": "json",
                    "timeout": "20000",
                    "sessionOption": "AutoLoginOnly",
                },
                data={"data": "{}"},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://www.goofish.com",
                    "Referer": "https://www.goofish.com/",
                },
            )
        except XianyuLoginError as error:
            if error.code in {"login_busy", "network_error"}:
                raise
            raise XianyuLoginError("mtop_context_failed") from None
        try:
            return _cookie_header(item.session, time.time())
        except XianyuLoginError:
            raise XianyuLoginError("qr_cookie_incomplete") from None

    def poll(self, user_id, login_id, account_key=DEFAULT_ACCOUNT_ID) -> dict:
        item, now = self._get(user_id, login_id, account_key)
        with self._lock:
            if item.status in {"confirmed", "expired", "error"}:
                return self._public(item, now)
            if item.in_flight:
                raise XianyuLoginError("login_busy")
            if item.last_poll_at and now - item.last_poll_at < self._poll_interval:
                return self._public(item, now)
            item.in_flight = True
            item.last_poll_at = now
            query_token = item.query_token
            query_ck = item.query_ck

        next_status = item.status
        login_token = ""
        cookie_header = ""
        error_code = ""
        with item.lock:
            try:
                try:
                    payload = self._call_json(
                        item,
                        "POST",
                        QUERY_URL,
                        params={"appName": "xianyu", "fromSite": "77"},
                        data={
                            "appName": "xianyu",
                            "fromSite": "77",
                            "appEntrance": "web",
                            "t": query_token,
                            "ck": query_ck,
                        },
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Origin": "https://passport.goofish.com",
                            "Referer": "https://passport.goofish.com/mini_login.htm",
                        },
                    )
                    data = _content_data(payload)
                except XianyuLoginError as error:
                    if error.code in {"login_busy", "network_error"}:
                        raise
                    raise XianyuLoginError("qr_query_failed") from None
                upstream_status = data.get("qrCodeStatus")
                if not isinstance(upstream_status, str):
                    raise XianyuLoginError("qr_query_failed")
                upstream_status = upstream_status.upper()
                if upstream_status == "NEW":
                    next_status = "waiting"
                elif upstream_status in {"SCANED", "SCANNED"}:
                    next_status = "scanned"
                elif upstream_status in {"EXPIRED", "CANCELED"}:
                    next_status = "expired"
                elif upstream_status == "CONFIRMED":
                    try:
                        login_token = _login_token(data)
                    except XianyuLoginError:
                        raise XianyuLoginError("login_confirm_failed") from None
                    cookie_header = self._confirm(item, login_token)
                    next_status = "confirmed"
                else:
                    raise XianyuLoginError("qr_query_failed")
            except XianyuLoginError as exc:
                error_code = exc.code
            except Exception:
                error_code = "platform_error"

        with self._lock:
            current_now = float(self._clock())
            current = self._items.get(item.login_id)
            if current is not item:
                self._cleanup(item)
                raise XianyuLoginError("login_not_found")
            if current_now >= item.expires_at or next_status == "expired":
                item.in_flight = False
                self._expire(item, current_now)
                return self._public(item, current_now)
            item.in_flight = False
            item.login_token = ""
            if error_code in {"login_busy", "network_error"}:
                raise XianyuLoginError(error_code)
            if error_code:
                item.status = "error"
                item.error_code = error_code
                payload = self._public(item, current_now)
                self._items.pop(item.login_id, None)
                scope = (item.user_id, item.account_key)
                if self._by_user.get(scope) == item.login_id:
                    self._by_user.pop(scope, None)
                self._cleanup(item)
                return payload
            item.status = next_status
            item.error_code = ""
            if next_status == "confirmed":
                item.cookie_header = cookie_header
                item.expires_at = current_now + self._confirmed_ttl
                item.query_token = ""
                item.query_ck = ""
                item.qr_url = ""
                item.qr_svg = b""
                self._close_session(item)
            return self._public(item, current_now)

    def begin_consume(self, user_id, login_id, account_key=DEFAULT_ACCOUNT_ID) -> str:
        item, _now = self._get(user_id, login_id, account_key)
        with self._lock:
            if item.status == "expired":
                raise XianyuLoginError("login_expired")
            if item.consumed:
                raise XianyuLoginError("login_consumed")
            if item.consuming:
                raise XianyuLoginError("login_busy")
            if item.status != "confirmed" or not item.cookie_header:
                raise XianyuLoginError("login_not_confirmed")
            item.consuming = True
            return item.cookie_header

    def finish_consume(
        self,
        user_id,
        login_id,
        success: bool,
        account_key=DEFAULT_ACCOUNT_ID,
    ) -> None:
        item, now = self._get(user_id, login_id, account_key)
        with self._lock:
            if not item.consuming:
                raise XianyuLoginError("login_not_confirmed")
            item.consuming = False
            if success:
                item.consumed = True
                self._items.pop(item.login_id, None)
                scope = (item.user_id, item.account_key)
                if self._by_user.get(scope) == item.login_id:
                    self._by_user.pop(scope, None)
                self._cleanup(item)
            elif now >= item.expires_at:
                self._expire(item, now)

    def consume(self, user_id, login_id, account_key=DEFAULT_ACCOUNT_ID) -> str:
        """Compatibility helper; application code uses the two-phase API."""
        cookie_header = self.begin_consume(user_id, login_id, account_key)
        self.finish_consume(user_id, login_id, True, account_key)
        return cookie_header

    def cancel(self, user_id, login_id, account_key=DEFAULT_ACCOUNT_ID) -> None:
        item, _now = self._get(user_id, login_id, account_key)
        with self._lock:
            if item.consuming:
                raise XianyuLoginError("login_busy")
            self._items.pop(item.login_id, None)
            scope = (item.user_id, item.account_key)
            if self._by_user.get(scope) == item.login_id:
                self._by_user.pop(scope, None)
        with item.lock:
            self._cleanup(item)

    def clear_user(
        self,
        user_id,
        preserve_cooldown: bool = False,
        account_key=DEFAULT_ACCOUNT_ID,
    ) -> None:
        uid, key = self._scope(user_id, account_key)
        scope = (uid, key)
        with self._lock:
            self._by_user.pop(scope, None)
            if not preserve_cooldown:
                self._last_start.pop(scope, None)
            items = [
                item
                for item in self._items.values()
                if item.user_id == uid and item.account_key == key
            ]
            for item in items:
                self._items.pop(item.login_id, None)
        for item in items:
            # An upstream request that currently owns the lock will observe
            # that the item was detached and clean itself before returning.
            if not item.lock.acquire(blocking=False):
                continue
            try:
                self._cleanup(item)
            finally:
                item.lock.release()

    def clear_user_all(self, user_id, preserve_cooldown: bool = False) -> None:
        """Clear QR sessions for every account owned by one user.

        Account-scoped callers use :meth:`clear_user`; logout needs the wider
        operation so a session for a secondary shop cannot survive sign-out.
        """
        uid = _safe_user_id(user_id)
        with self._lock:
            scopes = {
                scope for scope in self._by_user if scope[0] == uid
            }
            for scope in scopes:
                self._by_user.pop(scope, None)
                if not preserve_cooldown:
                    self._last_start.pop(scope, None)
            items = [item for item in self._items.values() if item.user_id == uid]
            for item in items:
                self._items.pop(item.login_id, None)
        for item in items:
            if not item.lock.acquire(blocking=False):
                continue
            try:
                self._cleanup(item)
            finally:
                item.lock.release()

    def clear(self) -> None:
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
            self._by_user.clear()
            self._last_start.clear()
        for item in items:
            with item.lock:
                self._cleanup(item)

    def shutdown(self) -> None:
        self._stop_sweeper.set()
        self.clear()
        if threading.current_thread() is not self._sweeper:
            self._sweeper.join(timeout=self._sweep_interval + 0.5)


qr_logins = XianyuLoginManager()
