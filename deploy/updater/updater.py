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
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests


SCRIPT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = SCRIPT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from db import DB  # noqa: E402
from platform_update import (  # noqa: E402
    CACHED_MANIFEST_FILE,
    CACHED_SIGNATURE_FILE,
    INTERNAL_CANDIDATE_FILES,
    MARKER_FILE,
    MAX_FILE_BYTES,
    PlatformUpdateError,
    SemVer,
    _read_secure_file,
    _release_from_marker,
    _validate_release_path,
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

    @classmethod
    def from_env(cls) -> "Config":
        state = Path(os.environ.get("SAAS_STATE_DIR", "/var/lib/xianyu-saas").strip())
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
            intent_file=Path(
                os.environ.get(
                    "SAAS_UPDATE_INTENT_FILE", str(state / "update-intents" / "intent.json")
                ).strip()
            ),
            lock_file=Path(
                os.environ.get("SAAS_UPDATE_LOCK_FILE", str(state / "updater.lock")).strip()
            ),
            backup_dir=Path(
                os.environ.get("SAAS_UPDATE_BACKUP_DIR", str(state / "backups")).strip()
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
    if not config.api_service or not config.consumer_service:
        raise UpdaterError("update_service_name_invalid")


def _secure_directory(path: Path, mode: int = 0o700) -> None:
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise UpdaterError("update_directory_invalid")
    os.chmod(path, mode)


def acquire_lock(config: Config):
    _secure_directory(config.lock_file.parent)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(config.lock_file, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise UpdaterError("update_already_running") from exc
    return descriptor


def _processing_file(config: Config) -> Path:
    return config.intent_file.with_name("intent.processing.json")


def _read_private_json(path: Path, max_bytes: int = INTENT_MAX_BYTES) -> dict:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise UpdaterError("update_intent_missing") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o077
        or metadata.st_size <= 0
        or metadata.st_size > max_bytes
    ):
        raise UpdaterError("update_intent_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != metadata.st_size:
                raise UpdaterError("update_intent_invalid")
            raw = os.read(descriptor, max_bytes + 1)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise UpdaterError("update_intent_invalid") from exc
    if len(raw) > max_bytes:
        raise UpdaterError("update_intent_invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdaterError("update_intent_invalid") from exc
    if not isinstance(payload, dict):
        raise UpdaterError("update_intent_invalid")
    return payload


def claim_intent(config: Config) -> tuple[Intent, Path]:
    _secure_directory(config.intent_file.parent)
    processing = _processing_file(config)
    if processing.exists() or processing.is_symlink():
        source = processing
    else:
        if not config.intent_file.exists() and not config.intent_file.is_symlink():
            raise UpdaterError("update_intent_missing")
        try:
            os.replace(config.intent_file, processing)
        except OSError as exc:
            raise UpdaterError("update_intent_claim_failed") from exc
        source = processing
    payload = _read_private_json(source)
    allowed = {
        "schema",
        "action",
        "version",
        "channel",
        "candidate_path",
        "manifest_sha256",
        "requested_by",
        "requested_at",
        "nonce",
    }
    if set(payload) != allowed or payload.get("schema") != 1:
        raise UpdaterError("update_intent_invalid")
    action = str(payload.get("action", ""))
    version = str(payload.get("version", ""))
    channel = str(payload.get("channel", ""))
    candidate_path = str(payload.get("candidate_path", ""))
    manifest_sha256 = str(payload.get("manifest_sha256", ""))
    nonce = str(payload.get("nonce", ""))
    try:
        requested_by = int(payload.get("requested_by"))
        requested_at = float(payload.get("requested_at"))
        SemVer.parse(version)
    except (TypeError, ValueError, PlatformUpdateError) as exc:
        raise UpdaterError("update_intent_invalid") from exc
    if (
        action not in {"apply", "rollback"}
        or channel not in {"stable", "beta"}
        or requested_by <= 0
        or requested_at <= 0
        or len(nonce) != 32
        or any(character not in "0123456789abcdef" for character in nonce)
    ):
        raise UpdaterError("update_intent_invalid")
    if action == "apply":
        if not candidate_path or len(manifest_sha256) != 64:
            raise UpdaterError("update_intent_invalid")
    elif candidate_path or manifest_sha256:
        raise UpdaterError("update_intent_invalid")
    return (
        Intent(
            action=action,
            version=version,
            channel=channel,
            candidate_path=candidate_path,
            manifest_sha256=manifest_sha256,
            requested_by=requested_by,
            nonce=nonce,
        ),
        source,
    )


def current_release(config: Config) -> Path:
    try:
        metadata = config.current_link.lstat()
    except OSError as exc:
        raise UpdaterError("update_current_missing") from exc
    if not stat.S_ISLNK(metadata.st_mode):
        raise UpdaterError("update_current_invalid")
    try:
        target = config.current_link.resolve(strict=True)
        releases = config.releases_dir.resolve(strict=True)
    except OSError as exc:
        raise UpdaterError("update_current_invalid") from exc
    if not _within(target, releases) or target.parent != releases or not target.is_dir():
        raise UpdaterError("update_current_invalid")
    return target


def _load_release_manifest(release_root: Path, version: str) -> dict[str, object]:
    marker = _read_private_json(release_root / MARKER_FILE, max_bytes=8192)
    if marker.get("schema") != 1 or str(marker.get("version", "")) != version:
        raise UpdaterError("update_release_invalid")
    try:
        manifest_raw = _read_secure_file(
            release_root / CACHED_MANIFEST_FILE, 1024 * 1024
        )
        signature_raw = _read_secure_file(
            release_root / CACHED_SIGNATURE_FILE, 4096
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
        or source_metadata.st_size != size
    ):
        raise UpdaterError("update_candidate_invalid")
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o755 if expected.get("executable") else 0o644)
    digest = hashlib.sha256()
    total = 0
    try:
        source_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
        source_fd = os.open(source, source_flags)
        try:
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
        finally:
            os.close(source_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if total != size or digest.hexdigest() != expected_digest:
        destination.unlink(missing_ok=True)
        raise UpdaterError("update_candidate_changed")


def materialize_release(config: Config, intent: Intent) -> Path:
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
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{intent.version}-", dir=config.releases_dir)
    )
    temporary.chmod(0o755)
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
                os.fsync(output.fileno())
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
            or source_metadata.st_size != expected.size
        ):
            raise UpdaterError("update_release_invalid")
        digest = hashlib.sha256(_read_secure_file(source, expected.size)).hexdigest()
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


def backup_database(config: Config, version: str, worker_intents: list[dict]) -> Path:
    _secure_directory(config.backup_dir)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = config.backup_dir / f"saas-{stamp}-{time.time_ns()}-before-{version}.db"
    metadata = backup.with_suffix(".json")
    try:
        database_metadata = config.database_path.lstat()
        if stat.S_ISLNK(database_metadata.st_mode) or not stat.S_ISREG(database_metadata.st_mode):
            raise UpdaterError("update_database_backup_failed")
        source_uri = config.database_path.resolve(strict=True).as_uri() + "?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=30)
        destination = sqlite3.connect(backup)
        try:
            source.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).lower() != "ok":
                raise sqlite3.DatabaseError("backup integrity check failed")
            destination.commit()
        finally:
            destination.close()
            source.close()
        os.chmod(backup, 0o600)
        encoded = json.dumps(
            {
                "schema": 1,
                "created_at": time.time(),
                "target_version": version,
                "worker_intents": worker_intents,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(metadata, flags, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        directory_fd = os.open(
            config.backup_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, sqlite3.Error) as exc:
        backup.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)
        raise UpdaterError("update_database_backup_failed") from exc
    prune_backups(config)
    return backup


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
    environment = _child_environment({"SAAS_DB": str(config.database_path)})
    completed = subprocess.run(
        command,
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
            if (
                health.status_code == 200
                and _response_json(health).get("ok") is True
                and ready.status_code == 200
                and _response_json(ready).get("database") == "ready"
                and unauthenticated.status_code == 401
                and version_response.status_code == 200
                and _response_json(version_response).get("version") == version
                and index.status_code == 200
                and str(_response_json(version_response).get("asset_version", ""))
                in str(getattr(index, "text", ""))
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
) -> None:
    database.upsert_platform_update(
        intent.version,
        intent.channel,
        status,
        manifest_sha256=intent.manifest_sha256,
        candidate_path=candidate_path or intent.candidate_path,
        error_code=error_code,
        requested_by=intent.requested_by,
    )


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
    database = database or DB(str(config.database_path))
    old_release: Path | None = None
    target: Path | None = None
    created_release = intent.action == "apply"
    switched = False
    services_stopped = False
    try:
        update_status(database, intent, "preparing")
        old_release = current_release(config)
        if old_release.name == intent.version:
            raise UpdaterError("update_version_already_current")
        try:
            requested_version = SemVer.parse(intent.version)
            installed_version = SemVer.parse(old_release.name)
        except PlatformUpdateError as exc:
            raise UpdaterError(exc.code) from exc
        comparison = requested_version.compare(installed_version)
        if intent.action == "apply" and comparison <= 0:
            raise UpdaterError("update_downgrade_rejected")
        if intent.action == "rollback" and comparison >= 0:
            raise UpdaterError("update_rollback_target_invalid")
        if created_release:
            target = materialize_release(config, intent)
        else:
            target = verify_existing_release(config, intent.version)
        worker_intents = snapshot_worker_intents(config.database_path)
        update_status(database, intent, "stopping")
        services_stopped = True
        stop_services(config, runner)
        backup_database(config, intent.version, worker_intents)
        if intent.action == "apply":
            update_status(database, intent, "migrating")
            run_migrations(target, config, runner)
        update_status(database, intent, "switching")
        switch_current(config, target)
        switched = True
        # Mark the group as potentially running before the first start command;
        # if a later service start fails, rollback will stop the partial group.
        services_stopped = False
        start_services(config, runner)
        update_status(database, intent, "verifying")
        check_health(config, intent.version, health_client)
        terminal = "succeeded" if intent.action == "apply" else "rolled_back"
        update_status(database, intent, terminal)
        prune_releases(config)
        return {
            "ok": True,
            "action": intent.action,
            "version": intent.version,
            "previous_version": old_release.name,
        }
    except BaseException as exc:
        error_code = str(getattr(exc, "code", "update_failed"))[:80]
        if switched and old_release is not None:
            try:
                if not services_stopped:
                    services_stopped = True
                    stop_services(config, runner)
                switch_current(config, old_release)
                services_stopped = False
                start_services(config, runner)
                check_health(config, old_release.name, health_client)
                update_status(database, intent, "rolled_back", error_code=error_code)
            except BaseException:
                try:
                    update_status(
                        database,
                        intent,
                        "failed",
                        error_code="update_rollback_failed",
                    )
                except BaseException:
                    pass
        else:
            if services_stopped:
                try:
                    services_stopped = False
                    start_services(config, runner)
                except BaseException:
                    error_code = "update_service_restore_failed"
            try:
                update_status(database, intent, "failed", error_code=error_code)
            except BaseException:
                pass
        if created_release and target is not None:
            try:
                active = current_release(config)
                if active.resolve() != target.resolve():
                    shutil.rmtree(target, ignore_errors=True)
            except (OSError, UpdaterError):
                shutil.rmtree(target, ignore_errors=True)
        raise


def main() -> int:
    config = Config.from_env()
    lock_descriptor = None
    processing = _processing_file(config)
    try:
        validate_layout(config)
        lock_descriptor = acquire_lock(config)
        intent, processing = claim_intent(config)
        process_intent(config, intent)
        return 0
    except (UpdaterError, PlatformUpdateError, OSError, sqlite3.Error):
        return 1
    finally:
        try:
            processing.unlink(missing_ok=True)
        except OSError:
            pass
        if lock_descriptor is not None:
            os.close(lock_descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
