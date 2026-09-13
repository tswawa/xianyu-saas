#!/usr/bin/env python3
"""Platform settings, user administration and bounded audit contracts."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import types
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
if os.name != "posix":
    # The contract uses an isolated temporary DB and does not claim to verify
    # the production POSIX supervisor lock. Fail if app requests any other API.
    def portable_flock(_descriptor, flags):
        assert flags == 3

    sys.modules.setdefault(
        "fcntl",
        types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=portable_flock),
    )
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
from db import AUDIT_RETENTION_SECONDS  # noqa: E402
from platform_update import SemVer  # noqa: E402
from version import VERSION  # noqa: E402


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
    route_methods = {}
    for route in app.app.routes:
        path = getattr(route, "path", "")
        route_methods.setdefault(path, set()).update(getattr(route, "methods", set()) or set())
    for method, path in (
        ("POST", "/api/admin/confirm"),
        ("GET", "/api/admin/updates"),
        ("POST", "/api/admin/updates/check"),
        ("POST", "/api/admin/updates/download"),
        ("POST", "/api/admin/updates/apply"),
        ("POST", "/api/admin/updates/rollback"),
    ):
        assert method in route_methods.get(path, set()), (method, path)
    with patch.object(app, "start_services") as start, patch.object(app, "shutdown_services") as stop:
        with TestClient(app.app) as lifecycle_client:
            assert lifecycle_client.get("/api/version/public").status_code == 200
        start.assert_called_once_with()
        stop.assert_called_once_with()

    admin_id = app.db.create_user(
        "platform-admin", ADMIN_PASSWORD, role="admin"
    )
    owner_id = app.db.create_user("shop-owner", OWNER_PASSWORD, role="owner")
    admin_client = TestClient(app.app)
    owner_client = TestClient(app.app)
    admin_token = login(admin_client, "platform-admin", ADMIN_PASSWORD)
    owner_token = login(owner_client, "shop-owner", OWNER_PASSWORD)

    account = app.db.ensure_default_shop_account(owner_id)
    app.db.persist_worker_runtime(
        owner_id, account["id"], desired_state="running", mode="rules",
        state="waiting_login", pid=None, last_error="session_expired",
    )
    job = app.db.enqueue_job(
        owner_id, "shop_sync", "maintenance-completed-read", account_id=account["id"],
        payload={"replace_cookie": False, "cookie_fingerprint": "synthetic"}, max_attempts=1,
    )
    job_owner = "maintenance-completed-contract"
    assert app.db.claim_job(job["id"], job_owner, lease_seconds=30) is not None
    assert app.db.complete_job(job["id"], job_owner)
    app.write_secret(
        owner_id, "auth_status.json",
        json.dumps({"code": "session_expired", "reauthorization_required": True}),
        str(account["account_key"]),
    )
    auth_before = app.read_secret(owner_id, "auth_status.json", str(account["account_key"]))
    runtime_before = dict(app.db.get_worker_runtime(owner_id, account["id"]))
    changes_before = app.db.con.total_changes
    snapshot = {"nickname": "维护期合成店铺", "product_count": 1,
                "synced_at": "2026-09-12T00:00:00Z", "truncated": False}
    with patch.object(app, "maintenance_active", return_value=True), \
         patch.object(app, "load_verified_snapshot", return_value=snapshot), \
         patch.object(app, "_autostart_account_worker", wraps=app._autostart_account_worker) as autostart, \
         patch.object(app, "_acquire_account_lease") as acquire, \
         patch.object(app, "_clear_auth_status") as clear_auth, \
         patch.object(app, "_persist_worker_observation") as persist, \
         patch.object(app, "bot_start") as start, \
         patch.object(app, "bot_status", return_value={"running": False}):
        completed = owner_client.get(f"/api/bot/jobs/{job['id']}")
        assert completed.status_code == 200, completed.text
        worker = completed.json()["result"]["worker"]
        assert worker["desired_running"] is True
        assert worker["code"] == "update_maintenance_active"
        autostart.assert_not_called()
        direct = autostart(owner_id, account)
        assert direct["desired_running"] is True
        acquire.assert_not_called()
        clear_auth.assert_not_called()
        persist.assert_not_called()
        start.assert_not_called()
    assert app.db.con.total_changes == changes_before
    assert dict(app.db.get_worker_runtime(owner_id, account["id"])) == runtime_before
    assert app.read_secret(owner_id, "auth_status.json", str(account["account_key"])) == auth_before

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
    assert channel.status_code == 422
    assert "update_channel" not in admin_client.get("/api/admin/settings").json()
    invalid_channel = admin_client.put(
        "/api/admin/settings", json={"update_channel": "attacker-controlled"}
    )
    assert invalid_channel.status_code == 422

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

    installed = SemVer.parse(VERSION)
    target_version = f"{installed.major}.{installed.minor + 1}.0"
    operation_id = "a" * 32
    app.db.create_update_operation(
        operation_id=operation_id, action="apply", version=target_version, channel=app.RELEASE_CHANNEL,
        deployment="systemd", manifest_sha256="a" * 64, candidate_path="/isolated/candidate",
        expected_current_version=VERSION, requested_by=owner_id,
        session_digest=hashlib.sha256(promoted_token.encode()).hexdigest(), release_notes="verified",
    )
    confirmation_fields = {"action": "update.apply", "version": target_version, "operation_id": operation_id}
    capabilities = {"deployment": "systemd", "check": True, "download": True, "apply": True,
                    "rollback": True, "reason": ""}
    wrong_confirmation = promoted_client.post(
        "/api/admin/confirm", json={"password": "Wrong-Admin-Password!", **confirmation_fields},
    )
    assert wrong_confirmation.status_code == 403
    assert promoted_client.post("/api/admin/confirm", json={"password": OWNER_PASSWORD, "action": "update.apply"}).status_code == 422
    with patch("platform_update.update_capabilities", return_value=capabilities), patch("platform_update.validate_candidate"):
        confirmation = promoted_client.post(
            "/api/admin/confirm", json={"password": OWNER_PASSWORD, **confirmation_fields},
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
    binding = {"session_token": promoted_token, "version": target_version,
               "manifest_sha256": "a" * 64, "operation_id": operation_id}
    assert not app.db.consume_admin_confirmation(raw_confirmation, owner_id, "update.apply")
    assert not app.db.consume_admin_confirmation(raw_confirmation, owner_id, "update.rollback", **binding)
    assert not app.db.consume_admin_confirmation(raw_confirmation, owner_id, "update.apply", **{**binding, "version": "9.9.9"})
    assert app.db.consume_admin_confirmation(raw_confirmation, owner_id, "update.apply", **binding)
    assert not app.db.consume_admin_confirmation(raw_confirmation, owner_id, "update.apply", **binding)

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
    # Check results are durable and must never replace installation records.
    # Derive fixture versions so a new project release cannot invalidate its own cache test.
    installed = SemVer.parse(VERSION)
    target_version = f"{installed.major}.{installed.minor + 1}.0"
    unknown_version = f"{installed.major}.{installed.minor + 2}.0"
    assert SemVer.parse(target_version).compare(installed) > 0
    channel = app.RELEASE_CHANNEL
    # Existing installation metadata and obsolete settings must not choose the feed.
    app.db.set_platform_setting("update_channel", "beta")
    payload = {"status": "available", "available": True, "version": target_version,
               "current_version": VERSION, "channel": channel, "release_notes": "candidate"}
    app.db.upsert_platform_update(target_version, channel, "staged", candidate_path="/isolated/candidate",
                                  manifest_sha256="a" * 64, release_notes="verified")
    before = dict(app.db.get_platform_update(target_version, channel))
    probe_clock = [time.time()]
    app.update_probe.clock = lambda: probe_clock[0]
    with patch.object(app, "inspect_public_releases", return_value=payload) as inspector:
        for _ in range(2):
            checked = promoted_client.post("/api/admin/updates/check")
            assert checked.status_code == 200, checked.text
            assert checked.json()["status"] == "available" and checked.json()["checked_at"] > 0
        assert inspector.call_count == 1, "manual checks share the coordinator cooldown"
    assert dict(app.db.get_platform_update(target_version, channel)) == before
    for status in ("no_release", "current"):
        probe_clock[0] += 61
        result = {**payload, "status": status, "available": False,
                  "version": "" if status == "no_release" else VERSION}
        with patch.object(app, "inspect_public_releases", return_value=result):
            assert promoted_client.post("/api/admin/updates/check").json()["status"] == status
        assert promoted_client.get("/api/admin/updates").json()["update_check"]["status"] == status
    probe_clock[0] += 61
    with patch.object(app, "inspect_public_releases", side_effect=app.PlatformUpdateError("update_source_failed")):
        assert promoted_client.post("/api/admin/updates/check").status_code == 502
    assert app.db.get_platform_update_check(channel)["status"] == "current", "failure preserves the last successful discovery"
    status_payload = promoted_client.get("/api/admin/updates").json()
    assert status_payload["update_probe"]["error_code"] == "update_source_failed"
    assert status_payload["update_probe"]["state"] == "error"
    assert "operation" in status_payload
    assert "update_probe" in promoted_client.get("/api/version").json()
    assert set(promoted_client.get("/api/version/public").json()) == {"version", "asset_version"}
    assert dict(app.db.get_platform_update(target_version, channel)) == before
    other_channel = "stable" if channel == "beta" else "beta"
    assert app.db.get_platform_update_check(other_channel)["status"] == "unchecked"
    rollback_op = "b" * 32
    app.db.create_update_operation(
        operation_id=rollback_op, action="rollback", version=target_version, channel=channel,
        deployment="docker", manifest_sha256="b" * 64, expected_current_version=VERSION,
        requested_by=owner_id, session_digest=hashlib.sha256(promoted_token.encode()).hexdigest(),
    )
    with patch("platform_update.deployment_kind", return_value="docker"), \
         patch("platform_update.read_docker_capabilities", side_effect=app.PlatformUpdateError("update_service_unavailable")), \
         patch("platform_update.write_update_intent") as intent:
        for action in ("download", "apply", "rollback"):
            data = {"version": target_version}
            if action != "download":
                data.update(confirmation_token="unused-confirmation-token",
                            operation_id=rollback_op if action == "rollback" else operation_id)
            unsupported = promoted_client.post("/api/admin/updates/" + action, json=data)
            assert unsupported.status_code == 503, unsupported.text
            assert unsupported.json()["detail"]["code"] == "update_service_unavailable"
        intent.assert_not_called()
    with patch("platform_update.update_capabilities", return_value=capabilities), \
         patch("platform_update.validate_candidate"), patch("platform_update.read_operation_status", return_value=None), \
         patch("platform_update.write_update_intent", return_value={"queued": True}) as intent:
        confirmation = promoted_client.post("/api/admin/confirm", json={"password": OWNER_PASSWORD, **confirmation_fields})
        assert confirmation.status_code == 200, confirmation.text
        body = {"version": target_version, "operation_id": operation_id,
                "confirmation_token": confirmation.json()["confirmation_token"]}
        applied = promoted_client.post("/api/admin/updates/apply", json=body)
        assert applied.status_code == 202, applied.text
        assert applied.json()["status"] == "queued"
        assert app.db.get_platform_update(target_version, channel)["status"] == "apply_requested"
        assert promoted_client.post("/api/admin/updates/apply", json=body).status_code == 202
        assert intent.call_count == 1, "repeated submission must not publish a second request"
        with patch.object(app, "inspect_public_releases", return_value=payload) as inspector:
            assert promoted_client.post("/api/admin/updates/check").status_code == 200
            inspector.assert_not_called()
        assert app.db.get_platform_update(target_version, channel)["status"] == "apply_requested"
        assert promoted_client.post("/api/admin/updates/download", json={"version": unknown_version}).status_code == 409
    print("platform admin contract: ok")


if __name__ == "__main__":
    main()
