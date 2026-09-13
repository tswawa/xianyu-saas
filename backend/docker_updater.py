#!/usr/bin/env python3
"""Root-only, file-IPC Docker updater. No HTTP server and no application imports.

The engine is injected: contract tests use an in-memory Engine and Clock. Only
main() creates the Unix-socket transport. Compose settings and execution records
are private; the shared projection contains no environment or paths.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docker_update_protocol import extract_verified_source, load_docker_public_key, verify_docker_manifest
from update_maintenance import supports_maintenance_protocol

OPID = re.compile(r"[0-9a-f]{32}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z")
TERMINAL = frozenset({"succeeded", "rolled_back", "failed", "recovery_failed"})
CRITICAL = frozenset({"stopping", "switching", "verifying", "rolling_back"})
OWNERSHIP_DRIFT = frozenset({"update_container_context_changed", "update_image_alias_drift", "update_compose_config_changed", "update_compose_resource_changed"})
REQUEST_FIELDS = frozenset({"schema", "operation_id", "action", "version", "expected_current_version", "manifest_sha256", "requested_at", "requested_by"})
MAX_REGISTRATION_BYTES = 16 * 1024 * 1024
ROLE = "io.xianyu.updates.role"
PROJECT = "com.docker.compose.project"
SERVICE = "com.docker.compose.service"
OP_LABEL = "io.xianyu.updates.operation"


class UpdaterError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Clock:
    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class Config:
    shared: Path
    private: Path
    public_key: Path
    self_id: str
    app_service: str = "xianyu-saas"
    updater_service: str = "xianyu-updater"
    app_uid: int = 10001
    request_ttl: float = 600
    drain_timeout: float = 120
    health_timeout: float = 120
    # This is constructor-only for native FakeEngine tests, NEVER an env flag.
    enforce_permissions: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            shared=Path(os.environ.get("SAAS_DOCKER_UPDATE_ROOT", "/updates")),
            private=Path(os.environ.get("SAAS_DOCKER_UPDATER_STATE_DIR", "/var/lib/xianyu-updater")),
            public_key=Path(os.environ.get("SAAS_UPDATE_PUBLIC_KEY_FILE", "/app/update-signing.pub")),
            self_id=os.environ.get("HOSTNAME", ""),
            app_service=os.environ.get("SAAS_DOCKER_APP_SERVICE", "xianyu-saas"),
            health_timeout=max(10, min(600, float(os.environ.get("SAAS_DOCKER_UPDATER_HEALTH_TIMEOUT", "120")))),
        )


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdaterError("update_duplicate_json_key")
        result[key] = value
    return result


def decode(raw: bytes) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError) as error:
        raise UpdaterError("update_invalid_json") from error
    if not isinstance(value, dict):
        raise UpdaterError("update_invalid_json")
    return value


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def atomic_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict, public: bool = False) -> None:
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644 if public else 0o600)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o644 if public else 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def _regular(path: Path, limit: int, *, owner: int | None = None) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise UpdaterError("update_unsafe_file")
        if owner is not None and (info.st_uid != owner or info.st_mode & 0o022):
            raise UpdaterError("update_unsafe_permissions")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise UpdaterError("update_unsafe_file")
        return value
    finally:
        os.close(fd)


def _version_key(value: str):
    match = SEMVER.fullmatch(value)
    if not match:
        raise UpdaterError("update_invalid_version")
    pre = match[4]
    if pre and any(part.isdigit() and len(part) > 1 and part[0] == "0" for part in pre.split(".")):
        raise UpdaterError("update_invalid_version")
    return tuple(int(match[i]) for i in (1, 2, 3)) + ((1,) if not pre else (0, tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split("."))),)


def validate_request(value: dict, operation_id: str, now: float, ttl: float) -> dict:
    if set(value) != REQUEST_FIELDS or type(value.get("schema")) is not int or value["schema"] != 1:
        raise UpdaterError("update_invalid_request")
    if not OPID.fullmatch(operation_id) or value.get("operation_id") != operation_id:
        raise UpdaterError("update_invalid_operation_id")
    if value.get("action") not in {"apply", "rollback"}:
        raise UpdaterError("update_invalid_action")
    for name in ("version", "expected_current_version"):
        if not isinstance(value.get(name), str):
            raise UpdaterError("update_invalid_version")
        _version_key(value[name])
    if not isinstance(value.get("manifest_sha256"), str) or not DIGEST.fullmatch(value["manifest_sha256"]):
        raise UpdaterError("update_invalid_digest")
    when = value.get("requested_at")
    if type(when) not in (int, float) or not math.isfinite(when) or when <= 0 or when > now + 30 or now - when > ttl:
        raise UpdaterError("update_request_expired")
    if type(value.get("requested_by")) is not int or value["requested_by"] <= 0:
        raise UpdaterError("update_invalid_admin")
    return value


class Store:
    def __init__(self, config: Config):
        self.config = config
        self.root = config.private
        self.status = config.shared / "status"
        if not all(p.is_absolute() for p in (config.shared, config.private, config.public_key)):
            raise UpdaterError("update_absolute_paths_required")
        if config.shared == config.private or config.shared in config.private.parents or config.private in config.shared.parents:
            raise UpdaterError("update_private_shared_overlap")
        if config.enforce_permissions and (os.name != "posix" or os.geteuid() != 0):
            raise UpdaterError("update_root_required")
        for path, mode, owner in (
            (config.private, 0o700, 0), (config.shared, 0o755, 0),
            (self.status, 0o755, 0), (self.status / "operations", 0o755, 0),
            (config.shared / "requests", 0o700, config.app_uid),
            (config.shared / "artifacts", 0o700, config.app_uid),
            (config.private / "operations", 0o700, 0),
            (config.private / "packages", 0o700, 0),
            (config.private / "logs", 0o700, 0),
            (config.private / "compose", 0o700, 0),
        ):
            self.directory(path, mode, owner)

    def directory(self, path: Path, mode: int, owner: int = 0):
        for parent in [path, *path.parents]:
            if parent.exists() and parent.is_symlink():
                raise UpdaterError("update_symlink_directory")
        existed = path.exists()
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise UpdaterError("update_unsafe_directory")
        if self.config.enforce_permissions:
            # Empty new Docker volumes start root-owned. Never repair app-owned status.
            if existed and owner == 0 and (info.st_uid != 0 or info.st_mode & 0o022):
                raise UpdaterError("update_unsafe_permissions")
            os.chown(path, owner, owner)
            os.chmod(path, mode)

    @contextlib.contextmanager
    def lock(self):
        path = self.root / "executor.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "posix":
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise UpdaterError("update_executor_busy") from error
            else:
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, 0)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    raise UpdaterError("update_executor_busy") from error
            yield
        finally:
            os.close(fd)

    def load(self, path: Path) -> dict:
        return decode(_regular(path, 16 * 1024 * 1024, owner=0 if self.config.enforce_permissions else None))

    def journal(self, operation_id: str) -> Path:
        if not OPID.fullmatch(operation_id):
            raise UpdaterError("update_invalid_operation_id")
        return self.root / "operations" / (operation_id + ".json")

    @property
    def registration_path(self) -> Path:
        return self.root / "docker-deployment.json"

    @property
    def compose_file(self) -> Path:
        return self.root / "compose" / "runtime.json"

    @property
    def trusted_key(self) -> Path:
        return self.root / "trusted-update-signing.pub"

    def save(self, journal: dict, now: float):
        journal["updated_at"] = now
        atomic_json(self.journal(journal["operation_id"]), journal)
        self.project(journal)

    def project(self, journal: dict):
        public = {key: journal.get(key, "") for key in (
            "operation_id", "action", "version", "current_version", "phase", "updated_at", "error_code"
        )}
        public.update(schema=1, status=journal["phase"] if journal["phase"] in TERMINAL else "running")
        if journal["phase"] == "queued":
            public["status"] = "queued"
        atomic_json(self.status / "operations" / (journal["operation_id"] + ".json"), public, True)

    def maintenance(self, journal: dict, active: bool, now: float):
        atomic_json(self.status / "maintenance.json", {
            "schema": 1, "operation_id": journal["operation_id"], "active": active,
            "phase": journal["phase"], "updated_at": now,
        }, True)

    def copy_artifacts(self, operation_id: str, public_key: bytes, expected_version: str, expected_digest: str):
        destination = self.root / "packages" / operation_id
        # An interrupted copy is discarded; no build starts before a complete private verify.
        if destination.exists():
            shutil.rmtree(destination)
        self.directory(destination, 0o700)
        artifact_dir = self.config.shared / "artifacts" / operation_id
        if artifact_dir.is_symlink() or not artifact_dir.is_dir():
            raise UpdaterError("update_unsafe_artifacts")
        directory_fd = None
        try:
            if os.name == "posix":
                directory_fd = os.open(artifact_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for name, limit in (("docker.manifest.json", 64 * 1024), ("docker.manifest.sig", 4096), ("source.zip", 512 * 1024 * 1024)):
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
                fd = os.open(name, flags, dir_fd=directory_fd) if directory_fd is not None else os.open(artifact_dir / name, flags)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limit:
                        raise UpdaterError("update_unsafe_artifacts")
                    with os.fdopen(fd, "rb", closefd=False) as source, (destination / name).open("xb") as target:
                        remaining = limit
                        while chunk := source.read(min(1024 * 1024, remaining + 1)):
                            remaining -= len(chunk)
                            if remaining < 0:
                                raise UpdaterError("update_artifact_too_large")
                            target.write(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                    os.chmod(destination / name, 0o600)
                finally:
                    os.close(fd)
            raw = (destination / "docker.manifest.json").read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_digest:
                raise UpdaterError("update_manifest_mismatch")
            manifest = verify_docker_manifest(raw, (destination / "docker.manifest.sig").read_bytes(), public_key, expected_version=expected_version)
            source_root = extract_verified_source(destination / "source.zip", destination / "source", manifest)
            return manifest, source_root
        finally:
            if directory_fd is not None:
                os.close(directory_fd)


class Updater:
    def __init__(self, config: Config, engine, clock=None):
        self.config = config
        self.engine = engine
        self.clock = clock or Clock()
        self.store = Store(config)
        self.session = uuid.uuid4().hex
        self.deployment: dict = {}
        self.registration: dict = {}
        self.capability: dict = {"schema": 1, "protocol": 1, "ready": False, "reason": "update_initializing", "deployment_id": "", "current_version": "", "rollback_versions": []}

    def _set_key_identity(self, raw: bytes) -> bytes:
        key = load_docker_public_key(raw)
        self.capability["public_key_sha256"] = hashlib.sha256(key.public_bytes_raw()).hexdigest()
        return raw

    def _bootstrap_key(self) -> bytes:
        mount = self.deployment.get("public_key_mount") or {}
        if mount.get("RW") or mount.get("Type") not in {"bind", "volume"}:
            raise UpdaterError("update_public_key_mount_mismatch")
        # Root explicitly performs this one-time trust ceremony. Copy the verified
        # read-only mount into updater-private storage so later host/NTFS changes
        # cannot replace the executor's trust root.
        raw = self._set_key_identity(_regular(self.config.public_key, 16 * 1024))
        atomic_bytes(self.store.trusted_key, raw)
        return raw

    def _key(self) -> bytes:
        if not self.store.trusted_key.exists():
            raise UpdaterError("update_compose_registration_invalid")
        return self._set_key_identity(_regular(self.store.trusted_key, 16 * 1024, owner=0 if self.config.enforce_permissions else None))

    @staticmethod
    def _data_version(metadata):
        value = metadata.get("update_data_version")
        if type(value) is not int or value < 1:
            raise UpdaterError("update_data_version_unknown")
        return value

    def _history(self) -> dict:
        path = self.config.private / "history.json"
        return self.store.load(path) if path.exists() else {"schema": 1, "images": []}

    def _journals(self) -> list[dict]:
        return [self.store.load(p) for p in sorted((self.config.private / "operations").glob("*.json"))]

    def _base_identity(self):
        deployment = self.engine.identity(
            self.config.self_id,
            self.config.app_service,
            self.config.updater_service,
            str(self.config.public_key),
        )
        project = deployment["project"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
            raise UpdaterError("update_invalid_compose_project")
        deployment["compose_directory"] = self.store.compose_file.parent
        deployment["compose_file"] = self.store.compose_file
        return deployment

    def initialize(self, compose_config: dict):
        """One-time root-only registration of a host-rendered Compose JSON model."""
        with self.store.lock():
            if self.store.registration_path.exists() or self._journals() or (self.config.private / "history.json").exists():
                raise UpdaterError("update_compose_initialization_conflict")
            deployment = self._base_identity()
            self.deployment = deployment
            candidates = self.engine.targets(deployment)
            if len(candidates) != 1:
                raise UpdaterError("update_target_not_unique")
            registration, model = self.engine.register(compose_config, deployment, candidates[0])
            self._bootstrap_key()
            atomic_json(self.store.compose_file, model)
            atomic_json(self.store.registration_path, registration)
            self.deployment = deployment
            self.registration = registration
            self.deployment["alias"] = registration["alias"]
            self.deployment["unset_environment"] = registration["unset_environment"]
            self.deployment["inherited_config_fields"] = registration["inherited_config_fields"]
            self.capability["deployment_id"] = registration["project"] + ":" + registration["service"]
            return {"schema": 1, "protocol": 1, "deployment_id": self.capability["deployment_id"], "compose_version": registration["compose_version"]}

    def _identity(self):
        if not self.store.registration_path.exists() and not self.store.compose_file.exists():
            raise UpdaterError("update_compose_not_initialized")
        if not self.store.registration_path.exists() or not self.store.compose_file.exists():
            raise UpdaterError("update_compose_registration_invalid")
        deployment = self._base_identity()
        registration = self.store.load(self.store.registration_path)
        model = self.store.load(self.store.compose_file)
        self.engine.validate_registration(registration, model, deployment)
        deployment["alias"] = registration["alias"]
        deployment["unset_environment"] = registration["unset_environment"]
        deployment["inherited_config_fields"] = registration["inherited_config_fields"]
        self.deployment = deployment
        self.registration = registration
        self.capability["deployment_id"] = registration["project"] + ":" + registration["service"]

    def _validate_registration(self):
        model = self.store.load(self.store.compose_file)
        self.engine.validate_registration(self.registration, model, self.deployment)

    def _target(self):
        candidates = self.engine.targets(self.deployment)
        if len(candidates) != 1:
            raise UpdaterError("update_target_not_unique")
        target = candidates[0]
        self.engine.validate_target(target, self.deployment, self.config, self.registration)
        return target

    def publish_capability(self):
        value = dict(self.capability)
        value["heartbeat_at"] = self.clock.time()
        atomic_json(self.store.status / "capabilities.json", value, True)
        return value

    def refresh_capability(self):
        try:
            self._identity()
            self._key()
            active = [j for j in self._journals() if j["phase"] not in TERMINAL or j["phase"] == "recovery_failed"]
            if active:
                self.capability.update(ready=False, reason="update_recovery_failed" if active[0]["phase"] == "recovery_failed" else "update_busy", current_version=active[0].get("current_version", ""))
            else:
                target = self._target()
                metadata = self.engine.metadata(target["Image"])
                _version_key(metadata["version"])
                # A ready capability requires installed maintenance/drain support, not just a socket.
                self.engine.check_protocol(target, self.config)
                current_key = _version_key(metadata["version"])
                data_version = self._data_version(metadata)
                versions = [{"version": record["version"], "manifest_sha256": record["manifest_sha256"]}
                            for record in self._history()["images"]
                            if _version_key(record["version"]) < current_key
                            and type(record.get("metadata", {}).get("update_data_version")) is int
                            and record["metadata"]["update_data_version"] == data_version
                            and self.engine.image_exists(record["image_id"])]
                self.capability.update(ready=True, reason="", current_version=metadata["version"], rollback_versions=versions)
        except Exception as error:
            self.capability.update(ready=False, reason=self._error(error))
        return self.publish_capability()

    @staticmethod
    def _error(error):
        # Never project exception text, Docker output, environment, or inspect data.
        code = getattr(error, "code", "update_executor_error")
        return code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code) else "update_executor_error"

    def _owned_alias(self, journal, expected=None):
        """Return the managed alias target only while this operation still owns it."""
        try:
            current = self.engine.image_id(self.deployment["alias"])
        except Exception as error:
            raise UpdaterError("update_image_alias_drift") from error
        allowed = {expected} if expected is not None else {journal.get("old_alias"), journal.get("candidate")}
        allowed.discard(None)
        if current not in allowed:
            raise UpdaterError("update_image_alias_drift")
        return current

    def _pre_stop_context(self, journal):
        """Narrow the drain-to-stop race; Docker has no transactional inspect/CAS."""
        try:
            current = self._target()
            # _target already checks the saved Compose configuration and mounts.
            changed = (current["Id"] != journal["old"]["Id"]
                       or current["Image"] != journal["old_alias"])
        except Exception as error:
            code = self._error(error)
            if code in OWNERSHIP_DRIFT:
                raise UpdaterError(code) from error
            raise UpdaterError("update_container_context_changed") from error
        if changed:
            raise UpdaterError("update_container_context_changed")
        self._owned_alias(journal)
        return current

    def _phase(self, journal, phase):
        journal["phase"] = phase
        self.store.save(journal, self.clock.time())
        self.capability.update(operation_id=journal["operation_id"], phase=phase)
        self.publish_capability()

    def _admit(self, path: Path):
        operation_id = path.stem
        if not OPID.fullmatch(operation_id):
            return None
        previous = self.store.journal(operation_id)
        if previous.exists():
            # An opid is consumed forever, including malformed/expired/failed requests.
            self.store.project(self.store.load(previous))
            return None
        journal = {"schema": 1, "operation_id": operation_id, "action": "", "version": "", "current_version": self.capability.get("current_version", ""), "phase": "queued", "error_code": "", "session": self.session}
        try:
            value = decode(_regular(path, 16 * 1024))
            # Preserve a safe operation binding even when a valid queued intent
            # expires; the API must be able to merge the executor's rejection.
            if value.get("operation_id") == operation_id and value.get("action") in {"apply", "rollback"} and isinstance(value.get("version"), str) and SEMVER.fullmatch(value["version"]):
                journal.update(action=value["action"], version=value["version"])
            validate_request(value, operation_id, self.clock.time(), self.config.request_ttl)
            journal.update(action=value["action"], version=value["version"], request=value, request_sha256=hashlib.sha256(canonical(value)).hexdigest())
            if not self.capability.get("ready"):
                raise UpdaterError(self.capability.get("reason") or "update_not_ready")
            if value["expected_current_version"] != self.capability["current_version"]:
                raise UpdaterError("update_current_version_changed")
            if value["action"] == "apply" and _version_key(value["version"]) <= _version_key(value["expected_current_version"]):
                raise UpdaterError("update_downgrade_rejected")
        except Exception as error:
            journal.update(phase="failed", error_code=self._error(error))
        self.store.save(journal, self.clock.time())
        return journal

    def tick(self):
        """Execute at most one durable phase; restarting in a critical phase restores code."""
        with self.store.lock():
            if not self.deployment:
                self._identity()
            journals = self._journals()
            active = [j for j in journals if j["phase"] not in TERMINAL]
            if len(active) > 1:
                raise UpdaterError("update_multiple_active_journals")
            if any(j["phase"] == "recovery_failed" for j in journals):
                self.refresh_capability()
                return None
            if active:
                journal = active[0]
            else:
                self.refresh_capability()
                journal = None
                for path in sorted((self.config.shared / "requests").glob("*.json")):
                    journal = self._admit(path)
                    if journal is not None:
                        break
                if not journal or journal["phase"] in TERMINAL:
                    return journal
            self.capability.update(ready=False, reason="update_busy", current_version=journal["current_version"])
            self.publish_capability()
            if journal.get("unsafe_ownership_drift"):
                self._recover(journal)
                return journal
            if journal["phase"] in CRITICAL and journal.get("session") != self.session:
                journal["error_code"] = "update_interrupted"
                self._recover(journal)
                return journal
            journal["session"] = self.session
            try:
                self._advance(journal)
            except Exception as error:
                journal["error_code"] = self._error(error)
                if journal.get("maintenance_started") and journal["error_code"] in OWNERSHIP_DRIFT:
                    journal["unsafe_ownership_drift"] = True
                    self.store.save(journal, self.clock.time())
                    self._recover(journal)
                elif journal["phase"] in CRITICAL or journal.get("disrupted"):
                    self._recover(journal)
                else:
                    self._phase(journal, "failed")
                    self.store.maintenance(journal, False, self.clock.time())
            if journal["phase"] in TERMINAL:
                self.refresh_capability()
            return journal

    @staticmethod
    def _maintenance_source(root: Path):
        """Check signed source bytes with the trusted static helper; never import them."""
        try:
            source = _regular(root / "backend/update_maintenance.py", 256 * 1024)
            supported = supports_maintenance_protocol(source)
        except (OSError, UpdaterError) as error:
            raise UpdaterError("update_maintenance_protocol_unsupported") from error
        if not supported:
            raise UpdaterError("update_maintenance_protocol_unsupported")

    def _advance(self, journal):
        self._validate_registration()
        phase = journal["phase"]
        if phase == "queued":
            self._phase(journal, "verifying_package")
        elif phase == "verifying_package":
            req = journal["request"]
            if journal["action"] == "rollback":
                candidates = [record for record in self._history()["images"] if record["version"] == req["version"] and record["manifest_sha256"] == req["manifest_sha256"]]
                if len(candidates) != 1:
                    raise UpdaterError("update_rollback_not_verified")
                record = candidates[0]
                package = self.config.private / "packages" / record["operation_id"]
                raw = _regular(package / "docker.manifest.json", 64 * 1024)
                manifest = verify_docker_manifest(raw, _regular(package / "docker.manifest.sig", 4096), self._key(), expected_version=req["version"])
                if hashlib.sha256(raw).hexdigest() != req["manifest_sha256"] or not self.engine.image_exists(record["image_id"]):
                    raise UpdaterError("update_rollback_not_verified")
                self._maintenance_source(package / "source" / ("xianyu-saas-" + manifest.version))
                journal.update(candidate=record["image_id"], metadata=record["metadata"], commit=manifest.commit)
            else:
                manifest, root = self.store.copy_artifacts(journal["operation_id"], self._key(), req["version"], req["manifest_sha256"])
                self._maintenance_source(root)
                # Extraction deliberately uses private 0700 directories. COPY must
                # not bake those modes into an unreadable UID10001 runtime image;
                # all enclosing private package/source parents remain root-only.
                for directory in [root, *(p for p in root.rglob("*") if p.is_dir())]:
                    os.chmod(directory, 0o755)
                journal.update(source_root=str(root), commit=manifest.commit)
            self._phase(journal, "building")
        elif phase == "building":
            if journal["action"] == "apply":
                candidate = self.engine.build(Path(journal["source_root"]), self.deployment, journal["operation_id"], journal["commit"], self.config.private / "logs" / (journal["operation_id"] + ".build.log"))
                journal["candidate"] = candidate
            metadata = self.engine.metadata(journal["candidate"])
            if metadata.get("version") != journal["version"] or metadata.get("commit") != journal["commit"] or not metadata.get("asset_version"):
                raise UpdaterError("update_image_identity_mismatch")
            journal["metadata"] = metadata
            self._phase(journal, "preflighting")
        elif phase == "preflighting":
            target = self._target()
            original_metadata = self.engine.metadata(target["Image"])
            if original_metadata["version"] != journal["request"]["expected_current_version"]:
                raise UpdaterError("update_current_version_changed")
            old_alias = self.engine.image_id(self.deployment["alias"])
            if old_alias != target["Image"]:
                raise UpdaterError("update_image_alias_drift")
            self.engine.check_protocol(target, self.config)
            # Signed releases declare whether both versions can use the same data.
            # Unknown/breaking formats require a manual migration, not a data restore.
            if self._data_version(original_metadata) != self._data_version(journal["metadata"]):
                raise UpdaterError("update_data_backward_incompatible")
            self.engine.preflight(target, journal["candidate"], self.deployment)
            recovery_alias = self.deployment["project"] + "-" + self.deployment["service"] + ":recovery-" + journal["operation_id"]
            journal.update(old={"Id": target["Id"]}, old_metadata=original_metadata,
                           old_alias=old_alias, recovery_alias=recovery_alias)
            self.store.save(journal, self.clock.time())
            self.engine.retain(old_alias, recovery_alias)
            journal["old_image_retained"] = True
            self.store.save(journal, self.clock.time())
            self._phase(journal, "preparing")
        elif phase == "preparing":
            current = self._pre_stop_context(journal)
            if not journal.get("maintenance_started"):
                # Persist intent before publishing the separate maintenance marker;
                # a crash between the two files therefore remains fail-closed.
                journal["maintenance_started"] = True
                self.store.save(journal, self.clock.time())
            self.store.maintenance(journal, True, self.clock.time())
            self.engine.drain(current, journal["operation_id"], self.config)
            self._pre_stop_context(journal)
            journal["disrupted"] = True
            self._phase(journal, "stopping")
        elif phase == "stopping":
            # Persist both sides of the Compose side effect. A restart from this
            # critical phase rebuilds the old image with the private runtime model.
            self._pre_stop_context(journal)
            if not journal.get("compose_stop_started"):
                journal["compose_stop_started"] = True
                self.store.save(journal, self.clock.time())
            self.engine.stop(
                journal["old"], self.deployment, journal["operation_id"],
                self.config.private / "logs" / (journal["operation_id"] + ".compose.log"),
            )
            journal["compose_stop_complete"] = True
            self.store.save(journal, self.clock.time())
            self._phase(journal, "switching")
        elif phase == "switching":
            self.engine.operation_target(journal, self.deployment, self.registration)
            if not journal.get("alias_switch_started"):
                journal["alias_switch_started"] = True
                self.store.save(journal, self.clock.time())
            self._owned_alias(journal, journal["old_alias"])
            self.engine.tag(journal["candidate"], self.deployment["alias"])
            journal["alias_updated"] = True
            self.store.save(journal, self.clock.time())
            journal["compose_up_started"] = True
            self.store.save(journal, self.clock.time())
            target = self.engine.compose_up(
                self.deployment,
                self.registration,
                self.config,
                journal["operation_id"],
                self.config.private / "logs" / (journal["operation_id"] + ".compose.log"),
            )
            journal["new_id"] = target["Id"]
            journal["compose_up_complete"] = True
            self.store.save(journal, self.clock.time())
            self._phase(journal, "verifying")
        elif phase == "verifying":
            target = self.engine.operation_target(journal, self.deployment, self.registration)
            if target is None or target["Id"] != journal.get("new_id"):
                raise UpdaterError("update_container_context_changed")
            self._owned_alias(journal, journal["candidate"])
            self.engine.verify(target["Id"], journal["metadata"], self.config)
            self._owned_alias(journal, journal["candidate"])
            journal["verified"] = True
            self.store.save(journal, self.clock.time())
            history = self._history()
            if journal["action"] == "apply":
                history["images"] = [record for record in history["images"] if record["version"] != journal["version"]] + [{
                    "version": journal["version"], "image_id": journal["candidate"], "operation_id": journal["operation_id"],
                    "manifest_sha256": journal["request"]["manifest_sha256"], "metadata": journal["metadata"],
                }]
            atomic_json(self.config.private / "history.json", history)
            # Commit point precedes opening the write gate; recovery rechecks the image.
            journal["current_version"] = journal["version"]
            journal["committed"] = True
            self.store.save(journal, self.clock.time())
            self.store.maintenance(journal, False, self.clock.time())
            self._phase(journal, "rolled_back" if journal["action"] == "rollback" else "succeeded")

    def _recover(self, journal):
        try:
            if journal.get("unsafe_ownership_drift"):
                code = journal.get("error_code")
                raise UpdaterError(code if code in OWNERSHIP_DRIFT else "update_container_context_changed")
            self._validate_registration()
            # The private model may only converge a missing/old/candidate target that
            # still has this operation's registered configuration and resources.
            current = self.engine.operation_target(journal, self.deployment, self.registration)
            if current is None and not journal.get("compose_up_started") and not journal.get("compose_restore_started"):
                raise UpdaterError("update_container_context_changed")
            self._owned_alias(journal)
            if journal.get("committed"):
                if current is None or current["Image"] != journal["candidate"]:
                    raise UpdaterError("update_container_context_changed")
                self._owned_alias(journal, journal["candidate"])
                self.engine.verify(current["Id"], journal["metadata"], self.config)
                self.store.maintenance(journal, False, self.clock.time())
                journal["error_code"] = ""
                self._phase(journal, "rolled_back" if journal["action"] == "rollback" else "succeeded")
                return
            self._phase(journal, "rolling_back")
            self.store.maintenance(journal, True, self.clock.time())
            if not journal.get("compose_restore_started"):
                journal["compose_restore_started"] = True
                self.store.save(journal, self.clock.time())
            self._owned_alias(journal)
            self.engine.tag(journal["old_alias"], self.deployment["alias"])
            journal["alias_restored"] = True
            self.store.save(journal, self.clock.time())
            current = self.engine.operation_target(journal, self.deployment, self.registration)
            if current is None or current["Image"] != journal["old_alias"] or not current["State"].get("Running") or (current["Config"].get("Labels") or {}).get("com.docker.compose.config-hash") != self.registration["runtime_config_hash"]:
                current = self.engine.compose_up(
                    self.deployment,
                    self.registration,
                    self.config,
                    journal["operation_id"],
                    self.config.private / "logs" / (journal["operation_id"] + ".compose.log"),
                )
            journal["restored_id"] = current["Id"]
            journal["compose_restore_complete"] = True
            self.store.save(journal, self.clock.time())
            self._owned_alias(journal, journal["old_alias"])
            self.engine.verify(current["Id"], journal["old_metadata"], self.config)
            journal["current_version"] = journal["old_metadata"]["version"]
            # A pre-commit history write must not advertise an uncommitted candidate.
            history = self._history()
            history["images"] = [record for record in history["images"] if record["operation_id"] != journal["operation_id"]]
            atomic_json(self.config.private / "history.json", history)
            self.store.maintenance(journal, False, self.clock.time())
            self._phase(journal, "rolled_back")
        except Exception as error:
            code = self._error(error)
            if code in OWNERSHIP_DRIFT:
                journal["error_code"] = code
            journal["recovery_error_code"] = code
            self._phase(journal, "recovery_failed")
            self.store.maintenance(journal, True, self.clock.time())

    def repair_terminal_projection(self):
        """Repair a crash between private commit and public projection without trusting public state."""
        with self.store.lock():
            for journal in self._journals():
                self.store.project(journal)
            active = [j for j in self._journals() if j["phase"] not in TERMINAL or j["phase"] == "recovery_failed"]
            if not active:
                complete = self._journals()
                if complete:
                    latest = max(complete, key=lambda j: j["updated_at"])
                    self.store.maintenance(latest, False, self.clock.time())


def main() -> int:
    from docker_engine import DockerEngine
    os.umask(0o077)
    config = Config.from_env()
    engine = DockerEngine()
    updater = Updater(config, engine)
    if sys.argv[1:]:
        if sys.argv[1:] != ["initialize"]:
            sys.stdout.buffer.write(canonical({"ok": False, "error_code": "update_invalid_cli"}))
            return 2
        try:
            raw = sys.stdin.buffer.read(MAX_REGISTRATION_BYTES + 1)
            if len(raw) > MAX_REGISTRATION_BYTES:
                raise UpdaterError("update_compose_config_too_large")
            result = updater.initialize(decode(raw))
            sys.stdout.buffer.write(canonical({"ok": True, **result}))
            return 0
        except Exception as error:
            sys.stdout.buffer.write(canonical({"ok": False, "error_code": updater._error(error)}))
            return 2
    updater.repair_terminal_projection()
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(5):
            updater.publish_capability()

    worker = threading.Thread(target=heartbeat, name="updater-heartbeat", daemon=True)
    worker.start()
    try:
        while True:
            try:
                updater.tick()
            except Exception as error:
                updater.capability.update(ready=False, reason=updater._error(error))
                updater.publish_capability()
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
