"""Signed, server-pinned GitHub Release download and update intent handling.

The API may inspect and stage a signed code release, but it never switches the
running tree or invokes systemd.  Only the independent updater consumes the
0600 intent written by this module.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import urlsplit

import requests
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


RELEASE_OWNER = "tswawa"
RELEASE_REPOSITORY = "xianyu-saas"
GITHUB_API_HOST = "api.github.com"
GITHUB_API_ROOT = f"https://{GITHUB_API_HOST}/repos/{RELEASE_OWNER}/{RELEASE_REPOSITORY}"
GITHUB_API_VERSION = "2022-11-28"
RELEASE_ASSET_PREFIX = "xianyu-saas"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_RELEASE_METADATA_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SIGNATURE_BYTES = 4096
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5000
MAX_RELEASE_NOTES_CHARS = 16_000
MAX_PATH_LENGTH = 500
MAX_PATH_COMPONENT = 240
VALID_CHANNELS = frozenset({"stable", "beta"})
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_TOP_LEVEL_DIRS = frozenset(
    {"backend", "frontend", "worker", "scripts", "deploy", "config", "docs", "tests"}
)
ALLOWED_ROOT_FILES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "CHANGELOG.md",
        ".gitignore",
    }
)
FORBIDDEN_PATH_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        "current",
        "releases",
        "staging",
        "tenants",
        "data",
        "logs",
        "backups",
        "update-intents",
        "credentials",
        "secrets",
    }
)
DEPENDENCY_TEXT_FILES = (
    "backend/requirements.txt",
    "worker/requirements.txt",
)
DEPENDENCY_JSON_FIELDS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
    "engines",
)
MARKER_FILE = ".xianyu-release.json"
CACHED_MANIFEST_FILE = ".xianyu-manifest.json"
CACHED_SIGNATURE_FILE = ".xianyu-manifest.sig"
INTERNAL_CANDIDATE_FILES = frozenset(
    {MARKER_FILE, CACHED_MANIFEST_FILE, CACHED_SIGNATURE_FILE}
)


class PlatformUpdateError(RuntimeError):
    """Stable update error safe to map to an API error code."""

    def __init__(self, code: str, message: str = "更新操作失败"):
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...]

    @classmethod
    def parse(cls, value: str) -> "SemVer":
        match = SEMVER_RE.fullmatch(str(value or "").strip())
        if match is None:
            raise PlatformUpdateError("release_version_invalid")
        prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
        for part in prerelease:
            if part.isdigit() and len(part) > 1 and part.startswith("0"):
                raise PlatformUpdateError("release_version_invalid")
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease)

    def _prerelease_compare(self, other: "SemVer") -> int:
        if not self.prerelease and not other.prerelease:
            return 0
        if not self.prerelease:
            return 1
        if not other.prerelease:
            return -1
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_numeric = left.isdigit()
            right_numeric = right.isdigit()
            if left_numeric and right_numeric:
                return -1 if int(left) < int(right) else 1
            if left_numeric != right_numeric:
                return -1 if left_numeric else 1
            return -1 if left < right else 1
        if len(self.prerelease) == len(other.prerelease):
            return 0
        return -1 if len(self.prerelease) < len(other.prerelease) else 1

    def compare(self, other: "SemVer") -> int:
        left = (self.major, self.minor, self.patch)
        right = (other.major, other.minor, other.patch)
        if left != right:
            return -1 if left < right else 1
        return self._prerelease_compare(other)


@dataclass(frozen=True)
class ReleaseAsset:
    asset_id: int
    name: str
    size: int

    @property
    def api_url(self) -> str:
        return f"{GITHUB_API_ROOT}/releases/assets/{self.asset_id}"


@dataclass(frozen=True)
class ReleaseInfo:
    release_id: str
    version: str
    tag: str
    published_at: str
    notes: str
    prerelease: bool
    artifact: ReleaseAsset
    manifest: ReleaseAsset
    signature: ReleaseAsset


@dataclass(frozen=True)
class ManifestFile:
    path: str
    size: int
    sha256: str
    executable: bool


def _github_headers(*, binary: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "xianyu-saas-updater/1",
    }
    token = os.environ.get("SAAS_GITHUB_READ_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _validate_fixed_api_url(url: str, *, asset: bool = False) -> None:
    parsed = urlsplit(str(url or ""))
    if (
        parsed.scheme != "https"
        or parsed.hostname != GITHUB_API_HOST
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise PlatformUpdateError("update_source_rejected")
    if asset:
        pattern = re.compile(
            rf"^/repos/{re.escape(RELEASE_OWNER)}/{re.escape(RELEASE_REPOSITORY)}/releases/assets/[1-9][0-9]*$"
        )
        if not pattern.fullmatch(parsed.path) or parsed.query:
            raise PlatformUpdateError("update_source_rejected")
        return
    expected = f"/repos/{RELEASE_OWNER}/{RELEASE_REPOSITORY}/releases"
    if parsed.path != expected or parsed.query != "per_page=30":
        raise PlatformUpdateError("update_source_rejected")


def _status_error(status_code: int) -> PlatformUpdateError:
    if status_code in {401, 403}:
        return PlatformUpdateError("update_source_auth_failed")
    if status_code == 404:
        return PlatformUpdateError("update_release_not_found")
    if status_code == 429:
        return PlatformUpdateError("update_source_rate_limited")
    return PlatformUpdateError("update_source_failed")


def _response_bytes(response, max_bytes: int) -> bytes:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if 300 <= status_code < 400:
        raise PlatformUpdateError("update_redirect_rejected")
    if status_code != 200:
        raise _status_error(status_code)
    raw_length = str(getattr(response, "headers", {}).get("content-length", "") or "").strip()
    if raw_length:
        try:
            if int(raw_length) > max_bytes:
                raise PlatformUpdateError("update_download_too_large")
        except ValueError as exc:
            raise PlatformUpdateError("update_source_invalid") from exc
    payload = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise PlatformUpdateError("update_download_too_large")
    except requests.RequestException as exc:
        raise PlatformUpdateError("update_source_failed") from exc
    return bytes(payload)


def _request_bytes(session, url: str, *, max_bytes: int, asset: bool = False) -> bytes:
    _validate_fixed_api_url(url, asset=asset)
    try:
        response = session.get(
            url,
            headers=_github_headers(binary=asset),
            timeout=(5, 60),
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException as exc:
        raise PlatformUpdateError("update_source_failed") from exc
    try:
        return _response_bytes(response, max_bytes)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _download_asset_to_file(session, asset: ReleaseAsset, destination: Path) -> str:
    _validate_fixed_api_url(asset.api_url, asset=True)
    if asset.size <= 0 or asset.size > MAX_ARCHIVE_BYTES:
        raise PlatformUpdateError("update_download_too_large")
    try:
        response = session.get(
            asset.api_url,
            headers=_github_headers(binary=True),
            timeout=(5, 120),
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException as exc:
        raise PlatformUpdateError("update_source_failed") from exc
    status_code = int(getattr(response, "status_code", 0) or 0)
    try:
        if 300 <= status_code < 400:
            raise PlatformUpdateError("update_redirect_rejected")
        if status_code != 200:
            raise _status_error(status_code)
        digest = hashlib.sha256()
        total = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                descriptor = -1
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES or total > asset.size:
                        raise PlatformUpdateError("update_download_too_large")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if total != asset.size:
            raise PlatformUpdateError("update_download_size_mismatch")
        return digest.hexdigest()
    except requests.RequestException as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise PlatformUpdateError("update_source_failed") from exc
    except OSError as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise PlatformUpdateError("update_staging_failed") from exc
    except BaseException:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _version_from_tag(tag: str) -> str:
    tag = str(tag or "").strip()
    version = tag[1:] if tag.startswith("v") else tag
    SemVer.parse(version)
    return version


def _asset_names(version: str) -> tuple[str, str, str]:
    base = f"{RELEASE_ASSET_PREFIX}-{version}"
    return f"{base}.tar.gz", f"{base}.manifest.json", f"{base}.manifest.sig"


def _parse_asset(raw, expected_name: str, max_size: int) -> ReleaseAsset:
    if not isinstance(raw, dict) or str(raw.get("name", "")) != expected_name:
        raise PlatformUpdateError("release_assets_invalid")
    try:
        asset_id = int(raw["id"])
        size = int(raw["size"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlatformUpdateError("release_assets_invalid") from exc
    if asset_id <= 0 or size <= 0 or size > max_size:
        raise PlatformUpdateError("release_assets_invalid")
    return ReleaseAsset(asset_id=asset_id, name=expected_name, size=size)


def _parse_release(raw, channel: str) -> ReleaseInfo | None:
    if not isinstance(raw, dict) or raw.get("draft") is True:
        return None
    try:
        version = _version_from_tag(str(raw.get("tag_name", "")))
        parsed_version = SemVer.parse(version)
    except PlatformUpdateError:
        return None
    prerelease = bool(raw.get("prerelease") or parsed_version.prerelease)
    if channel == "stable" and prerelease:
        return None
    names = _asset_names(version)
    assets = raw.get("assets")
    if not isinstance(assets, list):
        raise PlatformUpdateError("release_assets_invalid")
    by_name: dict[str, dict] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        if name in by_name:
            raise PlatformUpdateError("release_assets_invalid")
        by_name[name] = asset
    if any(name not in by_name for name in names):
        raise PlatformUpdateError("release_assets_missing")
    artifact = _parse_asset(by_name[names[0]], names[0], MAX_ARCHIVE_BYTES)
    manifest = _parse_asset(by_name[names[1]], names[1], MAX_MANIFEST_BYTES)
    signature = _parse_asset(by_name[names[2]], names[2], MAX_SIGNATURE_BYTES)
    notes = str(raw.get("body", ""))[:MAX_RELEASE_NOTES_CHARS]
    return ReleaseInfo(
        release_id=str(raw.get("id", ""))[:120],
        version=version,
        tag=str(raw.get("tag_name", ""))[:120],
        published_at=str(raw.get("published_at", ""))[:80],
        notes=notes,
        prerelease=prerelease,
        artifact=artifact,
        manifest=manifest,
        signature=signature,
    )


def fetch_release(channel: str, current_version: str, session=None) -> ReleaseInfo | None:
    channel = str(channel or "")
    if channel not in VALID_CHANNELS:
        raise PlatformUpdateError("update_channel_invalid")
    current = SemVer.parse(current_version)
    session = session or requests.Session()
    url = f"{GITHUB_API_ROOT}/releases?per_page=30"
    raw = _request_bytes(session, url, max_bytes=MAX_RELEASE_METADATA_BYTES)
    try:
        releases = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlatformUpdateError("update_source_invalid") from exc
    if not isinstance(releases, list):
        raise PlatformUpdateError("update_source_invalid")
    candidates: list[tuple[SemVer, ReleaseInfo]] = []
    for item in releases:
        release = _parse_release(item, channel)
        if release is None:
            continue
        version = SemVer.parse(release.version)
        if version.compare(current) > 0:
            candidates.append((version, release))
    if not candidates:
        return None
    winner_version, winner = candidates[0]
    for candidate_version, candidate in candidates[1:]:
        if candidate_version.compare(winner_version) > 0:
            winner_version, winner = candidate_version, candidate
    return winner


def release_payload(release: ReleaseInfo | None, channel: str, current_version: str) -> dict:
    if release is None:
        return {
            "available": False,
            "current_version": current_version,
            "channel": channel,
        }
    return {
        "available": True,
        "current_version": current_version,
        "version": release.version,
        "channel": channel,
        "published_at": release.published_at,
        "release_notes": release.notes,
    }


def _public_key_file() -> Path:
    raw = os.environ.get("SAAS_UPDATE_PUBLIC_KEY_FILE", "").strip()
    if not raw:
        raise PlatformUpdateError("update_public_key_missing")
    path = Path(raw)
    if not path.is_absolute():
        raise PlatformUpdateError("update_public_key_invalid")
    return path


def load_public_key() -> Ed25519PublicKey:
    path = _public_key_file()
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PlatformUpdateError("update_public_key_invalid")
        if (
            metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_mode & 0o022
            or metadata.st_size <= 0
            or metadata.st_size > 4096
        ):
            raise PlatformUpdateError("update_public_key_invalid")
        raw = path.read_bytes().strip()
    except OSError as exc:
        raise PlatformUpdateError("update_public_key_invalid") from exc
    try:
        if raw.startswith(b"-----BEGIN"):
            key = serialization.load_pem_public_key(raw)
            if not isinstance(key, Ed25519PublicKey):
                raise PlatformUpdateError("update_public_key_invalid")
            return key
        decoded = base64.b64decode(raw, validate=True)
        if len(decoded) != 32:
            raise PlatformUpdateError("update_public_key_invalid")
        return Ed25519PublicKey.from_public_bytes(decoded)
    except (TypeError, ValueError, UnsupportedAlgorithm, binascii.Error) as exc:
        raise PlatformUpdateError("update_public_key_invalid") from exc


def _decode_signature(raw: bytes) -> bytes:
    if len(raw) == 64:
        return raw
    stripped = raw.strip()
    try:
        decoded = base64.b64decode(stripped, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PlatformUpdateError("update_signature_invalid") from exc
    if len(decoded) != 64:
        raise PlatformUpdateError("update_signature_invalid")
    return decoded


def verify_manifest_signature(manifest_raw: bytes, signature_raw: bytes) -> None:
    signature = _decode_signature(signature_raw)
    try:
        load_public_key().verify(signature, manifest_raw)
    except InvalidSignature as exc:
        raise PlatformUpdateError("update_signature_invalid") from exc


def _validate_release_path(raw_path: str, *, directory: bool = False) -> str:
    raw_path = str(raw_path or "")
    if not raw_path or "\x00" in raw_path or "\\" in raw_path or len(raw_path) > MAX_PATH_LENGTH:
        raise PlatformUpdateError("update_archive_path_invalid")
    path = PurePosixPath(raw_path.rstrip("/") if directory else raw_path)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise PlatformUpdateError("update_archive_path_invalid")
    if any(len(part) > MAX_PATH_COMPONENT for part in path.parts):
        raise PlatformUpdateError("update_archive_path_invalid")
    lower_parts = tuple(part.casefold() for part in path.parts)
    if any(part in FORBIDDEN_PATH_PARTS for part in lower_parts):
        raise PlatformUpdateError("update_runtime_path_rejected")
    if any(part.startswith(".env") for part in lower_parts):
        raise PlatformUpdateError("update_runtime_path_rejected")
    filename = lower_parts[-1]
    if (
        filename.endswith((".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".log", ".pid", ".cookie"))
        or filename in {"saas.db", "intent.json"}
    ):
        raise PlatformUpdateError("update_runtime_path_rejected")
    if len(path.parts) == 1:
        if path.parts[0] not in ALLOWED_ROOT_FILES and path.parts[0] not in ALLOWED_TOP_LEVEL_DIRS:
            raise PlatformUpdateError("update_archive_path_invalid")
    elif path.parts[0] not in ALLOWED_TOP_LEVEL_DIRS:
        raise PlatformUpdateError("update_archive_path_invalid")
    return path.as_posix()


def _manifest_files(raw_files) -> dict[str, ManifestFile]:
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > MAX_ARCHIVE_MEMBERS:
        raise PlatformUpdateError("update_manifest_invalid")
    files: dict[str, ManifestFile] = {}
    folded: set[str] = set()
    total = 0
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise PlatformUpdateError("update_manifest_invalid")
        path = _validate_release_path(str(raw.get("path", "")))
        folded_path = path.casefold()
        if path in files or folded_path in folded:
            raise PlatformUpdateError("update_archive_duplicate")
        try:
            size = int(raw["size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PlatformUpdateError("update_manifest_invalid") from exc
        sha256 = str(raw.get("sha256", "")).lower()
        if size < 0 or size > MAX_FILE_BYTES or not SHA256_RE.fullmatch(sha256):
            raise PlatformUpdateError("update_manifest_invalid")
        executable = bool(raw.get("executable", False))
        total += size
        if total > MAX_UNPACKED_BYTES:
            raise PlatformUpdateError("update_archive_too_large")
        files[path] = ManifestFile(path, size, sha256, executable)
        folded.add(folded_path)
    return files


def parse_manifest(manifest_raw: bytes, release: ReleaseInfo) -> tuple[dict, dict[str, ManifestFile]]:
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlatformUpdateError("update_manifest_invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise PlatformUpdateError("update_manifest_invalid")
    if str(manifest.get("version", "")) != release.version:
        raise PlatformUpdateError("update_manifest_version_mismatch")
    if str(manifest.get("artifact", "")) != release.artifact.name:
        raise PlatformUpdateError("update_manifest_invalid")
    artifact_hash = str(manifest.get("artifact_sha256", "")).lower()
    try:
        artifact_size = int(manifest.get("artifact_size", -1))
    except (TypeError, ValueError) as exc:
        raise PlatformUpdateError("update_manifest_invalid") from exc
    if not SHA256_RE.fullmatch(artifact_hash) or artifact_size != release.artifact.size:
        raise PlatformUpdateError("update_manifest_invalid")
    files = _manifest_files(manifest.get("files"))
    return manifest, files


def _safe_destination(root: Path, path: str) -> Path:
    destination = root.joinpath(*PurePosixPath(path).parts)
    root_resolved = root.resolve()
    resolved = destination.resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PlatformUpdateError("update_archive_path_invalid")
    return destination


def _write_member(source: BinaryIO, destination: Path, expected: ManifestFile) -> None:
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    mode = 0o755 if expected.executable else 0o644
    descriptor = os.open(destination, flags, mode)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            while True:
                chunk = source.read(128 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected.size or total > MAX_FILE_BYTES:
                    raise PlatformUpdateError("update_archive_size_mismatch")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if total != expected.size or not secrets.compare_digest(digest.hexdigest(), expected.sha256):
        destination.unlink(missing_ok=True)
        raise PlatformUpdateError("update_archive_hash_mismatch")


def extract_verified_archive(
    archive_path: Path,
    candidate_root: Path,
    expected_files: dict[str, ManifestFile],
) -> None:
    try:
        with archive_path.open("rb") as probe:
            if probe.read(2) != b"\x1f\x8b":
                raise PlatformUpdateError("update_archive_format_invalid")
        archive = tarfile.open(archive_path, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        if isinstance(exc, PlatformUpdateError):
            raise
        raise PlatformUpdateError("update_archive_format_invalid") from exc
    seen: set[str] = set()
    folded: set[str] = set()
    extracted_files: set[str] = set()
    total_size = 0
    member_count = 0
    try:
        for member in archive:
            member_count += 1
            if member_count > MAX_ARCHIVE_MEMBERS:
                raise PlatformUpdateError("update_archive_too_many_files")
            is_directory = member.isdir()
            path = _validate_release_path(member.name, directory=is_directory)
            if path in seen or path.casefold() in folded:
                raise PlatformUpdateError("update_archive_duplicate")
            seen.add(path)
            folded.add(path.casefold())
            if member.issym() or member.islnk():
                raise PlatformUpdateError("update_archive_link_rejected")
            if member.isdev() or member.isfifo() or not (member.isfile() or is_directory):
                raise PlatformUpdateError("update_archive_type_rejected")
            if getattr(member, "sparse", None):
                raise PlatformUpdateError("update_archive_type_rejected")
            destination = _safe_destination(candidate_root, path)
            if is_directory:
                destination.mkdir(mode=0o755, parents=True, exist_ok=True)
                if destination.is_symlink() or not destination.is_dir():
                    raise PlatformUpdateError("update_archive_path_invalid")
                continue
            expected = expected_files.get(path)
            if expected is None or int(member.size) != expected.size:
                raise PlatformUpdateError("update_archive_manifest_mismatch")
            total_size += int(member.size)
            if total_size > MAX_UNPACKED_BYTES:
                raise PlatformUpdateError("update_archive_too_large")
            source = archive.extractfile(member)
            if source is None:
                raise PlatformUpdateError("update_archive_format_invalid")
            with source:
                _write_member(source, destination, expected)
            extracted_files.add(path)
    except BaseException:
        raise
    finally:
        archive.close()
    if extracted_files != set(expected_files):
        raise PlatformUpdateError("update_archive_manifest_mismatch")


def _json_file(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlatformUpdateError("update_candidate_invalid") from exc
    if not isinstance(payload, dict):
        raise PlatformUpdateError("update_candidate_invalid")
    return payload


def _normalized_package_lock(root: Path) -> dict | None:
    path = root / "package-lock.json"
    if not path.exists():
        return None
    payload = _json_file(path)
    payload = json.loads(json.dumps(payload, ensure_ascii=True))
    payload.pop("name", None)
    payload.pop("version", None)
    packages = payload.get("packages")
    if isinstance(packages, dict) and isinstance(packages.get(""), dict):
        packages[""] = dict(packages[""])
        packages[""].pop("name", None)
        packages[""].pop("version", None)
    return payload


def _verify_dependency_stability(candidate_root: Path, source_root: Path) -> None:
    for relative in DEPENDENCY_TEXT_FILES:
        current = source_root / relative
        candidate = candidate_root / relative
        if current.exists() != candidate.exists():
            raise PlatformUpdateError("update_dependency_change_rejected")
        if current.exists():
            try:
                if not secrets.compare_digest(
                    hashlib.sha256(current.read_bytes()).digest(),
                    hashlib.sha256(candidate.read_bytes()).digest(),
                ):
                    raise PlatformUpdateError("update_dependency_change_rejected")
            except OSError as exc:
                raise PlatformUpdateError("update_dependency_check_failed") from exc
    current_package = _json_file(source_root / "package.json")
    candidate_package = _json_file(candidate_root / "package.json")
    for field in DEPENDENCY_JSON_FIELDS:
        if current_package.get(field, {}) != candidate_package.get(field, {}):
            raise PlatformUpdateError("update_dependency_change_rejected")
    if _normalized_package_lock(source_root) != _normalized_package_lock(candidate_root):
        raise PlatformUpdateError("update_dependency_change_rejected")


def _verify_candidate_version(candidate_root: Path, version: str) -> None:
    package = _json_file(candidate_root / "package.json")
    if str(package.get("version", "")) != version:
        raise PlatformUpdateError("update_manifest_version_mismatch")
    try:
        version_source = (candidate_root / "backend" / "version.py").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PlatformUpdateError("update_candidate_invalid") from exc
    match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']\s*$', version_source, re.MULTILINE)
    if match is None or match.group(1) != version:
        raise PlatformUpdateError("update_manifest_version_mismatch")


def _staging_root() -> Path:
    raw = os.environ.get("SAAS_UPDATE_STAGING_DIR", "/var/lib/xianyu-saas/update-staging").strip()
    path = Path(raw)
    if not path.is_absolute():
        raise PlatformUpdateError("update_staging_invalid")
    return path


def _source_root() -> Path:
    raw = os.environ.get("SAAS_CURRENT_ROOT", str(PROJECT_ROOT)).strip()
    path = Path(raw)
    if not path.is_absolute():
        raise PlatformUpdateError("update_source_root_invalid")
    return path.resolve()


def _create_staging_directory(version: str) -> Path:
    root = _staging_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise PlatformUpdateError("update_staging_invalid") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PlatformUpdateError("update_staging_invalid")
    os.chmod(root, 0o700)
    candidate = Path(tempfile.mkdtemp(prefix=f"{version}-", dir=root))
    candidate.chmod(0o700)
    return candidate


def _write_secure_file(path: Path, payload: bytes, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _write_marker(candidate_root: Path, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _write_secure_file(candidate_root / MARKER_FILE, encoded)


def stage_release(
    release: ReleaseInfo,
    channel: str,
    current_version: str,
    *,
    session=None,
) -> dict:
    if channel not in VALID_CHANNELS:
        raise PlatformUpdateError("update_channel_invalid")
    if SemVer.parse(release.version).compare(SemVer.parse(current_version)) <= 0:
        raise PlatformUpdateError("update_downgrade_rejected")
    session = session or requests.Session()
    manifest_raw = _request_bytes(
        session, release.manifest.api_url, max_bytes=MAX_MANIFEST_BYTES, asset=True
    )
    signature_raw = _request_bytes(
        session, release.signature.api_url, max_bytes=MAX_SIGNATURE_BYTES, asset=True
    )
    verify_manifest_signature(manifest_raw, signature_raw)
    manifest, expected_files = parse_manifest(manifest_raw, release)
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    stage: Path | None = None
    try:
        stage = _create_staging_directory(release.version)
        archive_path = stage / release.artifact.name
        candidate_root = stage / "candidate"
        candidate_root.mkdir(mode=0o700)
        artifact_sha256 = _download_asset_to_file(session, release.artifact, archive_path)
        if not secrets.compare_digest(artifact_sha256, str(manifest["artifact_sha256"])):
            raise PlatformUpdateError("update_artifact_hash_mismatch")
        extract_verified_archive(archive_path, candidate_root, expected_files)
        _verify_candidate_version(candidate_root, release.version)
        _verify_dependency_stability(candidate_root, _source_root())
        _write_secure_file(candidate_root / CACHED_MANIFEST_FILE, manifest_raw)
        _write_secure_file(candidate_root / CACHED_SIGNATURE_FILE, signature_raw)
        _write_marker(
            candidate_root,
            {
                "schema": 1,
                "version": release.version,
                "channel": channel,
                "manifest_sha256": manifest_sha256,
                "release_id": release.release_id,
                "artifact": release.artifact.name,
                "artifact_size": release.artifact.size,
            },
        )
        archive_path.unlink(missing_ok=True)
    except PlatformUpdateError:
        import shutil

        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    except OSError as exc:
        import shutil

        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        raise PlatformUpdateError("update_staging_failed") from exc
    except BaseException:
        import shutil

        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    return {
        "version": release.version,
        "channel": channel,
        "release_id": release.release_id,
        "manifest_sha256": manifest_sha256,
        "candidate_path": str(candidate_root.resolve()),
        "release_notes": release.notes,
    }


def _candidate_root(candidate_path: str) -> Path:
    configured_root = _staging_root()
    candidate = Path(str(candidate_path or ""))
    if not candidate.is_absolute():
        raise PlatformUpdateError("update_candidate_invalid")
    try:
        root_metadata = configured_root.lstat()
        candidate_metadata = candidate.lstat()
        parent_metadata = candidate.parent.lstat()
        root = configured_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PlatformUpdateError("update_candidate_invalid") from exc
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or stat.S_ISLNK(candidate_metadata.st_mode)
        or not stat.S_ISDIR(candidate_metadata.st_mode)
        or root not in resolved.parents
        or resolved.name != "candidate"
    ):
        raise PlatformUpdateError("update_candidate_invalid")
    return resolved


def _read_secure_file(path: Path, max_bytes: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PlatformUpdateError("update_candidate_invalid") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size < 0
        or metadata.st_size > max_bytes
    ):
        raise PlatformUpdateError("update_candidate_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != metadata.st_size:
                raise PlatformUpdateError("update_candidate_invalid")
            payload = bytearray()
            while True:
                chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    raise PlatformUpdateError("update_candidate_invalid")
            return bytes(payload)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PlatformUpdateError("update_candidate_invalid") from exc


def _marker_payload(candidate: Path) -> dict:
    raw = _read_secure_file(candidate / MARKER_FILE, 8192)
    try:
        marker = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlatformUpdateError("update_candidate_invalid") from exc
    if not isinstance(marker, dict):
        raise PlatformUpdateError("update_candidate_invalid")
    return marker


def _release_from_marker(marker: dict) -> ReleaseInfo:
    version = str(marker.get("version", ""))
    artifact_name = str(marker.get("artifact", ""))
    try:
        artifact_size = int(marker.get("artifact_size", -1))
    except (TypeError, ValueError) as exc:
        raise PlatformUpdateError("update_candidate_invalid") from exc
    expected_names = _asset_names(version)
    if artifact_name != expected_names[0] or artifact_size <= 0 or artifact_size > MAX_ARCHIVE_BYTES:
        raise PlatformUpdateError("update_candidate_invalid")
    return ReleaseInfo(
        release_id=str(marker.get("release_id", ""))[:120],
        version=version,
        tag=f"v{version}",
        published_at="",
        notes="",
        prerelease=bool(SemVer.parse(version).prerelease),
        artifact=ReleaseAsset(1, expected_names[0], artifact_size),
        manifest=ReleaseAsset(2, expected_names[1], 1),
        signature=ReleaseAsset(3, expected_names[2], 1),
    )


def load_verified_candidate(
    candidate_path: str,
    version: str,
    manifest_sha256: str = "",
) -> tuple[dict, dict[str, ManifestFile]]:
    resolved = _candidate_root(candidate_path)
    marker = _marker_payload(resolved)
    if (
        marker.get("schema") != 1
        or str(marker.get("version", "")) != str(version)
        or (manifest_sha256 and str(marker.get("manifest_sha256", "")) != manifest_sha256)
    ):
        raise PlatformUpdateError("update_candidate_invalid")
    manifest_raw = _read_secure_file(resolved / CACHED_MANIFEST_FILE, MAX_MANIFEST_BYTES)
    signature_raw = _read_secure_file(resolved / CACHED_SIGNATURE_FILE, MAX_SIGNATURE_BYTES)
    if not secrets.compare_digest(
        hashlib.sha256(manifest_raw).hexdigest(), str(marker.get("manifest_sha256", ""))
    ):
        raise PlatformUpdateError("update_candidate_invalid")
    verify_manifest_signature(manifest_raw, signature_raw)
    release = _release_from_marker(marker)
    _, expected_files = parse_manifest(manifest_raw, release)
    seen_files: set[str] = set()
    total_size = 0
    for directory, directories, filenames in os.walk(resolved, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in tuple(directories):
            child = directory_path / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise PlatformUpdateError("update_candidate_invalid")
            relative = child.relative_to(resolved).as_posix()
            _validate_release_path(relative, directory=True)
        for name in filenames:
            child = directory_path / name
            relative = child.relative_to(resolved).as_posix()
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise PlatformUpdateError("update_candidate_invalid")
            if relative in INTERNAL_CANDIDATE_FILES:
                continue
            relative = _validate_release_path(relative)
            expected = expected_files.get(relative)
            if expected is None or int(metadata.st_size) != expected.size:
                raise PlatformUpdateError("update_archive_manifest_mismatch")
            total_size += int(metadata.st_size)
            if total_size > MAX_UNPACKED_BYTES:
                raise PlatformUpdateError("update_archive_too_large")
            digest = hashlib.sha256(_read_secure_file(child, expected.size)).hexdigest()
            if not secrets.compare_digest(digest, expected.sha256):
                raise PlatformUpdateError("update_archive_hash_mismatch")
            seen_files.add(relative)
    if seen_files != set(expected_files):
        raise PlatformUpdateError("update_archive_manifest_mismatch")
    _verify_candidate_version(resolved, str(version))
    _verify_dependency_stability(resolved, _source_root())
    return marker, expected_files


def validate_candidate(candidate_path: str, version: str, manifest_sha256: str = "") -> dict:
    marker, _ = load_verified_candidate(candidate_path, version, manifest_sha256)
    return marker


def available_rollback_versions(current_version: str) -> list[str]:
    raw = os.environ.get("SAAS_RELEASES_DIR", "/opt/xianyu-saas/releases").strip()
    root = Path(raw)
    if not root.is_absolute() or not root.exists() or root.is_symlink() or not root.is_dir():
        return []
    current = SemVer.parse(current_version)
    versions: list[tuple[SemVer, str]] = []
    try:
        entries = tuple(root.iterdir())
    except OSError:
        return []
    for path in entries:
        if path.is_symlink() or not path.is_dir() or path.name == current_version:
            continue
        try:
            parsed = SemVer.parse(path.name)
            marker = _json_file(path / MARKER_FILE)
        except PlatformUpdateError:
            continue
        if parsed.compare(current) >= 0:
            continue
        if marker.get("schema") != 1 or str(marker.get("version", "")) != path.name:
            continue
        versions.append((parsed, path.name))
    ordered: list[tuple[SemVer, str]] = []
    for item in versions:
        inserted = False
        for index, existing in enumerate(ordered):
            if item[0].compare(existing[0]) > 0:
                ordered.insert(index, item)
                inserted = True
                break
        if not inserted:
            ordered.append(item)
    return [version for _, version in ordered[:3]]


def _intent_file() -> Path:
    raw = os.environ.get(
        "SAAS_UPDATE_INTENT_FILE", "/var/lib/xianyu-saas/update-intents/intent.json"
    ).strip()
    path = Path(raw)
    if not path.is_absolute():
        raise PlatformUpdateError("update_intent_path_invalid")
    return path


def _write_update_intent_unwrapped(
    action: str,
    version: str,
    *,
    channel: str,
    requested_by: int,
    candidate_path: str = "",
    manifest_sha256: str = "",
) -> dict:
    action = str(action)
    channel = str(channel)
    if action not in {"apply", "rollback"}:
        raise PlatformUpdateError("update_intent_invalid")
    if channel not in VALID_CHANNELS:
        raise PlatformUpdateError("update_channel_invalid")
    SemVer.parse(version)
    if action == "apply":
        validate_candidate(candidate_path, version, manifest_sha256)
    elif candidate_path:
        raise PlatformUpdateError("update_intent_invalid")
    intent = _intent_file()
    parent = intent.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = parent.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PlatformUpdateError("update_intent_path_invalid")
    os.chmod(parent, 0o700)
    lock_path = parent / ".intent.lock"
    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, lock_flags, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        processing = intent.with_name("intent.processing.json")
        if (
            intent.exists()
            or intent.is_symlink()
            or processing.exists()
            or processing.is_symlink()
        ):
            raise PlatformUpdateError("update_intent_pending")
        payload = {
            "schema": 1,
            "action": action,
            "version": str(version),
            "channel": channel,
            "candidate_path": str(candidate_path) if action == "apply" else "",
            "manifest_sha256": str(manifest_sha256) if action == "apply" else "",
            "requested_by": int(requested_by),
            "requested_at": __import__("time").time(),
            "nonce": secrets.token_hex(16),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = parent / f".{intent.name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                descriptor = -1
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, intent)
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
    finally:
        os.close(lock_fd)
    return {"queued": True, "action": action, "version": str(version)}


def write_update_intent(
    action: str,
    version: str,
    *,
    channel: str,
    requested_by: int,
    candidate_path: str = "",
    manifest_sha256: str = "",
) -> dict:
    """Write one private updater intent while keeping filesystem errors stable."""
    try:
        return _write_update_intent_unwrapped(
            action,
            version,
            channel=channel,
            requested_by=requested_by,
            candidate_path=candidate_path,
            manifest_sha256=manifest_sha256,
        )
    except PlatformUpdateError:
        raise
    except OSError as exc:
        raise PlatformUpdateError("update_intent_write_failed") from exc
