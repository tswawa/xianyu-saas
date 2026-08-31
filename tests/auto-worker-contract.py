#!/usr/bin/env python3
"""Offline contracts for durable per-shop worker auto-start intent."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-auto-worker-"))
os.environ.update(
    {
        "SAAS_DB": str(RUN_DIR / "saas.db"),
        "SAAS_TENANTS_DIR": str(RUN_DIR / "tenants"),
        "SAAS_COOKIE_SECURE": "0",
        "SAAS_RESTORE_WORKERS": "0",
        "SAAS_TESTING": "1",
        "SAAS_ALLOW_REGISTRATION": "1",
        "SAAS_PLATFORM_AI_BASE_URL": "",
        "SAAS_PLATFORM_AI_MODEL": "",
        "SAAS_PLATFORM_AI_KEY": "",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import shop_sync  # noqa: E402


def fake_shop_sync(cookie_header: str) -> dict:
    _, cookies = shop_sync.parse_cookie_header(cookie_header)
    seller = cookies["unb"]
    return {
        "version": 1,
        "account_ref": shop_sync.account_ref(cookies),
        "nickname": f"自动店铺-{seller}",
        "products": [],
        "product_count": 0,
        "synced_at": "2026-08-20T12:00:00+0800",
        "truncated": False,
    }


def login(client: TestClient) -> int:
    app.db.create_user(
        "auto-worker-owner",
        "password-123",
        role="owner",
        initializer=app._new_user_initializer({}),
    )
    response = client.post(
        "/api/auth/login",
        json={"username": "auto-worker-owner", "password": "password-123"},
    )
    assert response.status_code == 200
    client.cookies.set(
        "xianyu_saas_session",
        response.cookies.get("xianyu_saas_session"),
        path="/",
    )
    user_id = int(app.db.get_user("auto-worker-owner")["id"])
    with app.db._lock:
        app.db.con.execute(
            "UPDATE users SET expires_at = 4102444800 WHERE id = ?", (user_id,)
        )
        app.db.con.commit()
    return user_id


class FakeQRLogins:
    def __init__(self, cookies: dict[str, str]):
        self.cookies = cookies
        self.finished = []
        self.counter = 0

    def start(self, user_id, account_key="default"):
        del user_id
        self.counter += 1
        del account_key
        return {
            "login_id": f"qr-auto-{self.counter:032d}",
            "status": "waiting",
            "expires_in": 150,
        }

    def begin_consume(self, user_id, login_id, account_key="default"):
        del user_id, login_id
        return self.cookies[account_key]

    def finish_consume(self, user_id, login_id, success, account_key="default"):
        del user_id
        self.finished.append((login_id, bool(success), account_key))

    def clear_user(self, *_args, **_kwargs):
        return None


def create_account(client: TestClient, key: str):
    response = client.post(
        "/api/bot/accounts", json={"key": key, "name": f"店铺-{key}"}
    )
    assert response.status_code == 200, response.text
    return response


def main() -> None:
    app.sync_shop = fake_shop_sync
    app.reserve_sync = lambda *_args: None
    client = TestClient(app.app)
    user_id = login(client)

    running: dict[str, int] = {}
    starts: dict[str, int] = {}
    next_pid = 56000
    failing_accounts = set()

    def fake_status(_user_id, account_key="default"):
        snapshot = shop_sync.load_verified_snapshot(user_id, account_key)
        return {
            "connected": snapshot is not None,
            "running": account_key in running,
        }

    def fake_start(_user_id, mode, account_key="default"):
        nonlocal next_pid
        del mode
        if account_key in failing_accounts:
            raise OSError("synthetic worker spawn failure")
        if account_key in running:
            return True, "already_running"
        next_pid += 1
        running[account_key] = next_pid
        starts[account_key] = starts.get(account_key, 0) + 1
        return True, "started"

    def fake_stop(_user_id, account_key="default"):
        if account_key not in running:
            return False, "not_running"
        running.pop(account_key, None)
        return True, "stopped"

    def fake_process_id(_user_id, account_key="default"):
        return running.get(account_key)

    with (
        patch.object(app, "bot_status", side_effect=fake_status),
        patch.object(app, "bot_start", side_effect=fake_start),
        patch.object(app, "bot_stop", side_effect=fake_stop),
        patch.object(app, "bot_process_id", side_effect=fake_process_id),
    ):
        # The compatibility default shop also receives a durable intent even
        # when it was created before account-scoped worker runtimes existed.
        default_account = app.db.ensure_default_shop_account(user_id)
        default_runtime = app.db.get_worker_runtime(user_id, default_account["id"])
        assert default_runtime["desired_state"] == "running"
        assert default_runtime["state"] == "waiting_login"
        default_cookie = "unb=610000; _m_h5_tk=default-token_tail; sid=default"
        default_connected = client.put("/api/bot/cookies", json={"cookies": default_cookie})
        assert default_connected.status_code == 200, default_connected.text
        assert default_connected.json()["worker"]["state"] == "running"
        assert starts["default"] == 1
        default_runtime = app.db.get_worker_runtime(user_id, default_account["id"])
        assert default_runtime["desired_state"] == "running"
        assert default_runtime["state"] == "running"

        # New shops persist running intent but wait for a verified login without
        # creating a process.
        create_account(client, "cookie-shop")
        cookie_account = app.db.get_shop_account(user_id, account_key="cookie-shop")
        runtime = app.db.get_worker_runtime(user_id, cookie_account["id"])
        assert runtime["desired_state"] == "running"
        assert runtime["state"] == "waiting_login"
        assert runtime["mode"] == "rules"
        assert runtime["pid"] is None
        assert "cookie-shop" not in running
        waiting_attention = client.get(
            "/api/bot/attention", headers={"X-Shop-Account": "cookie-shop"}
        )
        assert waiting_attention.status_code == 200
        assert all(
            item.get("kind") != "worker"
            for item in waiting_attention.json()["items"]
        )

        cookie = "unb=610001; _m_h5_tk=cookie-token_tail; sid=cookie"
        headers = {"X-Shop-Account": "cookie-shop"}
        connected = client.put("/api/bot/cookies", headers=headers, json={"cookies": cookie})
        assert connected.status_code == 200, connected.text
        assert connected.json()["worker"]["state"] == "running"
        assert starts["cookie-shop"] == 1
        runtime = app.db.get_worker_runtime(user_id, cookie_account["id"])
        assert runtime["desired_state"] == "running" and runtime["state"] == "running"

        # Repeating the verified Cookie is idempotent: bot_start may observe the
        # same process, but no second worker is spawned.
        repeated = client.put("/api/bot/cookies", headers=headers, json={"cookies": cookie})
        assert repeated.status_code == 200, repeated.text
        assert starts["cookie-shop"] == 1

        # Manual stop closes the durable intent. A later successful Cookie
        # replacement must not silently re-enable the worker.
        stopped = client.post("/api/bot/stop", headers=headers)
        assert stopped.status_code == 200, stopped.text
        stopped_runtime = app.db.get_worker_runtime(user_id, cookie_account["id"])
        assert stopped_runtime["desired_state"] == "stopped"
        refreshed = client.put(
            "/api/bot/cookies", headers=headers, json={"cookies": cookie}
        )
        assert refreshed.status_code == 200, refreshed.text
        assert starts["cookie-shop"] == 1
        assert refreshed.json()["worker"]["desired_running"] is False

        # A saved and verified Cookie is never rolled back when process launch
        # fails. The durable intent remains running but the observation degrades.
        create_account(client, "failed-shop")
        failed_account = app.db.get_shop_account(user_id, account_key="failed-shop")
        failed_headers = {"X-Shop-Account": "failed-shop"}
        app.write_secret(
            user_id,
            "auth_status.json",
            json.dumps(
                {
                    "code": "session_expired",
                    "reauthorization_required": True,
                    "updated_at": 123.0,
                    "secret": "must-not-leak",
                }
            ),
            "failed-shop",
        )
        safe_status = client.get("/api/bot/status", headers=failed_headers)
        assert safe_status.status_code == 200
        assert safe_status.json()["auth_code"] == "session_expired"
        assert safe_status.json()["auth_phase"] == "NEEDS_HUMAN"
        assert safe_status.json()["needs_human"] is True
        assert safe_status.json()["reauthorization_required"] is True
        assert safe_status.json()["runtime_state"] == "waiting_login"
        assert safe_status.json()["account"]["status"] == "expired"
        assert safe_status.json()["account"]["last_error_code"] == "session_expired"
        assert "must-not-leak" not in safe_status.text

        app.write_secret(
            user_id,
            "auth_status.json",
            json.dumps(
                {
                    "version": 2,
                    "phase": "NEEDS_HUMAN",
                    "code": "risk_control",
                    "failure_class": "NEEDS_HUMAN",
                    "needs_human": True,
                    "reauthorization_required": True,
                    "updated_at": 124.0,
                    "session": {"state": "SECURITY_CHECK", "updated_at": 124.0},
                    "mtop_token": {"state": "DEGRADED", "updated_at": 124.0},
                    "websocket": {"state": "DISCONNECTED", "updated_at": 124.0},
                }
            ),
            "failed-shop",
        )
        risk_status = client.get("/api/bot/status", headers=failed_headers)
        assert risk_status.status_code == 200
        assert risk_status.json()["auth_code"] == "risk_control"
        assert risk_status.json()["auth_phase"] == "NEEDS_HUMAN"
        assert risk_status.json()["account"]["status"] == "degraded"
        assert risk_status.json()["account"]["last_error_code"] == "risk_control"
        assert risk_status.json()["account"]["status"] != "restricted"
        listed_failed = next(
            item for item in client.get("/api/bot/accounts").json()["accounts"]
            if item["key"] == "failed-shop"
        )
        assert listed_failed["status"] == "degraded"
        assert listed_failed["last_error_code"] == "risk_control"

        failing_accounts.add("failed-shop")
        failed_cookie = "unb=610002; _m_h5_tk=failed-token_tail; sid=failed"
        failed = client.put(
            "/api/bot/cookies",
            headers=failed_headers,
            json={"cookies": failed_cookie},
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["connected"] is True
        assert failed.json()["worker"]["state"] == "degraded"
        failed_runtime = app.db.get_worker_runtime(user_id, failed_account["id"])
        assert failed_runtime["desired_state"] == "running"
        assert failed_runtime["state"] == "degraded"
        assert app.read_secret(user_id, "cookies.txt", "failed-shop") == failed_cookie
        assert app._read_auth_status(user_id, "failed-shop")["code"] == "ok"
        failing_accounts.remove("failed-shop")
        app.db.persist_worker_runtime(
            user_id,
            failed_account["id"],
            desired_state="stopped",
            mode="rules",
            state="stopped",
            pid=None,
            generation=int(failed_runtime["generation"] or 0),
            expected_generation=int(failed_runtime["generation"] or 0),
        )

        # Reconnecting by QR never asks the owner to stop an active worker.
        # The old worker keeps serving during QR validation, then the API
        # pauses it only for the verified Cookie swap and resumes its durable
        # running intent with the new login.
        create_account(client, "qr-shop")
        qr_headers = {"X-Shop-Account": "qr-shop"}
        old_qr_cookie = "unb=610003; _m_h5_tk=qr-old-token_tail; sid=qr-old"
        first_qr_connection = client.put(
            "/api/bot/cookies", headers=qr_headers, json={"cookies": old_qr_cookie}
        )
        assert first_qr_connection.status_code == 200, first_qr_connection.text
        assert starts["qr-shop"] == 1
        qr_cookie = "unb=610003; _m_h5_tk=qr-new-token_tail; sid=qr-new"
        fake_qr = FakeQRLogins({"qr-shop": qr_cookie})
        with patch.object(app, "qr_logins", fake_qr):
            qr_start = client.post("/api/bot/login/start", headers=qr_headers)
            assert qr_start.status_code == 200, qr_start.text
            qr_result = client.post(
                "/api/bot/login/complete",
                headers=qr_headers,
                json={"login_id": "qr-contract"},
            )
        assert qr_result.status_code == 200, qr_result.text
        assert qr_result.json()["worker"]["running"] is True
        assert starts["qr-shop"] == 2
        assert running["qr-shop"]
        assert app.read_secret(user_id, "cookies.txt", "qr-shop") == qr_cookie
        assert fake_qr.finished == [("qr-contract", True, "qr-shop")]


        create_account(client, "extension-shop")
        extension_headers = {"X-Shop-Account": "extension-shop"}
        handoff = client.post("/api/bot/connector/handoff", headers=extension_headers)
        assert handoff.status_code == 200, handoff.text
        extension_cookie = "unb=610004; _m_h5_tk=extension-token_tail; sid=extension"
        extension = TestClient(app.app).post(
            "/api/bot/connector/cookies",
            json={
                "handoff_token": handoff.json()["handoff_token"],
                "cookies": extension_cookie,
            },
        )
        assert extension.status_code == 200, extension.text
        assert starts["extension-shop"] == 1

        create_account(client, "sync-shop")
        sync_headers = {"X-Shop-Account": "sync-shop"}
        sync_cookie = "unb=610005; _m_h5_tk=sync-token_tail; sid=sync"
        app.write_secret(user_id, "cookies.txt", sync_cookie, "sync-shop")
        synced = client.post("/api/bot/shop/sync", headers=sync_headers)
        assert synced.status_code == 200, synced.text
        assert starts["sync-shop"] == 1

        # Verified login must not bypass fail-closed account controls. Missing
        # rules and corrupt settings preserve durable intent but never spawn.
        create_account(client, "missing-rules-shop")
        missing_rules_headers = {"X-Shop-Account": "missing-rules-shop"}
        missing_rules_account = app.db.get_shop_account(
            user_id, account_key="missing-rules-shop"
        )
        missing_rules_path = (
            RUN_DIR
            / "tenants"
            / str(user_id)
            / "accounts"
            / "missing-rules-shop"
            / "reply_rules.json"
        )
        missing_rules_path.unlink()
        missing_rules_login = client.put(
            "/api/bot/cookies",
            headers=missing_rules_headers,
            json={"cookies": "unb=610006; _m_h5_tk=missing-rules_tail; sid=missing-rules"},
        )
        assert missing_rules_login.status_code == 200, missing_rules_login.text
        assert missing_rules_login.json()["worker"]["code"] == "reply_rules_unavailable"
        assert missing_rules_login.json()["worker"]["state"] == "degraded"
        assert "missing-rules-shop" not in starts
        missing_rules_runtime = app.db.get_worker_runtime(
            user_id, missing_rules_account["id"]
        )
        assert missing_rules_runtime["desired_state"] == "running"
        assert missing_rules_runtime["last_error"] == "reply_rules_unavailable"

        create_account(client, "broken-settings-shop")
        broken_settings_headers = {"X-Shop-Account": "broken-settings-shop"}
        broken_settings_account = app.db.get_shop_account(
            user_id, account_key="broken-settings-shop"
        )
        broken_settings_path = (
            RUN_DIR
            / "tenants"
            / str(user_id)
            / "accounts"
            / "broken-settings-shop"
            / "automation_settings.json"
        )
        broken_settings_path.write_text("{broken", encoding="utf-8")
        os.chmod(broken_settings_path, 0o600)
        broken_settings_login = client.put(
            "/api/bot/cookies",
            headers=broken_settings_headers,
            json={"cookies": "unb=610007; _m_h5_tk=broken-settings_tail; sid=broken-settings"},
        )
        assert broken_settings_login.status_code == 200, broken_settings_login.text
        assert (
            broken_settings_login.json()["worker"]["code"]
            == "automation_settings_unavailable"
        )
        assert broken_settings_login.json()["worker"]["state"] == "degraded"
        assert "broken-settings-shop" not in starts
        broken_settings_runtime = app.db.get_worker_runtime(
            user_id, broken_settings_account["id"]
        )
        assert broken_settings_runtime["desired_state"] == "running"
        assert broken_settings_runtime["last_error"] == "automation_settings_unavailable"

        # API restart recovery validates controls before adopting a live PID.
        create_account(client, "restore-shop")
        restore_headers = {"X-Shop-Account": "restore-shop"}
        restore_cookie = "unb=610008; _m_h5_tk=restore-token_tail; sid=restore"
        restored_login = client.put(
            "/api/bot/cookies", headers=restore_headers, json={"cookies": restore_cookie}
        )
        assert restored_login.status_code == 200, restored_login.text
        restore_account = app.db.get_shop_account(user_id, account_key="restore-shop")
        restore_rules_path = (
            RUN_DIR
            / "tenants"
            / str(user_id)
            / "accounts"
            / "restore-shop"
            / "reply_rules.json"
        )
        saved_restore_rules = restore_rules_path.read_text(encoding="utf-8")
        restore_rules_path.unlink()
        restore_runtime = app.db.get_worker_runtime(user_id, restore_account["id"])
        restore_start_count = starts["restore-shop"]
        previous_restore_setting = os.environ.get("SAAS_RESTORE_WORKERS")
        os.environ["SAAS_RESTORE_WORKERS"] = "1"
        try:
            with (
                patch.object(
                    app.db, "list_worker_runtimes", return_value=[restore_runtime]
                ),
                patch.object(app, "bot_adopt") as blocked_restore_adopt,
            ):
                app.restore_desired_workers()
        finally:
            if previous_restore_setting is None:
                os.environ.pop("SAAS_RESTORE_WORKERS", None)
            else:
                os.environ["SAAS_RESTORE_WORKERS"] = previous_restore_setting
        blocked_restore_adopt.assert_not_called()
        assert starts["restore-shop"] == restore_start_count
        assert "restore-shop" not in running
        blocked_restore_runtime = app.db.get_worker_runtime(
            user_id, restore_account["id"]
        )
        assert blocked_restore_runtime["desired_state"] == "running"
        assert blocked_restore_runtime["state"] == "degraded"
        assert blocked_restore_runtime["pid"] is None
        assert blocked_restore_runtime["last_error"] == "reply_rules_unavailable"
        restore_rules_path.write_text(saved_restore_rules, encoding="utf-8")
        os.chmod(restore_rules_path, 0o600)

        # rules_ai recovery has the same control-file checks and additionally
        # requires a currently valid account-scoped AI reply configuration.
        resumed_restore = client.put(
            "/api/bot/cookies", headers=restore_headers, json={"cookies": restore_cookie}
        )
        assert resumed_restore.status_code == 200, resumed_restore.text
        ai_runtime = app.db.get_worker_runtime(user_id, restore_account["id"])
        ai_pid = running["restore-shop"]
        ai_runtime = app.db.persist_worker_runtime(
            user_id,
            restore_account["id"],
            desired_state="running",
            mode="rules_ai",
            state="running",
            pid=ai_pid,
            generation=int(ai_runtime["generation"] or 0),
            expected_generation=int(ai_runtime["generation"] or 0),
        )
        ai_restore_start_count = starts["restore-shop"]
        previous_restore_setting = os.environ.get("SAAS_RESTORE_WORKERS")
        os.environ["SAAS_RESTORE_WORKERS"] = "1"
        try:
            with (
                patch.object(app.db, "list_worker_runtimes", return_value=[ai_runtime]),
                patch.object(app.ai_service, "is_reply_ready", return_value=False),
                patch.object(app, "bot_adopt") as blocked_ai_adopt,
            ):
                app.restore_desired_workers()
        finally:
            if previous_restore_setting is None:
                os.environ.pop("SAAS_RESTORE_WORKERS", None)
            else:
                os.environ["SAAS_RESTORE_WORKERS"] = previous_restore_setting
        blocked_ai_adopt.assert_not_called()
        assert starts["restore-shop"] == ai_restore_start_count
        assert "restore-shop" not in running
        blocked_ai_runtime = app.db.get_worker_runtime(user_id, restore_account["id"])
        assert blocked_ai_runtime["desired_state"] == "running"
        assert blocked_ai_runtime["state"] == "degraded"
        assert blocked_ai_runtime["pid"] is None
        assert blocked_ai_runtime["mode"] == "rules_ai"
        assert blocked_ai_runtime["last_error"] == "ai_reply_not_ready"

    # Runtime initialization failure compensates both the account row and any
    # partially created runtime, leaving no ghost account in the control plane.
    def fail_initial_runtime(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic runtime initialization failure")

    with patch.object(app.db, "persist_worker_runtime", side_effect=fail_initial_runtime):
        failed_create = client.post(
            "/api/bot/accounts", json={"key": "ghost-shop", "name": "幽灵店铺"}
        )
    assert failed_create.status_code == 400
    assert app.db.get_shop_account(user_id, account_key="ghost-shop") is None
    ghost_path = RUN_DIR / "tenants" / str(user_id) / "accounts" / "ghost-shop"
    assert not ghost_path.exists(), "failed shop initialization must remove its private directory"
    account_ids = {
        int(row["id"])
        for row in app.db.list_shop_accounts(user_id, include_disabled=True)
    }
    assert all(int(row["account_id"]) in account_ids for row in app.db.list_worker_runtimes())

    print(
        "auto-worker contract: waiting intent, auth auto-start, stop fencing, "
        "degraded login preservation and route reuse passed"
    )


if __name__ == "__main__":
    main()
