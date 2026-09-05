#!/usr/bin/env python3
"""Offline first-registration and unused-storage contracts using real functions.

Run directly with ``python -B tests/first-registration-contract.py``.  Importing
bot_manager starts only its idle daemon reaper; these tests never import app,
start a worker, or call a network service.  All persistent fixtures live in
TemporaryDirectory, and every SQLite connection closes before its cleanup.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import bot_manager  # noqa: E402
from account_storage import AccountStorage, AccountStorageError  # noqa: E402
from db import (  # noqa: E402
    BootstrapTokenError,
    BootstrapUnavailableError,
    DB,
    RegistrationClosedError,
    verify_password,
)


PASSWORD = "First-Registration-Contract-123!"
TOKEN_DIGEST = hashlib.sha256(b"first-registration-contract-token").hexdigest()
OTHER_DIGEST = hashlib.sha256(b"different-contract-token").hexdigest()
# Independent expected documents: a change to the implementation's defaults
# must not silently change this contract's assertions too.
EMPTY_DOCUMENTS = {
    "redeem_codes.json": [],
    "pan_links.json": {"links": []},
    "reply_rules.json": {"version": 1, "rules": []},
    "automation_settings.json": {
        "version": 1, "strategy": "standard", "enabled": True,
    },
    "products_config.json": {"types": []},
}


def snapshot(directory: Path):
    """Record bytes and file identity without following links or comparing ACLs."""
    if not directory.exists() and not directory.is_symlink():
        return None
    result = {}

    def visit(path):
        info = path.lstat()
        name = path.relative_to(directory).as_posix()
        if stat.S_ISLNK(info.st_mode):
            result[name] = ("symlink", os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            result[name] = ("directory",)
            for child in sorted(path.iterdir()):
                visit(child)
        else:
            result[name] = ("file", path.read_bytes(), info.st_mtime_ns, info.st_ino)

    visit(directory)
    return result


class StorageContractCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="first-registration-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.tenants = self.work / "custom tenants"
        self.environment_root = self.work / "unused environment tenants"
        root_patch = patch.object(bot_manager, "TENANTS_ROOT", str(self.tenants))
        root_patch.start()
        self.addCleanup(root_patch.stop)
        env_patch = patch.dict(os.environ, {"SAAS_TENANTS_DIR": str(self.environment_root)})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.storage = AccountStorage(self.tenants)
        self.database_path = self.work / "control.db"
        self.db = self.open_database()
        self.next_username = 0

    def open_database(self):
        database = DB(str(self.database_path))
        self.addCleanup(database.con.close)
        return database

    def legacy_user(self):
        # This trusted helper deliberately omits the initializer, modelling a
        # pre-marker account without bypassing user/account/runtime creation.
        self.next_username += 1
        return self.db.create_user(f"legacy-{self.next_username}", PASSWORD)

    def initialize_new(self, user_id):
        return bot_manager.ensure_dir(user_id, initialize=True)

    def repair(self, user_id, database=None):
        return (database or self.db).initialize_unused_default_storage(
            user_id, bot_manager.initialize_unused_account_storage
        )

    def marker(self, user_id, database=None):
        return (database or self.db).get_shop_account(user_id)["storage_initialized_at"]

    def assert_defaults(self, user_id):
        directory = self.tenants / str(user_id)
        self.assertEqual(Path(bot_manager.tenant_dir(user_id)), directory)
        self.assertEqual(
            {child.name for child in directory.iterdir()},
            set(EMPTY_DOCUMENTS) | {"ai_knowledge"},
        )
        for name, expected in EMPTY_DOCUMENTS.items():
            self.assertEqual(json.loads((directory / name).read_text(encoding="utf-8")), expected)
        self.assertTrue((directory / "ai_knowledge").is_dir())
        self.assertEqual(list((directory / "ai_knowledge").iterdir()), [])
        self.assertFalse(self.environment_root.exists())
        self.assertFalse(bot_manager.is_running(user_id))

    def assert_db_refuses_repair(self, user_id, database=None):
        database = database or self.db
        before = snapshot(self.tenants)
        account = database.get_shop_account(user_id)
        old_marker = account["storage_initialized_at"] if account is not None else None
        calls = []

        def initializer(uid):
            calls.append(uid)
            return bot_manager.initialize_unused_account_storage(uid)

        self.assertIs(database.initialize_unused_default_storage(user_id, initializer), False)
        self.assertEqual(calls, [], "ineligible accounts must not reach the filesystem callback")
        self.assertFalse(database.con.in_transaction)
        self.assertEqual(snapshot(self.tenants), before)
        if account is not None:
            self.assertEqual(self.marker(user_id, database), old_marker)

    def run_pair(self, first, second):
        barrier = threading.Barrier(2)

        def invoke(operation):
            barrier.wait(timeout=10)
            return operation()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(invoke, operation) for operation in (first, second)]
            # Future.result propagates assertions and unexpected thread errors.
            return [future.result(timeout=45) for future in futures]

    def make_symlink(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except NotImplementedError:
            self.skipTest("native filesystem does not support symbolic links")
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) in {1, 50, 1314}:
                self.skipTest(f"native Windows symlink capability unavailable (WinError {error.winerror})")
            raise


class FirstRegistrationTests(StorageContractCase):
    def test_first_admin_requires_explicit_caller_opt_in(self):
        self.assertEqual(self.db.user_count(), 0)
        state = self.db.get_bootstrap_state()
        self.assertEqual(state["state"], "pending")
        self.assertEqual(state["token_configured"], 0)
        self.assertEqual(
            self.db.con.execute("SELECT token_digest FROM bootstrap_state").fetchone()[0], ""
        )
        for switch in ("0", "1"):
            self.db.set_platform_setting("registration_open", switch)
            for allowed in (False, True):
                with self.subTest(switch=switch, registration_allowed=allowed):
                    # The default False is also how an enabled bootstrap caller
                    # closes the token-free route; no API import is needed.
                    with self.assertRaises(RegistrationClosedError):
                        self.db.register_user(
                            "not-opted-in", PASSWORD, self.initialize_new,
                            registration_allowed=allowed,
                        )
                    with self.assertRaises(RegistrationClosedError):
                        self.db.register_user(
                            "explicitly-disabled", PASSWORD, self.initialize_new,
                            allow_first_admin=False, registration_allowed=allowed,
                        )
                    self.assertTrue(self.db.first_registration_available())
                    self.assertFalse(self.db.con.in_transaction)
        self.assertEqual(self.db.user_count(), 0)
        self.assertFalse(self.tenants.exists())

    def test_first_registration_ignores_owner_switches_and_marks_after_callback(self):
        self.assertEqual(self.db.get_platform_setting("registration_open"), "0")
        observer = self.open_database()
        calls = []

        def initialize(uid):
            calls.append(uid)
            self.assertTrue(self.db.con.in_transaction)
            self.assertIsNone(self.marker(uid))
            self.assertIsNone(observer.get_user_by_id(uid))
            self.assertIsNone(observer.get_shop_account(uid))
            self.initialize_new(uid)
            self.assertIsNone(self.marker(uid))

        uid = self.db.register_user(
            "first-admin", PASSWORD, initialize,
            allow_first_admin=True, registration_allowed=False,
        )
        self.assertEqual(calls, [uid])
        user = observer.get_user_by_id(uid)
        self.assertEqual(user["role"], "admin")
        self.assertTrue(verify_password(PASSWORD, user["password_hash"]))
        self.assertGreater(self.marker(uid, observer), 0)
        self.assertEqual(self.db.count_enabled_admins(), 1)
        self.assert_defaults(uid)
        state = observer.get_bootstrap_state()
        self.assertEqual(state["state"], "consumed")
        self.assertEqual(state["created_user_id"], uid)
        self.assertGreater(state["consumed_at"], 0)
        self.assertEqual(state["token_configured"], 0)
        self.assertFalse(observer.first_registration_available())
        account = observer.get_shop_account(uid)
        runtime = observer.get_worker_runtime(uid, account["id"])
        self.assertEqual(runtime["state"], "waiting_login")
        self.assertEqual(runtime["generation"], 0)
        self.assertIsNone(runtime["pid"])
        self.assertIsNone(runtime["started_at"])
        self.assertFalse(self.db.con.in_transaction)

    def test_first_registration_uses_allocated_non_one_uid_and_custom_root(self):
        # An empty installation can have previously allocated SQLite IDs.  Do
        # not simulate this by deleting a user: consumption must be irreversible.
        for table, sequence in (("users", 40), ("shop_accounts", 90)):
            updated = self.db.con.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (sequence, table)
            )
            if updated.rowcount == 0:
                self.db.con.execute(
                    "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, sequence)
                )
        self.db.con.commit()
        uid = self.db.register_user(
            "allocated-admin", PASSWORD, self.initialize_new, allow_first_admin=True
        )
        self.assertEqual(uid, 41)
        self.assertEqual(self.db.get_shop_account(uid)["id"], 91)
        self.assertEqual(self.db.get_user_by_id(uid)["role"], "admin")
        self.assertGreater(self.marker(uid), 0)
        self.assert_defaults(uid)
        self.assertFalse((self.tenants / "1").exists())
        self.assertFalse((self.tenants / "91").exists())

    def test_omitted_initializer_leaves_storage_unmarked_for_one_repair(self):
        uid = self.db.register_user("no-initializer", PASSWORD, allow_first_admin=True)
        self.assertIsNone(self.marker(uid))
        self.assertFalse(self.tenants.exists())
        self.assertIs(self.repair(uid), True)
        self.assert_defaults(uid)
        self.assertGreater(self.marker(uid), 0)
        self.assert_db_refuses_repair(uid)

    def test_later_owners_require_both_switches_and_never_become_admin(self):
        admin = self.db.register_user("admin", PASSWORD, allow_first_admin=True)
        consumed = dict(self.db.get_bootstrap_state())
        for switch in ("0", "1"):
            self.db.set_platform_setting("registration_open", switch)
            for allowed in (False, True):
                name = f"owner-{switch}-{allowed}"
                with self.subTest(switch=switch, registration_allowed=allowed):
                    if switch == "1" and allowed:
                        uid = self.db.register_user(
                            name, PASSWORD, self.initialize_new,
                            allow_first_admin=True, registration_allowed=allowed,
                        )
                        self.assertEqual(self.db.get_user_by_id(uid)["role"], "owner")
                        self.assertGreater(self.marker(uid), 0)
                        self.assert_defaults(uid)
                    else:
                        before = snapshot(self.tenants)
                        with self.assertRaises(RegistrationClosedError):
                            self.db.register_user(
                                name, PASSWORD, self.initialize_new,
                                allow_first_admin=True, registration_allowed=allowed,
                            )
                        self.assertIsNone(self.db.get_user(name))
                        self.assertEqual(snapshot(self.tenants), before)
                    self.assertFalse(self.db.first_registration_available())
                    self.assertEqual(dict(self.db.get_bootstrap_state()), consumed)
                    self.assertFalse(self.db.con.in_transaction)
        ordinary = self.db.register_user("ordinary-owner", PASSWORD)
        self.assertEqual(self.db.get_user_by_id(ordinary)["role"], "owner")
        self.assertEqual(self.db.get_user_by_id(admin)["role"], "admin")
        self.assertEqual(self.db.count_enabled_admins(), 1)
        self.db.con.execute("DELETE FROM platform_settings WHERE setting_key = 'registration_open'")
        self.db.con.commit()
        with self.assertRaises(RegistrationClosedError):
            self.db.register_user("missing-setting", PASSWORD, allow_first_admin=True)

    def assert_first_admin_race(self, switch):
        self.db.set_platform_setting("registration_open", switch)
        peer = self.open_database()
        self.assertIsNot(self.db.con, peer.con)
        calls = []

        def register(database, name):
            def initialize(uid):
                self.assertTrue(database.con.in_transaction)
                calls.append(uid)
                self.initialize_new(uid)

            try:
                return database.register_user(
                    name, PASSWORD, initialize, allow_first_admin=True
                )
            except RegistrationClosedError:
                return None

        results = self.run_pair(
            lambda: register(self.db, "racing-a"), lambda: register(peer, "racing-b")
        )
        created = [uid for uid in results if uid is not None]
        self.assertEqual(len(created), 1 if switch == "0" else 2)
        self.assertCountEqual(calls, created)
        roles = [self.db.get_user_by_id(uid)["role"] for uid in created]
        self.assertCountEqual(roles, ["admin"] if switch == "0" else ["admin", "owner"])
        self.assertEqual(self.db.user_count(), len(created))
        self.assertEqual(self.db.count_enabled_admins(), 1)
        state = self.db.get_bootstrap_state()
        self.assertEqual(state["state"], "consumed")
        self.assertEqual(self.db.get_user_by_id(state["created_user_id"])["role"], "admin")
        for database in (self.db, peer):
            self.assertFalse(database.first_registration_available())
            self.assertFalse(database.con.in_transaction)
        for uid in created:
            self.assertGreater(self.marker(uid), 0)
            self.assert_defaults(uid)

    def test_two_db_first_registration_race_with_owner_registration_closed(self):
        self.assert_first_admin_race("0")

    def test_two_db_first_registration_race_with_owner_registration_open(self):
        self.assert_first_admin_race("1")

    def test_consumed_state_survives_deletion_reopening_and_registration_switches(self):
        uid = self.db.register_user("temporary-admin", PASSWORD, allow_first_admin=True)
        consumed = dict(self.db.get_bootstrap_state())
        self.assertTrue(self.db.remove_unconfigured_user(uid))
        self.assertEqual(self.db.user_count(), 0)
        reopened = self.open_database()
        for database in (self.db, reopened):
            self.assertFalse(database.first_registration_available())
            self.assertFalse(database.configure_bootstrap(TOKEN_DIGEST))
            database.set_platform_setting("registration_open", "1")
            with self.assertRaises(RegistrationClosedError):
                database.register_user(
                    "cannot-reclaim", PASSWORD, self.initialize_new,
                    allow_first_admin=True, registration_allowed=True,
                )
            with self.assertRaises(BootstrapUnavailableError):
                database.bootstrap_user("cannot-bootstrap", PASSWORD, TOKEN_DIGEST)
            self.assertEqual(dict(database.get_bootstrap_state()), consumed)
            self.assertEqual(database.user_count(), 0)
            self.assertFalse(database.con.in_transaction)
        self.assertFalse(self.tenants.exists())

    def test_bound_token_cannot_be_replaced_or_bypassed_by_first_registration(self):
        self.assertTrue(self.db.configure_bootstrap(TOKEN_DIGEST))
        self.assertTrue(self.db.configure_bootstrap(TOKEN_DIGEST))
        self.assertFalse(self.db.configure_bootstrap(OTHER_DIGEST))
        self.assertFalse(self.db.first_registration_available())
        self.assertTrue(self.db.bootstrap_available(TOKEN_DIGEST))
        self.assertFalse(self.db.bootstrap_available(OTHER_DIGEST))
        self.db.set_platform_setting("registration_open", "1")
        with self.assertRaises(RegistrationClosedError):
            self.db.register_user(
                "token-thief", PASSWORD, self.initialize_new,
                allow_first_admin=True, registration_allowed=True,
            )
        with self.assertRaises(BootstrapTokenError):
            self.db.bootstrap_user("wrong-token", PASSWORD, OTHER_DIGEST, self.initialize_new)
        self.assertEqual(self.db.user_count(), 0)
        self.assertFalse(self.tenants.exists())
        self.assertEqual(self.db.get_bootstrap_state()["state"], "pending")
        self.assertEqual(self.db.get_bootstrap_state()["token_configured"], 1)
        uid = self.db.bootstrap_user("token-admin", PASSWORD, TOKEN_DIGEST, self.initialize_new)
        self.assertEqual(self.db.get_user_by_id(uid)["role"], "admin")
        self.assertGreater(self.marker(uid), 0)
        self.assert_defaults(uid)
        self.assertFalse(self.db.first_registration_available())
        self.assertFalse(self.db.bootstrap_available(TOKEN_DIGEST))
        self.assertEqual(self.db.get_bootstrap_state()["token_configured"], 0)

    def test_token_bootstrap_and_public_registration_race_has_only_token_admin(self):
        self.assertTrue(self.db.configure_bootstrap(TOKEN_DIGEST))
        peer = self.open_database()

        def public_registration():
            with self.assertRaises(RegistrationClosedError):
                peer.register_user(
                    "public-racer", PASSWORD, self.initialize_new, allow_first_admin=True
                )

        results = self.run_pair(
            lambda: self.db.bootstrap_user(
                "bootstrap-racer", PASSWORD, TOKEN_DIGEST, self.initialize_new
            ),
            public_registration,
        )
        self.assertEqual(self.db.user_count(), 1)
        self.assertEqual(self.db.count_enabled_admins(), 1)
        self.assertEqual(self.db.get_user_by_id(results[0])["role"], "admin")
        self.assertIsNone(self.db.get_user("public-racer"))
        self.assertFalse(peer.first_registration_available())
        self.assert_defaults(results[0])

    def assert_initializer_failure_rollback(self, *, first):
        if not first:
            self.db.register_user("existing-admin", PASSWORD, allow_first_admin=True)
            self.db.set_platform_setting("registration_open", "1")
        before_state = dict(self.db.get_bootstrap_state())
        before_count = self.db.user_count()
        sibling = self.storage.ensure_account_dir(900)
        (sibling / "business.json").write_bytes(b'{"preserve": true}')
        before_sibling = snapshot(sibling)
        original_write = AccountStorage.atomic_write_path
        attempted = []

        def fail_second_write(storage, path, data, **kwargs):
            target = Path(path)
            if target.name == "pan_links.json":
                self.assertTrue((target.parent / "redeem_codes.json").is_file())
                raise OSError("injected initialization write failure")
            return original_write(storage, path, data, **kwargs)

        def initialize(uid):
            attempted.append(uid)
            self.assertTrue(self.db.con.in_transaction)
            self.assertIsNone(self.marker(uid))
            self.initialize_new(uid)

        with patch.object(AccountStorage, "atomic_write_path", fail_second_write):
            with self.assertRaises(OSError):
                self.db.register_user(
                    "retry-same-name", PASSWORD, initialize, allow_first_admin=True
                )
        self.assertEqual(len(attempted), 1)
        self.assertFalse((self.tenants / str(attempted[0])).exists())
        self.assertEqual(snapshot(sibling), before_sibling)
        self.assertIsNone(self.db.get_user("retry-same-name"))
        for table in ("users", "shop_accounts", "worker_runtimes"):
            self.assertEqual(
                self.db.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], before_count
            )
        self.assertEqual(dict(self.db.get_bootstrap_state()), before_state)
        self.assertEqual(self.db.first_registration_available(), first)
        self.assertFalse(self.db.con.in_transaction)
        uid = self.db.register_user(
            "retry-same-name", PASSWORD, self.initialize_new, allow_first_admin=True
        )
        self.assertEqual(uid, attempted[0])
        self.assertEqual(self.db.get_user_by_id(uid)["role"], "admin" if first else "owner")
        self.assertGreater(self.marker(uid), 0)
        self.assert_defaults(uid)
        self.assertEqual(snapshot(sibling), before_sibling)

    def test_first_initializer_failure_rolls_back_rows_and_own_directory_then_retries(self):
        self.assert_initializer_failure_rollback(first=True)

    def test_owner_initializer_failure_rolls_back_without_harming_existing_admin(self):
        self.assert_initializer_failure_rollback(first=False)

    def test_registration_never_deletes_a_preexisting_directory_on_failure(self):
        directory = self.storage.ensure_account_dir(1)
        (directory / "cookies.txt").write_bytes(b"preexisting-business-content")
        before = snapshot(self.tenants)
        with self.assertRaises(OSError):
            self.db.register_user(
                "colliding-directory", PASSWORD, self.initialize_new, allow_first_admin=True
            )
        self.assertEqual(snapshot(self.tenants), before)
        self.assertEqual(self.db.user_count(), 0)
        self.assertIsNone(self.db.get_shop_account(1))
        self.assertTrue(self.db.first_registration_available())
        self.assertFalse(self.db.con.in_transaction)


class UnusedStorageRecoveryTests(StorageContractCase):
    def test_partial_empty_non_one_uid_recovery_preserves_files_and_marks_once(self):
        first_uid = self.legacy_user()
        self.db.create_shop_account(first_uid, "unrelated-shop")
        uid = self.legacy_user()
        self.assertNotEqual(uid, 1)
        self.assertNotEqual(self.db.get_shop_account(uid)["id"], uid)
        sibling = self.storage.ensure_account_dir(first_uid)
        (sibling / "cookies.txt").write_bytes(b"untouched-other-user")
        sibling_before = snapshot(sibling)
        directory = self.storage.ensure_account_dir(uid)
        preserved = {
            "reply_rules.json": b'\n{ "rules" : [], "version" : 1 }\n',
            "pan_links.json": b'{\n  "links": []\n}\n',
        }
        for name, data in preserved.items():
            (directory / name).write_bytes(data)
        (directory / "ai_knowledge").mkdir()
        original = snapshot(directory)
        peer = self.open_database()

        def initialize(user_id):
            self.assertEqual(user_id, uid)
            self.assertTrue(self.db.con.in_transaction)
            self.assertIsNone(self.marker(uid))
            self.assertIsNone(self.marker(uid, peer))
            return bot_manager.initialize_unused_account_storage(user_id)

        self.assertIs(self.db.initialize_unused_default_storage(uid, initialize), True)
        self.assert_defaults(uid)
        for name in preserved:
            self.assertEqual(snapshot(directory)[name], original[name])
        self.assertEqual(snapshot(sibling), sibling_before)
        self.assertGreater(self.marker(uid, peer), 0)
        (directory / "automation_settings.json").unlink()
        (directory / "ai_knowledge").rmdir()
        self.assert_db_refuses_repair(uid, peer)
        self.assertFalse((directory / "automation_settings.json").exists())
        self.assertFalse((directory / "ai_knowledge").exists())

    def test_missing_empty_and_complete_default_directories_are_eligible(self):
        for layout in ("missing", "empty", "complete"):
            with self.subTest(layout=layout):
                uid = self.legacy_user()
                if layout != "missing":
                    directory = self.storage.ensure_account_dir(uid)
                    if layout == "complete":
                        for name, document in EMPTY_DOCUMENTS.items():
                            (directory / name).write_text(json.dumps(document), encoding="utf-8")
                        (directory / "ai_knowledge").mkdir()
                self.assertIsNone(self.marker(uid))
                self.assertIs(self.repair(uid), True)
                self.assert_defaults(uid)
                self.assertGreater(self.marker(uid), 0)

    def test_registered_marker_prevents_recreation_after_entire_directory_is_lost(self):
        uid = self.db.register_user(
            "initialized-admin", PASSWORD, self.initialize_new, allow_first_admin=True
        )
        old_marker = self.marker(uid)
        shutil.rmtree(self.tenants / str(uid))
        reopened = self.open_database()
        self.assert_db_refuses_repair(uid, reopened)
        self.assertEqual(self.marker(uid, reopened), old_marker)
        self.assertFalse((self.tenants / str(uid)).exists())

    def test_nonempty_edited_and_malformed_default_json_are_never_overwritten(self):
        cases = [
            ("redeem_codes.json", b'[{"code": "preserve"}]'),
            ("pan_links.json", b'{"links": ["preserve"]}'),
            ("reply_rules.json", b'{"version": 1, "rules": [{"reply": "preserve"}]}'),
            ("automation_settings.json", b'{"version": 1, "strategy": "standard", "enabled": false}'),
            ("products_config.json", b'{"types": [{"payload": "preserve"}]}'),
            ("reply_rules.json", b'{"version": 1, "rules": ['),
            ("products_config.json", b""),
            ("redeem_codes.json", b"{}"),
            ("automation_settings.json", b'{"version": 1, "strategy": "standard", "enabled": 1}'),
        ]
        for name, payload in cases:
            with self.subTest(name=name, payload=payload):
                uid = self.legacy_user()
                directory = self.storage.ensure_account_dir(uid)
                (directory / name).write_bytes(payload)
                before = snapshot(directory)
                self.assertIs(self.repair(uid), False)
                self.assertEqual(snapshot(directory), before)
                self.assertIsNone(self.marker(uid))
                self.assertFalse(self.db.con.in_transaction)

    def test_invalid_utf8_fails_closed_without_mutating_or_marking(self):
        uid = self.legacy_user()
        directory = self.storage.ensure_account_dir(uid)
        (directory / "reply_rules.json").write_bytes(b"\xff\xfe")
        before = snapshot(directory)
        with self.assertRaises(AccountStorageError):
            self.repair(uid)
        self.assertEqual(snapshot(directory), before)
        self.assertIsNone(self.marker(uid))
        self.assertFalse(self.db.con.in_transaction)

    def test_other_business_files_directories_and_nonempty_knowledge_prevent_repair(self):
        for name, is_directory in (
            ("cookies.txt", False), ("bot.log", False), ("shop_snapshot.json", False),
            ("other-business", True), ("ai_knowledge/article.txt", False),
            ("ai_knowledge/empty-subdirectory", True), ("reply_rules.json", True),
        ):
            with self.subTest(name=name):
                uid = self.legacy_user()
                directory = self.storage.ensure_account_dir(uid)
                target = directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if is_directory:
                    target.mkdir()
                else:
                    target.write_bytes(b"")
                before = snapshot(directory)
                self.assertIs(self.repair(uid), False)
                self.assertEqual(snapshot(directory), before)
                self.assertIsNone(self.marker(uid))
                self.assertFalse(self.db.con.in_transaction)

    def test_used_account_metadata_and_existing_markers_never_reach_callback(self):
        for table, field, value in (
            ("users", "disabled_at", 0.0),
            ("shop_accounts", "enabled", 0),
            ("shop_accounts", "status", "ready"),
            ("shop_accounts", "generation", 1),
            ("shop_accounts", "account_ref", "previous-account"),
            ("shop_accounts", "last_verified_at", 0.0),
            ("shop_accounts", "last_sync_at", 0.0),
            ("shop_accounts", "storage_initialized_at", 0.0),
        ):
            with self.subTest(field=field):
                uid = self.legacy_user()
                directory = self.storage.ensure_account_dir(uid)
                (directory / "redeem_codes.json").write_bytes(b"[]")
                identity = "id" if table == "users" else "user_id"
                self.db.con.execute(
                    f"UPDATE {table} SET {field} = ? WHERE {identity} = ?", (value, uid)
                )
                self.db.con.commit()
                self.assert_db_refuses_repair(uid)
                self.assertFalse((directory / "reply_rules.json").exists())

    def test_missing_user_or_default_account_never_reaches_callback(self):
        self.assert_db_refuses_repair(404)
        uid = self.legacy_user()
        self.db.con.execute("DELETE FROM shop_accounts WHERE user_id = ?", (uid,))
        self.db.con.commit()
        self.assert_db_refuses_repair(uid)

    def test_started_or_active_runtime_never_reaches_callback(self):
        for fields in (
            {"pid": 12345}, {"started_at": 0.0}, {"generation": 1},
            {"state": "running"}, {"state": "starting"},
            {"state": "stopped", "started_at": 1.0},
        ):
            with self.subTest(fields=fields):
                uid = self.legacy_user()
                account = self.db.get_shop_account(uid)
                self.db.update_worker_runtime(uid, account["id"], **fields)
                self.assert_db_refuses_repair(uid)

    def test_unstarted_idle_or_missing_runtime_and_empty_legacy_config_are_eligible(self):
        for state in ("waiting_login", "stopped", "degraded", None):
            with self.subTest(state=state):
                uid = self.legacy_user()
                account = self.db.get_shop_account(uid)
                if state is None:
                    self.db.con.execute("DELETE FROM worker_runtimes WHERE user_id = ?", (uid,))
                    self.db.con.commit()
                else:
                    self.db.update_worker_runtime(uid, account["id"], state=state)
                self.db.save_config(uid, {"keywords_json": "{}", "bot_running": 0})
                self.assertIs(self.repair(uid), True)
                self.assert_defaults(uid)

    def test_any_default_or_legacy_job_history_prevents_callback(self):
        for legacy in (False, True):
            for status in ("queued", "completed", "dead_letter"):
                with self.subTest(legacy=legacy, status=status):
                    uid = self.legacy_user()
                    account = self.db.get_shop_account(uid)
                    job = self.db.enqueue_job(
                        uid, "sync_shop", "history", account_id=None if legacy else account["id"]
                    )
                    self.db.con.execute(
                        "UPDATE jobs SET status = ?, completed_at = ? WHERE id = ?",
                        (status, time.time() if status != "queued" else None, job["id"]),
                    )
                    self.db.con.commit()
                    self.assert_db_refuses_repair(uid)

    def test_active_lease_blocks_callback_but_released_lease_allows_repair(self):
        uid = self.legacy_user()
        account = self.db.get_shop_account(uid)
        resource = f"worker:{uid}:{account['id']}"
        self.assertEqual(
            self.db.acquire_control_lease(resource, "contract", lease_seconds=60, cooldown_seconds=0),
            "acquired",
        )
        self.assert_db_refuses_repair(uid)
        self.assertTrue(self.db.release_control_lease(resource, "contract"))
        self.assertIs(self.repair(uid), True)
        self.assert_defaults(uid)

    def test_nonempty_legacy_config_fields_prevent_callback(self):
        for field, value in (
            ("bot_running", 1), ("llm_api_key", "legacy-test-key"),
            ("llm_base_url", "https://unused.invalid"), ("llm_model", "legacy-model"),
            ("keywords_json", '{"question": "answer"}'),
        ):
            with self.subTest(field=field):
                uid = self.legacy_user()
                self.db.save_config(uid, {"keywords_json": "{}"})
                # Public config writes no longer accept llm_* fields; raw SQL
                # here represents legacy data, not a substitute implementation.
                self.db.con.execute(
                    f"UPDATE tenant_configs SET {field} = ? WHERE user_id = ?", (value, uid)
                )
                self.db.con.commit()
                self.assert_db_refuses_repair(uid)

    def test_other_user_or_shop_history_does_not_block_unused_default(self):
        other_uid = self.legacy_user()
        uid = self.legacy_user()
        other_shop = self.db.create_shop_account(uid, "other-shop")
        self.db.enqueue_job(other_uid, "sync_shop", "other-user")
        self.db.enqueue_job(uid, "sync_shop", "other-shop", account_id=other_shop["id"])
        resource = f"worker:{uid}:{other_shop['id']}"
        self.assertEqual(self.db.acquire_control_lease(resource, "other-shop"), "acquired")
        self.assertIs(self.repair(uid), True)
        self.assert_defaults(uid)
        self.assertIsNone(self.marker(other_uid))
        self.assertIsNone(self.db.get_shop_account(uid, other_shop["id"])["storage_initialized_at"])

    def test_repair_write_failure_leaves_unmarked_partial_files_for_safe_retry(self):
        uid = self.legacy_user()
        original_write = AccountStorage.atomic_write_path

        def fail_third_write(storage, path, data, **kwargs):
            if Path(path).name == "reply_rules.json":
                raise OSError("injected repair publication failure")
            return original_write(storage, path, data, **kwargs)

        with patch.object(AccountStorage, "atomic_write_path", fail_third_write):
            with self.assertRaisesRegex(OSError, "injected repair"):
                self.repair(uid)
        directory = self.tenants / str(uid)
        self.assertEqual({child.name for child in directory.iterdir()}, {"redeem_codes.json", "pan_links.json"})
        partial = snapshot(directory)
        self.assertIsNone(self.marker(uid))
        self.assertFalse(self.db.con.in_transaction)
        peer = self.open_database()
        self.assertIs(self.repair(uid, peer), True)
        self.assert_defaults(uid)
        for name in ("redeem_codes.json", "pan_links.json"):
            self.assertEqual(snapshot(directory)[name], partial[name])
        self.assertGreater(self.marker(uid), 0)

    def test_two_db_concurrent_recovery_calls_callback_once_under_write_transaction(self):
        uid = self.legacy_user()
        peer = self.open_database()
        entered = threading.Event()
        release = threading.Event()
        second_write_attempt = threading.Event()
        calls = []
        peer.con.set_trace_callback(
            lambda sql: second_write_attempt.set() if sql.strip().upper() == "BEGIN IMMEDIATE" else None
        )

        def recover(database):
            def initialize(user_id):
                self.assertTrue(database.con.in_transaction)
                self.assertIsNone(self.marker(user_id, database))
                calls.append(user_id)
                entered.set()
                if not release.wait(timeout=10):
                    raise AssertionError("test did not release initialization callback")
                return bot_manager.initialize_unused_account_storage(user_id)

            return database.initialize_unused_default_storage(uid, initialize)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(recover, self.db)
                try:
                    self.assertTrue(entered.wait(timeout=10), "first callback did not run")
                    second = pool.submit(recover, peer)
                    self.assertTrue(second_write_attempt.wait(timeout=10), "second writer did not contend")
                    self.assertFalse(second.done(), "second writer escaped the first write transaction")
                finally:
                    release.set()
                self.assertIs(first.result(timeout=45), True)
                self.assertIs(second.result(timeout=45), False)
        finally:
            release.set()
            peer.con.set_trace_callback(None)
        self.assertEqual(calls, [uid])
        self.assertGreater(self.marker(uid, peer), 0)
        self.assert_defaults(uid)
        self.assertFalse(self.db.con.in_transaction)
        self.assertFalse(peer.con.in_transaction)

    def test_symlinked_default_file_or_knowledge_directory_is_not_repaired(self):
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "source.json").write_bytes(b"[]")
        for name, is_directory in (("redeem_codes.json", False), ("ai_knowledge", True)):
            with self.subTest(name=name):
                uid = self.legacy_user()
                directory = self.storage.ensure_account_dir(uid)
                self.make_symlink(
                    directory / name, outside if is_directory else outside / "source.json",
                    directory=is_directory,
                )
                before = snapshot(directory)
                outside_before = snapshot(outside)
                with self.assertRaises(AccountStorageError):
                    self.repair(uid)
                self.assertEqual(snapshot(directory), before)
                self.assertEqual(snapshot(outside), outside_before)
                self.assertIsNone(self.marker(uid))
                self.assertFalse(self.db.con.in_transaction)


class SafeInitializationTests(StorageContractCase):
    def test_create_text_and_atomic_create_mode_preserve_existing_files(self):
        target = self.storage.create_text(41, "default", "seed.json", "first contents")
        self.assertEqual(target, self.tenants / "41" / "seed.json")
        self.assertEqual(target.read_bytes(), b"first contents")
        before = snapshot(target.parent)
        with self.assertRaises(FileExistsError):
            self.storage.create_text(41, "default", "seed.json", "must not replace")
        self.assertEqual(snapshot(target.parent), before)
        with self.assertRaises(FileExistsError):
            self.storage.atomic_write_path(target, "also must not replace", replace_existing=False)
        self.assertEqual(snapshot(target.parent), before)
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_concurrent_create_text_has_one_winner_without_overwrite_or_temp_leaks(self):
        directory = self.storage.ensure_account_dir(41)
        peer = AccountStorage(self.tenants)

        def publish(storage, text):
            try:
                storage.create_text(41, "default", "seed.json", text)
                return text
            except FileExistsError:
                return None

        results = self.run_pair(
            lambda: publish(self.storage, "writer-a"), lambda: publish(peer, "writer-b")
        )
        winners = [value for value in results if value is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual((directory / "seed.json").read_text(encoding="utf-8"), winners[0])
        self.assertEqual({path.name for path in directory.iterdir()}, {"seed.json"})

    def test_create_text_refuses_existing_and_dangling_file_symlinks(self):
        directory = self.storage.ensure_account_dir(41)
        for exists in (True, False):
            with self.subTest(target_exists=exists):
                outside = self.work / f"outside-{exists}.json"
                if exists:
                    outside.write_bytes(b"external original")
                link = directory / f"linked-{exists}.json"
                self.make_symlink(link, outside)
                before = snapshot(directory)
                with self.assertRaises(AccountStorageError):
                    self.storage.create_text(41, "default", link.name, "never follow")
                self.assertEqual(snapshot(directory), before)
                self.assertTrue(link.is_symlink())
                self.assertEqual(outside.exists(), exists)
                if exists:
                    self.assertEqual(outside.read_bytes(), b"external original")

    def test_create_text_refuses_symlinked_tenant_and_initialization_preserves_it(self):
        self.tenants.mkdir()
        outside = self.work / "outside directory"
        outside.mkdir()
        (outside / "business.txt").write_bytes(b"external original")
        link = self.tenants / "41"
        self.make_symlink(link, outside, directory=True)
        before = snapshot(outside)
        with self.assertRaises(AccountStorageError):
            self.storage.create_text(41, "default", "seed.json", "never follow")
        with self.assertRaises(OSError):
            bot_manager.ensure_dir(41, initialize=True)
        self.assertTrue(link.is_symlink())
        self.assertEqual(snapshot(outside), before)

    def test_new_initialization_rejects_preexisting_empty_default_and_business_directories(self):
        for uid, kind in enumerate(("empty", "default", "business"), 41):
            with self.subTest(kind=kind):
                directory = self.storage.ensure_account_dir(uid)
                if kind == "default":
                    (directory / "redeem_codes.json").write_bytes(b"[]")
                elif kind == "business":
                    (directory / "cookies.txt").write_bytes(b"preserved content")
                    (directory / "ai_knowledge").mkdir()
                    (directory / "ai_knowledge" / "article.txt").write_bytes(b"knowledge")
                before = snapshot(self.tenants)
                with self.assertRaisesRegex(OSError, "refusing to reseed"):
                    bot_manager.ensure_dir(uid, initialize=True)
                self.assertEqual(snapshot(self.tenants), before)
                self.assertTrue(directory.is_dir())

    @unittest.skipUnless(sys.platform.startswith("linux"), "strict POSIX mode assertions require Linux")
    def test_new_initialization_and_create_text_use_private_linux_permissions(self):
        directory = Path(bot_manager.ensure_dir(41, initialize=True))
        for path in (self.tenants, directory, directory / "ai_knowledge"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for name in EMPTY_DOCUMENTS:
            self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)
        created = self.storage.create_text(42, "default", "seed.json", "[]")
        self.assertEqual(stat.S_IMODE(created.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
