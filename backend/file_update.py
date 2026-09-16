"""Built-in signed file update for launcher-managed installations.

The running application (service user) downloads and verifies the existing signed
Docker source package, stages it inside its own writable code store and asks the
bundled supervisor launcher to stop, switch and restart.  The launcher reports
the real outcome (health + target version, rollback on failure) back through
``result.json``.  Business data, the master key and the trusted public key stay
outside the writable code store, and no privileged updater registration is
required.  Same-UID ownership is an explicit boundary here, not an equivalent of
root-owned updater records.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import time
from pathlib import Path

import version


STORE_SCHEMA = 1
HEARTBEAT_MAX_AGE = 45.0
MAX_LAUNCHER_BYTES = 16 * 1024
MAX_RESULT_BYTES = 16 * 1024
MAX_REQUEST_BYTES = 16 * 1024
REQUIRED_CANDIDATE_FILES = (
    "backend/app.py",
    "backend/job_consumer.py",
    "backend/platform_update.py",
    "frontend/index.html",
    "worker/main.py",
)
UPDATE_DATA_VERSION_RE = re.compile(r"^UPDATE_DATA_VERSION\s*=\s*([0-9]+)\s*$", re.MULTILINE)


class FileUpdateError(RuntimeError):
    """Stable code only; never paths, environment values or file contents."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def code_root() -> Path:
    configured = os.environ.get("SAAS_APP_CODE_DIR", "").strip()
    if not configured:
        configured = "/data/app-code" if version.deployment_kind() == "docker" else "/var/lib/xianyu-saas/app-code"
    path = Path(configured)
    if not path.is_absolute() or ".." in path.parts:
        raise FileUpdateError("update_store_invalid")
    return path


def _read_small(path: Path, limit: int) -> dict | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise FileUpdateError("update_store_invalid") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
        raise FileUpdateError("update_store_invalid")
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeError):
        raise FileUpdateError("update_store_invalid") from None
    if not isinstance(payload, dict):
        raise FileUpdateError("update_store_invalid")
    return payload


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8) + ".tmp")
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def launcher_payload() -> dict | None:
    try:
        payload = _read_small(code_root() / "launcher.json", MAX_LAUNCHER_BYTES)
    except FileUpdateError:
        return None
    if payload is None or payload.get("schema") != STORE_SCHEMA:
        return None
    stamp = payload.get("heartbeat_at")
    if (type(stamp) not in {int, float} or not math.isfinite(stamp)
            or stamp <= 0 or time.time() - stamp > HEARTBEAT_MAX_AGE or stamp - time.time() > 5):
        return None
    if not isinstance(payload.get("code_root"), str) or not isinstance(payload.get("active_version"), str):
        return None
    return payload


def _pid_alive(pid) -> bool:
    if os.name != "posix" or type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def launcher_fresh() -> bool:
    """A live, ready launcher is required; a stale or not-ready heartbeat is not."""
    payload = launcher_payload()
    if payload is None or payload.get("ready") is not True or not _pid_alive(payload.get("pid")):
        return False
    return True


def configured() -> bool:
    """True when the installation explicitly selects the built-in code store."""
    return bool(os.environ.get("SAAS_APP_CODE_DIR", "").strip())


def _store_marker_present() -> bool:
    try:
        root = code_root()
        return ((root / "launcher.json").exists()
                or (root / "history.json").exists()
                or (root / "current").is_symlink())
    except FileUpdateError:
        return False


def capability_or_reason() -> dict | None:
    """Full capability when live; an explicit own not-ready reason when configured."""
    mode = version.deployment_kind()
    if mode not in {"docker", "systemd"}:
        return None
    payload = launcher_payload()
    if payload is not None and _pid_alive(payload.get("pid")):
        if payload.get("ready") is True:
            return capability()
        reason = payload.get("reason")
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
            reason = "update_launcher_unhealthy"
        return {"deployment": mode, "check": True, "download": False, "apply": False, "rollback": False,
                "reason": reason,
                "instruction": "内置文件更新监督启动器当前未就绪；请检查启动器服务与容器日志后重试。"}
    if configured() or _store_marker_present():
        return {"deployment": mode, "check": True, "download": False, "apply": False, "rollback": False,
                "reason": "update_launcher_stale",
                "instruction": "内置文件更新器已配置但监督启动器心跳无效；请检查启动器服务后重试。"}
    return None


def active_root() -> Path | None:
    """Return the currently active release directory, or None for the immutable base."""
    link = code_root() / "current"
    try:
        if not link.is_symlink():
            return None
        resolved = link.resolve(strict=True)
    except OSError:
        return None
    releases = (code_root() / "releases").resolve()
    if not resolved.is_relative_to(releases) or not (resolved / "backend/app.py").is_file():
        return None
    return resolved


def active_version() -> str:
    root = active_root()
    return root.name if root is not None else version.VERSION


def _history() -> dict:
    payload = _read_small(code_root() / "history.json", MAX_RESULT_BYTES)
    if payload is None:
        return {"schema": STORE_SCHEMA, "releases": {}}
    releases = payload.get("releases")
    if payload.get("schema") != STORE_SCHEMA or not isinstance(releases, dict):
        return {"schema": STORE_SCHEMA, "releases": {}}
    return payload


def _record_release(version_text: str, manifest_sha256: str) -> None:
    history = _history()
    history["releases"][version_text] = {"manifest_sha256": manifest_sha256, "installed_at": time.time()}
    _write_atomic(code_root() / "history.json", history)


def available_rollback_versions(current_version: str) -> list[dict]:
    import platform_update as protocol

    try:
        history = _history()["releases"]
    except FileUpdateError:
        return []
    result = []
    for name, record in history.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            continue
        digest = record.get("manifest_sha256")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not (code_root() / "releases" / name).is_dir()):
            continue
        try:
            if protocol.SemVer.parse(name).compare(protocol.SemVer.parse(current_version)) >= 0:
                continue
        except protocol.PlatformUpdateError:
            continue
        result.append({"version": name, "manifest_sha256": digest})
    return sorted(result, key=lambda item: item["version"], reverse=True)


def _key_raw() -> bytes:
    import platform_update as protocol
    from cryptography.hazmat.primitives import serialization

    key_file = protocol._public_key_file()
    if not key_file.is_file() or key_file.is_symlink():
        raise FileUpdateError("update_public_key_invalid")
    if key_file.resolve().is_relative_to(code_root().resolve()):
        raise FileUpdateError("update_public_key_invalid")
    return protocol.load_public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def capability() -> dict | None:
    """Return the built-in file-update capability, or None when no launcher is live."""
    if not launcher_fresh():
        return None
    mode = version.deployment_kind()
    if mode not in {"docker", "systemd"}:
        return None
    instruction = (
        "内置文件更新：下载并校验签名源码包后切换应用版本目录并自动重启，保留业务数据与配置；"
        "依赖或数据格式不兼容的版本会在停服前拒绝。"
    )
    result = {"deployment": mode, "check": True, "download": True, "apply": True, "rollback": True,
              "reason": "", "instruction": instruction}
    try:
        root = code_root()
        if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
            result.update(apply=False, rollback=False, reason="update_store_unwritable")
            return result
        _key_raw()
        link = root / "current"
        if link.is_symlink():
            resolved = link.resolve(strict=True)
            if not resolved.is_relative_to((root / "releases").resolve()) or not (resolved / "backend/app.py").is_file():
                result.update(apply=False, rollback=False, reason="update_installation_unavailable")
    except FileUpdateError as error:
        result.update(apply=False, rollback=False, reason=error.code)
    except Exception:
        result.update(apply=False, rollback=False, reason="update_installation_unavailable")
    return result


def _fetch(session, url: str, limit: int) -> bytes:
    import platform_update as protocol

    return protocol._request_bytes(session, url, max_bytes=limit, asset=True)


def _runtime_manifest(raw: bytes, release, parsed) -> dict:
    if hashlib.sha256(raw).hexdigest() != parsed.runtime_manifest_sha256:
        raise FileUpdateError("update_manifest_invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise FileUpdateError("update_manifest_invalid") from None
    if (not isinstance(payload, dict) or payload.get("schema") != 1
            or payload.get("version") != release.version
            or payload.get("artifact") != f"xianyu-saas-{release.version}.tar.gz"
            or not isinstance(payload.get("files"), list) or not payload["files"]):
        raise FileUpdateError("update_manifest_invalid")
    return payload


def stage(release, channel: str, current_version: str, operation_id: str) -> dict:
    """Download, verify and stage a candidate release without touching running code."""
    import platform_update as protocol
    import requests
    from docker_update_protocol import DockerUpdateError, extract_verified_source, verify_docker_manifest
    from update_progress import report_phase

    if not launcher_fresh():
        raise FileUpdateError("update_installation_unavailable")
    if channel not in protocol.VALID_CHANNELS:
        raise FileUpdateError("update_channel_invalid")
    if protocol.SemVer.parse(release.version).compare(protocol.SemVer.parse(current_version)) <= 0:
        raise FileUpdateError("update_version_already_current")
    if not protocol.OPERATION_ID_RE.fullmatch(str(operation_id)):
        raise FileUpdateError("update_operation_invalid")
    root = code_root()
    releases = root / "releases"
    staging = root / "staging" / str(operation_id)
    if staging.exists() or staging.is_symlink():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700, parents=True)
    session = requests.Session()
    try:
        report_phase("checking")
        manifest_raw = _fetch(session, release.manifest.api_url, protocol.MAX_MANIFEST_BYTES)
        signature_raw = _fetch(session, release.signature.api_url, protocol.MAX_SIGNATURE_BYTES)
        report_phase("verifying")
        parsed = verify_docker_manifest(manifest_raw, signature_raw, _key_raw(), expected_version=release.version)
        if parsed.source_name != release.artifact.name or parsed.source_size != release.artifact.size:
            raise FileUpdateError("update_manifest_invalid")
        if release.runtime_manifest is None:
            raise FileUpdateError("release_assets_missing")
        runtime_raw = _fetch(session, release.runtime_manifest.api_url, protocol.MAX_MANIFEST_BYTES)
        _runtime_manifest(runtime_raw, release, parsed)
        source_path = staging / "source.zip"
        digest = protocol._download_asset_to_file(session, release.artifact, source_path,
                                                 max_bytes=parsed.source_size)
        if not secrets.compare_digest(digest, parsed.source_sha256):
            raise FileUpdateError("update_artifact_hash_mismatch")
        candidate = extract_verified_source(source_path, staging / "extract", parsed)
        _verify_candidate_tree(candidate)
        _verify_compatibility(candidate)
        target_dir = releases / release.version
        if target_dir.is_symlink() or active_root() == target_dir:
            raise FileUpdateError("update_candidate_invalid")
        if target_dir.exists():
            shutil.rmtree(target_dir)
        releases.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.replace(candidate, target_dir)
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        _record_release(release.version, manifest_sha)
        return {"version": release.version, "channel": channel, "manifest_sha256": manifest_sha,
                "candidate_path": str(target_dir), "release_id": release.release_id,
                "release_notes": release.notes}
    except DockerUpdateError as error:
        code = ("update_artifact_hash_mismatch"
                if error.code in {"docker_source_hash_mismatch", "docker_source_size_mismatch"}
                else error.code)
        raise FileUpdateError(code) from error
    except OSError as error:
        raise FileUpdateError("update_store_write_failed") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _verify_candidate_tree(candidate: Path) -> None:
    for relative in REQUIRED_CANDIDATE_FILES:
        path = candidate / relative
        if path.is_symlink() or not path.is_file():
            raise FileUpdateError("update_candidate_invalid")


def _verify_compatibility(candidate: Path) -> None:
    import platform_update as protocol

    current = active_root() or protocol.PROJECT_ROOT
    for relative in ("backend/requirements.txt", "worker/requirements.txt"):
        try:
            if (candidate / relative).read_bytes() != (current / relative).read_bytes():
                raise FileUpdateError("update_dependency_changed")
        except OSError:
            raise FileUpdateError("update_candidate_invalid") from None
    try:
        source = (candidate / "backend/version.py").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise FileUpdateError("update_candidate_invalid") from None
    match = UPDATE_DATA_VERSION_RE.search(source)
    if match is None or int(match.group(1)) != version.UPDATE_DATA_VERSION:
        raise FileUpdateError("update_data_backward_incompatible")


def publish(operation: dict) -> dict:
    """Atomically hand the staged switch to the launcher."""
    import platform_update as protocol

    if not launcher_fresh():
        raise FileUpdateError("update_installation_unavailable")
    operation_id = str(operation.get("operation_id") or "")
    if not protocol.OPERATION_ID_RE.fullmatch(operation_id):
        raise FileUpdateError("update_operation_invalid")
    action = operation.get("action")
    if action not in {"apply", "rollback"}:
        raise FileUpdateError("update_intent_invalid")
    version_text = str(operation.get("version") or "")
    protocol.SemVer.parse(version_text)
    if str(operation.get("expected_current_version") or "") != version.VERSION:
        raise FileUpdateError("update_current_version_mismatch")
    digest = str(operation.get("manifest_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise FileUpdateError("update_intent_invalid")
    candidate = code_root() / "releases" / version_text
    if not candidate.is_dir() or not (candidate / "backend/app.py").is_file():
        raise FileUpdateError("update_candidate_invalid")
    payload = {"schema": STORE_SCHEMA, "operation_id": operation_id, "action": action,
               "version": version_text, "expected_current_version": str(operation.get("expected_current_version") or ""),
               "manifest_sha256": digest, "requested_at": operation.get("requested_at"),
               "requested_by": int(operation.get("requested_by") or 0)}
    request = code_root() / "switch.json"
    existing = _read_small(request, MAX_REQUEST_BYTES)
    if existing is not None:
        if all(existing.get(key) == value for key, value in payload.items()):
            return {"queued": True, "status": "queued", "action": action, "version": version_text,
                    "operation_id": operation_id}
        raise FileUpdateError("update_intent_pending")
    _write_atomic(request, payload)
    return {"queued": True, "status": "queued", "action": action, "version": version_text,
            "operation_id": operation_id}


def read_status(operation: dict) -> dict | None:
    try:
        payload = _read_small(code_root() / "result.json", MAX_RESULT_BYTES)
    except FileUpdateError:
        return None
    if payload is None:
        return None
    operation_id = str(operation.get("operation_id") or "")
    if (payload.get("schema") != STORE_SCHEMA or payload.get("operation_id") != operation_id
            or payload.get("action") != operation.get("action") or payload.get("version") != operation.get("version")
            or payload.get("status") not in {"succeeded", "rolled_back", "failed"}):
        return None
    stamp = payload.get("updated_at")
    if type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp <= 0 or stamp > time.time() + 5:
        return None
    current = payload.get("current_version")
    if not isinstance(current, str):
        return None
    return {"schema": 1, "operation_id": operation_id, "action": operation["action"],
            "version": operation["version"], "current_version": current,
            "status": payload["status"], "phase": payload["status"],
            "updated_at": float(stamp), "error_code": str(payload.get("error_code") or "")[:96]}


def validate_operation(version_text: str, manifest_sha256: str) -> None:
    import platform_update as protocol
    protocol.SemVer.parse(version_text)
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest_sha256 or "")):
        raise FileUpdateError("update_candidate_invalid")
    history = _history()["releases"].get(version_text)
    target = code_root() / "releases" / version_text
    if not isinstance(history, dict) or history.get("manifest_sha256") != manifest_sha256:
        raise FileUpdateError("update_candidate_invalid")
    _verify_candidate_tree(target)
    _verify_compatibility(target)
