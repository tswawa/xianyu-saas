#!/usr/bin/env python3
"""Bootstrap, role and durable registration contracts on isolated state."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
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
import bot_manager  # noqa: E402
import shop_sync  # noqa: E402
from account_storage import AccountStorage  # noqa: E402
from db import (  # noqa: E402
    BootstrapUnavailableError,
    DB,
    RegistrationClosedError,
    hash_password,
)


PASSWORD = "Contract-Pass-123!"


@contextmanager
def isolated_registration(name):
    root = RUN_DIR / name
    root.mkdir()
    database = DB(str(root / "saas.db"))
    tenants = root / "tenants"
    storage = AccountStorage(tenants)
    try:
        with (
            patch.object(app, "db", database),
            patch.object(bot_manager, "TENANTS_ROOT", str(tenants)),
            patch.object(shop_sync, "TENANTS_ROOT", str(tenants)),
            patch.object(app.ai_service, "storage", storage),
            patch.dict(os.environ, {
                "SAAS_TENANTS_DIR": str(tenants), "SAAS_ALLOW_REGISTRATION": "0",
                "SAAS_BOOTSTRAP_ENABLED": "0", "SAAS_TESTING": "0",
            }),
            TestClient(app.app, headers={
                "Origin": "http://testserver", "X-SaaS-Browser-Intent": "browser-write",
            }) as client,
        ):
            yield database, storage, client
    finally:
        database.con.close()


def assert_first_registration_http() -> None:
    with isolated_registration("first-registration") as (database, storage, client):
        capabilities = client.get("/api/auth/capabilities").json()
        assert capabilities["first_registration_available"] is True
        assert capabilities["registration_enabled"] is True
        assert capabilities["bootstrap_available"] is False
        rejected = client.post("/api/auth/register", headers={"Origin": "https://other.example"},
                               json={"username": "first-web-admin", "password": PASSWORD})
        assert rejected.status_code == 403
        assert database.user_count() == 0
        weak = client.post("/api/auth/register", json={"username": "first-web-admin", "password": "short"})
        assert weak.status_code == 400
        created = client.post("/api/auth/register", json={"username": "first-web-admin", "password": PASSWORD})
        assert created.status_code == 200, created.text
        assert created.json() == {"ok": True, "role": "admin"}
        user = database.get_user("first-web-admin")
        account = database.get_shop_account(user["id"])
        assert account["storage_initialized_at"] is not None
        for name, expected in bot_manager.INITIAL_ACCOUNT_FILES.items():
            assert json.loads(storage.read_text(user["id"], "default", name)) == json.loads(expected)
        assert (storage.account_dir(user["id"]) / "ai_knowledge").is_dir()
        login = client.post("/api/auth/login", json={"username": "first-web-admin", "password": PASSWORD})
        assert login.status_code == 200, login.text
        client.cookies.set(app.SESSION_COOKIE, login.cookies[app.SESSION_COOKIE], path="/")
        assert client.get("/api/me").json()["role"] == "admin"
        assert client.get("/api/config").status_code == 200
        assert client.get("/api/automation").status_code == 200
        capabilities = client.get("/api/auth/capabilities").json()
        assert capabilities["first_registration_available"] is False
        assert capabilities["registration_enabled"] is False
        again = client.post("/api/auth/register", json={"username": "second-admin", "password": PASSWORD})
        assert again.status_code == 403
        assert database.user_count() == 1
        with patch.dict(os.environ, {"SAAS_ALLOW_REGISTRATION": "1"}):
            database.set_platform_setting("registration_open", "1", user["id"])
            owner = client.post("/api/auth/register", json={"username": "later-owner", "password": PASSWORD})
            assert owner.status_code == 200 and owner.json()["role"] == "owner"


def assert_first_registration_storage_failure() -> None:
    with isolated_registration("first-registration-failure") as (database, storage, client):
        original = AccountStorage.atomic_write_path

        def fail_midway(instance, path, data, **kwargs):
            if Path(path).name == "automation_settings.json":
                raise OSError("synthetic write failure")
            return original(instance, path, data, **kwargs)

        with patch.object(AccountStorage, "atomic_write_path", fail_midway):
            failed = client.post("/api/auth/register", json={"username": "retry-admin", "password": PASSWORD})
        assert failed.status_code == 503
        assert failed.json()["detail"]["code"] == "account_initialization_failed"
        assert database.user_count() == 0 and database.first_registration_available()
        assert not storage.account_dir(1).exists()
        # A directory left by an operator must not be removed by compensation.
        existing = storage.write_text(1, "default", "reply_rules.json", '{"version":1,"rules":[]}')
        preserved = existing.read_bytes()
        failed = client.post("/api/auth/register", json={"username": "retry-admin", "password": PASSWORD})
        assert failed.status_code == 503
        assert existing.read_bytes() == preserved
        assert database.user_count() == 0 and database.first_registration_available()
        existing.unlink()
        existing.parent.rmdir()
        retried = client.post("/api/auth/register", json={"username": "retry-admin", "password": PASSWORD})
        assert retried.status_code == 200 and database.count_enabled_admins() == 1


def assert_legacy_login_initialization() -> None:
    with isolated_registration("legacy-login") as (database, storage, client):
        database.create_user("padding-admin", PASSWORD, role="admin")
        uid = database.create_user("legacy-owner", PASSWORD, role="owner")
        assert uid != 1
        original = storage.write_text(uid, "default", "reply_rules.json", '{ "rules": [], "version": 1 }')
        original_bytes = original.read_bytes()
        wrong = client.post("/api/auth/login", json={"username": "legacy-owner", "password": "incorrect-password"})
        assert wrong.status_code == 401
        assert not (original.parent / "automation_settings.json").exists()
        login = client.post("/api/auth/login", json={"username": "legacy-owner", "password": PASSWORD})
        assert login.status_code == 200, login.text
        assert original.read_bytes() == original_bytes
        assert database.get_shop_account(uid)["storage_initialized_at"] is not None
        client.cookies.set(app.SESSION_COOKIE, login.cookies[app.SESSION_COOKIE], path="/")
        assert client.get("/api/config").status_code == 200
        assert client.get("/api/automation").status_code == 200
        original.unlink()
        login = client.post("/api/auth/login", json={"username": "legacy-owner", "password": PASSWORD})
        assert login.status_code == 200
        assert not original.exists(), "an initialized account must never be reseeded on login"
        assert client.get("/api/config").status_code == 503

    with isolated_registration("legacy-permission") as (database, storage, client):
        uid = database.create_user("legacy-permission", PASSWORD)
        with patch.object(bot_manager, "initialize_unused_account_storage", side_effect=PermissionError("synthetic")):
            failed = client.post("/api/auth/login", json={"username": "legacy-permission", "password": PASSWORD})
        assert failed.status_code == 503
        assert failed.json()["detail"]["code"] == "account_initialization_failed"
        assert not failed.cookies.get(app.SESSION_COOKIE)
        assert database.get_shop_account(uid)["storage_initialized_at"] is None
        assert database.con.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0


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
    assert_first_registration_http()
    assert_first_registration_storage_failure()
    assert_legacy_login_initialization()
    client = TestClient(app.app)
    capabilities = client.get("/api/auth/capabilities").json()
    assert capabilities == {
        "registration_enabled": True,
        "first_registration_available": True,
        "bootstrap_available": False,
        "password_min_length": 12,
    }
    # Explicit token bootstrap must not be bypassed by ordinary registration.
    os.environ["SAAS_BOOTSTRAP_ENABLED"] = "1"
    assert client.get("/api/auth/capabilities").json()["first_registration_available"] is False
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
