"""Signed, server-pinned GitHub Release download and update intent handling.

The API may inspect and stage a signed code release, but it never switches the
running tree or invokes systemd.  Only the independent updater consumes the
0600 intent written by this module.
"""

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
import stat
import subprocess
import tarfile
import tempfile
import time
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import unquote, urlsplit

import requests
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from version import VERSION, RELEASE_CHANNEL, deployment_kind
from update_maintenance import (
    read_trusted_json,
    status_directory,
    supports_maintenance_protocol,
    UpdateStateError,
)
from standalone_runtime import (
    MANAGER_PROTOCOL,
    STANDALONE_RELEASE_KIND,
    StandaloneRuntimeError,
    load_runtime_metadata,
    normalize_architecture as _standalone_architecture,
    release_kind,
    standalone_asset_names,
    target_name as standalone_target_name,
    validate_standalone_manifest,
)


RELEASE_OWNER = "tswawa"
RELEASE_REPOSITORY = "xianyu-saas"
GITHUB_API_HOST = "api.github.com"
GITHUB_API_ROOT = f"https://{GITHUB_API_HOST}/repos/{RELEASE_OWNER}/{RELEASE_REPOSITORY}"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_ASSET_CDN_HOSTS = frozenset({
    "release-assets.githubusercontent.com", "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
})
MAX_ASSET_REDIRECTS = 3
OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
UPDATE_STAGES = frozenset({
    "staged", "queued", "verifying_package", "building", "preflighting", "preparing",
    "stopping", "backing_up", "migrating", "switching", "verifying", "succeeded",
    "rolling_back", "rolled_back", "failed", "recovery_failed",
})
RELEASE_ASSET_PREFIX = "xianyu-saas"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_RELEASE_METADATA_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_STANDALONE_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_SIGNATURE_BYTES = 4096
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_STANDALONE_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
MAX_STANDALONE_UNPACKED_BYTES = 1536 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_STANDALONE_FILE_BYTES = 256 * 1024 * 1024
MAX_MAINTENANCE_SOURCE_BYTES = 256 * 1024
MAX_UPDATER_BUNDLE_FILE_BYTES = 4 * 1024 * 1024
MAX_MANAGER_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5000
MAX_STANDALONE_ARCHIVE_MEMBERS = 20000
MAX_RELEASE_NOTES_CHARS = 16_000
MAX_PATH_LENGTH = 500
MAX_PATH_COMPONENT = 240
VALID_CHANNELS = frozenset({"stable", "beta", RELEASE_CHANNEL})
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_TOP_LEVEL_DIRS = frozenset(
    {"backend", "frontend", "worker", "scripts", "deploy", "config", "docs", "tests", "runtime", "manager"}
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
MAINTENANCE_MODULE_PATH = "backend/update_maintenance.py"
SYSTEMD_INITIALIZATION_FILE = "initialization.json"
SYSTEMD_MANAGER_RELEASES = Path("/opt/xianyu-saas/manager/releases")
SYSTEMD_MANAGER_CURRENT = Path("/opt/xianyu-saas/manager/current")
SYSTEMD_MANAGER_EXECUTABLE = SYSTEMD_MANAGER_CURRENT / "xianyu-saas"
SYSTEMD_MANAGER_KEYS = frozenset({
    "schema", "protocol", "public_key_sha256", "manager_version",
    "manager_sha256", "platform", "architecture", "initialized_at",
})
LEGACY_SYSTEMD_INITIALIZATION_KEYS = frozenset({
    "schema", "protocol", "public_key_sha256", "bundle_sha256",
    "entrypoint_sha256", "initialized_at",
})
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
        text = str(value or "").strip()
        match = SEMVER_RE.fullmatch(text) if len(text) <= 200 else None
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
    runtime_manifest: ReleaseAsset | None = None
    kind: str = "source"
    target: str = ""


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
    try:
        parsed = urlsplit(str(url or ""))
        port = parsed.port
    except (ValueError, TypeError) as exc:
        raise PlatformUpdateError("update_source_rejected") from exc
    if (
        any(ord(c) < 32 or ord(c) == 127 for c in str(url)) or "\\" in str(url)
        or parsed.scheme != "https"
        or parsed.hostname != GITHUB_API_HOST
        or port not in {None, 443}
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
    allowed_query = parsed.query == "per_page=30" or re.fullmatch(r"per_page=100&page=[1-9][0-9]*", parsed.query)
    if parsed.path != expected or not allowed_query:
        raise PlatformUpdateError("update_source_rejected")


def _status_error(status_code: int, response=None) -> PlatformUpdateError:
    headers = {str(k).lower(): str(v) for k, v in getattr(response, "headers", {}).items()}
    limited = status_code == 429 or (status_code == 403 and (
        headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers))
    code = "update_source_rate_limited" if limited else (
        "update_source_auth_failed" if status_code in {401, 403} else
        "update_release_not_found" if status_code == 404 else "update_source_failed")
    error = PlatformUpdateError(code)
    delays = []
    for field in ("retry-after", "x-ratelimit-reset"):
        value = headers.get(field, "")
        if not value:
            continue
        try:
            delay = float(value) - (time.time() if field == "x-ratelimit-reset" else 0)
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(delay) and delay > 0:
            delays.append(delay)
    if delays:
        error.retry_after = min(max(delays), 86400)
    return error


def _validate_asset_redirect(url: str) -> None:
    try:
        parsed = urlsplit(url)
        path = unquote(parsed.path)
        if (len(url) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in url)
                or "\\" in url or parsed.scheme != "https"
                or parsed.hostname not in GITHUB_ASSET_CDN_HOSTS
                or parsed.port not in {None, 443} or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or any(p in {".", "..", ""} for p in path.split("/")[1:])
                or not re.fullmatch(r"/github-production-release-asset(?:-[0-9a-f]+)?/[1-9][0-9]*/[A-Za-z0-9_.-]+", path)
                or "%" in parsed.path or "\\" in path):
            raise ValueError("untrusted CDN")
    except (ValueError, TypeError) as exc:
        raise PlatformUpdateError("update_redirect_rejected") from exc


def _open_release_response(session, url: str, *, asset: bool = False, timeout=(5, 60)):
    """Follow only bounded asset-CDN hops; never reuse ambient credentials."""
    _validate_fixed_api_url(url, asset=asset)
    headers = _github_headers(binary=asset)
    for hop in range(MAX_ASSET_REDIRECTS + 1):
        try:
            if isinstance(session, requests.Session):
                prepared = session.prepare_request(requests.Request("GET", url, headers=headers))
                # Session headers, cookies, auth and .netrc must not restore
                # credentials removed at a cross-origin redirect.
                for name in ("Authorization", "Cookie", "Proxy-Authorization"):
                    prepared.headers.pop(name, None)
                if "Authorization" in headers:
                    prepared.headers["Authorization"] = headers["Authorization"]
                # Session.send also prepares Response.next when redirects are
                # disabled, which can eagerly consume an unbounded redirect body.
                # Use its transport adapter without cookie/redirect processing.
                proxies = requests.utils.get_environ_proxies(url) if session.trust_env else {}
                proxies.update(session.proxies)
                response = session.get_adapter(url).send(prepared, timeout=timeout,
                                                        stream=True, verify=True, cert=None, proxies=proxies)
            else:
                response = session.get(url, headers=dict(headers), timeout=timeout,
                                       allow_redirects=False, stream=True, verify=True)
        except requests.RequestException as exc:
            raise PlatformUpdateError("update_source_failed") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if not 300 <= status < 400:
            return response
        try:
            if not asset or hop >= MAX_ASSET_REDIRECTS or status not in {301, 302, 303, 307, 308}:
                raise PlatformUpdateError("update_redirect_rejected")
            location = next((str(v) for k, v in response.headers.items() if k.lower() == "location"), "")
            _validate_asset_redirect(location)
            # Once leaving the pinned API host, credentials never return.
            headers = {k: v for k, v in headers.items()
                       if k.lower() not in {"authorization", "cookie", "proxy-authorization"}}
            url = location
        finally:
            response.close()
    raise PlatformUpdateError("update_redirect_rejected")


def _response_bytes(response, max_bytes: int) -> bytes:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if 300 <= status_code < 400:
        raise PlatformUpdateError("update_redirect_rejected")
    if status_code != 200:
        raise _status_error(status_code, response)
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
    response = _open_release_response(session, url, asset=asset)
    try:
        return _response_bytes(response, max_bytes)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _download_asset_to_file(
    session,
    asset: ReleaseAsset,
    destination: Path,
    *,
    max_bytes: int = MAX_ARCHIVE_BYTES,
) -> str:
    _validate_fixed_api_url(asset.api_url, asset=True)
    if asset.size <= 0 or asset.size > max_bytes:
        raise PlatformUpdateError("update_download_too_large")
    response = _open_release_response(session, asset.api_url, asset=True, timeout=(5, 120))
    status_code = int(getattr(response, "status_code", 0) or 0)
    try:
        if 300 <= status_code < 400:
            raise PlatformUpdateError("update_redirect_rejected")
        if status_code != 200:
            raise _status_error(status_code, response)
        raw_length = getattr(response, "headers", {}).get("content-length")
        if raw_length is not None:
            try:
                length = int(raw_length)
            except (TypeError, ValueError) as exc:
                raise PlatformUpdateError("update_source_invalid") from exc
            if length > asset.size or length > max_bytes:
                raise PlatformUpdateError("update_download_too_large")
            if length < 0:
                raise PlatformUpdateError("update_source_invalid")
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
                    if total > max_bytes or total > asset.size:
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


def _standalone_target() -> str:
    try:
        return standalone_target_name()
    except StandaloneRuntimeError as exc:
        raise PlatformUpdateError(exc.code) from exc


def _standalone_asset_names(version: str) -> tuple[str, str, str]:
    try:
        return standalone_asset_names(version)
    except StandaloneRuntimeError as exc:
        raise PlatformUpdateError(exc.code) from exc


def _systemd_release_kind() -> str:
    return release_kind() if deployment_kind() == "systemd" else "source"


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


def _parse_release(raw, channel: str, *, deployment: str = "systemd") -> ReleaseInfo | None:
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
    kind = "source"
    target = ""
    if deployment == "docker":
        from docker_update_protocol import docker_asset_names
        names = docker_asset_names(version)
    elif deployment == "systemd" and _systemd_release_kind() == STANDALONE_RELEASE_KIND:
        kind = STANDALONE_RELEASE_KIND
        target = _standalone_target()
        names = _standalone_asset_names(version)
    else:
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
    archive_limit = MAX_STANDALONE_ARCHIVE_BYTES if kind == STANDALONE_RELEASE_KIND else MAX_ARCHIVE_BYTES
    manifest_limit = MAX_STANDALONE_MANIFEST_BYTES if kind == STANDALONE_RELEASE_KIND else MAX_MANIFEST_BYTES
    artifact = _parse_asset(by_name[names[0]], names[0], archive_limit)
    manifest = _parse_asset(by_name[names[1]], names[1], manifest_limit)
    signature = _parse_asset(by_name[names[2]], names[2], MAX_SIGNATURE_BYTES)
    runtime_manifest = None
    if deployment == "docker":
        runtime_name = _asset_names(version)[1]
        if runtime_name not in by_name:
            raise PlatformUpdateError("release_assets_missing")
        runtime_manifest = _parse_asset(by_name[runtime_name], runtime_name, MAX_MANIFEST_BYTES)
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
        runtime_manifest=runtime_manifest,
        kind=kind,
        target=target,
    )


def inspect_releases(channel: str, current_version: str, session=None, *, deployment: str = "systemd") -> tuple[dict, ReleaseInfo | None]:
    """Separate an empty channel from an up-to-date build and incomplete releases."""
    if channel not in VALID_CHANNELS:
        raise PlatformUpdateError("update_channel_invalid")
    current = SemVer.parse(current_version)
    session = session or requests.Session()
    raw = _request_bytes(session, f"{GITHUB_API_ROOT}/releases?per_page=30", max_bytes=MAX_RELEASE_METADATA_BYTES)
    try:
        releases = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlatformUpdateError("update_source_invalid") from exc
    if not isinstance(releases, list):
        raise PlatformUpdateError("update_source_invalid")
    winner = None
    winner_version = None
    # Compare metadata first: old or wrong-channel assets cannot break a check.
    for item in releases:
        if not isinstance(item, dict) or item.get("draft") is True:
            continue
        try:
            version = SemVer.parse(_version_from_tag(item.get("tag_name", "")))
        except PlatformUpdateError:
            continue
        if channel == "stable" and (item.get("prerelease") or version.prerelease):
            continue
        if winner_version is None or version.compare(winner_version) > 0:
            winner_version, winner = version, item
    payload = {"channel": channel, "current_version": current_version, "available": False,
               "status": "no_release", "version": "", "release_notes": "", "error_code": ""}
    if winner is None:
        return payload, None
    payload.update(version=_version_from_tag(winner["tag_name"]),
                   published_at=str(winner.get("published_at", ""))[:80])
    if winner_version.compare(current) <= 0:
        payload["status"] = "current"
        return payload, None
    payload.update(available=True, release_notes=str(winner.get("body", ""))[:MAX_RELEASE_NOTES_CHARS])
    try:
        release = _parse_release(winner, channel, deployment=deployment)
    except PlatformUpdateError as exc:
        payload.update(status="incomplete", error_code=exc.code)
        return payload, None
    payload["status"] = "available"
    return payload, release


def inspect_public_releases(current_version: str, session=None) -> dict:
    """One published-version feed, independent of signed installer artifacts."""
    current = SemVer.parse(current_version)
    session = session or requests.Session()
    winner = winner_version = None
    page = 1
    seen_pages = set()
    total_bytes = 0
    while True:
        raw = _request_bytes(session, f"{GITHUB_API_ROOT}/releases?per_page=100&page={page}",
                             max_bytes=MAX_RELEASE_METADATA_BYTES)
        total_bytes += len(raw)
        fingerprint = hashlib.sha256(raw).hexdigest()
        if total_bytes > MAX_RELEASE_METADATA_BYTES or fingerprint in seen_pages:
            raise PlatformUpdateError("update_source_invalid")
        seen_pages.add(fingerprint)
        try:
            releases = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PlatformUpdateError("update_source_invalid") from exc
        if not isinstance(releases, list):
            raise PlatformUpdateError("update_source_invalid")
        for item in releases:
            if not isinstance(item, dict) or item.get("draft") is True:
                continue
            try:
                parsed = SemVer.parse(_version_from_tag(item.get("tag_name", "")))
            except (PlatformUpdateError, TypeError, AttributeError):
                continue
            if winner_version is None or parsed.compare(winner_version) > 0:
                winner, winner_version = item, parsed
        if len(releases) < 100:
            break
        page += 1
    payload = {"channel": RELEASE_CHANNEL, "current_version": current_version,
               "available": False, "status": "no_release", "version": "",
               "release_notes": "", "error_code": ""}
    if winner is None:
        return payload
    available = winner_version.compare(current) > 0
    payload.update(version=_version_from_tag(winner["tag_name"]), available=available,
                   status="available" if available else "current",
                   published_at=str(winner.get("published_at") or "")[:80],
                   release_notes=str(winner.get("body") or "")[:MAX_RELEASE_NOTES_CHARS])
    payload["installer_assets"] = {}
    for deployment in ("docker", "systemd"):
        try:
            payload["installer_assets"][deployment] = _parse_release(winner, RELEASE_CHANNEL, deployment=deployment) is not None
        except PlatformUpdateError:
            payload["installer_assets"][deployment] = False
    return payload


def fetch_release(channel: str, current_version: str, session=None, *, deployment: str = "systemd") -> ReleaseInfo | None:
    payload, release = inspect_releases(channel, current_version, session=session, deployment=deployment)
    if payload["status"] == "incomplete":
        raise PlatformUpdateError(payload["error_code"])
    return release


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
    if not raw and deployment_kind() == "docker":
        raw = "/app/update-signing.pub"
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
        protected = not metadata.st_mode & 0o022
        # Docker Desktop Windows binds report 0777 even on read-only mounts.
        # Accept that only for root-owned keys on a kernel-enforced read-only mount.
        if not protected and metadata.st_uid == 0 and deployment_kind() == "docker" and hasattr(os, "statvfs"):
            protected = bool(os.statvfs(path).f_flag & os.ST_RDONLY)
        if (
            metadata.st_uid not in {0, getattr(os, "geteuid", lambda: 0)()}
            or not protected
            or metadata.st_size <= 0
            or metadata.st_size > 4096
        ):
            raise PlatformUpdateError("update_public_key_invalid")
        raw = _read_secure_file(path, 4096).strip()
    except (OSError, PlatformUpdateError) as exc:
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


def _systemd_properties(unit: str) -> dict[str, str]:
    """Read only two fixed units; no shell, service control or inherited secrets."""
    if unit not in {"xianyu-saas-updater.path", "xianyu-saas-updater.service"}:
        raise PlatformUpdateError("update_service_unavailable")
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "--no-pager", "--no-ask-password", "show", unit,
             "--property=LoadState,ActiveState,SubState,Paths,Triggers,ExecStart"],
            capture_output=True, text=True, timeout=2,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "SYSTEMD_PAGER": "cat"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PlatformUpdateError("update_service_unavailable") from exc
    if result.returncode != 0:
        raise PlatformUpdateError("update_service_unavailable")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def _trusted_update_directory(path: Path, *, writable: bool = False) -> None:
    if not path.is_absolute():
        raise PlatformUpdateError("update_installation_unavailable")
    for parent in (path, *path.parents):
        metadata = parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PlatformUpdateError("update_installation_unavailable")
        allowed_owner = {0, os.geteuid()} if writable else {0}
        sticky_root = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        if metadata.st_uid not in allowed_owner or (metadata.st_mode & 0o022 and not sticky_root):
            raise PlatformUpdateError("update_installation_unavailable")
    if writable and not os.access(path, os.W_OK | os.X_OK):
        raise PlatformUpdateError("update_installation_unavailable")


def _systemd_updater_bundle_root() -> Path:
    path = Path(os.environ.get("SAAS_UPDATER_BUNDLE_ROOT", "/opt/xianyu-saas/updater").strip())
    if not path.is_absolute() or ".." in path.parts:
        raise PlatformUpdateError("update_updater_identity_mismatch")
    return path


def _systemd_updater_entrypoint(bundle_root: Path | None = None) -> Path:
    bundle_root = bundle_root or _systemd_updater_bundle_root()
    expected = bundle_root.joinpath(*PurePosixPath(SYSTEMD_UPDATER_ENTRYPOINT_RELATIVE).parts)
    path = Path(os.environ.get("SAAS_UPDATER_ENTRYPOINT", str(expected)).strip())
    if not path.is_absolute() or ".." in path.parts or path != expected:
        raise PlatformUpdateError("update_updater_identity_mismatch")
    return path


def _read_systemd_bundle_file(bundle_root: Path, relative: str) -> bytes:
    if relative not in SYSTEMD_UPDATER_BUNDLE_FILES:
        raise PlatformUpdateError("update_updater_identity_mismatch")
    path = bundle_root.joinpath(*PurePosixPath(relative).parts)
    try:
        _trusted_update_directory(path.parent)
        before = path.lstat()
        if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1 or before.st_uid != 0
                or stat.S_IMODE(before.st_mode) & 0o022
                or before.st_size <= 0 or before.st_size > MAX_UPDATER_BUNDLE_FILE_BYTES):
            raise PlatformUpdateError("update_updater_identity_mismatch")
        payload = _read_secure_file(path, MAX_UPDATER_BUNDLE_FILE_BYTES)
        after = path.lstat()
        if ((before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_nlink,
             before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_nlink,
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise PlatformUpdateError("update_updater_identity_mismatch")
        return payload
    except PlatformUpdateError as exc:
        if exc.code == "update_updater_identity_mismatch":
            raise
        raise PlatformUpdateError("update_updater_identity_mismatch") from exc
    except OSError as exc:
        raise PlatformUpdateError("update_updater_identity_mismatch") from exc


def _systemd_updater_identity() -> tuple[str, str]:
    bundle_root = _systemd_updater_bundle_root()
    _trusted_update_directory(bundle_root)
    entrypoint = _systemd_updater_entrypoint(bundle_root)
    canonical = bytearray()
    entrypoint_sha256 = ""
    for relative in SYSTEMD_UPDATER_BUNDLE_FILES:
        payload = _read_systemd_bundle_file(bundle_root, relative)
        digest = hashlib.sha256(payload).hexdigest()
        canonical.extend(relative.encode("utf-8"))
        canonical.extend(b"\0")
        canonical.extend(str(len(payload)).encode("ascii"))
        canonical.extend(b"\0")
        canonical.extend(digest.encode("ascii"))
        canonical.extend(b"\n")
        if bundle_root.joinpath(*PurePosixPath(relative).parts) == entrypoint:
            entrypoint_sha256 = digest
    if not entrypoint_sha256:
        raise PlatformUpdateError("update_updater_identity_mismatch")
    return hashlib.sha256(canonical).hexdigest(), entrypoint_sha256


def _systemd_manager_identity() -> tuple[str, str, str]:
    releases = Path(os.environ.get("SAAS_MANAGER_RELEASES_DIR", str(SYSTEMD_MANAGER_RELEASES)).strip())
    current = Path(os.environ.get("SAAS_MANAGER_CURRENT", str(SYSTEMD_MANAGER_CURRENT)).strip())
    executable = Path(os.environ.get("SAAS_MANAGER_EXECUTABLE", str(current / "xianyu-saas")).strip())
    if (not releases.is_absolute() or not current.is_absolute() or not executable.is_absolute()
            or ".." in releases.parts or ".." in current.parts or ".." in executable.parts
            or current.parent != releases.parent or executable != current / "xianyu-saas"):
        raise PlatformUpdateError("update_updater_identity_mismatch")
    try:
        _trusted_update_directory(releases)
        _trusted_update_directory(current.parent)
        link = current.lstat()
        if not stat.S_ISLNK(link.st_mode) or link.st_uid != 0:
            raise PlatformUpdateError("update_updater_identity_mismatch")
        release_root = current.resolve(strict=True)
        releases_root = releases.resolve(strict=True)
        if release_root.parent != releases_root:
            raise PlatformUpdateError("update_updater_identity_mismatch")
        SemVer.parse(release_root.name)
        _trusted_update_directory(release_root)
        resolved_executable = executable.resolve(strict=True)
        if resolved_executable != release_root / "xianyu-saas":
            raise PlatformUpdateError("update_updater_identity_mismatch")
        before = resolved_executable.lstat()
        if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
                or before.st_uid != 0 or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) & 0o022
                or not stat.S_IMODE(before.st_mode) & 0o111
                or before.st_size <= 0 or before.st_size > MAX_MANAGER_BYTES):
            raise PlatformUpdateError("update_updater_identity_mismatch")
        payload = _read_secure_file(resolved_executable, MAX_MANAGER_BYTES)
        after = resolved_executable.lstat()
        if ((before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_nlink,
             before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_nlink,
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise PlatformUpdateError("update_updater_identity_mismatch")
        return release_root.name, hashlib.sha256(payload).hexdigest(), _standalone_architecture()
    except PlatformUpdateError:
        raise
    except (OSError, ValueError) as exc:
        raise PlatformUpdateError("update_updater_identity_mismatch") from exc


def read_systemd_initialization() -> dict:
    try:
        payload = read_trusted_json(status_directory() / SYSTEMD_INITIALIZATION_FILE)
    except UpdateStateError as exc:
        raise PlatformUpdateError("update_state_invalid") from exc
    if payload is None:
        raise PlatformUpdateError("update_updater_not_initialized")
    if isinstance(payload, dict) and set(payload) == LEGACY_SYSTEMD_INITIALIZATION_KEYS:
        raise PlatformUpdateError("update_installation_migration_required")
    if not isinstance(payload, dict) or set(payload) != SYSTEMD_MANAGER_KEYS:
        raise PlatformUpdateError("update_state_invalid")
    if type(payload.get("schema")) is not int or payload["schema"] != 2:
        raise PlatformUpdateError("update_state_invalid")
    if type(payload.get("protocol")) is not int or payload["protocol"] != MANAGER_PROTOCOL:
        raise PlatformUpdateError("update_protocol_mismatch")
    stamp = payload.get("initialized_at")
    if (type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp <= 0
            or payload.get("platform") != "linux"
            or payload.get("architecture") != _standalone_architecture()
            or any(not isinstance(payload.get(name), str) or not SHA256_RE.fullmatch(payload[name])
                   for name in ("public_key_sha256", "manager_sha256"))):
        raise PlatformUpdateError("update_state_invalid")
    key = load_public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if not secrets.compare_digest(payload["public_key_sha256"], hashlib.sha256(key).hexdigest()):
        raise PlatformUpdateError("update_public_key_mismatch")
    manager_version, manager_sha256, architecture = _systemd_manager_identity()
    if (payload.get("manager_version") != manager_version
            or payload.get("architecture") != architecture
            or not secrets.compare_digest(payload["manager_sha256"], manager_sha256)):
        raise PlatformUpdateError("update_updater_identity_mismatch")
    return {**payload, "initialized_at": float(stamp)}


def _systemd_exec_start_matches(value: str, entrypoint: Path = SYSTEMD_MANAGER_EXECUTABLE) -> bool:
    text = str(value or "")
    if text.count("argv[]=") != 1 or text.count("path=") != 1:
        return False
    fields = {}
    for raw in text.replace("{", ";").replace("}", ";").split(";"):
        item = raw.strip()
        if "=" not in item:
            continue
        key, field = item.split("=", 1)
        if key in {"path", "argv[]"}:
            if key in fields:
                return False
            fields[key] = field.strip()
    return (
        fields.get("path") == str(entrypoint)
        and fields.get("argv[]", "").split() == [str(entrypoint), "internal", "consume-intent"]
    )


def _systemd_update_ready() -> None:
    if _systemd_release_kind() != STANDALONE_RELEASE_KIND:
        raise PlatformUpdateError("update_installation_migration_required")
    current = Path(os.environ.get("SAAS_CURRENT_LINK", "/opt/xianyu-saas/current"))
    releases = Path(os.environ.get("SAAS_RELEASES_DIR", "/opt/xianyu-saas/releases"))
    _trusted_update_directory(releases)
    _trusted_update_directory(current.parent)
    if not current.is_symlink() or current.lstat().st_uid != 0:
        raise PlatformUpdateError("update_installation_unavailable")
    source = current.resolve(strict=True)
    if source != releases / VERSION or source != PROJECT_ROOT.resolve() or source != _source_root():
        raise PlatformUpdateError("update_installation_unavailable")
    _trusted_update_directory(source)
    load_public_key()
    if _public_key_file().lstat().st_uid != 0:
        raise PlatformUpdateError("update_public_key_invalid")
    read_systemd_initialization()
    marker = _marker_payload(source)
    if (marker.get("schema") != 1 or marker.get("version") != VERSION
            or marker.get("kind") != STANDALONE_RELEASE_KIND
            or marker.get("target") != _standalone_target()):
        raise PlatformUpdateError("update_installation_unavailable")
    manifest = _read_secure_file(source / CACHED_MANIFEST_FILE, MAX_STANDALONE_MANIFEST_BYTES)
    verify_manifest_signature(manifest, _read_secure_file(source / CACHED_SIGNATURE_FILE, MAX_SIGNATURE_BYTES))
    parsed, files = parse_manifest(manifest, _release_from_marker(marker))
    _require_maintenance_protocol(source, files)
    _verify_candidate_version(source, VERSION)
    try:
        runtime = load_runtime_metadata(
            source,
            expected_version=VERSION,
            expected_architecture=str(parsed.get("architecture", "")),
        )
    except StandaloneRuntimeError as exc:
        raise PlatformUpdateError(exc.code) from exc
    if runtime.manager_protocol != MANAGER_PROTOCOL:
        raise PlatformUpdateError("update_protocol_mismatch")
    _trusted_update_directory(_staging_root(), writable=True)
    _trusted_update_directory(_intent_file().parent, writable=True)
    watcher = _systemd_properties("xianyu-saas-updater.path")
    service = _systemd_properties("xianyu-saas-updater.service")
    if (watcher.get("LoadState") != "loaded" or watcher.get("ActiveState") != "active"
            or service.get("LoadState") != "loaded" or service.get("ActiveState") == "failed"
            or "xianyu-saas-updater.service" not in watcher.get("Triggers", "").split()
            or not re.search(r"(?:^|\s)" + re.escape(f"{_intent_file()} (PathExists)") + r"(?:$|\s)", watcher.get("Paths", ""))):
        raise PlatformUpdateError("update_service_unavailable")
    manager_entrypoint = Path(
        os.environ.get("SAAS_MANAGER_EXECUTABLE", str(SYSTEMD_MANAGER_EXECUTABLE)).strip()
    )
    if not _systemd_exec_start_matches(service.get("ExecStart", ""), manager_entrypoint):
        raise PlatformUpdateError("update_updater_identity_mismatch")


def docker_update_root() -> Path:
    root = Path(os.environ.get("SAAS_DOCKER_UPDATE_ROOT", "/updates"))
    if not root.is_absolute() or ".." in root.parts:
        raise PlatformUpdateError("update_installation_unavailable")
    return root


def _safe_error_code(value, default="update_state_invalid") -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) else default


def _rollback_candidates(raw, current_version: str) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > 20:
        raise PlatformUpdateError("update_state_invalid")
    result = []
    seen = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise PlatformUpdateError("update_state_invalid")
        version, digest = entry.get("version"), entry.get("manifest_sha256")
        if (not isinstance(version, str) or not isinstance(digest, str)
                or not SHA256_RE.fullmatch(digest) or version in seen
                or SemVer.parse(version).compare(SemVer.parse(current_version)) >= 0):
            raise PlatformUpdateError("update_state_invalid")
        result.append({"version": version, "manifest_sha256": digest})
        seen.add(version)
    return result


def read_docker_capabilities() -> dict:
    try:
        payload = read_trusted_json(docker_update_root() / "status" / "capabilities.json")
        if payload is None:
            raise PlatformUpdateError("update_service_unavailable")
        if (type(payload.get("schema")) is not int or payload["schema"] != 1
                or type(payload.get("protocol")) is not int or payload["protocol"] != 1):
            raise PlatformUpdateError("update_protocol_mismatch")
        stamp = payload.get("heartbeat_at")
        if (type(stamp) not in {int, float} or not math.isfinite(stamp)
                or stamp <= 0 or time.time() - stamp > 45 or stamp - time.time() > 5):
            raise PlatformUpdateError("update_service_stale")
        deployment = os.environ.get("SAAS_DOCKER_DEPLOYMENT_ID", "").strip()
        if not deployment or payload.get("deployment_id") != deployment:
            raise PlatformUpdateError("update_deployment_mismatch")
        if payload.get("current_version") != VERSION:
            raise PlatformUpdateError("update_current_version_mismatch")
        key_file = _public_key_file()
        _trusted_update_directory(key_file.parent)
        if key_file.lstat().st_uid != 0:
            raise PlatformUpdateError("update_public_key_invalid")
        key_raw = load_public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if payload.get("public_key_sha256") != hashlib.sha256(key_raw).hexdigest():
            raise PlatformUpdateError("update_public_key_mismatch")
        if payload.get("ready") is not True:
            raise PlatformUpdateError(_safe_error_code(payload.get("reason"), "update_service_unavailable"))
        for name in ("artifacts", "requests"):
            _trusted_update_directory(docker_update_root() / name, writable=True)
        return {"schema": 1, "protocol": 1, "ready": True, "reason": "",
                "heartbeat_at": float(stamp), "current_version": VERSION,
                "rollback_versions": _rollback_candidates(payload.get("rollback_versions", []), VERSION)}
    except UpdateStateError as exc:
        raise PlatformUpdateError(exc.code) from exc
    except OSError as exc:
        raise PlatformUpdateError("update_installation_unavailable") from exc


def read_operation_status(operation: dict) -> dict | None:
    """Merge only executor-owned, identity-matched, safe progress fields."""
    operation_id = str(operation.get("operation_id") or "")
    if not OPERATION_ID_RE.fullmatch(operation_id):
        raise PlatformUpdateError("update_operation_invalid")
    root = docker_update_root() / "status" if operation.get("deployment") == "docker" else status_directory()
    try:
        payload = read_trusted_json(root / "operations" / f"{operation_id}.json")
    except UpdateStateError as exc:
        raise PlatformUpdateError(exc.code) from exc
    if payload is None:
        return None
    stamp = payload.get("updated_at")
    if (type(payload.get("schema")) is not int or payload["schema"] != 1
            or payload.get("operation_id") != operation_id
            or payload.get("action") != operation.get("action")
            or payload.get("version") != operation.get("version")
            or payload.get("status") not in UPDATE_STAGES | {"running"}
            or payload.get("phase") not in UPDATE_STAGES
            or ((payload.get("status") in {"succeeded", "rolled_back", "failed", "recovery_failed"}
                 or payload.get("phase") in {"succeeded", "rolled_back", "failed", "recovery_failed"})
                and payload.get("status") != payload.get("phase"))
            or type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp <= 0
            or stamp > time.time() + 5):
        raise PlatformUpdateError("update_state_invalid")
    current = payload.get("current_version")
    if not isinstance(current, str):
        raise PlatformUpdateError("update_state_invalid")
    SemVer.parse(current)
    return {"schema": 1, "operation_id": operation_id, "action": operation["action"],
            "version": operation["version"], "current_version": current,
            "status": payload["status"], "phase": payload["phase"], "updated_at": float(stamp),
            "error_code": _safe_error_code(payload.get("error_code"), "")}


def update_capabilities() -> dict:
    mode = deployment_kind()
    instructions = {
        "docker": "Docker 网页更新需显式接入独立更新器、可信签名公钥和本项目共享目录；普通容器不会获得宿主控制权限。升级仅回退代码或镜像，不恢复旧业务数据。",
        "source": "这是源码部署。请维护者备份数据、取得目标源码并按原部署方式更新和重启；当前未配置网页安装服务。",
        "systemd": "签名版本部署需配置可信发布目录、签名公钥以及运行中的独立更新服务。应用前请备份数据；页面会显示请求及执行状态。",
    }
    result = {"deployment": mode, "check": True, "download": False, "apply": False,
              "rollback": False, "reason": "update_installation_unsupported", "instruction": instructions[mode]}
    if mode not in {"systemd", "docker"}:
        return result
    try:
        if mode == "docker":
            read_docker_capabilities()
        else:
            _systemd_update_ready()
    except PlatformUpdateError as exc:
        result["reason"] = exc.code
    except (OSError, ValueError, RuntimeError):
        result["reason"] = "update_installation_unavailable"
    else:
        result.update(download=True, apply=True, rollback=True, reason="")
    return result


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


def _manifest_files(
    raw_files,
    *,
    max_members: int = MAX_ARCHIVE_MEMBERS,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_unpacked_bytes: int = MAX_UNPACKED_BYTES,
) -> dict[str, ManifestFile]:
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > max_members:
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
        if size < 0 or size > max_file_bytes or not SHA256_RE.fullmatch(sha256):
            raise PlatformUpdateError("update_manifest_invalid")
        executable = bool(raw.get("executable", False))
        total += size
        if total > max_unpacked_bytes:
            raise PlatformUpdateError("update_archive_too_large")
        files[path] = ManifestFile(path, size, sha256, executable)
        folded.add(folded_path)
    return files


def parse_manifest(manifest_raw: bytes, release: ReleaseInfo) -> tuple[dict, dict[str, ManifestFile]]:
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlatformUpdateError("update_manifest_invalid") from exc
    expected_schema = 2 if release.kind == STANDALONE_RELEASE_KIND else 1
    if not isinstance(manifest, dict) or manifest.get("schema") != expected_schema:
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
    if release.kind == STANDALONE_RELEASE_KIND:
        try:
            validate_standalone_manifest(
                manifest,
                expected_version=release.version,
                expected_target=release.target,
                expected_artifact=release.artifact.name,
            )
        except StandaloneRuntimeError as exc:
            raise PlatformUpdateError(exc.code) from exc
        files = _manifest_files(
            manifest.get("files"),
            max_members=MAX_STANDALONE_ARCHIVE_MEMBERS,
            max_file_bytes=MAX_STANDALONE_FILE_BYTES,
            max_unpacked_bytes=MAX_STANDALONE_UNPACKED_BYTES,
        )
    else:
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
                if total > expected.size:
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
    *,
    standalone: bool = False,
) -> None:
    max_members = MAX_STANDALONE_ARCHIVE_MEMBERS if standalone else MAX_ARCHIVE_MEMBERS
    max_unpacked_bytes = MAX_STANDALONE_UNPACKED_BYTES if standalone else MAX_UNPACKED_BYTES
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
            if member_count > max_members:
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
            if total_size > max_unpacked_bytes:
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
    require_maintenance=False,
) -> dict:
    if channel not in VALID_CHANNELS:
        raise PlatformUpdateError("update_channel_invalid")
    if SemVer.parse(release.version).compare(SemVer.parse(current_version)) <= 0:
        raise PlatformUpdateError("update_downgrade_rejected")
    session = session or requests.Session()
    standalone = release.kind == STANDALONE_RELEASE_KIND
    manifest_limit = MAX_STANDALONE_MANIFEST_BYTES if standalone else MAX_MANIFEST_BYTES
    archive_limit = MAX_STANDALONE_ARCHIVE_BYTES if standalone else MAX_ARCHIVE_BYTES
    manifest_raw = _request_bytes(
        session, release.manifest.api_url, max_bytes=manifest_limit, asset=True
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
        artifact_sha256 = _download_asset_to_file(
            session, release.artifact, archive_path, max_bytes=archive_limit
        )
        if not secrets.compare_digest(artifact_sha256, str(manifest["artifact_sha256"])):
            raise PlatformUpdateError("update_artifact_hash_mismatch")
        extract_verified_archive(
            archive_path, candidate_root, expected_files, standalone=standalone
        )
        if require_maintenance:
            _require_maintenance_protocol(candidate_root, expected_files)
        _verify_candidate_version(candidate_root, release.version)
        if standalone:
            try:
                runtime = load_runtime_metadata(
                    candidate_root,
                    expected_version=release.version,
                    expected_architecture=manifest["architecture"],
                )
            except StandaloneRuntimeError as exc:
                raise PlatformUpdateError(exc.code) from exc
            if runtime.manager_protocol != manifest["manager_protocol"]:
                raise PlatformUpdateError("standalone_manager_protocol_invalid")
            manager = candidate_root / "manager" / "xianyu-saas"
            try:
                metadata = manager.lstat()
            except OSError as exc:
                raise PlatformUpdateError("standalone_manager_missing") from exc
            if manager.is_symlink() or not manager.is_file() or not stat.S_IMODE(metadata.st_mode) & 0o111:
                raise PlatformUpdateError("standalone_manager_invalid")
        else:
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
                "kind": release.kind,
                "target": release.target,
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


def _docker_artifact_directory(operation_id: str, *, create: bool = False) -> Path:
    if not OPERATION_ID_RE.fullmatch(str(operation_id)):
        raise PlatformUpdateError("update_operation_invalid")
    parent = docker_update_root() / "artifacts"
    _trusted_update_directory(parent, writable=True)
    path = parent / operation_id
    if create:
        path.mkdir(mode=0o700)
    metadata = path.lstat()
    if (stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise PlatformUpdateError("update_candidate_invalid")
    return path


def _docker_manifest(raw: bytes, signature: bytes, version: str):
    from docker_update_protocol import DockerUpdateError, verify_docker_manifest
    key = load_public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    try:
        return verify_docker_manifest(raw, signature, key, expected_version=version)
    except DockerUpdateError as exc:
        raise PlatformUpdateError(exc.code) from exc


def stage_docker_release(release: ReleaseInfo, channel: str, current_version: str,
                         operation_id: str, *, session=None) -> dict:
    from docker_update_protocol import DockerUpdateError, extract_verified_source
    import shutil
    if channel not in VALID_CHANNELS:
        raise PlatformUpdateError("update_channel_invalid")
    if SemVer.parse(release.version).compare(SemVer.parse(current_version)) <= 0:
        raise PlatformUpdateError("update_downgrade_rejected")
    session = session or requests.Session()
    raw = _request_bytes(session, release.manifest.api_url, max_bytes=MAX_MANIFEST_BYTES, asset=True)
    signature = _request_bytes(session, release.signature.api_url, max_bytes=MAX_SIGNATURE_BYTES, asset=True)
    manifest = _docker_manifest(raw, signature, release.version)
    if release.artifact.name != manifest.source_name or release.artifact.size != manifest.source_size:
        raise PlatformUpdateError("update_manifest_invalid")
    if release.runtime_manifest is None:
        raise PlatformUpdateError("release_assets_missing")
    runtime_raw = _request_bytes(session, release.runtime_manifest.api_url, max_bytes=MAX_MANIFEST_BYTES, asset=True)
    if hashlib.sha256(runtime_raw).hexdigest() != manifest.runtime_manifest_sha256:
        raise PlatformUpdateError("update_manifest_invalid")
    directory = None
    try:
        directory = _docker_artifact_directory(operation_id, create=True)
        digest = _download_asset_to_file(session, release.artifact, directory / "source.zip")
        if not secrets.compare_digest(digest, manifest.source_sha256):
            raise PlatformUpdateError("update_artifact_hash_mismatch")
        source_root = extract_verified_source(directory / "source.zip", directory / ".validation", manifest)
        _require_maintenance_protocol(source_root)
        shutil.rmtree(directory / ".validation")
        _write_secure_file(directory / "docker.manifest.json", raw)
        _write_secure_file(directory / "docker.manifest.sig", signature)
    except BaseException as exc:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        if isinstance(exc, DockerUpdateError):
            code = (
                "update_artifact_hash_mismatch"
                if exc.code in {"docker_source_hash_mismatch", "docker_source_size_mismatch"}
                else exc.code
            )
            raise PlatformUpdateError(code) from exc
        if isinstance(exc, OSError):
            raise PlatformUpdateError("update_staging_failed") from exc
        raise
    return {"version": release.version, "channel": channel, "operation_id": operation_id,
            "deployment": "docker", "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "candidate_path": "", "release_id": release.release_id, "release_notes": release.notes}


def validate_docker_candidate(operation_id: str, version: str, manifest_sha256: str) -> None:
    from docker_update_protocol import DockerUpdateError, extract_verified_source
    import shutil

    validation: Path | None = None
    try:
        directory = _docker_artifact_directory(operation_id)
        raw = _read_secure_file(directory / "docker.manifest.json", MAX_MANIFEST_BYTES)
        signature = _read_secure_file(directory / "docker.manifest.sig", MAX_SIGNATURE_BYTES)
        if not secrets.compare_digest(hashlib.sha256(raw).hexdigest(), manifest_sha256):
            raise PlatformUpdateError("update_candidate_invalid")
        manifest = _docker_manifest(raw, signature, version)
        validation = directory / f".validation-{secrets.token_hex(8)}"
        source_root = extract_verified_source(directory / "source.zip", validation, manifest)
        _require_maintenance_protocol(source_root)
    except DockerUpdateError as exc:
        code = (
            "update_artifact_hash_mismatch"
            if exc.code in {"docker_source_hash_mismatch", "docker_source_size_mismatch"}
            else exc.code
        )
        raise PlatformUpdateError(code) from exc
    except OSError as exc:
        raise PlatformUpdateError("update_candidate_invalid") from exc
    finally:
        if validation is not None:
            shutil.rmtree(validation, ignore_errors=True)


def write_docker_update_request(operation: dict) -> dict:
    operation_id = str(operation.get("operation_id") or "")
    if not OPERATION_ID_RE.fullmatch(operation_id):
        raise PlatformUpdateError("update_operation_invalid")
    action = operation.get("action")
    if (action not in {"apply", "rollback"} or not SHA256_RE.fullmatch(str(operation.get("manifest_sha256", "")))
            or int(operation.get("requested_by", 0)) <= 0):
        raise PlatformUpdateError("update_intent_invalid")
    SemVer.parse(operation["version"])
    SemVer.parse(operation["expected_current_version"])
    parent = docker_update_root() / "requests"
    _trusted_update_directory(parent, writable=True)
    payload = {"schema": 1, "operation_id": operation_id, "action": action,
               "version": operation["version"], "expected_current_version": operation["expected_current_version"],
               "manifest_sha256": operation["manifest_sha256"], "requested_at": operation["requested_at"],
               "requested_by": int(operation["requested_by"])}
    try:
        _publish_request(parent / f"{operation_id}.json", payload)
    except OSError as exc:
        raise PlatformUpdateError("update_intent_write_failed") from exc
    return {"queued": True, "status": "queued", "action": action, "version": operation["version"],
            "operation_id": operation_id}


def _publish_request(path: Path, payload: dict, *, processing: Path | None = None) -> None:
    # No fake lock implementation on Windows: deployment I/O requires Linux.
    if os.name != "posix":
        raise PlatformUpdateError("update_state_platform_unsupported")
    import fcntl
    parent = path.parent
    lock_fd = os.open(parent / ".intent.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    temporary = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        for existing in (path, processing):
            if existing is None:
                continue
            if existing.exists() or existing.is_symlink():
                old = json.loads(_read_secure_file(existing, 16384))
                if payload.get("operation_id") and all(old.get(k) == v for k, v in payload.items()):
                    return
                raise PlatformUpdateError("update_intent_pending")
        _write_secure_file(temporary, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        os.replace(temporary, path)
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except (ValueError, UnicodeError) as exc:
        raise PlatformUpdateError("update_intent_pending") from exc
    finally:
        temporary.unlink(missing_ok=True)
        os.close(lock_fd)


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
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino, opened.st_size) != (metadata.st_dev, metadata.st_ino, metadata.st_size)):
                raise PlatformUpdateError("update_candidate_invalid")
            payload = bytearray()
            while True:
                chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    raise PlatformUpdateError("update_candidate_invalid")
            after = os.fstat(descriptor)
            if (len(payload) != opened.st_size
                    or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise PlatformUpdateError("update_candidate_invalid")
            return bytes(payload)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PlatformUpdateError("update_candidate_invalid") from exc


def _require_maintenance_protocol(candidate_root: Path, files: dict[str, ManifestFile] | None = None) -> None:
    """Require one signed, literal maintenance protocol declaration without importing it."""
    expected = files.get(MAINTENANCE_MODULE_PATH) if files is not None else None
    if files is not None and expected is None:
        raise PlatformUpdateError("update_maintenance_protocol_unsupported")
    if expected is not None and (expected.size <= 0 or expected.size > MAX_MAINTENANCE_SOURCE_BYTES):
        raise PlatformUpdateError("update_maintenance_protocol_unsupported")
    try:
        source = _read_secure_file(
            candidate_root / MAINTENANCE_MODULE_PATH,
            expected.size if expected is not None else MAX_MAINTENANCE_SOURCE_BYTES,
        )
    except PlatformUpdateError as exc:
        raise PlatformUpdateError("update_maintenance_protocol_unsupported") from exc
    if expected is not None and (
        len(source) != expected.size
        or not secrets.compare_digest(hashlib.sha256(source).hexdigest(), expected.sha256)
    ):
        raise PlatformUpdateError("update_candidate_invalid")
    if not supports_maintenance_protocol(source):
        raise PlatformUpdateError("update_maintenance_protocol_unsupported")


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
    kind = str(marker.get("kind", "source"))
    target = str(marker.get("target", ""))
    if kind == STANDALONE_RELEASE_KIND:
        if target != _standalone_target():
            raise PlatformUpdateError("standalone_runtime_architecture_mismatch")
        expected_names = _standalone_asset_names(version)
        archive_limit = MAX_STANDALONE_ARCHIVE_BYTES
    elif kind == "source":
        expected_names = _asset_names(version)
        archive_limit = MAX_ARCHIVE_BYTES
    else:
        raise PlatformUpdateError("update_candidate_invalid")
    if artifact_name != expected_names[0] or artifact_size <= 0 or artifact_size > archive_limit:
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
        kind=kind,
        target=target,
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
    release = _release_from_marker(marker)
    standalone = release.kind == STANDALONE_RELEASE_KIND
    manifest_limit = MAX_STANDALONE_MANIFEST_BYTES if standalone else MAX_MANIFEST_BYTES
    manifest_raw = _read_secure_file(resolved / CACHED_MANIFEST_FILE, manifest_limit)
    signature_raw = _read_secure_file(resolved / CACHED_SIGNATURE_FILE, MAX_SIGNATURE_BYTES)
    if not secrets.compare_digest(
        hashlib.sha256(manifest_raw).hexdigest(), str(marker.get("manifest_sha256", ""))
    ):
        raise PlatformUpdateError("update_candidate_invalid")
    verify_manifest_signature(manifest_raw, signature_raw)
    manifest, expected_files = parse_manifest(manifest_raw, release)
    max_unpacked_bytes = MAX_STANDALONE_UNPACKED_BYTES if standalone else MAX_UNPACKED_BYTES
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
            if total_size > max_unpacked_bytes:
                raise PlatformUpdateError("update_archive_too_large")
            digest = hashlib.sha256(_read_secure_file(child, expected.size)).hexdigest()
            if not secrets.compare_digest(digest, expected.sha256):
                raise PlatformUpdateError("update_archive_hash_mismatch")
            seen_files.add(relative)
    if seen_files != set(expected_files):
        raise PlatformUpdateError("update_archive_manifest_mismatch")
    _verify_candidate_version(resolved, str(version))
    if standalone:
        try:
            runtime = load_runtime_metadata(
                resolved,
                expected_version=str(version),
                expected_architecture=str(manifest.get("architecture", "")),
            )
        except StandaloneRuntimeError as exc:
            raise PlatformUpdateError(exc.code) from exc
        if runtime.manager_protocol != manifest.get("manager_protocol"):
            raise PlatformUpdateError("standalone_manager_protocol_invalid")
    else:
        _verify_dependency_stability(resolved, _source_root())
    return marker, expected_files


def validate_candidate(candidate_path: str, version: str, manifest_sha256: str = "", *, require_maintenance=False) -> dict:
    marker, files = load_verified_candidate(candidate_path, version, manifest_sha256)
    if require_maintenance:
        _require_maintenance_protocol(_candidate_root(candidate_path), files)
    return marker


def available_rollback_versions(current_version: str) -> list[dict]:
    if deployment_kind() == "docker":
        try:
            return read_docker_capabilities()["rollback_versions"]
        except PlatformUpdateError:
            return []
    raw = os.environ.get("SAAS_RELEASES_DIR", "/opt/xianyu-saas/releases").strip()
    root = Path(raw)
    if not root.is_absolute() or not root.exists() or root.is_symlink() or not root.is_dir():
        return []
    current = SemVer.parse(current_version)
    current_runtime = None
    if release_kind() == STANDALONE_RELEASE_KIND:
        try:
            current_runtime = load_runtime_metadata(
                _source_root(),
                expected_version=current_version,
                expected_architecture=_standalone_architecture(),
            )
        except StandaloneRuntimeError:
            return []
    versions: list[tuple[SemVer, dict]] = []
    try:
        entries = tuple(root.iterdir())
    except OSError:
        return []
    for path in entries:
        if path.is_symlink() or not path.is_dir() or path.name == current_version:
            continue
        try:
            parsed = SemVer.parse(path.name)
            _trusted_update_directory(path)
            marker = _marker_payload(path)
            release = _release_from_marker(marker)
            manifest_limit = (
                MAX_STANDALONE_MANIFEST_BYTES
                if release.kind == STANDALONE_RELEASE_KIND
                else MAX_MANIFEST_BYTES
            )
            raw_manifest = _read_secure_file(path / CACHED_MANIFEST_FILE, manifest_limit)
            verify_manifest_signature(raw_manifest, _read_secure_file(path / CACHED_SIGNATURE_FILE, MAX_SIGNATURE_BYTES))
            if hashlib.sha256(raw_manifest).hexdigest() != marker.get("manifest_sha256"):
                continue
            manifest, files = parse_manifest(raw_manifest, release)
            for relative, expected in files.items():
                file = path / relative
                _trusted_update_directory(file.parent)
                metadata = file.lstat()
                if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                    raise PlatformUpdateError("update_candidate_invalid")
                if hashlib.sha256(_read_secure_file(file, expected.size)).hexdigest() != expected.sha256:
                    raise PlatformUpdateError("update_candidate_invalid")
            _require_maintenance_protocol(path, files)
            _verify_candidate_version(path, path.name)
            if release.kind == STANDALONE_RELEASE_KIND:
                runtime = load_runtime_metadata(
                    path,
                    expected_version=path.name,
                    expected_architecture=_standalone_architecture(),
                )
                if (
                    current_runtime is None
                    or runtime.manager_protocol > MANAGER_PROTOCOL
                    or runtime.update_data_version != current_runtime.update_data_version
                    or runtime.manager_protocol != manifest.get("manager_protocol")
                    or runtime.update_data_version != manifest.get("update_data_version")
                ):
                    continue
            else:
                _verify_dependency_stability(path, _source_root())
        except (PlatformUpdateError, OSError):
            continue
        if parsed.compare(current) >= 0:
            continue
        if marker.get("schema") != 1 or str(marker.get("version", "")) != path.name:
            continue
        versions.append((parsed, {"version": path.name, "manifest_sha256": marker["manifest_sha256"]}))
    ordered: list[tuple[SemVer, dict]] = []
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
        "SAAS_UPDATE_INTENT_FILE", "/var/lib/xianyu-saas-updates/intent.json"
    ).strip()
    path = Path(raw)
    if not path.is_absolute():
        raise PlatformUpdateError("update_intent_path_invalid")
    return path


def _write_update_intent_unwrapped(
    action: str, version: str, *, channel: str, requested_by: int,
    candidate_path: str = "", manifest_sha256: str = "", operation_id: str = "",
    expected_current_version: str = "", requested_at: float | None = None,
) -> dict:
    if action not in {"apply", "rollback"} or int(requested_by) <= 0:
        raise PlatformUpdateError("update_intent_invalid")
    if channel not in VALID_CHANNELS:
        raise PlatformUpdateError("update_channel_invalid")
    SemVer.parse(version)
    if operation_id and not OPERATION_ID_RE.fullmatch(operation_id):
        raise PlatformUpdateError("update_operation_invalid")
    if action == "apply":
        validate_candidate(
            candidate_path,
            version,
            manifest_sha256,
            require_maintenance=bool(operation_id),
        )
    elif candidate_path:
        raise PlatformUpdateError("update_intent_invalid")
    intent = _intent_file()
    parent = intent.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _trusted_update_directory(parent, writable=True)
    # A root-owned sticky IPC parent must remain root-owned and traversable to
    # expose read-only status. Never chmod somebody else's deployment directory.
    payload = {"schema": 1, "action": action, "version": str(version), "channel": channel,
               "candidate_path": candidate_path if action == "apply" else "",
               "manifest_sha256": manifest_sha256, "requested_by": int(requested_by),
               "requested_at": time.time() if requested_at is None else requested_at,
               "nonce": operation_id or secrets.token_hex(16)}
    if operation_id:
        SemVer.parse(expected_current_version)
        payload.update(operation_id=operation_id, expected_current_version=expected_current_version)
    _publish_request(intent, payload, processing=intent.with_name("intent.processing.json"))
    result = {"queued": True, "action": action, "version": str(version)}
    if operation_id:
        result.update(operation_id=operation_id, status="queued")
    return result


def write_update_intent(
    action: str,
    version: str,
    *,
    channel: str,
    requested_by: int,
    candidate_path: str = "",
    manifest_sha256: str = "",
    operation_id: str = "",
    expected_current_version: str = "",
    requested_at: float | None = None,
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
            operation_id=operation_id, expected_current_version=expected_current_version,
            requested_at=requested_at,
        )
    except PlatformUpdateError:
        raise
    except OSError as exc:
        raise PlatformUpdateError("update_intent_write_failed") from exc
