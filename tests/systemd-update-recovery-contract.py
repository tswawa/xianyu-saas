#!/usr/bin/env python3
"""Static checks everywhere; real-fd/signature/SIGKILL contracts on Linux only.

No systemctl command is executed. All releases, keys, SQLite rows and HTTP
responses are synthetic and isolated in TemporaryDirectory. No fcntl shim is
installed: Windows explicitly skips Linux execution and permission contracts.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
UPDATER_FILE = ROOT / "deploy/updater/updater.py"
THIS_FILE = Path(__file__).resolve()


def static_contracts() -> None:
    source = UPDATER_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    for name in ("_directory_fd", "_read_protected_file", "_read_private_json", "_parse_intent",
                 "_open_journal", "_save_journal", "_recover", "_publish_state", "initialize_layout",
                 "import_trusted_baseline", "_parse_cli", "_supports_signed_maintenance_protocol",
                 "_installed_bundle_identity", "_trusted_public_current_version"):
        assert name in functions, name
    parser = ast.get_source_segment(source, functions["_parse_intent"])
    assert all(f'"{channel}"' in parser for channel in ("release", "stable", "beta"))
    assert '"operation_id"' in parser and '"expected_current_version"' in parser
    journal_open = ast.get_source_segment(source, functions["_open_journal"])
    rejector = ast.get_source_segment(source, functions["_discard_rejected_intent"])
    assert 'intent.expected_current_version' in journal_open
    assert "rejected.expected_current_version" in rejector
    assert '"current_version": ""' not in source
    protected = ast.get_source_segment(source, functions["_read_protected_file"])
    assert "O_NOFOLLOW" in protected and "st_nlink" in protected and "_file_identity" in protected
    status_writer = ast.get_source_segment(source, functions["update_status"])
    assert "upsert_platform_update(" not in status_writer
    assert "UPDATE platform_updates SET status" in status_writer
    health = ast.get_source_segment(source, functions["check_health"])
    assert "asset_version.strip()" in health
    assert "import fcntl" in source and "fcntl.flock" in source
    assert "from update_maintenance import supports_maintenance_protocol" in source
    assert "BASELINE_IMPORT_OPTION" in source and "extract_verified_archive" in source
    protocol_gate = ast.get_source_segment(source, functions["_supports_signed_maintenance_protocol"])
    baseline_import = ast.get_source_segment(source, functions["import_trusted_baseline"])
    initializer = ast.get_source_segment(source, functions["initialize_layout"])
    bundle_identity = ast.get_source_segment(source, functions["_installed_bundle_identity"])
    assert "exec(" not in protocol_gate and "import_module" not in protocol_gate
    assert 'config.status_dir / "initialization.json"' in initializer
    assert all(name in initializer for name in (
        '"schema"', '"protocol"', '"public_key_sha256"', '"bundle_sha256"',
        '"entrypoint_sha256"', '"initialized_at"'
    ))
    assert "UPDATER_BUNDLE_FILES" in bundle_identity and "load_public_key" in bundle_identity
    assert "verify_manifest_signature" in baseline_import and "_copy_protected_archive" in baseline_import
    assert all(forbidden not in baseline_import for forbidden in (
        "switch_current(", "stop_services(", "start_services(", "run_migrations(", "backup_database("
    ))
    service = (ROOT / "deploy/systemd/xianyu-saas-updater.service").read_text(encoding="utf-8")
    watcher = (ROOT / "deploy/systemd/xianyu-saas-updater.path").read_text(encoding="utf-8")
    bundle_root = PurePosixPath("/") / "opt" / "xianyu-saas" / "updater"
    updater_binary = str(bundle_root / "deploy" / "updater" / "updater.py")
    public_key = str(bundle_root / "deploy" / "update-signing.pub")
    intent = str(PurePosixPath("/") / "var" / "lib" / "xianyu-saas-updates" / "intent.json")
    active = str(PurePosixPath("/") / "var" / "lib" / "xianyu-saas-updater" / "active.json")
    assert updater_binary in service
    assert f"Environment=SAAS_UPDATE_PUBLIC_KEY_FILE={public_key}" in service
    assert f"Environment=SAAS_UPDATER_BUNDLE_ROOT={bundle_root}" in service
    assert f"Environment=SAAS_UPDATER_ENTRYPOINT={updater_binary}" in service
    assert "Restart=on-failure" in service and "StateDirectoryMode=0700" in service
    assert f"PathExists={active}" in watcher
    assert f"PathExists={intent}" in watcher
    assert "DirectoryMode=01770" in watcher
    print("systemd recovery static contract: passed")


def load_updater(bundle_root: Path | None = None):
    assert sys.platform == "linux", "Linux runtime contracts may not use an fcntl shim"
    temporary = None
    if bundle_root is None:
        # sudo does not make a CI checkout root-owned. Provision the independent
        # installed bundle with real ownership/modes instead of relaxing trust.
        temporary = tempfile.TemporaryDirectory(prefix="xianyu-systemd-bundle-")
        bundle_root = Path(temporary.name)
        tree = ast.parse(UPDATER_FILE.read_text(encoding="utf-8"))
        files = next(ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign) and any(
                         isinstance(target, ast.Name) and target.id == "UPDATER_BUNDLE_FILES"
                         for target in node.targets))
        for relative in files:
            target = bundle_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
            target.chmod(0o644)
        for directory, _, _ in os.walk(bundle_root):
            Path(directory).chmod(0o755)
    sys.path.insert(0, str(ROOT / "backend"))  # App-only contracts are not part of the installed updater.
    sys.path.insert(0, str(bundle_root / "backend"))
    spec = importlib.util.spec_from_file_location("systemd_recovery_updater", bundle_root / "deploy/updater/updater.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._contract_bundle = temporary  # Keep the real installed tree alive for all fixtures.
    return module


def expect_error(code, operation):
    try:
        operation()
    except Exception as exc:
        assert getattr(exc, "code", "") == code, (type(exc).__name__, str(exc), getattr(exc, "code", None), code)
    else:
        raise AssertionError(f"expected {code}")


def write_json(path: Path, payload: dict, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(mode)


def code_files(version: str, *, dependency="1.0.0", migration="", maintenance=False):
    package = {"name": "xianyu-saas", "version": version, "dependencies": {"safe": dependency}, "devDependencies": {}}
    lock = {"name": "xianyu-saas", "version": version, "lockfileVersion": 3,
            "packages": {"": package, "node_modules/safe": {"version": dependency}}}
    maintenance_source = (b"MAINTENANCE_PROTOCOL = 1\n" if maintenance is True
                          else maintenance if isinstance(maintenance, bytes) else None)
    return {
        **({"backend/update_maintenance.py": maintenance_source} if maintenance_source is not None else {}),
        "package.json": json.dumps(package, sort_keys=True).encode(),
        "package-lock.json": json.dumps(lock, sort_keys=True).encode(),
        "backend/requirements.txt": b"requests==2.32.5\ncryptography==46.0.1\n",
        "backend/version.py": f'VERSION = "{version}"\nASSET_VERSION = "fixture-{version}"\n'.encode(),
        "backend/db.py": ("import os, sqlite3\nclass DB:\n"
                          "    def __init__(self):\n"
                          "        self.con = sqlite3.connect(os.environ['SAAS_DB'])\n"
                          + (f"        {migration}\n" if migration else "")
                          + "    def is_ready(self):\n        return self.con.execute('SELECT 1').fetchone()[0] == 1\n").encode(),
        "frontend/index.html": f'<html><script src="assets/app.js?v=fixture-{version}"></script></html>'.encode(),
        "frontend/assets/app.js": b"/* synthetic signed asset */\n",
    }


def install_signed(updater, key, destination: Path, version: str, *, dependency="1.0.0", migration="", maintenance=False) -> str:
    files = code_files(version, dependency=dependency, migration=migration, maintenance=maintenance)
    destination.mkdir(parents=True)
    for relative, raw in files.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o644)
    artifact = f"xianyu-saas-{version}.tar.gz"
    # The candidate verifier checks signed extracted files. The artifact identity
    # is synthetic but correctly bound to the signed schema-1 marker/manifest.
    manifest = {"schema": 1, "version": version, "artifact": artifact,
                "artifact_size": 8, "artifact_sha256": hashlib.sha256(b"fixture!").hexdigest(),
                "files": [{"path": path, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "executable": False}
                          for path, raw in files.items()]}
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    (destination / updater.CACHED_MANIFEST_FILE).write_bytes(raw)
    (destination / updater.CACHED_SIGNATURE_FILE).write_bytes(base64.b64encode(key.sign(raw)))
    write_json(destination / updater.MARKER_FILE, {"schema": 1, "version": version, "channel": "release",
               "manifest_sha256": digest, "release_id": f"synthetic-{version}", "artifact": artifact, "artifact_size": 8})
    for name in updater.INTERNAL_CANDIDATE_FILES:
        (destination / name).chmod(0o600)  # Exercise legacy root-only metadata.
    for directory, _, _ in os.walk(destination):
        Path(directory).chmod(0o755)
    return digest


def build_offline_bundle(updater, key, root: Path, version: str, *, maintenance=True, dependency="1.0.0"):
    files = code_files(version, dependency=dependency, maintenance=maintenance)
    root.mkdir(parents=True)
    artifact_name, manifest_name, signature_name = updater._asset_names(version)
    archive = root / artifact_name
    with tarfile.open(archive, mode="w:gz") as output:
        for relative, raw in sorted(files.items()):
            info = tarfile.TarInfo(relative)
            info.mode = 0o644
            info.size = len(raw)
            info.mtime = 1
            output.addfile(info, io.BytesIO(raw))
    archive_raw = archive.read_bytes()
    manifest = {
        "schema": 1,
        "version": version,
        "artifact": artifact_name,
        "artifact_size": len(archive_raw),
        "artifact_sha256": hashlib.sha256(archive_raw).hexdigest(),
        "files": [
            {"path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "executable": False}
            for relative, raw in sorted(files.items())
        ],
    }
    manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_path = root / manifest_name
    signature_path = root / signature_name
    manifest_path.write_bytes(manifest_raw)
    signature_path.write_bytes(base64.b64encode(key.sign(manifest_raw)))
    for path in (archive, manifest_path, signature_path):
        path.chmod(0o644)
    root.chmod(0o755)
    return archive, manifest_path, signature_path, hashlib.sha256(manifest_raw).hexdigest()


class HealthServer:
    def __init__(self, config):
        self.config = config
        self.fail_versions = set()
        self.empty_asset = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                version = owner.config.current_link.resolve().name
                if version in owner.fail_versions:
                    status, body, kind = 503, b"unhealthy", "text/plain"
                elif self.path == "/health":
                    status, body, kind = 200, b'{"ok":true}', "application/json"
                elif self.path == "/api/ready":
                    status, body, kind = 200, b'{"database":"ready"}', "application/json"
                elif self.path == "/api/me":
                    status, body, kind = 401, b"{}", "application/json"
                elif self.path == "/api/version/public":
                    status, body, kind = 200, json.dumps({"version": version, "asset_version": "" if owner.empty_asset else f"fixture-{version}"}).encode(), "application/json"
                else:
                    status, body, kind = 200, (owner.config.current_link / "frontend/index.html").read_bytes(), "text/html"
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Runner:
    """Record fixed service commands, never execute a service manager."""
    def __init__(self, config, *, fail_stop=False):
        self.config = config
        self.commands = []
        self.fail_stop = fail_stop

    def run(self, command, **_kwargs):
        assert command[0] == "systemctl" and command[1] in {"stop", "start"}
        assert set(command[2:]) <= {"xianyu-saas.service", "xianyu-saas-consumer.service"}
        self.commands.append(command)
        if self.fail_stop and command[1] == "stop":
            self.fail_stop = False
            raise RuntimeError("synthetic partial stop")


class Fixture:
    def __init__(self, updater, *, dependency="1.0.0", migration="", maintenance=True,
                 target_maintenance=None, initialize=True):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from db import DB
        self.updater = updater
        self.temporary = tempfile.TemporaryDirectory(prefix="xianyu-systemd-recovery-")
        self.root = Path(self.temporary.name)
        self.root.chmod(0o755)
        self.key = Ed25519PrivateKey.generate()
        keyfile = self.root / "signing.pub"
        keyfile.write_bytes(base64.b64encode(self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)))
        keyfile.chmod(0o644)
        releases = self.root / "install/releases"
        self.old = releases / "0.1.0"
        self.old_digest = install_signed(updater, self.key, self.old, "0.1.0", maintenance=maintenance)
        (self.root / "install").chmod(0o755)
        releases.chmod(0o755)
        current = self.root / "install/current"
        current.symlink_to(self.old)
        state = self.root / "state"
        state.mkdir(mode=0o700)
        database_path = state / "saas.db"
        database = DB(str(database_path))
        database.con.execute("CREATE TABLE contract_data (value TEXT NOT NULL)")
        database.con.execute("INSERT INTO contract_data VALUES ('synthetic-data-must-survive')")
        database.con.commit()
        self.candidate = state / "update-staging/operation/candidate"
        self.digest = install_signed(updater, self.key, self.candidate, "0.2.0", dependency=dependency, migration=migration,
                                     maintenance=maintenance if target_maintenance is None else target_maintenance)
        database.upsert_platform_update("0.2.0", "release", "apply_requested", release_id="retained-release-id",
                                        manifest_sha256=self.digest, candidate_path=str(self.candidate),
                                        release_notes="Release notes must survive all executor stages", requested_by=1)
        database.con.close()
        self.config = updater.Config(
            releases_dir=releases, current_link=current, staging_dir=state / "update-staging", state_dir=state,
            database_path=database_path, intent_file=self.root / "ipc/intent.json", lock_file=state / "legacy.lock",
            backup_dir=self.root / "private/backups", api_service="xianyu-saas.service",
            consumer_service="xianyu-saas-consumer.service", health_base_url="http://127.0.0.1/", public_base_url="http://127.0.0.1/",
            private_state_dir=self.root / "private", intent_owner_uid=os.geteuid())
        self.health = HealthServer(self.config)
        self.config = replace(self.config, health_base_url=self.health.base_url, public_base_url=self.health.base_url)
        self.health.config = self.config
        self.intent = updater.Intent(
            "apply", "0.2.0", "release", str(self.candidate), self.digest, 1,
            "a" * 32, expected_current_version="0.1.0"
        )
        self.environment = {"SAAS_CURRENT_ROOT": str(current), "SAAS_UPDATE_STAGING_DIR": str(self.config.staging_dir),
                            "SAAS_UPDATE_PUBLIC_KEY_FILE": str(keyfile), "SAAS_UPDATE_INTENT_FILE": str(self.config.intent_file),
                            "SAAS_UPDATER_STATE_DIR": str(self.config.private_state_dir),
                            "SAAS_UPDATER_BUNDLE_ROOT": str(updater.SCRIPT_ROOT), "SAAS_UPDATER_ENTRYPOINT": str(updater.SCRIPT_PATH),
                            "SAAS_TESTING": "1",
                            "SAAS_DB": str(database_path), "SAAS_TENANTS_DIR": str(self.config.tenants_dir),
                            "SAAS_RESTORE_WORKERS": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        self.environment_patch = patch.dict(os.environ, self.environment)
        self.environment_patch.start()
        if initialize:
            updater.initialize_layout(self.config)
        else:
            updater._prepare_private(self.config)
            updater._secure_directory(self.config.intent_file.parent)
            updater._secure_directory(self.config.status_dir, 0o755)
            updater._secure_directory(self.config.status_dir / "operations", 0o755)
        self.runner = Runner(self.config)
        self.real_health = updater.check_health

    def write_intent(self, payload=None):
        write_json(self.config.intent_file, payload or self.updater._intent_payload(self.intent))

    def run(self):
        descriptor = self.updater.acquire_lock(self.config)
        try:
            intent, _ = self.updater.claim_intent(self.config)
            result = self.updater.process_intent(self.config, intent, runner=self.runner)
            self.updater._remove_consumed_intent(self.config, intent)
            return result
        finally:
            os.close(descriptor)

    def journal(self):
        return self.updater._read_journal(self.config, self.intent.operation_id)

    def public(self):
        return json.loads((self.config.status_dir / "operations" / f"{self.intent.operation_id}.json").read_text())

    def data(self):
        with sqlite3.connect(self.config.database_path) as connection:
            return connection.execute("SELECT value FROM contract_data").fetchone()[0]

    def close(self):
        self.health.close()
        self.environment_patch.stop()
        self.temporary.cleanup()


@contextmanager
def fixture(updater, **kwargs):
    instance = Fixture(updater, **kwargs)
    try:
        # Real requests/JSON parsing, one attempt against the synthetic HTTP server.
        with patch.object(updater, "check_health", side_effect=lambda c, v, h: instance.real_health(c, v, h, attempts=1, interval=0)):
            yield instance
    finally:
        instance.close()


def baseline_import_contracts(updater):
    with fixture(updater, maintenance=True) as f:
        sentinel = f.config.state_dir / "business-state.must-not-change"
        sentinel.write_text("synthetic-live-state", encoding="utf-8")
        sentinel.chmod(0o640)
        sentinel_identity = (sentinel.stat().st_uid, sentinel.stat().st_gid, sentinel.stat().st_mode & 0o777)
        active_before = f.config.current_link.resolve()
        archive, manifest, signature, digest = build_offline_bundle(
            updater, f.key, f.root / "offline-baseline", "0.3.0"
        )
        action, arguments = updater._parse_cli(
            [updater.BASELINE_IMPORT_OPTION, str(archive), str(manifest), str(signature)]
        )
        assert action == "import" and arguments == (archive, manifest, signature)
        assert updater._parse_cli([]) == ("process", ())
        assert updater._parse_cli(["--initialize"]) == ("initialize", ())
        for argv, code in (
            (["--initialize", "extra"], "update_cli_invalid"),
            ([updater.BASELINE_IMPORT_OPTION, str(archive)], "update_cli_invalid"),
            (["--unknown"], "update_cli_invalid"),
            ([updater.BASELINE_IMPORT_OPTION, "relative.tar.gz", str(manifest), str(signature)], "update_import_path_invalid"),
        ):
            expect_error(code, lambda argv=argv: updater._parse_cli(argv))

        imported = updater.import_trusted_baseline(f.config, archive, manifest, signature)
        assert imported == f.config.releases_dir / "0.3.0"
        assert f.config.current_link.resolve() == active_before
        assert sentinel.read_text(encoding="utf-8") == "synthetic-live-state"
        assert (sentinel.stat().st_uid, sentinel.stat().st_gid, sentinel.stat().st_mode & 0o777) == sentinel_identity
        payload = updater._load_release_manifest(imported, "0.3.0")
        assert hashlib.sha256(payload["manifest_raw"]).hexdigest() == digest
        assert updater._supports_signed_maintenance_protocol(imported, payload["expected_files"], protected=True)
        for name in updater.INTERNAL_CANDIDATE_FILES:
            assert (imported / name).stat().st_mode & 0o777 == 0o644
        assert not any("baseline" in path.name and path.name.endswith(".partial")
                       for path in f.config.releases_dir.iterdir())

        before = (imported.stat().st_dev, imported.stat().st_ino, (imported / updater.MARKER_FILE).stat().st_mtime_ns)
        assert updater.import_trusted_baseline(f.config, archive, manifest, signature) == imported
        after = (imported.stat().st_dev, imported.stat().st_ino, (imported / updater.MARKER_FILE).stat().st_mtime_ns)
        assert after == before
        archive_raw = archive.read_bytes()
        changed_archive = bytearray(archive_raw)
        changed_archive[len(changed_archive) // 2] ^= 1
        archive.write_bytes(changed_archive)
        archive.chmod(0o644)
        expect_error(
            "update_artifact_hash_mismatch",
            lambda: updater.import_trusted_baseline(f.config, archive, manifest, signature),
        )
        assert (imported.stat().st_dev, imported.stat().st_ino) == before[:2]
        archive.write_bytes(archive_raw)
        archive.chmod(0o644)
        conflicting = build_offline_bundle(
            updater, f.key, f.root / "offline-conflict", "0.3.0", dependency="2.0.0"
        )
        expect_error(
            "update_release_exists",
            lambda: updater.import_trusted_baseline(f.config, *conflicting[:3]),
        )
        assert f.config.current_link.resolve() == active_before

        updater.initialize_layout(f.config)
        assert sentinel.read_text(encoding="utf-8") == "synthetic-live-state"
        assert (sentinel.stat().st_uid, sentinel.stat().st_gid, sentinel.stat().st_mode & 0o777) == sentinel_identity
        assert f.config.current_link.resolve() == active_before
        assert f.config.intent_file.parent.stat().st_mode & 0o7777 == 0o1770

    with fixture(updater, maintenance=True) as f:
        archive, manifest, signature, _ = build_offline_bundle(
            updater, f.key, f.root / "offline-bad-signature", "0.3.1"
        )
        signature.write_bytes(base64.b64encode(b"x" * 64))
        expect_error(
            "update_signature_invalid",
            lambda: updater.import_trusted_baseline(f.config, archive, manifest, signature),
        )
        assert not (f.config.releases_dir / "0.3.1").exists()

    with fixture(updater, maintenance=True) as f:
        archive, manifest, signature, _ = build_offline_bundle(
            updater, f.key, f.root / "offline-bad-archive", "0.3.2"
        )
        raw = bytearray(archive.read_bytes())
        raw[len(raw) // 2] ^= 1
        archive.write_bytes(raw)
        archive.chmod(0o644)
        expect_error(
            "update_artifact_hash_mismatch",
            lambda: updater.import_trusted_baseline(f.config, archive, manifest, signature),
        )
        assert not (f.config.releases_dir / "0.3.2").exists()

    for version, maintenance in (
        ("0.3.3", False),
        ("0.3.4", b"MAINTENANCE_PROTOCOL = 2\n"),
        ("0.3.5", b"MAINTENANCE_PROTOCOL = 1\nimport math as MAINTENANCE_PROTOCOL\n"),
    ):
        with fixture(updater, maintenance=True) as f:
            inputs = build_offline_bundle(
                updater, f.key, f.root / f"offline-protocol-{version}", version,
                maintenance=maintenance,
            )
            expect_error(
                "update_maintenance_protocol_unsupported",
                lambda inputs=inputs: updater.import_trusted_baseline(f.config, *inputs[:3]),
            )
            assert not (f.config.releases_dir / version).exists()

    with fixture(updater, maintenance=True) as f:
        inputs = build_offline_bundle(
            updater, f.key, f.root / "offline-static-only", "0.3.9",
            maintenance=b"MAINTENANCE_PROTOCOL = 1\nraise RuntimeError('candidate code executed')\n",
        )
        assert updater.import_trusted_baseline(f.config, *inputs[:3]).name == "0.3.9"

    with fixture(updater, maintenance=True) as f:
        inputs = build_offline_bundle(
            updater, f.key, f.root / "offline-path", "0.3.6"
        )
        archive, manifest, signature = inputs[:3]
        manifest.chmod(0o666)
        expect_error(
            "update_release_invalid",
            lambda: updater.import_trusted_baseline(f.config, archive, manifest, signature),
        )
        manifest.chmod(0o644)
        linked = archive.with_name("linked-" + archive.name)
        os.link(archive, linked)
        expect_error(
            "update_import_source_invalid",
            lambda: updater.import_trusted_baseline(f.config, archive, manifest, signature),
        )
        linked.unlink()
        renamed = manifest.with_name("renamed-manifest.json")
        manifest.rename(renamed)
        renamed.chmod(0o644)
        expect_error(
            "update_import_path_invalid",
            lambda: updater.import_trusted_baseline(f.config, archive, renamed, signature),
        )

    with fixture(updater, maintenance=True) as f:
        inputs = build_offline_bundle(
            updater, f.key, f.root / "offline-existing", "0.3.7"
        )
        occupied = f.config.releases_dir / "0.3.7"
        occupied.mkdir()
        marker = occupied / "must-survive"
        marker.write_text("do-not-overwrite", encoding="utf-8")
        expect_error(
            "update_release_exists",
            lambda: updater.import_trusted_baseline(f.config, *inputs[:3]),
        )
        assert marker.read_text(encoding="utf-8") == "do-not-overwrite"

    with fixture(updater, maintenance=True) as f:
        inputs = build_offline_bundle(
            updater, f.key, f.root / "offline-existing-link", "0.3.10"
        )
        occupied = f.config.releases_dir / "0.3.10"
        occupied.symlink_to(f.old, target_is_directory=True)
        expect_error(
            "update_release_exists",
            lambda: updater.import_trusted_baseline(f.config, *inputs[:3]),
        )
        assert occupied.is_symlink() and f.old.is_dir()

    with fixture(updater, maintenance=True) as f:
        active_before = f.config.current_link.resolve()
        inputs = build_offline_bundle(
            updater, f.key, f.root / "offline-cli", "0.3.8"
        )
        with patch.object(updater.Config, "from_env", return_value=f.config), patch.object(
            updater.sys, "argv", [str(UPDATER_FILE), updater.BASELINE_IMPORT_OPTION, *map(str, inputs[:3])]
        ), patch.object(updater.os, "geteuid", return_value=12345):
            assert updater.main() == 1
        assert not (f.config.releases_dir / "0.3.8").exists()
        if os.geteuid() == 0:
            with patch.object(updater.Config, "from_env", return_value=f.config), patch.object(
                updater.sys, "argv", [str(UPDATER_FILE), updater.BASELINE_IMPORT_OPTION, *map(str, inputs[:3])]
            ):
                assert updater.main() == 0
                assert updater.main() == 0
            assert (f.config.releases_dir / "0.3.8").is_dir()
            assert f.config.current_link.resolve() == active_before
    print("systemd offline trusted baseline import contracts: passed")


def initialization_identity_contracts(updater):
    from cryptography.hazmat.primitives import serialization

    with fixture(updater, maintenance=True) as f:
        path = f.config.status_dir / "initialization.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert set(payload) == {
            "schema", "protocol", "public_key_sha256", "bundle_sha256",
            "entrypoint_sha256", "initialized_at",
        }
        assert payload["schema"] == 1 and payload["protocol"] == 1
        assert type(payload["initialized_at"]) in {int, float} and payload["initialized_at"] > 0
        canonical = bytearray()
        entrypoint_sha256 = ""
        assert updater.SCRIPT_ROOT != ROOT
        for relative in updater.UPDATER_BUNDLE_FILES:
            raw = ROOT.joinpath(*Path(relative).parts).read_bytes()
            installed = updater.SCRIPT_ROOT / relative
            assert installed.read_bytes() == raw
            assert installed.stat().st_uid == os.geteuid()
            assert installed.stat().st_mode & 0o022 == 0
            digest = hashlib.sha256(raw).hexdigest()
            canonical.extend(relative.encode("utf-8"))
            canonical.extend(b"\0")
            canonical.extend(str(len(raw)).encode("ascii"))
            canonical.extend(b"\0")
            canonical.extend(digest.encode("ascii"))
            canonical.extend(b"\n")
            if relative == "deploy/updater/updater.py":
                entrypoint_sha256 = digest
        key_raw = f.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        assert payload["public_key_sha256"] == hashlib.sha256(key_raw).hexdigest()
        assert payload["bundle_sha256"] == hashlib.sha256(canonical).hexdigest()
        assert payload["entrypoint_sha256"] == entrypoint_sha256
        assert path.stat().st_mode & 0o777 == 0o644
        assert not any(name in payload for name in (
            "heartbeat_at", "expires_at", "bundle_root", "entrypoint", "command"
        ))
        f.write_intent()
        intent, _ = updater.claim_intent(f.config)
        assert intent.expected_current_version == "0.1.0"
        assert f.journal()["current_version"] == "0.1.0"

    with fixture(updater, maintenance=False, initialize=False) as f:
        expect_error(
            "update_maintenance_protocol_unsupported",
            lambda: updater.initialize_layout(f.config),
        )
        assert not (f.config.status_dir / "initialization.json").exists()

    with fixture(updater, maintenance=True, initialize=False) as f:
        wrong = f.root / "wrong-updater.py"
        wrong.write_text("raise RuntimeError('not the installed updater')\n", encoding="utf-8")
        wrong.chmod(0o644)
        with patch.dict(os.environ, {"SAAS_UPDATER_ENTRYPOINT": str(wrong)}):
            expect_error(
                "update_updater_identity_invalid",
                lambda: updater.initialize_layout(f.config),
            )
        assert not (f.config.status_dir / "initialization.json").exists()

    with fixture(updater, maintenance=True, initialize=False) as f:
        directory = updater.SCRIPT_ROOT / "backend"
        directory.chmod(0o777)
        try:
            expect_error("update_directory_untrusted", lambda: updater.initialize_layout(f.config))
        finally:
            directory.chmod(0o755)
        assert not (f.config.status_dir / "initialization.json").exists()
    print("systemd static initialization identity contracts: passed")


def signature_channels_and_permissions(updater):
    with fixture(updater) as f:
        payload = updater._intent_payload(f.intent)
        payload.pop("operation_id")
        payload.pop("expected_current_version")
        for channel in ("release", "stable", "beta"):
            parsed = updater._parse_intent({**payload, "channel": channel})
            assert parsed.channel == channel and parsed.operation_id == parsed.nonce
        assert updater._parse_intent({**payload, "operation_id": "b" * 32}).operation_id == "b" * 32
        expect_error("update_intent_invalid", lambda: updater._parse_intent({**payload, "command": "anything"}))
        for changes in ({"requested_at": float("nan")}, {"requested_at": float("inf")}, {"requested_by": True},
                        {"manifest_sha256": "z" * 64}, {"channel": "arbitrary"}, {"operation_id": "../escape"}):
            expect_error("update_intent_invalid", lambda changes=changes: updater._parse_intent({**payload, **changes}))
        f.write_intent({**payload, "requested_at": time.time() - 901})
        expect_error("update_intent_expired", lambda: updater.claim_intent(f.config))
        f.write_intent({**payload, "requested_at": time.time() + 120})
        expect_error("update_intent_expired", lambda: updater.claim_intent(f.config))
        f.write_intent()
        f.config.intent_file.chmod(0o644)
        expect_error("update_intent_invalid", lambda: updater.claim_intent(f.config))
        f.config.intent_file.chmod(0o600)
        hardlink = f.root / "linked-intent"
        os.link(f.config.intent_file, hardlink)
        expect_error("update_intent_invalid", lambda: updater.claim_intent(f.config))
        hardlink.unlink()
        # Replace the inode during the actual openat call, even at identical size.
        actual_open = updater.os.open
        changed = False

        def race(path, flags, *args, **kwargs):
            nonlocal changed
            if path == f.config.intent_file.name and not changed:
                changed = True
                replacement = f.config.intent_file.with_name("replacement")
                replacement.write_bytes(f.config.intent_file.read_bytes())
                replacement.chmod(0o600)
                os.replace(replacement, f.config.intent_file)
            return actual_open(path, flags, *args, **kwargs)

        with patch.object(updater.os, "open", side_effect=race):
            expect_error("update_file_changed", lambda: updater.claim_intent(f.config))
        # The app input can disappear after acceptance; the private journal wins.
        f.write_intent()
        intent, _ = updater.claim_intent(f.config)
        f.config.intent_file.unlink()
        with patch.object(updater.time, "time", return_value=time.time() + 86400):
            resumed, _ = updater.claim_intent(f.config)
            assert resumed == intent
        signature = f.candidate / updater.CACHED_SIGNATURE_FILE
        original = signature.read_bytes()
        signature.write_bytes(base64.b64encode(b"x" * 64))
        expect_error("update_signature_invalid", lambda: updater.materialize_release(f.config, f.intent))
        signature.write_bytes(original)
        original_umask = os.umask(0o077)
        try:
            result = f.run()
        finally:
            os.umask(original_umask)
        assert result["ok"] and result["status"] == "succeeded"
        target = f.config.current_link.resolve()
        for name in updater.INTERNAL_CANDIDATE_FILES:
            assert (target / name).stat().st_mode & 0o777 == 0o644
        for path in target.rglob("*"):
            assert path.stat().st_mode & 0o022 == 0
            if path.is_dir():
                assert path.stat().st_mode & 0o777 == 0o755
        assert updater._journal_path(f.config, f.intent.operation_id).stat().st_mode & 0o777 == 0o600
        expected_public = {"schema", "operation_id", "action", "version", "current_version", "status", "phase", "updated_at", "error_code"}
        assert set(f.public()) == expected_public
        assert str(f.root) not in json.dumps(f.public())
        assert json.loads((f.config.status_dir / "maintenance.json").read_text())["active"] is False
        with sqlite3.connect(f.config.database_path) as connection:
            row = connection.execute("SELECT release_notes, release_id FROM platform_updates WHERE version='0.2.0' AND channel='release'").fetchone()
            assert row == ("Release notes must survive all executor stages", "retained-release-id")
        previous_commands = list(f.runner.commands)
        assert updater.process_intent(f.config, f.intent, runner=f.runner)["status"] == "succeeded"
        assert f.runner.commands == previous_commands
        expect_error("update_nonce_conflict", lambda: updater.process_intent(f.config, replace(f.intent, operation_id="b" * 32), runner=f.runner))
        expect_error("update_nonce_conflict", lambda: updater.process_intent(f.config, replace(f.intent, version="0.3.0"), runner=f.runner))
        assert f.data() == "synthetic-data-must-survive"


def rejection_and_recovery_contracts(updater):
    with fixture(updater, dependency="9.0.0") as f:
        f.write_intent()
        expect_error("update_dependency_change_rejected", f.run)
        assert not f.runner.commands and f.config.current_link.resolve() == f.old
    with fixture(updater, migration="self.con.execute('CREATE TABLE destructive_fixture (id INTEGER)'); self.con.commit()") as f:
        f.write_intent()
        expect_error("update_migration_requires_manual", f.run)
        assert f.journal()["phase"] == "failed" and f.journal()["recovery_health_ok"]
        with sqlite3.connect(f.config.database_path) as connection:
            assert not connection.execute("SELECT name FROM sqlite_master WHERE name='destructive_fixture'").fetchall()
    with fixture(updater) as f:
        f.write_intent({**updater._intent_payload(f.intent), "expected_current_version": "0.0.9"})
        expect_error("update_current_version_changed", f.run)
        assert not f.runner.commands
    with fixture(updater) as f:
        f.health.empty_asset = True
        expect_error("update_health_failed", lambda: f.real_health(f.config, "0.1.0", updater.HealthClient(), attempts=1, interval=0))
    with fixture(updater) as f:
        f.write_intent()
        f.health.fail_versions = {"0.2.0", "0.1.0"}
        expect_error("update_health_failed", f.run)
        assert f.journal()["phase"] == "recovery_failed"
        assert (f.config.private_state_dir / "blocked.json").exists()
        assert not (f.config.private_state_dir / "active.json").exists()
        assert json.loads((f.config.status_dir / "maintenance.json").read_text())["active"]
        assert (f.config.releases_dir / "0.2.0").exists()
        assert Path(f.journal()["backup_path"]).exists()
        # A changed live sentinel must never be overwritten by the cold backup.
        with sqlite3.connect(f.config.database_path) as connection:
            connection.execute("UPDATE contract_data SET value='post-backup-live-data'")
        f.health.fail_versions = set()
        assert f.run()["status"] == "rolled_back"
        assert f.data() == "post-backup-live-data" and f.config.current_link.resolve() == f.old
        assert not json.loads((f.config.status_dir / "maintenance.json").read_text())["active"]
    with fixture(updater) as f:
        f.write_intent()
        f.runner.fail_stop = True
        try:
            f.run()
        except RuntimeError:
            pass
        else:
            raise AssertionError("partial stop must enter recovery")
        assert f.journal()["phase"] == "failed" and f.journal()["recovery_health_ok"]
        assert f.config.current_link.resolve() == f.old
    with fixture(updater) as f:
        # CAS loss before the stop command must prevent all forward service work.
        real_transition = updater._transition

        def steal(config, journal, database, phase, **kwargs):
            real_transition(config, journal, database, phase, **kwargs)
            if phase == "preflighting" and not journal.get("maintenance_active"):
                with sqlite3.connect(config.database_path) as connection:
                    connection.execute("UPDATE platform_updates SET updated_at=updated_at+100, status='apply_requested'")

        f.write_intent()
        with patch.object(updater, "_transition", side_effect=steal):
            expect_error("update_status_conflict", f.run)
        assert all(command[1] != "stop" for command in f.runner.commands), f.runner.commands
        assert f.data() == "synthetic-data-must-survive"


def runtime_backup_contracts(updater):
    with fixture(updater) as f:
        credential = f.config.tenants_dir / "1/accounts/default/credentials.json"
        write_json(credential, {"encrypted_token": "synthetic-encrypted-credential"})
        external = f.root / "external-tenants"
        write_json(external / "2/config.json", {"shop": "synthetic-external-shop"})
        f.config = replace(f.config, tenants_dir=external)
        f.runner.config = f.config
        f.write_intent()
        assert f.run()["status"] == "succeeded"
        archive = Path(f.journal()["runtime_backup_path"])
        assert archive.stat().st_mode & 0o777 == 0o600
        with tarfile.open(archive) as contents:
            names = contents.getnames()
            assert "state/tenants/1/accounts/default/credentials.json" in names
            assert "tenants/2/config.json" in names
            assert not any("update-staging" in name or name.endswith(("saas.db", "-wal", "-shm")) for name in names)
            assert b"synthetic-encrypted-credential" in contents.extractfile("state/tenants/1/accounts/default/credentials.json").read()
        assert f.journal()["runtime_backup_sha256"] == updater._backup_digest(archive)
    for kind in ("symlink", "hardlink", "fifo"):
        with fixture(updater) as f:
            tenants = f.config.tenants_dir
            tenants.mkdir()
            source = f.root / "outside-secret"
            source.write_text("must not enter a runtime backup")
            if kind == "symlink":
                (tenants / "unsafe").symlink_to(source)
            elif kind == "hardlink":
                os.link(source, tenants / "unsafe")
            else:
                os.mkfifo(tenants / "unsafe", 0o600)
            f.write_intent()
            expect_error("update_runtime_backup_invalid", f.run)
            assert f.config.current_link.resolve() == f.old and f.journal()["recovery_health_ok"]
            assert f.data() == "synthetic-data-must-survive"


def crash_child(config_file: str, point: str):
    updater = load_updater(Path(os.environ["SAAS_UPDATER_BUNDLE_ROOT"]))
    payload = json.loads(Path(config_file).read_text())
    fields = dict(payload["config"])
    path_fields = {"releases_dir", "current_link", "staging_dir", "state_dir", "database_path", "intent_file",
                   "lock_file", "backup_dir", "private_state_dir", "status_dir", "tenants_dir"}
    config = updater.Config(**{key: Path(value) if key in path_fields else value for key, value in fields.items()})
    intent = updater.Intent(**payload["intent"])
    runner = Runner(config)
    originals = {name: getattr(updater, name) for name in ("_save_journal", "materialize_release", "backup_database", "backup_runtime_state", "run_migrations", "switch_current", "_transition", "check_health")}

    def die():
        os.kill(os.getpid(), signal.SIGKILL)

    def save(c, journal):
        originals["_save_journal"](c, journal)
        if point == "accepted" and journal["phase"] == "queued":
            die()

    def materialize(*args, **kwargs):
        result = originals["materialize_release"](*args, **kwargs)
        if point == "materialized":
            die()
        return result

    def backup(*args, **kwargs):
        result = originals["backup_database"](*args, **kwargs)
        if point == "backed_up":
            die()
        return result

    def runtime_backup(*args, **kwargs):
        result = originals["backup_runtime_state"](*args, **kwargs)
        if point == "runtime_backed_up":
            die()
        return result

    def migrate(release, c, r):
        originals["run_migrations"](release, c, r)
        if point == "migration_committed" and c.database_path == config.database_path:
            die()

    def switch(c, target):
        originals["switch_current"](c, target)
        if point == "switched" and target.name == "0.2.0":
            die()
        if point == "rollback_switched" and target.name == "0.1.0":
            die()

    def transition(c, journal, database, phase, **kwargs):
        originals["_transition"](c, journal, database, phase, **kwargs)
        if point == phase and phase in {"preparing", "stopping", "verifying", "succeeded", "rolling_back", "recovery_failed"}:
            die()

    def health(c, version, client):
        originals["check_health"](c, version, client, attempts=1, interval=0)
        if point == "health_passed" and version == "0.2.0":
            die()

    patches = {"_save_journal": save, "materialize_release": materialize, "backup_database": backup,
               "backup_runtime_state": runtime_backup, "run_migrations": migrate, "switch_current": switch, "_transition": transition, "check_health": health}
    for name, function in patches.items():
        setattr(updater, name, function)
    descriptor = updater.acquire_lock(config)
    try:
        accepted, _ = updater.claim_intent(config)
        assert accepted == intent
        updater.process_intent(config, intent, runner=runner)
    finally:
        os.close(descriptor)
    raise AssertionError(f"crash checkpoint was not reached: {point}")


def sigkill_contracts(updater):
    points = ("accepted", "materialized", "preparing", "stopping", "backed_up", "runtime_backed_up", "migration_committed", "switched",
              "verifying", "health_passed", "succeeded", "rolling_back", "rollback_switched", "recovery_failed")
    for point in points:
        with fixture(updater) as f:
            f.write_intent()
            if point in {"rolling_back", "rollback_switched", "recovery_failed"}:
                f.health.fail_versions.add("0.2.0")
            if point == "recovery_failed":
                f.health.fail_versions.add("0.1.0")
            child_config = f.root / "child.json"
            write_json(child_config, {"config": {key: str(value) if isinstance(value, Path) else value for key, value in asdict(f.config).items()},
                                      "intent": asdict(f.intent)})
            child = subprocess.run([sys.executable, str(THIS_FILE), "--crash-child", str(child_config), point],
                                   env={**os.environ, **f.environment}, capture_output=True, text=True, timeout=40)
            assert child.returncode == -signal.SIGKILL, (point, child.returncode, child.stdout, child.stderr)
            # Remove all input/processing state; only the root journal survives.
            f.config.intent_file.unlink(missing_ok=True)
            updater._processing_file(f.config).unlink(missing_ok=True)
            f.health.fail_versions.clear()
            try:
                result = f.run()
            except updater.UpdaterError as exc:
                assert point == "migration_committed" and exc.code == "update_migration_interrupted"
                result = {"status": f.journal()["phase"]}
            expected = "failed" if point == "migration_committed" else "rolled_back" if point in {"rolling_back", "rollback_switched", "recovery_failed"} else "succeeded"
            assert result["status"] == expected, (point, result, f.journal())
            assert not json.loads((f.config.status_dir / "maintenance.json").read_text())["active"], point
            assert f.data() == "synthetic-data-must-survive", point
            calls = list(f.runner.commands)
            updater.process_intent(f.config, f.intent, runner=f.runner)
            assert calls == f.runner.commands, point
    print(f"systemd SIGKILL recovery: {len(points)} real Linux process checkpoints passed")


def admission_contracts(updater):
    cases = (
        (False, False, False),
        (True, False, True),
        (True, b"MAINTENANCE_PROTOCOL = 2\n", True),
        (True, b"MAINTENANCE_PROTOCOL = 1\nclass MAINTENANCE_PROTOCOL:\n    pass\n", True),
        (b"MAINTENANCE_PROTOCOL = 2\n", True, False),
    )
    for maintenance, target_maintenance, initialize in cases:
        with fixture(updater, maintenance=maintenance, target_maintenance=target_maintenance,
                     initialize=initialize) as f:
            f.write_intent()
            intent, _ = updater.claim_intent(f.config)
            expect_error(
                "update_maintenance_protocol_unsupported",
                lambda: updater._admit_maintenance_protocol(f.config, intent, f.journal()),
            )
            assert not f.runner.commands
    with fixture(updater, maintenance=True) as f:
        f.write_intent()
        intent, _ = updater.claim_intent(f.config)
        updater._admit_maintenance_protocol(f.config, intent, f.journal())
        assert f.journal()["maintenance_protocol_checked"] is True
        updater._admit_maintenance_protocol(f.config, intent, f.journal())
        assert not f.runner.commands
    if os.geteuid() == 0:
        for maintenance in (False, True):
            with fixture(updater, maintenance=maintenance, initialize=maintenance) as f:
                f.write_intent()
                with patch.object(updater.Config, "from_env", return_value=f.config), patch.object(
                    updater, "SystemRunner", return_value=f.runner
                ), patch.object(updater.sys, "argv", [str(UPDATER_FILE)]):
                    assert updater.main() == 0
                assert f.public()["status"] == ("succeeded" if maintenance else "failed")
                if not maintenance:
                    assert f.public()["error_code"] == "update_maintenance_protocol_unsupported"
                    assert not f.runner.commands
                assert not f.config.intent_file.exists()


def early_admission_status_contracts(updater):
    if os.geteuid() != 0:
        print("SKIP early executor-to-API status reconciliation: root-owned reader requires isolated Linux root")
        return
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from db import DB
    import platform_update
    from update_api import UpdateAPI

    def operation(f):
        return {
            "operation_id": f.intent.operation_id,
            "action": f.intent.action,
            "version": f.intent.version,
            "deployment": "systemd",
        }

    def queued_api(f):
        database = DB(str(f.config.database_path))
        uid = database.create_user(
            f"expired-contract-{time.time_ns()}", "Synthetic-Expired-Contract-Password!", role="admin"
        )
        session = database.create_token(uid)
        database.create_update_operation(
            operation_id=f.intent.operation_id,
            action=f.intent.action,
            version=f.intent.version,
            channel=f.intent.channel,
            deployment="systemd",
            manifest_sha256=f.intent.manifest_sha256,
            candidate_path=f.intent.candidate_path,
            release_id="synthetic-expired",
            release_notes="expired intent reconciliation",
            expected_current_version=f.intent.expected_current_version,
            requested_by=uid,
            session_digest=hashlib.sha256(session.encode()).hexdigest(),
        )
        confirmation = database.create_admin_confirmation(
            uid, "update.apply", session_token=session, version=f.intent.version,
            manifest_sha256=f.intent.manifest_sha256, operation_id=f.intent.operation_id,
        )
        assert database.queue_update_operation(
            f.intent.operation_id, confirmation, uid, session,
            action=f.intent.action, version=f.intent.version,
        )["status"] == "queued"
        database.mark_update_operation_published(f.intent.operation_id)
        return database, UpdateAPI(
            database, f.intent.expected_current_version, backend=platform_update,
            is_paused=lambda: False,
        )

    with fixture(updater, maintenance=True) as f:
        f.intent = replace(f.intent, requested_at=time.time() - 901)
        database, service = queued_api(f)
        try:
            f.write_intent()
            with patch.object(updater.Config, "from_env", return_value=f.config), patch.object(
                updater, "SystemRunner", return_value=f.runner
            ), patch.object(updater.sys, "argv", [str(UPDATER_FILE)]):
                assert updater.main() == 0
            public = f.public()
            assert public["status"] == public["phase"] == "failed"
            assert public["current_version"] == "0.1.0"
            assert public["error_code"] == "update_intent_expired"
            observed = platform_update.read_operation_status(operation(f))
            assert observed == {**public, "updated_at": float(public["updated_at"])}
            service.reconcile(f.intent.operation_id)
            terminal = service.latest_operation()
            with patch.object(platform_update, "write_update_intent") as republish:
                service.recover_pending()
                republish.assert_not_called()
            assert terminal["status"] == terminal["phase"] == "failed"
            assert terminal["current_version"] == "0.1.0"
            assert terminal["error_code"] == "update_intent_expired"
            assert database.active_update_operation() is None
            assert not updater._journal_path(f.config, f.intent.operation_id).exists()
            assert json.loads((f.config.private_state_dir / "last-rejection.json").read_text())["error_code"] == "update_intent_expired"
            assert not f.runner.commands and not f.config.intent_file.exists()
        finally:
            service.stop()
            database.con.close()

    with fixture(updater, maintenance=True) as f:
        f.intent = replace(f.intent, requested_at=time.time() - 901, expected_current_version="")
        legacy = updater._intent_payload(f.intent)
        legacy.pop("expected_current_version")
        f.write_intent(legacy)
        with patch.object(updater.Config, "from_env", return_value=f.config), patch.object(
            updater, "SystemRunner", return_value=f.runner
        ), patch.object(updater.sys, "argv", [str(UPDATER_FILE)]):
            assert updater.main() == 0
        public = f.public()
        assert public["current_version"] == "0.1.0"
        assert platform_update.read_operation_status(operation(f))["status"] == "failed"

    with fixture(updater, maintenance=True) as f:
        f.intent = replace(f.intent, requested_at=time.time() - 901, expected_current_version="")
        wrong = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        keyfile = Path(os.environ["SAAS_UPDATE_PUBLIC_KEY_FILE"])
        keyfile.write_bytes(base64.b64encode(wrong))
        keyfile.chmod(0o644)
        legacy = updater._intent_payload(f.intent)
        legacy.pop("expected_current_version")
        f.write_intent(legacy)
        with patch.object(updater.Config, "from_env", return_value=f.config), patch.object(
            updater, "SystemRunner", return_value=f.runner
        ), patch.object(updater.sys, "argv", [str(UPDATER_FILE)]):
            assert updater.main() == 0
        assert not (f.config.status_dir / "operations" / f"{f.intent.operation_id}.json").exists()
        assert not f.config.intent_file.exists()

    with fixture(updater, maintenance=True) as f:
        signature = f.candidate / updater.CACHED_SIGNATURE_FILE
        signature.write_bytes(base64.b64encode(b"x" * 64))
        signature.chmod(0o600)
        f.write_intent()
        with patch.object(updater.Config, "from_env", return_value=f.config), patch.object(
            updater, "SystemRunner", return_value=f.runner
        ), patch.object(updater.sys, "argv", [str(UPDATER_FILE)]):
            assert updater.main() == 0
        public = f.public()
        assert public["status"] == public["phase"] == "failed"
        assert public["current_version"] == "0.1.0"
        assert public["error_code"] == "update_signature_invalid"
        observed = platform_update.read_operation_status(operation(f))
        assert observed == {**public, "updated_at": float(public["updated_at"])}
        assert not f.runner.commands and not f.config.intent_file.exists()

    with fixture(updater, maintenance=True) as f:
        wrong = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        keyfile = Path(os.environ["SAAS_UPDATE_PUBLIC_KEY_FILE"])
        keyfile.write_bytes(base64.b64encode(wrong))
        keyfile.chmod(0o644)
        f.write_intent()
        with patch.object(updater.Config, "from_env", return_value=f.config), patch.object(
            updater, "SystemRunner", return_value=f.runner
        ), patch.object(updater.sys, "argv", [str(UPDATER_FILE)]):
            assert updater.main() == 0
        public = f.public()
        assert public["status"] == public["phase"] == "failed"
        assert public["current_version"] == "0.1.0"
        assert public["error_code"] == "update_signature_invalid"
        observed = platform_update.read_operation_status(operation(f))
        assert observed == {**public, "updated_at": float(public["updated_at"])}
        assert not f.runner.commands and not f.config.intent_file.exists()
    print("systemd early admission executor-to-API status contracts: passed")


def root_reader_contract(updater):
    if os.geteuid() != 0:
        print("SKIP root ownership/non-root reader contract: run this test as root on isolated Linux")
        return
    import pwd
    user = pwd.getpwnam("nobody")
    with fixture(updater, maintenance=True) as f:
        f.config = replace(f.config, intent_owner_uid=user.pw_uid)
        f.runner.config = f.config
        f.intent = replace(f.intent, expected_current_version="0.1.0")
        # Give only synthetic business/staging state to the application UID.
        for path in [f.config.state_dir, *f.config.state_dir.rglob("*")]:
            os.chown(path, user.pw_uid, user.pw_gid)
        updater.initialize_layout(f.config)
        publish_code = """import json, sys
sys.path.insert(0, sys.argv[1])
from platform_update import write_update_intent
request = json.loads(sys.argv[2])
result = write_update_intent('apply', request['version'], channel='release', requested_by=1,
    candidate_path=request['candidate_path'], manifest_sha256=request['manifest_sha256'],
    operation_id=request['operation_id'], expected_current_version='0.1.0', requested_at=request['requested_at'])
assert result['queued'] is True and result['status'] == 'queued'
"""
        published = subprocess.run([sys.executable, "-c", publish_code, str(ROOT / "backend"), json.dumps(asdict(f.intent))],
                                   user=user.pw_uid, group=user.pw_gid, extra_groups=[], env={**os.environ, **f.environment},
                                   capture_output=True, text=True, timeout=30)
        assert published.returncode == 0, published.stderr
        assert f.config.intent_file.stat().st_uid == user.pw_uid
        assert f.config.intent_file.stat().st_mode & 0o777 == 0o600
        assert f.run()["status"] == "succeeded"
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(f.config.database_path) + suffix)
            if sidecar.exists():
                assert sidecar.stat().st_uid == user.pw_uid
        # A real unprivileged process can read but cannot rewrite metadata,
        # rename the root status directory, or read the private root journal.
        ipc = f.config.intent_file.parent
        os.chown(ipc, 0, user.pw_gid)
        ipc.chmod(0o1770)
        target = f.config.current_link.resolve()
        code = """import json, os, pathlib, sys
root, status, private = map(pathlib.Path, sys.argv[1:4])
for name in json.loads(sys.argv[4]):
    path = root / name
    assert path.read_bytes()
    try: path.write_bytes(b'bad')
    except PermissionError: pass
    else: raise AssertionError('app rewrote signed metadata')
assert json.loads((status / 'operation.json').read_text())['status'] == 'succeeded'
try: os.rename(status, status.with_name('forged-status'))
except PermissionError: pass
else: raise AssertionError('app replaced root status directory')
try: list(private.iterdir())
except PermissionError: pass
else: raise AssertionError('app read private journal')
"""
        result = subprocess.run([sys.executable, "-c", code, str(target), str(f.config.status_dir), str(f.config.private_state_dir),
                                 json.dumps(list(updater.INTERNAL_CANDIDATE_FILES))], user=user.pw_uid, group=user.pw_gid,
                                extra_groups=[], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        for path in [f.config.status_dir, f.config.status_dir / "operation.json", *[target / name for name in updater.INTERNAL_CANDIDATE_FILES]]:
            assert path.stat().st_uid == 0
    print("systemd root-owned metadata/non-root reader contract: passed")


def main():
    static_contracts()
    if sys.platform != "linux":
        print("SKIP Linux execution/signature/permissions/SIGKILL contracts: native Linux required; no fcntl shim used")
        return
    updater = load_updater()
    baseline_import_contracts(updater)
    initialization_identity_contracts(updater)
    signature_channels_and_permissions(updater)
    rejection_and_recovery_contracts(updater)
    runtime_backup_contracts(updater)
    sigkill_contracts(updater)
    admission_contracts(updater)
    early_admission_status_contracts(updater)
    root_reader_contract(updater)
    print("systemd recovery Linux contracts: passed (no real systemd services were operated)")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--crash-child":
        crash_child(sys.argv[2], sys.argv[3])
    else:
        main()
