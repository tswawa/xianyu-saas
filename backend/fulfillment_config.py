"""Strict, account-local fulfillment configuration shared by manual APIs and tools.

Only products_config.json is written here. Resources and redeem inventory are
read-only inputs; template updates never mutate their source resources.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
from urllib.parse import urlparse

from account_storage import AccountStorageError, normalize_account_key
from automation import AutomationValidationError, _clean_text, _safe_item_id, MAX_MATERIAL_CHARS

PRODUCTS_FILE = "products_config.json"
RECEIPT_FIELD = "_ops_receipt"
READ_FILES = frozenset({PRODUCTS_FILE, "shop_snapshot.json", "reply_rules.json", "pan_links.json", "redeem_codes.json", "card_pool.json", "delivery_state.db"})
MAX_FILE_BYTES = 32 * 1024 * 1024  # Explicit I/O protection, never a product-count quota.


class FulfillmentConfigError(ValueError):
    def __init__(self, code="invalid_arguments", message=None, status_code=400):
        self.code, self.status_code = code, status_code
        super().__init__(message or {"storage_unavailable": "配置文件不可安全读取，请在手动页面核对，未重置原文件", "revision_conflict": "配置已变化，请重新读取后操作", "clarification_required": "缺少可用资料或明确的覆盖范围，请补充说明", "invalid_arguments": "履约配置字段无效"}.get(code, "履约配置未保存"))


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()


def without_receipt(document):
    return {key: value for key, value in document.items() if key != RECEIPT_FIELD} if isinstance(document, dict) else document


def validate_receipt(document):
    if not isinstance(document, dict) or RECEIPT_FIELD not in document:
        return
    receipt = document[RECEIPT_FIELD]
    if (not isinstance(receipt, dict) or set(receipt) != {"plan_id", "item_id", "digest", "after_hash"}
            or any(not isinstance(receipt[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", receipt[key]) for key in ("plan_id", "item_id"))
            or any(not isinstance(receipt[key], str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[key]) for key in ("digest", "after_hash"))
            or digest(without_receipt(document)) != receipt["after_hash"]):
        raise FulfillmentConfigError("storage_unavailable", status_code=503)


def strict_json(text):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def constant(_value):
        raise ValueError("non-finite JSON number")
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc


def account_path(storage, user_id, account_key, name, item_id=None):
    """Resolve only fixed business files, rejecting symlinks and Windows junctions."""
    try:
        key = normalize_account_key(account_key)
        if name == "ai_knowledge":
            path = storage.account_dir(user_id, key) / name / (_safe_item_id(item_id) + ".json")
        elif name in READ_FILES:
            path = storage.account_dir(user_id, key) / name
        else:
            raise ValueError("file is not allowed")
        current = storage.root
        for part in path.relative_to(storage.root).parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                    or (current != path and not stat.S_ISDIR(info.st_mode))):
                raise ValueError("unsafe path")
        return path
    except (OSError, ValueError, AccountStorageError, AutomationValidationError) as exc:
        raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc


def read_json(path):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError("unsafe file size or type")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("file size exceeds safe I/O bound")
        result = strict_json(data.decode("utf-8"))
        if result is None:
            # Only FileNotFoundError means an absent document; JSON null is corrupt configuration.
            raise ValueError("null configuration document")
        validate_receipt(result)
        return result
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as exc:
        raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def item_ids(raw):
    values = raw.get("item_ids")
    if values is None:
        values = [raw["item_id"]] if raw.get("item_id") is not None else []
    if not isinstance(values, list):
        raise FulfillmentConfigError("storage_unavailable", status_code=503)
    try:
        clean = [_safe_item_id(value) for value in values]
    except AutomationValidationError as exc:
        raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc
    if len(set(clean)) != len(clean):
        raise FulfillmentConfigError("storage_unavailable", status_code=503)
    return clean


def products_document(value):
    if value is None:
        return {"version": 1, "types": []}
    if not isinstance(value, dict) or not isinstance(value.get("types"), list):
        raise FulfillmentConfigError("storage_unavailable", status_code=503)
    validate_receipt(value)
    for entry in value["types"]:
        if not isinstance(entry, dict) or entry.get("delivery") not in {"material", "pan", "redeem"} or type(entry.get("enabled", True)) is not bool:
            raise FulfillmentConfigError("storage_unavailable", status_code=503)
        item_ids(entry)
    return copy.deepcopy(value)


def normalise_template_item_ids(raw, snapshot, preserved_item_ids=None):
    if not isinstance(raw, dict):
        raise FulfillmentConfigError()
    try:
        selected = item_ids(raw)
    except FulfillmentConfigError as exc:
        raise FulfillmentConfigError() from exc
    available = {str(row.get("id")) for row in (snapshot or {}).get("products", []) if isinstance(row, dict)}
    preserved = {str(value) for value in (preserved_item_ids or [])} if (snapshot or {}).get("truncated") else set()
    if set(selected) - available - preserved:
        raise FulfillmentConfigError(message="只能绑定当前店铺已识别的商品")
    return selected


def normalise_template_input(raw, snapshot, preserved_item_ids=None):
    if not isinstance(raw, dict):
        raise FulfillmentConfigError()
    try:
        name = _clean_text(raw.get("name"), limit=120, field="模板名称", allow_newlines=False)
    except AutomationValidationError as exc:
        raise FulfillmentConfigError(message=str(exc)) from exc
    delivery = raw.get("delivery")
    if delivery not in {"redeem", "pan"}:
        raise FulfillmentConfigError(message="delivery 只能是 redeem/pan")
    enabled = raw.get("enabled", True)
    enabled = True if enabled is None else enabled
    if type(enabled) is not bool:
        raise FulfillmentConfigError()
    result = {"name": name, "delivery": delivery, "enabled": enabled,
              "item_ids": normalise_template_item_ids(raw, snapshot, preserved_item_ids)}
    for key, limit in (("description", 500), ("price", 120)):
        if raw.get(key) is not None:
            value = raw[key]
            if not isinstance(value, str) or len(value.strip()) > limit or "\x00" in value:
                raise FulfillmentConfigError()
            result[key] = value.strip()
    if delivery == "pan":
        tags = raw.get("resource_match")
        if not isinstance(tags, list) or not tags or len(tags) > 32 or any(not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 120 for tag in tags):
            raise FulfillmentConfigError(message="网盘模板必须配置有效资源匹配标签")
        result["resource_match"] = list(dict.fromkeys(tag.strip() for tag in tags))
    return result


def template_public(item):
    result = {key: copy.deepcopy(item[key]) for key in ("id", "name", "description", "price", "delivery", "resource_match") if key in item}
    result.update(item_ids=item_ids(item), enabled=item.get("enabled", True))
    result["item_count"] = len(result["item_ids"])
    if item.get("payload") or item.get("material"):
        result["payload_set"] = True
    return result


def upsert_template(document, raw, snapshot, *, new_id):
    result = without_receipt(products_document(document))
    template_id = str(raw.get("id") or "").strip()
    matches = [pos for pos, row in enumerate(result["types"]) if row.get("delivery") in {"redeem", "pan"} and str(row.get("id") or "") == template_id]
    if len(matches) > 1:
        raise FulfillmentConfigError("clarification_required")
    position = matches[0] if matches else None
    previous = result["types"][position] if position is not None else {}
    clean = normalise_template_input(raw, snapshot, item_ids(previous))
    saved = {**previous, **clean, "id": template_id if position is not None else new_id}
    # The manual endpoint treats omission/null of these known optional fields
    # as clearing them. Preserve only unknown extensions by default here.
    for field in ("description", "price"):
        if field not in clean:
            saved.pop(field, None)
    saved.pop("item_id", None)
    if saved["delivery"] != "pan":
        saved.pop("resource_match", None)
    targets = set(saved["item_ids"])
    if any(targets.intersection(item_ids(row)) for index, row in enumerate(result["types"]) if index != position):
        raise FulfillmentConfigError("clarification_required", "商品已绑定其他模板，请明确处理绑定冲突")
    if position is None:
        result["types"].append(saved)
    else:
        result["types"][position] = saved
    return result, saved


def remove_template(document, template_id):
    result = without_receipt(products_document(document))
    retained = [row for row in result["types"] if not (row.get("delivery") in {"redeem", "pan"} and str(row.get("id") or "") == template_id)]
    if len(retained) == len(result["types"]):
        raise FulfillmentConfigError("not_found", "发货模板不存在", 404)
    result["types"] = retained
    return result


def target_config(document, item_id):
    matches = [row for row in products_document(document)["types"] if item_id in item_ids(row)]
    if len(matches) > 1:
        raise FulfillmentConfigError("clarification_required", "商品同时绑定多份配置，请先核对绑定冲突")
    return copy.deepcopy(matches[0]) if matches else None


def configure_target(document, item_id, title, delivery, *, enabled, stable_id, material=None, resource=None, replace_existing=False):
    """Copy-on-write one product binding; preserve non-target mappings and metadata."""
    result = without_receipt(products_document(document))
    old = target_config(result, item_id)
    if type(enabled) is not bool or delivery not in {"material", "pan", "redeem"}:
        raise FulfillmentConfigError()
    if old and old["delivery"] != delivery and not replace_existing:
        raise FulfillmentConfigError("clarification_required", "商品已有不同类型的发货配置，请明确是否替换")
    saved = copy.deepcopy(old or {})
    identifier = old.get("id") if old and len(item_ids(old)) == 1 else stable_id
    saved.update(id=identifier or stable_id, name=saved.get("name") or title, item_ids=[item_id], delivery=delivery, enabled=enabled)
    saved.pop("item_id", None)
    # Replacing the type does not delete unknown extensions or price metadata.
    if delivery != "pan":
        saved.pop("resource_match", None)
    if delivery != "material":
        saved.pop("payload", None)
        saved.pop("material", None)
    if delivery == "material":
        if material is None and resource and resource.get("delivery") == "material":
            material = resource.get("material")
        if material is None and old and old["delivery"] == delivery:
            material = old.get("payload", old.get("material"))
        try:
            saved["payload"] = _clean_text(material, limit=MAX_MATERIAL_CHARS, field="自动发送资料")
        except AutomationValidationError as exc:
            raise FulfillmentConfigError("clarification_required", "请提供明确的文字资料，或绑定已有文字资料资源") from exc
        saved.pop("material", None)
    elif delivery == "pan":
        if not resource or resource.get("delivery") != "pan" or not resource.get("available"):
            if not (not enabled and old and old["delivery"] == "pan"):
                raise FulfillmentConfigError("clarification_required", "未找到可绑定的网盘资料，请选择现有资源或通过手动页面补充")
        else:
            saved["resource_match"] = list(resource["tags"])
    elif not resource or resource.get("delivery") != "redeem" or not resource.get("available"):
        if not (not enabled and old and old["delivery"] == "redeem"):
            raise FulfillmentConfigError("clarification_required", "当前没有可用卡密库存，请在手动页面补充库存")
    if old:
        comparable = {**saved, "id": old.get("id")}
        original = {**old, "id": old.get("id"), "item_ids": [item_id], "enabled": old.get("enabled", True)}
        original.pop("item_id", None)
        if comparable == original:
            # An unchanged setting does not need a new binding, even when the
            # source template is shared. Split only for an actual target edit.
            return result, old
    retained = []
    for row in result["types"]:
        ids = item_ids(row)
        if item_id not in ids:
            retained.append(row)
        elif len(ids) > 1:
            remainder = copy.deepcopy(row)
            remainder["item_ids"] = [value for value in ids if value != item_id]
            remainder.pop("item_id", None)
            retained.append(remainder)
    retained.append(saved)
    result["types"] = retained
    return result, saved


class FulfillmentConfig:
    def __init__(self, storage):
        self.storage = storage

    def read_products(self, user_id, account_key):
        return products_document(read_json(account_path(self.storage, user_id, account_key, PRODUCTS_FILE)))

    def write_products(self, user_id, account_key, document, *, expected_revision=None, ensure_current=None, receipt=None):
        clean = without_receipt(products_document(document))
        if receipt is not None:
            clean[RECEIPT_FIELD] = copy.deepcopy(receipt)
            validate_receipt(clean)
        if ensure_current is not None and ensure_current() is False:
            raise FulfillmentConfigError("revision_conflict", status_code=409)
        current = self.read_products(user_id, account_key)
        if expected_revision is not None and digest(current) != expected_revision:
            raise FulfillmentConfigError("revision_conflict", status_code=409)
        path = account_path(self.storage, user_id, account_key, PRODUCTS_FILE)
        self.storage.ensure_account_dir(user_id, account_key)
        if ensure_current is not None and ensure_current() is False:
            raise FulfillmentConfigError("revision_conflict", status_code=409)
        if expected_revision is not None and digest(self.read_products(user_id, account_key)) != expected_revision:
            raise FulfillmentConfigError("revision_conflict", status_code=409)
        self.storage.atomic_write_path(path, json_text(clean).encode("utf-8"))
        return clean

    def resources(self, user_id, account_key):
        """Private descriptors. Tools expose an explicit safe projection, not this list."""
        root_args = (self.storage, user_id, account_key)
        products = self.read_products(user_id, account_key)
        pan = read_json(account_path(*root_args, "pan_links.json"))
        if pan is not None and (not isinstance(pan, dict) or not isinstance(pan.get("links"), list)):
            raise FulfillmentConfigError("storage_unavailable", status_code=503)
        groups = {}
        for entry in (pan or {}).get("links", []):
            if not isinstance(entry, dict):
                raise FulfillmentConfigError("storage_unavailable", status_code=503)
            if entry.get("used") is True:
                continue
            url, code, remark, tags = (entry.get(key) for key in ("url", "code", "remark", "match"))
            try:
                parsed = urlparse(url)
                valid = (isinstance(url, str) and len(url) <= 2048 and parsed.scheme == "https" and parsed.hostname and not parsed.username
                         and isinstance(code, str) and 0 < len(code.strip()) <= 64 and isinstance(remark, str) and 0 < len(remark.strip()) <= 512
                         and isinstance(tags, list) and tags and all(isinstance(tag, str) and tag.strip() for tag in tags))
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise FulfillmentConfigError("storage_unavailable", status_code=503)
            key = tuple(sorted(set(tag.strip() for tag in tags)))
            groups.setdefault(key, []).append(entry)
        resources = []
        for tags, entries in sorted(groups.items()):
            resources.append({"key": "pan:" + digest(tags), "kind": "pan", "delivery": "pan", "name": " / ".join(entry["remark"] for entry in entries),
                              "tags": list(tags), "available": True, "count": len(entries), "version": digest(entries)})
        codes = read_json(account_path(*root_args, "redeem_codes.json"))
        if codes is not None and (not isinstance(codes, list) or any(not isinstance(row, dict) or not isinstance(row.get("code"), str) or not row["code"] for row in codes)):
            raise FulfillmentConfigError("storage_unavailable", status_code=503)
        meta = read_json(account_path(*root_args, "card_pool.json"))
        if meta is not None and (not isinstance(meta, dict) or not isinstance(meta.get("name", "兑换码池"), str)):
            raise FulfillmentConfigError("storage_unavailable", status_code=503)
        if codes is not None:
            count = sum(not bool(row.get("used")) for row in codes)
            counts = self._inventory_counts(user_id, account_key)
            if counts is not None:
                count = counts.get("available", 0)
            resources.append({"key": "inventory:redeem", "kind": "inventory", "delivery": "redeem", "name": (meta or {}).get("name", "兑换码池"),
                              "tags": [], "available": count > 0, "count": count, "version": digest([codes, counts, meta])})
        for position, row in enumerate(products["types"]):
            delivery = row["delivery"]
            resource = {"key": f"template:{position}", "kind": "template", "delivery": delivery, "name": row.get("name", "已有发货模板"),
                        "tags": row.get("resource_match", []) if delivery == "pan" else [], "version": digest(row), "available": True}
            if delivery == "material":
                resource["material"] = row.get("payload", row.get("material", ""))
                resource["available"] = isinstance(resource["material"], str) and bool(resource["material"].strip())
            elif delivery == "pan":
                group = groups.get(tuple(sorted(set(resource["tags"]))))
                resource["available"] = bool(group)
                resource["version"] = digest([row, group])
            else:
                pool = next((item for item in resources if item["kind"] == "inventory"), None)
                resource["available"] = bool(pool and pool["available"])
                resource["version"] = digest([row, pool])
            resources.append(resource)
        return resources

    def _inventory_counts(self, user_id, account_key):
        path = account_path(self.storage, user_id, account_key, "delivery_state.db")
        if not path.exists():
            return None
        con = None
        try:
            # Read-only SQLite; never create/initialize the Worker's private DB.
            con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory'").fetchone()
            if not exists:
                return None
            return {str(row[0]): int(row[1]) for row in con.execute("SELECT status,COUNT(*) FROM inventory WHERE kind='redeem' GROUP BY status")}
        except sqlite3.Error as exc:
            raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc
        finally:
            if con is not None:
                con.close()
