#!/usr/bin/env python3
"""Privileged, intent-driven atomic release switcher.

This process is deliberately separate from the API.  It consumes one private
intent, re-verifies the signed candidate, backs up SQLite, switches the
``current`` symlink atomically, and rolls back the code link when health checks
fail.  Runtime data is never copied into a release and is never rolled back.
"""

from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
import json
import math
import os
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urljoin

import requests
from cryptography.hazmat.primitives import serialization


SCRIPT_PATH = Path(__file__)
SCRIPT_ROOT = SCRIPT_PATH.resolve().parents[2]
_SCRIPT_LOAD_METADATA = SCRIPT_PATH.lstat()
SCRIPT_LOAD_IDENTITY = (
    _SCRIPT_LOAD_METADATA.st_dev,
    _SCRIPT_LOAD_METADATA.st_ino,
    _SCRIPT_LOAD_METADATA.st_nlink,
    _SCRIPT_LOAD_METADATA.st_uid,
    _SCRIPT_LOAD_METADATA.st_mode,
    _SCRIPT_LOAD_METADATA.st_size,
    _SCRIPT_LOAD_METADATA.st_mtime_ns,
    _SCRIPT_LOAD_METADATA.st_ctime_ns,
)
BACKEND_ROOT = SCRIPT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from db import DB  # noqa: E402
from update_maintenance import supports_maintenance_protocol  # noqa: E402
from platform_update import (  # noqa: E402
    CACHED_MANIFEST_FILE,
    CACHED_SIGNATURE_FILE,
    INTERNAL_CANDIDATE_FILES,
    MARKER_FILE,
    MAX_ARCHIVE_BYTES,
    MAX_FILE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_SIGNATURE_BYTES,
    PlatformUpdateError,
    ReleaseAsset,
    ReleaseInfo,
    SemVer,
    _asset_names,
    _read_secure_file,
    _public_key_file,
    _release_from_marker,
    _validate_release_path,
    _verify_candidate_version,
    _verify_dependency_stability,
    extract_verified_archive,
    load_public_key,
    load_verified_candidate,
    parse_manifest,
    verify_manifest_signature,
)


INTENT_MAX_BYTES = 16 * 1024
HEALTH_ATTEMPTS = 20
HEALTH_INTERVAL_SECONDS = 1.0
SERVICE_TIMEOUT_SECONDS = 90
BACKUP_KEEP = 5
RELEASE_KEEP = 3
INTENT_MAX_AGE_SECONDS = 900
INTENT_FUTURE_SKEW_SECONDS = 60
JOURNAL_MAX_BYTES = 4 * 1024 * 1024
MAINTENANCE_MODULE = "backend/update_maintenance.py"
BASELINE_IMPORT_OPTION = "--import-trusted-baseline"
SYSTEMD_UPDATER_PROTOCOL = 1
UPDATER_BUNDLE_FILES = (
    "backend/account_storage.py",
    "backend/db.py",
    "backend/docker_update_protocol.py",
    "backend/platform_update.py",
    "backend/runtime_settings.py",
    "backend/update_maintenance.py",
    "backend/version.py",
    "deploy/updater/updater.py",
)
UPDATER_BUNDLE_MAX_FILE_BYTES = 4 * 1024 * 1024
TERMINAL_PHASES = {"succeeded", "rolled_back", "failed", "recovery_failed"}
PHASES = {
    "queued", "verifying_package", "preflighting", "preparing", "stopping",
    "backing_up", "migrating", "switching", "verifying", "rolling_back",
    *TERMINAL_PHASES,
}


class UpdaterError(RuntimeError):
    def __init__(self, code: str, message: str = "update failed"):
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True)
class Config:
    releases_dir: Path
    current_link: Path
    staging_dir: Path
    state_dir: Path
    database_path: Path
    intent_file: Path
    lock_file: Path
    backup_dir: Path
    api_service: str
    consumer_service: str
    health_base_url: str
    public_base_url: str
    private_state_dir: Path | None = None
    status_dir: Path | None = None
    intent_max_age_seconds: int = INTENT_MAX_AGE_SECONDS
    intent_owner_uid: int | None = None
    tenants_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.private_state_dir is None:
            object.__setattr__(self, "private_state_dir", self.state_dir.with_name(self.state_dir.name + "-updater"))
        if self.status_dir is None:
            object.__setattr__(self, "status_dir", self.intent_file.parent / "status")
        if self.tenants_dir is None:
            object.__setattr__(self, "tenants_dir", self.state_dir / "tenants")

    @classmethod
    def from_env(cls) -> "Config":
        state = Path(os.environ.get("SAAS_STATE_DIR", "/var/lib/xianyu-saas").strip())
        intent = Path(os.environ.get("SAAS_UPDATE_INTENT_FILE", "/var/lib/xianyu-saas-updates/intent.json").strip())
        owner = os.environ.get("SAAS_UPDATE_APP_UID", "").strip()
        try:
            app_uid = int(owner) if owner else pwd.getpwnam("xianyu-saas").pw_uid
        except (ValueError, KeyError) as exc:
            raise UpdaterError("update_intent_owner_unavailable") from exc
        return cls(
            releases_dir=Path(
                os.environ.get("SAAS_RELEASES_DIR", "/opt/xianyu-saas/releases").strip()
            ),
            current_link=Path(
                os.environ.get("SAAS_CURRENT_LINK", "/opt/xianyu-saas/current").strip()
            ),
            staging_dir=Path(
                os.environ.get(
                    "SAAS_UPDATE_STAGING_DIR", str(state / "update-staging")
                ).strip()
            ),
            state_dir=state,
            database_path=Path(
                os.environ.get("SAAS_DB", str(state / "saas.db")).strip()
            ),
            intent_file=intent,
            lock_file=Path(
                os.environ.get("SAAS_UPDATE_LOCK_FILE", str(Path(os.environ.get("SAAS_UPDATER_STATE_DIR", str(state.with_name(state.name + "-updater")))) / "updater.lock")).strip()
            ),
            backup_dir=Path(
                os.environ.get("SAAS_UPDATE_BACKUP_DIR", str(Path(os.environ.get("SAAS_UPDATER_STATE_DIR", str(state.with_name(state.name + "-updater")))) / "backups")).strip()
            ),
            api_service=os.environ.get(
                "SAAS_API_SERVICE", "xianyu-saas.service"
            ).strip(),
            consumer_service=os.environ.get(
                "SAAS_CONSUMER_SERVICE", "xianyu-saas-consumer.service"
            ).strip(),
            health_base_url=os.environ.get(
                "SAAS_UPDATE_HEALTH_BASE_URL", "http://127.0.0.1:8096/"
            ).strip(),
            public_base_url=os.environ.get(
                "SAAS_UPDATE_PUBLIC_BASE_URL", "http://127.0.0.1/xianyu-saas/"
            ).strip(),
            private_state_dir=Path(os.environ.get("SAAS_UPDATER_STATE_DIR", str(state.with_name(state.name + "-updater"))).strip()),
            status_dir=Path(os.environ.get("SAAS_UPDATE_STATUS_DIR", str(intent.parent / "status")).strip()),
            intent_max_age_seconds=int(os.environ.get("SAAS_UPDATE_INTENT_MAX_AGE_SECONDS", str(INTENT_MAX_AGE_SECONDS))),
            intent_owner_uid=app_uid,
            tenants_dir=Path(os.environ.get("SAAS_TENANTS_DIR", str(state / "tenants")).strip()),
        )


@dataclass(frozen=True)
class Intent:
    action: str
    version: str
    channel: str
    candidate_path: str
    manifest_sha256: str
    requested_by: int
    nonce: str
    requested_at: float = 0.0
    operation_id: str = ""
    expected_current_version: str = ""

    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", self.nonce)
        if not self.requested_at:
            object.__setattr__(self, "requested_at", time.time())


class SystemRunner:
    def run(self, command: list[str], *, timeout: int = SERVICE_TIMEOUT_SECONDS) -> None:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=_child_environment(),
        )
        if completed.returncode != 0:
            raise UpdaterError("update_service_command_failed")


class HealthClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False

    def get(self, url: str, *, timeout: float = 5.0):
        return self.session.get(url, timeout=timeout, allow_redirects=False)


def _child_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Pass only non-secret process settings to systemctl and migrations."""
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if extra:
        environment.update({str(key): str(value) for key, value in extra.items()})
    return environment


def _absolute(path: Path, code: str) -> Path:
    if not path.is_absolute():
        raise UpdaterError(code)
    return path


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_layout(config: Config) -> None:
    paths = (
        config.releases_dir,
        config.current_link,
        config.staging_dir,
        config.state_dir,
        config.database_path,
        config.intent_file,
        config.lock_file,
        config.backup_dir,
        config.private_state_dir,
        config.status_dir,
        config.tenants_dir,
    )
    for path in paths:
        _absolute(path, "update_layout_invalid")
    releases = config.releases_dir.resolve(strict=False)
    current_parent = config.current_link.parent.resolve(strict=False)
    state = config.state_dir.resolve(strict=False)
    staging = config.staging_dir.resolve(strict=False)
    database = config.database_path.resolve(strict=False)
    intent = config.intent_file.resolve(strict=False)
    backups = config.backup_dir.resolve(strict=False)
    if releases == state or _within(state, releases) or _within(releases, state):
        raise UpdaterError("update_state_release_overlap")
    if any(_within(path, releases) for path in (staging, database, intent, backups)):
        raise UpdaterError("update_state_release_overlap")
    if _within(releases, staging):
        raise UpdaterError("update_staging_release_overlap")
    if current_parent != releases.parent:
        raise UpdaterError("update_current_layout_invalid")
    if config.current_link.name == "releases":
        raise UpdaterError("update_current_layout_invalid")
    if (config.api_service, config.consumer_service) != ("xianyu-saas.service", "xianyu-saas-consumer.service"):
        raise UpdaterError("update_service_name_invalid")
    private = config.private_state_dir.resolve(strict=False)
    public = config.status_dir.resolve(strict=False)
    if any(_within(private, root) or _within(root, private) for root in (state, releases, staging, public)):
        raise UpdaterError("update_private_layout_invalid")
    if _within(public, releases) or _within(public, staging):
        raise UpdaterError("update_status_layout_invalid")
    if not 1 <= config.intent_max_age_seconds <= 86400:
        raise UpdaterError("update_intent_expiry_invalid")
    if config.intent_owner_uid is not None and config.intent_owner_uid < 0:
        raise UpdaterError("update_intent_owner_unavailable")
    tenants = config.tenants_dir.resolve(strict=False)
    if tenants == Path(tenants.anchor) or state == Path(state.anchor):
        raise UpdaterError("update_runtime_layout_invalid")
    if any(_within(state, root) for root in (staging, intent.parent, public, backups)):
        raise UpdaterError("update_runtime_layout_invalid")
    if any(_within(tenants, root) or _within(root, tenants) for root in (private, releases, staging, public)):
        raise UpdaterError("update_runtime_layout_invalid")


def _directory_fd(path: Path, *, create: bool = False, mode: int = 0o700, owner_uid: int | None = None) -> int:
    """Walk with openat/O_NOFOLLOW; never trust a resolved pathname alone.

    Production runs as root. Non-root unit tests use their real UID, never fake
    fcntl or permission results. Writable root-owned sticky ancestors are safe
    for root-owned children; app-owned ancestors are not safe for public status.
    """
    _absolute(path, "update_directory_invalid")
    if ".." in path.parts:
        raise UpdaterError("update_directory_invalid")
    allowed = {0, os.geteuid()}
    if owner_uid is not None:
        allowed.add(owner_uid)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            sticky = bool(metadata.st_mode & stat.S_ISVTX) and metadata.st_uid in {0, os.geteuid()}
            if metadata.st_uid not in allowed or (metadata.st_mode & 0o022 and not sticky):
                raise UpdaterError("update_directory_untrusted")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _secure_directory(path: Path, mode: int = 0o700) -> None:
    descriptor = _directory_fd(path, create=True, mode=mode)
    try:
        if os.fstat(descriptor).st_uid != os.geteuid():
            raise UpdaterError("update_directory_untrusted")
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = _directory_fd(path)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_identity(metadata) -> tuple:
    return (metadata.st_dev, metadata.st_ino, metadata.st_nlink, metadata.st_uid,
            metadata.st_mode, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _read_protected_file(path: Path, max_bytes: int, *, private: bool, owner_uid: int | None = None) -> bytes:
    owner = os.geteuid() if owner_uid is None else owner_uid
    parent = _directory_fd(path.parent, owner_uid=owner_uid)
    descriptor = -1
    try:
        before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        forbidden = 0o077 if private else 0o022
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != owner or before.st_mode & forbidden
                or before.st_size < 0 or before.st_size > max_bytes):
            raise UpdaterError("update_intent_invalid" if private else "update_release_invalid")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        if _file_identity(before) != _file_identity(os.fstat(descriptor)):
            raise UpdaterError("update_file_changed")
        raw = bytearray()
        while len(raw) <= max_bytes:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (_file_identity(before) != _file_identity(after)
                or _file_identity(after) != _file_identity(named)
                or len(raw) != before.st_size or len(raw) > max_bytes):
            raise UpdaterError("update_file_changed")
        return bytes(raw)
    except FileNotFoundError as exc:
        raise UpdaterError("update_intent_missing" if private else "update_release_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _decode_json(raw: bytes) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, ValueError) as exc:
        raise UpdaterError("update_intent_invalid") from exc
    if not isinstance(payload, dict):
        raise UpdaterError("update_intent_invalid")
    return payload


def _read_private_json(path: Path, max_bytes: int = INTENT_MAX_BYTES, *, owner_uid: int | None = None) -> dict:
    return _decode_json(_read_protected_file(path, max_bytes, private=True, owner_uid=owner_uid))


def _atomic_json(path: Path, payload: dict, *, public: bool = False) -> None:
    parent = _directory_fd(path.parent)
    temporary = f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
            output.flush()
            os.fchmod(output.fileno(), 0o644 if public else 0o600)
            os.fsync(output.fileno())
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _prepare_private(config: Config) -> None:
    _secure_directory(config.private_state_dir)
    _secure_directory(config.private_state_dir / "journals")
    _secure_directory(config.private_state_dir / "nonces")


def acquire_lock(config: Config):
    _prepare_private(config)
    # The historical lock_file can be in app-owned state; it is not authoritative.
    path = config.private_state_dir / "updater.lock"
    parent = _directory_fd(path.parent)
    descriptor = -1
    try:
        descriptor = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.geteuid():
            raise UpdaterError("update_lock_invalid")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BaseException as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(exc, BlockingIOError):
            raise UpdaterError("update_already_running") from exc
        raise
    finally:
        os.close(parent)


def _processing_file(config: Config) -> Path:
    # Legacy input only. No execution state or recovery trust is kept here.
    return config.intent_file.with_name("intent.processing.json")


def _valid_operation_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _valid_timestamp(value: object) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _parse_intent(payload: dict) -> Intent:
    required = {"schema", "action", "version", "channel", "candidate_path", "manifest_sha256", "requested_by", "requested_at", "nonce"}
    if not required <= set(payload) <= required | {"operation_id", "expected_current_version"} or type(payload.get("schema")) is not int or payload["schema"] != 1:
        raise UpdaterError("update_intent_invalid")
    if any(not isinstance(payload.get(key), str) for key in required - {"schema", "requested_at", "requested_by"}):
        raise UpdaterError("update_intent_invalid")
    operation_id = payload.get("operation_id", payload["nonce"])
    if (payload["action"] not in {"apply", "rollback"} or payload["channel"] not in {"release", "stable", "beta"}
            or type(payload["requested_by"]) is not int or not 0 < payload["requested_by"] < 2**63
            or not _valid_timestamp(payload["requested_at"])
            or not _valid_operation_id(payload["nonce"]) or not _valid_operation_id(operation_id)):
        raise UpdaterError("update_intent_invalid")
    try:
        SemVer.parse(payload["version"])
        expected_current = payload.get("expected_current_version", "")
        if not isinstance(expected_current, str):
            raise UpdaterError("update_intent_invalid")
        if expected_current:
            SemVer.parse(expected_current)
    except (PlatformUpdateError, ValueError) as exc:
        raise UpdaterError("update_intent_invalid") from exc
    if payload["action"] == "apply":
        if (not Path(payload["candidate_path"]).is_absolute()
                or len(payload["candidate_path"]) > 1024
                or re.fullmatch(r"[0-9a-f]{64}", payload["manifest_sha256"]) is None):
            raise UpdaterError("update_intent_invalid")
    elif (payload["candidate_path"] or (payload["manifest_sha256"]
            and re.fullmatch(r"[0-9a-f]{64}", payload["manifest_sha256"]) is None)):
        raise UpdaterError("update_intent_invalid")
    return Intent(**{key: value for key, value in payload.items() if key != "schema"}, **({"operation_id": operation_id} if "operation_id" not in payload else {}))


def _intent_payload(intent: Intent) -> dict:
    return {"schema": 1, **asdict(intent)}


def _journal_path(config: Config, operation_id: str) -> Path:
    if not _valid_operation_id(operation_id):
        raise UpdaterError("update_journal_invalid")
    return config.private_state_dir / "journals" / f"{operation_id}.json"


def _read_journal(config: Config, operation_id: str) -> dict:
    journal = _read_private_json(_journal_path(config, operation_id), JOURNAL_MAX_BYTES)
    if (journal.get("schema") != 1 or journal.get("operation_id") != operation_id
            or journal.get("phase") not in PHASES or not isinstance(journal.get("intent"), dict)):
        raise UpdaterError("update_journal_invalid")
    return journal


def _pending_journal(config: Config) -> dict | None:
    for name in ("active.json", "blocked.json"):
        pointer = config.private_state_dir / name
        if pointer.exists() or pointer.is_symlink():
            payload = _read_private_json(pointer)
            return _read_journal(config, payload.get("operation_id"))
    return None


def _save_journal(config: Config, journal: dict) -> None:
    journal["updated_at"] = time.time()
    _atomic_json(_journal_path(config, journal["operation_id"]), journal)


def _open_journal(config: Config, intent: Intent, *, enforce_expiry: bool = False) -> tuple[dict, bool]:
    _prepare_private(config)
    if (not _valid_operation_id(intent.nonce) or intent.action not in {"apply", "rollback"}
            or intent.channel not in {"release", "stable", "beta"}
            or type(intent.requested_by) is not int or not 0 < intent.requested_by < 2**63):
        raise UpdaterError("update_intent_invalid")
    path = _journal_path(config, intent.operation_id)
    payload = _intent_payload(intent)
    nonce_path = config.private_state_dir / "nonces" / f"{intent.nonce}.json"
    if nonce_path.exists() and _read_private_json(nonce_path).get("operation_id") != intent.operation_id:
        raise UpdaterError("update_nonce_conflict")
    if path.exists():
        journal = _read_journal(config, intent.operation_id)
        if journal["intent"] != payload:
            raise UpdaterError("update_nonce_conflict")
        pending = _pending_journal(config)
        if journal["phase"] not in TERMINAL_PHASES and pending is None:
            _atomic_json(config.private_state_dir / "active.json", {"schema": 1, "operation_id": intent.operation_id})
        return journal, False
    pending = _pending_journal(config)
    if pending is not None:
        raise UpdaterError("update_recovery_required" if pending["phase"] == "recovery_failed" else "update_already_running")
    # Recover the journal-before-pointer power-loss window before accepting work.
    for orphan in (config.private_state_dir / "journals").glob("*.json"):
        saved = _read_journal(config, orphan.stem)
        if saved["intent"].get("nonce") == intent.nonce:
            raise UpdaterError("update_nonce_conflict")
        if saved["phase"] not in TERMINAL_PHASES or saved.get("maintenance_active"):
            raise UpdaterError("update_recovery_required")
    if enforce_expiry:
        if not _valid_timestamp(intent.requested_at):
            raise UpdaterError("update_intent_invalid")
        now = time.time()
        if intent.requested_at > now + INTENT_FUTURE_SKEW_SECONDS or now - intent.requested_at > config.intent_max_age_seconds:
            raise UpdaterError("update_intent_expired")
    journal = {"schema": 1, "operation_id": intent.operation_id, "intent": payload,
               "phase": "queued", "status": "queued", "error_code": "",
               "current_version": _trusted_public_current_version(
                   config, intent.expected_current_version
               ),
               "maintenance_active": False, "accepted_at": time.time()}
    _save_journal(config, journal)
    _atomic_json(nonce_path, {"schema": 1, "operation_id": intent.operation_id})
    _atomic_json(config.private_state_dir / "active.json", {"schema": 1, "operation_id": intent.operation_id})
    return journal, True


def claim_intent(config: Config) -> tuple[Intent, Path]:
    _prepare_private(config)
    pending = _pending_journal(config)
    if pending is None:
        for path in sorted((config.private_state_dir / "journals").glob("*.json")):
            saved = _read_journal(config, path.stem)
            if saved["phase"] not in TERMINAL_PHASES or saved.get("maintenance_active"):
                pending = saved
                _atomic_json(config.private_state_dir / "active.json", {"schema": 1, "operation_id": saved["operation_id"]})
                break
    if pending is not None:
        if pending["phase"] == "recovery_failed" and config.intent_file.exists():
            incoming = _parse_intent(_read_private_json(config.intent_file, owner_uid=config.intent_owner_uid))
            if incoming.operation_id != pending["operation_id"]:
                raise UpdaterError("update_recovery_required")
        # Accepted requests survive expiry and disappearance of the app's file.
        return Intent(**{key: value for key, value in pending["intent"].items() if key != "schema"}), config.intent_file
    source = config.intent_file
    if not source.exists() and not source.is_symlink():
        source = _processing_file(config)
    intent = _parse_intent(_read_private_json(source, owner_uid=config.intent_owner_uid))
    journal, created = _open_journal(config, intent, enforce_expiry=True)
    if created:
        metadata = source.lstat()
        journal["source"] = {"name": source.name, "dev": metadata.st_dev, "ino": metadata.st_ino}
        _save_journal(config, journal)
    return intent, source


def current_release(config: Config) -> Path:
    parent = _directory_fd(config.current_link.parent)
    os.close(parent)
    try:
        metadata = config.current_link.lstat()
    except OSError as exc:
        raise UpdaterError("update_current_missing") from exc
    if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise UpdaterError("update_current_invalid")
    try:
        target = config.current_link.resolve(strict=True)
        releases = config.releases_dir.resolve(strict=True)
    except OSError as exc:
        raise UpdaterError("update_current_invalid") from exc
    if not _within(target, releases) or target.parent != releases or not target.is_dir():
        raise UpdaterError("update_current_invalid")
    return target


def _trusted_signing_key() -> None:
    try:
        _read_protected_file(_public_key_file(), 4096, private=False)
    except (UpdaterError, PlatformUpdateError, OSError) as exc:
        raise UpdaterError("update_public_key_invalid") from exc


def _configured_bundle_root() -> Path:
    default = Path("/") / "opt" / "xianyu-saas" / "updater"
    raw = os.environ.get("SAAS_UPDATER_BUNDLE_ROOT", str(default)).strip()
    root = Path(raw)
    if not raw or not root.is_absolute() or ".." in root.parts:
        raise UpdaterError("update_updater_identity_invalid")
    return root


def _configured_entrypoint(bundle_root: Path) -> Path:
    fixed = bundle_root / "deploy" / "updater" / "updater.py"
    raw = os.environ.get("SAAS_UPDATER_ENTRYPOINT", str(fixed)).strip()
    entrypoint = Path(raw)
    if (
        not raw
        or not entrypoint.is_absolute()
        or ".." in entrypoint.parts
        or entrypoint != fixed
    ):
        raise UpdaterError("update_updater_identity_invalid")
    return entrypoint


def _installed_bundle_identity() -> dict:
    """Hash the fixed, root-readable updater bundle that this process loaded."""
    bundle_root = _configured_bundle_root()
    entrypoint = _configured_entrypoint(bundle_root)
    try:
        loaded = SCRIPT_PATH.lstat()
        resolved_entrypoint = entrypoint.resolve(strict=True)
    except OSError as exc:
        raise UpdaterError("update_updater_identity_invalid") from exc
    if (
        SCRIPT_LOAD_IDENTITY != _file_identity(loaded)
        or stat.S_ISLNK(loaded.st_mode)
        or SCRIPT_PATH.resolve(strict=True) != resolved_entrypoint
    ):
        raise UpdaterError("update_updater_identity_invalid")
    records = []
    digest = hashlib.sha256()
    entrypoint_sha256 = ""
    for relative in UPDATER_BUNDLE_FILES:
        path = bundle_root.joinpath(*Path(relative).parts)
        raw = _read_protected_file(
            path, UPDATER_BUNDLE_MAX_FILE_BYTES, private=False
        )
        if not raw:
            raise UpdaterError("update_updater_identity_invalid")
        file_digest = hashlib.sha256(raw).hexdigest()
        record = {"path": relative, "size": len(raw), "sha256": file_digest}
        records.append(record)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(raw)).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
        if relative == "deploy/updater/updater.py":
            entrypoint_sha256 = file_digest
    if not entrypoint_sha256:
        raise UpdaterError("update_updater_identity_invalid")
    try:
        key_raw = load_public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    except PlatformUpdateError as exc:
        raise UpdaterError(exc.code) from exc
    return {
        "bundle_root": str(bundle_root),
        "entrypoint": str(entrypoint),
        "entrypoint_sha256": entrypoint_sha256,
        "bundle_sha256": digest.hexdigest(),
        "bundle_files": records,
        "public_key_sha256": hashlib.sha256(key_raw).hexdigest(),
    }


def _supports_signed_maintenance_protocol(
    release_root: Path,
    expected_files: dict,
    *,
    protected: bool,
) -> bool:
    """Inspect only digest-bound source bytes with the trusted local parser."""
    expected = expected_files.get(MAINTENANCE_MODULE)
    if expected is None:
        return False
    try:
        size = int(expected.size)
        digest = str(expected.sha256)
    except (AttributeError, TypeError, ValueError):
        return False
    if size < 0 or size > 256 * 1024 or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return False
    source_path = release_root.joinpath(*Path(MAINTENANCE_MODULE).parts)
    try:
        if protected:
            source = _read_protected_file(source_path, size, private=False)
        else:
            source = _read_secure_file(source_path, size)
    except PlatformUpdateError as exc:
        raise UpdaterError(exc.code) from exc
    if len(source) != size or hashlib.sha256(source).hexdigest() != digest:
        raise UpdaterError("update_release_invalid" if protected else "update_candidate_changed")
    return supports_maintenance_protocol(source)


def _baseline_input_path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw or len(raw) > 4096:
        raise UpdaterError("update_import_path_invalid")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise UpdaterError("update_import_path_invalid")
    return path


def _offline_release(
    archive_path: Path,
    manifest_path: Path,
    signature_path: Path,
    manifest_raw: bytes,
    signature_raw: bytes,
) -> tuple[ReleaseInfo, dict, dict]:
    try:
        header = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise UpdaterError("update_manifest_invalid") from exc
    if not isinstance(header, dict):
        raise UpdaterError("update_manifest_invalid")
    version = str(header.get("version", ""))
    try:
        parsed = SemVer.parse(version)
        artifact_size = int(header.get("artifact_size", -1))
    except (PlatformUpdateError, TypeError, ValueError) as exc:
        raise UpdaterError("update_manifest_invalid") from exc
    expected_names = _asset_names(version)
    if (
        archive_path.name != expected_names[0]
        or manifest_path.name != expected_names[1]
        or signature_path.name != expected_names[2]
        or str(header.get("artifact", "")) != expected_names[0]
    ):
        raise UpdaterError("update_import_path_invalid")
    release = ReleaseInfo(
        release_id="offline-baseline",
        version=version,
        tag=f"v{version}",
        published_at="",
        notes="",
        prerelease=bool(parsed.prerelease),
        artifact=ReleaseAsset(1, expected_names[0], artifact_size),
        manifest=ReleaseAsset(2, expected_names[1], len(manifest_raw)),
        signature=ReleaseAsset(3, expected_names[2], len(signature_raw)),
    )
    try:
        manifest, expected_files = parse_manifest(manifest_raw, release)
    except PlatformUpdateError as exc:
        raise UpdaterError(exc.code) from exc
    return release, manifest, expected_files


def _copy_protected_archive(
    source: Path,
    destination: Path,
    *,
    expected_size: int,
    expected_digest: str,
) -> None:
    if expected_size <= 0 or expected_size > MAX_ARCHIVE_BYTES:
        raise UpdaterError("update_manifest_invalid")
    parent = _directory_fd(source.parent)
    source_descriptor = -1
    destination_descriptor = -1
    try:
        before = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o022
            or before.st_size != expected_size
        ):
            raise UpdaterError("update_import_source_invalid")
        source_descriptor = os.open(
            source.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=parent,
        )
        if _file_identity(before) != _file_identity(os.fstat(source_descriptor)):
            raise UpdaterError("update_file_changed")
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(destination_descriptor, "wb") as output:
            destination_descriptor = -1
            while True:
                chunk = os.read(source_descriptor, 128 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    raise UpdaterError("update_artifact_hash_mismatch")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fchmod(output.fileno(), 0o600)
            os.fsync(output.fileno())
        after = os.fstat(source_descriptor)
        named = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
        if (
            _file_identity(before) != _file_identity(after)
            or _file_identity(after) != _file_identity(named)
        ):
            raise UpdaterError("update_file_changed")
        if total != expected_size or digest.hexdigest() != expected_digest:
            raise UpdaterError("update_artifact_hash_mismatch")
    except FileNotFoundError as exc:
        raise UpdaterError("update_import_source_invalid") from exc
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        os.close(parent)


def _write_public_file(path: Path, payload: bytes) -> None:
    parent = _directory_fd(path.parent)
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.fsync(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def import_trusted_baseline(
    config: Config,
    archive_path: Path,
    manifest_path: Path,
    signature_path: Path,
) -> Path:
    """Offline-only import into releases; never switch current or touch services/data."""
    validate_layout(config)
    archive_path = _baseline_input_path(str(archive_path))
    manifest_path = _baseline_input_path(str(manifest_path))
    signature_path = _baseline_input_path(str(signature_path))
    if len({archive_path, manifest_path, signature_path}) != 3:
        raise UpdaterError("update_import_path_invalid")
    _trusted_signing_key()
    manifest_raw = _read_protected_file(manifest_path, MAX_MANIFEST_BYTES, private=False)
    signature_raw = _read_protected_file(signature_path, MAX_SIGNATURE_BYTES, private=False)
    try:
        verify_manifest_signature(manifest_raw, signature_raw)
    except PlatformUpdateError as exc:
        raise UpdaterError(exc.code) from exc
    release, manifest, expected_files = _offline_release(
        archive_path, manifest_path, signature_path, manifest_raw, signature_raw
    )
    manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
    _secure_directory(config.releases_dir, 0o755)
    final = config.releases_dir / release.version
    temporary = config.releases_dir / f".{release.version}-baseline.partial"
    archive_copy = config.releases_dir / f".{release.version}-baseline.archive.partial"
    for stale, directory in ((temporary, True), (archive_copy, False)):
        if not stale.exists() and not stale.is_symlink():
            continue
        metadata = stale.lstat()
        valid_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
        if metadata.st_uid != os.geteuid() or not valid_type or stale.is_symlink():
            raise UpdaterError("update_release_invalid")
        if directory:
            shutil.rmtree(stale)
        else:
            stale.unlink()
    try:
        # Validate and pin every supplied offline asset even for an idempotent
        # repeat. A matching installed tree never makes a missing/tampered tar OK.
        _copy_protected_archive(
            archive_path,
            archive_copy,
            expected_size=release.artifact.size,
            expected_digest=str(manifest["artifact_sha256"]),
        )
        if final.exists() or final.is_symlink():
            if final.is_symlink():
                raise UpdaterError("update_release_exists")
            try:
                existing = verify_existing_release(config, release.version)
                existing_manifest = _load_release_manifest(existing, release.version)
            except (UpdaterError, PlatformUpdateError, OSError) as exc:
                raise UpdaterError("update_release_exists") from exc
            if hashlib.sha256(existing_manifest["manifest_raw"]).hexdigest() != manifest_digest:
                raise UpdaterError("update_release_exists")
            if not _supports_signed_maintenance_protocol(
                existing, existing_manifest["expected_files"], protected=True
            ):
                raise UpdaterError("update_maintenance_protocol_unsupported")
            return existing

        _secure_directory(temporary, 0o755)
        try:
            extract_verified_archive(archive_copy, temporary, expected_files)
            _verify_candidate_version(temporary, release.version)
        except PlatformUpdateError as exc:
            raise UpdaterError(exc.code) from exc
        if not _supports_signed_maintenance_protocol(
            temporary, expected_files, protected=True
        ):
            raise UpdaterError("update_maintenance_protocol_unsupported")
        _write_public_file(temporary / CACHED_MANIFEST_FILE, manifest_raw)
        _write_public_file(temporary / CACHED_SIGNATURE_FILE, signature_raw)
        _atomic_json(
            temporary / MARKER_FILE,
            {
                "schema": 1,
                "version": release.version,
                "channel": "release",
                "manifest_sha256": manifest_digest,
                "release_id": "offline-baseline",
                "artifact": release.artifact.name,
                "artifact_size": release.artifact.size,
            },
            public=True,
        )
        for directory, _, _ in os.walk(temporary):
            _secure_directory(Path(directory), 0o755)
        copied = _load_release_manifest(temporary, release.version)
        if hashlib.sha256(copied["manifest_raw"]).hexdigest() != manifest_digest:
            raise UpdaterError("update_candidate_changed")
        os.replace(temporary, final)
        _fsync_directory(config.releases_dir)
        return final
    finally:
        if archive_copy.exists() and not archive_copy.is_symlink():
            archive_copy.unlink()
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)


def _load_release_manifest(release_root: Path, version: str) -> dict[str, object]:
    _trusted_signing_key()
    marker = _decode_json(_read_protected_file(release_root / MARKER_FILE, 8192, private=False))
    if marker.get("schema") != 1 or str(marker.get("version", "")) != version:
        raise UpdaterError("update_release_invalid")
    try:
        manifest_raw = _read_protected_file(
            release_root / CACHED_MANIFEST_FILE, 1024 * 1024, private=False
        )
        signature_raw = _read_protected_file(
            release_root / CACHED_SIGNATURE_FILE, 4096, private=False
        )
        verify_manifest_signature(manifest_raw, signature_raw)
    except PlatformUpdateError as exc:
        raise UpdaterError(exc.code) from exc
    if hashlib.sha256(manifest_raw).hexdigest() != str(
        marker.get("manifest_sha256", "")
    ):
        raise UpdaterError("update_release_invalid")
    try:
        release = _release_from_marker(marker)
        manifest, expected_files = parse_manifest(manifest_raw, release)
    except PlatformUpdateError as exc:
        raise UpdaterError(exc.code) from exc
    if str(manifest.get("version", "")) != version:
        raise UpdaterError("update_release_invalid")
    return {
        "marker": marker,
        "manifest": manifest,
        "manifest_raw": manifest_raw,
        "expected_files": expected_files,
    }


def _copy_file_verified(source: Path, destination: Path, expected: dict) -> None:
    relative = _validate_release_path(str(expected["path"]))
    try:
        size = int(expected["size"])
        expected_digest = str(expected["sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdaterError("update_manifest_invalid") from exc
    if size < 0 or size > MAX_FILE_BYTES or len(expected_digest) != 64:
        raise UpdaterError("update_manifest_invalid")
    try:
        source_metadata = source.lstat()
    except OSError as exc:
        raise UpdaterError("update_candidate_invalid") from exc
    if (
        stat.S_ISLNK(source_metadata.st_mode)
        or not stat.S_ISREG(source_metadata.st_mode)
        or source_metadata.st_nlink != 1
        or source_metadata.st_size != size
    ):
        raise UpdaterError("update_candidate_invalid")
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    os.fchmod(descriptor, 0o755 if expected.get("executable") else 0o644)
    digest = hashlib.sha256()
    total = 0
    try:
        source_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
        source_fd = os.open(source, source_flags)
        try:
            if _file_identity(os.fstat(source_fd)) != _file_identity(source_metadata):
                raise UpdaterError("update_candidate_changed")
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                descriptor = -1
                while True:
                    chunk = os.read(source_fd, 128 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > size:
                        raise UpdaterError("update_candidate_changed")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
                if _file_identity(os.fstat(source_fd)) != _file_identity(source_metadata):
                    raise UpdaterError("update_candidate_changed")
        finally:
            os.close(source_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if total != size or digest.hexdigest() != expected_digest:
        destination.unlink(missing_ok=True)
        raise UpdaterError("update_candidate_changed")


def materialize_release(config: Config, intent: Intent) -> Path:
    _trusted_signing_key()
    try:
        marker, expected_files = load_verified_candidate(
            intent.candidate_path, intent.version, intent.manifest_sha256
        )
    except PlatformUpdateError as exc:
        raise UpdaterError(exc.code) from exc
    candidate = Path(intent.candidate_path).resolve(strict=True)
    _secure_directory(config.releases_dir, mode=0o755)
    final = config.releases_dir / intent.version
    if final.exists() or final.is_symlink():
        raise UpdaterError("update_release_exists")
    temporary = config.releases_dir / f".{intent.version}-{intent.operation_id}.partial"
    if temporary.exists() or temporary.is_symlink():
        if temporary.is_symlink() or temporary.lstat().st_uid != os.geteuid():
            raise UpdaterError("update_release_invalid")
        shutil.rmtree(temporary)
    _secure_directory(temporary, 0o755)
    try:
        for relative, expected in expected_files.items():
            destination = temporary.joinpath(*Path(relative).parts)
            _copy_file_verified(
                candidate.joinpath(*Path(relative).parts),
                destination,
                {
                    "path": relative,
                    "size": expected.size,
                    "sha256": expected.sha256,
                    "executable": expected.executable,
                },
            )
        for internal in INTERNAL_CANDIDATE_FILES:
            source = candidate / internal
            destination = temporary / internal
            payload = _read_secure_file(
                source,
                1024 * 1024 if internal == CACHED_MANIFEST_FILE else 8192,
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(destination, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fchmod(output.fileno(), 0o644)
                os.fsync(output.fileno())
        for directory, _, _ in os.walk(temporary):
            _secure_directory(Path(directory), 0o755)
        # The root copy, not the app's mutable candidate, is the execution input.
        copied = _load_release_manifest(temporary, intent.version)
        if hashlib.sha256(copied["manifest_raw"]).hexdigest() != intent.manifest_sha256:
            raise UpdaterError("update_candidate_changed")
        os.replace(temporary, final)
        directory_fd = os.open(
            config.releases_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    if str(marker.get("version", "")) != intent.version:
        raise UpdaterError("update_candidate_invalid")
    return final


def verify_existing_release(config: Config, version: str) -> Path:
    release = config.releases_dir / version
    try:
        resolved = release.resolve(strict=True)
        releases = config.releases_dir.resolve(strict=True)
    except OSError as exc:
        raise UpdaterError("update_rollback_target_missing") from exc
    if release.is_symlink() or resolved.parent != releases or not resolved.is_dir():
        raise UpdaterError("update_rollback_target_invalid")
    payload = _load_release_manifest(resolved, version)
    expected_files = payload["expected_files"]
    if not isinstance(expected_files, dict) or not expected_files:
        raise UpdaterError("update_manifest_invalid")
    seen: set[str] = set()
    for relative, expected in expected_files.items():
        source = resolved.joinpath(*Path(relative).parts)
        try:
            source_metadata = source.lstat()
        except OSError as exc:
            raise UpdaterError("update_release_invalid") from exc
        if (
            stat.S_ISLNK(source_metadata.st_mode)
            or not stat.S_ISREG(source_metadata.st_mode)
            or source_metadata.st_nlink != 1
            or source_metadata.st_uid != os.geteuid()
            or source_metadata.st_mode & 0o022
            or source_metadata.st_size != expected.size
        ):
            raise UpdaterError("update_release_invalid")
        digest = hashlib.sha256(_read_protected_file(source, expected.size, private=False)).hexdigest()
        if digest != expected.sha256:
            raise UpdaterError("update_release_invalid")
        seen.add(relative)
    actual: set[str] = set()
    for directory, directories, filenames in os.walk(resolved, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in directories:
            child = directory_path / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise UpdaterError("update_release_invalid")
        for name in filenames:
            child = directory_path / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise UpdaterError("update_release_invalid")
            relative = child.relative_to(resolved).as_posix()
            if relative not in INTERNAL_CANDIDATE_FILES:
                actual.add(_validate_release_path(relative))
    if actual != seen:
        raise UpdaterError("update_release_invalid")
    _verify_candidate_version(resolved, version)
    # Repair legacy 0600 metadata only after independent signature/file checks.
    for internal in INTERNAL_CANDIDATE_FILES:
        path = resolved / internal
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
                raise UpdaterError("update_release_invalid")
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return resolved


def snapshot_worker_intents(database_path: Path) -> list[dict[str, object]]:
    try:
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT user_id, account_id, desired_state, mode, generation
                FROM worker_runtimes ORDER BY user_id, account_id
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise UpdaterError("update_worker_snapshot_failed") from exc
    return [
        {
            "user_id": int(row["user_id"]),
            "account_id": int(row["account_id"]),
            "desired_state": str(row["desired_state"]),
            "mode": str(row["mode"]),
            "generation": int(row["generation"]),
        }
        for row in rows
    ]


def backup_database(config: Config, version: str, worker_intents: list[dict], *, destination_path: Path | None = None) -> Path:
    _secure_directory(config.backup_dir)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = destination_path or config.backup_dir / f"saas-{stamp}-{time.time_ns()}-before-{version}.db"
    if backup.parent != config.backup_dir or backup.is_symlink():
        raise UpdaterError("update_database_backup_failed")
    metadata = backup.with_suffix(".json")
    temporary = backup.with_name("." + backup.name + ".partial")
    try:
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        database_metadata = config.database_path.lstat()
        if not stat.S_ISREG(database_metadata.st_mode) or database_metadata.st_nlink != 1:
            raise UpdaterError("update_database_backup_failed")
        source_uri = config.database_path.resolve(strict=True).as_uri() + "?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=30)
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).lower() != "ok":
                raise sqlite3.DatabaseError("backup integrity check failed")
            destination.commit()
        finally:
            destination.close()
            source.close()
        descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, backup)
        _atomic_json(metadata, {"schema": 1, "created_at": time.time(), "target_version": version,
                                "worker_intents": worker_intents})
        _fsync_directory(config.backup_dir)
    except (OSError, sqlite3.Error) as exc:
        raise UpdaterError("update_database_backup_failed") from exc
    finally:
        temporary.unlink(missing_ok=True)
    # Active recovery backups are retained; pruning occurs only after success.
    return backup


def backup_runtime_state(config: Config, journal: dict) -> Path:
    """Cold, root-private state/tenant archive; never restored automatically.

    Archive only through directory descriptors. Reject links, mounts and special
    files rather than accidentally following credentials outside the data roots.
    SQLite is backed up separately; untrusted download queues are not business data.
    """
    _secure_directory(config.backup_dir)
    archive = Path(journal["backup_path"]).with_suffix(".state.tar")
    temporary = archive.with_name("." + archive.name + ".partial")
    roots = [(config.state_dir, "state")]
    if not _within(config.tenants_dir, config.state_dir):
        roots.append((config.tenants_dir, "tenants"))
    excluded = (config.staging_dir, config.intent_file.parent, config.status_dir,
                config.private_state_dir, config.backup_dir)
    excluded_files = {config.database_path, Path(str(config.database_path) + "-wal"),
                      Path(str(config.database_path) + "-shm"), config.lock_file}
    count = 0

    def copy_tree(output, descriptor, source_root, prefix, device):
        nonlocal count
        before = os.fstat(descriptor)
        for name in sorted(os.listdir(descriptor)):
            source_path = source_root / name
            if source_path in excluded_files or any(_within(source_path, item) for item in excluded):
                continue
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            count += 1
            if count > 200000 or metadata.st_dev != device:
                raise UpdaterError("update_runtime_backup_invalid")
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.mode = stat.S_IMODE(metadata.st_mode) & 0o777
            info.uid, info.gid, info.mtime = metadata.st_uid, metadata.st_gid, metadata.st_mtime
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
                try:
                    if _file_identity(os.fstat(child)) != _file_identity(metadata):
                        raise UpdaterError("update_runtime_backup_changed")
                    info.type = tarfile.DIRTYPE
                    output.addfile(info)
                    copy_tree(output, child, source_path, info.name, device)
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=descriptor)
                with os.fdopen(child, "rb") as source:
                    if _file_identity(os.fstat(source.fileno())) != _file_identity(metadata):
                        raise UpdaterError("update_runtime_backup_changed")
                    info.size = metadata.st_size
                    output.addfile(info, source)
                    if _file_identity(os.fstat(source.fileno())) != _file_identity(metadata):
                        raise UpdaterError("update_runtime_backup_changed")
            else:
                raise UpdaterError("update_runtime_backup_invalid")
        after = os.fstat(descriptor)
        # Reading a directory can update atime, which is deliberately excluded.
        if _file_identity(before) != _file_identity(after):
            raise UpdaterError("update_runtime_backup_changed")

    journal["runtime_backup_roots"] = [str(root) for root, _ in roots]
    journal["runtime_backup_exclusions"] = [str(root) for root in excluded] + [str(path) for path in sorted(excluded_files)]
    _save_journal(config, journal)
    try:
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            with tarfile.open(fileobj=stream, mode="w") as output:
                for root, prefix in roots:
                    if not root.exists() and root == config.tenants_dir:
                        continue
                    source = _directory_fd(root, owner_uid=config.intent_owner_uid)
                    try:
                        copy_tree(output, source, root, prefix, os.fstat(source).st_dev)
                    finally:
                        os.close(source)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, archive)
        _fsync_directory(config.backup_dir)
    except (OSError, tarfile.TarError) as exc:
        raise UpdaterError("update_runtime_backup_failed") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def prune_backups(config: Config) -> None:
    backups = sorted(
        (
            path
            for path in config.backup_dir.glob("saas-*-before-*.db")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[BACKUP_KEEP:]:
        stale.unlink(missing_ok=True)
        stale.with_suffix(".json").unlink(missing_ok=True)
        stale.with_suffix(".state.tar").unlink(missing_ok=True)


def stop_services(config: Config, runner: SystemRunner) -> None:
    runner.run(
        ["systemctl", "stop", config.consumer_service, config.api_service]
    )


def start_services(config: Config, runner: SystemRunner) -> None:
    runner.run(["systemctl", "start", config.api_service])
    runner.run(["systemctl", "start", config.consumer_service])


def run_migrations(release: Path, config: Config, runner: SystemRunner) -> None:
    command = [
        sys.executable,
        "-c",
        "from db import DB; database = DB(); assert database.is_ready()",
    ]
    environment = _child_environment({"SAAS_DB": str(config.database_path),
                                      "SAAS_STATE_DIR": str(config.state_dir),
                                      "SAAS_TENANTS_DIR": str(config.tenants_dir),
                                      "SAAS_RESTORE_WORKERS": "0"})
    identity = {}
    if os.geteuid() == 0 and config.intent_owner_uid not in {None, 0}:
        identity = {"user": config.intent_owner_uid, "group": pwd.getpwuid(config.intent_owner_uid).pw_gid, "extra_groups": []}
    completed = subprocess.run(
        command,
        **identity,
        cwd=release / "backend",
        env=environment,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=SERVICE_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise UpdaterError("update_migration_failed")


def switch_current(config: Config, release: Path) -> None:
    releases = config.releases_dir.resolve(strict=True)
    target = release.resolve(strict=True)
    if target.parent != releases or target.is_symlink() or not target.is_dir():
        raise UpdaterError("update_release_invalid")
    temporary = config.current_link.with_name(
        f".{config.current_link.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    os.symlink(str(target), temporary)
    try:
        os.replace(temporary, config.current_link)
        directory_fd = os.open(
            config.current_link.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _response_json(response) -> dict:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise UpdaterError("update_health_failed") from exc
    if not isinstance(payload, dict):
        raise UpdaterError("update_health_failed")
    return payload


def check_health(
    config: Config,
    version: str,
    health_client: HealthClient,
    *,
    attempts: int = HEALTH_ATTEMPTS,
    interval: float = HEALTH_INTERVAL_SECONDS,
) -> None:
    last_error: Exception | None = None
    for attempt in range(max(int(attempts), 1)):
        try:
            health = health_client.get(urljoin(config.health_base_url, "health"))
            ready = health_client.get(urljoin(config.health_base_url, "api/ready"))
            unauthenticated = health_client.get(urljoin(config.health_base_url, "api/me"))
            version_response = health_client.get(
                urljoin(config.health_base_url, "api/version/public")
            )
            index = health_client.get(config.public_base_url)
            version_payload = _response_json(version_response)
            asset_version = version_payload.get("asset_version")
            if (
                health.status_code == 200
                and _response_json(health).get("ok") is True
                and ready.status_code == 200
                and _response_json(ready).get("database") == "ready"
                and unauthenticated.status_code == 401
                and version_response.status_code == 200
                and version_payload.get("version") == version
                and isinstance(asset_version, str) and bool(asset_version.strip())
                and index.status_code == 200
                and asset_version in str(getattr(index, "text", ""))
            ):
                return
            last_error = UpdaterError("update_health_failed")
        except Exception as exc:  # Network failures are folded into one stable code.
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(max(float(interval), 0.0))
    raise UpdaterError("update_health_failed") from last_error


def update_status(
    database: DB,
    intent: Intent,
    status: str,
    *,
    error_code: str = "",
    candidate_path: str = "",
    expected: dict | None = None,
) -> dict:
    """CAS the legacy projection, never upsert over release notes/identity.

    The private journal is authoritative. An API replacing this row causes the
    executor to stop forward work; recovery still uses its private identities.
    """
    now = time.time()
    with database._lock:
        connection = database.con
        if connection.in_transaction:
            raise UpdaterError("update_status_transaction_active")
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM platform_updates WHERE version = ? AND channel = ?", (intent.version, intent.channel)).fetchone()
            if row is None:
                if expected is not None:
                    raise UpdaterError("update_status_conflict")
                connection.execute("""INSERT INTO platform_updates
                    (version, channel, status, manifest_sha256, candidate_path, error_code,
                     requested_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (intent.version, intent.channel, status, intent.manifest_sha256,
                     candidate_path or intent.candidate_path, error_code, intent.requested_by, now, now))
            else:
                if (intent.action == "apply" and row["manifest_sha256"] not in {"", intent.manifest_sha256}
                        or row["requested_by"] not in {None, intent.requested_by}
                        or ("operation_id" in row.keys() and row["operation_id"] and row["operation_id"] != intent.operation_id)):
                    raise UpdaterError("update_status_conflict")
                if expected is not None:
                    previous = row["id"] == expected["id"] and row["updated_at"] == expected["updated_at"]
                    repeated = (row["id"] == expected["id"] and row["status"] == status
                                and row["error_code"] == error_code)
                    if not previous and not repeated:
                        raise UpdaterError("update_status_conflict")
                changed = connection.execute("""UPDATE platform_updates SET status = ?, error_code = ?,
                    requested_by = ?, updated_at = ? WHERE id = ? AND status = ? AND updated_at = ?""",
                    (status, error_code, intent.requested_by, now, row["id"], row["status"], row["updated_at"]))
                if changed.rowcount != 1:
                    raise UpdaterError("update_status_conflict")
            result = connection.execute("SELECT id, status, updated_at FROM platform_updates WHERE version = ? AND channel = ?", (intent.version, intent.channel)).fetchone()
            connection.commit()
            return dict(result)
        except BaseException:
            connection.rollback()
            raise


def prune_releases(config: Config) -> None:
    try:
        current = current_release(config)
    except UpdaterError:
        current = None
    releases = []
    for path in config.releases_dir.iterdir():
        if path.name.startswith(".") or path.is_symlink() or not path.is_dir():
            continue
        try:
            SemVer.parse(path.name)
        except PlatformUpdateError:
            continue
        releases.append(path)
    releases.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    keep: set[Path] = set()
    if current is not None:
        keep.add(current.resolve())
    for release in releases:
        if len(keep) >= RELEASE_KEEP:
            break
        keep.add(release.resolve())
    for stale in releases:
        if stale.resolve() not in keep:
            shutil.rmtree(stale)


class _DatabaseProjection:
    """Open only the existing DB, as the app UID; do not run DB.__init__ as root.

    SQLite may create WAL/SHM files during a status write. Running every such
    operation with the database owner's UID avoids root-only live sidecars.
    This executor is deliberately single-threaded.
    """
    def __init__(self, config: Config):
        self.uid = config.intent_owner_uid
        metadata = config.database_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UpdaterError("update_database_invalid")
        if self.uid is not None and metadata.st_uid != self.uid:
            raise UpdaterError("update_database_owner_invalid")
        with self._lock:
            self.con = sqlite3.connect(config.database_path.as_uri() + "?mode=rw", uri=True, timeout=30)
            self.con.row_factory = sqlite3.Row

    @property
    @contextmanager
    def _lock(self):
        uid, gid, groups = os.geteuid(), os.getegid(), os.getgroups()
        change = uid == 0 and self.uid not in {None, 0}
        try:
            if change:
                os.setgroups([])
                os.setegid(pwd.getpwuid(self.uid).pw_gid)
                os.seteuid(self.uid)
            yield
        finally:
            if change:
                os.seteuid(uid)
                os.setegid(gid)
                os.setgroups(groups)

    def close(self):
        with self._lock:
            self.con.close()


def _safe_error(exc: Exception) -> str:
    code = str(getattr(exc, "code", "update_failed"))
    return code if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) else "update_failed"


def _trusted_public_current_version(config: Config, preferred: str = "") -> str:
    if preferred:
        try:
            SemVer.parse(preferred)
            return preferred
        except PlatformUpdateError:
            return ""
    try:
        current = current_release(config)
        return verify_existing_release(config, current.name).name
    except (UpdaterError, PlatformUpdateError, OSError):
        return ""


def _publish_state(config: Config, journal: dict) -> None:
    _secure_directory(config.status_dir, 0o755)
    _secure_directory(config.status_dir / "operations", 0o755)
    intent = journal["intent"]
    phase = "preparing" if journal["phase"] == "migrating" else journal["phase"]
    current_version = _trusted_public_current_version(
        config, str(journal.get("current_version", ""))
    )
    if current_version:
        payload = {"schema": 1, "operation_id": journal["operation_id"], "action": intent["action"],
                   "version": intent["version"], "current_version": current_version,
                   "status": "preparing" if journal["status"] == "migrating" else journal["status"],
                   "phase": phase, "updated_at": journal["updated_at"],
                   "error_code": journal.get("error_code", "")}
        _atomic_json(config.status_dir / "operations" / f"{journal['operation_id']}.json", payload, public=True)
        _atomic_json(config.status_dir / "operation.json", payload, public=True)
    _atomic_json(config.status_dir / "maintenance.json", {
        "schema": 1, "operation_id": journal["operation_id"], "active": bool(journal.get("maintenance_active")),
        "phase": phase, "updated_at": journal["updated_at"],
    }, public=True)


def _sync_projection(config: Config, journal: dict, database) -> None:
    intent = Intent(**{key: value for key, value in journal["intent"].items() if key != "schema"})
    journal["db_token"] = update_status(database, intent, journal["status"],
                                        error_code=journal.get("error_code", ""), expected=journal.get("db_token"))
    _save_journal(config, journal)


def _transition(config: Config, journal: dict, database, phase: str, *, error_code: str | None = None, recovery: bool = False) -> None:
    if phase not in PHASES:
        raise UpdaterError("update_journal_invalid")
    journal["phase"] = phase
    journal["status"] = phase
    if error_code is not None:
        journal["error_code"] = error_code
    _save_journal(config, journal)
    try:
        _sync_projection(config, journal, database)
    except (UpdaterError, sqlite3.Error):
        if not recovery:
            raise
        # A stale API row cannot veto recovery or replace a newer operation.
    _publish_state(config, journal)


def _assert_execution_owner(config: Config, journal: dict, database, *, recovery: bool = False) -> None:
    pending = _pending_journal(config)
    if pending is None or pending["operation_id"] != journal["operation_id"]:
        raise UpdaterError("update_execution_ownership_lost")
    token = journal.get("db_token")
    if token and not recovery:
        with database._lock:
            row = database.con.execute("SELECT id, status, updated_at FROM platform_updates WHERE id = ?", (token["id"],)).fetchone()
        if row is None or dict(row) != token:
            raise UpdaterError("update_status_conflict")


def _code_identity(config: Config, version: str) -> dict:
    release = verify_existing_release(config, version)
    metadata = release.lstat()
    manifest = _load_release_manifest(release, version)
    return {"path": str(release), "version": version, "dev": metadata.st_dev, "ino": metadata.st_ino,
            "manifest_sha256": hashlib.sha256(manifest["manifest_raw"]).hexdigest()}


def _verify_code_identity(config: Config, identity: dict) -> Path:
    if not isinstance(identity, dict):
        raise UpdaterError("update_journal_invalid")
    actual = _code_identity(config, str(identity.get("version", "")))
    if actual != identity:
        raise UpdaterError("update_release_identity_changed")
    return Path(actual["path"])


def _backup_digest(path: Path) -> str:
    parent = _directory_fd(path.parent)
    descriptor = -1
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_mode & 0o077):
            raise UpdaterError("update_database_backup_failed")
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        if _file_identity(before) != _file_identity(os.fstat(descriptor)):
            raise UpdaterError("update_database_backup_failed")
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _database_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise UpdaterError("update_database_backup_failed")
        for statement in connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _probe_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for directory, directories, filenames in os.walk(root, followlinks=False):
        for name in sorted(directories + filenames):
            path = Path(directory) / name
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise UpdaterError("update_migration_requires_manual")
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def preflight_migrations(config: Config, journal: dict, target: Path, old_release: Path, runner: SystemRunner) -> None:
    """Conservative lane: both versions open only private DB/runtime copies.

    Schema/data-changing upgrades need a separately reviewed manual migration;
    no backup is ever copied into live data during automatic recovery.
    """
    backup = Path(journal["backup_path"])
    archive = Path(journal["runtime_backup_path"])
    if (_backup_digest(backup) != journal["backup_sha256"]
            or _backup_digest(archive) != journal["runtime_backup_sha256"]):
        raise UpdaterError("update_database_backup_failed")
    workspace = config.private_state_dir / f"migration-{journal['operation_id']}"
    try:
        if workspace.is_symlink():
            raise UpdaterError("update_directory_invalid")
        if workspace.exists():
            shutil.rmtree(workspace)
        _secure_directory(workspace)
        # The archive is root-generated and digest-bound; still extract only
        # regular files/directories with a bounded relative state/tenants prefix.
        with tarfile.open(archive) as source:
            for member in source:
                relative = Path(member.name)
                if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                        or relative.parts[0] not in {"state", "tenants"}
                        or not (member.isdir() or member.isfile())):
                    raise UpdaterError("update_runtime_backup_invalid")
                destination = workspace / relative
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if member.isdir():
                    destination.mkdir(mode=0o700, exist_ok=True)
                else:
                    with source.extractfile(member) as incoming, destination.open("xb") as output:
                        shutil.copyfileobj(incoming, output)
                    destination.chmod(0o600)
        state = workspace / "state"
        state.mkdir(mode=0o700, exist_ok=True)
        tenants = state / config.tenants_dir.relative_to(config.state_dir) if _within(config.tenants_dir, config.state_dir) else workspace / "tenants"
        tenants.mkdir(mode=0o700, parents=True, exist_ok=True)
        probe = workspace / "saas.db"
        shutil.copyfile(backup, probe)
        os.chmod(probe, 0o600)
        before = _database_fingerprint(probe)
        runtime_before = (_probe_tree_digest(state), _probe_tree_digest(tenants))
        isolated = replace(config, database_path=probe, state_dir=state, tenants_dir=tenants, intent_owner_uid=None)
        for release in (target, old_release):
            run_migrations(release, isolated, runner)
            if (_database_fingerprint(probe) != before
                    or (_probe_tree_digest(state), _probe_tree_digest(tenants)) != runtime_before):
                raise UpdaterError("update_migration_requires_manual")
    finally:
        if workspace.exists() and not workspace.is_symlink():
            shutil.rmtree(workspace)


def _remove_pointer(config: Config, name: str, operation_id: str) -> None:
    path = config.private_state_dir / name
    if path.exists() and _read_private_json(path).get("operation_id") == operation_id:
        path.unlink()
        _fsync_directory(path.parent)


def _finish(config: Config, journal: dict) -> None:
    failed_recovery = journal["phase"] == "recovery_failed"
    journal["maintenance_active"] = failed_recovery
    _save_journal(config, journal)
    _publish_state(config, journal)
    if failed_recovery:
        _atomic_json(config.private_state_dir / "blocked.json", {"schema": 1, "operation_id": journal["operation_id"]})
    # Cleanup only if old services were untouched or actually passed recovery
    # health. Never delete on an uncertain current-link read or failed recovery.
    safe_inactive_target = journal.get("recovery_health_ok") or not journal.get("services_may_be_stopped")
    if (journal["phase"] in {"rolled_back", "failed"} and safe_inactive_target
            and journal["intent"]["action"] == "apply" and journal.get("target_identity")):
        target = Path(journal["target_identity"]["path"])
        old = Path(journal["old_identity"]["path"])
        try:
            if current_release(config) == old and target != old and target.exists():
                _verify_code_identity(config, journal["target_identity"])
                shutil.rmtree(target)
                _fsync_directory(config.releases_dir)
        except (UpdaterError, OSError):
            # Retain uncertain artifacts; cleanup failure is not failed health.
            pass
    if not failed_recovery:
        _remove_pointer(config, "blocked.json", journal["operation_id"])
    _remove_pointer(config, "active.json", journal["operation_id"])


def _recover(config: Config, journal: dict, database, runner: SystemRunner, health_client: HealthClient) -> None:
    error_code = journal.get("original_error_code") or journal.get("error_code") or "update_interrupted"
    journal["original_error_code"] = error_code
    journal["maintenance_active"] = True
    try:
        _transition(config, journal, database, "rolling_back", error_code=error_code, recovery=True)
        _assert_execution_owner(config, journal, database, recovery=True)
        old_release = _verify_code_identity(config, journal["old_identity"])
        active = current_release(config)
        target = config.releases_dir / journal["intent"]["version"]
        if active not in {old_release, target}:
            raise UpdaterError("update_current_identity_changed")
        if active == target:
            journal["switched"] = True
        journal["rollback_step"] = "stopping"
        _save_journal(config, journal)
        stop_services(config, runner)
        journal["rollback_step"] = "switching"
        _save_journal(config, journal)
        _assert_execution_owner(config, journal, database, recovery=True)
        switch_current(config, old_release)
        journal["current_version"] = old_release.name
        journal["rollback_step"] = "starting"
        _save_journal(config, journal)
        start_services(config, runner)
        journal["rollback_step"] = "verifying"
        _save_journal(config, journal)
        check_health(config, old_release.name, health_client)
        journal["recovery_health_ok"] = True
        _transition(config, journal, database, "rolled_back" if journal.get("switched") else "failed", error_code=error_code, recovery=True)
        _finish(config, journal)
    except Exception as exc:
        journal["recovery_error_code"] = _safe_error(exc)
        _transition(config, journal, database, "recovery_failed", error_code="update_rollback_failed", recovery=True)
        _finish(config, journal)


def _result(journal: dict) -> dict:
    return {"ok": journal["phase"] in {"succeeded", "rolled_back"} and not journal.get("error_code"),
            "action": journal["intent"]["action"], "version": journal["intent"]["version"],
            "previous_version": journal.get("old_identity", {}).get("version", ""),
            "operation_id": journal["operation_id"], "status": journal["status"]}


def process_intent(
    config: Config,
    intent: Intent,
    *,
    runner: SystemRunner | None = None,
    health_client: HealthClient | None = None,
    database: DB | None = None,
) -> dict:
    validate_layout(config)
    runner = runner or SystemRunner()
    health_client = health_client or HealthClient()
    journal, _ = _open_journal(config, intent, enforce_expiry=True)
    pending = _pending_journal(config)
    if journal["phase"] in TERMINAL_PHASES and journal["phase"] != "recovery_failed":
        # Old nonce replays never touch services or overwrite a newer operation.
        if pending and pending["operation_id"] == intent.operation_id:
            _finish(config, journal)
        return _result(journal)
    owned_database = database is None
    database = database or _DatabaseProjection(config)
    try:
        if journal["phase"] in {"rolling_back", "recovery_failed"}:
            _recover(config, journal, database, runner, health_client)
            return _result(journal)
        if journal.get("migrations_started") and not journal.get("migrations_done"):
            raise UpdaterError("update_migration_interrupted")
        if "old_identity" not in journal:
            old_release = current_release(config)
            if intent.expected_current_version and old_release.name != intent.expected_current_version:
                raise UpdaterError("update_current_version_changed")
            if old_release.name == intent.version:
                raise UpdaterError("update_version_already_current")
            comparison = SemVer.parse(intent.version).compare(SemVer.parse(old_release.name))
            if intent.action == "apply" and comparison <= 0:
                raise UpdaterError("update_downgrade_rejected")
            if intent.action == "rollback" and comparison >= 0:
                raise UpdaterError("update_rollback_target_invalid")
            journal["old_identity"] = _code_identity(config, old_release.name)
            journal["current_version"] = old_release.name
            _save_journal(config, journal)
        old_release = _verify_code_identity(config, journal["old_identity"])
        target = config.releases_dir / intent.version
        if "target_identity" not in journal:
            _transition(config, journal, database, "verifying_package")
            if intent.action == "apply":
                if not journal.get("materialization_started"):
                    # Materialization itself enforces pre-existing destination
                    # rejection. Record ownership before its atomic rename.
                    journal["target_preexisting"] = target.exists() or target.is_symlink()
                    journal["materialization_started"] = True
                    _save_journal(config, journal)
                    _assert_execution_owner(config, journal, database)
                    target = materialize_release(config, intent)
                elif target.exists() and not journal.get("target_preexisting"):
                    target = verify_existing_release(config, intent.version)
                else:
                    _assert_execution_owner(config, journal, database)
                    target = materialize_release(config, intent)
            else:
                target = verify_existing_release(config, intent.version)
            identity = _code_identity(config, target.name)
            if intent.manifest_sha256 and identity["manifest_sha256"] != intent.manifest_sha256:
                raise UpdaterError("update_candidate_changed")
            journal["target_identity"] = identity
            _save_journal(config, journal)
        target = _verify_code_identity(config, journal["target_identity"])
        if not journal.get("dependencies_checked"):
            _transition(config, journal, database, "preflighting")
            _verify_dependency_stability(target, old_release)
            _secure_directory(config.backup_dir)
            journal["dependencies_checked"] = True
            _save_journal(config, journal)
        if not journal.get("maintenance_active"):
            _assert_execution_owner(config, journal, database)
            journal["maintenance_active"] = True
            _transition(config, journal, database, "preparing")
        if "worker_intents" not in journal:
            journal["worker_intents"] = snapshot_worker_intents(config.database_path)
            _save_journal(config, journal)
        # After a restart the services may be partially started. Stop again
        # before taking/resuming the cold backup, never infer safety from phase.
        if not journal.get("migrations_done"):
            journal["services_may_be_stopped"] = True
            _transition(config, journal, database, "stopping")
            _assert_execution_owner(config, journal, database)
            stop_services(config, runner)
            if not journal.get("backup_done"):
                _transition(config, journal, database, "backing_up")
                journal["backup_path"] = str(config.backup_dir / f"saas-{intent.operation_id}-before-{intent.version}.db")
                _save_journal(config, journal)
                _assert_execution_owner(config, journal, database)
                backup_database(config, intent.version, journal["worker_intents"], destination_path=Path(journal["backup_path"]))
                journal["backup_sha256"] = _backup_digest(Path(journal["backup_path"]))
                journal["backup_done"] = True
                _save_journal(config, journal)
            if not journal.get("runtime_backup_done"):
                _transition(config, journal, database, "backing_up")
                _assert_execution_owner(config, journal, database)
                archive = backup_runtime_state(config, journal)
                journal["runtime_backup_path"] = str(archive)
                journal["runtime_backup_sha256"] = _backup_digest(archive)
                journal["runtime_backup_done"] = True
                _save_journal(config, journal)
            if intent.action == "apply":
                _transition(config, journal, database, "preflighting")
                preflight_migrations(config, journal, target, old_release, runner)
                journal["migrations_started"] = True
                _transition(config, journal, database, "migrating")
                _assert_execution_owner(config, journal, database)
                run_migrations(target, config, runner)
            journal["migrations_done"] = True
            _save_journal(config, journal)
        _transition(config, journal, database, "switching")
        _assert_execution_owner(config, journal, database)
        active = current_release(config)
        if active not in {old_release, target}:
            raise UpdaterError("update_current_identity_changed")
        if active == old_release:
            switch_current(config, target)
        journal["switched"] = True
        journal["current_version"] = intent.version
        _transition(config, journal, database, "verifying")
        _assert_execution_owner(config, journal, database)
        start_services(config, runner)
        check_health(config, intent.version, health_client)
        journal["target_health_ok"] = True
        _transition(config, journal, database, "succeeded" if intent.action == "apply" else "rolled_back")
        _finish(config, journal)
        # Cleanup is not part of deciding application health or recovery.
        try:
            prune_releases(config)
            prune_backups(config)
        except OSError:
            pass
        return _result(journal)
    except Exception as exc:
        error_code = _safe_error(exc)
        journal["original_error_code"] = error_code
        if journal.get("maintenance_active") or journal.get("services_may_be_stopped") or journal.get("switched"):
            _recover(config, journal, database, runner, health_client)
        else:
            _transition(config, journal, database, "failed", error_code=error_code, recovery=True)
            _finish(config, journal)
        raise
    finally:
        if owned_database:
            database.close()


def _remove_consumed_intent(config: Config, intent: Intent) -> None:
    for source in (config.intent_file, _processing_file(config)):
        if not source.exists() and not source.is_symlink():
            continue
        try:
            saved = _parse_intent(_read_private_json(source, owner_uid=config.intent_owner_uid))
            if _intent_payload(saved) != _intent_payload(intent):
                continue
            parent = _directory_fd(source.parent, owner_uid=config.intent_owner_uid)
            try:
                os.unlink(source.name, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
        except (UpdaterError, OSError):
            continue


def _discard_rejected_intent(config: Config, error_code: str) -> bool:
    """Retire a rejected app-owned queue entry, without changing maintenance."""
    source = config.intent_file if config.intent_file.exists() or config.intent_file.is_symlink() else _processing_file(config)
    parent = _directory_fd(source.parent, owner_uid=config.intent_owner_uid)
    try:
        metadata = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
        owner = os.geteuid() if config.intent_owner_uid is None else config.intent_owner_uid
        if metadata.st_uid != owner or stat.S_ISDIR(metadata.st_mode):
            return False
        try:
            rejected = _parse_intent(_read_private_json(source, owner_uid=config.intent_owner_uid))
        except (UpdaterError, OSError):
            rejected = None
        _atomic_json(config.private_state_dir / "last-rejection.json", {"schema": 1, "error_code": error_code, "updated_at": time.time()})
        if rejected is not None:
            # Do not overwrite a prior nonce's genuine outcome with a replay error.
            if not _journal_path(config, rejected.operation_id).exists():
                current_version = _trusted_public_current_version(
                    config, rejected.expected_current_version
                )
                # The API schema requires a SemVer current_version. New requests
                # always bind it; legacy requests publish only after verifying the
                # installed signed release rather than emitting an unreadable row.
                if current_version:
                    _secure_directory(config.status_dir, 0o755)
                    _secure_directory(config.status_dir / "operations", 0o755)
                    payload = {"schema": 1, "operation_id": rejected.operation_id, "action": rejected.action,
                               "version": rejected.version, "current_version": current_version,
                               "status": "failed", "phase": "failed",
                               "updated_at": time.time(), "error_code": error_code}
                    _atomic_json(config.status_dir / "operations" / f"{rejected.operation_id}.json", payload, public=True)
                    if _pending_journal(config) is None:
                        _atomic_json(config.status_dir / "operation.json", payload, public=True)
        named = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
        if _file_identity(named) != _file_identity(metadata):
            return False
        os.unlink(source.name, dir_fd=parent)
        os.fsync(parent)
        return True
    finally:
        os.close(parent)


def initialize_layout(config: Config) -> None:
    """Initialize updater-owned paths after an offline trusted baseline exists."""
    validate_layout(config)
    current = current_release(config)
    current = verify_existing_release(config, current.name)
    current_manifest = _load_release_manifest(current, current.name)
    if not _supports_signed_maintenance_protocol(
        current, current_manifest["expected_files"], protected=True
    ):
        raise UpdaterError("update_maintenance_protocol_unsupported")
    identity = _installed_bundle_identity()
    _prepare_private(config)
    # The IPC root must not be replaceable by the app. Only this explicit
    # installation command changes its group/mode, never the business state.
    _secure_directory(config.intent_file.parent)
    descriptor = _directory_fd(config.intent_file.parent)
    try:
        if config.intent_owner_uid is not None:
            os.fchown(descriptor, os.geteuid(), pwd.getpwuid(config.intent_owner_uid).pw_gid)
        os.fchmod(descriptor, 0o1770)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _secure_directory(config.status_dir, 0o755)
    _secure_directory(config.status_dir / "operations", 0o755)
    _atomic_json(
        config.status_dir / "initialization.json",
        {
            "schema": 1,
            "protocol": SYSTEMD_UPDATER_PROTOCOL,
            "public_key_sha256": identity["public_key_sha256"],
            "bundle_sha256": identity["bundle_sha256"],
            "entrypoint_sha256": identity["entrypoint_sha256"],
            "initialized_at": time.time(),
        },
        public=True,
    )


def _admit_maintenance_protocol(config: Config, intent: Intent, journal: dict) -> None:
    """Require protocol 1 in current and target signed source without importing it."""
    if journal.get("maintenance_protocol_checked"):
        return
    old_version = (
        str(journal["old_identity"]["version"])
        if journal.get("old_identity")
        else current_release(config).name
    )
    old = verify_existing_release(config, old_version)
    current_manifest = _load_release_manifest(old, old.name)
    if not _supports_signed_maintenance_protocol(
        old, current_manifest["expected_files"], protected=True
    ):
        raise UpdaterError("update_maintenance_protocol_unsupported")
    if intent.action == "apply" and not journal.get("target_identity"):
        _trusted_signing_key()
        try:
            _, target_files = load_verified_candidate(
                intent.candidate_path, intent.version, intent.manifest_sha256
            )
        except PlatformUpdateError as exc:
            raise UpdaterError(exc.code) from exc
        target = Path(intent.candidate_path).resolve(strict=True)
        protected = False
    else:
        target = verify_existing_release(config, intent.version)
        target_files = _load_release_manifest(target, intent.version)["expected_files"]
        protected = True
    if not _supports_signed_maintenance_protocol(
        target, target_files, protected=protected
    ):
        raise UpdaterError("update_maintenance_protocol_unsupported")
    journal["maintenance_protocol_checked"] = True
    _save_journal(config, journal)


def _parse_cli(argv: list[str]) -> tuple[str, tuple[Path, ...]]:
    if not argv:
        return "process", ()
    if argv == ["--initialize"]:
        return "initialize", ()
    if len(argv) == 4 and argv[0] == BASELINE_IMPORT_OPTION:
        return "import", tuple(_baseline_input_path(value) for value in argv[1:])
    raise UpdaterError("update_cli_invalid")


def main() -> int:
    lock_descriptor = None
    intent = None
    config = None
    try:
        action, arguments = _parse_cli(list(sys.argv[1:]))
        config = Config.from_env()
        validate_layout(config)
        if os.geteuid() != 0:
            raise UpdaterError("update_root_required")
        lock_descriptor = acquire_lock(config)
        if action == "initialize":
            initialize_layout(config)
            return 0
        if action == "import":
            import_trusted_baseline(config, *arguments)
            return 0
        try:
            intent, _ = claim_intent(config)
        except UpdaterError as exc:
            if exc.code == "update_intent_missing":
                return 0
            raise
        journal = _read_journal(config, intent.operation_id)
        if journal["phase"] not in TERMINAL_PHASES or journal["phase"] == "recovery_failed":
            try:
                _admit_maintenance_protocol(config, intent, journal)
            except (UpdaterError, PlatformUpdateError, OSError) as exc:
                projection = _DatabaseProjection(config)
                try:
                    unsafe = journal.get("maintenance_active") or journal.get("services_may_be_stopped")
                    phase = "recovery_failed" if unsafe else "failed"
                    journal["maintenance_active"] = bool(unsafe)
                    _transition(config, journal, projection, phase, error_code=_safe_error(exc), recovery=True)
                    _finish(config, journal)
                finally:
                    projection.close()
                _remove_consumed_intent(config, intent)
                return 0
        try:
            process_intent(config, intent)
        except (UpdaterError, PlatformUpdateError, OSError, sqlite3.Error):
            journal = _read_journal(config, intent.operation_id)
            if journal["phase"] not in TERMINAL_PHASES:
                raise
            # A published terminal failure is handled, not a systemd restart loop.
        _remove_consumed_intent(config, intent)
        return 0
    except (UpdaterError, PlatformUpdateError, OSError, sqlite3.Error, ValueError) as exc:
        if (lock_descriptor is not None and intent is None
                and getattr(exc, "code", "") in {"update_intent_invalid", "update_file_changed", "update_intent_expired", "update_nonce_conflict", "update_recovery_required"}):
            try:
                if _discard_rejected_intent(config, _safe_error(exc)):
                    return 0
            except (UpdaterError, OSError):
                pass
        return 1
    finally:
        # No app-file deletion before lock acquisition, and no deletion on an
        # interrupted stage. The journal remains sufficient if app files vanish.
        if lock_descriptor is not None:
            os.close(lock_descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
