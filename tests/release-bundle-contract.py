#!/usr/bin/env python3
"""Offline release-builder contract; all Git writes and keys live in temporary repos."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import types
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-release.py"
VERIFY_SCRIPT = ROOT / "scripts" / "verify-public-release.py"
VERSION = "0.2.0"
EPOCH = 1700000000
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import docker_update_protocol as DOCKER
import standalone_runtime as STANDALONE
from deploy.manager import installer as MANAGER_INSTALLER


def load_builder():
    spec = importlib.util.spec_from_file_location("release_bundle_builder_contract", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUILDER = load_builder()


def load_verifier():
    spec = importlib.util.spec_from_file_location("public_release_verifier_contract", VERIFY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VERIFIER = load_verifier()
UPDATER_SOURCE = (ROOT / "backend" / "platform_update.py").read_bytes()


def load_real_updater():
    # The entire real updater is executed, not an extracted or reimplemented validator.
    # Only its version import is isolated from machine build-info and Linux-only locks
    # receive an import shim on Windows. No tested operation calls flock.
    version = types.ModuleType("version")
    version.VERSION = "0.1.0"
    version.RELEASE_CHANNEL = "release"
    version.deployment_kind = lambda: "source"
    replacements = {"version": version}
    try:
        import fcntl  # noqa: F401
    except ImportError:
        locks = types.ModuleType("fcntl")

        def unsupported_lock(*_args, **_kwargs):
            raise AssertionError("the release contract must not use Linux locks")

        locks.flock = unsupported_lock
        replacements["fcntl"] = locks
    name = "real_release_bundle_updater_contract"
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / "backend" / "platform_update.py")
    sys.modules[name] = module
    with patch.dict(sys.modules, replacements):
        exec(compile(UPDATER_SOURCE, module.__file__, "exec"), module.__dict__)
    return module


UPDATER = load_real_updater()


def key_material():
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, seed, base64.b64encode(seed).decode("ascii"), public


def clean_environment() -> dict:
    env = BUILDER.git_environment()
    env.update({
        "GIT_AUTHOR_NAME": "Release Contract", "GIT_AUTHOR_EMAIL": "release-contract@example.invalid",
        "GIT_COMMITTER_NAME": "Release Contract", "GIT_COMMITTER_EMAIL": "release-contract@example.invalid",
        "GIT_AUTHOR_DATE": f"@{EPOCH} +0000", "GIT_COMMITTER_DATE": f"@{EPOCH} +0000",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
    })
    return env


class Repository:
    def __init__(self, directory: Path, *, version=VERSION, public_pem=False):
        self.root = directory
        self.root.mkdir()
        self.version = version
        self.build_count = 0
        self.key, self.seed, self.encoded, self.public = key_material()
        self.git("init", "--quiet")
        public = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo) if public_pem else base64.b64encode(self.public) + b"\n"
        self.files = {
            ".gitignore": b".local/\nworker/.env\n*.db\n",
            ".gitattributes": b"* -text\n",
            ".editorconfig": b"root = true\n",
            ".dockerignore": b".git\n.local\n",
            ".github/workflows/ci.yml": b"name: offline fixture\n",
            "package.json": BUILDER.json_bytes({"name": "xianyu-saas", "version": version, "private": True}),
            "package-lock.json": BUILDER.json_bytes({"name": "xianyu-saas", "version": version, "lockfileVersion": 3, "packages": {"": {"name": "xianyu-saas", "version": version}}}),
            "backend/version.py": f'VERSION = "{version}"\nASSET_VERSION = "contract"\nUPDATE_DATA_VERSION = 1\n'.encode(),
            "backend/platform_update.py": UPDATER_SOURCE,
            "backend/docker_update_protocol.py": (ROOT / "backend/docker_update_protocol.py").read_bytes(),
            "backend/requirements.txt": b"requests\ncryptography\n",
            "backend/example.py": b"VALUE = 42\n",
            "frontend/index.html": b"<!doctype html><title>fixture</title>\n",
            "frontend/assets/font.woff2": b"fixture-font\x00\xff\x01",
            "frontend/assets/OFL-NotoSansSC.txt": b"SIL OPEN FONT LICENSE fixture\n",
            "LICENSE": b"GNU GENERAL PUBLIC LICENSE fixture\n",
            "worker/LICENSE": b"GNU GENERAL PUBLIC LICENSE worker fixture\n",
            "worker/NOTICE.md": b"GPL-3.0 upstream notice fixture\n",
            "worker/products_config.json": b'{"types": []}\n',
            "worker/.env.example": b"COOKIES_STR=\nAPI_KEY=\n",
            "config/saas.env.example": b"SAAS_ADMIN_TOKEN=\n",
            "README.md": b"Source deployment fixture.\n",
            "SECURITY.md": b"Report fixture issues.\n",
            "LICENSING.md": b"GPL and font license boundary fixture.\n",
            "CONTRIBUTING.md": b"Contribution fixture.\n",
            "CHANGELOG.md": f"# Changes\n\n## [Unreleased]\n\nDo not publish this.\n\n## [{version}] - 2023-11-14\n\n### Added\n\n- Release fixture only.\n\n## [0.0.1]\n\nOld notes.\n".encode(),
            "Dockerfile": b"FROM scratch\nCOPY . /app\n",
            "docker-compose.yml": b"services: {}\n",
            "docker-compose.updates.yml": b"services: {}\n",
            "deploy/docker-install.sh": b"#!/bin/sh\n# fixture docker installer\nexit 0\n",
            "config/saas.env.docker.example": b"SAAS_ENV=production\n",
            "docker/entrypoint.sh": b"#!/bin/sh\nexit 0\n",
            "docs/DEPLOYMENT.md": b"Docker and manual deployment fixture.\n",
            "docs/assets/readme/orders.png": b"public documentation screenshot fixture\n",
            "docs/说明.md": "说明样例\n".encode(),
            "deploy/update-signing.pub": public,
            "deploy/runtime/backend.lock.json": b"{}\n",
            "deploy/runtime/python-build-standalone.lock.json": b"{}\n",
            "deploy/runtime/worker.lock.json": b"{}\n",
            "scripts/check.sh": b"#!/bin/sh\nexit 0\n",
            "tests/test_private_key.py": b"# Test code may discuss private keys without containing any.\n",
        }
        for name, payload in self.files.items():
            self.write(name, payload)
        executables = ("scripts/check.sh", "docker/entrypoint.sh")
        # --chmod changes only the index. POSIX worktree modes must match it;
        # Windows still needs the explicit index flag to preserve executable bits.
        for name in executables:
            (self.root / name).chmod(0o755)
        self.git("add", "--all")
        self.git("add", "--chmod=+x", "--", *executables)
        self.commit = self.commit_changes()
        assert not self.git("status", "--porcelain=v1", "--untracked-files=no"), "new release fixture must have a clean tracked worktree"

    def git(self, *args: str, data=None):
        result = subprocess.run(["git", *args], cwd=self.root, input=data, capture_output=True, env=clean_environment(), timeout=30)
        assert result.returncode == 0, (args, result.stderr.decode("utf-8", "replace"))
        return result.stdout

    def write(self, name: str, payload: bytes):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def commit_changes(self):
        self.git("commit", "--quiet", "-m", "Record isolated release fixture")
        return self.git("rev-parse", "HEAD").decode().strip()

    def change(self, name: str, payload: bytes):
        self.write(name, payload)
        self.git("add", "--force", "--", name)
        self.commit = self.commit_changes()

    def standalone_inputs(self, ref: str) -> Path:
        self.build_count += 1
        commit = self.git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
        root = self.root / ".local" / "standalone-inputs" / str(self.build_count)
        for architecture in ("x86_64", "aarch64"):
            target = f"linux-{architecture}"
            directory = root / target
            bundle = directory / "bundle"
            manager = b"#!/bin/sh\n# synthetic manager " + target.encode() + b"\nexit 0\n"
            runtime = {
                "schema": 1,
                "version": self.version,
                "commit": commit,
                "platform": "linux",
                "architecture": architecture,
                "target": target,
                "python_version": "3.12.14",
                "python_build": "3.12.14+contract-install_only_stripped",
                "uv_version": "0.12.13",
                "manager_protocol": 1,
                "update_data_version": 1,
                "backend_lock_sha256": hashlib.sha256(b"backend-lock").hexdigest(),
                "worker_lock_sha256": hashlib.sha256(b"worker-lock").hexdigest(),
            }
            payloads = {
                "package.json": BUILDER.json_bytes({"name": "xianyu-saas", "version": self.version}),
                "backend/version.py": f'VERSION = "{self.version}"\nUPDATE_DATA_VERSION = 1\n'.encode(),
                "backend/main.py": b"BACKEND = True\n",
                "frontend/index.html": b"<!doctype html><title>standalone</title>\n",
                "worker/main.py": b"WORKER = True\n",
                "runtime/python/bin/python3": b"#!/bin/sh\nexit 0\n",
                "runtime/python/lib/python3.12/site-packages/pip/_vendor/packaging/licenses/_spdx.py": b'EXCEPTIONS = {"sk-linking-protocols-exception": 0}\n',
                "runtime/site/backend/fixture.py": b"BACKEND_DEP = True\n",
                "runtime/site/worker/fixture.py": b"WORKER_DEP = True\n",
                "runtime/runtime.json": BUILDER.json_bytes(runtime),
                "runtime/sbom.cdx.json": BUILDER.json_bytes({"bomFormat": "CycloneDX", "specVersion": "1.5"}),
                "runtime/third-party.json": BUILDER.json_bytes({"schema": 1, "packages": []}),
                "manager/xianyu-saas": manager,
                "docker/launcher.sh": b"#!/bin/sh\n# synthetic launcher\nexit 0\n",
            }
            for name, payload in payloads.items():
                path = bundle / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                if name in {"manager/xianyu-saas", "docker/launcher.sh"}:
                    path.chmod(0o755)
            bootstrap = directory / "manager"
            bootstrap.write_bytes(manager)
            bootstrap.chmod(0o755)
        return root

    def build(self, label="bundle", *, ref="HEAD", changes=None, extra=(), default_output=False):
        output = self.root / ".local" / "releases" / (self.version if default_output else label)
        inputs = self.standalone_inputs(ref)
        command = [sys.executable, "-B", str(SCRIPT), "--ref", ref, "--standalone-input-root", str(inputs)]
        if not default_output:
            command.extend(["--output", str(output)])
        command.extend(extra)
        env = clean_environment()
        env["RELEASE_SIGNING_KEY"] = self.encoded
        env["UNRELATED_ENV_SECRET"] = "not-an-asset-" + self.encoded
        for name, value in (changes or {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        result = subprocess.run(command, cwd=self.root, env=env, capture_output=True, timeout=60)
        for secret in (self.encoded.encode(), self.seed, env["UNRELATED_ENV_SECRET"].encode()):
            assert secret not in result.stdout + result.stderr, "CLI output leaked a generated test secret"
        return output, result

    def notes_path(self, label="bundle", *, default_output=False):
        name = self.version if default_output else label
        return self.root / ".local" / "releases" / (name + "-release-notes.md")


def assert_success(result):
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return json.loads(result.stdout)


def assert_rejected(repo, code, label="rejected", **kwargs):
    output, result = repo.build(label, **kwargs)
    assert result.returncode != 0, "expected builder rejection"
    assert result.stderr.decode().strip() == code, result.stderr.decode()
    assert not output.exists(), "a failed build left an apparently complete release"
    if output.parent.exists():
        assert not list(output.parent.glob(f".{repo.version}-*")), "temporary assets escaped cleanup"


def updater_error(code, operation):
    try:
        operation()
    except UPDATER.PlatformUpdateError as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError(f"real updater should reject: {code}")


def verifier_error(code, operation):
    try:
        operation()
    except VERIFIER.VerificationError as exc:
        assert str(exc) == code, (str(exc), code)
    else:
        raise AssertionError(f"public verifier should reject: {code}")


@contextlib.contextmanager
def public_key_environment(path: Path):
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"SAAS_UPDATE_PUBLIC_KEY_FILE": str(path)}))
        if os.name == "nt":
            # Windows has no geteuid and reports writable regular files as 0666.
            # Emulate only POSIX owner/write bits, preserving file type, size, bytes,
            # cryptography, path checks and every actual validator implementation.
            original_lstat = Path.lstat

            def posix_key_metadata(candidate, *args, **kwargs):
                metadata = original_lstat(candidate, *args, **kwargs)
                if candidate == path:
                    values = list(metadata)
                    values[0] &= ~0o022
                    values[4] = 0
                    return os.stat_result(values)
                return metadata

            stack.enter_context(patch.object(UPDATER.os, "geteuid", return_value=0, create=True))
            stack.enter_context(patch.object(Path, "lstat", posix_key_metadata))
        yield


class Response:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.headers = {"content-length": str(len(payload))}

    def iter_content(self, chunk_size):
        for start in range(0, len(self.payload), chunk_size):
            yield self.payload[start:start + chunk_size]

    def close(self):
        pass


class OfflineSession:
    def __init__(self, responses):
        self.responses = responses

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        assert url in self.responses, "unexpected upstream request"
        return Response(self.responses[url])


def assert_generated_notes(repo: Repository, notes_path: Path) -> None:
    notes = notes_path.read_text(encoding="utf-8")
    base = f"xianyu-saas-{repo.version}"
    download = f"https://github.com/{UPDATER.RELEASE_OWNER}/{UPDATER.RELEASE_REPOSITORY}/releases/download/v{repo.version}"
    assert notes.startswith(f"# xianyu-saas {repo.version} 发布说明")
    install_section = notes.index("## 首次安装")
    native_section = notes.index("### 2. Ubuntu 原生安装")
    update_section = notes.index("## 已有用户更新")
    changes_section = notes.index("## 本次版本变更")
    attachments_section = notes.index("## 附件说明")
    assert install_section < native_section < update_section < attachments_section < changes_section
    assert notes.count("## 附件说明") == 1
    for name in (f"{base}-source.zip", f"{base}-linux-x86_64", f"{base}-linux-aarch64"):
        assert f"{download}/{name}" in notes, name
    assert "sudo bash deploy/docker-install.sh" in notes
    assert f"sudo ./{base}-linux-x86_64 install" in notes
    assert f"`{base}-linux-aarch64`" in notes
    assert f"install --version {repo.version}" in notes
    assert "### Added\n\n- Release fixture only." in notes
    assert "Source code" in notes
    assert len(notes) <= UPDATER.MAX_RELEASE_NOTES_CHARS


def verify_bundle(repo: Repository, output: Path, *, notes_path=None, epoch=EPOCH):
    base = f"xianyu-saas-{repo.version}"
    docker_names = DOCKER.docker_asset_names(repo.version)
    content = VERIFIER.expected_content(repo.version)
    expected_assets = set(content) | {"artifacts.json", "artifacts.json.sig"}
    assert len(content) == 12 and len(expected_assets) == 14
    assert {path.name for path in output.iterdir()} == expected_assets
    release_metadata = {"id": 1, "tag_name": f"v{repo.version}", "prerelease": "-" in repo.version,
                        "assets": [{"id": index + 1, "name": name, "size": (output / name).stat().st_size}
                                   for index, name in enumerate(sorted(expected_assets))]}
    # Real deployed clients: the Docker descriptor and the per-architecture
    # standalone records must still parse from the reduced attachment set.
    docker_release = UPDATER._parse_release(release_metadata, "release", deployment="docker")
    assert (docker_release.artifact.name, docker_release.manifest.name, docker_release.signature.name) == docker_names
    assert docker_release.runtime_manifest is not None
    assert docker_release.runtime_manifest.name == f"{base}.manifest.json"
    for architecture in ("x86_64", "aarch64"):
        with patch.object(UPDATER, "deployment_kind", return_value="systemd"), \
                patch.object(UPDATER, "release_kind", return_value="standalone"), \
                patch.object(STANDALONE.platform, "machine", return_value=architecture):
            standalone_release = UPDATER._parse_release(release_metadata, "release", deployment="systemd")
        assert standalone_release is not None and standalone_release.kind == "standalone"
        assert (
            standalone_release.artifact.name,
            standalone_release.manifest.name,
            standalone_release.signature.name,
        ) == STANDALONE.standalone_asset_names(repo.version, architecture)
    # The retired source-OTA channel must fail closed instead of accepting a partial set.
    with patch.object(UPDATER, "release_kind", return_value="source"):
        try:
            UPDATER._parse_release(release_metadata, "release", deployment="systemd")
        except UPDATER.PlatformUpdateError as exc:
            assert exc.code == "release_assets_missing"
        else:
            raise AssertionError("source OTA discovery must reject the reduced inventory")
    index_raw = (output / "artifacts.json").read_bytes()
    index = json.loads(index_raw)
    assert set(index) == {"schema", "version", "commit", "manager_protocol", "public_key_fingerprint", "files"}
    assert index["schema"] == 2 and index["version"] == repo.version and index["commit"] == repo.commit
    assert index["manager_protocol"] == 1
    assert index["public_key_fingerprint"] == "sha256:" + hashlib.sha256(repo.public).hexdigest()
    assert len(index["files"]) == len(content)
    assert {record["name"] for record in index["files"]} == set(content)
    for record in index["files"]:
        kind_target = content[record["name"]]
        assert record == {**BUILDER.asset_record(output / record["name"]), **kind_target, "manager_protocol": 1}
    manager_index = MANAGER_INSTALLER._parse_index(index_raw, repo.version)
    for architecture in ("x86_64", "aarch64"):
        standalone_base = f"{base}-linux-{architecture}"
        for kind, name in (
            ("manager", standalone_base),
            ("archive", standalone_base + ".tar.gz"),
            ("manifest", standalone_base + ".manifest.json"),
            ("signature", standalone_base + ".manifest.sig"),
        ):
            assert MANAGER_INSTALLER._select_record(manager_index, name, kind, architecture).name == name
    repo.key.public_key().verify(base64.b64decode((output / "artifacts.json.sig").read_bytes(), validate=True), index_raw)
    for absent in ("SHA256SUMS", f"{base}.update-signing.pub", f"{base}.manifest.sig", "release-notes.md", f"{base}.tar.gz"):
        assert not (output / absent).exists(), absent
    if notes_path is not None:
        assert_generated_notes(repo, notes_path)
    docker_raw = (output / docker_names[1]).read_bytes()
    docker_signature = (output / docker_names[2]).read_bytes()
    docker_manifest = DOCKER.verify_docker_manifest(docker_raw, docker_signature, repo.public, repo.version)
    runtime_manifest_raw = (output / f"{base}.manifest.json").read_bytes()
    runtime_manifest = json.loads(runtime_manifest_raw)
    assert json.loads(docker_raw) == {
        "schema": 1, "protocol": 1, "version": repo.version, "commit": repo.commit,
        "source": BUILDER.asset_record(output / docker_names[0]),
        "runtime_manifest_sha256": hashlib.sha256(runtime_manifest_raw).hexdigest(),
    }
    assert runtime_manifest["schema"] == 1 and runtime_manifest["version"] == repo.version
    assert runtime_manifest["artifact"] == f"{base}.tar.gz"
    assert set(runtime_manifest) == {"schema", "version", "artifact", "artifact_sha256", "artifact_size", "files"}
    assert len(runtime_manifest["artifact_sha256"]) == 64 and runtime_manifest["artifact_size"] > 0
    source_root = DOCKER.extract_verified_source(output / docker_names[0], output.parent / (output.name + "-docker"), docker_manifest)
    assert source_root.name == base
    for name in BUILDER.REQUIRED_DOCKER_INSTALL_FILES:
        assert (source_root / name).read_bytes() == repo.files[name], name
    assert set(docker_manifest.__dict__) == {"version", "commit", "source_name", "source_size", "source_sha256", "runtime_manifest_sha256", "protocol"}
    modified = bytearray(base64.b64decode(docker_signature))
    modified[0] ^= 1
    try:
        DOCKER.verify_docker_manifest(docker_raw, base64.b64encode(modified), repo.public, repo.version)
    except DOCKER.DockerUpdateError as exc:
        assert exc.code == "docker_signature_invalid", exc.code
    else:
        raise AssertionError("tampered Docker signature accepted")
    source_payloads = {}
    with zipfile.ZipFile(output / f"{base}-source.zip") as archive:
        prefix = base + "/"
        paths = {entry.filename.removeprefix(prefix) for entry in archive.infolist()}
        assert paths == set(repo.files) | {"backend/build-info.json"}
        assert set(BUILDER.REQUIRED_LICENSES) <= paths
        assert set(BUILDER.REQUIRED_DOCKER_INSTALL_FILES) <= paths
        for entry in archive.infolist():
            assert entry.filename.startswith(prefix) and stat.S_ISREG(entry.external_attr >> 16)
            relative = entry.filename.removeprefix(prefix)
            executable = relative in {"scripts/check.sh", "docker/entrypoint.sh"}
            assert stat.S_IMODE(entry.external_attr >> 16) == (0o755 if executable else 0o644)
            payload = archive.read(entry)
            source_payloads[relative] = payload
            if relative != "backend/build-info.json":
                assert payload == repo.files[relative]
            assert repo.seed not in payload and repo.encoded.encode() not in payload
    info = json.loads(source_payloads["backend/build-info.json"])
    assert info == {"version": repo.version, "commit": repo.commit, "dirty": False,
                    "build_time": BUILDER.datetime.fromtimestamp(epoch, BUILDER.timezone.utc).isoformat(timespec="seconds")}
    for architecture in ("x86_64", "aarch64"):
        target = STANDALONE.target_name(architecture)
        archive_name, standalone_manifest_name, standalone_signature_name = STANDALONE.standalone_asset_names(repo.version, architecture)
        manager_name = STANDALONE.manager_asset_name(repo.version, architecture)
        standalone_raw = (output / standalone_manifest_name).read_bytes()
        repo.key.public_key().verify(base64.b64decode((output / standalone_signature_name).read_bytes(), validate=True), standalone_raw)
        standalone_manifest = json.loads(standalone_raw)
        runtime = STANDALONE.validate_standalone_manifest(
            standalone_manifest,
            expected_version=repo.version,
            expected_target=target,
            expected_artifact=archive_name,
        )
        assert runtime.commit == repo.commit and runtime.target == target and runtime.manager_protocol == 1
        assert standalone_manifest["artifact_sha256"] == hashlib.sha256((output / archive_name).read_bytes()).hexdigest()
        assert standalone_manifest["artifact_size"] == (output / archive_name).stat().st_size
        with tarfile.open(output / archive_name, "r:gz") as archive:
            members = archive.getmembers()
            paths = {member.name for member in members}
            assert not any(path == "app" or path.startswith("app/") for path in paths)
            assert not any(path == "deploy" or path.startswith("deploy/") for path in paths)
            assert not any("nginx" in path.lower() for path in paths)
            assert {path.split("/", 1)[0] for path in paths} >= {"backend", "frontend", "worker", "runtime", "manager"}
            assert all(member.isfile() and not member.issym() and not member.islnk() for member in members)
            assert archive.extractfile("manager/xianyu-saas").read() == (output / manager_name).read_bytes()
            assert json.loads(archive.extractfile("runtime/runtime.json").read()) == standalone_manifest["runtime"]
        assert {item["path"] for item in standalone_manifest["files"]} == paths
        by_path = {item["path"]: item for item in standalone_manifest["files"]}
        assert by_path["runtime/python/bin/python3"]["executable"] is True
        assert by_path["manager/xianyu-saas"]["executable"] is True
        assert by_path["docker/launcher.sh"]["executable"] is True
        with tarfile.open(output / archive_name, "r:gz") as archive:
            assert stat.S_IMODE(archive.getmember("runtime/python/bin/python3").mode) == 0o755
    with tempfile.TemporaryDirectory(prefix="release-contract-public-") as temporary:
        trusted_public = Path(temporary) / "update-signing.pub"
        trusted_public.write_bytes(base64.b64encode(repo.public))
        VERIFIER.verify(output, repo.version, repo.commit, trusted_public)
    return runtime_manifest, source_payloads


def workflow_contract(repo: Repository, output: Path):
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    manager_spec = (ROOT / "deploy/manager/xianyu-saas-manager.spec").read_text(encoding="utf-8")
    standalone_builder = (ROOT / "scripts/build-standalone.py").read_text(encoding="utf-8")
    assert "deploy/nginx/" not in manager_spec and "nginx" not in manager_spec.lower()
    assert "deploy/nginx/" not in standalone_builder and "nginx" not in standalone_builder.lower()
    standalone_section = workflow.split("  standalone:\n", 1)[1].split("  publish:\n", 1)[0]
    publish_section = workflow.split("  publish:\n", 1)[1]
    assert "target: linux-x86_64" in standalone_section and "architecture: x86_64" in standalone_section
    assert "target: linux-aarch64" in standalone_section and "architecture: aarch64" in standalone_section
    assert "runner: ubuntu-24.04-arm" in standalone_section
    assert "manager_platform: linux/amd64" in standalone_section
    assert "manager_platform: linux/arm64" in standalone_section
    assert "python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254" in standalone_section
    assert 'docker run --rm --platform "$MANAGER_PLATFORM"' in standalone_section
    assert "--env TARGET" in standalone_section
    assert 'pyinstaller==6.16.0' in standalone_section
    assert "apt-get install --yes --no-install-recommends binutils" in standalone_section
    assert 'dist/xianyu-saas" ".local/standalone/$TARGET/manager"' in standalone_section
    assert 'internal self-check > /tmp/manager-self-check.json' in standalone_section
    assert 'sudo chown -R "$(id -u):$(id -g)"' in standalone_section
    assert 'parsed.hostname != "files.pythonhosted.org"' in standalone_section
    assert 'lock.get("status") != "locked"' in standalone_section
    assert "python scripts/build-standalone.py" in standalone_section
    assert "--manager-binary" in standalone_section
    assert 'PYTHONDONTWRITEBYTECODE: "1"' in standalone_section
    assert "standalone-${{ matrix.target }}" in standalone_section
    assert "RELEASE_SIGNING_KEY" not in standalone_section
    assert workflow.count("secrets.RELEASE_SIGNING_KEY") == 1
    assert "needs: [validate, standalone]" in publish_section
    assert "--standalone-input-root .local/standalone-inputs" in publish_section
    assert '--notes-output ".local/releases/$RELEASE_VERSION-release-notes.md"' in publish_section
    assert 'assets=("$dir"/*)' in publish_section and 'test "${#assets[@]}" -eq 14' in publish_section
    assert 'notes_file=".local/releases/$RELEASE_VERSION-release-notes.md"' in publish_section
    assert 'test -s "$notes_file"' in publish_section
    assert '--notes-file "$notes_file"' in publish_section
    assert '--notes-file "$dir/release-notes.md"' not in publish_section
    assert publish_section.count("scripts/verify-public-release.py") == 2
    assert publish_section.index('gh release download "$RELEASE_TAG"') < publish_section.index('--draft=false')
    assert "python -B tests/docker-update-protocol-contract.py" in publish_section
    assert "python -B tests/standalone-build-contract.py" in publish_section
    assert "python -B tests/release-bundle-contract.py" in publish_section
    assert {path.name for path in output.iterdir()} == set(VERIFIER.expected_content(repo.version)) | {
        "artifacts.json", "artifacts.json.sig"
    }
    print("release bundle: workflow has native dual-architecture unsigned builds and release-job-only signing")


def normal_and_tamper_contract(run: Path):
    repo = Repository(run / "normal")
    # These are deliberately ignored/untracked and must never be opened or shipped.
    repo.write("worker/.env", repo.encoded.encode())
    repo.write("untracked-secret.txt", repo.seed)
    output, result = repo.build(default_output=True)
    assert_success(result)
    runtime_manifest, source_payloads = verify_bundle(
        repo, output, notes_path=repo.notes_path(default_output=True)
    )
    expected = {
        item["path"]: UPDATER.ManifestFile(item["path"], item["size"], item["sha256"], item["executable"])
        for item in runtime_manifest["files"]
    }
    ota_files = {
        item["path"]: (source_payloads[item["path"]], bool(item["executable"]))
        for item in runtime_manifest["files"]
    }
    tar_name, manifest_name, signature_name = UPDATER._asset_names(repo.version)
    ota_tar = run / f"synthetic-{tar_name}"
    BUILDER.write_tar(ota_tar, ota_files, EPOCH)
    ota_manifest_raw = BUILDER.json_bytes(runtime_manifest)
    ota_signature_raw = base64.b64encode(repo.key.sign(ota_manifest_raw))
    ota_release = UPDATER.ReleaseInfo(
        "fixture", repo.version, f"v{repo.version}", "", "", False,
        UPDATER.ReleaseAsset(1, tar_name, ota_tar.stat().st_size),
        UPDATER.ReleaseAsset(2, manifest_name, len(ota_manifest_raw)),
        UPDATER.ReleaseAsset(3, signature_name, len(ota_signature_raw)),
    )
    UPDATER.parse_manifest(ota_manifest_raw, ota_release)
    workflow_contract(repo, output)
    index_path = output / "artifacts.json"
    original_index = index_path.read_bytes()
    try:
        index_path.write_bytes(original_index + b" ")
        verifier_error(
            "release_index_signature_invalid",
            lambda: VERIFIER.verify(output, repo.version, repo.commit, repo.root / "deploy/update-signing.pub"),
        )
    finally:
        index_path.write_bytes(original_index)
    architecture = "x86_64"
    target = STANDALONE.target_name(architecture)
    archive_name, standalone_manifest_name, _ = STANDALONE.standalone_asset_names(repo.version, architecture)
    bound = json.loads((output / standalone_manifest_name).read_bytes())
    bound["architecture"] = "aarch64"
    try:
        STANDALONE.validate_standalone_manifest(
            bound,
            expected_version=repo.version,
            expected_target=target,
            expected_artifact=archive_name,
        )
    except STANDALONE.StandaloneRuntimeError as exc:
        assert exc.code == "standalone_runtime_architecture_mismatch"
    else:
        raise AssertionError("standalone manifest accepted another architecture")
    linked = run / "linked-standalone.tar.gz"
    with tarfile.open(linked, "w:gz") as archive:
        info = tarfile.TarInfo("manager/xianyu-saas")
        info.type = tarfile.SYMTYPE
        info.linkname = "../outside"
        archive.addfile(info)
    verifier_error(
        "release_archive_link_or_path_invalid",
        lambda: VERIFIER.verify_tar(
            linked,
            {"manager/xianyu-saas": {"size": 0, "sha256": hashlib.sha256(b"").hexdigest(), "executable": True}},
            standalone=True,
        ),
    )
    repeated, result = repo.build("repeat")
    assert_success(result)
    assert {p.name: p.read_bytes() for p in output.iterdir()} == {p.name: p.read_bytes() for p in repeated.iterdir()}
    assert_generated_notes(repo, repo.notes_path("repeat"))
    override, result = repo.build("epoch", changes={"SOURCE_DATE_EPOCH": "1720000000"})
    assert_success(result)
    verify_bundle(repo, override, notes_path=repo.notes_path("epoch"), epoch=1720000000)
    override_again, result = repo.build("epoch-repeat", changes={"SOURCE_DATE_EPOCH": "1720000000"})
    assert_success(result)
    assert {p.name: p.read_bytes() for p in override.iterdir()} == {p.name: p.read_bytes() for p in override_again.iterdir()}
    empty = repo.root / ".local/releases/empty"
    empty.mkdir()
    _, result = repo.build("empty")
    assert_success(result)
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    notes_before = repo.notes_path(default_output=True).read_bytes()
    _, result = repo.build(default_output=True)
    assert result.returncode != 0 and result.stderr.strip() == b"release_output_not_empty"
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}
    assert repo.notes_path(default_output=True).read_bytes() == notes_before
    assert_rejected(repo, "release_timestamp_invalid", changes={"SOURCE_DATE_EPOCH": "not-a-date"})
    assert_rejected(repo, "release_timestamp_invalid", changes={"SOURCE_DATE_EPOCH": "4294967296"})
    _, result = repo.build("bad-output", extra=("--output", str(repo.root / "public-assets")))
    assert result.returncode != 0 and result.stderr.strip() == b"release_output_not_ignored"
    assert not (repo.root / "public-assets").exists()
    # Change the actual payload but preserve member names and sizes: extraction must hash.
    bad_tar = run / "tampered.tar.gz"
    with tarfile.open(ota_tar, "r:gz") as source, tarfile.open(bad_tar, "w:gz") as target:
        for member in source:
            payload = source.extractfile(member).read()
            if member.name == "frontend/index.html":
                payload = bytes([payload[0] ^ 1]) + payload[1:]
            target.addfile(member, io.BytesIO(payload))
    bad_extract = run / "bad-extract"
    bad_extract.mkdir()
    updater_error("update_archive_hash_mismatch", lambda: UPDATER.extract_verified_archive(bad_tar, bad_extract, expected))
    bad_manifest = json.loads(ota_manifest_raw)
    bad_manifest["files"][0]["path"] = "../outside"
    updater_error("update_archive_path_invalid", lambda: UPDATER.parse_manifest(BUILDER.json_bytes(bad_manifest), ota_release))
    responses = {
        ota_release.artifact.api_url: ota_tar.read_bytes(),
        ota_release.manifest.api_url: ota_manifest_raw,
        ota_release.signature.api_url: ota_signature_raw,
    }
    public_file = repo.root / "deploy/update-signing.pub"
    with public_key_environment(public_file), patch.dict(os.environ, {
        "SAAS_UPDATE_STAGING_DIR": str(run / "staging"), "SAAS_CURRENT_ROOT": str(repo.root),
    }):
        staged = UPDATER.stage_release(ota_release, "stable", "0.1.0", session=OfflineSession(responses))
        assert Path(staged["candidate_path"]).is_dir()
        broken = dict(responses)
        archive_raw = bytearray(broken[ota_release.artifact.api_url])
        archive_raw[-1] ^= 1
        broken[ota_release.artifact.api_url] = bytes(archive_raw)
        updater_error("update_artifact_hash_mismatch", lambda: UPDATER.stage_release(ota_release, "stable", "0.1.0", session=OfflineSession(broken)))
    print("release bundle: reproducibility, assets, real verifier and tampering passed")


def key_and_version_contract(run: Path):
    repo = Repository(run / "keys", public_pem=True)
    output, result = repo.build()
    assert_success(result)
    verify_bundle(repo, output, notes_path=repo.notes_path())
    assert_rejected(repo, "release_signing_key_missing", changes={"RELEASE_SIGNING_KEY": None})
    for bad in ("invalid-base64!", base64.b64encode(b"short").decode(), repo.encoded + "\n"):
        assert_rejected(repo, "release_signing_key_invalid", changes={"RELEASE_SIGNING_KEY": bad})
    _, _, other_key, _ = key_material()
    assert_rejected(repo, "release_public_key_mismatch", changes={"RELEASE_SIGNING_KEY": other_key})
    assert_rejected(repo, "release_public_key_missing", extra=("--public-key-file", "deploy/missing.pub"))
    assert_rejected(repo, "release_path_invalid", extra=("--public-key-file", "../outside.pub"))
    output, result = repo.build("custom-env", changes={"RELEASE_SIGNING_KEY": None, "CONTRACT_KEY": repo.encoded}, extra=("--signing-key-env", "CONTRACT_KEY"))
    assert_success(result)
    repo.change("deploy/update-signing.pub", b"not-a-public-key\n")
    assert_rejected(repo, "release_public_key_invalid")
    for index, field in enumerate(("package", "lock", "lock-root", "backend")):
        changed = Repository(run / f"version-{index}")
        if field == "backend":
            changed.change("backend/version.py", b'VERSION = "0.3.0"\n')
        else:
            filename = "package.json" if field == "package" else "package-lock.json"
            payload = json.loads(changed.files[filename])
            if field == "lock-root":
                payload["packages"][""]["version"] = "0.3.0"
            else:
                payload["version"] = "0.3.0"
            changed.change(filename, BUILDER.json_bytes(payload))
        assert_rejected(changed, "release_version_mismatch")
    for index, version in enumerate(("0.2.0/escape", "0.2.0-01", " 0.2.0")):
        changed = Repository(run / f"semver-{index}", version=version)
        assert_rejected(changed, "release_version_invalid")
    prerelease = Repository(run / "prerelease", version="0.3.0-rc.1+build.2")
    output, result = prerelease.build()
    assert_success(result)
    verify_bundle(prerelease, output, notes_path=prerelease.notes_path())
    for index, name in enumerate(BUILDER.REQUIRED_DOCKER_INSTALL_FILES):
        incomplete = Repository(run / f"docker-input-{index}")
        incomplete.change(name, b"")
        assert_rejected(incomplete, "release_docker_source_missing")
    literal = Repository(run / "literal-version")
    literal_source = f'VERSION = "{VERSION}"\nraise AssertionError("never execute version source")\n'.encode()
    literal.change("backend/version.py", literal_source)
    literal.files["backend/version.py"] = literal_source
    output, result = literal.build()
    assert_success(result)
    verify_bundle(literal, output, notes_path=literal.notes_path())
    for payload in (f'"""VERSION = "{VERSION}"\n"""\nVERSION = "0.3.0"\n'.encode(),
                    f'VERSION = "{VERSION}"\nVERSION = "0.3.0"\n'.encode(), b"VERSION = current_version()\n"):
        literal.change("backend/version.py", payload)
        assert_rejected(literal, "release_version_mismatch")
    repetitive = Repository(run / "repetitive-source")
    payload = b"0" * (2 * 1024 * 1024)
    repetitive.change("frontend/assets/repeated.txt", payload)
    repetitive.files["frontend/assets/repeated.txt"] = payload
    output, result = repetitive.build()
    assert_success(result)
    verify_bundle(repetitive, output, notes_path=repetitive.notes_path())
    with zipfile.ZipFile(output / DOCKER.docker_asset_names(repetitive.version)[0]) as source:
        assert source.getinfo(f"xianyu-saas-{repetitive.version}/frontend/assets/repeated.txt").compress_type == zipfile.ZIP_STORED
    print("release bundle: key validation, static commit versions and required Docker inputs passed")


def privacy_contract(run: Path):
    paths = (
        "config/saas.env", "worker/cookies.txt", "worker/auth_cookie.json", "worker/redeem_codes.json",
        "worker/pan_links.json", "worker/legacy_delivery_ledger.json", "worker/orders.csv", "worker/卡密.txt",
        "deploy/signing.key", "worker/data/runtime.json", "worker/runtime-data/state.json",
        "backend/.venv/lib/site.py", "backend/venv/site.py", "backend/saas.db", "worker/logs/app.log",
        "worker/app.log.1", "AGENTS.md", "handoff/MEMORY.md", ".local/releases/private.txt",
    )
    for index, path in enumerate(paths):
        repo = Repository(run / f"private-{index}")
        repo.change(path, b"sensitive fixture\n")
        assert_rejected(repo, "release_private_path_rejected")
    repo = Repository(run / "products")
    repo.change("worker/products_config.json", b'{"types": [{"id": "real-item"}]}\n')
    assert_rejected(repo, "release_products_template_invalid")
    for index, kind in enumerate(("private-key", "active-seed", "github-secret", "openai-secret")):
        repo = Repository(run / f"content-{index}")
        if kind == "private-key":
            payload = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        elif kind == "active-seed":
            payload = repo.encoded.encode()
        elif kind == "github-secret":
            payload = ("gh" + "p_" + "x" * 32).encode()
        else:
            payload = ("sk-" + "A" * 32).encode()
        repo.change("frontend/assets/innocent.txt", payload)
        assert_rejected(repo, "release_secret_content_rejected")
    repo = Repository(run / "license")
    (repo.root / "worker/NOTICE.md").unlink()
    repo.git("add", "--all")
    repo.commit_changes()
    assert_rejected(repo, "release_license_missing")
    repo = Repository(run / "case")
    # Use a Git tree entry, not a Windows case-insensitive on-disk collision.
    oid = repo.git("hash-object", "-w", "--stdin", data=b"case fixture").decode().strip()
    repo.git("update-index", "--add", "--cacheinfo", f"100644,{oid},BACKEND/case.py")
    repo.commit_changes()
    # Make it historical so an intentionally index-only fixture is not a dirty HEAD.
    historical = repo.git("rev-parse", "HEAD").decode().strip()
    repo.change("README.md", b"second commit\n")
    assert_rejected(repo, "release_path_collision", ref=historical)
    repo = Repository(run / "symlink")
    oid = repo.git("hash-object", "-w", "--stdin", data=b"../outside").decode().strip()
    repo.git("update-index", "--add", "--cacheinfo", f"120000,{oid},frontend/link")
    repo.commit_changes()
    historical = repo.git("rev-parse", "HEAD").decode().strip()
    repo.change("README.md", b"second commit\n")
    assert_rejected(repo, "release_link_or_special_file_rejected", ref=historical)
    print("release bundle: forced-tracked secrets, runtime paths, templates and links passed")


def dirty_and_atomic_contract(run: Path):
    repo = Repository(run / "dirty")
    if os.name != "nt":
        executable = repo.root / "scripts/check.sh"
        executable.chmod(0o644)
        assert_rejected(repo, "release_worktree_dirty")
        executable.chmod(0o755)
    repo.write("README.md", b"uncommitted\n")
    assert_rejected(repo, "release_worktree_dirty")
    repo.git("add", "--", "README.md")
    assert_rejected(repo, "release_worktree_dirty")
    old_commit = repo.commit
    repo.commit = repo.commit_changes()
    repo.write("package.json", b"broken local file; never load this\n")
    repo.write("deploy/update-signing.pub", repo.seed)
    repo.write("backend/platform_update.py", b"raise AssertionError('must use the commit blob')\n")
    repo.write("frontend/index.html", b"dirty local payload\n")
    assert_rejected(repo, "release_worktree_dirty", ref=repo.commit)
    output, result = repo.build("historical", ref=old_commit)
    assert_success(result)
    repo.commit = old_commit
    verify_bundle(repo, output, notes_path=repo.notes_path("historical"))
    repo = Repository(run / "atomic")
    cwd = Path.cwd()
    try:
        os.chdir(repo.root)
        inputs = repo.standalone_inputs("HEAD")
        with patch.dict(os.environ, {"RELEASE_SIGNING_KEY": repo.encoded}), patch.object(BUILDER, "write_source_zip", side_effect=OSError("injected write failure")), contextlib.redirect_stderr(io.StringIO()) as errors:
            assert BUILDER.main(["--output", ".local/releases/failure", "--standalone-input-root", str(inputs)]) == 1
        assert errors.getvalue().strip() == "release_build_failed"
        with patch.dict(os.environ, {"RELEASE_SIGNING_KEY": repo.encoded}), patch.object(BUILDER.os, "rename", side_effect=OSError("injected publication failure")), contextlib.redirect_stderr(io.StringIO()) as errors:
            assert BUILDER.main(["--output", ".local/releases/rename-failure", "--standalone-input-root", str(inputs)]) == 1
        assert errors.getvalue().strip() == "release_build_failed"
        assert not (repo.root / ".local/releases/rename-failure").exists()
        assert not repo.notes_path("rename-failure").exists(), "failed publication must remove its notes file"
    finally:
        os.chdir(cwd)
    assert not (repo.root / ".local/releases/failure").exists()
    assert list((repo.root / ".local/releases").iterdir()) == []
    # The protocol loader must retain the updater's limits and exact SemVer semantics.
    protocol = BUILDER.load_protocol(UPDATER_SOURCE)
    for name in ("MAX_ARCHIVE_BYTES", "MAX_UNPACKED_BYTES", "MAX_FILE_BYTES", "MAX_ARCHIVE_MEMBERS", "MAX_MANIFEST_BYTES"):
        assert getattr(protocol, name) == getattr(UPDATER, name)
    with patch.dict(os.environ, {"RELEASE_SIGNING_KEY": repo.encoded, "OTHER_SECRET": "fixture"}):
        assert "RELEASE_SIGNING_KEY" not in BUILDER.git_environment()
        assert "OTHER_SECRET" not in BUILDER.git_environment()
    print("release bundle: dirty HEAD, historical blobs, overwrite and atomic cleanup passed")


def notes_and_metadata_contract(run: Path):
    repo = Repository(run / "notes-oversized")
    oversized = "### Added\n\n- " + "x" * (UPDATER.MAX_RELEASE_NOTES_CHARS + 1)
    repo.change("CHANGELOG.md", f"# Changes\n\n## [{repo.version}] - 2023-11-14\n\n{oversized}\n".encode())
    assert_rejected(repo, "release_notes_invalid")

    repo = Repository(run / "notes-path")
    assert_rejected(
        repo,
        "release_notes_output_invalid",
        label="notes-in-assets",
        extra=("--notes-output", ".local/releases/notes-in-assets/release-notes.md"),
    )
    assert_rejected(repo, "release_notes_output_invalid", label="notes-source", extra=("--notes-output", "README.md"))
    # A pre-existing notes file is never overwritten.
    existing = repo.root / ".local" / "releases" / "existing-notes.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("keep me\n", encoding="utf-8")
    assert_rejected(
        repo,
        "release_notes_output_invalid",
        label="notes-existing",
        extra=("--notes-output", str(existing)),
    )
    assert existing.read_text(encoding="utf-8") == "keep me\n"
    # A file created after path validation must also survive publication.
    raced = BUILDER.notes_output_path(repo.root, str(existing.with_name("raced.md")), existing.parent / "assets")
    raced.write_text("other writer\n", encoding="utf-8")
    try:
        BUILDER.write_notes_output(raced, b"replacement\n")
    except BUILDER.BundleError as exc:
        assert str(exc) == "release_notes_output_invalid"
    else:
        raise AssertionError("concurrent notes output was overwritten")
    assert raced.read_text(encoding="utf-8") == "other writer\n"
    # A symlinked ancestor must not redirect notes outside the allowed scope.
    link = repo.root / ".local" / "releases-link"
    try:
        link.symlink_to(repo.root / ".local" / "releases", target_is_directory=True)
    except (OSError, NotImplementedError):
        print("release bundle: notes symlink rejection skipped (host needs symlink privilege)")
    else:
        assert_rejected(
            repo,
            "release_notes_output_invalid",
            label="notes-symlink",
            extra=("--notes-output", str(link / "linked-notes.md")),
        )
    output, result = repo.build("notes-explicit")
    assert_success(result)
    assert_generated_notes(repo, repo.notes_path("notes-explicit"))
    custom = repo.root / ".local" / "releases" / "custom-notes.md"
    output, result = repo.build("notes-custom", extra=("--notes-output", str(custom)))
    assert_success(result)
    assert_generated_notes(repo, custom)
    assert not (output / "release-notes.md").exists()

    # A Release source ZIP has no .git: explicit build arguments win, otherwise the
    # packaged same-version build info must survive the Dockerfile write step.
    repo = Repository(run / "no-git")
    version_source = (ROOT / "backend/version.py").read_text(encoding="utf-8")
    version_source = re.sub(r'^VERSION = "[^"]+"', f'VERSION = "{repo.version}"', version_source, flags=re.MULTILINE)
    repo.change("backend/version.py", version_source.encode("utf-8"))
    output, result = repo.build()
    assert_success(result)
    source = run / "no-git-source"
    with zipfile.ZipFile(output / f"xianyu-saas-{repo.version}-source.zip") as archive:
        archive.extractall(source)
    root = source / f"xianyu-saas-{repo.version}"
    build_info = root / "backend/build-info.json"
    packaged = json.loads(build_info.read_bytes())
    assert packaged["version"] == repo.version and packaged["commit"] == repo.commit and packaged["dirty"] is False

    env = clean_environment()
    env["SAAS_BUILD_DIRTY"] = "unknown"
    result = subprocess.run([sys.executable, "-B", "backend/version.py", "--write-build-info"],
                            cwd=root, env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert json.loads(build_info.read_bytes()) == packaged, "no-Git build must retain packaged same-version metadata"

    override_env = dict(env, SAAS_BUILD_COMMIT="a" * 40, SAAS_BUILD_DIRTY="true")
    result = subprocess.run([sys.executable, "-B", "backend/version.py", "--write-build-info"],
                            cwd=root, env=override_env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    overridden = json.loads(build_info.read_bytes())
    assert overridden["commit"] == "a" * 40 and overridden["dirty"] is True

    build_info.write_text(json.dumps({
        "version": "9.9.9", "commit": "b" * 40, "build_time": packaged["build_time"], "dirty": False,
    }) + "\n", encoding="utf-8")
    result = subprocess.run([sys.executable, "-B", "backend/version.py", "--write-build-info"],
                            cwd=root, env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    replaced = json.loads(build_info.read_bytes())
    assert replaced["version"] == repo.version and replaced["commit"] == "" and replaced["dirty"] is None

    dockerignore = {line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")}
    assert "backend/build-info.json" not in dockerignore, "packaged metadata must reach the Docker build context"

    # The signed public verifier rejects a source package without installer inputs.
    assert BUILDER.REQUIRED_DOCKER_INSTALL_FILES == VERIFIER.REQUIRED_DOCKER_INSTALL_FILES
    prefix = f"xianyu-saas-{repo.version}/"
    def zip_entry(name):
        entry = zipfile.ZipInfo(prefix + name)
        entry.create_system = 3
        entry.external_attr = (stat.S_IFREG | 0o644) << 16
        return entry
    complete = run / "complete-source.zip"
    with zipfile.ZipFile(complete, "w") as archive:
        for name in BUILDER.REQUIRED_DOCKER_INSTALL_FILES:
            archive.writestr(zip_entry(name), b"content\n")
    VERIFIER.verify_source_zip(complete, repo.version)
    for name in BUILDER.REQUIRED_DOCKER_INSTALL_FILES:
        partial = run / ("missing-" + name.replace("/", "-") + ".zip")
        with zipfile.ZipFile(partial, "w") as archive:
            for other in BUILDER.REQUIRED_DOCKER_INSTALL_FILES:
                if other != name:
                    archive.writestr(zip_entry(other), b"content\n")
        verifier_error("docker_source_invalid", lambda partial=partial: VERIFIER.verify_source_zip(partial, repo.version))
    empty = run / "empty-installer.zip"
    with zipfile.ZipFile(empty, "w") as archive:
        for name in BUILDER.REQUIRED_DOCKER_INSTALL_FILES:
            archive.writestr(zip_entry(name), b"" if name == "Dockerfile" else b"content\n")
    verifier_error("docker_source_invalid", lambda: VERIFIER.verify_source_zip(empty, repo.version))
    print("release bundle: notes policy, no-Git build metadata and required installer files passed")


def main():
    with tempfile.TemporaryDirectory(prefix="xianyu-release-contract-") as temporary:
        run = Path(temporary)
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
            normal_and_tamper_contract(run)
            key_and_version_contract(run)
            privacy_contract(run)
            dirty_and_atomic_contract(run)
            notes_and_metadata_contract(run)
    print("release bundle contract: ok (temporary keys/repos only; real updater verified)")


if __name__ == "__main__":
    main()
