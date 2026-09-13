"""Validated metadata and paths for self-contained Linux systemd releases."""
from __future__ import annotations

import json
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path


RELEASE_KIND_ENV = "SAAS_RELEASE_KIND"
STANDALONE_RELEASE_KIND = "standalone"
MANAGER_PROTOCOL = 1
SUPPORTED_ARCHITECTURES = frozenset({"x86_64", "aarch64"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class StandaloneRuntimeError(ValueError):
    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class RuntimeMetadata:
    version: str
    commit: str
    platform: str
    architecture: str
    target: str
    python_version: str
    python_build: str
    uv_version: str
    manager_protocol: int
    update_data_version: int
    backend_lock_sha256: str
    worker_lock_sha256: str


def release_kind(environ=None) -> str:
    source = os.environ if environ is None else environ
    return STANDALONE_RELEASE_KIND if str(source.get(RELEASE_KIND_ENV, "")).strip().lower() == STANDALONE_RELEASE_KIND else "source"


def normalize_architecture(value: str | None = None) -> str:
    machine = str(value if value is not None else platform.machine()).strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }
    machine = aliases.get(machine, machine)
    if machine not in SUPPORTED_ARCHITECTURES:
        raise StandaloneRuntimeError("standalone_architecture_unsupported")
    return machine


def target_name(architecture: str | None = None) -> str:
    return f"linux-{normalize_architecture(architecture)}"


def standalone_asset_names(version: str, architecture: str | None = None) -> tuple[str, str, str]:
    if not VERSION_RE.fullmatch(str(version or "")):
        raise StandaloneRuntimeError("release_version_invalid")
    base = f"xianyu-saas-{version}-{target_name(architecture)}"
    return f"{base}.tar.gz", f"{base}.manifest.json", f"{base}.manifest.sig"


def manager_asset_name(version: str, architecture: str | None = None) -> str:
    if not VERSION_RE.fullmatch(str(version or "")):
        raise StandaloneRuntimeError("release_version_invalid")
    return f"xianyu-saas-{version}-{target_name(architecture)}"


def runtime_python(release_root: str | Path) -> Path:
    return Path(release_root) / "runtime" / "python" / "bin" / "python3"


def runtime_site(release_root: str | Path, role: str) -> Path:
    if role not in {"backend", "worker"}:
        raise StandaloneRuntimeError("standalone_runtime_role_invalid")
    return Path(release_root) / "runtime" / "site" / role


def _bounded_text(payload: dict, name: str, *, maximum: int = 200) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum or value != value.strip():
        raise StandaloneRuntimeError("standalone_runtime_metadata_invalid")
    return value


def parse_runtime_metadata(raw: bytes | str, *, expected_version: str = "", expected_architecture: str = "") -> RuntimeMetadata:
    try:
        if isinstance(raw, bytes):
            if len(raw) > 64 * 1024:
                raise ValueError("metadata too large")
            payload = json.loads(raw.decode("utf-8"))
        elif isinstance(raw, str):
            if len(raw.encode("utf-8")) > 64 * 1024:
                raise ValueError("metadata too large")
            payload = json.loads(raw)
        else:
            raise TypeError("metadata must be bytes or text")
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise StandaloneRuntimeError("standalone_runtime_metadata_invalid") from exc
    expected_keys = {
        "schema", "version", "commit", "platform", "architecture", "target",
        "python_version", "python_build", "uv_version", "manager_protocol",
        "update_data_version", "backend_lock_sha256", "worker_lock_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys or type(payload.get("schema")) is not int or payload["schema"] != 1:
        raise StandaloneRuntimeError("standalone_runtime_metadata_invalid")
    version = _bounded_text(payload, "version")
    if not VERSION_RE.fullmatch(version) or (expected_version and version != expected_version):
        raise StandaloneRuntimeError("standalone_runtime_version_mismatch")
    commit = _bounded_text(payload, "commit", maximum=64)
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise StandaloneRuntimeError("standalone_runtime_metadata_invalid")
    platform_name = _bounded_text(payload, "platform", maximum=20)
    if platform_name != "linux":
        raise StandaloneRuntimeError("standalone_runtime_platform_mismatch")
    architecture = normalize_architecture(_bounded_text(payload, "architecture", maximum=20))
    if expected_architecture and architecture != normalize_architecture(expected_architecture):
        raise StandaloneRuntimeError("standalone_runtime_architecture_mismatch")
    target = _bounded_text(payload, "target", maximum=40)
    if target != target_name(architecture):
        raise StandaloneRuntimeError("standalone_runtime_metadata_invalid")
    python_version = _bounded_text(payload, "python_version", maximum=40)
    python_build = _bounded_text(payload, "python_build", maximum=40)
    uv_version = _bounded_text(payload, "uv_version", maximum=40)
    manager_protocol = payload.get("manager_protocol")
    update_data_version = payload.get("update_data_version")
    if type(manager_protocol) is not int or not 1 <= manager_protocol <= 2**31 - 1:
        raise StandaloneRuntimeError("standalone_manager_protocol_invalid")
    if type(update_data_version) is not int or not 1 <= update_data_version <= 2**31 - 1:
        raise StandaloneRuntimeError("standalone_data_version_invalid")
    backend_lock = _bounded_text(payload, "backend_lock_sha256", maximum=64)
    worker_lock = _bounded_text(payload, "worker_lock_sha256", maximum=64)
    if not SHA256_RE.fullmatch(backend_lock) or not SHA256_RE.fullmatch(worker_lock):
        raise StandaloneRuntimeError("standalone_runtime_metadata_invalid")
    return RuntimeMetadata(
        version=version,
        commit=commit,
        platform=platform_name,
        architecture=architecture,
        target=target,
        python_version=python_version,
        python_build=python_build,
        uv_version=uv_version,
        manager_protocol=manager_protocol,
        update_data_version=update_data_version,
        backend_lock_sha256=backend_lock,
        worker_lock_sha256=worker_lock,
    )


def validate_standalone_manifest(
    payload: dict,
    *,
    expected_version: str,
    expected_target: str,
    expected_artifact: str,
) -> RuntimeMetadata:
    """Validate architecture-bound metadata embedded in a standalone manifest."""
    required = {
        "schema", "kind", "version", "artifact", "artifact_sha256", "artifact_size",
        "platform", "architecture", "target", "manager_protocol", "update_data_version",
        "runtime", "files",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise StandaloneRuntimeError("standalone_manifest_invalid")
    if type(payload.get("schema")) is not int or payload["schema"] != 2:
        raise StandaloneRuntimeError("standalone_manifest_invalid")
    if payload.get("kind") != STANDALONE_RELEASE_KIND or payload.get("platform") != "linux":
        raise StandaloneRuntimeError("standalone_manifest_invalid")
    if payload.get("version") != expected_version or payload.get("artifact") != expected_artifact:
        raise StandaloneRuntimeError("standalone_manifest_invalid")
    architecture = normalize_architecture(str(payload.get("architecture", "")))
    target = target_name(architecture)
    if target != expected_target or payload.get("target") != target:
        raise StandaloneRuntimeError("standalone_runtime_architecture_mismatch")
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        raise StandaloneRuntimeError("standalone_manifest_invalid")
    metadata = parse_runtime_metadata(
        json.dumps(runtime, ensure_ascii=True, separators=(",", ":")),
        expected_version=expected_version,
        expected_architecture=architecture,
    )
    if payload.get("manager_protocol") != metadata.manager_protocol:
        raise StandaloneRuntimeError("standalone_manager_protocol_invalid")
    if payload.get("update_data_version") != metadata.update_data_version:
        raise StandaloneRuntimeError("standalone_data_version_invalid")
    return metadata


def load_runtime_metadata(release_root: str | Path, *, expected_version: str = "", expected_architecture: str = "") -> RuntimeMetadata:
    path = Path(release_root) / "runtime" / "runtime.json"
    try:
        metadata = path.lstat()
        if not path.is_file() or path.is_symlink() or metadata.st_size <= 0 or metadata.st_size > 64 * 1024:
            raise OSError("invalid runtime metadata file")
        raw = path.read_bytes()
    except OSError as exc:
        raise StandaloneRuntimeError("standalone_runtime_metadata_invalid") from exc
    return parse_runtime_metadata(raw, expected_version=expected_version, expected_architecture=expected_architecture)
