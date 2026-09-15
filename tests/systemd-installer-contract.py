#!/usr/bin/env python3
"""Offline contracts for systemd install, adoption and v0.4.0 migration."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
from contextlib import closing
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

from deploy.manager.cli import parse_args
from deploy.manager.constants import (
    API_SERVICE,
    CONSUMER_SERVICE,
    MANAGER_CURRENT_LINK,
    MANAGER_INSTALL_PATH,
    MANAGER_RELEASES_DIR,
    UPDATER_PATH_UNIT,
    UPDATER_SERVICE,
)
from deploy.manager.errors import ManagerError
from deploy.manager.installer import (
    APPLICATION_UNITS,
    ASSET_ARCHITECTURES,
    GITHUB_API_ROOT,
    GITHUB_REPOSITORY,
    HEALTH_URL,
    PUBLIC_URL,
    READY_URL,
    VERSION_URL,
    LEGACY_COMMIT,
    LEGACY_VERSION,
    MANAGER_PROTOCOL,
    START_UNITS,
    SYSTEMD_ANALYZE_BIN,
    SYSTEMD_BIN,
    USERADD_BIN,
    AccountIdentity,
    CommandAdapter,
    Filesystem,
    InstallPaths,
    Installer,
    NetworkAdapter,
    discover_legacy_layout,
)
from deploy.manager.runtime import MANAGER_VERSION, map_architecture


APP_VERSION = "9.8.7"
MANAGER = b"synthetic current manager executable\n"
APP_MANAGER = b"synthetic target release manager executable\n"
LEGACY_MANAGER = b"synthetic legacy release manager executable\n"


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def expect_error(code: str, operation) -> None:
    try:
        operation()
    except ManagerError as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError(f"expected {code}")


class DryFilesystem(Filesystem):
    def __init__(self):
        self.ownership = []
        self.links: dict[Path, str] = {}

    def chown(self, path: Path, uid: int, gid: int) -> None:
        self.ownership.append((path, uid, gid))

    def chown_tree(self, path: Path, uid: int, gid: int) -> None:
        raise AssertionError("installer must not recursively chown business state")

    def is_executable(self, path: Path) -> bool:
        return path.is_file()

    def is_secure_executable(self, path: Path) -> bool:
        return path.is_file()

    def validate_root_file(self, path: Path, maximum: int) -> None:
        assert path not in self.links and path.is_file() and path.stat().st_size <= maximum

    def validate_private_root_file(self, path: Path, maximum: int) -> None:
        self.validate_root_file(path, maximum)

    def validate_root_directory(self, path: Path) -> None:
        assert path not in self.links and path.is_dir()

    def validate_root_ancestor(self, path: Path) -> None:
        assert path.is_absolute()

    def validate_owned_directory(self, path: Path, uid: int, gid: int, mode: int) -> None:
        assert path.is_dir() and uid >= 0 and gid >= 0 and mode in {0o700, 0o755, 0o1770}

    def validate_persistent_path(self, path: Path, *, directory: bool, allowed_uids: set[int]) -> None:
        assert allowed_uids
        if path.exists():
            assert path.is_dir() if directory else path.is_file()

    def free_bytes(self, path: Path) -> int:
        return 16 * 1024 * 1024 * 1024

    def atomic_symlink(self, link: Path, target: str) -> None:
        if self.exists(link):
            raise ManagerError("manager_install_path_occupied")
        self.replace_symlink(link, target)

    def replace_symlink(self, link: Path, target: str) -> None:
        if self.exists(link) and link not in self.links:
            raise ManagerError("manager_install_path_unsafe")
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists():
            link.unlink()
        link.write_text(target, encoding="utf-8")
        self.links[link] = target

    def readlink(self, path: Path) -> str:
        try:
            return self.links[path]
        except KeyError as exc:
            raise ManagerError("manager_install_identity_invalid") from exc

    def remove(self, path: Path) -> None:
        self.links.pop(path, None)
        super().remove(path)


class DryCommands(CommandAdapter):
    def __init__(self, *, shows=None, port=True, os_release=("ubuntu", "24.04"), identity=None,
                 fail=None, show_returncodes=None):
        self.commands = []
        self.shows = shows or {}
        self.port = port
        self.release = os_release
        self.identity = identity
        self.availability_checks = []
        self.fail = fail
        self.show_returncodes = show_returncodes or {}

    def available(self, executable: str) -> bool:
        self.availability_checks.append(executable)
        return executable in {SYSTEMD_BIN, SYSTEMD_ANALYZE_BIN, USERADD_BIN}

    def account(self, name: str):
        assert name == "xianyu-saas"
        return self.identity

    def port_available(self, host: str, port: int) -> bool:
        assert (host, port) == ("0.0.0.0", 8096)
        return self.port

    def os_release(self):
        return self.release

    def run(self, command: tuple[str, ...], *, check: bool = True):
        command = tuple(command)
        assert CommandAdapter._allowed(command), command
        self.commands.append(command)
        if self.fail is not None and self.fail(command):
            raise ManagerError("manager_command_failed")
        if command[:2] in {(SYSTEMD_BIN, "start"), (SYSTEMD_BIN, "stop")}:
            active = "active" if command[1] == "start" else "inactive"
            for unit in command[2:]:
                if unit in self.shows:
                    self.shows[unit] = re.sub(
                        r"(?m)^ActiveState=.*$", f"ActiveState={active}", self.shows[unit]
                    )
        if command[:2] in {(SYSTEMD_BIN, "enable"), (SYSTEMD_BIN, "disable")}:
            state = "enabled" if command[1] == "enable" else "disabled"
            for unit in command[2:]:
                if unit in self.shows:
                    self.shows[unit] = re.sub(
                        r"(?m)^UnitFileState=.*$", f"UnitFileState={state}", self.shows[unit]
                    )
        if command[:2] == (SYSTEMD_BIN, "show"):
            unit = command[-1]
            output = self.shows.get(
                unit,
                f"Id={unit}\nLoadState=not-found\nActiveState=inactive\nUnitFileState=disabled\n",
            )
            return subprocess.CompletedProcess(
                command, self.show_returncodes.get(unit, 0), stdout=output, stderr=""
            )
        if command[0] == USERADD_BIN:
            self.identity = managed_identity()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class DryNetwork:
    def __init__(self, responses: dict[str, bytes], *, healthy=True):
        self.responses = responses
        self.healthy = healthy
        self.requests = []
        self.health_versions = []

    def fetch(self, url: str, maximum: int) -> bytes:
        self.requests.append(url)
        assert url in self.responses, url
        payload = self.responses[url]
        assert len(payload) <= maximum
        return payload

    def health(self, expected_version: str) -> bool:
        self.health_versions.append(expected_version)
        return self.healthy


def managed_identity() -> AccountIdentity:
    return AccountIdentity(4100, 4100, "/var/lib/xianyu-saas", "/usr/sbin/nologin", "xianyu-saas")


def make_archive(
    version: str,
    architecture: str,
    *,
    manager_payload: bytes,
    update_data_version: int = 1,
    path_override: str | None = None,
):
    runtime = {
        "schema": 1,
        "version": version,
        "commit": "1" * 40,
        "platform": "linux",
        "architecture": architecture,
        "target": f"linux-{architecture}",
        "python_version": "3.12.14",
        "python_build": "20260901-install_only_stripped",
        "uv_version": "0.12.13",
        "manager_protocol": MANAGER_PROTOCOL,
        "update_data_version": update_data_version,
        "backend_lock_sha256": "2" * 64,
        "worker_lock_sha256": "3" * 64,
    }
    files = {
        "package.json": json_bytes({"name": "xianyu-saas", "version": version}),
        "backend/version.py": (
            f'VERSION = "{version}"\nUPDATE_DATA_VERSION = {update_data_version}\n'
        ).encode(),
        "backend/update_maintenance.py": b"MAINTENANCE_PROTOCOL = 1\n",
        "worker/main.py": b"VALUE = 'worker'\n",
        "docker/launcher.sh": b"#!/bin/sh\nexit 0\n",
        "frontend/index.html": b"<!doctype html><title>fixture</title>\n",
        "runtime/python/bin/python3": b"#!/bin/sh\nexit 0\n",
        "runtime/python/bin/python3.12": b"#!/bin/sh\nexit 0\n",
        "runtime/runtime.json": json_bytes(runtime),
        "manager/xianyu-saas": manager_payload,
    }
    manifest_files = []
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        directories = {
            "/".join(name.split("/")[:index])
            for name in files
            for index in range(1, len(name.split("/")))
        }
        for directory in sorted(directories):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, payload in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            executable = name in {"manager/xianyu-saas", "docker/launcher.sh"}
            info.mode = 0o755 if executable else 0o644
            archive.addfile(info, io.BytesIO(payload))
            manifest_files.append({
                "path": path_override if name == "backend/version.py" and path_override else name,
                "size": len(payload),
                "sha256": digest(payload),
                "executable": executable,
            })
    return archive_buffer.getvalue(), manifest_files, runtime


def asset_record(name: str, payload: bytes, kind: str, architecture: str) -> dict:
    return {
        "name": name,
        "size": len(payload),
        "sha256": digest(payload),
        "kind": kind,
        "target": f"linux-{architecture}",
        "manager_protocol": MANAGER_PROTOCOL,
    }


def build_release(
    key: Ed25519PrivateKey,
    version: str,
    manager_payload: bytes,
    *,
    path_override: str | None = None,
    mutate_records=None,
):
    payloads = {}
    records = []
    bundles = {}
    for architecture in ("x86_64", "aarch64"):
        base = f"xianyu-saas-{version}-linux-{architecture}"
        archive, files, runtime = make_archive(
            version,
            architecture,
            manager_payload=manager_payload,
            path_override=path_override if architecture == "x86_64" else None,
        )
        archive_record = asset_record(base + ".tar.gz", archive, "standalone-archive", architecture)
        manifest = json_bytes({
            "schema": 2,
            "kind": "standalone",
            "version": version,
            "platform": "linux",
            "architecture": architecture,
            "target": f"linux-{architecture}",
            "manager_protocol": MANAGER_PROTOCOL,
            "update_data_version": 1,
            "runtime": runtime,
            "artifact": archive_record["name"],
            "artifact_size": archive_record["size"],
            "artifact_sha256": archive_record["sha256"],
            "files": files,
        })
        signature = base64.b64encode(key.sign(manifest))
        manager_record = asset_record(base, manager_payload, "bootstrap-manager", architecture)
        manifest_record = asset_record(base + ".manifest.json", manifest, "standalone-manifest", architecture)
        signature_record = asset_record(base + ".manifest.sig", signature, "standalone-signature", architecture)
        records.extend((manager_record, archive_record, manifest_record, signature_record))
        payloads.update({
            archive_record["name"]: archive,
            manifest_record["name"]: manifest,
            signature_record["name"]: signature,
            manager_record["name"]: manager_payload,
        })
        bundles[architecture] = {
            "archive": archive,
            "archive_record": archive_record,
            "manifest": manifest,
            "signature": signature,
        }
    if mutate_records is not None:
        mutate_records(records)
    index = json_bytes({"schema": 2, "version": version, "commit": "0" * 40, "files": records})
    index_signature = base64.b64encode(key.sign(index))
    names = ["artifacts.json", "artifacts.json.sig", *payloads]
    prefix = f"https://github.com/{GITHUB_REPOSITORY}/releases/download/v{version}/"
    metadata = json_bytes({
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [{"name": name, "browser_download_url": prefix + name} for name in names],
    })
    responses = {
        f"{GITHUB_API_ROOT}/releases/tags/v{version}": metadata,
        prefix + "artifacts.json": index,
        prefix + "artifacts.json.sig": index_signature,
    }
    responses.update({prefix + name: payload for name, payload in payloads.items()})
    return metadata, responses, bundles


def release_fixture(*, app_version=APP_VERSION, path_override=None, mutate_app_records=None, extra_version=None):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    public_encoded = base64.b64encode(public) + b"\n"
    _, manager_responses, _ = build_release(key, MANAGER_VERSION, MANAGER)
    app_manager = LEGACY_MANAGER if app_version == LEGACY_VERSION else APP_MANAGER
    app_metadata, app_responses, bundles = build_release(
        key,
        app_version,
        app_manager,
        path_override=path_override,
        mutate_records=mutate_app_records,
    )
    responses = {**manager_responses, **app_responses}
    if extra_version:
        _, extra_responses, _ = build_release(key, extra_version, APP_MANAGER)
        responses.update(extra_responses)
    responses[f"{GITHUB_API_ROOT}/releases/latest"] = app_metadata
    return public_encoded, responses, bundles


def install_paths(root: Path) -> InstallPaths:
    return InstallPaths(
        install_root=root / "opt/xianyu-saas",
        releases_dir=root / "opt/xianyu-saas/releases",
        current_link=root / "opt/xianyu-saas/current",
        manager_releases_dir=root / "opt/xianyu-saas/manager/releases",
        manager_current_link=root / "opt/xianyu-saas/manager/current",
        launcher_link=root / "usr/local/bin/xianyu-saas",
        state_dir=root / "var/lib/xianyu-saas",
        update_queue_dir=root / "var/lib/xianyu-saas-updates",
        updater_state_dir=root / "var/lib/xianyu-saas-updater",
        env_file=root / "etc/xianyu-saas.env",
        public_key_file=root / "etc/xianyu-saas/update-signing.pub",
        systemd_dir=root / "etc/systemd/system",
        logrotate_dir=root / "etc/logrotate.d",
        legacy_srv_root=root / "srv/xianyu-saas",
        legacy_state_root=root / "srv/xianyu-saas-data",
    )


def new_installer(
    root: Path,
    *,
    app_version=APP_VERSION,
    responses=None,
    public=None,
    bundles=None,
    commands=None,
    healthy=True,
    initializer=None,
    path_override=None,
    mutate_app_records=None,
):
    manager = root / "bootstrap-manager"
    manager.parent.mkdir(parents=True, exist_ok=True)
    manager.write_bytes(MANAGER)
    if public is None or responses is None or bundles is None:
        public, responses, bundles = release_fixture(
            app_version=app_version,
            path_override=path_override,
            mutate_app_records=mutate_app_records,
        )
    filesystem = DryFilesystem()
    command_adapter = commands or DryCommands()
    network = DryNetwork(responses, healthy=healthy)
    initializations = []
    callback = initializer or (lambda environment: initializations.append(dict(environment)))
    installer = Installer(
        filesystem=filesystem,
        commands=command_adapter,
        network=network,
        paths=install_paths(root),
        templates_root=ROOT,
        executable=manager,
        public_key=public,
        updater_initializer=callback,
        update_capability_probe=lambda account, root: True,
        clock=lambda: 1789257600,
    )
    if os.name == "posix" and os.geteuid() != 0:
        installer._acquire_update_lock = lambda: os.open(os.devnull, os.O_RDONLY)
    return installer, filesystem, command_adapter, network, bundles, initializations


def service_shows(root: str, environment: str, *, active=True, custom=False, updater=False):
    state = "active" if active else "inactive"
    result = {}
    commands = {
        API_SERVICE: f"{root}/runtime/backend-venv/bin/python -m uvicorn app:app",
        CONSUMER_SERVICE: f"{root}/runtime/backend-venv/bin/python -m job_consumer",
    }
    for unit in APPLICATION_UNITS:
        executable = f"/bin/bash -c {root}/start" if custom else commands[unit]
        result[unit] = (
            f"Id={unit}\nLoadState=loaded\nFragmentPath=/etc/systemd/system/{unit}\n"
            f"ExecStart={executable}\nWorkingDirectory={root}/current/backend\n"
            f"EnvironmentFiles={environment}\nActiveState={state}\nUnitFileState=enabled\n"
        )
    if updater:
        result[UPDATER_SERVICE] = (
            f"Id={UPDATER_SERVICE}\nLoadState=loaded\nFragmentPath=/etc/systemd/system/{UPDATER_SERVICE}\n"
            "ExecStart=/opt/xianyu-saas/manager/current/xianyu-saas internal consume-intent\n"
            "WorkingDirectory=\nEnvironmentFiles=/etc/xianyu-saas.env\n"
            "ActiveState=inactive\nUnitFileState=static\n"
        )
        result[UPDATER_PATH_UNIT] = (
            f"Id={UPDATER_PATH_UNIT}\nLoadState=loaded\nFragmentPath=/etc/systemd/system/{UPDATER_PATH_UNIT}\n"
            "ExecStart=\nWorkingDirectory=\nEnvironmentFiles=\n"
            "ActiveState=active\nUnitFileState=enabled\n"
        )
    return result


def cached_release(installer: Installer, filesystem: DryFilesystem, bundle: dict, version: str) -> Path:
    release = installer.paths.releases_dir / version
    release.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(bundle["archive"]), mode="r:gz") as archive:
        archive.extractall(release)
    record = bundle["archive_record"]
    (release / ".xianyu-release.json").write_bytes(json_bytes({
        "schema": 1,
        "version": version,
        "channel": "release",
        "manifest_sha256": digest(bundle["manifest"]),
        "release_id": "bootstrap-install",
        "artifact": record["name"],
        "artifact_size": record["size"],
        "kind": "standalone",
        "target": "linux-x86_64",
    }))
    (release / ".xianyu-manifest.json").write_bytes(bundle["manifest"])
    (release / ".xianyu-manifest.sig").write_bytes(bundle["signature"])
    filesystem.replace_symlink(installer.paths.current_link, f"releases/{version}")
    return release


def install_application_units(installer: Installer) -> None:
    installer.paths.systemd_dir.mkdir(parents=True, exist_ok=True)
    for unit in APPLICATION_UNITS:
        (installer.paths.systemd_dir / unit).write_bytes((ROOT / "deploy/systemd" / unit).read_bytes())


def write_environment(path: Path, *, db="/var/lib/xianyu-saas/saas.db", tenants="/var/lib/xianyu-saas/tenants") -> bytes:
    raw = (
        "SAAS_ENV=production\n"
        "SAAS_TESTING=0\n"
        "SAAS_AI_MASTER_KEY=operator-owned-value\n"
        f"SAAS_DB={db}\n"
        f"SAAS_TENANTS_DIR={tenants}\n"
        "CUSTOM_SETTING=preserve-me\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def write_database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database:
        database.execute("CREATE TABLE contract_data (value TEXT NOT NULL)")
        database.execute("INSERT INTO contract_data(value) VALUES (?)", (value,))
        database.commit()


def read_database(path: Path) -> str:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        return database.execute("SELECT value FROM contract_data").fetchone()[0]


def health_retry_contract() -> None:
    attempts = 0
    sleeps = []
    payloads = {
        HEALTH_URL: json_bytes({"ok": True, "service": "xianyu-saas-api"}),
        READY_URL: json_bytes({"ok": True, "database": "ready"}),
        VERSION_URL: json_bytes({"version": APP_VERSION, "asset_version": "contract-asset"}),
        PUBLIC_URL: b"<html>contract-asset</html>",
    }

    class Response:
        def __init__(self, url: str):
            self.url = url
            self.status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

        def read(self, _maximum):
            return payloads[self.url]

    def open_health(request, timeout=0):
        nonlocal attempts
        assert timeout == 5
        if request.full_url == HEALTH_URL:
            attempts += 1
            if attempts < 3:
                raise urllib.error.URLError("synthetic startup delay")
        return Response(request.full_url)

    adapter = NetworkAdapter(health_opener=open_health, sleeper=sleeps.append)
    assert adapter.health(APP_VERSION) is True
    assert attempts == 3 and sleeps == [1.0, 1.0]
    print("installer: API startup health retries before accepting the candidate")


def successful_transaction_and_repeat(run: Path) -> None:
    commands = DryCommands()
    observed = []
    installer, filesystem, commands, network, _, _ = new_installer(
        run / "success",
        commands=commands,
        initializer=lambda environment: observed.append((dict(environment), tuple(commands.commands))),
    )

    def guarded_health(version: str) -> bool:
        network.health_versions.append(version)
        maintenance = json.loads((installer.paths.update_queue_dir / "status/maintenance.json").read_bytes())
        assert maintenance["active"] is True and maintenance["phase"] == "installing"
        assert (SYSTEMD_BIN, "start", API_SERVICE) in commands.commands
        assert (SYSTEMD_BIN, "start", CONSUMER_SERVICE, UPDATER_PATH_UNIT) not in commands.commands
        return True

    network.health = guarded_health
    result = installer.install(architecture="x86_64")
    assert result == {
        "ok": True,
        "status": "installed",
        "version": APP_VERSION,
        "manager_version": MANAGER_VERSION,
        "architecture": "x86_64",
    }
    paths = installer.paths
    assert filesystem.links[paths.current_link] == f"releases/{APP_VERSION}"
    assert filesystem.links[paths.manager_current_link] == f"releases/{MANAGER_VERSION}"
    assert filesystem.links[paths.launcher_link] == str(paths.manager_current_link / "xianyu-saas")
    assert (paths.manager_releases_dir / MANAGER_VERSION / "xianyu-saas").read_bytes() == MANAGER
    assert (paths.releases_dir / APP_VERSION / "manager/xianyu-saas").read_bytes() == APP_MANAGER
    if os.name == "posix":
        assert stat.S_IMODE((paths.releases_dir / APP_VERSION / "runtime/python/bin/python3").stat().st_mode) == 0o755
    initialization = json.loads(paths.initialization_file.read_bytes())
    assert initialization["schema"] == 2
    assert initialization["manager_version"] == MANAGER_VERSION
    assert initialization["manager_sha256"] == digest(MANAGER)
    assert initialization["architecture"] == "x86_64"
    assert observed and not any(command[:2] == (SYSTEMD_BIN, "start") for command in observed[0][1])
    assert Path(observed[0][0]["SAAS_DB"]) == paths.state_dir / "saas.db"
    for unit in (API_SERVICE, CONSUMER_SERVICE, UPDATER_SERVICE, UPDATER_PATH_UNIT):
        assert (paths.systemd_dir / unit).read_bytes() == (ROOT / "deploy/systemd" / unit).read_bytes()
    assert commands.availability_checks == [SYSTEMD_BIN, SYSTEMD_ANALYZE_BIN, USERADD_BIN]
    assert (SYSTEMD_BIN, "daemon-reload") in commands.commands
    # The shipped units run the built-in launcher: only the API unit is enabled
    # and started; the legacy consumer/updater units are disabled and untouched.
    assert (SYSTEMD_BIN, "enable", API_SERVICE) in commands.commands
    assert (SYSTEMD_BIN, "disable", CONSUMER_SERVICE, UPDATER_PATH_UNIT) in commands.commands
    assert (SYSTEMD_BIN, "start", API_SERVICE) in commands.commands
    assert (SYSTEMD_BIN, "start", CONSUMER_SERVICE, UPDATER_PATH_UNIT) not in commands.commands
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)
    maintenance = json.loads((paths.update_queue_dir / "status/maintenance.json").read_bytes())
    assert maintenance["active"] is False and maintenance["phase"] == "succeeded"
    assert network.requests[0] == f"{GITHUB_API_ROOT}/releases/tags/v{MANAGER_VERSION}"
    assert f"{GITHUB_API_ROOT}/releases/latest" in network.requests
    assert network.health_versions == [APP_VERSION]
    write_database(paths.state_dir / "saas.db", "fresh installation")
    (paths.state_dir / "tenants").mkdir(exist_ok=True)

    requests_before = list(network.requests)
    commands_before = list(commands.commands)
    repeated = installer.install(architecture="x86_64")
    assert repeated["status"] == "already_installed"
    assert repeated["version"] == APP_VERSION and repeated["manager_version"] == MANAGER_VERSION
    assert network.requests == requests_before
    assert commands.commands == commands_before + [(SYSTEMD_BIN, "start", API_SERVICE)]
    print("installer: manager/app version split, fresh transaction and idempotency passed")


def managed_runtime_upgrade_and_readiness(run: Path) -> None:
    target = "9.8.8"
    public, responses, bundles = release_fixture(extra_version=target)
    installer, filesystem, commands, network, _, _ = new_installer(
        run / "managed-upgrade", public=public, responses=responses, bundles=bundles)
    installer.install(architecture="x86_64", version=APP_VERSION)
    write_database(installer.paths.state_dir / "saas.db", "keep through runtime upgrade")
    (installer.paths.state_dir / "tenants").mkdir(exist_ok=True)
    before = (installer.paths.state_dir / "saas.db").read_bytes()
    link = installer._file_update_store_link()
    web_code = link.parent / "releases/9.8.8-preview.1"
    (web_code / "backend").mkdir(parents=True)
    (web_code / "backend/app.py").write_text("# signed-code fixture\n")
    link.symlink_to("releases/9.8.8-preview.1")
    assert installer._file_update_pointer_state() == str(web_code.resolve())
    result = installer.install(architecture="x86_64", version=target)
    assert result["version"] == target
    assert not link.is_symlink(), "new runtime was shadowed by old web code"
    assert (installer.paths.state_dir / "saas.db").read_bytes() == before
    installer.update_capability_probe = lambda *_: False
    expect_error("manager_update_ready_failed", lambda: installer.install(architecture="x86_64"))
    print("installer: signed runtime upgrade preserves data, resets older web code and requires readiness on rerun")


def existing_environment_is_never_overwritten(run: Path) -> None:
    installer, _, _, _, _, _ = new_installer(run / "existing-env")
    original = write_environment(installer.paths.env_file)
    installer.install(architecture="x86_64")
    assert installer.paths.env_file.read_bytes() == original
    print("installer: existing root-owned environment is preserved byte-for-byte")


def architecture_selection(run: Path) -> None:
    installer, _, _, network, _, _ = new_installer(run / "arm")
    result = installer.install(architecture="aarch64", version=APP_VERSION)
    assert result["architecture"] == "aarch64"
    requested = "\n".join(network.requests)
    assert "linux-aarch64.tar.gz" in requested
    assert "linux-x86_64.tar.gz" not in requested
    assert network.requests[0].endswith(f"/releases/tags/v{MANAGER_VERSION}")
    assert f"{GITHUB_API_ROOT}/releases/tags/v{APP_VERSION}" in network.requests
    print("installer: manager and requested application architecture bindings passed")


def signed_unmanaged_adoption(run: Path) -> None:
    commands = DryCommands(
        shows=service_shows("/opt/xianyu-saas", "/etc/xianyu-saas.env"),
        identity=managed_identity(),
    )
    installer, filesystem, commands, network, bundles, initializations = new_installer(
        run / "adoption", app_version=LEGACY_VERSION, commands=commands
    )
    release = cached_release(installer, filesystem, bundles["x86_64"], LEGACY_VERSION)
    original_current = filesystem.links[installer.paths.current_link]
    original_environment = write_environment(installer.paths.env_file)
    installer.paths.state_dir.mkdir(parents=True)
    write_database(installer.paths.state_dir / "saas.db", "business database")
    (installer.paths.state_dir / "tenants").mkdir()
    install_application_units(installer)
    api_before = (installer.paths.systemd_dir / API_SERVICE).read_bytes()

    result = installer.install(architecture="x86_64")
    assert result["status"] == "adopted"
    assert result["version"] == LEGACY_VERSION and result["manager_version"] == MANAGER_VERSION
    assert filesystem.links[installer.paths.current_link] == original_current
    assert (installer.paths.systemd_dir / API_SERVICE).read_bytes() == api_before
    for unit in (UPDATER_SERVICE, UPDATER_PATH_UNIT):
        assert (installer.paths.systemd_dir / unit).read_bytes() == (ROOT / "deploy/systemd" / unit).read_bytes()
    assert installer.paths.env_file.read_bytes() == original_environment
    assert (release / "backend/version.py").is_file()
    if os.name == "posix":
        assert stat.S_IMODE((release / "runtime/python/bin/python3").stat().st_mode) == 0o755
    assert len(initializations) == 1
    assert all(f"/releases/tags/v{LEGACY_VERSION}" not in request for request in network.requests)
    requests_before = list(network.requests)
    commands_before = list(commands.commands)
    assert installer.install(architecture="x86_64")["status"] == "already_installed"
    assert network.requests == requests_before
    assert commands.commands == commands_before + [(SYSTEMD_BIN, "start", API_SERVICE)]
    print("installer: signed unmanaged v0.4.0 adoption and repeat passed")


def configure_legacy(
    root: Path,
    production_root: str,
    *,
    initializer=None,
    healthy=True,
    db=None,
    tenants=None,
    updater=False,
    fail=None,
):
    if db is None:
        db = "/var/lib/xianyu-saas/saas.db" if production_root == "/opt/xianyu-saas" else "/srv/xianyu-saas-data/saas.db"
    if tenants is None:
        tenants = "/var/lib/xianyu-saas/tenants" if production_root == "/opt/xianyu-saas" else "/srv/xianyu-saas-data/tenants"
    environment_name = "/etc/xianyu-saas.env" if production_root == "/opt/xianyu-saas" else "/srv/xianyu-saas/.env"
    commands = DryCommands(
        shows=service_shows(production_root, environment_name, updater=updater),
        identity=managed_identity(),
        fail=fail,
    )
    installer, filesystem, commands, network, bundles, initializations = new_installer(
        root,
        app_version=LEGACY_VERSION,
        commands=commands,
        healthy=healthy,
        initializer=initializer,
    )
    legacy_root = installer.paths.install_root if production_root == "/opt/xianyu-saas" else installer.paths.legacy_srv_root
    source = legacy_root / "releases/legacy-build"
    (source / "backend").mkdir(parents=True)
    (source / "package.json").write_bytes(json_bytes({"name": "xianyu-saas", "version": LEGACY_VERSION}))
    (source / "backend/version.py").write_text(
        f'VERSION = "{LEGACY_VERSION}"\nUPDATE_DATA_VERSION = 1\n', encoding="utf-8"
    )
    (source / "backend/build-info.json").write_bytes(json_bytes({
        "version": LEGACY_VERSION,
        "commit": LEGACY_COMMIT,
        "dirty": False,
        "build_time": "2026-09-13T12:00:00+00:00",
    }))
    filesystem.replace_symlink(legacy_root / "current", "releases/legacy-build")
    env_path = installer.paths.env_file if production_root == "/opt/xianyu-saas" else legacy_root / ".env"
    environment_raw = write_environment(env_path, db=db, tenants=tenants)
    installer.paths.systemd_dir.mkdir(parents=True, exist_ok=True)
    old_units = {}
    installed_units = APPLICATION_UNITS + ((UPDATER_SERVICE, UPDATER_PATH_UNIT) if updater else ())
    for unit in installed_units:
        old_units[unit] = f"legacy unit {production_root} {unit}\n".encode()
        (installer.paths.systemd_dir / unit).write_bytes(old_units[unit])
    database_path = installer._local_path(Path(db))
    tenants_path = installer._local_path(Path(tenants))
    database_path.parent.mkdir(parents=True, exist_ok=True)
    write_database(database_path, "legacy business database")
    tenants_path.mkdir(parents=True, exist_ok=True)
    sentinel = database_path.parent / "business.sentinel"
    sentinel.write_bytes(b"preserve me")
    installer._contract_database_path = database_path
    installer._contract_tenants_path = tenants_path
    installer._contract_sentinel = sentinel
    return (
        installer,
        filesystem,
        commands,
        network,
        bundles,
        initializations,
        legacy_root,
        source,
        env_path,
        environment_raw,
        old_units,
    )


def legacy_migrations(run: Path) -> None:
    cases = (
        ("opt", "/opt/xianyu-saas", False),
        ("srv", "/srv/xianyu-saas", False),
        ("srv-with-updater", "/srv/xianyu-saas", True),
    )
    for label, production_root, updater in cases:
        fixture = configure_legacy(run / f"legacy-{label}", production_root, updater=updater)
        installer, filesystem, commands, network, _, initializations, legacy_root, source, env_path, env_raw, _ = fixture
        layout = discover_legacy_layout(commands)
        assert layout.kind == "supported" and layout.root == production_root
        result = installer.install(architecture="x86_64")
        assert result == {
            "ok": True,
            "status": "migrated",
            "version": LEGACY_VERSION,
            "manager_version": MANAGER_VERSION,
            "architecture": "x86_64",
        }
        assert source.is_dir(), "old source release must be retained"
        assert env_path.read_bytes() == env_raw
        assert installer.paths.env_file.read_bytes() == env_raw
        assert read_database(installer._contract_database_path) == "legacy business database"
        assert installer._contract_sentinel.read_bytes() == b"preserve me"
        assert filesystem.links[installer.paths.current_link] == f"releases/{LEGACY_VERSION}"
        if os.name == "posix":
            assert stat.S_IMODE((
                installer.paths.releases_dir / LEGACY_VERSION / "runtime/python/bin/python3"
            ).stat().st_mode) == 0o755
        if production_root == "/srv/xianyu-saas":
            assert filesystem.links[legacy_root / "current"] == str(installer.paths.current_link)
            for unit in (API_SERVICE, CONSUMER_SERVICE, UPDATER_SERVICE):
                installed = (installer.paths.systemd_dir / unit).read_text(encoding="utf-8")
                assert "ReadWritePaths=/srv/xianyu-saas-data" in installed
            logrotate = (installer.paths.logrotate_dir / "xianyu-saas").read_text(encoding="utf-8")
            assert "/srv/xianyu-saas-data/tenants" in logrotate
        assert (SYSTEMD_BIN, "stop", *reversed(APPLICATION_UNITS)) in commands.commands
        assert network.requests[0].endswith(f"/releases/tags/v{MANAGER_VERSION}")
        assert f"{GITHUB_API_ROOT}/releases/tags/v{LEGACY_VERSION}" in network.requests
        assert network.health_versions == [LEGACY_VERSION]
        assert len(initializations) == 1
        if production_root == "/opt/xianyu-saas":
            assert not any(path == installer.paths.state_dir for path, _, _ in filesystem.ownership)
        requests_before = list(network.requests)
        commands_before = list(commands.commands)
        assert installer.install(architecture="x86_64")["status"] == "already_installed"
        assert network.requests == requests_before
        assert commands.commands == commands_before + [(SYSTEMD_BIN, "start", API_SERVICE)]
    print("installer: /opt and /srv v0.4.0 migrations preserve data and become idempotent")


def assert_legacy_restored(fixture) -> None:
    installer, filesystem, commands, _, _, _, legacy_root, source, env_path, env_raw, old_units = fixture
    assert filesystem.links[legacy_root / "current"] == "releases/legacy-build"
    assert installer.paths.current_link not in filesystem.links
    assert installer.paths.manager_current_link not in filesystem.links
    assert installer.paths.launcher_link not in filesystem.links
    assert source.is_dir()
    assert env_path.read_bytes() == env_raw
    assert read_database(installer._contract_database_path) == "legacy business database"
    assert installer._contract_sentinel.read_bytes() == b"preserve me"
    for unit in (API_SERVICE, CONSUMER_SERVICE, UPDATER_SERVICE, UPDATER_PATH_UNIT):
        path = installer.paths.systemd_dir / unit
        if unit in old_units:
            assert path.read_bytes() == old_units[unit]
        else:
            assert not path.exists()
    assert not (installer.paths.releases_dir / LEGACY_VERSION).exists()
    assert not (installer.paths.manager_releases_dir / MANAGER_VERSION).exists()
    assert not installer.paths.initialization_file.exists()
    assert (SYSTEMD_BIN, "start", *APPLICATION_UNITS) in commands.commands


def failure_recovery(run: Path) -> None:
    def fail_initialize(_environment):
        raise ManagerError("manager_updater_failed")

    fixture = configure_legacy(run / "rollback-initialize", "/srv/xianyu-saas", initializer=fail_initialize)
    installer = fixture[0]
    expect_error("manager_updater_failed", lambda: installer.install(architecture="x86_64"))
    assert_legacy_restored(fixture)
    diagnostic = json.loads(installer.paths.diagnostic_file.read_bytes())
    assert diagnostic["error_code"] == "manager_updater_failed"

    fixture = configure_legacy(run / "rollback-health", "/srv/xianyu-saas", healthy=False)
    installer, _, commands, network = fixture[:4]

    def mutate_then_fail(version: str) -> bool:
        network.health_versions.append(version)
        for unit in APPLICATION_UNITS:
            commands.shows[unit] = re.sub(
                r"(?m)^ActiveState=.*$", "ActiveState=failed", commands.shows[unit]
            )
        with closing(sqlite3.connect(installer._contract_database_path)) as database:
            database.execute("UPDATE contract_data SET value='candidate-mutated-data'")
            database.commit()
        return False

    network.health = mutate_then_fail
    expect_error("manager_install_health_failed", lambda: installer.install(architecture="x86_64"))
    assert_legacy_restored(fixture)
    assert network.health_versions == [LEGACY_VERSION]
    backups = list((installer.paths.updater_state_dir / "backups").glob("saas-install-*-before-*.db"))
    assert len(backups) == 1 and read_database(backups[0]) == "legacy business database"

    fixture = configure_legacy(
        run / "rollback-command-failure",
        "/opt/xianyu-saas",
        fail=lambda command: command[:2] == (SYSTEMD_BIN, "start"),
    )
    installer = fixture[0]
    expect_error("manager_install_recovery_failed", lambda: installer.install(architecture="x86_64"))
    print("installer: failures restore database, links and units; restore-command errors stay fatal")


def maintenance_clear_uncertainty_contract(run: Path) -> None:
    fixture = configure_legacy(run / "maintenance-clear-uncertain", "/opt/xianyu-saas")
    installer, filesystem, _, network = fixture[:4]

    def commit_candidate_data(version: str) -> bool:
        network.health_versions.append(version)
        with closing(sqlite3.connect(installer._contract_database_path)) as database:
            database.execute("UPDATE contract_data SET value='candidate-committed-data'")
            database.commit()
        return True

    network.health = commit_candidate_data
    real_fsync = filesystem.fsync_directory
    failed = False

    def fail_after_visible_clear(path: Path) -> None:
        nonlocal failed
        maintenance = installer.paths.update_queue_dir / "status/maintenance.json"
        if path == maintenance.parent and maintenance.is_file() and not failed:
            payload = json.loads(maintenance.read_bytes())
            if payload.get("active") is False:
                failed = True
                raise ManagerError("manager_install_file_failed")
        real_fsync(path)

    filesystem.fsync_directory = fail_after_visible_clear
    expect_error("manager_install_recovery_failed", lambda: installer.install(architecture="x86_64"))
    assert failed
    assert read_database(installer._contract_database_path) == "candidate-committed-data"
    assert filesystem.links[installer.paths.current_link] == f"releases/{LEGACY_VERSION}"
    assert installer.paths.install_journal_file.is_file()
    maintenance = json.loads((installer.paths.update_queue_dir / "status/maintenance.json").read_bytes())
    assert maintenance["active"] is False

    filesystem.fsync_directory = real_fsync
    assert installer.install(architecture="x86_64")["status"] == "already_installed"
    assert not installer.paths.install_journal_file.exists()
    assert read_database(installer._contract_database_path) == "candidate-committed-data"
    print("installer: visible maintenance clear never rolls back committed candidate data")


def interrupted_install_journal_recovery(run: Path) -> None:
    phases = (
        "maintenance", "stopped", "backed_up", "published", "switched", "units",
        "initialized", "api_started", "healthy", "services_started", "completed",
    )
    for phase in phases:
        fixture = configure_legacy(run / f"interrupted-{phase}", "/opt/xianyu-saas")
        installer = fixture[0]
        real_update = installer._update_install_journal
        crashed = False

        def crash_after_journal(journal, current_phase, **changes):
            nonlocal crashed
            real_update(journal, current_phase, **changes)
            if current_phase == phase and not crashed:
                crashed = True
                raise SystemExit("synthetic installer interruption")

        installer._update_install_journal = crash_after_journal
        try:
            installer.install(architecture="x86_64")
        except SystemExit:
            pass
        else:
            raise AssertionError(f"installer interruption phase not reached: {phase}")
        assert crashed and installer.paths.install_journal_file.is_file()
        installer._update_install_journal = real_update
        result = installer.install(architecture="x86_64")
        assert result["status"] == ("already_installed" if phase == "completed" else "migrated")
        assert not installer.paths.install_journal_file.exists()
        assert read_database(installer._contract_database_path) == "legacy business database"
    print(f"installer: {len(phases)} durable interruption phases recover and resume")


def tampering_and_danger_fail_before_stop(run: Path) -> None:
    public, responses, bundles = release_fixture()
    signature_url = next(
        url for url in responses
        if f"/releases/download/v{MANAGER_VERSION}/" in url and url.endswith("artifacts.json.sig")
    )
    damaged = dict(responses)
    damaged[signature_url] = base64.b64encode(b"x" * 64)
    cases = []
    cases.append(("manager-index", public, damaged, bundles, {}, "manager_artifacts_signature_invalid"))

    def wrong_arch(records):
        for record in records:
            if record["name"].endswith("linux-x86_64.tar.gz"):
                record["target"] = "linux-aarch64"

    public2, responses2, bundles2 = release_fixture(mutate_app_records=wrong_arch)
    cases.append(("application-architecture", public2, responses2, bundles2, {}, "manager_release_asset_mismatch"))
    public3, responses3, bundles3 = release_fixture(path_override="../escape")
    cases.append(("manifest-path", public3, responses3, bundles3, {}, "manager_archive_path_invalid"))

    for label, key, fixture_responses, fixture_bundles, options, code in cases:
        commands = DryCommands()
        installer, _, _, _, _, _ = new_installer(
            run / label,
            public=key,
            responses=fixture_responses,
            bundles=fixture_bundles,
            commands=commands,
            **options,
        )
        expect_error(code, lambda installer=installer: installer.install(architecture="x86_64"))
        assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands), label

    commands = DryCommands(
        shows=service_shows("/opt/xianyu-saas", "/etc/xianyu-saas.env"),
        identity=managed_identity(),
    )
    installer, filesystem, commands, _, bundles, _ = new_installer(
        run / "signed-tamper", app_version=LEGACY_VERSION, commands=commands
    )
    release = cached_release(installer, filesystem, bundles["x86_64"], LEGACY_VERSION)
    write_environment(installer.paths.env_file)
    install_application_units(installer)
    (release / "backend/version.py").write_bytes(b'VERSION = "0.4.0"\nUPDATE_DATA_VERSION = 2\n')
    expect_error("manager_signed_adoption_invalid", lambda: installer.install(architecture="x86_64"))
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    fixture = configure_legacy(
        run / "relative-data", "/opt/xianyu-saas", db="relative/saas.db"
    )
    installer, _, commands = fixture[:3]
    expect_error("manager_data_path_invalid", lambda: installer.install(architecture="x86_64"))
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    fixture = configure_legacy(run / "missing-data", "/opt/xianyu-saas")
    installer, _, commands = fixture[:3]
    installer._contract_database_path.unlink()
    expect_error("manager_data_path_invalid", lambda: installer.install(architecture="x86_64"))
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    fixture = configure_legacy(run / "missing-env-keys", "/opt/xianyu-saas")
    installer, _, commands, _, _, _, _, _, env_path = fixture[:9]
    env_path.write_text(
        "SAAS_ENV=production\nSAAS_TESTING=0\nSAAS_AI_MASTER_KEY=operator-owned-value\n",
        encoding="utf-8",
    )
    expect_error("manager_data_path_invalid", lambda: installer.install(architecture="x86_64"))
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    fixture = configure_legacy(run / "testing-environment", "/opt/xianyu-saas")
    installer, _, commands, _, _, _, _, _, env_path = fixture[:9]
    env_path.write_bytes(env_path.read_bytes().replace(b"SAAS_TESTING=0", b"SAAS_TESTING=1"))
    expect_error("manager_environment_invalid", lambda: installer.install(architecture="x86_64"))
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    fixture = configure_legacy(run / "legacy-build-tamper", "/opt/xianyu-saas")
    installer, _, commands, _, _, _, _, source = fixture[:8]
    (source / "backend/build-info.json").write_bytes(json_bytes({
        "version": LEGACY_VERSION,
        "commit": "0" * 40,
        "dirty": False,
        "build_time": "2026-09-13T12:00:00+00:00",
    }))
    expect_error("manager_legacy_identity_invalid", lambda: installer.install(architecture="x86_64"))
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    fixture = configure_legacy(run / "pending-update", "/opt/xianyu-saas")
    installer, _, commands = fixture[:3]
    installer.paths.update_queue_dir.mkdir(parents=True, exist_ok=True)
    (installer.paths.update_queue_dir / "intent.json").write_text("{}", encoding="utf-8")
    expect_error("manager_update_in_progress", lambda: installer.install(architecture="x86_64"))
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    fixture = configure_legacy(run / "active-updater", "/opt/xianyu-saas", updater=True)
    installer, _, commands = fixture[:3]
    commands.shows[UPDATER_SERVICE] = commands.shows[UPDATER_SERVICE].replace(
        "ActiveState=inactive", "ActiveState=active"
    )
    expect_error("manager_update_in_progress", lambda: installer.install(architecture="x86_64"))
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    commands = DryCommands(
        shows=service_shows("/opt/xianyu-saas", "/etc/xianyu-saas.env"),
        identity=managed_identity(),
        show_returncodes={API_SERVICE: 1},
    )
    installer, _, _, network, _, _ = new_installer(run / "systemctl-show-failure", commands=commands)
    expect_error("manager_unit_state_unsafe", lambda: installer._unit_state(API_SERVICE))
    expect_error("manager_legacy_layout_unsafe", lambda: installer.install(architecture="x86_64"))
    assert not network.requests

    commands = DryCommands(shows=service_shows("/custom/app", "/custom/app/.env", custom=True))
    installer, _, _, network, _, _ = new_installer(run / "custom-layout", commands=commands)
    expect_error("manager_legacy_layout_unsafe", lambda: installer.install(architecture="x86_64"))
    assert not network.requests
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    installer, filesystem, commands, network, _, _ = new_installer(run / "half-installed")
    filesystem.replace_symlink(installer.paths.manager_current_link, f"releases/{MANAGER_VERSION}")
    expect_error("manager_install_incomplete", lambda: installer.install(architecture="x86_64"))
    assert not network.requests
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    installer, filesystem, commands, _, _, _ = new_installer(run / "unsafe-owner")

    def reject_privileged_root(path: Path) -> None:
        if path == installer.paths.install_root:
            raise ManagerError("manager_install_path_unsafe")
        assert path.is_dir()

    filesystem.validate_root_directory = reject_privileged_root
    expect_error("manager_install_path_unsafe", lambda: installer.install(architecture="x86_64"))
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)
    print("installer: tampering, updater activity, unsafe data/owners and half-installed states fail before stop")


def updater_lock_contract(run: Path) -> None:
    if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("installer: updater flock contention contract skipped outside Linux root")
        return
    installer, _, _, _, _, _ = new_installer(run / "updater-lock")
    account = managed_identity()
    installer._create_directories(account.uid, account.gid)
    descriptor = installer._acquire_update_lock()
    try:
        expect_error("manager_update_in_progress", installer._acquire_update_lock)
    finally:
        os.close(descriptor)
    print("installer: updater flock contention is rejected under Linux root")


def preflight_boundaries(run: Path) -> None:
    commands = DryCommands(port=False)
    installer, _, _, network, _, _ = new_installer(run / "port", commands=commands)
    expect_error("manager_install_port_in_use", lambda: installer.install(architecture="x86_64"))
    assert not network.requests
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    commands = DryCommands()
    installer, _, _, network, _, _ = new_installer(run / "launcher", commands=commands)
    installer.paths.launcher_link.parent.mkdir(parents=True, exist_ok=True)
    installer.paths.launcher_link.write_bytes(b"operator-owned launcher\n")
    expect_error("manager_install_incomplete", lambda: installer.install(architecture="x86_64"))
    assert not network.requests
    print("installer: fresh-only port and occupied launcher preflight boundaries passed")


def static_boundaries() -> None:
    source = (ROOT / "deploy/manager/installer.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "subprocess.run" in source and "shell=False" in source
    assert GITHUB_API_ROOT == f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
    assert "artifacts.json.sig" in source and "Ed25519PublicKey" in source
    assert "nginx" not in source.lower()
    assert 'VERSION_URL = "http://127.0.0.1:8096/api/version/public"' in source
    assert not CommandAdapter._allowed(("/usr/sbin/nginx", "-t"))
    assert not CommandAdapter._allowed((SYSTEMD_BIN, "reload-or-restart", "nginx.service"))
    assert MANAGER_RELEASES_DIR == Path("/opt/xianyu-saas/manager/releases")
    assert MANAGER_CURRENT_LINK == Path("/opt/xianyu-saas/manager/current")
    assert MANAGER_INSTALL_PATH == Path("/usr/local/bin/xianyu-saas")
    assert ASSET_ARCHITECTURES == {"x86_64": "x86_64", "aarch64": "aarch64"}
    assert map_architecture("AMD64") == "x86_64"
    assert map_architecture("arm64") == "aarch64"
    assert parse_args(["install", "--version", APP_VERSION]).release_version == APP_VERSION
    print("installer: fixed command, network, signature and CLI boundaries passed")


def main() -> None:
    static_boundaries()
    health_retry_contract()
    with tempfile.TemporaryDirectory(prefix="xianyu-installer-contract-") as temporary:
        run = Path(temporary)
        successful_transaction_and_repeat(run)
        managed_runtime_upgrade_and_readiness(run)
        existing_environment_is_never_overwritten(run)
        architecture_selection(run)
        signed_unmanaged_adoption(run)
        legacy_migrations(run)
        failure_recovery(run)
        maintenance_clear_uncertainty_contract(run)
        interrupted_install_journal_recovery(run)
        tampering_and_danger_fail_before_stop(run)
        updater_lock_contract(run)
        preflight_boundaries(run)
    print("systemd installer contract: passed")


if __name__ == "__main__":
    main()
