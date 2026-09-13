#!/usr/bin/env python3
"""Build a fail-closed standalone Python runtime from an exact Git commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_INPUTS = ROOT / "deploy" / "runtime"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
MAX_MEMBERS = 200_000
MAX_FILE_SIZE = 512 * 1024 * 1024
MAX_UNPACKED_SIZE = 4 * 1024 * 1024 * 1024


class StandaloneError(RuntimeError):
    """Stable public error code without leaking subprocess or environment data."""


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def canonical_member_path(raw: str) -> PurePosixPath:
    if not raw or "\\" in raw or raw.startswith("/") or "\x00" in raw:
        raise StandaloneError("standalone_archive_path_invalid")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise StandaloneError("standalone_archive_path_invalid")
    if len(raw) > 1000 or any(len(part) > 240 for part in path.parts):
        raise StandaloneError("standalone_archive_path_invalid")
    return path


def ensure_destination(root: Path, relative: PurePosixPath) -> Path:
    destination = root.joinpath(*relative.parts)
    try:
        destination.resolve(strict=False).relative_to(root.resolve())
    except ValueError:
        raise StandaloneError("standalone_archive_path_invalid") from None
    return destination


def write_regular_file(destination: Path, payload, mode: int, expected_size: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise StandaloneError("standalone_archive_path_collision")
    total = 0
    try:
        with destination.open("xb") as target:
            while True:
                chunk = payload.read(min(1024 * 1024, expected_size + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    raise StandaloneError("standalone_archive_size_mismatch")
                target.write(chunk)
        if total != expected_size:
            raise StandaloneError("standalone_archive_size_mismatch")
        destination.chmod(0o755 if mode & 0o111 else 0o644)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def relative_link_target(path: PurePosixPath, raw: str) -> PurePosixPath:
    if not raw or "\\" in raw or "\x00" in raw or PurePosixPath(raw).is_absolute():
        raise StandaloneError("standalone_archive_link_invalid")
    parts: list[str] = []
    for part in (path.parent / PurePosixPath(raw)).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise StandaloneError("standalone_archive_link_invalid")
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise StandaloneError("standalone_archive_link_invalid")
    return canonical_member_path(PurePosixPath(*parts).as_posix())


def safe_extract_tar(
    archive_path: Path,
    destination: Path,
    *,
    required_root: str | None = None,
    materialize_symlinks: bool = False,
) -> list[str]:
    seen: set[str] = set()
    extracted: list[str] = []
    total = 0
    try:
        archive = tarfile.open(archive_path, "r:*")
    except (OSError, tarfile.TarError):
        raise StandaloneError("standalone_archive_invalid") from None
    with archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_MEMBERS:
            raise StandaloneError("standalone_archive_invalid")
        entries: list[tuple[tarfile.TarInfo, PurePosixPath, PurePosixPath | None]] = []
        by_path: dict[str, tarfile.TarInfo] = {}
        for member in members:
            path = canonical_member_path(member.name.rstrip("/"))
            if required_root is not None:
                if path.parts[0] != required_root:
                    raise StandaloneError("standalone_archive_root_invalid")
                if len(path.parts) == 1:
                    if not member.isdir():
                        raise StandaloneError("standalone_archive_root_invalid")
                    continue
                path = PurePosixPath(*path.parts[1:])
            relative = path.as_posix()
            if relative in seen:
                raise StandaloneError("standalone_archive_path_collision")
            seen.add(relative)
            if member.islnk() or member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                raise StandaloneError("standalone_archive_special_file")
            if member.issym():
                if not materialize_symlinks:
                    raise StandaloneError("standalone_archive_special_file")
                link_target = relative_link_target(path, member.linkname)
                entries.append((member, path, link_target))
                by_path[path.as_posix()] = member
                continue
            if not (member.isdir() or member.isfile()):
                raise StandaloneError("standalone_archive_special_file")
            if member.isfile():
                if member.size < 0 or member.size > MAX_FILE_SIZE:
                    raise StandaloneError("standalone_archive_too_large")
                total += member.size
                if total > MAX_UNPACKED_SIZE:
                    raise StandaloneError("standalone_archive_too_large")
            entries.append((member, path, None))
            by_path[path.as_posix()] = member

        for member, _path, link_target in entries:
            if link_target is None:
                continue
            target_member = by_path.get(link_target.as_posix())
            if target_member is None or not target_member.isfile() or target_member.issym() or target_member.islnk():
                raise StandaloneError("standalone_archive_link_invalid")
            total += target_member.size
            if total > MAX_UNPACKED_SIZE:
                raise StandaloneError("standalone_archive_too_large")

        for member, path, link_target in entries:
            target = ensure_destination(destination, path)
            if link_target is not None:
                continue
            if member.isdir():
                if target.exists() and not target.is_dir():
                    raise StandaloneError("standalone_archive_path_collision")
                target.mkdir(parents=True, exist_ok=True)
                continue
            payload = archive.extractfile(member)
            if payload is None:
                raise StandaloneError("standalone_archive_invalid")
            with payload:
                write_regular_file(target, payload, member.mode, member.size)
            extracted.append(path.as_posix())

        for _member, path, link_target in entries:
            if link_target is None:
                continue
            target_member = by_path[link_target.as_posix()]
            source = ensure_destination(destination, link_target)
            if source.is_symlink() or not source.is_file():
                raise StandaloneError("standalone_archive_link_invalid")
            with source.open("rb") as payload:
                write_regular_file(
                    ensure_destination(destination, path), payload, target_member.mode, target_member.size
                )
            extracted.append(path.as_posix())
    return extracted


def zip_entry_mode(entry: zipfile.ZipInfo) -> int:
    return (entry.external_attr >> 16) & 0xFFFF


def wheel_destination(path: PurePosixPath) -> PurePosixPath:
    parts = path.parts
    for index, part in enumerate(parts):
        if part.endswith(".data"):
            if index + 2 > len(parts):
                raise StandaloneError("standalone_wheel_path_invalid")
            category = parts[index + 1]
            if category not in {"purelib", "platlib"} or index + 2 >= len(parts):
                raise StandaloneError("standalone_wheel_data_unsupported")
            return PurePosixPath(*parts[index + 2:])
    return path


def validate_wheel_filename(filename: str, architecture: str) -> None:
    try:
        _prefix, python_raw, abi_raw, platform_raw = filename[:-4].rsplit("-", 3)
    except ValueError:
        raise StandaloneError("standalone_wheel_tag_invalid") from None
    python_tags = set(python_raw.split("."))
    abi_tags = set(abi_raw.split("."))
    platform_tags = set(platform_raw.split("."))
    compatible_python = bool(python_tags & {"py3", "py312", "cp312"})
    if "abi3" in abi_tags:
        for tag in python_tags:
            match = re.fullmatch(r"cp3([0-9]{1,2})", tag)
            if match and int(match.group(1)) <= 12:
                compatible_python = True
    compatible_platform = "any" in platform_tags or any(
        (architecture == "x86_64" and tag.endswith("_x86_64"))
        or (architecture == "aarch64" and tag.endswith("_aarch64"))
        for tag in platform_tags
    )
    if not compatible_python or not compatible_platform:
        raise StandaloneError("standalone_wheel_tag_invalid")


def inspect_wheel(archive: zipfile.ZipFile) -> tuple[str, str, dict]:
    metadata_entries = [entry for entry in archive.infolist() if entry.filename.endswith(".dist-info/METADATA")]
    if len(metadata_entries) != 1:
        raise StandaloneError("standalone_wheel_metadata_invalid")
    entry = metadata_entries[0]
    if entry.file_size > 2 * 1024 * 1024:
        raise StandaloneError("standalone_wheel_metadata_invalid")
    try:
        message = BytesParser().parsebytes(archive.read(entry))
        name = message["Name"].strip()
        version = message["Version"].strip()
    except (AttributeError, KeyError, UnicodeError, zipfile.BadZipFile):
        raise StandaloneError("standalone_wheel_metadata_invalid") from None
    license_value = (message.get("License-Expression") or message.get("License") or "").strip()
    classifiers = [value for value in message.get_all("Classifier", []) if value.startswith("License ::")]
    return name, version, {"declared": license_value, "classifiers": classifiers}


def extract_wheel(wheel: Path, destination: Path, package: dict, installed: dict[str, str]) -> dict:
    try:
        archive = zipfile.ZipFile(wheel)
    except (OSError, zipfile.BadZipFile):
        raise StandaloneError("standalone_wheel_invalid") from None
    with archive:
        name, version, license_info = inspect_wheel(archive)
        if normalized_name(name) != normalized_name(package["name"]) or version != package["version"]:
            raise StandaloneError("standalone_wheel_identity_mismatch")
        entries = archive.infolist()
        if not entries or len(entries) > MAX_MEMBERS:
            raise StandaloneError("standalone_wheel_invalid")
        total = 0
        for entry in entries:
            path = canonical_member_path(entry.filename.rstrip("/"))
            mode = zip_entry_mode(entry)
            kind = stat.S_IFMT(mode)
            if kind == stat.S_IFLNK:
                raise StandaloneError("standalone_archive_special_file")
            if entry.flag_bits & 0x1:
                raise StandaloneError("standalone_wheel_invalid")
            target_path = wheel_destination(path)
            target = ensure_destination(destination, target_path)
            folded = target_path.as_posix().casefold()
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if entry.file_size < 0 or entry.file_size > MAX_FILE_SIZE:
                raise StandaloneError("standalone_archive_too_large")
            total += entry.file_size
            if total > MAX_UNPACKED_SIZE:
                raise StandaloneError("standalone_archive_too_large")
            if folded in installed:
                raise StandaloneError("standalone_wheel_path_collision")
            installed[folded] = target_path.as_posix()
            try:
                with archive.open(entry) as payload:
                    write_regular_file(target, payload, mode, entry.file_size)
            except (OSError, RuntimeError, zipfile.BadZipFile):
                raise StandaloneError("standalone_wheel_invalid") from None
    return {"name": name, "version": version, "license": license_info}


def load_json(path: Path, code: str) -> tuple[dict, bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, ValueError):
        raise StandaloneError(code) from None
    if not isinstance(value, dict):
        raise StandaloneError(code)
    return value, raw


def load_pbs_asset(lock_path: Path, architecture: str) -> tuple[dict, dict, bytes]:
    lock, raw = load_json(lock_path, "standalone_pbs_lock_invalid")
    try:
        asset = lock["assets"][architecture]
        fields = (asset["filename"], asset["url"], asset["size"], asset["sha256"], asset["archive_root"])
    except (KeyError, TypeError):
        raise StandaloneError("standalone_pbs_lock_invalid") from None
    filename, url, size, digest, archive_root = fields
    parsed_url = urllib.parse.urlparse(url) if isinstance(url, str) else None
    if (lock.get("schema") != 1 or lock.get("provider") != "astral-sh/python-build-standalone"
            or not isinstance(lock.get("release"), str) or not isinstance(lock.get("python_version"), str)
            or not isinstance(lock.get("flavor"), str)
            or not isinstance(filename, str) or Path(filename).name != filename
            or parsed_url is None or parsed_url.scheme != "https" or not parsed_url.hostname
            or urllib.parse.unquote(Path(parsed_url.path).name) != filename
            or not isinstance(size, int) or size <= 0 or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
            or not isinstance(archive_root, str) or not archive_root):
        raise StandaloneError("standalone_pbs_lock_invalid")
    return lock, asset, raw


def verified_external_file(path: Path, *, size: int, digest: str, code: str) -> None:
    try:
        actual_size = path.stat().st_size
    except OSError:
        raise StandaloneError(code) from None
    if actual_size != size or sha256_file(path) != digest:
        raise StandaloneError(code)


def download_pbs(asset: dict, destination: Path) -> None:
    request = urllib.request.Request(asset["url"], headers={"User-Agent": "xianyu-saas-standalone-builder/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("xb") as target:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > asset["size"]:
                    raise StandaloneError("standalone_pbs_hash_mismatch")
                target.write(chunk)
    except StandaloneError:
        raise
    except (OSError, urllib.error.URLError):
        raise StandaloneError("standalone_pbs_download_failed") from None


def git_environment() -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}
    environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
    })
    return environment


def git(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(["git", *arguments], cwd=root, env=git_environment(), capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        raise StandaloneError("standalone_git_failed") from None
    if result.returncode != 0:
        raise StandaloneError("standalone_git_failed")
    return result.stdout


def prepare_source(args, temporary: Path) -> tuple[Path, str, int, str, int, str]:
    if not COMMIT_RE.fullmatch(args.commit):
        raise StandaloneError("standalone_commit_invalid")
    commit = args.commit.lower()
    archive = temporary / "source.tar"
    if args.source_archive:
        if not args.source_sha256 or not SHA256_RE.fullmatch(args.source_sha256) or args.source_date_epoch is None:
            raise StandaloneError("standalone_source_injection_invalid")
        source = Path(args.source_archive).resolve()
        try:
            size = source.stat().st_size
        except OSError:
            raise StandaloneError("standalone_source_archive_invalid") from None
        if sha256_file(source) != args.source_sha256:
            raise StandaloneError("standalone_source_hash_mismatch")
        shutil.copyfile(source, archive)
        return archive, commit, args.source_date_epoch, args.source_sha256, size, "injected_archive"
    if args.source_sha256 or args.source_date_epoch is not None:
        raise StandaloneError("standalone_source_injection_invalid")
    resolved = git(args.repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode("ascii", "strict").strip().lower()
    if resolved != commit:
        raise StandaloneError("standalone_commit_invalid")
    epoch_raw = git(args.repo, "show", "-s", "--format=%ct", commit).decode("ascii", "strict").strip()
    try:
        epoch = int(epoch_raw)
    except ValueError:
        raise StandaloneError("standalone_git_failed") from None
    try:
        with archive.open("xb") as target:
            result = subprocess.run(["git", "archive", "--format=tar", commit], cwd=args.repo, env=git_environment(), stdout=target, stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.SubprocessError):
        raise StandaloneError("standalone_git_failed") from None
    if result.returncode != 0:
        raise StandaloneError("standalone_git_failed")
    return archive, commit, epoch, sha256_file(archive), archive.stat().st_size, "git_archive"


def source_identity(release_root: Path) -> tuple[str, int]:
    try:
        package_version = json.loads((release_root / "package.json").read_bytes())["version"]
        source = (release_root / "backend" / "version.py").read_text(encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        raise StandaloneError("standalone_source_version_invalid") from None
    versions = re.findall(r'^VERSION\s*=\s*["\']([^"\']+)["\']\s*$', source, re.MULTILINE)
    data_versions = re.findall(r"^UPDATE_DATA_VERSION\s*=\s*([0-9]+)\s*$", source, re.MULTILINE)
    if (len(versions) != 1 or versions[0] != package_version or not VERSION_RE.fullmatch(package_version)
            or len(data_versions) != 1):
        raise StandaloneError("standalone_source_version_invalid")
    update_data_version = int(data_versions[0])
    if not 1 <= update_data_version <= 2**31 - 1:
        raise StandaloneError("standalone_source_version_invalid")
    return package_version, update_data_version


def select_wheels(lock_path: Path, app: Path, service: str, architecture: str, wheelhouse: Path, python_version: str) -> tuple[list[tuple[dict, dict, Path]], dict, bytes]:
    lock, raw = load_json(lock_path, "standalone_dependency_lock_invalid")
    if lock.get("status") != "locked":
        raise StandaloneError("standalone_dependency_lock_incomplete")
    if (lock.get("schema") != 1 or lock.get("kind") != "python-wheel-lock" or lock.get("service") != service
            or lock.get("python_version") != python_version or not isinstance(lock.get("packages"), list)):
        raise StandaloneError("standalone_dependency_lock_invalid")
    requirements = lock.get("requirements")
    expected_path = f"{service}/requirements.txt"
    if not isinstance(requirements, dict) or requirements.get("path") != expected_path or not SHA256_RE.fullmatch(str(requirements.get("sha256", ""))):
        raise StandaloneError("standalone_dependency_lock_invalid")
    requirement_file = app.joinpath(*PurePosixPath(expected_path).parts)
    try:
        requirement_digest = sha256_file(requirement_file)
    except OSError:
        raise StandaloneError("standalone_dependency_input_missing") from None
    if requirement_digest != requirements["sha256"]:
        raise StandaloneError("standalone_dependency_input_mismatch")
    selected = []
    names = set()
    for package in lock["packages"]:
        if not isinstance(package, dict):
            raise StandaloneError("standalone_dependency_lock_invalid")
        name, version, artifacts = package.get("name"), package.get("version"), package.get("artifacts")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version or not isinstance(artifacts, list):
            raise StandaloneError("standalone_dependency_lock_invalid")
        normalized = normalized_name(name)
        if normalized in names:
            raise StandaloneError("standalone_dependency_lock_invalid")
        names.add(normalized)
        candidates = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise StandaloneError("standalone_dependency_lock_invalid")
            architectures = artifact.get("architectures")
            filename, size, digest, url = (
                artifact.get("filename"), artifact.get("size"), artifact.get("sha256"), artifact.get("url")
            )
            parsed_url = urllib.parse.urlparse(url) if isinstance(url, str) else None
            if (not isinstance(architectures, list) or not architectures or not all(item in {"any", "x86_64", "aarch64"} for item in architectures)
                    or not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".whl")
                    or not isinstance(size, int) or size <= 0 or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
                    or parsed_url is None or parsed_url.scheme != "https" or parsed_url.hostname != "files.pythonhosted.org"
                    or urllib.parse.unquote(Path(parsed_url.path).name) != filename):
                raise StandaloneError("standalone_dependency_lock_invalid")
            if architecture in architectures or "any" in architectures:
                validate_wheel_filename(filename, architecture)
                candidates.append(artifact)
        if len(candidates) != 1:
            raise StandaloneError("standalone_dependency_artifact_missing")
        artifact = candidates[0]
        wheel = wheelhouse / artifact["filename"]
        verified_external_file(wheel, size=artifact["size"], digest=artifact["sha256"], code="standalone_dependency_hash_mismatch")
        selected.append((package, artifact, wheel))
    return selected, lock, raw


def install_dependencies(items: list[tuple[dict, dict, Path]], destination: Path) -> list[dict]:
    destination.mkdir(parents=True)
    installed_paths: dict[str, str] = {}
    components = []
    for package, artifact, wheel in items:
        metadata = extract_wheel(wheel, destination, package, installed_paths)
        components.append({
            "name": metadata["name"], "version": metadata["version"], "filename": artifact["filename"],
            "size": artifact["size"], "sha256": artifact["sha256"], "license": metadata["license"],
        })
    return components


def remove_caches(root: Path) -> None:
    directories = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for dirname in list(dirnames):
            path = current_path / dirname
            if path.is_symlink():
                raise StandaloneError("standalone_final_link_rejected")
            if dirname in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}:
                directories.append(path)
                dirnames.remove(dirname)
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink():
                raise StandaloneError("standalone_final_link_rejected")
            if path.suffix in {".pyc", ".pyo"}:
                path.unlink()
    for path in reversed(directories):
        shutil.rmtree(path)


def payload_digest(root: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    count = 0
    size = 0
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise StandaloneError("standalone_final_link_rejected")
        relative = path.relative_to(root).as_posix()
        file_size = path.stat().st_size
        file_hash = sha256_file(path)
        digest.update(f"{relative}\0{file_size}\0{file_hash}\n".encode("utf-8"))
        count += 1
        size += file_size
    return digest.hexdigest(), count, size


def architecture_default() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x86_64"
    if machine in {"aarch64", "arm64"}:
        return "aarch64"
    raise StandaloneError("standalone_architecture_unsupported")


def write_metadata(runtime: Path, *, version: str, commit: str, architecture: str, epoch: int,
                   update_data_version: int, pbs_lock: dict, pbs_asset: dict,
                   dependency_data: dict, tree: tuple[str, int, int]) -> None:
    created = datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")
    tree_hash, file_count, total_size = tree
    packages = dependency_data["backend"]["components"] + dependency_data["worker"]["components"]
    uv_versions = {
        data["lock"].get("generator", {}).get("required_version")
        for data in dependency_data.values()
    }
    if len(uv_versions) != 1 or not all(isinstance(value, str) and value for value in uv_versions):
        raise StandaloneError("standalone_dependency_lock_invalid")
    uv_version = uv_versions.pop()
    python_build = f'{pbs_lock["python_version"]}+{pbs_lock["release"]}-{pbs_lock["flavor"]}'
    runtime_manifest = {
        "schema": 1,
        "version": version,
        "commit": commit,
        "platform": "linux",
        "architecture": architecture,
        "target": f"linux-{architecture}",
        "python_version": pbs_lock["python_version"],
        "python_build": python_build,
        "uv_version": uv_version,
        "manager_protocol": 1,
        "update_data_version": update_data_version,
        "backend_lock_sha256": dependency_data["backend"]["lock_sha256"],
        "worker_lock_sha256": dependency_data["worker"]["lock_sha256"],
    }
    sbom_components = [{
        "type": "application", "name": "xianyu-saas", "version": version,
        "bom-ref": f"pkg:generic/xianyu-saas@{version}?commit={commit}",
    }, {
        "type": "framework", "name": "CPython", "version": pbs_lock["python_version"],
        "bom-ref": f"pkg:generic/cpython@{pbs_lock['python_version']}?download_url={urllib.parse.quote(pbs_asset['url'], safe='')}",
        "hashes": [{"alg": "SHA-256", "content": pbs_asset["sha256"]}],
    }]
    for component in packages:
        item = {
            "type": "library", "name": component["name"], "version": component["version"],
            "bom-ref": f"pkg:pypi/{normalized_name(component['name'])}@{component['version']}",
            "hashes": [{"alg": "SHA-256", "content": component["sha256"]}],
        }
        if component["license"]["declared"]:
            item["licenses"] = [{"license": {"name": component["license"]["declared"]}}]
        sbom_components.append(item)
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"xianyu-saas:{version}:{commit}:{architecture}:{tree_hash}")
    sbom = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": f"urn:uuid:{serial}", "version": 1,
        "metadata": {"timestamp": created, "component": sbom_components[0], "tools": {"components": [{"type": "application", "name": "build-standalone.py"}]}},
        "components": sbom_components[1:],
    }
    third_party = {
        "schema": 1,
        "python": {"provider": pbs_lock["provider"], "release": pbs_lock["release"], "version": pbs_lock["python_version"],
                   "url": pbs_asset["url"], "sha256": pbs_asset["sha256"]},
        "packages": packages,
    }
    (runtime / "runtime.json").write_bytes(json_bytes(runtime_manifest))
    (runtime / "sbom.cdx.json").write_bytes(json_bytes(sbom))
    (runtime / "third-party.json").write_bytes(json_bytes(third_party))


def install_manager(source: Path, destination: Path) -> None:
    try:
        metadata = source.lstat()
    except OSError:
        raise StandaloneError("standalone_manager_missing") from None
    if source.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 512 * 1024 * 1024:
        raise StandaloneError("standalone_manager_invalid")
    destination.parent.mkdir(parents=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o755)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--architecture", choices=("x86_64", "aarch64"))
    parser.add_argument("--pbs-lock", type=Path, default=RUNTIME_INPUTS / "python-build-standalone.lock.json")
    parser.add_argument("--pbs-archive", type=Path)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--manager-binary", type=Path, required=True)
    parser.add_argument("--backend-lock", type=Path, default=RUNTIME_INPUTS / "backend.lock.json")
    parser.add_argument("--worker-lock", type=Path, default=RUNTIME_INPUTS / "worker.lock.json")
    parser.add_argument("--source-archive", type=Path)
    parser.add_argument("--source-sha256")
    parser.add_argument("--source-date-epoch", type=int)
    return parser.parse_args(argv)


def build(args) -> Path:
    args.repo = args.repo.resolve()
    architecture = args.architecture or architecture_default()
    local_root = args.repo / ".local"
    if local_root.is_symlink():
        raise StandaloneError("standalone_output_invalid")
    local_root.mkdir(parents=True, exist_ok=True)
    pbs_lock, pbs_asset, pbs_lock_raw = load_pbs_asset(args.pbs_lock.resolve(), architecture)
    with tempfile.TemporaryDirectory(prefix=".standalone-work-", dir=local_root) as work_raw:
        work = Path(work_raw)
        source_archive, commit, epoch, source_hash, source_size, source_mode = prepare_source(args, work)
        if epoch < 0 or epoch > 4294967295:
            raise StandaloneError("standalone_source_date_invalid")
        source_root = work / "source"
        source_root.mkdir()
        safe_extract_tar(source_archive, source_root)
        version, update_data_version = source_identity(source_root)
        bundle = work / "bundle"
        bundle.mkdir()
        for required in ("backend", "frontend", "worker"):
            source_directory = source_root / required
            if not source_directory.is_dir() or source_directory.is_symlink():
                raise StandaloneError("standalone_source_layout_invalid")
            shutil.copytree(source_directory, bundle / required, symlinks=False)
        worker_env_example = bundle / "worker" / ".env.example"
        if worker_env_example.exists():
            if worker_env_example.is_symlink() or not worker_env_example.is_file():
                raise StandaloneError("standalone_source_layout_invalid")
            worker_env_example.unlink()
        shutil.copyfile(source_root / "package.json", bundle / "package.json")
        runtime = bundle / "runtime"
        runtime.mkdir()
        install_manager(args.manager_binary.resolve(), bundle / "manager" / "xianyu-saas")
        output = args.output or (local_root / "standalone" / f"{version}-{commit[:12]}-{architecture}")
        output = output.resolve(strict=False)
        try:
            output.relative_to(local_root.resolve())
        except ValueError:
            raise StandaloneError("standalone_output_invalid") from None
        if output.exists() or output.is_symlink():
            raise StandaloneError("standalone_output_exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        pbs_archive = args.pbs_archive.resolve() if args.pbs_archive else work / pbs_asset["filename"]
        if args.pbs_archive is None:
            download_pbs(pbs_asset, pbs_archive)
        verified_external_file(pbs_archive, size=pbs_asset["size"], digest=pbs_asset["sha256"], code="standalone_pbs_hash_mismatch")
        python_root = runtime / "python"
        python_root.mkdir()
        extracted = safe_extract_tar(
            pbs_archive,
            python_root,
            required_root=pbs_asset["archive_root"],
            materialize_symlinks=True,
        )
        if not any(path in {"bin/python", "bin/python3", "python.exe"} or re.fullmatch(r"bin/python3\.[0-9]+", path) for path in extracted):
            raise StandaloneError("standalone_python_tree_invalid")
        dependency_data = {}
        for service, lock_path in (("backend", args.backend_lock), ("worker", args.worker_lock)):
            selected, lock, lock_raw = select_wheels(lock_path.resolve(), bundle, service, architecture, args.wheelhouse.resolve(), pbs_lock["python_version"])
            components = install_dependencies(selected, runtime / "site" / service)
            dependency_data[service] = {"lock": lock, "lock_sha256": hashlib.sha256(lock_raw).hexdigest(), "components": components}
        remove_caches(bundle)
        tree = payload_digest(bundle)
        write_metadata(
            runtime, version=version, commit=commit, architecture=architecture, epoch=epoch,
            update_data_version=update_data_version, pbs_lock=pbs_lock, pbs_asset=pbs_asset,
            dependency_data=dependency_data, tree=tree,
        )
        remove_caches(bundle)
        payload_digest(bundle)
        try:
            os.replace(bundle, output)
        except OSError:
            raise StandaloneError("standalone_atomic_publish_failed") from None
        return output


def main(argv=None) -> int:
    try:
        output = build(parse_args(argv))
        print(json.dumps({"output": str(output)}, sort_keys=True))
        return 0
    except StandaloneError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("standalone_build_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
