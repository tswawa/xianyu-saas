"""Fail-closed first-install transaction for the standalone systemd release."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
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
MAX_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
SYSTEMD_BIN = "/usr/bin/systemctl"
SYSTEMD_ANALYZE_BIN = "/usr/bin/systemd-analyze"
USERADD_BIN = "/usr/sbin/useradd"
HEALTH_URL = "http://127.0.0.1:8096/health"
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

    @property
    def initialization_file(self) -> Path:
        return self.update_queue_dir / "status/initialization.json"

    @property
    def diagnostic_file(self) -> Path:
        return self.updater_state_dir / "install-failure.json"


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
class LegacyLayout:
    kind: str
    root: str = ""
    environment_file: str = ""


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

    def is_file(self, path: Path) -> bool:
        return path.is_file() and not path.is_symlink()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir() and not path.is_symlink()

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
        if path.exists() and not path.is_dir():
            raise ManagerError("manager_install_path_unsafe")
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)

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
        except OSError as exc:
            raise ManagerError("manager_install_file_failed") from exc

    def atomic_symlink(self, link: Path, target: str) -> None:
        self.mkdir(link.parent, 0o755)
        if self.exists(link):
            raise ManagerError("manager_install_path_occupied")
        try:
            link.symlink_to(target)
        except FileExistsError as exc:
            raise ManagerError("manager_install_path_occupied") from exc
        except OSError as exc:
            raise ManagerError("manager_install_link_failed") from exc

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

    def __init__(self, opener=None):
        self._opener = opener or urllib.request.build_opener(_BoundedRedirectHandler())

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

    def health(self) -> bool:
        request = urllib.request.Request(HEALTH_URL, headers={"User-Agent": "xianyu-saas-manager/1"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.geturl() != HEALTH_URL or response.status != 200:
                    return False
                payload = json.loads(response.read(4097))
                return payload == {"ok": True, "service": "xianyu-saas-api"}
        except (OSError, ValueError, urllib.error.URLError):
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
        updater_initializer: Callable[[], object] | None = None,
        clock=time.time,
    ):
        self.fs = filesystem or Filesystem()
        self.commands = commands or CommandAdapter()
        self.network = network or NetworkAdapter()
        self.paths = paths or InstallPaths()
        self.templates_root = (templates_root or resource_root()).resolve()
        self.executable = manager_file_path(executable)
        self.public_key = public_key if public_key is not None else read_embedded_public_key(source_root=self.templates_root)
        self.updater_initializer = updater_initializer or (lambda: invoke_updater("initialize"))
        self.clock = clock

    def install(self, *, architecture: str, version: str | None = None) -> dict:
        if architecture not in ASSET_ARCHITECTURES:
            raise ManagerError("manager_architecture_unsupported")
        if version is not None and not VERSION_RE.fullmatch(version):
            raise ManagerError("manager_release_version_invalid")
        existing = self._existing_installation(architecture)
        if existing is not None:
            if version is not None and existing["version"] != version:
                raise ManagerError("manager_install_version_conflict")
            return {**existing, "ok": True, "status": "already_installed"}

        layout = discover_legacy_layout(self.commands)
        if layout.kind == "unsafe":
            raise ManagerError("manager_legacy_layout_unsafe")
        if layout.kind == "supported":
            raise ManagerError("manager_migration_not_implemented")
        self._preflight()
        verified = self._download_and_verify(architecture, version)
        return self._commit(verified)

    def _existing_installation(self, architecture: str) -> dict | None:
        marker = self.paths.initialization_file
        occupied = any(self.fs.exists(path) for path in (
            self.paths.current_link, self.paths.manager_current_link, marker,
        ))
        if not occupied:
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
            not isinstance(payload, dict) or set(payload) != required or payload["schema"] != 2
            or payload["platform"] != "linux" or payload["architecture"] != ASSET_ARCHITECTURES[architecture]
            or payload["protocol"] != MANAGER_PROTOCOL
            or not VERSION_RE.fullmatch(str(payload["manager_version"]))
            or not SHA256_RE.fullmatch(str(payload["manager_sha256"]))
            or not SHA256_RE.fullmatch(str(payload["public_key_sha256"]))
            or type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp <= 0
        ):
            raise ManagerError("manager_install_identity_invalid")
        app_link = self.fs.readlink(self.paths.current_link)
        app_match = re.fullmatch(r"releases/([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)", app_link)
        if app_match is None:
            raise ManagerError("manager_install_identity_invalid")
        version = app_match.group(1)
        expected_manager = f"releases/{payload['manager_version']}"
        if self.fs.readlink(self.paths.manager_current_link) != expected_manager:
            raise ManagerError("manager_install_identity_invalid")
        manager = self.paths.manager_releases_dir / payload["manager_version"] / "xianyu-saas"
        if _sha256(self.fs.read_bytes(manager, MAX_FILE_BYTES)) != payload["manager_sha256"]:
            raise ManagerError("manager_install_identity_invalid")
        if _public_key_sha256(self.public_key) != payload["public_key_sha256"]:
            raise ManagerError("manager_install_identity_invalid")
        release_root = self.paths.releases_dir / version
        marker_raw = self.fs.read_bytes(release_root / MARKER_FILE, MAX_INDEX_BYTES)
        manifest_raw = self.fs.read_bytes(release_root / CACHED_MANIFEST_FILE, MAX_MANIFEST_BYTES)
        signature_raw = self.fs.read_bytes(release_root / CACHED_SIGNATURE_FILE, MAX_SIGNATURE_BYTES)
        marker_payload = _json_object(marker_raw, "manager_install_identity_invalid")
        marker_keys = {
            "schema", "version", "channel", "manifest_sha256", "release_id",
            "artifact", "artifact_size", "kind", "target",
        }
        if (set(marker_payload) != marker_keys or marker_payload.get("schema") != 1
                or marker_payload.get("version") != version or marker_payload.get("kind") != "standalone"
                or marker_payload.get("target") != f"linux-{architecture}"
                or marker_payload.get("manifest_sha256") != _sha256(manifest_raw)):
            raise ManagerError("manager_install_identity_invalid")
        _verify_signature(self.public_key, manifest_raw, signature_raw, "manager_install_identity_invalid")
        manifest_header = _json_object(manifest_raw, "manager_install_identity_invalid")
        archive = AssetRecord(
            str(marker_payload.get("artifact", "")),
            marker_payload.get("artifact_size", -1),
            str(manifest_header.get("artifact_sha256", "")),
            "standalone-archive",
            "linux",
            architecture,
            MANAGER_PROTOCOL,
        )
        try:
            _, expected_files = _parse_manifest(manifest_raw, version, architecture, archive)
        except ManagerError as exc:
            raise ManagerError("manager_install_identity_invalid") from exc
        _verify_installed_tree(
            self.fs,
            release_root,
            expected_files,
            {
                MARKER_FILE: marker_raw,
                CACHED_MANIFEST_FILE: manifest_raw,
                CACHED_SIGNATURE_FILE: signature_raw,
            },
        )
        return {
            "version": version,
            "manager_version": payload["manager_version"],
            "architecture": architecture,
        }

    def _preflight(self) -> None:
        os_id, version = self.commands.os_release()
        supported = (os_id == "ubuntu" and version in {"22.04", "24.04"}) or (os_id == "debian" and version == "12")
        if not supported:
            raise ManagerError("manager_install_os_unsupported")
        for executable, code in (
            (SYSTEMD_BIN, "manager_systemd_unavailable"),
            (SYSTEMD_ANALYZE_BIN, "manager_systemd_unavailable"),
            (USERADD_BIN, "manager_useradd_unavailable"),
        ):
            if not self.commands.available(executable):
                raise ManagerError(code)
        if self.fs.free_bytes(self.paths.install_root) < MIN_FREE_BYTES:
            raise ManagerError("manager_install_disk_insufficient")
        if not self.commands.port_available("0.0.0.0", 8096):
            raise ManagerError("manager_install_port_in_use")
        template_units = tuple(str(self._template(f"deploy/systemd/{unit}")) for unit in UNITS)
        self.commands.run((SYSTEMD_ANALYZE_BIN, "verify", *template_units))
        self._admit_destinations()

    def _admit_destinations(self) -> None:
        if self.fs.exists(self.paths.launcher_link):
            raise ManagerError("manager_install_path_occupied")
        for source, target, _mode in self._templates():
            raw = self.fs.read_bytes(source, MAX_MANIFEST_BYTES)
            if self.fs.exists(target) and (not self.fs.is_file(target) or self.fs.read_bytes(target, MAX_MANIFEST_BYTES) != raw):
                raise ManagerError("manager_install_path_occupied")
        if self.fs.exists(self.paths.env_file):
            try:
                self.fs.validate_root_file(self.paths.env_file, MAX_INDEX_BYTES)
            except ManagerError as exc:
                raise ManagerError("manager_environment_invalid") from exc
        if self.fs.exists(self.paths.public_key_file):
            try:
                self.fs.validate_root_file(self.paths.public_key_file, 4096)
            except ManagerError as exc:
                raise ManagerError("manager_public_key_conflict") from exc
            if self.fs.read_bytes(self.paths.public_key_file, 4096) != self.public_key:
                raise ManagerError("manager_public_key_conflict")
        for path in (self.paths.releases_dir, self.paths.manager_releases_dir):
            if self.fs.exists(path) and not self.fs.is_dir(path):
                raise ManagerError("manager_install_path_unsafe")

    def _download_and_verify(self, architecture: str, requested_version: str | None) -> VerifiedRelease:
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
        index = _parse_index(index_raw, version)
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
        manager_raw = self.fs.read_bytes(self.executable, MAX_FILE_BYTES)
        _verify_record(selected["manager"], manager_raw, "manager_bootstrap_identity_invalid")
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

    def _commit(self, release: VerifiedRelease) -> dict:
        work = release.archive_path.parent
        extracted = work / "release"
        service_changes = False
        created_templates: tuple[Path, ...] = ()
        try:
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
            if (runtime != release.manifest_payload.get("runtime")
                    or metadata.manager_protocol != MANAGER_PROTOCOL
                    or metadata.update_data_version != release.manifest_payload.get("update_data_version")):
                raise ManagerError("manager_runtime_metadata_invalid")
            embedded_manager = self.fs.read_bytes(extracted / "manager/xianyu-saas", MAX_FILE_BYTES)
            _verify_record(release.manager, embedded_manager, "manager_bootstrap_identity_invalid")

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
            self._create_directories(account.uid, account.gid)
            self._install_environment()
            self._install_public_key()
            self._install_manager(release, embedded_manager)
            self._install_release(release, extracted)
            created_templates = self._install_templates()
            self.updater_initializer()
            self._write_initialization(release)
            self.commands.run((SYSTEMD_BIN, "daemon-reload"))
            service_changes = True
            self.commands.run((SYSTEMD_BIN, "enable", *START_UNITS))
            self.commands.run((SYSTEMD_BIN, "start", *START_UNITS))
            if not self.network.health():
                raise ManagerError("manager_install_health_failed")
            self.fs.remove(self.paths.diagnostic_file)
            return {
                "ok": True,
                "status": "installed",
                "version": release.version,
                "manager_version": release.version,
                "architecture": release.architecture,
            }
        except Exception as exc:
            self._cleanup_failed_install(
                exc,
                service_changes=service_changes,
                created_templates=created_templates,
                release_version=release.version,
            )
            raise
        finally:
            self.fs.remove(work)

    def _create_directories(self, uid: int, gid: int) -> None:
        for path, mode in (
            (self.paths.install_root, 0o755), (self.paths.releases_dir, 0o755),
            (self.paths.manager_releases_dir, 0o755), (self.paths.state_dir, 0o700),
            (self.paths.update_queue_dir, 0o1770), (self.paths.update_queue_dir / "status", 0o755),
            (self.paths.update_queue_dir / "status/operations", 0o755),
            (self.paths.updater_state_dir, 0o700), (self.paths.public_key_file.parent, 0o755),
        ):
            self.fs.mkdir(path, mode)
        self.fs.chown_tree(self.paths.state_dir, uid, gid)
        self.fs.chown_tree(self.paths.update_queue_dir, 0, gid)

    def _install_environment(self) -> None:
        if self.fs.exists(self.paths.env_file):
            if not self.fs.is_file(self.paths.env_file):
                raise ManagerError("manager_environment_invalid")
            return
        master_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        payload = (
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
        self.fs.atomic_write(self.paths.env_file, payload, 0o600, replace=False)

    def _install_public_key(self) -> None:
        if not self.fs.exists(self.paths.public_key_file):
            self.fs.atomic_write(self.paths.public_key_file, self.public_key, 0o644, replace=False)

    def _install_manager(self, release: VerifiedRelease, payload: bytes) -> None:
        destination = self.paths.manager_releases_dir / release.version
        binary = destination / "xianyu-saas"
        if self.fs.exists(destination):
            if self.fs.regular_files(destination) != {"xianyu-saas"} or not self.fs.is_file(binary):
                raise ManagerError("manager_install_path_occupied")
            _verify_record(release.manager, self.fs.read_bytes(binary, MAX_FILE_BYTES), "manager_install_identity_invalid")
        else:
            self.fs.mkdir(destination, 0o755)
            self.fs.atomic_write(binary, payload, 0o755, replace=False)
        self.fs.atomic_symlink(self.paths.manager_current_link, f"releases/{release.version}")
        self.fs.atomic_symlink(self.paths.launcher_link, str(self.paths.manager_current_link / "xianyu-saas"))

    def _install_release(self, release: VerifiedRelease, extracted: Path) -> None:
        destination = self.paths.releases_dir / release.version
        marker = _json_bytes({
            "schema": 1,
            "version": release.version,
            "channel": "release",
            "manifest_sha256": release.manifest_sha256,
            "release_id": "bootstrap-install",
            "artifact": release.archive.name,
            "artifact_size": release.archive.size,
            "kind": "standalone",
            "target": f"linux-{release.architecture}",
        })
        internal = {
            CACHED_MANIFEST_FILE: release.manifest_raw,
            CACHED_SIGNATURE_FILE: release.signature_raw,
            MARKER_FILE: marker,
        }
        if self.fs.exists(destination):
            _verify_installed_tree(self.fs, destination, release.expected_files, internal)
            self.fs.remove(extracted)
        else:
            for name, payload in internal.items():
                self.fs.atomic_write(extracted / name, payload, 0o644, replace=False)
            self.fs.publish_directory(extracted, destination)
        self.fs.atomic_symlink(self.paths.current_link, f"releases/{release.version}")

    def _install_templates(self) -> tuple[Path, ...]:
        created = []
        for source, destination, mode in self._templates():
            raw = self.fs.read_bytes(source, MAX_MANIFEST_BYTES)
            if not self.fs.exists(destination):
                self.fs.atomic_write(destination, raw, mode, replace=False)
                created.append(destination)
        return tuple(created)

    def _write_initialization(self, release: VerifiedRelease) -> None:
        payload = _json_bytes({
            "schema": 2,
            "protocol": MANAGER_PROTOCOL,
            "public_key_sha256": _public_key_sha256(self.public_key),
            "manager_version": release.version,
            "manager_sha256": release.manager.sha256,
            "platform": "linux",
            "architecture": release.architecture,
            "initialized_at": self.clock(),
        })
        self.fs.atomic_write(self.paths.initialization_file, payload, 0o644)

    def _cleanup_failed_install(
        self,
        exc: Exception,
        *,
        service_changes: bool,
        created_templates: tuple[Path, ...],
        release_version: str,
    ) -> None:
        try:
            if service_changes:
                self.commands.run((SYSTEMD_BIN, "stop", *reversed(START_UNITS)), check=False)
                self.commands.run((SYSTEMD_BIN, "disable", *START_UNITS), check=False)
        finally:
            self.fs.remove(self.paths.initialization_file)
            expected_links = {
                self.paths.current_link: f"releases/{release_version}",
                self.paths.manager_current_link: f"releases/{release_version}",
                self.paths.launcher_link: str(self.paths.manager_current_link / "xianyu-saas"),
            }
            for path, target in expected_links.items():
                try:
                    if self.fs.readlink(path) == target:
                        self.fs.remove(path)
                except ManagerError:
                    pass
            for path in created_templates:
                self.fs.remove(path)
            code = exc.code if isinstance(exc, ManagerError) else "manager_install_failed"
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
    for unit in (API_SERVICE, CONSUMER_SERVICE, UPDATER_SERVICE):
        result = commands.run((SYSTEMD_BIN, "show", "--no-pager", f"--property={properties}", unit), check=False)
        data = _parse_systemd_show(result.stdout or "")
        if result.returncode == 0 and data.get("LoadState") not in {"", "not-found"}:
            observed[unit] = data
    if not observed:
        return LegacyLayout("none")
    if set(observed) & {API_SERVICE, CONSUMER_SERVICE} != {API_SERVICE, CONSUMER_SERVICE}:
        return LegacyLayout("unsafe")
    combined = "\n".join(value for data in observed.values() for value in data.values())
    roots = [root for root in ("/srv/xianyu-saas", "/opt/xianyu-saas") if root in combined]
    if len(roots) != 1:
        return LegacyLayout("unsafe")
    environment_files = set()
    for data in observed.values():
        value = data.get("EnvironmentFiles", "").strip()
        if value:
            tokens = [token.lstrip("-") for token in value.split() if token.startswith(("/", "-/"))]
            environment_files.update(tokens)
        fragment = data.get("FragmentPath", "")
        if fragment and not fragment.startswith("/etc/systemd/system/"):
            return LegacyLayout("unsafe")
        for field in ("ExecStart", "WorkingDirectory"):
            text = data.get(field, "")
            if text and roots[0] not in text:
                return LegacyLayout("unsafe")
        if any(token in data.get("ExecStart", "") for token in ("/bin/sh", "/bin/bash", " sh -c", " bash -c")):
            return LegacyLayout("unsafe")
    if len(environment_files) != 1:
        return LegacyLayout("unsafe")
    env = next(iter(environment_files))
    allowed_envs = {"/etc/xianyu-saas.env", f"{roots[0]}/.env", f"{roots[0]}/config/saas.env"}
    if env not in allowed_envs:
        return LegacyLayout("unsafe")
    return LegacyLayout("supported", roots[0], env)
