"""Fail-closed first-install transaction for the standalone systemd release."""

from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import io
import json
import math
import os
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from backend.standalone_runtime import (
    StandaloneRuntimeError,
    parse_runtime_metadata,
    validate_standalone_manifest,
)
from .constants import (
    API_SERVICE,
    CONSUMER_SERVICE,
    MANAGER_PROTOCOL,
    PRODUCT,
    UPDATER_PATH_UNIT,
    UPDATER_SERVICE,
)
from .errors import ManagerError
from .runtime import MANAGER_VERSION, manager_file_path, read_embedded_public_key, resource_root
from .updater_bridge import invoke_updater


GITHUB_REPOSITORY = "tswawa/xianyu-saas"
GITHUB_API_ROOT = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})
ASSET_ARCHITECTURES = {"x86_64": "x86_64", "aarch64": "aarch64"}
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?\Z")
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_SIGNATURE_BYTES = 4096
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 200_000
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_DATABASE_BACKUP_BYTES = 64 * 1024 * 1024 * 1024
MAX_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
SYSTEMD_BIN = "/usr/bin/systemctl"
SYSTEMD_ANALYZE_BIN = "/usr/bin/systemd-analyze"
USERADD_BIN = "/usr/sbin/useradd"
HEALTH_URL = "http://127.0.0.1:8096/health"
HEALTH_ATTEMPTS = 30
HEALTH_INTERVAL_SECONDS = 1.0
READY_URL = "http://127.0.0.1:8096/api/ready"
VERSION_URL = "http://127.0.0.1:8096/api/version/public"
PUBLIC_URL = "http://127.0.0.1:8096/xianyu-saas/"
LEGACY_VERSION = "0.4.0"
LEGACY_COMMIT = "779ed3432cf288b7e3fb8697c584f388cf3d79f0"
APPLICATION_UNITS = (API_SERVICE, CONSUMER_SERVICE)
UNITS = (API_SERVICE, CONSUMER_SERVICE, UPDATER_SERVICE, UPDATER_PATH_UNIT)
START_UNITS = (API_SERVICE, CONSUMER_SERVICE, UPDATER_PATH_UNIT)
MARKER_FILE = ".xianyu-release.json"
CACHED_MANIFEST_FILE = ".xianyu-manifest.json"
CACHED_SIGNATURE_FILE = ".xianyu-manifest.sig"
INTERNAL_RELEASE_FILES = frozenset({MARKER_FILE, CACHED_MANIFEST_FILE, CACHED_SIGNATURE_FILE})


@dataclass(frozen=True)
class InstallPaths:
    install_root: Path = Path("/opt/xianyu-saas")
    releases_dir: Path = Path("/opt/xianyu-saas/releases")
    current_link: Path = Path("/opt/xianyu-saas/current")
    manager_releases_dir: Path = Path("/opt/xianyu-saas/manager/releases")
    manager_current_link: Path = Path("/opt/xianyu-saas/manager/current")
    launcher_link: Path = Path("/usr/local/bin/xianyu-saas")
    state_dir: Path = Path("/var/lib/xianyu-saas")
    update_queue_dir: Path = Path("/var/lib/xianyu-saas-updates")
    updater_state_dir: Path = Path("/var/lib/xianyu-saas-updater")
    env_file: Path = Path("/etc/xianyu-saas.env")
    public_key_file: Path = Path("/etc/xianyu-saas/update-signing.pub")
    systemd_dir: Path = Path("/etc/systemd/system")
    logrotate_dir: Path = Path("/etc/logrotate.d")
    legacy_srv_root: Path = Path("/srv/xianyu-saas")
    legacy_state_root: Path = Path("/srv/xianyu-saas-data")

    @property
    def initialization_file(self) -> Path:
        return self.update_queue_dir / "status/initialization.json"

    @property
    def diagnostic_file(self) -> Path:
        return self.updater_state_dir / "install-failure.json"

    @property
    def install_journal_file(self) -> Path:
        return self.updater_state_dir / "install-journal.json"


@dataclass(frozen=True)
class AssetRecord:
    name: str
    size: int
    sha256: str
    kind: str
    platform: str
    architecture: str
    manager_protocol: int


@dataclass(frozen=True)
class VerifiedRelease:
    version: str
    architecture: str
    archive: AssetRecord
    manifest: AssetRecord
    signature: AssetRecord
    manager: AssetRecord
    manifest_sha256: str
    manifest_raw: bytes
    signature_raw: bytes
    manifest_payload: dict
    expected_files: dict[str, dict]
    archive_path: Path


@dataclass(frozen=True)
class VerifiedManager:
    version: str
    architecture: str
    record: AssetRecord
    payload: bytes


@dataclass(frozen=True)
class LegacyLayout:
    kind: str
    root: str = ""
    environment_file: str = ""


@dataclass(frozen=True)
class InstallationState:
    kind: str
    version: str = ""
    release_root: Path | None = None
    legacy: LegacyLayout | None = None


@dataclass(frozen=True)
class UnitSnapshot:
    contents: dict[Path, bytes | None]
    modes: dict[Path, int | None]
    loaded: dict[str, bool]
    active: dict[str, bool]
    unit_file_state: dict[str, str]
    maintenance: bytes | None


@dataclass(frozen=True)
class DatabaseBackup:
    path: Path
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class AccountIdentity:
    uid: int
    gid: int
    home: str
    shell: str
    group: str


class InstallerLike(Protocol):
    def install(self, *, architecture: str, version: str | None = None) -> dict:
        """Install the manager-managed systemd deployment."""


class Filesystem:
    """Small injectable boundary for privileged filesystem mutation."""

    @staticmethod
    def _reject_link_ancestors(path: Path) -> None:
        for candidate in (path, *path.parents):
            if candidate.exists() and (
                candidate.is_symlink()
                or (hasattr(candidate, "is_junction") and candidate.is_junction())
            ):
                raise ManagerError("manager_install_path_unsafe")

    def exists(self, path: Path) -> bool:
        return path.exists() or path.is_symlink()

    def fsync_directory(self, path: Path) -> None:
        if os.name != "posix":
            return
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            os.fsync(descriptor)
        except OSError as exc:
            raise ManagerError("manager_install_file_failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def is_file(self, path: Path) -> bool:
        return path.is_file() and not path.is_symlink()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir() and not path.is_symlink()

    def is_executable(self, path: Path) -> bool:
        try:
            return self.is_file(path) and bool(stat.S_IMODE(path.lstat().st_mode) & 0o111)
        except OSError:
            return False

    def is_secure_executable(self, path: Path) -> bool:
        try:
            mode = stat.S_IMODE(path.lstat().st_mode)
            return self.is_executable(path) and not mode & 0o022
        except OSError:
            return False

    def validate_root_file(self, path: Path, maximum: int) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ManagerError("manager_install_file_invalid") from exc
        if (
            not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or metadata.st_uid != 0 or metadata.st_mode & 0o022 or metadata.st_size > maximum
        ):
            raise ManagerError("manager_install_file_invalid")

    def validate_private_root_file(self, path: Path, maximum: int) -> None:
        self.validate_root_file(path, maximum)
        try:
            if stat.S_IMODE(path.lstat().st_mode) != 0o600:
                raise ManagerError("manager_install_file_invalid")
        except ManagerError:
            raise
        except OSError as exc:
            raise ManagerError("manager_install_file_invalid") from exc

    def read_bytes(self, path: Path, maximum: int | None = None) -> bytes:
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError
            if maximum is not None and metadata.st_size > maximum:
                raise OSError
            raw = path.read_bytes()
        except OSError as exc:
            raise ManagerError("manager_install_file_invalid") from exc
        if maximum is not None and len(raw) > maximum:
            raise ManagerError("manager_install_file_invalid")
        return raw

    def mkdir(self, path: Path, mode: int) -> None:
        self._reject_link_ancestors(path)
        if path.exists():
            if not path.is_dir():
                raise ManagerError("manager_install_path_unsafe")
            return
        missing = []
        current = path
        while not current.exists() and current != current.parent:
            missing.append(current)
            current = current.parent
        if not current.is_dir() or current.is_symlink():
            raise ManagerError("manager_install_path_unsafe")
        for directory in reversed(missing):
            directory.mkdir(exist_ok=False)
            directory.chmod(mode if directory == path else 0o755)
            self.fsync_directory(directory.parent)

    def validate_root_directory(self, path: Path) -> None:
        try:
            for candidate in (path, *path.parents):
                metadata = candidate.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != 0 or metadata.st_mode & 0o022
                ):
                    raise ManagerError("manager_install_path_unsafe")
        except ManagerError:
            raise
        except OSError as exc:
            raise ManagerError("manager_install_path_unsafe") from exc

    def validate_root_ancestor(self, path: Path) -> None:
        candidate = path
        while not candidate.exists() and not candidate.is_symlink() and candidate != candidate.parent:
            candidate = candidate.parent
        self.validate_root_directory(candidate)

    def atomic_write(self, path: Path, payload: bytes, mode: int, *, replace: bool = True) -> None:
        self.mkdir(path.parent, 0o755)
        if not replace and self.exists(path):
            raise ManagerError("manager_install_path_occupied")
        descriptor, temporary_raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_raw)
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            temporary.chmod(mode)
            if not replace and self.exists(path):
                raise ManagerError("manager_install_path_occupied")
            os.replace(temporary, path)
            self.fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def write_exclusive(self, path: Path, payload, mode: int) -> None:
        self.mkdir(path.parent, 0o755)
        if self.exists(path):
            raise ManagerError("manager_archive_path_collision")
        try:
            with path.open("xb") as target:
                shutil.copyfileobj(payload, target, length=1024 * 1024)
            path.chmod(mode)
        except OSError as exc:
            path.unlink(missing_ok=True)
            raise ManagerError("manager_install_file_failed") from exc

    def make_temporary_directory(self, parent: Path) -> Path:
        self.mkdir(parent, 0o755)
        return Path(tempfile.mkdtemp(prefix=".install-", dir=parent))

    def publish_directory(self, source: Path, destination: Path) -> None:
        if self.exists(destination):
            raise ManagerError("manager_install_path_occupied")
        try:
            os.replace(source, destination)
            self.fsync_directory(destination.parent)
        except OSError as exc:
            raise ManagerError("manager_install_file_failed") from exc

    def atomic_symlink(self, link: Path, target: str) -> None:
        self.mkdir(link.parent, 0o755)
        if self.exists(link):
            raise ManagerError("manager_install_path_occupied")
        try:
            link.symlink_to(target)
            self.fsync_directory(link.parent)
        except FileExistsError as exc:
            raise ManagerError("manager_install_path_occupied") from exc
        except OSError as exc:
            raise ManagerError("manager_install_link_failed") from exc

    def replace_symlink(self, link: Path, target: str) -> None:
        self.mkdir(link.parent, 0o755)
        if self.exists(link) and not link.is_symlink():
            raise ManagerError("manager_install_path_unsafe")
        descriptor, temporary_raw = tempfile.mkstemp(prefix=f".{link.name}.", dir=link.parent)
        os.close(descriptor)
        temporary = Path(temporary_raw)
        temporary.unlink(missing_ok=True)
        try:
            temporary.symlink_to(target)
            os.replace(temporary, link)
            self.fsync_directory(link.parent)
        except OSError as exc:
            raise ManagerError("manager_install_link_failed") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def readlink(self, path: Path) -> str:
        try:
            if not path.is_symlink():
                raise OSError
            return os.readlink(path)
        except OSError as exc:
            raise ManagerError("manager_install_identity_invalid") from exc

    def remove(self, path: Path) -> None:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
        except OSError:
            pass

    def chmod(self, path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except OSError as exc:
            raise ManagerError("manager_install_file_failed") from exc

    def chown(self, path: Path, uid: int, gid: int) -> None:
        try:
            os.chown(path, uid, gid, follow_symlinks=False)
        except OSError as exc:
            raise ManagerError("manager_install_permissions_failed") from exc

    def validate_owned_directory(self, path: Path, uid: int, gid: int, mode: int) -> None:
        try:
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != uid or metadata.st_gid != gid
                or stat.S_IMODE(metadata.st_mode) != mode
            ):
                raise ManagerError("manager_install_path_unsafe")
        except ManagerError:
            raise
        except OSError as exc:
            raise ManagerError("manager_install_path_unsafe") from exc

    def validate_persistent_path(
        self,
        path: Path,
        *,
        directory: bool,
        allowed_uids: set[int],
    ) -> None:
        try:
            for candidate in (path, *path.parents):
                if not candidate.exists() and not candidate.is_symlink():
                    continue
                metadata = candidate.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise ManagerError("manager_data_path_invalid")
                if candidate == path:
                    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
                    if not expected or (not directory and metadata.st_nlink != 1):
                        raise ManagerError("manager_data_path_invalid")
                elif not stat.S_ISDIR(metadata.st_mode):
                    raise ManagerError("manager_data_path_invalid")
                if metadata.st_uid not in allowed_uids or metadata.st_mode & 0o002:
                    raise ManagerError("manager_data_path_invalid")
        except ManagerError:
            raise
        except OSError as exc:
            raise ManagerError("manager_data_path_invalid") from exc

    def chown_tree(self, path: Path, uid: int, gid: int) -> None:
        try:
            os.chown(path, uid, gid, follow_symlinks=False)
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.is_symlink():
                        raise ManagerError("manager_install_path_unsafe")
                    os.chown(child, uid, gid, follow_symlinks=False)
        except ManagerError:
            raise
        except OSError as exc:
            raise ManagerError("manager_install_permissions_failed") from exc

    def regular_files(self, root: Path) -> set[str]:
        if not self.is_dir(root):
            raise ManagerError("manager_install_identity_invalid")
        result = set()
        try:
            for path in root.rglob("*"):
                if path.is_symlink() or (not path.is_dir() and not path.is_file()):
                    raise ManagerError("manager_install_identity_invalid")
                if path.is_file():
                    metadata = path.lstat()
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        raise ManagerError("manager_install_identity_invalid")
                    result.add(path.relative_to(root).as_posix())
        except OSError as exc:
            raise ManagerError("manager_install_identity_invalid") from exc
        return result

    def free_bytes(self, path: Path) -> int:
        try:
            probe = path
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            return shutil.disk_usage(probe).free
        except OSError as exc:
            raise ManagerError("manager_install_disk_check_failed") from exc


class CommandAdapter:
    """Fixed command whitelist; dynamic shell input is never accepted."""

    def __init__(self, executor=subprocess.run):
        self._executor = executor

    def available(self, executable: str) -> bool:
        return Path(executable).is_file()

    def account(self, name: str) -> AccountIdentity | None:
        try:
            import grp
            import pwd

            record = pwd.getpwnam(name)
            group = grp.getgrgid(record.pw_gid)
        except (ImportError, KeyError):
            return None
        return AccountIdentity(record.pw_uid, record.pw_gid, record.pw_dir, record.pw_shell, group.gr_name)

    def port_available(self, host: str, port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                listener.bind((host, port))
            return True
        except OSError:
            return False

    def os_release(self) -> tuple[str, str]:
        values = {}
        try:
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value.strip().strip('"')
        except OSError as exc:
            raise ManagerError("manager_install_os_unsupported") from exc
        return values.get("ID", ""), values.get("VERSION_ID", "")

    @staticmethod
    def _allowed(command: tuple[str, ...]) -> bool:
        if command == (SYSTEMD_BIN, "daemon-reload"):
            return True
        if len(command) >= 3 and command[:2] == (SYSTEMD_BIN, "show"):
            return command[-1] in UNITS and all(
                item == "--no-pager" or item.startswith("--property=") for item in command[2:-1]
            )
        if len(command) >= 3 and command[:2] in {
            (SYSTEMD_BIN, "enable"), (SYSTEMD_BIN, "start"),
            (SYSTEMD_BIN, "stop"), (SYSTEMD_BIN, "disable"),
        }:
            return all(item in UNITS for item in command[2:])
        if command == (
            USERADD_BIN, "--system", "--home-dir", "/var/lib/xianyu-saas",
            "--shell", "/usr/sbin/nologin", "--user-group", PRODUCT,
        ):
            return True
        if len(command) >= 3 and command[:2] == (SYSTEMD_ANALYZE_BIN, "verify"):
            return all(Path(item).name in UNITS for item in command[2:])
        return False

    def run(self, command: tuple[str, ...], *, check: bool = True) -> subprocess.CompletedProcess:
        if not self._allowed(command):
            raise ManagerError("manager_command_rejected")
        try:
            result = self._executor(
                list(command), check=False, shell=False, cwd="/",
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ManagerError("manager_command_failed") from exc
        if check and result.returncode != 0:
            raise ManagerError("manager_command_failed")
        return result


class _BoundedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        _validate_network_url(new_url)
        return super().redirect_request(request, fp, code, msg, headers, new_url)


class NetworkAdapter:
    """HTTPS-only GitHub API/CDN and loopback-health transport."""

    def __init__(self, opener=None, health_opener=None, sleeper=time.sleep):
        self._opener = opener or urllib.request.build_opener(_BoundedRedirectHandler())
        self._health_opener = health_opener or urllib.request.urlopen
        self._sleeper = sleeper

    def fetch(self, url: str, maximum: int) -> bytes:
        _validate_network_url(url)
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json" if urllib.parse.urlparse(url).hostname == "api.github.com" else "application/octet-stream",
            "User-Agent": "xianyu-saas-manager/1",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with self._opener.open(request, timeout=60) as response:
                _validate_network_url(response.geturl())
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) > maximum:
                    raise ManagerError("manager_download_too_large")
                chunks, total = [], 0
                while True:
                    chunk = response.read(min(1024 * 1024, maximum + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > maximum:
                        raise ManagerError("manager_download_too_large")
                return b"".join(chunks)
        except ManagerError:
            raise
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise ManagerError("manager_download_failed") from exc

    def _health_once(self, expected_version: str) -> bool:
        expected = (
            (HEALTH_URL, {"ok": True, "service": "xianyu-saas-api"}),
            (READY_URL, {"ok": True, "database": "ready"}),
            (VERSION_URL, {"version": expected_version}),
        )
        try:
            version_payload = None
            for url, required in expected:
                request = urllib.request.Request(url, headers={"User-Agent": "xianyu-saas-manager/1"})
                with self._health_opener(request, timeout=5) as response:
                    if response.geturl() != url or response.status != 200:
                        return False
                    payload = json.loads(response.read(4097))
                if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in required.items()):
                    return False
                if url == VERSION_URL:
                    version_payload = payload
            asset_version = version_payload.get("asset_version") if version_payload else None
            if not isinstance(asset_version, str) or not asset_version.strip():
                return False
            request = urllib.request.Request(PUBLIC_URL, headers={"User-Agent": "xianyu-saas-manager/1"})
            with self._health_opener(request, timeout=5) as response:
                if response.geturl() != PUBLIC_URL or response.status != 200:
                    return False
                index = response.read(1024 * 1024 + 1)
            return len(index) <= 1024 * 1024 and asset_version.encode("utf-8") in index
        except (OSError, ValueError, UnicodeError, urllib.error.URLError):
            return False

    def health(self, expected_version: str) -> bool:
        for attempt in range(HEALTH_ATTEMPTS):
            if self._health_once(expected_version):
                return True
            if attempt + 1 < HEALTH_ATTEMPTS:
                self._sleeper(HEALTH_INTERVAL_SECONDS)
        return False


class Installer:
    def __init__(
        self,
        *,
        filesystem: Filesystem | None = None,
        commands: CommandAdapter | None = None,
        network: NetworkAdapter | None = None,
        paths: InstallPaths | None = None,
        templates_root: Path | None = None,
        executable: Path | None = None,
        public_key: bytes | None = None,
        updater_initializer: Callable[[dict[str, str]], object] | None = None,
        clock=time.time,
    ):
        self.fs = filesystem or Filesystem()
        self.commands = commands or CommandAdapter()
        self.network = network or NetworkAdapter()
        self.paths = paths or InstallPaths()
        self.templates_root = (templates_root or resource_root()).resolve()
        self.executable = manager_file_path(executable)
        self.public_key = public_key if public_key is not None else read_embedded_public_key(source_root=self.templates_root)
        self.updater_initializer = updater_initializer or (
            lambda environment: invoke_updater("initialize", environment_overrides=environment)
        )
        self.clock = clock
        self._managed_data_roots: tuple[Path, ...] = ()
        self._managed_tenants_path = Path("/var/lib/xianyu-saas/tenants")

    def install(self, *, architecture: str, version: str | None = None) -> dict:
        release: VerifiedRelease | None = None
        manager: VerifiedManager | None = None
        try:
            if architecture not in ASSET_ARCHITECTURES:
                raise ManagerError("manager_architecture_unsupported")
            if version is not None and not VERSION_RE.fullmatch(version):
                raise ManagerError("manager_release_version_invalid")
            if self.fs.exists(self.paths.install_journal_file):
                self._preflight(fresh=False)
                manager = self._download_manager(architecture)
                self._recover_interrupted_install(architecture)
            existing = self._existing_installation(architecture)
            if existing is not None:
                if version is not None and existing["version"] != version:
                    raise ManagerError("manager_install_version_conflict")
                self.fs.remove(self.paths.diagnostic_file)
                return {**existing, "ok": True, "status": "already_installed"}

            state = self._classify_state(architecture)
            if state.kind in {"legacy", "signed_unmanaged"} and version not in {None, LEGACY_VERSION}:
                raise ManagerError("manager_install_version_conflict")
            self._preflight(fresh=state.kind == "fresh")

            manager = manager or self._download_manager(architecture)
            metadata = None
            extracted = None
            if state.kind in {"fresh", "legacy"}:
                target_version = LEGACY_VERSION if state.kind == "legacy" else version
                release = self._download_and_verify(architecture, target_version)
                extracted, metadata = self._prepare_release(release)
            elif state.kind == "signed_unmanaged":
                if state.release_root is None:
                    raise ManagerError("manager_signed_adoption_invalid")
                metadata = self._verify_cached_release(
                    state.release_root, LEGACY_VERSION, architecture, "manager_signed_adoption_invalid"
                )
                self._repair_runtime_executables(state.release_root, metadata)
            else:
                raise ManagerError("manager_install_state_unsafe")

            account = self._prepare_account()
            environment_raw, data_environment = self._admit_environment(state, account)
            if state.kind == "legacy":
                if metadata is None or metadata.update_data_version != self._legacy_identity(state):
                    raise ManagerError("manager_legacy_identity_invalid")
            self._admit_destinations(state, manager, release, environment_raw)
            snapshot = self._snapshot_transaction(state)
            return self._commit(
                state=state,
                manager=manager,
                release=release,
                extracted=extracted,
                environment_raw=environment_raw,
                data_environment=data_environment,
                account=account,
                snapshot=snapshot,
            )
        except Exception as exc:
            self._write_diagnostic(exc)
            raise
        finally:
            if release is not None:
                self.fs.remove(release.archive_path.parent)

    def _classify_state(self, architecture: str) -> InstallationState:
        manager_markers = any(self.fs.exists(path) for path in (
            self.paths.manager_current_link, self.paths.launcher_link, self.paths.initialization_file,
        ))
        if manager_markers:
            raise ManagerError("manager_install_incomplete")

        layout = discover_legacy_layout(self.commands)
        if layout.kind == "unsafe":
            raise ManagerError("manager_legacy_layout_unsafe")
        if self.fs.exists(self.paths.current_link):
            try:
                version, release_root = self._current_release_path()
            except ManagerError:
                if layout.kind == "supported" and layout.root == "/opt/xianyu-saas":
                    return InstallationState("legacy", legacy=layout)
                raise ManagerError("manager_install_state_unsafe") from None
            marker_paths = tuple(release_root / name for name in INTERNAL_RELEASE_FILES)
            if any(self.fs.exists(path) for path in marker_paths):
                if version != LEGACY_VERSION:
                    raise ManagerError("manager_signed_adoption_invalid")
                self._verify_cached_release(
                    release_root, version, architecture, "manager_signed_adoption_invalid"
                )
                return InstallationState("signed_unmanaged", version, release_root)
            if layout.kind == "supported" and layout.root == "/opt/xianyu-saas":
                return InstallationState("legacy", legacy=layout)
            raise ManagerError("manager_install_state_unsafe")
        if layout.kind == "supported":
            return InstallationState("legacy", legacy=layout)
        if any(self.fs.exists(path) for path in (self.paths.releases_dir, self.paths.manager_releases_dir)):
            for path in (self.paths.releases_dir, self.paths.manager_releases_dir):
                if self.fs.exists(path) and (not self.fs.is_dir(path) or any(path.iterdir())):
                    raise ManagerError("manager_install_state_unsafe")
        for unit in UNITS:
            if self.fs.exists(self.paths.systemd_dir / unit):
                raise ManagerError("manager_install_incomplete")
        return InstallationState("fresh")

    def _existing_installation(self, architecture: str) -> dict | None:
        marker = self.paths.initialization_file
        managed = any(self.fs.exists(path) for path in (marker, self.paths.manager_current_link))
        if not managed:
            return None
        if not self.fs.is_file(marker):
            raise ManagerError("manager_install_incomplete")
        try:
            payload = json.loads(self.fs.read_bytes(marker, MAX_INDEX_BYTES))
        except (ValueError, UnicodeError):
            raise ManagerError("manager_install_identity_invalid") from None
        required = {
            "schema", "protocol", "public_key_sha256", "manager_version",
            "manager_sha256", "platform", "architecture", "initialized_at",
        }
        stamp = payload.get("initialized_at") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict) or set(payload) != required or payload.get("schema") != 2
            or payload.get("platform") != "linux" or payload.get("architecture") != ASSET_ARCHITECTURES[architecture]
            or payload.get("protocol") != MANAGER_PROTOCOL
            or not VERSION_RE.fullmatch(str(payload.get("manager_version", "")))
            or not SHA256_RE.fullmatch(str(payload.get("manager_sha256", "")))
            or not SHA256_RE.fullmatch(str(payload.get("public_key_sha256", "")))
            or type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp <= 0
        ):
            raise ManagerError("manager_install_identity_invalid")
        version, release_root = self._current_release_path()
        expected_manager = f"releases/{payload['manager_version']}"
        if self.fs.readlink(self.paths.manager_current_link) != expected_manager:
            raise ManagerError("manager_install_identity_invalid")
        if self.fs.readlink(self.paths.launcher_link) != str(self.paths.manager_current_link / "xianyu-saas"):
            raise ManagerError("manager_install_identity_invalid")
        manager = self.paths.manager_releases_dir / payload["manager_version"] / "xianyu-saas"
        if _sha256(self.fs.read_bytes(manager, MAX_FILE_BYTES)) != payload["manager_sha256"]:
            raise ManagerError("manager_install_identity_invalid")
        if _public_key_sha256(self.public_key) != payload["public_key_sha256"]:
            raise ManagerError("manager_install_identity_invalid")
        metadata = self._verify_cached_release(
            release_root, version, architecture, "manager_install_identity_invalid"
        )
        if not self._runtime_executables_ready(release_root, metadata):
            raise ManagerError("manager_install_identity_invalid")
        if not self.fs.is_file(self.paths.public_key_file):
            raise ManagerError("manager_install_identity_invalid")
        if self.fs.read_bytes(self.paths.public_key_file, 4096) != self.public_key:
            raise ManagerError("manager_install_identity_invalid")
        account = self.commands.account(PRODUCT)
        if account is None or account.home != "/var/lib/xianyu-saas" or account.shell != "/usr/sbin/nologin" or account.group != PRODUCT:
            raise ManagerError("manager_install_identity_invalid")
        self._admit_environment(InstallationState("signed_unmanaged", version, release_root), account)
        for unit in UNITS:
            source = self._template(f"deploy/systemd/{unit}")
            destination = self.paths.systemd_dir / unit
            try:
                self.fs.validate_root_file(destination, MAX_MANIFEST_BYTES)
            except ManagerError as exc:
                raise ManagerError("manager_install_identity_invalid") from exc
            if self.fs.read_bytes(destination, MAX_MANIFEST_BYTES) != self._template_payload(source, destination):
                raise ManagerError("manager_install_identity_invalid")
        return {
            "version": version,
            "manager_version": payload["manager_version"],
            "architecture": architecture,
        }

    def _preflight(self, *, fresh: bool) -> None:
        os_id, os_version = self.commands.os_release()
        supported = (os_id == "ubuntu" and os_version in {"22.04", "24.04"}) or (os_id == "debian" and os_version == "12")
        if not supported:
            raise ManagerError("manager_install_os_unsupported")
        for executable, code in (
            (SYSTEMD_BIN, "manager_systemd_unavailable"),
            (SYSTEMD_ANALYZE_BIN, "manager_systemd_unavailable"),
            (USERADD_BIN, "manager_useradd_unavailable"),
        ):
            if not self.commands.available(executable):
                raise ManagerError(code)
        for path in (
            self.paths.install_root.parent,
            self.paths.launcher_link.parent,
            self.paths.env_file.parent,
            self.paths.systemd_dir,
            self.paths.logrotate_dir,
        ):
            self.fs.validate_root_ancestor(path)
        if self.fs.exists(self.paths.install_root):
            if not self.fs.is_dir(self.paths.install_root):
                raise ManagerError("manager_install_path_unsafe")
            self.fs.validate_root_directory(self.paths.install_root)
        if self.fs.free_bytes(self.paths.install_root) < MIN_FREE_BYTES:
            raise ManagerError("manager_install_disk_insufficient")
        if fresh and not self.commands.port_available("0.0.0.0", 8096):
            raise ManagerError("manager_install_port_in_use")

    def _admit_destinations(
        self,
        state: InstallationState,
        manager: VerifiedManager,
        release: VerifiedRelease | None,
        environment_raw: bytes,
    ) -> None:
        privileged_directories = (
            self.paths.install_root.parent,
            self.paths.install_root,
            self.paths.releases_dir,
            self.paths.manager_releases_dir,
            self.paths.manager_current_link.parent,
            self.paths.launcher_link.parent,
            self.paths.env_file.parent,
            self.paths.public_key_file.parent,
            self.paths.systemd_dir,
            self.paths.logrotate_dir,
        )
        for path in privileged_directories:
            if self.fs.exists(path):
                if not self.fs.is_dir(path):
                    raise ManagerError("manager_install_path_unsafe")
                self.fs.validate_root_directory(path)
        self._assert_no_pending_update()
        expected_launcher = str(self.paths.manager_current_link / "xianyu-saas")
        if self.fs.exists(self.paths.launcher_link):
            if self.fs.readlink(self.paths.launcher_link) != expected_launcher:
                raise ManagerError("manager_install_path_occupied")
        manager_destination = self.paths.manager_releases_dir / manager.version
        manager_binary = manager_destination / "xianyu-saas"
        if self.fs.exists(manager_destination):
            if self.fs.regular_files(manager_destination) != {"xianyu-saas"}:
                raise ManagerError("manager_install_path_occupied")
            _verify_record(manager.record, self.fs.read_bytes(manager_binary, MAX_FILE_BYTES), "manager_install_identity_invalid")
        if release is not None:
            destination = self.paths.releases_dir / release.version
            if self.fs.exists(destination):
                _verify_installed_tree(self.fs, destination, release.expected_files, self._release_internal(release))
        if self.fs.exists(self.paths.env_file):
            current = self._read_environment_file(self.paths.env_file)
            if current != environment_raw:
                raise ManagerError("manager_environment_invalid")
        if self.fs.exists(self.paths.public_key_file):
            try:
                self.fs.validate_root_file(self.paths.public_key_file, 4096)
            except ManagerError as exc:
                raise ManagerError("manager_public_key_conflict") from exc
            if self.fs.read_bytes(self.paths.public_key_file, 4096) != self.public_key:
                raise ManagerError("manager_public_key_conflict")
        templates = self._transaction_templates(state)
        if state.kind == "signed_unmanaged":
            templates += tuple(
                (self._template(f"deploy/systemd/{unit}"), self.paths.systemd_dir / unit, 0o644)
                for unit in APPLICATION_UNITS
            )
        for source, target, _mode in templates:
            raw = self._template_payload(source, target)
            if not self.fs.exists(target):
                if state.kind == "signed_unmanaged" and target.name in APPLICATION_UNITS:
                    raise ManagerError("manager_signed_adoption_invalid")
                continue
            try:
                self.fs.validate_root_file(target, MAX_MANIFEST_BYTES)
            except ManagerError as exc:
                raise ManagerError("manager_unit_invalid") from exc
            if state.kind != "legacy" and self.fs.read_bytes(target, MAX_MANIFEST_BYTES) != raw:
                raise ManagerError("manager_unit_invalid")

    def _release_index(
        self, requested_version: str | None
    ) -> tuple[str, dict[str, str], dict[str, AssetRecord]]:
        release_url = (
            f"{GITHUB_API_ROOT}/releases/tags/v{requested_version}"
            if requested_version else f"{GITHUB_API_ROOT}/releases/latest"
        )
        release = _json_object(self.network.fetch(release_url, MAX_INDEX_BYTES), "manager_release_metadata_invalid")
        tag = release.get("tag_name")
        if (
            release.get("draft") is not False or release.get("prerelease") is not False
            or not isinstance(tag, str) or not tag.startswith("v") or not VERSION_RE.fullmatch(tag[1:])
            or (requested_version is not None and tag != f"v{requested_version}")
        ):
            raise ManagerError("manager_release_metadata_invalid")
        version = tag[1:]
        urls = _release_asset_urls(release, tag)
        index_raw = self.network.fetch(urls["artifacts.json"], MAX_INDEX_BYTES)
        index_signature = self.network.fetch(urls["artifacts.json.sig"], MAX_SIGNATURE_BYTES)
        _verify_signature(self.public_key, index_raw, index_signature, "manager_artifacts_signature_invalid")
        return version, urls, _parse_index(index_raw, version)

    def _download_manager(self, architecture: str) -> VerifiedManager:
        version, urls, index = self._release_index(MANAGER_VERSION)
        asset_arch = ASSET_ARCHITECTURES[architecture]
        name = f"xianyu-saas-{version}-linux-{asset_arch}"
        record = _select_record(index, name, "manager", asset_arch)
        if name not in urls:
            raise ManagerError("manager_release_asset_missing")
        payload = self.fs.read_bytes(self.executable, MAX_FILE_BYTES)
        _verify_record(record, payload, "manager_bootstrap_identity_invalid")
        return VerifiedManager(version, asset_arch, record, payload)

    def _download_and_verify(self, architecture: str, requested_version: str | None) -> VerifiedRelease:
        version, urls, index = self._release_index(requested_version)
        asset_arch = ASSET_ARCHITECTURES[architecture]
        base = f"xianyu-saas-{version}-linux-{asset_arch}"
        names = {
            "manager": base,
            "archive": base + ".tar.gz",
            "manifest": base + ".manifest.json",
            "signature": base + ".manifest.sig",
        }
        selected = {kind: _select_record(index, name, kind, asset_arch) for kind, name in names.items()}
        if any(name not in urls for name in names.values()):
            raise ManagerError("manager_release_asset_missing")
        manifest_raw = self.network.fetch(urls[names["manifest"]], selected["manifest"].size)
        manifest_signature = self.network.fetch(urls[names["signature"]], selected["signature"].size)
        _verify_record(selected["manifest"], manifest_raw, "manager_asset_hash_mismatch")
        _verify_record(selected["signature"], manifest_signature, "manager_asset_hash_mismatch")
        _verify_signature(self.public_key, manifest_raw, manifest_signature, "manager_manifest_signature_invalid")
        manifest, expected = _parse_manifest(manifest_raw, version, asset_arch, selected["archive"])
        required = {"backend", "worker", "frontend", "runtime"}
        if not required <= {PurePosixPath(path).parts[0] for path in expected}:
            raise ManagerError("manager_manifest_invalid")
        if "runtime/runtime.json" not in expected or "manager/xianyu-saas" not in expected:
            raise ManagerError("manager_manifest_invalid")
        required_free = selected["archive"].size + sum(item["size"] for item in expected.values()) + MIN_FREE_BYTES
        if self.fs.free_bytes(self.paths.install_root) < required_free:
            raise ManagerError("manager_install_disk_insufficient")
        work = self.fs.make_temporary_directory(self.paths.install_root)
        archive_path = work / names["archive"]
        try:
            archive_raw = self.network.fetch(urls[names["archive"]], selected["archive"].size)
            _verify_record(selected["archive"], archive_raw, "manager_asset_hash_mismatch")
            self.fs.atomic_write(archive_path, archive_raw, 0o600, replace=False)
            return VerifiedRelease(
                version=version, architecture=asset_arch,
                archive=selected["archive"], manifest=selected["manifest"],
                signature=selected["signature"], manager=selected["manager"],
                manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
                manifest_raw=manifest_raw, signature_raw=manifest_signature,
                manifest_payload=manifest, expected_files=expected, archive_path=archive_path,
            )
        except Exception:
            self.fs.remove(work)
            raise

    def _prepare_release(self, release: VerifiedRelease):
        extracted = release.archive_path.parent / "release"
        self.fs.mkdir(extracted, 0o755)
        _extract_verified_archive(self.fs, release.archive_path, extracted, release.expected_files)
        runtime_raw = self.fs.read_bytes(extracted / "runtime/runtime.json", MAX_MANIFEST_BYTES)
        runtime = _json_object(runtime_raw, "manager_runtime_metadata_invalid")
        try:
            metadata = parse_runtime_metadata(
                runtime_raw,
                expected_version=release.version,
                expected_architecture=release.architecture,
            )
        except StandaloneRuntimeError as exc:
            raise ManagerError("manager_runtime_metadata_invalid") from exc
        if (
            runtime != release.manifest_payload.get("runtime")
            or metadata.manager_protocol != MANAGER_PROTOCOL
            or metadata.update_data_version != release.manifest_payload.get("update_data_version")
        ):
            raise ManagerError("manager_runtime_metadata_invalid")
        embedded_manager = self.fs.read_bytes(extracted / "manager/xianyu-saas", MAX_FILE_BYTES)
        _verify_record(release.manager, embedded_manager, "manager_asset_hash_mismatch")
        version, data_version = _application_identity(self.fs, extracted, "manager_runtime_metadata_invalid")
        if version != release.version or data_version != metadata.update_data_version:
            raise ManagerError("manager_runtime_metadata_invalid")
        self._repair_runtime_executables(extracted, metadata)
        return extracted, metadata

    def _runtime_executable_paths(self, root: Path, metadata) -> tuple[Path, ...]:
        match = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\.[0-9]+)?", metadata.python_version)
        if match is None:
            raise ManagerError("manager_runtime_metadata_invalid")
        names = ("python", "python3", f"python{match.group(1)}.{match.group(2)}")
        return tuple(root / "runtime/python/bin" / name for name in names)

    def _repair_runtime_executables(self, root: Path, metadata) -> None:
        paths = self._runtime_executable_paths(root, metadata)
        required = root / "runtime/python/bin/python3"
        for path in paths:
            if not self.fs.exists(path):
                if path == required:
                    raise ManagerError("manager_runtime_metadata_invalid")
                continue
            if not self.fs.is_file(path):
                raise ManagerError("manager_runtime_metadata_invalid")
            self.fs.chmod(path, 0o755)

    def _runtime_executables_ready(self, root: Path, metadata) -> bool:
        required = root / "runtime/python/bin/python3"
        try:
            self._runtime_executable_paths(root, metadata)
            return self.fs.is_secure_executable(required)
        except (OSError, ManagerError):
            return False

    def _current_release_path(self) -> tuple[str, Path]:
        link = self.fs.readlink(self.paths.current_link)
        match = re.fullmatch(r"releases/([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)", link)
        if match is None:
            raise ManagerError("manager_install_identity_invalid")
        return match.group(1), self.paths.releases_dir / match.group(1)

    def _verify_cached_release(self, root: Path, version: str, architecture: str, code: str):
        try:
            self.fs.validate_root_directory(root)
            marker_raw = self.fs.read_bytes(root / MARKER_FILE, MAX_INDEX_BYTES)
            manifest_raw = self.fs.read_bytes(root / CACHED_MANIFEST_FILE, MAX_MANIFEST_BYTES)
            signature_raw = self.fs.read_bytes(root / CACHED_SIGNATURE_FILE, MAX_SIGNATURE_BYTES)
            marker = _json_object(marker_raw, code)
            marker_keys = {
                "schema", "version", "channel", "manifest_sha256", "release_id",
                "artifact", "artifact_size", "kind", "target",
            }
            if (
                set(marker) != marker_keys or marker.get("schema") != 1
                or marker.get("version") != version or marker.get("kind") != "standalone"
                or marker.get("target") != f"linux-{architecture}"
                or marker.get("manifest_sha256") != _sha256(manifest_raw)
            ):
                raise ManagerError(code)
            _verify_signature(self.public_key, manifest_raw, signature_raw, code)
            manifest = _json_object(manifest_raw, code)
            archive = AssetRecord(
                str(marker.get("artifact", "")), marker.get("artifact_size", -1),
                str(manifest.get("artifact_sha256", "")), "standalone-archive", "linux",
                architecture, MANAGER_PROTOCOL,
            )
            _, expected = _parse_manifest(manifest_raw, version, architecture, archive)
            _verify_installed_tree(self.fs, root, expected, {
                MARKER_FILE: marker_raw,
                CACHED_MANIFEST_FILE: manifest_raw,
                CACHED_SIGNATURE_FILE: signature_raw,
            })
            runtime_raw = self.fs.read_bytes(root / "runtime/runtime.json", MAX_MANIFEST_BYTES)
            metadata = parse_runtime_metadata(
                runtime_raw, expected_version=version, expected_architecture=architecture
            )
            app_version, data_version = _application_identity(self.fs, root, code)
            if (
                app_version != version or data_version != metadata.update_data_version
                or metadata.manager_protocol != MANAGER_PROTOCOL
                or manifest.get("runtime") != _json_object(runtime_raw, code)
            ):
                raise ManagerError(code)
            return metadata
        except ManagerError as exc:
            if exc.code == code:
                raise
            raise ManagerError(code) from exc
        except StandaloneRuntimeError as exc:
            raise ManagerError(code) from exc

    def _release_internal(self, release: VerifiedRelease) -> dict[str, bytes]:
        return {
            CACHED_MANIFEST_FILE: release.manifest_raw,
            CACHED_SIGNATURE_FILE: release.signature_raw,
            MARKER_FILE: _json_bytes({
                "schema": 1,
                "version": release.version,
                "channel": "release",
                "manifest_sha256": release.manifest_sha256,
                "release_id": "bootstrap-install",
                "artifact": release.archive.name,
                "artifact_size": release.archive.size,
                "kind": "standalone",
                "target": f"linux-{release.architecture}",
            }),
        }

    def _prepare_account(self) -> AccountIdentity:
        account = self.commands.account(PRODUCT)
        if account is None:
            self.commands.run((
                USERADD_BIN, "--system", "--home-dir", "/var/lib/xianyu-saas",
                "--shell", "/usr/sbin/nologin", "--user-group", PRODUCT,
            ))
            account = self.commands.account(PRODUCT)
        if account is None:
            raise ManagerError("manager_account_creation_failed")
        if (
            account.home != "/var/lib/xianyu-saas"
            or account.shell != "/usr/sbin/nologin"
            or account.group != PRODUCT
        ):
            raise ManagerError("manager_account_conflict")
        return account

    def _admit_environment(
        self, state: InstallationState, account: AccountIdentity
    ) -> tuple[bytes, dict[str, str]]:
        if state.kind == "fresh":
            raw = self._read_environment_file(self.paths.env_file) if self.fs.exists(self.paths.env_file) else self._fresh_environment()
        elif state.kind == "signed_unmanaged":
            if not self.fs.exists(self.paths.env_file):
                raise ManagerError("manager_environment_invalid")
            raw = self._read_environment_file(self.paths.env_file)
        else:
            if state.legacy is None:
                raise ManagerError("manager_legacy_identity_invalid")
            source = self._legacy_environment_path(state.legacy)
            raw = self._read_environment_file(source)
            if self.fs.exists(self.paths.env_file) and self._read_environment_file(self.paths.env_file) != raw:
                raise ManagerError("manager_environment_invalid")
        values = _parse_environment(raw)
        if values.get("SAAS_ENV") != "production" or values.get("SAAS_TESTING") != "0":
            raise ManagerError("manager_environment_invalid")
        if state.kind != "fresh" and {"SAAS_DB", "SAAS_TENANTS_DIR"} - set(values):
            raise ManagerError("manager_data_path_invalid")
        db = self._persistent_path(values.get("SAAS_DB", "/var/lib/xianyu-saas/saas.db"), directory=False)
        tenants = self._persistent_path(
            values.get("SAAS_TENANTS_DIR", "/var/lib/xianyu-saas/tenants"), directory=True
        )
        if db == tenants or db in tenants.parents or tenants in db.parents:
            raise ManagerError("manager_data_path_invalid")
        configured_roots = []
        for value in (db, tenants):
            pure = PurePosixPath(value.as_posix())
            for root in (PurePosixPath("/var/lib/xianyu-saas"), PurePosixPath("/srv/xianyu-saas-data")):
                if _posix_within(pure, root) and Path(root.as_posix()) not in configured_roots:
                    configured_roots.append(Path(root.as_posix()))
        self._managed_data_roots = tuple(
            root for root in configured_roots if root != Path("/var/lib/xianyu-saas")
        )
        self._managed_tenants_path = tenants
        local_db, local_tenants = self._local_path(db), self._local_path(tenants)
        allowed = {0, account.uid}
        self.fs.validate_persistent_path(local_db, directory=False, allowed_uids=allowed)
        self.fs.validate_persistent_path(local_tenants, directory=True, allowed_uids=allowed)
        if state.kind != "fresh":
            if not self.fs.is_file(local_db) or not self.fs.is_dir(local_tenants):
                raise ManagerError("manager_data_path_invalid")
            try:
                database_size = local_db.stat().st_size
            except OSError as exc:
                raise ManagerError("manager_data_path_invalid") from exc
            if not 0 < database_size <= MAX_DATABASE_BACKUP_BYTES:
                raise ManagerError("manager_data_path_invalid")
            if self.fs.free_bytes(self.paths.updater_state_dir) < database_size + MIN_FREE_BYTES:
                raise ManagerError("manager_install_disk_insufficient")
        return raw, {"SAAS_DB": str(local_db), "SAAS_TENANTS_DIR": str(local_tenants)}

    def _read_environment_file(self, path: Path) -> bytes:
        try:
            self.fs.validate_root_file(path, MAX_INDEX_BYTES)
            return self.fs.read_bytes(path, MAX_INDEX_BYTES)
        except ManagerError as exc:
            raise ManagerError("manager_environment_invalid") from exc

    def _fresh_environment(self) -> bytes:
        master_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        return (
            "SAAS_ENV=production\n"
            "SAAS_TESTING=0\n"
            "SAAS_DB=/var/lib/xianyu-saas/saas.db\n"
            "SAAS_TENANTS_DIR=/var/lib/xianyu-saas/tenants\n"
            "SAAS_COOKIE_SECURE=0\n"
            "SAAS_TRUSTED_PROXY_IPS=127.0.0.1,::1\n"
            "SAAS_ALLOW_REGISTRATION=0\n"
            f"SAAS_AI_MASTER_KEY={master_key}\n"
            "SAAS_UPDATE_PUBLIC_KEY_FILE=/etc/xianyu-saas/update-signing.pub\n"
            "SAAS_CURRENT_ROOT=/opt/xianyu-saas/current\n"
            "SAAS_RELEASES_DIR=/opt/xianyu-saas/releases\n"
            "SAAS_UPDATE_STAGING_DIR=/var/lib/xianyu-saas/update-staging\n"
            "SAAS_UPDATE_INTENT_FILE=/var/lib/xianyu-saas-updates/intent.json\n"
            "SAAS_UPDATE_STATUS_DIR=/var/lib/xianyu-saas-updates/status\n"
            "SAAS_UPDATER_STATE_DIR=/var/lib/xianyu-saas-updater\n"
            "SAAS_UPDATE_HEALTH_BASE_URL=http://127.0.0.1:8096/\n"
            "SAAS_UPDATE_PUBLIC_BASE_URL=http://127.0.0.1:8096/xianyu-saas/\n"
        ).encode("utf-8")

    def _persistent_path(self, raw: str, *, directory: bool) -> Path:
        value = str(raw)
        path = PurePosixPath(value)
        if (
            not value or "\x00" in value or "\\" in value or not path.is_absolute()
            or ".." in path.parts or path == PurePosixPath("/")
            or not any(_posix_within(path, root) for root in (
                PurePosixPath("/var/lib/xianyu-saas"),
                PurePosixPath("/srv/xianyu-saas-data"),
            ))
        ):
            raise ManagerError("manager_data_path_invalid")
        if directory and path.name in {"", ".", ".."}:
            raise ManagerError("manager_data_path_invalid")
        return Path(path.as_posix())

    def _local_path(self, path: Path) -> Path:
        mappings = (
            (Path("/opt/xianyu-saas"), self.paths.install_root),
            (Path("/srv/xianyu-saas"), self.paths.legacy_srv_root),
            (Path("/var/lib/xianyu-saas-updates"), self.paths.update_queue_dir),
            (Path("/var/lib/xianyu-saas-updater"), self.paths.updater_state_dir),
            (Path("/var/lib/xianyu-saas"), self.paths.state_dir),
            (Path("/srv/xianyu-saas-data"), self.paths.legacy_state_root),
        )
        for source, destination in mappings:
            try:
                relative = path.relative_to(source)
            except ValueError:
                continue
            return destination / relative
        if self.paths.install_root == Path("/opt/xianyu-saas"):
            return path
        sandbox = self.paths.install_root.parents[1]
        return sandbox.joinpath(*path.parts[1:])

    def _legacy_environment_path(self, layout: LegacyLayout) -> Path:
        if layout.environment_file == "/etc/xianyu-saas.env":
            return self.paths.env_file
        root = self.paths.install_root if layout.root == "/opt/xianyu-saas" else self.paths.legacy_srv_root
        suffix = Path(layout.environment_file).relative_to(Path(layout.root))
        return root / suffix

    def _legacy_root(self, layout: LegacyLayout) -> Path:
        return self.paths.install_root if layout.root == "/opt/xianyu-saas" else self.paths.legacy_srv_root

    def _legacy_release_root(self, layout: LegacyLayout) -> Path:
        root = self._legacy_root(layout)
        self.fs.validate_root_directory(root)
        self.fs.validate_root_directory(root / "releases")
        current = root / "current"
        link = self.fs.readlink(current)
        target = Path(link)
        if target.is_absolute():
            target = self._local_path(target)
        else:
            target = current.parent / target
        target = target.resolve(strict=False)
        releases = (root / "releases").resolve(strict=False)
        if target.parent != releases or not self.fs.is_dir(target):
            raise ManagerError("manager_legacy_identity_invalid")
        self.fs.validate_root_directory(target)
        return target

    def _legacy_identity(self, state: InstallationState) -> int:
        if state.legacy is None:
            raise ManagerError("manager_legacy_identity_invalid")
        root = self._legacy_release_root(state.legacy)
        version, data_version = _application_identity(self.fs, root, "manager_legacy_identity_invalid")
        if version != LEGACY_VERSION:
            raise ManagerError("manager_legacy_version_unsupported")
        build = _json_object(
            self.fs.read_bytes(root / "backend/build-info.json", MAX_INDEX_BYTES),
            "manager_legacy_identity_invalid",
        )
        if (
            set(build) != {"version", "commit", "dirty", "build_time"}
            or build.get("version") != LEGACY_VERSION
            or build.get("commit") != LEGACY_COMMIT
            or build.get("dirty") is not False
            or not isinstance(build.get("build_time"), str)
            or not build["build_time"]
        ):
            raise ManagerError("manager_legacy_identity_invalid")
        return data_version

    def _maintenance_path(self) -> Path:
        return self.paths.update_queue_dir / "status/maintenance.json"

    def _pending_update_paths(self) -> tuple[Path, ...]:
        return (
            self.paths.update_queue_dir / "intent.json",
            self.paths.update_queue_dir / "intent.processing.json",
            self.paths.updater_state_dir / "active.json",
            self.paths.updater_state_dir / "blocked.json",
        )

    def _assert_no_pending_update(self) -> None:
        if any(self.fs.exists(path) for path in self._pending_update_paths()):
            raise ManagerError("manager_update_in_progress")

    def _acquire_update_lock(self) -> int:
        path = self.paths.updater_state_dir / "updater.lock"
        descriptor = -1
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or (os.name == "posix" and (
                    metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600
                ))
            ):
                raise OSError("unsafe updater lock")
            if os.name == "posix":
                import fcntl
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ManagerError("manager_update_in_progress") from exc
            return descriptor
        except ManagerError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise ManagerError("manager_update_in_progress") from exc

    def _write_maintenance(self, operation_id: str, active: bool, phase: str) -> None:
        self.fs.atomic_write(self._maintenance_path(), _json_bytes({
            "schema": 1,
            "operation_id": operation_id,
            "active": active,
            "phase": phase,
            "updated_at": self.clock(),
        }), 0o644)

    def _unit_state(self, unit: str) -> tuple[bool, bool, str]:
        properties = "Id,LoadState,ActiveState,UnitFileState"
        result = self.commands.run(
            (SYSTEMD_BIN, "show", "--no-pager", f"--property={properties}", unit), check=False
        )
        payload = _parse_systemd_show(result.stdout or "")
        load_state = payload.get("LoadState", "")
        if result.returncode != 0 or not load_state:
            raise ManagerError("manager_unit_state_unsafe")
        if load_state == "not-found":
            return False, False, "not-found"
        if load_state != "loaded":
            raise ManagerError("manager_unit_state_unsafe")
        active_value = payload.get("ActiveState", "")
        state_value = payload.get("UnitFileState", "")
        allowed_file_states = {"enabled", "disabled"} if unit in START_UNITS else {"static", "disabled"}
        if active_value not in {"active", "inactive"} or state_value not in allowed_file_states:
            raise ManagerError("manager_unit_state_unsafe")
        return True, active_value == "active", state_value

    def _unit_loaded_for_stop(self, unit: str) -> bool:
        properties = "Id,LoadState"
        result = self.commands.run(
            (SYSTEMD_BIN, "show", "--no-pager", f"--property={properties}", unit), check=False
        )
        payload = _parse_systemd_show(result.stdout or "")
        load_state = payload.get("LoadState", "")
        if result.returncode != 0 or not load_state:
            raise ManagerError("manager_unit_state_unsafe")
        return load_state != "not-found"

    def _snapshot_transaction(self, state: InstallationState) -> UnitSnapshot:
        contents: dict[Path, bytes | None] = {}
        modes: dict[Path, int | None] = {}
        for _source, destination, _mode in self._transaction_templates(state):
            if self.fs.exists(destination):
                try:
                    self.fs.validate_root_file(destination, MAX_MANIFEST_BYTES)
                except ManagerError as exc:
                    raise ManagerError("manager_unit_invalid") from exc
                contents[destination] = self.fs.read_bytes(destination, MAX_MANIFEST_BYTES)
                modes[destination] = stat.S_IMODE(destination.lstat().st_mode)
            else:
                contents[destination] = None
                modes[destination] = None
        loaded, active, unit_file_state = {}, {}, {}
        for unit in UNITS:
            is_loaded, is_active, state_value = self._unit_state(unit)
            if unit == UPDATER_SERVICE and is_active:
                raise ManagerError("manager_update_in_progress")
            loaded[unit] = is_loaded
            active[unit] = is_active
            unit_file_state[unit] = state_value
        maintenance = None
        maintenance_path = self._maintenance_path()
        if self.fs.exists(maintenance_path):
            try:
                self.fs.validate_root_file(maintenance_path, 16 * 1024)
                maintenance = self.fs.read_bytes(maintenance_path, 16 * 1024)
                payload = _json_object(maintenance, "manager_install_state_unsafe")
                if (
                    payload.get("schema") != 1 or not isinstance(payload.get("active"), bool)
                    or payload.get("active") is True
                ):
                    raise ManagerError("manager_update_in_progress")
            except ManagerError:
                raise
            except (OSError, ValueError, TypeError) as exc:
                raise ManagerError("manager_install_state_unsafe") from exc
        return UnitSnapshot(contents, modes, loaded, active, unit_file_state, maintenance)

    def _transaction_templates(self, state: InstallationState) -> tuple[tuple[Path, Path, int], ...]:
        templates = self._templates()
        if state.kind == "signed_unmanaged":
            return tuple(item for item in templates if item[1].name in {UPDATER_SERVICE, UPDATER_PATH_UNIT})
        return templates

    def _backup_database(self, database: Path, version: str) -> DatabaseBackup:
        destination = self.paths.updater_state_dir / "backups" / (
            f"saas-install-{int(self.clock())}-{secrets.token_hex(8)}-before-{version}.db"
        )
        temporary = destination.with_name("." + destination.name + ".partial")
        try:
            metadata = database.lstat()
            if (
                database.is_symlink() or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise OSError("unsafe database")
            temporary.unlink(missing_ok=True)
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise OSError("database integrity check failed")
                with closing(sqlite3.connect(temporary, timeout=30)) as target:
                    source.backup(target)
                    target.commit()
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise OSError("backup integrity check failed")
            temporary.chmod(0o600)
            with temporary.open("r+b") as saved:
                os.fsync(saved.fileno())
            os.replace(temporary, destination)
            self.fs.fsync_directory(destination.parent)
            return DatabaseBackup(
                destination,
                metadata.st_uid,
                metadata.st_gid,
                stat.S_IMODE(metadata.st_mode),
            )
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            self.fs.remove(temporary)
            self.fs.remove(destination)
            raise ManagerError("manager_database_backup_failed") from exc

    def _restore_database(self, database: Path, backup: DatabaseBackup) -> None:
        temporary: Path | None = None
        try:
            self.fs.validate_private_root_file(backup.path, MAX_DATABASE_BACKUP_BYTES)
            descriptor, temporary_raw = tempfile.mkstemp(prefix=f".{database.name}.restore-", dir=database.parent)
            os.close(descriptor)
            temporary = Path(temporary_raw)
            temporary.unlink()
            with closing(sqlite3.connect(backup.path.as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise OSError("backup integrity check failed")
                with closing(sqlite3.connect(temporary, timeout=30)) as target:
                    source.backup(target)
                    target.commit()
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise OSError("restored database integrity check failed")
            temporary.chmod(backup.mode)
            self.fs.chown(temporary, backup.uid, backup.gid)
            with temporary.open("r+b") as restored:
                os.fsync(restored.fileno())
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(database) + suffix)
                if not sidecar.exists() and not sidecar.is_symlink():
                    continue
                sidecar_metadata = sidecar.lstat()
                if (
                    sidecar.is_symlink() or not stat.S_ISREG(sidecar_metadata.st_mode)
                    or sidecar_metadata.st_nlink != 1
                    or sidecar_metadata.st_uid not in {0, backup.uid}
                ):
                    raise OSError("unsafe database sidecar")
                sidecar.unlink()
            os.replace(temporary, database)
            temporary = None
            if os.name == "posix":
                directory = os.open(database.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise ManagerError("manager_database_restore_failed") from exc

    def _snapshot_payload(self, snapshot: UnitSnapshot) -> dict:
        return {
            "contents": {
                str(path): None if raw is None else base64.b64encode(raw).decode("ascii")
                for path, raw in snapshot.contents.items()
            },
            "modes": {str(path): mode for path, mode in snapshot.modes.items()},
            "loaded": snapshot.loaded,
            "active": snapshot.active,
            "unit_file_state": snapshot.unit_file_state,
            "maintenance": (
                None if snapshot.maintenance is None
                else base64.b64encode(snapshot.maintenance).decode("ascii")
            ),
        }

    def _snapshot_from_payload(self, state_kind: str, payload: object) -> UnitSnapshot:
        if not isinstance(payload, dict) or set(payload) != {
            "contents", "modes", "loaded", "active", "unit_file_state", "maintenance",
        }:
            raise ManagerError("manager_install_recovery_failed")
        expected_paths = {
            str(destination)
            for _source, destination, _mode in self._transaction_templates(InstallationState(state_kind))
        }
        raw_contents = payload.get("contents")
        raw_modes = payload.get("modes")
        if (
            not isinstance(raw_contents, dict) or set(raw_contents) != expected_paths
            or not isinstance(raw_modes, dict) or set(raw_modes) != expected_paths
        ):
            raise ManagerError("manager_install_recovery_failed")
        contents: dict[Path, bytes | None] = {}
        modes: dict[Path, int | None] = {}
        for name in expected_paths:
            encoded = raw_contents[name]
            mode = raw_modes[name]
            if encoded is None:
                if mode is not None:
                    raise ManagerError("manager_install_recovery_failed")
                contents[Path(name)] = None
                modes[Path(name)] = None
                continue
            if not isinstance(encoded, str) or not isinstance(mode, int) or isinstance(mode, bool):
                raise ManagerError("manager_install_recovery_failed")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ManagerError("manager_install_recovery_failed") from exc
            if len(raw) > MAX_MANIFEST_BYTES or not 0 <= mode <= 0o7777:
                raise ManagerError("manager_install_recovery_failed")
            contents[Path(name)] = raw
            modes[Path(name)] = mode
        maps = {}
        for key in ("loaded", "active", "unit_file_state"):
            value = payload.get(key)
            if not isinstance(value, dict) or set(value) != set(UNITS):
                raise ManagerError("manager_install_recovery_failed")
            maps[key] = value
        if any(type(value) is not bool for value in maps["loaded"].values()):
            raise ManagerError("manager_install_recovery_failed")
        if any(type(value) is not bool for value in maps["active"].values()):
            raise ManagerError("manager_install_recovery_failed")
        for unit, value in maps["unit_file_state"].items():
            allowed = {"enabled", "disabled", "not-found"} if unit in START_UNITS else {"static", "disabled", "not-found"}
            if value not in allowed:
                raise ManagerError("manager_install_recovery_failed")
        maintenance_encoded = payload.get("maintenance")
        maintenance = None
        if maintenance_encoded is not None:
            if not isinstance(maintenance_encoded, str):
                raise ManagerError("manager_install_recovery_failed")
            try:
                maintenance = base64.b64decode(maintenance_encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ManagerError("manager_install_recovery_failed") from exc
            if len(maintenance) > 16 * 1024:
                raise ManagerError("manager_install_recovery_failed")
        return UnitSnapshot(
            contents,
            modes,
            dict(maps["loaded"]),
            dict(maps["active"]),
            dict(maps["unit_file_state"]),
            maintenance,
        )

    def _save_install_journal(self, journal: dict) -> None:
        self.fs.atomic_write(self.paths.install_journal_file, _json_bytes(journal), 0o600)

    def _new_install_journal(
        self,
        *,
        operation_id: str,
        state: InstallationState,
        target_version: str,
        manager_version: str,
        old_links: dict[Path, str | None],
        snapshot: UnitSnapshot,
        database_path: Path,
    ) -> dict:
        return {
            "schema": 1,
            "operation_id": operation_id,
            "phase": "prepared",
            "state_kind": state.kind,
            "target_version": target_version,
            "manager_version": manager_version,
            "old_links": {str(path): target for path, target in old_links.items()},
            "snapshot": self._snapshot_payload(snapshot),
            "created_release": "",
            "created_manager": "",
            "created_environment": False,
            "created_key": False,
            "database_path": str(database_path),
            "database_backup": None,
            "stopped": False,
            "changed_units": False,
        }

    def _update_install_journal(self, journal: dict, phase: str, **changes) -> None:
        journal.update(changes)
        journal["phase"] = phase
        self._save_install_journal(journal)

    def _read_install_journal(self) -> dict | None:
        path = self.paths.install_journal_file
        if not self.fs.exists(path):
            return None
        try:
            self.fs.validate_private_root_file(path, MAX_INDEX_BYTES)
            payload = _json_object(self.fs.read_bytes(path, MAX_INDEX_BYTES), "manager_install_recovery_failed")
        except ManagerError as exc:
            raise ManagerError("manager_install_recovery_failed") from exc
        required = {
            "schema", "operation_id", "phase", "state_kind", "target_version", "manager_version",
            "old_links", "snapshot", "created_release", "created_manager", "created_environment",
            "created_key", "database_path", "database_backup", "stopped", "changed_units",
        }
        if set(payload) != required or payload.get("schema") != 1:
            raise ManagerError("manager_install_recovery_failed")
        operation_id = payload.get("operation_id")
        state_kind = payload.get("state_kind")
        if (
            not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id)
            or state_kind not in {"fresh", "legacy", "signed_unmanaged"}
            or payload.get("phase") not in {
                "prepared", "maintenance", "stopped", "backed_up", "published", "switched",
                "units", "initialized", "api_started", "healthy", "services_started", "completed",
            }
            or not VERSION_RE.fullmatch(str(payload.get("target_version", "")))
            or not VERSION_RE.fullmatch(str(payload.get("manager_version", "")))
            or type(payload.get("created_environment")) is not bool
            or type(payload.get("created_key")) is not bool
            or type(payload.get("stopped")) is not bool
            or type(payload.get("changed_units")) is not bool
        ):
            raise ManagerError("manager_install_recovery_failed")
        allowed_links = {
            str(self.paths.current_link), str(self.paths.manager_current_link),
            str(self.paths.launcher_link), str(self.paths.legacy_srv_root / "current"),
        }
        old_links = payload.get("old_links")
        if not isinstance(old_links, dict) or not set(old_links) <= allowed_links:
            raise ManagerError("manager_install_recovery_failed")
        for target in old_links.values():
            if target is not None and (
                not isinstance(target, str) or not target or len(target) > 4096 or "\x00" in target
            ):
                raise ManagerError("manager_install_recovery_failed")
        self._snapshot_from_payload(state_kind, payload.get("snapshot"))
        database_raw = payload.get("database_path")
        created_release = payload.get("created_release")
        created_manager = payload.get("created_manager")
        if not all(isinstance(value, str) for value in (database_raw, created_release, created_manager)):
            raise ManagerError("manager_install_recovery_failed")
        database = Path(database_raw)
        if not database.is_absolute() or ".." in database.parts:
            raise ManagerError("manager_install_recovery_failed")
        expected_release = self.paths.releases_dir / payload["target_version"]
        expected_manager = self.paths.manager_releases_dir / payload["manager_version"]
        if created_release not in {"", str(expected_release)}:
            raise ManagerError("manager_install_recovery_failed")
        if created_manager not in {"", str(expected_manager)}:
            raise ManagerError("manager_install_recovery_failed")
        backup = payload.get("database_backup")
        if backup is not None:
            if not isinstance(backup, dict) or set(backup) != {"path", "uid", "gid", "mode"}:
                raise ManagerError("manager_install_recovery_failed")
            backup_path = Path(str(backup.get("path", "")))
            try:
                backup_path.relative_to(self.paths.updater_state_dir / "backups")
            except ValueError as exc:
                raise ManagerError("manager_install_recovery_failed") from exc
            if (
                not backup_path.is_absolute() or ".." in backup_path.parts
                or type(backup.get("uid")) is not int or backup["uid"] < 0
                or type(backup.get("gid")) is not int or backup["gid"] < 0
                or type(backup.get("mode")) is not int or not 0 <= backup["mode"] <= 0o7777
            ):
                raise ManagerError("manager_install_recovery_failed")
        return payload

    def _remove_install_journal(self) -> None:
        self.fs.remove(self.paths.install_journal_file)
        if self.fs.exists(self.paths.install_journal_file):
            raise ManagerError("manager_install_recovery_failed")
        self.fs.fsync_directory(self.paths.install_journal_file.parent)

    def _recover_interrupted_install(self, architecture: str) -> bool:
        journal = self._read_install_journal()
        if journal is None:
            return False
        if journal["phase"] in {"services_started", "completed"}:
            maintenance_path = self._maintenance_path()
            if self.fs.exists(maintenance_path):
                try:
                    self.fs.validate_root_file(maintenance_path, 16 * 1024)
                    maintenance = _json_object(
                        self.fs.read_bytes(maintenance_path, 16 * 1024),
                        "manager_install_recovery_failed",
                    )
                except ManagerError as exc:
                    raise ManagerError("manager_install_recovery_failed") from exc
                if maintenance.get("schema") == 1 and maintenance.get("active") is False:
                    if self._existing_installation(architecture) is None:
                        raise ManagerError("manager_install_recovery_failed")
                    self._remove_install_journal()
                    return True
        snapshot = self._snapshot_from_payload(journal["state_kind"], journal["snapshot"])
        backup_payload = journal.get("database_backup")
        backup = None if backup_payload is None else DatabaseBackup(
            Path(backup_payload["path"]), backup_payload["uid"], backup_payload["gid"], backup_payload["mode"]
        )
        lock_descriptor = self._acquire_update_lock()
        try:
            self._assert_no_pending_update()
            self._rollback(
                snapshot=snapshot,
                old_links={Path(path): target for path, target in journal["old_links"].items()},
                created_release=Path(journal["created_release"]) if journal["created_release"] else None,
                created_manager=Path(journal["created_manager"]) if journal["created_manager"] else None,
                created_environment=journal["created_environment"],
                created_key=journal["created_key"],
                database_path=Path(journal["database_path"]),
                database_backup=backup,
                stopped=journal["stopped"],
                changed_units=journal["changed_units"],
            )
            self._remove_install_journal()
            return True
        finally:
            os.close(lock_descriptor)

    def _commit(
        self,
        *,
        state: InstallationState,
        manager: VerifiedManager,
        release: VerifiedRelease | None,
        extracted: Path | None,
        environment_raw: bytes,
        data_environment: dict[str, str],
        account: AccountIdentity,
        snapshot: UnitSnapshot,
    ) -> dict:
        target_version = release.version if release is not None else state.version
        link_paths = [self.paths.current_link, self.paths.manager_current_link, self.paths.launcher_link]
        compatibility = None
        if state.kind == "legacy" and state.legacy is not None and state.legacy.root == "/srv/xianyu-saas":
            compatibility = self.paths.legacy_srv_root / "current"
            link_paths.append(compatibility)
        old_links = {path: self._link_snapshot(path) for path in link_paths}
        created_release = created_manager = created_environment = created_key = False
        stopped = changed_units = False
        database_path = Path(data_environment["SAAS_DB"])
        database_backup: DatabaseBackup | None = None
        operation_id = secrets.token_hex(16)
        lock_descriptor = -1
        journal: dict | None = None
        traffic_open = False
        try:
            self._create_directories(account.uid, account.gid)
            lock_descriptor = self._acquire_update_lock()
            self._assert_no_pending_update()
            journal = self._new_install_journal(
                operation_id=operation_id,
                state=state,
                target_version=target_version,
                manager_version=manager.version,
                old_links=old_links,
                snapshot=snapshot,
                database_path=database_path,
            )
            self._save_install_journal(journal)
            self._update_install_journal(journal, "maintenance")
            self._write_maintenance(operation_id, True, "installing")
            if state.kind != "fresh":
                self._update_install_journal(journal, "stopped", stopped=True)
                updater_units = tuple(
                    unit for unit in (UPDATER_PATH_UNIT, UPDATER_SERVICE) if snapshot.loaded.get(unit)
                )
                if updater_units:
                    self.commands.run((SYSTEMD_BIN, "stop", *updater_units))
                    stopped = True
                if (
                    snapshot.loaded.get(UPDATER_PATH_UNIT)
                    and snapshot.unit_file_state.get(UPDATER_PATH_UNIT) == "enabled"
                ):
                    self.commands.run((SYSTEMD_BIN, "disable", UPDATER_PATH_UNIT))
                    stopped = True
                application_units = tuple(
                    unit for unit in reversed(APPLICATION_UNITS) if snapshot.loaded.get(unit)
                )
                if application_units:
                    self.commands.run((SYSTEMD_BIN, "stop", *application_units))
                    stopped = True
                self._assert_no_pending_update()
                database_backup = self._backup_database(database_path, target_version)
                self._update_install_journal(journal, "backed_up", database_backup={
                    "path": str(database_backup.path),
                    "uid": database_backup.uid,
                    "gid": database_backup.gid,
                    "mode": database_backup.mode,
                })
            planned_environment = not self.fs.exists(self.paths.env_file)
            planned_key = not self.fs.exists(self.paths.public_key_file)
            planned_manager = not self.fs.exists(self.paths.manager_releases_dir / manager.version)
            planned_release = release is not None and not self.fs.exists(self.paths.releases_dir / target_version)
            self._update_install_journal(
                journal,
                "published",
                created_environment=planned_environment,
                created_key=planned_key,
                created_manager=str(self.paths.manager_releases_dir / manager.version) if planned_manager else "",
                created_release=str(self.paths.releases_dir / target_version) if planned_release else "",
            )
            if not self.fs.exists(self.paths.env_file):
                self.fs.atomic_write(self.paths.env_file, environment_raw, 0o600, replace=False)
                created_environment = True
            if not self.fs.exists(self.paths.public_key_file):
                self.fs.atomic_write(self.paths.public_key_file, self.public_key, 0o644, replace=False)
                created_key = True
            created_manager = self._publish_manager(manager)
            if release is not None:
                if extracted is None:
                    raise ManagerError("manager_install_failed")
                created_release = self._publish_release(release, extracted)
            changed_units = True
            self._update_install_journal(journal, "switched", changed_units=True)
            if release is not None:
                self.fs.replace_symlink(self.paths.current_link, f"releases/{release.version}")
            self.fs.replace_symlink(self.paths.manager_current_link, f"releases/{manager.version}")
            self.fs.replace_symlink(
                self.paths.launcher_link, str(self.paths.manager_current_link / "xianyu-saas")
            )
            if compatibility is not None:
                self.fs.replace_symlink(compatibility, str(self.paths.current_link))
            self._verify_templates()
            self._install_templates(state)
            self.commands.run((SYSTEMD_BIN, "daemon-reload"))
            self._update_install_journal(journal, "units")
            os.close(lock_descriptor)
            lock_descriptor = -1
            self.updater_initializer(data_environment)
            lock_descriptor = self._acquire_update_lock()
            self._assert_no_pending_update()
            self._write_initialization(manager)
            self._update_install_journal(journal, "initialized")
            self.commands.run((SYSTEMD_BIN, "enable", *START_UNITS))
            self.commands.run((SYSTEMD_BIN, "start", API_SERVICE))
            self._update_install_journal(journal, "api_started")
            if not self.network.health(target_version):
                raise ManagerError("manager_install_health_failed")
            self._update_install_journal(journal, "healthy")
            self.commands.run((SYSTEMD_BIN, "start", CONSUMER_SERVICE, UPDATER_PATH_UNIT))
            self._update_install_journal(journal, "services_started")
            traffic_open = True
            self._write_maintenance(operation_id, False, "succeeded")
            self._update_install_journal(journal, "completed")
            self._remove_install_journal()
            self.fs.remove(self.paths.diagnostic_file)
            status = {
                "fresh": "installed", "legacy": "migrated", "signed_unmanaged": "adopted",
            }[state.kind]
            return {
                "ok": True, "status": status, "version": target_version,
                "manager_version": manager.version, "architecture": manager.architecture,
            }
        except Exception as exc:
            if traffic_open:
                raise ManagerError("manager_install_recovery_failed") from exc
            try:
                self._rollback(
                    snapshot=snapshot,
                    old_links=old_links,
                    created_release=(self.paths.releases_dir / target_version) if created_release else None,
                    created_manager=(self.paths.manager_releases_dir / manager.version) if created_manager else None,
                    created_environment=created_environment,
                    created_key=created_key,
                    database_path=database_path,
                    database_backup=database_backup,
                    stopped=stopped,
                    changed_units=changed_units,
                )
                if journal is not None:
                    self._remove_install_journal()
            except Exception as recovery:
                raise ManagerError("manager_install_recovery_failed") from recovery
            raise exc
        finally:
            if lock_descriptor >= 0:
                os.close(lock_descriptor)

    def _create_directories(self, uid: int, gid: int) -> None:
        entries = (
            (self.paths.install_root, 0o755, None),
            (self.paths.releases_dir, 0o755, None),
            (self.paths.manager_releases_dir, 0o755, None),
            (self.paths.state_dir, 0o700, (uid, gid)),
            (self.paths.update_queue_dir, 0o1770, (0, gid)),
            (self.paths.update_queue_dir / "status", 0o755, (0, gid)),
            (self.paths.update_queue_dir / "status/operations", 0o755, (0, gid)),
            (self.paths.updater_state_dir, 0o700, (0, 0)),
            (self.paths.updater_state_dir / "backups", 0o700, (0, 0)),
            (self.paths.public_key_file.parent, 0o755, None),
        )
        for path, mode, owner in entries:
            if self.fs.exists(path):
                if not self.fs.is_dir(path):
                    raise ManagerError("manager_install_path_unsafe")
                if owner is not None:
                    self.fs.validate_owned_directory(path, owner[0], owner[1], mode)
                continue
            self.fs.mkdir(path, mode)
            if owner is not None:
                self.fs.chown(path, *owner)

    def _publish_manager(self, manager: VerifiedManager) -> bool:
        destination = self.paths.manager_releases_dir / manager.version
        if self.fs.exists(destination):
            return False
        self.fs.mkdir(destination, 0o755)
        try:
            self.fs.atomic_write(destination / "xianyu-saas", manager.payload, 0o755, replace=False)
        except Exception:
            self.fs.remove(destination)
            raise
        return True

    def _publish_release(self, release: VerifiedRelease, extracted: Path) -> bool:
        destination = self.paths.releases_dir / release.version
        if self.fs.exists(destination):
            self.fs.remove(extracted)
            return False
        for name, payload in self._release_internal(release).items():
            self.fs.atomic_write(extracted / name, payload, 0o644, replace=False)
        self.fs.publish_directory(extracted, destination)
        return True

    def _template_payload(self, source: Path, destination: Path) -> bytes:
        raw = self.fs.read_bytes(source, MAX_MANIFEST_BYTES)
        if self._managed_data_roots and destination.name in {
            API_SERVICE, CONSUMER_SERVICE, UPDATER_SERVICE,
        }:
            try:
                text = raw.decode("utf-8")
            except UnicodeError as exc:
                raise ManagerError("manager_template_missing") from exc
            extra = "".join(f"ReadWritePaths={root.as_posix()}\n" for root in self._managed_data_roots)
            marker = "\n[Install]\n"
            if marker in text:
                text = text.replace(marker, "\n" + extra + "[Install]\n", 1)
            else:
                text = text.rstrip("\n") + "\n" + extra
            raw = text.encode("utf-8")
        if destination == self.paths.logrotate_dir / "xianyu-saas" and self._managed_tenants_path != Path(
            "/var/lib/xianyu-saas/tenants"
        ):
            raw = raw.replace(
                b"/var/lib/xianyu-saas/tenants",
                self._managed_tenants_path.as_posix().encode("utf-8"),
            )
        return raw

    def _verify_templates(self) -> None:
        work = self.fs.make_temporary_directory(self.paths.updater_state_dir)
        try:
            units = []
            for unit in UNITS:
                source = self._template(f"deploy/systemd/{unit}")
                destination = work / unit
                self.fs.atomic_write(
                    destination,
                    self._template_payload(source, self.paths.systemd_dir / unit),
                    0o644,
                    replace=False,
                )
                units.append(str(destination))
            self.commands.run((SYSTEMD_ANALYZE_BIN, "verify", *units))
        finally:
            self.fs.remove(work)

    def _install_templates(self, state: InstallationState) -> None:
        for source, destination, mode in self._transaction_templates(state):
            self.fs.atomic_write(destination, self._template_payload(source, destination), mode)

    def _write_initialization(self, manager: VerifiedManager) -> None:
        self.fs.atomic_write(self.paths.initialization_file, _json_bytes({
            "schema": 2,
            "protocol": MANAGER_PROTOCOL,
            "public_key_sha256": _public_key_sha256(self.public_key),
            "manager_version": manager.version,
            "manager_sha256": manager.record.sha256,
            "platform": "linux",
            "architecture": manager.architecture,
            "initialized_at": self.clock(),
        }), 0o644)

    def _link_snapshot(self, path: Path) -> str | None:
        if not self.fs.exists(path):
            return None
        return self.fs.readlink(path)

    def _restore_link(self, path: Path, target: str | None) -> None:
        if target is None:
            self.fs.remove(path)
        else:
            self.fs.replace_symlink(path, target)

    def _rollback(
        self,
        *,
        snapshot: UnitSnapshot,
        old_links: dict[Path, str | None],
        created_release: Path | None,
        created_manager: Path | None,
        created_environment: bool,
        created_key: bool,
        database_path: Path,
        database_backup: DatabaseBackup | None,
        stopped: bool,
        changed_units: bool,
    ) -> None:
        if stopped or changed_units:
            current_units = []
            for unit in (UPDATER_PATH_UNIT, UPDATER_SERVICE, *reversed(APPLICATION_UNITS)):
                if self._unit_loaded_for_stop(unit):
                    current_units.append(unit)
            if current_units:
                self.commands.run((SYSTEMD_BIN, "stop", *current_units))
        if database_backup is not None:
            self._restore_database(database_path, database_backup)
        maintenance_path = self._maintenance_path()
        if snapshot.maintenance is None:
            self.fs.remove(maintenance_path)
        else:
            self.fs.atomic_write(maintenance_path, snapshot.maintenance, 0o644)
        self.fs.remove(self.paths.initialization_file)
        for path, target in old_links.items():
            self._restore_link(path, target)
        if changed_units:
            for path, raw in snapshot.contents.items():
                if raw is None:
                    self.fs.remove(path)
                else:
                    mode = snapshot.modes.get(path)
                    if mode is None:
                        raise ManagerError("manager_install_recovery_failed")
                    self.fs.atomic_write(path, raw, mode)
            self.commands.run((SYSTEMD_BIN, "daemon-reload"))
        if stopped or changed_units:
            for unit in START_UNITS:
                if not snapshot.loaded.get(unit):
                    continue
                state = snapshot.unit_file_state.get(unit)
                if state == "enabled":
                    self.commands.run((SYSTEMD_BIN, "enable", unit))
                elif state == "disabled":
                    self.commands.run((SYSTEMD_BIN, "disable", unit))
                else:
                    raise ManagerError("manager_unit_state_unsafe")
            inactive = tuple(
                unit for unit in (UPDATER_PATH_UNIT, UPDATER_SERVICE, *reversed(APPLICATION_UNITS))
                if snapshot.loaded.get(unit) and not snapshot.active.get(unit)
            )
            if inactive:
                self.commands.run((SYSTEMD_BIN, "stop", *inactive))
            active = tuple(unit for unit in UNITS if snapshot.loaded.get(unit) and snapshot.active.get(unit))
            if active:
                self.commands.run((SYSTEMD_BIN, "start", *active))
            for unit in UNITS:
                observed = self._unit_state(unit)
                expected = (
                    snapshot.loaded.get(unit, False),
                    snapshot.active.get(unit, False),
                    snapshot.unit_file_state.get(unit, "not-found"),
                )
                if observed != expected:
                    raise ManagerError("manager_install_recovery_failed")
        if created_release is not None:
            self.fs.remove(created_release)
        if created_manager is not None:
            self.fs.remove(created_manager)
        if created_environment:
            self.fs.remove(self.paths.env_file)
        if created_key:
            self.fs.remove(self.paths.public_key_file)

    def _write_diagnostic(self, exc: Exception) -> None:
        code = exc.code if isinstance(exc, ManagerError) else "manager_install_failed"
        if code == "manager_update_in_progress":
            return
        try:
            self.fs.mkdir(self.paths.updater_state_dir, 0o700)
            self.fs.atomic_write(self.paths.diagnostic_file, _json_bytes({
                "schema": 1, "status": "failed", "error_code": code, "updated_at": self.clock(),
            }), 0o600)
        except Exception:
            pass

    def _template(self, relative: str) -> Path:
        path = self.templates_root.joinpath(*PurePosixPath(relative).parts)
        try:
            path.resolve().relative_to(self.templates_root)
        except ValueError:
            raise ManagerError("manager_template_missing") from None
        if not self.fs.is_file(path):
            raise ManagerError("manager_template_missing")
        return path

    def _templates(self) -> tuple[tuple[Path, Path, int], ...]:
        return (
            *((self._template(f"deploy/systemd/{unit}"), self.paths.systemd_dir / unit, 0o644) for unit in UNITS),
            (self._template("deploy/xianyu-saas-bot-logrotate.conf"), self.paths.logrotate_dir / "xianyu-saas", 0o644),
        )


def _posix_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    try:
        path.relative_to(root)
        return path != root
    except ValueError:
        return False


def _parse_environment(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ManagerError("manager_environment_invalid") from exc
    if "\x00" in text:
        raise ManagerError("manager_environment_invalid")
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^(SAAS_DB|SAAS_TENANTS_DIR|SAAS_ENV|SAAS_TESTING)=", stripped)
        if match is None:
            continue
        try:
            fields = shlex.split(stripped, comments=True, posix=True)
        except ValueError as exc:
            raise ManagerError("manager_environment_invalid") from exc
        if len(fields) != 1 or "=" not in fields[0]:
            raise ManagerError("manager_environment_invalid")
        name, value = fields[0].split("=", 1)
        if name != match.group(1) or name in result or not value:
            raise ManagerError("manager_environment_invalid")
        result[name] = value
    return result


def _application_identity(fs: Filesystem, root: Path, code: str) -> tuple[str, int]:
    try:
        package = _json_object(fs.read_bytes(root / "package.json", MAX_INDEX_BYTES), code)
        package_version = package.get("version")
        if not isinstance(package_version, str) or not VERSION_RE.fullmatch(package_version):
            raise ManagerError(code)
        source = fs.read_bytes(root / "backend/version.py", MAX_INDEX_BYTES).decode("utf-8")
        tree = ast.parse(source, filename="backend/version.py", mode="exec")
        values: dict[str, object] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            name = node.targets[0].id
            if name not in {"VERSION", "UPDATE_DATA_VERSION"}:
                continue
            if name in values or not isinstance(node.value, ast.Constant):
                raise ManagerError(code)
            values[name] = node.value.value
        backend_version = values.get("VERSION")
        data_version = values.get("UPDATE_DATA_VERSION")
        if (
            not isinstance(backend_version, str) or backend_version != package_version
            or type(data_version) is not int or not 1 <= data_version <= 2**31 - 1
        ):
            raise ManagerError(code)
        return package_version, data_version
    except ManagerError:
        raise
    except (SyntaxError, UnicodeError, ValueError, TypeError) as exc:
        raise ManagerError(code) from exc


def _validate_network_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS
        or parsed.username is not None or parsed.password is not None or parsed.port not in {None, 443}
        or parsed.fragment or "\\" in parsed.path
    ):
        raise ManagerError("manager_download_url_rejected")
    if parsed.hostname == "api.github.com" and not parsed.path.startswith(f"/repos/{GITHUB_REPOSITORY}/"):
        raise ManagerError("manager_download_url_rejected")
    if parsed.hostname == "github.com" and not parsed.path.startswith(f"/{GITHUB_REPOSITORY}/releases/download/"):
        raise ManagerError("manager_download_url_rejected")


def _json_object(raw: bytes, code: str) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ManagerError(code) from None
    if not isinstance(value, dict):
        raise ManagerError(code)
    return value


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _public_key(raw: bytes) -> Ed25519PublicKey:
    try:
        value = raw.strip()
        if value.startswith(b"-----BEGIN PUBLIC KEY-----"):
            key = serialization.load_pem_public_key(value)
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError
            return key
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) != 32:
            raise ValueError
        return Ed25519PublicKey.from_public_bytes(decoded)
    except (ValueError, TypeError, binascii.Error, UnsupportedAlgorithm) as exc:
        raise ManagerError("manager_public_key_invalid") from exc


def _public_key_sha256(raw: bytes) -> str:
    key = _public_key(raw)
    encoded = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(encoded).hexdigest()


def _verify_signature(public_key: bytes, payload: bytes, encoded: bytes, code: str) -> None:
    try:
        signature = base64.b64decode(encoded.strip(), validate=True)
        if len(signature) != 64 or base64.b64encode(signature) != encoded.strip():
            raise ValueError
        _public_key(public_key).verify(signature, payload)
    except (ValueError, TypeError, binascii.Error, InvalidSignature) as exc:
        raise ManagerError(code) from exc


def _release_asset_urls(release: dict, tag: str) -> dict[str, str]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ManagerError("manager_release_metadata_invalid")
    result = {}
    prefix = f"https://github.com/{GITHUB_REPOSITORY}/releases/download/{tag}/"
    for asset in assets:
        if not isinstance(asset, dict):
            raise ManagerError("manager_release_metadata_invalid")
        name, url = asset.get("name"), asset.get("browser_download_url")
        if (
            not isinstance(name, str) or Path(name).name != name or name in result
            or not isinstance(url, str) or url != prefix + urllib.parse.quote(name)
        ):
            raise ManagerError("manager_release_metadata_invalid")
        _validate_network_url(url)
        result[name] = url
    for required in ("artifacts.json", "artifacts.json.sig"):
        if required not in result:
            raise ManagerError("manager_release_asset_missing")
    return result


def _parse_index(raw: bytes, version: str) -> dict[str, AssetRecord]:
    payload = _json_object(raw, "manager_artifacts_invalid")
    if payload.get("schema") != 2 or payload.get("version") != version or not isinstance(payload.get("files"), list):
        raise ManagerError("manager_artifacts_invalid")
    records = {}
    record_keys = {"name", "size", "sha256", "kind", "target", "manager_protocol"}
    for item in payload["files"]:
        if not isinstance(item, dict) or set(item) != record_keys:
            raise ManagerError("manager_artifacts_invalid")
        name, size, digest = item.get("name"), item.get("size"), item.get("sha256")
        kind, target, protocol = item.get("kind"), item.get("target"), item.get("manager_protocol")
        match = re.fullmatch(r"linux-(x86_64|aarch64)", target) if isinstance(target, str) else None
        generic_target = isinstance(target, str) and target in {"source", "docker", "all"}
        platform_name = "linux" if match else "any" if generic_target else ""
        architecture = match.group(1) if match else "any" if generic_target else ""
        if (
            not isinstance(name, str) or Path(name).name != name or name in records
            or not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ARCHIVE_BYTES
            or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
            or not isinstance(kind, str) or not kind
            or not platform_name or not architecture
            or not isinstance(protocol, int) or isinstance(protocol, bool) or protocol <= 0
        ):
            raise ManagerError("manager_artifacts_invalid")
        records[name] = AssetRecord(name, size, digest, kind, platform_name, architecture, protocol)
    return records


def _select_record(index: dict[str, AssetRecord], name: str, kind: str, architecture: str) -> AssetRecord:
    try:
        record = index[name]
    except KeyError as exc:
        raise ManagerError("manager_release_asset_missing") from exc
    accepted_kinds = {
        "manager": {"manager", "bootstrap-manager"},
        "archive": {"standalone", "standalone-archive"},
        "manifest": {"standalone-manifest", "manifest"},
        "signature": {"standalone-signature", "signature"},
    }
    if (
        record.kind not in accepted_kinds[kind] or record.platform != "linux"
        or record.architecture != architecture or record.manager_protocol != MANAGER_PROTOCOL
    ):
        raise ManagerError("manager_release_asset_mismatch")
    maximum = MAX_ARCHIVE_BYTES if kind == "archive" else MAX_MANIFEST_BYTES if kind == "manifest" else MAX_FILE_BYTES if kind == "manager" else MAX_SIGNATURE_BYTES
    if record.size > maximum:
        raise ManagerError("manager_download_too_large")
    return record


def _verify_record(record: AssetRecord, raw: bytes, code: str) -> None:
    if len(raw) != record.size or _sha256(raw) != record.sha256:
        raise ManagerError(code)


def _canonical_path(raw: str) -> str:
    if not raw or raw.startswith("/") or "\\" in raw or "\x00" in raw or len(raw) > 1000:
        raise ManagerError("manager_archive_path_invalid")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} or len(part) > 240 for part in path.parts):
        raise ManagerError("manager_archive_path_invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ManagerError("manager_archive_path_invalid")
    return path.as_posix()


def _parse_manifest(raw: bytes, version: str, architecture: str, archive: AssetRecord) -> tuple[dict, dict[str, dict]]:
    payload = _json_object(raw, "manager_manifest_invalid")
    try:
        validate_standalone_manifest(
            payload,
            expected_version=version,
            expected_target=f"linux-{architecture}",
            expected_artifact=archive.name,
        )
    except StandaloneRuntimeError as exc:
        raise ManagerError("manager_manifest_invalid") from exc
    if (
        payload.get("schema") != 2 or payload.get("kind") != "standalone"
        or payload.get("version") != version or payload.get("platform") != "linux"
        or payload.get("architecture") != architecture or payload.get("manager_protocol") != MANAGER_PROTOCOL
        or payload.get("artifact") != archive.name or payload.get("artifact_size") != archive.size
        or payload.get("artifact_sha256") != archive.sha256 or not isinstance(payload.get("files"), list)
    ):
        raise ManagerError("manager_manifest_invalid")
    expected, total = {}, 0
    if not payload["files"] or len(payload["files"]) > MAX_ARCHIVE_MEMBERS:
        raise ManagerError("manager_manifest_invalid")
    for item in payload["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256", "executable"}:
            raise ManagerError("manager_manifest_invalid")
        path = _canonical_path(item["path"]) if isinstance(item.get("path"), str) else ""
        size, digest, executable = item.get("size"), item.get("sha256"), item.get("executable")
        if (
            not path or path in expected
            or not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_FILE_BYTES
            or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
            or not isinstance(executable, bool)
        ):
            raise ManagerError("manager_manifest_invalid")
        total += size
        if total > MAX_UNPACKED_BYTES:
            raise ManagerError("manager_manifest_invalid")
        expected[path] = item
    return payload, expected


def _extract_verified_archive(fs: Filesystem, archive_path: Path, destination: Path, expected: dict[str, dict]) -> None:
    seen, seen_entries, total = set(), set(), 0
    allowed_directories = {
        PurePosixPath(*PurePosixPath(path).parts[:index]).as_posix()
        for path in expected
        for index in range(1, len(PurePosixPath(path).parts))
    }
    try:
        archive = tarfile.open(archive_path, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise ManagerError("manager_archive_invalid") from exc
    with archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise ManagerError("manager_archive_invalid")
        for member in members:
            path = _canonical_path(member.name.rstrip("/"))
            if path in seen_entries:
                raise ManagerError("manager_archive_path_collision")
            seen_entries.add(path)
            target = destination.joinpath(*PurePosixPath(path).parts)
            try:
                target.resolve(strict=False).relative_to(destination.resolve())
            except ValueError:
                raise ManagerError("manager_archive_path_invalid") from None
            if member.isdir():
                if path not in allowed_directories:
                    raise ManagerError("manager_archive_manifest_mismatch")
                fs.mkdir(target, 0o755)
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise ManagerError("manager_archive_special_file")
            record = expected.get(path)
            if record is None or path in seen or member.size != record["size"]:
                raise ManagerError("manager_archive_manifest_mismatch")
            total += member.size
            if total > MAX_UNPACKED_BYTES:
                raise ManagerError("manager_archive_too_large")
            source = archive.extractfile(member)
            if source is None:
                raise ManagerError("manager_archive_invalid")
            payload = source.read(member.size + 1)
            if len(payload) != member.size or _sha256(payload) != record["sha256"]:
                raise ManagerError("manager_archive_hash_mismatch")
            fs.write_exclusive(target, io.BytesIO(payload), 0o755 if record["executable"] else 0o644)
            seen.add(path)
    if seen != set(expected):
        raise ManagerError("manager_archive_manifest_mismatch")


def _verify_installed_tree(
    fs: Filesystem,
    root: Path,
    expected: dict[str, dict],
    internal: dict[str, bytes] | None = None,
) -> None:
    internal = internal or {}
    if fs.regular_files(root) != set(expected) | set(internal):
        raise ManagerError("manager_install_identity_invalid")
    for path, record in expected.items():
        target = root.joinpath(*PurePosixPath(path).parts)
        raw = fs.read_bytes(target, record["size"])
        if len(raw) != record["size"] or _sha256(raw) != record["sha256"]:
            raise ManagerError("manager_install_identity_invalid")
    for path, payload in internal.items():
        if fs.read_bytes(root / path, len(payload)) != payload:
            raise ManagerError("manager_install_identity_invalid")


def _parse_systemd_show(raw: str) -> dict[str, str]:
    result = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def discover_legacy_layout(commands: CommandAdapter) -> LegacyLayout:
    """Read only fixed units; never stops or rewrites an existing deployment."""
    observed = {}
    properties = "Id,LoadState,FragmentPath,ExecStart,WorkingDirectory,EnvironmentFiles"
    for unit in UNITS:
        result = commands.run((SYSTEMD_BIN, "show", "--no-pager", f"--property={properties}", unit), check=False)
        data = _parse_systemd_show(result.stdout or "")
        if result.returncode != 0 or not data.get("LoadState"):
            return LegacyLayout("unsafe")
        if data.get("LoadState") != "not-found":
            observed[unit] = data
    if not observed:
        return LegacyLayout("none")
    if set(observed) & {API_SERVICE, CONSUMER_SERVICE} != {API_SERVICE, CONSUMER_SERVICE}:
        return LegacyLayout("unsafe")
    application_text = "\n".join(
        value for unit in APPLICATION_UNITS for value in observed[unit].values()
    )
    roots = [root for root in ("/srv/xianyu-saas", "/opt/xianyu-saas") if root in application_text]
    if len(roots) != 1:
        return LegacyLayout("unsafe")
    environment_files = set()
    for unit, data in observed.items():
        if unit in APPLICATION_UNITS:
            value = data.get("EnvironmentFiles", "").strip()
            if value:
                tokens = [token.lstrip("-") for token in value.split() if token.startswith(("/", "-/"))]
                environment_files.update(tokens)
        fragment = data.get("FragmentPath", "")
        if fragment and fragment != f"/etc/systemd/system/{data.get('Id', '')}":
            return LegacyLayout("unsafe")
        command = data.get("ExecStart", "")
        if any(token in command for token in ("/bin/sh", "/bin/bash", " sh -c", " bash -c")):
            return LegacyLayout("unsafe")
        if unit in APPLICATION_UNITS:
            for field in ("ExecStart", "WorkingDirectory"):
                text = data.get(field, "")
                if text and roots[0] not in text:
                    return LegacyLayout("unsafe")
        elif unit == UPDATER_SERVICE and command and not any(
            root in command for root in ("/opt/xianyu-saas", "/srv/xianyu-saas")
        ):
            return LegacyLayout("unsafe")
    for unit in APPLICATION_UNITS:
        data = observed[unit]
        if data.get("WorkingDirectory", "") != f"{roots[0]}/current/backend":
            return LegacyLayout("unsafe")
        command = data.get("ExecStart", "")
        required = "uvicorn" if unit == API_SERVICE else "job_consumer"
        if required not in command:
            return LegacyLayout("unsafe")
    if len(environment_files) != 1:
        return LegacyLayout("unsafe")
    env = next(iter(environment_files))
    allowed_envs = {"/etc/xianyu-saas.env", f"{roots[0]}/.env", f"{roots[0]}/config/saas.env"}
    if env not in allowed_envs:
        return LegacyLayout("unsafe")
    return LegacyLayout("supported", roots[0], env)
