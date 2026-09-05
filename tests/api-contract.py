#!/usr/bin/env python3
"""Isolated API contract checks; never uses the production database."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = tempfile.mkdtemp(prefix="xianyu-saas-api-contract-")
DB_PATH = str(Path(RUN_DIR) / "saas.db")
TENANTS_PATH = str(Path(RUN_DIR) / "tenants")
os.environ.update(
    {
        "SAAS_DB": DB_PATH,
        "SAAS_TENANTS_DIR": TENANTS_PATH,
        "SAAS_COOKIE_SECURE": "0",
        "SAAS_ADMIN_TOKEN": "contract-admin",
        "SAAS_PLATFORM_AI_BASE_URL": "http://127.0.0.1:19991/v1",
        "SAAS_PLATFORM_AI_MODEL": "platform-contract-model",
        "SAAS_PLATFORM_AI_KEY": "legacy-fixture-ignored",
        "SAAS_AI_MASTER_KEY": base64.b64encode(b"a" * 32).decode("ascii"),
        "SAAS_AI_ALLOW_HTTP_LOCAL": "1",
        "SAAS_ALLOW_REGISTRATION": "1",
        "SAAS_PUBLIC_ORIGIN": "http://testserver",
        "SAAS_TRUSTED_HOSTS": "testserver",
        "SAAS_BROWSER_LOGS_MODE": "off",
        "SAAS_TESTING": "1",
        "SAAS_ACCESS_RECONCILE_SECONDS": "60",
    }
)
import sys

sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import bot_manager  # noqa: E402
import shop_sync  # noqa: E402
from platform_ai import identify_scope, issue_token, revoke_token  # noqa: E402
from version import ASSET_VERSION, VERSION  # noqa: E402


class UpstreamHandler(BaseHTTPRequestHandler):
    seen = {}

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        UpstreamHandler.seen = {
            "path": self.path,
            "authorization": self.headers.get("authorization", ""),
            "anthropic_key": self.headers.get("x-api-key", ""),
            "anthropic_version": self.headers.get("anthropic-version", ""),
            "google_key": self.headers.get("x-goog-api-key", ""),
            "payload": json.loads(body),
        }
        upstream_payload = UpstreamHandler.seen["payload"]
        is_reply_decision = "reply|handoff|no_reply" in json.dumps(upstream_payload, ensure_ascii=False)
        text = (
            json.dumps({"decision": "reply", "reply": "收到", "reason_code": "answered"}, ensure_ascii=False)
            if is_reply_decision
            else "收到"
        )
        if self.path.endswith("/responses"):
            payload = {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
        elif self.path.endswith("/messages"):
            payload = {"content": [{"type": "text", "text": text}]}
        elif self.path.endswith(":generateContent"):
            payload = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        elif self.path.endswith("/chat"):
            payload = {"message": {"role": "assistant", "content": text}}
        else:
            payload = {
                "id": "contract-response",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            }
        response = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_args):
        return


def mock_upstream_request(url, api_key, payload, headers):
    path = "/" + url.split("/", 3)[3] if "/" in url.split("://", 1)[-1] else "/"
    UpstreamHandler.seen = {
        "path": path,
        "authorization": headers.get("Authorization", ""),
        "anthropic_key": headers.get("x-api-key", ""),
        "anthropic_version": headers.get("anthropic-version", ""),
        "google_key": headers.get("x-goog-api-key", ""),
        "payload": payload,
    }
    upstream_payload = payload if isinstance(payload, dict) else {}
    is_reply_decision = "reply|handoff|no_reply" in json.dumps(upstream_payload, ensure_ascii=False)
    text = (
        json.dumps({"decision": "reply", "reply": "收到", "reason_code": "answered"}, ensure_ascii=False)
        if is_reply_decision
        else "收到"
    )
    if path.endswith("/responses"):
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
    if path.endswith("/messages"):
        return {"content": [{"type": "text", "text": text}]}
    if path.endswith(":generateContent"):
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    if path.endswith("/chat"):
        return {"message": {"role": "assistant", "content": text}}
    return {
        "id": "contract-response",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }


def fake_shop_sync(cookie_header):
    _, cookies = shop_sync.parse_cookie_header(cookie_header)
    return {
        "version": 1,
        "account_ref": shop_sync.account_ref(cookies),
        "nickname": "合同店铺",
        "products": [
            {
                "id": "100002",
                "title": "自动识别教程",
                "description": "来自店铺列表的简介",
                "price": "6.50",
                "status": "在售",
                "source": "cookie",
                "updated_at": "2026-08-15T10:02:00+0800",
            },
            {
                "id": "100004",
                "title": "自动化指南",
                "description": "暂无商品简介",
                "price": "12",
                "status": "已下架",
                "source": "cookie",
                "updated_at": "2026-08-15T10:01:00+0800",
            },
        ],
        "product_count": 2,
        "synced_at": "2026-08-15T10:02:00+0800",
        "truncated": False,
    }


def configure_ai_fixture(
    user_id,
    account,
    model="fixture-contract-model",
    api_key="fixture-ai-key",
    provider="openai_chat_completions",
    base_url="http://127.0.0.1:19991/v1",
):
    app.ai_service.resolver = lambda _host, port, type=None: [
        (None, None, None, None, ("93.184.216.34", port))
    ]
    scope = (int(user_id), int(account["id"]), str(account["account_key"]))
    revision = int(app.ai_service.get_connection(*scope)["revision"])
    tested = app.ai_service.test_connection(
        *scope,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        expected_revision=revision,
    )
    return app.ai_service.save_connection(
        *scope,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        verification_token=tested["verification_token"],
        expected_revision=revision,
    )


class FakeManagedProcess:
    next_pid = 20000

    def __init__(self):
        self.pid = self.next_pid
        type(self).next_pid += 1
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        self.returncode = 0
        return self.returncode


class FakeQrLogins:
    def __init__(self):
        self.counter = 0
        self.sessions = {}
        self.cleared_users = []
        self.finished = []

    def start(self, user_id):
        self.counter += 1
        login_id = f"qr-contract-{self.counter:032d}"
        self.sessions[login_id] = {"user_id": user_id, "polls": 0}
        return {"login_id": login_id, "status": "waiting", "expires_in": 150}

    def _get(self, user_id, login_id):
        item = self.sessions.get(login_id)
        if item is None or item["user_id"] != user_id:
            raise app.XianyuLoginError("login_not_found")
        return item

    def qr_svg(self, user_id, login_id):
        self._get(user_id, login_id)
        return b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h10v10z"/></svg>'

    def poll(self, user_id, login_id):
        item = self._get(user_id, login_id)
        item["polls"] += 1
        status = ("waiting", "scanned", "confirmed")[min(item["polls"] - 1, 2)]
        return {"login_id": login_id, "status": status, "expires_in": 149}

    def begin_consume(self, user_id, login_id):
        item = self._get(user_id, login_id)
        if item.get("consuming"):
            raise app.XianyuLoginError("login_busy")
        if item.get("polls", 0) < 3:
            raise app.XianyuLoginError("login_not_confirmed")
        item["consuming"] = True
        return "unb=123456; _m_h5_tk=qr-contract-secret_value; cookie2=contract"

    def finish_consume(self, user_id, login_id, success):
        item = self._get(user_id, login_id)
        assert item.get("consuming") is True
        item["consuming"] = False
        self.finished.append(bool(success))
        if success:
            self.sessions.pop(login_id, None)

    def cancel(self, user_id, login_id):
        self._get(user_id, login_id)
        self.sessions.pop(login_id, None)

    def clear_user(self, user_id, preserve_cooldown=False):
        del preserve_cooldown
        self.cleared_users.append(user_id)
        self.sessions = {
            login_id: item
            for login_id, item in self.sessions.items()
            if item["user_id"] != user_id
        }

    def clear(self):
        self.sessions.clear()

    def shutdown(self):
        self.clear()


def assert_bot_manager_access_contract():
    popen_calls = []
    issued_tokens = []
    revoked_tokens = []

    def fake_popen(argv, **kwargs):
        process = FakeManagedProcess()
        popen_calls.append({"argv": argv, "env": kwargs["env"], "process": process})
        return process

    def fake_issue(user_id):
        token = f"ai-worker-token-{user_id}"
        issued_tokens.append((user_id, token))
        return token

    def fake_revoke(user_id, token=None):
        revoked_tokens.append((user_id, token))

    rules_user = 99001
    ai_user = 99002
    invalid_user = 99003
    legacy_expires_at = 5000.0
    bot_manager.ensure_dir(rules_user, initialize=True)
    bot_manager.ensure_dir(ai_user, initialize=True)
    bot_manager.ensure_dir(invalid_user, initialize=True)
    invalid_rules_path = Path(bot_manager.tenant_dir(invalid_user)) / "reply_rules.json"
    invalid_rules_path.unlink()
    bot_manager.ensure_dir(invalid_user)
    assert not invalid_rules_path.exists(), "ensure_dir must never reseed an existing account"
    assert bot_manager._procs == {}
    try:
        with (
            patch.dict(
                os.environ,
                {
                    "API_KEY": "inherited-key-must-not-reach-free-worker",
                    "MODEL_BASE_URL": "https://inherited.invalid/v1",
                    "MODEL_NAME": "inherited-model",
                },
                clear=False,
            ),
            patch.object(bot_manager.subprocess, "Popen", fake_popen),
            patch.object(bot_manager, "issue_token", fake_issue),
            patch.object(bot_manager, "revoke_token", fake_revoke),
            patch.object(bot_manager.os, "getpgid", lambda pid: pid),
            patch.object(bot_manager.os, "killpg", lambda _pid, _signal: None),
        ):
            try:
                bot_manager.start(invalid_user)
                raise AssertionError("missing rules must fail closed before worker spawn")
            except OSError:
                pass
            assert popen_calls == []
            invalid_rules_path.write_text(
                bot_manager.INITIAL_ACCOUNT_FILES["reply_rules.json"], encoding="utf-8"
            )
            os.chmod(invalid_rules_path, 0o600)
            invalid_settings_path = Path(bot_manager.tenant_dir(invalid_user)) / "automation_settings.json"
            invalid_settings_path.write_text("{broken", encoding="utf-8")
            os.chmod(invalid_settings_path, 0o600)
            try:
                bot_manager.start(invalid_user)
                raise AssertionError("corrupt settings must fail closed before worker spawn")
            except OSError:
                pass
            assert popen_calls == []

            assert bot_manager.start(rules_user) == (True, "started")
            rules_env = popen_calls[-1]["env"]
            assert rules_env["AUTOMATION_MODE"] == "rules"
            assert rules_env["REPLY_RULES_FILE"].endswith(f"/{rules_user}/reply_rules.json")
            assert rules_env["AUTOMATION_SETTINGS_FILE"].endswith(f"/{rules_user}/automation_settings.json")
            assert rules_env["SIMULATE_HUMAN_TYPING"] == "False"
            assert all(key not in rules_env for key in ("API_KEY", "MODEL_BASE_URL", "MODEL_NAME"))
            assert issued_tokens == []
            rules_status = bot_manager.status(rules_user)
            assert rules_status["running"] is True
            assert rules_status["automation_mode"] == "rules"

            assert bot_manager.start(ai_user, "rules_ai") == (True, "started")
            ai_env = popen_calls[-1]["env"]
            assert ai_env["AUTOMATION_MODE"] == "rules_ai"
            assert ai_env["API_KEY"] == issued_tokens[-1][1]
            assert ai_env["MODEL_BASE_URL"] == bot_manager.PLATFORM_AI_BASE_URL
            assert ai_env["MODEL_NAME"] == "account-scoped"
            assert bot_manager.status(ai_user)["automation_mode"] == "rules_ai"

            expirations = {rules_user: 0, ai_user: legacy_expires_at}
            assert bot_manager._reconcile_access_modes(expirations.get, now=legacy_expires_at - 0.001) == {}
            assert bot_manager.status(ai_user)["automation_mode"] == "rules_ai"

            expirations[ai_user] = 0
            assert bot_manager._reconcile_access_modes(expirations.get, now=legacy_expires_at) == {}
            assert issued_tokens == [(ai_user, issued_tokens[0][1])]
            assert (ai_user, issued_tokens[0][1]) not in revoked_tokens
            unchanged_ai_status = bot_manager.status(ai_user)
            assert unchanged_ai_status["running"] is True
            assert unchanged_ai_status["automation_mode"] == "rules_ai"
    finally:
        with (
            patch.object(bot_manager, "revoke_token", fake_revoke),
            patch.object(bot_manager.os, "getpgid", lambda pid: pid),
            patch.object(bot_manager.os, "killpg", lambda _pid, _signal: None),
        ):
            bot_manager.shutdown_all()
        bot_manager._procs.clear()
        bot_manager._tokens.clear()
        bot_manager._modes.clear()
        for user_id in list(bot_manager._log_files):
            bot_manager._close_log(user_id)


def main():
    app.ai_service.requester = mock_upstream_request
    client = TestClient(app.app)
    fake_qr_logins = FakeQrLogins()
    app.qr_logins = fake_qr_logins
    app.sync_shop = fake_shop_sync
    app.reserve_sync = lambda _user_id: None
    bootstrap_admin_id = app.db.create_user(
        "contract-bootstrap-admin", "contract-bootstrap-password", role="admin"
    )
    app.db.set_platform_setting("registration_open", "1", bootstrap_admin_id)

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {"ok": True, "service": "xianyu-saas-api"}
    assert health.headers["cache-control"] == "no-store"
    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["x-frame-options"] == "DENY"
    assert health.headers["referrer-policy"] == "same-origin"
    assert client.get("/").json()["service"] == "xianyu-saas-api"
    ready = client.get("/api/ready")
    assert ready.status_code == 200
    assert ready.json() == {
        "ok": True,
        "service": "xianyu-saas-api",
        "database": "ready",
    }
    with patch.object(app.db, "is_ready", return_value=False):
        unavailable = client.get("/api/ready")
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "database_unavailable"
    assert client.get("/api/auth/capabilities").json() == {
        "registration_enabled": True,
        "first_registration_available": False,
        "bootstrap_available": False,
        "password_min_length": 12,
    }
    assert client.get("/api/version/public").json() == {
        "version": VERSION,
        "asset_version": ASSET_VERSION,
    }
    assert client.get("/api/version").status_code == 401
    assert client.get("/xianyu-saas/index.html").status_code == 404
    assert_bot_manager_access_contract()

    with patch.dict(os.environ, {"SAAS_TESTING": "1", "SAAS_ENV": "production"}, clear=False):
        try:
            app._assert_not_testing_in_production()
        except RuntimeError:
            pass
        else:
            raise AssertionError("production mode must reject SAAS_TESTING=1")

    with patch.dict(os.environ, {"SAAS_ALLOW_REGISTRATION": "0"}, clear=False):
        registration_disabled = client.post(
            "/api/auth/register",
            json={"username": "blocked-registration", "password": "password-123"},
        )
    assert registration_disabled.status_code == 403
    assert registration_disabled.json()["detail"]["code"] == "registration_disabled"

    failed_registration_path = {"value": None}

    original_write = app.AccountStorage.atomic_write_path

    def fail_registration_write(storage, path, data, **kwargs):
        if Path(path).name == "automation_settings.json":
            failed_registration_path["value"] = Path(path).parent
            raise OSError("synthetic account initialization failure")
        return original_write(storage, path, data, **kwargs)

    with patch.object(app.AccountStorage, "atomic_write_path", fail_registration_write):
        failed_registration = client.post(
            "/api/auth/register",
            json={"username": "failed-registration", "password": "password-123"},
        )
    assert failed_registration.status_code == 503
    assert app.db.get_user("failed-registration") is None
    assert failed_registration_path["value"] is not None
    assert not failed_registration_path["value"].exists()

    assert client.post("/api/auth/register", json={"username": "free-user", "password": "password-123"}).status_code == 200
    login = client.post("/api/auth/login", json={"username": "free-user", "password": "password-123"})
    assert login.status_code == 200
    # TestClient calls the backend without nginx's /xianyu-saas/ prefix, so
    # mirror the browser cookie under its direct API path for this check.
    client.cookies.set("xianyu_saas_session", login.cookies.get("xianyu_saas_session"), path="/")
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["plan"] == "free"
    assert me.json()["role"] == "owner"
    assert me.json()["role_label"] == "店主"
    assert me.json()["is_admin"] is False
    assert me.json()["platform_permissions"] == []
    assert set(me.json()["permissions"]) == {
        "shop.configure", "products.manage", "automation.rules", "automation.ai",
        "fulfillment.basic", "fulfillment.manage", "records.read", "runtime.logs",
        "analytics.read",
    }
    assert "token" not in me.json()
    version = client.get("/api/version")
    assert version.status_code == 200
    version_payload = version.json()
    assert version_payload["version"] == VERSION
    assert version_payload["asset_version"] == ASSET_VERSION
    assert version_payload["update_channel"] == "stable"
    assert set(version_payload) == {
        "version", "commit", "build_time", "asset_version", "update_channel",
        "release_notes", "latest_update",
    }
    assert ".git" not in json.dumps(version_payload, ensure_ascii=False)
    user_id = int(app.db.get_user("free-user")["id"])
    tenant_dir = Path(TENANTS_PATH) / str(user_id)
    rules_path = tenant_dir / "reply_rules.json"
    settings_path = tenant_dir / "automation_settings.json"
    assert json.loads(rules_path.read_text(encoding="utf-8")) == {"version": 1, "rules": []}
    assert json.loads(settings_path.read_text(encoding="utf-8"))["strategy"] == "standard"

    saved_rules_text = rules_path.read_text(encoding="utf-8")
    rules_path.unlink()
    missing_rules = client.get("/api/config")
    assert missing_rules.status_code == 503
    assert missing_rules.json()["detail"]["code"] == "reply_rules_unavailable"
    assert not rules_path.exists(), "normal reads must not silently reseed missing rules"
    rules_path.write_text("{broken", encoding="utf-8")
    os.chmod(rules_path, 0o600)
    broken_rules = client.get("/api/config")
    assert broken_rules.status_code == 503
    assert broken_rules.json()["detail"]["code"] == "reply_rules_unavailable"
    assert rules_path.read_text(encoding="utf-8") == "{broken"
    rules_path.write_text(saved_rules_text, encoding="utf-8")
    os.chmod(rules_path, 0o600)

    saved_settings_text = settings_path.read_text(encoding="utf-8")
    settings_path.unlink()
    missing_settings = client.get("/api/automation")
    assert missing_settings.status_code == 503
    assert missing_settings.json()["detail"]["code"] == "automation_settings_unavailable"
    assert not settings_path.exists(), "normal reads must not silently reseed missing settings"
    settings_path.write_text("{broken", encoding="utf-8")
    os.chmod(settings_path, 0o600)
    broken_settings = client.get("/api/automation")
    assert broken_settings.status_code == 503
    assert broken_settings.json()["detail"]["code"] == "automation_settings_unavailable"
    assert settings_path.read_text(encoding="utf-8") == "{broken"
    settings_path.write_text(saved_settings_text, encoding="utf-8")
    os.chmod(settings_path, 0o600)

    config = client.get("/api/config").json()
    assert "keywords_json" in config
    assert config["reply_rules"] == []
    assert "llm_base_url" not in config and "llm_model" not in config and "llm_api_key" not in config
    assert config["platform_ai"]["managed"] is False
    assert config["platform_ai"]["available"] is False

    # The primary shop login flow exposes only a same-origin QR image and
    # public states. Platform Cookie/token material remains server-side.
    qr_start = client.post("/api/bot/login/start")
    assert qr_start.status_code == 200
    qr_payload = qr_start.json()
    assert set(qr_payload) == {"login_id", "status", "expires_in"}
    assert qr_payload["status"] == "waiting"
    login_id = qr_payload["login_id"]
    qr_image = client.get(f"/api/bot/login/{login_id}/qr.svg")
    assert qr_image.status_code == 200
    assert qr_image.headers["content-type"].startswith("image/svg+xml")
    assert qr_image.headers["cache-control"] == "no-store"
    assert qr_image.headers["content-security-policy"] == "default-src 'none'"
    assert b"<script" not in qr_image.content.lower()
    assert client.get("/api/bot/login/qr-contract-missing-000000000000000/status").status_code == 404
    assert client.get(f"/api/bot/login/{login_id}/status").json()["status"] == "waiting"
    assert client.get(f"/api/bot/login/{login_id}/status").json()["status"] == "scanned"
    qr_confirmed = client.get(f"/api/bot/login/{login_id}/status")
    assert qr_confirmed.status_code == 200
    assert qr_confirmed.json()["status"] == "confirmed"
    serialized_qr_status = json.dumps(qr_confirmed.json(), ensure_ascii=False)
    assert "qr-contract-secret" not in serialized_qr_status
    assert "unb=" not in serialized_qr_status and "_m_h5_tk=" not in serialized_qr_status
    qr_connected = client.post("/api/bot/login/complete", json={"login_id": login_id})
    assert qr_connected.status_code == 200
    assert qr_connected.json()["status"] == "connected"
    serialized_qr_result = json.dumps(qr_connected.json(), ensure_ascii=False)
    assert "qr-contract-secret" not in serialized_qr_result
    assert "unb=" not in serialized_qr_result and "_m_h5_tk=" not in serialized_qr_result
    assert client.get(f"/api/bot/login/{login_id}/status").status_code == 404

    # A failed durable sync releases the confirmed login for a direct retry.
    retry_login_id = client.post("/api/bot/login/start").json()["login_id"]
    for _ in range(3):
        retry_status = client.get(f"/api/bot/login/{retry_login_id}/status")
    assert retry_status.json()["status"] == "confirmed"
    original_run_shop_sync = app._run_shop_sync

    def fail_shop_sync(*_args, **_kwargs):
        raise app.HTTPException(503, detail={"code": "network_error", "message": "暂时无法连接闲鱼"})

    app._run_shop_sync = fail_shop_sync
    failed_complete = client.post("/api/bot/login/complete", json={"login_id": retry_login_id})
    assert failed_complete.status_code == 503
    assert fake_qr_logins.sessions[retry_login_id]["consuming"] is False
    app._run_shop_sync = original_run_shop_sync
    retried_complete = client.post("/api/bot/login/complete", json={"login_id": retry_login_id})
    assert retried_complete.status_code == 200
    assert fake_qr_logins.finished[-2:] == [False, True]

    cancelled = client.post("/api/bot/login/start").json()["login_id"]
    assert client.post(f"/api/bot/login/{cancelled}/cancel").status_code == 200
    assert client.post(f"/api/bot/login/{cancelled}/cancel").status_code == 200

    # Free tier keeps only shop connection and simplified product cards.
    browser_write_headers = {
        "X-SaaS-Browser-Intent": "browser-write",
        "Origin": "http://testserver",
        "Referer": "http://testserver/xianyu-saas/",
    }
    with patch.dict(os.environ, {"SAAS_TESTING": "0"}, clear=False):
        missing_browser_guard = client.put(
            "/api/bot/cookies",
            json={"cookies": "unb=123456; _m_h5_tk=missing-guard"},
        )
        assert missing_browser_guard.status_code == 403
        assert missing_browser_guard.json()["detail"]["code"] == "browser_write_header_required"
        mismatched_browser_origin = client.put(
            "/api/bot/cookies",
            headers={**browser_write_headers, "Origin": "http://evil.test"},
            json={"cookies": "unb=123456; _m_h5_tk=wrong-origin"},
        )
        assert mismatched_browser_origin.status_code == 403
        assert mismatched_browser_origin.json()["detail"]["code"] == "browser_origin_mismatch"
        invalid_cookie = client.put(
            "/api/bot/cookies",
            headers=browser_write_headers,
            json={"cookies": ""},
        )
        assert invalid_cookie.status_code == 400
        assert invalid_cookie.json()["detail"]["code"] == "cookie_invalid"
        cookie_response = client.put(
            "/api/bot/cookies",
            headers=browser_write_headers,
            json={"cookies": "unb=123456; _m_h5_tk=contract-token_abc; sid=contract"},
        )
    assert cookie_response.status_code == 200
    assert cookie_response.json()["shop_name"] == "合同店铺"
    assert cookie_response.json()["product_count"] == 2
    # The block above deliberately runs with ``SAAS_TESTING=0`` to exercise the
    # browser-write guard, so that sync left a real 60s egress cooldown behind.
    # Later checks expect their own error classifier rather than that leftover
    # cooldown, so clear it for the sync leases only.
    app.db.con.execute(
        "UPDATE control_leases SET cooldown_until = 0 WHERE resource_key LIKE 'shop-sync:%'"
    )
    app.db.con.commit()
    verified_status = client.get("/api/bot/status").json()
    assert verified_status["sync_status"] == "verified"
    assert verified_status["shop_name"] == "合同店铺"
    assert verified_status["connection_state"] == "connected"
    assert verified_status["catalog_state"] == "ready"
    assert verified_status["capabilities"]["view_products"] is True
    assert verified_status["capabilities"]["publish_products"] is False
    accounts_response = client.get("/api/bot/accounts")
    assert accounts_response.status_code == 200
    accounts = accounts_response.json()["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["key"] == "default"
    assert accounts[0]["status"] == "ready"
    assert "account_ref" not in accounts[0]
    attention = client.get("/api/bot/attention")
    assert attention.status_code == 200
    attention_payload = attention.json()
    assert attention_payload["total"] == len(attention_payload["items"])
    assert attention_payload["pending_total"] + attention_payload["resolved_total"] == attention_payload["total"]
    assert all(
        set(item) == {
            "id", "kind", "code", "account_id", "error_code", "count", "title", "message",
            "severity", "action_view", "action_label", "resolved", "resolved_at",
        }
        for item in attention_payload["items"]
    )
    assert all(item["id"].startswith("att_") and len(item["id"]) == 28 for item in attention_payload["items"])
    assert "payload" not in json.dumps(attention_payload)
    assert "contract-token" not in json.dumps(attention_payload)
    if attention_payload["items"]:
        attention_id = attention_payload["items"][0]["id"]
        resolved_attention = client.put(
            f"/api/bot/attention/{attention_id}", json={"resolved": True}
        )
        assert resolved_attention.status_code == 200, resolved_attention.text
        resolved_item = next(item for item in resolved_attention.json()["items"] if item["id"] == attention_id)
        assert resolved_item["resolved"] is True
        assert resolved_item["resolved_at"] > 0
        persisted_item = next(
            item for item in client.get("/api/bot/attention").json()["items"]
            if item["id"] == attention_id
        )
        assert persisted_item["resolved"] is True
        reopened_attention = client.put(
            f"/api/bot/attention/{attention_id}", json={"resolved": False}
        )
        assert reopened_attention.status_code == 200
        reopened_item = next(item for item in reopened_attention.json()["items"] if item["id"] == attention_id)
        assert reopened_item["resolved"] is False
        assert reopened_item["resolved_at"] is None
    assert client.put("/api/bot/attention/att_000000000000000000000000", json={"resolved": True}).status_code == 404
    tenant_dir = Path(TENANTS_PATH) / str(app.db.get_user("free-user")["id"])
    state_file = tenant_dir / "shop_sync_state.json"
    assert json.loads(state_file.read_text())["code"] == "verified"
    assert "contract-token" not in state_file.read_text()
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert client.get("/api/bot/status").json()["cookie_status"]["code"] == "verified"
    assert client.get("/api/automation").json()["rules"] == []
    assert client.put(
        "/api/automation",
        json={"rules": [], "deliveries": [{"item_id": "999999", "material": "不应保存"}]},
    ).status_code == 400
    basic_automation = {
        "rules": [
            {
                "name": "使用咨询",
                "item_id": "100002",
                "keywords": ["怎么用", "教程"],
                "reply": "付款后会自动发送使用说明。",
            }
        ],
        "deliveries": [
            {
                "item_id": "100002",
                "enabled": True,
                "material": "使用说明：https://example.com/guide",
            }
        ],
    }
    saved_automation = client.put("/api/automation", json=basic_automation)
    assert saved_automation.status_code == 200
    assert saved_automation.json()["automation"]["rules"][0]["name"] == "使用咨询"
    assert saved_automation.json()["automation"]["rules"][0]["item_id"] == "100002"
    assert saved_automation.json()["automation"]["rules"][0]["keywords"] == ["怎么用", "教程"]
    assert saved_automation.json()["automation"]["deliveries"][0]["item_id"] == "100002"
    assert client.put(
        "/api/automation",
        json={"rules": [{"name": "   ", "keywords": ["空名"], "reply": "不应保存"}]},
    ).status_code == 400
    strategy_update = client.put("/api/automation", json={"strategy": "conservative"})
    assert strategy_update.status_code == 200
    assert strategy_update.json()["automation"]["strategy"] == "conservative"
    assert json.loads((tenant_dir / "automation_settings.json").read_text())["strategy"] == "conservative"
    assert (tenant_dir / "automation_settings.json").stat().st_mode & 0o777 == 0o600
    assert client.put("/api/automation", json={"strategy": "unknown"}).status_code == 400
    assert client.put("/api/automation", json={"strategy": "standard"}).status_code == 200
    gemini_settings = client.put("/api/automation", json={
        "first_reply": "欢迎光临，付款后自动发货。",
        "fallback_reply": "稍后店主会人工回复。",
        "delay_min_seconds": 2,
        "delay_max_seconds": 5,
        "trigger_cooldown_seconds": 2,
        "manual_takeover_cooldown_seconds": 30,
        "business_hours_enabled": True,
        "business_start": "09:00",
        "business_end": "23:30",
    })
    assert gemini_settings.status_code == 200
    assert gemini_settings.json()["automation"]["first_reply"] == "欢迎光临，付款后自动发货。"
    assert gemini_settings.json()["automation"]["fallback_reply"] == "稍后店主会人工回复。"
    assert gemini_settings.json()["automation"]["delay_max_seconds"] == 5
    assert gemini_settings.json()["automation"]["trigger_cooldown_seconds"] == 2
    assert gemini_settings.json()["automation"]["manual_takeover_cooldown_seconds"] == 30
    assert gemini_settings.json()["automation"]["business_hours_enabled"] is True
    partial_settings = client.put("/api/automation", json={"first_reply": "更新后的首次回复。"})
    assert partial_settings.status_code == 200
    partial_payload = partial_settings.json()["automation"]
    assert partial_payload["first_reply"] == "更新后的首次回复。"
    assert partial_payload["fallback_reply"] == "稍后店主会人工回复。"
    assert partial_payload["delay_min_seconds"] == 2
    assert partial_payload["delay_max_seconds"] == 5
    assert partial_payload["trigger_cooldown_seconds"] == 2
    assert partial_payload["manual_takeover_cooldown_seconds"] == 30
    assert partial_payload["business_start"] == "09:00"
    assert partial_payload["business_end"] == "23:30"

    automation_user = app.db.get_user("free-user")
    automation_account = app.db.get_shop_account(automation_user["id"], account_key="default")
    first_read_entered = threading.Event()
    release_first_read = threading.Event()
    original_read_settings = app._read_automation_settings
    read_calls = 0
    read_lock = threading.Lock()

    def block_first_settings_read(*args, **kwargs):
        nonlocal read_calls
        with read_lock:
            read_calls += 1
            current_call = read_calls
        if current_call == 1:
            first_read_entered.set()
            assert release_first_read.wait(2), "concurrent automation test timed out"
        return original_read_settings(*args, **kwargs)

    first_save = []

    def save_first_setting():
        first_save.append(app.save_automation(
            app.AutomationIn(first_reply="并发写入的首次回复"),
            user=automation_user,
            account=automation_account,
        ))

    with patch.object(app, "_read_automation_settings", side_effect=block_first_settings_read):
        first_thread = threading.Thread(target=save_first_setting)
        first_thread.start()
        assert first_read_entered.wait(2), "first automation save did not enter the merge"
        try:
            app.save_automation(
                app.AutomationIn(fallback_reply="并发写入的兜底回复"),
                user=automation_user,
                account=automation_account,
            )
        except app.HTTPException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("concurrent automation save must be rejected instead of racing")
        finally:
            release_first_read.set()
        first_thread.join(2)
    assert len(first_save) == 1 and first_save[0]["ok"] is True

    assert client.put("/api/automation", json={"delay_max_seconds": 61}).status_code == 400
    assert client.put("/api/automation", json={"trigger_cooldown_seconds": 301}).status_code == 400
    assert client.put("/api/automation", json={"business_start": "25:00"}).status_code == 400
    assert json.loads((tenant_dir / "reply_rules.json").read_text())["version"] == 1
    assert (tenant_dir / "reply_rules.json").stat().st_mode & 0o777 == 0o600
    saved_products = json.loads((tenant_dir / "products_config.json").read_text())
    assert saved_products["types"][0]["delivery"] == "material"
    assert saved_products["types"][0]["payload"].startswith("使用说明")
    products_payload = {
        "types": [
            {
                "id": "guide",
                "name": "DeepSeek 使用教程",
                "description": "包含配置步骤和常见问题处理方法",
                "price": "5.00",
                "item_ids": ["100002"],
                "delivery": "redeem",
            },
            {
                "id": "stickers",
                "name": "聊天表情包",
                "description": "适合日常聊天使用的表情素材",
                "price": "0.01",
                "item_id": "100004",
                "delivery": "pan",
            },
        ]
    }
    paid_products = client.put("/api/bot/products", json={"products": products_payload})
    assert paid_products.status_code == 400
    assert paid_products.json()["detail"] == "网盘商品必须配置资源匹配标签"

    # Enabled changes and explicit start/stop share one account-level worker
    # lease so a slow stop cannot be overtaken by a concurrent restart.
    free_user_row = app.db.get_user("free-user")
    free_account = app.db.get_shop_account(free_user_row["id"], account_key="default")
    worker_lease = f"worker-control:{int(free_user_row['id'])}:{int(free_account['id'])}"
    assert app.db.acquire_control_lease(
        worker_lease, "contract-holder", lease_seconds=45, cooldown_seconds=0
    ) == "acquired"
    assert client.post("/api/bot/stop").status_code == 409
    assert client.put("/api/automation", json={"enabled": False}).status_code == 409
    assert app.db.release_control_lease(worker_lease, "contract-holder") is True
    with (
        patch.object(app, "bot_stop", return_value=(False, "stop_timeout")),
        patch.object(app, "bot_process_id", return_value=42001),
    ):
        timed_out_stop = client.post("/api/bot/stop")
    assert timed_out_stop.status_code == 503
    assert isinstance(timed_out_stop.json()["detail"], dict), timed_out_stop.json()
    assert timed_out_stop.json()["detail"]["code"] == "stop_timeout"
    timeout_runtime = app.db.get_worker_runtime(free_user_row["id"], free_account["id"])
    assert timeout_runtime["desired_state"] == "stopped"
    assert timeout_runtime["state"] == "degraded"
    assert timeout_runtime["pid"] == 42001
    assert client.post("/api/bot/stop").status_code == 200
    stopped_runtime = app.db.get_worker_runtime(free_user_row["id"], free_account["id"])
    assert stopped_runtime["desired_state"] == "stopped"
    assert stopped_runtime["state"] == "stopped"
    assert stopped_runtime["pid"] is None

    # Explicit automation disable must also stop a durable worker owned by a
    # different API process, not just consult this process's in-memory map.
    disable_generation = int(stopped_runtime["generation"] or 0)
    app.db.persist_worker_runtime(
        free_user_row["id"],
        free_account["id"],
        desired_state="running",
        mode="rules",
        state="running",
        pid=42002,
        generation=disable_generation + 1,
        heartbeat_at=time.time(),
        expected_generation=disable_generation,
    )
    with (
        patch.object(app, "bot_process_id", return_value=None),
        patch.object(app, "bot_stop", return_value=(False, "not_running")),
        patch.object(app, "bot_terminate_pid", return_value=(True, "stopped")) as disable_remote_stop,
    ):
        disabled_automation = client.put("/api/automation", json={"enabled": False})
    assert disabled_automation.status_code == 200, disabled_automation.text
    disable_remote_stop.assert_called_once_with(free_user_row["id"], 42002, "default")
    disabled_runtime = app.db.get_worker_runtime(free_user_row["id"], free_account["id"])
    assert disabled_runtime["desired_state"] == "stopped"
    assert disabled_runtime["pid"] is None
    assert client.put("/api/automation", json={"enabled": True}).status_code == 200

    # Desired intent and observed runtime are one CAS-protected transaction.
    # The watchdog callback uses the same worker-control lease and publishes the
    # actual replacement PID/mode rather than leaving the old AI PID recoverable.
    runtime = app.db.get_worker_runtime(free_user_row["id"], free_account["id"])
    base_generation = int(runtime["generation"] or 0)
    prepared = app.db.persist_worker_runtime(
        free_user_row["id"],
        free_account["id"],
        desired_state="running",
        mode="rules_ai",
        state="running",
        pid=41001,
        generation=base_generation + 1,
        started_at=time.time(),
        heartbeat_at=time.time(),
        expected_generation=base_generation,
    )
    assert prepared is not None
    watchdog_reservation = app._reserve_watchdog_transition(
        free_user_row["id"], "default", 41001, "rules_ai", "rules"
    )
    assert watchdog_reservation is not None
    persisted_ok = app._persist_watchdog_transition(
        watchdog_reservation,
        free_user_row["id"],
        "default",
        "rules",
        41002,
        99,
        41001,
    )
    assert persisted_ok is True
    assert app.db.acquire_control_lease(
        worker_lease, "stop-racer", lease_seconds=45, cooldown_seconds=0
    ) != "acquired"
    app._release_watchdog_transition(watchdog_reservation)
    assert app.db.acquire_control_lease(
        worker_lease, "post-publish", lease_seconds=45, cooldown_seconds=0
    ) == "acquired"
    assert app.db.release_control_lease(worker_lease, "post-publish") is True
    downgraded_runtime = app.db.get_worker_runtime(free_user_row["id"], free_account["id"])
    assert downgraded_runtime["desired_state"] == "running"
    assert downgraded_runtime["mode"] == "rules"
    assert downgraded_runtime["state"] == "running"
    assert downgraded_runtime["pid"] == 41002
    assert int(downgraded_runtime["generation"]) == base_generation + 2
    stale_reservation = app._reserve_watchdog_transition(
        free_user_row["id"], "default", 41001, "rules_ai", "rules"
    )
    assert stale_reservation is None
    assert app.db.get_worker_runtime(free_user_row["id"], free_account["id"])["pid"] == 41002
    stale_write = app.db.persist_worker_runtime(
        free_user_row["id"],
        free_account["id"],
        desired_state="running",
        mode="rules_ai",
        state="running",
        pid=41003,
        generation=base_generation + 2,
        expected_generation=base_generation + 1,
    )
    assert stale_write is None
    assert app.db.get_worker_runtime(free_user_row["id"], free_account["id"])["pid"] == 41002
    app.db.persist_worker_runtime(
        free_user_row["id"],
        free_account["id"],
        desired_state="stopped",
        mode="rules",
        state="stopped",
        pid=None,
        generation=base_generation + 2,
        heartbeat_at=time.time(),
        expected_generation=base_generation + 2,
    )

    user_id = app.db.get_user("free-user")["id"]
    tenant_dir = Path(TENANTS_PATH) / str(user_id)
    with sqlite3.connect(tenant_dir / "chat_history.db") as con:
        con.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp DATETIME,
                chat_id TEXT,
                source_id TEXT
            );
            CREATE TABLE items (
                item_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                price REAL,
                description TEXT,
                last_updated DATETIME
            );
            """
        )
        con.executemany(
            "INSERT INTO messages(user_id, item_id, role, content, timestamp, chat_id, source_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("buyer-1", "100002", "user", "这个商品怎么使用？", "2026-08-15 10:00:00", "chat-1", "buyer:1"),
                ("buyer-1", "100002", "assistant", "付款后会发送使用说明。", "2026-08-15 10:01:00", "chat-1", "assistant:1"),
            ],
        )
        con.executemany(
            "INSERT INTO items(item_id, data, price, description, last_updated) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "100002",
                    json.dumps({"title": "DeepSeek 完整教程", "desc": "自动识别后的完整商品简介", "soldPrice": "6.50"}, ensure_ascii=False),
                    6.5,
                    "自动识别后的完整商品简介",
                    "2026-08-15T10:02:00",
                ),
                (
                    "100004",
                    json.dumps({"title": "店铺自动化指南", "desc": "从店铺缓存识别的新商品", "soldPrice": 12}, ensure_ascii=False),
                    12,
                    "从店铺缓存识别的新商品",
                    "2026-08-15T10:03:00",
                ),
            ],
        )

    products = client.get("/api/bot/products").json()["products"]
    assert len(products) == 2
    by_id = {item["id"]: item for item in products}
    assert by_id["100002"]["title"] == "自动识别教程"
    assert by_id["100002"]["description"] == "来自店铺列表的简介"
    assert by_id["100002"]["price_display"] == "¥6.5"
    assert by_id["100004"]["status"] == "已下架"

    # A failed check of the saved Cookie is surfaced as a stable status and
    # never replaces the previously verified Cookie or product snapshot.
    previous_cookie = bot_manager.read_secret(user_id, "cookies.txt")
    previous_snapshot = json.loads((tenant_dir / "shop_snapshot.json").read_text())
    def failed_shop_sync(_cookie_header):
        raise shop_sync.ShopSyncError("cookie_expired", "Cookie 已失效，请重新登录闲鱼后复制完整 Cookie")
    app.sync_shop = lambda _cookie_header: (_ for _ in ()).throw(
        shop_sync.ShopSyncError("risk_control", "闲鱼需要安全验证，请先在浏览器完成验证后再试")
    )
    risk_check = client.post("/api/bot/shop/sync")
    assert risk_check.status_code == 422
    assert risk_check.json()["detail"]["code"] == "risk_control"
    assert "安全验证" in risk_check.json()["detail"]["message"]
    risk_status = client.get("/api/bot/status").json()
    assert risk_status["sync_status"] == "risk_control"
    assert risk_status["cookie_status"]["label"] == "需要安全验证"
    assert risk_status["connected"] is False
    assert bot_manager.read_secret(user_id, "cookies.txt") == previous_cookie
    busy_error = app._shop_sync_http_error(
        shop_sync.ShopSyncError("platform_busy", "闲鱼当前请求繁忙，请稍后重试")
    )
    assert busy_error.status_code == 429
    assert busy_error.detail["code"] == "platform_busy"
    assert busy_error.detail["retryable"] is True
    assert json.loads((tenant_dir / "shop_snapshot.json").read_text()) == previous_snapshot

    app.sync_shop = lambda _cookie_header: (_ for _ in ()).throw(
        shop_sync.ShopSyncError("account_restricted", "闲鱼限制了当前账号的部分操作，暂时不能发布商品")
    )
    restricted_check = client.post("/api/bot/shop/sync")
    assert restricted_check.status_code == 422
    assert restricted_check.json()["detail"]["code"] == "account_restricted"
    restricted_status = client.get("/api/bot/status").json()
    assert restricted_status["connection_state"] == "connected"
    assert restricted_status["catalog_state"] == "stale"
    assert restricted_status["publish_state"] == "blocked"
    assert restricted_status["capabilities"]["publish_products"] is False
    assert restricted_status["attention"][0]["code"] == "account_restricted"

    app.sync_shop = failed_shop_sync
    expired_check = client.post("/api/bot/shop/sync")
    assert expired_check.status_code == 422
    assert expired_check.json()["detail"]["code"] == "cookie_expired"
    assert "重新登录闲鱼" in expired_check.json()["detail"]["message"]
    assert client.get("/api/bot/status").json()["cookie_status"]["code"] == "cookie_expired"

    app.sync_shop = fake_shop_sync
    assert client.post("/api/bot/shop/sync").status_code == 200
    assert client.get("/api/bot/status").json()["sync_status"] == "verified"

    # A failed replacement never replaces the previously verified Cookie or
    # downgrades its status: the user can safely retry with a new value.
    app.sync_shop = failed_shop_sync
    failed = client.put(
        "/api/bot/cookies",
        json={"cookies": "unb=999999; _m_h5_tk=expired-token_abc"},
    )
    assert failed.status_code == 422
    assert bot_manager.read_secret(user_id, "cookies.txt") == previous_cookie
    assert client.get("/api/bot/status").json()["sync_status"] == "verified"
    app.sync_shop = fake_shop_sync

    assert client.get("/api/membership/plans").status_code == 404

    saved_rules_text = rules_path.read_text(encoding="utf-8")
    rules_path.unlink()
    with patch.object(app, "bot_start") as missing_rules_start:
        missing_rules_worker = client.post("/api/bot/start", json={"mode": "rules"})
    assert missing_rules_worker.status_code == 503
    assert missing_rules_worker.json()["detail"]["code"] == "reply_rules_unavailable"
    missing_rules_start.assert_not_called()
    rules_path.write_text(saved_rules_text, encoding="utf-8")
    os.chmod(rules_path, 0o600)

    saved_settings_text = settings_path.read_text(encoding="utf-8")
    settings_path.write_text("{broken", encoding="utf-8")
    os.chmod(settings_path, 0o600)
    with patch.object(app, "bot_start") as broken_settings_start:
        broken_settings_worker = client.post("/api/bot/start", json={"mode": "rules"})
    assert broken_settings_worker.status_code == 503
    assert broken_settings_worker.json()["detail"]["code"] == "automation_settings_unavailable"
    broken_settings_start.assert_not_called()
    settings_path.write_text(saved_settings_text, encoding="utf-8")
    os.chmod(settings_path, 0o600)

    with (
        patch.object(app, "bot_start", return_value=(True, "started")),
        patch.object(app, "bot_stop", return_value=(True, "stopped")) as failed_start_stop,
        patch.object(app, "_persist_worker_started", side_effect=RuntimeError("persist failed")),
    ):
        failed_start = client.post("/api/bot/start", json={"mode": "rules"})
    assert failed_start.status_code == 503
    failed_start_stop.assert_called_once()
    failed_runtime = app.db.get_worker_runtime(free_user_row["id"], free_account["id"])
    assert failed_runtime["desired_state"] == "stopped"
    assert failed_runtime["state"] == "degraded"
    assert failed_runtime["pid"] is None

    # A non-owner API instance coordinates through the durable PID while it
    # holds worker-control: deterministic workers are adopted, mode changes are
    # pidfd-terminated before spawn, and stop cleans the remote owner process.
    remote_runtime = app.db.get_worker_runtime(free_user_row["id"], free_account["id"])
    remote_generation = int(remote_runtime["generation"] or 0)
    app.db.persist_worker_runtime(
        free_user_row["id"],
        free_account["id"],
        desired_state="running",
        mode="rules",
        state="running",
        pid=43001,
        generation=remote_generation + 1,
        started_at=time.time(),
        heartbeat_at=time.time(),
        expected_generation=remote_generation,
    )
    remote_process_ids = iter((None, 43001))
    with (
        patch.object(app, "bot_process_id", side_effect=lambda *_args: next(remote_process_ids)),
        patch.object(app, "bot_adopt", return_value=(True, "adopted")) as remote_adopt,
        patch.object(app, "bot_terminate_pid") as remote_adopt_terminate,
        patch.object(app, "bot_start", return_value=(True, "already_running")),
    ):
        adopted_start = client.post("/api/bot/start", json={"mode": "rules"})
    assert adopted_start.status_code == 200
    remote_adopt.assert_called_once_with(free_user_row["id"], 43001, "rules", "default")
    remote_adopt_terminate.assert_not_called()

    remote_runtime = app.db.get_worker_runtime(free_user_row["id"], free_account["id"])
    remote_generation = int(remote_runtime["generation"] or 0)
    app.db.persist_worker_runtime(
        free_user_row["id"],
        free_account["id"],
        desired_state="running",
        mode="rules_ai",
        state="running",
        pid=43002,
        generation=remote_generation + 1,
        started_at=time.time(),
        heartbeat_at=time.time(),
        expected_generation=remote_generation,
    )
    switched_process_ids = iter((None, 43003))
    with (
        patch.object(app, "bot_process_id", side_effect=lambda *_args: next(switched_process_ids)),
        patch.object(app, "bot_terminate_pid", return_value=(True, "stopped")) as remote_switch_stop,
        patch.object(app, "bot_start", return_value=(True, "started")),
    ):
        switched_start = client.post("/api/bot/start", json={"mode": "rules"})
    assert switched_start.status_code == 200
    remote_switch_stop.assert_called_once_with(free_user_row["id"], 43002, "default")
    assert app.db.get_worker_runtime(free_user_row["id"], free_account["id"])["pid"] == 43003

    remote_runtime = app.db.get_worker_runtime(free_user_row["id"], free_account["id"])
    remote_generation = int(remote_runtime["generation"] or 0)
    app.db.persist_worker_runtime(
        free_user_row["id"],
        free_account["id"],
        desired_state="running",
        mode="rules_ai",
        state="running",
        pid=43005,
        generation=remote_generation + 1,
        started_at=time.time(),
        heartbeat_at=time.time(),
        expected_generation=remote_generation,
    )
    with (
        patch.object(app, "bot_process_id", return_value=None),
        patch.object(
            app, "bot_terminate_pid", return_value=(False, "pidfd_unavailable")
        ) as unsafe_remote_stop,
        patch.object(app, "bot_start") as unsafe_remote_start,
    ):
        unsafe_start = client.post("/api/bot/start", json={"mode": "rules"})
    assert unsafe_start.status_code == 503
    unsafe_remote_stop.assert_called_once_with(free_user_row["id"], 43005, "default")
    unsafe_remote_start.assert_not_called()
    unsafe_runtime = app.db.get_worker_runtime(free_user_row["id"], free_account["id"])
    assert unsafe_runtime["desired_state"] == "stopped"
    assert unsafe_runtime["state"] == "degraded"
    assert unsafe_runtime["pid"] == 43005

    remote_runtime = app.db.get_worker_runtime(free_user_row["id"], free_account["id"])
    remote_generation = int(remote_runtime["generation"] or 0)
    app.db.persist_worker_runtime(
        free_user_row["id"],
        free_account["id"],
        desired_state="running",
        mode="rules",
        state="running",
        pid=43004,
        generation=remote_generation + 1,
        started_at=time.time(),
        heartbeat_at=time.time(),
        expected_generation=remote_generation,
    )
    with (
        patch.object(app, "bot_process_id", return_value=None),
        patch.object(app, "bot_stop", return_value=(False, "not_running")),
        patch.object(app, "bot_terminate_pid", return_value=(True, "stopped")) as remote_stop,
    ):
        remote_stop_response = client.post("/api/bot/stop")
    assert remote_stop_response.status_code == 200
    remote_stop.assert_called_once_with(free_user_row["id"], 43004, "default")
    remote_stopped_runtime = app.db.get_worker_runtime(free_user_row["id"], free_account["id"])
    assert remote_stopped_runtime["desired_state"] == "stopped"
    assert remote_stopped_runtime["pid"] is None

    original_bot_start = app.bot_start
    started_modes = []
    app.bot_start = lambda _user_id, _mode="rules": (started_modes.append(_mode) or True, "started")
    started = client.post("/api/bot/start")
    assert started.status_code == 200 and started.json()["ok"] is True
    assert started_modes[-1] == "rules"
    explicit_rules = client.post("/api/bot/start", json={"mode": "rules"})
    assert explicit_rules.status_code == 200 and started_modes[-1] == "rules"
    explicit_ai = client.post("/api/bot/start", json={"mode": "rules_ai"})
    assert explicit_ai.status_code == 409
    assert explicit_ai.json()["detail"]["code"] == "ai_connection_unavailable"
    assert started_modes[-1] == "rules"
    assert client.post("/api/bot/start", json={"mode": "unknown"}).status_code == 400
    app.bot_start = original_bot_start

    # Public connection APIs expose only the bounded provider catalog. Every
    # supported format can be tested, unknown formats are rejected, and saving
    # OpenAI Responses never returns the candidate key.
    app.ai_service.resolver = lambda _host, port, type=None: [
        (None, None, None, None, ("93.184.216.34", port))
    ]
    ai_connection = client.get("/api/bot/ai/connection")
    assert ai_connection.status_code == 200
    ai_connection_payload = ai_connection.json()
    assert ai_connection_payload["provider"] == "openai_chat_completions"
    assert {item["code"] for item in ai_connection_payload["providers"]} == {
        "openai_chat_completions",
        "openai_responses",
        "anthropic_messages",
        "google_gemini",
        "ollama_chat",
    }
    assert "api_key" not in ai_connection_payload
    rejected_provider = client.post(
        "/api/bot/ai/connection/test",
        json={
            "provider": "unknown_provider",
            "base_url": "http://127.0.0.1:19991/v1",
            "model": "fixture-model",
            "api_key": "unknown-key",
            "expected_revision": 0,
        },
    )
    assert rejected_provider.status_code == 400
    assert rejected_provider.json()["detail"]["code"] == "invalid_payload"

    provider_cases = {
        "openai_chat_completions": ("http://127.0.0.1:19991/v1", "chat-model", "key-chat", "/v1/chat/completions"),
        "openai_responses": ("http://127.0.0.1:19991/v1", "responses-model", "key-responses", "/v1/responses"),
        "anthropic_messages": ("http://127.0.0.1:19991/v1", "claude-model", "key-anthropic", "/v1/messages"),
        "google_gemini": ("http://127.0.0.1:19991/v1beta", "gemini-model", "key-gemini", "/v1beta/models/gemini-model:generateContent"),
        "ollama_chat": ("http://127.0.0.1:19991/api", "ollama-model", "", "/api/chat"),
    }
    provider_tokens = {}
    for provider, (base_url, model, api_key, expected_path) in provider_cases.items():
        tested_provider = client.post(
            "/api/bot/ai/connection/test",
            json={
                "provider": provider,
                "base_url": base_url,
                "model": model,
                "api_key": api_key,
                "expected_revision": 0,
            },
        )
        assert tested_provider.status_code == 200, tested_provider.text
        provider_tokens[provider] = tested_provider.json()["verification_token"]
        assert tested_provider.json()["provider"] == provider
        assert UpstreamHandler.seen["path"] == expected_path
        if provider == "openai_responses":
            assert UpstreamHandler.seen["payload"]["input"][0]["role"] == "system"
            assert UpstreamHandler.seen["payload"]["max_output_tokens"] == 256
        elif provider == "anthropic_messages":
            assert UpstreamHandler.seen["anthropic_key"] == api_key
            assert UpstreamHandler.seen["anthropic_version"] == "2023-06-01"
        elif provider == "google_gemini":
            assert UpstreamHandler.seen["google_key"] == api_key
        elif provider == "ollama_chat":
            assert UpstreamHandler.seen["authorization"] == ""

    responses_base_url, responses_model, responses_key, _ = provider_cases["openai_responses"]
    saved_responses = client.put(
        "/api/bot/ai/connection",
        json={
            "provider": "openai_responses",
            "base_url": responses_base_url,
            "model": responses_model,
            "api_key": responses_key,
            "verification_token": provider_tokens["openai_responses"],
            "expected_revision": 0,
        },
    )
    assert saved_responses.status_code == 200, saved_responses.text
    saved_responses_payload = saved_responses.json()
    assert saved_responses_payload["connection"]["provider"] == "openai_responses"
    assert "api_key" not in saved_responses_payload["connection"]
    assert responses_key not in saved_responses.text
    assert client.get("/api/bot/ai/connection").json()["provider"] == "openai_responses"

    # A new shop exposes safe persona defaults but no effective content or
    # enabled state. Only substantive v2 natural-language content can activate AI.
    initial_ai_config = client.get("/api/bot/ai/config")
    assert initial_ai_config.status_code == 200
    initial_config = initial_ai_config.json()["config"]
    assert initial_config["version"] == 2
    assert initial_config["status"] == "unconfigured"
    assert initial_config["persona_preset"] == "catgirl"
    assert initial_config["enabled"] is False
    assert initial_config["content_valid"] is False
    assert "draft" not in initial_config
    assert "published" not in initial_config
    assert "history" not in initial_config
    invalid_ai_config = client.put(
        "/api/bot/ai/config",
        json={
            "store_content": "……！？---",
            "expected_revision": 0,
        },
    )
    assert invalid_ai_config.status_code == 400
    assert invalid_ai_config.json()["detail"]["code"] == "invalid_payload"
    valid_store_content = "本店提供软件使用指导，只在闲鱼站内沟通。价格、库存和状态以实时商品事实为准。"
    saved_ai_config = client.put(
        "/api/bot/ai/config",
        json={
            "store_content": valid_store_content,
            "persona_preset": "friendly",
            "persona_name": "店铺客服",
            "tone": "friendly",
            "buyer_address": "亲",
            "reply_length": "short",
            "emoji_level": "low",
            "forbidden_claims": "永久稳定\n保证立即发货",
            "handoff_rules": "退款争议\n订单或付款状态无法核实时",
            "expected_revision": 0,
        },
    )
    assert saved_ai_config.status_code == 200, saved_ai_config.text
    saved_ai_payload = saved_ai_config.json()["config"]
    assert saved_ai_payload["status"] == "saved"
    assert saved_ai_payload["content_valid"] is True
    assert "draft" not in saved_ai_payload
    assert "published" not in saved_ai_payload
    assert "history" not in saved_ai_payload
    ai_status_payload = client.get("/api/bot/ai/status").json()
    assert ai_status_payload["active_config_revision"] == saved_ai_payload["revision"]
    assert "published_config_revision" not in ai_status_payload
    empty_product_content = client.put(
        "/api/bot/ai/products/100002/knowledge",
        json={"content": "   ", "expected_revision": 0},
    )
    assert empty_product_content.status_code == 400
    saved_product_content = client.put(
        "/api/bot/ai/products/100002/knowledge",
        json={
            "content": "适合新手使用，请按商品说明操作；异常问题转人工。",
            "expected_revision": 0,
        },
    )
    assert saved_product_content.status_code == 200, saved_product_content.text
    saved_knowledge_payload = saved_product_content.json()["knowledge"]
    assert saved_knowledge_payload["status"] == "saved"
    assert saved_knowledge_payload["content"].startswith("适合新手")
    assert "draft" not in saved_knowledge_payload
    assert "published" not in saved_knowledge_payload
    assert "history" not in saved_knowledge_payload
    before_organize_revision = saved_knowledge_payload["revision"]
    organized_product_content = client.post(
        "/api/bot/ai/products/100002/extract",
        json={"content": "适合新手，按页面步骤使用；遇到争议时转人工。"},
    )
    assert organized_product_content.status_code == 200, organized_product_content.text
    organized_payload = organized_product_content.json()
    assert organized_payload == {"content": "收到", "saved": False}
    assert "draft" not in organized_payload
    assert "published" not in organized_payload
    assert "history" not in organized_payload
    assert "raw_output" not in organized_payload
    after_organize = client.get("/api/bot/ai/products/100002/knowledge").json()["knowledge"]
    assert after_organize["revision"] == before_organize_revision
    assert client.get("/api/bot/ai/products/100002/versions").status_code == 404

    ai_started_modes = []
    with patch.object(
        app,
        "bot_start",
        side_effect=lambda _uid, mode="rules", *_args: (ai_started_modes.append(mode) or True, "started"),
    ):
        explicit_ai_ready = client.post("/api/bot/start", json={"mode": "rules_ai"})
    assert explicit_ai_ready.status_code == 200, explicit_ai_ready.text
    assert ai_started_modes == ["rules_ai"]

    assert client.get("/api/bot/messages").status_code == 200
    reply_before_takeover = client.post("/api/bot/messages/reply", json={"content": "人工回复", "chat_id": "chat-1"})
    assert reply_before_takeover.status_code == 409
    assert reply_before_takeover.json()["detail"]["code"] == "manual_takeover_required"
    assert client.put("/api/config", json={"keywords_json": "{}"}).status_code == 200
    assert client.put("/api/config", json={"llm_base_url": "https://attacker.invalid"}).status_code == 422

    assert client.post("/api/admin/codes", headers={"X-Admin-Token": "contract-admin"}, json={"count": 1, "days": 30}).status_code == 404
    assert client.post("/api/activate", json={"code": "legacy-code"}).status_code == 404
    user_id = app.db.get_user("free-user")["id"]
    assert client.post(
        f"/api/admin/users/{user_id}/extend",
        headers={"X-Admin-Token": "contract-admin"},
        json={"days": 30},
    ).status_code == 404
    expired_me = client.get("/api/me").json()
    assert expired_me["plan"] == "free"
    assert "automation.ai" in expired_me["permissions"]
    with app.db._lock:
        app.db.con.execute("UPDATE users SET expires_at = ? WHERE id = ?", (time.time() + 86400, user_id))
        app.db.con.commit()
    compatibility_me = client.get("/api/me").json()
    assert compatibility_me["plan"] == "member"
    assert compatibility_me["permissions"] == expired_me["permissions"]
    assert "keywords_json" in client.get("/api/config").json()
    assert client.put("/api/bot/products", json={"products": {"types": [{"delivery": "redeem", "item_ids": ["bad-id"]}]}}).status_code == 400
    assert client.put("/api/bot/products", json={"products": {"types": [{"delivery": "redeem", "item_ids": ["100002"]}]}}).status_code == 200
    assert json.loads((tenant_dir / "products_config.json").read_text())["types"][0]["item_ids"] == ["100002"]

    # Switching to a different seller account clears old fulfillment mappings.
    switched = client.put(
        "/api/bot/cookies",
        json={"cookies": "unb=999999; _m_h5_tk=new-token_abc; sid=contract"},
    )
    assert switched.status_code == 200
    assert json.loads((tenant_dir / "products_config.json").read_text()) == {"types": []}

    with sqlite3.connect(tenant_dir / "chat_history.db") as con:
        con.execute(
            "INSERT INTO messages(user_id, item_id, role, content, timestamp, chat_id, source_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("buyer-2", "100004", "user", "另一个会话的消息", "2026-08-15 11:00:00", "chat-2", "buyer:2"),
        )
        con.commit()

    conversations = client.get("/api/bot/conversations").json()["conversations"]
    assert [item["chat_id"] for item in conversations] == ["chat-2", "chat-1"]
    messages = client.get("/api/bot/messages").json()["messages"]
    assert [item["role"] for item in messages] == ["user"]
    assert messages[0]["chat_id"] == "chat-2"
    long_message = "完整正文 " + ("内容" * 100)
    with sqlite3.connect(tenant_dir / "chat_history.db") as con:
        con.execute("UPDATE messages SET content = ? WHERE chat_id = 'chat-1' AND role = 'user'", (long_message,))
        con.commit()
    chat_one = client.get("/api/bot/messages?chat_id=chat-1").json()["messages"]
    assert chat_one[0]["content"] == long_message
    assert client.post("/api/bot/messages/reply", json={"content": "缺少会话"}).status_code == 422
    takeover = client.post("/api/bot/conversations/chat-1/takeover", json={"enabled": True})
    assert takeover.status_code == 200
    reply_headers = {"Idempotency-Key": "contract-manual-reply-0001"}
    draft = client.post(
        "/api/bot/messages/reply",
        headers=reply_headers,
        json={"content": "我来帮你处理", "chat_id": "chat-1"},
    )
    assert draft.status_code == 200
    assert draft.json()["saved"] is True and draft.json()["delivered"] is False
    assert draft.json()["accepted"] is True
    assert draft.json()["platform_acknowledged"] is False
    assert draft.json()["message"]["role"] == "assistant_manual"
    assert draft.json()["message"]["delivery_status"] == "queued"
    assert draft.json()["message"]["item_id"] == "100002"
    with sqlite3.connect(tenant_dir / "chat_history.db") as con:
        assert con.execute("SELECT COUNT(*) FROM messages WHERE role = 'assistant_manual_draft'").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM manual_reply_drafts").fetchone()[0] == 1
        queued = con.execute(
            "SELECT status, attempts, recipient_id FROM manual_reply_drafts"
        ).fetchone()
        assert queued == ("queued", 0, "buyer-1")
    queued_message = client.get("/api/bot/messages?chat_id=chat-1").json()["messages"][-1]
    assert queued_message["role"] == "assistant_manual"
    assert queued_message["delivery_status"] == "queued"
    replay = client.post(
        "/api/bot/messages/reply",
        headers=reply_headers,
        json={"content": "我来帮你处理", "chat_id": "chat-1"},
    )
    assert replay.status_code == 200
    assert replay.json()["message"]["outbox_id"] == draft.json()["message"]["outbox_id"]
    conflict = client.post(
        "/api/bot/messages/reply",
        headers=reply_headers,
        json={"content": "另一条回复", "chat_id": "chat-1"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    reply_status = client.get("/api/bot/messages/reply/contract-manual-reply-0001")
    assert reply_status.status_code == 200
    assert reply_status.json()["reply"]["status"] == "queued"
    assert "content" not in reply_status.text and "我来帮你处理" not in reply_status.text
    assert client.post("/api/bot/messages/reply", json={"content": "错误会话", "chat_id": "missing"}).status_code == 409

    # The internal token is accepted only while present. The proxy resolves the
    # exact shop's saved provider connection and keeps the worker-facing protocol
    # stable while adapting the upstream request.
    internal_token = issue_token(user_id)
    internal = TestClient(app.app)
    missing_reply_scope = internal.post(
        "/internal/v1/ai/reply",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"message": "怎么使用？", "history": [], "item_id": "100002", "item_context": {}},
    )
    assert missing_reply_scope.status_code == 403
    internal_reply = internal.post(
        "/internal/v1/ai/reply",
        headers={
            "Authorization": f"Bearer {internal_token}",
            "X-Shop-Account": "default",
        },
        json={
            "message": "现在多少钱，怎么使用？",
            "history": [
                {"role": "user", "content": "我刚才问了版本"},
                {"role": "assistant", "content": "请问想了解哪个方面？"},
            ],
            "item_id": "100002",
            "item_context": {
                "title": "自动识别教程",
                "description": "来自 worker 的实时商品说明",
                "price": "6.50",
                "stock": 2,
                "status": "在售",
            },
            "recent_assistant_replies": ["上一条不同回复"],
        },
    )
    assert internal_reply.status_code == 200, internal_reply.text
    assert internal_reply.json() == {
        "decision": "reply",
        "reply": "收到",
        "reason_code": "answered",
        "sources": ["store_content", "real_time_product_facts", "product_content", "conversation_history"],
        "knowledge_status": "published",
        "config_revision": 1,
    }, internal_reply.json()
    assert "prompt" not in internal_reply.text
    assert responses_key not in internal_reply.text
    assert "raw" not in internal_reply.text
    assert UpstreamHandler.seen["payload"]["input"][-1]["role"] == "user"
    assert UpstreamHandler.seen["payload"]["input"][-1]["content"] == "现在多少钱，怎么使用？"
    ready_headers = {
        "Authorization": f"Bearer {internal_token}",
        "X-Shop-Account": "default",
    }
    ready = internal.post(
        "/internal/v1/ai/ready",
        headers=ready_headers,
        json={"expected_config_revision": internal_reply.json()["config_revision"]},
    )
    assert ready.status_code == 200, ready.text
    assert ready.json() == {"ok": True, "config_revision": 1}
    stale_ready = internal.post(
        "/internal/v1/ai/ready",
        headers=ready_headers,
        json={"expected_config_revision": internal_reply.json()["config_revision"] + 1},
    )
    assert stale_ready.status_code == 409, stale_ready.text
    assert stale_ready.json()["detail"]["code"] == "ai_disabled"
    assert internal.post(
        "/internal/v1/ai/ready", headers=ready_headers, json={}
    ).status_code == 400
    assert internal.post(
        "/internal/v1/ai/ready",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"expected_config_revision": 1},
    ).status_code == 403

    response = internal.post(
        "/internal/v1/chat/completions",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"model": "tenant-model", "stream": False, "messages": [{"role": "user", "content": "你好"}], "max_tokens": 40},
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "收到"
    assert UpstreamHandler.seen["path"] == "/v1/responses"
    assert UpstreamHandler.seen["payload"]["model"] == "responses-model"
    assert UpstreamHandler.seen["payload"]["input"][0]["content"] == "你好"
    assert UpstreamHandler.seen["authorization"] == "Bearer key-responses"
    assert identify_scope(internal_token) == (user_id, "default")

    secondary = app.db.create_shop_account(user_id, "secondary", "第二店铺")
    configure_ai_fixture(user_id, secondary, model="secondary-fixture-model", api_key="secondary-fixture-key")
    secondary_token = issue_token(user_id, "secondary")
    assert identify_scope(secondary_token) == (user_id, "secondary")
    scoped_response = internal.post(
        "/internal/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {secondary_token}",
            "X-Shop-Account": "secondary",
        },
        json={"messages": [{"role": "user", "content": "二号店"}]},
    )
    assert scoped_response.status_code == 200
    assert internal.post(
        "/internal/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {secondary_token}",
            "X-Shop-Account": "default",
        },
        json={"messages": [{"role": "user", "content": "错误作用域"}]},
    ).status_code == 403
    assert internal.post(
        "/internal/v1/chat/completions",
        headers={"Authorization": f"Bearer {secondary_token}"},
        json={"messages": [{"role": "user", "content": "缺少作用域"}]},
    ).status_code == 403
    app.db.update_shop_account(user_id, account_id=secondary["id"], enabled=False)
    assert internal.post(
        "/internal/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {secondary_token}",
            "X-Shop-Account": "secondary",
        },
        json={"messages": [{"role": "user", "content": "停用账号"}]},
    ).status_code == 403
    app.db.update_shop_account(user_id, account_id=secondary["id"], enabled=True)
    revoke_token(user_id, internal_token)
    revoke_token(user_id, secondary_token, "secondary")
    assert internal.post("/internal/v1/chat/completions", headers={"Authorization": f"Bearer {internal_token}"}, json={"messages": [{"role": "user", "content": "x"}]}).status_code == 401

    assert client.post("/api/auth/logout").status_code == 200
    assert user_id in fake_qr_logins.cleared_users
    assert client.get("/api/me").status_code == 401
    print("api-contract: self-use permissions, product-card, worker and AI proxy contracts passed")


if __name__ == "__main__":
    main()
