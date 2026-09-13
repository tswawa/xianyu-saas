"""Fixed identities and filesystem locations for the systemd manager."""

from __future__ import annotations

from pathlib import Path


PRODUCT = "xianyu-saas"
MANAGER_PROTOCOL = 1
UPDATER_PROTOCOL = 1

INSTALL_ROOT = Path("/opt/xianyu-saas")
RELEASES_DIR = INSTALL_ROOT / "releases"
CURRENT_LINK = INSTALL_ROOT / "current"
MANAGER_ROOT = INSTALL_ROOT / "manager"
MANAGER_RELEASES_DIR = MANAGER_ROOT / "releases"
MANAGER_CURRENT_LINK = MANAGER_ROOT / "current"
MANAGER_INSTALL_PATH = Path("/usr/local/bin/xianyu-saas")
RUNTIME_DIR = INSTALL_ROOT / "runtime"
UPDATER_BUNDLE_ROOT = INSTALL_ROOT / "updater"
STATE_DIR = Path("/var/lib/xianyu-saas")
UPDATE_QUEUE_DIR = Path("/var/lib/xianyu-saas-updates")
UPDATER_STATE_DIR = Path("/var/lib/xianyu-saas-updater")
PUBLIC_KEY_RELATIVE = Path("deploy/update-signing.pub")
SYSTEMCTL_BINARY = Path("/usr/bin/systemctl")

API_SERVICE = "xianyu-saas.service"
CONSUMER_SERVICE = "xianyu-saas-consumer.service"
UPDATER_SERVICE = "xianyu-saas-updater.service"
UPDATER_PATH_UNIT = "xianyu-saas-updater.path"
APPLICATION_UNITS = (API_SERVICE, CONSUMER_SERVICE)
MANAGED_UNITS = APPLICATION_UNITS + (UPDATER_SERVICE, UPDATER_PATH_UNIT)

ROOT_COMMANDS = frozenset({
    "install",
    "start",
    "stop",
    "restart",
    "internal:consume-intent",
    "internal:initialize",
    "internal:import-baseline",
    "internal:resume",
})
