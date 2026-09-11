#!/usr/bin/env python3
"""Offline registration/storage ownership contracts; no application lifespan."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
IMPORT_DIR = tempfile.TemporaryDirectory(prefix="registration-import-")
os.environ.update({
    "SAAS_DB": str(Path(IMPORT_DIR.name) / "import.db"),
    "SAAS_TENANTS_DIR": str(Path(IMPORT_DIR.name) / "tenants"),
    "SAAS_BOOTSTRAP_ENABLED": "0", "SAAS_ALLOW_REGISTRATION": "0",
    "SAAS_BOOTSTRAP_TOKEN_FILE": "", "SAAS_COOKIE_SECURE": "0",
    "SAAS_TESTING": "1", "SAAS_RESTORE_WORKERS": "0",
    "SAAS_PUBLIC_ORIGIN": "http://testserver", "SAAS_TRUSTED_HOSTS": "testserver",
})
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
import bot_manager as manager  # noqa: E402
from account_storage import AccountStorage  # noqa: E402
from db import DB, RegistrationClosedError  # noqa: E402

PASSWORD = "Registration-Contract-123!"
USERNAME = "contract-admin"
FILES = ("redeem_codes.json", "pan_links.json", "reply_rules.json",
         "automation_settings.json", "products_config.json")


def tearDownModule():
    app.db.con.close()
    app._api_process_lock.close()
    IMPORT_DIR.cleanup()


@contextmanager
def isolated():
    with tempfile.TemporaryDirectory(prefix="registration-contract-") as temporary:
        root = Path(temporary)
        database = DB(str(root / "control.db"))
        tenants = root / "tenants"
        with patch.dict(os.environ, {
            "SAAS_DB": str(root / "control.db"), "SAAS_TENANTS_DIR": str(tenants),
            "SAAS_BOOTSTRAP_ENABLED": "0", "SAAS_ALLOW_REGISTRATION": "0",
        }), patch.object(app, "db", database), patch.object(manager, "TENANTS_ROOT", str(tenants)):
            client = TestClient(app.app)  # Deliberately not a context manager: no workers.
            try:
                yield database, tenants / "1", client
            finally:
                client.close()
                database.con.close()


def snapshot(path):
    """Capture bytes/mtime without following even dangling directory symlinks."""
    if path.is_symlink():
        return {".": ("link", os.readlink(path), path.lstat().st_mtime_ns)}
    if not path.exists():
        return {}
    entries = [path, *path.rglob("*")] if path.is_dir() else [path]
    return {str(item.relative_to(path)): (
        ("link", os.readlink(item), item.lstat().st_mtime_ns) if item.is_symlink()
        else ("dir",) if item.is_dir()
        else ("file", item.read_bytes(), item.stat().st_mtime_ns)
    ) for item in entries}


def seed(folder, shape):
    folder.mkdir(parents=True)
    names = FILES[:2] if shape == "partial" else FILES if shape == "complete" else ()
    for name in names:
        # Different formatting and an old mtime detect even equivalent rewrites.
        content = json.dumps(json.loads(manager.INITIAL_ACCOUNT_FILES[name]), indent=3) + "\n"
        target = folder / name
        target.write_text(content, encoding="utf-8")
        os.utime(target, ns=(1_600_000_000_000_000_000,) * 2)
    if shape in {"knowledge", "complete"}:
        (folder / "ai_knowledge").mkdir()


def register(client):
    return client.post("/api/auth/register", json={"username": USERNAME, "password": PASSWORD})


class FailCommitOnce:
    def __init__(self, connection):
        self.connection, self.failed = connection, False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        if not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("synthetic registration commit failure")
        return self.connection.commit()


class RegistrationInitContract(unittest.TestCase):
    def assert_files(self, folder):
        self.assertEqual(set(manager.INITIAL_ACCOUNT_FILES), set(FILES))
        self.assertEqual({item.name for item in folder.iterdir()}, set(FILES) | {"ai_knowledge"})
        for name in FILES:
            self.assertEqual(json.loads((folder / name).read_text(encoding="utf-8")),
                             json.loads(manager.INITIAL_ACCOUNT_FILES[name]))
        self.assertTrue((folder / "ai_knowledge").is_dir())
        self.assertEqual(list((folder / "ai_knowledge").iterdir()), [])

    def assert_counts(self, database, count):
        for table in ("users", "shop_accounts", "worker_runtimes"):
            self.assertEqual(database.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], count)
        self.assertFalse(database.con.in_transaction)

    def assert_rollback(self, database):
        self.assert_counts(database, 0)
        state = database.get_bootstrap_state()
        self.assertEqual(state["state"], "pending")
        self.assertIsNone(state["created_user_id"])
        self.assertEqual(state["token_configured"], 0)
        self.assertTrue(database.first_registration_available())

    def assert_registered(self, database, folder, response):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True, "role": "admin"})
        self.assert_counts(database, 1)
        self.assertEqual(database.get_user(USERNAME)["role"], "admin")
        account = database.con.execute("SELECT * FROM shop_accounts").fetchone()
        self.assertEqual((account["user_id"], account["account_key"]), (1, "default"))
        self.assertGreater(account["storage_initialized_at"], 0)
        state = database.get_bootstrap_state()
        self.assertEqual((state["state"], state["created_user_id"]), ("consumed", 1))
        self.assertEqual(state["token_configured"], 0)
        self.assert_files(folder)

    def assert_error(self, response, reason):
        self.assertEqual(response.status_code, 503, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "account_initialization_failed")
        self.assertEqual(detail["reason"], reason)
        self.assertTrue(detail["message"])
        for secret in (USERNAME, PASSWORD, "fake-secret-token", "fake-private-storage"):
            self.assertNotIn(secret, response.text)

    def assert_preserved(self, before, folder):
        after = snapshot(folder)
        for name, value in before.items():
            self.assertEqual(after.get(name), value, name)

    def test_first_web_registration_with_default_closed_flags(self):
        with isolated() as (database, folder, client):
            capabilities = client.get("/api/auth/capabilities").json()
            self.assertTrue(capabilities["first_registration_available"])
            self.assertFalse(capabilities["bootstrap_available"])
            with patch.object(manager, "initialize_new_user_storage",
                              wraps=manager.initialize_new_user_storage) as initialize:
                self.assert_registered(database, folder, register(client))
            initialize.assert_called_once_with(1)

    def test_empty_and_default_only_directories_are_completed_without_overwrite(self):
        for shape in ("empty", "knowledge", "partial", "complete"):
            with self.subTest(shape=shape), isolated() as (database, folder, client):
                seed(folder, shape)
                before = snapshot(folder)
                self.assert_registered(database, folder, register(client))
                self.assert_preserved(before, folder)

    def test_initializer_reports_directory_ownership(self):
        for shape in (None, "empty", "partial", "complete"):
            with self.subTest(shape=shape), isolated() as (_, folder, _client):
                if shape:
                    seed(folder, shape)
                self.assertIs(manager.initialize_new_user_storage(1), shape is None)
                before = snapshot(folder)
                self.assertIs(manager.initialize_new_user_storage(1), False)
                self.assertEqual(snapshot(folder), before)

    def test_conflicting_storage_rolls_back_without_deletion(self):
        conflicts = {"cookies.txt": b"synthetic-cookie", "unknown.bin": b"opaque",
                     "unknown-dir/": None, "ai_knowledge/note.txt": b"private fixture",
                     "products_config.json": b'{"types": [{"name": "custom"}]}',
                     "reply_rules.json": b"{broken-json"}
        for name, content in conflicts.items():
            with self.subTest(conflict=name), isolated() as (database, folder, client):
                seed(folder, "partial")
                target = folder / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.mkdir() if content is None else target.write_bytes(content)
                before = snapshot(folder)
                with patch.object(app, "_discard_new_account_storage") as discard:
                    self.assert_error(register(client), "storage_conflict")
                discard.assert_not_called()
                self.assert_rollback(database)
                self.assertEqual(snapshot(folder), before)

    def test_symlinks_and_dangling_symlinks_are_never_followed(self):
        for directory in (False, True):
            for dangling in (False, True):
                with self.subTest(directory=directory, dangling=dangling), isolated() as (database, folder, client):
                    folder.parent.mkdir()
                    outside = folder.parent.parent / "link-target"
                    if not dangling:
                        if directory:
                            outside.mkdir()
                            (outside / "keep.txt").write_bytes(b"external fixture")
                        else:
                            outside.write_bytes(b'{"types": []}')
                    if not directory:
                        seed(folder, "partial")
                    link = folder if directory else folder / "products_config.json"
                    try:
                        link.symlink_to(outside, target_is_directory=directory)
                    except OSError as error:
                        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                            self.skipTest("Windows symlink creation privilege unavailable")
                        raise
                    before, external = snapshot(folder), snapshot(outside)
                    self.assert_error(register(client), "storage_conflict")
                    self.assert_rollback(database)
                    self.assertEqual(snapshot(folder), before)
                    self.assertEqual(snapshot(outside), external)

    def test_third_json_write_failure_is_retryable(self):
        atomic_write = AccountStorage.atomic_write_path
        for shape in (None, "partial"):
            with self.subTest(shape=shape), isolated() as (database, folder, client):
                if shape:
                    seed(folder, shape)
                before, writes = snapshot(folder), []

                def fail_third(storage, path, data, **kwargs):
                    writes.append(Path(path).name)
                    if len(writes) == 3:
                        raise OSError(errno.ENOSPC, "synthetic third JSON write failure")
                    return atomic_write(storage, path, data, **kwargs)

                with patch.object(AccountStorage, "atomic_write_path", new=fail_third):
                    self.assert_error(register(client), "storage_full")
                self.assertEqual(len(writes), 3)
                self.assert_rollback(database)
                if shape:
                    self.assertTrue(folder.is_dir())
                    self.assert_preserved(before, folder)
                else:
                    self.assertFalse(folder.exists())
                self.assert_registered(database, folder, register(client))
                self.assert_preserved(before, folder)

    def test_commit_failure_compensation_respects_directory_ownership(self):
        for shape in (None, "partial", "complete"):
            with self.subTest(shape=shape), isolated() as (database, folder, client):
                if shape:
                    seed(folder, shape)
                before = snapshot(folder)
                failure = FailCommitOnce(database.con)
                with patch.object(database, "con", failure), patch.object(
                    app, "_discard_new_account_storage", wraps=app._discard_new_account_storage
                ) as discard:
                    response = register(client)
                self.assertTrue(failure.failed)
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["detail"]["code"], "account_initialization_failed")
                self.assert_rollback(database)
                if shape:
                    discard.assert_not_called()
                    self.assert_preserved(before, folder)
                    self.assert_files(folder)
                else:
                    discard.assert_called_once_with(1, "default")
                    self.assertFalse(folder.exists())
                self.assert_registered(database, folder, register(client))

    def test_failed_request_cannot_delete_storage_claimed_after_rollback(self):
        token = "bootstrap-race-contract-token-0123456789abcdef"
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for mode in ("register", "bootstrap"):
            with self.subTest(mode=mode), isolated() as (database, folder, client):
                if mode == "bootstrap":
                    self.assertTrue(database.configure_bootstrap(digest))
                second = DB(os.environ["SAAS_DB"])
                method = f"{mode}_user"
                original = getattr(database, method)
                failure = FailCommitOnce(database.con)
                claimed = {}
                winner = f"claimed-{mode}-admin"

                def claim_after_rollback():
                    holder = {}
                    initializer = app._new_user_initializer(holder)
                    if mode == "bootstrap":
                        uid = second.bootstrap_user(winner, PASSWORD, digest, initializer=initializer)
                    else:
                        uid = second.register_user(winner, PASSWORD, initializer=initializer,
                                                   allow_first_admin=True, registration_allowed=False)
                    self.assertEqual(uid, 1, "rolled-back user ID must be reused")
                    self.assertEqual(holder, {}, "new request adopted default-only residue")
                    # Valid business data under a default filename must also survive.
                    (folder / "products_config.json").write_text(
                        '{"types": [{"name": "claimed-account-data"}]}', encoding="utf-8",
                    )
                    claimed["files"] = snapshot(folder)

                def fail_then_claim(*args, **kwargs):
                    try:
                        with patch.object(database, "con", failure):
                            return original(*args, **kwargs)
                    except sqlite3.OperationalError:
                        self.assertFalse(database.con.in_transaction)
                        with ThreadPoolExecutor(max_workers=1) as executor:
                            executor.submit(claim_after_rollback).result(timeout=10)
                        raise

                try:
                    with patch.dict(os.environ, {
                        "SAAS_BOOTSTRAP_ENABLED": "1" if mode == "bootstrap" else "0",
                        "SAAS_BOOTSTRAP_TRUSTED_SOURCES": "testclient",
                    }), patch.object(app, "_bootstrap_token_digest", return_value=digest), patch.object(
                        database, method, side_effect=fail_then_claim,
                    ), patch.object(
                        app, "_discard_new_account_storage", wraps=app._discard_new_account_storage,
                    ) as discard:
                        response = client.post(f"/api/auth/{mode}",
                            json={"username": USERNAME, "password": PASSWORD},
                            headers={"X-Bootstrap-Token": token})
                    self.assertTrue(failure.failed)
                    self.assert_error(response, "storage_error")
                    self.assert_counts(database, 1)
                    self.assertIsNone(database.get_user(USERNAME))
                    self.assertEqual(database.get_user(winner)["role"], "admin")
                    self.assertEqual(database.get_bootstrap_state()["created_user_id"], 1)
                    self.assertFalse(database.first_registration_available())
                    self.assertEqual(snapshot(folder), claimed["files"], "late compensation deleted claimed storage")
                    discard.assert_not_called()
                finally:
                    second.con.close()

    def test_cleanup_holds_sqlite_writer_lock_until_directory_removal(self):
        with isolated() as (database, folder, client):
            manager.initialize_new_user_storage(1)
            second = DB(os.environ["SAAS_DB"])
            second.con.execute("PRAGMA busy_timeout=0")

            def cleanup(uid):
                self.assertTrue(database.con.in_transaction)
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    second.register_user("competing-admin", PASSWORD,
                        initializer=app._new_user_initializer({}),
                        allow_first_admin=True, registration_allowed=False)
                self.assertFalse(second.con.in_transaction)
                return app._discard_new_account_storage(uid, "default")

            try:
                self.assertTrue(database.discard_unregistered_storage(1, cleanup))
                self.assertFalse(folder.exists())
                self.assertFalse(database.con.in_transaction)
                self.assert_registered(database, folder, register(client))
            finally:
                second.con.close()

    def test_cleanup_refuses_claimed_users_accounts_and_runtimes(self):
        tables = ("users", "shop_accounts", "worker_runtimes")
        for retained in tables:
            with self.subTest(retained=retained), isolated() as (database, folder, _client):
                database.create_user(USERNAME, PASSWORD, initializer=app._new_user_initializer({}))
                for table in tables:
                    if table != retained:
                        database.con.execute(f"DELETE FROM {table}")
                database.con.commit()
                before = snapshot(folder)
                with patch.object(app, "_discard_new_account_storage") as cleanup:
                    self.assertFalse(app._discard_new_user_storage(1))
                cleanup.assert_not_called()
                self.assertEqual(snapshot(folder), before)
                self.assertFalse(database.con.in_transaction)

    def test_cleanup_does_not_rollback_an_existing_transaction(self):
        with isolated() as (database, folder, _client):
            manager.initialize_new_user_storage(1)
            before = snapshot(folder)
            database.con.execute("BEGIN IMMEDIATE")
            database.con.execute("UPDATE bootstrap_state SET created_user_id=99")
            try:
                with patch.object(app, "_discard_new_account_storage") as cleanup:
                    self.assertFalse(app._discard_new_user_storage(1))
                cleanup.assert_not_called()
                self.assertTrue(database.con.in_transaction)
                self.assertEqual(database.get_bootstrap_state()["created_user_id"], 99)
                self.assertEqual(snapshot(folder), before)
            finally:
                database.con.rollback()
            self.assertIsNone(database.get_bootstrap_state()["created_user_id"])

    def test_cleanup_failure_or_busy_database_preserves_retryable_residue(self):
        with isolated() as (database, folder, client):
            manager.initialize_new_user_storage(1)
            before = snapshot(folder)
            database.con.execute("PRAGMA busy_timeout=0")
            second = DB(os.environ["SAAS_DB"])
            try:
                second.con.execute("BEGIN IMMEDIATE")
                with patch.object(app, "_discard_new_account_storage") as cleanup:
                    self.assertFalse(app._discard_new_user_storage(1))
                cleanup.assert_not_called()
                self.assertEqual(snapshot(folder), before)
                self.assertFalse(database.con.in_transaction)
            finally:
                second.con.rollback()
                second.con.close()
            with patch.object(app, "_discard_new_account_storage", side_effect=PermissionError("fixture cleanup denied")):
                self.assertFalse(app._discard_new_user_storage(1))
            self.assertEqual(snapshot(folder), before)
            self.assertFalse(database.con.in_transaction)
            self.assert_registered(database, folder, register(client))

    def test_closed_database_cannot_authorize_storage_cleanup(self):
        with isolated() as (database, folder, _client):
            manager.initialize_new_user_storage(1)
            before = snapshot(folder)
            database.con.close()
            with patch.object(app, "_discard_new_account_storage") as cleanup:
                self.assertFalse(app._discard_new_user_storage(1))
            cleanup.assert_not_called()
            self.assertEqual(snapshot(folder), before)

    def test_concurrent_registration_creates_only_one_admin(self):
        with isolated() as (first, folder, _client):
            second = DB(os.environ["SAAS_DB"])
            barrier = threading.Barrier(2)

            def create(database, username):
                barrier.wait(timeout=5)
                try:
                    database.register_user(username, PASSWORD,
                        initializer=app._new_user_initializer({}),
                        allow_first_admin=True, registration_allowed=False)
                    return "created"
                except RegistrationClosedError:
                    return "closed"

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(create, database, f"race-admin-{index}")
                               for index, database in enumerate((first, second))]
                    self.assertEqual(sorted(f.result(timeout=15) for f in futures), ["closed", "created"])
                self.assert_counts(first, 1)
                self.assertEqual(first.count_enabled_admins(), 1)
                self.assertEqual(first.get_bootstrap_state()["state"], "consumed")
                self.assert_files(folder)
            finally:
                second.con.close()

    def test_http_errno_reasons_are_specific_and_do_not_leak(self):
        reasons = {errno.EACCES: "storage_permission_denied", errno.EPERM: "storage_permission_denied",
                   errno.EROFS: "storage_read_only", errno.ENOSPC: "storage_full", errno.EIO: "storage_error"}
        for number, reason in reasons.items():
            with self.subTest(errno=number), isolated() as (database, folder, client):
                error = OSError(number, f"username={USERNAME} password={PASSWORD} fake-secret-token",
                                "/fake-private-storage/credentials")
                with patch.object(AccountStorage, "atomic_write_path", side_effect=error):
                    self.assert_error(register(client), reason)
                self.assert_rollback(database)
                self.assertFalse(folder.exists())

    def test_low_level_initialize_still_refuses_every_existing_directory(self):
        for shape in ("empty", "knowledge", "partial", "complete"):
            with self.subTest(shape=shape), isolated() as (_, folder, _client):
                seed(folder, shape)
                before = snapshot(folder)
                with self.assertRaises(OSError):
                    manager.ensure_dir(1, initialize=True)
                self.assertEqual(snapshot(folder), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
