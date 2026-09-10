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
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import types
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-release.py"
VERSION = "0.2.0"
EPOCH = 1700000000
sys.dont_write_bytecode = True


def load_builder():
    spec = importlib.util.spec_from_file_location("release_bundle_builder_contract", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUILDER = load_builder()
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
            "backend/version.py": f'VERSION = "{version}"\nASSET_VERSION = "contract"\n'.encode(),
            "backend/platform_update.py": UPDATER_SOURCE,
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
            "docker/entrypoint.sh": b"#!/bin/sh\nexit 0\n",
            "docs/DEPLOYMENT.md": b"Docker and manual deployment fixture.\n",
            "docs/assets/readme/orders.png": b"public documentation screenshot fixture\n",
            "docs/说明.md": "说明样例\n".encode(),
            "deploy/update-signing.pub": public,
            "scripts/check.sh": b"#!/bin/sh\nexit 0\n",
            "tests/test_private_key.py": b"# Test code may discuss private keys without containing any.\n",
        }
        for name, payload in self.files.items():
            self.write(name, payload)
        self.git("add", "--all")
        self.git("add", "--chmod=+x", "--", "scripts/check.sh", "docker/entrypoint.sh")
        self.commit = self.commit_changes()

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

    def build(self, label="bundle", *, ref="HEAD", changes=None, extra=(), default_output=False):
        output = self.root / ".local" / "releases" / (self.version if default_output else label)
        command = [sys.executable, "-B", str(SCRIPT), "--ref", ref]
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


def verify_bundle(repo: Repository, output: Path, *, epoch=EPOCH):
    names = UPDATER._asset_names(repo.version)
    base = f"xianyu-saas-{repo.version}"
    expected_assets = {*names, f"{base}-source.zip", f"{base}.update-signing.pub", "release-notes.md", "artifacts.json", "SHA256SUMS"}
    assert {path.name for path in output.iterdir()} == expected_assets
    index = json.loads((output / "artifacts.json").read_bytes())
    assert set(index) == {"schema", "version", "commit", "public_key_fingerprint", "files"}
    assert index["schema"] == 1 and index["version"] == repo.version and index["commit"] == repo.commit
    assert index["public_key_fingerprint"] == "sha256:" + hashlib.sha256(repo.public).hexdigest()
    assert len(index["files"]) == 6
    for record in index["files"]:
        assert record == BUILDER.asset_record(output / record["name"])
    checksums = dict(line.split("  ", 1)[::-1] for line in (output / "SHA256SUMS").read_text().splitlines())
    assert set(checksums) == expected_assets - {"SHA256SUMS"}
    for name, checksum in checksums.items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == checksum
    assert (output / "release-notes.md").read_bytes() == b"### Added\n\n- Release fixture only.\n"
    assert (output / f"{base}.update-signing.pub").read_bytes() == repo.files["deploy/update-signing.pub"]
    manifest_raw = (output / names[1]).read_bytes()
    signature_raw = (output / names[2]).read_bytes()
    assert len(base64.b64decode(signature_raw, validate=True)) == 64
    release = UPDATER.ReleaseInfo("fixture", repo.version, f"v{repo.version}", "", "", "-" in repo.version,
                                  UPDATER.ReleaseAsset(1, names[0], (output / names[0]).stat().st_size),
                                  UPDATER.ReleaseAsset(2, names[1], len(manifest_raw)),
                                  UPDATER.ReleaseAsset(3, names[2], len(signature_raw)))
    parsed, expected = UPDATER.parse_manifest(manifest_raw, release)
    assert parsed["artifact_sha256"] == hashlib.sha256((output / names[0]).read_bytes()).hexdigest()
    assert parsed["artifact_size"] == (output / names[0]).stat().st_size
    assert set(parsed) == {"schema", "version", "artifact", "artifact_sha256", "artifact_size", "files"}
    public_file = output / f"{base}.update-signing.pub"
    public_file.chmod(0o644)
    with public_key_environment(public_file):
        UPDATER.verify_manifest_signature(manifest_raw, signature_raw)
        updater_error("update_signature_invalid", lambda: UPDATER.verify_manifest_signature(manifest_raw + b" ", signature_raw))
        modified_sig = bytearray(base64.b64decode(signature_raw))
        modified_sig[0] ^= 1
        updater_error("update_signature_invalid", lambda: UPDATER.verify_manifest_signature(manifest_raw, base64.b64encode(modified_sig)))
    with tarfile.open(output / names[0], "r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == sorted(expected)
        for member in members:
            assert member.isfile() and not member.issym() and not member.islnk()
            assert member.mtime == epoch and member.uid == member.gid == 0
            assert not member.uname and not member.gname
            assert UPDATER._validate_release_path(member.name) == member.name
            item = expected[member.name]
            payload = archive.extractfile(member).read()
            assert item.size == len(payload) and item.sha256 == hashlib.sha256(payload).hexdigest()
            assert member.mode == (0o755 if item.executable else 0o644)
            if member.name != "backend/build-info.json":
                assert payload == repo.files[member.name]
            assert repo.seed not in payload and repo.encoded.encode() not in payload
    assert expected["scripts/check.sh"].executable is True
    assert expected["frontend/index.html"].executable is False
    extracted = output.parent / (output.name + "-extracted")
    extracted.mkdir()
    UPDATER.extract_verified_archive(output / names[0], extracted, expected)
    info = json.loads((extracted / "backend/build-info.json").read_bytes())
    assert info == {"version": repo.version, "commit": repo.commit, "dirty": False,
                    "build_time": BUILDER.datetime.fromtimestamp(epoch, BUILDER.timezone.utc).isoformat(timespec="seconds")}
    if os.name != "nt":
        assert stat.S_IMODE((extracted / "scripts/check.sh").stat().st_mode) == 0o755
        assert stat.S_IMODE((extracted / "frontend/index.html").stat().st_mode) == 0o644
    source_only = {"Dockerfile", "docker-compose.yml", "docker/entrypoint.sh", "LICENSING.md", "CONTRIBUTING.md", ".github/workflows/ci.yml", "worker/.env.example"}
    assert not set(expected) & source_only
    with zipfile.ZipFile(output / f"{base}-source.zip") as archive:
        prefix = base + "/"
        paths = {entry.filename.removeprefix(prefix) for entry in archive.infolist()}
        assert paths == set(repo.files) | {"backend/build-info.json"}
        assert source_only | set(BUILDER.REQUIRED_LICENSES) <= paths
        for entry in archive.infolist():
            assert entry.filename.startswith(prefix) and stat.S_ISREG(entry.external_attr >> 16)
            relative = entry.filename.removeprefix(prefix)
            executable = relative in {"scripts/check.sh", "docker/entrypoint.sh"}
            assert stat.S_IMODE(entry.external_attr >> 16) == (0o755 if executable else 0o644)
            payload = archive.read(entry)
            assert payload == ((extracted / relative).read_bytes() if relative == "backend/build-info.json" else repo.files[relative])
            assert repo.seed not in payload and repo.encoded.encode() not in payload
    assert set(BUILDER.REQUIRED_LICENSES) <= set(expected)
    return release, expected, manifest_raw, signature_raw


def normal_and_tamper_contract(run: Path):
    repo = Repository(run / "normal")
    # These are deliberately ignored/untracked and must never be opened or shipped.
    repo.write("worker/.env", repo.encoded.encode())
    repo.write("untracked-secret.txt", repo.seed)
    output, result = repo.build(default_output=True)
    assert_success(result)
    release, expected, manifest_raw, signature_raw = verify_bundle(repo, output)
    repeated, result = repo.build("repeat")
    assert_success(result)
    assert {p.name: p.read_bytes() for p in output.iterdir()} == {p.name: p.read_bytes() for p in repeated.iterdir()}
    override, result = repo.build("epoch", changes={"SOURCE_DATE_EPOCH": "1720000000"})
    assert_success(result)
    verify_bundle(repo, override, epoch=1720000000)
    override_again, result = repo.build("epoch-repeat", changes={"SOURCE_DATE_EPOCH": "1720000000"})
    assert_success(result)
    assert {p.name: p.read_bytes() for p in override.iterdir()} == {p.name: p.read_bytes() for p in override_again.iterdir()}
    empty = repo.root / ".local/releases/empty"
    empty.mkdir()
    _, result = repo.build("empty")
    assert_success(result)
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    _, result = repo.build(default_output=True)
    assert result.returncode != 0 and result.stderr.strip() == b"release_output_not_empty"
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}
    assert_rejected(repo, "release_timestamp_invalid", changes={"SOURCE_DATE_EPOCH": "not-a-date"})
    assert_rejected(repo, "release_timestamp_invalid", changes={"SOURCE_DATE_EPOCH": "4294967296"})
    _, result = repo.build("bad-output", extra=("--output", str(repo.root / "public-assets")))
    assert result.returncode != 0 and result.stderr.strip() == b"release_output_not_ignored"
    assert not (repo.root / "public-assets").exists()
    # Change the actual payload but preserve member names and sizes: extraction must hash.
    bad_tar = run / "tampered.tar.gz"
    with tarfile.open(output / release.artifact.name, "r:gz") as source, tarfile.open(bad_tar, "w:gz") as target:
        for member in source:
            payload = source.extractfile(member).read()
            if member.name == "frontend/index.html":
                payload = bytes([payload[0] ^ 1]) + payload[1:]
            target.addfile(member, io.BytesIO(payload))
    bad_extract = run / "bad-extract"
    bad_extract.mkdir()
    updater_error("update_archive_hash_mismatch", lambda: UPDATER.extract_verified_archive(bad_tar, bad_extract, expected))
    bad_manifest = json.loads(manifest_raw)
    bad_manifest["files"][0]["path"] = "../outside"
    updater_error("update_archive_path_invalid", lambda: UPDATER.parse_manifest(BUILDER.json_bytes(bad_manifest), release))
    responses = {
        release.artifact.api_url: (output / release.artifact.name).read_bytes(),
        release.manifest.api_url: manifest_raw, release.signature.api_url: signature_raw,
    }
    public_file = output / f"xianyu-saas-{repo.version}.update-signing.pub"
    with public_key_environment(public_file), patch.dict(os.environ, {
        "SAAS_UPDATE_STAGING_DIR": str(run / "staging"), "SAAS_CURRENT_ROOT": str(repo.root),
    }):
        staged = UPDATER.stage_release(release, "stable", "0.1.0", session=OfflineSession(responses))
        assert Path(staged["candidate_path"]).is_dir()
        broken = dict(responses)
        archive_raw = bytearray(broken[release.artifact.api_url])
        archive_raw[-1] ^= 1
        broken[release.artifact.api_url] = bytes(archive_raw)
        updater_error("update_artifact_hash_mismatch", lambda: UPDATER.stage_release(release, "stable", "0.1.0", session=OfflineSession(broken)))
    print("release bundle: reproducibility, assets, real verifier and tampering passed")


def key_and_version_contract(run: Path):
    repo = Repository(run / "keys", public_pem=True)
    output, result = repo.build()
    assert_success(result)
    verify_bundle(repo, output)
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
    _, result = prerelease.build()
    assert_success(result)
    print("release bundle: key validation and commit version consistency passed")


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
    for index, kind in enumerate(("private-key", "active-seed", "provider-secret")):
        repo = Repository(run / f"content-{index}")
        if kind == "private-key":
            payload = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        elif kind == "active-seed":
            payload = repo.encoded.encode()
        else:
            payload = ("gh" + "p_" + "x" * 32).encode()
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
    verify_bundle(repo, output)
    repo = Repository(run / "atomic")
    cwd = Path.cwd()
    try:
        os.chdir(repo.root)
        with patch.dict(os.environ, {"RELEASE_SIGNING_KEY": repo.encoded}), patch.object(BUILDER, "write_source_zip", side_effect=OSError("injected write failure")), contextlib.redirect_stderr(io.StringIO()) as errors:
            assert BUILDER.main(["--output", ".local/releases/failure"]) == 1
        assert errors.getvalue().strip() == "release_build_failed"
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


def main():
    with tempfile.TemporaryDirectory(prefix="xianyu-release-contract-") as temporary:
        run = Path(temporary)
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
            normal_and_tamper_contract(run)
            key_and_version_contract(run)
            privacy_contract(run)
            dirty_and_atomic_contract(run)
    print("release bundle contract: ok (temporary keys/repos only; real updater verified)")


if __name__ == "__main__":
    main()
