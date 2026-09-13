"""Read executor-owned maintenance state without granting the web process write access.

A present but invalid marker fails closed. Only the executor may clear maintenance;
clock changes or a stale heartbeat never automatically re-enable business writes.
"""
from __future__ import annotations

import ast
import errno
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
from typing import Callable


MAINTENANCE_PROTOCOL = 1
_MAX_JSON_DEPTH = 64
_JSON_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff", b"\xff\xfe", b"\xfe\xff")


def supports_maintenance_protocol(source: str | bytes) -> bool:
    """Check a signed candidate's declaration without importing its code.

    This compatibility check does not replace signature/inventory verification.
    A single module-level literal declaration is required; ambiguous or dynamic
    declarations and other syntactic writes to the same name are rejected.
    """
    if not isinstance(source, (str, bytes)) or len(source) > 256 * 1024:
        return False
    try:
        tree = ast.parse(source)
        declarations = [
            node for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "MAINTENANCE_PROTOCOL"
        ]
        if len(declarations) != 1:
            return False
        declaration = declarations[0]
        if (not isinstance(declaration.value, ast.Constant)
                or type(declaration.value.value) is not int
                or declaration.value.value != MAINTENANCE_PROTOCOL):
            return False

        def binds_protocol(node: ast.AST) -> bool:
            if (isinstance(node, ast.Name) and node.id == "MAINTENANCE_PROTOCOL"
                    and isinstance(node.ctx, (ast.Store, ast.Del))):
                return True
            if isinstance(node, ast.alias):
                if node.name == "*":
                    return True
                return (node.asname or node.name.split(".", 1)[0]) == "MAINTENANCE_PROTOCOL"
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return node.name == "MAINTENANCE_PROTOCOL"
            if isinstance(node, ast.ExceptHandler):
                return node.name == "MAINTENANCE_PROTOCOL"
            if isinstance(node, (ast.MatchAs, ast.MatchStar)):
                return node.name == "MAINTENANCE_PROTOCOL"
            if isinstance(node, ast.MatchMapping):
                return node.rest == "MAINTENANCE_PROTOCOL"
            return False

        bindings = [node for node in ast.walk(tree) if binds_protocol(node)]
        return len(bindings) == 1 and bindings[0] is declaration.targets[0]
    except (SyntaxError, ValueError, TypeError, RecursionError, MemoryError, OverflowError):
        return False


class UpdateStateError(RuntimeError):
    def __init__(self, code: str = "update_state_untrusted"):
        self.code = code
        super().__init__(code)


def status_directory() -> Path:
    override = os.environ.get("SAAS_UPDATE_STATUS_DIR", "").strip()
    if override:
        return Path(override)
    docker_root = os.environ.get("SAAS_DOCKER_UPDATE_ROOT", "").strip()
    if docker_root or os.environ.get("SAAS_DEPLOYMENT_MODE", "").lower() == "docker" or Path("/.dockerenv").exists():
        return Path(docker_root or "/updates") / "status"
    intent = Path(os.environ.get("SAAS_UPDATE_INTENT_FILE", "/var/lib/xianyu-saas-updates/intent.json"))
    return intent.parent / "status"


def maintenance_file() -> Path:
    override = os.environ.get("SAAS_UPDATE_MAINTENANCE_FILE", "").strip()
    return Path(override) if override else status_directory() / "maintenance.json"


def _trusted_directory(metadata, owner_uid: int) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, owner_uid}:
        raise UpdateStateError()
    writable = stat.S_IMODE(metadata.st_mode) & 0o022
    # A root-owned sticky /tmp is safe to traverse; descendants still have their
    # own owner/mode checks. Non-sticky writable parents can replace children.
    if writable and not (metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX):
        raise UpdateStateError()


def _parse_trusted_json(data: bytes) -> object:
    """Parse bounded executor JSON after a shallow, BOM-free UTF-8 scan."""
    try:
        # json.loads(bytes) auto-detects UTF-16/32. The executor contract is
        # BOM-free UTF-8, so decode first to keep the depth scan authoritative.
        if data.startswith(_JSON_BOMS):
            raise ValueError("state JSON must be BOM-free UTF-8")
        text = data.decode("utf-8")
        depth = 0
        in_string = False
        escaped = False
        for character in text:
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
            elif character == '"':
                in_string = True
            elif character in "[{":
                depth += 1
                if depth > _MAX_JSON_DEPTH:
                    raise ValueError("state JSON nesting is too deep")
            elif character in "]}":
                if depth == 0:
                    raise ValueError("state JSON containers are unbalanced")
                depth -= 1
        if in_string or depth:
            raise ValueError("state JSON is incomplete")

        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate state key")
                result[key] = value
            return result

        def reject_constant(_value):
            raise ValueError("non-finite state value")

        return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError, OverflowError) as error:
        raise UpdateStateError() from error


def read_trusted_json(path: str | Path, *, max_bytes: int = 64 * 1024, owner_uid: int = 0) -> dict | None:
    """Read a bounded public executor record through pinned, no-follow dir fds.

    Files and their directory chain must be owned by the trusted executor, never
    writable by its group or other users. Return None only for a genuinely absent
    component. Native Windows cannot establish this POSIX ownership guarantee.
    """
    path = Path(path)
    if max_bytes <= 0:
        raise UpdateStateError("update_state_path_invalid")
    if os.name != "posix" or os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise UpdateStateError() from error
        raise UpdateStateError("update_state_platform_unsupported")
    if not path.is_absolute() or ".." in path.parts:
        raise UpdateStateError("update_state_path_invalid")

    directories: list[int] = []
    descriptor = -1
    directory_flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current = os.open(path.anchor, directory_flags)
        directories.append(current)
        _trusted_directory(os.fstat(current), owner_uid)
        for component in path.parts[1:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            directories.append(current)
            _trusted_directory(os.fstat(current), owner_uid)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=current)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != owner_uid or stat.S_IMODE(before.st_mode) & 0o022
                or before.st_size < 2 or before.st_size > max_bytes):
            raise UpdateStateError()
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(descriptor, min(16384, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        if (total != before.st_size or total > max_bytes
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise UpdateStateError()
        value = _parse_trusted_json(b"".join(chunks))
        if not isinstance(value, dict):
            raise UpdateStateError()
        return value
    except FileNotFoundError:
        return None
    except UpdateStateError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError) as error:
        code = "update_state_untrusted"
        if isinstance(error, OSError) and error.errno == errno.ENAMETOOLONG:
            code = "update_state_path_invalid"
        raise UpdateStateError(code) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for directory in reversed(directories):
            os.close(directory)


def parse_maintenance_payload(payload: dict | None) -> dict:
    if payload is None:
        return {"schema": 1, "active": False, "operation_id": "", "phase": "", "updated_at": 0.0, "error_code": ""}
    if not isinstance(payload, dict) or type(payload.get("schema")) is not int or payload["schema"] != 1 or not isinstance(payload.get("active"), bool):
        raise UpdateStateError("update_maintenance_invalid")
    active = payload["active"]
    operation_id = payload.get("operation_id") or ""
    phase = payload.get("phase") or ""
    updated_at = payload.get("updated_at", 0)
    if (not isinstance(operation_id, str) or not isinstance(phase, str) or len(phase) > 64
            or (operation_id and not re.fullmatch(r"[0-9a-f]{32}", operation_id))
            or (active and not operation_id)
            or isinstance(updated_at, bool) or not isinstance(updated_at, (int, float))):
        raise UpdateStateError("update_maintenance_invalid")
    try:
        updated_at = float(updated_at)
    except (OverflowError, ValueError):
        raise UpdateStateError("update_maintenance_invalid") from None
    if not math.isfinite(updated_at) or updated_at < 0:
        raise UpdateStateError("update_maintenance_invalid")
    return {"schema": 1, "active": active, "operation_id": operation_id, "phase": phase,
            "updated_at": updated_at, "error_code": ""}


def _blocked_state() -> dict:
    return {"schema": 1, "active": True, "operation_id": "", "phase": "recovery_failed",
            "updated_at": 0.0, "error_code": "update_maintenance_untrusted"}


def read_maintenance() -> dict:
    try:
        return parse_maintenance_payload(read_trusted_json(maintenance_file(), max_bytes=16384))
    except Exception:
        # Malformed numbers/nesting and unexpected parser failures must never
        # leave the API and workers assuming that maintenance was cleared.
        return _blocked_state()


def maintenance_active() -> bool:
    return read_maintenance()["active"]


class MaintenanceWatcher:
    """Run enter/leave callbacks once per successful maintenance transition."""

    def __init__(self, on_enter: Callable[[dict], None], on_leave: Callable[[], None], *,
                 reader: Callable[[], dict] = read_maintenance, interval: float = 1.0):
        self._on_enter = on_enter
        self._on_leave = on_leave
        self._reader = reader
        self._interval = max(0.1, float(interval))
        self._active = False
        self._operation = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()

    def poll_once(self) -> dict:
        with self._poll_lock:
            try:
                state = self._reader()
                if not isinstance(state, dict) or not isinstance(state.get("active"), bool):
                    raise UpdateStateError()
                active = state["active"]
                operation = str(state.get("operation_id") or "")
            except Exception:
                state = _blocked_state()
                active, operation = True, ""
            if active and (not self._active or operation != self._operation):
                self._on_enter(state)
            elif not active and self._active:
                self._on_leave()
            # A failed callback leaves the old transition pending for retry.
            self._active = active
            self._operation = operation if active else ""
            return state

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                # Callback exceptions may include credentials; don't expose them.
                pass
            self._stop.wait(self._interval)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="update-maintenance", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
