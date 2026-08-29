"""Loopback-only proxy for account-scoped OpenAI-compatible connections.

Workers receive only a lifecycle token.  The control plane resolves its exact
user/shop scope, decrypts that shop's verified credential, and overwrites the
model before forwarding.
"""

from __future__ import annotations

import secrets
import threading

from account_storage import DEFAULT_ACCOUNT_ID, normalize_account_key
from ai_customer_service import AIServiceError, service as ai_service


MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
UPSTREAM_TIMEOUT = 60


class PlatformAIError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502, code: str = "service_unavailable"):
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code or "service_unavailable")[:80]


class InternalTokenRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._tokens: dict[str, tuple[int, str]] = {}
        self._scope_tokens: dict[tuple[int, str], str] = {}

    def issue(self, user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> str:
        token = secrets.token_urlsafe(32)
        key = normalize_account_key(account_key)
        scope = (int(user_id), key)
        with self._lock:
            previous = self._scope_tokens.get(scope)
            if previous:
                self._tokens.pop(previous, None)
            self._tokens[token] = scope
            self._scope_tokens[scope] = token
        return token

    def revoke(
        self,
        user_id: int,
        token: str | None = None,
        account_key: str = DEFAULT_ACCOUNT_ID,
    ) -> None:
        scope = (int(user_id), normalize_account_key(account_key))
        with self._lock:
            current = self._scope_tokens.get(scope)
            target = token or current
            if target:
                self._tokens.pop(target, None)
            if current == target:
                self._scope_tokens.pop(scope, None)

    def identify(self, token: str) -> int | None:
        if not token:
            return None
        with self._lock:
            value = self._tokens.get(token)
            return value[0] if isinstance(value, tuple) else None

    def identify_scope(self, token: str) -> tuple[int, str] | None:
        if not token:
            return None
        with self._lock:
            value = self._tokens.get(token)
            return value if isinstance(value, tuple) else None


registry = InternalTokenRegistry()


def issue_token(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> str:
    return registry.issue(user_id, account_key)


def revoke_token(
    user_id: int,
    token: str | None = None,
    account_key: str = DEFAULT_ACCOUNT_ID,
) -> None:
    registry.revoke(user_id, token, account_key)


def identify_token(token: str) -> int | None:
    return registry.identify(token)


def identify_scope(token: str) -> tuple[int, str] | None:
    return registry.identify_scope(token)


def is_configured(
    user_id: int | None = None,
    shop_account_id: int | None = None,
    account_key: str = DEFAULT_ACCOUNT_ID,
) -> bool:
    if user_id is None or shop_account_id is None:
        return False
    return ai_service.is_configured(int(user_id), int(shop_account_id), account_key)


def forward(
    payload: dict,
    *,
    user_id: int,
    shop_account_id: int,
    account_key: str = DEFAULT_ACCOUNT_ID,
) -> tuple[int, bytes]:
    """Forward a validated non-streaming request using this shop's connection."""
    try:
        return ai_service.forward_payload(
            int(user_id), int(shop_account_id), account_key, payload
        )
    except AIServiceError as exc:
        raise PlatformAIError(str(exc), exc.status_code, exc.code) from exc
