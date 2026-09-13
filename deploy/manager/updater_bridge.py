"""Narrow adapter to the existing privileged updater implementation."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable
from pathlib import Path

from .constants import (
    API_SERVICE,
    CONSUMER_SERVICE,
    CURRENT_LINK,
    MANAGER_CURRENT_LINK,
    MANAGER_RELEASES_DIR,
    PUBLIC_KEY_RELATIVE,
    RELEASES_DIR,
    STATE_DIR,
    UPDATE_QUEUE_DIR,
    UPDATER_STATE_DIR,
)
from .errors import ManagerError
from .runtime import resource_root


_INTERNAL_ARGUMENTS = {
    "consume-intent": (),
    "initialize": ("--initialize",),
    "resume": (),
}


def updater_arguments(action: str, arguments: tuple[Path, ...] = ()) -> tuple[str, ...]:
    if action == "import-baseline":
        if len(arguments) != 3:
            raise ManagerError("manager_internal_invalid")
        return ("--import-trusted-baseline", *(str(path) for path in arguments))
    if arguments or action not in _INTERNAL_ARGUMENTS:
        raise ManagerError("manager_internal_invalid")
    return _INTERNAL_ARGUMENTS[action]


def _load_updater():
    try:
        return importlib.import_module("deploy.updater.updater")
    except (ImportError, OSError) as exc:
        raise ManagerError("manager_updater_unavailable") from exc


def invoke_updater(
    action: str,
    arguments: tuple[Path, ...] = (),
    *,
    loader: Callable[[], object] = _load_updater,
) -> dict:
    argv = updater_arguments(action, arguments)
    root = resource_root()
    environment = {
        "SAAS_CURRENT_ROOT": str(CURRENT_LINK),
        "SAAS_CURRENT_LINK": str(CURRENT_LINK),
        "SAAS_RELEASES_DIR": str(RELEASES_DIR),
        "SAAS_STATE_DIR": str(STATE_DIR),
        "SAAS_DB": str(STATE_DIR / "saas.db"),
        "SAAS_TENANTS_DIR": str(STATE_DIR / "tenants"),
        "SAAS_UPDATE_STAGING_DIR": str(STATE_DIR / "update-staging"),
        "SAAS_UPDATE_INTENT_FILE": str(UPDATE_QUEUE_DIR / "intent.json"),
        "SAAS_UPDATE_STATUS_DIR": str(UPDATE_QUEUE_DIR / "status"),
        "SAAS_UPDATER_STATE_DIR": str(UPDATER_STATE_DIR),
        "SAAS_UPDATE_LOCK_FILE": str(UPDATER_STATE_DIR / "updater.lock"),
        "SAAS_UPDATE_BACKUP_DIR": str(UPDATER_STATE_DIR / "backups"),
        "SAAS_UPDATE_APP_UID": "",
        "SAAS_API_SERVICE": API_SERVICE,
        "SAAS_CONSUMER_SERVICE": CONSUMER_SERVICE,
        "SAAS_UPDATE_HEALTH_BASE_URL": "http://127.0.0.1:8096/",
        "SAAS_UPDATE_PUBLIC_BASE_URL": "http://127.0.0.1:8096/xianyu-saas/",
        "SAAS_UPDATE_INTENT_MAX_AGE_SECONDS": "900",
        "SAAS_RELEASE_KIND": "standalone",
        "SAAS_MANAGER_RELEASES_DIR": str(MANAGER_RELEASES_DIR),
        "SAAS_MANAGER_CURRENT": str(MANAGER_CURRENT_LINK),
        "SAAS_MANAGER_EXECUTABLE": str(MANAGER_CURRENT_LINK / "xianyu-saas"),
        "SAAS_UPDATE_PUBLIC_KEY_FILE": str(root / PUBLIC_KEY_RELATIVE),
        "SAAS_UPDATER_BUNDLE_ROOT": str(root),
        "SAAS_UPDATER_ENTRYPOINT": str(root / "deploy/updater/updater.py"),
        "SAAS_TESTING": "0",
    }
    previous_argv = sys.argv
    previous_environment = {name: os.environ.get(name) for name in environment}
    try:
        os.environ.update(environment)
        sys.argv = ["xianyu-saas-manager internal " + action, *argv]
        updater = loader()
        result = updater.main()
    except ManagerError:
        raise
    except Exception as exc:
        raise ManagerError("manager_updater_failed") from exc
    finally:
        sys.argv = previous_argv
        for name, value in previous_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    if result != 0:
        raise ManagerError("manager_updater_failed")
    return {"ok": True, "action": action, "updater_exit_code": result}
