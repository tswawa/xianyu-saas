#!/usr/bin/env python3
"""Offline contract for the fail-closed standalone runtime builder."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-standalone.py"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
VERSION = "9.8.7"
EPOCH = 1789257600
sys.dont_write_bytecode = True


def load_builder():
    spec = importlib.util.spec_from_file_location("standalone_builder_contract", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUILDER = load_builder()
sys.path.insert(0, str(ROOT / "backend"))
import standalone_runtime as RUNTIME


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(BUILDER.json_bytes(value))


def make_tar(path: Path, files: dict[str, bytes], *, kinds: dict[str, tuple] | None = None) -> None:
    kinds = kinds or {}
    with tarfile.open(path, "w:gz") as archive:
        directories = set()
        for name in files:
            parts = Path(name).parts[:-1]
            for index in range(1, len(parts) + 1):
                directories.add("/".join(parts[:index]))
        for name in sorted(directories):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.mode = 0o755 if name.endswith(("python", "python3")) else 0o644
            if name in kinds:
                info.type, info.linkname, info.devmajor, info.devminor = kinds[name]
                info.size = 0
                archive.addfile(info)
            else:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))


def make_wheel(path: Path, name: str, version: str, module: str) -> None:
    distribution = name.replace("-", "_")
    dist_info = f"{distribution}-{version}.dist-info"
    entries = {
        f"{module}/__init__.py": f'VERSION = "{version}"\n'.encode(),
        f"{dist_info}/METADATA": f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\nLicense-Expression: MIT\n\nfixture\n".encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: standalone-contract\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist_info}/RECORD": b"",
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, payload in entries.items():
            info = zipfile.ZipInfo(filename)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload)


def fixture(run: Path):
    repo = run / "repo"
    repo.mkdir(parents=True)
    source = run / "source.tar.gz"
    backend_requirements = b"backend-dep==1.0.0\n"
    worker_requirements = b"worker-dep==2.0.0\n"
    make_tar(source, {
        "package.json": BUILDER.json_bytes({"name": "fixture", "version": VERSION}),
        "backend/version.py": f'VERSION = "{VERSION}"\nUPDATE_DATA_VERSION = 1\n'.encode(),
        "backend/requirements.txt": backend_requirements,
        "backend/main.py": b"VALUE = 1\n",
        "backend/__pycache__/old.cpython-312.pyc": b"cache",
        "frontend/index.html": b"<!doctype html><title>fixture</title>\n",
        "docker/launcher.sh": b"#!/bin/sh\nexit 0\n",
        "worker/requirements.txt": worker_requirements,
        "worker/main.py": b"VALUE = 2\n",
        "worker/.env.example": b"COOKIES_STR=\nAPI_KEY=\n",
        "worker/stale.pyc": b"cache",
    })
    pbs = run / "python.tar.gz"
    make_tar(
        pbs,
        {
            "python/bin/python3.12": b"#!/bin/sh\nexit 0\n",
            "python/bin/python3": b"",
            "python/lib/python3.12/os.py": b"name = 'posix'\n",
            "python/lib/python3.12/__pycache__/os.cpython-312.pyc": b"cache",
            "python/share/terminfo/61/ansi": b"terminal database fixture\n",
        },
        kinds={"python/bin/python3": (tarfile.SYMTYPE, "python3.12", 0, 0)},
    )
    wheelhouse = run / "wheelhouse"
    wheelhouse.mkdir()
    manager = run / "xianyu-saas-manager"
    manager.write_bytes(b"#!/bin/sh\nexit 0\n")
    manager.chmod(0o755)
    backend_wheel = wheelhouse / "backend_dep-1.0.0-py3-none-any.whl"
    worker_wheel = wheelhouse / "worker_dep-2.0.0-py3-none-any.whl"
    make_wheel(backend_wheel, "backend-dep", "1.0.0", "backend_dep")
    make_wheel(worker_wheel, "worker-dep", "2.0.0", "worker_dep")
    pbs_lock = run / "pbs.lock.json"
    write_json(pbs_lock, {
        "schema": 1,
        "provider": "astral-sh/python-build-standalone",
        "release": "contract",
        "python_version": "3.12.14",
        "flavor": "install_only_stripped",
        "assets": {"x86_64": {
            "filename": pbs.name,
            "url": f"https://example.invalid/{pbs.name}",
            "size": pbs.stat().st_size,
            "sha256": digest(pbs),
            "archive_root": "python",
        }},
    })

    def dependency_lock(service: str, requirements: bytes, wheel: Path, name: str, version: str):
        return {
            "schema": 1,
            "kind": "python-wheel-lock",
            "service": service,
            "python_version": "3.12.14",
            "requirements": {"path": f"{service}/requirements.txt", "sha256": hashlib.sha256(requirements).hexdigest()},
            "status": "locked",
            "generator": {"name": "uv", "required_version": "0.12.13", "result": "synthetic"},
            "packages": [{
                "name": name,
                "version": version,
                "artifacts": [{
                    "filename": wheel.name,
                    "size": wheel.stat().st_size,
                    "sha256": digest(wheel),
                    "url": f"https://files.pythonhosted.org/packages/fixture/{wheel.name}",
                    "architectures": ["any"],
                }],
            }],
        }

    backend_lock = run / "backend.lock.json"
    worker_lock = run / "worker.lock.json"
    write_json(backend_lock, dependency_lock("backend", backend_requirements, backend_wheel, "backend-dep", "1.0.0"))
    write_json(worker_lock, dependency_lock("worker", worker_requirements, worker_wheel, "worker-dep", "2.0.0"))
    return repo, source, pbs, wheelhouse, manager, pbs_lock, backend_lock, worker_lock


def command_for(repo: Path, source: Path, pbs: Path, wheelhouse: Path, manager: Path, pbs_lock: Path, backend_lock: Path, worker_lock: Path, output: Path):
    return [
        sys.executable, "-B", str(SCRIPT),
        "--repo", str(repo),
        "--commit", COMMIT,
        "--architecture", "x86_64",
        "--output", str(output),
        "--source-archive", str(source),
        "--source-sha256", digest(source),
        "--source-date-epoch", str(EPOCH),
        "--pbs-archive", str(pbs),
        "--pbs-lock", str(pbs_lock),
        "--wheelhouse", str(wheelhouse),
        "--manager-binary", str(manager),
        "--backend-lock", str(backend_lock),
        "--worker-lock", str(worker_lock),
    ]


def run_command(command):
    environment = dict(os.environ)
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})
    return subprocess.run(command, capture_output=True, env=environment, timeout=30)


def assert_error(result, code: str) -> None:
    assert result.returncode != 0
    assert result.stderr.decode().strip() == code, result.stderr.decode()


def success_contract(run: Path) -> None:
    repo, source, pbs, wheelhouse, manager, pbs_lock, backend_lock, worker_lock = fixture(run / "success")
    output = repo / ".local" / "standalone-fixture"
    result = run_command(command_for(repo, source, pbs, wheelhouse, manager, pbs_lock, backend_lock, worker_lock, output))
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout)["output"] == str(output.resolve())
    assert (output / "backend/main.py").is_file()
    assert (output / "frontend/index.html").is_file()
    assert (output / "worker/main.py").is_file()
    assert (output / "docker/launcher.sh").read_bytes() == b"#!/bin/sh\nexit 0\n"
    if os.name == "posix":
        assert stat.S_IMODE((output / "docker/launcher.sh").stat().st_mode) == 0o755
    assert not (output / "worker/.env.example").exists()
    assert not (output / "app").exists()
    assert (output / "runtime/python/bin/python3").is_file()
    assert (output / "runtime/python/bin/python3").read_bytes() == (output / "runtime/python/bin/python3.12").read_bytes()
    assert not any(path.name.casefold() == "terminfo" for path in (output / "runtime/python").rglob("*"))
    assert (output / "runtime/site/backend/backend_dep/__init__.py").is_file()
    assert (output / "runtime/site/worker/worker_dep/__init__.py").is_file()
    assert (output / "manager/xianyu-saas").read_bytes() == manager.read_bytes()
    assert not list(output.rglob("*.pyc"))
    assert not any(path.name == "__pycache__" for path in output.rglob("*"))
    assert not any(path.is_symlink() for path in output.rglob("*"))
    runtime_raw = (output / "runtime/runtime.json").read_bytes()
    runtime = json.loads(runtime_raw)
    assert set(runtime) == {
        "schema", "version", "commit", "platform", "architecture", "target",
        "python_version", "python_build", "uv_version", "manager_protocol",
        "update_data_version", "backend_lock_sha256", "worker_lock_sha256",
    }
    parsed = RUNTIME.parse_runtime_metadata(runtime_raw, expected_version=VERSION, expected_architecture="x86_64")
    assert parsed.commit == COMMIT and parsed.target == "linux-x86_64"
    assert parsed.python_version == "3.12.14" and parsed.python_build == "3.12.14+contract-install_only_stripped"
    assert parsed.uv_version == "0.12.13" and parsed.manager_protocol == 1 and parsed.update_data_version == 1
    assert parsed.backend_lock_sha256 == digest(backend_lock)
    assert parsed.worker_lock_sha256 == digest(worker_lock)
    sbom = json.loads((output / "runtime/sbom.cdx.json").read_bytes())
    assert sbom["bomFormat"] == "CycloneDX" and sbom["specVersion"] == "1.5"
    assert {item["name"] for item in sbom["components"]} == {"CPython", "backend-dep", "worker-dep"}
    third_party = json.loads((output / "runtime/third-party.json").read_bytes())
    assert {item["name"] for item in third_party["packages"]} == {"backend-dep", "worker-dep"}
    print("standalone build: offline paths, hashes, metadata and link-free tree passed")


def extraction_security_contract(run: Path) -> None:
    cases = {
        "traversal": ("../outside", b"escape", None, "standalone_archive_path_invalid"),
        "symlink": ("python/link", b"", (tarfile.SYMTYPE, "../outside", 0, 0), "standalone_archive_special_file"),
        "device": ("python/device", b"", (tarfile.CHRTYPE, "", 1, 3), "standalone_archive_special_file"),
    }
    for label, (name, payload, kind, expected) in cases.items():
        archive = run / f"{label}.tar.gz"
        make_tar(archive, {name: payload}, kinds={name: kind} if kind else None)
        destination = run / f"extract-{label}"
        destination.mkdir()
        try:
            BUILDER.safe_extract_tar(archive, destination)
        except BUILDER.StandaloneError as exc:
            assert str(exc) == expected
        else:
            raise AssertionError(f"unsafe tar accepted: {label}")
        assert not (run / "outside").exists()

    materialized = run / "materialized.tar.gz"
    make_tar(
        materialized,
        {"python/bin/python3.12": b"runtime", "python/bin/python3": b""},
        kinds={"python/bin/python3": (tarfile.SYMTYPE, "python3.12", 0, 0)},
    )
    materialized_root = run / "materialized"
    materialized_root.mkdir()
    BUILDER.safe_extract_tar(
        materialized,
        materialized_root,
        required_root="python",
        materialize_symlinks=True,
    )
    assert (materialized_root / "bin/python3").read_bytes() == b"runtime"
    assert not (materialized_root / "bin/python3").is_symlink()

    escaping = run / "materialized-escape.tar.gz"
    make_tar(
        escaping,
        {"python/bin/python3.12": b"runtime", "python/bin/python3": b""},
        kinds={"python/bin/python3": (tarfile.SYMTYPE, "../../outside", 0, 0)},
    )
    escaping_root = run / "materialized-escape"
    escaping_root.mkdir()
    try:
        BUILDER.safe_extract_tar(
            escaping,
            escaping_root,
            required_root="python",
            materialize_symlinks=True,
        )
    except BUILDER.StandaloneError as exc:
        assert str(exc) == "standalone_archive_link_invalid"
    else:
        raise AssertionError("escaping materialized symlink accepted")
    assert not (run / "outside").exists()

    for label, payload, expected_size in (("short", b"x", 2), ("long", b"xx", 1)):
        destination = run / f"bounded-{label}"
        try:
            BUILDER.write_regular_file(destination, io.BytesIO(payload), 0o644, expected_size)
        except BUILDER.StandaloneError as exc:
            assert str(exc) == "standalone_archive_size_mismatch"
        else:
            raise AssertionError(f"{label} archive stream size mismatch accepted")
        assert not destination.exists()

    wheel = run / "linked-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        metadata = zipfile.ZipInfo("linked-1.0.0.dist-info/METADATA")
        metadata.create_system = 3
        metadata.external_attr = (stat.S_IFREG | 0o644) << 16
        archive.writestr(metadata, b"Metadata-Version: 2.3\nName: linked\nVersion: 1.0.0\n")
        link = zipfile.ZipInfo("linked/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, b"../../outside")
    try:
        BUILDER.extract_wheel(wheel, run / "wheel-site", {"name": "linked", "version": "1.0.0"}, {})
    except BUILDER.StandaloneError as exc:
        assert str(exc) == "standalone_archive_special_file"
    else:
        raise AssertionError("wheel symlink accepted")
    print("standalone build: traversal, links and device members rejected")


def fail_closed_and_atomic_contract(run: Path) -> None:
    repo, source, pbs, wheelhouse, manager, pbs_lock, backend_lock, worker_lock = fixture(run / "failure")
    output = repo / ".local" / "failed"
    bad_lock = json.loads(pbs_lock.read_bytes())
    bad_lock["assets"]["x86_64"]["sha256"] = "0" * 64
    write_json(pbs_lock, bad_lock)
    result = run_command(command_for(repo, source, pbs, wheelhouse, manager, pbs_lock, backend_lock, worker_lock, output))
    assert_error(result, "standalone_pbs_hash_mismatch")
    assert not output.exists()
    assert not list((repo / ".local").glob(".standalone-work-*"))

    pbs_lock.unlink()
    write_json(pbs_lock, {
        "schema": 1, "provider": "astral-sh/python-build-standalone", "release": "contract",
        "python_version": "3.12.14", "flavor": "install_only_stripped",
        "assets": {"x86_64": {"filename": pbs.name, "url": f"https://example.invalid/{pbs.name}",
                    "size": pbs.stat().st_size, "sha256": digest(pbs), "archive_root": "python"}},
    })
    backend_wheel = wheelhouse / "backend_dep-1.0.0-py3-none-any.whl"
    original_wheel = backend_wheel.read_bytes()
    backend_wheel.write_bytes(original_wheel + b"tampered")
    dependency_output = repo / ".local" / "dependency-hash-failed"
    result = run_command(command_for(repo, source, pbs, wheelhouse, manager, pbs_lock, backend_lock, worker_lock, dependency_output))
    assert_error(result, "standalone_dependency_hash_mismatch")
    assert not dependency_output.exists()
    backend_wheel.write_bytes(original_wheel)
    assert not list((repo / ".local").glob(".standalone-work-*"))

    existing = repo / ".local" / "existing"
    existing.mkdir()
    sentinel = existing / "sentinel"
    sentinel.write_bytes(b"keep")
    result = run_command(command_for(repo, source, pbs, wheelhouse, manager, pbs_lock, backend_lock, worker_lock, existing))
    assert_error(result, "standalone_output_exists")
    assert sentinel.read_bytes() == b"keep"
    assert not list((repo / ".local").glob(".standalone-work-*"))

    incomplete_backend = repo / ".local" / "backend.incomplete.json"
    incomplete_lock = json.loads(backend_lock.read_bytes())
    incomplete_lock["status"] = "incomplete"
    incomplete_lock["generator"]["result"] = "not-generated"
    write_json(incomplete_backend, incomplete_lock)
    incomplete_output = repo / ".local" / "incomplete"
    command = command_for(repo, source, pbs, wheelhouse, manager, pbs_lock, incomplete_backend, worker_lock, incomplete_output)
    result = run_command(command)
    assert_error(result, "standalone_dependency_lock_incomplete")
    assert not incomplete_output.exists()
    assert not list((repo / ".local").glob(".standalone-work-*"))
    print("standalone build: verified hash failure, incomplete locks and existing outputs fail atomically")


def official_lock_contract() -> None:
    for service in ("backend", "worker"):
        dependency_lock = json.loads((ROOT / f"deploy/runtime/{service}.lock.json").read_bytes())
        assert dependency_lock["requirements"]["sha256"] == digest(ROOT / service / "requirements.txt")
        assert dependency_lock["status"] == "locked"
        assert dependency_lock["generator"] == {
            "name": "uv",
            "required_version": "0.12.13",
            "result": "locked",
            "exclude_newer": "2026-09-13T00:00:00Z",
            "only_binary": True,
            "python_platforms": ["x86_64-manylinux_2_17", "aarch64-manylinux_2_17"],
        }
        for package in dependency_lock["packages"]:
            assert package["artifacts"]
            for artifact in package["artifacts"]:
                assert artifact["url"].startswith("https://files.pythonhosted.org/")
                assert artifact["url"].endswith("/" + artifact["filename"])
                assert artifact["size"] > 0 and len(artifact["sha256"]) == 64
            for architecture in ("x86_64", "aarch64"):
                selected = [artifact for artifact in package["artifacts"] if architecture in artifact["architectures"] or "any" in artifact["architectures"]]
                assert len(selected) == 1
    lock = json.loads((ROOT / "deploy/runtime/python-build-standalone.lock.json").read_bytes())
    assert lock["release"] == "20260901" and lock["python_version"] == "3.12.14"
    expected = {
        "x86_64": (34143368, "72748da13197c1fb161e3afeef20a6a385ff24f2165e6e2758e47008e7faba4c"),
        "aarch64": (29199399, "577b4bec0793ad1ff0cbff9adbd0df078eddde38a4c41bf5d83ad381a85ee39d"),
    }
    for architecture, (size, sha256) in expected.items():
        asset = lock["assets"][architecture]
        assert asset["size"] == size and asset["sha256"] == sha256
        assert "3.12.14+20260901" in asset["filename"] and "install_only_stripped" in asset["filename"]
    print("standalone build: official PBS version, sizes and SHA-256 pins passed")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="xianyu-standalone-contract-") as temporary:
        run = Path(temporary)
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
            success_contract(run)
            extraction_security_contract(run)
            fail_closed_and_atomic_contract(run)
    official_lock_contract()
    print("standalone build contract: ok (small synthetic archives and wheels only)")


if __name__ == "__main__":
    main()
