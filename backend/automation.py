"""Validation and persistence helpers for the simple tenant automation UI.

The public API deliberately exposes a small, deterministic contract.  The
managed agent reads the same files from the tenant directory, so these
helpers keep the on-disk representation strict and bounded.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re


MAX_RULES = 50
MAX_KEYWORDS_PER_RULE = 10
MAX_KEYWORD_CHARS = 40
MAX_REPLY_CHARS = 1000
MAX_MATERIAL_CHARS = 8000
MAX_DELIVERIES = 500
MAX_BATCH_ITEMS = 500
STRATEGY_PRESETS = ("conservative", "standard", "aggressive")

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class AutomationValidationError(ValueError):
    """Raised when a tenant supplied automation payload is unsafe/invalid."""


def normalise_strategy(value) -> str:
    """Validate the small public strategy surface used by the workbench."""
    if value is None or value == "":
        return "standard"
    if not isinstance(value, str) or value.strip().lower() not in STRATEGY_PRESETS:
        raise AutomationValidationError("自动策略无效")
    return value.strip().lower()


def normalise_settings(value) -> dict:
    """Return a bounded account-scoped automation settings document."""
    if value is None:
        return {"version": 1, "strategy": "standard", "enabled": True,
                "rules_enabled": True, "ai_enabled": True}
    if not isinstance(value, dict):
        raise AutomationValidationError("自动策略格式无效")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AutomationValidationError("自动化开关格式无效")
    # Per-engine switches control the rule engine and the AI engine
    # independently; legacy documents that only have ``enabled`` inherit it.
    rules_enabled = value.get("rules_enabled", enabled)
    ai_enabled = value.get("ai_enabled", enabled)
    if not isinstance(rules_enabled, bool):
        raise AutomationValidationError("规则客服开关格式无效")
    if not isinstance(ai_enabled, bool):
        raise AutomationValidationError("智能客服开关格式无效")

    def _optional_text(key: str, limit: int) -> str:
        raw = value.get(key)
        if raw is None or raw == "":
            return ""
        if not isinstance(raw, str):
            raise AutomationValidationError(f"{key}格式无效")
        text = raw.strip()
        if len(text) > limit or _CONTROL_RE.search(text):
            raise AutomationValidationError(f"{key}不能超过 {limit} 字")
        return text

    def _bounded_int(key: str, default: int, minimum: int, maximum: int) -> int:
        raw = value.get(key, default)
        try:
            number = int(raw)
        except (TypeError, ValueError):
            raise AutomationValidationError(f"{key}格式无效")
        if not minimum <= number <= maximum:
            raise AutomationValidationError(f"{key}必须在 {minimum}-{maximum} 之间")
        return number

    def _time(key: str, default: str) -> str:
        raw = value.get(key, default)
        if not isinstance(raw, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", raw.strip()):
            raise AutomationValidationError(f"{key}时间格式无效")
        return raw.strip()

    business_hours_enabled = value.get("business_hours_enabled", False)
    if not isinstance(business_hours_enabled, bool):
        raise AutomationValidationError("营业时间开关格式无效")
    return {
        "version": 1,
        "strategy": normalise_strategy(value.get("strategy", "standard")),
        "enabled": enabled,
        "rules_enabled": rules_enabled,
        "ai_enabled": ai_enabled,
        "first_reply": _optional_text("first_reply", 1000),
        "fallback_reply": _optional_text("fallback_reply", 1000),
        "delay_min_seconds": _bounded_int("delay_min_seconds", 0, 0, 60),
        "delay_max_seconds": _bounded_int("delay_max_seconds", 0, 0, 60),
        "trigger_cooldown_seconds": _bounded_int("trigger_cooldown_seconds", 0, 0, 300),
        "manual_takeover_cooldown_seconds": _bounded_int("manual_takeover_cooldown_seconds", 0, 0, 300),
        "business_hours_enabled": business_hours_enabled,
        "business_start": _time("business_start", "09:00"),
        "business_end": _time("business_end", "23:30"),
    }


def _clean_text(value, *, limit: int, field: str, allow_newlines: bool = True) -> str:
    if not isinstance(value, str):
        raise AutomationValidationError(f"{field}格式无效")
    value = value.strip()
    if not value or len(value) > limit or _CONTROL_RE.search(value):
        raise AutomationValidationError(f"{field}不能为空且不能超过 {limit} 字")
    if not allow_newlines and any(char in value for char in "\r\n"):
        raise AutomationValidationError(f"{field}不能换行")
    return value


def _normalise_keyword(value: str) -> str:
    return _clean_text(
        value,
        limit=MAX_KEYWORD_CHARS,
        field="关键词",
        allow_newlines=False,
    )


def _legacy_rules(value):
    """Convert the old keyword map into the v1 list without widening it."""
    if value == {} or value == [] or value is None:
        return []
    if isinstance(value, dict) and isinstance(value.get("rules"), list):
        return value["rules"]
    if isinstance(value, dict):
        converted = []
        for keyword, reply in value.items():
            if not isinstance(keyword, str) or not isinstance(reply, str):
                raise AutomationValidationError("回复规则格式无效")
            converted.append({"keywords": [keyword], "reply": reply})
        return converted
    if isinstance(value, list):
        return value
    raise AutomationValidationError("回复规则格式无效")


def normalise_rules(value) -> list[dict]:
    """Return canonical deterministic reply rules in first-match order."""
    raw_rules = _legacy_rules(value)
    if not isinstance(raw_rules, list) or len(raw_rules) > MAX_RULES:
        raise AutomationValidationError(f"最多设置 {MAX_RULES} 条回复规则")
    result = []
    seen_keywords: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_rules, start=1):
        if not isinstance(raw, dict):
            raise AutomationValidationError("回复规则格式无效")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AutomationValidationError("规则开关格式无效")
        name_missing = "name" not in raw
        name_raw = raw.get("name", f"规则 {index}")
        if not isinstance(name_raw, str):
            raise AutomationValidationError("规则名称格式无效")
        name = name_raw.strip()
        if not name and name_missing:
            name = f"规则 {index}"
        elif not name:
            raise AutomationValidationError("规则名称不能为空")
        if len(name) > 80 or _CONTROL_RE.search(name) or any(char in name for char in "\r\n"):
            raise AutomationValidationError("规则名称不能超过 80 字且不能换行")
        item_raw = raw.get("item_id", "")
        item_id = "" if item_raw is None or item_raw == "" else _safe_item_id(item_raw)
        keywords = raw.get("keywords")
        if isinstance(keywords, str):
            keywords = [part for part in keywords.split(",")]
        if not isinstance(keywords, list) or not keywords or len(keywords) > MAX_KEYWORDS_PER_RULE:
            raise AutomationValidationError(
                f"每条规则需要 1-{MAX_KEYWORDS_PER_RULE} 个关键词"
            )
        clean_keywords = []
        for keyword in keywords:
            clean = _normalise_keyword(keyword)
            key = (item_id, clean.casefold())
            if key in seen_keywords:
                raise AutomationValidationError("同一商品范围内关键词不能重复")
            seen_keywords.add(key)
            clean_keywords.append(clean)
        match = raw.get("match", "contains")
        if match != "contains":
            raise AutomationValidationError("目前只支持包含关键词")
        reply = _clean_text(raw.get("reply", ""), limit=MAX_REPLY_CHARS, field="回复内容")
        result.append(
            {
                "id": f"rule-{index}",
                "name": name,
                "item_id": item_id,
                "enabled": enabled,
                "keywords": clean_keywords,
                "match": "contains",
                "reply": reply,
            }
        )
    return result


def rules_document(value) -> dict:
    return {"version": 1, "rules": normalise_rules(value)}


def _safe_item_id(value) -> str:
    if isinstance(value, bool):
        raise AutomationValidationError("商品 ID 格式无效")
    item_id = str(value).strip()
    if not item_id or len(item_id) > 64 or not item_id.isascii() or not item_id.isdigit():
        raise AutomationValidationError("商品 ID 格式无效")
    return item_id


def _snapshot_ids(snapshot: dict | None) -> set[str]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("products"), list):
        return set()
    return {
        str(item.get("id")).strip()
        for item in snapshot["products"]
        if isinstance(item, dict)
        and str(item.get("id") or "").strip().isdigit()
    }


def normalise_deliveries(value, snapshot: dict | None) -> list[dict]:
    """Validate simple fixed-material deliveries against the shop snapshot."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise AutomationValidationError("自动发货配置必须是列表")
    allowed = _snapshot_ids(snapshot)
    result = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise AutomationValidationError("自动发货配置格式无效")
        item_id = _safe_item_id(raw.get("item_id"))
        if item_id not in allowed:
            raise AutomationValidationError("只能选择当前店铺已识别的商品")
        if item_id in seen:
            raise AutomationValidationError("同一个商品不能重复配置")
        seen.add(item_id)
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AutomationValidationError("自动发货开关格式无效")
        material = _clean_text(
            raw.get("material", ""),
            limit=MAX_MATERIAL_CHARS,
            field="自动发送资料",
        )
        result.append(
            {
                "item_id": item_id,
                "enabled": enabled,
                "delivery": "material",
                "material": material,
            }
        )
    return result


def normalise_material_batch(item_ids, material, enabled, snapshot: dict | None) -> list[dict]:
    """Validate one simple, account-local batch material operation."""
    if not isinstance(item_ids, list) or not item_ids:
        raise AutomationValidationError("请至少指定一个商品")
    if not isinstance(enabled, bool):
        raise AutomationValidationError("商品资料开关格式无效")
    if not enabled:
        # Disabling preserves the existing payload; no new material is created.
        clean_material = ""
    else:
        clean_material = _clean_text(material, limit=MAX_MATERIAL_CHARS, field="自动发送资料")
    allowed = _snapshot_ids(snapshot)
    clean_ids = []
    seen: set[str] = set()
    for value in item_ids:
        item_id = _safe_item_id(value)
        if item_id not in allowed:
            raise AutomationValidationError("只能选择当前店铺已识别的商品")
        if item_id in seen:
            raise AutomationValidationError("同一个商品不能重复配置")
        seen.add(item_id)
        clean_ids.append(item_id)
    return [
        {"item_id": item_id, "enabled": enabled, "material": clean_material}
        for item_id in clean_ids
    ]


def product_snapshot_revision(snapshot: dict | None) -> str:
    """Return an opaque revision for the verified product snapshot."""
    products = []
    if isinstance(snapshot, dict) and isinstance(snapshot.get("products"), list):
        for item in snapshot["products"]:
            if not isinstance(item, dict):
                continue
            products.append(
                {
                    "id": str(item.get("id") or ""),
                    "title": str(item.get("title") or ""),
                    "description": str(item.get("description") or ""),
                    "price": item.get("price"),
                    "status": str(item.get("status") or ""),
                }
            )
    payload = {
        "account_ref": str(snapshot.get("account_ref") or "") if isinstance(snapshot, dict) else "",
        "synced_at": str(snapshot.get("synced_at") or "") if isinstance(snapshot, dict) else "",
        "products": sorted(products, key=lambda item: item["id"]),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]


def product_config_revision(products: object) -> str:
    """Return an opaque revision for the private product configuration."""
    payload = products if isinstance(products, dict) else {"version": 1, "types": []}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def material_batch_preview(
    products: object,
    updates: list[dict],
    snapshot: dict | None,
) -> dict:
    """Describe selected changes without returning material payloads."""
    current = {}
    for item in deliveries_from_products(products, snapshot):
        current[item["item_id"]] = item
    changes = 0
    for update in updates:
        existing = current.get(update["item_id"])
        if not update["enabled"]:
            changed = existing is not None and existing.get("enabled") is not False
        else:
            changed = (
                existing is None
                or existing.get("enabled") is not True
                or existing.get("material") != update["material"]
            )
        if changed:
            changes += 1
    return {
        "selected_count": len(updates),
        "change_count": changes,
        "unchanged_count": len(updates) - changes,
    }


def _material_product_entry(source, item_id, material, enabled, snapshot):
    """Copy a single target's metadata, splitting shared bindings only as needed."""
    source = source or {}
    ids = source.get("item_ids")
    if not isinstance(ids, list):
        ids = [source["item_id"]] if source.get("item_id") is not None else []
    result = copy.deepcopy(source)
    identifier = source.get("id") if len(ids) == 1 else None
    result.update(id=identifier or f"basic-{item_id}", name=source.get("name") or _product_title(snapshot, item_id),
                  item_ids=[item_id], delivery="material", enabled=enabled, payload=material)
    for field in ("item_id", "material", "resource_match"):
        result.pop(field, None)
    return result


def merge_material_product_updates(
    existing: object,
    updates: list[dict],
    snapshot: dict | None,
) -> dict:
    """Apply selected material updates while preserving all other mappings."""
    if existing is not None and not isinstance(existing, dict):
        raise AutomationValidationError("商品配置文件格式无效，未重置原文件")
    existing_types = existing.get("types", []) if isinstance(existing, dict) else []
    if not isinstance(existing_types, list):
        raise AutomationValidationError("商品配置 types 格式无效，未重置原文件")
    by_id = {item["item_id"]: item for item in updates}
    sources = {}
    retained = []
    for raw in existing_types:
        if not isinstance(raw, dict):
            retained.append(raw)
            continue
        raw_ids = raw.get("item_ids")
        legacy_id = raw.get("item_id") if raw_ids is None else None
        item_ids = raw_ids if isinstance(raw_ids, list) else ([legacy_id] if legacy_id is not None else None)
        if item_ids is None:
            retained.append(raw)
            continue
        clean_ids = [str(item).strip() for item in item_ids]
        selected_ids = [item_id for item_id in clean_ids if item_id in by_id]
        if not selected_ids:
            retained.append(raw)
            continue
        for item_id in selected_ids:
            if by_id[item_id]["enabled"] or raw.get("delivery") == "material":
                sources.setdefault(item_id, raw)
        # Disable only existing material mappings. A selected redeem/pan ID
        # remains in place even when it shares a legacy group with a material
        # ID that is being changed.
        preserved = [
            item_id
            for item_id in clean_ids
            if item_id not in by_id
            or (not by_id[item_id]["enabled"] and raw.get("delivery") != "material")
        ]
        if preserved:
            remainder = copy.deepcopy(raw)
            remainder["item_ids"] = preserved
            remainder.pop("item_id", None)
            retained.append(remainder)
    existing_material = {
        item["item_id"]: item
        for item in deliveries_from_products(existing, snapshot)
    }
    for update in updates:
        if not update["enabled"]:
            current = existing_material.get(update["item_id"])
            if current is None:
                continue
            source = sources.get(update["item_id"], {})
            payload = source.get("payload", source.get("material", current["material"]))
            retained.append(_material_product_entry(source, update["item_id"], payload, False, snapshot))
            continue
        retained.append(_material_product_entry(sources.get(update["item_id"]), update["item_id"], update["material"], True, snapshot))
    return {**{key: value for key, value in (existing or {}).items() if key != "_ops_receipt"},
            "version": (existing or {}).get("version", 1), "types": retained}


def _product_title(snapshot: dict | None, item_id: str) -> str:
    if isinstance(snapshot, dict):
        for item in snapshot.get("products", []):
            if isinstance(item, dict) and str(item.get("id")) == item_id:
                return str(item.get("title") or "未命名商品")[:160]
    return "未命名商品"


def merge_material_products(existing: object, deliveries: list[dict], snapshot: dict | None) -> dict:
    """Replace only simple material entries and retain member-only legacy types."""
    if existing is not None and not isinstance(existing, dict):
        raise AutomationValidationError("商品配置文件格式无效，未重置原文件")
    existing_types = existing.get("types", []) if isinstance(existing, dict) else []
    if not isinstance(existing_types, list):
        raise AutomationValidationError("商品配置 types 格式无效，未重置原文件")
    retained = []
    sources = {}
    material_ids = {item["item_id"] for item in deliveries}
    for raw in existing_types:
        if not isinstance(raw, dict):
            raise AutomationValidationError("商品配置条目格式无效，未重置原文件")
        delivery = raw.get("delivery")
        ids = raw.get("item_ids")
        if not isinstance(ids, list):
            ids = [raw["item_id"]] if raw.get("item_id") is not None else []
        ids = [str(item).strip() for item in ids]
        for item_id in ids:
            if item_id in material_ids:
                sources.setdefault(item_id, raw)
        if delivery == "material":
            # This is the manual full material-list save: omitted material
            # bindings are removed, but unrelated advanced bindings are not.
            continue
        if any(item in material_ids for item in ids):
            preserved = [item_id for item_id in ids if item_id not in material_ids]
            if preserved:
                remainder = copy.deepcopy(raw)
                remainder["item_ids"] = preserved
                remainder.pop("item_id", None)
                retained.append(remainder)
            continue
        retained.append(raw)
    for item in deliveries:
        retained.append(_material_product_entry(sources.get(item["item_id"]), item["item_id"], item["material"], item["enabled"], snapshot))
    return {**{key: value for key, value in (existing or {}).items() if key != "_ops_receipt"},
            "version": (existing or {}).get("version", 1), "types": retained}


def deliveries_from_products(products: object, snapshot: dict | None) -> list[dict]:
    """Build the UI-safe material list; never return redeem/pan payloads."""
    if not isinstance(products, dict) or not isinstance(products.get("types"), list):
        return []
    allowed = _snapshot_ids(snapshot)
    output = []
    seen: set[str] = set()
    for raw in products["types"]:
        if not isinstance(raw, dict) or raw.get("delivery") != "material":
            continue
        ids = raw.get("item_ids")
        if not isinstance(ids, list):
            ids = [raw.get("item_id")]
        payload = raw.get("payload", raw.get("material", ""))
        if not isinstance(payload, str) or not payload.strip():
            continue
        for value in ids:
            item_id = str(value).strip()
            if item_id not in allowed or item_id in seen:
                continue
            seen.add(item_id)
            output.append(
                {
                    "item_id": item_id,
                    "enabled": raw.get("enabled", True) is True,
                    "delivery": "material",
                    "material": payload[:MAX_MATERIAL_CHARS],
                }
            )
    return output
