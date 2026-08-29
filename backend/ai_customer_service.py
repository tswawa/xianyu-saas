"""Account-scoped AI customer-service configuration and OpenAI-compatible access.

Long-lived upstream credentials stay in the control plane.  Public callers and
workers only receive bounded metadata or account-private, non-secret published
configuration files.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import inspect
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import stat
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from account_storage import AccountStorage, AccountStorageError, DEFAULT_ACCOUNT_ID, normalize_account_key
from ai_provider_adapters import (
    ProviderAdapterError,
    build_request,
    is_api_key_required,
    normalize_base_url as normalize_provider_base_url,
    normalize_provider,
    parse_response,
    provider_spec,
)
from ai_reply_engine import (
    ReplyEngineError,
    compile_effective_context,
    generate_reply_decision,
    has_substantive_text,
)


CONNECTION_FILE = "ai_connection.json"
CONNECTION_SECRET_FILE = "ai_connection_secret.json"
SETTINGS_FILE = "ai_settings.json"
TEMPLATES_FILE = "ai_templates.json"
KNOWLEDGE_DIR = "ai_knowledge"
SNAPSHOT_FILE = "shop_snapshot.json"
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_HISTORY = 20
MAX_TEMPLATES = 50
VERIFICATION_TTL_SECONDS = 300
MAX_MODEL_MESSAGE_CHARS = 14_500
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_ITEM_ID_RE = re.compile(r"[0-9]{1,64}\Z")
_SAFE_CODE_RE = re.compile(r"[a-z0-9_]{1,80}\Z")


ERROR_MESSAGES = {
    "credential_store_unavailable": "AI 凭据存储暂时不可用",
    "connection_unconfigured": "当前店铺尚未配置 AI 连接",
    "connection_unverified": "当前店铺 AI 连接尚未通过测试",
    "credential_unavailable": "当前店铺 AI 凭据不可用",
    "verification_invalid": "连接测试凭证无效或已过期",
    "revision_conflict": "配置已更新，请刷新后重试",
    "address_unsafe": "连接地址不符合安全要求",
    "authentication_failed": "模型服务鉴权失败",
    "model_not_found": "模型不存在或不可用",
    "rate_limited": "模型服务请求过于频繁",
    "timeout": "模型服务响应超时",
    "response_invalid": "模型服务响应格式无效",
    "service_unavailable": "模型服务暂时不可用",
    "item_not_found": "当前店铺中不存在该商品",
    "knowledge_stale": "商品知识需要重新复核",
    "ai_unconfigured": "当前店铺尚未保存可用的 AI 客服配置",
    "ai_disabled": "当前店铺 AI 已停用",
    "invalid_payload": "AI 配置内容无效",
}


class AIServiceError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400, message: str | None = None):
        safe_code = code if _SAFE_CODE_RE.fullmatch(str(code or "")) else "service_unavailable"
        self.code = safe_code
        self.status_code = int(status_code)
        super().__init__(message or ERROR_MESSAGES.get(safe_code, "AI 服务暂时不可用"))


def _now_iso(now: float | None = None) -> str:
    stamp = time.time() if now is None else float(now)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _bounded_text(value: Any, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise AIServiceError("invalid_payload", 400)
    text = value.strip()
    if required and not text:
        raise AIServiceError("invalid_payload", 400)
    if len(text) > limit or _CONTROL_RE.search(text):
        raise AIServiceError("invalid_payload", 400)
    return text


def _bounded_list(value: Any, *, maximum: int, item_limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise AIServiceError("invalid_payload", 400)
    result: list[str] = []
    for item in value:
        text = _bounded_text(item, item_limit)
        if text and text not in result:
            result.append(text)
    return result


def _private_path_read(path: Path, *, maximum_bytes: int = 1024 * 1024) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum_bytes:
            raise AIServiceError("credential_unavailable", 503)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            return json.load(stream)
    except FileNotFoundError:
        raise
    except AIServiceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AIServiceError("credential_unavailable", 503) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _safe_item_id(value: Any) -> str:
    if isinstance(value, bool):
        raise AIServiceError("invalid_payload", 400)
    text = str(value or "").strip()
    if not _ITEM_ID_RE.fullmatch(text):
        raise AIServiceError("invalid_payload", 400)
    return text


def _safe_number(value: Any) -> int | float | str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return value
    return str(value).strip()[:80]


def product_facts(product: dict) -> dict:
    """Normalize only product fields used for stale checks and model facts."""
    if not isinstance(product, dict):
        raise AIServiceError("item_not_found", 404)
    item_id = _safe_item_id(product.get("id") or product.get("item_id") or product.get("itemId"))
    raw_skus = product.get("sku") or product.get("skus") or product.get("skuList") or []
    clean_skus: list[dict] = []
    if isinstance(raw_skus, list):
        for raw in raw_skus[:50]:
            if not isinstance(raw, dict):
                continue
            properties = raw.get("propertyList") or raw.get("properties") or []
            labels: list[str] = []
            if isinstance(properties, list):
                for prop in properties[:10]:
                    if isinstance(prop, dict):
                        label = prop.get("valueText") or prop.get("value") or prop.get("name")
                        if isinstance(label, str) and label.strip():
                            labels.append(label.strip()[:80])
            name = raw.get("name") or raw.get("title") or " ".join(labels)
            clean_skus.append(
                {
                    "name": str(name or "").strip()[:160],
                    "price": _safe_number(raw.get("price")),
                    "stock": _safe_number(raw.get("stock") if "stock" in raw else raw.get("quantity")),
                }
            )
    facts = {
        "item_id": item_id,
        "title": str(product.get("title") or product.get("name") or "").strip()[:300],
        "description": str(product.get("description") or product.get("desc") or "").strip()[:4000],
        "price": _safe_number(product.get("price") if "price" in product else product.get("soldPrice")),
        "stock": _safe_number(product.get("stock") if "stock" in product else product.get("quantity")),
        "status": str(product.get("status") or product.get("itemStatus") or "").strip()[:80],
        "skus": clean_skus,
    }
    return facts


def identity_fingerprint(product: dict) -> str:
    """Fingerprint semantic product identity, excluding live price/stock/status."""
    facts = product_facts(product)
    identity = {
        "item_id": facts["item_id"],
        "title": facts.get("title", ""),
        "description": facts.get("description", ""),
        "skus": [str(item.get("name") or "") for item in facts.get("skus", []) if isinstance(item, dict)],
    }
    return "sha256:" + hashlib.sha256(_json_bytes(identity)).hexdigest()


def facts_fingerprint(product: dict) -> str:
    """Fingerprint all reply-time facts, including volatile values."""
    return "sha256:" + hashlib.sha256(_json_bytes(product_facts(product))).hexdigest()


def snapshot_fingerprint(product: dict) -> str:
    """Compatibility alias for the former all-facts fingerprint."""
    return facts_fingerprint(product)


def _connection_defaults() -> dict:
    return {
        "version": 2,
        "provider": "openai_chat_completions",
        "base_url": "",
        "model": "",
        "api_key_configured": False,
        "connection_status": "unconfigured",
        "revision": 0,
        "key_revision": 0,
        "last_tested_at": "",
        "last_error_code": "",
        "updated_at": "",
    }


def ensure_development_master_key(path: Path, tenants_root: Path) -> str:
    """Load or create one private, persistent master key for local development."""
    key_path = Path(path)
    tenant_path = Path(tenants_root)

    def read_existing() -> str:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(key_path, flags)
        except OSError as exc:
            raise AIServiceError("credential_store_unavailable", 503) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise AIServiceError("credential_store_unavailable", 503)
            raw = os.read(descriptor, 257)
            if len(raw) > 256:
                raise AIServiceError("credential_store_unavailable", 503)
        finally:
            os.close(descriptor)
        try:
            value = raw.decode("ascii").strip()
            decoded = base64.b64decode(value, validate=True)
        except (UnicodeError, ValueError) as exc:
            raise AIServiceError("credential_store_unavailable", 503) from exc
        if len(decoded) != 32:
            raise AIServiceError("credential_store_unavailable", 503)
        return value

    if key_path.exists() or key_path.is_symlink():
        return read_existing()
    try:
        encrypted_connections_exist = tenant_path.exists() and any(
            candidate.is_file() for candidate in tenant_path.rglob(CONNECTION_SECRET_FILE)
        )
    except OSError as exc:
        raise AIServiceError("credential_store_unavailable", 503) from exc
    if encrypted_connections_exist:
        raise AIServiceError(
            "credential_store_unavailable",
            503,
            "检测到已有加密 AI 凭据，请恢复原有开发主密钥",
        )
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if key_path.parent.is_symlink():
            raise OSError("unsafe key directory")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(key_path, flags, 0o600)
        try:
            value = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(value.encode("ascii") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
    except FileExistsError:
        pass
    except OSError as exc:
        raise AIServiceError("credential_store_unavailable", 503) from exc
    return read_existing()


def empty_store_config() -> dict:
    """Safe display defaults; they are disabled and do not count as store content."""
    return {
        "version": 2,
        "enabled": False,
        "store_content": "",
        # ``common_knowledge`` remains a read/write alias for old callers.
        "common_knowledge": "",
        "persona_preset": "catgirl",
        "persona_name": "小喵客服",
        "persona_instruction": "亲切、克制地回答，可少量使用“喵”；必须先准确回答当前问题。",
        "tone": "friendly",
        "buyer_address": "亲",
        "reply_length": "short",
        "emoji_level": "low",
        "forbidden_claims": "不得编造价格、库存、规格或商品状态\n不得声称已付款、已到账、已发货或已退款\n不得引导站外联系或交易",
        "handoff_rules": "退款、争议或投诉\n付款、订单、发货状态无法核实时\n商品事实不足或冲突时",
        # Deprecated compatibility field. Runtime generation never sends it.
        "fallback_reply": "",
    }


def catgirl_preset() -> dict:
    """Return the display preset; it is never persisted or enabled automatically."""
    return empty_store_config()


def _natural_lines(value: Any, *, maximum: int = 50, item_limit: int = 300) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _bounded_text(value, maximum * item_limit)
    return "\n".join(_bounded_list(value, maximum=maximum, item_limit=item_limit))


def normalize_store_config(value: Any) -> dict:
    if not isinstance(value, dict):
        raise AIServiceError("invalid_payload", 400)
    defaults = empty_store_config()
    allowed = set(defaults) | {"content"}
    if set(value) - allowed:
        raise AIServiceError("invalid_payload", 400)
    enabled = value.get("enabled", defaults["enabled"])
    if not isinstance(enabled, bool):
        raise AIServiceError("invalid_payload", 400)
    preset = _bounded_text(value.get("persona_preset", defaults["persona_preset"]), 32).lower()
    if preset not in {"none", "professional", "friendly", "catgirl", "custom"}:
        raise AIServiceError("invalid_payload", 400)
    tone = _bounded_text(value.get("tone", defaults["tone"]), 32).lower()
    if tone not in {"natural", "professional", "friendly", "lively", "restrained"}:
        raise AIServiceError("invalid_payload", 400)
    reply_length = _bounded_text(value.get("reply_length", defaults["reply_length"]), 32).lower()
    if reply_length not in {"short", "standard", "detailed"}:
        raise AIServiceError("invalid_payload", 400)
    emoji_level = _bounded_text(value.get("emoji_level", defaults["emoji_level"]), 32).lower()
    if emoji_level not in {"none", "low", "medium"}:
        raise AIServiceError("invalid_payload", 400)
    raw_content = value.get("store_content")
    if raw_content is None:
        raw_content = value.get("content")
    if raw_content is None:
        raw_content = value.get("common_knowledge", "")
    content = _bounded_text(raw_content, 12_000)
    return {
        "version": 2,
        "enabled": enabled,
        "store_content": content,
        "common_knowledge": content,
        "persona_preset": preset,
        "persona_name": _bounded_text(value.get("persona_name", defaults["persona_name"]), 80),
        "persona_instruction": _bounded_text(value.get("persona_instruction", defaults["persona_instruction"]), 1200),
        "tone": tone,
        "buyer_address": _bounded_text(value.get("buyer_address", defaults["buyer_address"]), 40),
        "reply_length": reply_length,
        "emoji_level": emoji_level,
        "forbidden_claims": _natural_lines(value.get("forbidden_claims", defaults["forbidden_claims"])),
        "handoff_rules": _natural_lines(value.get("handoff_rules", defaults["handoff_rules"])),
        "fallback_reply": "",
    }


def store_config_has_content(value: Any) -> bool:
    try:
        clean = normalize_store_config(value)
    except AIServiceError:
        return False
    return has_substantive_text(clean.get("store_content"))


def empty_knowledge() -> dict:
    return {"version": 2, "content": "", "imported_from_v1": False}


def _legacy_knowledge_content(value: dict) -> str:
    sections: list[str] = []
    scalar_fields = (
        ("商品说明", "summary", 2000),
        ("价格说明", "price_policy", 2000),
        ("交付说明", "delivery_notes", 2000),
        ("使用说明", "usage_notes", 4000),
        ("售后说明", "after_sales", 2000),
        ("补充说明", "custom_notes", 4000),
    )
    for label, key, limit in scalar_fields:
        text = _bounded_text(value.get(key, ""), limit)
        if has_substantive_text(text):
            sections.append(f"{label}：\n{text}")
    list_fields = (
        ("商品特点", "selling_points"),
        ("规格与适用条件", "specifications"),
        ("禁止回答", "forbidden_answers"),
        ("转人工条件", "handoff_rules"),
    )
    for label, key in list_fields:
        items = _bounded_list(value.get(key, []), maximum=100, item_limit=300)
        items = [item for item in items if has_substantive_text(item)]
        if items:
            sections.append(label + "：\n" + "\n".join(f"- {item}" for item in items))
    faqs = value.get("faqs", [])
    if not isinstance(faqs, list) or len(faqs) > 50:
        raise AIServiceError("invalid_payload", 400)
    faq_lines: list[str] = []
    for faq in faqs:
        if not isinstance(faq, dict) or set(faq) - {"question", "answer", "keywords"}:
            raise AIServiceError("invalid_payload", 400)
        question = _bounded_text(faq.get("question", ""), 300)
        answer = _bounded_text(faq.get("answer", ""), 1200)
        if has_substantive_text(question) and has_substantive_text(answer):
            faq_lines.append(f"问：{question}\n答：{answer}")
    if faq_lines:
        sections.append("常见问答：\n" + "\n\n".join(faq_lines))
    return "\n\n".join(sections)


def normalize_knowledge(value: Any) -> dict:
    if isinstance(value, str):
        return {"version": 2, "content": _bounded_text(value, 16_000), "imported_from_v1": False}
    if not isinstance(value, dict):
        raise AIServiceError("invalid_payload", 400)
    v2_allowed = {"version", "content", "imported_from_v1"}
    if "content" in value or set(value).issubset(v2_allowed):
        if set(value) - v2_allowed:
            raise AIServiceError("invalid_payload", 400)
        imported = value.get("imported_from_v1", False)
        if not isinstance(imported, bool):
            raise AIServiceError("invalid_payload", 400)
        return {
            "version": 2,
            "content": _bounded_text(value.get("content", ""), 16_000),
            "imported_from_v1": imported,
        }
    legacy_allowed = {
        "summary", "selling_points", "specifications", "price_policy", "delivery_notes",
        "usage_notes", "after_sales", "faqs", "forbidden_answers", "handoff_rules", "custom_notes",
    }
    if set(value) - legacy_allowed:
        raise AIServiceError("invalid_payload", 400)
    return {
        "version": 2,
        "content": _legacy_knowledge_content(value),
        "imported_from_v1": True,
    }


def knowledge_has_content(value: Any) -> bool:
    try:
        clean = normalize_knowledge(value)
    except AIServiceError:
        return False
    return has_substantive_text(clean.get("content"))


def _prompt_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return _CONTROL_RE.sub(" ", text)[:limit]


def _prompt_list(value: Any, *, maximum: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (_prompt_text(item, item_limit) for item in value[:maximum]) if text]


def _prompt_product_facts(product: dict) -> dict:
    facts = product_facts(product)
    skus = []
    for raw in facts.get("skus", [])[:4]:
        if not isinstance(raw, dict):
            continue
        skus.append({
            "name": _prompt_text(raw.get("name"), 80),
            "price": _safe_number(raw.get("price")),
            "stock": _safe_number(raw.get("stock")),
        })
    return {
        "item_id": facts["item_id"],
        "title": _prompt_text(facts.get("title"), 180),
        "description": _prompt_text(facts.get("description"), 600),
        "price": _safe_number(facts.get("price")),
        "stock": _safe_number(facts.get("stock")),
        "status": _prompt_text(facts.get("status"), 60),
        "skus": skus,
    }


def _prompt_store_config(value: dict) -> dict:
    clean = normalize_store_config(value)
    return {
        "enabled": clean["enabled"],
        "store_content": _prompt_text(clean["store_content"], 8_000),
        "persona_preset": clean["persona_preset"],
        "persona_name": _prompt_text(clean["persona_name"], 60),
        "persona_instruction": _prompt_text(clean["persona_instruction"], 500),
        "tone": clean["tone"],
        "buyer_address": _prompt_text(clean["buyer_address"], 30),
        "reply_length": clean["reply_length"],
        "emoji_level": clean["emoji_level"],
        "forbidden_claims": _prompt_text(clean["forbidden_claims"], 2_000),
        "handoff_rules": _prompt_text(clean["handoff_rules"], 2_000),
    }


def _prompt_knowledge(value: dict) -> dict:
    clean = normalize_knowledge(value)
    return {"content": _prompt_text(clean["content"], 10_000)}


def _model_json_object(text: str) -> dict:
    clean = _bounded_text(text, 32_000, required=True).lstrip("\ufeff")
    candidates = [clean]
    candidates.extend(match.group(1).strip() for match in _FENCED_JSON_RE.finditer(clean))
    decoder = json.JSONDecoder()
    candidates.extend(clean[index:] for index, char in enumerate(clean) if char == "{")
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            value, _end = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise AIServiceError("response_invalid", 502, "AI 返回内容无法解析为商品知识，请重新生成")


def _generated_knowledge(text: str) -> dict:
    clean = _bounded_text(text, 16_000, required=True)
    if "```" in clean or clean.lstrip().startswith(("{", "[")):
        raise AIServiceError("response_invalid", 502, "AI 返回的商品内容不是可用的自然语言，请重新生成")
    if not has_substantive_text(clean):
        raise AIServiceError("response_invalid", 502, "AI 返回内容为空，请重新生成")
    return {"version": 2, "content": clean, "imported_from_v1": False}


class AIService:
    def __init__(
        self,
        tenants_root: str | os.PathLike[str] | None = None,
        *,
        environ: dict[str, str] | None = None,
        clock: Callable[[], float] | None = None,
        resolver: Callable[..., Any] | None = None,
        requester: Callable[..., dict] | None = None,
    ):
        self.storage = AccountStorage(tenants_root)
        self.environ = os.environ if environ is None else environ
        self.clock = time.time if clock is None else clock
        self.resolver = socket.getaddrinfo if resolver is None else resolver
        self.requester = requester

    @staticmethod
    def _scope(user_id: int, shop_account_id: int, account_key: str) -> tuple[int, int, str]:
        try:
            uid = int(user_id)
            sid = int(shop_account_id)
            key = normalize_account_key(account_key)
        except (TypeError, ValueError, AccountStorageError) as exc:
            raise AIServiceError("invalid_payload", 400) from exc
        if uid <= 0 or sid <= 0:
            raise AIServiceError("invalid_payload", 400)
        return uid, sid, key

    def _account_dir(self, scope: tuple[int, int, str]) -> Path:
        return self.storage.ensure_account_dir(scope[0], scope[2])

    def knowledge_dir(self, user_id: int, shop_account_id: int, account_key: str) -> Path:
        scope = self._scope(user_id, shop_account_id, account_key)
        root = self._account_dir(scope)
        target = root / KNOWLEDGE_DIR
        try:
            os.makedirs(target, mode=0o700, exist_ok=True)
            info = os.lstat(target)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise OSError("unsafe knowledge directory")
            os.chmod(target, 0o700)
        except OSError as exc:
            raise AIServiceError("credential_unavailable", 503) from exc
        return target

    def runtime_paths(self, user_id: int, shop_account_id: int, account_key: str) -> dict[str, str]:
        scope = self._scope(user_id, shop_account_id, account_key)
        root = self._account_dir(scope)
        return {
            "settings_file": str(root / SETTINGS_FILE),
            "templates_file": str(root / TEMPLATES_FILE),
            "knowledge_dir": str(self.knowledge_dir(*scope)),
            "products_snapshot_file": str(root / SNAPSHOT_FILE),
        }

    def _master_keys(self) -> tuple[bytes, bytes]:
        raw = str(self.environ.get("SAAS_AI_MASTER_KEY", "") or "").strip()
        try:
            master = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError) as exc:
            raise AIServiceError("credential_store_unavailable", 503) from exc
        if len(master) != 32:
            raise AIServiceError("credential_store_unavailable", 503)
        encryption = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=b"xianyu-saas-ai-v1",
            info=b"connection-encryption",
        ).derive(master)
        signing = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=b"xianyu-saas-ai-v1",
            info=b"verification-signing",
        ).derive(master)
        return encryption, signing

    def _read_root_json(self, scope: tuple[int, int, str], name: str, default: Any) -> Any:
        path = self._account_dir(scope) / name
        try:
            payload = _private_path_read(path)
        except FileNotFoundError:
            return default
        return payload

    def _write_path_json(self, path: Path, payload: Any) -> None:
        try:
            self.storage.atomic_write_path(path, _json_bytes(payload))
        except (OSError, AccountStorageError) as exc:
            raise AIServiceError("credential_unavailable", 503) from exc

    def _write_root_json(self, scope: tuple[int, int, str], name: str, payload: Any) -> None:
        self._write_path_json(self._account_dir(scope) / name, payload)

    def get_connection(self, user_id: int, shop_account_id: int, account_key: str) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        payload = self._read_root_json(scope, CONNECTION_FILE, _connection_defaults())
        if not isinstance(payload, dict):
            return {**_connection_defaults(), "connection_status": "credential_unavailable", "last_error_code": "credential_unavailable"}
        result = _connection_defaults()
        for key in result:
            if key in payload:
                result[key] = payload[key]
        try:
            result["provider"] = normalize_provider(result.get("provider"))
        except ProviderAdapterError:
            result.update(provider="openai_chat_completions", connection_status="credential_unavailable", last_error_code="credential_unavailable")
        result["base_url"] = str(result["base_url"] or "")[:2048]
        result["model"] = str(result["model"] or "")[:200]
        result["api_key_configured"] = result["api_key_configured"] is True
        try:
            result["revision"] = max(0, int(result["revision"]))
            result["key_revision"] = max(0, int(result["key_revision"]))
        except (TypeError, ValueError):
            result.update(connection_status="credential_unavailable", last_error_code="credential_unavailable")
        return result

    @staticmethod
    def _aad(scope: tuple[int, int, str], connection_revision: int) -> bytes:
        return f"v1:{scope[0]}:{scope[1]}:{scope[2]}:{int(connection_revision)}".encode("utf-8")

    def _encrypt_key(self, scope: tuple[int, int, str], api_key: str, connection_revision: int, key_revision: int) -> dict:
        encryption_key, _ = self._master_keys()
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(encryption_key).encrypt(
            nonce, api_key.encode("utf-8"), self._aad(scope, connection_revision)
        )
        return {
            "schema": 1,
            "key_revision": int(key_revision),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }

    def _decrypt_key(self, scope: tuple[int, int, str], metadata: dict) -> str:
        try:
            encryption_key, _ = self._master_keys()
            secret = self._read_root_json(scope, CONNECTION_SECRET_FILE, None)
            if not isinstance(secret, dict) or secret.get("schema") != 1:
                raise ValueError("secret schema")
            if int(secret.get("key_revision")) != int(metadata.get("key_revision")):
                raise ValueError("key revision")
            nonce = base64.b64decode(str(secret.get("nonce") or ""), validate=True)
            ciphertext = base64.b64decode(str(secret.get("ciphertext") or ""), validate=True)
            plaintext = AESGCM(encryption_key).decrypt(
                nonce, ciphertext, self._aad(scope, int(metadata.get("revision") or 0))
            )
            key = plaintext.decode("utf-8")
            return _bounded_text(key, 4096, required=True)
        except AIServiceError:
            raise
        except Exception as exc:
            raise AIServiceError("credential_unavailable", 503) from exc

    def _allow_local_provider(self, provider: str) -> bool:
        return (
            provider == "ollama_chat"
            and str(self.environ.get("SAAS_AI_ALLOW_OLLAMA_LOCAL", "") or "").strip() == "1"
        )

    def normalize_base_url(self, value: Any, provider: Any = "openai_chat_completions") -> str:
        """Normalize URL syntax without performing an early DNS lookup.

        The real request path resolves, validates and connects to one identical
        numeric address set. Resolving here as well would create a DNS
        rebinding window and make connection tests depend on two answers.
        """
        try:
            clean_provider = normalize_provider(provider)
            allow_http = str(self.environ.get("SAAS_AI_ALLOW_HTTP_LOCAL", "") or "").strip() == "1"
            allow_http = allow_http or self._allow_local_provider(clean_provider)
            return normalize_provider_base_url(value, clean_provider, allow_http=allow_http)
        except ProviderAdapterError as exc:
            raise AIServiceError(exc.code, 400, str(exc)) from exc

    @staticmethod
    def _address_allowed(address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_loopback: bool) -> bool:
        if address.is_loopback:
            return allow_loopback
        return not (
            address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
            or not address.is_global
        )

    def _resolved_target(self, url: str, *, allow_loopback: bool = False):
        try:
            parsed = urllib.parse.urlsplit(url)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise AIServiceError("address_unsafe", 400) from exc
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            raise AIServiceError("address_unsafe", 400)
        try:
            addresses = self.resolver(host, port, type=socket.SOCK_STREAM)
        except (OSError, socket.gaierror) as exc:
            raise AIServiceError("service_unavailable", 503) from exc
        if not addresses:
            raise AIServiceError("service_unavailable", 503)
        resolved = []
        for entry in addresses:
            try:
                address = ipaddress.ip_address(entry[4][0])
                family = entry[0] if entry[0] in {socket.AF_INET, socket.AF_INET6} else (
                    socket.AF_INET6 if address.version == 6 else socket.AF_INET
                )
                socktype = entry[1] if entry[1] in {socket.SOCK_STREAM} else socket.SOCK_STREAM
                protocol = entry[2] if isinstance(entry[2], int) else 0
                sockaddr = entry[4]
                if family == socket.AF_INET6 and len(sockaddr) < 4:
                    sockaddr = (str(address), port, 0, 0)
                elif family == socket.AF_INET:
                    sockaddr = (str(address), port)
            except (ValueError, IndexError, TypeError) as exc:
                raise AIServiceError("address_unsafe", 400) from exc
            if not self._address_allowed(address, allow_loopback=allow_loopback):
                raise AIServiceError("address_unsafe", 400)
            candidate = (family, socktype, protocol, sockaddr)
            if candidate not in resolved:
                resolved.append(candidate)
        if not resolved:
            raise AIServiceError("service_unavailable", 503)
        return parsed, resolved

    def _validate_target(self, base_url: str, *, allow_loopback: bool = False) -> None:
        self._resolved_target(base_url, allow_loopback=allow_loopback)

    def _request_pinned(
        self,
        url: str,
        body: bytes,
        headers: dict,
        *,
        allow_loopback: bool,
        timeout: float = 60,
    ) -> tuple[int, bytes]:
        """Resolve, validate and connect to the same numeric address set."""
        parsed, addresses = self._resolved_target(url, allow_loopback=allow_loopback)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        default_port = 443 if parsed.scheme == "https" else 80
        host_header = f"[{host}]" if ":" in host else host
        if port != default_port:
            host_header = f"{host_header}:{port}"
        request_headers = {**headers, "Host": host_header}
        last_error: Exception | None = None
        for family, socktype, protocol, sockaddr in addresses:
            raw_socket = None
            connection = None
            try:
                raw_socket = socket.socket(family, socktype, protocol)
                raw_socket.settimeout(timeout)
                raw_socket.connect(sockaddr)
                connected_socket = raw_socket
                if parsed.scheme == "https":
                    connected_socket = ssl.create_default_context().wrap_socket(
                        raw_socket, server_hostname=host
                    )
                    raw_socket = None
                # HTTPConnection is used only for HTTP framing. Its socket is
                # already connected to the validated numeric address, so it
                # cannot perform a second hostname resolution. Host and SNI
                # still use the original hostname, preserving virtual hosting
                # and default TLS certificate verification.
                connection = http.client.HTTPConnection(host, port, timeout=timeout)
                connection.sock = connected_socket
                connection.request("POST", target, body=body, headers=request_headers)
                response = connection.getresponse()
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(response_body) > MAX_RESPONSE_BYTES:
                    raise AIServiceError("response_invalid", 502)
                return int(response.status), response_body
            except AIServiceError:
                raise
            except (TimeoutError, socket.timeout) as exc:
                raise AIServiceError("timeout", 504) from exc
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
            finally:
                if connection is not None:
                    connection.close()
                elif raw_socket is not None:
                    raw_socket.close()
        raise AIServiceError("service_unavailable", 503) from last_error

    def _call_requester(self, url: str, api_key: str, payload: dict, headers: dict) -> dict:
        if self.requester is None:
            raise RuntimeError("requester is not configured")
        try:
            parameter_count = len(inspect.signature(self.requester).parameters)
        except (TypeError, ValueError):
            parameter_count = 3
        if parameter_count >= 4:
            return self.requester(url, api_key, payload, headers)
        return self.requester(url, api_key, payload)

    def _request_json(self, provider: str, base_url: str, model: str, api_key: str, payload: dict) -> dict:
        try:
            clean_provider = normalize_provider(provider)
            request_data = build_request(clean_provider, base_url, model, api_key, payload)
        except ProviderAdapterError as exc:
            raise AIServiceError(exc.code, 400, str(exc)) from exc
        allow_loopback = self._allow_local_provider(clean_provider)
        if self.requester is not None:
            self._validate_target(request_data["url"], allow_loopback=allow_loopback)
            try:
                result = self._call_requester(
                    request_data["url"], api_key, request_data["payload"], request_data["headers"]
                )
                return parse_response(clean_provider, result)
            except ProviderAdapterError as exc:
                raise AIServiceError(exc.code, 502, str(exc)) from exc
        encoded = _json_bytes(request_data["payload"])
        if len(encoded) > MAX_REQUEST_BYTES:
            raise AIServiceError("invalid_payload", 413)
        status_code, body = self._request_pinned(
            request_data["url"],
            encoded,
            {**request_data["headers"], "User-Agent": "xianyu-saas-ai-proxy/2"},
            allow_loopback=allow_loopback,
        )
        if status_code in {401, 403}:
            raise AIServiceError("authentication_failed", 401)
        if status_code == 404:
            raise AIServiceError("model_not_found", 400)
        if status_code == 429:
            raise AIServiceError("rate_limited", 429)
        if 300 <= status_code < 400:
            raise AIServiceError("address_unsafe", 400)
        if status_code < 200 or status_code >= 300:
            raise AIServiceError("service_unavailable", 503)
        try:
            result = json.loads(body)
            return parse_response(clean_provider, result)
        except ProviderAdapterError as exc:
            raise AIServiceError(exc.code, 502, str(exc)) from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AIServiceError("response_invalid", 502) from exc

    @staticmethod
    def _response_text(response: dict) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AIServiceError("response_invalid", 502)
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise AIServiceError("response_invalid", 502)
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") in {None, "text", "output_text"}:
                    text = block.get("text")
                    if isinstance(text, dict):
                        text = text.get("value")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            if parts:
                return "\n".join(parts)
        raise AIServiceError("response_invalid", 502)

    def _key_fingerprint(self, api_key: str) -> str:
        _, signing = self._master_keys()
        return hmac.new(signing, b"candidate-key\0" + api_key.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _config_fingerprint(provider: str, base_url: str, model: str, key_fingerprint: str, key_revision: int) -> str:
        return hashlib.sha256(_json_bytes({
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "key_fingerprint": key_fingerprint,
            "key_revision": int(key_revision),
        })).hexdigest()

    def _issue_verification(self, scope, provider, base_url, model, api_key, key_revision, expected_revision) -> str:
        _, signing = self._master_keys()
        payload = {
            "v": 2,
            "uid": scope[0],
            "sid": scope[1],
            "account": scope[2],
            "expected_revision": int(expected_revision),
            "key_revision": int(key_revision),
            "fingerprint": self._config_fingerprint(
                provider, base_url, model, self._key_fingerprint(api_key), key_revision
            ),
            "exp": int(self.clock()) + VERIFICATION_TTL_SECONDS,
        }
        raw = base64.urlsafe_b64encode(_json_bytes(payload)).rstrip(b"=")
        signature = hmac.new(signing, raw, hashlib.sha256).digest()
        return raw.decode("ascii") + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

    def _verify_token(self, token: str, scope, provider, base_url, model, api_key, key_revision, expected_revision) -> None:
        try:
            raw_text, signature_text = _bounded_text(token, 4096, required=True).split(".", 1)
            raw = raw_text.encode("ascii")
            padding = "=" * (-len(signature_text) % 4)
            signature = base64.urlsafe_b64decode(signature_text + padding)
            _, signing = self._master_keys()
            if not hmac.compare_digest(signature, hmac.new(signing, raw, hashlib.sha256).digest()):
                raise ValueError("signature")
            payload_padding = "=" * (-len(raw_text) % 4)
            payload = json.loads(base64.urlsafe_b64decode(raw_text + payload_padding))
            expected_fingerprint = self._config_fingerprint(
                provider, base_url, model, self._key_fingerprint(api_key), key_revision
            )
            valid = (
                isinstance(payload, dict)
                and payload.get("v") == 2
                and int(payload.get("uid")) == scope[0]
                and int(payload.get("sid")) == scope[1]
                and payload.get("account") == scope[2]
                and int(payload.get("expected_revision")) == int(expected_revision)
                and int(payload.get("key_revision")) == int(key_revision)
                and hmac.compare_digest(str(payload.get("fingerprint") or ""), expected_fingerprint)
                and int(payload.get("exp")) >= int(self.clock())
            )
            if not valid:
                raise ValueError("claims")
        except AIServiceError:
            raise
        except Exception as exc:
            raise AIServiceError("verification_invalid", 409) from exc

    def _candidate_key(self, scope, metadata: dict, provider: str, api_key: str) -> tuple[str, bool]:
        candidate_key = _bounded_text(api_key, 4096) if api_key else ""
        replacing = bool(candidate_key)
        same_provider = normalize_provider(metadata.get("provider")) == provider
        if not candidate_key and same_provider and metadata["api_key_configured"]:
            candidate_key = self._decrypt_key(scope, metadata)
        if not candidate_key and is_api_key_required(provider):
            raise AIServiceError("connection_unconfigured", 409, "切换接口格式后请重新填写对应的 API Key")
        return candidate_key, replacing

    def test_connection(
        self, user_id: int, shop_account_id: int, account_key: str, *,
        base_url: str, model: str, api_key: str = "", expected_revision: int,
        provider: str = "openai_chat_completions",
    ) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        metadata = self.get_connection(*scope)
        if int(expected_revision) != int(metadata["revision"]):
            raise AIServiceError("revision_conflict", 409)
        try:
            clean_provider = normalize_provider(provider)
        except ProviderAdapterError as exc:
            raise AIServiceError(exc.code, 400, str(exc)) from exc
        normalized_url = self.normalize_base_url(base_url, clean_provider)
        clean_model = _bounded_text(model, 200, required=True)
        candidate_key, replacing = self._candidate_key(scope, metadata, clean_provider, api_key)
        key_revision = int(metadata["key_revision"]) + (1 if replacing else 0)
        response = self._request_json(
            clean_provider,
            normalized_url,
            clean_model,
            candidate_key,
            {
                "messages": [
                    {"role": "system", "content": "Return only OK."},
                    {"role": "user", "content": "Connection test."},
                ],
                "max_tokens": 256 if clean_provider == "openai_responses" else 32,
                "temperature": 0,
            },
        )
        self._response_text(response)
        return {
            "ok": True,
            "status": "verified",
            "provider": clean_provider,
            "verification_token": self._issue_verification(
                scope, clean_provider, normalized_url, clean_model, candidate_key,
                key_revision, metadata["revision"],
            ),
            "expires_in": VERIFICATION_TTL_SECONDS,
        }

    def save_connection(
        self, user_id: int, shop_account_id: int, account_key: str, *, base_url: str,
        model: str, api_key: str = "", verification_token: str, expected_revision: int,
        provider: str = "openai_chat_completions",
    ) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        metadata = self.get_connection(*scope)
        if int(expected_revision) != int(metadata["revision"]):
            raise AIServiceError("revision_conflict", 409)
        try:
            clean_provider = normalize_provider(provider)
        except ProviderAdapterError as exc:
            raise AIServiceError(exc.code, 400, str(exc)) from exc
        normalized_url = self.normalize_base_url(base_url, clean_provider)
        clean_model = _bounded_text(model, 200, required=True)
        candidate_key, replacing = self._candidate_key(scope, metadata, clean_provider, api_key)
        key_revision = int(metadata["key_revision"]) + (1 if replacing else 0)
        self._verify_token(
            verification_token, scope, clean_provider, normalized_url, clean_model, candidate_key,
            key_revision, metadata["revision"],
        )
        revision = int(metadata["revision"]) + 1
        secret_path = self._account_dir(scope) / CONNECTION_SECRET_FILE
        if candidate_key:
            secret_payload = self._encrypt_key(scope, candidate_key, revision, key_revision)
            self._write_root_json(scope, CONNECTION_SECRET_FILE, secret_payload)
        else:
            try:
                secret_path.unlink(missing_ok=True)
            except OSError as exc:
                raise AIServiceError("credential_unavailable", 503) from exc
        saved = {
            "version": 2,
            "provider": clean_provider,
            "base_url": normalized_url,
            "model": clean_model,
            "api_key_configured": bool(candidate_key),
            "connection_status": "verified",
            "revision": revision,
            "key_revision": key_revision,
            "last_tested_at": _now_iso(self.clock()),
            "last_error_code": "",
            "updated_at": _now_iso(self.clock()),
        }
        self._write_root_json(scope, CONNECTION_FILE, saved)
        return saved

    def delete_key(
        self, user_id: int, shop_account_id: int, account_key: str, *, expected_revision: int,
    ) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        metadata = self.get_connection(*scope)
        if int(expected_revision) != int(metadata["revision"]):
            raise AIServiceError("revision_conflict", 409)
        saved = {
            **metadata,
            "api_key_configured": False,
            "connection_status": "unconfigured",
            "revision": int(metadata["revision"]) + 1,
            "key_revision": int(metadata["key_revision"]) + 1,
            "last_error_code": "",
            "updated_at": _now_iso(self.clock()),
        }
        self._write_root_json(scope, CONNECTION_FILE, saved)
        secret_path = self._account_dir(scope) / CONNECTION_SECRET_FILE
        try:
            if secret_path.is_symlink():
                raise OSError("unsafe secret path")
            secret_path.unlink(missing_ok=True)
        except OSError as exc:
            raise AIServiceError("credential_unavailable", 503) from exc
        settings = self.get_config(*scope)
        draft = dict(settings.get("draft") or empty_store_config())
        draft["enabled"] = False
        published = settings.get("published")
        if isinstance(published, dict) and isinstance(published.get("config"), dict):
            published = dict(published)
            published["config"] = {**published["config"], "enabled": False}
        settings.update(
            revision=int(settings["revision"]) + 1,
            draft=draft,
            published=published,
            status="disabled",
            updated_at=_now_iso(self.clock()),
        )
        self._write_root_json(scope, SETTINGS_FILE, settings)
        return saved

    def get_runtime_connection(self, user_id: int, shop_account_id: int, account_key: str) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        metadata = self.get_connection(*scope)
        try:
            provider = normalize_provider(metadata.get("provider"))
        except ProviderAdapterError as exc:
            raise AIServiceError("credential_unavailable", 503) from exc
        if is_api_key_required(provider) and not metadata["api_key_configured"]:
            raise AIServiceError("connection_unconfigured", 503)
        if metadata["connection_status"] != "verified":
            raise AIServiceError("connection_unverified", 503)
        base_url = self.normalize_base_url(metadata["base_url"], provider)
        model = _bounded_text(metadata["model"], 200, required=True)
        api_key = self._decrypt_key(scope, metadata) if metadata["api_key_configured"] else ""
        return {"provider": provider, "base_url": base_url, "model": model, "api_key": api_key}

    def is_configured(self, user_id: int, shop_account_id: int, account_key: str) -> bool:
        try:
            self.get_runtime_connection(user_id, shop_account_id, account_key)
            return True
        except AIServiceError:
            return False

    def ensure_reply_ready(
        self,
        user_id: int,
        shop_account_id: int,
        account_key: str,
        *,
        expected_config_revision: int | None = None,
    ) -> dict:
        """Fail closed unless the current published AI configuration can still reply."""
        self.get_runtime_connection(user_id, shop_account_id, account_key)
        settings = self.get_config(user_id, shop_account_id, account_key)
        published = settings.get("published")
        if not (
            isinstance(published, dict)
            and isinstance(published.get("config"), dict)
            and published.get("content_valid") is True
            and store_config_has_content(published["config"])
        ):
            raise AIServiceError("ai_unconfigured", 409, "当前店铺尚未保存有效客服内容")
        if published["config"].get("enabled") is not True:
            raise AIServiceError("ai_disabled", 409, "当前店铺 AI 客服未启用")
        revision = int(published.get("revision") or 0)
        if expected_config_revision is not None and revision != int(expected_config_revision):
            raise AIServiceError("ai_disabled", 409, "AI 配置已更新，本次迟到回复已取消")
        return {"config_revision": revision}

    def is_reply_ready(self, user_id: int, shop_account_id: int, account_key: str) -> bool:
        try:
            self.ensure_reply_ready(user_id, shop_account_id, account_key)
            return True
        except AIServiceError:
            return False

    def forward_payload(self, user_id: int, shop_account_id: int, account_key: str, payload: dict) -> tuple[int, bytes]:
        connection = self.get_runtime_connection(user_id, shop_account_id, account_key)
        outbound = dict(payload)
        outbound["stream"] = False
        response = self._request_json(
            connection["provider"], connection["base_url"], connection["model"],
            connection["api_key"], outbound,
        )
        self._response_text(response)
        return 200, _json_bytes(response)

    def get_config(self, user_id: int, shop_account_id: int, account_key: str) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        default = {
            "version": 2, "revision": 0, "status": "unconfigured", "draft": empty_store_config(),
            "published": None, "history": [], "updated_at": "",
        }
        payload = self._read_root_json(scope, SETTINGS_FILE, default)
        if not isinstance(payload, dict):
            return {**default, "content_valid": False}
        result = dict(default)
        result.update({key: payload.get(key) for key in default if key in payload})
        result["version"] = 2
        try:
            result["revision"] = max(0, int(result["revision"]))
        except (TypeError, ValueError) as exc:
            raise AIServiceError("credential_unavailable", 503) from exc
        result["draft"] = normalize_store_config(result["draft"] or empty_store_config())
        if result["published"] is not None:
            published = result["published"]
            if not isinstance(published, dict) or not isinstance(published.get("config"), dict):
                raise AIServiceError("credential_unavailable", 503)
            clean_published = normalize_store_config(published["config"])
            result["published"] = {
                "revision": int(published.get("revision") or 0),
                "published_at": str(published.get("published_at") or "")[:40],
                "config": clean_published,
                "content_valid": store_config_has_content(clean_published),
            }
        if not isinstance(result["history"], list):
            result["history"] = []
        result["history"] = result["history"][-MAX_HISTORY:]
        published = result.get("published")
        result["content_valid"] = bool(published and published.get("content_valid"))
        if not published:
            result["status"] = "draft" if store_config_has_content(result["draft"]) else "unconfigured"
        elif not result["content_valid"]:
            result["status"] = "needs_content"
        elif not published["config"].get("enabled"):
            result["status"] = "disabled"
        else:
            result["status"] = "published"
        return result

    def save_config(
        self, user_id: int, shop_account_id: int, account_key: str, *, config: dict | None,
        expected_revision: int, action: str,
    ) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        current = self.get_config(*scope)
        if int(expected_revision) != int(current["revision"]):
            raise AIServiceError("revision_conflict", 409)
        action = str(action or "save").strip().lower()
        if action not in {"draft", "publish", "save", "restore_published"}:
            raise AIServiceError("invalid_payload", 400)
        if action == "restore_published":
            if not isinstance(current.get("published"), dict):
                raise AIServiceError("invalid_payload", 409)
            clean = dict(current["published"]["config"])
        else:
            clean = normalize_store_config(config)
        content_valid = store_config_has_content(clean)
        if action in {"publish", "save"} and not content_valid:
            raise AIServiceError("invalid_payload", 400, "请先填写有实质内容的店铺与客服说明")
        revision = int(current["revision"]) + 1
        history = list(current.get("history") or [])
        published = current.get("published")
        status = "draft" if content_valid else "unconfigured"
        if action in {"publish", "save"}:
            if published:
                history.append({
                    "revision": int(published.get("revision") or 0),
                    "published_at": str(published.get("published_at") or "")[:40],
                    "status": "archived",
                })
            published = {
                "revision": revision,
                "published_at": _now_iso(self.clock()),
                "config": clean,
            }
            status = "published" if clean["enabled"] else "disabled"
        saved = {
            "version": 2, "revision": revision, "status": status, "draft": clean,
            "published": published, "history": history[-MAX_HISTORY:], "updated_at": _now_iso(self.clock()),
        }
        self._write_root_json(scope, SETTINGS_FILE, saved)
        return self.get_config(*scope)

    @staticmethod
    def _template_id(value: Any) -> str:
        text = _bounded_text(value, 80, required=True)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", text):
            raise AIServiceError("invalid_payload", 400)
        return text

    def _read_templates(self, scope: tuple[int, int, str]) -> list[dict]:
        payload = self._read_root_json(scope, TEMPLATES_FILE, {"version": 1, "templates": []})
        if not isinstance(payload, dict) or not isinstance(payload.get("templates"), list):
            raise AIServiceError("credential_unavailable", 503)
        if len(payload["templates"]) > MAX_TEMPLATES:
            raise AIServiceError("credential_unavailable", 503)
        clean: list[dict] = []
        try:
            for raw in payload["templates"]:
                if not isinstance(raw, dict):
                    raise ValueError("template shape")
                template_id = self._template_id(raw.get("id"))
                name = _bounded_text(raw.get("name"), 80, required=True)
                config = normalize_store_config(raw.get("config"))
                created_at = _bounded_text(raw.get("created_at", ""), 40)
                updated_at = _bounded_text(raw.get("updated_at", ""), 40)
                clean.append({
                    "id": template_id,
                    "name": name,
                    "config": config,
                    "created_at": created_at,
                    "updated_at": updated_at,
                })
        except (AIServiceError, TypeError, ValueError) as exc:
            raise AIServiceError("credential_unavailable", 503) from exc
        return clean

    def get_templates(self, user_id: int, shop_account_id: int, account_key: str) -> list[dict]:
        scope = self._scope(user_id, shop_account_id, account_key)
        templates = self._read_templates(scope)
        return sorted(templates, key=lambda item: item.get("updated_at", ""), reverse=True)

    def save_template(
        self, user_id: int, shop_account_id: int, account_key: str, *,
        name: str, config: dict, template_id: str | None = None,
    ) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        clean_name = _bounded_text(name, 80, required=True)
        clean_config = normalize_store_config(config)
        if not store_config_has_content(clean_config):
            raise AIServiceError("invalid_payload", 400, "请先填写有实质内容的店铺与客服说明")
        templates = self._read_templates(scope)
        selected_id = self._template_id(template_id) if template_id else ""
        existing = next((item for item in templates if item["id"] == selected_id), None) if selected_id else None
        duplicate = next(
            (item for item in templates
             if item["name"].casefold() == clean_name.casefold() and item["id"] != selected_id),
            None,
        )
        if duplicate and existing:
            raise AIServiceError("invalid_payload", 409, "当前店铺已有同名客服模板")
        if duplicate:
            existing = duplicate
        now = _now_iso(self.clock())
        if existing:
            existing.update({"name": clean_name, "config": clean_config, "updated_at": now})
            saved = existing
        else:
            if len(templates) >= MAX_TEMPLATES:
                raise AIServiceError("invalid_payload", 409, "客服模板数量已达上限")
            saved = {
                "id": "tpl-" + secrets.token_hex(8),
                "name": clean_name,
                "config": clean_config,
                "created_at": now,
                "updated_at": now,
            }
            templates.append(saved)
        self._write_root_json(scope, TEMPLATES_FILE, {"version": 1, "templates": templates})
        return saved

    def delete_template(self, user_id: int, shop_account_id: int, account_key: str, template_id: str) -> None:
        scope = self._scope(user_id, shop_account_id, account_key)
        selected_id = self._template_id(template_id)
        templates = self._read_templates(scope)
        remaining = [item for item in templates if item["id"] != selected_id]
        if len(remaining) == len(templates):
            raise AIServiceError("invalid_payload", 404, "客服模板不存在")
        self._write_root_json(scope, TEMPLATES_FILE, {"version": 1, "templates": remaining})

    def _products(self, scope) -> list[dict]:
        snapshot = self._read_root_json(scope, SNAPSHOT_FILE, None)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("products"), list):
            return []
        return [item for item in snapshot["products"] if isinstance(item, dict)]

    def product(self, user_id: int, shop_account_id: int, account_key: str, item_id: str) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        selected = _safe_item_id(item_id)
        for product in self._products(scope):
            try:
                if product_facts(product)["item_id"] == selected:
                    return product
            except AIServiceError:
                continue
        raise AIServiceError("item_not_found", 404)

    def _knowledge_path(self, scope, item_id: str) -> Path:
        return self.knowledge_dir(*scope) / f"{_safe_item_id(item_id)}.json"

    def _knowledge_default(self, item_id: str) -> dict:
        return {
            "version": 2, "item_id": item_id, "revision": 0, "status": "unconfigured",
            "draft": None, "published": None, "disabled": False, "history": [], "updated_at": "",
        }

    def get_knowledge(self, user_id: int, shop_account_id: int, account_key: str, item_id: str) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        product = self.product(*scope, item_id)
        selected = _safe_item_id(item_id)
        path = self._knowledge_path(scope, selected)
        try:
            payload = _private_path_read(path)
        except FileNotFoundError:
            payload = self._knowledge_default(selected)
        if not isinstance(payload, dict) or str(payload.get("item_id") or "") != selected:
            raise AIServiceError("credential_unavailable", 503)
        result = self._knowledge_default(selected)
        result.update({key: payload.get(key) for key in result if key in payload})
        result["version"] = 2
        try:
            result["revision"] = max(0, int(result["revision"] or 0))
        except (TypeError, ValueError) as exc:
            raise AIServiceError("credential_unavailable", 503) from exc
        if result["draft"] is not None:
            result["draft"] = normalize_knowledge(result["draft"])
        if result["published"] is not None:
            published = result["published"]
            if not isinstance(published, dict):
                raise AIServiceError("credential_unavailable", 503)
            knowledge_value = published.get("knowledge")
            if knowledge_value is None and "content" in published:
                knowledge_value = {"content": published.get("content", "")}
            clean_knowledge = normalize_knowledge(knowledge_value or {})
            result["published"] = {
                "revision": int(published.get("revision") or 0),
                "published_at": str(published.get("published_at") or "")[:40],
                "identity_fingerprint": str(published.get("identity_fingerprint") or "")[:80],
                "facts_fingerprint": str(
                    published.get("facts_fingerprint") or published.get("snapshot_fingerprint") or ""
                )[:80],
                "snapshot_fingerprint": str(
                    published.get("facts_fingerprint") or published.get("snapshot_fingerprint") or ""
                )[:80],
                "knowledge": clean_knowledge,
                "content_valid": knowledge_has_content(clean_knowledge),
            }
        current_identity = identity_fingerprint(product)
        current_facts = facts_fingerprint(product)
        published = result.get("published")
        legacy_fingerprint = bool(published and not published.get("identity_fingerprint"))
        # Old v1 files stored only an all-facts hash, so price/stock changes
        # cannot be separated from identity changes. Keep substantive imported
        # content usable as reference and require the next v2 save to establish
        # precise identity/facts fingerprints.
        stale = bool(
            published
            and published.get("content_valid")
            and not legacy_fingerprint
            and published.get("identity_fingerprint") != current_identity
        )
        facts_changed = bool(
            published and published.get("facts_fingerprint") and published.get("facts_fingerprint") != current_facts
        )
        if result.get("disabled"):
            effective_status = "disabled"
        elif stale:
            effective_status = "stale"
        elif published and published.get("content_valid"):
            effective_status = "published"
        elif result["draft"] is not None and knowledge_has_content(result["draft"]):
            effective_status = "draft"
        else:
            effective_status = "unconfigured"
        result.update(
            status=effective_status,
            stale=stale,
            needs_confirmation=stale,
            review_recommended=bool(
                published
                and isinstance(published.get("knowledge"), dict)
                and published["knowledge"].get("imported_from_v1") is True
            ),
            facts_changed=facts_changed,
            identity_fingerprint=current_identity,
            facts_fingerprint=current_facts,
            snapshot_fingerprint=current_facts,
            facts=product_facts(product),
        )
        if not isinstance(result.get("history"), list):
            result["history"] = []
        result["history"] = result["history"][-MAX_HISTORY:]
        return result

    def list_products(self, user_id: int, shop_account_id: int, account_key: str) -> list[dict]:
        scope = self._scope(user_id, shop_account_id, account_key)
        result = []
        for product in self._products(scope):
            try:
                item_id = product_facts(product)["item_id"]
                knowledge = self.get_knowledge(*scope, item_id)
            except AIServiceError:
                continue
            result.append({
                "item_id": item_id,
                "facts": knowledge["facts"],
                "identity_fingerprint": knowledge["identity_fingerprint"],
                "facts_fingerprint": knowledge["facts_fingerprint"],
                "snapshot_fingerprint": knowledge["snapshot_fingerprint"],
                "knowledge_status": knowledge["status"],
                "knowledge_revision": knowledge["revision"],
                "stale": knowledge["stale"],
                "needs_confirmation": knowledge["needs_confirmation"],
                "review_recommended": knowledge["review_recommended"],
                "facts_changed": knowledge["facts_changed"],
            })
        return result

    def save_knowledge(
        self, user_id: int, shop_account_id: int, account_key: str, item_id: str, *,
        knowledge: dict, expected_revision: int,
    ) -> dict:
        """Atomically save and activate substantive v2 product content."""
        scope = self._scope(user_id, shop_account_id, account_key)
        current = self.get_knowledge(*scope, item_id)
        if int(expected_revision) != int(current["revision"]):
            raise AIServiceError("revision_conflict", 409)
        clean = normalize_knowledge(knowledge)
        if not knowledge_has_content(clean):
            raise AIServiceError("invalid_payload", 400, "商品客服内容不能为空或只有标点")
        revision = int(current["revision"]) + 1
        history = list(current.get("history") or [])
        if current.get("published"):
            history.append({
                "revision": int(current["published"].get("revision") or 0),
                "published_at": current["published"].get("published_at", ""),
                "status": "archived",
            })
        saved = {
            "version": 2, "item_id": current["item_id"], "revision": revision,
            "status": "published", "draft": clean, "disabled": False,
            "published": {
                "revision": revision, "published_at": _now_iso(self.clock()),
                "identity_fingerprint": current["identity_fingerprint"],
                "facts_fingerprint": current["facts_fingerprint"],
                "snapshot_fingerprint": current["facts_fingerprint"],
                "knowledge": clean,
            },
            "history": history[-MAX_HISTORY:], "updated_at": _now_iso(self.clock()),
        }
        self._write_path_json(self._knowledge_path(scope, current["item_id"]), saved)
        return self.get_knowledge(*scope, current["item_id"])

    def publish_knowledge(
        self, user_id: int, shop_account_id: int, account_key: str, item_id: str, *,
        expected_revision: int,
    ) -> dict:
        """Compatibility publish path; it uses the same substantive validation."""
        scope = self._scope(user_id, shop_account_id, account_key)
        current = self.get_knowledge(*scope, item_id)
        if int(expected_revision) != int(current["revision"]):
            raise AIServiceError("revision_conflict", 409)
        candidate = current.get("draft")
        if candidate is None and isinstance(current.get("published"), dict):
            candidate = current["published"].get("knowledge")
        clean = normalize_knowledge(candidate or {})
        if not knowledge_has_content(clean):
            raise AIServiceError("invalid_payload", 400, "商品客服内容不能为空或只有标点")
        revision = int(current["revision"]) + 1
        history = list(current.get("history") or [])
        if current.get("published"):
            history.append({
                "revision": int(current["published"].get("revision") or 0),
                "published_at": current["published"].get("published_at", ""),
                "status": "archived",
            })
        saved = {
            "version": 2, "item_id": current["item_id"], "revision": revision,
            "status": "published", "draft": clean, "disabled": False,
            "published": {
                "revision": revision, "published_at": _now_iso(self.clock()),
                "identity_fingerprint": current["identity_fingerprint"],
                "facts_fingerprint": current["facts_fingerprint"],
                "snapshot_fingerprint": current["facts_fingerprint"],
                "knowledge": clean,
            },
            "history": history[-MAX_HISTORY:], "updated_at": _now_iso(self.clock()),
        }
        self._write_path_json(self._knowledge_path(scope, current["item_id"]), saved)
        return self.get_knowledge(*scope, current["item_id"])

    def disable_knowledge(
        self, user_id: int, shop_account_id: int, account_key: str, item_id: str, *,
        expected_revision: int,
    ) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        current = self.get_knowledge(*scope, item_id)
        if int(expected_revision) != int(current["revision"]):
            raise AIServiceError("revision_conflict", 409)
        saved = {
            "version": 2, "item_id": current["item_id"], "revision": int(current["revision"]) + 1,
            "status": "disabled", "draft": current.get("draft"), "published": current.get("published"),
            "disabled": True, "history": current.get("history", [])[-MAX_HISTORY:],
            "updated_at": _now_iso(self.clock()),
        }
        self._write_path_json(self._knowledge_path(scope, current["item_id"]), saved)
        return self.get_knowledge(*scope, current["item_id"])

    def knowledge_versions(self, user_id: int, shop_account_id: int, account_key: str, item_id: str) -> list[dict]:
        current = self.get_knowledge(user_id, shop_account_id, account_key, item_id)
        versions = list(current.get("history") or [])[-MAX_HISTORY:]
        if current.get("published"):
            versions.append({
                "revision": current["published"]["revision"],
                "published_at": current["published"]["published_at"],
                "status": "disabled" if current["status"] == "disabled" else "stale" if current["stale"] else "published",
            })
        return versions[-MAX_HISTORY:]

    def _chat(self, scope, messages: list[dict], *, max_tokens: int = 500) -> str:
        connection = self.get_runtime_connection(*scope)
        response = self._request_json(
            connection["provider"], connection["base_url"], connection["model"], connection["api_key"],
            {"stream": False, "messages": messages,
             "temperature": 0.2, "max_tokens": max(1, min(int(max_tokens), 1200))},
        )
        return self._response_text(response)

    def extract_knowledge(
        self, user_id: int, shop_account_id: int, account_key: str, item_id: str, source_text: str,
    ) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        product = self.product(*scope, item_id)
        source = _bounded_text(source_text, 12_000, required=True)
        text = self._chat(scope, [
            {
                "role": "system",
                "content": (
                    "把店主提供的商品资料整理为一段可直接供客服参考的中文自然语言内容。"
                    "保留真实细节，按需要分段，不得编造，不得输出 JSON、Markdown 代码块或解释说明。"
                ),
            },
            {
                "role": "user",
                "content": "店主原始资料：\n" + source[:9_000] + "\n\n只读实时商品事实：\n" + json.dumps(
                    _prompt_product_facts(product), ensure_ascii=False, separators=(",", ":")
                ),
            },
        ], max_tokens=1_000)
        preview = _generated_knowledge(text)
        return {"content": preview["content"], "saved": False}

    @staticmethod
    def _persona_payload(store_config: dict) -> dict:
        return {
            key: store_config.get(key)
            for key in (
                "persona_preset", "persona_name", "persona_instruction", "tone",
                "buyer_address", "reply_length", "emoji_level",
            )
        }

    def compile_effective_context(
        self,
        *,
        current_message: str,
        history: list[dict] | None,
        product_facts_value: dict | None,
        store_config: dict,
        product_knowledge: dict | None,
        knowledge_status: str,
    ) -> dict:
        clean_store = normalize_store_config(store_config)
        clean_product = normalize_knowledge(product_knowledge) if product_knowledge is not None else empty_knowledge()
        try:
            return compile_effective_context(
                current_message=current_message,
                history=history,
                product_facts=product_facts_value,
                store_content=clean_store["store_content"],
                product_content=clean_product["content"] if knowledge_has_content(clean_product) else "",
                persona=self._persona_payload(clean_store),
                forbidden_claims=clean_store["forbidden_claims"],
                handoff_rules=clean_store["handoff_rules"],
                knowledge_status=knowledge_status,
            )
        except ReplyEngineError as exc:
            raise AIServiceError(exc.code, 400, str(exc)) from exc

    # Compatibility alias retained for contracts and old callers.
    def compile_context(
        self, store_config: dict, product: dict | None, knowledge: dict | None,
        buyer_message: str, history: list[dict] | None = None,
    ) -> list[dict]:
        facts = product_facts(product) if product else None
        return self.compile_effective_context(
            current_message=buyer_message,
            history=history,
            product_facts_value=facts,
            store_config=store_config,
            product_knowledge=knowledge,
            knowledge_status="published" if knowledge_has_content(knowledge) else "missing",
        )["messages"]

    def generate_reply_decision(
        self,
        scope: tuple[int, int, str],
        compiled: dict,
        store_config: dict,
        recent_assistant_replies: list[str] | None = None,
    ) -> dict:
        clean_store = normalize_store_config(store_config)
        return generate_reply_decision(
            compiled,
            lambda messages: self._chat(scope, messages, max_tokens=700),
            forbidden_claims=clean_store["forbidden_claims"],
            recent_assistant_replies=recent_assistant_replies,
        )

    def _effective_reply_inputs(
        self,
        scope: tuple[int, int, str],
        *,
        item_id: str | None,
        item_context: dict | None,
        store_config_override: dict | None,
        knowledge_override: dict | None,
        require_enabled: bool,
    ) -> tuple[dict, dict | None, dict | None, str, int, str, int | None]:
        settings = self.get_config(*scope)
        published = settings.get("published") if isinstance(settings.get("published"), dict) else None
        live_config_revision = int(published.get("revision") or 0) if published else 0
        candidate_revision: int | None = None
        if store_config_override is not None:
            store_config = normalize_store_config(store_config_override)
            config_source = "draft_override"
            candidate_revision = int(settings.get("revision") or 0)
        elif published and isinstance(published.get("config"), dict):
            store_config = published["config"]
            config_source = "published"
        else:
            raise AIServiceError("ai_unconfigured", 409, "当前店铺尚未保存有效客服内容")
        if not store_config_has_content(store_config):
            if store_config_override is not None:
                raise AIServiceError("invalid_payload", 409, "当前店铺尚未保存有效客服内容")
            raise AIServiceError("ai_unconfigured", 409, "当前店铺尚未保存有效客服内容")
        if require_enabled:
            if not (
                published
                and isinstance(published.get("config"), dict)
                and published.get("content_valid") is True
                and store_config_has_content(published["config"])
            ):
                raise AIServiceError("ai_unconfigured", 409, "当前店铺尚未保存有效客服内容")
            if published["config"].get("enabled") is not True:
                raise AIServiceError("ai_disabled", 409, "当前店铺 AI 客服未启用")
        facts = None
        knowledge = None
        knowledge_status = "not_selected"
        if item_id:
            product = self.product(*scope, item_id)
            trusted_facts = product_facts(product)
            if item_context is not None:
                if not isinstance(item_context, dict):
                    raise AIServiceError("invalid_payload", 400)
                claimed_id = item_context.get("id") or item_context.get("item_id") or item_context.get("itemId")
                if claimed_id is not None and _safe_item_id(claimed_id) != trusted_facts["item_id"]:
                    raise AIServiceError("item_not_found", 404)
                facts = product_facts({**item_context, "id": item_id})
            else:
                facts = trusted_facts
            current = self.get_knowledge(*scope, item_id)
            if knowledge_override is not None:
                knowledge = normalize_knowledge(knowledge_override)
                knowledge_status = "draft_override" if knowledge_has_content(knowledge) else "unconfigured"
            elif current["status"] == "published":
                knowledge = current["published"]["knowledge"]
                knowledge_status = "published"
            else:
                knowledge_status = current["status"]
        return store_config, facts, knowledge, knowledge_status, live_config_revision, config_source, candidate_revision

    def reply(
        self,
        user_id: int,
        shop_account_id: int,
        account_key: str,
        *,
        message: str,
        history: list[dict] | None = None,
        item_id: str | None = None,
        item_context: dict | None = None,
        recent_assistant_replies: list[str] | None = None,
    ) -> dict:
        scope = self._scope(user_id, shop_account_id, account_key)
        (
            store_config,
            facts,
            knowledge,
            knowledge_status,
            config_revision,
            _source,
            _candidate_revision,
        ) = self._effective_reply_inputs(
            scope,
            item_id=item_id,
            item_context=item_context,
            store_config_override=None,
            knowledge_override=None,
            require_enabled=True,
        )
        compiled = self.compile_effective_context(
            current_message=message,
            history=history,
            product_facts_value=facts,
            store_config=store_config,
            product_knowledge=knowledge,
            knowledge_status=knowledge_status,
        )
        decision = self.generate_reply_decision(scope, compiled, store_config, recent_assistant_replies)
        if decision.get("decision") == "reply":
            self.ensure_reply_ready(*scope, expected_config_revision=config_revision)
        return {**decision, "config_revision": config_revision}

    def preview(
        self, user_id: int, shop_account_id: int, account_key: str, *, buyer_message: str,
        item_id: str | None = None, store_config_override: dict | None = None,
        knowledge_override: dict | None = None, history: list[dict] | None = None,
        recent_assistant_replies: list[str] | None = None,
    ) -> dict:
        started = self.clock()
        scope = self._scope(user_id, shop_account_id, account_key)
        (
            store_config,
            facts,
            knowledge,
            knowledge_status,
            config_revision,
            source,
            candidate_revision,
        ) = self._effective_reply_inputs(
            scope,
            item_id=item_id,
            item_context=None,
            store_config_override=store_config_override,
            knowledge_override=knowledge_override,
            require_enabled=False,
        )
        compiled = self.compile_effective_context(
            current_message=buyer_message,
            history=history,
            product_facts_value=facts,
            store_config=store_config,
            product_knowledge=knowledge,
            knowledge_status=knowledge_status,
        )
        decision = self.generate_reply_decision(scope, compiled, store_config, recent_assistant_replies)
        sources = decision.get("sources") if isinstance(decision.get("sources"), list) else []
        result = {
            **decision,
            "hit_level": (
                "product_content"
                if "product_content" in sources
                else "store_content"
                if "store_content" in sources
                else ""
            ),
            "config_source": source,
            "latency_ms": max(0, int((self.clock() - started) * 1000)),
            "safety_status": (
                "passed"
                if decision.get("decision") == "reply"
                else str(decision.get("reason_code") or "not_available")
            ),
            "sent": False,
        }
        if source == "published":
            result["config_revision"] = config_revision
        elif candidate_revision is not None:
            result["candidate_revision"] = candidate_revision
        return result


service = AIService()


__all__ = [
    "AIService", "AIServiceError", "CONNECTION_FILE", "CONNECTION_SECRET_FILE",
    "KNOWLEDGE_DIR", "SETTINGS_FILE", "SNAPSHOT_FILE", "TEMPLATES_FILE", "catgirl_preset",
    "empty_knowledge", "empty_store_config", "facts_fingerprint", "identity_fingerprint",
    "knowledge_has_content", "normalize_knowledge", "normalize_store_config", "product_facts",
    "service", "snapshot_fingerprint", "store_config_has_content",
]
