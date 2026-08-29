"""Legacy account-local AI runtime reader.

The live reply path no longer imports this module.  It remains only for bounded
v1 migration tooling and therefore fails with typed errors instead of silently
returning an empty context.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class AIRuntimeError(RuntimeError):
    """Base error for legacy AI runtime documents."""


class AIRuntimeMissingError(AIRuntimeError):
    """A required legacy runtime input is absent."""


class AIRuntimeFormatError(AIRuntimeError):
    """A legacy runtime input is malformed or unsafe."""


class AIRuntimeScopeError(AIRuntimeError):
    """The requested item does not match the trusted runtime scope."""


def _runtime_text(value, limit=2000):
    if not isinstance(value, str):
        return ""
    return _CONTROL_RE.sub(" ", value).strip()[:limit]


def _runtime_number(value):
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return value
    return str(value).strip()[:80]


def _runtime_list(value, limit=40, item_limit=240):
    if not isinstance(value, list):
        raise AIRuntimeFormatError("legacy AI list field is invalid")
    result = []
    for item in value[:limit]:
        text = _runtime_text(item, item_limit)
        if text and text not in result:
            result.append(text)
    return result


def _read_private_json(path, *, maximum_bytes=256 * 1024):
    if not isinstance(path, str) or not path.strip():
        raise AIRuntimeMissingError("legacy AI runtime path is missing")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise AIRuntimeFormatError("legacy AI runtime file must be a private regular file")
        if info.st_size > maximum_bytes:
            raise AIRuntimeFormatError("legacy AI runtime file is too large")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            payload = json.load(stream)
    except FileNotFoundError as exc:
        raise AIRuntimeMissingError("legacy AI runtime file is missing") from exc
    except AIRuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AIRuntimeFormatError("legacy AI runtime file cannot be read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise AIRuntimeFormatError("legacy AI runtime file must contain an object")
    return payload


def product_facts(item_info):
    if not isinstance(item_info, dict):
        raise AIRuntimeFormatError("legacy product facts are invalid")
    item_id = str(
        item_info.get("id") or item_info.get("item_id") or item_info.get("itemId") or ""
    ).strip()
    if not item_id.isdigit() or len(item_id) > 64:
        raise AIRuntimeFormatError("legacy product item_id is invalid")
    raw_skus = item_info.get("sku") or item_info.get("skus") or item_info.get("skuList") or []
    if not isinstance(raw_skus, list):
        raise AIRuntimeFormatError("legacy product skus are invalid")
    clean_skus = []
    for raw in raw_skus[:50]:
        if not isinstance(raw, dict):
            raise AIRuntimeFormatError("legacy product sku is invalid")
        properties = raw.get("propertyList") or raw.get("properties") or []
        if not isinstance(properties, list):
            raise AIRuntimeFormatError("legacy product sku properties are invalid")
        labels = []
        for prop in properties[:10]:
            if not isinstance(prop, dict):
                raise AIRuntimeFormatError("legacy product sku property is invalid")
            label = prop.get("valueText") or prop.get("value") or prop.get("name")
            if isinstance(label, str) and label.strip():
                labels.append(label.strip()[:80])
        clean_skus.append({
            "name": str(raw.get("name") or raw.get("title") or " ".join(labels) or "").strip()[:160],
            "price": _runtime_number(raw.get("price")),
            "stock": _runtime_number(raw.get("stock") if "stock" in raw else raw.get("quantity")),
        })
    return {
        "item_id": item_id,
        "title": _runtime_text(item_info.get("title") or item_info.get("name"), 300),
        "description": _runtime_text(item_info.get("description") or item_info.get("desc"), 4000),
        "price": _runtime_number(item_info.get("price") if "price" in item_info else item_info.get("soldPrice")),
        "stock": _runtime_number(item_info.get("stock") if "stock" in item_info else item_info.get("quantity")),
        "status": _runtime_text(item_info.get("status") or item_info.get("itemStatus"), 80),
        "skus": clean_skus,
    }


def snapshot_fingerprint(item_info):
    facts = product_facts(item_info)
    encoded = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _snapshot_product(products_snapshot_path, item_id):
    snapshot = _read_private_json(products_snapshot_path, maximum_bytes=1024 * 1024)
    products = snapshot.get("products")
    if not isinstance(products, list):
        raise AIRuntimeFormatError("legacy products snapshot is invalid")
    for product in products:
        facts = product_facts(product)
        if facts["item_id"] == item_id:
            return product
    raise AIRuntimeScopeError("legacy product is absent from the trusted snapshot")


def load_published_context(
    settings_path, knowledge_dir, item_id, item_info, products_snapshot_path=""
):
    """Strictly read one exact published v1 scope for migration tooling."""
    if not isinstance(knowledge_dir, str) or not knowledge_dir.strip():
        raise AIRuntimeMissingError("legacy AI knowledge directory is missing")
    selected_item_id = str(item_id or "").strip()
    if not selected_item_id.isdigit() or len(selected_item_id) > 64:
        raise AIRuntimeScopeError("legacy AI item scope is invalid")
    facts = product_facts(item_info)
    if facts["item_id"] != selected_item_id:
        raise AIRuntimeScopeError("legacy AI item facts do not match the requested scope")

    settings = _read_private_json(settings_path)
    published = settings.get("published")
    if not isinstance(published, dict) or not isinstance(published.get("config"), dict):
        raise AIRuntimeFormatError("legacy published store configuration is invalid")
    store = published["config"]
    if store.get("enabled") is not True:
        raise AIRuntimeScopeError("legacy published AI configuration is disabled")

    snapshot_product = _snapshot_product(products_snapshot_path, selected_item_id)
    current_snapshot_fingerprint = snapshot_fingerprint(snapshot_product)
    document = _read_private_json(os.path.join(knowledge_dir, selected_item_id + ".json"))
    published_knowledge = document.get("published")
    if (
        document.get("item_id") != selected_item_id
        or document.get("disabled") is True
        or document.get("status") != "published"
        or not isinstance(published_knowledge, dict)
        or not isinstance(published_knowledge.get("knowledge"), dict)
    ):
        raise AIRuntimeFormatError("legacy published product knowledge is invalid")
    if published_knowledge.get("snapshot_fingerprint") != current_snapshot_fingerprint:
        raise AIRuntimeScopeError("legacy product knowledge is stale")

    compiled = {
        "store_service": {
            "persona_preset": _runtime_text(store.get("persona_preset"), 32),
            "persona_name": _runtime_text(store.get("persona_name"), 80),
            "persona_instruction": _runtime_text(store.get("persona_instruction"), 1200),
            "tone": _runtime_text(store.get("tone"), 32),
            "buyer_address": _runtime_text(store.get("buyer_address"), 40),
            "reply_length": _runtime_text(store.get("reply_length"), 32),
            "emoji_level": _runtime_text(store.get("emoji_level"), 32),
            "common_knowledge": _runtime_text(store.get("common_knowledge"), 8000),
            "forbidden_claims": _runtime_list(store.get("forbidden_claims", []), 50, 240),
            "handoff_rules": _runtime_list(store.get("handoff_rules", []), 50, 240),
        },
        "product_knowledge": published_knowledge["knowledge"],
    }
    encoded = json.dumps(compiled, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 24_000:
        raise AIRuntimeFormatError("legacy published AI context exceeds the migration limit")
    fallback = _runtime_text(store.get("fallback_reply"), 1000)
    return encoded, fallback


__all__ = [
    "AIRuntimeError",
    "AIRuntimeFormatError",
    "AIRuntimeMissingError",
    "AIRuntimeScopeError",
    "load_published_context",
    "product_facts",
    "snapshot_fingerprint",
]
