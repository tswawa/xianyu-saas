"""Fixed, shell-free systemctl operations."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable

from .constants import APPLICATION_UNITS, MANAGED_UNITS, SYSTEMCTL_BINARY
from .errors import ManagerError


SYSTEMCTL_TIMEOUT_SECONDS = 90
_ACTION_UNITS = {
    "start": APPLICATION_UNITS,
    "stop": tuple(reversed(APPLICATION_UNITS)),
    "restart": APPLICATION_UNITS,
}


def validate_units(units: Iterable[str]) -> tuple[str, ...]:
    requested = tuple(units)
    if not requested or any(unit not in MANAGED_UNITS for unit in requested):
        raise ManagerError("manager_service_invalid")
    return requested


def command_for(action: str, units: Iterable[str]) -> tuple[str, ...]:
    allowed = validate_units(units)
    if action not in {"start", "stop", "restart", "show"}:
        raise ManagerError("manager_service_invalid")
    if action == "show":
        return (
            str(SYSTEMCTL_BINARY),
            "show",
            "--no-pager",
            "--property=Id,LoadState,ActiveState,SubState,UnitFileState",
            *allowed,
        )
    return (str(SYSTEMCTL_BINARY), action, *allowed)


def _environment() -> dict[str, str]:
    return {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }


class Systemctl:
    def __init__(self, executor: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self._executor = executor

    def _run(self, command: tuple[str, ...], *, capture: bool) -> subprocess.CompletedProcess:
        try:
            completed = self._executor(
                list(command),
                check=False,
                shell=False,
                cwd="/",
                env=_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=SYSTEMCTL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ManagerError("manager_systemctl_failed") from exc
        if completed.returncode != 0:
            raise ManagerError("manager_systemctl_failed")
        return completed

    def control(self, action: str) -> dict:
        try:
            units = _ACTION_UNITS[action]
        except KeyError as exc:
            raise ManagerError("manager_service_invalid") from exc
        self._run(command_for(action, units), capture=False)
        return {"ok": True, "action": action, "units": list(units)}

    def status(self) -> dict:
        completed = self._run(command_for("show", MANAGED_UNITS), capture=True)
        return {"ok": True, "action": "status", "units": list(MANAGED_UNITS), "systemctl_output": completed.stdout or ""}
