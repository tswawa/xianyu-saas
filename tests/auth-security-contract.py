#!/usr/bin/env python3
"""Password, session, throttling and secret-redaction contracts."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-saas-auth-security-"))
os.environ.update(
    {
        "SAAS_DB": str(RUN_DIR / "saas.db"),
        "SAAS_TENANTS_DIR": str(RUN_DIR / "tenants"),
        "SAAS_COOKIE_SECURE": "0",
        "SAAS_ALLOW_REGISTRATION": "0",
        "SAAS_PUBLIC_ORIGIN": "http://testserver",
        "SAAS_TRUSTED_HOSTS": "testserver",
        "SAAS_TESTING": "1",
        "SAAS_RESTORE_WORKERS": "0",
        "SAAS_MAX_ACTIVE_SESSIONS": "2",
        "SAAS_AUDIT_HMAC_KEY": "contract-audit-hmac-key",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
from db import DB, DUMMY_PASSWORD_HASH, PASSWORD_SCHEME, verify_password  # noqa: E402


OLD_PASSWORD = "Legacy-Pass-123!"
NEW_PASSWORD = "Changed-Pass-456!"


def attach_direct_cookie(client: TestClient, response) -> str:
    token = response.cookies.get(app.SESSION_COOKIE)
    assert token
    client.cookies.set(app.SESSION_COOKIE, token, path="/")
    return token


def legacy_hash(password: str, salt: str = "legacy-contract-salt") -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        120_000,
    ).hex()
    return f"{salt}${digest}"


def main() -> None:
    user_id = app.db.create_user("security-admin", OLD_PASSWORD, role="admin")
    legacy = legacy_hash(OLD_PASSWORD)
    with app.db._lock:
        app.db.con.execute(
            "UPDATE users SET password_hash = ?, password_changed_at = NULL WHERE id = ?",
            (legacy, user_id),
        )
        app.db.con.commit()
    assert verify_password(OLD_PASSWORD, legacy)

    client = TestClient(app.app)
    login = client.post(
        "/api/auth/login",
        json={"username": "security-admin", "password": OLD_PASSWORD},
    )
    assert login.status_code == 200, login.text
    first_token = attach_direct_cookie(client, login)
    upgraded = app.db.get_user_by_id(user_id)
    assert str(upgraded["password_hash"]).startswith(f"{PASSWORD_SCHEME}$600000$")
    assert upgraded["password_hash"] != legacy

    captured: list[str] = []
    original_verify = app.verify_password_details

    def capture_dummy(password, stored):
        captured.append(str(stored))
        return original_verify(password, stored)

    app.db.clear_login_failures(app._username_hash("missing-account"))
    with patch.object(app, "verify_password_details", side_effect=capture_dummy):
        missing = client.post(
            "/api/auth/login",
            json={"username": "missing-account", "password": "Wrong-Pass-123!"},
        )
    assert missing.status_code == 401
    assert captured == [DUMMY_PASSWORD_HASH]

    app.db.clear_login_failures(app._username_hash("security-admin"))
    known_wrong = client.post(
        "/api/auth/login",
        json={"username": "security-admin", "password": "Wrong-Pass-123!"},
    )
    app.db.clear_login_failures(app._username_hash("security-admin"))
    app.db.clear_login_failures(app._username_hash("missing-account"))
    missing_again = client.post(
        "/api/auth/login",
        json={"username": "missing-account", "password": "Wrong-Pass-123!"},
    )
    assert known_wrong.status_code == missing_again.status_code == 401
    assert known_wrong.json() == missing_again.json()
    assert known_wrong.json()["detail"]["code"] == "login_failed"

    with app.db._lock:
        app.db.con.execute("DELETE FROM login_failures")
        app.db.con.commit()
    attempts = [
        client.post(
            "/api/auth/login",
            json={"username": "security-admin", "password": "Wrong-Pass-123!"},
        )
        for _ in range(5)
    ]
    assert [response.status_code for response in attempts[:4]] == [401, 401, 401, 401]
    assert attempts[4].status_code == 429
    assert int(attempts[4].headers["retry-after"]) >= 1
    username_key = app._username_hash("security-admin")

    class TestClientPeer:
        host = "testclient"

    class TestClientRequest:
        client = TestClientPeer()
        headers = {}

    client_key = app._login_client_hash(TestClientRequest())
    persisted = DB(str(RUN_DIR / "saas.db"))
    assert persisted.login_rate_status(username_key, client_key)["locked"] is True
    app.db.clear_login_failures(username_key)
    with app.db._lock:
        app.db.con.execute(
            "DELETE FROM login_failures WHERE username_hash = '*'"
        )
        app.db.con.commit()

    second_client = TestClient(app.app)
    second_login = second_client.post(
        "/api/auth/login",
        json={"username": "security-admin", "password": OLD_PASSWORD},
    )
    assert second_login.status_code == 200
    second_token = attach_direct_cookie(second_client, second_login)
    assert first_token != second_token

    changed = client.post(
        "/api/auth/password",
        json={"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["other_sessions_revoked"] is True
    assert client.get("/api/me").status_code == 200
    assert second_client.get("/api/me").status_code == 401
    assert app.db.get_token_user(first_token) == user_id
    assert app.db.get_token_user(second_token) is None
    assert client.post(
        "/api/auth/login",
        json={"username": "security-admin", "password": OLD_PASSWORD},
    ).status_code == 401
    app.db.clear_login_failures(username_key)
    with app.db._lock:
        app.db.con.execute("DELETE FROM login_failures WHERE username_hash = '*'")
        app.db.con.commit()
    assert client.post(
        "/api/auth/login",
        json={"username": "security-admin", "password": NEW_PASSWORD},
    ).status_code == 200

    session_tokens = [app.db.create_token(user_id) for _ in range(3)]
    assert app.db.get_token_user(session_tokens[0]) is None
    assert app.db.get_token_user(session_tokens[1]) == user_id
    assert app.db.get_token_user(session_tokens[2]) == user_id

    class Client:
        host = "203.0.113.7"

    class Request:
        client = Client()
        headers = {"x-forwarded-for": "198.51.100.10"}

    assert app._client_address(Request()) == "203.0.113.7"
    app.TRUSTED_PROXY_IPS.add("203.0.113.7")
    assert app._client_address(Request()) == "198.51.100.10"
    app.TRUSTED_PROXY_IPS.remove("203.0.113.7")

    redacted = app._redact_browser_logs(
        "X-Bootstrap-Token: bootstrap-contract-secret Authorization: Bearer session-secret"
    )
    assert "bootstrap-contract-secret" not in redacted
    assert "session-secret" not in redacted
    assert "[redacted]" in redacted
    events, _ = app.db.list_audit(limit=100)
    encoded_events = json.dumps(
        [dict(row) for row in events], ensure_ascii=False, sort_keys=True
    )
    assert OLD_PASSWORD not in encoded_events
    assert NEW_PASSWORD not in encoded_events
    assert first_token not in encoded_events
    assert second_token not in encoded_events
    print("auth security contract: ok")


if __name__ == "__main__":
    main()
