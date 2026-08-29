#!/usr/bin/env python3
"""Offline API -> durable job consumer -> polling contract."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-async-sync-"))
os.environ.update(
    {
        "SAAS_DB": str(RUN_DIR / "saas.db"),
        "SAAS_TENANTS_DIR": str(RUN_DIR / "tenants"),
        "SAAS_COOKIE_SECURE": "0",
        "SAAS_RESTORE_WORKERS": "0",
        "SAAS_TESTING": "1",
        "SAAS_ALLOW_REGISTRATION": "1",
        "SAAS_SHOP_SYNC_COOLDOWN_SECONDS": "1",
        "SAAS_PLATFORM_AI_BASE_URL": "",
        "SAAS_PLATFORM_AI_MODEL": "",
        "SAAS_PLATFORM_AI_KEY": "",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import shop_sync  # noqa: E402
from account_storage import AccountStorage  # noqa: E402
from job_consumer import JobConsumer  # noqa: E402


def fake_sync(cookie_header: str) -> dict:
    _, cookies = shop_sync.parse_cookie_header(cookie_header)
    seller = cookies["unb"]
    return {
        "version": 1,
        "account_ref": shop_sync.account_ref(cookies),
        "nickname": f"异步店铺-{seller}",
        "products": [],
        "product_count": 0,
        "synced_at": "2026-08-16T00:00:00+0800",
        "truncated": False,
    }


def main() -> None:
    app.sync_shop = fake_sync
    app.reserve_sync = lambda *_args: None
    client = TestClient(app.app)
    assert client.post(
        "/api/auth/register", json={"username": "async-owner", "password": "password-123"}
    ).status_code == 200
    login = client.post(
        "/api/auth/login", json={"username": "async-owner", "password": "password-123"}
    )
    assert login.status_code == 200
    client.cookies.set("xianyu_saas_session", login.cookies.get("xianyu_saas_session"), path="/")

    # Seed the saved Cookie and a verified snapshot through the existing safe
    # replacement path. The async path never receives the Cookie value.
    assert client.put(
        "/api/bot/cookies", json={"cookies": "unb=200001; _m_h5_tk=seed-token_tail"}
    ).status_code == 200
    user_id = int(app.db.get_user("async-owner")["id"])
    account = app.db.ensure_default_shop_account(user_id)
    second = app.db.create_shop_account(user_id, "second", "第二店铺")

    # A previous worker auth failure must not survive a later successful
    # background detection. The API polling endpoint owns clearing that stale
    # state and resuming the account's durable running intent.
    app.write_secret(
        user_id,
        "auth_status.json",
        json.dumps({"code": "session_expired", "reauthorization_required": True}),
        "default",
    )
    before_refresh = client.get("/api/bot/status").json()
    assert before_refresh["reauthorization_required"] is True
    assert before_refresh["account"]["status"] == "expired"

    queued = client.post(
        "/api/bot/shop/sync",
        headers={"Prefer": "respond-async"},
    )
    assert queued.status_code == 202
    job_id = queued.json()["job"]["id"]
    raw_job = app.db.get_job(job_id)
    assert "seed-token" not in raw_job["payload_json"]
    assert raw_job["status"] == "queued"
    assert client.get(f"/api/bot/jobs/{job_id}").status_code == 202
    assert client.get(
        f"/api/bot/jobs/{job_id}", headers={"X-Shop-Account": "second"}
    ).status_code == 404

    storage = AccountStorage(str(RUN_DIR / "tenants"))
    consumer = JobConsumer(
        db=app.DB(str(RUN_DIR / "saas.db")),
        sync_func=fake_sync,
        reserve_sync_func=lambda *_args: None,
        storage=storage,
        owner="async-contract-consumer",
    )
    assert consumer.run_once() == 1
    done = client.get(f"/api/bot/jobs/{job_id}")
    assert done.status_code == 200
    assert done.json()["job"]["status"] == "completed"
    assert done.json()["result"]["connected"] is True
    assert done.json()["result"]["shop_name"] == "异步店铺-200001"
    assert done.json()["result"]["worker"]["desired_running"] is True
    refreshed_status = client.get("/api/bot/status").json()
    assert refreshed_status["auth_code"] == "ok"
    assert refreshed_status["reauthorization_required"] is False
    assert refreshed_status["account"]["status"] == "ready"
    assert refreshed_status["account"]["last_error_code"] == ""
    assert client.get(f"/api/bot/jobs/{job_id}", headers={"X-Shop-Account": "second"}).status_code == 404

    # The second account remains empty and isolated. Missing control files keep
    # automation disabled without making the read-only status endpoint fail.
    second_status = client.get(
        "/api/bot/status", headers={"X-Shop-Account": "second"}
    )
    assert second_status.status_code == 200, second_status.text
    second_payload = second_status.json()
    assert second_payload["connected"] is False
    assert second_payload["automation_enabled"] is False
    assert second_payload["automation_config_valid"] is False
    assert app.db.get_shop_account(app.db.get_user("async-owner")["id"], account_id=account["id"])["status"] == "ready"
    assert app.db.get_shop_account(app.db.get_user("async-owner")["id"], account_id=second["id"])["status"] == "unconfigured"

    # Regression: deleting a shop while its consumer is inside the platform
    # call must invalidate the in-flight sync.  The stale result must not
    # recreate a snapshot, re-enable the account, or complete the job.
    race = app.db.create_shop_account(
        app.db.get_user("async-owner")["id"], "race", "竞态店铺"
    )
    storage.write_text(
        app.db.get_user("async-owner")["id"], "race", "cookies.txt",
        "unb=300001; _m_h5_tk=race-token_tail",
    )
    race_job = app.db.enqueue_job(
        app.db.get_user("async-owner")["id"],
        "shop_sync",
        "race-refresh",
        account_id=race["id"],
        payload={"replace_cookie": False, "cookie_fingerprint": ""},
        max_attempts=1,
    )
    sync_entered = threading.Event()
    release_sync = threading.Event()

    def blocking_sync(cookie_header: str) -> dict:
        sync_entered.set()
        if not release_sync.wait(5):
            raise AssertionError("blocking sync was not released")
        return fake_sync(cookie_header)

    race_consumer = JobConsumer(
        db=app.DB(str(RUN_DIR / "saas.db")),
        sync_func=blocking_sync,
        reserve_sync_func=lambda *_args: None,
        storage=storage,
        owner="async-race-consumer",
    )
    worker_error: list[BaseException] = []

    def consume_race() -> None:
        try:
            assert race_consumer.run_once() == 1
        except BaseException as error:  # surface worker failures in this contract
            worker_error.append(error)

    consumer_thread = threading.Thread(target=consume_race)
    consumer_thread.start()
    assert sync_entered.wait(5), "consumer did not enter the platform call"
    deleted = client.delete("/api/bot/accounts/race")
    assert deleted.status_code == 200, deleted.text
    release_sync.set()
    consumer_thread.join(timeout=10)
    assert not consumer_thread.is_alive(), "consumer thread did not finish"
    assert not worker_error, worker_error
    race_after = app.db.get_shop_account(
        app.db.get_user("async-owner")["id"], account_id=race["id"]
    )
    assert race_after["enabled"] == 0
    assert race_after["status"] == "deleted"
    assert int(race_after["generation"]) > int(race["generation"])
    assert app.db.get_job(race_job["id"])["status"] != "completed"
    assert not (
        RUN_DIR
        / "tenants"
        / str(app.db.get_user("async-owner")["id"])
        / "race"
        / "shop_snapshot.json"
    ).exists()
    # A late state callback carrying the pre-delete row must also be fenced;
    # this directly guards the worker's historical unconditional re-enable.
    JobConsumer._account_state(
        app.db,
        app.db.get_user("async-owner")["id"],
        "verified",
        fake_sync("unb=300001; _m_h5_tk=race-token_tail"),
        race,
    )
    assert app.db.get_shop_account(
        app.db.get_user("async-owner")["id"], account_id=race["id"]
    )["enabled"] == 0
    app._sync_account_state(
        app.db.get_user("async-owner")["id"],
        "verified",
        fake_sync("unb=300001; _m_h5_tk=race-token_tail"),
        race,
    )
    assert app.db.get_shop_account(
        app.db.get_user("async-owner")["id"], account_id=race["id"]
    )["enabled"] == 0

    print("async-shop-sync-contract: enqueue, scoped polling and consumer completion passed")


if __name__ == "__main__":
    main()
