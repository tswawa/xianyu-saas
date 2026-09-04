import base64
import json
import asyncio
import difflib
import hashlib
import math
import re
import sqlite3
import stat
import time
import os
import uuid
import websockets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from loguru import logger
from dotenv import load_dotenv
from XianyuApis import XianyuApis, XianyuApiError, XianyuAuthenticationError
from auth_state import AuthStateStore
from platform_profile import DINGTALK_REGISTRATION_UA, websocket_headers
from private_auth_storage import PrivateAuthStorage
import sys
import random
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_CONFIG_FILE = os.getenv('PRODUCTS_CONFIG_FILE', os.path.join(BASE_DIR, 'products_config.json'))
DEFAULT_AUTOMATION_MODE = 'rules'

VALID_AUTOMATION_MODES = frozenset({'rules', 'rules_ai'})
VALID_AUTOMATION_STRATEGIES = frozenset({'conservative', 'standard', 'aggressive'})
API_KEY_PLACEHOLDERS = frozenset({
    '',
    'your_api_key_here',
    '默认使用通义千问,apikey通过百炼模型平台获取',
})
MAX_REPLY_RULES = 100
MAX_REPLY_RULE_KEYWORDS = 20
MAX_REPLY_RULE_KEYWORD_CHARS = 128
MAX_REPLY_RULE_REPLY_CHARS = 4096
MAX_REPLY_RULE_FILE_BYTES = 256 * 1024
MAX_MATERIAL_PAYLOAD_CHARS = 16 * 1024

try:
    MERCHANT_TIMEZONE = ZoneInfo("Asia/Shanghai")
except Exception:
    # Some minimal container images omit the system tzdata database. China has
    # used a stable UTC+8 offset for the dates relevant to this service.
    MERCHANT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def normalize_automation_mode(value=None):
    """Return a supported worker mode, failing closed for unknown values."""
    raw = os.getenv('AUTOMATION_MODE', DEFAULT_AUTOMATION_MODE) if value is None else value
    mode = str(raw or '').strip().lower()
    if mode not in VALID_AUTOMATION_MODES:
        raise RuntimeError('AUTOMATION_MODE 必须是 rules 或 rules_ai')
    return mode


def normalize_automation_strategy(value=None):
    """Return one of the small, deterministic workbench strategy presets."""
    raw = os.getenv('AUTOMATION_STRATEGY', 'standard') if value is None else value
    strategy = str(raw or '').strip().lower()
    if strategy not in VALID_AUTOMATION_STRATEGIES:
        raise RuntimeError('AUTOMATION_STRATEGY 必须是 conservative、standard 或 aggressive')
    return strategy


def load_automation_settings(path=None):
    """Read one strict account-scoped settings file or raise on corruption."""
    defaults = {
        "version": 1,
        "strategy": "standard",
        "enabled": True,
        "first_reply": "",
        "fallback_reply": "",
        "delay_min_seconds": 0,
        "delay_max_seconds": 0,
        "trigger_cooldown_seconds": 0,
        "manual_takeover_cooldown_seconds": 0,
        "business_hours_enabled": False,
        "business_start": "09:00",
        "business_end": "23:30",
    }
    candidate = os.getenv("AUTOMATION_SETTINGS_FILE", "") if path is None else path
    if not candidate:
        return defaults
    payload = load_private_json_file(candidate, dict)
    unknown = set(payload) - set(defaults)
    if unknown:
        raise RuntimeError("自动化设置包含未知字段")
    if payload.get("version", 1) != 1:
        raise RuntimeError("自动化设置必须使用 version=1")

    strategy = payload.get("strategy", defaults["strategy"])
    if not isinstance(strategy, str) or strategy.strip().lower() not in VALID_AUTOMATION_STRATEGIES:
        raise RuntimeError("自动化策略无效")
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RuntimeError("自动化开关无效")

    control_re = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
    for key in ("first_reply", "fallback_reply"):
        value = payload.get(key, "")
        if not isinstance(value, str) or len(value.strip()) > 1000 or control_re.search(value):
            raise RuntimeError(f"自动化设置 {key} 无效")
        defaults[key] = value.strip()
    for key in ("delay_min_seconds", "delay_max_seconds"):
        value = payload.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 60:
            raise RuntimeError(f"自动化设置 {key} 无效")
        defaults[key] = value
    for key in ("trigger_cooldown_seconds", "manual_takeover_cooldown_seconds"):
        value = payload.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 300:
            raise RuntimeError(f"自动化设置 {key} 无效")
        defaults[key] = value
    business_hours_enabled = payload.get("business_hours_enabled", False)
    if not isinstance(business_hours_enabled, bool):
        raise RuntimeError("营业时间开关无效")
    defaults["business_hours_enabled"] = business_hours_enabled
    for key in ("business_start", "business_end"):
        value = payload.get(key, defaults[key])
        if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value.strip()):
            raise RuntimeError(f"自动化设置 {key} 无效")
        defaults[key] = value.strip()
    defaults["strategy"] = strategy.strip().lower()
    defaults["enabled"] = enabled
    return defaults


def within_business_hours(settings, now=None):
    """True when merchant-local time falls inside the configured window."""
    if not settings.get("business_hours_enabled"):
        return True
    if now is None:
        current = datetime.now(MERCHANT_TIMEZONE)
    elif now.tzinfo is not None:
        current = now.astimezone(MERCHANT_TIMEZONE)
    else:
        # Naive values are treated as merchant-local for deterministic tests and
        # backwards compatibility with existing callers.
        current = now
    start_text = str(settings.get("business_start") or "09:00")
    end_text = str(settings.get("business_end") or "23:30")
    try:
        start_hour, start_minute = (int(part) for part in start_text.split(":", 1))
        end_hour, end_minute = (int(part) for part in end_text.split(":", 1))
    except (TypeError, ValueError):
        return True
    current_minutes = current.hour * 60 + current.minute
    start_minutes = start_hour * 60 + start_minute
    end_minutes = end_hour * 60 + end_minute
    if start_minutes <= end_minutes:
        return start_minutes <= current_minutes <= end_minutes
    return current_minutes >= start_minutes or current_minutes <= end_minutes


def _reply_rules_file_signature(path):
    try:
        stat_result = os.lstat(path)
    except OSError:
        return None
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_mtime_ns,
        stat_result.st_size,
        stat.S_IMODE(stat_result.st_mode),
    )


def load_private_json_file(path, expected_type, *, maximum_bytes=1024 * 1024):
    """Load a tenant runtime JSON file without following links or broad modes."""
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        stat_result = os.fstat(descriptor)
        if not stat.S_ISREG(stat_result.st_mode):
            raise RuntimeError(f"运行态配置必须是普通文件: {os.path.basename(path)}")
        if stat.S_IMODE(stat_result.st_mode) != 0o600:
            raise RuntimeError(f"运行态配置权限必须是 0600: {os.path.basename(path)}")
        if stat_result.st_size > maximum_bytes:
            raise RuntimeError(f"运行态配置文件过大: {os.path.basename(path)}")
        with os.fdopen(descriptor, 'r', encoding='utf-8') as handle:
            descriptor = None
            payload = json.load(handle)
    except RuntimeError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取运行态配置: {os.path.basename(path)}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(payload, expected_type):
        raise RuntimeError(f"运行态配置结构无效: {os.path.basename(path)}")
    return payload


def _ai_fact_text(value, limit=2000):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()[:limit]


def _ai_fact_number(value):
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return value
    return str(value).strip()[:80]


def _ai_product_facts(item_id, item_info):
    """Project current platform facts without loading or compiling merchant knowledge."""
    selected_item_id = str(item_id or "").strip()
    if not selected_item_id.isdigit() or len(selected_item_id) > 64:
        return {}
    if not isinstance(item_info, dict):
        return {"item_id": selected_item_id}
    raw_skus = item_info.get("sku") or item_info.get("skus") or item_info.get("skuList") or []
    clean_skus = []
    if isinstance(raw_skus, list):
        for raw in raw_skus[:50]:
            if not isinstance(raw, dict):
                continue
            properties = raw.get("propertyList") or raw.get("properties") or []
            labels = []
            if isinstance(properties, list):
                for prop in properties[:10]:
                    if not isinstance(prop, dict):
                        continue
                    label = prop.get("valueText") or prop.get("value") or prop.get("name")
                    if isinstance(label, str) and label.strip():
                        labels.append(label.strip()[:80])
            name = raw.get("name") or raw.get("title") or " ".join(labels)
            clean_skus.append({
                "name": str(name or "").strip()[:160],
                "price": _ai_fact_number(raw.get("price")),
                "stock": _ai_fact_number(
                    raw.get("stock") if "stock" in raw else raw.get("quantity")
                ),
            })
    return {
        "item_id": selected_item_id,
        "title": _ai_fact_text(item_info.get("title") or item_info.get("name"), 300),
        "description": _ai_fact_text(item_info.get("description") or item_info.get("desc"), 4000),
        "price": _ai_fact_number(
            item_info.get("price") if "price" in item_info else item_info.get("soldPrice")
        ),
        "stock": _ai_fact_number(
            item_info.get("stock") if "stock" in item_info else item_info.get("quantity")
        ),
        "status": _ai_fact_text(item_info.get("status") or item_info.get("itemStatus"), 80),
        "skus": clean_skus,
    }


def _normalized_ai_reply(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^\w\u4e00-\u9fa5]", "", value).casefold()


def _ai_replies_similar(candidate, previous):
    left = _normalized_ai_reply(candidate)
    right = _normalized_ai_reply(previous)
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 8:
        return False
    return difflib.SequenceMatcher(None, left, right).ratio() >= 0.9


def _unsafe_ai_reply(value):
    if not isinstance(value, str):
        return True
    clean = value.strip()
    if clean in {"-", "[TRIAL]", "[TUTORIAL]"}:
        return True
    if "```" in clean:
        return True
    if clean[:1] in {"{", "["}:
        try:
            decoded = json.loads(clean)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, (dict, list)):
            return True
    return False


def load_products_config_file(path):
    """Load the product map without following links and return its exact mode."""
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        stat_result = os.fstat(descriptor)
        if not stat.S_ISREG(stat_result.st_mode):
            raise RuntimeError("products_config.json 必须是普通文件")
        if stat_result.st_size > 1024 * 1024:
            raise RuntimeError("配置文件过大: products_config.json")
        with os.fdopen(descriptor, 'r', encoding='utf-8') as handle:
            descriptor = None
            payload = json.load(handle)
    except RuntimeError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("无法读取配置文件: products_config.json") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise RuntimeError("配置文件结构无效: products_config.json")
    return payload, stat.S_IMODE(stat_result.st_mode)


def load_reply_rules(path):
    """Load and strictly normalize version-1 deterministic reply rules.

    A missing file means that no deterministic rules are configured.  Existing
    files must carry an explicit version and a list of bounded rule objects;
    malformed content is rejected instead of being interpreted permissively.
    """
    if _reply_rules_file_signature(path) is None:
        return ()
    payload = load_private_json_file(
        path, dict, maximum_bytes=MAX_REPLY_RULE_FILE_BYTES
    )
    if set(payload) - {"version", "rules"}:
        raise RuntimeError('回复规则包含未知顶层字段')
    if payload.get('version') != 1:
        raise RuntimeError('回复规则必须使用 version=1')
    raw_rules = payload.get('rules')
    if not isinstance(raw_rules, list) or len(raw_rules) > MAX_REPLY_RULES:
        raise RuntimeError('回复规则列表无效或过长')

    normalized = []
    seen_rule_ids = set()
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict):
            raise RuntimeError(f'回复规则第 {index + 1} 项无效')
        if set(raw_rule) - {"id", "name", "item_id", "enabled", "match", "keywords", "reply"}:
            raise RuntimeError(f'回复规则第 {index + 1} 项包含未知字段')
        enabled = raw_rule.get('enabled', True)
        if not isinstance(enabled, bool):
            raise RuntimeError(f'回复规则第 {index + 1} 项 enabled 无效')
        name = raw_rule.get('name', f'规则 {index + 1}')
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or any(char in name for char in '\r\n'):
            raise RuntimeError(f'回复规则第 {index + 1} 项 name 无效')
        item_id = raw_rule.get('item_id', '')
        if item_id is None:
            item_id = ''
        if not isinstance(item_id, (str, int)) or isinstance(item_id, bool):
            raise RuntimeError(f'回复规则第 {index + 1} 项 item_id 无效')
        item_id = str(item_id).strip()
        if item_id and (len(item_id) > 64 or not item_id.isascii() or not item_id.isdigit()):
            raise RuntimeError(f'回复规则第 {index + 1} 项 item_id 无效')
        match = raw_rule.get('match', 'contains')
        if match != 'contains':
            raise RuntimeError(f'回复规则第 {index + 1} 项只支持 contains')
        keywords = raw_rule.get('keywords')
        if not isinstance(keywords, list) or not (1 <= len(keywords) <= MAX_REPLY_RULE_KEYWORDS):
            raise RuntimeError(f'回复规则第 {index + 1} 项关键词无效')
        clean_keywords = []
        seen_keywords = set()
        for keyword in keywords:
            if not isinstance(keyword, str):
                raise RuntimeError(f'回复规则第 {index + 1} 项关键词无效')
            clean_keyword = keyword.strip()
            if not clean_keyword or len(clean_keyword) > MAX_REPLY_RULE_KEYWORD_CHARS:
                raise RuntimeError(f'回复规则第 {index + 1} 项关键词过长或为空')
            normalized_keyword = clean_keyword.casefold()
            if normalized_keyword not in seen_keywords:
                seen_keywords.add(normalized_keyword)
                clean_keywords.append(normalized_keyword)
        if not clean_keywords:
            raise RuntimeError(f'回复规则第 {index + 1} 项没有有效关键词')
        reply = raw_rule.get('reply')
        if (
            not isinstance(reply, str)
            or not reply.strip()
            or len(reply.strip()) > MAX_REPLY_RULE_REPLY_CHARS
        ):
            raise RuntimeError(f'回复规则第 {index + 1} 项回复无效')
        rule_id = raw_rule.get('id', str(index + 1))
        if not isinstance(rule_id, (str, int)) or isinstance(rule_id, bool):
            raise RuntimeError(f'回复规则第 {index + 1} 项 id 无效')
        rule_id = str(rule_id).strip()
        if not rule_id or len(rule_id) > 128:
            raise RuntimeError(f'回复规则第 {index + 1} 项 id 无效')
        if rule_id in seen_rule_ids:
            raise RuntimeError(f'回复规则第 {index + 1} 项 id 重复')
        seen_rule_ids.add(rule_id)
        normalized.append(
            {
                'id': rule_id,
                'name': name.strip(),
                'item_id': item_id,
                'enabled': enabled,
                'match': 'contains',
                'keywords': tuple(clean_keywords),
                'reply': reply.strip(),
            }
        )
    return tuple(normalized)


def load_json_file(path, expected_type):
    try:
        if os.path.getsize(path) > 1024 * 1024:
            raise RuntimeError(f"配置文件过大: {os.path.basename(path)}")
        with open(path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except RuntimeError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取配置文件: {os.path.basename(path)}") from exc
    if not isinstance(payload, expected_type):
        raise RuntimeError(f"配置文件结构无效: {os.path.basename(path)}")
    return payload


def stable_ref(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]


def read_number_env(name, default, minimum, maximum, *, integer=False):
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw) if integer else float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} 必须是有效数字") from exc
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeError(f"{name} 必须是有限数字")
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


from utils.xianyu_utils import generate_mid, generate_uuid, trans_cookies, generate_device_id, decrypt
from XianyuAgent import XianyuReplyBot
from context_manager import ChatContextManager, normalize_manual_reply_media, normalize_media
from delivery_store import DeliveryStore


class ManualTakeoverError(ConnectionError):
    pass


class AutomationReplySuppressed(RuntimeError):
    def __init__(self, reason):
        self.reason = str(reason or "automation_suppressed")
        super().__init__(self.reason)


class ManualReplyLeaseLost(ConnectionError):
    pass


class PlatformMessageRejected(ConnectionError):
    pass


class AuthenticationUnavailableError(ConnectionError):
    ALLOWED_CODES = frozenset({"session_expired", "risk_control", "token_unavailable"})

    def __init__(self, code="token_unavailable"):
        if code not in self.ALLOWED_CODES:
            code = "token_unavailable"
        self.code = code
        super().__init__(code)


class OrderVerificationRejected(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("platform order is not eligible for automatic delivery")


class XianyuLive:
    MAX_CHAT_CONTENT_CHARS = 8192
    MAX_REPLY_CHARS = 4096
    MAX_ITEM_DESCRIPTION_CHARS = 16_000
    MAX_RICH_MEDIA_ITEMS = 8
    MAX_RICH_MEDIA_URL_CHARS = 2048
    COMPLETED_MANUAL_MEDIA_CLEANUP_INTERVAL = 60.0
    COMPLETED_MANUAL_MEDIA_CLEANUP_LIMIT = 16
    IMAGE_URL_RE = re.compile(
        r'https?://[^\s"\'<>\u4e00-\u9fa5]+\.(?:jpe?g|png|webp|gif|heic|bmp)(?:\?[^\s"\'<>]*)?',
        re.IGNORECASE,
    )

    def __init__(
        self,
        cookies_str,
        reply_bot=None,
        api_client=None,
        delivery_store=None,
        context_manager=None,
        data_dir=None,
        products_config_path=None,
        automation_mode=None,
    ):
        self.xianyu = api_client or XianyuApis()
        self.base_url = 'wss://wss-goofish.dingtalk.com/'
        self.state_dir = os.path.abspath(
            data_dir or os.getenv("XIAN_YU_DATA_DIR", os.path.join(BASE_DIR, "data"))
        )
        self.auth_status_file = os.path.join(self.state_dir, "auth_status.json")
        self.auth_state_store = AuthStateStore(self.auth_status_file)
        initial_auth_status = self.auth_state_store.read()
        if initial_auth_status["needs_human"]:
            raise AuthenticationUnavailableError(
                initial_auth_status["code"]
            )
        self.auth_storage = PrivateAuthStorage(self.state_dir)
        self.cookies_str = self.auth_storage.merged_cookie_header(cookies_str or "")
        self.cookies = trans_cookies(self.cookies_str)
        if hasattr(self.xianyu, "update_cookies"):
            self.xianyu.update_cookies(self.cookies)
        else:
            self.xianyu.session.cookies.update(self.cookies)
        self.myid = self.cookies.get('unb')
        if not self.myid:
            if not initial_auth_status["needs_human"]:
                self.auth_state_store.update(
                    phase="UNCONFIGURED",
                    session="MISSING",
                    mtop_token="ABSENT",
                    websocket="DISCONNECTED",
                    failure_code="cookie_invalid",
                    failure_class="CONFIGURATION",
                    failure_count=1,
                    next_retry_at=0,
                    needs_human=False,
                )
            raise RuntimeError("长期 Cookie 缺少必需的 unb 字段")
        self.device_id = generate_device_id(self.myid)
        self.account_key = os.getenv("XIAN_YU_ACCOUNT_KEY", "default").strip() or "default"
        if (
            len(self.account_key) > 80
            or not self.account_key.isascii()
            or any(not (character.isalnum() or character in "-_") for character in self.account_key)
        ):
            raise RuntimeError("XIAN_YU_ACCOUNT_KEY 无效")
        auth_status = self.auth_state_store.read()
        self.auth_snapshot = auth_status
        self.authentication_failure_code = (
            auth_status["code"] if auth_status["needs_human"] else None
        )
        if not auth_status["needs_human"]:
            self.auth_snapshot = self.auth_state_store.update(
                phase="SESSION_VALID",
                session=(
                    "VALID" if auth_status["session"]["state"] == "VALID" else "UNKNOWN"
                ),
                mtop_token="ABSENT",
                websocket="DISCONNECTED",
                failure_code="ok",
                failure_class="NONE",
                failure_count=0,
                next_retry_at=0,
                needs_human=False,
            )
        self.delivery_db_file = os.path.join(self.state_dir, "delivery_state.db")
        self.chat_db_file = os.path.join(self.state_dir, "chat_history.db")
        self.products_config_file = products_config_path or os.getenv(
            "PRODUCTS_CONFIG_FILE", PRODUCTS_CONFIG_FILE
        )
        configured_rules_file = os.getenv("REPLY_RULES_FILE", "").strip()
        self.reply_rules_file = os.path.abspath(
            configured_rules_file or os.path.join(self.state_dir, "reply_rules.json")
        )
        self.automation_mode = normalize_automation_mode(automation_mode)
        self.automation_strategy = normalize_automation_strategy()
        configured_settings_file = os.getenv("AUTOMATION_SETTINGS_FILE", "").strip()
        self.automation_settings_file = os.path.abspath(
            configured_settings_file or os.path.join(self.state_dir, "automation_settings.json")
        )
        self.automation_settings = load_automation_settings("")
        self.automation_settings_available = True
        self._automation_settings_signature = None
        self.reply_rules = ()
        self.reply_rules_available = True
        self._reply_rules_signature = None
        self.products = {}
        self.pan_resources = []
        self._products_signature = None
        self._pan_resources_signature = None
        self.context_manager = context_manager or ChatContextManager(db_path=self.chat_db_file)
        self._init_assistant_draft_provenance()
        if reply_bot is not None:
            self.bot = reply_bot
        elif self.automation_mode == "rules_ai":
            try:
                validate_ai_runtime_env()
                self.bot = XianyuReplyBot()
            except RuntimeError as exc:
                # AI configuration failures must not disable deterministic
                # rules. Unmatched questions will terminate as no_reply.
                self.bot = None
                logger.error("内部 AI 客户端不可用，固定规则仍可工作 error={}", type(exc).__name__)
        else:
            self.bot = None
        paid_automation = self.automation_mode == "rules_ai"
        self.delivery_store = delivery_store or DeliveryStore(
            self.delivery_db_file,
            redeem_pool_path=(
                self._state_input_path("redeem_codes.json") if paid_automation else None
            ),
        )
        quarantined = self.delivery_store.quarantine_automatic_orders(
            "platform_order_identity_unavailable"
        )
        if quarantined:
            logger.warning("已隔离旧自动发货记录 count={}", quarantined)
        open_reviews = self.delivery_store.manual_review_count()
        if open_reviews:
            logger.warning("存在待人工处理的付款审核 count={}", open_reviews)
        dead_letters = self.delivery_store.dead_letter_inbound_count()
        if dead_letters:
            logger.error("存在待人工重放或关闭的入站死信 count={}", dead_letters)
        self._refresh_runtime_config(force=True)
        self.payment_notice_retention = 30 * 86400
        self.max_message_tasks = read_number_env(
            "MAX_MESSAGE_TASKS", 100, 1, 10_000, integer=True
        )
        self.inbound_retry_interval = read_number_env(
            "INBOUND_RETRY_INTERVAL", 5, 1, 3600
        )
        self.send_ack_timeout = read_number_env(
            "SEND_ACK_TIMEOUT", 15, 1, 120
        )
        self.manual_reply_poll_interval = read_number_env(
            "MANUAL_REPLY_POLL_INTERVAL", 1, 0.2, 30
        )
        self.manual_reply_lease_seconds = read_number_env(
            "MANUAL_REPLY_LEASE_SECONDS", 300, 30, 3600
        )
        self.manual_reply_lease_heartbeat_interval = max(
            5.0, min(self.manual_reply_lease_seconds / 3.0, 30.0)
        )
        self.manual_reply_owner = f"{os.getpid()}:{uuid.uuid4().hex}"
        self._completed_manual_media_cleanup_next_at = 0.0
        self._completed_manual_media_cleanup_before_id = 0
        self.item_cache_ttl = read_number_env(
            "ITEM_CACHE_TTL", 300, 30, 3600
        )
        self.delivery_retry_interval = read_number_env(
            "DELIVERY_RETRY_INTERVAL", 600, 60, 86_400, integer=True
        )
        self.last_delivery_retry_at = 0.0
        self.manual_review_alert_interval = read_number_env(
            "MANUAL_REVIEW_ALERT_INTERVAL", 300, 60, 86_400
        )
        self.last_manual_review_alert = time.time() if open_reviews else 0

        # 心跳相关配置
        self.heartbeat_interval = read_number_env(
            "HEARTBEAT_INTERVAL", 15, 5, 300, integer=True
        )
        self.heartbeat_timeout = max(
            self.heartbeat_interval * 2,
            read_number_env(
                "HEARTBEAT_TIMEOUT", 90, 10, 900, integer=True
            ),
        )
        self.websocket_registration_wait_seconds = read_number_env(
            "WEBSOCKET_REGISTRATION_WAIT_SECONDS", 1, 0, 10
        )
        self.last_heartbeat_time = 0
        self.last_heartbeat_response = 0
        self.heartbeat_task = None
        self.ws = None
        self.connection_ready = asyncio.Event()
        self.pending_send_acks = {}
        self.heartbeat_mids = set()

        # Token刷新相关配置。托管环境注入的错峰值必须严格位于计划边界内。
        self.token_refresh_interval = read_number_env(
            "TOKEN_REFRESH_INTERVAL", 3600, 1800, 86_400, integer=True
        )
        self.token_retry_interval = read_number_env(
            "TOKEN_RETRY_INTERVAL", 300, 60, 3600, integer=True
        )
        self.token_startup_jitter_seconds = read_number_env(
            "TOKEN_STARTUP_JITTER_SECONDS", 0, 0, 30
        )
        self.token_refresh_jitter_seconds = read_number_env(
            "TOKEN_REFRESH_JITTER_SECONDS", 0, 0, 300
        )
        self.last_token_refresh_time = 0.0
        self.next_token_refresh_at = 0.0
        self.current_token = None
        self.token_consecutive_failures = 0
        self.token_circuit_open = self.authentication_failure_code is not None
        self.token_refresh_lock = asyncio.Lock()
        self._token_refresh_generation = 0
        self._reconnect_generation = 0
        self._startup_jitter_applied = False
        self.token_refresh_task = None
        self.inbound_recovery_task = None
        self.manual_outbox_task = None
        self.connection_restart_flag = False  # 连接重启标志

        # 人工接管相关配置（状态由 DeliveryStore 持久化）
        self.manual_mode_timeout = read_number_env(
            "MANUAL_MODE_TIMEOUT", 3600, 60, 604_800, integer=True
        )

        # 消息过期时间配置
        self.message_expire_time = read_number_env(
            "MESSAGE_EXPIRE_TIME", 300_000, 1000, 86_400_000, integer=True
        )

        # 人工接管关键词，从环境变量读取
        self.toggle_keywords = {
            keyword.strip()
            for keyword in os.getenv("TOGGLE_KEYWORDS", "。").split(",")
            if keyword.strip()
        }
        if (
            not self.toggle_keywords
            or len(self.toggle_keywords) > 10
            or any(len(keyword) > 32 for keyword in self.toggle_keywords)
        ):
            raise RuntimeError("TOGGLE_KEYWORDS 必须包含 1 到 10 个短关键词")

        # 模拟人工输入配置
        human_typing_value = os.getenv("SIMULATE_HUMAN_TYPING", "False").strip().lower()
        if human_typing_value not in {"true", "false"}:
            raise RuntimeError("SIMULATE_HUMAN_TYPING 必须是 True 或 False")
        self.simulate_human_typing = human_typing_value == "true"
        self.max_reply_delay = read_number_env("MAX_REPLY_DELAY", 25, 1, 60)
        self.llm_timeout = read_number_env("LLM_TIMEOUT", 50, 5, 180)
        self.ai_readiness_lock_timeout = 5.0
        self.ai_readiness_timeout = 8.0
        self.llm_lock = asyncio.Lock()
        self.llm_tasks = set()
        self.message_semaphore = asyncio.Semaphore(
            read_number_env("MESSAGE_WORKERS", 6, 1, 32, integer=True)
        )
        self.message_tasks = set()
        self.inbound_chat_tasks = {}
        self.delivery_tasks = set()
        self.chat_locks = {}
        self.item_locks = {}
        self.order_locks = {}
        self.chat_lock_users = {}
        self.item_lock_users = {}
        self.order_lock_users = {}

    @asynccontextmanager
    async def _keyed_lock(self, locks, users, key):
        key = str(key)
        lock = locks.setdefault(key, asyncio.Lock())
        users[key] = users.get(key, 0) + 1
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            remaining = users.get(key, 1) - 1
            if remaining <= 0:
                users.pop(key, None)
                if locks.get(key) is lock:
                    locks.pop(key, None)
            else:
                users[key] = remaining

    def _release_llm_slot(self, task):
        """Release the single LLM slot only after the synchronous call ends."""
        self.llm_tasks.discard(task)
        try:
            task.exception()
        except BaseException:
            pass
        if self.llm_lock.locked():
            self.llm_lock.release()

    async def _generate_llm_reply(
        self,
        content,
        item_id,
        item_context,
        context,
        recent_assistant_replies,
    ):
        """Run the synchronous internal reply client behind one bounded slot."""
        if self.bot is None or self.automation_mode != "rules_ai":
            raise RuntimeError("AI reply is unavailable in rules mode")
        await asyncio.wait_for(self.llm_lock.acquire(), timeout=self.llm_timeout)
        try:
            task = asyncio.create_task(
                asyncio.to_thread(
                    self.bot.generate_reply_result,
                    content,
                    "",
                    context,
                    "",
                    item_id=item_id,
                    item_context=item_context,
                    recent_assistant_replies=recent_assistant_replies,
                )
            )
        except BaseException:
            # Task creation can fail during loop shutdown; do not strand the
            # exclusive slot in that case.
            self.llm_lock.release()
            raise
        self.llm_tasks.add(task)
        try:
            reply = await asyncio.wait_for(
                asyncio.shield(task), timeout=self.llm_timeout
            )
        except asyncio.TimeoutError:
            # The SDK call is synchronous and cannot be force-cancelled. Keep
            # the slot held until its finite client timeout completes so a
            # timed-out request cannot overlap or race on bot.last_intent.
            task.add_done_callback(self._release_llm_slot)
            raise
        except asyncio.CancelledError:
            task.add_done_callback(self._release_llm_slot)
            raise
        except Exception:
            self._release_llm_slot(task)
            raise
        except BaseException:
            # A worker thread can surface SystemExit/KeyboardInterrupt-like
            # exceptions too; keep the lock owned until that thread is done.
            task.add_done_callback(self._release_llm_slot)
            raise
        self._release_llm_slot(task)
        if not isinstance(reply, dict):
            raise RuntimeError("AI reply result is invalid")
        return (
            reply.get("reply"),
            reply.get("decision"),
            reply.get("reason_code"),
            reply.get("config_revision"),
        )

    async def _ensure_ai_reply_ready(self, chat_id, expected_config_revision):
        """Revalidate account-scoped AI state immediately before a send attempt."""
        if self.bot is None or self.automation_mode != "rules_ai":
            raise AutomationReplySuppressed("ai_readiness_unavailable")
        acquired = False
        task = None
        try:
            await asyncio.wait_for(
                self.llm_lock.acquire(), timeout=self.ai_readiness_lock_timeout
            )
            acquired = True
            try:
                task = asyncio.create_task(
                    asyncio.to_thread(
                        self.bot.ensure_ready,
                        expected_config_revision,
                    )
                )
            except BaseException:
                self.llm_lock.release()
                acquired = False
                raise
            self.llm_tasks.add(task)
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=self.ai_readiness_timeout
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # The synchronous HTTP call cannot be force-cancelled. Keep the
                # shared slot until it really ends so another reply/readiness
                # request cannot overlap the same session after a timeout.
                task.add_done_callback(self._release_llm_slot)
                acquired = False
                raise
            except Exception:
                self._release_llm_slot(task)
                acquired = False
                raise
            except BaseException:
                task.add_done_callback(self._release_llm_slot)
                acquired = False
                raise
            self._release_llm_slot(task)
            acquired = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "发送前 AI 状态复核失败 chat={} error={}",
                stable_ref(chat_id),
                type(exc).__name__,
            )
            raise AutomationReplySuppressed("ai_readiness_failed") from exc
        finally:
            if acquired:
                self.llm_lock.release()

    def _init_assistant_draft_provenance(self):
        with sqlite3.connect(self.chat_db_file, timeout=10) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_draft_provenance (
                    source_id TEXT PRIMARY KEY,
                    reply_origin TEXT NOT NULL CHECK(reply_origin IN ('rule', 'ai')),
                    config_revision INTEGER,
                    automation_revision TEXT,
                    updated_at REAL NOT NULL,
                    CHECK(
                        (reply_origin = 'rule' AND config_revision IS NULL)
                        OR
                        (reply_origin = 'ai' AND config_revision >= 0)
                    )
                )
                """
            )
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(assistant_draft_provenance)"
                ).fetchall()
            }
            if "automation_revision" not in columns:
                conn.execute(
                    "ALTER TABLE assistant_draft_provenance "
                    "ADD COLUMN automation_revision TEXT"
                )
            conn.execute(
                """
                DELETE FROM assistant_draft_provenance
                WHERE source_id NOT IN (
                    SELECT source_id FROM assistant_outcomes
                    WHERE role = 'assistant_pending'
                )
                """
            )

    def _store_assistant_draft_provenance(
        self,
        source_id,
        origin,
        config_revision=None,
        automation_revision=None,
    ):
        if origin not in {"rule", "ai"}:
            raise ValueError("assistant draft origin is invalid")
        if origin == "ai" and (
            isinstance(config_revision, bool)
            or not isinstance(config_revision, int)
            or config_revision < 0
        ):
            raise ValueError("assistant draft config revision is invalid")
        if not isinstance(automation_revision, str) or not re.fullmatch(
            r"[0-9a-f]{64}", automation_revision
        ):
            raise ValueError("assistant draft automation revision is invalid")
        revision = config_revision if origin == "ai" else None
        with sqlite3.connect(self.chat_db_file, timeout=10) as conn:
            conn.execute(
                """
                INSERT INTO assistant_draft_provenance(
                    source_id, reply_origin, config_revision,
                    automation_revision, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    reply_origin = excluded.reply_origin,
                    config_revision = excluded.config_revision,
                    automation_revision = excluded.automation_revision,
                    updated_at = excluded.updated_at
                """,
                (
                    str(source_id),
                    origin,
                    revision,
                    automation_revision,
                    time.time(),
                ),
            )

    def _assistant_draft_provenance(self, source_id):
        with sqlite3.connect(self.chat_db_file, timeout=10) as conn:
            row = conn.execute(
                """
                SELECT reply_origin, config_revision, automation_revision
                FROM assistant_draft_provenance
                WHERE source_id = ?
                """,
                (str(source_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "origin": row[0],
            "config_revision": row[1],
            "automation_revision": row[2],
        }

    def _delete_assistant_draft_provenance(self, source_id):
        with sqlite3.connect(self.chat_db_file, timeout=10) as conn:
            conn.execute(
                "DELETE FROM assistant_draft_provenance WHERE source_id = ?",
                (str(source_id),),
            )

    def _state_input_path(self, filename):
        state_path = os.path.join(self.state_dir, filename)
        legacy_path = os.path.join(BASE_DIR, filename)
        return state_path if os.path.exists(state_path) else legacy_path

    def _read_auth_status(self):
        state = self.auth_state_store.read()
        self.auth_snapshot = state
        return state

    def _write_auth_status(self, code, reauthorization_required):
        """兼容旧调用点，同时始终写入 v2 脱敏状态。"""
        required = bool(reauthorization_required) and code in {
            "session_expired",
            "risk_control",
        }
        if required:
            session_state = "SECURITY_CHECK" if code == "risk_control" else "EXPIRED"
            state = self.auth_state_store.update(
                phase="NEEDS_HUMAN",
                session=session_state,
                mtop_token="DEGRADED",
                websocket="DEGRADED" if self.ws is not None else "DISCONNECTED",
                failure_code=code,
                failure_class="NEEDS_HUMAN",
                failure_count=max(1, self.token_consecutive_failures),
                next_retry_at=0,
                needs_human=True,
            )
        else:
            state = self.auth_state_store.update(
                phase="DEGRADED",
                mtop_token="DEGRADED",
                websocket=("REGISTERED" if self.connection_ready.is_set() else "DISCONNECTED"),
                failure_code=code,
                failure_class="TRANSIENT",
                failure_count=max(1, self.token_consecutive_failures),
                next_retry_at=self.next_token_refresh_at,
                needs_human=False,
            )
        self.auth_snapshot = state
        return state

    def _set_auth_state(self, **changes):
        self.auth_snapshot = self.auth_state_store.update(**changes)
        return self.auth_snapshot

    def _persist_mtop_cookies(self):
        session = getattr(self.xianyu, "session", None)
        cookies = getattr(session, "cookies", {})
        self.auth_storage.persist_short_cookies(cookies)
        if hasattr(self.xianyu, "cookie_header_snapshot"):
            snapshot = self.xianyu.cookie_header_snapshot()
            if snapshot:
                self.cookies_str = snapshot

    def _has_live_websocket(self):
        return self.ws is not None and self.connection_ready.is_set()

    def _refresh_automation_settings(self, force=False):
        signature = _reply_rules_file_signature(self.automation_settings_file)
        if not force and signature == self._automation_settings_signature:
            return
        if signature is None:
            settings = load_automation_settings("")
            settings["enabled"] = False
            self.automation_settings_available = False
            logger.error("自动化设置文件缺失，已暂停自动回复")
        else:
            try:
                settings = load_automation_settings(self.automation_settings_file)
                self.automation_settings_available = True
            except RuntimeError as exc:
                settings = load_automation_settings("")
                settings["enabled"] = False
                self.automation_settings_available = False
                logger.error("自动化设置无效，已暂停自动回复 error={}", type(exc).__name__)
        self.automation_settings = settings
        self.automation_strategy = settings.get("strategy", "standard")
        self._automation_settings_signature = signature

    def _refresh_reply_rules(self, force=False):
        signature = _reply_rules_file_signature(self.reply_rules_file)
        if not force and signature == self._reply_rules_signature:
            return
        if signature is None:
            self.reply_rules = ()
            self.reply_rules_available = False
            self._reply_rules_signature = None
            logger.error("回复规则文件缺失，已暂停全部自动回复")
            return
        try:
            rules = load_reply_rules(self.reply_rules_file)
        except RuntimeError as exc:
            # A malformed live update revokes the whole automatic reply chain.
            # The recorded signature prevents a hot-loop; a repaired file gets
            # a new signature and restores both deterministic and AI replies.
            self.reply_rules = ()
            self.reply_rules_available = False
            self._reply_rules_signature = signature
            logger.error("回复规则配置无效，已暂停全部自动回复 error={}", type(exc).__name__)
            return
        self.reply_rules = rules
        self.reply_rules_available = True
        self._reply_rules_signature = signature

    def _refresh_products(self, force=False):
        config_signature = _reply_rules_file_signature(self.products_config_file)
        pan_path = self._state_input_path("pan_links.json")
        pan_signature = _reply_rules_file_signature(pan_path)
        signature = (config_signature, pan_signature)
        previous_signature = (
            getattr(self, "_products_signature", None),
            getattr(self, "_pan_resources_signature", None),
        )
        if not force and signature == previous_signature:
            return
        try:
            products = self._load_products()
            pan_resources = self._load_pan_resources(products)
        except RuntimeError as exc:
            if force:
                raise
            # Invalid or removed mappings revoke automatic delivery until a
            # valid file is written; never continue with stale authorization.
            self.products = {}
            self.pan_resources = []
            self._products_signature = config_signature
            self._pan_resources_signature = pan_signature
            logger.error("商品履约配置无效，已暂停自动发货 error={}", type(exc).__name__)
            return
        self.products = products
        self.pan_resources = pan_resources
        self._products_signature = config_signature
        self._pan_resources_signature = pan_signature

    def _refresh_runtime_config(self, force=False):
        self._refresh_automation_settings(force=force)
        self._refresh_reply_rules(force=force)
        self._refresh_products(force=force)

    def _loaded_automation_revision(self):
        """Hash the exact validated rules/settings snapshot used for one reply."""
        if not getattr(self, "automation_settings_available", False):
            return None
        if not getattr(self, "reply_rules_available", False):
            return None
        payload = {
            "mode": self.automation_mode,
            "settings": self.automation_settings,
            "rules": list(self.reply_rules),
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _current_automation_revision(self):
        self._refresh_automation_settings()
        self._refresh_reply_rules()
        return self._loaded_automation_revision()

    def _match_reply_rule(self, content, item_id=None, *, refresh=True):
        """Return the first enabled contains-rule reply for this product scope."""
        if refresh:
            self._refresh_automation_settings()
            self._refresh_reply_rules()
        if not isinstance(content, str):
            return None
        if self.automation_strategy == 'conservative' and len(content.strip()) > 240:
            return None
        normalized_content = content.casefold()
        current_item_id = str(item_id or "")
        scoped_matches = []
        generic_matches = []
        for rule in self.reply_rules:
            if not rule.get("enabled", True):
                continue
            scoped_item_id = str(rule.get("item_id") or "")
            if scoped_item_id and scoped_item_id != current_item_id:
                continue
            if not any(keyword in normalized_content for keyword in rule["keywords"]):
                continue
            (scoped_matches if scoped_item_id else generic_matches).append(rule)
        matches = scoped_matches or generic_matches
        if not matches:
            return None
        if self.automation_strategy == 'aggressive':
            # Prefer the most specific keyword only within the winning scope;
            # a generic rule must never shadow a matching product-specific one.
            return max(matches, key=lambda item: max(map(len, item["keywords"])))['reply']
        return matches[0]['reply']

    def _load_products(self):
        payload, config_mode = load_products_config_file(self.products_config_file)
        configs = payload.get("types")
        if not isinstance(configs, list):
            raise RuntimeError("products_config.json 缺少 types 列表")
        by_item = {}
        allowed_deliveries = (
            {"material"}
            if self.automation_mode == "rules"
            else {"redeem", "pan", "material"}
        )
        for config in configs:
            if not isinstance(config, dict) or config.get("delivery") not in {
                "redeem",
                "pan",
                "material",
            }:
                raise RuntimeError("products_config.json 包含无效商品配置")
            enabled = config.get("enabled", True)
            if not isinstance(enabled, bool):
                raise RuntimeError("products_config.json 商品 enabled 无效")
            if not enabled:
                # Disabled entries remain in the SaaS document for editing,
                # but must not authorize either replies or delivery.
                continue
            # The deterministic/free worker may share a config file with the
            # paid worker.  Never let a downgrade inherit redeem or pan
            # inventory mappings; those entries are intentionally invisible to
            # this process, while material delivery remains available.
            if config["delivery"] not in allowed_deliveries:
                continue
            config = dict(config)
            if config["delivery"] == "material":
                material_payload = config.get("payload")
                if material_payload is None:
                    # The versioned application map may describe a material
                    # product, but only a tenant-owned private copy can
                    # authorize delivery by attaching its payload.
                    continue
                if config_mode != 0o600:
                    raise RuntimeError("包含资料正文的商品配置权限必须是 0600")
                if (
                    not isinstance(material_payload, str)
                    or not material_payload.strip()
                    or len(material_payload.strip()) > MAX_MATERIAL_PAYLOAD_CHARS
                    or "\x00" in material_payload
                ):
                    raise RuntimeError("资料商品必须配置有效 payload")
                material_id = config.get("id")
                if (
                    not isinstance(material_id, (str, int))
                    or isinstance(material_id, bool)
                    or not str(material_id).strip()
                    or len(str(material_id).strip()) > 128
                ):
                    raise RuntimeError("资料商品必须配置有效 id")
                config["id"] = str(material_id).strip()
                config["payload"] = material_payload.strip()
            item_ids = config.get("item_ids")
            if not isinstance(item_ids, list) or not item_ids:
                raise RuntimeError("自动发货商品必须配置明确 item_ids")
            for item_id in item_ids:
                item_key = str(item_id).strip()
                if not item_key or not item_key.isdigit():
                    raise RuntimeError("products_config.json 包含无效 item_id")
                if item_key in by_item:
                    raise RuntimeError("products_config.json 存在重复 item_id")
                by_item[item_key] = dict(config)
            if config["delivery"] == "pan":
                tags = config.get("resource_match")
                if (
                    not isinstance(tags, list)
                    or not tags
                    or not all(isinstance(tag, str) and tag.strip() for tag in tags)
                ):
                    raise RuntimeError("网盘商品必须配置有效 resource_match")
        return by_item

    def _load_pan_resources(self, products=None):
        configured_products = self.products if products is None else products
        if not any(config.get("delivery") == "pan" for config in configured_products.values()):
            return []
        payload = load_json_file(self._state_input_path("pan_links.json"), dict)
        links = payload.get("links")
        if not isinstance(links, list):
            raise RuntimeError("pan_links.json 缺少 links 列表")
        resources = []
        for entry in links:
            if not isinstance(entry, dict) or entry.get("used") is True:
                continue
            url = entry.get("url")
            code = entry.get("code")
            remark = entry.get("remark")
            tags = entry.get("match")
            try:
                parsed = urlparse(url)
            except (TypeError, ValueError):
                parsed = None
            if (
                not isinstance(url, str)
                or len(url) > 2048
                or parsed is None
                or parsed.scheme != "https"
                or not parsed.hostname
                or not isinstance(code, str)
                or not code.strip()
                or len(code) > 64
                or not isinstance(remark, str)
                or not remark.strip()
                or len(remark) > 512
                or not isinstance(tags, list)
                or not tags
                or not all(isinstance(tag, str) and tag.strip() for tag in tags)
            ):
                raise RuntimeError("pan_links.json 包含无效资源")
            resources.append(
                {
                    "url": url.strip(),
                    "code": code.strip(),
                    "remark": remark.strip(),
                    "match": frozenset(tag.strip() for tag in tags),
                    "host": parsed.hostname.lower(),
                }
            )
        if not resources:
            raise RuntimeError("pan_links.json 没有可用资源")
        return resources

    def _pan_payload_for(self, item_config):
        required = frozenset(
            tag.strip() for tag in item_config.get("resource_match", [])
        )
        matches = [
            resource
            for resource in self.pan_resources
            # Resource tags are an explicit group key.  Subset matching would
            # let a broad product accidentally receive a more specific group.
            if required and required == resource["match"]
        ]
        if not matches:
            raise RuntimeError("configured pan resource is unavailable")
        blocks = []
        for resource in matches:
            if resource["host"] == "pan.baidu.com":
                provider = "百度网盘"
            elif resource["host"] == "pan.quark.cn":
                provider = "夸克网盘"
            else:
                provider = "网盘"
            blocks.append(
                f"{resource['remark']}\n{provider}链接：{resource['url']}\n"
                f"提取码：{resource['code']}"
            )
        return "\n\n".join(blocks)

    def classify_item(self, item_id):
        """Only explicit platform item IDs may enable automatic delivery."""
        return self.products.get(str(item_id))

    @staticmethod
    def _json_object(value):
        for _ in range(2):
            if isinstance(value, dict):
                return value
            if not isinstance(value, str) or len(value) > 2 * 1024 * 1024:
                return None
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError, ValueError):
                return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _numeric_identifier(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            value = str(value)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 64
            or not value.isascii()
            or not value.isdigit()
        ):
            return None
        return value

    @staticmethod
    def _platform_boolean(value):
        if value is True or value == 1:
            return True
        if value is False or value == 0:
            return False
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
        return None

    @classmethod
    def _parse_message_head_info(cls, response):
        response = cls._json_object(response)
        data = cls._json_object(response.get("data")) if response else None
        common = cls._json_object(data.get("commonData")) if data else None
        if not common:
            raise RuntimeError("headinfo_invalid")
        order_id = cls._numeric_identifier(
            common.get("orderId") or common.get("bizOrderId")
        )
        item_id = cls._numeric_identifier(common.get("itemId"))
        seller = cls._platform_boolean(common.get("seller"))
        if order_id is None or item_id is None or seller is None:
            raise RuntimeError("headinfo_invalid")
        return {
            "order_id": order_id,
            "item_id": item_id,
            "seller": seller,
        }

    @classmethod
    def _parse_order_detail(cls, response):
        response = cls._json_object(response)
        data = cls._json_object(response.get("data")) if response else None
        if not data:
            raise RuntimeError("order_detail_invalid")
        ut_args = cls._json_object(data.get("utArgs"))
        if not ut_args:
            raise RuntimeError("order_detail_invalid")

        order_info = None
        components = data.get("components")
        if isinstance(components, list):
            for component in components:
                if isinstance(component, dict) and component.get("render") == "orderInfoVO":
                    order_info = cls._json_object(component.get("data"))
                    break
        if order_info is None:
            order_info = cls._json_object(data.get("orderInfoVO"))
            if order_info and isinstance(order_info.get("data"), dict):
                order_info = order_info["data"]
        item_info = cls._json_object(order_info.get("itemInfo")) if order_info else None
        price_info = cls._json_object(order_info.get("priceInfo")) if order_info else None
        amount = cls._json_object(price_info.get("amount")) if price_info else None
        amount_value = amount.get("value") if amount else None

        order_id = cls._numeric_identifier(
            data.get("orderId")
            or data.get("bizOrderId")
            or ut_args.get("orderId")
            or ut_args.get("bizOrderId")
        )
        item_id = cls._numeric_identifier(data.get("itemId") or ut_args.get("itemId"))
        buyer_id = cls._numeric_identifier(
            data.get("peerUserId") or ut_args.get("peerUserId")
        )
        seller = cls._platform_boolean(data.get("seller"))
        raw_status = data.get("status")
        if isinstance(raw_status, bool):
            status = None
        elif isinstance(raw_status, int):
            status = raw_status
        elif isinstance(raw_status, str) and raw_status.isascii() and raw_status.isdigit():
            status = int(raw_status)
        else:
            status = None
        raw_quantity = item_info.get("buyAmount") if item_info else None
        if isinstance(raw_quantity, bool):
            quantity = None
        elif isinstance(raw_quantity, int):
            quantity = raw_quantity
        elif isinstance(raw_quantity, str) and raw_quantity.isascii() and raw_quantity.isdigit():
            quantity = int(raw_quantity)
        else:
            quantity = None
        try:
            paid_amount = Decimal(str(amount_value))
        except (InvalidOperation, TypeError, ValueError):
            paid_amount = None
        ut_status = ut_args.get("orderStatus")
        if (
            order_id is None
            or item_id is None
            or buyer_id is None
            or seller is None
            or status is None
            or quantity is None
            or paid_amount is None
            or not paid_amount.is_finite()
            or paid_amount < 0
            or not isinstance(ut_status, (str, int))
            or isinstance(ut_status, bool)
            or not str(ut_status).strip()
        ):
            raise RuntimeError("order_detail_invalid")
        normalized_amount = format(paid_amount.normalize(), "f")
        return {
            "order_id": order_id,
            "item_id": item_id,
            "buyer_id": buyer_id,
            "seller": seller,
            "status": status,
            "quantity": quantity,
            "paid_amount": normalized_amount,
            "ut_status": str(ut_status).strip()[:64],
        }

    @staticmethod
    def _canonical_order_key(order_id):
        return "goofish:" + hashlib.sha256(str(order_id).encode("utf-8")).hexdigest()

    @staticmethod
    def parse_paid_order_event(message):
        """Parse a platform reminder; authorization still requires both order APIs."""
        if not isinstance(message, dict):
            return None
        reminder = message.get("3")
        raw_account = message.get("1")
        event_timestamp = message.get("4")
        if not isinstance(reminder, dict) or reminder.get("redReminder") != "等待卖家发货":
            return None
        if str(reminder.get("redReminderStyle")) != "1":
            return None
        if type(message.get("2")) is not int or message.get("2") != 1:
            return None
        if not isinstance(raw_account, str) or not raw_account.endswith("@goofish"):
            return None
        if (
            isinstance(event_timestamp, bool)
            or not isinstance(event_timestamp, (int, float))
            or not math.isfinite(float(event_timestamp))
        ):
            return None
        session_id = raw_account[:-8]
        if not session_id or len(session_id) > 128:
            return None
        canonical = json.dumps(
            {
                "session_id": session_id,
                "event_type": message.get("2"),
                "reminder": reminder.get("redReminder"),
                "style": str(reminder.get("redReminderStyle")),
                "timestamp": event_timestamp,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        event_key = "goofish:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        timestamp = float(event_timestamp)
        if timestamp > 100_000_000_000:
            timestamp /= 1000
        now = time.time()
        if timestamp < 0 or timestamp > now + 86_400 or now - timestamp > 10 * 365 * 86_400:
            return None
        # Keep the legacy field names for the SQLite audit schema, but make
        # the account-vs-chat distinction explicit to callers.
        return {
            "order_key": event_key,
            "chat_id": session_id,
            "session_id": session_id,
            "event_at": timestamp,
        }

    def _record_unverified_manual(self, event, reason, order_id=None):
        order_key = (
            self._canonical_order_key(order_id) if order_id else event["order_key"]
        )
        payment = self.delivery_store.record_payment_event(
            order_key,
            event["session_id"],
            event["event_at"],
            self.payment_notice_retention,
        )
        if payment.status != "delivered":
            self.delivery_store.mark_order_manual_review(payment.key, reason)
        logger.warning(
            "付款订单转人工 event={} chat={} reason={}",
            stable_ref(payment.key),
            stable_ref(payment.chat_id),
            reason,
        )

    @staticmethod
    def _verification_rejection(head, detail, binding, item_config=None):
        if head["item_id"] != str(binding["item_id"]):
            return "headinfo_item_mismatch"
        if head["seller"] is not True:
            return "seller_identity_mismatch"
        if detail["order_id"] != head["order_id"]:
            return "order_identity_mismatch"
        if detail["item_id"] != str(binding["item_id"]):
            return "order_item_mismatch"
        if detail["buyer_id"] != str(binding["buyer_id"]):
            return "order_buyer_mismatch"
        if detail["seller"] is not True:
            return "seller_identity_mismatch"
        if detail["status"] != 2:
            return "order_not_awaiting_shipment"
        if detail["quantity"] < 1 or detail["quantity"] > 50:
            return "unsupported_quantity"
        # A material payload is a single fixed document/code. Reusing it for
        # a multi-unit order could silently under-deliver or duplicate a
        # one-time secret, so route those orders to manual review.
        if (
            item_config
            and item_config.get("delivery") == "material"
            and detail["quantity"] != 1
        ):
            return "unsupported_quantity"
        return None

    async def handle_paid_order(self, message):
        # Product mappings and material payloads may be replaced atomically by
        # the SaaS control plane while this worker stays connected.
        self._refresh_runtime_config()
        event = self.parse_paid_order_event(message)
        if event is None:
            return False
        binding = self.delivery_store.get_chat_binding(event["session_id"])
        if binding is None:
            self._record_unverified_manual(event, "chat_binding_unavailable")
            return True
        item_config = self.classify_item(binding["item_id"])
        if item_config is None:
            self._record_unverified_manual(event, "unsupported_item")
            return True

        head_response = await self._call_xianyu_api(
            self.xianyu.get_message_head_info,
            event["session_id"],
            str(binding["item_id"]),
        )
        head = self._parse_message_head_info(head_response)
        order_key = self._canonical_order_key(head["order_id"])
        existing = self.delivery_store.get_order(order_key)
        if existing is not None and existing.status in {
            "delivered",
            "manual_review",
            "cancelled",
            "expired",
        }:
            return True
        if head["item_id"] != str(binding["item_id"]) or head["seller"] is not True:
            reason = (
                "headinfo_item_mismatch"
                if head["item_id"] != str(binding["item_id"])
                else "seller_identity_mismatch"
            )
            self._record_unverified_manual(event, reason, head["order_id"])
            return True

        detail_response = await self._call_xianyu_api(
            self.xianyu.get_order_detail, head["order_id"]
        )
        detail = self._parse_order_detail(detail_response)
        rejection = self._verification_rejection(
            head, detail, binding, item_config
        )
        if existing is None or existing.platform_order_id is None:
            payment = self.delivery_store.record_verified_payment_event(
                order_key,
                event["session_id"],
                event["event_at"],
                self.payment_notice_retention,
                platform_order_id=head["order_id"],
                platform_status=str(detail["status"]),
                paid_amount=detail["paid_amount"],
                quantity=detail["quantity"],
            )
        else:
            if (
                existing.platform_order_id != head["order_id"]
                or existing.platform_status != str(detail["status"])
                or existing.paid_amount != detail["paid_amount"]
                or existing.quantity != detail["quantity"]
            ):
                rejection = rejection or "order_reverification_failed"
            payment = existing
        if rejection:
            self.delivery_store.mark_order_manual_review(
                order_key, rejection
            )
            logger.warning(
                "付款订单核验不通过 event={} reason={}",
                stable_ref(order_key),
                rejection,
            )
            return True
        # 已核验订单照常自动发货:人工接管只影响闲聊回复,不阻断发货。
        await self._fulfill_order(order_key, item_config)
        return True

    async def retry_pending_deliveries(self):
        self._refresh_runtime_config()
        quarantined = self.delivery_store.quarantine_automatic_orders(
            "platform_order_identity_unavailable"
        )
        if quarantined:
            logger.warning("恢复时隔离自动发货记录 count={}", quarantined)
        revived_orders = self.delivery_store.revive_takeover_blocked_orders()
        if revived_orders:
            logger.info(
                "已恢复人工接管期间被搁置的已核验订单 count={}",
                len(revived_orders),
            )
        for order in revived_orders:
            binding = self.delivery_store.get_chat_binding(order.chat_id)
            item_config = (
                self.classify_item(binding["item_id"]) if binding else None
            )
            if item_config is None:
                self.delivery_store.mark_order_manual_review(
                    order.key, "chat_binding_unavailable"
                )
                continue
            try:
                await self._fulfill_order(order.key, item_config)
            except Exception as exc:
                logger.error(
                    "恢复接管搁置订单失败 event={} error={}",
                    stable_ref(order.key),
                    type(exc).__name__,
                )
        for order in self.delivery_store.retryable_orders():
            item_config = self.classify_item(order.item_id)
            if (
                item_config is None
                or item_config.get("delivery") != order.delivery_type
            ):
                self.delivery_store.mark_order_manual_review(
                    order.key, "unsupported_item"
                )
                continue
            try:
                await self._fulfill_order(order.key, item_config)
            except Exception as exc:
                logger.error(
                    "自动发货恢复失败 event={} error={}",
                    stable_ref(order.key),
                    type(exc).__name__,
                )
        for order in self.delivery_store.pending_platform_shipments():
            try:
                await self._call_xianyu_api(
                    self.xianyu.consign_dummy, order.platform_order_id
                )
                self.delivery_store.mark_platform_shipped(order.key)
                logger.info("平台发货补发成功 event={}", stable_ref(order.key))
            except Exception as exc:
                self.delivery_store.record_platform_ship_attempt(
                    order.key, type(exc).__name__
                )
                logger.warning(
                    "平台发货补发失败 event={} error={}",
                    stable_ref(order.key),
                    type(exc).__name__,
                )

    async def _reverify_order(self, reservation):
        response = await self._call_xianyu_api(
            self.xianyu.get_order_detail, reservation.platform_order_id
        )
        detail = self._parse_order_detail(response)
        if detail["order_id"] != reservation.platform_order_id:
            raise OrderVerificationRejected("order_identity_mismatch")
        if detail["item_id"] != reservation.item_id:
            raise OrderVerificationRejected("order_item_mismatch")
        if detail["buyer_id"] != reservation.buyer_id:
            raise OrderVerificationRejected("order_buyer_mismatch")
        if detail["seller"] is not True:
            raise OrderVerificationRejected("seller_identity_mismatch")
        if detail["status"] != 2:
            raise OrderVerificationRejected("order_not_awaiting_shipment")
        if detail["quantity"] != reservation.quantity:
            raise OrderVerificationRejected("unsupported_quantity")
        if reservation.delivery_type == "material" and detail["quantity"] != 1:
            raise OrderVerificationRejected("unsupported_quantity")
        if detail["paid_amount"] != reservation.paid_amount:
            raise OrderVerificationRejected("order_reverification_failed")

    @staticmethod
    def _delivery_text(reservation):
        if reservation.delivery_type == "redeem":
            if len(reservation.resources) != reservation.quantity:
                raise RuntimeError("reserved redeem inventory is unavailable")
            # The configured template message wins; the fallback stays generic so a
            # self-hosted shop never ships another operator's product wording.
            intro = (reservation.payload or "").strip() or "付款已经核验，这是你的兑换码："
            if reservation.quantity == 1:
                return f"{intro}\n{reservation.resources[0]}"
            codes = "\n".join(
                f"{index}. {code}"
                for index, code in enumerate(reservation.resources, start=1)
            )
            return f"{intro}\n{codes}"
        if (
            reservation.delivery_type == "material"
            and reservation.quantity == 1
            and reservation.payload
        ):
            return reservation.payload
        if reservation.delivery_type == "pan" and reservation.payload:
            return f"付款已经核验，这是你购买的资源：\n{reservation.payload}"
        raise RuntimeError("delivery payload is unavailable")

    async def _fulfill_order(self, order_key, item_config):
        async with self._keyed_lock(
            self.order_locks, self.order_lock_users, order_key
        ):
            current = self.delivery_store.get_order(order_key)
            if current is None:
                raise RuntimeError("verified order disappeared")
            if current.status in {"delivered", "manual_review", "cancelled", "expired"}:
                return current.status
            delivery_type = item_config.get("delivery")
            if delivery_type == "material" and current.quantity not in (None, 1):
                self.delivery_store.mark_order_manual_review(
                    order_key, "unsupported_quantity"
                )
                return "manual_review"
            delivery_payload = None
            if delivery_type == "pan":
                try:
                    delivery_payload = current.payload or self._pan_payload_for(item_config)
                except RuntimeError:
                    self.delivery_store.mark_order_manual_review(
                        order_key, "pan_resource_unavailable"
                    )
                    return "manual_review"
            elif delivery_type == "material":
                delivery_payload = current.payload or item_config.get("payload")
                if (
                    not isinstance(delivery_payload, str)
                    or not delivery_payload.strip()
                    or len(delivery_payload.strip()) > MAX_MATERIAL_PAYLOAD_CHARS
                    or "\x00" in delivery_payload
                ):
                    self.delivery_store.mark_order_manual_review(
                        order_key, "material_payload_unavailable"
                    )
                    return "manual_review"
                delivery_payload = delivery_payload.strip()
            binding = None
            if not current.buyer_id or not current.item_id:
                binding = self.delivery_store.get_chat_binding(current.chat_id)
                if binding is None:
                    self.delivery_store.mark_order_manual_review(
                        order_key, "chat_binding_unavailable"
                    )
                    return "manual_review"
            reservation = self.delivery_store.prepare_order(
                order_key,
                current.chat_id,
                current.buyer_id or binding["buyer_id"],
                current.item_id or binding["item_id"],
                delivery_type,
                quantity=current.quantity or 1,
                delivery_payload=delivery_payload,
            )
            if reservation.status in {"delivered", "manual_review", "cancelled", "expired"}:
                return reservation.status
            if not reservation.resources and delivery_type == "redeem":
                return reservation.status
            reservation = self.delivery_store.claim_order_for_send(order_key)
            if reservation is None:
                return "busy"
            text = self._delivery_text(reservation)

            async def verify_before_attempt():
                if (
                    reservation.delivery_type == "redeem"
                    and not self.delivery_store.order_inventory_is_sendable(order_key)
                ):
                    latest = self.delivery_store.get_order(order_key)
                    reason = (
                        latest.reason
                        if latest is not None
                        and latest.reason in {
                            "inventory_marked_used",
                            "inventory_removed_from_manifest",
                        }
                        else "inventory_state_changed"
                    )
                    raise OrderVerificationRejected(reason)
                await self._reverify_order(reservation)

            try:
                await self.send_text_reliably(
                    reservation.chat_id,
                    reservation.buyer_id,
                    text,
                    message_key=f"order:{order_key}",
                    before_attempt=verify_before_attempt,
                    allow_manual=True,
                )
                self.delivery_store.mark_order_delivered(order_key)
            except OrderVerificationRejected as exc:
                self.delivery_store.mark_order_manual_review(order_key, exc.reason)
                logger.warning(
                    "发送前订单状态不再满足自动发货 event={} reason={}",
                    stable_ref(order_key),
                    exc.reason,
                )
                return "manual_review"
            except ManualTakeoverError:
                self.delivery_store.mark_order_manual_review(
                    order_key, "manual_takeover_before_send"
                )
                return "manual_review"
            except Exception as exc:
                self.delivery_store.mark_order_retry(order_key, type(exc).__name__)
                raise
            logger.info("自动发货完成 event={}", stable_ref(order_key))
            await self._ship_platform_order(order_key, reservation)
            return "delivered"

    async def _ship_platform_order(self, order_key, reservation):
        """发码成功后把订单在平台上做无需邮寄发货;失败进入恢复重试,不影响发码状态。"""
        try:
            await self._call_xianyu_api(
                self.xianyu.consign_dummy, reservation.platform_order_id
            )
        except Exception as exc:
            self.delivery_store.record_platform_ship_attempt(
                order_key, type(exc).__name__
            )
            logger.warning(
                "平台发货失败，稍后重试 event={} error={}",
                stable_ref(order_key),
                type(exc).__name__,
            )
            return "retry"
        self.delivery_store.mark_platform_shipped(order_key)
        logger.info("平台发货完成 event={}", stable_ref(order_key))
        return "shipped"


    async def human_reply_delay(self, user_msg, bot_reply, chat_id):
        """Return the account-configured base delay plus bounded random jitter."""
        settings = getattr(self, "automation_settings", {})
        base_delay = int(settings.get("delay_min_seconds") or 0)
        random_delay = int(settings.get("delay_max_seconds") or 0)
        if self._automation_settings_signature is not None:
            return min(float(base_delay) + random.uniform(0, float(random_delay)), 120.0)
        if not self.simulate_human_typing:
            return 0.0
        maximum = self.max_reply_delay
        hour = time.localtime().tm_hour
        night = hour >= 23 or hour < 8
        if night:
            return min(random.uniform(10, 25), maximum)
        think = random.uniform(2, 5) + min(len(user_msg) / 20, 4)
        typing = min(len(bot_reply) * random.uniform(0.04, 0.1), 8)
        return min(max(think + typing, 2.0), maximum)

    async def _call_xianyu_api(self, operation, *args):
        try:
            return await asyncio.to_thread(operation, *args)
        except XianyuAuthenticationError as exc:
            if exc.code in {"session_expired", "risk_control"}:
                await self._stop_for_auth_failure(exc.code)
                raise AuthenticationUnavailableError(exc.code) from None
            raise

    def _token_failure_delay(self):
        exponent = max(0, self.token_consecutive_failures - 1)
        base_delay = min(
            float(self.token_retry_interval) * (2 ** min(exponent, 8)),
            3600.0,
        )
        jitter_bound = min(float(self.token_refresh_jitter_seconds), 300.0)
        return min(base_delay + random.uniform(0.0, jitter_bound), 3600.0)

    def _next_regular_refresh_at(self, refreshed_at):
        jitter_bound = min(
            float(self.token_refresh_jitter_seconds),
            max(0.0, float(self.token_refresh_interval) / 4.0),
        )
        jitter = random.uniform(-jitter_bound, jitter_bound)
        return refreshed_at + max(60.0, float(self.token_refresh_interval) + jitter)

    async def _stop_for_auth_failure(self, code):
        if code not in {"session_expired", "risk_control"}:
            raise ValueError("only confirmed auth failures can open the circuit")
        self.authentication_failure_code = code
        self.token_circuit_open = True
        self.connection_ready.clear()
        self._write_auth_status(code, True)
        websocket = self.ws
        if websocket is not None:
            try:
                await websocket.close()
            except Exception as exc:
                logger.warning("认证失败关闭连接异常 error={}", type(exc).__name__)

    async def _record_transient_token_failure(self, code):
        if code not in {
            "platform_busy",
            "network_error",
            "response_invalid",
            "token_unavailable",
            "account_restricted",
        }:
            code = "token_unavailable"
        self.token_consecutive_failures += 1
        retry_delay = self._token_failure_delay()
        self.next_token_refresh_at = time.time() + retry_delay
        failure_class = (
            "CAPABILITY_RESTRICTED" if code == "account_restricted" else "TRANSIENT"
        )
        self._set_auth_state(
            phase="DEGRADED",
            mtop_token="DEGRADED",
            websocket=("REGISTERED" if self._has_live_websocket() else "DISCONNECTED"),
            failure_code=code,
            failure_class=failure_class,
            failure_count=self.token_consecutive_failures,
            next_retry_at=self.next_token_refresh_at,
            needs_human=False,
        )
        logger.warning(
            "Token刷新暂时失败 code={} round={} next_retry_seconds={:.1f}",
            code,
            self.token_consecutive_failures,
            retry_delay,
        )

    async def refresh_token(self):
        """进程内 single-flight；普通调度轮次只产生一个 Token 请求。"""
        observed_generation = self._token_refresh_generation
        async with self.token_refresh_lock:
            auth_status = self._read_auth_status()
            if self.token_circuit_open or auth_status["needs_human"]:
                code = self.authentication_failure_code or auth_status["code"]
                self.authentication_failure_code = code
                self.token_circuit_open = True
                raise AuthenticationUnavailableError(code)

            if observed_generation != self._token_refresh_generation and self.current_token:
                return self.current_token

            now = time.time()
            if now < self.next_token_refresh_at:
                return self.current_token

            self._set_auth_state(
                phase=("WS_REGISTERED" if self._has_live_websocket() else "SESSION_VALID"),
                mtop_token="REFRESHING",
                websocket=("REGISTERED" if self._has_live_websocket() else "DISCONNECTED"),
                failure_code="ok",
                failure_class="NONE",
                next_retry_at=0,
                needs_human=False,
            )
            logger.info("开始刷新Token")
            token_result = None
            failure_code = None
            try:
                token_result = await asyncio.to_thread(
                    self.xianyu.get_token, self.device_id
                )
            except XianyuAuthenticationError as exc:
                await self._stop_for_auth_failure(exc.code)
                raise AuthenticationUnavailableError(exc.code) from None
            except XianyuApiError as exc:
                failure_code = exc.code
            except Exception as exc:
                failure_code = "response_invalid"
                logger.warning("Token刷新异常 error={}", type(exc).__name__)
            finally:
                try:
                    self._persist_mtop_cookies()
                except (OSError, RuntimeError) as exc:
                    logger.warning("短期认证状态保存失败 error={}", type(exc).__name__)

            data = token_result.get("data") if isinstance(token_result, dict) else None
            new_token = data.get("accessToken") if isinstance(data, dict) else None
            if isinstance(new_token, str) and new_token:
                refreshed_at = time.time()
                self.current_token = new_token
                self.last_token_refresh_time = refreshed_at
                self.next_token_refresh_at = self._next_regular_refresh_at(refreshed_at)
                self.token_consecutive_failures = 0
                self._token_refresh_generation += 1
                self._set_auth_state(
                    phase=("WS_REGISTERED" if self._has_live_websocket() else "TOKEN_VALID"),
                    session="VALID",
                    mtop_token="VALID",
                    websocket=("REGISTERED" if self._has_live_websocket() else "DISCONNECTED"),
                    failure_code="ok",
                    failure_class="NONE",
                    failure_count=0,
                    next_retry_at=self.next_token_refresh_at,
                    needs_human=False,
                )
                logger.info("Token刷新成功")
                return new_token

            await self._record_transient_token_failure(
                failure_code or "token_unavailable"
            )
            return None

    async def _ensure_startup_token(self):
        auth_status = self._read_auth_status()
        if auth_status["needs_human"]:
            code = auth_status["code"]
            self.authentication_failure_code = code
            self.token_circuit_open = True
            raise AuthenticationUnavailableError(code)

        if not self._startup_jitter_applied:
            self._startup_jitter_applied = True
            if self.token_startup_jitter_seconds > 0:
                await asyncio.sleep(
                    random.uniform(0.0, float(self.token_startup_jitter_seconds))
                )

        if self.current_token and time.time() < self.next_token_refresh_at:
            return self.current_token

        while True:
            token = await self.refresh_token()
            if token or self.current_token:
                return token or self.current_token
            retry_delay = max(0.0, self.next_token_refresh_at - time.time())
            await asyncio.sleep(max(retry_delay, 0.05))

    async def _request_controlled_reconnect(self, generation):
        if generation <= self._reconnect_generation or not self._has_live_websocket():
            return False
        self._reconnect_generation = generation
        self.connection_restart_flag = True
        logger.info("Token刷新成功，准备受控重连")
        await self.ws.close()
        return True

    async def token_refresh_loop(self):
        """统一外层调度器：瞬时失败保留旧连接并按有界退避重试。"""
        while True:
            retry_delay = max(0.0, self.next_token_refresh_at - time.time())
            if retry_delay > 0:
                await asyncio.sleep(retry_delay)
            previous_generation = self._token_refresh_generation
            try:
                new_token = await self.refresh_token()
            except AuthenticationUnavailableError:
                return
            if new_token and self._token_refresh_generation != previous_generation:
                if await self._request_controlled_reconnect(
                    self._token_refresh_generation
                ):
                    return

    async def _outgoing_image_content(self, media):
        try:
            items = normalize_manual_reply_media(media)
        except sqlite3.IntegrityError as exc:
            raise ValueError("manual reply media must contain one image") from exc
        if len(items) != 1:
            raise ValueError("manual reply media must contain one image")
        relative_path = str(items[0].get("path") or "").strip()
        root = os.path.realpath(self.state_dir)
        media_path = os.path.join(root, relative_path)
        try:
            file_stat = os.lstat(media_path)
        except OSError as exc:
            raise FileNotFoundError("manual reply image is unavailable") from exc
        if (
            os.path.dirname(os.path.realpath(media_path)) != root
            or not stat.S_ISREG(file_stat.st_mode)
        ):
            raise FileNotFoundError("manual reply image is unavailable")
        result = await asyncio.to_thread(self.xianyu.upload_media, media_path)
        uploaded = result.get("object") if isinstance(result, dict) else None
        if not isinstance(uploaded, dict):
            raise RuntimeError("platform image upload response invalid")
        url = self._safe_media_url(uploaded.get("url"))
        if not url:
            raise RuntimeError("platform image upload URL invalid")
        width = height = 0
        pix = str(uploaded.get("pix") or "")
        match = re.fullmatch(r"(\d{1,5})x(\d{1,5})", pix)
        if match:
            width, height = (int(value) for value in match.groups())
        return {
            "contentType": 2,
            "image": {
                "pics": [{
                    "type": 0,
                    "url": url,
                    "width": width,
                    "height": height,
                }],
            },
        }

    @classmethod
    def _sent_media_summary(cls, content):
        """Extract only the safe public image summary from a send payload."""
        if not isinstance(content, dict) or content.get("contentType") != 2:
            return None
        image = content.get("image")
        pics = image.get("pics") if isinstance(image, dict) else None
        first = pics[0] if isinstance(pics, list) and pics else None
        if not isinstance(first, dict):
            return None
        url = cls._safe_media_url(first.get("url"))
        if not url:
            return None
        def bounded_dimension(value):
            try:
                number = int(value)
            except (TypeError, ValueError, OverflowError):
                return 0
            return number if 0 <= number <= 10000 else 0
        return {
            "type": "image",
            "url": url,
            "alt": "图片",
            "label": "图片",
            "width": bounded_dimension(first.get("width")),
            "height": bounded_dimension(first.get("height")),
        }

    async def send_msg(self, ws, cid, toid, text, message_uuid=None, media=None):
        content = (
            await self._outgoing_image_content(media)
            if media
            else {"contentType": 1, "text": {"text": text}}
        )
        sent_media = self._sent_media_summary(content) if media else None
        content_type = 2 if media else 1
        text_base64 = str(base64.b64encode(json.dumps(content).encode('utf-8')), 'utf-8')
        message_mid = generate_mid()
        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {
                "mid": message_mid
            },
            "body": [
                {
                    "uuid": message_uuid or generate_uuid(),
                    "cid": f"{cid}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {
                            "type": content_type,
                            "data": text_base64
                        }
                    },
                    "redPointPolicy": 0,
                    "extension": {
                        "extJson": "{}"
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    },
                    "mtags": {},
                    "msgReadStatusSetting": 1
                },
                {
                    "actualReceivers": [
                        f"{toid}@goofish",
                        f"{self.myid}@goofish"
                    ]
                }
            ]
        }
        ack_future = asyncio.get_running_loop().create_future()
        self.pending_send_acks[message_mid] = ack_future
        try:
            await ws.send(json.dumps(msg))
            await asyncio.wait_for(ack_future, timeout=self.send_ack_timeout)
        finally:
            self.pending_send_acks.pop(message_mid, None)
        return sent_media

    async def send_text_reliably(
        self, cid, toid, text, message_key=None, before_attempt=None,
        allow_manual=False, media=None,
    ):
        """Retry once after a real reconnection, reusing the same message UUID.

        allow_manual=True 用于已核验订单的自动发货消息:人工接管不阻断发货。
        """
        message_uuid = (
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"xianyu:{message_key}"))
            if message_key
            else generate_uuid()
        )
        last_error = None
        for attempt in range(2):
            if not allow_manual and self.is_manual_mode(cid):
                raise ManualTakeoverError("chat entered manual mode")
            try:
                await asyncio.wait_for(self.connection_ready.wait(), timeout=45)
                if not allow_manual and self.is_manual_mode(cid):
                    raise ManualTakeoverError("chat entered manual mode")
                ws = self.ws
                if ws is None:
                    raise ConnectionError("websocket is unavailable")
                # Reverify only after the connection is usable, immediately
                # before the send, so a reconnect wait cannot stale the proof.
                if before_attempt is not None:
                    await before_attempt()
                send_kwargs = {"message_uuid": message_uuid}
                if media:
                    send_kwargs["media"] = media
                return await self.send_msg(ws, cid, toid, text, **send_kwargs)
            except Exception as exc:
                last_error = exc
                if isinstance(exc, (ManualTakeoverError, AutomationReplySuppressed)):
                    raise
                current_ws = self.ws
                self.connection_ready.clear()
                if current_ws is not None:
                    try:
                        await asyncio.wait_for(current_ws.close(), timeout=5)
                    except Exception:
                        pass
                if attempt == 0:
                    logger.warning("消息发送失败，等待连接恢复后重试 error={}", type(exc).__name__)
                    await asyncio.sleep(0)
        raise last_error or ConnectionError("message delivery failed")

    @staticmethod
    def _manual_reply_error_code(error):
        if isinstance(error, ManualTakeoverError):
            return "manual_takeover_ended"
        if isinstance(error, PlatformMessageRejected):
            return "platform_rejected"
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return "platform_ack_timeout"
        if isinstance(error, ConnectionError):
            return "connection_unavailable"
        return "send_error"

    async def _manual_reply_lease_heartbeat(
        self, reply_id, done, lease_lost, send_task,
    ):
        """Keep a claimed outbox row private while its platform send is active."""
        interval = self.manual_reply_lease_heartbeat_interval
        while True:
            try:
                await asyncio.wait_for(done.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                renewed = self.context_manager.renew_manual_reply_lease(
                    reply_id,
                    self.manual_reply_owner,
                    self.manual_reply_lease_seconds,
                )
            except Exception as error:
                logger.error("人工回复发送租约续期失败 error={}", type(error).__name__)
                renewed = False
            if renewed:
                continue
            try:
                reply = self.context_manager.get_manual_reply(reply_id)
            except Exception:
                reply = None
            if reply is not None and str(reply.get("status") or "") == "acknowledged":
                return
            lease_lost.set()
            if not send_task.done():
                send_task.cancel()
            return

    def _cleanup_manual_media(self, media):
        try:
            items = normalize_manual_reply_media(media)
        except sqlite3.IntegrityError:
            logger.warning("人工图片临时文件清理已拒绝 invalid_payload")
            return []
        results = []
        for item in items:
            status = self.context_manager.cleanup_manual_reply_image(item.get("path"))
            results.append(status)
            if status in {"invalid", "unavailable"}:
                logger.warning("人工图片临时文件清理未完成 status={}", status)
        return results

    def _cleanup_completed_manual_media_if_due(self):
        """Boundedly compensate files left after a committed final ACK."""
        now = time.monotonic()
        next_at = float(
            getattr(self, "_completed_manual_media_cleanup_next_at", 0.0) or 0.0
        )
        if now < next_at:
            return []
        self._completed_manual_media_cleanup_next_at = (
            now + self.COMPLETED_MANUAL_MEDIA_CLEANUP_INTERVAL
        )
        before_id = int(
            getattr(self, "_completed_manual_media_cleanup_before_id", 0) or 0
        )
        try:
            batch = self.context_manager.completed_manual_reply_image_cleanup_batch(
                before_id=before_id,
                limit=self.COMPLETED_MANUAL_MEDIA_CLEANUP_LIMIT,
            )
        except Exception as error:
            logger.warning(
                "已完成人工图片补偿扫描失败 error={}", type(error).__name__
            )
            return []
        self._completed_manual_media_cleanup_before_id = int(
            batch.get("before_id") or 0
        )
        results = []
        for path in batch.get("paths") or []:
            status = self.context_manager.cleanup_manual_reply_image(path)
            results.append(status)
            if status in {"invalid", "unavailable"}:
                logger.warning("已完成人工图片补偿清理未完成 status={}", status)
        return results

    async def process_manual_outbox_once(self):
        """Send one parent reply as ordered image parts followed by optional text."""
        self._cleanup_completed_manual_media_if_due()
        claimed = self.context_manager.claim_manual_replies(
            self.manual_reply_owner,
            limit=1,
            lease_seconds=self.manual_reply_lease_seconds,
        )
        if not claimed:
            return "empty"
        reply = claimed[0]
        reply_id = int(reply["id"])
        chat_id = str(reply.get("chat_id") or "").strip()
        recipient_id = str(reply.get("recipient_id") or "").strip()
        content = str(reply.get("content") or "")
        try:
            media = normalize_manual_reply_media(reply.get("media_json"))
            parts = sorted(
                [part for part in reply.get("parts", []) if isinstance(part, dict)],
                key=lambda part: int(part.get("part_index", -1)),
            )
            expected_parts = [
                (index, "image", index) for index in range(len(media))
            ]
            if content.strip():
                expected_parts.append((len(expected_parts), "text", None))
            actual_parts = [
                (
                    int(part.get("part_index", -1)),
                    str(part.get("kind") or ""),
                    None if part.get("media_index") is None else int(part.get("media_index")),
                )
                for part in parts
            ]
            pending_seen = False
            acknowledgements_out_of_order = False
            for part in parts:
                if part.get("acknowledged_at") is None:
                    pending_seen = True
                elif pending_seen:
                    acknowledgements_out_of_order = True
                    break
        except (sqlite3.IntegrityError, TypeError, ValueError, OverflowError):
            media = []
            parts = []
            expected_parts = []
            actual_parts = []
            acknowledgements_out_of_order = True
        invalid = (
            not chat_id
            or not recipient_id
            or (not content.strip() and not media)
            or len(content) > self.MAX_REPLY_CHARS
            or actual_parts != expected_parts
            or acknowledgements_out_of_order
        )
        if invalid:
            self.context_manager.fail_manual_reply(
                reply_id,
                self.manual_reply_owner,
                "invalid_payload",
                terminal=True,
            )
            return "manual_review"

        for part in parts:
            if part.get("acknowledged_at") is not None and str(part.get("kind")) == "image":
                media_index = int(part.get("media_index"))
                self._cleanup_manual_media([media[media_index]])

        async def verify_takeover():
            if not self.context_manager.renew_manual_reply_lease(
                reply_id,
                self.manual_reply_owner,
                self.manual_reply_lease_seconds,
            ):
                raise ManualReplyLeaseLost("manual reply lease lost")
            if not self.is_manual_mode(chat_id):
                raise ManualTakeoverError("manual takeover ended")

        async def send_remaining_parts():
            base_key = f"manual_reply:{self.account_key}:{reply_id}"
            has_images = bool(media)
            final_result = None
            for part in parts:
                if part.get("acknowledged_at") is not None:
                    continue
                part_index = int(part["part_index"])
                kind = str(part["kind"])
                sent_media = None
                if kind == "image":
                    media_index = int(part["media_index"])
                    message_key = (
                        base_key
                        if media_index == 0
                        else f"{base_key}:image:{media_index + 1}"
                    )
                    sent_media = await self.send_text_reliably(
                        chat_id,
                        recipient_id,
                        "",
                        message_key=message_key,
                        before_attempt=verify_takeover,
                        allow_manual=True,
                        media=[media[media_index]],
                    )
                elif kind == "text":
                    message_key = f"{base_key}:text" if has_images else base_key
                    await self.send_text_reliably(
                        chat_id,
                        recipient_id,
                        content,
                        message_key=message_key,
                        before_attempt=verify_takeover,
                        allow_manual=True,
                    )
                else:
                    raise ValueError("manual reply part kind is invalid")
                final_result = self.context_manager.acknowledge_manual_reply_part(
                    reply_id,
                    self.manual_reply_owner,
                    self.myid,
                    part_index,
                    sent_media=sent_media,
                )
                if final_result is None:
                    raise ManualReplyLeaseLost("manual reply lease lost")
                if final_result.get("complete"):
                    self._cleanup_manual_media(media)
                elif kind == "image":
                    self._cleanup_manual_media([media[int(part["media_index"])]])
            return final_result

        lease_done = asyncio.Event()
        lease_lost = asyncio.Event()
        send_task = asyncio.create_task(send_remaining_parts())
        heartbeat_task = asyncio.create_task(
            self._manual_reply_lease_heartbeat(
                reply_id,
                lease_done,
                lease_lost,
                send_task,
            )
        )
        send_error = None
        result = None
        try:
            result = await send_task
        except asyncio.CancelledError:
            if lease_lost.is_set():
                return "lease_lost"
            raise
        except Exception as error:
            send_error = error
        finally:
            lease_done.set()
            await heartbeat_task

        if isinstance(send_error, ManualReplyLeaseLost):
            return "lease_lost"
        if isinstance(send_error, ManualTakeoverError):
            self.context_manager.fail_manual_reply(
                reply_id,
                self.manual_reply_owner,
                self._manual_reply_error_code(send_error),
                terminal=True,
            )
            return "manual_review"
        if send_error is not None:
            attempts = max(1, int(reply.get("attempts") or 1))
            return self.context_manager.fail_manual_reply(
                reply_id,
                self.manual_reply_owner,
                self._manual_reply_error_code(send_error),
                retry_delay=min(300, 2 ** min(attempts, 8)),
            ) or "lease_lost"
        return "acknowledged" if result and result.get("complete") else "lease_lost"

    async def _manual_outbox_loop(self):
        while True:
            try:
                await self.connection_ready.wait()
                outcome = await self.process_manual_outbox_once()
                if outcome == "empty":
                    await asyncio.sleep(self.manual_reply_poll_interval)
                else:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("人工回复队列处理失败 error={}", type(error).__name__)
                await asyncio.sleep(self.manual_reply_poll_interval)

    async def init(self, ws):
        # Token 必须在 WebSocket 握手前准备完成；init 只负责注册。
        auth_status = self._read_auth_status()
        if auth_status["needs_human"]:
            raise AuthenticationUnavailableError(auth_status["code"])
        if not self.current_token:
            logger.error("无法获取有效Token，初始化失败")
            raise AuthenticationUnavailableError("token_unavailable")
        self._set_auth_state(
            phase="TOKEN_VALID",
            session="VALID",
            mtop_token="VALID",
            websocket="REGISTERING",
            failure_code="ok",
            failure_class="NONE",
            needs_human=False,
        )

        msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": self.current_token,
                "ua": DINGTALK_REGISTRATION_UA,
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        await ws.send(json.dumps(msg))
        # 生产默认保留短暂注册等待；测试只注入此值为 0，绝不全局替换
        # asyncio.sleep，否则后台刷新循环会退化为无限忙循环。
        await asyncio.sleep(self.websocket_registration_wait_seconds)
        msg = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "5701741704675979 0"}, "body": [
            {"pipeline": "sync", "tooLong2Tag": "PNM,1", "channel": "sync", "topic": "sync", "highPts": 0,
             "pts": int(time.time() * 1000) * 1000, "seq": 0, "timestamp": int(time.time() * 1000)}]}
        await ws.send(json.dumps(msg))
        logger.info('连接注册完成')
        if self.ws is ws:
            self.connection_ready.set()
            self._set_auth_state(
                phase="WS_REGISTERED",
                session="VALID",
                mtop_token="VALID",
                websocket="REGISTERED",
                failure_code="ok",
                failure_class="NONE",
                failure_count=0,
                next_retry_at=self.next_token_refresh_at,
                needs_human=False,
            )

    @classmethod
    def _media_kind(cls, value):
        raw = str(value or "").strip().lower()
        if raw in {"2", "image", "img", "picture", "photo", "pics"} or any(
            token in raw for token in ("image", "img", "pic", "photo")
        ):
            return "image"
        if raw in {"emoji", "emoticon", "emotion", "face", "sticker"} or any(
            token in raw for token in ("emoji", "emotion", "sticker")
        ):
            return "emoji"
        if "audio" in raw or "voice" in raw:
            return "audio"
        if "video" in raw:
            return "video"
        if "file" in raw or "document" in raw:
            return "file"
        if "link" in raw or raw in {"url", "href", "uri"}:
            return "link"
        return ""

    @staticmethod
    def _media_placeholder(kind):
        return {
            "image": "[图片]",
            "emoji": "[表情]",
            "audio": "[音频]",
            "video": "[视频]",
            "file": "[文件]",
            "link": "[链接]",
        }.get(kind, "[富媒体]")

    @classmethod
    def _safe_media_url(cls, value):
        if not isinstance(value, str):
            return ""
        candidate = value.strip()
        if len(candidate) > cls.MAX_RICH_MEDIA_URL_CHARS:
            return ""
        try:
            parsed = urlparse(candidate)
        except ValueError:
            return ""
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            return ""
        return candidate

    @classmethod
    def _normalize_chat_content(cls, details):
        """标准化文本与富媒体，保证结构化消息不会在识别阶段丢失。"""
        if not isinstance(details, dict):
            return {"text": None, "media": [], "content_type": "text"}

        text_parts = []
        media = []
        seen_media = set()
        text_keys = {"text", "title", "alt", "label", "caption", "message", "remindercontent"}
        media_value_keys = {
            "url", "src", "href", "uri", "imageurl", "image_url", "audiourl", "audio_url",
            "videourl", "video_url", "fileurl", "file_url", "downloadurl", "download_url",
        }

        def append_text(value):
            if not isinstance(value, str):
                return
            raw = value.strip()
            if not raw:
                return
            if raw[:1] in {"{", "["} and raw[-1:] in {"}", "]"}:
                try:
                    decoded = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    decoded = None
                if isinstance(decoded, (dict, list)):
                    walk(decoded)
                    return

            cursor = 0
            found_image = False
            for match in cls.IMAGE_URL_RE.finditer(raw):
                prefix = raw[cursor:match.start()].strip()
                if prefix:
                    text_parts.append(prefix)
                url = cls._safe_media_url(match.group(0))
                if url:
                    media_item = {"type": "image", "url": url, "alt": "图片", "label": "图片"}
                    key = (media_item["type"], media_item["url"])
                    if key not in seen_media and len(media) < cls.MAX_RICH_MEDIA_ITEMS:
                        seen_media.add(key)
                        media.append(media_item)
                    text_parts.append("[图片]")
                    found_image = True
                else:
                    text_parts.append(match.group(0))
                cursor = match.end()
            suffix = raw[cursor:].strip()
            if suffix:
                text_parts.append(suffix)
            if not found_image and cursor == 0 and suffix:
                # The suffix is the complete plain-text value; do not append it twice.
                return

        def number(value, minimum=0, maximum=100000):
            try:
                value = int(value)
            except (TypeError, ValueError):
                return 0
            return value if minimum <= value <= maximum else 0

        def add_media(kind, value=None, *, label="", width=0, height=0, duration_ms=0):
            kind = kind or "unknown"
            url = cls._safe_media_url(value)
            clean_label = str(label or "").strip()[:160]
            if kind == "emoji" and not clean_label and isinstance(value, str):
                clean_label = value.strip()[:160]
            key = (kind, url, clean_label, width, height, duration_ms)
            if key in seen_media or len(media) >= cls.MAX_RICH_MEDIA_ITEMS:
                return
            seen_media.add(key)
            item = {
                "type": kind,
                "url": url,
                "alt": clean_label or cls._media_placeholder(kind),
                "width": number(width, 0, 10000),
                "height": number(height, 0, 10000),
                "duration_ms": number(duration_ms, 0, 86_400_000),
                "label": clean_label or cls._media_placeholder(kind),
            }
            media.append(item)

        media_container_keys = {
            "attachments", "attachment", "media", "resources", "resource", "richmedia", "rich_media",
        }

        def walk(node, hint="", field="", inherited_dimensions=None):
            if len(media) >= cls.MAX_RICH_MEDIA_ITEMS and not text_parts:
                return
            if isinstance(node, str):
                kind = cls._media_kind(hint or field)
                if kind and kind not in {"link", "unknown"} and field not in text_keys:
                    add_media(kind, node, label=node if kind == "emoji" else "")
                    if kind == "emoji":
                        append_text(node)
                else:
                    append_text(node)
                return
            if isinstance(node, list):
                for item in node[: cls.MAX_RICH_MEDIA_ITEMS]:
                    walk(item, hint, field, inherited_dimensions)
                return
            if not isinstance(node, dict):
                return

            local_hint = cls._media_kind(
                node.get("contentType")
                or node.get("content_type")
                or node.get("mediaType")
                or node.get("type")
                or hint
                or field
            )
            dimensions = inherited_dimensions or {
                "width": number(node.get("width") or node.get("w")),
                "height": number(node.get("height") or node.get("h")),
                "duration_ms": number(node.get("duration_ms") or node.get("duration")),
            }
            media_before = len(media)
            for key, value in list(node.items())[:80]:
                lowered = str(key).lower()
                key_kind = cls._media_kind(lowered)
                if lowered in {"reminderurl", "reminder_url"}:
                    # 商品上下文导航地址不是买家发送的富媒体附件。
                    continue
                if lowered in media_value_keys or lowered.endswith("url") or lowered in {"src", "href", "uri"}:
                    url_kind = key_kind if key_kind and key_kind != "link" else local_hint
                    add_media(
                        url_kind or "link",
                        value,
                        width=dimensions["width"],
                        height=dimensions["height"],
                        duration_ms=dimensions["duration_ms"],
                    )
                elif lowered in text_keys:
                    if isinstance(value, str):
                        append_text(value)
                    else:
                        walk(value, local_hint, lowered, dimensions)
                elif lowered in {"pics", "images", "image", "photo", "audio", "video", "file", "emoji", "emotion", "sticker"}:
                    walk(value, key_kind or local_hint, lowered, dimensions)
                elif lowered in {"content", "data", "body", "message", "value", "richcontent", "rich_content", "custom"}:
                    walk(value, local_hint or key_kind, lowered, dimensions)
                elif lowered in media_container_keys:
                    walk(value, key_kind or local_hint or "unknown", lowered, dimensions)
                elif isinstance(value, (dict, list)) and (key_kind or local_hint):
                    walk(value, key_kind or local_hint, lowered, dimensions)

            if local_hint and local_hint not in {"link"} and len(media) == media_before:
                summary = ""
                for summary_key in ("label", "alt", "name", "title", "filename", "fileName", "id", "key"):
                    candidate = node.get(summary_key)
                    if isinstance(candidate, (str, int, float)) and str(candidate).strip():
                        summary = str(candidate).strip()[:160]
                        break
                add_media(local_hint, label=summary)

        walk(details)
        if not media:
            def find_media_hint(node):
                if isinstance(node, dict):
                    for key, value in list(node.items())[:80]:
                        hint = cls._media_kind(key)
                        if hint:
                            return hint
                        if str(key).lower() in {"contenttype", "content_type", "mediatype", "type"}:
                            hint = cls._media_kind(value)
                            if hint:
                                return hint
                        if str(key).lower() in media_container_keys:
                            return "unknown"
                        nested = find_media_hint(value)
                        if nested:
                            return nested
                elif isinstance(node, list):
                    for value in node[: cls.MAX_RICH_MEDIA_ITEMS]:
                        nested = find_media_hint(value)
                        if nested:
                            return nested
                return ""
            fallback_kind = find_media_hint(details)
            if fallback_kind:
                add_media(fallback_kind)
        if media:
            placeholders = []
            for item in media:
                placeholder = cls._media_placeholder(item["type"])
                if placeholder not in text_parts:
                    placeholders.append(placeholder)
            text_parts.extend(placeholders)
        text = " ".join(part for part in text_parts if isinstance(part, str) and part.strip()).strip()
        content_type = "text" if not media else media[0]["type"] if len(media) == 1 else "rich"
        return {"text": text or None, "media": media, "content_type": content_type}

    @classmethod
    def _extract_chat_text(cls, details):
        """兼容旧调用方，返回 (文本摘要, 是否含图片)。"""
        normalized = cls._normalize_chat_content(details)
        return normalized["text"], any(item.get("type") == "image" for item in normalized["media"])

    @classmethod
    def _details_have_image(cls, details):
        def contains(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    lowered = str(key).lower()
                    if any(tag in lowered for tag in ("img", "pic", "image", "photo")):
                        return True
                    if contains(value):
                        return True
                return False
            if isinstance(node, list):
                return any(contains(value) for value in node[: cls.MAX_RICH_MEDIA_ITEMS])
            return isinstance(node, str) and bool(
                re.search(r'\.(?:jpe?g|png|webp|gif|heic|bmp)(?:\?|$)', node, re.IGNORECASE)
            )

        return contains(details)

    def _log_rich_media_structure(self, chat_raw, details):
        try:
            shape = ", ".join(
                f"{k}:{type(v).__name__}" for k, v in list(details.items())[:20]
            )
            content = details.get("reminderContent")
            logger.info(
                "富媒体消息结构 chat={} reminderContent_type={} fields={}",
                stable_ref(chat_raw[:256]),
                type(content).__name__,
                shape,
            )
        except Exception as exc:
            logger.warning(
                "富媒体结构诊断失败 error_type={}", type(exc).__name__
            )

    def is_chat_message(self, message):
        """判断是否为包含文本或富媒体内容的聊天消息。"""
        try:
            envelope = message.get("1") if isinstance(message, dict) else None
            details = envelope.get("10") if isinstance(envelope, dict) else None
            chat_raw = envelope.get("2") if isinstance(envelope, dict) else None
            normalized = self._normalize_chat_content(details)
            return (
                isinstance(message, dict)
                and isinstance(envelope, dict)
                and isinstance(chat_raw, str)
                and chat_raw.endswith("@goofish")
                and isinstance(details, dict)
                and (normalized["text"] is not None or bool(normalized["media"]))
            )
        except Exception:
            return False

    def is_sync_package(self, message_data):
        """判断是否为同步包消息"""
        try:
            return (
                isinstance(message_data, dict)
                and "body" in message_data
                and "syncPushPackage" in message_data["body"]
                and "data" in message_data["body"]["syncPushPackage"]
                and len(message_data["body"]["syncPushPackage"]["data"]) > 0
            )
        except Exception:
            return False

    def is_typing_status(self, message):
        """判断是否为用户正在输入状态消息"""
        try:
            return (
                isinstance(message, dict)
                and "1" in message
                and isinstance(message["1"], list)
                and len(message["1"]) > 0
                and isinstance(message["1"][0], dict)
                and "1" in message["1"][0]
                and isinstance(message["1"][0]["1"], str)
                and message["1"][0]["1"].endswith("@goofish")
            )
        except Exception:
            return False

    def is_system_message(self, message):
        """判断是否为系统消息"""
        try:
            return (
                isinstance(message, dict)
                and "3" in message
                and isinstance(message["3"], dict)
                and "needPush" in message["3"]
                and message["3"]["needPush"] == "false"
            )
        except Exception:
            return False

    def is_bracket_system_message(self, message):
        """Identify display-only notices. This function never authorizes delivery."""
        try:
            if not message or not isinstance(message, str):
                return False

            clean_message = message.strip()
            return clean_message.startswith('[') and clean_message.endswith(']')
        except Exception as e:
            logger.error("检查系统消息失败: {}", type(e).__name__)
            return False

    def check_toggle_keywords(self, message):
        """检查消息是否包含切换关键词"""
        message_stripped = message.strip()
        return message_stripped in self.toggle_keywords

    def is_manual_mode(self, chat_id):
        return self.delivery_store.is_manual_mode(chat_id)

    def automatic_reply_suppression_reason(self, chat_id):
        """Return a current, persisted reason that forbids an automatic reply."""
        self._refresh_automation_settings()
        self._refresh_reply_rules()
        if not getattr(self, "reply_rules_available", True):
            return "reply_rules_invalid"
        settings = getattr(self, "automation_settings", {})
        if settings.get("enabled") is False:
            return "automation_disabled"
        if not within_business_hours(settings):
            return "business_hours"
        if self.is_manual_mode(chat_id):
            return "manual_mode"
        now = self.delivery_store.now_fn()
        manual_cooldown = int(settings.get("manual_takeover_cooldown_seconds") or 0)
        manual_exit_at = self.delivery_store.manual_exit_at(chat_id)
        if manual_cooldown > 0 and manual_exit_at is not None and now - manual_exit_at < manual_cooldown:
            return "manual_takeover_cooldown"
        trigger_cooldown = int(settings.get("trigger_cooldown_seconds") or 0)
        last_reply_at = self.delivery_store.automation_last_reply_at(chat_id)
        if trigger_cooldown > 0 and last_reply_at is not None and now - last_reply_at < trigger_cooldown:
            return "trigger_cooldown"
        return None

    def enter_manual_mode(self, chat_id):
        self.delivery_store.set_manual_mode(chat_id, True, self.manual_mode_timeout)

    def exit_manual_mode(self, chat_id):
        self.delivery_store.set_manual_mode(chat_id, False, self.manual_mode_timeout)

    def toggle_manual_mode(self, chat_id):
        """切换人工接管模式"""
        if self.is_manual_mode(chat_id):
            self.exit_manual_mode(chat_id)
            return "auto"
        else:
            self.enter_manual_mode(chat_id)
            return "manual"

    def format_price(self, price):
        """
        处理逻辑：标准化价格（分转元）
        """
        try:
            value = float(price) / 100
            return round(value, 2) if math.isfinite(value) and value >= 0 else 0.0
        except (ValueError, TypeError, OverflowError):
            # 遇到 None 或脏数据，默认返回 0
            return 0.0

    def build_item_description(self, item_info):
        """构建商品描述"""
        clean_skus = []
        raw_sku_list = item_info.get('skuList', [])
        if not isinstance(raw_sku_list, list):
            raw_sku_list = []

        for sku in raw_sku_list[:20]:
            if not isinstance(sku, dict):
                continue
            properties = sku.get('propertyList', [])
            if not isinstance(properties, list):
                properties = []
            specs = [
                value[:64]
                for prop in properties[:8]
                if isinstance(prop, dict)
                and isinstance((value := prop.get('valueText')), str)
                and value
            ]
            spec_text = " ".join(specs) if specs else "默认规格"

            clean_skus.append({
                "spec": spec_text,
                "price": self.format_price(sku.get('price', 0)),
                "stock": self._safe_nonnegative_number(sku.get('quantity', 0)),
            })

        # 获取价格
        valid_prices = [s['price'] for s in clean_skus if s['price'] > 0]

        if valid_prices:
            min_price = min(valid_prices)
            max_price = max(valid_prices)
            if min_price == max_price:
                price_display = f"¥{min_price}"
            else:
                price_display = f"¥{min_price} - ¥{max_price}" # 价格区间
        else:
            main_price = self._safe_nonnegative_number(item_info.get('soldPrice', 0))
            price_display = f"¥{main_price}"

        summary = {
            "title": self._bounded_text(item_info.get('title', ''), 256),
            "desc": self._bounded_text(item_info.get('desc', ''), 2000),
            "price_range": price_display,
            "total_stock": self._safe_nonnegative_number(item_info.get('quantity', 0)),
            "sku_details": clean_skus
        }

        encoded = json.dumps(summary, ensure_ascii=False)
        if len(encoded) <= self.MAX_ITEM_DESCRIPTION_CHARS:
            return encoded

        # Keep title/price and a small representative SKU sample when a
        # malformed or unusually detailed listing exceeds the model budget.
        summary["desc"] = summary["desc"][:1000]
        summary["sku_details"] = summary["sku_details"][:8]
        encoded = json.dumps(summary, ensure_ascii=False)
        if len(encoded) > self.MAX_ITEM_DESCRIPTION_CHARS:
            summary["sku_details"] = []
            encoded = json.dumps(summary, ensure_ascii=False)
        return encoded[: self.MAX_ITEM_DESCRIPTION_CHARS]

    @staticmethod
    def _bounded_text(value, limit):
        return value[:limit] if isinstance(value, str) else ""

    @staticmethod
    def _safe_nonnegative_number(value):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        if not math.isfinite(number) or number < 0:
            return 0
        return int(number) if number.is_integer() else round(number, 2)

    async def _get_fresh_item_info(self, item_id):
        cached = self.context_manager.get_item_info(
            item_id, max_age=self.item_cache_ttl
        )
        if isinstance(cached, dict):
            return cached
        async with self._keyed_lock(
            self.item_locks, self.item_lock_users, item_id
        ):
            cached = self.context_manager.get_item_info(
                item_id, max_age=self.item_cache_ttl
            )
            if isinstance(cached, dict):
                return cached
            try:
                api_result = await self._call_xianyu_api(
                    self.xianyu.get_item_info, item_id
                )
            except Exception as exc:
                logger.error(
                    "商品信息请求失败 item={} error={}",
                    stable_ref(item_id),
                    type(exc).__name__,
                )
                return None
            if not isinstance(api_result, dict) or not isinstance(
                api_result.get("data"), dict
            ):
                return None
            item_info = api_result["data"].get("itemDO")
            if not isinstance(item_info, dict):
                return None
            self.context_manager.save_item_info(item_id, item_info)
            return item_info

    @staticmethod
    def _decode_sync_payload(payload):
        if (
            not isinstance(payload, str)
            or len(payload) > 2 * DeliveryStore.MAX_INBOUND_EVENT_BYTES
        ):
            return None
        try:
            decoded = base64.b64decode(payload, validate=True).decode("utf-8")
            if len(decoded.encode("utf-8")) > DeliveryStore.MAX_INBOUND_EVENT_BYTES:
                return None
            message = json.loads(decoded)
        except Exception:
            try:
                message = json.loads(decrypt(payload))
            except Exception as exc:
                logger.error("消息解密失败: {}", type(exc).__name__)
                return None
        return message if isinstance(message, dict) else None

    @staticmethod
    def _sync_entry_identity(sync_data, index):
        """Return a bounded discriminator for duplicate payloads in one packet.

        Most events expose a stable wrapper key/sequence outside the encrypted
        payload. When that metadata is absent, the packet index is the only
        durable distinction available for two byte-identical entries; replaying
        the same packet preserves that index.
        """
        metadata = {
            key: value
            for key, value in sync_data.items()
            if key != "data"
        }
        if len(metadata) > 32:
            metadata = dict(sorted(metadata.items(), key=lambda pair: str(pair[0]))[:32])
        if metadata:
            try:
                encoded = json.dumps(
                    metadata,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError, OverflowError, RecursionError):
                encoded = ""
            if encoded and len(encoded.encode("utf-8")) <= 4096:
                return "meta:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return f"index:{index}"

    def _decode_sync_entries(self, message_data):
        if not self.is_sync_package(message_data):
            return []
        entries = []
        raw_entries = message_data["body"]["syncPushPackage"]["data"]
        if not isinstance(raw_entries, list):
            return None
        for index, sync_data in enumerate(raw_entries):
            if not isinstance(sync_data, dict):
                return None
            message = self._decode_sync_payload(sync_data.get("data"))
            if message is None:
                return None
            entries.append((message, self._sync_entry_identity(sync_data, index)))
        return entries

    def _decode_sync_messages(self, message_data):
        entries = self._decode_sync_entries(message_data)
        if entries is None:
            return None
        return [message for message, _identity in entries]

    def _persist_sync_package(self, message_data):
        entries = self._decode_sync_entries(message_data)
        if entries is None:
            raise ValueError("sync package could not be decoded")
        decoded_messages = [message for message, _identity in entries]
        base_keys = [self._inbound_event_key(message) for message in decoded_messages]
        duplicate_counts = {}
        for base_key in base_keys:
            duplicate_counts[base_key] = duplicate_counts.get(base_key, 0) + 1
        inbound_events = []
        event_keys = []
        for (decoded_message, entry_identity), base_key in zip(entries, base_keys):
            event_key = (
                self._inbound_event_key(decoded_message, entry_identity)
                if duplicate_counts[base_key] > 1
                else base_key
            )
            chat_key = self._inbound_chat_key(decoded_message, event_key)
            event_keys.append(event_key)
            inbound_events.append(
                self.delivery_store.record_inbound_event(
                    event_key, chat_key, decoded_message
                )
            )
        # A takeover command may preempt a reply that is already running, but
        # it must not jump ahead of an earlier chat event in the same sync
        # packet.  Defer such controls to the ordered worker below; controls
        # that are first for a chat can still be applied before the worker is
        # scheduled.
        earlier_chat_events = set()
        for decoded_message, event_key in zip(decoded_messages, event_keys):
            inbound_chat = self._inbound_chat_key(
                decoded_message, self._inbound_event_key(decoded_message)
            )
            control_chat = self._manual_control_chat_id(decoded_message)
            if control_chat is not None:
                if f"chat:{control_chat}" not in earlier_chat_events:
                    self._preapply_manual_control(decoded_message, event_key)
                continue
            if inbound_chat.startswith("chat:"):
                earlier_chat_events.add(inbound_chat)
        return inbound_events

    @staticmethod
    def _parse_message_timestamp(value):
        """Parse a bounded platform millisecond timestamp without big-int math."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            timestamp = value
        elif isinstance(value, str):
            value = value.strip()
            if not value or len(value) > 16 or not value.isdigit():
                return None
            try:
                timestamp = int(value)
            except (TypeError, ValueError, OverflowError):
                return None
        else:
            return None
        if timestamp < 0 or timestamp > 1_000_000_000_000_000:
            return None
        return timestamp

    def _preapply_manual_control(self, message, inbound_event_key=None):
        """Apply a persisted seller takeover command before delayed replies run."""
        if not self.is_chat_message(message):
            return None
        envelope = message.get("1")
        details = envelope.get("10") if isinstance(envelope, dict) else None
        if not isinstance(details, dict) or details.get("senderUserId") != self.myid:
            return None
        content = details.get("reminderContent")
        chat_raw = envelope.get("2")
        create_time = self._parse_message_timestamp(envelope.get("5"))
        if create_time is None:
            return None
        if (
            not isinstance(content, str)
            or not self.check_toggle_keywords(content)
            or not isinstance(chat_raw, str)
            or not chat_raw.endswith("@goofish")
        ):
            return None
        chat_id = chat_raw[: -len("@goofish")]
        age_ms = time.time() * 1000 - create_time
        if (
            not chat_id
            or len(chat_id) > 256
            or age_ms > self.message_expire_time
            or age_ms < -60_000
        ):
            return None
        source_id = self._manual_control_source_id(
            message,
            chat_id,
            self.myid,
            create_time,
            content,
        )
        mode, applied = self.delivery_store.toggle_manual_mode_once(
            source_id, chat_id, self.manual_mode_timeout
        )
        if applied:
            logger.info(
                "已预应用人工接管命令 chat={} mode={}",
                stable_ref(chat_id),
                mode,
            )
        return mode

    def _manual_control_chat_id(self, message):
        """Return the chat ID for a valid seller takeover candidate."""
        if not self.is_chat_message(message):
            return None
        envelope = message.get("1")
        details = envelope.get("10") if isinstance(envelope, dict) else None
        if not isinstance(details, dict) or details.get("senderUserId") != self.myid:
            return None
        content = details.get("reminderContent")
        raw_chat = envelope.get("2")
        create_time = self._parse_message_timestamp(envelope.get("5"))
        if (
            not isinstance(content, str)
            or not self.check_toggle_keywords(content)
            or not isinstance(raw_chat, str)
            or not raw_chat.endswith("@goofish")
            or create_time is None
        ):
            return None
        chat_id = raw_chat[: -len("@goofish")]
        age_ms = time.time() * 1000 - create_time
        if (
            not chat_id
            or len(chat_id) > 256
            or age_ms > self.message_expire_time
            or age_ms < -60_000
        ):
            return None
        return chat_id

    def _decode_sync_message(self, message_data):
        messages = self._decode_sync_messages(message_data)
        return messages[0] if messages else None

    @staticmethod
    def _inbound_event_key(message, discriminator=None):
        canonical = json.dumps(
            message
            if discriminator is None
            else {"message": message, "entry": str(discriminator)},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sync:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _inbound_chat_key(message, event_key):
        if isinstance(message, dict):
            envelope = message.get("1")
            if isinstance(envelope, dict):
                raw_chat = envelope.get("2")
                if isinstance(raw_chat, str) and raw_chat.endswith("@goofish"):
                    chat_id = raw_chat[: -len("@goofish")]
                    if chat_id and len(chat_id) <= 256:
                        return "chat:" + chat_id
            if isinstance(envelope, str) and envelope.endswith("@goofish"):
                account_id = envelope[: -len("@goofish")]
                if account_id and len(account_id) <= 256:
                    return "chat:" + account_id
            if isinstance(envelope, list) and envelope:
                first = envelope[0]
                raw_account = first.get("1") if isinstance(first, dict) else None
                if isinstance(raw_account, str) and raw_account.endswith("@goofish"):
                    account_id = raw_account[: -len("@goofish")]
                    if account_id and len(account_id) <= 256:
                        return "chat:" + account_id
        return "event:" + event_key.removeprefix("sync:")[:32]

    @staticmethod
    def _platform_message_id(message):
        """Return a bounded platform message ID when the payload exposes one."""
        if not isinstance(message, dict):
            return None
        envelope = message.get("1")
        details = envelope.get("10") if isinstance(envelope, dict) else None
        metadata = message.get("metadata")
        containers = (details, envelope, metadata, message)
        keys = (
            "platform_message_id",
            "messageId",
            "message_id",
            "msgId",
            "msg_id",
            "reminderId",
            "reminder_id",
        )
        for container in containers:
            if not isinstance(container, dict):
                continue
            for key in keys:
                value = container.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    value = str(value)
                if (
                    isinstance(value, str)
                    and value.strip()
                    and len(value.strip()) <= 256
                ):
                    return value.strip()
        return None

    def _message_source_nonce(self, message, inbound_event_key=None):
        platform_id = self._platform_message_id(message)
        if platform_id is not None:
            return "platform:" + platform_id
        if inbound_event_key is None:
            # Direct compatibility callers have no durable package identity;
            # retain the historical deterministic key for those calls.
            return None
        return "event:" + str(inbound_event_key)

    def _manual_control_source_id(
        self, message, chat_id, sender_id, create_time, content
    ):
        """Deduplicate one seller control independently of packet position.

        A real platform message ID distinguishes two intentional controls.  If
        the platform omits it, identical chat/sender/time/content controls are
        treated as one command even when a sync packet repeats the payload.
        """
        platform_id = self._platform_message_id(message)
        source_nonce = "platform:" + platform_id if platform_id is not None else None
        return self._chat_source_id(
            chat_id,
            sender_id,
            create_time,
            content,
            source_nonce=source_nonce,
        )

    @staticmethod
    def _chat_source_id(
        chat_id,
        sender_id,
        create_time,
        content,
        item_id=None,
        source_nonce=None,
    ):
        digest = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
        nonce = ""
        if source_nonce is not None:
            nonce_digest = hashlib.sha256(
                str(source_nonce).encode("utf-8")
            ).hexdigest()
            nonce = f":{nonce_digest}"
        if item_id is None:
            return f"goofish:{chat_id}:{sender_id}:{create_time}{nonce}:{digest}"
        return f"goofish:{chat_id}:{sender_id}:{create_time}:{item_id}{nonce}:{digest}"

    def _resolve_chat_source_id(
        self, chat_id, sender_id, create_time, content, item_id, source_nonce=None
    ):
        source_id = self._chat_source_id(
            chat_id,
            sender_id,
            create_time,
            content,
            item_id,
            source_nonce=source_nonce,
        )
        if self.context_manager.get_source_message(source_id) is not None:
            return source_id
        if source_nonce is not None:
            return source_id
        legacy_source_id = self._chat_source_id(
            chat_id, sender_id, create_time, content
        )
        legacy = self.context_manager.get_source_message(legacy_source_id)
        if legacy is not None and legacy.get("item_id") == str(item_id):
            return legacy_source_id
        return source_id

    @staticmethod
    def _bounded_item_id(value):
        if isinstance(value, bool):
            return ""
        candidate = str(value or "").strip()
        if 1 <= len(candidate) <= 64 and candidate.isascii() and candidate.isdigit():
            return candidate
        return ""

    @classmethod
    def _extract_item_id(cls, details, url_info=""):
        """Find a trusted numeric item ID without requiring reminderUrl."""
        query_keys = {"itemid", "item_id", "productid", "product_id", "goodsid", "goods_id", "auctionid", "auction_id"}
        compact_query_keys = {key.replace("_", "") for key in query_keys}
        url_keys = {"url", "href", "uri", "reminderurl", "reminder_url"}

        def from_url(value):
            if not isinstance(value, str) or not value or len(value) > 4096:
                return ""
            try:
                query = parse_qs(urlparse(value).query, max_num_fields=32)
            except ValueError:
                return ""
            for raw_key, values in query.items():
                if str(raw_key).strip().lower().replace("_", "").replace("-", "") not in compact_query_keys:
                    continue
                for candidate in values:
                    item_id = cls._bounded_item_id(candidate)
                    if item_id:
                        return item_id
            return ""

        item_id = from_url(url_info)
        if item_id:
            return item_id

        visited = set()

        def walk(node, depth=0):
            if depth > 5 or len(visited) >= 160:
                return ""
            if isinstance(node, dict):
                marker = id(node)
                if marker in visited:
                    return ""
                visited.add(marker)
                for key, value in list(node.items())[:80]:
                    lowered = str(key).strip().lower().replace("-", "_")
                    if lowered.replace("_", "") in compact_query_keys:
                        if isinstance(value, (str, int)):
                            item_id = cls._bounded_item_id(value)
                            if item_id:
                                return item_id
                        nested = walk(value, depth + 1)
                        if nested:
                            return nested
                    if lowered in url_keys and isinstance(value, str):
                        nested = from_url(value)
                        if nested:
                            return nested
                    if isinstance(value, (dict, list)):
                        nested = walk(value, depth + 1)
                        if nested:
                            return nested
            elif isinstance(node, list):
                marker = id(node)
                if marker in visited:
                    return ""
                visited.add(marker)
                for value in node[:32]:
                    nested = walk(value, depth + 1)
                    if nested:
                        return nested
            return ""

        return walk(details)

    async def handle_message(self, message_data):
        """Compatibility entry point for tests and non-websocket callers."""
        try:
            messages = (
                self._decode_sync_messages(message_data)
                if self.is_sync_package(message_data)
                else [message_data] if isinstance(message_data, dict) else None
            )
            if messages is None:
                raise ValueError("invalid sync package")
            for message in messages:
                await self._handle_decoded_message(message)
        except Exception as exc:
            logger.error("处理消息失败: {}", type(exc).__name__)

    async def _handle_decoded_message(self, message, inbound_event_key=None):
        if await self.handle_paid_order(message):
            return
        reminder = message.get("3") if isinstance(message, dict) else None
        if isinstance(reminder, dict) and reminder.get("redReminder") == "交易关闭":
            raw_account = message.get("1")
            if isinstance(raw_account, str) and raw_account.endswith("@goofish"):
                self.delivery_store.cancel_awaiting_for_chat(raw_account[:-8])
            return
        if self.is_typing_status(message):
            return
        return await self._process_chat_message(message, inbound_event_key)

    async def _process_chat_message(self, message, inbound_event_key=None):
        envelope = message.get("1") if isinstance(message, dict) else None
        details = envelope.get("10") if isinstance(envelope, dict) else None
        if not isinstance(envelope, dict) or not isinstance(details, dict):
            return
        create_time = self._parse_message_timestamp(envelope.get("5"))
        if create_time is None:
            logger.warning("忽略无效消息时间")
            return
        raw_sender_id = details.get("senderUserId")
        if not isinstance(raw_sender_id, str):
            return
        sender_id = raw_sender_id
        url_info = details.get("reminderUrl") or ""
        chat_raw = envelope.get("2")
        if not isinstance(chat_raw, str):
            return
        if url_info and (not isinstance(url_info, str) or len(url_info) > 4096):
            return
        if not chat_raw.endswith("@goofish"):
            return
        normalized = self._normalize_chat_content(details)
        content = normalized["text"]
        media = normalized["media"]
        content_type = normalized["content_type"]
        if content is None:
            if media or self._details_have_image(details):
                self._log_rich_media_structure(chat_raw, details)
            return
        chat_id = chat_raw[: -len("@goofish")]
        item_id = self._extract_item_id(details, url_info)
        if not item_id:
            item_id = self._bounded_item_id(
                self.context_manager.latest_item_id_by_chat(chat_id)
            )
        if (
            not chat_id
            or len(chat_id) > 256
            or not sender_id
            or len(sender_id) > 256
        ):
            return
        original_content = content
        source_content = original_content
        if media:
            source_content += "\n[media:" + json.dumps(
                media, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )[:4096] + "]"
        if len(content) > self.MAX_CHAT_CONTENT_CHARS:
            logger.warning("买家消息过长，截断处理 chat={}", stable_ref(chat_id))
            content = content[: self.MAX_CHAT_CONTENT_CHARS]
        source_nonce = self._message_source_nonce(message, inbound_event_key)
        if sender_id == self.myid and self.check_toggle_keywords(content):
            # Manual takeover is chat-scoped, so its idempotency key stays
            # independent of the listing and matches the pre-apply path.
            source_id = self._manual_control_source_id(
                message,
                chat_id,
                sender_id,
                create_time,
                original_content,
            )
        else:
            source_id = self._resolve_chat_source_id(
                chat_id,
                sender_id,
                create_time,
                source_content,
                item_id,
                source_nonce=source_nonce,
            )
        age_ms = time.time() * 1000 - create_time
        if age_ms > self.message_expire_time or age_ms < -60_000:
            logger.info("忽略过期消息 chat={}", stable_ref(chat_id))
            return "ignored_expired_message"

        if sender_id == self.myid:
            if self.check_toggle_keywords(content):
                inserted = self.context_manager.add_message_by_chat(
                    chat_id,
                    self.myid,
                    item_id,
                    "control",
                    content,
                    source_id=source_id,
                )
                mode, applied = self.delivery_store.toggle_manual_mode_once(
                    source_id, chat_id, self.manual_mode_timeout
                )
                if inserted or applied:
                    logger.info(
                        "人工接管状态已确认 chat={} mode={}",
                        stable_ref(chat_id),
                        mode,
                    )
                else:
                    logger.info("忽略重复控制消息 chat={}", stable_ref(chat_id))
                return
            inserted = self.context_manager.add_message_by_chat(
                chat_id,
                self.myid,
                item_id,
                "assistant",
                content,
                source_id=source_id,
                content_type=content_type,
                media=media,
            )
            if not inserted:
                # A process may stop after the context commit but before the
                # inbound event is marked complete.  Treat an exact replay as
                # successful, while rejecting a source collision.
                existing = self.context_manager.get_source_message(source_id)
                if not existing or any(
                    existing.get(field) != expected
                    for field, expected in (
                        ("chat_id", chat_id),
                        ("user_id", self.myid),
                        ("item_id", item_id),
                        ("role", "assistant"),
                        ("content", content),
                    )
                ):
                    raise RuntimeError("seller message source collision")
                logger.info("忽略已持久化的重复卖家消息 chat={}", stable_ref(chat_id))
                return
            logger.info("记录卖家人工回复 chat={} chars={}", stable_ref(chat_id), len(content))
            return

        if item_id and not self.delivery_store.record_chat_binding(
            chat_id, sender_id, item_id, observed_at=create_time / 1000
        ):
            logger.warning("聊天身份绑定发生冲突 chat={}", stable_ref(chat_id))

        if (not media and self.is_bracket_system_message(content)) or self.is_system_message(message):
            logger.info("忽略展示型系统消息 chat={}", stable_ref(chat_id))
            return

        async with self._keyed_lock(
            self.chat_locks, self.chat_lock_users, chat_id
        ):
            await self._process_buyer_chat(
                chat_id, sender_id, item_id, content, source_id,
                media=media, content_type=content_type,
            )

    async def _process_buyer_chat(
        self, chat_id, sender_id, item_id, content, source_id,
        media=None, content_type="text",
    ):
        self._refresh_runtime_config()
        assistant_source = f"assistant:{source_id}"
        inserted = self.context_manager.add_message_by_chat(
            chat_id,
            sender_id,
            item_id,
            "user",
            content,
            source_id=source_id,
            content_type=content_type,
            media=media,
        )
        if not inserted:
            user_record = self.context_manager.get_source_message(source_id)
            if not user_record or any(
                user_record.get(field) != expected
                for field, expected in (
                    ("chat_id", str(chat_id)),
                    ("user_id", str(sender_id)),
                    ("item_id", str(item_id)),
                    ("role", "user"),
                    ("content", content),
                )
            ):
                raise RuntimeError("buyer message source collision")
        else:
            logger.info(
                "收到买家消息 chat={} item={} chars={}",
                stable_ref(chat_id),
                stable_ref(item_id),
                len(content),
            )

        if not item_id:
            logger.info("消息已保存但未关联商品，跳过自动回复 chat={}", stable_ref(chat_id))
            return

        draft = self.context_manager.get_source_message(assistant_source)
        if draft is not None and draft.get("role") in {
            "assistant",
            "assistant_cancelled",
            "assistant_no_reply",
        }:
            logger.info("忽略已完成消息 chat={} source={}", stable_ref(chat_id), stable_ref(source_id))
            return
        if draft is not None and draft.get("role") != "assistant_pending":
            raise RuntimeError("assistant reply source has invalid state")
        if self.is_manual_mode(chat_id):
            if draft is not None:
                self.context_manager.cancel_assistant_reply(assistant_source)
                self._delete_assistant_draft_provenance(assistant_source)
            else:
                self.context_manager.record_assistant_outcome(
                    chat_id,
                    self.myid,
                    item_id,
                    assistant_source,
                    "assistant_cancelled",
                )
            logger.info("人工接管中，跳过自动回复 chat={}", stable_ref(chat_id))
            return

        replaying_draft = draft is not None
        rule_matched = False
        reply_from_ai = False
        config_revision = None
        automation_revision = None

        def record_no_reply(reason):
            """Persist a terminal silent outcome when no trustworthy reply exists."""
            self.context_manager.record_assistant_outcome(
                chat_id,
                self.myid,
                item_id,
                assistant_source,
                "assistant_no_reply",
            )
            logger.warning(
                "未发送自动回复，等待后续消息或人工处理 chat={} reason={}",
                stable_ref(chat_id),
                reason,
            )

        def suppress_reply(reason):
            if draft is not None:
                self.context_manager.cancel_assistant_reply(assistant_source)
                self._delete_assistant_draft_provenance(assistant_source)
                logger.warning(
                    "已取消待发送自动回复 chat={} reason={}",
                    stable_ref(chat_id),
                    reason,
                )
            elif reason == "manual_mode":
                self.context_manager.record_assistant_outcome(
                    chat_id,
                    self.myid,
                    item_id,
                    assistant_source,
                    "assistant_cancelled",
                )
                logger.warning(
                    "已取消待发送自动回复 chat={} reason={}",
                    stable_ref(chat_id),
                    reason,
                )
            else:
                record_no_reply(reason)

        suppression_reason = self.automatic_reply_suppression_reason(chat_id)
        if suppression_reason:
            suppress_reply(suppression_reason)
            return
        settings = getattr(self, "automation_settings", {})

        if replaying_draft:
            bot_reply = draft["content"]
            detected_intent = "replay"
            provenance = self._assistant_draft_provenance(assistant_source)
            if provenance is None:
                suppress_reply("draft_provenance_missing")
                return
            rule_matched = provenance["origin"] == "rule"
            reply_from_ai = provenance["origin"] == "ai"
            config_revision = provenance["config_revision"]
            automation_revision = provenance.get("automation_revision")
            if (
                not isinstance(automation_revision, str)
                or not re.fullmatch(r"[0-9a-f]{64}", automation_revision)
                or automation_revision != self._loaded_automation_revision()
            ):
                suppress_reply("automation_config_changed")
                return
        else:
            automation_revision = self._loaded_automation_revision()
            if not isinstance(automation_revision, str) or not re.fullmatch(
                r"[0-9a-f]{64}", automation_revision
            ):
                suppress_reply("automation_revision_invalid")
                return

        if not replaying_draft and (
            rule_reply := self._match_reply_rule(content, item_id, refresh=False)
        ) is not None:
            # Deterministic rules are literal, always precede item lookup and AI,
            # and never inherit any model-output business interpretation.
            bot_reply = rule_reply
            detected_intent = "rule"
            rule_matched = True
        elif not replaying_draft:
            prior_context = self.context_manager.get_context_by_chat(chat_id)
            if (
                prior_context
                and prior_context[-1].get("role") == "user"
                and prior_context[-1].get("content") == content
            ):
                prior_context = prior_context[:-1]
            first_reply = str(settings.get("first_reply") or "").strip()
            fallback_reply = str(settings.get("fallback_reply") or "").strip()
            if getattr(self, "automation_mode", "rules_ai") == "rules":
                if first_reply and not any(
                    isinstance(message, dict) and message.get("role") == "user"
                    for message in prior_context
                ):
                    bot_reply = first_reply
                    detected_intent = "first_reply"
                elif fallback_reply:
                    bot_reply = fallback_reply
                    detected_intent = "fallback"
                else:
                    record_no_reply("no_rule_match")
                    return
            else:
                if self.bot is None:
                    record_no_reply("ai_configuration_invalid")
                    return
                reply_from_ai = True
                item_info = await self._get_fresh_item_info(item_id)
                item_context = _ai_product_facts(item_id, item_info)
                recent_assistant_replies = self.context_manager.get_recent_assistant_replies(
                    chat_id, limit=8
                )
                base_input_chars = len(content) + len(
                    json.dumps(item_context, ensure_ascii=False, separators=(",", ":"))
                ) + sum(
                    len(str(message.get("content", "")))
                    for message in prior_context
                    if isinstance(message, dict)
                )
                input_chars = base_input_chars + sum(
                    len(reply) for reply in recent_assistant_replies
                )
                if not self.delivery_store.reserve_llm_budget(chat_id, input_chars):
                    logger.warning(
                        "LLM预算已达上限，跳过模型调用 chat={}", stable_ref(chat_id)
                    )
                    record_no_reply("budget_exhausted")
                    return

                async def call_ai(recent_replies):
                    return await self._generate_llm_reply(
                        content,
                        item_id,
                        item_context,
                        prior_context,
                        recent_replies,
                    )

                try:
                    (
                        bot_reply,
                        detected_intent,
                        reason_code,
                        config_revision,
                    ) = await call_ai(recent_assistant_replies)
                    if detected_intent != "reply":
                        record_no_reply(reason_code or f"ai_{detected_intent}")
                        return
                    if _unsafe_ai_reply(bot_reply):
                        record_no_reply("unsafe_ai_reply")
                        return
                    duplicate = any(
                        _ai_replies_similar(bot_reply, previous)
                        for previous in recent_assistant_replies
                    )
                    if duplicate:
                        first_candidate = bot_reply.strip()
                        retry_recent = (recent_assistant_replies + [first_candidate])[-8:]
                        retry_input_chars = base_input_chars + sum(
                            len(reply) for reply in retry_recent
                        )
                        if not self.delivery_store.reserve_llm_budget(
                            chat_id, retry_input_chars
                        ):
                            record_no_reply("budget_exhausted")
                            return
                        (
                            bot_reply,
                            detected_intent,
                            reason_code,
                            config_revision,
                        ) = await call_ai(retry_recent)
                        if detected_intent != "reply":
                            record_no_reply(reason_code or f"ai_{detected_intent}")
                            return
                        if _unsafe_ai_reply(bot_reply):
                            record_no_reply("unsafe_ai_reply")
                            return
                        if _ai_replies_similar(bot_reply, first_candidate) or any(
                            _ai_replies_similar(bot_reply, previous)
                            for previous in recent_assistant_replies
                        ):
                            record_no_reply("reply_recent_duplicate")
                            return
                except asyncio.TimeoutError:
                    logger.error("LLM 回复超时 chat={}", stable_ref(chat_id))
                    record_no_reply("timeout")
                    return
                except Exception as exc:
                    logger.error(
                        "LLM 回复失败 chat={} error={}",
                        stable_ref(chat_id),
                        type(exc).__name__,
                    )
                    record_no_reply(type(exc).__name__.lower())
                    return

        if not isinstance(bot_reply, str) or not bot_reply.strip():
            record_no_reply("empty_reply")
            return
        bot_reply = bot_reply.strip()
        if len(bot_reply) > self.MAX_REPLY_CHARS:
            if rule_matched or replaying_draft:
                bot_reply = bot_reply[: self.MAX_REPLY_CHARS]
            else:
                record_no_reply("reply_too_long")
                return
        delay = 0 if replaying_draft else await self.human_reply_delay(content, bot_reply, chat_id)
        if delay:
            logger.info("计划延迟回复 chat={} seconds={:.1f}", stable_ref(chat_id), delay)
            await asyncio.sleep(delay)

        suppression_reason = self.automatic_reply_suppression_reason(chat_id)
        if suppression_reason:
            suppress_reply(suppression_reason)
            return
        if automation_revision != self._loaded_automation_revision():
            suppress_reply("automation_config_changed")
            return

        if reply_from_ai and (
            isinstance(config_revision, bool)
            or not isinstance(config_revision, int)
            or config_revision < 0
        ):
            suppress_reply("ai_config_revision_invalid")
            return

        async def verify_automatic_reply_before_attempt():
            reason = self.automatic_reply_suppression_reason(chat_id)
            if reason:
                raise AutomationReplySuppressed(reason)
            if automation_revision != self._loaded_automation_revision():
                raise AutomationReplySuppressed("automation_config_changed")
            if reply_from_ai:
                await self._ensure_ai_reply_ready(chat_id, config_revision)

        self._store_assistant_draft_provenance(
            assistant_source,
            "ai" if reply_from_ai else "rule",
            config_revision=config_revision,
            automation_revision=automation_revision,
        )
        draft = self.context_manager.prepare_assistant_reply(
            chat_id,
            self.myid,
            item_id,
            bot_reply,
            assistant_source,
        )
        bot_reply = draft["content"]

        try:
            await self.send_text_reliably(
                chat_id,
                sender_id,
                bot_reply,
                message_key=f"reply:{source_id}",
                before_attempt=verify_automatic_reply_before_attempt,
            )
        except (ManualTakeoverError, AutomationReplySuppressed) as exc:
            reason = exc.reason if isinstance(exc, AutomationReplySuppressed) else "manual_mode"
            self.context_manager.cancel_assistant_reply(assistant_source)
            self._delete_assistant_draft_provenance(assistant_source)
            logger.warning(
                "发送前策略检查取消自动回复 chat={} reason={}",
                stable_ref(chat_id),
                reason,
            )
            return
        self.context_manager.complete_assistant_reply(assistant_source, bot_reply)
        self._delete_assistant_draft_provenance(assistant_source)
        self.delivery_store.mark_automation_reply_sent(chat_id)
        logger.info("机器人回复已发送 chat={} chars={}", stable_ref(chat_id), len(bot_reply))

    async def send_heartbeat(self, ws):
        """发送心跳包并等待响应"""
        try:
            heartbeat_mid = generate_mid()
            heartbeat_msg = {
                "lwp": "/!",
                "headers": {
                    "mid": heartbeat_mid
                }
            }
            self.heartbeat_mids.add(heartbeat_mid)
            await ws.send(json.dumps(heartbeat_msg))
            self.last_heartbeat_time = time.time()
            logger.debug("心跳包已发送")
            return heartbeat_mid
        except Exception as e:
            self.heartbeat_mids.discard(locals().get("heartbeat_mid"))
            logger.error("发送心跳包失败: {}", type(e).__name__)
            raise

    async def heartbeat_loop(self, ws):
        """心跳维护循环"""
        while True:
            try:
                current_time = time.time()

                # 检查是否需要发送心跳
                if current_time - self.last_heartbeat_time >= self.heartbeat_interval:
                    await self.send_heartbeat(ws)

                # Keep the reader alive long enough for sparse server heartbeat responses.
                if (current_time - self.last_heartbeat_response) > self.heartbeat_timeout:
                    logger.warning("心跳响应超时，主动断开连接触发重连")
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    break

                await asyncio.sleep(1)
            except Exception as e:
                logger.error("心跳循环出错: {}", type(e).__name__)
                break

    async def handle_heartbeat_response(self, message_data):
        """处理心跳响应"""
        try:
            if not isinstance(message_data, dict):
                return False
            headers = message_data.get("headers")
            if not isinstance(headers, dict):
                return False
            response_mid = headers.get("mid")
            if not isinstance(response_mid, str):
                return False
            pending = self.pending_send_acks.get(response_mid)
            if pending is not None:
                if not pending.done():
                    if message_data.get("code") == 200:
                        pending.set_result(True)
                    else:
                        pending.set_exception(PlatformMessageRejected("platform rejected message"))
                return True
            if message_data.get("code") != 200:
                return False
            if response_mid in self.heartbeat_mids:
                self.heartbeat_mids.discard(response_mid)
                self.last_heartbeat_response = time.time()
                logger.debug("收到心跳响应")
                return True
        except Exception as e:
            logger.error("处理协议响应出错: {}", type(e).__name__)
        return False

    def _task_done(self, task, collection, label):
        collection.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("后台任务失败 task={} error={}", label, type(error).__name__)

    async def _run_inbound_chat_worker(self, chat_id):
        async with self.message_semaphore:
            while True:
                pending = next(
                    (
                        event
                        for event in self.delivery_store.pending_inbound_events(ready_only=True)
                        if event.chat_id == chat_id
                    ),
                    None,
                )
                if pending is None:
                    return True
                event = self.delivery_store.claim_inbound_event(pending.key)
                if event is None:
                    return True
                try:
                    payload = json.loads(event.payload)
                    if not isinstance(payload, dict):
                        raise ValueError("persisted inbound payload is invalid")
                    outcome = await self._handle_decoded_message(
                        payload, inbound_event_key=event.key
                    )
                    if outcome == "ignored_expired_message":
                        self.delivery_store.ignore_inbound_event(
                            event.key, "expired_message"
                        )
                    else:
                        self.delivery_store.complete_inbound_event(event.key)
                except asyncio.CancelledError:
                    self.delivery_store.requeue_inbound_event(event.key, "worker_cancelled")
                    raise
                except Exception as exc:
                    retry_status = self.delivery_store.requeue_inbound_event(
                        event.key, type(exc).__name__
                    )
                    logger.error(
                        "入站事件处理失败 event={} error={} status={}",
                        stable_ref(event.key),
                        type(exc).__name__,
                        retry_status,
                    )
                    return False

    def _inbound_worker_done(self, task, chat_id):
        self.message_tasks.discard(task)
        if self.inbound_chat_tasks.get(chat_id) is task:
            self.inbound_chat_tasks.pop(chat_id, None)
        failed = task.cancelled()
        if not failed:
            try:
                failed = task.exception() is not None or task.result() is False
            except (asyncio.CancelledError, Exception):
                failed = True
        self._schedule_pending_inbound_events(
            excluded_chat=chat_id if failed else None
        )

    def _schedule_inbound_chat(self, chat_id):
        existing = self.inbound_chat_tasks.get(chat_id)
        if existing is not None and not existing.done():
            return True
        if len(self.message_tasks) >= self.max_message_tasks:
            return False
        task = asyncio.create_task(self._run_inbound_chat_worker(chat_id))
        self.inbound_chat_tasks[chat_id] = task
        self.message_tasks.add(task)
        task.add_done_callback(
            lambda done, key=chat_id: self._inbound_worker_done(done, key)
        )
        return True

    def _schedule_pending_inbound_events(self, excluded_chat=None):
        scheduled = set()
        for event in self.delivery_store.pending_inbound_events(ready_only=True):
            if event.chat_id == excluded_chat or event.chat_id in scheduled:
                continue
            if not self._schedule_inbound_chat(event.chat_id):
                break
            scheduled.add(event.chat_id)

    def _schedule_message_task(self, event):
        """Schedule a persisted inbound event; retained as a private compatibility shim."""
        chat_id = getattr(event, "chat_id", event)
        if not any(
            pending.chat_id == str(chat_id)
            for pending in self.delivery_store.pending_inbound_events(ready_only=True)
        ):
            return False
        return self._schedule_inbound_chat(str(chat_id))

    async def _inbound_recovery_loop(self):
        consecutive_failures = 0
        while True:
            try:
                self._schedule_pending_inbound_events()
                now = time.time()
                if (
                    now - self.last_delivery_retry_at
                    >= self.delivery_retry_interval
                ):
                    self.last_delivery_retry_at = now
                    self._schedule_delivery_task(self.retry_pending_deliveries())
                if (
                    now - self.last_manual_review_alert
                    >= self.manual_review_alert_interval
                ):
                    open_reviews = self.delivery_store.manual_review_count()
                    if open_reviews:
                        logger.warning(
                            "待人工处理的付款审核仍未清空 count={}", open_reviews
                        )
                    dead_letters = self.delivery_store.dead_letter_inbound_count()
                    if dead_letters:
                        logger.error(
                            "待人工重放或关闭的入站死信仍未清空 count={}", dead_letters
                        )
                    self.last_manual_review_alert = now
            except asyncio.CancelledError:
                raise
            except (sqlite3.Error, OSError, RuntimeError) as exc:
                consecutive_failures += 1
                base_delay = min(max(float(self.inbound_retry_interval), 1.0), 30.0)
                retry_delay = min(
                    base_delay * (2 ** min(consecutive_failures - 1, 4)),
                    30.0,
                )
                logger.warning(
                    "入站恢复轮询临时失败，将继续重试 error={} delay={:.1f}",
                    type(exc).__name__,
                    retry_delay,
                )
                await asyncio.sleep(retry_delay)
                continue

            consecutive_failures = 0
            await asyncio.sleep(self.inbound_retry_interval)

    def _schedule_delivery_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.delivery_tasks.add(task)
        task.add_done_callback(
            lambda done: self._task_done(done, self.delivery_tasks, "delivery")
        )
        return task

    async def _send_inbound_ack(self, websocket, message_data):
        headers = message_data.get("headers") if isinstance(message_data, dict) else None
        if not isinstance(headers, dict) or "mid" not in headers:
            return
        ack = {
            "code": 200,
            "headers": {
                "mid": headers["mid"],
                "sid": headers.get("sid", ""),
            },
        }
        for key in ("app-key", "ua", "dt"):
            if key in headers:
                ack["headers"][key] = headers[key]
        await websocket.send(json.dumps(ack))

    async def _persist_and_ack_inbound(self, websocket, message_data):
        """Durably record every decoded event before acknowledging the packet."""
        inbound_events = self._persist_sync_package(message_data)
        await self._send_inbound_ack(websocket, message_data)
        for event in inbound_events:
            if event.status == "pending":
                self._schedule_inbound_chat(event.chat_id)
        return inbound_events

    async def main(self):
        while True:
            fatal_auth_error = None
            try:
                # Token 与最新内存 Cookie 必须先准备好，再发起 WebSocket 握手。
                self.connection_restart_flag = False
                await self._ensure_startup_token()
                if hasattr(self.xianyu, "cookie_header_snapshot"):
                    cookie_snapshot = self.xianyu.cookie_header_snapshot()
                    if cookie_snapshot:
                        self.cookies_str = cookie_snapshot

                headers = websocket_headers(self.cookies_str)
                self._set_auth_state(
                    phase="TOKEN_VALID",
                    session="VALID",
                    mtop_token="VALID",
                    websocket="CONNECTING",
                    failure_code="ok",
                    failure_class="NONE",
                    needs_human=False,
                )

                async with websockets.connect(self.base_url, extra_headers=headers) as websocket:
                    self.ws = websocket
                    await self.init(websocket)
                    if self.manual_outbox_task is None or self.manual_outbox_task.done():
                        self.manual_outbox_task = asyncio.create_task(
                            self._manual_outbox_loop()
                        )

                    # 初始化心跳时间
                    self.last_heartbeat_time = time.time()
                    self.last_heartbeat_response = time.time()

                    # 启动心跳任务
                    self.heartbeat_task = asyncio.create_task(self.heartbeat_loop(websocket))

                    # 启动token刷新任务
                    self.token_refresh_task = asyncio.create_task(self.token_refresh_loop())
                    self.inbound_recovery_task = asyncio.create_task(
                        self._inbound_recovery_loop()
                    )
                    self.last_delivery_retry_at = time.time()
                    self._schedule_delivery_task(self.retry_pending_deliveries())

                    async for message in websocket:
                        try:
                            # 检查是否需要重启连接
                            if self.connection_restart_flag:
                                logger.info("检测到连接重启标志，准备重新建立连接...")
                                break

                            message_data = json.loads(message)

                            # 处理心跳响应
                            if await self.handle_heartbeat_response(message_data):
                                continue

                            if self.is_sync_package(message_data):
                                try:
                                    await self._persist_and_ack_inbound(
                                        websocket, message_data
                                    )
                                except Exception as exc:
                                    logger.error(
                                        "同步包持久化失败，关闭连接等待平台重放 error={}",
                                        type(exc).__name__,
                                    )
                                    await websocket.close()
                                    break
                                continue
                            await self._send_inbound_ack(websocket, message_data)

                        except json.JSONDecodeError:
                            logger.error("消息解析失败")
                        except Exception as e:
                            logger.error("读取消息失败: {}", type(e).__name__)

            except AuthenticationUnavailableError as exc:
                logger.error("认证不可用，Worker停止 code={}", exc.code)
                fatal_auth_error = exc

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket连接已关闭")
                if not self.connection_restart_flag and not self.authentication_failure_code:
                    self._set_auth_state(
                        phase="DEGRADED",
                        websocket="DEGRADED",
                        failure_code="network_error",
                        failure_class="TRANSIENT",
                        failure_count=max(1, self.token_consecutive_failures),
                        needs_human=False,
                    )

            except Exception as e:
                logger.error("连接发生错误: {}", type(e).__name__)
                if not self.authentication_failure_code:
                    self._set_auth_state(
                        phase="DEGRADED",
                        websocket="DEGRADED",
                        failure_code="network_error",
                        failure_class="TRANSIENT",
                        failure_count=max(1, self.token_consecutive_failures),
                        needs_human=False,
                    )

            finally:
                self.connection_ready.clear()
                self.heartbeat_mids.clear()
                for future in tuple(self.pending_send_acks.values()):
                    if not future.done():
                        future.set_exception(ConnectionError("websocket disconnected"))
                if self.ws is not None:
                    self.ws = None
                # 清理任务
                if self.heartbeat_task:
                    self.heartbeat_task.cancel()
                    try:
                        await self.heartbeat_task
                    except asyncio.CancelledError:
                        pass
                    self.heartbeat_task = None

                if self.token_refresh_task:
                    self.token_refresh_task.cancel()
                    try:
                        await self.token_refresh_task
                    except asyncio.CancelledError:
                        pass
                    self.token_refresh_task = None
                if self.inbound_recovery_task:
                    self.inbound_recovery_task.cancel()
                    try:
                        await self.inbound_recovery_task
                    except asyncio.CancelledError:
                        pass
                    self.inbound_recovery_task = None

                auth_error_code = (
                    fatal_auth_error.code
                    if fatal_auth_error is not None
                    else self.authentication_failure_code
                )
                if auth_error_code:
                    if self.manual_outbox_task:
                        self.manual_outbox_task.cancel()
                        try:
                            await self.manual_outbox_task
                        except asyncio.CancelledError:
                            pass
                        self.manual_outbox_task = None
                    raise AuthenticationUnavailableError(auth_error_code)

                # 如果是主动重启，Token 仍有效，立即进行下一次受控握手。
                if self.connection_restart_flag:
                    self._set_auth_state(
                        phase="TOKEN_VALID",
                        session="VALID",
                        mtop_token="VALID",
                        websocket="DISCONNECTED",
                        failure_code="ok",
                        failure_class="NONE",
                        needs_human=False,
                    )
                    logger.info("主动重启连接，立即重连...")
                else:
                    logger.info("等待5秒后重连...")
                    await asyncio.sleep(5)



def validate_ai_runtime_env():
    if os.getenv("API_KEY", "").strip() in API_KEY_PLACEHOLDERS:
        raise RuntimeError("缺少必需配置: API_KEY")


def validate_runtime_env(automation_mode=None):
    """Fail closed in unattended deployments; never prompt for or persist secrets."""
    mode = normalize_automation_mode(automation_mode)
    state_dir = os.path.abspath(
        os.getenv("XIAN_YU_DATA_DIR", os.path.join(BASE_DIR, "data"))
    )
    persisted_auth = AuthStateStore(
        os.path.join(state_dir, "auth_status.json")
    ).read()
    if persisted_auth["needs_human"]:
        return mode
    raw_cookies = os.getenv("COOKIES_STR", "").strip()
    if raw_cookies in {"", "your_cookies_here"}:
        try:
            raw_cookies = PrivateAuthStorage(state_dir).load_long_cookie_header("")
        except (OSError, RuntimeError):
            raw_cookies = ""
    if not trans_cookies(raw_cookies).get("unb"):
        raise RuntimeError("缺少必需配置: COOKIES_STR")
    # AI configuration is validated when the optional internal client is
    # created. A broken AI credential must not disable fixed reply rules.
    return mode


if __name__ == '__main__':
    # 加载环境变量
    local_env = os.path.join(BASE_DIR, ".env")
    if os.path.exists(local_env):
        load_dotenv(local_env)
        logger.info("已加载 .env 配置")

    # 配置日志级别；生产默认不输出调试细节或异常变量诊断。
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.remove()  # 移除默认handler
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        backtrace=False,
        diagnose=False,
    )
    logger.info(f"日志级别设置为: {log_level}")

    automation_mode = validate_runtime_env()

    cookies_str = os.getenv("COOKIES_STR")
    xianyuLive = XianyuLive(cookies_str, automation_mode=automation_mode)
    # 常驻进程
    asyncio.run(xianyuLive.main())
