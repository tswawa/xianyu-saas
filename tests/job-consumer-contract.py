#!/usr/bin/env python3
"""Offline contract for the independent durable shop-sync consumer."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
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
            raise ShopSyncError("sync_cooldown", "操作太频繁，请稍后再试", retry_after=2)

    consumer.reserve_sync_func = cooldown_reserve
    assert consumer.run_once() == 1
    cooldown_row = db.get_job(cooldown_job["id"])
    assert cooldown_row["status"] == "retry"
    assert not cooldown_row["last_error_code"]
    assert cooldown_row["attempts"] == 0
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

    ops_consumer_contract(db, storage, user_id, default, second)
    print("job-consumer-contract: scoped sync, ops dispatch, retry fencing, lease recovery and independent lanes passed")


def ops_consumer_contract(db, storage, uid, account, other):
    from ai_customer_service import AIServiceError
    from operations import OperationsService

    class Model:
        clock = staticmethod(time.time)

        def __init__(self):
            self.storage = storage
            self.calls = 0
            self.respond = None

        def _connection_generation(self, _scope):
            return ("user", 1, 1, "verified")

        def agent_turn(self, *_args):
            self.calls += 1
            if self.respond:
                self.respond()
            return {"role": "assistant", "content": "本轮查询已完成，未修改配置。", "tool_calls": []}

    class ReadOnlyTools:
        def catalog(self, _run):
            return []

        def execute(self, *_args):
            raise AssertionError("consumer must not dispatch an unrequested write")

    class ProviderFailure(AIServiceError):
        def __init__(self):
            super().__init__("provider_error", 502, "模型暂时不可用")

        def public_detail(self):
            return {"source": "provider", "code": "provider_error", "message": "模型暂时不可用", "upstream_status": 503}

    model = Model()
    service = OperationsService(db, model)
    service.tools = ReadOnlyTools()
    consumer = JobConsumer(db, storage=storage, operations_service=service, owner="ops-contract", poll_seconds=0.05)
    scope = (uid, int(account["id"]), str(account["account_key"]))
    token_hash = hashlib.sha256(db.create_token(uid).encode()).hexdigest()
    counter = 0

    def enqueue():
        nonlocal counter
        counter += 1
        return service.chat(*scope, request_id=f"ops-queue-{counter}", message="只读查询本店商品", auth_session_hash=token_hash)

    def job_for(run):
        with db._lock:
            identity = db.con.execute("SELECT job_id FROM ops_runs WHERE id=?", (run["run_id"],)).fetchone()[0]
        return db.get_job(identity)

    # Construction is local to the consumer, never imports the API or uses a
    # different DB/storage root. It does not resolve keys or call a model.
    constructed = JobConsumer(db, storage=storage, owner="ops-construct")._operations()
    assert constructed.db is db
    assert constructed.ai_service.storage.root == storage.root
    assert constructed.ai_service.user_connections.db is db
    assert "app" not in sys.modules

    normal = enqueue()
    assert set(json.loads(job_for(normal)["payload_json"])) == {"run_id"}
    assert consumer.run_once(kinds=("ops_run",)) == 1
    assert service.get_run(*scope, normal["run_id"])["status"] == "succeeded"
    assert job_for(normal)["status"] == "completed"
    assert model.calls == 1

    # A persisted provider error finishes the dispatch, not an automatic retry.
    def fail_provider():
        raise ProviderFailure()

    model.respond = fail_provider
    failed = enqueue()
    consumer.run_once(kinds=("ops_run",))
    failed_result = service.get_run(*scope, failed["run_id"])
    assert failed_result["status"] == "failed" and failed_result["recoverable"]
    assert failed_result["error"]["upstream_status"] == 503
    assert job_for(failed)["status"] == "completed"
    called = model.calls
    assert consumer.run_once(kinds=("ops_run",)) == 0 and model.calls == called

    # An explicit retry changes job_id; a stale duplicate cannot execute it.
    model.respond = None
    service.retry(*scope, failed["run_id"], request_id="explicit-retry-1", auth_session_hash=token_hash)
    obsolete = db.enqueue_job(uid, "ops_run", "obsolete-dispatch", account_id=account["id"], payload={"run_id": failed["run_id"]})
    claimed = db.claim_job(obsolete["id"], consumer.owner)
    assert consumer.process(claimed) == "completed" and model.calls == called
    assert job_for(failed)["status"] == "queued"
    consumer.run_once(kinds=("ops_run",))
    assert service.get_run(*scope, failed["run_id"])["status"] == "succeeded"
    assert model.calls == called + 1

    # Queue metadata and payloads do not grant another shop access to the run.
    for label, sid, payload in (
        ("wrong-shop", other["id"], {"run_id": normal["run_id"]}),
        ("extra-field", account["id"], {"run_id": normal["run_id"], "message": "must-not-dispatch"}),
        ("fake-run", account["id"], {"run_id": "unknown-run"}),
    ):
        invalid = db.enqueue_job(uid, "ops_run", label, account_id=sid, payload=payload, max_attempts=1)
        called = model.calls
        consumer.run_once(kinds=("ops_run",))
        assert db.get_job(invalid["id"])["status"] == "dead_letter"
        assert db.get_job(invalid["id"])["last_error_code"] == "invalid_payload"
        assert model.calls == called

    # Yielding on a busy claim doesn't consume an attempt or mark a run done.
    deferred = enqueue()
    process_run = service.process_run
    service.process_run = lambda *_args, **_kwargs: {"status": "running"}
    try:
        claimed = db.claim_job(job_for(deferred)["id"], consumer.owner)
        assert consumer.process(claimed) == "deferred"
    finally:
        service.process_run = process_run
    row = job_for(deferred)
    assert row["status"] == "retry" and row["attempts"] == 0
    consumer.run_once(now=row["available_at"], kinds=("ops_run",))
    assert job_for(deferred)["status"] == "completed"

    # A lost queue lease cannot be acknowledged or dispatched by the old owner.
    fenced = enqueue()
    claimed = db.claim_job(job_for(fenced)["id"], consumer.owner)
    def steal_lease():
        with db._lock, db.con:
            db.con.execute("UPDATE jobs SET lease_owner='another-consumer' WHERE id=?", (claimed["id"],))
    model.respond = steal_lease
    assert consumer.process(claimed) == "lease_lost"
    assert db.get_job(claimed["id"])["lease_owner"] == "another-consumer"
    assert service.get_run(*scope, fenced["run_id"])["status"] == "running"
    model.respond = None
    with db._lock, db.con:
        db.con.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, claimed["id"]))
    consumer.run_once(kinds=("ops_run",))
    assert job_for(fenced)["status"] == "completed"

    # One waiting model must not block an independent shop-sync lane. A process
    # stop yields the live run, which a new consumer resumes durably.
    entered, release, synced = threading.Event(), threading.Event(), threading.Event()
    def slow_model():
        entered.set()
        assert release.wait(10), "test model gate was not released"
    def sync_without_network(cookie_header):
        synced.set()
        return snapshot_for(cookie_header)
    model.respond = slow_model
    pending = enqueue()
    consumer.sync_func = sync_without_network
    consumer.reserve_sync_func = lambda *_args: None
    thread = threading.Thread(target=consumer.run_forever, daemon=True)
    thread.start()
    try:
        assert entered.wait(5), "operations lane didn't start"
        sync_job = db.enqueue_job(uid, "shop_sync", "while-model-waits", account_id=other["id"], payload={"replace_cookie": False})
        assert synced.wait(5), "shop sync was blocked by a model request"
        consumer.stop()
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert db.get_job(sync_job["id"])["status"] == "completed"
    interrupted = job_for(pending)
    assert interrupted["status"] == "retry" and interrupted["attempts"] == 0
    assert service.get_run(*scope, pending["run_id"])["status"] == "running"
    model.respond = None
    resumed = JobConsumer(db, storage=storage, operations_service=service, owner="ops-resumed")
    resumed.run_once(now=interrupted["available_at"], kinds=("ops_run",))
    assert job_for(pending)["status"] == "completed"
    assert "app" not in sys.modules


if __name__ == "__main__":
    main()
