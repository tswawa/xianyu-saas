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
import bot_manager  # noqa: E402
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
        with patch.object(app, "bot_status", side_effect=app.worker_manager.status):
            risk_status = client.get("/api/bot/status", headers=failed_headers)
        assert risk_status.status_code == 200
        assert risk_status.json()["auth_code"] == "risk_control"
        assert risk_status.json()["auth_phase"] == "NEEDS_HUMAN"
        assert risk_status.json()["account"]["status"] == "degraded"
        assert risk_status.json()["account"]["last_error_code"] == "risk_control"
        assert risk_status.json()["account"]["status"] != "restricted"
        assert risk_status.json()["auth_layers"]["session"]["state"] == "UNKNOWN"
        assert risk_status.json()["needs_human"] is True
        assert risk_status.json()["cookie_status"]["label"] == "接口请求受限"
        listed_failed = next(
            item for item in client.get("/api/bot/accounts").json()["accounts"]
            if item["key"] == "failed-shop"
        )
        assert listed_failed["status"] == "degraded"
        assert listed_failed["last_error_code"] == "risk_control"

        # A verified cache followed by a legacy risk marker must remain stale,
        # protected and readable, not turn into an asserted App challenge.
        cached_cookie = "unb=610002; _m_h5_tk=cached-token_tail"
        app.write_secret(user_id, "cookies.txt", cached_cookie, "failed-shop")
        cached_snapshot = fake_shop_sync(cached_cookie)
        cached_snapshot["products"] = [{"id": "1001", "title": "缓存商品"}]
        cached_snapshot["product_count"] = 1
        shop_sync.save_snapshot(user_id, cached_snapshot, "failed-shop")
        shop_sync.save_sync_state(user_id, "verified", account_key="failed-shop")
        for version, code, nested in ((1, "risk_control", False), (2, "risk_control", False), (2, "risk_control", True), (2, "verification_required", False)):
            legacy_auth = {
                "version": version, "phase": "NEEDS_HUMAN", "code": code,
                "failure_class": "NEEDS_HUMAN", "needs_human": True,
                "reauthorization_required": True, "updated_at": 124.0,
                "session": {"state": "SECURITY_CHECK", "updated_at": 124.0},
                "mtop_token": {"state": "DEGRADED", "updated_at": 124.0},
                "websocket": {"state": "DISCONNECTED", "updated_at": 124.0},
                "message": "闲鱼App要求安全验证",
            }
            if nested:
                legacy_auth["failure"] = {"code": legacy_auth.pop("code"), "class": "NEEDS_HUMAN", "count": 3}
            encoded = json.dumps(legacy_auth, ensure_ascii=False)
            app.write_secret(user_id, "auth_status.json", encoded, "failed-shop")
            with patch.object(app, "bot_status", side_effect=app.worker_manager.status):
                observed = client.get("/api/bot/status", headers=failed_headers)
                alerts = client.get("/api/bot/attention", headers=failed_headers).json()["items"]
            payload = observed.json()
            assert payload["auth_code"] == code and payload["sync_status"] == code
            assert payload["needs_human"] is True and payload["reauthorization_required"] is True
            assert payload["runtime_state"] == "waiting_login"
            assert payload["account"]["status"] == "degraded"
            assert payload["connection_state"] == ("security_check" if code == "verification_required" else "degraded")
            assert payload["auth_layers"]["session"]["state"] == ("SECURITY_CHECK" if code == "verification_required" else "UNKNOWN")
            assert payload["connected"] is False and payload["catalog_state"] == "stale"
            assert payload["product_count"] == 1 and payload["capabilities"]["view_products"] is True
            assert payload["cookie_status"]["message"] == shop_sync.SYNC_STATUS_CATALOG[code]["message"]
            assert "闲鱼App要求安全验证" not in observed.text
            assert app.read_secret(user_id, "auth_status.json", "failed-shop") == encoded
            assert any(item["code"] == code and item["title"] == shop_sync.SYNC_STATUS_CATALOG[code]["label"] for item in alerts)

        # Display copy changes must not reopen an acknowledged alert or change
        # its ID/fingerprint, even if the raw provider still supplies old text.
        old_alert = {"code": "risk_control", "title": "需要安全验证", "message": "闲鱼App要求安全验证"}
        with patch.object(app, "bot_status", return_value={"attention": [old_alert]}):
            owner = app.db.get_user("auto-worker-owner")
            before = next(item for item in app._attention_payload(owner, failed_account)["_items"] if item["code"] == "risk_control")
            resolved = client.put(f"/api/bot/attention/{before['id']}", headers=failed_headers, json={"resolved": True})
            assert resolved.status_code == 200
            old_alert["title"] = "旧标题再次变化"
            old_alert["message"] = "另一个旧App弹窗文案"
            after = next(item for item in app._attention_payload(owner, failed_account)["_items"] if item["code"] == "risk_control")
            assert after["id"] == before["id"] and after["_fingerprint"] == before["_fingerprint"]
            assert after["resolved"] is True and after["resolved_at"] > 0
            assert after["title"] == "接口请求受限"
            assert after["message"] == shop_sync.SYNC_STATUS_CATALOG["risk_control"]["message"]
        for code in ("risk_control", "risk_cooldown", "verification_required"):
            for kind in ("shop_account", "worker"):
                shown = app._attention_display({"kind": kind, "code": "degraded", "error_code": code, "title": "需要安全验证", "message": "闲鱼App要求安全验证"})
                assert shown["title"] == shop_sync.SYNC_STATUS_CATALOG[code]["label"]
                assert shown["message"] == shop_sync.SYNC_STATUS_CATALOG[code]["message"]

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


        create_account(client, "sync-shop")
        sync_headers = {"X-Shop-Account": "sync-shop"}
        sync_cookie = "unb=610005; _m_h5_tk=sync-token_tail; sid=sync"
        app.write_secret(user_id, "cookies.txt", sync_cookie, "sync-shop")
        synced = client.post("/api/bot/shop/sync", headers=sync_headers)
        assert synced.status_code == 200, synced.text
        assert starts["sync-shop"] == 1

        # Paid-order delivery is independent of the automatic reply switch.
        # Exercise public control routes and durable recovery for every type.
        create_account(client, "delivery-shop")
        delivery_headers = {"X-Shop-Account": "delivery-shop"}
        delivery_cookie = "unb=610009; _m_h5_tk=delivery-token_tail; sid=delivery"
        delivery_login = client.put(
            "/api/bot/cookies", headers=delivery_headers, json={"cookies": delivery_cookie}
        )
        assert delivery_login.status_code == 200, delivery_login.text
        delivery_snapshot = fake_shop_sync(delivery_cookie)
        delivery_snapshot.update(products=[{"id": "100009", "title": "发货商品"}], product_count=1)
        shop_sync.save_snapshot(user_id, delivery_snapshot, "delivery-shop")
        delivery_account = app.db.get_shop_account(user_id, account_key="delivery-shop")
        delivery_configs = [
            {"delivery": "material", "payload": "测试订单资料"},
            {"delivery": "pan", "resource_match": ["测试资源"]},
            {"delivery": "redeem"},
        ]
        for config in delivery_configs:
            app.write_secret(user_id, "products_config.json", json.dumps({
                "version": 1, "types": [{**config, "item_ids": ["100009"], "enabled": True}],
            }), "delivery-shop")
            assert client.put("/api/automation", headers=delivery_headers, json={"enabled": True}).status_code == 200
            started = client.post("/api/bot/start", headers=delivery_headers, json={"mode": "rules"})
            assert started.status_code == 200, started.text
            delivery_pid = running["delivery-shop"]
            disabled_replies = client.put("/api/automation", headers=delivery_headers, json={"enabled": False})
            assert disabled_replies.status_code == 200, disabled_replies.text
            assert disabled_replies.json()["automation"]["enabled"] is False
            assert running["delivery-shop"] == delivery_pid
            delivery_runtime = app.db.get_worker_runtime(user_id, delivery_account["id"])
            assert delivery_runtime["desired_state"] == "running"
            assert delivery_runtime["mode"] == "rules"
            assert bot_manager._worker_automation_enabled(user_id, "delivery-shop") is True

            # A dead listener must recover with replies still off, using the
            # existing desired state and account-scoped rules mode.
            running.pop("delivery-shop")
            previous_starts = starts["delivery-shop"]
            with (
                patch.dict(os.environ, {"SAAS_RESTORE_WORKERS": "1"}),
                patch.object(app.db, "list_worker_runtimes", return_value=[delivery_runtime]),
                patch.object(app, "bot_adopt", return_value=(False, "pid_dead")),
            ):
                app.restore_desired_workers()
            assert starts["delivery-shop"] == previous_starts + 1
            recovered = app.db.get_worker_runtime(user_id, delivery_account["id"])
            assert recovered["state"] == "running" and recovered["mode"] == "rules"
            assert app._read_automation_settings(user_id, "delivery-shop")["enabled"] is False

            # Explicit stop fences recovery even though the delivery remains
            # enabled. Only a new explicit start reopens the listener.
            stopped_delivery = client.post("/api/bot/stop", headers=delivery_headers)
            assert stopped_delivery.status_code == 200, stopped_delivery.text
            stopped_runtime = app.db.get_worker_runtime(user_id, delivery_account["id"])
            with (
                patch.dict(os.environ, {"SAAS_RESTORE_WORKERS": "1"}),
                patch.object(app.db, "list_worker_runtimes", return_value=[stopped_runtime]),
            ):
                app.restore_desired_workers()
            assert "delivery-shop" not in running
            assert starts["delivery-shop"] == previous_starts + 1
            restarted = client.post("/api/bot/start", headers=delivery_headers, json={"mode": "rules"})
            assert restarted.status_code == 200, restarted.text
            assert app._read_automation_settings(user_id, "delivery-shop")["enabled"] is False
            assert client.post("/api/bot/stop", headers=delivery_headers).status_code == 200

        # Disabled, incomplete and foreign-shop bindings cannot authorize a
        # delivery-only start. Strict document errors still fail closed.
        for invalid_config in (
            {"delivery": "material", "item_ids": ["100009"], "payload": "资料", "enabled": False},
            {"delivery": "material", "item_ids": ["100009"], "payload": "   "},
            {"delivery": "material", "item_ids": ["999999"], "payload": "其他店铺资料"},
            {"delivery": "pan", "item_ids": ["100009"], "resource_match": []},
            {"delivery": "redeem", "item_ids": []},
        ):
            app.write_secret(user_id, "products_config.json", json.dumps({
                "version": 1, "types": [invalid_config],
            }), "delivery-shop")
            denied_delivery = client.post("/api/bot/start", headers=delivery_headers, json={"mode": "rules"})
            assert denied_delivery.status_code == 409, denied_delivery.text
            assert denied_delivery.json()["detail"]["code"] == "automation_disabled"
            assert "delivery-shop" not in running
            assert bot_manager._worker_automation_enabled(user_id, "delivery-shop") is False
        app.write_secret(user_id, "products_config.json", "{broken", "delivery-shop")
        corrupt_delivery = client.post("/api/bot/start", headers=delivery_headers, json={"mode": "rules"})
        assert corrupt_delivery.status_code == 503, corrupt_delivery.text
        app.write_secret(user_id, "products_config.json", '{"version":1,"types":[]}', "delivery-shop")

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
