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
    "SAAS_XIANYU_LOGIN_COOLDOWN_SECONDS": "5",
    "SAAS_PLATFORM_AI_BASE_URL": "", "SAAS_PLATFORM_AI_KEY": "",
    "SAAS_COOKIE_SECURE": "0", "SAAS_PUBLIC_ORIGIN": "http://testserver",
    "SAAS_TRUSTED_HOSTS": "testserver",
})
sys.path.insert(0, str(ROOT / "backend"))
import app
import bot_manager
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


def expect_sync(code, callback, remaining=None):
    try:
        callback()
    except shop_sync.ShopSyncError as error:
        assert error.code == code, error.code
        if remaining is not None:
            assert math.ceil(error.retry_after) == remaining, error.retry_after
        return error
    raise AssertionError(f"expected {code}")


def check_worker_login_transition(first, clock, make_manager):
    """Keep the complete login/worker seam real; replace only external I/O."""
    def run_case(label, stale_pid, *, recognized=True):
        clock.sleep(61)
        uid = first.create_user(f"qr-worker-{label}", "offline-password")
        user = first.get_user_by_id(uid)
        account = first.ensure_default_shop_account(uid)
        account_id = account["id"]
        bot_manager.ensure_dir(uid, initialize=True)
        old_cookie = "unb=777777; _m_h5_tk=old_offline; cookie2=old"
        _, old_values = shop_sync.parse_cookie_header(old_cookie)
        old_snapshot = {
            "version": 1, "account_ref": shop_sync.account_ref(old_values),
            "nickname": "old-worker-shop", "products": [], "product_count": 0,
            "synced_at": "offline-old", "truncated": False,
        }
        old_auth = json.dumps({"version": 1, "code": "session_expired",
                               "reauthorization_required": True})
        app.write_secret(uid, "cookies.txt", old_cookie)
        shop_sync.save_snapshot(uid, old_snapshot)
        app.write_secret(uid, bot_manager.AUTH_STATUS_FILE, old_auth)
        saved_snapshot = app.read_secret(uid, "shop_snapshot.json")
        first.persist_worker_runtime(
            uid, account_id, desired_state="running", mode="rules", state="degraded",
            pid=stale_pid, generation=7, last_error="session_expired",
        )
        manager = make_manager(cooldown_seconds=0)
        events = []
        verified = {}

        class Process:
            pid = 900_000 + uid
            returncode = None

            def poll(self):
                return self.returncode

        process = Process()

        def verify(cookie):
            assert app.read_secret(uid, "cookies.txt") == old_cookie
            assert app.read_secret(uid, "shop_snapshot.json") == saved_snapshot
            assert app.read_secret(uid, bot_manager.AUTH_STATUS_FILE) == old_auth
            assert first.get_worker_runtime(uid, account_id)["pid"] == stale_pid
            _, values = shop_sync.parse_cookie_header(cookie)
            verified.update({**old_snapshot, "account_ref": shop_sync.account_ref(values),
                             "nickname": "new-worker-shop", "synced_at": "offline-new"})
            events.append("verified")
            return dict(verified)

        def spawn(worker_uid, mode, key, limits):
            assert (worker_uid, mode, key) == (uid, "rules", (uid, "default"))
            runtime = first.get_worker_runtime(uid, account_id)
            assert runtime["pid"] is None and runtime["state"] == "waiting_login"
            assert runtime["last_error"] == "login_replace:pid_reused"
            assert runtime["generation"] == 7 and runtime["desired_state"] == "running"
            assert "unb=123456" in app.read_secret(uid, "cookies.txt")
            assert shop_sync.load_verified_snapshot(uid) == verified
            assert shop_sync.load_sync_state(uid)["code"] == "verified"
            assert first.get_shop_account(uid, account_id=account_id)["status"] == "ready"
            auth = bot_manager.auth_status(uid)
            assert auth["code"] == "ok" and auth["reauthorization_required"] is False
            assert first.get_control_lease(f"worker-control:{uid}:{account_id}")["owner"]
            process._saas_resource_limits = dict(limits)
            events.append("spawned")
            return process, None, None

        with ExitStack() as stack:
            stack.enter_context(patch.object(app, "qr_logins", manager))
            stack.enter_context(patch.object(app, "sync_shop", verify))
            spawn_mock = stack.enter_context(patch.object(bot_manager, "_spawn_process", side_effect=spawn))
            guards = [
                stack.enter_context(patch.object(module, name, create=True,
                                                 side_effect=AssertionError("must not signal a reused or unknown PID")))
                for module, name in ((bot_manager.os, "kill"), (bot_manager.os, "killpg"),
                                     (bot_manager.os, "pidfd_open"), (bot_manager.signal, "pidfd_send_signal"))
            ]
            if not recognized:
                stack.enter_context(patch.object(bot_manager, "_pid_alive", return_value=True))
                stack.enter_context(patch.object(bot_manager, "_proc_cmdline", return_value=[]))
            try:
                login_id = app.start_xianyu_login(user, account)["login_id"]
                assert manager.poll(uid, login_id)["status"] == "confirmed"
                clock.sleep(shop_sync.REQUEST_INTERVAL)
                complete = lambda: app.complete_xianyu_login(
                    app.XianyuLoginCompleteIn(login_id=login_id), user, account)
                if recognized:
                    result = complete()
                    assert result["status"] == "connected" and result["connected"] is True
                    assert result["worker"] == {
                        "desired_running": True, "state": "running", "running": True, "code": "started",
                    }, result["worker"]
                    runtime = first.get_worker_runtime(uid, account_id)
                    assert runtime["pid"] == process.pid and runtime["state"] == "running"
                    assert runtime["generation"] == 8 and runtime["last_error"] == ""
                    assert events == ["verified", "spawned"]
                    spawn_mock.assert_called_once()
                else:
                    detail = expect_http("pid_mismatch", complete)
                    assert detail["can_retry_login"] is False
                    assert app.read_secret(uid, "cookies.txt") == old_cookie
                    assert app.read_secret(uid, "shop_snapshot.json") == saved_snapshot
                    assert app.read_secret(uid, bot_manager.AUTH_STATUS_FILE) == old_auth
                    runtime = first.get_worker_runtime(uid, account_id)
                    assert runtime["pid"] == stale_pid and runtime["state"] == "degraded"
                    assert runtime["last_error"] == "login_replace:pid_mismatch"
                    assert runtime["generation"] == 7 and runtime["desired_state"] == "running"
                    assert events == ["verified"]
                    spawn_mock.assert_not_called()
                assert first.get_control_lease(f"shop-connect:{uid}:{account_id}")["owner"] == ""
                assert first.get_control_lease(f"worker-control:{uid}:{account_id}")["owner"] == ""
                for guard in guards:
                    guard.assert_not_called()
            finally:
                process.returncode = 0
                bot_manager.stop(uid)
                manager.shutdown()

    run_case("api-pid", os.getpid())
    if sys.platform == "linux":
        failures = []

        def complete_from_api_thread():
            try:
                native_id = threading.get_native_id()
                assert native_id != os.getpid()
                # Real /proc data proves this TID belongs to the current API process.
                assert bot_manager._proc_tgid(native_id) == os.getpid()
                run_case("api-thread", native_id)
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=complete_from_api_thread)
        thread.start()
        thread.join(10)
        assert not thread.is_alive(), "login completion must not wait for its own API thread to exit"
        if failures:
            raise failures[0]
    # Unreadable unrelated identities remain fail-closed, even when alive.
    unknown_pid = 2_000_000_001
    assert bot_manager._proc_tgid(unknown_pid) is None
    run_case("unknown-pid", unknown_pid, recognized=False)


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

    def make_manager(coord=coordinator, cooldown_seconds=None):
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

        options = {} if cooldown_seconds is None else {"cooldown_seconds": cooldown_seconds}
        manager = xianyu_login.XianyuLoginManager(
            session_factory=factory, qr_factory=fixtures.svg_factory, clock=clock,
            poll_interval_seconds=0, sweep_interval_seconds=5,
            acquire_hook=coord.acquire_qr, renew_hook=coord.renew, release_hook=coord.release,
            before_request_hook=coord.before_request, after_request_hook=coord.after_request,
            **options,
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
    real_pause = app._pause_account_worker_for_login_replace
    real_autostart = app._autostart_account_worker
    try:
        with ExitStack() as stack:
            # Background sync remains 60s; explicit QR authorization keeps its own 5s limit.
            stack.enter_context(patch.dict(os.environ, {"SAAS_TESTING": "0"}))
            for module in (db_module, app, shop_sync, shop_sync_service, job_consumer):
                stack.enter_context(patch.object(module, "time", clock))
            stack.enter_context(patch.object(app, "sync_shop", snapshot))
            stack.enter_context(patch.object(app, "_pause_account_worker_for_login_replace", pause))
            stack.enter_context(patch.object(app, "_autostart_account_worker", resume))
            manager = make_manager()
            stack.enter_context(patch.object(app, "qr_logins", manager))
            assert shop_sync.SYNC_COOLDOWN_SECONDS == 60
            assert manager._cooldown == xianyu_login.DEFAULT_COOLDOWN_SECONDS == 5
            first.acquire_control_lease(sync_key, "old-check", lease_seconds=5, cooldown_seconds=60)
            expect_http("sync_busy", start, 5)
            assert not sessions and not requests
            first.release_control_lease(sync_key, "old-check")
            expect_sync("sync_cooldown", lambda: coordinator.acquire(uid, "default", "background-check", 150), 60)
            assert first.reserve_shop_connection(connect_key, "default-db-policy", sync_key) == {
                "code": "sync_cooldown", "retry_after": 60,
            }, "the database default must retain background sync cooldowns"
            assert not sessions and not requests
            assert first.get_control_lease(sync_key)["cooldown_until"] == 2_000_000_060

            # QR generation succeeds through the real route during that same cooldown.
            app.app.dependency_overrides[app.Auth.current_user] = lambda: user
            app.app.dependency_overrides[app.current_shop_account] = lambda: account
            headers = {
                "Origin": "http://testserver", "X-SaaS-Browser-Intent": "browser-write",
            }
            response = TestClient(app.app).post("/api/bot/login/start", headers=headers)
            assert response.status_code == 200, response.text
            early_id = response.json()["login_id"]
            assert b"<svg" in manager.qr_svg(uid, early_id)
            assert requests and first.get_control_lease(connect_key)["owner"] == early_id
            expect_sync("sync_busy", lambda: peer.acquire_qr(uid, "default", "peer-qr", 150))
            manager.cancel(uid, early_id)
            request_count = len(requests)
            response = TestClient(app.app).post("/api/bot/login/start", headers=headers)
            assert response.status_code == 429, response.text
            assert response.headers["retry-after"] == "5"
            assert response.json()["detail"]["code"] == "login_cooldown"
            assert response.json()["detail"]["retry_after"] == 5
            app.app.dependency_overrides.clear()
            clock.sleep(4)
            expect_http("login_cooldown", start, 1)
            assert len(requests) == request_count, "local QR cooldown performs no upstream request"
            clock.sleep(1)
            early_id = start()["login_id"]
            assert b"<svg" in manager.qr_svg(uid, early_id)
            manager.cancel(uid, early_id)
            assert first.get_control_lease(sync_key)["cooldown_until"] == 2_000_000_060
            expect_sync("sync_cooldown", lambda: coordinator.acquire(uid, "default", "background-check", 150), 55)

            # The remaining lease/concurrency cases isolate their own deadlines.
            manager = make_manager(cooldown_seconds=0)
            stack.enter_context(patch.object(app, "qr_logins", manager))

            shop_sync._trip_circuit()
            request_count = len(requests)
            expect_http("risk_cooldown", start, 600)
            assert len(requests) == request_count, "QR-specific acquire must still honor risk protection"
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
            peer_manager = make_manager(peer, cooldown_seconds=0)
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
            expect_sync("sync_cooldown", lambda: coordinator.acquire(uid, "default", "background-after-sync", 150), 60)
            post_sync_id = start()["login_id"]
            assert b"<svg" in manager.qr_svg(uid, post_sync_id)
            manager.cancel(uid, post_sync_id)
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
                    results.append(coord.acquire_qr(uid, "default", owner, 150))
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
            # An explicit login replace clears the background sync cooldown so a
            # following login start is not artificially blocked.

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
            with patch.object(app, "_pause_account_worker_for_login_replace", real_pause), \
                 patch.object(app, "_autostart_account_worker", real_autostart):
                check_worker_login_transition(first, clock, make_manager)
        print("qr-login-cooldown-contract: QR 5s/background 60s isolation, atomic preflight, dual-DB exclusion, 90/600s protection, generation, retry budget, verified swap and real worker transition passed")
    finally:
        app.app.dependency_overrides.clear()
        for manager in managers:
            manager.shutdown()
        second.con.close()


if __name__ == "__main__":
    main()
