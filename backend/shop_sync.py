"""Low-frequency, read-only Xianyu account and product synchronization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from account_storage import AccountStorage, AccountStorageError, DEFAULT_ACCOUNT_ID, normalize_account_key
from platform_profile import browser_headers


APP_KEY = "34839810"
MTOP_HOST = "https://h5api.m.goofish.com"
PROFILE_API = "mtop.taobao.idlemessage.pc.loginuser.get"
PRODUCTS_API = "mtop.idle.web.xyh.item.list"
PAGE_SIZE = 20
MAX_ITEMS = 100
MAX_PAGES = 5
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT = max(3, min(int(os.environ.get("SAAS_SHOP_SYNC_REQUEST_TIMEOUT_SECONDS", "8")), 15))
REQUEST_INTERVAL = max(0.2, min(float(os.environ.get("SAAS_SHOP_SYNC_REQUEST_INTERVAL_SECONDS", "0.5")), 5.0))
SYNC_MAX_SECONDS = max(15, min(int(os.environ.get("SAAS_SHOP_SYNC_MAX_SECONDS", "55")), 65))
SYNC_COOLDOWN_SECONDS = max(1, int(os.environ.get("SAAS_SHOP_SYNC_COOLDOWN_SECONDS", "60")))
TENANTS_ROOT = os.environ.get("SAAS_TENANTS_DIR", "/var/lib/xianyu-saas/tenants")
CIRCUIT_PATH = os.path.join(os.path.dirname(TENANTS_ROOT), "shop-sync-circuit.json")
SNAPSHOT_NAME = "shop_snapshot.json"
SYNC_STATE_NAME = "shop_sync_state.json"
SYNC_STATE_VERSION = 1

# These values are deliberately small and stable: they are exposed to the
# browser as connection state and never contain platform response text or a
# Cookie value.
SYNC_STATUS_CATALOG = {
    "unconfigured": {
        "label": "未连接",
        "message": "还没有连接闲鱼店铺",
        "action": "请粘贴 Cookie 并开始检测",
    },
    "pending": {
        "label": "待检测",
        "message": "Cookie 已保存，等待完成检测",
        "action": "点击重新检测",
    },
    "verified": {
        "label": "已验证",
        "message": "Cookie 已验证（不显示内容）",
        "action": "可随时重新检测店铺商品",
    },
    "risk_control": {
        "label": "需要安全验证",
        "message": "闲鱼要求安全验证，请先在闲鱼 App 或浏览器完成安全验证",
        "action": "完成安全验证后重新检测",
    },
    "account_restricted": {
        "label": "账号受限",
        "message": "闲鱼限制了当前账号的部分操作，暂时不能发布商品",
        "action": "请在闲鱼官方页面查看处理通知，处理后重新检测",
    },
    "risk_cooldown": {
        "label": "安全验证冷却中",
        "message": "闲鱼安全验证冷却中，请稍后再检测",
        "action": "等待冷却结束后重新检测",
    },
    "cookie_expired": {
        "label": "Cookie 已失效",
        "message": "Cookie 已失效，请重新登录闲鱼并重新获取 Cookie",
        "action": "重新获取完整 Cookie 后粘贴",
    },
    "cookie_invalid": {
        "label": "Cookie 格式有误",
        "message": "Cookie 格式无效，请重新复制完整 Cookie",
        "action": "重新复制完整 Cookie 后粘贴",
    },
    "cookie_incomplete": {
        "label": "需要重新获取",
        "message": "Cookie 缺少登录信息，请重新复制完整 Cookie",
        "action": "重新获取完整 Cookie 后粘贴",
    },
    "sync_error": {
        "label": "检测失败",
        "message": "暂时无法确认 Cookie 状态，请稍后重新检测",
        "action": "稍后重新检测",
    },
    "bot_running": {
        "label": "自动客服运行中",
        "message": "请先暂停自动客服，再更新店铺 Cookie",
        "action": "暂停后重新检测",
    },
    "sync_cooldown": {
        "label": "操作太频繁",
        "message": "操作太频繁，请稍后再检测",
        "action": "稍后再检测",
    },
    "sync_busy": {
        "label": "正在检测",
        "message": "已有店铺检测正在进行，请稍后再试",
        "action": "等待当前检测完成",
    },
    "sync_timeout": {
        "label": "检测超时",
        "message": "店铺商品较多，检测超时，请稍后再试",
        "action": "稍后重新检测",
    },
    "profile_missing": {
        "label": "账号信息不完整",
        "message": "已连接账号，但没有读取到闲鱼昵称，请稍后再检测",
        "action": "稍后重新检测",
    },
    "platform_error": {
        "label": "闲鱼暂时不可用",
        "message": "闲鱼暂时无法识别该账号，请稍后再检测",
        "action": "稍后重新检测",
    },
    "network_error": {
        "label": "网络检测失败",
        "message": "暂时无法连接闲鱼，请稍后再检测",
        "action": "稍后重新检测",
    },
    "platform_busy": {
        "label": "闲鱼请求繁忙",
        "message": "闲鱼当前请求繁忙，系统会降低频率后再试",
        "action": "请稍后重新检测",
    },
}
PERSISTED_SYNC_CODES = frozenset(
    {
        "risk_control",
        "account_restricted",
        "risk_cooldown",
        "cookie_expired",
        "cookie_invalid",
        "cookie_incomplete",
    }
)

ACCOUNT_RESTRICTION_MARKERS = (
    "ACCOUNT_BANNED",
    "USER_BANNED",
    "PUBLISH_FORBIDDEN",
    "ITEM_PUBLISH_FORBIDDEN",
    "VIOLATION",
    "违规",
    "限制发布",
    "禁止发布",
)
RISK_CONTROL_MARKERS = (
    "RGV587",
    "USER_VALIDATE",
    "USER_VALID",
    "LOGIN_CHECK",
    "SECURITY_CHECK",
    "CAPTCHA",
    "/PUNISH",
    "被挤爆",
)
SESSION_EXPIRED_MARKERS = (
    "SESSION_EXPIRED",
    "SESSION INVALID",
    "TOKEN_EX",
    "TOKEN_EMPTY",
    "ILLEGAL_ACCESS",
    "LOGIN_EXPIRED",
    "NOT_LOGIN",
    "AUTH_EXPIRED",
    "AUTH_INVALID",
)
PLATFORM_BUSY_MARKERS = (
    "FAIL_SYS_BUSY",
    "SYSTEM_BUSY",
    "SERVER_BUSY",
    "TOO_MANY_REQUESTS",
    "RATE_LIMIT",
    "FREQUENCY_LIMIT",
)

_request_lock = threading.Lock()
_last_request_at = 0.0
_sync_lock = threading.Lock()
_last_sync_by_tenant: dict[tuple[int, str], float] = {}
_sync_gate = threading.Lock()


def _account_storage() -> AccountStorage:
    return AccountStorage(TENANTS_ROOT)


def _account_root(user_id: int, account_key: str | None = DEFAULT_ACCOUNT_ID, *, create: bool = False) -> str:
    storage = _account_storage()
    try:
        path = (
            storage.ensure_account_dir(user_id, account_key)
            if create
            else storage.account_dir(user_id, account_key)
        )
    except AccountStorageError as error:
        raise OSError("invalid account storage path") from error
    return str(path)


class ShopSyncError(RuntimeError):
    """A safe synchronization failure that never contains a platform response."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def sync_status_payload(code: str, message: str | None = None, checked_at: str = "") -> dict:
    """Return a bounded, browser-safe description of a sync status."""
    candidate = str(code or "")
    normalized = candidate if re.fullmatch(r"[a-z][a-z0-9_]{1,48}", candidate) else "sync_error"
    catalog = SYNC_STATUS_CATALOG.get(normalized, SYNC_STATUS_CATALOG["sync_error"])
    return {
        "code": normalized,
        "label": catalog["label"],
        "message": (message or catalog["message"])[:240],
        "action": catalog["action"],
        "checked_at": str(checked_at or "")[:40],
    }


def save_sync_state(
    user_id: int,
    code: str,
    message: str | None = None,
    checked_at: str = "",
    account_ref_value: str = "",
    account_key: str = DEFAULT_ACCOUNT_ID,
) -> None:
    """Persist only non-secret connection state for the tenant."""
    status = sync_status_payload(code, message, checked_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    payload = {
        "version": SYNC_STATE_VERSION,
        "code": status["code"],
        "message": status["message"],
        "checked_at": status["checked_at"],
        "account_ref": str(account_ref_value or "")[:64],
    }
    root = _account_root(user_id, account_key, create=True)
    _account_storage().atomic_write_path(os.path.join(root, SYNC_STATE_NAME), json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def load_sync_state(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> dict | None:
    """Load validated connection state; malformed state fails closed."""
    try:
        path = os.path.join(_account_root(user_id, account_key), SYNC_STATE_NAME)
    except OSError:
        return None
    try:
        with open(path, encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != SYNC_STATE_VERSION:
        return None
    code = payload.get("code")
    if code not in SYNC_STATUS_CATALOG:
        return None
    status = sync_status_payload(code, payload.get("message"), payload.get("checked_at", ""))
    status["account_ref"] = str(payload.get("account_ref") or "")[:64]
    return status


def parse_cookie_header(value: str) -> tuple[str, dict[str, str]]:
    if not isinstance(value, str):
        raise ShopSyncError("cookie_invalid", "Cookie 格式无效")
    normalized = value.strip()
    if normalized.lower().startswith("cookie:"):
        normalized = normalized[7:].strip()
    if not normalized or "\r" in normalized or "\n" in normalized:
        raise ShopSyncError("cookie_invalid", "Cookie 格式无效")

    cookies: dict[str, str] = {}
    for part in normalized.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if not separator or not name:
            continue
        if not name.isascii() or any(character.isspace() for character in name):
            raise ShopSyncError("cookie_invalid", "Cookie 格式无效")
        cookies[name] = cookie_value.strip()

    user_id = cookies.get("unb", "")
    token_value = cookies.get("_m_h5_tk", "")
    token = token_value.split("_", 1)[0]
    if (
        not user_id.isascii()
        or not user_id.isdigit()
        or len(user_id) > 64
        or not token
        or len(token) > 256
        or not token.isascii()
    ):
        raise ShopSyncError("cookie_incomplete", "Cookie 缺少有效的登录信息，请重新复制完整 Cookie")
    return normalized, cookies


def account_ref(cookies: dict[str, str]) -> str:
    return hashlib.sha256(cookies["unb"].encode("utf-8")).hexdigest()[:16]


def reserve_sync(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> None:
    """Apply a per-tenant cooldown before using the shared platform egress."""
    now = time.monotonic()
    with _sync_lock:
        scope = (int(user_id), normalize_account_key(account_key))
        previous = _last_sync_by_tenant.get(scope, 0.0)
        if previous and now - previous < SYNC_COOLDOWN_SECONDS:
            raise ShopSyncError("sync_cooldown", "操作太频繁，请稍后再试")
        _last_sync_by_tenant[scope] = now


def _safe_text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split()).strip()
    return value[:limit]


# The profile endpoint has returned several equivalent shapes over time. Keep
# the traversal deliberately allow-listed so a product/card field can never
# accidentally become the seller identity.
_PROFILE_CONTAINER_KEYS = (
    "data",
    "userInfo",
    "userInfoModel",
    "currentUser",
    "profile",
    "user",
    "account",
    "result",
)
_PROFILE_NICK_KEYS = (
    "nick",
    "nickname",
    "nickName",
    "userNick",
    "userNickname",
    "userNickName",
    "fishNick",
    "nicknameStr",
    "displayName",
)
_PROFILE_ID_KEYS = ("userId", "user_id", "userIdStr", "accountId", "uid")


def _profile_nodes(value):
    """Yield bounded, allow-listed profile dictionaries from mtop data."""
    pending = [value]
    visited = set()
    while pending and len(visited) < 16:
        node = pending.pop(0)
        if not isinstance(node, dict) or id(node) in visited:
            continue
        visited.add(id(node))
        yield node
        for key in _PROFILE_CONTAINER_KEYS:
            child = node.get(key)
            if isinstance(child, dict):
                pending.append(child)
            elif isinstance(child, str) and len(child) <= MAX_RESPONSE_BYTES:
                try:
                    decoded = json.loads(child)
                except (TypeError, ValueError):
                    continue
                if isinstance(decoded, dict):
                    pending.append(decoded)


def _profile_text(value, keys, limit: int) -> str:
    for node in _profile_nodes(value):
        for key in keys:
            candidate = _safe_text(node.get(key), limit)
            if candidate:
                return candidate
    return ""


def _profile_id(value) -> str:
    for node in _profile_nodes(value):
        for key in _PROFILE_ID_KEYS:
            candidate = node.get(key)
            if candidate in (None, "") or isinstance(candidate, bool):
                continue
            normalized = str(candidate).strip()
            if normalized.isascii() and normalized.isdigit() and len(normalized) <= 64:
                return normalized
    return ""


def _cookie_nickname(cookies: dict[str, str]) -> str:
    """Use only the platform's display-name cookies as a bounded fallback."""
    for key in ("tracknick", "lgc", "nick"):
        raw = cookies.get(key, "")
        if not isinstance(raw, str) or not raw:
            continue
        candidate = _safe_text(urllib.parse.unquote_plus(raw), 80)
        if candidate:
            return candidate
    return ""


def _price_value(value) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).strip().replace(",", "")
    text = re.sub(r"^[^0-9.+-]+", "", text)
    match = re.match(r"^[+]?(\d+(?:\.\d+)?)", text)
    return match.group(1) if match else ""


def _image_url(value) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if len(candidate) > 2048 or any(character.isspace() for character in candidate):
        return ""
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return candidate


def _product_image_url(card: dict) -> str:
    candidates = []
    pic_info = card.get("picInfo")
    if isinstance(pic_info, dict):
        candidates.append(pic_info.get("picUrl"))
    detail_params = card.get("detailParams")
    if isinstance(detail_params, dict):
        candidates.append(detail_params.get("picUrl"))
    candidates.extend(
        card.get(key)
        for key in ("image_url", "imageUrl", "picUrl")
    )
    for candidate in candidates:
        normalized = _image_url(candidate)
        if normalized:
            return normalized
    return ""


def _text_labels(card: dict) -> list[str]:
    labels: list[str] = []
    label_vo = card.get("itemLabelDataVO")
    label_data = label_vo.get("labelData") if isinstance(label_vo, dict) else None
    if not isinstance(label_data, dict):
        return labels
    for region in label_data.values():
        tags = region.get("tagList") if isinstance(region, dict) else None
        if not isinstance(tags, list):
            continue
        for tag in tags:
            data = tag.get("data") if isinstance(tag, dict) else None
            if not isinstance(data, dict) or data.get("type") == "img":
                continue
            content = _safe_text(data.get("content"), 60)
            if content and content not in labels:
                labels.append(content)
    return labels[:4]


def extract_product(card: dict, synced_at: str) -> dict | None:
    if not isinstance(card, dict):
        return None
    item_id = str(card.get("id") or card.get("itemId") or "").strip()
    if not item_id.isascii() or not item_id.isdigit() or len(item_id) > 64:
        return None

    summary = card.get("titleSummary")
    summary_title = summary.get("text") if isinstance(summary, dict) else ""
    title = _safe_text(card.get("title") or summary_title, 160) or "未命名商品"
    price_info = card.get("priceInfo")
    price = _price_value(price_info.get("price") if isinstance(price_info, dict) else card.get("price"))
    description = _safe_text(
        card.get("desc")
        or card.get("description")
        or card.get("itemDesc")
        or card.get("summary"),
        240,
    )
    labels = _text_labels(card)
    if not description and labels:
        description = " · ".join(labels)
    status_code = str(card.get("itemStatus", ""))
    status = {"0": "在售", "1": "已下架"}.get(status_code, _safe_text(card.get("itemStatusStr"), 24))
    product = {
        "id": item_id,
        "title": title,
        "description": description,
        "price": price,
        "status": status,
        "source": "cookie",
        "updated_at": synced_at,
    }
    image_url = _product_image_url(card)
    if image_url:
        product["image_url"] = image_url
    return product


def _atomic_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".shop-sync-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_snapshot(user_id: int, payload: dict, account_key: str = DEFAULT_ACCOUNT_ID) -> None:
    """Persist a non-secret, account-bound product snapshot."""
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("invalid shop snapshot")
    root = _account_root(user_id, account_key, create=True)
    _account_storage().atomic_write_path(
        os.path.join(root, SNAPSHOT_NAME),
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def load_verified_snapshot(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> dict | None:
    """Return a snapshot only when it belongs to the currently stored Cookie."""
    try:
        root = _account_root(user_id, account_key)
    except OSError:
        return None
    try:
        with open(os.path.join(root, SNAPSHOT_NAME), encoding="utf-8") as file:
            snapshot = json.load(file)
        with open(os.path.join(root, "cookies.txt"), encoding="utf-8") as file:
            cookie_header = file.read().strip()
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        return None
    try:
        _, cookies = parse_cookie_header(cookie_header)
    except ShopSyncError:
        return None
    if snapshot.get("account_ref") != account_ref(cookies):
        return None
    if not isinstance(snapshot.get("nickname"), str) or not isinstance(snapshot.get("products"), list):
        return None
    return snapshot


def _circuit_until() -> float:
    try:
        with open(CIRCUIT_PATH, encoding="utf-8") as file:
            payload = json.load(file)
        return float(payload.get("until", 0)) if isinstance(payload, dict) else 0
    except (OSError, TypeError, ValueError):
        return 0


def _trip_circuit() -> None:
    _atomic_json(CIRCUIT_PATH, {"until": time.time() + 600})


def _failure_code_from_text(value: str) -> str:
    """Classify a platform failure without returning or logging its raw text."""
    text = str(value or "").upper()
    if any(marker in text for marker in ACCOUNT_RESTRICTION_MARKERS):
        return "account_restricted"
    if any(marker in text for marker in RISK_CONTROL_MARKERS):
        return "risk_control"
    if any(marker in text for marker in SESSION_EXPIRED_MARKERS):
        return "cookie_expired"
    if any(marker in text for marker in PLATFORM_BUSY_MARKERS):
        return "platform_busy"
    return "platform_error"


def _raise_classified_failure(code: str) -> None:
    if code == "account_restricted":
        raise ShopSyncError(code, "闲鱼限制了当前账号的部分操作，暂时不能发布商品")
    if code == "risk_control":
        _trip_circuit()
        raise ShopSyncError(code, "闲鱼需要安全验证，请先在浏览器完成验证后再试")
    if code == "cookie_expired":
        raise ShopSyncError(code, "Cookie 已失效，请重新登录闲鱼后复制完整 Cookie")
    if code == "platform_busy":
        raise ShopSyncError(code, "闲鱼当前请求繁忙，请稍后重试")
    raise ShopSyncError("platform_error", "闲鱼暂时无法识别该账号，请稍后重试")


def _classify_response(raw) -> dict:
    if not isinstance(raw, dict):
        raise ShopSyncError("platform_error", "闲鱼返回异常，请稍后重试")
    ret = raw.get("ret") or []
    if isinstance(ret, str):
        ret = [ret]
    ret_text = " ".join(str(item) for item in ret)
    if "SUCCESS" not in ret_text.upper():
        # The browser receives only a stable code and catalog copy, never the
        # platform response text used for this classification.
        _raise_classified_failure(_failure_code_from_text(ret_text))
    data = raw.get("data")
    if isinstance(data, str) and len(data) <= MAX_RESPONSE_BYTES:
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            data = {}
    return data if isinstance(data, dict) else {}


def _request(cookie_header: str, cookies: dict[str, str], api: str, data: dict, spm_cnt: str) -> dict:
    global _last_request_at
    if _circuit_until() > time.time():
        raise ShopSyncError("risk_cooldown", "闲鱼安全验证冷却中，请稍后再试")

    data_value = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    timestamp = str(int(time.time() * 1000))
    token = cookies["_m_h5_tk"].split("_", 1)[0]
    signature = hashlib.md5(f"{token}&{timestamp}&{APP_KEY}&{data_value}".encode("utf-8")).hexdigest()
    params = urllib.parse.urlencode(
        {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": timestamp,
            "sign": signature,
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": spm_cnt,
        }
    )
    endpoint = f"{MTOP_HOST}/h5/{api}/1.0/?{params}"
    body = urllib.parse.urlencode({"data": data_value}).encode("utf-8")
    headers = browser_headers()
    headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": cookie_header,
        }
    )
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers=headers,
    )

    try:
        with _request_lock:
            remaining = REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
            if remaining > 0:
                time.sleep(remaining)
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw_body = response.read(MAX_RESPONSE_BYTES + 1)
            _last_request_at = time.monotonic()
        if len(raw_body) > MAX_RESPONSE_BYTES:
            raise ShopSyncError("platform_error", "闲鱼返回数据过大，请稍后重试")
        return json.loads(raw_body.decode("utf-8"))
    except ShopSyncError:
        raise
    except urllib.error.HTTPError as error:
        # Read only a bounded body for internal classification. Raw platform
        # text is never logged, persisted or returned to the browser.
        try:
            error_body = error.read(min(MAX_RESPONSE_BYTES, 64 * 1024)).decode("utf-8", errors="ignore")
        except (OSError, UnicodeError):
            error_body = ""
        body_code = _failure_code_from_text(error_body)
        if body_code != "platform_error":
            _raise_classified_failure(body_code)
        if error.code in {401, 403}:
            raise ShopSyncError("cookie_expired", "Cookie 已失效，请重新登录闲鱼后复制完整 Cookie") from None
        if error.code in {409, 412, 429}:
            raise ShopSyncError("platform_busy", "闲鱼当前请求繁忙，请稍后重试") from None
        raise ShopSyncError("network_error", "暂时无法连接闲鱼，请稍后重试") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError, OSError):
        raise ShopSyncError("network_error", "暂时无法连接闲鱼，请稍后重试") from None


def sync_shop(cookie_header: str, request_func=None) -> dict:
    if not _sync_gate.acquire(blocking=False):
        raise ShopSyncError("sync_busy", "已有店铺同步正在进行，请稍后再试")
    started_at = time.monotonic()
    try:
        normalized, cookies = parse_cookie_header(cookie_header)
        call = request_func or (lambda api, data, spm: _request(normalized, cookies, api, data, spm))

        profile = _classify_response(call(PROFILE_API, {}, "a21ybx.im.0.0"))
        # Some account responses expose the authenticated numeric ID.  When
        # present, require it to agree with ``unb`` so a mixed Cookie header
        # cannot bind one seller's profile to another seller's snapshot.  A
        # response without an ID remains compatible with older mtop shapes.
        profile_id = _profile_id(profile)
        if profile_id and profile_id != cookies.get("unb", ""):
            raise ShopSyncError("cookie_invalid", "登录账号与闲鱼店铺信息不匹配，请重新登录")
        nickname = _profile_text(profile, _PROFILE_NICK_KEYS, 80) or _cookie_nickname(cookies)
        if not nickname:
            raise ShopSyncError("profile_missing", "已验证账号，但没有读取到闲鱼昵称，请稍后重试")

        synced_at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        items: list[dict] = []
        seen: set[str] = set()
        page = 1
        truncated = False
        while page <= MAX_PAGES and len(items) < MAX_ITEMS:
            if time.monotonic() - started_at >= SYNC_MAX_SECONDS:
                raise ShopSyncError("sync_timeout", "店铺商品较多，同步超时，请稍后重试")
            data = _classify_response(
                call(
                    PRODUCTS_API,
                    {
                        "needGroupInfo": True,
                        "pageNumber": page,
                        "userId": cookies["unb"],
                        "pageSize": PAGE_SIZE,
                    },
                    "a21ybx.item.0.0",
                )
            )
            before = len(items)

            candidates = []
            if page == 1 and isinstance(data.get("topItem"), dict):
                candidates.append(data["topItem"])
            if isinstance(data.get("cardList"), list):
                candidates.extend(data["cardList"])
            for candidate in candidates:
                card = candidate.get("cardData", candidate) if isinstance(candidate, dict) else None
                product = extract_product(card, synced_at)
                if product and product["id"] not in seen:
                    seen.add(product["id"])
                    items.append(product)
                    if len(items) >= MAX_ITEMS:
                        break

            next_page = data.get("nextPage")
            has_next = next_page is True or next_page == 1 or (
                isinstance(next_page, str) and next_page.strip().lower() in {"1", "true", "yes"}
            )
            if not has_next or len(items) == before:
                break
            if page == MAX_PAGES or len(items) >= MAX_ITEMS:
                truncated = True
                break
            page += 1

        return {
            "version": 1,
            "account_ref": account_ref(cookies),
            "nickname": nickname,
            "products": items,
            "product_count": len(items),
            "synced_at": synced_at,
            "truncated": truncated,
        }
    finally:
        _sync_gate.release()
