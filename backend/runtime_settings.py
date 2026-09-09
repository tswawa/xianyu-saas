"""Small, versioned policy for shop creation and supervised Worker resources.

Saved database values override environment defaults. Nothing here restarts a
Worker, changes Docker settings, or imports the API/process supervisor.
"""
from __future__ import annotations

import math
import os


LIMIT_BOUNDS = {
    "max_shop_accounts": (1, 1000),
    "max_running_workers": (1, 1000),
    "worker_memory_mib": (128, 16384),
}
DEFAULTS = {"max_shop_accounts": 20, "max_running_workers": 15, "worker_memory_mib": 400}
ENVIRONMENT_KEYS = {"max_running_workers": "SAAS_MAX_BOTS", "worker_memory_mib": "SAAS_BOT_MEM_MB"}
LABELS = {"max_shop_accounts": "可添加店铺数", "max_running_workers": "同时运行店铺数", "worker_memory_mib": "单店内存上限"}


class RuntimeSettingsError(ValueError):
    def __init__(self, code="invalid_resource_settings", status_code=400, message=None):
        self.code, self.status_code = code, status_code
        super().__init__(message or {
            "invalid_resource_settings": "运行限制格式无效",
            "resource_settings_unavailable": "运行限制暂时无法读取，请检查设置后重试",
            "resource_revision_conflict": "运行限制已被修改，请刷新后再保存",
            "shop_limit_reached": "已达到可添加店铺数量上限，请在设置中调整限制",
            "admin_required": "仅管理员可以修改全局运行限制",
        }.get(code, "运行限制操作未完成"))

    def public_detail(self):
        return {"code": self.code, "message": str(self)}


def validate_limits(values):
    if not isinstance(values, dict) or set(values) != set(LIMIT_BOUNDS):
        raise RuntimeSettingsError()
    result = {}
    for field, (minimum, maximum) in LIMIT_BOUNDS.items():
        value = values[field]
        if type(value) is not int or not minimum <= value <= maximum:
            raise RuntimeSettingsError(message=f"{LABELS[field]}必须是 {minimum} 至 {maximum} 的整数")
        result[field] = value
    return result


def environment_defaults(environ=None):
    source = os.environ if environ is None else environ
    values = dict(DEFAULTS)
    try:
        for field, key in ENVIRONMENT_KEYS.items():
            if key in source:
                raw = str(source[key]).strip()
                if not raw.isascii() or not raw.isdecimal():
                    raise ValueError("invalid environment integer")
                values[field] = int(raw)
        return validate_limits(values)
    except (ValueError, TypeError) as exc:
        # Do not expose raw environment values and never fall back to unlimited.
        raise RuntimeSettingsError("resource_settings_unavailable", 503) from exc


def settings_payload(row, environ=None):
    if row is None:
        return {**environment_defaults(environ), "revision": 0, "source": "environment", "updated_at": None}
    try:
        values = validate_limits({field: row[field] for field in LIMIT_BOUNDS})
        revision = row["revision"]
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid revision")
        updated_at = float(row["updated_at"])
        if not math.isfinite(updated_at) or updated_at < 0:
            raise ValueError("invalid settings timestamp")
        return {**values, "revision": revision, "source": "saved", "updated_at": updated_at}
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise RuntimeSettingsError("resource_settings_unavailable", 503) from exc


class RuntimeSettings:
    def __init__(self, db):
        self.db = db

    def read(self):
        return self.db.get_runtime_settings()

    def save(self, user_id, *, expected_revision, max_shop_accounts, max_running_workers, worker_memory_mib, emergency_admin=False):
        values = validate_limits({"max_shop_accounts": max_shop_accounts,
            "max_running_workers": max_running_workers, "worker_memory_mib": worker_memory_mib})
        return self.db.save_runtime_settings(user_id, expected_revision, values, emergency_admin=emergency_admin)

    @staticmethod
    def bounds():
        return {key: {"min": low, "max": high} for key, (low, high) in LIMIT_BOUNDS.items()}
