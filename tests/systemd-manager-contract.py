#!/usr/bin/env python3
"""Contracts for the onefile systemd manager; never invokes real systemctl."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deploy.manager import MANAGER_PROTOCOL, MANAGER_VERSION
from deploy.manager.cli import Command, dispatch, parse_args
from deploy.manager.constants import (
    APPLICATION_UNITS,
    CURRENT_LINK,
    MANAGED_UNITS,
    MANAGER_CURRENT_LINK,
    MANAGER_RELEASES_DIR,
    RELEASES_DIR,
    STATE_DIR,
    SYSTEMCTL_BINARY,
    UPDATE_QUEUE_DIR,
    UPDATER_STATE_DIR,
)
from deploy.manager.errors import ManagerError
from deploy.manager.runtime import (
    embedded_public_key_path,
    manager_sha256,
    map_architecture,
    read_embedded_public_key,
    self_check,
)
from deploy.manager.systemctl import Systemctl, command_for, validate_units
from deploy.manager.updater_bridge import invoke_updater, updater_arguments


def expect_error(code, operation):
    try:
        operation()
    except ManagerError as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError(f"expected {code}")


def parsing_contracts():
    for name in ("install", "status", "start", "stop", "restart", "doctor"):
        assert parse_args([name]) == Command(name)
    for name in ("consume-intent", "initialize", "resume", "self-check"):
        assert parse_args(["internal", name]) == Command("internal", name)
    baseline = parse_args([
        "internal", "import-baseline", "/srv/release.tar.gz", "/srv/manifest.json", "/srv/manifest.sig"
    ])
    assert baseline.paths == tuple(map(PurePosixPath, (
        "/srv/release.tar.gz", "/srv/manifest.json", "/srv/manifest.sig"
    )))
    for invalid in (
        [], ["sta"], ["start", "x.service"], ["internal", "consume"],
        ["internal", "import-baseline", "relative", "/m", "/s"],
        ["internal", "import-baseline", "/a/../b", "/m", "/s"],
    ):
        expect_error("manager_cli_invalid", lambda invalid=invalid: parse_args(invalid))


def architecture_contracts():
    for source, target in {
        "x86_64": "x86_64", "AMD64": "x86_64", "aarch64": "aarch64", "ARM64": "aarch64"
    }.items():
        assert map_architecture(source) == target
    expect_error("manager_architecture_unsupported", lambda: map_architecture("riscv64"))


def root_and_install_contracts():
    calls = []

    class FakeInstaller:
        def install(self, *, architecture):
            calls.append(architecture)
            return {"ok": True, "architecture": architecture}

    expect_error(
        "manager_root_required",
        lambda: dispatch(Command("install"), installer=FakeInstaller(), platform_name="linux", machine="x86_64", euid=1000),
    )
    assert not calls
    result = dispatch(
        Command("install"), installer=FakeInstaller(), platform_name="linux", machine="x86_64", euid=0
    )
    assert result == {"ok": True, "architecture": "x86_64"}
    assert calls == ["x86_64"]
    expect_error(
        "manager_linux_required",
        lambda: dispatch(Command("install"), installer=FakeInstaller(), platform_name="win32", machine="AMD64", euid=0),
    )


class RecordingExecutor:
    def __init__(self):
        self.commands = []

    def __call__(self, command, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["cwd"] == "/"
        assert kwargs["stdin"] == subprocess.DEVNULL
        self.commands.append(tuple(command))
        output = "Id=xianyu-saas.service\nActiveState=active\n" if "show" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")


def systemctl_contracts():
    assert set(APPLICATION_UNITS) < set(MANAGED_UNITS)
    assert command_for("start", APPLICATION_UNITS) == (
        str(SYSTEMCTL_BINARY), "start", *APPLICATION_UNITS
    )
    expect_error("manager_service_invalid", lambda: validate_units(("ssh.service",)))
    expect_error("manager_service_invalid", lambda: command_for("enable", APPLICATION_UNITS))

    executor = RecordingExecutor()
    manager = Systemctl(executor)
    manager.control("start")
    manager.control("stop")
    status = manager.status()
    assert status["ok"] is True and "ActiveState=active" in status["systemctl_output"]
    assert executor.commands == [
        (str(SYSTEMCTL_BINARY), "start", *APPLICATION_UNITS),
        (str(SYSTEMCTL_BINARY), "stop", *reversed(APPLICATION_UNITS)),
        (
            str(SYSTEMCTL_BINARY), "show", "--no-pager",
            "--property=Id,LoadState,ActiveState,SubState,UnitFileState", *MANAGED_UNITS,
        ),
    ]


def public_key_and_self_check_contracts():
    source_key = read_embedded_public_key(source_root=ROOT)
    assert source_key == (ROOT / "deploy/update-signing.pub").read_bytes()
    with tempfile.TemporaryDirectory(prefix="manager-contract-") as directory:
        root = Path(directory)
        embedded = root / "deploy/update-signing.pub"
        embedded.parent.mkdir(parents=True)
        embedded.write_bytes(source_key)
        assert embedded_public_key_path(meipass=root) == embedded
        assert read_embedded_public_key(meipass=root) == source_key
        executable = root / "manager.bin"
        executable.write_bytes(b"synthetic manager executable")
        assert manager_sha256(executable) == hashlib.sha256(executable.read_bytes()).hexdigest()
        report = self_check(
            platform_name="linux", machine="aarch64", euid=1000,
            meipass=root, executable=executable,
        )
        assert report["ok"] is True
        assert report["version"] == MANAGER_VERSION
        assert report["manager_protocol"] == MANAGER_PROTOCOL
        assert report["architecture"] == "aarch64"
        assert report["root"] is False
        assert report["public_key_sha256"] == hashlib.sha256(source_key).hexdigest()
        embedded.write_bytes(b"not a public key")
        expect_error("manager_public_key_invalid", lambda: read_embedded_public_key(meipass=root))


def internal_mapping_contracts():
    assert updater_arguments("consume-intent") == ()
    assert updater_arguments("initialize") == ("--initialize",)
    assert updater_arguments("resume") == ()
    paths = tuple(map(PurePosixPath, ("/a.tar.gz", "/a.json", "/a.sig")))
    assert updater_arguments("import-baseline", paths) == (
        "--import-trusted-baseline", "/a.tar.gz", "/a.json", "/a.sig"
    )
    expect_error("manager_internal_invalid", lambda: updater_arguments("self-check"))

    observations = []

    class FakeUpdater:
        @staticmethod
        def main():
            observations.append((tuple(sys.argv[1:]), dict(os.environ)))
            return 0

    original_argv = sys.argv
    inherited_database = ROOT / ".local/manager-contract/inherited.db"
    inherited_tenants = ROOT / ".local/manager-contract/inherited-tenants"
    with patch.dict(os.environ, {
        "SAAS_CURRENT_ROOT": "/attacker",
        "SAAS_API_SERVICE": "attacker.service",
        "SAAS_ENV": "development",
        "SAAS_TESTING": "1",
        "SAAS_DB": str(inherited_database),
        "SAAS_TENANTS_DIR": str(inherited_tenants),
    }, clear=False):
        result = invoke_updater("import-baseline", paths, loader=lambda: FakeUpdater)
        assert os.environ["SAAS_CURRENT_ROOT"] == "/attacker"
        assert os.environ["SAAS_API_SERVICE"] == "attacker.service"
        assert os.environ["SAAS_ENV"] == "development"
        assert os.environ["SAAS_TESTING"] == "1"
    assert result["ok"] is True
    argv, environment = observations[0]
    assert argv == updater_arguments("import-baseline", paths)
    assert Path(environment["SAAS_UPDATE_PUBLIC_KEY_FILE"]) == ROOT / "deploy/update-signing.pub"
    assert Path(environment["SAAS_CURRENT_ROOT"]) == CURRENT_LINK
    assert Path(environment["SAAS_RELEASES_DIR"]) == RELEASES_DIR
    assert Path(environment["SAAS_STATE_DIR"]) == STATE_DIR
    assert Path(environment["SAAS_DB"]) == inherited_database
    assert Path(environment["SAAS_TENANTS_DIR"]) == inherited_tenants
    assert Path(environment["SAAS_UPDATE_INTENT_FILE"]) == UPDATE_QUEUE_DIR / "intent.json"
    assert Path(environment["SAAS_UPDATER_STATE_DIR"]) == UPDATER_STATE_DIR
    assert Path(environment["SAAS_MANAGER_RELEASES_DIR"]) == MANAGER_RELEASES_DIR
    assert Path(environment["SAAS_MANAGER_CURRENT"]) == MANAGER_CURRENT_LINK
    assert environment["SAAS_API_SERVICE"] == "xianyu-saas.service"
    assert environment["SAAS_UPDATE_HEALTH_BASE_URL"] == "http://127.0.0.1:8096/"
    assert environment["SAAS_UPDATE_PUBLIC_BASE_URL"] == "http://127.0.0.1:8096/xianyu-saas/"
    assert environment["SAAS_RELEASE_KIND"] == "standalone"
    assert environment["SAAS_ENV"] == "production"
    assert environment["SAAS_TESTING"] == "0"
    assert sys.argv is original_argv

    database = ROOT / ".local/manager-contract/saas.db"
    tenants = ROOT / ".local/manager-contract/tenants"
    invoke_updater(
        "initialize",
        loader=lambda: FakeUpdater,
        environment_overrides={"SAAS_DB": str(database), "SAAS_TENANTS_DIR": str(tenants)},
    )
    _, environment = observations[1]
    assert Path(environment["SAAS_DB"]) == database
    assert Path(environment["SAAS_TENANTS_DIR"]) == tenants
    expect_error(
        "manager_internal_invalid",
        lambda: invoke_updater(
            "initialize", loader=lambda: FakeUpdater,
            environment_overrides={"SAAS_API_SERVICE": "attacker.service"},
        ),
    )
    with patch.dict(os.environ, {"SAAS_DB": "relative.db"}, clear=False):
        expect_error(
            "manager_internal_invalid",
            lambda: invoke_updater("resume", loader=lambda: FakeUpdater),
        )

    invoked = []
    dispatch(
        Command("internal", "resume"), updater_invoker=lambda action, arguments: invoked.append((action, arguments)) or {"ok": True},
        platform_name="linux", machine="x86_64", euid=0,
    )
    assert invoked == [("resume", ())]


def static_bundle_contracts():
    spec = (ROOT / "deploy/manager/xianyu-saas-manager.spec").read_text(encoding="utf-8")
    assert "upx=False" in spec
    assert "COLLECT(" not in spec
    assert 'name="xianyu-saas"' in spec
    assert 'VERSION_FILE = ROOT / "backend/version.py"' in spec
    assert "deploy/nginx/" not in spec and "nginx" not in spec.lower()
    installer = (ROOT / "deploy/manager/installer.py").read_text(encoding="utf-8")
    assert "nginx" not in installer.lower()
    service = (ROOT / "deploy/systemd/xianyu-saas.service").read_text(encoding="utf-8")
    assert "--host 0.0.0.0 --port 8096" in service
    assert "--limit-concurrency 100" in service and "--backlog 128" in service
    assert "--host 127.0.0.1" not in service
    for relative in (
        "deploy/update-signing.pub", "deploy/updater/updater.py", "backend/platform_update.py",
        "backend/standalone_runtime.py", "backend/update_maintenance.py", "backend/version.py",
    ):
        assert relative in spec


if __name__ == "__main__":
    parsing_contracts()
    architecture_contracts()
    root_and_install_contracts()
    systemctl_contracts()
    public_key_and_self_check_contracts()
    internal_mapping_contracts()
    static_bundle_contracts()
    print("systemd manager contract: passed")
