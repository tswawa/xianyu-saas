#!/usr/bin/env python3
"""Bootstrap, role and durable registration contracts on isolated state."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-saas-auth-roles-"))
DB_PATH = RUN_DIR / "saas.db"
TOKEN_FILE = RUN_DIR / "bootstrap-token"
BOOTSTRAP_TOKEN = "bootstrap-contract-token-0123456789abcdef"
TOKEN_FILE.write_text(BOOTSTRAP_TOKEN, encoding="utf-8")
TOKEN_FILE.chmod(0o600)
os.environ.update(
    {
        "SAAS_DB": str(DB_PATH),
        "SAAS_TENANTS_DIR": str(RUN_DIR / "tenants"),
        "SAAS_COOKIE_SECURE": "0",
        "SAAS_ALLOW_REGISTRATION": "1",
        "SAAS_BOOTSTRAP_ENABLED": "0",
        "SAAS_BOOTSTRAP_TOKEN_FILE": str(TOKEN_FILE),
        "SAAS_BOOTSTRAP_TRUSTED_SOURCES": "testclient",
        "SAAS_PUBLIC_ORIGIN": "http://testserver",
        "SAAS_TRUSTED_HOSTS": "testserver",
        "SAAS_TESTING": "1",
        "SAAS_RESTORE_WORKERS": "0",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
from db import (  # noqa: E402
    BootstrapUnavailableError,
    DB,
    RegistrationClosedError,
    hash_password,
)


PASSWORD = "Contract-Pass-123!"


def assert_bootstrap_concurrency() -> None:
    race_path = RUN_DIR / "bootstrap-race.db"
    first = DB(str(race_path))
    second = DB(str(race_path))
    digest = hashlib.sha256(BOOTSTRAP_TOKEN.encode("utf-8")).hexdigest()
    assert first.configure_bootstrap(digest)
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def create(database: DB, username: str) -> None:
        barrier.wait()
        try:
            database.bootstrap_user(username, PASSWORD, digest)
        except (BootstrapUnavailableError, sqlite3.IntegrityError):
            outcomes.append("closed")
        else:
            outcomes.append("created")

    threads = [
        threading.Thread(target=create, args=(first, "race-admin-a")),
        threading.Thread(target=create, args=(second, "race-admin-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["closed", "created"]
    assert first.user_count() == 1
    state = first.get_bootstrap_state()
    assert state["state"] == "consumed"
    assert state["token_configured"] == 0


def assert_bootstrap_registration_race() -> None:
    race_path = RUN_DIR / "bootstrap-registration-race.db"
    bootstrap_db = DB(str(race_path))
    registration_db = DB(str(race_path))
    digest = hashlib.sha256(BOOTSTRAP_TOKEN.encode("utf-8")).hexdigest()
    assert bootstrap_db.configure_bootstrap(digest)
    # The durable public-registration switch defaults closed and cannot be
    # opened through the UI before an administrator exists.
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def create_admin() -> None:
        barrier.wait()
        bootstrap_db.bootstrap_user("race-bootstrap-admin", PASSWORD, digest)
        outcomes.append("bootstrap")

    def create_owner() -> None:
        barrier.wait()
        try:
            registration_db.register_user("race-public-owner", PASSWORD)
        except RegistrationClosedError:
            outcomes.append("registration_closed")
        else:
            outcomes.append("registration")

    threads = [threading.Thread(target=create_admin), threading.Thread(target=create_owner)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["bootstrap", "registration_closed"]
    assert bootstrap_db.user_count() == 1
    assert bootstrap_db.get_user("race-bootstrap-admin")["role"] == "admin"
    assert bootstrap_db.get_user("race-public-owner") is None


def assert_consumed_bootstrap_is_irreversible() -> None:
    irreversible_path = RUN_DIR / "bootstrap-irreversible.db"
    database = DB(str(irreversible_path))
    user_id = database.create_user("temporary-first-admin", PASSWORD, role="admin")
    assert database.get_bootstrap_state()["state"] == "consumed"
    assert database.remove_unconfigured_user(user_id) is True
    assert database.user_count() == 0
    state = database.get_bootstrap_state()
    assert state["state"] == "consumed"
    assert state["token_configured"] == 0
    digest = hashlib.sha256(BOOTSTRAP_TOKEN.encode("utf-8")).hexdigest()
    assert database.configure_bootstrap(digest) is False


def assert_legacy_migration() -> None:
    legacy_path = RUN_DIR / "legacy.db"
    with sqlite3.connect(legacy_path) as con:
        con.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                expires_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
            """
        )
        con.execute(
            "INSERT INTO users(username, password_hash, expires_at, created_at) VALUES (?, ?, 0, ?)",
            ("legacy-first", hash_password(PASSWORD), 10.0),
        )
        con.execute(
            "INSERT INTO users(username, password_hash, expires_at, created_at) VALUES (?, ?, 0, ?)",
            ("legacy-second", hash_password(PASSWORD), 20.0),
        )
        con.commit()
    migrated = DB(str(legacy_path))
    assert migrated.get_user("legacy-first")["role"] == "admin"
    assert migrated.get_user("legacy-second")["role"] == "owner"
    assert migrated.get_bootstrap_state()["state"] == "consumed"
    migrated._init()
    assert migrated.get_user("legacy-first")["role"] == "admin"
    assert migrated.count_enabled_admins() == 1


def main() -> None:
    client = TestClient(app.app)
    capabilities = client.get("/api/auth/capabilities").json()
    assert capabilities == {
        "registration_enabled": False,
        "bootstrap_available": False,
        "password_min_length": 12,
    }
    first_public = client.post(
        "/api/auth/register",
        json={"username": "public-first", "password": PASSWORD},
    )
    assert first_public.status_code == 403
    assert app.db.user_count() == 0

    os.environ["SAAS_BOOTSTRAP_ENABLED"] = "1"
    os.environ["SAAS_BOOTSTRAP_TRUSTED_SOURCES"] = "192.0.2.44"
    untrusted = client.get("/api/auth/capabilities").json()
    assert untrusted["bootstrap_available"] is False
    assert app.db.get_bootstrap_state()["token_configured"] == 0
    os.environ["SAAS_BOOTSTRAP_TRUSTED_SOURCES"] = "testclient"
    capabilities = client.get("/api/auth/capabilities").json()
    assert capabilities["bootstrap_available"] is True
    with patch.dict(os.environ, {"SAAS_TESTING": "0"}, clear=False):
        missing_origin_guard = client.post(
            "/api/auth/bootstrap",
            headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
            json={"username": "first-admin", "password": PASSWORD},
        )
        assert missing_origin_guard.status_code == 403
        assert missing_origin_guard.json()["detail"]["code"] == "browser_write_header_required"
    missing = client.post(
        "/api/auth/bootstrap",
        json={"username": "first-admin", "password": PASSWORD},
    )
    assert missing.status_code == 403
    assert app.db.user_count() == 0
    assert app.db.get_bootstrap_state()["state"] == "pending"

    wrong = client.post(
        "/api/auth/bootstrap",
        headers={"X-Bootstrap-Token": "wrong-token-value-0123456789abcdef"},
        json={"username": "first-admin", "password": PASSWORD},
    )
    assert wrong.status_code == 403
    assert BOOTSTRAP_TOKEN not in wrong.text
    assert app.db.user_count() == 0
    assert app.db.get_bootstrap_state()["state"] == "pending"

    created = client.post(
        "/api/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"username": "first-admin", "password": PASSWORD},
    )
    assert created.status_code == 200, created.text
    admin = app.db.get_user("first-admin")
    assert admin["role"] == "admin"
    state = app.db.get_bootstrap_state()
    assert state["state"] == "consumed"
    assert state["token_configured"] == 0
    assert state["created_user_id"] == admin["id"]

    replay = client.post(
        "/api/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"username": "replayed-admin", "password": PASSWORD},
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "bootstrap_consumed"
    events, _ = app.db.list_audit(limit=100)
    serialized_events = str([dict(row) for row in events])
    assert BOOTSTRAP_TOKEN not in serialized_events
    assert hashlib.sha256(BOOTSTRAP_TOKEN.encode("utf-8")).hexdigest() not in serialized_events
    restarted = DB(str(DB_PATH))
    assert restarted.get_bootstrap_state()["state"] == "consumed"
    assert restarted.user_count() == 1

    still_closed = client.post(
        "/api/auth/register",
        json={"username": "closed-owner", "password": PASSWORD},
    )
    assert still_closed.status_code == 403
    app.db.set_platform_setting("registration_open", "1", admin["id"])
    opened = client.get("/api/auth/capabilities").json()
    assert opened["registration_enabled"] is True
    owner_created = client.post(
        "/api/auth/register",
        json={"username": "shop-owner", "password": PASSWORD},
    )
    assert owner_created.status_code == 200, owner_created.text
    assert app.db.get_user("shop-owner")["role"] == "owner"

    os.environ["SAAS_ALLOW_REGISTRATION"] = "0"
    ceiling = client.get("/api/auth/capabilities").json()
    assert ceiling["registration_enabled"] is False
    denied_by_ceiling = client.post(
        "/api/auth/register",
        json={"username": "ceiling-owner", "password": PASSWORD},
    )
    assert denied_by_ceiling.status_code == 403

    assert_bootstrap_concurrency()
    assert_bootstrap_registration_race()
    assert_consumed_bootstrap_is_irreversible()
    assert_legacy_migration()
    print("auth roles contract: ok")


if __name__ == "__main__":
    main()
