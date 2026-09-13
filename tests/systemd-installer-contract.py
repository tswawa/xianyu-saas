#!/usr/bin/env python3
"""Offline contracts for the standalone manager first-install transaction."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

from deploy.manager.cli import parse_args
from deploy.manager.constants import (
    API_SERVICE, CONSUMER_SERVICE, MANAGER_CURRENT_LINK, MANAGER_INSTALL_PATH,
    MANAGER_RELEASES_DIR, UPDATER_PATH_UNIT, UPDATER_SERVICE,
)
from deploy.manager.errors import ManagerError
from deploy.manager.installer import (
    ASSET_ARCHITECTURES,
    GITHUB_API_ROOT,
    GITHUB_REPOSITORY,
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
    discover_legacy_layout,
)
from deploy.manager.runtime import map_architecture


VERSION = "9.8.7"
MANAGER = b"synthetic standalone manager executable\n"


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
        self.links = {}

    def chown_tree(self, path: Path, uid: int, gid: int) -> None:
        self.ownership.append((path, uid, gid))

    def validate_root_file(self, path: Path, maximum: int) -> None:
        assert path.is_file() and path.stat().st_size <= maximum

    def free_bytes(self, path: Path) -> int:
        return 16 * 1024 * 1024 * 1024

    def atomic_symlink(self, link: Path, target: str) -> None:
        if self.exists(link):
            raise ManagerError("manager_install_path_occupied")
        link.parent.mkdir(parents=True, exist_ok=True)
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
    def __init__(self, *, shows=None, port=True, os_release=("ubuntu", "24.04")):
        self.commands = []
        self.shows = shows or {}
        self.port = port
        self.release = os_release
        self.identity = None
        self.availability_checks = []

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
        if command[:2] == (SYSTEMD_BIN, "show"):
            unit = command[-1]
            output = self.shows.get(unit, f"Id={unit}\nLoadState=not-found\n")
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
        if command[0] == USERADD_BIN:
            self.identity = AccountIdentity(
                4100, 4100, "/var/lib/xianyu-saas", "/usr/sbin/nologin", "xianyu-saas"
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class DryNetwork:
    def __init__(self, responses: dict[str, bytes], *, healthy=True):
        self.responses = responses
        self.healthy = healthy
        self.requests = []

    def fetch(self, url: str, maximum: int) -> bytes:
        self.requests.append(url)
        assert url in self.responses, url
        payload = self.responses[url]
        assert len(payload) <= maximum
        return payload

    def health(self) -> bool:
        return self.healthy


def make_archive(architecture: str, *, path_override: str | None = None):
    runtime = {
        "schema": 1,
        "version": VERSION,
        "commit": "1" * 40,
        "platform": "linux",
        "architecture": architecture,
        "target": f"linux-{architecture}",
        "python_version": "3.12.14",
        "python_build": "20260901-install_only_stripped",
        "uv_version": "0.12.13",
        "manager_protocol": MANAGER_PROTOCOL,
        "update_data_version": 1,
        "backend_lock_sha256": "2" * 64,
        "worker_lock_sha256": "3" * 64,
    }
    files = {
        "package.json": json_bytes({"name": "xianyu-saas", "version": VERSION}),
        "backend/version.py": f'VERSION = "{VERSION}"\nUPDATE_DATA_VERSION = 1\n'.encode(),
        "worker/main.py": b"VALUE = 'worker'\n",
        "frontend/index.html": b"<!doctype html><title>fixture</title>\n",
        "runtime/runtime.json": json_bytes(runtime),
        "manager/xianyu-saas": MANAGER,
    }
    manifest_files = []
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        directories = {name.split("/", 1)[0] for name in files if "/" in name}
        for directory in sorted(directories):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, payload in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            executable = name == "manager/xianyu-saas"
            info.mode = 0o755 if executable else 0o644
            archive.addfile(info, io.BytesIO(payload))
            manifest_files.append({
                "path": path_override if name == "backend/version.py" and path_override else name,
                "size": len(payload), "sha256": digest(payload), "executable": executable,
            })
    return archive_buffer.getvalue(), manifest_files, runtime


def asset_record(name: str, payload: bytes, kind: str, architecture: str) -> dict:
    return {
        "name": name, "size": len(payload), "sha256": digest(payload), "kind": kind,
        "target": f"linux-{architecture}", "manager_protocol": MANAGER_PROTOCOL,
    }


def release_fixture(*, mutate_index=None, path_override=None):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    public_encoded = base64.b64encode(public) + b"\n"
    payloads = {}
    records = []
    for architecture in ("x86_64", "aarch64"):
        base = f"xianyu-saas-{VERSION}-linux-{architecture}"
        archive, files, runtime = make_archive(
            architecture, path_override=path_override if architecture == "x86_64" else None
        )
        archive_record = asset_record(base + ".tar.gz", archive, "standalone-archive", architecture)
        manifest = json_bytes({
            "schema": 2, "kind": "standalone", "version": VERSION, "platform": "linux",
            "architecture": architecture, "target": f"linux-{architecture}",
            "manager_protocol": MANAGER_PROTOCOL, "update_data_version": 1,
            "runtime": runtime,
            "artifact": archive_record["name"], "artifact_size": archive_record["size"],
            "artifact_sha256": archive_record["sha256"], "files": files,
        })
        signature = base64.b64encode(key.sign(manifest))
        manager_record = asset_record(base, MANAGER, "bootstrap-manager", architecture)
        manifest_record = asset_record(base + ".manifest.json", manifest, "standalone-manifest", architecture)
        signature_record = asset_record(base + ".manifest.sig", signature, "standalone-signature", architecture)
        records.extend((manager_record, archive_record, manifest_record, signature_record))
        payloads.update({
            archive_record["name"]: archive,
            manifest_record["name"]: manifest,
            signature_record["name"]: signature,
            manager_record["name"]: MANAGER,
        })
    if mutate_index:
        mutate_index(records)
    index = json_bytes({"schema": 2, "version": VERSION, "commit": "0" * 40, "files": records})
    index_signature = base64.b64encode(key.sign(index))
    names = ["artifacts.json", "artifacts.json.sig", *payloads]
    prefix = f"https://github.com/{GITHUB_REPOSITORY}/releases/download/v{VERSION}/"
    metadata = json_bytes({
        "tag_name": f"v{VERSION}", "draft": False, "prerelease": False,
        "assets": [{"name": name, "browser_download_url": prefix + name} for name in names],
    })
    responses = {f"{GITHUB_API_ROOT}/releases/latest": metadata, f"{GITHUB_API_ROOT}/releases/tags/v{VERSION}": metadata}
    responses[prefix + "artifacts.json"] = index
    responses[prefix + "artifacts.json.sig"] = index_signature
    for name, payload in payloads.items():
        responses[prefix + name] = payload
    return public_encoded, responses


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
    )


def new_installer(root: Path, *, responses=None, public=None, commands=None, healthy=True, initializer=None):
    manager = root / "bootstrap-manager"
    manager.parent.mkdir(parents=True, exist_ok=True)
    manager.write_bytes(MANAGER)
    public, default_responses = release_fixture() if public is None or responses is None else (public, responses)
    filesystem = DryFilesystem()
    command_adapter = commands or DryCommands()
    network = DryNetwork(default_responses if responses is None else responses, healthy=healthy)
    installer = Installer(
        filesystem=filesystem, commands=command_adapter, network=network,
        paths=install_paths(root), templates_root=ROOT, executable=manager,
        public_key=public, updater_initializer=initializer or (lambda: None), clock=lambda: 1789257600,
    )
    return installer, filesystem, command_adapter, network


def successful_transaction_and_repeat(run: Path) -> None:
    commands = DryCommands()
    initialization_commands = []
    installer, filesystem, commands, network = new_installer(
        run / "success",
        commands=commands,
        initializer=lambda: initialization_commands.append(tuple(commands.commands)),
    )
    result = installer.install(architecture="x86_64")
    assert result == {
        "ok": True, "status": "installed", "version": VERSION,
        "manager_version": VERSION, "architecture": "x86_64",
    }
    paths = installer.paths
    assert filesystem.links[paths.current_link] == f"releases/{VERSION}"
    assert filesystem.links[paths.manager_current_link] == f"releases/{VERSION}"
    assert filesystem.links[paths.launcher_link] == str(paths.manager_current_link / "xianyu-saas")
    release_root = paths.releases_dir / VERSION
    assert (release_root / "backend/version.py").is_file()
    for internal in (".xianyu-release.json", ".xianyu-manifest.json", ".xianyu-manifest.sig"):
        assert (release_root / internal).is_file()
    marker = json.loads((release_root / ".xianyu-release.json").read_bytes())
    assert marker["kind"] == "standalone" and marker["target"] == "linux-x86_64"
    assert (paths.manager_releases_dir / VERSION / "xianyu-saas").read_bytes() == MANAGER
    environment = paths.env_file.read_text(encoding="utf-8")
    key_line = next(line for line in environment.splitlines() if line.startswith("SAAS_AI_MASTER_KEY="))
    assert len(base64.b64decode(key_line.split("=", 1)[1], validate=True)) == 32
    assert "SAAS_PUBLIC_ORIGIN=" not in environment
    assert "SAAS_TRUSTED_HOSTS=" not in environment
    assert "SAAS_UPDATE_HEALTH_BASE_URL=http://127.0.0.1:8096/\n" in environment
    assert "SAAS_UPDATE_PUBLIC_BASE_URL=http://127.0.0.1:8096/xianyu-saas/\n" in environment
    initialization = json.loads(paths.initialization_file.read_bytes())
    assert initialization["schema"] == 2 and initialization["architecture"] == "x86_64"
    assert initialization["protocol"] == MANAGER_PROTOCOL
    assert initialization["manager_sha256"] == digest(MANAGER)
    assert len(initialization_commands) == 1
    assert not any(command[:2] == (SYSTEMD_BIN, "start") for command in initialization_commands[0])
    for unit in (API_SERVICE, CONSUMER_SERVICE, UPDATER_SERVICE, UPDATER_PATH_UNIT):
        assert (paths.systemd_dir / unit).read_bytes() == (ROOT / "deploy/systemd" / unit).read_bytes()
    assert (paths.logrotate_dir / "xianyu-saas").read_bytes() == (ROOT / "deploy/xianyu-saas-bot-logrotate.conf").read_bytes()
    assert not (run / "success/etc/nginx").exists()
    assert commands.availability_checks == [SYSTEMD_BIN, SYSTEMD_ANALYZE_BIN, USERADD_BIN]
    assert (SYSTEMD_BIN, "daemon-reload") in commands.commands
    assert (SYSTEMD_BIN, "enable", *START_UNITS) in commands.commands
    assert (SYSTEMD_BIN, "start", *START_UNITS) in commands.commands
    assert not any("nginx" in argument.lower() for command in commands.commands for argument in command)
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)
    requests_before = list(network.requests)
    commands_before = list(commands.commands)
    repeated = installer.install(architecture="x86_64")
    assert repeated["status"] == "already_installed" and repeated["version"] == VERSION
    assert network.requests == requests_before and commands.commands == commands_before
    print("installer: successful dry transaction, templates, identity and repeat passed")


def existing_environment_is_never_overwritten(run: Path) -> None:
    root = run / "existing-env"
    installer, _, _, _ = new_installer(root)
    installer.paths.env_file.parent.mkdir(parents=True)
    original = b"SAAS_AI_MASTER_KEY=operator-owned-value\nCUSTOM_SETTING=preserve-me\n"
    installer.paths.env_file.write_bytes(original)
    installer.install(architecture="x86_64")
    assert installer.paths.env_file.read_bytes() == original
    print("installer: existing environment file was preserved byte-for-byte")


def architecture_selection(run: Path) -> None:
    installer, _, _, network = new_installer(run / "arm")
    result = installer.install(architecture="aarch64", version=VERSION)
    assert result["architecture"] == "aarch64"
    requested = "\n".join(network.requests)
    assert f"linux-aarch64.tar.gz" in requested
    assert f"linux-x86_64.tar.gz" not in requested
    assert network.requests[0].endswith(f"/releases/tags/v{VERSION}")
    print("installer: requested version and architecture select only matching assets")


def tampering_is_rejected_before_service_changes(run: Path) -> None:
    cases = []
    public, responses = release_fixture()
    bad_signature = dict(responses)
    signature_url = next(url for url in responses if url.endswith("artifacts.json.sig"))
    bad_signature[signature_url] = base64.b64encode(b"x" * 64)
    cases.append(("index-signature", public, bad_signature, "manager_artifacts_signature_invalid"))

    def wrong_arch(records):
        for record in records:
            if record["name"].endswith("linux-x86_64.tar.gz"):
                record["target"] = "linux-aarch64"

    public, wrong_arch_responses = release_fixture(mutate_index=wrong_arch)
    cases.append(("architecture", public, wrong_arch_responses, "manager_release_asset_mismatch"))
    public, bad_path_responses = release_fixture(path_override="../escape")
    cases.append(("path", public, bad_path_responses, "manager_archive_path_invalid"))

    for label, public, responses, code in cases:
        commands = DryCommands()
        installer, _, _, _ = new_installer(run / label, responses=responses, public=public, commands=commands)
        expect_error(code, lambda installer=installer: installer.install(architecture="x86_64"))
        assert not installer.paths.initialization_file.exists()
        assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands), label
    print("installer: index signature, architecture and manifest path tampering rejected")


def legacy_layout_boundaries(run: Path) -> None:
    def shows(root: str, environment: str, *, shell=False):
        result = {}
        for unit in (API_SERVICE, CONSUMER_SERVICE):
            executable = f"/bin/bash -c {root}/start" if shell else f"{root}/runtime/backend-venv/bin/python -m uvicorn app:app"
            result[unit] = (
                f"Id={unit}\nLoadState=loaded\nFragmentPath=/etc/systemd/system/{unit}\n"
                f"ExecStart={executable}\nWorkingDirectory={root}/current/backend\n"
                f"EnvironmentFiles={environment}\n"
            )
        return result

    for root in ("/srv/xianyu-saas", "/opt/xianyu-saas"):
        commands = DryCommands(shows=shows(root, "/etc/xianyu-saas.env"))
        layout = discover_legacy_layout(commands)
        assert layout.kind == "supported" and layout.root == root
        installer, _, _, _ = new_installer(run / root.split("/")[-2], commands=commands)
        expect_error("manager_migration_not_implemented", lambda installer=installer: installer.install(architecture="x86_64"))
        assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    for label, observed in (
        ("custom", shows("/custom/app", "/custom/app/.env")),
        ("multiple-env", shows("/srv/xianyu-saas", "/etc/xianyu-saas.env /srv/xianyu-saas/.env")),
        ("shell", shows("/srv/xianyu-saas", "/etc/xianyu-saas.env", shell=True)),
    ):
        commands = DryCommands(shows=observed)
        assert discover_legacy_layout(commands).kind == "unsafe", label
        installer, _, _, _ = new_installer(run / f"unsafe-{label}", commands=commands)
        expect_error("manager_legacy_layout_unsafe", lambda installer=installer: installer.install(architecture="x86_64"))
        assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)
    print("installer: /srv and /opt legacy layouts are read-only detected; unsafe layouts fail closed")


def preflight_and_health_cleanup(run: Path) -> None:
    commands = DryCommands(port=False)
    installer, _, _, network = new_installer(run / "port", commands=commands)
    expect_error("manager_install_port_in_use", lambda: installer.install(architecture="x86_64"))
    assert not network.requests
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    installer, _, commands, network = new_installer(run / "launcher")
    installer.paths.launcher_link.parent.mkdir(parents=True, exist_ok=True)
    installer.paths.launcher_link.write_bytes(b"operator-owned launcher\n")
    expect_error("manager_install_path_occupied", lambda: installer.install(architecture="x86_64"))
    assert installer.paths.launcher_link.read_bytes() == b"operator-owned launcher\n"
    assert not network.requests
    assert not any(command[:2] == (SYSTEMD_BIN, "stop") for command in commands.commands)

    commands = DryCommands()

    def fail_initialization():
        raise ManagerError("manager_updater_failed")

    installer, _, _, _ = new_installer(
        run / "initialize", commands=commands, initializer=fail_initialization,
    )
    expect_error("manager_updater_failed", lambda: installer.install(architecture="x86_64"))
    assert not any(command[:2] in {(SYSTEMD_BIN, "start"), (SYSTEMD_BIN, "stop")} for command in commands.commands)
    assert not installer.paths.initialization_file.exists()
    assert installer.paths.current_link not in installer.fs.links
    assert installer.paths.manager_current_link not in installer.fs.links

    commands = DryCommands()
    installer, _, _, _ = new_installer(run / "health", commands=commands, healthy=False)
    expect_error("manager_install_health_failed", lambda: installer.install(architecture="x86_64"))
    assert (SYSTEMD_BIN, "stop", *reversed(START_UNITS)) in commands.commands
    assert (SYSTEMD_BIN, "disable", *START_UNITS) in commands.commands
    assert not any("nginx" in argument.lower() for command in commands.commands for argument in command)
    for _source, destination, _mode in installer._templates():
        assert not destination.exists()
    assert not installer.paths.initialization_file.exists()

    assert installer.paths.current_link not in installer.fs.links
    assert installer.paths.manager_current_link not in installer.fs.links
    assert installer.paths.launcher_link not in installer.fs.links

    commands = DryCommands()
    unmanaged_nginx = run / "health-unmanaged-nginx/etc/nginx/conf.d/operator.conf"
    unmanaged_nginx.parent.mkdir(parents=True, exist_ok=True)
    unmanaged_nginx.write_bytes(b"operator-owned nginx configuration\n")
    installer, _, _, _ = new_installer(run / "health-unmanaged-nginx", commands=commands, healthy=False)
    expect_error("manager_install_health_failed", lambda: installer.install(architecture="x86_64"))
    assert unmanaged_nginx.read_bytes() == b"operator-owned nginx configuration\n"
    assert list(unmanaged_nginx.parent.iterdir()) == [unmanaged_nginx]
    assert not any("nginx" in argument.lower() for command in commands.commands for argument in command)
    print("installer: preflight fails before stop; health failure stops services without managing nginx")


def static_boundaries() -> None:
    source = (ROOT / "deploy/manager/installer.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "subprocess.run" in source and "shell=False" in source
    assert GITHUB_API_ROOT == f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
    assert "artifacts.json.sig" in source and "Ed25519PublicKey" in source
    assert "nginx" not in source.lower()
    assert not CommandAdapter._allowed(("/usr/sbin/nginx", "-t"))
    assert not CommandAdapter._allowed((SYSTEMD_BIN, "reload-or-restart", "nginx.service"))
    service = (ROOT / "deploy/systemd/xianyu-saas.service").read_text(encoding="utf-8")
    assert "--host 0.0.0.0 --port 8096" in service
    assert "--limit-concurrency 100" in service and "--backlog 128" in service
    assert "--host 127.0.0.1" not in service
    assert 'HEALTH_URL = "http://127.0.0.1:8096/health"' in source
    spec = (ROOT / "deploy/manager/xianyu-saas-manager.spec").read_text(encoding="utf-8")
    for template in (
        "deploy/systemd/xianyu-saas.service", "deploy/systemd/xianyu-saas-consumer.service",
        "deploy/systemd/xianyu-saas-updater.service", "deploy/systemd/xianyu-saas-updater.path",
        "deploy/xianyu-saas-bot-logrotate.conf", "backend/standalone_runtime.py",
    ):
        assert template in spec
    assert "deploy/nginx/" not in spec and "nginx" not in spec.lower()
    assert "Path(SPECPATH).resolve().parents[1]" in spec
    assert 'name="xianyu-saas"' in spec
    assert MANAGER_RELEASES_DIR == Path("/opt/xianyu-saas/manager/releases")
    assert MANAGER_CURRENT_LINK == Path("/opt/xianyu-saas/manager/current")
    assert MANAGER_INSTALL_PATH == Path("/usr/local/bin/xianyu-saas")
    assert ASSET_ARCHITECTURES == {"x86_64": "x86_64", "aarch64": "aarch64"}
    assert map_architecture("AMD64") == "x86_64"
    assert map_architecture("arm64") == "aarch64"
    assert parse_args(["install", "--version", VERSION]).release_version == VERSION
    print("installer: fixed network, command and frozen-template boundaries passed")


def main() -> None:
    static_boundaries()
    with tempfile.TemporaryDirectory(prefix="xianyu-installer-contract-") as temporary:
        run = Path(temporary)
        successful_transaction_and_repeat(run)
        existing_environment_is_never_overwritten(run)
        architecture_selection(run)
        tampering_is_rejected_before_service_changes(run)
        legacy_layout_boundaries(run)
        preflight_and_health_cleanup(run)
    print("systemd installer contract: passed")


if __name__ == "__main__":
    main()
