#!/usr/bin/env python3
"""Verify a complete public GitHub Release directory without private key access."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import stat
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from docker_update_protocol import DockerUpdateError, docker_asset_names, extract_verified_source, verify_docker_manifest
from standalone_runtime import (
    MANAGER_PROTOCOL,
    SUPPORTED_ARCHITECTURES,
    StandaloneRuntimeError,
    manager_asset_name,
    standalone_asset_names,
    target_name,
    validate_standalone_manifest,
)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
REQUIRED_STANDALONE_ROOTS = frozenset({"backend", "frontend", "worker", "runtime", "manager"})
REQUIRED_DOCKER_INSTALL_FILES = (
    "Dockerfile",
    "docker/entrypoint.sh",
    "deploy/docker-install.sh",
    "docker-compose.yml",
    "docker-compose.updates.yml",
    "config/saas.env.docker.example",
    "deploy/update-signing.pub",
)


class VerificationError(RuntimeError):
    """Stable public verification error code."""


def json_value(path: Path, code: str):
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 16 * 1024 * 1024:
            raise ValueError
        return json.loads(raw.decode("utf-8")), raw
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise VerificationError(code) from None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise VerificationError("release_asset_missing") from None
    return digest.hexdigest()


def asset_record(path: Path) -> dict:
    try:
        metadata = path.lstat()
    except OSError:
        raise VerificationError("release_asset_missing") from None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise VerificationError("release_asset_invalid")
    return {"name": path.name, "size": metadata.st_size, "sha256": sha256_file(path)}


def public_key(raw: bytes) -> tuple[Ed25519PublicKey, bytes]:
    try:
        value = raw.strip()
        if not value or len(raw) > 4096:
            raise ValueError
        if value.startswith(b"-----BEGIN"):
            key = serialization.load_pem_public_key(value)
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError
        else:
            decoded = base64.b64decode(value, validate=True)
            if len(decoded) != 32:
                raise ValueError
            key = Ed25519PublicKey.from_public_bytes(decoded)
        encoded = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return key, encoded
    except (ValueError, TypeError, binascii.Error, UnsupportedAlgorithm):
        raise VerificationError("release_public_key_invalid") from None


def verify_signature(key: Ed25519PublicKey, payload: bytes, signature: bytes, code: str) -> None:
    try:
        decoded = base64.b64decode(signature, validate=True)
        if len(decoded) != 64 or base64.b64encode(decoded) != signature:
            raise ValueError
        key.verify(decoded, payload)
    except (ValueError, TypeError, binascii.Error, InvalidSignature):
        raise VerificationError(code) from None


def expected_content(version: str) -> dict[str, dict]:
    base = f"xianyu-saas-{version}"
    docker = docker_asset_names(version)
    result = {
        docker[0]: {"kind": "docker-source", "target": "docker"},
        docker[1]: {"kind": "docker-manifest", "target": "docker"},
        docker[2]: {"kind": "docker-signature", "target": "docker"},
        # The schema-1 runtime inventory stays published unsigned; the signed
        # docker manifest binds it through runtime_manifest_sha256.
        f"{base}.manifest.json": {"kind": "runtime-manifest", "target": "docker"},
    }
    for architecture in sorted(SUPPORTED_ARCHITECTURES):
        target = target_name(architecture)
        archive, manifest, signature = standalone_asset_names(version, architecture)
        result.update({
            archive: {"kind": "standalone-archive", "target": target},
            manifest: {"kind": "standalone-manifest", "target": target},
            signature: {"kind": "standalone-signature", "target": target},
            manager_asset_name(version, architecture): {"kind": "bootstrap-manager", "target": target},
        })
    return result


def manifest_files(payload, code: str) -> dict[str, dict]:
    if not isinstance(payload, list) or not payload:
        raise VerificationError(code)
    result = {}
    folded = set()
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256", "executable"}:
            raise VerificationError(code)
        path = item["path"]
        size = item["size"]
        digest = item["sha256"]
        executable = item["executable"]
        if (not isinstance(path, str) or not path or "\\" in path or path.startswith("/")
                or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
                or path.casefold() in folded or type(size) is not int or size < 0
                or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
                or type(executable) is not bool):
            raise VerificationError(code)
        folded.add(path.casefold())
        result[path] = item
    return result


def verify_tar(path: Path, expected: dict[str, dict], *, standalone: bool) -> dict[str, bytes]:
    captured = {}
    seen = set()
    roots = set()
    try:
        with path.open("rb") as probe:
            if probe.read(2) != b"\x1f\x8b":
                raise VerificationError("release_archive_invalid")
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                parts = PurePosixPath(name).parts
                if (not name or "\\" in name or name.startswith("/") or any(part in {"", ".", ".."} for part in parts)
                        or member.issym() or member.islnk() or not member.isfile() or name in seen):
                    raise VerificationError("release_archive_link_or_path_invalid")
                if standalone and (parts[0] == "app" or (parts[0] not in REQUIRED_STANDALONE_ROOTS and name != "package.json")):
                    raise VerificationError("standalone_archive_layout_invalid")
                seen.add(name)
                roots.add(parts[0])
                item = expected.get(name)
                if item is None or member.size != item["size"]:
                    raise VerificationError("release_archive_manifest_mismatch")
                source = archive.extractfile(member)
                if source is None:
                    raise VerificationError("release_archive_invalid")
                payload = source.read()
                if len(payload) != item["size"] or hashlib.sha256(payload).hexdigest() != item["sha256"]:
                    raise VerificationError("release_archive_manifest_mismatch")
                if bool(member.mode & 0o111) != item["executable"]:
                    raise VerificationError("release_archive_mode_mismatch")
                if name in {"runtime/runtime.json", "manager/xianyu-saas"}:
                    captured[name] = payload
    except VerificationError:
        raise
    except (OSError, tarfile.TarError, OverflowError):
        raise VerificationError("release_archive_invalid") from None
    if seen != set(expected):
        raise VerificationError("release_archive_manifest_mismatch")
    if standalone and not REQUIRED_STANDALONE_ROOTS <= roots:
        raise VerificationError("standalone_archive_layout_invalid")
    return captured


def verify_source_zip(path: Path, version: str) -> None:
    prefix = f"xianyu-saas-{version}/"
    required = {f"{prefix}{name}" for name in REQUIRED_DOCKER_INSTALL_FILES}
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries:
                raise VerificationError("docker_source_invalid")
            present = {}
            for entry in entries:
                mode = entry.external_attr >> 16
                if (not entry.filename.startswith(prefix) or entry.filename == prefix
                        or not stat.S_ISREG(mode) or "\\" in entry.filename
                        or any(part in {"", ".", ".."} for part in PurePosixPath(entry.filename).parts)):
                    raise VerificationError("docker_source_invalid")
                if entry.filename in required:
                    present[entry.filename] = entry.file_size
            if set(present) != required or any(size <= 0 for size in present.values()):
                raise VerificationError("docker_source_invalid")
    except VerificationError:
        raise
    except (OSError, zipfile.BadZipFile, OverflowError):
        raise VerificationError("docker_source_invalid") from None


def verify_docker(folder: Path, version: str, commit: str, public_raw: bytes) -> None:
    """Verify the signed Docker descriptor, runtime inventory binding and source package."""
    base = f"xianyu-saas-{version}"
    runtime_name = f"{base}.manifest.json"
    runtime_manifest, runtime_raw = json_value(folder / runtime_name, "docker_runtime_manifest_invalid")
    if (not isinstance(runtime_manifest, dict)
            or set(runtime_manifest) != {"schema", "version", "artifact", "artifact_sha256", "artifact_size", "files"}
            or runtime_manifest.get("schema") != 1 or runtime_manifest.get("version") != version
            or runtime_manifest.get("artifact") != f"{base}.tar.gz"
            or not SHA256_RE.fullmatch(str(runtime_manifest.get("artifact_sha256", "")))
            or type(runtime_manifest.get("artifact_size")) is not int or runtime_manifest["artifact_size"] < 1):
        raise VerificationError("docker_runtime_manifest_invalid")
    runtime_files = manifest_files(runtime_manifest.get("files"), "docker_runtime_manifest_invalid")
    source_name, docker_manifest_name, docker_signature_name = docker_asset_names(version)
    docker_payload, docker_raw = json_value(folder / docker_manifest_name, "docker_manifest_invalid")
    try:
        parsed = verify_docker_manifest(
            docker_raw,
            (folder / docker_signature_name).read_bytes(),
            public_raw,
            expected_version=version,
        )
    except DockerUpdateError as exc:
        raise VerificationError(exc.code) from None
    if parsed.commit != commit or docker_payload.get("commit") != commit:
        raise VerificationError("docker_manifest_invalid")
    if hashlib.sha256(runtime_raw).hexdigest() != parsed.runtime_manifest_sha256:
        raise VerificationError("docker_manifest_invalid")
    source_record = asset_record(folder / source_name)
    if source_record["sha256"] != parsed.source_sha256 or source_record["size"] != parsed.source_size:
        raise VerificationError("docker_manifest_invalid")
    verify_source_zip(folder / source_name, version)
    # ZIP metadata preserves Unix modes even when extracted on Windows.
    with zipfile.ZipFile(folder / source_name) as archive:
        source_modes = {entry.filename: entry.external_attr >> 16 for entry in archive.infolist()}
    # Re-run the real protocol extractor and compare every runtime manifest
    # record against the actual signed source payload (hash, size, executable).
    try:
        with tempfile.TemporaryDirectory(prefix="release-runtime-") as temporary:
            source_root = extract_verified_source(folder / source_name, Path(temporary) / "source", parsed)
            for item in runtime_files.values():
                candidate = source_root.joinpath(*PurePosixPath(item["path"]).parts)
                try:
                    metadata = candidate.lstat()
                except OSError:
                    raise VerificationError("docker_runtime_manifest_invalid") from None
                if (candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_size != item["size"]
                        or bool(source_modes.get(f"{base}/{item['path']}", 0) & 0o111) != item["executable"]
                        or sha256_file(candidate) != item["sha256"]):
                    raise VerificationError("docker_runtime_manifest_invalid")
    except DockerUpdateError as exc:
        raise VerificationError(exc.code) from None
    except OSError:
        raise VerificationError("docker_source_invalid") from None


def verify_standalone(folder: Path, version: str, commit: str, architecture: str, key: Ed25519PublicKey) -> None:
    target = target_name(architecture)
    archive_name, manifest_name, signature_name = standalone_asset_names(version, architecture)
    manager_name = manager_asset_name(version, architecture)
    manifest, manifest_raw = json_value(folder / manifest_name, "standalone_manifest_invalid")
    verify_signature(key, manifest_raw, (folder / signature_name).read_bytes(), "standalone_manifest_signature_invalid")
    try:
        runtime = validate_standalone_manifest(
            manifest,
            expected_version=version,
            expected_target=target,
            expected_artifact=archive_name,
        )
    except StandaloneRuntimeError as exc:
        raise VerificationError(exc.code) from None
    record = asset_record(folder / archive_name)
    if manifest.get("artifact_sha256") != record["sha256"] or manifest.get("artifact_size") != record["size"]:
        raise VerificationError("standalone_manifest_invalid")
    if runtime.commit != commit or runtime.target != target or runtime.manager_protocol != MANAGER_PROTOCOL:
        raise VerificationError("standalone_manifest_invalid")
    captured = verify_tar(
        folder / archive_name,
        manifest_files(manifest.get("files"), "standalone_manifest_invalid"),
        standalone=True,
    )
    runtime_raw = captured.get("runtime/runtime.json")
    manager_raw = captured.get("manager/xianyu-saas")
    if runtime_raw is None or json.loads(runtime_raw) != manifest.get("runtime"):
        raise VerificationError("standalone_runtime_metadata_invalid")
    if manager_raw is None or hashlib.sha256(manager_raw).hexdigest() != sha256_file(folder / manager_name):
        raise VerificationError("standalone_manager_invalid")


def verify(folder: Path, version: str, commit: str, public_key_path: Path) -> dict:
    if not VERSION_RE.fullmatch(version):
        raise VerificationError("release_version_invalid")
    if not COMMIT_RE.fullmatch(commit):
        raise VerificationError("release_commit_invalid")
    try:
        if folder.is_symlink() or not folder.is_dir():
            raise OSError
        key, public_raw = public_key(public_key_path.read_bytes())
    except OSError:
        raise VerificationError("release_directory_invalid") from None
    content = expected_content(version)
    expected_names = set(content) | {"artifacts.json", "artifacts.json.sig"}
    actual_names = {path.name for path in folder.iterdir()}
    if actual_names != expected_names:
        raise VerificationError("release_asset_set_invalid")
    index, index_raw = json_value(folder / "artifacts.json", "release_index_invalid")
    verify_signature(key, index_raw, (folder / "artifacts.json.sig").read_bytes(), "release_index_signature_invalid")
    required_index = {"schema", "version", "commit", "manager_protocol", "public_key_fingerprint", "files"}
    if (not isinstance(index, dict) or set(index) != required_index or index.get("schema") != 2
            or index.get("version") != version or index.get("commit") != commit
            or index.get("manager_protocol") != MANAGER_PROTOCOL
            or index.get("public_key_fingerprint") != "sha256:" + hashlib.sha256(public_raw).hexdigest()
            or not isinstance(index.get("files"), list) or len(index["files"]) != len(content)):
        raise VerificationError("release_index_invalid")
    indexed = {}
    for item in index["files"]:
        if not isinstance(item, dict) or set(item) != {"name", "kind", "target", "sha256", "size", "manager_protocol"}:
            raise VerificationError("release_index_invalid")
        name = item.get("name")
        if name in indexed or name not in content or item.get("manager_protocol") != MANAGER_PROTOCOL:
            raise VerificationError("release_index_invalid")
        metadata = content[name]
        record = asset_record(folder / name)
        if item != {**record, **metadata, "manager_protocol": MANAGER_PROTOCOL}:
            raise VerificationError("release_index_invalid")
        indexed[name] = item
    if set(indexed) != set(content):
        raise VerificationError("release_index_invalid")
    verify_docker(folder, version, commit, public_raw)
    for architecture in sorted(SUPPORTED_ARCHITECTURES):
        verify_standalone(folder, version, commit, architecture, key)
    return {"schema": 1, "version": version, "commit": commit, "assets": len(expected_names)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify(args.directory.resolve(), args.version, args.commit.lower(), args.public_key.resolve())
    except VerificationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("release_verification_failed", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
