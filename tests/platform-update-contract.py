#!/usr/bin/env python3
"""Signed release staging, malicious archive and rollback contracts."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-saas-update-contract-"))
SOURCE_ROOT = RUN_DIR / "source"
STAGING_ROOT = RUN_DIR / "state" / "update-staging"
INTENT_FILE = RUN_DIR / "state" / "update-intents" / "intent.json"
PUBLIC_KEY_FILE = RUN_DIR / "update-signing.pub"
os.environ.update(
    {
        "SAAS_CURRENT_ROOT": str(SOURCE_ROOT),
        "SAAS_UPDATE_STAGING_DIR": str(STAGING_ROOT),
        "SAAS_UPDATE_INTENT_FILE": str(INTENT_FILE),
        "SAAS_UPDATE_PUBLIC_KEY_FILE": str(PUBLIC_KEY_FILE),
        "SAAS_RELEASES_DIR": str(RUN_DIR / "install" / "releases"),
        "SAAS_TESTING": "1",
        "SAAS_RESTORE_WORKERS": "0",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from db import DB  # noqa: E402
from platform_update import (  # noqa: E402
    CACHED_MANIFEST_FILE,
    CACHED_SIGNATURE_FILE,
    MARKER_FILE,
    PlatformUpdateError,
    ReleaseAsset,
    ReleaseInfo,
    _asset_names,
    available_rollback_versions,
    fetch_release,
    load_verified_candidate,
    stage_release,
    validate_candidate,
    write_update_intent,
)


PRIVATE_KEY = Ed25519PrivateKey.generate()
PUBLIC_KEY_FILE.write_bytes(
    base64.b64encode(
        PRIVATE_KEY.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
)
PUBLIC_KEY_FILE.chmod(0o644)


class FakeResponse:
    def __init__(self, payload: bytes, status_code: int = 200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = {"content-length": str(len(payload)), **(headers or {})}
        self.text = payload.decode("utf-8", "replace")

    def iter_content(self, chunk_size=64 * 1024):
        for offset in range(0, len(self.payload), max(int(chunk_size), 1)):
            yield self.payload[offset : offset + chunk_size]

    def close(self):
        return None

    def json(self):
        return json.loads(self.payload.decode("utf-8"))


class FakeSession:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.seen = []

    def get(self, url, **kwargs):
        self.seen.append((url, kwargs))
        response = self.responses[url]
        if callable(response):
            response = response()
        return response


def write_source_root() -> None:
    (SOURCE_ROOT / "backend").mkdir(parents=True)
    (SOURCE_ROOT / "frontend").mkdir(parents=True)
    package = {
        "name": "xianyu-saas",
        "version": "0.1.0",
        "dependencies": {"safe": "1.0.0"},
        "devDependencies": {"test": "1.0.0"},
    }
    lock = {
        "name": package["name"],
        "version": package["version"],
        "lockfileVersion": 3,
        "packages": {
            "": {
                "name": package["name"],
                "version": package["version"],
                "dependencies": package["dependencies"],
                "devDependencies": package["devDependencies"],
            },
            "node_modules/safe": {"version": "1.0.0"},
            "node_modules/test": {"version": "1.0.0"},
        },
    }
    (SOURCE_ROOT / "package.json").write_text(
        json.dumps(package, sort_keys=True), encoding="utf-8"
    )
    (SOURCE_ROOT / "package-lock.json").write_text(
        json.dumps(lock, sort_keys=True), encoding="utf-8"
    )
    (SOURCE_ROOT / "backend" / "requirements.txt").write_text(
        "requests==2.32.5\ncryptography==46.0.1\n", encoding="utf-8"
    )
    (SOURCE_ROOT / "backend" / "version.py").write_text(
        'VERSION = "0.1.0"\nASSET_VERSION = "contract-asset"\n', encoding="utf-8"
    )
    (SOURCE_ROOT / "frontend" / "index.html").write_text(
        "<html>contract-asset</html>", encoding="utf-8"
    )


def candidate_files(version: str, *, dependency="1.0.0") -> dict[str, tuple[bytes, bool]]:
    package = {
        "name": "xianyu-saas",
        "version": version,
        "dependencies": {"safe": dependency},
        "devDependencies": {"test": "1.0.0"},
    }
    lock = {
        "name": package["name"],
        "version": version,
        "lockfileVersion": 3,
        "packages": {
            "": {
                "name": package["name"],
                "version": version,
                "dependencies": package["dependencies"],
                "devDependencies": package["devDependencies"],
            },
            "node_modules/safe": {"version": dependency},
            "node_modules/test": {"version": "1.0.0"},
        },
    }
    return {
        "package.json": (json.dumps(package, sort_keys=True).encode(), False),
        "package-lock.json": (json.dumps(lock, sort_keys=True).encode(), False),
        "backend/requirements.txt": (
            b"requests==2.32.5\ncryptography==46.0.1\n",
            False,
        ),
        "backend/version.py": (
            f'VERSION = "{version}"\nASSET_VERSION = "contract-asset"\n'.encode(),
            False,
        ),
        "frontend/index.html": (b"<html>contract-asset</html>", False),
        "scripts/release-check.sh": (b"#!/bin/sh\nexit 0\n", True),
    }


def build_bundle(
    version: str,
    *,
    dependency="1.0.0",
    extra_member=None,
    manifest_path_override: str | None = None,
):
    files = candidate_files(version, dependency=dependency)
    artifact_name, manifest_name, signature_name = _asset_names(version)
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        for path, (payload, executable) in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(payload)
            info.mode = 0o755 if executable else 0o644
            archive.addfile(info, io.BytesIO(payload))
        if extra_member is not None:
            if isinstance(extra_member, tuple):
                member, member_payload = extra_member
                archive.addfile(member, io.BytesIO(member_payload))
            else:
                archive.addfile(extra_member)
    archive_raw = archive_buffer.getvalue()
    manifest_files = []
    for path, (payload, executable) in files.items():
        manifest_files.append(
            {
                "path": manifest_path_override if manifest_path_override and path == "package.json" else path,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "executable": executable,
            }
        )
    manifest = {
        "schema": 1,
        "version": version,
        "artifact": artifact_name,
        "artifact_sha256": hashlib.sha256(archive_raw).hexdigest(),
        "artifact_size": len(archive_raw),
        "files": manifest_files,
    }
    manifest_raw = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    signature_raw = base64.b64encode(PRIVATE_KEY.sign(manifest_raw))
    release = ReleaseInfo(
        release_id=f"release-{version}",
        version=version,
        tag=f"v{version}",
        published_at="2026-08-30T00:00:00Z",
        notes=f"Release {version}",
        prerelease="-" in version,
        artifact=ReleaseAsset(100, artifact_name, len(archive_raw)),
        manifest=ReleaseAsset(101, manifest_name, len(manifest_raw)),
        signature=ReleaseAsset(102, signature_name, len(signature_raw)),
    )
    assets = {
        release.artifact.api_url: FakeResponse(archive_raw),
        release.manifest.api_url: FakeResponse(manifest_raw),
        release.signature.api_url: FakeResponse(signature_raw),
    }
    return release, assets, manifest_raw, signature_raw, archive_raw


def release_metadata(release: ReleaseInfo) -> dict:
    return {
        "id": release.release_id,
        "tag_name": release.tag,
        "draft": False,
        "prerelease": release.prerelease,
        "published_at": release.published_at,
        "body": release.notes,
        "assets": [
            {
                "id": release.artifact.asset_id,
                "name": release.artifact.name,
                "size": release.artifact.size,
            },
            {
                "id": release.manifest.asset_id,
                "name": release.manifest.name,
                "size": release.manifest.size,
            },
            {
                "id": release.signature.asset_id,
                "name": release.signature.name,
                "size": release.signature.size,
            },
        ],
    }


def assert_error(code: str, operation) -> None:
    try:
        operation()
    except PlatformUpdateError as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError(f"expected PlatformUpdateError: {code}")


def install_signed_release(destination: Path, version: str) -> None:
    release, _, manifest_raw, signature_raw, _ = build_bundle(version)
    files = candidate_files(version)
    destination.mkdir(parents=True)
    for relative, (payload, executable) in files.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(0o755 if executable else 0o644)
    (destination / CACHED_MANIFEST_FILE).write_bytes(manifest_raw)
    (destination / CACHED_SIGNATURE_FILE).write_bytes(signature_raw)
    marker = {
        "schema": 1,
        "version": version,
        "channel": "stable",
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "release_id": release.release_id,
        "artifact": release.artifact.name,
        "artifact_size": release.artifact.size,
    }
    (destination / MARKER_FILE).write_text(
        json.dumps(marker, sort_keys=True), encoding="utf-8"
    )
    for internal in (CACHED_MANIFEST_FILE, CACHED_SIGNATURE_FILE, MARKER_FILE):
        (destination / internal).chmod(0o600)


def load_updater_module():
    path = ROOT / "deploy" / "updater" / "updater.py"
    spec = importlib.util.spec_from_file_location("xianyu_contract_updater", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    write_source_root()
    stable, stable_assets, _, _, _ = build_bundle("0.2.0")
    beta, _, _, _, _ = build_bundle("0.3.0-beta.1")
    metadata_url = "https://api.github.com/repos/tswawa/xianyu-saas/releases?per_page=30"
    metadata = [
        {"tag_name": "nightly", "draft": False},
        release_metadata(beta),
        release_metadata(stable),
    ]
    session = FakeSession(
        {
            metadata_url: FakeResponse(json.dumps(metadata).encode()),
            **stable_assets,
        }
    )
    selected = fetch_release("stable", "0.1.0", session=session)
    assert selected is not None and selected.version == "0.2.0"
    assert all(call[1]["allow_redirects"] is False for call in session.seen)

    redirect = FakeSession(
        {metadata_url: FakeResponse(b"", status_code=302, headers={"location": "https://evil.invalid"})}
    )
    assert_error(
        "update_redirect_rejected",
        lambda: fetch_release("stable", "0.1.0", session=redirect),
    )

    staged = stage_release(stable, "stable", "0.1.0", session=FakeSession(stable_assets))
    candidate = Path(staged["candidate_path"])
    assert candidate.is_dir()
    marker, expected_files = load_verified_candidate(
        str(candidate), stable.version, staged["manifest_sha256"]
    )
    assert marker["version"] == "0.2.0"
    assert "package.json" in expected_files
    assert not any(path.suffix in {".db", ".log"} for path in candidate.rglob("*"))

    original_package = (candidate / "package.json").read_bytes()
    (candidate / "package.json").write_bytes(original_package + b" ")
    assert_error(
        "update_archive_manifest_mismatch",
        lambda: validate_candidate(
            str(candidate), stable.version, staged["manifest_sha256"]
        ),
    )
    shutil.rmtree(candidate.parent)

    wrong_signature = dict(stable_assets)
    wrong_signature[stable.signature.api_url] = FakeResponse(base64.b64encode(b"x" * 64))
    assert_error(
        "update_signature_invalid",
        lambda: stage_release(
            stable, "stable", "0.1.0", session=FakeSession(wrong_signature)
        ),
    )

    malicious_release, malicious_assets, _, _, _ = build_bundle(
        "0.2.1", manifest_path_override="../package.json"
    )
    assert_error(
        "update_archive_path_invalid",
        lambda: stage_release(
            malicious_release,
            "stable",
            "0.1.0",
            session=FakeSession(malicious_assets),
        ),
    )

    symlink = tarfile.TarInfo("frontend/link")
    symlink.type = tarfile.SYMTYPE
    symlink.linkname = "/etc/passwd"
    symlink.size = 0
    linked_release, linked_assets, _, _, _ = build_bundle("0.2.2", extra_member=symlink)
    assert_error(
        "update_archive_link_rejected",
        lambda: stage_release(
            linked_release,
            "stable",
            "0.1.0",
            session=FakeSession(linked_assets),
        ),
    )

    hardlink = tarfile.TarInfo("frontend/hard-link")
    hardlink.type = tarfile.LNKTYPE
    hardlink.linkname = "frontend/index.html"
    hardlink.size = 0
    hardlink_release, hardlink_assets, _, _, _ = build_bundle("0.2.3", extra_member=hardlink)
    assert_error(
        "update_archive_link_rejected",
        lambda: stage_release(
            hardlink_release,
            "stable",
            "0.1.0",
            session=FakeSession(hardlink_assets),
        ),
    )

    device = tarfile.TarInfo("frontend/device-node")
    device.type = tarfile.CHRTYPE
    device.devmajor = 1
    device.devminor = 3
    device.size = 0
    device_release, device_assets, _, _, _ = build_bundle("0.2.4", extra_member=device)
    assert_error(
        "update_archive_type_rejected",
        lambda: stage_release(
            device_release,
            "stable",
            "0.1.0",
            session=FakeSession(device_assets),
        ),
    )

    duplicate_payload = candidate_files("0.2.5")["package.json"][0]
    duplicate = tarfile.TarInfo("package.json")
    duplicate.size = len(duplicate_payload)
    duplicate_release, duplicate_assets, _, _, _ = build_bundle(
        "0.2.5", extra_member=(duplicate, duplicate_payload)
    )
    assert_error(
        "update_archive_duplicate",
        lambda: stage_release(
            duplicate_release,
            "stable",
            "0.1.0",
            session=FakeSession(duplicate_assets),
        ),
    )

    runtime_release, runtime_assets, _, _, _ = build_bundle(
        "0.2.6", manifest_path_override="data/saas.db"
    )
    assert_error(
        "update_runtime_path_rejected",
        lambda: stage_release(
            runtime_release,
            "stable",
            "0.1.0",
            session=FakeSession(runtime_assets),
        ),
    )
    absolute_release, absolute_assets, _, _, _ = build_bundle(
        "0.2.7", manifest_path_override="/package.json"
    )
    assert_error(
        "update_archive_path_invalid",
        lambda: stage_release(
            absolute_release,
            "stable",
            "0.1.0",
            session=FakeSession(absolute_assets),
        ),
    )

    checksum_release, checksum_assets, _, _, checksum_archive = build_bundle("0.2.8")
    tampered_archive = bytearray(checksum_archive)
    tampered_archive[-1] ^= 1
    checksum_assets[checksum_release.artifact.api_url] = FakeResponse(bytes(tampered_archive))
    assert_error(
        "update_artifact_hash_mismatch",
        lambda: stage_release(
            checksum_release,
            "stable",
            "0.1.0",
            session=FakeSession(checksum_assets),
        ),
    )

    dependency_release, dependency_assets, _, _, _ = build_bundle(
        "0.2.3", dependency="9.9.9"
    )
    assert_error(
        "update_dependency_change_rejected",
        lambda: stage_release(
            dependency_release,
            "stable",
            "0.1.0",
            session=FakeSession(dependency_assets),
        ),
    )
    assert_error(
        "update_downgrade_rejected",
        lambda: stage_release(
            stable, "stable", "0.2.0", session=FakeSession(stable_assets)
        ),
    )

    clean_staged = stage_release(
        stable, "stable", "0.1.0", session=FakeSession(stable_assets)
    )
    intent_result = write_update_intent(
        "apply",
        stable.version,
        channel="stable",
        requested_by=1,
        candidate_path=clean_staged["candidate_path"],
        manifest_sha256=clean_staged["manifest_sha256"],
    )
    assert intent_result == {"queued": True, "action": "apply", "version": "0.2.0"}
    assert INTENT_FILE.stat().st_mode & 0o077 == 0
    assert_error(
        "update_intent_pending",
        lambda: write_update_intent(
            "apply",
            stable.version,
            channel="stable",
            requested_by=1,
            candidate_path=clean_staged["candidate_path"],
            manifest_sha256=clean_staged["manifest_sha256"],
        ),
    )
    INTENT_FILE.unlink()

    updater = load_updater_module()
    install_root = RUN_DIR / "install"
    releases = install_root / "releases"
    old_release = releases / "0.1.0"
    install_signed_release(old_release, "0.1.0")
    current = install_root / "current"
    current.symlink_to(old_release)
    state = RUN_DIR / "runtime-state"
    state.mkdir()
    database_path = state / "saas.db"
    database = DB(str(database_path))
    database.create_user("update-owner", "Update-Contract-123!", role="admin")
    runtime_sentinel = state / "tenant-runtime-sentinel"
    runtime_sentinel.write_text("must survive", encoding="utf-8")
    config = updater.Config(
        releases_dir=releases,
        current_link=current,
        staging_dir=STAGING_ROOT,
        state_dir=state,
        database_path=database_path,
        intent_file=state / "update-intents" / "intent.json",
        lock_file=state / "updater.lock",
        backup_dir=state / "backups",
        api_service="xianyu-saas.service",
        consumer_service="xianyu-saas-consumer.service",
        health_base_url="http://127.0.0.1:8096/",
        public_base_url="http://127.0.0.1/xianyu-saas/",
    )
    updater.validate_layout(config)
    lock_descriptor = updater.acquire_lock(config)
    try:
        try:
            updater.acquire_lock(config)
        except updater.UpdaterError as exc:
            assert exc.code == "update_already_running"
        else:
            raise AssertionError("updater lock must reject concurrent execution")
    finally:
        os.close(lock_descriptor)

    # A second updater must not delete the first process's claimed intent when
    # it exits after failing to acquire the lock.
    processing_path = config.intent_file.with_name("intent.processing.json")
    processing_path.parent.mkdir(parents=True, exist_ok=True)
    processing_path.write_text("{}", encoding="utf-8")
    with patch.object(updater.Config, "from_env", return_value=config), patch.object(
        updater, "acquire_lock", side_effect=updater.UpdaterError("update_already_running")
    ):
        assert updater.main() == 1
    assert processing_path.exists(), "a concurrent updater must preserve the claimed intent"
    processing_path.unlink()

    os.environ["SAAS_GITHUB_READ_TOKEN"] = "must-not-reach-updater-child"
    captured_child = {}

    def capture_child(_command, **kwargs):
        captured_child.update(kwargs.get("env") or {})
        return SimpleNamespace(returncode=0)

    with patch.object(updater.subprocess, "run", side_effect=capture_child):
        updater.SystemRunner().run(["systemctl", "is-active", "xianyu-saas.service"])
    assert "SAAS_GITHUB_READ_TOKEN" not in captured_child
    assert captured_child["PYTHONDONTWRITEBYTECODE"] == "1"
    captured_migration = {}

    def capture_migration(_command, **kwargs):
        captured_migration.update(kwargs.get("env") or {})
        return SimpleNamespace(returncode=0)

    with patch.object(updater.subprocess, "run", side_effect=capture_migration):
        updater.run_migrations(old_release, config, updater.SystemRunner())
    assert captured_migration["SAAS_DB"] == str(database_path)
    assert "SAAS_GITHUB_READ_TOKEN" not in captured_migration
    os.environ.pop("SAAS_GITHUB_READ_TOKEN", None)

    stale_apply = updater.Intent(
        action="apply",
        version="0.0.9",
        channel="stable",
        candidate_path="",
        manifest_sha256="",
        requested_by=1,
        nonce="d" * 32,
    )
    try:
        updater.process_intent(
            config,
            stale_apply,
            runner=object(),
            health_client=object(),
            database=database,
        )
    except updater.UpdaterError as exc:
        assert exc.code == "update_downgrade_rejected"
    else:
        raise AssertionError("updater must independently reject stale apply intents")

    forward_rollback = updater.Intent(
        action="rollback",
        version="0.2.0",
        channel="stable",
        candidate_path="",
        manifest_sha256="",
        requested_by=1,
        nonce="e" * 32,
    )
    try:
        updater.process_intent(
            config,
            forward_rollback,
            runner=object(),
            health_client=object(),
            database=database,
        )
    except updater.UpdaterError as exc:
        assert exc.code == "update_rollback_target_invalid"
    else:
        raise AssertionError("rollback must target an older signed release")

    update_intent = updater.Intent(
        action="apply",
        version="0.2.0",
        channel="stable",
        candidate_path=clean_staged["candidate_path"],
        manifest_sha256=clean_staged["manifest_sha256"],
        requested_by=1,
        nonce="a" * 32,
    )
    materialized = updater.materialize_release(config, update_intent)
    assert materialized == releases / "0.2.0"
    updater.verify_existing_release(config, "0.2.0")
    assert available_rollback_versions("0.1.0") == [], "newer releases are not rollback targets"
    updater.switch_current(config, materialized)
    assert updater.current_release(config) == materialized.resolve()
    assert available_rollback_versions("0.2.0") == ["0.1.0"]
    updater.switch_current(config, old_release)

    class FakeRunner:
        def __init__(self):
            self.commands = []

        def run(self, command, **_kwargs):
            self.commands.append(command)

    runner = FakeRunner()
    failing_intent = updater.Intent(
        action="apply",
        version="0.2.0",
        channel="stable",
        candidate_path=clean_staged["candidate_path"],
        manifest_sha256=clean_staged["manifest_sha256"],
        requested_by=1,
        nonce="b" * 32,
    )
    health_versions = []

    def fail_new_health(_config, version, _client):
        health_versions.append(version)
        if version == "0.2.0":
            raise updater.UpdaterError("update_health_failed")

    with (
        patch.object(updater, "materialize_release", return_value=materialized),
        patch.object(updater, "run_migrations", return_value=None),
        patch.object(updater, "check_health", side_effect=fail_new_health),
    ):
        try:
            updater.process_intent(
                config,
                failing_intent,
                runner=runner,
                health_client=object(),
                database=database,
            )
        except updater.UpdaterError as exc:
            assert exc.code == "update_health_failed"
        else:
            raise AssertionError("health failure must trigger rollback")
    assert updater.current_release(config) == old_release.resolve()
    assert health_versions == ["0.2.0", "0.1.0"]
    assert runtime_sentinel.read_text(encoding="utf-8") == "must survive"
    assert database.get_user("update-owner") is not None
    latest = database.get_platform_update("0.2.0", "stable")
    assert latest["status"] == "rolled_back"
    assert latest["error_code"] == "update_health_failed"
    existing_backups = list(config.backup_dir.glob("*.db"))
    assert existing_backups
    worker_intents = updater.snapshot_worker_intents(database_path)
    first_backup = updater.backup_database(config, "0.2.1", worker_intents)
    second_backup = updater.backup_database(config, "0.2.2", worker_intents)
    assert first_backup != second_backup
    assert first_backup.stat().st_mode & 0o077 == 0
    assert second_backup.stat().st_mode & 0o077 == 0
    assert not materialized.exists()

    for index, version in enumerate(("0.0.7", "0.0.8", "0.0.9"), start=1):
        release_dir = releases / version
        release_dir.mkdir(exist_ok=True)
        os.utime(release_dir, (1000 + index, 1000 + index))
    os.utime(old_release, (900, 900))
    updater.prune_releases(config)
    retained = {
        path.name for path in releases.iterdir()
        if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
    }
    assert len(retained) == 3
    assert "0.1.0" in retained, "the active release must be retained within the three-version cap"
    print("platform update contract: ok")


if __name__ == "__main__":
    main()
