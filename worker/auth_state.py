"""版本化、脱敏的 Worker 授权状态。"""

import copy
import json
import os
import stat
import time
import uuid


STATE_VERSION = 2
PHASES = frozenset({
    "UNCONFIGURED",
    "SESSION_VALID",
    "TOKEN_VALID",
    "WS_REGISTERED",
    "DEGRADED",
    "NEEDS_HUMAN",
})
SESSION_STATES = frozenset({"MISSING", "UNKNOWN", "VALID", "EXPIRED", "SECURITY_CHECK"})
TOKEN_STATES = frozenset({"ABSENT", "REFRESHING", "VALID", "DEGRADED"})
WEBSOCKET_STATES = frozenset({"DISCONNECTED", "CONNECTING", "REGISTERING", "REGISTERED", "DEGRADED"})
FAILURE_CLASSES = frozenset({
    "NONE",
    "TRANSIENT",
    "SESSION_RECOVERABLE",
    "NEEDS_HUMAN",
    "CONFIGURATION",
    "CAPABILITY_RESTRICTED",
})
FAILURE_CODES = frozenset({
    "ok",
    "risk_control",
    "session_expired",
    "platform_busy",
    "network_error",
    "response_invalid",
    "token_unavailable",
    "cookie_invalid",
    "account_restricted",
})
NEEDS_HUMAN_CODES = frozenset({"risk_control", "session_expired", "cookie_invalid"})


def _timestamp(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0.0, min(number, 10_000_000_000.0))


def default_auth_state(now=None):
    current = time.time() if now is None else _timestamp(now)
    return {
        "version": STATE_VERSION,
        "phase": "UNCONFIGURED",
        "session": {"state": "MISSING", "updated_at": current},
        "mtop_token": {"state": "ABSENT", "updated_at": current},
        "websocket": {"state": "DISCONNECTED", "updated_at": current},
        "code": "ok",
        "failure_class": "NONE",
        "needs_human": False,
        "reauthorization_required": False,
        "updated_at": current,
        "next_retry_at": 0.0,
        "failure_count": 0,
    }


def _legacy_state(payload):
    state = default_auth_state(payload.get("updated_at", 0.0))
    code = payload.get("code") if payload.get("code") in FAILURE_CODES else "ok"
    required = payload.get("reauthorization_required") is True and code in NEEDS_HUMAN_CODES
    if required:
        state["phase"] = "NEEDS_HUMAN"
        state["session"]["state"] = (
            "SECURITY_CHECK" if code == "risk_control" else "EXPIRED"
        )
        state["mtop_token"]["state"] = "DEGRADED"
        state["code"] = code
        state["failure_class"] = "NEEDS_HUMAN"
        state["failure_count"] = 1
        state["needs_human"] = True
        state["reauthorization_required"] = True
    elif code != "ok":
        state["phase"] = "DEGRADED"
        state["session"]["state"] = "UNKNOWN"
        state["mtop_token"]["state"] = "DEGRADED"
        state["code"] = code
        state["failure_class"] = "TRANSIENT"
        state["failure_count"] = 1
    return state


def normalize_auth_state(payload):
    if not isinstance(payload, dict):
        return default_auth_state(0.0)
    if payload.get("version") != STATE_VERSION:
        return _legacy_state(payload)

    state = default_auth_state(payload.get("updated_at", 0.0))
    phase = payload.get("phase")
    if phase not in PHASES:
        return default_auth_state(0.0)
    state["phase"] = phase

    for key, allowed in (
        ("session", SESSION_STATES),
        ("mtop_token", TOKEN_STATES),
        ("websocket", WEBSOCKET_STATES),
    ):
        raw = payload.get(key)
        if not isinstance(raw, dict) or raw.get("state") not in allowed:
            return default_auth_state(0.0)
        state[key] = {
            "state": raw["state"],
            "updated_at": _timestamp(raw.get("updated_at")),
        }

    # 兼容本分支早期写出的嵌套 failure v2，规范化后只输出扁平结构。
    failure = payload.get("failure")
    if isinstance(failure, dict):
        code = failure.get("code")
        failure_class = failure.get("class")
        raw_count = failure.get("count")
        raw_next_retry_at = failure.get("next_retry_at")
    else:
        code = payload.get("code")
        failure_class = payload.get("failure_class")
        raw_count = payload.get("failure_count")
        raw_next_retry_at = payload.get("next_retry_at")
    if code not in FAILURE_CODES or failure_class not in FAILURE_CLASSES:
        return default_auth_state(0.0)
    try:
        count = int(raw_count or 0)
    except (TypeError, ValueError, OverflowError):
        count = 0
    state["code"] = code
    state["failure_class"] = failure_class
    state["failure_count"] = max(0, min(count, 1_000_000))
    state["next_retry_at"] = _timestamp(raw_next_retry_at)
    needs_human = payload.get("needs_human") is True or phase == "NEEDS_HUMAN"
    if needs_human and code not in NEEDS_HUMAN_CODES:
        return default_auth_state(0.0)
    state["needs_human"] = needs_human
    state["code"] = code
    state["reauthorization_required"] = needs_human
    state["updated_at"] = _timestamp(payload.get("updated_at"))
    return state


class AuthStateStore:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.directory = os.path.dirname(self.path)

    def read(self):
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = None
        try:
            descriptor = os.open(self.path, flags)
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > 16 * 1024:
                return default_auth_state(0.0)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = None
                payload = json.load(handle)
        except (OSError, TypeError, ValueError):
            return default_auth_state(0.0)
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return normalize_auth_state(payload)

    def write(self, state):
        normalized = normalize_auth_state(state)
        payload = json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        temporary_path = os.path.join(
            self.directory,
            f".auth_status.{os.getpid()}.{uuid.uuid4().hex}.tmp",
        )
        descriptor = None
        try:
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
            directory_descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
        return normalized

    def update(
        self,
        *,
        phase=None,
        session=None,
        mtop_token=None,
        websocket=None,
        failure_code=None,
        failure_class=None,
        failure_count=None,
        next_retry_at=None,
        needs_human=None,
        now=None,
    ):
        state = copy.deepcopy(self.read())
        current = time.time() if now is None else _timestamp(now)
        if phase is not None:
            if phase not in PHASES:
                raise ValueError("invalid auth phase")
            state["phase"] = phase
        for key, value, allowed in (
            ("session", session, SESSION_STATES),
            ("mtop_token", mtop_token, TOKEN_STATES),
            ("websocket", websocket, WEBSOCKET_STATES),
        ):
            if value is not None:
                if value not in allowed:
                    raise ValueError(f"invalid {key} state")
                state[key] = {"state": value, "updated_at": current}
        if failure_code is not None:
            if failure_code not in FAILURE_CODES:
                raise ValueError("invalid failure code")
            state["code"] = failure_code
        if failure_class is not None:
            if failure_class not in FAILURE_CLASSES:
                raise ValueError("invalid failure class")
            state["failure_class"] = failure_class
        if failure_count is not None:
            state["failure_count"] = max(0, int(failure_count))
        if next_retry_at is not None:
            state["next_retry_at"] = _timestamp(next_retry_at)
        if needs_human is not None:
            state["needs_human"] = bool(needs_human)
        if state["phase"] == "NEEDS_HUMAN":
            state["needs_human"] = True
        state["reauthorization_required"] = state["needs_human"]
        state["updated_at"] = current
        return self.write(state)
