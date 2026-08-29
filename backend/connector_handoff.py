"""Short-lived, in-memory handoff credentials for the browser connector.

The handoff value is deliberately separate from the normal HttpOnly session
token.  It is scoped to one tenant, expires quickly, and is never written to
disk or returned after the initial issue call.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass

from account_storage import DEFAULT_ACCOUNT_ID, normalize_account_key


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


HANDOFF_TTL_SECONDS = _bounded_int(
    "SAAS_CONNECTOR_HANDOFF_TTL_SECONDS", 600, 60, 900
)
MAX_ATTEMPTS = _bounded_int("SAAS_CONNECTOR_HANDOFF_MAX_ATTEMPTS", 12, 1, 32)
MAX_ACTIVE_PER_USER = 3


@dataclass
class _Handoff:
    user_id: int
    expires_at: float
    account_key: str = DEFAULT_ACCOUNT_ID
    attempts: int = 0
    in_flight: bool = False


class HandoffStore:
    """Thread-safe in-memory token registry; values never enter tenant files."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._items: dict[str, _Handoff] = {}

    def _prune(self, now: float) -> None:
        for token, item in list(self._items.items()):
            if item.expires_at <= now:
                self._items.pop(token, None)

    def issue(
        self,
        user_id: int,
        account_key: str = DEFAULT_ACCOUNT_ID,
    ) -> tuple[str, float]:
        now = float(self._clock())
        try:
            key = normalize_account_key(account_key)
        except ValueError:
            raise ValueError("invalid account key") from None
        with self._lock:
            self._prune(now)
            user_tokens = [
                token
                for token, item in self._items.items()
                if item.user_id == int(user_id) and item.account_key == key
            ]
            remove_count = max(0, len(user_tokens) - MAX_ACTIVE_PER_USER + 1)
            for token in user_tokens[:remove_count]:
                self._items.pop(token, None)
            token = secrets.token_urlsafe(32)
            expires_at = now + HANDOFF_TTL_SECONDS
            self._items[token] = _Handoff(int(user_id), expires_at, key)
            return token, expires_at

    def begin_with_scope(self, token: str) -> tuple[str, int | None, str | None]:
        """Claim one attempt and return ``(code, user_id, account_key)``.

        The token remains valid after a platform risk response so the same
        official login flow can retry when the user completes verification.
        """

        now = float(self._clock())
        with self._lock:
            item = self._items.get(token)
            if item is not None and item.expires_at <= now:
                self._items.pop(token, None)
                self._prune(now)
                return "handoff_expired", None, None
            self._prune(now)
            item = self._items.get(token)
            if item is None:
                return "handoff_invalid", None, None
            if item.in_flight:
                return "handoff_busy", None, None
            if item.attempts >= MAX_ATTEMPTS:
                self._items.pop(token, None)
                return "handoff_attempts_exceeded", None, None
            item.attempts += 1
            item.in_flight = True
            return "ok", item.user_id, item.account_key

    def begin(self, token: str) -> tuple[str, int | None]:
        """Compatibility API that returns only the user for legacy callers."""
        code, user_id, _account_key = self.begin_with_scope(token)
        return code, user_id

    def finish(self, token: str, success: bool = False) -> None:
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return
            if success:
                self._items.pop(token, None)
            else:
                item.in_flight = False

    def revoke(self, token: str) -> None:
        with self._lock:
            self._items.pop(token, None)

    def clear_user(self, user_id: int) -> None:
        """Revoke every pending bridge token owned by one user."""
        uid = int(user_id)
        with self._lock:
            for token, item in list(self._items.items()):
                if item.user_id == uid:
                    self._items.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


handoffs = HandoffStore()
