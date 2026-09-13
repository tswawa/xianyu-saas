"""Low-frequency, shared release discovery; never installation or download.

Importing this portable module performs no I/O and starts no threads. Call
``start()`` explicitly for background checks, or ``check()`` for a synchronous
manual/due check. The injected inspector is called as ``inspect_release(version)``
and returns the existing public-release payload. It must bound its own I/O.

``check()`` and the read-only ``snapshot()`` return that legacy check shape plus
an ``update_probe`` object. Contending callers immediately receive the shared
snapshot rather than issuing another request or waiting for someone else's I/O.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime


DEFAULT_INTERVAL_SECONDS = 6 * 3600
MANUAL_COOLDOWN_SECONDS = 60
DEFAULT_LEASE_SECONDS = 120
BACKOFF_BASE_SECONDS = 60
MAX_BACKOFF_SECONDS = 6 * 3600
MAX_RETRY_AFTER_SECONDS = 24 * 3600
_VALID_CHANNELS = frozenset({"release", "stable", "beta"})
_SUCCESS_STATUSES = frozenset({"available", "current", "no_release", "incomplete"})
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
logger = logging.getLogger(__name__)


class ProbeError(RuntimeError):
    """A stable public error code, without exception text or source credentials."""

    def __init__(self, code="update_probe_failed", *, retry_after=None):
        self.code = _safe_error_code(code)
        self.retry_after = retry_after
        super().__init__(self.code)


def _safe_error_code(value):
    value = str(value or "")
    return value if _ERROR_CODE.fullmatch(value) else "update_probe_failed"


def _positive_seconds(value, name, *, minimum=1.0, maximum=None):
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"invalid {name}")
    return min(value, maximum) if maximum is not None else value


def _version_key(value):
    """Match the existing SemVer precedence without importing Unix-only staging."""
    value = str(value or "").strip()
    match = _SEMVER.fullmatch(value) if len(value) <= 200 else None
    if match is None:
        raise ProbeError("release_version_invalid")
    prerelease = match.group(4).split(".") if match.group(4) else []
    if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease):
        raise ProbeError("release_version_invalid")
    return (
        int(match.group(1)), int(match.group(2)), int(match.group(3)),
        not prerelease,
        tuple((0, int(part)) if part.isdigit() else (1, part) for part in prerelease),
    )


def _retry_after_seconds(value, now):
    """Accept delta seconds or an HTTP-date, ignoring invalid/unbounded values."""
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            deadline = parsedate_to_datetime(str(value))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            seconds = deadline.timestamp() - now
        except (TypeError, ValueError, OverflowError, OSError):
            return 0.0
    if not math.isfinite(seconds):
        return 0.0
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _successful_payload(payload, version, channel):
    if not isinstance(payload, dict):
        raise ProbeError("update_source_invalid")
    status = payload.get("status")
    if status == "error":
        raise ProbeError(payload.get("error_code"), retry_after=payload.get("retry_after"))
    if status not in _SUCCESS_STATUSES:
        raise ProbeError("update_source_invalid")
    target = str(payload.get("version") or "").strip()
    if status == "no_release":
        if target:
            raise ProbeError("update_source_invalid")
        available = False
    else:
        available = _version_key(target) > _version_key(version)
        status = "incomplete" if available and status == "incomplete" else (
            "available" if available else "current"
        )
    result = {
        "channel": channel,
        "current_version": version,
        "status": status,
        "available": available,
        "version": target,
        "published_at": str(payload.get("published_at") or "")[:80],
        "release_notes": str(payload.get("release_notes") or "")[:16000],
        "error_code": _safe_error_code(payload.get("error_code")) if status == "incomplete" else "",
    }
    if isinstance(payload.get("installer_assets"), dict):
        result["installer_assets"] = {
            kind: payload["installer_assets"].get(kind) is True
            for kind in ("docker", "systemd")
        }
    return result


@dataclass
class _Attempt:
    owner: str
    generation: int
    version: str
    failures: int
    cancelled: threading.Event = field(default_factory=threading.Event)
    renew_stop: threading.Event = field(default_factory=threading.Event)
    renew_thread: threading.Thread | None = None


class ProbeCoordinator:
    """Coordinate one shared channel with persistent scheduling and SQLite fencing.

    ``current_version`` may be a string or a zero-argument callable. ``is_paused``
    is an optional zero-argument maintenance predicate (exceptions fail closed).
    ``clock`` returns Unix seconds. The optional ``wait(event, timeout)`` hook
    has Event.wait semantics and permits deterministic scheduler/renewal tests.
    Force skips only the normal interval, never cooldown, backoff or a live lease.
    """

    def __init__(
        self, db, inspect_release, current_version, channel="release",
        interval_seconds=DEFAULT_INTERVAL_SECONDS, clock=time.time, *,
        is_paused=None, lease_seconds=DEFAULT_LEASE_SECONDS,
        poll_seconds=30, wait=None,
    ):
        if channel not in _VALID_CHANNELS:
            raise ValueError("invalid update channel")
        if not callable(inspect_release) or not callable(clock):
            raise TypeError("probe inspector and clock must be callable")
        if is_paused is not None and not callable(is_paused):
            raise TypeError("probe pause predicate must be callable")
        self.db = db
        self.inspect_release = inspect_release
        self.current_version = current_version
        self.channel = channel
        self.interval_seconds = _positive_seconds(interval_seconds, "probe interval")
        self.lease_seconds = _positive_seconds(lease_seconds, "probe lease", maximum=3600.0)
        self.poll_seconds = _positive_seconds(poll_seconds, "probe poll interval", minimum=0.01)
        self.clock = clock
        self.is_paused = is_paused or (lambda: False)
        self._wait = wait or (lambda event, timeout: event.wait(timeout))
        self._lock = threading.RLock()
        self._active = None
        self._thread = None
        self._stop_event = threading.Event()
        self._closing = False

    def _version(self):
        value = self.current_version() if callable(self.current_version) else self.current_version
        value = str(value or "").strip()
        _version_key(value)
        return value

    def _now(self):
        return _positive_seconds(self.clock(), "probe clock", minimum=0.0)

    def _paused(self):
        try:
            return bool(self.is_paused())
        except Exception:
            # An unreadable maintenance source must not accidentally resume I/O.
            return True

    def snapshot(self):
        """Return only local cache/metadata, reprojected against the running version."""
        now, version = self._now(), self._version()
        state = self.db.get_platform_update_probe(
            self.channel, interval_seconds=self.interval_seconds,
        )
        cached = state["result"]
        try:
            result = _successful_payload(cached, version, self.channel)
            result["checked_at"] = cached["checked_at"]
            has_success = state["last_success_at"] is not None
        except (ProbeError, KeyError):
            result = {
                "channel": self.channel, "current_version": version,
                "status": "unchecked", "available": False, "checked_at": None,
                "version": "", "published_at": "", "release_notes": "", "error_code": "",
            }
            has_success = False
        checking = bool(state["lease_owner"] and state["lease_until"] > now)
        paused = self._paused()
        failures = int(state["consecutive_failures"])
        cache_stale = bool(
            not has_success or cached.get("current_version") != version
            or float(result["checked_at"]) + self.interval_seconds <= now
        )
        next_check = float(state["next_check_at"])
        if not has_success or cached.get("current_version") != version:
            next_check = min(next_check, now)
        blocked_until = max(
            float(state["manual_cooldown_until"]), float(state["retry_not_before"]),
            float(state["lease_until"]) if checking else 0.0,
        )
        result["update_probe"] = {
            "state": "paused" if paused else "checking" if checking else (
                "error" if failures else "idle" if has_success else "unchecked"
            ),
            "checking": checking,
            "paused": paused,
            "has_success": has_success,
            "cache_stale": cache_stale,
            "last_attempt_at": state["last_attempt_at"],
            "last_success_at": state["last_success_at"],
            "last_failure_at": state["last_failure_at"],
            "next_check_at": max(next_check, blocked_until),
            "interval_seconds": self.interval_seconds,
            "consecutive_failures": failures,
            "error_code": state["last_error_code"],
            "retry_not_before": state["retry_not_before"],
            "manual_cooldown_until": state["manual_cooldown_until"],
            "retry_after": max(0, math.ceil(blocked_until - now)),
        }
        return result

    def _abandon(self, attempt):
        return self.db.abandon_platform_update_probe(
            self.channel, attempt.owner, attempt.generation, now=self._now(),
        )

    def _renew(self, attempt):
        while not self._wait(attempt.renew_stop, self.lease_seconds / 3.0):
            if attempt.cancelled.is_set():
                return
            try:
                renewed = self.db.renew_platform_update_probe(
                    self.channel, attempt.owner, attempt.generation,
                    lease_seconds=self.lease_seconds, now=self._now(),
                )
            except Exception:
                renewed = False
            if not renewed:
                # Completion also checks the database fence; this flag additionally
                # prevents a write if renewal became uncertain before expiry.
                attempt.cancelled.set()
                return

    def check(self, force=False, raise_errors=False):
        """Execute at most one due check; contention/cooldown return shared state."""
        with self._lock:
            if self._closing or self._active is not None or self._paused():
                return self.snapshot()
            version, now = self._version(), self._now()
            owner = secrets.token_hex(16)
            claim = self.db.acquire_platform_update_probe(
                self.channel, owner, version, force=bool(force), now=now,
                interval_seconds=self.interval_seconds, lease_seconds=self.lease_seconds,
                manual_cooldown_seconds=MANUAL_COOLDOWN_SECONDS,
            )
            if claim["outcome"] != "acquired":
                return self.snapshot()
            attempt = _Attempt(owner, claim["generation"], version, claim["consecutive_failures"])
            self._active = attempt
            attempt.renew_thread = threading.Thread(
                target=self._renew, args=(attempt,), name="update-probe-renew", daemon=True,
            )
            try:
                attempt.renew_thread.start()
            except BaseException:
                self._active = None
                self._abandon(attempt)
                raise
        failure = None
        committed = False
        try:
            if not attempt.cancelled.is_set() and not self._paused():
                try:
                    payload = self.inspect_release(version)
                    if isinstance(payload, dict) and payload.get("current_version", version) != version:
                        raise ProbeError("update_probe_result_mismatch")
                    payload = _successful_payload(payload, version, self.channel)
                except Exception as exc:
                    failure = exc
                    payload = None
                with self._lock:
                    obsolete = (
                        attempt.cancelled.is_set() or self._closing
                        or self._version() != version or self._paused()
                    )
                    if not obsolete:
                        now = self._now()
                        delay = self.interval_seconds
                        if failure is not None:
                            backoff = min(
                                BACKOFF_BASE_SECONDS * (2 ** min(attempt.failures, 16)),
                                MAX_BACKOFF_SECONDS,
                            )
                            delay = max(backoff, _retry_after_seconds(getattr(failure, "retry_after", None), now))
                        committed = self.db.finish_platform_update_probe(
                            self.channel, owner, attempt.generation, current_version=version,
                            payload=payload, error_code=_safe_error_code(getattr(failure, "code", None)),
                            next_check_at=now + delay,
                            retry_not_before=now + delay if failure is not None else 0.0, now=now,
                        )
        finally:
            attempt.renew_stop.set()
            # Always fence unfinished work, including changed versions and stop().
            # A newer generation or a successfully completed claim is untouched.
            try:
                self._abandon(attempt)
            finally:
                attempt.renew_thread.join(timeout=1.0)
                with self._lock:
                    if self._active is attempt:
                        self._active = None
        if committed and failure is not None and raise_errors:
            raise failure
        return self.snapshot()

    def _run(self, stop_event):
        while not stop_event.is_set():
            delay = self.poll_seconds
            try:
                self.check()
                if stop_event.is_set():
                    return
                probe = self.snapshot()["update_probe"]
                if not probe["paused"]:
                    delay = min(delay, max(0.05, probe["next_check_at"] - self._now()))
            except Exception:
                # Do not log arbitrary fetcher exceptions (URLs/tokens may occur).
                logger.warning("Background update probe could not complete its local coordination")
            if self._wait(stop_event, delay):
                return

    def start(self):
        """Start an asynchronous initial/due check without delaying service readiness."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if self._closing:
                    raise RuntimeError("previous update probe is still stopping")
                return
            self._closing = False
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(self._stop_event,), name="update-probe", daemon=True,
            )
            self._thread.start()

    def stop(self):
        """Stop scheduling and fence late I/O; a blocked inspector cannot delay shutdown indefinitely."""
        with self._lock:
            self._closing = True
            self._stop_event.set()
            attempt, thread = self._active, self._thread
            if attempt is not None:
                attempt.cancelled.set()
                attempt.renew_stop.set()
                self._abandon(attempt)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
