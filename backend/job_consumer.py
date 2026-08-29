"""Durable control-plane job consumer.

The first consumer owns only ``shop_sync`` refresh jobs.  It reads the
already-verified Cookie from the account's private directory; job payloads may
contain a fingerprint and flags, but never credential material.  Keeping the
consumer as a separate module/process prevents long platform requests from
occupying API request workers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import threading
import time
from collections.abc import Callable

from account_storage import AccountStorage, AccountStorageError
from db import DB
from shop_sync import (
    SYNC_COOLDOWN_SECONDS,
    SYNC_MAX_SECONDS,
    ShopSyncError,
    account_ref,
    load_verified_snapshot,
    parse_cookie_header,
    reserve_sync,
    sync_shop,
)
from shop_sync_service import ShopSyncPersistenceError, run_shop_sync_inner


POLL_SECONDS = max(
    0.2,
    min(float(os.environ.get("SAAS_JOB_CONSUMER_POLL_SECONDS", "1.0")), 30.0),
)
LEASE_SECONDS = max(
    float(SYNC_MAX_SECONDS + 120),
    min(float(os.environ.get("SAAS_JOB_LEASE_SECONDS", str(SYNC_MAX_SECONDS + 120))), 3600.0),
)


def _safe_read(storage: AccountStorage, user_id: int, name: str, account_key: str) -> str:
    try:
        return storage.read_text(user_id, account_key, name).strip()
    except (OSError, AccountStorageError):
        return ""


def _safe_write(storage: AccountStorage, user_id: int, name: str, value: str, account_key: str) -> None:
    try:
        storage.write_text(user_id, account_key, name, value)
    except AccountStorageError as error:
        raise OSError("cannot write account storage") from error


class JobConsumer:
    """One-process consumer with bounded polling and lease heartbeats."""

    SUPPORTED_KINDS = ("shop_sync",)

    def __init__(
        self,
        db: DB | None = None,
        *,
        sync_func: Callable = sync_shop,
        reserve_sync_func: Callable = reserve_sync,
        storage: AccountStorage | None = None,
        load_snapshot_func: Callable = load_verified_snapshot,
        poll_seconds: float = POLL_SECONDS,
        lease_seconds: float = LEASE_SECONDS,
        owner: str | None = None,
    ):
        self.db = db or DB()
        self.sync_func = sync_func
        self.reserve_sync_func = reserve_sync_func
        self.storage = storage or AccountStorage()
        self.load_snapshot_func = load_snapshot_func
        self.poll_seconds = max(0.05, min(float(poll_seconds), 30.0))
        self.lease_seconds = max(float(SYNC_MAX_SECONDS + 120), min(float(lease_seconds), 3600.0))
        self.owner = owner or f"consumer:{os.getpid()}:{time.time_ns()}"
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @staticmethod
    def _account_state(db: DB, user_id: int, code: str, snapshot=None, account=None) -> None:
        if account is None:
            return
        try:
            generation = int(account["generation"] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            generation = 0
        if snapshot is not None:
            now = time.time()
            fields = {
                "account_ref": str(snapshot.get("account_ref") or "")[:64],
                "status": "ready",
                "enabled": True,
                "last_error_code": "",
                "last_verified_at": now,
                "last_sync_at": now,
            }
            if not str(account["display_name"] or "").strip():
                fields["display_name"] = str(snapshot.get("nickname") or "")[:160]
            db.update_shop_account_if_current(
                user_id, account["id"], generation, **fields
            )
            return
        status = "expired" if code in {"cookie_expired", "cookie_invalid", "cookie_incomplete"} else "degraded"
        db.update_shop_account_if_current(
            user_id,
            account["id"],
            generation,
            status=status,
            last_error_code=str(code or "sync_error")[:80],
        )

    def _resolve_account(self, row):
        user_id = int(row["user_id"])
        raw_account_id = int(row["account_id"] or 0)
        if raw_account_id == 0:
            account = self.db.ensure_default_shop_account(user_id)
        else:
            account = self.db.get_shop_account(user_id, account_id=raw_account_id)
        if account is None or not account["enabled"]:
            return None
        return account

    @staticmethod
    def _payload(row) -> dict:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            raise ValueError("invalid job payload") from None
        if not isinstance(payload, dict):
            raise ValueError("invalid job payload")
        return payload

    def _heartbeat(self, job_id: int, done: threading.Event) -> None:
        interval = max(5.0, min(self.lease_seconds / 3.0, 30.0))
        while not done.wait(interval):
            if not self.db.renew_job(job_id, self.owner, lease_seconds=self.lease_seconds):
                return

    def _fail(
        self,
        row,
        code: str,
        message: str = "",
        *,
        retry_delay_seconds: float | None = None,
    ) -> bool:
        return self.db.fail_job(
            int(row["id"]),
            self.owner,
            str(code or "temporary")[:80],
            str(message or "")[:240],
            retry_delay_seconds=retry_delay_seconds,
        )

    def process(self, row) -> str:
        """Process one claimed row and return its terminal action."""
        job_id = int(row["id"])
        user_id = int(row["user_id"])
        account = self._resolve_account(row)
        if account is None:
            self._fail(row, "account_unavailable", "店铺账号不可用")
            return "failed"

        try:
            payload = self._payload(row)
            # Replacement jobs carry a fingerprint but cannot be safely
            # replayed by this process because their candidate Cookie exists
            # only in the API request.  Fail closed instead of trusting it.
            if payload.get("replace_cookie") is not False:
                self._fail(row, "unsupported_job", "该任务类型暂不由后台消费者处理")
                return "failed"

            account_key = str(account["account_key"])
            cookie_header = _safe_read(self.storage, user_id, "cookies.txt", account_key)
            if not cookie_header:
                self._fail(row, "unconfigured", "店铺尚未连接")
                self._account_state(self.db, user_id, "unconfigured", account=account)
                return "failed"
            normalized, cookies = parse_cookie_header(cookie_header)
            expected_fingerprint = str(payload.get("cookie_fingerprint") or "")
            actual_fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
            if expected_fingerprint and expected_fingerprint != actual_fingerprint:
                self._fail(row, "stale_job", "店铺登录信息已更新，请重新检测")
                return "failed"
            # Parsing above also ensures the account reference is available;
            # do not persist it or expose it in the job response.
            _ = account_ref(cookies)
        except ShopSyncError as error:
            self._fail(row, error.code, str(error))
            self._account_state(self.db, user_id, error.code, account=account)
            return "failed"
        except ValueError as error:
            self._fail(row, "invalid_payload", str(error))
            return "failed"

        heartbeat_done = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(job_id, heartbeat_done),
            daemon=True,
            name=f"job-lease-{job_id}",
        )
        heartbeat.start()
        try:
            run_shop_sync_inner(
                db=self.db,
                read_secret=lambda uid, name, key=account_key: _safe_read(
                    self.storage, uid, name, key
                ),
                write_secret=lambda uid, name, value, key=account_key: _safe_write(
                    self.storage, uid, name, value, key
                ),
                load_verified_snapshot=self.load_snapshot_func,
                sync_account_state=lambda uid, code, snapshot=None, account=None: self._account_state(
                    self.db, uid, code, snapshot, account
                ),
                user_id=user_id,
                cookie_header=normalized,
                replace_cookie=False,
                account=account,
                sync_func=self.sync_func,
                reserve_sync_func=self.reserve_sync_func,
                lease_owner_prefix="consumer",
            )
        except ShopSyncError as error:
            if error.code == "sync_cooldown":
                # A cooldown is a scheduling condition, not a failed shop
                # check. Defer beyond the platform window instead of burning
                # all attempts in a few seconds and reporting dead_letter.
                self._fail(
                    row,
                    error.code,
                    str(error),
                    retry_delay_seconds=SYNC_COOLDOWN_SECONDS + 1,
                )
                return "deferred"
            self._fail(row, error.code, str(error))
            return "failed"
        except ShopSyncPersistenceError as error:
            self._fail(row, error.code, str(error))
            return "failed"
        except (OSError, RuntimeError, ValueError, TypeError):
            self._fail(row, "sync_error", "店铺同步失败，请稍后重试")
            return "failed"
        finally:
            heartbeat_done.set()
            heartbeat.join(timeout=1.0)

        try:
            account_generation = int(account["generation"] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            account_generation = 0
        completed = self.db.complete_job_for_account(
            job_id,
            self.owner,
            user_id,
            int(account["id"]),
            account_generation,
            str(account["account_key"]),
        )
        if not completed and not self.db.account_is_current(
            user_id, int(account["id"]), account_generation
        ):
            self._fail(row, "account_unavailable", "店铺账号已停用")
            return "failed"
        if not completed:
            return "lease_lost"
        return "completed"

    def run_once(self, *, now: float | None = None) -> int:
        rows = self.db.claim_jobs(
            self.owner,
            limit=1,
            lease_seconds=self.lease_seconds,
            now=now,
            kinds=self.SUPPORTED_KINDS,
        )
        for row in rows:
            self.process(row)
        return len(rows)

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            if self.run_once() == 0:
                self.stop_event.wait(self.poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepWhale durable job consumer")
    parser.add_argument("--once", action="store_true", help="consume at most one available job")
    args = parser.parse_args()
    consumer = JobConsumer()

    def stop(_signum, _frame):
        consumer.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.once:
        consumer.run_once()
    else:
        consumer.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by service startup
    raise SystemExit(main())
