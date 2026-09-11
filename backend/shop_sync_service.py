"""Shared, side-effect-limited shop synchronization service.

The API and the durable job consumer both need the same account-scoped
snapshot update rules.  Keeping this module independent from ``app`` is
important: importing the consumer must not start FastAPI watchdogs or worker
recovery threads.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable

from shop_sync import (
    PERSISTED_SYNC_CODES,
    SYNC_COOLDOWN_SECONDS,
    SYNC_MAX_SECONDS,
    REQUEST_INTERVAL,
    ShopSyncError,
    _circuit_until,
    check_sync_circuit,
    account_ref,
    parse_cookie_header,
    reserve_sync,
    save_snapshot,
    save_sync_state,
    sync_shop,
)


class ShopSyncPersistenceError(RuntimeError):
    """A bounded error raised after platform verification but failed storage."""

    code = "sync_persistence_error"

    def __init__(self, message="店铺验证成功，但保存结果失败，请稍后重试"):
        super().__init__(message)


def control_lease_error(db, key, result) -> ShopSyncError:
    row = db.get_control_lease(key)
    deadline = max(float(row["lease_until"] or 0), float(row["cooldown_until"] or 0)) if row else 0
    code = "sync_cooldown" if result == "cooldown" else "sync_busy"
    return ShopSyncError(code, "店铺检测暂时等待，请稍后再试", max(1, deadline - time.time()))


class ShopConnectionCoordinator:
    """Persistent per-shop QR ownership; no lock/transaction spans platform I/O."""

    def __init__(self, db):
        self.db = db

    def acquire(self, user_id, account_key, owner, seconds):
        account = self.db.get_shop_account(user_id, account_key=account_key)
        if account is None or not account["enabled"]:
            raise ShopSyncPersistenceError("店铺账号已停用")
        lease = {
            "key": f"shop-connect:{int(user_id)}:{int(account['id'])}",
            "sync_key": f"shop-sync:{int(user_id)}:{int(account['id'])}",
            "owner": owner, "user_id": int(user_id), "account_id": int(account["id"]),
            "generation": int(account["generation"] or 0),
        }
        result = self.db.reserve_shop_connection(
            lease["key"], owner, lease["sync_key"], lease_seconds=seconds,
            risk_until=_circuit_until(),
        )
        if result["code"] != "acquired":
            raise ShopSyncError(result["code"], "店铺连接暂时等待，请稍后再试", result["retry_after"])
        return lease

    def renew(self, lease, seconds):
        if not self.db.account_is_current(lease["user_id"], lease["account_id"], lease["generation"]):
            raise ShopSyncPersistenceError("店铺账号已变更，请重新扫码")
        if not self.db.renew_control_lease(lease["key"], lease["owner"], seconds):
            raise ShopSyncPersistenceError("扫码连接租约已失效，请重新扫码")

    def release(self, lease):
        self.db.release_control_lease(lease["key"], lease["owner"])

    def before_request(self, lease, seconds):
        check_sync_circuit()
        # Only this HTTP request holds global egress, never the user's scan.
        result = self.db.acquire_control_lease(
            "shop-sync:egress", lease["owner"], lease_seconds=seconds,
            cooldown_seconds=REQUEST_INTERVAL,
        )
        if result == "cooldown":
            row = self.db.get_control_lease("shop-sync:egress")
            remaining = float(row["cooldown_until"] or 0) - time.time() if row else 0
            if 0 < remaining <= REQUEST_INTERVAL:
                # The bounded rate interval is not a retry loop; no lock or
                # reservation is held while waiting between QR HTTP calls.
                time.sleep(remaining)
                result = self.db.acquire_control_lease(
                    "shop-sync:egress", lease["owner"], lease_seconds=seconds,
                    cooldown_seconds=REQUEST_INTERVAL,
                )
        if result != "acquired":
            raise control_lease_error(self.db, "shop-sync:egress", result)
        try:
            check_sync_circuit()
        except Exception:
            self.after_request(lease)
            raise

    def after_request(self, lease):
        self.db.release_control_lease("shop-sync:egress", lease["owner"])


def run_shop_sync_inner(*, connection_lease=None, **kwargs) -> dict:
    """Keep per-shop ownership through validation AND durable persistence."""
    db, user_id = kwargs["db"], kwargs["user_id"]
    account = kwargs.get("account") or db.ensure_default_shop_account(user_id)
    if account is None:
        raise ShopSyncPersistenceError()
    kwargs["account"] = account
    coordinator = ShopConnectionCoordinator(db)
    owned_here = connection_lease is None
    lease = connection_lease or coordinator.acquire(
        user_id, str(account["account_key"]),
        f"sync:{os.getpid()}:{threading.get_ident()}:{time.time_ns()}", SYNC_MAX_SECONDS + 120,
    )
    try:
        if (lease["user_id"] != int(user_id) or lease["account_id"] != int(account["id"])
                or lease["generation"] != int(account["generation"] or 0)):
            raise ShopSyncPersistenceError("店铺账号已变更，请重新扫码")
        coordinator.renew(lease, SYNC_MAX_SECONDS + 120)
        return _run_shop_sync_owned(
            **kwargs, ensure_connection=lambda: coordinator.renew(lease, SYNC_MAX_SECONDS + 120),
        )
    finally:
        if owned_here:
            coordinator.release(lease)


def _run_shop_sync_owned(
    *,
    db,
    read_secret: Callable,
    write_secret: Callable,
    load_verified_snapshot: Callable,
    sync_account_state: Callable,
    user_id: int,
    cookie_header: str,
    replace_cookie: bool,
    account,
    sync_func: Callable = sync_shop,
    reserve_sync_func: Callable = reserve_sync,
    lease_owner_prefix: str = "sync",
    before_replace_persist: Callable | None = None,
    ensure_connection: Callable = lambda: None,
) -> dict:
    """Verify a Cookie and atomically persist the account snapshot.

    ``cookie_header`` is supplied directly only by the API's synchronous
    login/replacement path.  The consumer passes a Cookie read from the
    account's private directory.  Job payloads never reach this function.
    All platform calls and writes remain account-scoped and the durable
    control lease protects API/consumer overlap across processes.
    """
    if account is None:
        account = db.ensure_default_shop_account(user_id)
    if account is None:
        raise ShopSyncPersistenceError("店铺账号状态不可用，请稍后重试")

    account_key = str(account["account_key"])
    account_id = int(account["id"])
    try:
        account_generation = int(account["generation"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        # ``DB`` migrates old installations before returning rows.  Keep the
        # fallback for narrow test doubles and legacy callers.
        account_generation = 0

    def ensure_account_current() -> None:
        ensure_connection()
        try:
            current = db.get_shop_account(user_id, account_id=account_id)
            current_generation = int(current["generation"] or 0) if current is not None else -1
        except (KeyError, IndexError, TypeError, ValueError):
            current = None
            current_generation = -1
        if (
            current is None
            or not bool(current["enabled"])
            or current_generation != account_generation
        ):
            raise ShopSyncPersistenceError("店铺已删除或停用，本次同步结果已丢弃")

    ensure_account_current()
    previous_cookie = read_secret(user_id, "cookies.txt", account_key) if replace_cookie else ""
    previous_snapshot = load_verified_snapshot(user_id, account_key) if replace_cookie else None
    previous_products_config = (
        read_secret(user_id, "products_config.json", account_key) if replace_cookie else ""
    )
    previous_account_ref = str(previous_snapshot.get("account_ref") or "") if previous_snapshot else ""
    if not previous_account_ref and previous_cookie:
        try:
            _, previous_cookies = parse_cookie_header(previous_cookie)
            previous_account_ref = account_ref(previous_cookies)
        except ShopSyncError:
            # A malformed old Cookie is still evidence that a prior mapping
            # must not survive a new verified account.
            previous_account_ref = "invalid"

    attempted_account_ref = ""
    sync_lease_owner = ""
    sync_lease_key = ""
    egress_lease_key = ""
    try:
        normalized, _ = parse_cookie_header(cookie_header)
        if not replace_cookie:
            saved, _ = parse_cookie_header(read_secret(user_id, "cookies.txt", account_key))
            if saved != normalized:
                raise ShopSyncPersistenceError("店铺登录信息已更新，请重新检测")
        try:
            _, attempted_cookies = parse_cookie_header(normalized)
            attempted_account_ref = account_ref(attempted_cookies)
        except ShopSyncError:
            pass

        check_sync_circuit()
        sync_lease_key = f"shop-sync:{int(user_id)}:{int(account['id'])}"
        sync_lease_owner = (
            f"{lease_owner_prefix}:{os.getpid()}:{threading.get_ident()}:{time.time_ns()}"
        )
        # The in-process gate in ``shop_sync`` cannot coordinate the API and
        # the separate consumer. A short-lived persistent egress lease keeps
        # platform requests serialized across processes and machines sharing
        # the same SQLite control plane.
        egress_lease_key = "shop-sync:egress"
        egress_result = db.acquire_control_lease(
            egress_lease_key,
            sync_lease_owner,
            lease_seconds=SYNC_MAX_SECONDS + 120,
            cooldown_seconds=0,
        )
        if egress_result != "acquired":
            raise control_lease_error(db, egress_lease_key, egress_result)

        check_sync_circuit()
        cooldown = 0 if os.environ.get("SAAS_TESTING") == "1" else SYNC_COOLDOWN_SECONDS
        lease_result = db.acquire_control_lease(
            sync_lease_key, sync_lease_owner,
            lease_seconds=SYNC_MAX_SECONDS + 120, cooldown_seconds=cooldown,
        )
        if lease_result != "acquired":
            raise control_lease_error(db, sync_lease_key, lease_result)
        ensure_account_current()
        if account_key == "default":
            reserve_sync_func(user_id)
        else:
            reserve_sync_func(user_id, account_key)
        snapshot = sync_func(normalized)
        ensure_account_current()
    except ShopSyncError as error:
        # A failed replacement must not make a still-valid previous account
        # look broken. Checks against a saved Cookie do persist a blocking
        # result so the next status request can explain what needs attention.
        if error.code in PERSISTED_SYNC_CODES and not replace_cookie:
            try:
                ensure_account_current()
                save_sync_state(
                    user_id,
                    error.code,
                    str(error),
                    account_ref_value=attempted_account_ref or previous_account_ref,
                    account_key=account_key,
                )
            except (OSError, ShopSyncPersistenceError):
                pass
            sync_account_state(user_id, error.code, account=account)
        raise
    finally:
        if egress_lease_key and sync_lease_owner:
            db.release_control_lease(egress_lease_key, sync_lease_owner)
        if sync_lease_key and sync_lease_owner:
            db.release_control_lease(sync_lease_key, sync_lease_owner)

    account_changed = bool(previous_account_ref and previous_account_ref != snapshot.get("account_ref"))
    try:
        ensure_account_current()
        if replace_cookie and before_replace_persist is not None:
            # Keep the existing worker serving while the candidate login is
            # validated. Only pause it after platform verification succeeds,
            # immediately before the durable Cookie swap.
            before_replace_persist()
            ensure_account_current()
        if account_changed:
            # Never carry delivery mappings from a different seller account.
            ensure_account_current()
            write_secret(user_id, "products_config.json", json.dumps({"types": []}), account_key)
        if replace_cookie:
            ensure_account_current()
            write_secret(user_id, "cookies.txt", normalized, account_key)
        ensure_account_current()
        save_snapshot(user_id, snapshot, account_key)
        try:
            ensure_account_current()
            save_sync_state(
                user_id,
                "verified",
                checked_at=str(snapshot.get("synced_at") or ""),
                account_ref_value=str(snapshot.get("account_ref") or ""),
                account_key=account_key,
            )
        except OSError:
            # The snapshot and Cookie remain authoritative; a missing status
            # file simply falls back to the inferred verified state.
            pass
        ensure_account_current()
        sync_account_state(user_id, "verified", snapshot, account=account)
        ensure_account_current()
    except (OSError, ValueError, TypeError) as error:
        # A fenced/deleted account must not receive rollback writes from an
        # in-flight job.  Its private files are no longer part of an active
        # account and can be cleaned up by a separate retention process.
        still_current = False
        try:
            still_current = db.account_is_current(user_id, account_id, account_generation)
        except (AttributeError, TypeError, ValueError):
            still_current = True
        if replace_cookie and still_current:
            try:
                write_secret(user_id, "cookies.txt", previous_cookie, account_key)
            except OSError:
                pass
            if previous_snapshot is not None:
                try:
                    save_snapshot(user_id, previous_snapshot, account_key)
                except (OSError, ValueError, TypeError):
                    pass
        if account_changed and previous_products_config and still_current:
            try:
                write_secret(user_id, "products_config.json", previous_products_config, account_key)
            except OSError:
                pass
        raise ShopSyncPersistenceError() from error
    return snapshot
