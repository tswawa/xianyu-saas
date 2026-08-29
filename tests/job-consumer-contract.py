#!/usr/bin/env python3
"""Offline contract for the independent durable shop-sync consumer."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-job-consumer-"))
os.environ.update(
    {
        "SAAS_DB": str(RUN_DIR / "saas.db"),
        "SAAS_TENANTS_DIR": str(RUN_DIR / "tenants"),
        "SAAS_TESTING": "1",
        "SAAS_SHOP_SYNC_COOLDOWN_SECONDS": "1",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

from account_storage import AccountStorage  # noqa: E402
from db import DB  # noqa: E402
from job_consumer import JobConsumer  # noqa: E402
from shop_sync import ShopSyncError, account_ref, parse_cookie_header  # noqa: E402


def snapshot_for(cookie_header: str) -> dict:
    _, cookies = parse_cookie_header(cookie_header)
    seller = cookies["unb"]
    return {
        "version": 1,
        "account_ref": account_ref(cookies),
        "nickname": f"合同店铺-{seller}",
        "products": [
            {
                "id": seller,
                "title": f"商品-{seller}",
                "description": "离线 consumer 合同商品",
                "price": "1.00",
                "status": "在售",
                "source": "cookie",
                "updated_at": "2026-08-16T00:00:00+0800",
            }
        ],
        "product_count": 1,
        "synced_at": "2026-08-16T00:00:00+0800",
        "truncated": False,
    }


def main() -> None:
    db = DB(str(RUN_DIR / "saas.db"))
    storage = AccountStorage(str(RUN_DIR / "tenants"))
    user_id = db.create_user("consumer-owner", "password-123")
    default = db.ensure_default_shop_account(user_id)
    second = db.create_shop_account(user_id, "second", "第二店铺")
    storage.write_text(user_id, "default", "cookies.txt", "unb=100001; _m_h5_tk=token-a_tail")
    storage.write_text(user_id, "second", "cookies.txt", "unb=100002; _m_h5_tk=token-b_tail")

    calls: list[str] = []
    fail_next = {"value": False}

    def fake_sync(cookie_header: str) -> dict:
        _, cookies = parse_cookie_header(cookie_header)
        seller = cookies["unb"]
        calls.append(seller)
        if fail_next["value"]:
            fail_next["value"] = False
            raise ShopSyncError("network_error", "暂时无法连接闲鱼，请稍后重试")
        return snapshot_for(cookie_header)

    # Importing the consumer must not import the FastAPI app or start its
    # watchdog/recovery side effects.
    assert "app" not in sys.modules

    first = db.enqueue_job(
        user_id,
        "shop_sync",
        "same-refresh",
        payload={"replace_cookie": False, "cookie_fingerprint": ""},
    )
    duplicate = db.enqueue_job(
        user_id,
        "shop_sync",
        "same-refresh",
        payload={"replace_cookie": False, "cookie_fingerprint": "ignored"},
    )
    second_job = db.enqueue_job(
        user_id,
        "shop_sync",
        "same-refresh",
        account_id=second["id"],
        payload={"replace_cookie": False, "cookie_fingerprint": ""},
    )
    assert first["id"] == duplicate["id"]
    assert first["payload_json"] == duplicate["payload_json"]
    assert first["id"] != second_job["id"]
    assert "token-a" not in first["payload_json"]
    assert "token-b" not in second_job["payload_json"]

    consumer = JobConsumer(
        db,
        sync_func=fake_sync,
        reserve_sync_func=lambda *_args: None,
        storage=storage,
        poll_seconds=0.01,
        lease_seconds=180,
        owner="consumer-contract",
    )
    assert consumer.run_once() == 1
    assert consumer.run_once() == 1
    assert db.get_job(first["id"])["status"] == "completed"
    assert db.get_job(second_job["id"])["status"] == "completed"
    assert sorted(calls) == ["100001", "100002"]
    assert json.loads(storage.read_text(user_id, "default", "shop_snapshot.json"))["nickname"] == "合同店铺-100001"
    assert json.loads(storage.read_text(user_id, "second", "shop_snapshot.json"))["nickname"] == "合同店铺-100002"
    assert db.get_shop_account(user_id, account_id=second["id"])["status"] == "ready"

    retry_job = db.enqueue_job(
        user_id,
        "shop_sync",
        "retry-refresh",
        account_id=default["id"],
        payload={"replace_cookie": False, "cookie_fingerprint": ""},
        max_attempts=2,
    )
    fail_next["value"] = True
    assert consumer.run_once() == 1
    retry_row = db.get_job(retry_job["id"])
    assert retry_row["status"] == "retry"
    assert retry_row["last_error_code"] == "network_error"
    assert consumer.run_once(now=retry_row["available_at"]) == 1
    assert db.get_job(retry_job["id"])["status"] == "completed"

    cooldown_job = db.enqueue_job(
        user_id,
        "shop_sync",
        "cooldown-refresh",
        account_id=default["id"],
        payload={"replace_cookie": False, "cookie_fingerprint": ""},
        max_attempts=3,
    )
    cooldown_once = {"value": True}

    def cooldown_reserve(*_args):
        if cooldown_once["value"]:
            cooldown_once["value"] = False
            raise ShopSyncError("sync_cooldown", "操作太频繁，请稍后再试")

    consumer.reserve_sync_func = cooldown_reserve
    assert consumer.run_once() == 1
    cooldown_row = db.get_job(cooldown_job["id"])
    assert cooldown_row["status"] == "retry"
    assert cooldown_row["last_error_code"] == "sync_cooldown"
    assert float(cooldown_row["available_at"]) - float(cooldown_row["updated_at"]) >= 1.9
    assert db.get_shop_account(user_id, account_id=default["id"])["status"] == "ready"
    assert consumer.run_once(now=cooldown_row["available_at"] - 0.01) == 0
    assert consumer.run_once(now=cooldown_row["available_at"]) == 1
    assert db.get_job(cooldown_job["id"])["status"] == "completed"
    consumer.reserve_sync_func = lambda *_args: None

    unsupported = db.enqueue_job(
        user_id,
        "shop_sync",
        "replacement-must-not-leak",
        account_id=default["id"],
        payload={"replace_cookie": True, "cookie": "secret-must-not-be-used"},
        max_attempts=1,
    )
    assert consumer.run_once() == 1
    unsupported_row = db.get_job(unsupported["id"])
    assert unsupported_row["status"] == "dead_letter"
    assert unsupported_row["last_error_code"] == "unsupported_job"

    other_kind = db.enqueue_job(user_id, "delivery", "left-for-another-consumer")
    assert consumer.run_once() == 0
    assert db.get_job(other_kind["id"])["status"] == "queued"

    lease_job = db.enqueue_job(user_id, "shop_sync", "lease-contract", account_id=default["id"])
    lease_now = float(lease_job["available_at"])
    claimed = db.claim_job(lease_job["id"], "lease-owner", now=lease_now, lease_seconds=1)
    assert claimed is not None
    assert db.renew_job(lease_job["id"], "wrong-owner", now=lease_now, lease_seconds=30) is False
    assert db.renew_job(lease_job["id"], "lease-owner", now=lease_now, lease_seconds=30) is True
    assert db.complete_job(lease_job["id"], "lease-owner") is True

    print("job-consumer-contract: scoped refresh, retries, lease renewal and payload safety passed")


if __name__ == "__main__":
    main()
