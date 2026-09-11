"""Durable control-plane job consumer.

The consumer owns ``shop_sync`` and ``ops_run`` jobs on separate bounded lanes.
Sync reads the already-verified Cookie; operations payloads contain only a run
identifier. Neither queue carries credentials or business conversation text.
No API module is imported, and long model calls cannot block the sync lane.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sqlite3
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
from shop_sync_service import ShopConnectionCoordinator, ShopSyncPersistenceError, run_shop_sync_inner


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

    SUPPORTED_KINDS = ("shop_sync", "ops_run")

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
        operations_service=None,
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
        self.operations_service = operations_service

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

    def _heartbeat(self, job_id: int, done: threading.Event, lost: threading.Event | None = None) -> None:
        interval = max(5.0, min(self.lease_seconds / 3.0, 30.0))
        while not done.wait(interval):
            try:
                owned = self.db.renew_job(job_id, self.owner, lease_seconds=self.lease_seconds)
            except sqlite3.Error:
                owned = False
            if not owned:
                if lost is not None:
                    lost.set()
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

    def _operations(self):
        if self.operations_service is None:
            # Construct the services against this consumer's DB and storage.
            # Importing app here would create a second API/watchdog instance.
            from ai_customer_service import AIService
            from operations import OperationsService
            from user_ai_connection import UserAIConnections

            ai_service = AIService(self.storage.root)
            ai_service.user_connections = UserAIConnections(self.db, ai_service)
            self.operations_service = OperationsService(self.db, ai_service)
        return self.operations_service

    def _process_ops(self, row) -> str:
        from operations import OperationsError

        job_id = int(row["id"])
        try:
            payload = self._payload(row)
            run_id = payload.get("run_id")
            if (set(payload) != {"run_id"} or not isinstance(run_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id)):
                raise ValueError("invalid operations payload")
            service = self._operations()
            with self.db._lock:
                run = self.db.con.execute(
                    "SELECT * FROM ops_runs WHERE id = ?", (run_id,),
                ).fetchone()
            if (run is None or int(run["user_id"]) != int(row["user_id"])
                    or int(run["shop_account_id"]) != int(row["account_id"])):
                raise ValueError("invalid operations scope")
            if int(run["job_id"]) != job_id:
                # A durable explicit retry owns a new job; an old attempt can
                # be acknowledged but cannot dispatch the new attempt.
                return "completed" if self.db.complete_job(job_id, self.owner) else "lease_lost"
        except (ValueError, KeyError, TypeError):
            self._fail(row, "invalid_payload", "运维任务标识或归属无效")
            return "failed"

        done, lost = threading.Event(), threading.Event()

        def ensure_job():
            current = self.db.get_job(job_id)
            if (lost.is_set() or current is None or current["status"] != "running"
                    or current["lease_owner"] != self.owner
                    or float(current["lease_until"] or 0) <= time.time()):
                raise OperationsError("job_lease_lost", 409)
            if self.stop_event.is_set():
                raise OperationsError("runner_interrupted", 503)
            return current

        heartbeat = threading.Thread(
            target=self._heartbeat, args=(job_id, done, lost),
            daemon=True, name=f"ops-job-lease-{job_id}",
        )
        heartbeat.start()
        try:
            result = service.process_run(run_id, ensure_job=ensure_job)
            if lost.is_set():
                return "lease_lost"
            status = result.get("status") if isinstance(result, dict) else None
            if status in {"queued", "running", "cancel_requested"}:
                deferred = self.db.defer_job(job_id, self.owner, self.poll_seconds)
                return "deferred" if deferred else "lease_lost"
            if status not in {"waiting_user", "succeeded", "partial_failed", "failed",
                              "cancelled", "needs_review"}:
                raise ValueError("invalid operations result")
            # Business/model failures are already durable visible run results.
            # Complete this dispatch so the queue cannot silently retry a model.
            return "completed" if self.db.complete_job(job_id, self.owner) else "lease_lost"
        except OperationsError as error:
            if error.code in {"runner_interrupted", "job_lease_lost"}:
                deferred = self.db.defer_job(job_id, self.owner, self.poll_seconds)
                return "deferred" if deferred else "lease_lost"
            self._fail(row, "operations_unavailable", "运维任务处理暂时不可用")
            return "failed"
        except (OSError, sqlite3.Error, RuntimeError, ValueError, TypeError):
            self._fail(row, "operations_unavailable", "运维任务处理暂时不可用")
            return "failed"
        finally:
            done.set()
            heartbeat.join(timeout=1.0)

    def process(self, row) -> str:
        """Reserve before reading the saved Cookie, including malformed old logins."""
        if row["kind"] != "shop_sync":
            return self._process(row)
        account = self._resolve_account(row)
        if account is None:
            return self._process(row)
        coordinator = ShopConnectionCoordinator(self.db)
        try:
            lease = coordinator.acquire(
                int(row["user_id"]), str(account["account_key"]),
                f"consumer-connect:{os.getpid()}:{threading.get_ident()}:{time.time_ns()}",
                SYNC_MAX_SECONDS + 120,
            )
        except ShopSyncError as error:
            deferred = self.db.defer_job(int(row["id"]), self.owner, error.retry_after or SYNC_COOLDOWN_SECONDS)
            return "deferred" if deferred else "lease_lost"
        except ShopSyncPersistenceError as error:
            self._fail(row, error.code, str(error))
            return "failed"
        try:
            return self._process(row, connection_lease=lease)
        finally:
            coordinator.release(lease)

    def _process(self, row, connection_lease=None) -> str:
        """Process one claimed row and return its terminal action."""
        if row["kind"] == "ops_run":
            return self._process_ops(row)
        if row["kind"] != "shop_sync":
            self._fail(row, "unsupported_job", "该任务类型暂不由后台消费者处理")
            return "failed"
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
                connection_lease=connection_lease,
            )
        except ShopSyncError as error:
            if error.code in {"sync_cooldown", "sync_busy", "risk_cooldown"}:
                # Local protection is scheduling, not a failed platform call.
                deferred = self.db.defer_job(
                    job_id, self.owner, error.retry_after or SYNC_COOLDOWN_SECONDS,
                )
                return "deferred" if deferred else "lease_lost"
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

    def run_once(self, *, now: float | None = None, kinds=None) -> int:
        rows = self.db.claim_jobs(
            self.owner,
            limit=1,
            lease_seconds=self.lease_seconds,
            now=now,
            kinds=self.SUPPORTED_KINDS if kinds is None else kinds,
        )
        for row in rows:
            self.process(row)
        return len(rows)

    def _run_lane(self, kinds) -> None:
        while not self.stop_event.is_set():
            try:
                processed = self.run_once(kinds=kinds)
            except (OSError, sqlite3.Error):
                # A transient store failure must not silently kill a lane.
                # The existing lease recovers an interrupted claimed job.
                processed = 0
            if processed == 0:
                self.stop_event.wait(self.poll_seconds)

    def run_forever(self) -> None:
        ops_lane = threading.Thread(
            target=self._run_lane, args=(("ops_run",),),
            name="operations-consumer", daemon=True,
        )
        ops_lane.start()
        try:
            self._run_lane(("shop_sync",))
        finally:
            self.stop()
            ops_lane.join(timeout=2.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="xianyu-saas durable job consumer")
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
