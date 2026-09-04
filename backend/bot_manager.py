"""Per-tenant process supervision for the managed Xianyu agent.

Only ``rules_ai`` workers receive a lifecycle-scoped token for the loopback
account-scoped AI proxy.  Deterministic ``rules`` workers never receive model
configuration or an AI token.  Long-lived keys, upstream URLs and real model
names are never placed in child environments.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from account_storage import (
    AccountStorage,
    AccountStorageError,
    DEFAULT_ACCOUNT_ID,
    normalize_account_key,
)
from automation import AutomationValidationError, normalise_settings, rules_document
from platform_ai import issue_token, revoke_token
from shop_sync import (
    PERSISTED_SYNC_CODES,
    ShopSyncError,
    account_ref,
    load_sync_state,
    load_verified_snapshot,
    parse_cookie_header,
    sync_status_payload,
)


TENANTS_ROOT = os.environ.get("SAAS_TENANTS_DIR", "/var/lib/xianyu-saas/tenants")
BOT_ROOT = Path(os.environ.get("SAAS_BOT_ROOT", "/opt/xianyu-autoagent")).resolve()
BOT_MAIN = str(BOT_ROOT / "main.py")
BOT_PYTHON = str(BOT_ROOT / ".venv/bin/python")
MAX_BOTS = int(os.environ.get("SAAS_MAX_BOTS", "15"))
MEM_LIMIT_MB = int(os.environ.get("SAAS_BOT_MEM_MB", "400"))
ACCESS_RECONCILE_SECONDS = max(
    1.0,
    min(float(os.environ.get("SAAS_ACCESS_RECONCILE_SECONDS", "1")), 60.0),
)
PLATFORM_AI_BASE_URL = "http://127.0.0.1:8096/internal/v1"
AUTH_STATUS_FILE = "auth_status.json"
AUTH_STATUS_CODES = {
    "ok",
    "session_expired",
    "risk_control",
    "token_unavailable",
    "network_error",
    "platform_busy",
    "response_invalid",
}
AUTH_PHASES = {
    "UNCONFIGURED",
    "SESSION_VALID",
    "TOKEN_VALID",
    "WS_REGISTERED",
    "DEGRADED",
    "NEEDS_HUMAN",
}
INITIAL_ACCOUNT_FILES = {
    "redeem_codes.json": "[]",
    "pan_links.json": '{"links": []}',
    "reply_rules.json": '{"version": 1, "rules": []}',
    "automation_settings.json": '{"version": 1, "strategy": "standard", "enabled": true}',
    # An empty mapping keeps the agent bootable while preventing automatic
    # delivery until the owner explicitly configures a product.
    "products_config.json": '{"types": []}',
}


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


TOKEN_STARTUP_JITTER_MAX_SECONDS = _bounded_int_env(
    "SAAS_TOKEN_STARTUP_JITTER_MAX_SECONDS", 30, 0, 300
)
TOKEN_REFRESH_JITTER_MAX_SECONDS = _bounded_int_env(
    "SAAS_TOKEN_REFRESH_JITTER_MAX_SECONDS", 300, 0, 1800
)

_lock = threading.RLock()
_procs: dict[tuple[int, str], subprocess.Popen] = {}
_tokens: dict[tuple[int, str], str] = {}
_log_files: dict[tuple[int, str], object] = {}
_modes: dict[tuple[int, str], str] = {}
_generations: dict[tuple[int, str], int] = {}
_desired_running: dict[tuple[int, str], bool] = {}
_transitions: dict[tuple[int, str], "_ProcessTransition"] = {}


class _ProcessTransition:
    """An old process being retired before an optional replacement starts."""

    def __init__(self, proc, mode: str, log_file=None):
        self.proc = proc
        self.mode = mode
        self.log_file = log_file
        self.done = threading.Event()
        self.terminated: bool | None = None
        self.reason = "stopping"
        self.terminating = False


class _AdoptedProcess:
    """Small ``Popen``-compatible handle for a worker started by an old API.

    An API restart cannot call ``waitpid`` for a process it did not spawn, but
    it can still observe and signal the process.  Keeping this handle private
    lets the rest of the supervisor use the same status/stop paths for both
    newly spawned and safely adopted workers.
    """

    def __init__(self, pid: int, start_time: str, pidfd: int | None = None):
        self.pid = int(pid)
        self.start_time = str(start_time)
        self.pidfd = pidfd
        self.returncode: int | None = None

    def identity_matches(self) -> bool:
        return bool(self.start_time and _proc_start_time(self.pid) == self.start_time)

    def _close_pidfd(self) -> None:
        if self.pidfd is None:
            return
        try:
            os.close(self.pidfd)
        except OSError:
            pass
        self.pidfd = None

    def send_signal(self, signal_number: int) -> bool:
        """Signal the adopted task atomically when Linux pidfd is available."""
        if self.pidfd is None or not hasattr(signal, "pidfd_send_signal"):
            # A start-time check followed by kill(2) still has a reuse window.
            # Fail closed rather than risk signalling an unrelated process.
            return False
        try:
            signal.pidfd_send_signal(self.pidfd, signal_number, None, 0)
            return True
        except ProcessLookupError:
            self.returncode = 0
            self._close_pidfd()
            return True
        except OSError:
            return False

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if _pid_alive(self.pid) and self.identity_matches():
            return None
        self.returncode = 0
        self._close_pidfd()
        return self.returncode

    def wait(self, timeout=None):
        if self.poll() is not None:
            return self.returncode
        deadline = None if timeout is None else time.monotonic() + max(float(timeout), 0.0)
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired([BOT_MAIN], timeout)
            time.sleep(0.05)
        return self.returncode


def _storage() -> AccountStorage:
    return AccountStorage(TENANTS_ROOT)


def _account_key(account_key: str | None = DEFAULT_ACCOUNT_ID) -> str:
    try:
        return normalize_account_key(account_key)
    except AccountStorageError as error:
        raise ValueError("invalid account key") from error


def _proc_key(user_id: int, account_key: str | None = DEFAULT_ACCOUNT_ID) -> tuple[int, str]:
    return int(user_id), _account_key(account_key)


def _stable_account_jitter(account_key: str, maximum: int, purpose: str) -> int:
    if maximum <= 0:
        return 0
    digest = hashlib.sha256(f"{purpose}:{account_key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (maximum + 1)


def tenant_dir(user_id: int, account_key: str | None = DEFAULT_ACCOUNT_ID) -> str:
    try:
        return str(_storage().account_dir(user_id, account_key))
    except AccountStorageError as error:
        raise ValueError("invalid account storage path") from error


def _pid_alive(pid: int) -> bool:
    """Return whether a PID is still running (including a portable fallback)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    proc_state = Path("/proc") / str(pid) / "stat"
    try:
        # Linux reports zombies as ``Z``.  Treating one as alive would make a
        # stale persisted PID suppress recovery forever.
        stat_line = proc_state.read_text(encoding="utf-8")
        close = stat_line.rfind(")")
        if close >= 0 and len(stat_line) > close + 2 and stat_line[close + 2] == "Z":
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _proc_start_time(pid: int) -> str | None:
    """Return Linux ``/proc/<pid>/stat`` start-time identity (field 22)."""
    try:
        stat_line = (Path("/proc") / str(int(pid)) / "stat").read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError):
        return None
    close = stat_line.rfind(")")
    if close < 0:
        return None
    fields = stat_line[close + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def _proc_cmdline(pid: int) -> list[str]:
    try:
        raw = (Path("/proc") / str(int(pid)) / "cmdline").read_bytes()
    except (OSError, TypeError, ValueError):
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _expected_worker_pid(
    user_id: int,
    pid: int,
    account_key: str | None = DEFAULT_ACCOUNT_ID,
) -> bool:
    """Validate a persisted PID before it can be signalled or adopted.

    PID files are untrusted after a crash because the operating system may
    reuse the number.  On Linux we require the exact configured interpreter,
    worker entry point, working directory and tenant data directory.  The
    command/cwd checks also make this safe on hosts where ``/proc/*/environ``
    is hidden by the kernel's ptrace policy.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if not _pid_alive(pid):
        return False
    command = _proc_cmdline(pid)
    if len(command) < 2:
        return False
    try:
        if os.path.realpath(command[0]) != os.path.realpath(BOT_PYTHON):
            return False
        if os.path.realpath(command[1]) != os.path.realpath(BOT_MAIN):
            return False
    except (OSError, TypeError):
        return False
    proc_root = Path("/proc") / str(pid)
    try:
        cwd = os.path.realpath(os.readlink(proc_root / "cwd"))
    except OSError:
        return False
    if cwd != os.path.realpath(str(BOT_ROOT)):
        return False
    try:
        environ = (proc_root / "environ").read_bytes().split(b"\0")
    except OSError:
        # Command and cwd are shared by every tenant worker, so they cannot
        # safely distinguish one account from another. Fail closed unless the
        # tenant marker can be verified.
        return False
    values = {}
    for entry in environ:
        if b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        if key in {b"XIAN_YU_DATA_DIR", b"PRODUCTS_CONFIG_FILE", b"REPLY_RULES_FILE"}:
            values[key.decode()] = value.decode("utf-8", "replace")
    expected = os.path.realpath(tenant_dir(user_id, account_key))
    if not values.get("XIAN_YU_DATA_DIR") or os.path.realpath(values["XIAN_YU_DATA_DIR"]) != expected:
        return False
    for key, filename in (("PRODUCTS_CONFIG_FILE", "products_config.json"), ("REPLY_RULES_FILE", "reply_rules.json")):
        if values.get(key) and os.path.realpath(values[key]) != os.path.join(expected, filename):
            return False
    return True


def adopt(
    user_id: int,
    pid: int | None,
    automation_mode: str = "rules",
    account_key: str | None = DEFAULT_ACCOUNT_ID,
) -> tuple[bool, str]:
    """Attach this supervisor to a verified worker left by an API restart.

    Only deterministic ``rules`` workers are adoptable.  AI workers carry a
    loopback token held in the previous API's memory; they must be replaced
    with a fresh process/token instead of being silently left unauthorized.
    """
    mode = _normalise_mode(automation_mode)
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False, "pid_invalid"
    if not _pid_alive(pid):
        return False, "pid_dead"
    # Validate identity even for an AI worker.  A live, unrelated PID must be
    # reported as a mismatch; only a verified old worker may be replaced for a
    # fresh loopback token.
    if not _expected_worker_pid(user_id, pid, account_key):
        return False, "pid_mismatch"
    if mode != "rules":
        return False, "token_unavailable"
    start_time = _proc_start_time(pid)
    if not start_time:
        return False, "pid_mismatch"
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False, "pidfd_unavailable"
    try:
        pidfd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return False, "pid_dead"
    except OSError:
        return False, "pidfd_unavailable"
    with _lock:
        key = _proc_key(user_id, account_key)
        if is_running(user_id, account_key):
            if pidfd is not None:
                os.close(pidfd)
            return True, "already_running"
        # Re-run the complete identity check after pidfd acquisition. The
        # descriptor is PID-stable, while command/cwd/tenant verification binds
        # it to the exact worker that passed the first check.
        if (
            not _pid_alive(pid)
            or _proc_start_time(pid) != start_time
            or not _expected_worker_pid(user_id, pid, account_key)
        ):
            os.close(pidfd)
            return False, "pid_mismatch"
        _procs[key] = _AdoptedProcess(pid, start_time, pidfd)
        _modes[key] = mode
        _tokens.pop(key, None)
        _log_files.pop(key, None)
        _desired_running[key] = True
        _generations[key] = _generations.get(key, 0) + 1
        return True, "adopted"


def terminate_pid(
    user_id: int,
    pid: int | None,
    account_key: str | None = DEFAULT_ACCOUNT_ID,
) -> tuple[bool, str]:
    """Stop a verified orphan through a PID-stable Linux pidfd."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False, "pid_invalid"
    if not _pid_alive(pid):
        return True, "already_dead"
    if not _expected_worker_pid(user_id, pid, account_key):
        return False, "pid_mismatch"
    start_time = _proc_start_time(pid)
    if not start_time:
        return False, "pid_mismatch"
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False, "pidfd_unavailable"
    try:
        pidfd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return True, "already_dead"
    except OSError:
        return False, "pidfd_unavailable"
    try:
        # The descriptor is now bound to one task, but ensure it represents the
        # same worker identity that passed the command/cwd/tenant validation.
        if not _pid_alive(pid):
            return True, "already_dead"
        if _proc_start_time(pid) != start_time or not _expected_worker_pid(
            user_id, pid, account_key
        ):
            return False, "pid_mismatch"
        adopted = _AdoptedProcess(pid, start_time, pidfd)
        pidfd = None  # ownership transferred to the handle
        try:
            return _terminate_process(adopted)
        finally:
            adopted._close_pidfd()
    finally:
        if pidfd is not None:
            try:
                os.close(pidfd)
            except OSError:
                pass


def _discard_initialized_path(account_path: Path) -> bool:
    """Remove only a just-created directory containing initialization files."""
    try:
        info = os.lstat(account_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    allowed = set(INITIAL_ACCOUNT_FILES) | {"ai_knowledge"}
    try:
        for child in account_path.iterdir():
            if child.name not in allowed:
                return False
            child_info = os.lstat(child)
            if stat.S_ISLNK(child_info.st_mode):
                return False
            if child.name == "ai_knowledge":
                if not stat.S_ISDIR(child_info.st_mode) or any(child.iterdir()):
                    return False
            elif not stat.S_ISREG(child_info.st_mode):
                return False
        shutil.rmtree(account_path)
        return True
    except OSError:
        return False


def discard_initialized_dir(
    user_id: int,
    account_key: str | None = DEFAULT_ACCOUNT_ID,
) -> bool:
    """Compensate a failed explicit account initialization without broad deletion."""
    try:
        account_path = _storage().account_dir(user_id, account_key)
    except AccountStorageError:
        return False
    return _discard_initialized_path(account_path)


def ensure_dir(
    user_id: int,
    account_key: str | None = DEFAULT_ACCOUNT_ID,
    *,
    initialize: bool = False,
) -> str:
    storage = _storage()
    account_path = storage.account_dir(user_id, account_key)
    existed = account_path.exists()
    if initialize and existed:
        raise OSError("refusing to reseed existing account storage")
    try:
        path = str(storage.ensure_account_dir(user_id, account_key))
        if initialize:
            for name, content in INITIAL_ACCOUNT_FILES.items():
                storage.write_text(user_id, account_key, name, content)
        ai_knowledge_dir = os.path.join(path, "ai_knowledge")
        os.makedirs(ai_knowledge_dir, mode=0o700, exist_ok=True)
        info = os.lstat(ai_knowledge_dir)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError("unsafe AI knowledge directory")
        os.chmod(ai_knowledge_dir, 0o700)
        return path
    except (AccountStorageError, OSError) as error:
        if initialize and not existed:
            _discard_initialized_path(account_path)
        raise OSError("cannot initialize account storage" if initialize else "cannot prepare account storage") from error


def _limit():
    import resource

    limit = MEM_LIMIT_MB * 1024 * 1024

    def apply():
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return apply


def read_secret(user_id: int, name: str, account_key: str | None = DEFAULT_ACCOUNT_ID) -> str:
    try:
        return _storage().read_text(user_id, account_key, name).strip()
    except (OSError, AccountStorageError):
        return ""


def write_secret(
    user_id: int,
    name: str,
    content: str,
    account_key: str | None = DEFAULT_ACCOUNT_ID,
) -> None:
    try:
        _storage().write_text(user_id, account_key, name, content)
    except AccountStorageError as error:
        raise OSError("cannot write account storage") from error


def _safe_auth_timestamp(value) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _safe_auth_layer(payload, default_state: str) -> dict:
    layer = payload if isinstance(payload, dict) else {}
    state = str(layer.get("state") or default_state).strip().upper()
    if not state or len(state) > 40 or not all(char.isalnum() or char == "_" for char in state):
        state = default_state
    return {
        "state": state,
        "updated_at": _safe_auth_timestamp(layer.get("updated_at")),
    }


def auth_status(
    user_id: int,
    account_key: str | None = DEFAULT_ACCOUNT_ID,
) -> dict:
    """Read the worker's bounded authentication state without exposing secrets."""
    default = {
        "version": 2,
        "phase": "SESSION_VALID",
        "code": "ok",
        "failure_class": "NONE",
        "needs_human": False,
        "reauthorization_required": False,
        "updated_at": 0.0,
        "next_retry_at": 0.0,
        "failure_count": 0,
        "session": {"state": "UNKNOWN", "updated_at": 0.0},
        "mtop_token": {"state": "ABSENT", "updated_at": 0.0},
        "websocket": {"state": "DISCONNECTED", "updated_at": 0.0},
    }
    raw = read_secret(user_id, AUTH_STATUS_FILE, account_key)
    if not raw or len(raw) > 4096:
        return default
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return default
    if not isinstance(payload, dict):
        return default
    code = str(payload.get("code") or "ok")
    if code not in AUTH_STATUS_CODES:
        return default
    phase = str(payload.get("phase") or "").strip().upper()
    legacy_required = payload.get("reauthorization_required") is True and code in {
        "session_expired",
        "risk_control",
    }
    if phase not in AUTH_PHASES:
        if legacy_required:
            phase = "NEEDS_HUMAN"
        elif code == "ok":
            phase = "SESSION_VALID"
        else:
            phase = "DEGRADED"
    needs_human = phase == "NEEDS_HUMAN" or payload.get("needs_human") is True or legacy_required
    failure_class = str(payload.get("failure_class") or ("NEEDS_HUMAN" if needs_human else "NONE"))
    failure_class = failure_class.strip().upper()
    if not failure_class or len(failure_class) > 40 or not all(
        char.isalnum() or char == "_" for char in failure_class
    ):
        failure_class = "NEEDS_HUMAN" if needs_human else "NONE"
    try:
        failure_count = max(0, min(int(payload.get("failure_count") or 0), 1_000_000))
    except (TypeError, ValueError):
        failure_count = 0
    try:
        version = 2 if int(payload.get("version") or 1) >= 2 else 1
    except (TypeError, ValueError):
        version = 1
    return {
        "version": version,
        "phase": phase,
        "code": code,
        "failure_class": failure_class,
        "needs_human": needs_human,
        "reauthorization_required": needs_human,
        "updated_at": _safe_auth_timestamp(payload.get("updated_at")),
        "next_retry_at": _safe_auth_timestamp(payload.get("next_retry_at")),
        "failure_count": failure_count,
        "session": _safe_auth_layer(payload.get("session"), "UNKNOWN"),
        "mtop_token": _safe_auth_layer(payload.get("mtop_token"), "ABSENT"),
        "websocket": _safe_auth_layer(payload.get("websocket"), "DISCONNECTED"),
    }


def clear_auth_status(
    user_id: int,
    account_key: str | None = DEFAULT_ACCOUNT_ID,
) -> None:
    write_secret(
        user_id,
        AUTH_STATUS_FILE,
        json.dumps(
            {
                "version": 2,
                "phase": "SESSION_VALID",
                "code": "ok",
                "failure_class": "NONE",
                "needs_human": False,
                "reauthorization_required": False,
                "updated_at": time.time(),
                "next_retry_at": 0.0,
                "failure_count": 0,
                "session": {"state": "VALID", "updated_at": time.time()},
                "mtop_token": {"state": "ABSENT", "updated_at": 0.0},
                "websocket": {"state": "DISCONNECTED", "updated_at": 0.0},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        account_key,
    )


def _normalise_mode(value: str | None) -> str:
    return "rules_ai" if value == "rules_ai" else "rules"


def _automation_settings(user_id: int, account_key: str | None = DEFAULT_ACCOUNT_ID) -> dict:
    """Load both control documents strictly before a managed worker starts."""
    storage = _storage()
    try:
        rules_payload = json.loads(storage.read_text(user_id, account_key, "reply_rules.json"))
        settings_payload = json.loads(
            storage.read_text(user_id, account_key, "automation_settings.json")
        )
        rules_document(rules_payload)
        return normalise_settings(settings_payload)
    except (AccountStorageError, TypeError, ValueError, json.JSONDecodeError, AutomationValidationError) as error:
        raise OSError("automation control files are missing or invalid") from error


def _env_for(
    user_id: int,
    internal_token: str | None = None,
    automation_mode: str = "rules",
    account_key: str | None = DEFAULT_ACCOUNT_ID,
) -> dict[str, str]:
    mode = _normalise_mode(automation_mode)
    normalized_account_key = _account_key(account_key)
    account_path = ensure_dir(user_id, normalized_account_key)
    automation_settings = _automation_settings(user_id, normalized_account_key)
    env = dict(os.environ)
    for protected in (
        "SAAS_AI_MASTER_KEY",
        "SAAS_PLATFORM_AI_KEY",
        "SAAS_PLATFORM_AI_BASE_URL",
        "SAAS_PLATFORM_AI_MODEL",
        "SAAS_ADMIN_TOKEN",
        "API_KEY",
        "MODEL_BASE_URL",
        "MODEL_NAME",
    ):
        env.pop(protected, None)
    env.update(
        {
            "XIAN_YU_DATA_DIR": account_path,
            "XIAN_YU_ACCOUNT_KEY": normalized_account_key,
            "COOKIES_STR": read_secret(user_id, "cookies.txt", normalized_account_key),
            "PRODUCTS_CONFIG_FILE": os.path.join(account_path, "products_config.json"),
            "REPLY_RULES_FILE": os.path.join(account_path, "reply_rules.json"),
            "AUTOMATION_SETTINGS_FILE": os.path.join(account_path, "automation_settings.json"),
            "AUTOMATION_STRATEGY": automation_settings["strategy"],
            "AUTOMATION_MODE": mode,
            # Managed workers always read live delay values from the strict
            # account settings file; legacy environment-based typing delay is
            # disabled so a hot update to zero takes effect immediately.
            "SIMULATE_HUMAN_TYPING": "False",
            "MAX_REPLY_DELAY": "25",
            "LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
            "TOKEN_STARTUP_JITTER_SECONDS": str(
                _stable_account_jitter(
                    normalized_account_key,
                    TOKEN_STARTUP_JITTER_MAX_SECONDS,
                    "token-startup",
                )
            ),
            "TOKEN_REFRESH_JITTER_SECONDS": str(
                _stable_account_jitter(
                    normalized_account_key,
                    TOKEN_REFRESH_JITTER_MAX_SECONDS,
                    "token-refresh",
                )
            ),
        }
    )
    if mode == "rules_ai":
        if not internal_token:
            raise RuntimeError("rules_ai worker requires an internal AI token")
        env.update(
            {
                "API_KEY": internal_token,
                "MODEL_BASE_URL": PLATFORM_AI_BASE_URL,
                # The proxy overwrites this placeholder with the verified
                # account-scoped model and never exposes that real name here.
                "MODEL_NAME": "account-scoped",
                "AI_SETTINGS_FILE": os.path.join(account_path, "ai_settings.json"),
                "AI_KNOWLEDGE_DIR": os.path.join(account_path, "ai_knowledge"),
                "AI_PRODUCTS_SNAPSHOT_FILE": os.path.join(account_path, "shop_snapshot.json"),
            }
        )
    return env


def is_running(user_id: int, account_key: str | None = DEFAULT_ACCOUNT_ID) -> bool:
    with _lock:
        key = _proc_key(user_id, account_key)
        proc = _procs.get(key)
        return (proc is not None and proc.poll() is None) or key in _transitions


def process_id(user_id: int, account_key: str | None = DEFAULT_ACCOUNT_ID) -> int | None:
    """Return the live worker PID for durable runtime bookkeeping."""
    with _lock:
        key = _proc_key(user_id, account_key)
        proc = _procs.get(key)
        if proc is None:
            transition = _transitions.get(key)
            proc = transition.proc if transition is not None else None
        if proc is None or proc.poll() is not None:
            return None
        try:
            return int(proc.pid)
        except (AttributeError, TypeError, ValueError):
            return None


def _runtime_state(
    user_id: int,
    account_key: str | None = DEFAULT_ACCOUNT_ID,
) -> tuple[bool, str | None]:
    with _lock:
        key = _proc_key(user_id, account_key)
        proc = _procs.get(key)
        if proc is not None and proc.poll() is None:
            mode = _modes.get(key)
            return True, mode if mode in {"rules", "rules_ai"} else None
        transition = _transitions.get(key)
        if transition is not None:
            return True, transition.mode
        return False, None


def running_count() -> int:
    with _lock:
        return sum(1 for proc in _procs.values() if proc.poll() is None) + len(_transitions)


def _close_file(file) -> None:
    if file is not None:
        try:
            file.close()
        except OSError:
            pass


def _close_log(user_id: int, account_key: str | None = DEFAULT_ACCOUNT_ID) -> None:
    _close_file(_log_files.pop(_proc_key(user_id, account_key), None))


def _revoke_token_value(user_id: int, account_key: str, token: str | None) -> None:
    if account_key == DEFAULT_ACCOUNT_ID:
        revoke_token(user_id, token)
    else:
        revoke_token(user_id, token, account_key)


def _revoke(user_id: int, account_key: str | None = DEFAULT_ACCOUNT_ID) -> None:
    key = _proc_key(user_id, account_key)
    _revoke_token_value(user_id, key[1], _tokens.pop(key, None))


def _terminate_process(proc) -> tuple[bool, str]:
    if proc is None or proc.poll() is not None:
        return True, "already_dead"

    def identity_matches() -> bool:
        checker = getattr(proc, "identity_matches", None)
        return checker is None or bool(checker())

    try:
        if not identity_matches():
            return True, "already_dead"
        signal_sender = getattr(proc, "send_signal", None)
        if signal_sender is not None:
            if not signal_sender(signal.SIGTERM):
                return False, "stop_failed"
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            if not identity_matches():
                return True, "already_dead"
            if signal_sender is not None:
                if not signal_sender(signal.SIGKILL):
                    return False, "stop_failed"
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return False, "stop_timeout"
        if proc.poll() is None:
            return False, "stop_failed"
        return True, "stopped"
    except ProcessLookupError:
        return True, "already_dead"
    except OSError:
        return False, "stop_failed"


def _spawn_process(user_id: int, mode: str, key: tuple[int, str]):
    account_key = key[1]
    ensure_dir(user_id, account_key)
    if mode == "rules":
        _revoke(user_id, account_key)
    if mode == "rules_ai":
        token = issue_token(user_id) if account_key == DEFAULT_ACCOUNT_ID else issue_token(user_id, account_key)
    else:
        token = None
    log_path = os.path.join(tenant_dir(user_id, account_key), "bot.log")
    log_file = None
    try:
        log_file = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [BOT_PYTHON, BOT_MAIN],
            env=_env_for(user_id, token, mode, account_key),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(BOT_ROOT),
            preexec_fn=_limit(),
            start_new_session=True,
        )
    except Exception:
        _close_file(log_file)
        _revoke_token_value(user_id, account_key, token)
        raise
    return proc, token, log_file


def _register_process_locked(
    key: tuple[int, str], proc, token: str | None, log_file, mode: str, generation: int | None = None
) -> int:
    _procs[key] = proc
    if token:
        _tokens[key] = token
    else:
        _tokens.pop(key, None)
    _log_files[key] = log_file
    _modes[key] = mode
    _desired_running[key] = True
    next_generation = _generations.get(key, 0) + 1 if generation is None else int(generation)
    _generations[key] = next_generation
    return next_generation


def _spawn_locked(user_id: int, mode: str, key: tuple[int, str]) -> None:
    proc, token, log_file = _spawn_process(user_id, mode, key)
    _register_process_locked(key, proc, token, log_file, mode)


def start(
    user_id: int,
    automation_mode: str = "rules",
    account_key: str | None = DEFAULT_ACCOUNT_ID,
) -> tuple[bool, str]:
    mode = _normalise_mode(automation_mode)
    key = _proc_key(user_id, account_key)
    with _lock:
        if key in _transitions:
            return False, "transition_in_progress"
        current = _procs.get(key)
        current_running = current is not None and current.poll() is None
        if current_running and _modes.get(key) == mode:
            _desired_running[key] = True
            return True, "already_running"
    if current_running:
        # Process termination may wait up to the TERM/KILL deadlines, so never
        # invoke it while an outer acquisition of ``_lock`` is still held.
        stopped, reason = stop(user_id, account_key)
        if not stopped:
            return False, reason
    with _lock:
        if key in _transitions:
            return False, "transition_in_progress"
        if is_running(user_id, account_key):
            return False, "already_running"
        _desired_running[key] = True
        if running_count() >= MAX_BOTS:
            return False, "max_bots_reached"
        _spawn_locked(user_id, mode, key)
        return True, "started"


def stop(user_id: int, account_key: str | None = DEFAULT_ACCOUNT_ID) -> tuple[bool, str]:
    key = _proc_key(user_id, account_key)
    created_transition = False
    with _lock:
        _desired_running[key] = False
        _generations[key] = _generations.get(key, 0) + 1
        transition = _transitions.get(key)
        token = None
        if transition is None:
            proc = _procs.pop(key, None)
            mode = _modes.pop(key, None)
            token = _tokens.pop(key, None)
            log_file = _log_files.pop(key, None)
            if proc is not None and proc.poll() is None:
                transition = _ProcessTransition(proc, mode or "rules", log_file)
                transition.terminating = True
                _transitions[key] = transition
                created_transition = True
            else:
                _close_file(log_file)
        else:
            proc = None
    try:
        _revoke_token_value(user_id, key[1], token)
    except Exception:
        pass
    if transition is None:
        return False, "not_running"

    if created_transition:
        terminated, reason = _terminate_process(transition.proc)
        with _lock:
            transition.terminated = terminated
            transition.reason = reason
            transition.terminating = False
            if terminated:
                _close_file(transition.log_file)
                transition.log_file = None
                if _transitions.get(key) is transition:
                    _transitions.pop(key, None)
            transition.done.set()
        return terminated, reason

    if not transition.done.wait(timeout=25):
        return False, "stop_timeout"
    with _lock:
        if _transitions.get(key) is not transition:
            return True, "stopped"
        if transition.terminated:
            _transitions.pop(key, None)
            _close_file(transition.log_file)
            transition.log_file = None
            return True, transition.reason
        if transition.terminating:
            return False, "stop_timeout"
        transition.terminating = True
        transition.done.clear()

    terminated, reason = _terminate_process(transition.proc)
    with _lock:
        transition.terminated = terminated
        transition.reason = reason
        transition.terminating = False
        if terminated:
            _close_file(transition.log_file)
            transition.log_file = None
            if _transitions.get(key) is transition:
                _transitions.pop(key, None)
        transition.done.set()
    return terminated, reason


def logs(
    user_id: int,
    lines: int = 200,
    account_key: str | None = DEFAULT_ACCOUNT_ID,
) -> str:
    path = os.path.join(tenant_dir(user_id, account_key), "bot.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            return "".join(file.readlines()[-lines:])
    except OSError:
        return ""


def _status_view(sync_status: str, cookies_set: bool, snapshot: dict | None, product_count: int) -> dict:
    """Build a bounded UI state without exposing platform or secret data."""
    reauth_codes = {"cookie_expired", "cookie_invalid", "cookie_incomplete"}
    security_codes = {"risk_control", "risk_cooldown"}
    transient_codes = {
        "pending",
        "sync_error",
        "sync_busy",
        "sync_cooldown",
        "sync_timeout",
        "profile_missing",
        "platform_error",
        "network_error",
        "platform_busy",
    }

    if not cookies_set:
        connection_state = "unconfigured"
    elif sync_status == "account_restricted":
        # The login can remain valid while publishing or another platform
        # capability is restricted. Keep that distinction visible to the UI.
        connection_state = "connected"
    elif sync_status in reauth_codes:
        connection_state = "reauth_required"
    elif sync_status in security_codes:
        connection_state = "security_check"
    elif sync_status == "verified" and snapshot is not None:
        connection_state = "connected"
    elif sync_status in {"pending", "sync_busy"}:
        connection_state = "checking"
    elif snapshot is not None:
        # A previous verified snapshot is still useful while a refresh is
        # failing; label it degraded instead of asking the user to reconnect.
        connection_state = "degraded"
    elif sync_status in transient_codes:
        connection_state = "degraded"
    else:
        connection_state = "unconfigured"

    if snapshot is None:
        if sync_status in {"account_restricted", *security_codes}:
            catalog_state = "blocked"
        elif sync_status in {"pending", "sync_busy"}:
            catalog_state = "syncing"
        elif cookies_set:
            catalog_state = "unavailable"
        else:
            catalog_state = "not_started"
    elif sync_status == "verified":
        catalog_state = "ready" if product_count else "empty"
    elif sync_status in {"account_restricted", *security_codes}:
        catalog_state = "stale" if product_count else "blocked"
    else:
        catalog_state = "stale"

    publish_state = "blocked" if sync_status == "account_restricted" else "unavailable"
    can_view_products = snapshot is not None
    can_sync_products = bool(cookies_set and sync_status not in {"risk_cooldown", "account_restricted"})
    attention = []
    status = sync_status_payload(sync_status)
    if sync_status == "account_restricted":
        attention.append(
            {
                "code": "account_restricted",
                "severity": "error",
                "title": status["label"],
                "message": status["message"],
                "action": status["action"],
            }
        )
    elif sync_status in reauth_codes or sync_status in security_codes:
        attention.append(
            {
                "code": sync_status,
                "severity": "warning",
                "title": status["label"],
                "message": status["message"],
                "action": status["action"],
            }
        )

    return {
        "connection_state": connection_state,
        "catalog_state": catalog_state,
        "publish_state": publish_state,
        "capabilities": {
            "view_products": can_view_products,
            "sync_products": can_sync_products,
            # Product publishing is intentionally not inferred from a valid
            # Cookie; it needs a separate platform capability and preflight.
            "publish_products": False,
        },
        "attention": attention,
    }


def status(
    user_id: int,
    account_key: str | None = DEFAULT_ACCOUNT_ID,
) -> dict:
    running, automation_mode = _runtime_state(user_id, account_key)
    codes_total = 0
    codes_available = 0
    try:
        codes = json.loads(read_secret(user_id, "redeem_codes.json", account_key) or "[]")
        if isinstance(codes, list):
            valid_codes = [item for item in codes if isinstance(item, dict) and item.get("code")]
            codes_total = len(valid_codes)
            codes_available = sum(1 for item in valid_codes if not item.get("used"))
    except (TypeError, ValueError):
        pass

    cookie_value = read_secret(user_id, "cookies.txt", account_key)
    snapshot = load_verified_snapshot(user_id, _account_key(account_key))
    saved_state = load_sync_state(user_id, _account_key(account_key))
    current_account_ref = ""
    if cookie_value:
        try:
            _, current_cookies = parse_cookie_header(cookie_value)
            current_account_ref = account_ref(current_cookies)
        except ShopSyncError:
            pass

    # A persisted blocking result is tied to the Cookie/account that was
    # checked.  Ignore stale state if the file was replaced outside the API.
    state_matches = bool(
        saved_state
        and (
            not saved_state.get("account_ref")
            or not current_account_ref
            or saved_state.get("account_ref") == current_account_ref
        )
    )
    if not cookie_value:
        sync_status = "unconfigured"
    elif not current_account_ref:
        sync_status = "cookie_invalid"
    elif state_matches and saved_state["code"] in PERSISTED_SYNC_CODES:
        sync_status = saved_state["code"]
    elif snapshot is not None:
        sync_status = "verified"
    elif cookie_value:
        try:
            parse_cookie_header(cookie_value)
            sync_status = "pending"
        except ShopSyncError:
            sync_status = "cookie_invalid"
    else:
        sync_status = "unconfigured"

    worker_auth = auth_status(user_id, account_key)
    if not cookie_value and not worker_auth["needs_human"]:
        worker_auth = dict(worker_auth)
        worker_auth["phase"] = "UNCONFIGURED"
        worker_auth["session"] = {"state": "MISSING", "updated_at": worker_auth["updated_at"]}
    if worker_auth["reauthorization_required"]:
        sync_status = (
            "risk_control" if worker_auth["code"] == "risk_control" else "cookie_expired"
        )

    state_is_current = bool(state_matches and saved_state and sync_status == saved_state["code"])
    checked_at = saved_state.get("checked_at", "") if state_is_current else ""
    status_message = saved_state.get("message", "") if state_is_current else ""
    cookie_status = sync_status_payload(sync_status, status_message, checked_at)

    products = snapshot.get("products", []) if snapshot else []
    product_count = len([item for item in products if isinstance(item, dict)])
    shop_name = str(snapshot.get("nickname") or "")[:80] if snapshot else ""
    last_sync_at = str(snapshot.get("synced_at") or "") if snapshot else ""
    cookie_path = os.path.join(tenant_dir(user_id, account_key), "cookies.txt")
    rules_set = False
    deliveries_set = False
    try:
        with open(os.path.join(tenant_dir(user_id, account_key), "reply_rules.json"), encoding="utf-8") as file:
            payload = json.load(file)
        rules_set = bool(
            isinstance(payload, dict)
            and any(
                isinstance(rule, dict) and rule.get("enabled") is not False and rule.get("reply")
                for rule in payload.get("rules", [])
            )
        )
    except (OSError, TypeError, ValueError):
        pass
    try:
        with open(os.path.join(tenant_dir(user_id, account_key), "products_config.json"), encoding="utf-8") as file:
            payload = json.load(file)
        deliveries_set = bool(
            isinstance(payload, dict)
            and any(
                isinstance(item, dict)
                and item.get("delivery") == "material"
                and item.get("enabled", True) is not False
                and item.get("payload")
                for item in payload.get("types", [])
            )
        )
    except (OSError, TypeError, ValueError):
        pass
    try:
        cookie_updated_at = os.path.getmtime(cookie_path)
    except OSError:
        cookie_updated_at = 0

    status_view = _status_view(sync_status, bool(cookie_value), snapshot, product_count)
    try:
        automation_settings = _automation_settings(user_id, account_key)
        automation_config_valid = True
    except OSError:
        # Status queries must remain available while missing/corrupt control
        # files keep every automatic reply path fail-closed.
        automation_settings = {"strategy": "standard", "enabled": False}
        automation_config_valid = False
    return {
        "running": running,
        "connected": snapshot is not None and sync_status == "verified",
        "sync_status": sync_status,
        "cookie_status": cookie_status,
        "cookies_set": bool(cookie_value),
        "codes_set": codes_total > 0,
        "codes_total": codes_total,
        "codes_available": codes_available,
        "products_set": product_count > 0,
        "product_count": product_count,
        "running_total": running_count(),
        "shop_name": shop_name,
        "shop_nickname": shop_name,
        "last_sync_at": last_sync_at,
        "products_truncated": bool(snapshot.get("truncated")) if snapshot else False,
        "cookie_updated_at": cookie_updated_at,
        "rules_set": rules_set,
        "deliveries_set": deliveries_set,
        "automation_mode": automation_mode,
        "automation_strategy": automation_settings["strategy"],
        "automation_enabled": automation_settings["enabled"],
        "automation_config_valid": automation_config_valid,
        "auth_code": worker_auth["code"],
        "auth_phase": worker_auth["phase"],
        "auth_failure_class": worker_auth["failure_class"],
        "auth_layers": {
            "session": worker_auth["session"],
            "mtop_token": worker_auth["mtop_token"],
            "websocket": worker_auth["websocket"],
        },
        "auth_next_retry_at": worker_auth["next_retry_at"],
        "auth_failure_count": worker_auth["failure_count"],
        "needs_human": worker_auth["needs_human"],
        "reauthorization_required": worker_auth["reauthorization_required"],
        "auth_updated_at": worker_auth["updated_at"],
        **status_view,
    }


def _reaper():
    while True:
        time.sleep(30)
        with _lock:
            dead = [key for key, proc in _procs.items() if proc.poll() is not None]
            for uid, account_key in dead:
                key = (uid, account_key)
                _procs.pop(key, None)
                _modes.pop(key, None)
                _generations[key] = _generations.get(key, 0) + 1
                _revoke(uid, account_key)
                _close_log(uid, account_key)


def _transition_snapshot_inner(
    user_id: int,
    account_key: str,
    proc,
    generation: int,
    target_mode: str | None,
    persist_replacement=None,
) -> str | None:
    """Retire one exact generation and publish a replacement only after persistence."""
    key = (user_id, account_key)
    if target_mode is not None and not _automation_settings(user_id, account_key)["enabled"]:
        return None
    with _lock:
        if (
            _procs.get(key) is not proc
            or _generations.get(key, 0) != generation
            or proc.poll() is not None
            or not _desired_running.get(key, True)
        ):
            return None
        mode = _modes.get(key)
        if target_mode is not None and mode != "rules_ai":
            return None
        if target_mode is not None and not _automation_settings(user_id, account_key)["enabled"]:
            return None
        log_file = _log_files.pop(key, None)
        transition = _ProcessTransition(proc, mode or "rules", log_file)
        transition.terminating = True
        _transitions[key] = transition
        _procs.pop(key, None)
        _modes.pop(key, None)
        token = _tokens.pop(key, None)
        _generations[key] = generation + 1
        if target_mode is None:
            _desired_running[key] = False

    try:
        _revoke_token_value(user_id, account_key, token)
    except Exception:
        # Token cleanup must not prevent fail-closed process termination.
        pass
    terminated, reason = _terminate_process(proc)
    with _lock:
        transition.terminated = terminated
        transition.reason = reason
        transition.terminating = False
        if terminated:
            _close_file(transition.log_file)
            transition.log_file = None
    if not terminated:
        transition.done.set()
        return reason
    if target_mode is None:
        persisted = True
        if persist_replacement is not None:
            try:
                persisted = bool(
                    persist_replacement(
                        user_id,
                        account_key,
                        None,
                        None,
                        _generations.get(key, generation + 1),
                        int(proc.pid),
                    )
                )
            except Exception:
                persisted = False
        with _lock:
            if _transitions.get(key) is transition:
                _transitions.pop(key, None)
        transition.done.set()
        return "stopped" if persisted else "persist_failed"

    automation_enabled = _automation_settings(user_id, account_key)["enabled"]
    with _lock:
        if (
            _transitions.get(key) is not transition
            or not _desired_running.get(key, False)
            or not automation_enabled
            or not _automation_settings(user_id, account_key)["enabled"]
        ):
            _transitions.pop(key, None)
            transition.done.set()
            return "stopped"
        try:
            replacement, replacement_token, replacement_log = _spawn_process(user_id, target_mode, key)
        except Exception:
            transition.reason = "spawn_failed"
            transition.terminated = True
            _transitions.pop(key, None)
            transition.done.set()
            return "stopped"
        replacement_generation = _generations.get(key, 0) + 1
        transition.proc = replacement
        transition.mode = target_mode
        transition.log_file = replacement_log
        transition.terminated = None
        transition.reason = "persisting"

    persisted = True
    release_persistence_lease = None
    if persist_replacement is not None:
        try:
            persistence_result = persist_replacement(
                user_id,
                account_key,
                target_mode,
                int(replacement.pid),
                replacement_generation,
                int(proc.pid),
            )
            if isinstance(persistence_result, tuple) and len(persistence_result) == 2:
                persisted = bool(persistence_result[0])
                release_persistence_lease = persistence_result[1]
            else:
                persisted = bool(persistence_result)
        except Exception:
            persisted = False

    try:
        with _lock:
            publish = bool(
                persisted
                and _transitions.get(key) is transition
                and _desired_running.get(key, False)
                and _automation_settings(user_id, account_key)["enabled"]
            )
            if publish:
                _register_process_locked(
                    key,
                    replacement,
                    replacement_token,
                    replacement_log,
                    target_mode,
                    replacement_generation,
                )
                transition.log_file = None
                transition.terminated = True
                transition.reason = "persisted"
                _transitions.pop(key, None)
                transition.done.set()
                return "downgraded"
            transition.terminating = True

        try:
            _revoke_token_value(user_id, account_key, replacement_token)
        except Exception:
            pass
        replacement_stopped, replacement_reason = _terminate_process(replacement)
        with _lock:
            transition.terminated = replacement_stopped
            transition.reason = replacement_reason if not persisted else "cancelled"
            transition.terminating = False
            if replacement_stopped:
                _close_file(transition.log_file)
                transition.log_file = None
                if _transitions.get(key) is transition:
                    _transitions.pop(key, None)
            transition.done.set()
        return "stopped" if replacement_stopped else replacement_reason
    finally:
        if callable(release_persistence_lease):
            try:
                release_persistence_lease()
            except Exception:
                pass


def _transition_snapshot(
    user_id: int,
    account_key: str,
    proc,
    generation: int,
    target_mode: str | None,
    reservation=None,
    persist_transition=None,
    release_transition=None,
) -> str | None:
    def persist_with_reservation(uid, key, mode, pid, next_generation, expected_pid):
        if persist_transition is None:
            return True
        return persist_transition(
            reservation,
            uid,
            key,
            mode,
            pid,
            next_generation,
            expected_pid,
        )

    try:
        return _transition_snapshot_inner(
            user_id,
            account_key,
            proc,
            generation,
            target_mode,
            persist_with_reservation,
        )
    finally:
        if reservation is not None and release_transition is not None:
            try:
                release_transition(reservation)
            except Exception:
                pass


def _reconcile_access_modes(
    get_expires_at,
    now: float | None = None,
    reserve_transition=None,
    persist_transition=None,
    release_transition=None,
) -> dict[object, str]:
    """Keep the legacy watchdog hook without subscription-based mode changes."""
    return {}


def _expiry_watchdog(
    get_expires_at,
    reserve_transition=None,
    persist_transition=None,
    release_transition=None,
):
    while True:
        try:
            _reconcile_access_modes(
                get_expires_at,
                reserve_transition=reserve_transition,
                persist_transition=persist_transition,
                release_transition=release_transition,
            )
        except Exception:
            pass
        time.sleep(ACCESS_RECONCILE_SECONDS)


def shutdown_all():
    with _lock:
        keys = set(_procs) | set(_transitions)
    for uid, account_key in keys:
        try:
            stop(uid, account_key)
        except Exception:
            pass


threading.Thread(target=_reaper, daemon=True).start()


def start_watchdog(
    get_expires_at,
    reserve_transition=None,
    persist_transition=None,
    release_transition=None,
):
    thread = threading.Thread(
        target=_expiry_watchdog,
        args=(get_expires_at, reserve_transition, persist_transition, release_transition),
        daemon=True,
    )
    thread.start()
    return thread
