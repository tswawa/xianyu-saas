#!/usr/bin/env python3
"""Offline QR/consumer contracts with real, independent SQLite connections."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUN = Path(tempfile.mkdtemp(prefix="qr-login-cooldown-"))
os.environ.update({
    "SAAS_DB": str(RUN / "control.db"), "SAAS_TENANTS_DIR": str(RUN / "tenants"),
    "SAAS_BOT_ROOT": str(RUN / "no-worker"), "SAAS_RESTORE_WORKERS": "0",
    "SAAS_TESTING": "1", "SAAS_SHOP_SYNC_COOLDOWN_SECONDS": "60",
    "SAAS_PLATFORM_AI_BASE_URL": "", "SAAS_PLATFORM_AI_KEY": "",
    "SAAS_COOKIE_SECURE": "0", "SAAS_PUBLIC_ORIGIN": "http://testserver",
    "SAAS_TRUSTED_HOSTS": "testserver",
})
sys.path.insert(0, str(ROOT / "backend"))
import app
import db as db_module
import job_consumer
import shop_sync
import shop_sync_service
import xianyu_login
from fastapi import HTTPException
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location("qr_fixtures", ROOT / "tests/xianyu-login-contract.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class Clock:
    def __init__(self):
        self.value = 2_000_000_000.0

    def __call__(self):
        return self.value

    time = monotonic = __call__

    def sleep(self, seconds):
        self.value += seconds

    def __getattr__(self, name):
        return getattr(time, name)


def expect_http(code, callback, remaining=None):
    try:
        callback()
    except HTTPException as error:
        assert error.detail["code"] == code, error.detail
        if remaining is not None:
            assert error.status_code == 429
            assert error.detail["retry_after"] == remaining, error.detail
            assert error.headers["Retry-After"] == str(remaining)
        return error.detail
    raise AssertionError(f"expected {code}")


def main():
    clock = Clock()
    first = app.db
    second = db_module.DB(str(RUN / "control.db"))
    uid = first.create_user("qr-cooldown-owner", "offline-password")
    user = first.get_user_by_id(uid)
    account = first.ensure_default_shop_account(uid)
    other = first.create_shop_account(uid, "other", "other")
    coordinator = shop_sync_service.ShopConnectionCoordinator(first)
    peer = shop_sync_service.ShopConnectionCoordinator(second)
    sync_key = f"shop-sync:{uid}:{account['id']}"
    connect_key = f"shop-connect:{uid}:{account['id']}"
    sessions, managers, requests = [], [], []
    events = []
    old_cookie = "unb=777777; _m_h5_tk=old_offline; cookie2=old"
    app.write_secret(uid, "cookies.txt", old_cookie, "default")
    app.write_secret(uid, "cookies.txt", old_cookie, "other")

    def make_manager(coord=coordinator):
        holder = {}

        class Session(fixtures.FakeSession):
            def request(self, method, url, **kwargs):
                assert not first.con.in_transaction and not second.con.in_transaction
                assert not holder["manager"]._lock._is_owned(), "no thread lock across HTTP"
                requests.append(url)
                return super().request(method, url, **kwargs)

        def factory():
            session = Session(["CONFIRMED"])
            sessions.append(session)
            return session

        manager = xianyu_login.XianyuLoginManager(
            session_factory=factory, qr_factory=fixtures.svg_factory, clock=clock,
            cooldown_seconds=0, poll_interval_seconds=0, sweep_interval_seconds=5,
            acquire_hook=coord.acquire, renew_hook=coord.renew, release_hook=coord.release,
            before_request_hook=coord.before_request, after_request_hook=coord.after_request,
        )
        holder["manager"] = manager
        managers.append(manager)
        return manager

    def snapshot(cookie):
        events.append("verify")
        assert app.read_secret(uid, "cookies.txt", "default") == old_cookie
        _, values = shop_sync.parse_cookie_header(cookie)
        return {"version": 1, "account_ref": shop_sync.account_ref(values),
                "nickname": "offline-new-shop", "products": [], "product_count": 0,
                "synced_at": "offline", "truncated": False}

    def pause(*_args):
        events.append("stop")
        assert app.read_secret(uid, "cookies.txt", "default") == old_cookie

    def resume(*_args):
        events.append("start")
        return {"running": False, "desired_running": True, "state": "stopped"}

    def complete(login_id):
        return app.complete_xianyu_login(app.XianyuLoginCompleteIn(login_id=login_id), user, account)

    def start():
        return app.start_xianyu_login(user, account)

    consumer = job_consumer.JobConsumer(second, sync_func=lambda _cookie: (_ for _ in ()).throw(
        AssertionError("old cookie must not reach platform during QR")), owner="offline-consumer")
    try:
        with ExitStack() as stack:
            # Production cooldown remains 60s. Only time and upstream are fake.
            stack.enter_context(patch.dict(os.environ, {"SAAS_TESTING": "0"}))
            for module in (db_module, app, shop_sync, shop_sync_service, job_consumer):
                stack.enter_context(patch.object(module, "time", clock))
            stack.enter_context(patch.object(app, "sync_shop", snapshot))
            stack.enter_context(patch.object(app, "_pause_account_worker_for_login_replace", pause))
            stack.enter_context(patch.object(app, "_autostart_account_worker", resume))
            manager = make_manager()
            stack.enter_context(patch.object(app, "qr_logins", manager))
            assert shop_sync.SYNC_COOLDOWN_SECONDS == 60
            first.acquire_control_lease(sync_key, "old-check", lease_seconds=5, cooldown_seconds=60)
            first.release_control_lease(sync_key, "old-check")
            expect_http("sync_cooldown", start, 60)
            assert not sessions and not requests
            clock.sleep(10.25)
            expect_http("sync_cooldown", start, 50)
            assert first.get_control_lease(sync_key)["cooldown_until"] == 2_000_000_060

            # Assert the actual HTTP envelope, not only an exception helper.
            app.app.dependency_overrides[app.Auth.current_user] = lambda: user
            app.app.dependency_overrides[app.current_shop_account] = lambda: account
            response = TestClient(app.app).post("/api/bot/login/start", headers={
                "Origin": "http://testserver", "X-SaaS-Browser-Intent": "browser-write",
            })
            assert response.status_code == 429, response.text
            assert response.headers["retry-after"] == "50"
            assert response.json()["detail"]["retry_after"] == 50
            app.app.dependency_overrides.clear()
            assert not sessions

            shop_sync._trip_circuit()
            expect_http("risk_cooldown", start, 600)
            assert not requests
            clock.sleep(601)
            login_id = start()["login_id"]
            assert first.get_control_lease("shop-sync:egress")["owner"] == ""
            assert first.get_control_lease(connect_key)["owner"] == login_id
            queued = first.enqueue_job(uid, "shop_sync", "qr-old-check", account_id=account["id"],
                                       payload={"replace_cookie": False}, max_attempts=1)
            for index in range(4):
                if index:
                    queued = first.enqueue_job(uid, "shop_sync", f"qr-old-check-{index}",
                                               account_id=account["id"], payload={"replace_cookie": False},
                                               max_attempts=1)
                row = second.claim_job(queued["id"], consumer.owner)
                assert row is not None
                assert consumer.process(row) == "deferred"
                deferred = first.get_job(queued["id"])
                assert deferred["attempts"] == 0 and deferred["status"] == "retry"
                assert deferred["available_at"] > clock()
            # Another shop is independent while this user is still scanning.
            peer_manager = make_manager(peer)
            other_id = peer_manager.start(uid, "other")["login_id"]
            peer_manager.cancel(uid, other_id, "other")
            manager.cancel(uid, login_id)
            assert first.get_control_lease(connect_key)["owner"] == ""

            # Cancel never waits on a thread lock owned by platform I/O. The
            # in-flight request retains its lease until its own cleanup returns.
            login_id = start()["login_id"]
            session = sessions[-1]
            entered, release = threading.Event(), threading.Event()
            original_request = session.request
            def blocked_request(method, url, **kwargs):
                if url == xianyu_login.QUERY_URL:
                    entered.set()
                    assert release.wait(3)
                return original_request(method, url, **kwargs)
            session.request = blocked_request
            outcomes = []
            def poll_cancelled():
                try:
                    manager.poll(uid, login_id)
                except xianyu_login.XianyuLoginError as error:
                    outcomes.append(error.code)
            thread = threading.Thread(target=poll_cancelled)
            thread.start()
            assert entered.wait(3)
            manager.cancel(uid, login_id)
            assert first.get_control_lease(connect_key)["owner"] == login_id
            assert not first.con.in_transaction and not second.con.in_transaction
            release.set()
            thread.join(3)
            assert not thread.is_alive() and outcomes == ["login_not_found"]
            assert first.get_control_lease(connect_key)["owner"] == ""
            assert session.closed

            # An already-running independent consumer blocks QR generation,
            # and keeps ownership through snapshot persistence, not just HTTP.
            entered, release = threading.Event(), threading.Event()
            def old_sync(cookie):
                assert cookie == old_cookie
                entered.set()
                assert release.wait(3)
                _, values = shop_sync.parse_cookie_header(cookie)
                return {"version": 1, "account_ref": shop_sync.account_ref(values),
                        "nickname": "old-shop", "products": [], "product_count": 0,
                        "synced_at": "offline", "truncated": False}
            consumer.sync_func = old_sync
            clock.sleep(shop_sync.REQUEST_INTERVAL)
            queued = first.enqueue_job(uid, "shop_sync", "old-detection-in-flight",
                                       account_id=account["id"], payload={"replace_cookie": False})
            row = second.claim_job(queued["id"], consumer.owner)
            outcomes = []
            thread = threading.Thread(target=lambda: outcomes.append(consumer.process(row)))
            thread.start()
            assert entered.wait(3)
            before = len(requests)
            expect_http("sync_busy", start, shop_sync.SYNC_MAX_SECONDS + 120)
            assert len(requests) == before
            release.set()
            thread.join(3)
            assert not thread.is_alive() and outcomes == ["completed"]
            expect_http("sync_cooldown", start, 60)
            clock.sleep(61)

            # Failure after reservation releases only this QR's ownership.
            with patch.object(manager, "_qr_factory", side_effect=RuntimeError("bad svg")):
                expect_http("platform_error", start)
            assert first.get_control_lease(connect_key)["owner"] == ""
            assert app.read_secret(uid, "cookies.txt", "default") == old_cookie

            # Concurrent SQLite reservations: exactly one wins before any HTTP.
            barrier, results = threading.Barrier(2), []
            def race(coord, owner):
                barrier.wait()
                try:
                    results.append(coord.acquire(uid, "default", owner, 150))
                except shop_sync.ShopSyncError as error:
                    results.append(error)
            threads = [threading.Thread(target=race, args=(coordinator, "race-a")),
                       threading.Thread(target=race, args=(peer, "race-b"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
                assert not thread.is_alive()
            winners = [result for result in results if isinstance(result, dict)]
            assert len(winners) == 1 and len(results) == 2, results
            coordinator.release(winners[0])

            # A dead API's bounded lease expires; late cleanup cannot release a successor.
            lost_id = start()["login_id"]
            old_lease = dict(manager._items[lost_id].connection_lease)
            clock.sleep(200)
            replacement_id = peer_manager.start(uid)["login_id"]
            coordinator.release(old_lease)
            assert first.get_control_lease(connect_key)["owner"] == replacement_id
            manager.clear_user(uid)
            assert first.get_control_lease(connect_key)["owner"] == replacement_id
            peer_manager.cancel(uid, replacement_id)

            # Confirming near the QR deadline creates a fresh, exactly 90s lifetime.
            login_id = start()["login_id"]
            clock.sleep(140)
            confirmed = manager.poll(uid, login_id)
            assert confirmed == {"login_id": login_id, "status": "confirmed", "expires_in": 90}
            confirm_deadline = manager._items[login_id].expires_at
            clock.sleep(shop_sync.REQUEST_INTERVAL)
            for seconds in (5, 3, 2, 1):
                # Short contention is retryable and does not consume attempts.
                second.acquire_control_lease("shop-sync:egress", "other-platform-call",
                                             lease_seconds=seconds, cooldown_seconds=0)
                detail = expect_http("sync_busy", lambda: complete(login_id), seconds)
                assert detail["can_retry_login"] is True
                assert type(detail["can_retry_login"]) is bool
                assert detail["login_expires_in"] == math.ceil(confirm_deadline - clock())
                assert app.read_secret(uid, "cookies.txt", "default") == old_cookie
                assert manager._items[login_id].expires_at == confirm_deadline
                with first._lock:
                    rows = first.con.execute("SELECT * FROM jobs WHERE kind = 'shop_sync_replace'").fetchall()
                assert len(rows) == 1 and rows[0]["attempts"] == 0 and rows[0]["status"] == "retry"
                clock.sleep(seconds)
            result = complete(login_id)
            assert result["status"] == "connected"
            assert events == ["verify", "stop", "start"], events
            assert "unb=123456" in app.read_secret(uid, "cookies.txt", "default")
            assert first.get_control_lease(connect_key)["owner"] == ""
            assert first.get_job(rows[0]["id"])["attempts"] == 1
            assert first.get_job(rows[0]["id"])["status"] == "completed"
            assert consumer.SUPPORTED_KINDS == ("shop_sync", "ops_run")
            before = len(requests)
            expect_http("sync_cooldown", start, 60)
            assert len(requests) == before

            # A rejected candidate cannot overwrite the previous ready account.
            clock.sleep(61)
            login_id = start()["login_id"]
            manager.poll(uid, login_id)
            clock.sleep(shop_sync.REQUEST_INTERVAL)
            saved_cookie = app.read_secret(uid, "cookies.txt", "default")
            saved_snapshot = app.read_secret(uid, "shop_snapshot.json", "default")
            with patch.object(app, "sync_shop", side_effect=shop_sync.ShopSyncError("cookie_expired", "expired")):
                detail = expect_http("cookie_expired", lambda: complete(login_id))
            assert detail["can_retry_login"] is False
            assert app.read_secret(uid, "cookies.txt", "default") == saved_cookie
            assert app.read_secret(uid, "shop_snapshot.json", "default") == saved_snapshot
            assert first.get_shop_account(uid, account_id=account["id"])["status"] == "ready"
            assert first.get_control_lease(connect_key)["owner"] == ""

            # An expired confirmed cookie cannot be reused, even with a valid old lease.
            clock.sleep(61)
            login_id = start()["login_id"]
            assert manager.poll(uid, login_id)["expires_in"] == 90
            clock.sleep(90)
            detail = expect_http("login_expired", lambda: complete(login_id))
            assert detail["login_expires_in"] == 0 and detail["can_retry_login"] is False
            assert first.get_control_lease(connect_key)["owner"] == ""

            # A newly tripped 600s protection outlives a 90s confirmed cookie.
            login_id = start()["login_id"]
            manager.poll(uid, login_id)
            shop_sync._trip_circuit()
            detail = expect_http("risk_cooldown", lambda: complete(login_id), 600)
            assert detail["login_expires_in"] == 90 and detail["can_retry_login"] is False
            manager.cancel(uid, login_id)
            clock.sleep(601)

            # Generation fencing survives the user's confirmation and rejects all writes.
            login_id = start()["login_id"]
            manager.poll(uid, login_id)
            saved = app.read_secret(uid, "cookies.txt", "default")
            with second._lock:
                second.con.execute("UPDATE shop_accounts SET generation = generation + 1 WHERE id = ?", (account["id"],))
                second.con.commit()
            detail = expect_http("login_expired", lambda: complete(login_id))
            assert detail["can_retry_login"] is False
            assert app.read_secret(uid, "cookies.txt", "default") == saved
            assert first.get_control_lease(connect_key)["owner"] == ""

            # Unknown completion failures are conservative, and release terminal sessions.
            account = first.get_shop_account(uid, account_id=account["id"])
            login_id = start()["login_id"]
            manager.poll(uid, login_id)
            with patch.object(app, "_run_shop_sync", side_effect=RuntimeError("private-error")):
                detail = expect_http("login_complete_failed", lambda: complete(login_id))
            assert detail["can_retry_login"] is False and "private-error" not in str(detail)
            assert first.get_control_lease(connect_key)["owner"] == ""
            assert app.read_secret(uid, "cookies.txt", "default") == saved

            # Expiry and cancel release without touching the durable cooldown/circuit.
            login_id = start()["login_id"]
            clock.sleep(151)
            assert manager.poll(uid, login_id)["status"] == "expired"
            assert first.get_control_lease(connect_key)["owner"] == ""
            assert shop_sync._circuit_until() > 0
            assert first.get_control_lease(sync_key)["cooldown_until"] > 0
        print("qr-login-cooldown-contract: atomic preflight, dual-DB exclusion, 60/90/600s protection, generation, retry budget and verified swap passed")
    finally:
        app.app.dependency_overrides.clear()
        for manager in managers:
            manager.shutdown()
        second.con.close()


if __name__ == "__main__":
    main()
