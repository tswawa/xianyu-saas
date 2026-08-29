#!/usr/bin/env python3
"""Offline contracts for the durable control-plane records."""

from __future__ import annotations

import tempfile
import time
import sqlite3
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from db import (  # noqa: E402
    COMPLETED_JOB_RETENTION_SECONDS,
    DEAD_LETTER_JOB_RETENTION_SECONDS,
    TOKEN_TTL_SECONDS,
    DB,
)


def main():
    with tempfile.TemporaryDirectory(prefix="xianyu-control-plane-") as root:
        path = str(Path(root) / "saas.db")
        db = DB(path)
        user_a = db.create_user("owner-a", "password-123")
        user_b = db.create_user("owner-b", "password-123")

        default = db.ensure_default_shop_account(user_a)
        assert default["account_key"] == "default"
        assert len(db.list_shop_accounts(user_a)) == 1
        second = db.create_shop_account(user_a, "second", "第二店铺")
        assert len(db.list_shop_accounts(user_a)) == 2
        assert db.get_shop_account(user_b, account_id=second["id"]) is None
        assert db.update_shop_account(user_b, account_id=second["id"], status="ready") is None

        first = db.enqueue_job(
            user_a,
            "shop_sync",
            "same-event",
            account_id=default["id"],
            payload={"kind": "metadata-only"},
        )
        duplicate = db.enqueue_job(
            user_a,
            "shop_sync",
            "same-event",
            account_id=default["id"],
            payload={"ignored": True},
        )
        other_account = db.enqueue_job(
            user_a,
            "shop_sync",
            "same-event",
            account_id=second["id"],
        )
        other_user = db.enqueue_job(user_b, "shop_sync", "same-event")
        assert first["id"] == duplicate["id"]
        assert first["payload_json"] == duplicate["payload_json"]
        assert "metadata-only" in duplicate["payload_json"]
        assert len({first["id"], other_account["id"], other_user["id"]}) == 3

        now = first["available_at"]
        claimed = db.claim_jobs("worker-a", now=now, lease_seconds=10)
        assert [row["id"] for row in claimed] == [first["id"]]
        assert db.complete_job(first["id"], "wrong-worker") is False
        assert db.complete_job(first["id"], "worker-a") is True
        assert db.complete_job(first["id"], "worker-a") is False
        for row in db.claim_jobs("cleanup-worker", limit=10, now=time.time()):
            assert db.complete_job(row["id"], "cleanup-worker") is True

        retry_job = db.enqueue_job(user_a, "delivery", "retry-me", max_attempts=2)
        retry_now = retry_job["available_at"]
        claimed_retry = db.claim_job(retry_job["id"], "worker-b", now=retry_now, lease_seconds=1)
        assert claimed_retry["id"] == retry_job["id"]
        assert db.fail_job(retry_job["id"], "worker-b", "temporary", "network") is True
        retry_row = db.con.execute("SELECT * FROM jobs WHERE id = ?", (retry_job["id"],)).fetchone()
        assert retry_row["status"] == "retry"
        assert db.claim_jobs("worker-c", now=retry_row["available_at"] - 0.01) == []
        claimed_again = db.claim_jobs("worker-c", now=retry_row["available_at"], lease_seconds=1)
        assert len(claimed_again) == 1

        dead = db.enqueue_job(user_a, "sync", "dead", max_attempts=1)
        dead_claim = db.claim_job(dead["id"], "worker-d", now=dead["available_at"], lease_seconds=1)
        assert dead_claim["id"] == dead["id"]
        assert db.fail_job(dead["id"], "worker-d", "permanent", "stop") is True
        dead_row = db.con.execute("SELECT status FROM jobs WHERE id = ?", (dead["id"],)).fetchone()
        assert dead_row["status"] == "dead_letter"

        runtime = db.set_worker_desired(user_a, True, mode="rules_ai", account_id=default["id"])
        assert runtime["desired_state"] == "running"
        assert runtime["mode"] == "rules_ai"
        updated = db.update_worker_runtime(
            user_a,
            default["id"],
            state="running",
            pid=1234,
            generation=1,
        )
        assert updated["state"] == "running"
        assert updated["pid"] == 1234

        reopened = DB(path)
        assert len(reopened.list_shop_accounts(user_a)) == 2
        persisted = reopened.get_worker_runtime(user_a, default["id"])
        assert persisted["desired_state"] == "running"
        assert persisted["pid"] == 1234
        assert reopened.acquire_control_lease("shop-sync:default", "owner-1", now=1000, lease_seconds=10, cooldown_seconds=20) == "acquired"
        assert reopened.acquire_control_lease("shop-sync:default", "owner-2", now=1001, lease_seconds=10, cooldown_seconds=20) == "busy"
        assert reopened.release_control_lease("shop-sync:default", "owner-1", now=1002) is True
        assert reopened.acquire_control_lease("shop-sync:default", "owner-2", now=1003, lease_seconds=10, cooldown_seconds=20) == "cooldown"
        assert reopened.acquire_control_lease("shop-sync:default", "owner-2", now=1021, lease_seconds=10, cooldown_seconds=20) == "acquired"
        assert reopened.con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        fenced = reopened.create_shop_account(user_a, "fenced", "待删除店铺")
        old_generation = int(fenced["generation"])
        fenced_job = reopened.enqueue_job(
            user_a,
            "shop_sync",
            "fenced-job",
            account_id=fenced["id"],
        )
        claimed_fenced = reopened.claim_job(
            fenced_job["id"], "fenced-worker", now=fenced_job["available_at"]
        )
        assert claimed_fenced is not None
        deleted = reopened.disable_shop_account(user_a, fenced["id"])
        assert deleted["enabled"] == 0
        assert deleted["status"] == "deleted"
        assert int(deleted["generation"]) == old_generation + 1
        assert reopened.account_is_current(user_a, fenced["id"], old_generation) is False
        assert reopened.update_shop_account_if_current(
            user_a,
            fenced["id"],
            old_generation,
            enabled=True,
            status="ready",
        ) is None
        assert reopened.get_shop_account(user_a, account_id=fenced["id"])["enabled"] == 0
        assert reopened.complete_job_for_account(
            fenced_job["id"],
            "fenced-worker",
            user_a,
            fenced["id"],
            old_generation,
        ) is False
        assert reopened.get_job(fenced_job["id"])["status"] == "running"

    with tempfile.TemporaryDirectory(prefix="xianyu-control-plane-retention-") as root:
        retention_path = str(Path(root) / "retention.db")
        retention = DB(retention_path)
        retention_user = retention.create_user("retention-owner", "password-123")
        retention_account = retention.ensure_default_shop_account(retention_user)
        retention_now = time.time()
        old_token_at = retention_now - TOKEN_TTL_SECONDS - 1
        old_completed_at = retention_now - COMPLETED_JOB_RETENTION_SECONDS - 1
        old_dead_letter_at = retention_now - DEAD_LETTER_JOB_RETENTION_SECONDS - 1
        recent_at = retention_now - 60
        retention.con.executemany(
            "INSERT INTO tokens(token, user_id, created_at) VALUES (?, ?, ?)",
            [
                ("expired-session", retention_user, old_token_at),
                ("fresh-session", retention_user, recent_at),
            ],
        )

        def insert_retention_job(key, status, timestamp):
            retention.con.execute(
                """
                INSERT INTO jobs(
                    user_id, account_id, kind, idempotency_key, payload_json,
                    status, attempts, max_attempts, available_at, created_at,
                    updated_at, completed_at
                ) VALUES (?, ?, 'retention', ?, '{}', ?, 0, 1, ?, ?, ?, ?)
                """,
                (
                    retention_user,
                    retention_account["id"],
                    key,
                    status,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )

        insert_retention_job("expired-completed", "completed", old_completed_at)
        insert_retention_job("expired-dead", "dead_letter", old_dead_letter_at)
        insert_retention_job("recent-completed", "completed", recent_at)
        insert_retention_job("recent-dead", "dead_letter", recent_at)
        insert_retention_job("old-queued", "queued", old_dead_letter_at)
        insert_retention_job("old-retry", "retry", old_dead_letter_at)
        insert_retention_job("old-running", "running", old_dead_letter_at)
        retention.con.commit()
        retention.con.close()

        retention = DB(retention_path)
        assert retention.con.execute(
            "SELECT 1 FROM tokens WHERE token = 'expired-session'"
        ).fetchone() is None
        assert retention.con.execute(
            "SELECT 1 FROM tokens WHERE token = 'fresh-session'"
        ).fetchone() is not None
        remaining_keys = {
            row["idempotency_key"]
            for row in retention.con.execute(
                "SELECT idempotency_key FROM jobs WHERE kind = 'retention'"
            ).fetchall()
        }
        assert "expired-completed" not in remaining_keys
        assert "expired-dead" not in remaining_keys
        assert {
            "recent-completed",
            "recent-dead",
            "old-queued",
            "old-retry",
            "old-running",
        }.issubset(remaining_keys)

        for index in range(3):
            insert_retention_job(f"batch-completed-{index}", "completed", old_completed_at)
            insert_retention_job(f"batch-dead-{index}", "dead_letter", old_dead_letter_at)
        retention.con.commit()
        first_pass = retention.prune_retention(now=retention_now, batch_size=2)
        assert first_pass == {"tokens": 0, "completed_jobs": 2, "dead_letter_jobs": 2}
        assert retention.con.execute(
            "SELECT COUNT(*) FROM jobs WHERE idempotency_key LIKE 'batch-completed-%'"
        ).fetchone()[0] == 1
        assert retention.con.execute(
            "SELECT COUNT(*) FROM jobs WHERE idempotency_key LIKE 'batch-dead-%'"
        ).fetchone()[0] == 1
        second_pass = retention.prune_retention(now=retention_now, batch_size=2)
        assert second_pass == {"tokens": 0, "completed_jobs": 1, "dead_letter_jobs": 1}

    # Existing installations may have a shop_accounts table without the
    # fencing column. Opening such a database must migrate it in place.
    with tempfile.TemporaryDirectory(prefix="xianyu-control-plane-legacy-") as root:
        legacy_path = str(Path(root) / "legacy.db")
        legacy = sqlite3.connect(legacy_path)
        legacy.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                expires_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE shop_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_key TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'xianyu',
                display_name TEXT NOT NULL DEFAULT '',
                account_ref TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unconfigured',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_error_code TEXT NOT NULL DEFAULT '',
                last_verified_at REAL,
                last_sync_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(user_id, account_key)
            );
            INSERT INTO users(username, password_hash, created_at)
                VALUES ('legacy-owner', 'not-a-secret', 1);
            INSERT INTO shop_accounts(user_id, account_key, created_at, updated_at)
                VALUES (1, 'default', 1, 1);
            """
        )
        legacy.commit()
        legacy.close()
        migrated = DB(legacy_path)
        migrated_row = migrated.get_shop_account(1, account_key="default")
        assert migrated_row is not None
        assert int(migrated_row["generation"]) == 0

    print("control-plane contract: accounts, idempotent jobs, leases and runtime persistence passed")


if __name__ == "__main__":
    main()
