"""Database-backed account leases shared by HTTP edits and individual tool steps."""
from __future__ import annotations

import os
import re
import threading
import uuid
from contextlib import contextmanager


class AccountLeaseError(RuntimeError):
    def __init__(self, code="lease_busy"):
        self.code = code
        super().__init__({"lease_busy": "配置正在更新，请稍后重试", "lease_lost": "配置写入租约已失效", "storage_unavailable": "配置租约暂时不可用"}.get(code, "配置租约无效"))


class AccountLease:
    def __init__(self, db, key: str, owner: str, lease_seconds: float = 45):
        self.db, self.key, self.owner = db, key, owner
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.stop_event = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._renew, name="account-lease-renew", daemon=True)
        self.thread.start()

    def _renew(self):
        while not self.stop_event.wait(max(0.2, min(self.lease_seconds / 3, 10))):
            try:
                self.ensure_owned()
            except AccountLeaseError:
                return

    def ensure_owned(self):
        if self.stop_event.is_set() or self.lost.is_set():
            raise AccountLeaseError("lease_lost")
        try:
            owned = self.db.renew_control_lease(self.key, self.owner, lease_seconds=self.lease_seconds)
        except Exception as exc:
            self.lost.set()
            raise AccountLeaseError("lease_lost") from exc
        if not owned:
            self.lost.set()
            raise AccountLeaseError("lease_lost")
        return True

    def release(self):
        self.stop_event.set()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
        try:
            self.db.release_control_lease(self.key, self.owner)
        except Exception:
            # Ownership is time-bounded even when storage is temporarily unavailable.
            self.lost.set()

    def __enter__(self):
        self.ensure_owned()
        return self

    def __exit__(self, *_exc):
        self.release()


def acquire_account_lease(db, scope: str, user_id: int, shop_account_id: int, lease_seconds=45) -> AccountLease:
    if (not isinstance(scope, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", scope)
            or type(user_id) is not int or type(shop_account_id) is not int
            or user_id <= 0 or shop_account_id <= 0):
        raise AccountLeaseError("lease_lost")
    key = f"{scope}:{user_id}:{shop_account_id}"
    owner = f"config:{os.getpid()}:{uuid.uuid4().hex}"
    try:
        result = db.acquire_control_lease(key, owner, lease_seconds=lease_seconds, cooldown_seconds=0)
    except Exception as exc:
        raise AccountLeaseError("storage_unavailable") from exc
    if result != "acquired":
        raise AccountLeaseError("lease_busy")
    try:
        return AccountLease(db, key, owner, lease_seconds)
    except BaseException:
        db.release_control_lease(key, owner)
        raise


@contextmanager
def account_leases(db, scopes, user_id, shop_account_id, lease_seconds=45):
    """Acquire in caller's fixed business order; release even on partial acquisition."""
    leases = []
    try:
        for scope in scopes:
            leases.append(acquire_account_lease(db, scope, user_id, shop_account_id, lease_seconds))
        def ensure_owned():
            for lease in leases:
                lease.ensure_owned()
            return True
        yield ensure_owned
    finally:
        for lease in reversed(leases):
            lease.release()
