#!/usr/bin/env python3
"""Platform settings, user administration and bounded audit contracts."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-saas-platform-admin-"))
os.environ.update(
    {
        "SAAS_DB": str(RUN_DIR / "saas.db"),
        "SAAS_TENANTS_DIR": str(RUN_DIR / "tenants"),
        "SAAS_COOKIE_SECURE": "0",
        "SAAS_ALLOW_REGISTRATION": "0",
        "SAAS_ADMIN_TOKEN": "contract-emergency-admin",
        "SAAS_PUBLIC_ORIGIN": "http://testserver",
        "SAAS_TRUSTED_HOSTS": "testserver",
        "SAAS_TESTING": "1",
        "SAAS_RESTORE_WORKERS": "0",
        "SAAS_AUDIT_HMAC_KEY": "contract-audit-key",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
from db import AUDIT_RETENTION_SECONDS  # noqa: E402


ADMIN_PASSWORD = "Admin-Contract-123!"
OWNER_PASSWORD = "Owner-Contract-123!"


def login(client: TestClient, username: str, password: str) -> str:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get(app.SESSION_COOKIE)
    assert token
    client.cookies.set(app.SESSION_COOKIE, token, path="/")
    return token


def main() -> None:
    admin_id = app.db.create_user(
        "platform-admin", ADMIN_PASSWORD, role="admin"
    )
    owner_id = app.db.create_user("shop-owner", OWNER_PASSWORD, role="owner")
    admin_client = TestClient(app.app)
    owner_client = TestClient(app.app)
    admin_token = login(admin_client, "platform-admin", ADMIN_PASSWORD)
    owner_token = login(owner_client, "shop-owner", OWNER_PASSWORD)

    denied = owner_client.get("/api/admin/settings")
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "admin_required"
    assert owner_client.get("/api/admin/users").status_code == 403
    assert owner_client.get("/api/admin/audit").status_code == 403

    settings = admin_client.get("/api/admin/settings")
    assert settings.status_code == 200
    assert settings.json()["registration"] == {
        "environment_allowed": False,
        "database_open": False,
        "users_exist": True,
        "effective": False,
    }
    browser_headers = {
        "X-SaaS-Browser-Intent": "browser-write",
        "Origin": "http://testserver",
        "Referer": "http://testserver/xianyu-saas/",
    }
    with patch.dict(os.environ, {"SAAS_TESTING": "0"}, clear=False):
        missing_origin_guard = admin_client.put(
            "/api/admin/settings", json={"registration_open": True}
        )
        assert missing_origin_guard.status_code == 403
        assert missing_origin_guard.json()["detail"]["code"] == "browser_write_header_required"
        mismatched_origin = admin_client.put(
            "/api/admin/settings",
            headers={**browser_headers, "Origin": "http://evil.invalid"},
            json={"registration_open": True},
        )
        assert mismatched_origin.status_code == 403
        assert mismatched_origin.json()["detail"]["code"] == "browser_origin_mismatch"
        database_open = admin_client.put(
            "/api/admin/settings",
            headers=browser_headers,
            json={"registration_open": True},
        )
    assert database_open.status_code == 200
    assert database_open.json()["registration"]["database_open"] is True
    assert database_open.json()["registration"]["effective"] is False
    os.environ["SAAS_ALLOW_REGISTRATION"] = "1"
    assert admin_client.get("/api/admin/settings").json()["registration"]["effective"] is True
    channel = admin_client.put(
        "/api/admin/settings", json={"update_channel": "beta"}
    )
    assert channel.status_code == 200
    assert channel.json()["update_channel"] == "beta"
    invalid_channel = admin_client.put(
        "/api/admin/settings", json={"update_channel": "attacker-controlled"}
    )
    assert invalid_channel.status_code == 400

    users = admin_client.get("/api/admin/users?limit=1")
    assert users.status_code == 200
    payload = users.json()
    assert len(payload["users"]) == 1
    assert payload["next_cursor"] is not None
    user_text = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("password_hash", "cookie", "orders", "inventory", OWNER_PASSWORD):
        assert forbidden not in user_text.lower()
    second_page = admin_client.get(
        f"/api/admin/users?limit=10&cursor={payload['next_cursor']}"
    )
    assert second_page.status_code == 200
    assert second_page.json()["users"]

    self_change = admin_client.patch(
        f"/api/admin/users/{admin_id}", json={"role": "owner"}
    )
    assert self_change.status_code == 400
    assert self_change.json()["detail"]["code"] == "self_change_forbidden"

    emergency = TestClient(app.app)
    emergency_headers = {"X-Admin-Token": "contract-emergency-admin"}
    last_admin = emergency.patch(
        f"/api/admin/users/{admin_id}",
        headers=emergency_headers,
        json={"enabled": False},
    )
    assert last_admin.status_code == 409
    assert last_admin.json()["detail"]["code"] == "last_admin_protected"

    promoted = admin_client.patch(
        f"/api/admin/users/{owner_id}", json={"role": "admin"}
    )
    assert promoted.status_code == 200
    assert promoted.json()["user"]["role"] == "admin"
    assert app.db.get_token_user(owner_token) is None
    demoted = emergency.patch(
        f"/api/admin/users/{admin_id}",
        headers=emergency_headers,
        json={"role": "owner"},
    )
    assert demoted.status_code == 200
    assert app.db.get_token_user(admin_token) is None

    promoted_client = TestClient(app.app)
    promoted_token = login(promoted_client, "shop-owner", OWNER_PASSWORD)
    extra_token = app.db.create_token(owner_id)
    revoked = promoted_client.post(
        f"/api/admin/users/{owner_id}/sessions/revoke"
    )
    assert revoked.status_code == 200
    assert revoked.json()["sessions_revoked"] >= 1
    assert app.db.get_token_user(promoted_token) == owner_id
    assert app.db.get_token_user(extra_token) is None

    with app.db._lock:
        app.db.con.execute(
            """
            INSERT INTO login_failures(
                username_hash, client_hash, attempts, locked_until,
                last_failed_at, updated_at
            ) VALUES (?, '*', 9, ?, ?, ?)
            """,
            (
                app._username_hash("platform-admin"),
                time.time() + 300,
                time.time(),
                time.time(),
            ),
        )
        app.db.con.commit()
    unlock = promoted_client.post(f"/api/admin/users/{admin_id}/unlock")
    assert unlock.status_code == 200
    assert app.db.username_lock_status(app._username_hash("platform-admin"))["locked"] is False

    wrong_confirmation = promoted_client.post(
        "/api/admin/confirm",
        json={"password": "Wrong-Admin-Password!", "action": "update.apply"},
    )
    assert wrong_confirmation.status_code == 403
    confirmation = promoted_client.post(
        "/api/admin/confirm",
        json={"password": OWNER_PASSWORD, "action": "update.apply"},
    )
    assert confirmation.status_code == 200, confirmation.text
    raw_confirmation = confirmation.json()["confirmation_token"]
    digest = hashlib.sha256(raw_confirmation.encode("utf-8")).hexdigest()
    with app.db._lock:
        stored = app.db.con.execute(
            "SELECT token_digest FROM admin_confirmations WHERE token_digest = ?", (digest,)
        ).fetchone()
        leaked = app.db.con.execute(
            "SELECT 1 FROM admin_confirmations WHERE token_digest = ?", (raw_confirmation,)
        ).fetchone()
    assert stored is not None
    assert leaked is None
    assert app.db.consume_admin_confirmation(
        raw_confirmation, owner_id, "update.apply"
    )
    assert not app.db.consume_admin_confirmation(
        raw_confirmation, owner_id, "update.apply"
    )

    injection = promoted_client.get(
        "/api/admin/audit?event_type=auth.login_succeeded%27%20OR%201=1--"
    )
    assert injection.status_code == 400
    audit = promoted_client.get("/api/admin/audit?limit=100")
    assert audit.status_code == 200
    audit_text = json.dumps(audit.json(), ensure_ascii=False, sort_keys=True)
    for forbidden in (
        ADMIN_PASSWORD,
        OWNER_PASSWORD,
        raw_confirmation,
        admin_token,
        owner_token,
        "contract-emergency-admin",
    ):
        assert forbidden not in audit_text
    assert "platform.settings_changed" in audit_text
    assert "platform.user_changed" in audit_text

    old_id = app.db.append_audit(
        "contract.old",
        metadata={"code": "expired"},
        created_at=time.time() - AUDIT_RETENTION_SECONDS - 10,
    )
    recent_id = app.db.append_audit("contract.recent", metadata={"code": "kept"})
    counts = app.db.prune_retention()
    assert counts["audit_log"] >= 1
    rows, _ = app.db.list_audit(limit=100)
    ids = {int(row["id"]) for row in rows}
    assert old_id not in ids
    assert recent_id in ids
    print("platform admin contract: ok")


if __name__ == "__main__":
    main()
