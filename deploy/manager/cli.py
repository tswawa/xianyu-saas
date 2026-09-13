"""Command-line contract for the systemd deployment manager."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

from .constants import ROOT_COMMANDS, SYSTEMCTL_BINARY
from .errors import ManagerError
from .installer import Installer, InstallerLike
from .runtime import MANAGER_VERSION, map_architecture, require_linux, require_root, self_check
from .systemctl import Systemctl
from .updater_bridge import invoke_updater


@dataclass(frozen=True)
class Command:
    name: str
    internal_action: str = ""
    paths: tuple[PurePosixPath, ...] = ()
    release_version: str | None = None

    @property
    def key(self) -> str:
        return f"internal:{self.internal_action}" if self.name == "internal" else self.name


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ManagerError("manager_cli_invalid", message)


def _absolute_path(value: str) -> PurePosixPath:
    if not value or "\x00" in value:
        raise argparse.ArgumentTypeError("path must be absolute")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(prog="xianyu-saas-manager", allow_abbrev=False)
    parser.add_argument("--version", action="version", version=MANAGER_VERSION)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeArgumentParser)
    install = commands.add_parser("install", allow_abbrev=False)
    install.add_argument("--version", dest="release_version")
    for name in ("status", "start", "stop", "restart", "doctor"):
        commands.add_parser(name, allow_abbrev=False)

    internal = commands.add_parser("internal", allow_abbrev=False)
    internal_commands = internal.add_subparsers(
        dest="internal_command", required=True, parser_class=SafeArgumentParser
    )
    for name in ("consume-intent", "initialize", "resume", "self-check"):
        internal_commands.add_parser(name, allow_abbrev=False)
    baseline = internal_commands.add_parser("import-baseline", allow_abbrev=False)
    baseline.add_argument("archive", type=_absolute_path)
    baseline.add_argument("manifest", type=_absolute_path)
    baseline.add_argument("signature", type=_absolute_path)
    return parser


def parse_args(argv: Sequence[str]) -> Command:
    try:
        parsed = build_parser().parse_args(list(argv))
    except argparse.ArgumentError as exc:
        raise ManagerError("manager_cli_invalid") from exc
    if parsed.command != "internal":
        return Command(parsed.command, release_version=getattr(parsed, "release_version", None))
    paths = ()
    if parsed.internal_command == "import-baseline":
        paths = (parsed.archive, parsed.manifest, parsed.signature)
    return Command("internal", parsed.internal_command, paths)


def dispatch(
    command: Command,
    *,
    installer: InstallerLike | None = None,
    service_manager: Systemctl | None = None,
    updater_invoker=invoke_updater,
    platform_name: str | None = None,
    machine: str | None = None,
    euid: int | None = None,
) -> dict:
    require_linux(platform_name)
    architecture = map_architecture(machine)
    if command.key in ROOT_COMMANDS:
        require_root(euid)

    if command.name == "install":
        try:
            target = installer or Installer()
            if command.release_version is None:
                return target.install(architecture=architecture)
            return target.install(architecture=architecture, version=command.release_version)
        except ManagerError:
            raise
        except Exception as exc:
            raise ManagerError("manager_install_failed") from exc
    if command.name in {"start", "stop", "restart"}:
        return (service_manager or Systemctl()).control(command.name)
    if command.name == "status":
        return (service_manager or Systemctl()).status()
    if command.name in {"doctor", "internal"} and (
        command.name == "doctor" or command.internal_action == "self-check"
    ):
        report = self_check(platform_name=platform_name, machine=machine, euid=euid)
        if command.name == "doctor":
            report["systemctl_available"] = SYSTEMCTL_BINARY.is_file()
            report["action"] = "doctor"
        else:
            report["action"] = "self-check"
        return report
    if command.name == "internal":
        return updater_invoker(command.internal_action, command.paths)
    raise ManagerError("manager_cli_invalid")


def run(argv: Sequence[str], **dependencies) -> dict:
    return dispatch(parse_args(argv), **dependencies)


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = run(sys.argv[1:] if argv is None else argv)
    except ManagerError as exc:
        print(_json({"ok": False, "error": exc.code}), file=sys.stderr)
        return exc.exit_code
    except Exception:
        print(_json({"ok": False, "error": "manager_failed"}), file=sys.stderr)
        return 70
    print(_json(result if isinstance(result, dict) else {"ok": True}))
    return 0
