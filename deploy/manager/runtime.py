"""Runtime admission and self-integrity checks."""

from __future__ import annotations

import base64
import hashlib
import os
import platform
import stat
import sys
from pathlib import Path

from backend.version import VERSION as MANAGER_VERSION

from .constants import MANAGER_PROTOCOL, PUBLIC_KEY_RELATIVE, SYSTEMCTL_BINARY, UPDATER_PROTOCOL
from .errors import ManagerError


ARCHITECTURES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}
MAX_PUBLIC_KEY_BYTES = 4096
MAX_MANAGER_BYTES = 512 * 1024 * 1024


def require_linux(platform_name: str | None = None) -> None:
    if (sys.platform if platform_name is None else platform_name) != "linux":
        raise ManagerError("manager_linux_required")


def require_root(euid: int | None = None) -> None:
    if euid is None:
        getter = getattr(os, "geteuid", None)
        euid = getter() if getter is not None else -1
    if euid != 0:
        raise ManagerError("manager_root_required")


def map_architecture(machine: str | None = None) -> str:
    normalized = (platform.machine() if machine is None else machine).strip().lower()
    try:
        return ARCHITECTURES[normalized]
    except KeyError as exc:
        raise ManagerError("manager_architecture_unsupported") from exc


def resource_root(meipass: str | os.PathLike[str] | None = None) -> Path:
    frozen_root = meipass if meipass is not None else getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root).resolve()
    return Path(__file__).resolve().parents[2]


def embedded_public_key_path(
    *, meipass: str | os.PathLike[str] | None = None, source_root: Path | None = None
) -> Path:
    root = source_root.resolve() if source_root is not None else resource_root(meipass)
    candidates = (root / PUBLIC_KEY_RELATIVE, root / "update-signing.pub")
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise ManagerError("manager_public_key_missing")


def _validate_public_key(raw: bytes) -> None:
    if not raw or len(raw) > MAX_PUBLIC_KEY_BYTES or b"\x00" in raw:
        raise ManagerError("manager_public_key_invalid")
    stripped = raw.strip()
    try:
        if stripped.startswith(b"-----BEGIN PUBLIC KEY-----"):
            lines = stripped.splitlines()
            if lines[0] != b"-----BEGIN PUBLIC KEY-----" or lines[-1] != b"-----END PUBLIC KEY-----":
                raise ValueError
            payload = b"".join(lines[1:-1])
            decoded = base64.b64decode(payload, validate=True)
            if len(decoded) < 32 or len(decoded) > 128:
                raise ValueError
        else:
            decoded = base64.b64decode(stripped, validate=True)
            if len(decoded) != 32:
                raise ValueError
    except (ValueError, base64.binascii.Error) as exc:
        raise ManagerError("manager_public_key_invalid") from exc


def read_embedded_public_key(
    *, meipass: str | os.PathLike[str] | None = None, source_root: Path | None = None
) -> bytes:
    path = embedded_public_key_path(meipass=meipass, source_root=source_root)
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > MAX_PUBLIC_KEY_BYTES:
            raise ManagerError("manager_public_key_invalid")
        raw = path.read_bytes()
    except ManagerError:
        raise
    except OSError as exc:
        raise ManagerError("manager_public_key_missing") from exc
    _validate_public_key(raw)
    return raw


def manager_file_path(path: Path | None = None) -> Path:
    if path is not None:
        return path.resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def manager_sha256(path: Path | None = None) -> str:
    target = manager_file_path(path)
    try:
        before = target.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= MAX_MANAGER_BYTES:
            raise ManagerError("manager_self_hash_failed")
        digest = hashlib.sha256()
        with target.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
        after = target.stat(follow_symlinks=False)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_nlink, value.st_size, value.st_mtime_ns)
        if identity(before) != identity(after):
            raise ManagerError("manager_self_hash_failed")
        return digest.hexdigest()
    except ManagerError:
        raise
    except OSError as exc:
        raise ManagerError("manager_self_hash_failed") from exc


def self_check(
    *,
    platform_name: str | None = None,
    machine: str | None = None,
    euid: int | None = None,
    meipass: str | os.PathLike[str] | None = None,
    source_root: Path | None = None,
    executable: Path | None = None,
) -> dict:
    require_linux(platform_name)
    architecture = map_architecture(machine)
    key = read_embedded_public_key(meipass=meipass, source_root=source_root)
    path = manager_file_path(executable)
    return {
        "ok": True,
        "version": MANAGER_VERSION,
        "manager_protocol": MANAGER_PROTOCOL,
        "updater_protocol": UPDATER_PROTOCOL,
        "architecture": architecture,
        "root": (getattr(os, "geteuid", lambda: -1)() if euid is None else euid) == 0,
        "executable": str(path),
        "sha256": manager_sha256(path),
        "public_key_sha256": hashlib.sha256(key).hexdigest(),
        "systemctl": str(SYSTEMCTL_BINARY),
    }
