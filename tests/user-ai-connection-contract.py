#!/usr/bin/env python3
"""Offline SQLite/AES-GCM contracts; all credentials, files and requests are fake."""

from __future__ import annotations

import base64
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "backend"))

from ai_customer_service import AIService, AIServiceError, CONNECTION_FILE, CONNECTION_SECRET_FILE  # noqa: E402
from user_ai_connection import UserAIConnections  # noqa: E402


PUBLIC_FIELDS = {"scope", "initialized", "provider", "base_url", "model", "api_key_configured",
                 "connection_status", "revision", "key_revision", "last_error_code"}
CONFIG = {"provider": "openai_chat_completions", "base_url": "https://models.example.test/v1",
          "model": "contract-model", "api_key": "synthetic-contract-key"}


class UserConnectionContracts(unittest.TestCase):
    def setUp(self):
        for target, name in ((socket, "create_connection"), (socket.socket, "connect"), (socket.socket, "connect_ex")):
            guard = patch.object(target, name, side_effect=AssertionError("real network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="user-ai-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "control.sqlite"
        self.db = self.open_db()
        self.db.con.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL, disabled_at REAL);
            CREATE TABLE shop_accounts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                account_key TEXT NOT NULL, display_name TEXT NOT NULL, enabled INTEGER NOT NULL,
                UNIQUE(user_id, account_key));
            INSERT INTO users(id, username) VALUES (1, 'first'), (2, 'second'), (3, 'no-shops');
            INSERT INTO shop_accounts VALUES
                (11, 1, 'shop-a', 'First shop', 1), (12, 1, 'shop-b', 'Second shop', 1),
                (13, 1, 'disabled', 'Disabled shop', 0), (14, 1, 'unused', 'Unused shop', 1),
                (21, 2, 'shop-a', 'Other user shop', 1), (22, 2, 'foreign', 'Foreign shop', 1);
        """)
        self.now = [1000.0]
        self.calls = []
        self.resolutions = []
        self.ai = AIService(self.root / "tenants", environ={
            "SAAS_AI_MASTER_KEY": base64.b64encode(b"f" * 32).decode("ascii"),
        }, clock=lambda: self.now[0], resolver=self.resolver, requester=self.requester)
        self.shared = UserAIConnections(self.db, self.ai)
        self.ai.user_connections = self.shared

    def open_db(self):
        con = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        self.addCleanup(con.close)
        return SimpleNamespace(con=con, _lock=threading.RLock())

    def resolver(self, host, port, type=None):
        self.resolutions.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def requester(self, url, key, payload, headers):
        self.assertFalse(self.db._lock._is_owned(), "request must not hold DB lock")
        self.assertFalse(self.db.con.in_transaction, "request must not hold transaction")
        self.calls.append({"url": url, "key": key, "payload": payload, "headers": headers})
        if url.endswith("/responses"):
            return {"output": [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "OK"}]}]}
        if url.endswith("/api/chat"):
            return {"message": {"role": "assistant", "content": "OK"}}
        return {"choices": [{"message": {"content": "OK"}}]}

    def error(self, code, callback, status=None):
        with self.assertRaises(AIServiceError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        if status is not None:
            self.assertEqual(caught.exception.status_code, status)
        self.assertNotIn(CONFIG["api_key"], str(caught.exception))

    def verified_config(self, uid=1, revision=0, **overrides):
        config = {**CONFIG, **overrides, "expected_revision": revision}
        return config, self.shared.test(uid, **config)["verification_token"]

    def saved(self, uid=1, revision=0, **overrides):
        config, token = self.verified_config(uid, revision, **overrides)
        return self.shared.save(uid, **config, verification_token=token, confirm=True)

    def legacy(self, uid=1, shop_id=11, account_key="shop-a", revision=1, key="legacy-fake-key"):
        scope = (uid, shop_id, account_key)
        metadata = {"version": 2, **CONFIG, "api_key_configured": True,
                    "connection_status": "verified", "revision": revision,
                    "key_revision": revision, "last_error_code": ""}
        metadata.pop("api_key")
        self.ai._write_root_json(scope, CONNECTION_FILE, metadata)
        self.ai._write_root_json(scope, CONNECTION_SECRET_FILE,
                                 self.ai._encrypt_key(scope, key, revision, revision))
        return metadata

    def files(self):
        tenants = self.root / "tenants"
        return {str(path.relative_to(tenants)): path.read_bytes()
                for path in tenants.rglob("*") if path.is_file()}

    def test_constructor_only_creates_schema_and_read_does_not_write_or_decrypt(self):
        queries = []
        self.db.con.set_trace_callback(queries.append)
        with patch.object(self.ai, "_master_keys", side_effect=AssertionError("no key reads")), \
                patch.object(self.ai, "get_connection", side_effect=AssertionError("no legacy reads")):
            other = UserAIConnections(self.db, self.ai)
            row = other.read(1)
            self.assertEqual(set(row), PUBLIC_FIELDS)
            self.assertFalse(row["initialized"])
            self.assertFalse(other.initialized(1))
            self.assertEqual(other.revision(1), 0)
            self.assertEqual(other.legacy_sources(1), [])
        self.assertFalse((self.root / "tenants").exists())
        self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM user_ai_connections").fetchone()[0], 0)
        self.assertEqual(sum("CREATE TABLE IF NOT EXISTS user_ai_connections" in q for q in queries), 1)
        self.assertFalse(any(q.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.db.con.set_trace_callback(None)

    def test_test_does_not_persist_and_reuses_real_safety_adapter(self):
        config, token = self.verified_config()
        self.assertFalse(self.shared.initialized(1))
        self.assertFalse((self.root / "tenants").exists())
        self.assertTrue(self.resolutions)
        call = self.calls[-1]
        self.assertTrue(call["url"].endswith("/v1/chat/completions"))
        self.assertEqual(call["payload"]["messages"][0]["content"], "Return only OK.")
        self.assertEqual(call["payload"]["max_tokens"], 32)
        expected_session = self.ai._routing_session(("user", 1), [{"role": "user", "content": "Connection test."}])
        self.assertEqual(call["headers"]["x-opencode-session"], expected_session)
        self.assertTrue(expected_session.startswith("saas-"))
        decoded = base64.urlsafe_b64decode(token.split(".")[0] + "===").decode()
        self.assertNotIn(CONFIG["api_key"], decoded)
        saved = self.shared.save(1, **config, verification_token=token, confirm=True)
        self.assertEqual(set(saved), PUBLIC_FIELDS)
        self.assertEqual((saved["revision"], saved["key_revision"]), (1, 1))
        raw = dict(self.db.con.execute("SELECT * FROM user_ai_connections").fetchone())
        self.assertIsInstance(raw["ciphertext"], bytes)
        self.assertNotIn(CONFIG["api_key"].encode(), raw["ciphertext"])
        self.assertEqual(len(raw["nonce"]), 12)
        with patch.object(self.shared, "_decrypt", side_effect=AssertionError("GET must not decrypt")), \
                patch.object(self.ai, "_master_keys", side_effect=AssertionError("GET must not load master")):
            self.assertEqual(self.shared.read(1), saved)

    def test_shared_runtime_unifies_shops_without_crossing_users(self):
        self.legacy()
        self.legacy(1, 12, "shop-b", key="another-legacy-key")
        self.legacy(2, 21, "shop-a", key="other-user-legacy-key")
        self.assertEqual(self.ai.get_runtime_connection(1, 11, "shop-a")["api_key"], "legacy-fake-key")
        self.assertFalse(self.shared.initialized(1))
        before = self.files()
        self.saved()
        first = self.ai.get_runtime_connection(1, 11, "shop-a")
        second = self.ai.get_runtime_connection(1, 12, "shop-b")
        self.assertEqual(first, second)
        self.assertEqual(first, {**CONFIG, "revision": 1})
        self.assertEqual(self.ai.get_runtime_connection(2, 21, "shop-a")["api_key"], "other-user-legacy-key")
        self.assertEqual(before, self.files())
        self.assertFalse(self.shared.initialized(2))
        self.error("connection_unconfigured", lambda: self.shared.runtime(2), 503)

    def test_cross_user_tokens_legacy_tokens_and_aad_are_isolated(self):
        config, token = self.verified_config()
        self.error("verification_invalid", lambda: self.shared.save(2, **config, verification_token=token, confirm=True), 409)
        old_token = self.ai._issue_verification((1, 11, "shop-a"), CONFIG["provider"],
                    CONFIG["base_url"], CONFIG["model"], CONFIG["api_key"], 1, 0)
        self.error("verification_invalid", lambda: self.shared.save(1, **config, verification_token=old_token, confirm=True), 409)
        self.error("verification_invalid", lambda: self.ai._verify_token(token, (1, 11, "shop-a"),
                   CONFIG["provider"], CONFIG["base_url"], CONFIG["model"], CONFIG["api_key"], 1, 0), 409)
        self.shared.save(1, **config, verification_token=token, confirm=True)
        self.saved(2, api_key="second-user-key")
        original = self.db.con.execute("SELECT nonce, ciphertext FROM user_ai_connections WHERE user_id=1").fetchone()
        with self.db.con:
            self.db.con.execute("UPDATE user_ai_connections SET nonce=?, ciphertext=? WHERE user_id=2", tuple(original))
        self.error("credential_unavailable", lambda: self.shared.runtime(2), 503)
        self.assertEqual(self.shared.runtime(1)["api_key"], CONFIG["api_key"])
        legacy_secret = self.ai._encrypt_key((1, 11, "shop-a"), "not-user-scope", 1, 1)
        with self.db.con:
            self.db.con.execute("UPDATE user_ai_connections SET nonce=?, ciphertext=? WHERE user_id=1", (
                base64.b64decode(legacy_secret["nonce"]), base64.b64decode(legacy_secret["ciphertext"])))
        self.error("credential_unavailable", lambda: self.shared.runtime(1), 503)

    def test_explicit_migration_and_legacy_files_are_preserved(self):
        self.legacy()
        self.legacy(1, 13, "disabled")
        self.legacy(2, 21, "shop-a", key="other-user-key")
        before = self.files()
        with patch.object(self.ai, "_decrypt_key", side_effect=AssertionError("summary must not decrypt")):
            sources = self.shared.legacy_sources(1)
        self.assertEqual(len(sources), 1)
        self.assertEqual((sources[0]["account_key"], sources[0]["shop_account_id"]), ("shop-a", 11))
        self.assertEqual(sources[0]["name"], sources[0]["display_name"])
        self.assertFalse({"api_key", "nonce", "ciphertext"} & set(sources[0]))
        self.assertNotIn("legacy-fake-key", json.dumps(sources))
        self.error("connection_unconfigured", lambda: self.shared.test(1, **{**CONFIG, "api_key": ""}, expected_revision=0))
        self.error("invalid_payload", lambda: self.shared.test(1, **{**CONFIG, "api_key": ""},
                   expected_revision=0, source_account_key="shop-a"))
        self.error("source_not_found", lambda: self.shared.test(1, **CONFIG, expected_revision=0,
                   source_account_key="foreign", source_revision=1), 404)
        self.error("source_not_found", lambda: self.shared.test(1, **CONFIG, expected_revision=0,
                   source_account_key="disabled", source_revision=1), 404)
        with patch.object(self.ai, "get_runtime_connection", side_effect=AssertionError("no migration recursion")):
            self.saved(api_key="", source_account_key="shop-a", source_revision=1)
        self.assertEqual(self.shared.runtime(1)["api_key"], "legacy-fake-key")
        self.assertEqual(before, self.files())
        self.shared.delete(1, expected_revision=1, confirm=True)
        self.assertEqual(before, self.files())

    def test_source_revision_change_before_save_fails_409(self):
        self.legacy()
        config, token = self.verified_config(api_key="", source_account_key="shop-a", source_revision=1)
        self.legacy(revision=2, key="changed-source-key")
        self.error("source_revision_conflict", lambda: self.shared.save(1, **config, verification_token=token, confirm=True), 409)
        self.assertFalse(self.shared.initialized(1))
        self.error("verification_invalid", lambda: self.shared.save(1, **{**config, "source_revision": 2},
                   verification_token=token, confirm=True), 409)

    def test_source_rechecked_at_commit_and_token_binds_selection(self):
        self.legacy()
        self.legacy(1, 12, "shop-b")
        config, token = self.verified_config(api_key="", source_account_key="shop-a", source_revision=1)
        self.error("verification_invalid", lambda: self.shared.save(1, **{**config, "source_account_key": "shop-b"},
                   verification_token=token, confirm=True), 409)
        encrypt = self.shared._encrypt
        def change_source(*args):
            encrypted = encrypt(*args)
            self.legacy(revision=2)
            return encrypted
        with patch.object(self.shared, "_encrypt", side_effect=change_source):
            self.error("source_revision_conflict", lambda: self.shared.save(1, **config, verification_token=token, confirm=True), 409)
        self.assertFalse(self.shared.initialized(1))

    def test_disabled_source_is_a_conflict_before_save_and_at_commit(self):
        self.legacy()
        config, token = self.verified_config(api_key="", source_account_key="shop-a", source_revision=1)
        with self.db.con:
            self.db.con.execute("UPDATE shop_accounts SET enabled=0 WHERE id=11")
        self.error("source_revision_conflict", lambda: self.shared.save(1, **config,
                   verification_token=token, confirm=True), 409)
        with self.db.con:
            self.db.con.execute("UPDATE shop_accounts SET enabled=1 WHERE id=11")
        encrypt = self.shared._encrypt
        def disable_source(*args):
            result = encrypt(*args)
            with self.db.con:
                self.db.con.execute("UPDATE shop_accounts SET enabled=0 WHERE id=11")
            return result
        with patch.object(self.shared, "_encrypt", side_effect=disable_source):
            self.error("source_revision_conflict", lambda: self.shared.save(1, **config,
                       verification_token=token, confirm=True), 409)
        self.assertFalse(self.shared.initialized(1))

    def test_token_expiry_is_rechecked_at_commit(self):
        config, token = self.verified_config()
        encrypt = self.shared._encrypt
        def expire(*args):
            result = encrypt(*args)
            self.now[0] += 300
            return result
        with patch.object(self.shared, "_encrypt", side_effect=expire):
            self.error("verification_invalid", lambda: self.shared.save(1, **config,
                       verification_token=token, confirm=True), 409)
        self.assertFalse(self.shared.initialized(1))

    def test_existing_caller_transaction_is_not_committed_or_rolled_back(self):
        config, token = self.verified_config()
        self.db.con.execute("UPDATE users SET username='uncommitted' WHERE id=1")
        self.error("credential_store_unavailable", lambda: self.shared.save(1, **config,
                   verification_token=token, confirm=True), 503)
        self.assertTrue(self.db.con.in_transaction)
        self.assertEqual(self.db.con.execute("SELECT username FROM users WHERE id=1").fetchone()[0], "uncommitted")
        self.db.con.rollback()
        self.assertEqual(self.db.con.execute("SELECT username FROM users WHERE id=1").fetchone()[0], "first")
        self.assertFalse(self.shared.initialized(1))

    def test_current_db_class_compatibility(self):
        from db import DB
        actual_db = DB(str(self.root / "actual-control.sqlite"))
        self.addCleanup(actual_db.con.close)
        with actual_db._lock, actual_db.con:
            actual_db.con.execute("INSERT INTO users(id, username, password_hash, created_at) "
                                  "VALUES (91, 'db-contract', 'not-a-real-password', 1)")
        store = UserAIConnections(actual_db, self.ai)
        self.assertFalse(store.initialized(91))
        token = store.test(91, **CONFIG, expected_revision=0)["verification_token"]
        metadata = store.save(91, **CONFIG, expected_revision=0, verification_token=token, confirm=True)
        self.assertEqual(metadata["revision"], 1)
        self.assertEqual(store.runtime(91)["api_key"], CONFIG["api_key"])
        self.assertTrue(store.delete(91, expected_revision=1, confirm=True)["initialized"])
        token = store.test(91, **CONFIG, expected_revision=2)["verification_token"]
        with actual_db._lock, actual_db.con:
            actual_db.con.execute("UPDATE users SET disabled_at=1 WHERE id=91")
        self.error("user_not_found", lambda: store.read(91), 404)
        self.error("user_not_found", lambda: store.save(91, **CONFIG, expected_revision=2,
                   verification_token=token, confirm=True), 404)

    def test_user_settings_do_not_depend_on_selected_shop_or_legacy_storage(self):
        self.legacy()
        self.legacy(1, 12, "shop-b", key="second-legacy-key")
        before = self.files()
        with patch.object(self.ai, "get_connection", side_effect=AssertionError("no implicit legacy migration")):
            saved = self.saved()
            with self.db.con:
                self.db.con.execute("UPDATE shop_accounts SET enabled=0 WHERE user_id=1")
            self.assertEqual(self.shared.read(1), saved)
            self.assertEqual(self.shared.runtime(1)["api_key"], CONFIG["api_key"])
            self.shared.delete(1, expected_revision=1, confirm=True)
            self.error("connection_unconfigured", lambda: self.shared.runtime(1), 503)
        self.assertEqual(self.files(), before)
        self.assertFalse(self.shared.initialized(3))
        self.saved(3)
        self.assertEqual(self.shared.runtime(3)["api_key"], CONFIG["api_key"])

    def test_disabled_user_cannot_reuse_test_token_or_load_credentials(self):
        self.saved()
        config, token = self.verified_config(revision=1)
        before = dict(self.db.con.execute("SELECT * FROM user_ai_connections WHERE user_id=1").fetchone())
        with self.db.con:
            self.db.con.execute("UPDATE users SET disabled_at=1 WHERE id=1")
        for operation in (lambda: self.shared.read(1), lambda: self.shared.runtime(1),
                          lambda: self.shared.test(1, **CONFIG, expected_revision=1),
                          lambda: self.shared.save(1, **config, verification_token=token, confirm=True),
                          lambda: self.shared.delete(1, expected_revision=1, confirm=True)):
            self.error("user_not_found", operation, 404)
        self.assertEqual(dict(self.db.con.execute("SELECT * FROM user_ai_connections WHERE user_id=1").fetchone()), before)
        self.assertEqual(self.files(), {})

    def test_concurrent_saves_on_independent_sqlite_connections_have_one_winner(self):
        db2 = self.open_db()
        other = UserAIConnections(db2, self.ai)
        for revision in (0, 1):
            with self.subTest(revision=revision):
                config_a, token_a = self.verified_config(revision=revision, api_key="race-a")
                config_b, token_b = self.verified_config(revision=revision, api_key="race-b")
                barrier = threading.Barrier(2)
                commit_a, commit_b = self.shared._commit, other._commit
                def commit(original, *args, **kwargs):
                    barrier.wait(timeout=5)
                    return original(*args, **kwargs)
                def save(store, config, token):
                    try:
                        return store.save(1, **config, verification_token=token, confirm=True)
                    except AIServiceError as exc:
                        return exc.code
                with patch.object(self.shared, "_commit", side_effect=lambda *a, **k: commit(commit_a, *a, **k)), \
                        patch.object(other, "_commit", side_effect=lambda *a, **k: commit(commit_b, *a, **k)), \
                        ThreadPoolExecutor(max_workers=2) as pool:
                    a = pool.submit(save, self.shared, config_a, token_a)
                    b = pool.submit(save, other, config_b, token_b)
                    results = [a.result(timeout=10), b.result(timeout=10)]
                self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
                self.assertEqual(results.count("revision_conflict"), 1)
                self.assertEqual(self.shared.revision(1), revision + 1)
                self.assertIn(self.shared.runtime(1)["api_key"], {"race-a", "race-b"})
        self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM user_ai_connections").fetchone()[0], 1)

    def test_delete_recreate_tombstones_invalidate_old_tokens_without_modifying_shops(self):
        self.legacy()
        files = self.files()
        shops = [tuple(row) for row in self.db.con.execute("SELECT * FROM shop_accounts")]
        config, token = self.verified_config()
        tombstone = self.shared.delete(1, expected_revision=0, confirm=True)
        self.assertTrue(tombstone["initialized"])
        self.assertTrue(self.shared.initialized(1))
        self.assertEqual((tombstone["revision"], tombstone["key_revision"]), (1, 1))
        self.error("revision_conflict", lambda: self.shared.save(1, **config, verification_token=token, confirm=True), 409)
        self.error("verification_invalid", lambda: self.shared.save(1, **{**config, "expected_revision": 1},
                   verification_token=token, confirm=True), 409)
        self.error("connection_unconfigured", lambda: self.ai.get_runtime_connection(1, 11, "shop-a"), 503)
        self.saved(revision=1)
        pending_config, pending_token = self.verified_config(revision=2)
        self.shared.delete(1, expected_revision=2, confirm=True)
        self.assertEqual(self.shared.read(1)["key_revision"], 3)
        self.saved(revision=3, api_key="recreated-key")
        self.error("revision_conflict", lambda: self.shared.save(1, **pending_config,
                   verification_token=pending_token, confirm=True), 409)
        self.assertEqual(self.shared.runtime(1)["api_key"], "recreated-key")
        self.assertEqual(files, self.files())
        self.assertEqual(shops, [tuple(row) for row in self.db.con.execute("SELECT * FROM shop_accounts")])

    def test_failed_sqlite_update_rolls_back_metadata_and_ciphertext(self):
        self.saved()
        before = dict(self.db.con.execute("SELECT * FROM user_ai_connections").fetchone())
        config, token = self.verified_config(revision=1, api_key="replacement-key")
        self.db.con.execute("CREATE TEMP TRIGGER fail_shared_write BEFORE UPDATE ON user_ai_connections "
                            "BEGIN SELECT RAISE(ABORT, 'contract forced error'); END")
        self.error("credential_store_unavailable", lambda: self.shared.save(1, **config,
                   verification_token=token, confirm=True), 503)
        self.assertEqual(before, dict(self.db.con.execute("SELECT * FROM user_ai_connections").fetchone()))
        self.assertFalse(self.db.con.in_transaction)
        self.db.con.execute("DROP TRIGGER fail_shared_write")
        self.assertEqual(self.shared.runtime(1)["api_key"], CONFIG["api_key"])

    def test_user_existence_checked_even_when_foreign_keys_disabled(self):
        self.assertEqual(self.db.con.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        for callback in (lambda: self.shared.read(999), lambda: self.shared.initialized(999),
                         lambda: self.shared.revision(999), lambda: self.shared.runtime(999),
                         lambda: self.shared.legacy_sources(999),
                         lambda: self.shared.test(999, **CONFIG, expected_revision=0),
                         lambda: self.shared.save(999, **CONFIG, expected_revision=0, verification_token="x", confirm=True),
                         lambda: self.shared.delete(999, expected_revision=0, confirm=True)):
            self.error("user_not_found", callback, 404)
        config, token = self.verified_config()
        encrypt = self.shared._encrypt
        def remove_user(*args):
            result = encrypt(*args)
            with self.db.con:
                self.db.con.execute("DELETE FROM users WHERE id=1")
            return result
        with patch.object(self.shared, "_encrypt", side_effect=remove_user):
            self.error("user_not_found", lambda: self.shared.save(1, **config,
                       verification_token=token, confirm=True), 404)
        self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM user_ai_connections").fetchone()[0], 0)

    def test_confirmation_validation_token_fingerprint_and_ttl(self):
        config, token = self.verified_config()
        for confirm in (False, "true", 1, None):
            self.error("confirmation_required", lambda: self.shared.save(1, **config,
                       verification_token=token, confirm=confirm), 409)
            self.error("confirmation_required", lambda: self.shared.delete(1, expected_revision=0, confirm=confirm), 409)
        for override in ({"model": "different"}, {"api_key": "different"},
                         {"base_url": "https://other.example.test/v1"}, {"provider": "openai_responses"}):
            self.error("verification_invalid", lambda: self.shared.save(1, **{**config, **override},
                       verification_token=token, confirm=True), 409)
        for malformed in ("", None, "x.y", token + ".extra"):
            self.error("verification_invalid", lambda: self.shared.save(1, **config,
                       verification_token=malformed, confirm=True), 409)
        self.now[0] += 300
        self.error("verification_invalid", lambda: self.shared.save(1, **config, verification_token=token, confirm=True), 409)
        self.assertFalse(self.shared.initialized(1))
        for value in (True, -1, 0.1, "no", None, 2**64):
            self.error("invalid_payload", lambda: self.shared.test(1, **CONFIG, expected_revision=value), 400)
        self.error("invalid_payload", lambda: self.shared.read(True), 400)

    def test_network_does_not_block_db_and_detects_delete_during_test(self):
        def requester(url, key, payload, headers):
            self.requester(url, key, payload, headers)
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(self.shared.delete, 1, expected_revision=0, confirm=True).result(timeout=5)
                self.assertEqual(result["revision"], 1)
            return {"choices": [{"message": {"content": "OK"}}]}
        self.ai.requester = requester
        self.error("revision_conflict", lambda: self.shared.test(1, **CONFIG, expected_revision=0), 409)
        self.assertFalse(self.shared.read(1)["api_key_configured"])

    def test_missing_master_and_upstream_errors_do_not_persist_or_leak(self):
        self.ai.environ = {}
        self.error("credential_store_unavailable", lambda: self.shared.test(1, **CONFIG, expected_revision=0), 503)
        self.assertEqual(self.calls, [])
        self.ai.environ = {"SAAS_AI_MASTER_KEY": base64.b64encode(b"f" * 32).decode()}
        self.ai.requester = lambda *_: (_ for _ in ()).throw(AIServiceError("authentication_failed", 401))
        self.error("authentication_failed", lambda: self.shared.test(1, **CONFIG, expected_revision=0), 401)
        self.ai.requester = lambda *_: (_ for _ in ()).throw(RuntimeError(CONFIG["api_key"]))
        self.error("service_unavailable", lambda: self.shared.test(1, **CONFIG, expected_revision=0), 503)
        self.assertFalse(self.shared.initialized(1))
        self.assertFalse((self.root / "tenants").exists())

    def test_provider_auth_failure_is_retryable_without_persisting_credentials(self):
        # Exercise the real HTTP-status mapping instead of a successful requester.
        self.ai.requester = None
        before = self.shared.read(1)
        for status in (401, 403):
            with self.subTest(upstream_status=status), patch.object(
                self.ai, "_request_pinned", return_value=(
                    status, json.dumps({"error": {"message": CONFIG["api_key"]}}).encode(),
                ),
            ) as request:
                with self.assertRaises(AIServiceError) as caught:
                    self.shared.test(1, **CONFIG, expected_revision=0)
                self.assertEqual(caught.exception.code, "authentication_failed")
                self.assertEqual(caught.exception.status_code, 502)
                self.assertEqual(caught.exception.public_detail()["source"], "provider")
                self.assertEqual(caught.exception.public_detail()["upstream_status"], status)
                self.assertNotIn(CONFIG["api_key"], json.dumps(caught.exception.public_detail()))
                request.assert_called_once()
            self.assertEqual(self.shared.read(1), before)
            self.assertFalse((self.root / "tenants").exists())

        self.ai.requester = self.requester
        saved = self.saved()
        self.assertEqual(saved["connection_status"], "verified")
        self.assertEqual(saved["revision"], 1)

    def test_private_dns_blocked_deepseek_disabled_and_responses_budget(self):
        self.ai.resolver = lambda host, port, type=None: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        self.error("address_unsafe", lambda: self.shared.test(1, **CONFIG, expected_revision=0), 400)
        self.assertEqual(self.calls, [])
        self.ai.resolver = self.resolver
        for model in ("deepseek-v4-flash", "deepseek-v4-pro"):
            self.verified_config(model=model)
            self.assertEqual(self.calls[-1]["payload"]["thinking"], {"type": "disabled"})
        self.verified_config(provider="openai_responses")
        self.assertEqual(self.calls[-1]["payload"]["max_output_tokens"], 256)
        self.verified_config(routing_session="contract-session")
        self.assertEqual(self.calls[-1]["headers"]["x-opencode-session"], "contract-session")
        self.error("invalid_payload", lambda: self.shared.test(1, **CONFIG, expected_revision=0,
                   routing_session="unsafe\r\nheader"), 400)

    def test_keyless_provider_and_blank_key_reuse(self):
        self.saved()
        self.saved(revision=1, api_key="", model="next-model")
        self.assertEqual(self.shared.runtime(1)["api_key"], CONFIG["api_key"])
        self.assertEqual(self.shared.read(1)["key_revision"], 2)
        self.error("connection_unconfigured", lambda: self.shared.test(1, **{**CONFIG,
                   "provider": "anthropic_messages", "api_key": ""}, expected_revision=2), 409)
        self.saved(2, provider="ollama_chat", base_url="https://models.example.test/api", api_key="")
        self.assertFalse(self.shared.read(2)["api_key_configured"])
        self.assertEqual(self.shared.runtime(2)["api_key"], "")
        row = self.db.con.execute("SELECT nonce, ciphertext FROM user_ai_connections WHERE user_id=2").fetchone()
        self.assertEqual(tuple(row), (None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
