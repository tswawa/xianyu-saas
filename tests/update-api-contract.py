#!/usr/bin/env python3
"""Portable update API/DB/HTTP logic tests; POSIX trust is never mocked as real."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import requests
from requests.adapters import BaseAdapter
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from db import DB
import platform_update as protocol
from update_api import UpdateAPI
from version import VERSION
from update_maintenance import UpdateStateError


class Backend:
    def __init__(self):
        self.cap = {"deployment": "docker", "check": True, "download": True,
                    "apply": True, "rollback": True, "reason": "", "instruction": ""}
        self.requests = {}
        self.statuses = {}
        self.failure = None
        self.candidates = [{"version": "0.9.0", "manifest_sha256": "b" * 64}]
        self.valid = True
        self.fetches = []

    def update_capabilities(self):
        return dict(self.cap)

    def fetch_release(self, channel, current, *, deployment):
        self.fetches.append((channel, current, deployment))
        return SimpleNamespace(version="1.1.0", release_id="fixed-release", notes="verified notes")

    def stage_docker_release(self, release, channel, current, operation_id):
        return {"version": release.version, "manifest_sha256": "a" * 64,
                "candidate_path": "/must/not/leak", "release_notes": release.notes}

    def validate_docker_candidate(self, *args):
        if not self.valid:
            raise protocol.PlatformUpdateError("update_artifact_hash_mismatch")

    def available_rollback_versions(self, current):
        return list(self.candidates)

    def read_operation_status(self, operation):
        value = self.statuses.get(operation["operation_id"])
        if isinstance(value, Exception):
            raise value
        return value

    def write_docker_update_request(self, operation):
        if self.failure:
            raise self.failure
        payload = {k: operation[k] for k in ("operation_id", "action", "version", "expected_current_version",
                                             "manifest_sha256", "requested_at", "requested_by")}
        old = self.requests.setdefault(operation["operation_id"], payload)
        assert old == payload, "an idempotent retry cannot change artifact identity"


class OperationsContract(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="update-api-contract-")
        self.path = str(Path(self.directory.name) / "synthetic.db")
        self.db = DB(self.path)
        self.uid = self.db.create_user("synthetic-admin", "Only-Synthetic-Password!", role="admin")
        self.session = self.db.create_token(self.uid)
        self.backend = Backend()
        self.service = UpdateAPI(self.db, "1.0.0", backend=self.backend, is_paused=lambda: False)

    def tearDown(self):
        self.service.stop()
        self.db.con.close()
        self.directory.cleanup()

    def prepare(self, action="apply", version="1.1.0"):
        return self.service.prepare(version, action, self.uid, self.session)

    def confirm(self, row):
        return self.service.confirm(row["operation_id"], row["version"], row["action"], self.uid, self.session)["confirmation_token"]

    def submit(self, row, token, session=None):
        return self.service.submit(row["operation_id"], row["version"], row["action"], token,
                                   self.uid, self.session if session is None else session)

    def assertCode(self, code, call):
        with self.assertRaises(protocol.PlatformUpdateError) as context:
            call()
        self.assertEqual(context.exception.code, code)

    def test_prepare_does_not_publish_and_never_exposes_private_fields(self):
        row = self.prepare()
        self.assertRegex(row["operation_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(row["status"], "staged")
        self.assertEqual(self.backend.requests, {})
        for forbidden in ("candidate_path", "session_digest", "confirmation_digest", "requested_by"):
            self.assertNotIn(forbidden, row)
        self.assertNotIn("/must/not/leak", json.dumps(row))

    def test_repeated_preparation_reuses_verified_identity(self):
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(len(self.db.list_update_operations()), 1)
        self.assertEqual(self.backend.requests, {})
        self.assertCode("confirmation_invalid", lambda: self.service._publish(dict(self.db.get_update_operation(first["operation_id"]))))

    def test_systemd_protocol_keeps_candidate_and_binds_operation_identity(self):
        self.backend.cap["deployment"] = "systemd"
        self.backend.stage_release = Mock(return_value={
            "manifest_sha256": "a" * 64, "candidate_path": "/synthetic/staging/candidate", "release_notes": "notes"})
        self.backend.validate_candidate = Mock()
        self.backend.write_update_intent = Mock()
        row = self.prepare()
        self.assertTrue(self.backend.stage_release.call_args.kwargs["require_maintenance"])
        token = self.confirm(row)
        result = self.submit(row, token)
        self.assertTrue(self.backend.validate_candidate.call_args.kwargs["require_maintenance"])
        self.assertEqual(result["status"], "queued")
        args, kwargs = self.backend.write_update_intent.call_args
        self.assertEqual(args, ("apply", "1.1.0"))
        self.assertEqual(kwargs["operation_id"], row["operation_id"])
        self.assertEqual(kwargs["manifest_sha256"], row["manifest_sha256"])
        self.assertEqual(kwargs["expected_current_version"], "1.0.0")
        self.assertEqual(kwargs["candidate_path"], "/synthetic/staging/candidate")
        self.assertGreater(kwargs["requested_at"], 0)
        self.assertNotIn("candidate_path", row)

    def test_systemd_early_failure_status_converges_without_republication(self):
        self.backend.cap["deployment"] = "systemd"
        self.backend.stage_release = Mock(return_value={
            "manifest_sha256": "a" * 64,
            "candidate_path": "/synthetic/staging/candidate",
            "release_notes": "notes",
        })
        self.backend.validate_candidate = Mock()
        self.backend.write_update_intent = Mock(
            side_effect=protocol.PlatformUpdateError("update_intent_write_failed")
        )
        row = self.prepare()
        token = self.confirm(row)
        self.assertCode("update_intent_write_failed", lambda: self.submit(row, token))
        self.assertEqual(self.backend.write_update_intent.call_count, 1)
        with tempfile.TemporaryDirectory(prefix="systemd-early-status-") as temporary:
            status_root = Path(temporary) / "status"
            status_file = status_root / "operations" / f"{row['operation_id']}.json"
            status_file.parent.mkdir(parents=True)
            payload = {
                "schema": 1,
                "operation_id": row["operation_id"],
                "action": "apply",
                "version": row["version"],
                "current_version": "1.0.0",
                "status": "failed",
                "phase": "failed",
                "updated_at": time.time(),
                "error_code": "update_signature_invalid",
            }
            status_file.write_text(json.dumps(payload), encoding="utf-8")

            def trusted_reader(path):
                self.assertEqual(Path(path), status_file)
                return json.loads(status_file.read_text(encoding="utf-8"))

            self.backend.read_operation_status = protocol.read_operation_status
            with patch.dict(os.environ, {"SAAS_UPDATE_STATUS_DIR": str(status_root)}, clear=False), \
                 patch.object(protocol, "read_trusted_json", side_effect=trusted_reader):
                self.service.reconcile(row["operation_id"])
                terminal = self.service.latest_operation()
                self.service.recover_pending()
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["phase"], "failed")
        self.assertEqual(terminal["current_version"], "1.0.0")
        self.assertEqual(terminal["error_code"], "update_signature_invalid")
        self.assertEqual(self.backend.write_update_intent.call_count, 1)

    def test_confirmation_bound_to_session_action_version_manifest_and_operation(self):
        with self.assertRaisesRegex(ValueError, "confirmation_invalid"):
            self.db.create_admin_confirmation(self.uid, "update.apply")
        row = self.prepare()
        token = self.confirm(row)
        stored = self.db.con.execute("SELECT * FROM admin_confirmations").fetchone()
        self.assertEqual(stored["token_digest"], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(stored["token_digest"], token)
        self.assertFalse(self.db.consume_admin_confirmation(token, self.uid, "update.apply"))
        correct = dict(session_token=self.session, version=row["version"], manifest_sha256=row["manifest_sha256"], operation_id=row["operation_id"])
        for key, value in (("session_token", self.db.create_token(self.uid)), ("version", "1.2.0"),
                           ("manifest_sha256", "c" * 64), ("operation_id", "f" * 32)):
            self.assertFalse(self.db.consume_admin_confirmation(token, self.uid, "update.apply", **{**correct, key: value}))
        self.assertFalse(self.db.consume_admin_confirmation(token, self.uid, "update.rollback", **correct))
        self.assertTrue(self.db.consume_admin_confirmation(token, self.uid, "update.apply", **correct))
        self.assertFalse(self.db.consume_admin_confirmation(token, self.uid, "update.apply", **correct))

    def test_confirm_cannot_create_operations_or_bypass_preparation(self):
        self.assertCode("update_not_staged", lambda: self.service.confirm("f" * 32, "1.1.0", "apply", self.uid, self.session))
        row = self.prepare()
        self.assertCode("confirmation_invalid", lambda: self.service.confirm(row["operation_id"], "1.2.0", "apply", self.uid, self.session))
        self.assertCode("confirmation_invalid", lambda: self.service.confirm(row["operation_id"], "1.1.0", "rollback", self.uid, self.session))

    def test_expiry_logout_and_cross_session_reject_without_publishing(self):
        row = self.prepare()
        token = self.confirm(row)
        second_session = self.db.create_token(self.uid)
        self.assertCode("confirmation_invalid", lambda: self.submit(row, token, second_session))
        self.db.con.execute("UPDATE admin_confirmations SET expires_at=?", (time.time() - 1,))
        self.db.con.commit()
        self.assertCode("confirmation_invalid", lambda: self.submit(row, token))
        fresh = self.confirm(row)
        self.db.delete_token(self.session)
        self.assertCode("confirmation_invalid", lambda: self.submit(row, fresh))
        self.assertEqual(self.backend.requests, {})

    def test_atomic_token_consumption_and_outbox_rollback_on_database_error(self):
        row = self.prepare()
        token = self.confirm(row)
        self.db.con.execute("""CREATE TRIGGER contract_failure BEFORE UPDATE ON platform_update_operations
                            WHEN NEW.status='queued' BEGIN SELECT RAISE(ABORT,'synthetic'); END""")
        self.db.con.commit()
        with self.assertRaises(sqlite3.Error):
            self.submit(row, token)
        self.assertIsNone(self.db.con.execute("SELECT used_at FROM admin_confirmations").fetchone()[0])
        self.assertEqual(self.db.get_update_operation(row["operation_id"])["status"], "staged")
        self.assertEqual(self.backend.requests, {})

    def test_lease_loss_before_queue_preserves_confirmation_for_retry(self):
        row = self.prepare()
        token = self.confirm(row)

        def lost():
            raise protocol.PlatformUpdateError("update_lock_lost")

        self.assertCode(
            "update_lock_lost",
            lambda: self.service.submit(
                row["operation_id"], row["version"], row["action"], token,
                self.uid, self.session, ensure_owned=lost,
            ),
        )
        self.assertEqual(self.db.get_update_operation(row["operation_id"])["status"], "staged")
        self.assertIsNone(self.db.con.execute("SELECT used_at FROM admin_confirmations").fetchone()[0])
        self.assertFalse(self.backend.requests)
        self.assertEqual(self.submit(row, token)["status"], "queued")

    def test_lease_loss_after_queue_keeps_recoverable_confirmed_outbox(self):
        row = self.prepare()
        token = self.confirm(row)
        checks = 0

        def lose_after_queue():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise protocol.PlatformUpdateError("update_lock_lost")

        self.assertCode(
            "update_lock_lost",
            lambda: self.service.submit(
                row["operation_id"], row["version"], row["action"], token,
                self.uid, self.session, ensure_owned=lose_after_queue,
            ),
        )
        queued = self.db.get_update_operation(row["operation_id"])
        self.assertEqual(queued["status"], "queued")
        self.assertIsNotNone(queued["requested_at"])
        self.assertIsNone(queued["published_at"])
        self.assertIsNotNone(self.db.con.execute("SELECT used_at FROM admin_confirmations").fetchone()[0])
        self.assertFalse(self.backend.requests)
        self.service.recover_pending()
        self.assertEqual(len(self.backend.requests), 1)
        self.assertIsNotNone(self.db.get_update_operation(row["operation_id"])["published_at"])

    def test_artifact_change_after_confirmation_is_rejected(self):
        row = self.prepare()
        token = self.confirm(row)
        self.backend.valid = False
        self.assertCode("update_artifact_hash_mismatch", lambda: self.submit(row, token))
        self.assertIsNone(self.db.con.execute("SELECT used_at FROM admin_confirmations").fetchone()[0])
        self.assertFalse(self.backend.requests)

    def test_db_commit_before_publication_survives_restart(self):
        row = self.prepare()
        token = self.confirm(row)
        self.backend.failure = protocol.PlatformUpdateError("update_intent_write_failed")
        self.assertCode("update_intent_write_failed", lambda: self.submit(row, token))
        pending = self.db.get_update_operation(row["operation_id"])
        self.assertEqual(pending["status"], "queued")
        self.assertIsNotNone(self.db.con.execute("SELECT used_at FROM admin_confirmations").fetchone()[0])
        self.db.con.close()
        self.db = DB(self.path)
        self.service = UpdateAPI(self.db, "1.0.0", backend=self.backend, is_paused=lambda: False)
        self.backend.failure = None
        self.service.recover_pending()
        self.assertEqual(len(self.backend.requests), 1)
        self.assertIsNotNone(self.db.get_update_operation(row["operation_id"])["published_at"])
        self.assertEqual(self.submit(row, token)["status"], "queued")
        self.assertEqual(len(self.backend.requests), 1)

    def test_publication_before_commit_retry_is_identical(self):
        row = self.prepare()
        token = self.confirm(row)
        with patch.object(self.db, "mark_update_operation_published", side_effect=sqlite3.OperationalError("synthetic")):
            with self.assertRaises(sqlite3.Error):
                self.submit(row, token)
        original = dict(self.backend.requests[row["operation_id"]])
        self.service.recover_pending()
        self.assertEqual(self.backend.requests[row["operation_id"]], original)
        self.assertEqual(len(self.backend.requests), 1)

    def test_parallel_submit_is_one_operation_and_single_use_for_new_action(self):
        row = self.prepare()
        token = self.confirm(row)
        failures = []
        def submit():
            try:
                self.submit(row, token)
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(failures, [])
        self.assertEqual(len(self.backend.requests), 1)
        self.assertCode("confirmation_invalid", lambda: self.submit(row, secrets.token_urlsafe(32)))
        self.assertCode("update_busy", lambda: self.prepare())

    def test_two_database_connections_cannot_queue_different_operations(self):
        first = self.prepare()
        second = self.prepare("rollback", "0.9.0")
        first_token, second_token = self.confirm(first), self.confirm(second)
        other = DB(self.path)
        try:
            self.assertIsNotNone(self.db.queue_update_operation(first["operation_id"], first_token, self.uid,
                                                               self.session, action="apply", version="1.1.0"))
            self.assertIsNone(other.queue_update_operation(second["operation_id"], second_token, self.uid,
                                                           self.session, action="rollback", version="0.9.0"))
            digest = hashlib.sha256(second_token.encode()).hexdigest()
            self.assertIsNone(other.con.execute("SELECT used_at FROM admin_confirmations WHERE token_digest=?", (digest,)).fetchone()[0])
        finally:
            other.con.close()

    def test_rollback_uses_only_advertised_identity_and_never_fetches_release(self):
        row = self.prepare("rollback", "0.9.0")
        self.assertEqual(self.backend.fetches, [])
        token = self.confirm(row)
        self.backend.candidates[0] = {"version": "0.9.0", "manifest_sha256": "c" * 64}
        self.assertCode("rollback_version_unavailable", lambda: self.submit(row, token))
        self.assertCode("rollback_version_unavailable", lambda: self.prepare("rollback", "0.8.0"))
        self.backend.candidates[0] = {"version": "0.9.0", "manifest_sha256": "b" * 64}
        self.assertEqual(self.submit(row, token)["status"], "queued")

    def test_latest_changed_or_non_new_release_cannot_be_prepared(self):
        self.assertCode("update_version_changed", lambda: self.prepare(version="1.2.0"))
        self.assertEqual(self.backend.requests, {})
        self.service.is_paused = lambda: True
        self.assertCode("update_maintenance_active", self.prepare)

    def test_root_progress_survives_restart_and_terminal_does_not_regress(self):
        row = self.prepare()
        token = self.confirm(row)
        self.submit(row, token)
        stamp = time.time()
        status = {"schema": 1, "operation_id": row["operation_id"], "action": "apply",
                  "version": "1.1.0", "current_version": "1.0.0", "status": "staged",
                  "phase": "staged", "updated_at": stamp, "error_code": ""}
        self.backend.statuses[row["operation_id"]] = status
        self.service.reconcile(row["operation_id"])
        self.assertEqual(self.db.get_update_operation(row["operation_id"])["phase"], "queued")
        status.update(status="running", phase="building", updated_at=stamp + 1)
        self.service.reconcile(row["operation_id"])
        self.assertEqual(self.db.get_update_operation(row["operation_id"])["phase"], "building")
        status.update(phase="verifying_package", updated_at=stamp + 2)
        self.service.reconcile(row["operation_id"])
        self.assertEqual(self.db.get_update_operation(row["operation_id"])["phase"], "building")
        status.update(current_version="1.1.0", status="succeeded", phase="succeeded", updated_at=stamp + 3)
        self.assertEqual(self.service.latest_operation()["status"], "succeeded")
        self.backend.statuses[row["operation_id"]].update(status="running", phase="building", updated_at=stamp + 4)
        self.service.reconcile(row["operation_id"])
        self.assertEqual(self.service.latest_operation()["status"], "succeeded")
        self.assertFalse(self.submit(row, token)["queued"])
        legacy = self.db.get_platform_update("1.1.0", "release")
        self.assertEqual(legacy["release_notes"], "verified notes")
        self.assertEqual(legacy["status"], "succeeded")

    def test_untrusted_root_status_never_causes_republication(self):
        row = self.prepare()
        token = self.confirm(row)
        self.backend.failure = protocol.PlatformUpdateError("update_intent_write_failed")
        self.assertCode("update_intent_write_failed", lambda: self.submit(row, token))
        self.backend.failure = None
        self.backend.statuses[row["operation_id"]] = protocol.PlatformUpdateError("update_state_untrusted")
        self.service.recover_pending()
        self.assertEqual(self.backend.requests, {})


class RecordingAdapter(BaseAdapter):
    def __init__(self, urls):
        self.urls, self.seen = urls, []

    def send(self, request, **kwargs):
        self.seen.append((request.url, dict(request.headers), kwargs))
        status, headers, body = self.urls[request.url]
        response = requests.Response()
        response.status_code, response.headers = status, requests.structures.CaseInsensitiveDict(headers)
        response.raw = io.BytesIO(body)
        response.request, response.url = request, request.url
        return response

    def close(self):
        pass


class TransportContract(unittest.TestCase):
    API = protocol.GITHUB_API_ROOT + "/releases/assets/123"
    CDN = "https://release-assets.githubusercontent.com/github-production-release-asset/123/abc-def?sig=synthetic"

    def session(self, entries):
        session = requests.Session()
        adapter = RecordingAdapter(entries)
        session.mount("https://", adapter)
        session.auth = ("ambient-user", "ambient-password")
        session.headers.update(Authorization="ambient-secret", Cookie="ambient-cookie")
        session.cookies.set("private", "must-not-forward", domain="release-assets.githubusercontent.com")
        return session, adapter

    def test_allowlisted_https_hop_strips_session_and_explicit_credentials(self):
        session, adapter = self.session({self.API: (302, {"Location": self.CDN}, b""), self.CDN: (200, {}, b"verified")})
        with patch.dict(os.environ, {"SAAS_GITHUB_READ_TOKEN": "synthetic-token"}):
            self.assertEqual(protocol._request_bytes(session, self.API, max_bytes=8, asset=True), b"verified")
        self.assertEqual(adapter.seen[0][1]["Authorization"], "Bearer synthetic-token")
        self.assertNotIn("Authorization", adapter.seen[1][1])
        self.assertNotIn("Cookie", adapter.seen[1][1])
        self.assertTrue(all(options["verify"] for _, _, options in adapter.seen))

    def test_bad_redirects_metadata_redirects_and_loops_rejected(self):
        for url in ("http://release-assets.githubusercontent.com/github-production-release-asset/1/a",
                    "https://evil.invalid/github-production-release-asset/1/a",
                    "https://127.0.0.1/github-production-release-asset/1/a",
                    "https://release-assets.githubusercontent.com.evil.invalid/github-production-release-asset/1/a",
                    "https://release-assets.githubusercontent.com/github-production-release-asset/1/%2e%2e/private",
                    "https://release-assets.githubusercontent.com/private", self.API, "/relative"):
            with self.subTest(url=url):
                session, adapter = self.session({self.API: (302, {"location": url}, b"")})
                with self.assertRaises(protocol.PlatformUpdateError) as error:
                    protocol._request_bytes(session, self.API, max_bytes=8, asset=True)
                self.assertEqual(error.exception.code, "update_redirect_rejected")
                self.assertEqual(len(adapter.seen), 1)
        metadata = protocol.GITHUB_API_ROOT + "/releases?per_page=30"
        session, adapter = self.session({metadata: (302, {"Location": self.CDN}, b"")})
        with self.assertRaises(protocol.PlatformUpdateError):
            protocol._request_bytes(session, metadata, max_bytes=8)
        session, adapter = self.session({self.API: (302, {"Location": self.CDN}, b""), self.CDN: (302, {"Location": self.CDN}, b"")})
        with self.assertRaises(protocol.PlatformUpdateError):
            protocol._request_bytes(session, self.API, max_bytes=8, asset=True)
        self.assertEqual(len(adapter.seen), protocol.MAX_ASSET_REDIRECTS + 1)

    def test_redirects_keep_size_limits_and_retry_after(self):
        session, _ = self.session({self.API: (302, {"Location": self.CDN}, b""), self.CDN: (200, {}, b"oversized")})
        with self.assertRaises(protocol.PlatformUpdateError) as error:
            protocol._request_bytes(session, self.API, max_bytes=3, asset=True)
        self.assertEqual(error.exception.code, "update_download_too_large")
        session, _ = self.session({self.API: (429, {"Retry-After": "600"}, b"")})
        with self.assertRaises(protocol.PlatformUpdateError) as error:
            protocol._request_bytes(session, self.API, max_bytes=8, asset=True)
        self.assertEqual(error.exception.code, "update_source_rate_limited")
        self.assertEqual(error.exception.retry_after, 600)


class PublicKeyMountContract(unittest.TestCase):
    def test_windows_permission_bits_require_root_owned_readonly_docker_mount(self):
        import stat
        key = Ed25519PrivateKey.generate().public_key()
        raw = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        cases = (("docker", 0, 0o777, 1, True), ("docker", 0, 0o777, 0, False),
                 ("systemd", 0, 0o777, 1, False), ("docker", 10001, 0o777, 1, False),
                 ("docker", 0, 0o644, 0, True))
        for mode, owner, permissions, flags, accepted in cases:
            with self.subTest(mode=mode, owner=owner, permissions=permissions, flags=flags):
                metadata = SimpleNamespace(st_mode=stat.S_IFREG | permissions, st_uid=owner, st_size=len(raw))
                path = SimpleNamespace(lstat=lambda: metadata)
                with patch.object(protocol, "_public_key_file", return_value=path), \
                     patch.object(protocol, "_read_secure_file", return_value=raw), \
                     patch.object(protocol, "deployment_kind", return_value=mode), \
                     patch.object(protocol.os, "statvfs", create=True, return_value=SimpleNamespace(f_flag=flags)), \
                     patch.object(protocol.os, "ST_RDONLY", 1, create=True):
                    if accepted:
                        self.assertEqual(protocol.load_public_key().public_bytes_raw(), key.public_bytes_raw())
                    else:
                        with self.assertRaises(protocol.PlatformUpdateError) as error:
                            protocol.load_public_key()
                        self.assertEqual(error.exception.code, "update_public_key_invalid")


class DockerStagingContract(unittest.TestCase):
    def test_real_signature_source_validation_and_fixed_artifact_names(self):
        import base64
        import importlib.util
        spec = importlib.util.spec_from_file_location("docker_api_fixture", ROOT / "tests/docker-update-protocol-contract.py")
        fixture_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture_module)
        with tempfile.TemporaryDirectory(prefix="docker-api-synthetic-") as temporary:
            fixture = fixture_module.Fixture(Path(temporary) / "fixture")
            fixture.files["backend/update_maintenance.py"] = (
                b"MAINTENANCE_PROTOCOL = 1\n"
                b"raise AssertionError('candidate maintenance code must never execute')\n"
            )
            archive = fixture.archive()
            raw = fixture_module.encoded(fixture.descriptor(archive))
            signature = base64.b64encode(fixture.key.sign(raw))
            names = fixture_module.PROTOCOL.docker_asset_names(fixture_module.VERSION)
            runtime = b"external legacy runtime manifest"
            release = protocol.ReleaseInfo("synthetic", fixture_module.VERSION, "v" + fixture_module.VERSION,
                "", "signed notes", True, protocol.ReleaseAsset(1, names[0], archive.stat().st_size),
                protocol.ReleaseAsset(2, names[1], len(raw)), protocol.ReleaseAsset(3, names[2], len(signature)),
                protocol.ReleaseAsset(4, protocol._asset_names(fixture_module.VERSION)[1], len(runtime)))
            entries = {release.artifact.api_url: (200, {}, archive.read_bytes()),
                       release.manifest.api_url: (200, {}, raw), release.signature.api_url: (200, {}, signature),
                       release.runtime_manifest.api_url: (200, {}, runtime)}
            operation_id = "c" * 32
            directory = Path(temporary) / operation_id
            def app_private_directory(op, *, create=False):
                self.assertEqual(op, operation_id)
                if create:
                    directory.mkdir(mode=0o700)
                return directory
            # Only the POSIX directory/key provisioning boundary is injected;
            # HTTP source rules, real signature, ZIP verification and bytes are real.
            with patch.object(protocol, "_docker_artifact_directory", side_effect=app_private_directory), \
                 patch.object(protocol, "load_public_key", return_value=fixture.key.public_key()):
                session = requests.Session()
                session.mount("https://", RecordingAdapter(entries))
                staged = protocol.stage_docker_release(release, "release", "0.1.0", operation_id, session=session)
                self.assertEqual(staged["manifest_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual({x.name for x in directory.iterdir()}, {"source.zip", "docker.manifest.json", "docker.manifest.sig"})
                self.assertEqual(staged["candidate_path"], "")
                protocol.validate_docker_candidate(operation_id, release.version, staged["manifest_sha256"])
                source = directory / "source.zip"
                payload = bytearray(source.read_bytes())
                payload[-1] ^= 1
                source.write_bytes(payload)
                with self.assertRaises(protocol.PlatformUpdateError) as error:
                    protocol.validate_docker_candidate(operation_id, release.version, staged["manifest_sha256"])
                self.assertEqual(error.exception.code, "update_artifact_hash_mismatch")
                bad_entries = {**entries, release.signature.api_url: (200, {}, base64.b64encode(b"x" * 64))}
                session = requests.Session()
                session.mount("https://", RecordingAdapter(bad_entries))
                with self.assertRaises(protocol.PlatformUpdateError) as error:
                    protocol.stage_docker_release(release, "release", "0.1.0", operation_id, session=session)
                self.assertEqual(error.exception.code, "docker_signature_invalid")
                bad_entries = {**entries, release.runtime_manifest.api_url: (200, {}, b"tampered runtime")}
                session = requests.Session()
                session.mount("https://", RecordingAdapter(bad_entries))
                with self.assertRaises(protocol.PlatformUpdateError) as error:
                    protocol.stage_docker_release(release, "release", "0.1.0", operation_id, session=session)
                self.assertEqual(error.exception.code, "update_manifest_invalid")

    def test_signed_source_rejects_nonliteral_or_rebound_maintenance_protocol(self):
        import base64
        import importlib.util

        spec = importlib.util.spec_from_file_location("docker_api_protocol_gate", ROOT / "tests/docker-update-protocol-contract.py")
        fixture_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture_module)
        cases = (
            None,
            b"",
            b"MAINTENANCE_PROTOCOL = 2\n",
            b"MAINTENANCE_PROTOCOL = int('1')\n",
            b"MAINTENANCE_PROTOCOL = 1\nMAINTENANCE_PROTOCOL = 1\n",
            b"MAINTENANCE_PROTOCOL = 1\nimport math as MAINTENANCE_PROTOCOL\n",
        )
        with tempfile.TemporaryDirectory(prefix="docker-api-protocol-gate-") as temporary:
            for index, source in enumerate(cases, 1):
                with self.subTest(source=source):
                    fixture = fixture_module.Fixture(Path(temporary) / f"fixture-{index}")
                    if source is not None:
                        fixture.files["backend/update_maintenance.py"] = source
                    archive = fixture.archive()
                    raw = fixture_module.encoded(fixture.descriptor(archive))
                    signature = base64.b64encode(fixture.key.sign(raw))
                    names = fixture_module.PROTOCOL.docker_asset_names(fixture_module.VERSION)
                    runtime = b"external legacy runtime manifest"
                    release = protocol.ReleaseInfo(
                        "synthetic", fixture_module.VERSION, "v" + fixture_module.VERSION, "", "", True,
                        protocol.ReleaseAsset(1, names[0], archive.stat().st_size),
                        protocol.ReleaseAsset(2, names[1], len(raw)),
                        protocol.ReleaseAsset(3, names[2], len(signature)),
                        protocol.ReleaseAsset(4, protocol._asset_names(fixture_module.VERSION)[1], len(runtime)),
                    )
                    entries = {
                        release.artifact.api_url: (200, {}, archive.read_bytes()),
                        release.manifest.api_url: (200, {}, raw),
                        release.signature.api_url: (200, {}, signature),
                        release.runtime_manifest.api_url: (200, {}, runtime),
                    }
                    operation_id = f"{index:032x}"
                    directory = Path(temporary) / operation_id

                    def app_private_directory(op, *, create=False):
                        self.assertEqual(op, operation_id)
                        if create:
                            directory.mkdir(mode=0o700)
                        return directory

                    session = requests.Session()
                    session.mount("https://", RecordingAdapter(entries))
                    with patch.object(protocol, "_docker_artifact_directory", side_effect=app_private_directory), \
                         patch.object(protocol, "load_public_key", return_value=fixture.key.public_key()):
                        with self.assertRaises(protocol.PlatformUpdateError) as error:
                            protocol.stage_docker_release(
                                release, "release", "0.1.0", operation_id, session=session,
                            )
                    self.assertEqual(error.exception.code, "update_maintenance_protocol_unsupported")
                    self.assertFalse(directory.exists(), "rejected source must not leave staged artifacts")


class PublicStateLogicContract(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "SAAS_DOCKER_UPDATE_ROOT": str(Path(tempfile.gettempdir()) / "synthetic-uncreated-update-root")})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_capability_needs_trust_freshness_protocol_deployment_and_key(self):
        key = Ed25519PrivateKey.generate().public_key()
        digest = hashlib.sha256(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).hexdigest()
        good = {"schema": 1, "protocol": 1, "ready": True, "reason": "", "heartbeat_at": time.time(),
                "current_version": VERSION, "deployment_id": "synthetic:app", "public_key_sha256": digest,
                "rollback_versions": []}
        # This tests only field validation. Root ownership is covered by the
        # separate Linux maintenance/IPC tests, never asserted by these doubles.
        with patch.dict(os.environ, {"SAAS_DOCKER_DEPLOYMENT_ID": "synthetic:app"}), \
             patch.object(protocol, "_trusted_update_directory"), \
             patch.object(protocol, "_public_key_file", return_value=SimpleNamespace(parent=Path("/synthetic"), lstat=lambda: SimpleNamespace(st_uid=0))), \
             patch.object(protocol, "load_public_key", return_value=key):
            with patch.object(protocol, "read_trusted_json", return_value=good):
                self.assertTrue(protocol.read_docker_capabilities()["ready"])
            for changed in ({"heartbeat_at": time.time() - 60}, {"heartbeat_at": float("nan")},
                            {"protocol": True}, {"protocol": 2}, {"deployment_id": "other:app"},
                            {"public_key_sha256": "f" * 64}, {"ready": False}, {"current_version": "99.0.0"},
                            {"rollback_versions": ["0.0.1"]}):
                with patch.object(protocol, "read_trusted_json", return_value={**good, **changed}):
                    with self.assertRaises(protocol.PlatformUpdateError):
                        protocol.read_docker_capabilities()
            with patch.object(protocol, "read_trusted_json", side_effect=UpdateStateError()):
                with self.assertRaises(protocol.PlatformUpdateError):
                    protocol.read_docker_capabilities()

    def test_systemd_initialization_binds_key_bundle_entrypoint_and_fixed_exec(self):
        key = Ed25519PrivateKey.generate().public_key()
        key_raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        with tempfile.TemporaryDirectory(prefix="systemd-init-contract-") as temporary:
            temporary = Path(temporary)
            bundle_root = temporary / "updater"
            status_root = temporary / "status"
            for index, relative in enumerate(protocol.SYSTEMD_UPDATER_BUNDLE_FILES, 1):
                path = bundle_root.joinpath(*relative.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"synthetic updater file {index}: {relative}\n".encode())
                path.chmod(0o644)
            entrypoint = bundle_root.joinpath(*protocol.SYSTEMD_UPDATER_ENTRYPOINT_RELATIVE.split("/"))
            actual_lstat = Path.lstat

            def root_owned_read_only(path, *args, **kwargs):
                metadata = actual_lstat(path, *args, **kwargs)
                if path == bundle_root or bundle_root in path.parents:
                    values = list(metadata)
                    values[0] &= ~0o022
                    values[4] = 0
                    return os.stat_result(values)
                return metadata

            environment = {
                "SAAS_UPDATER_BUNDLE_ROOT": str(bundle_root),
                "SAAS_UPDATER_ENTRYPOINT": str(entrypoint),
                "SAAS_UPDATE_STATUS_DIR": str(status_root),
            }
            with patch.dict(os.environ, environment, clear=False), \
                 patch.object(protocol, "_trusted_update_directory"), \
                 patch.object(Path, "lstat", root_owned_read_only), \
                 patch.object(protocol, "load_public_key", return_value=key):
                bundle_sha256, entrypoint_sha256 = protocol._systemd_updater_identity()
                payload = {
                    "schema": 1,
                    "protocol": 1,
                    "public_key_sha256": hashlib.sha256(key_raw).hexdigest(),
                    "bundle_sha256": bundle_sha256,
                    "entrypoint_sha256": entrypoint_sha256,
                    "initialized_at": 1.0,
                }
                expected_path = status_root / protocol.SYSTEMD_INITIALIZATION_FILE

                def read_payload(path):
                    self.assertEqual(Path(path), expected_path)
                    return dict(payload)

                with patch.object(protocol, "read_trusted_json", side_effect=read_payload):
                    self.assertEqual(protocol.read_systemd_initialization()["initialized_at"], 1.0)
                for changed, code in (
                    ({"extra": True}, "update_state_invalid"),
                    ({"protocol": 2}, "update_protocol_mismatch"),
                    ({"public_key_sha256": "f" * 64}, "update_public_key_mismatch"),
                    ({"bundle_sha256": "f" * 64}, "update_updater_identity_mismatch"),
                    ({"entrypoint_sha256": "f" * 64}, "update_updater_identity_mismatch"),
                ):
                    with self.subTest(changed=changed), \
                         patch.object(protocol, "read_trusted_json", return_value={**payload, **changed}):
                        with self.assertRaises(protocol.PlatformUpdateError) as error:
                            protocol.read_systemd_initialization()
                        self.assertEqual(error.exception.code, code)
                with patch.object(protocol, "read_trusted_json", return_value=None):
                    with self.assertRaises(protocol.PlatformUpdateError) as error:
                        protocol.read_systemd_initialization()
                    self.assertEqual(error.exception.code, "update_updater_not_initialized")
                first = bundle_root.joinpath(*protocol.SYSTEMD_UPDATER_BUNDLE_FILES[0].split("/"))
                first.write_bytes(first.read_bytes() + b"tampered")
                with patch.object(protocol, "read_trusted_json", return_value=payload):
                    with self.assertRaises(protocol.PlatformUpdateError) as error:
                        protocol.read_systemd_initialization()
                    self.assertEqual(error.exception.code, "update_updater_identity_mismatch")

            valid_exec = (
                f"{{ path={protocol.SYSTEMD_UPDATER_PYTHON} ; "
                f"argv[]={protocol.SYSTEMD_UPDATER_PYTHON} {entrypoint} ; ignore_errors=no ; }}"
            )
            self.assertTrue(protocol._systemd_exec_start_matches(valid_exec, entrypoint))
            self.assertFalse(protocol._systemd_exec_start_matches(
                valid_exec.replace(str(entrypoint), str(entrypoint) + " --unexpected"), entrypoint,
            ))

    def test_status_projection_identity_and_terminal_semantics(self):
        operation = {"operation_id": "a" * 32, "action": "apply", "version": "1.1.0", "deployment": "docker"}
        raw = {"schema": 1, **operation, "status": "running", "phase": "building", "current_version": "1.0.0",
               "updated_at": time.time(), "error_code": "", "env": "secret", "path": "/host/private"}
        with patch.object(protocol, "read_trusted_json", return_value=raw):
            result = protocol.read_operation_status(operation)
            self.assertEqual(result["phase"], "building")
            self.assertNotIn("env", result)
            self.assertNotIn("path", result)
        for change in ({"operation_id": "b" * 32}, {"version": "1.2.0"}, {"action": "rollback"},
                       {"status": "succeeded", "phase": "verifying"}, {"updated_at": float("inf")}):
            with patch.object(protocol, "read_trusted_json", return_value={**raw, **change}):
                with self.assertRaises(protocol.PlatformUpdateError):
                    protocol.read_operation_status(operation)


APP_TREE = ast.parse((ROOT / "backend/app.py").read_text(encoding="utf-8"))


def app_function(name, namespace):
    node = next(x for x in APP_TREE.body if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == name)
    node = ast.parse(ast.unparse(node)).body[0]
    node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "app-contract-extracted", "exec"), namespace)
    return namespace[name]


class MaintenanceAPILogicContract(unittest.TestCase):
    def test_drain_actual_peer_and_matching_active_state_are_required(self):
        import ipaddress
        state = {"schema": 1, "active": True, "operation_id": "a" * 32, "error_code": ""}
        namespace = {"ipaddress": ipaddress, "HTTPException": HTTPException, "Query": lambda **kwargs: None,
                     "sqlite3": sqlite3, "read_maintenance": lambda: state,
                     "_update_drain_counts": lambda: (0, 0)}
        drain = app_function("internal_update_drain", namespace)
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        self.assertTrue(drain(request, "a" * 32)["ready"])
        self.assertFalse(drain(request, "b" * 32)["ready"])
        state["active"] = False
        self.assertFalse(drain(request, "a" * 32)["ready"])
        state["active"] = True
        for counts in ((1, 0), (0, 1)):
            namespace["_update_drain_counts"] = lambda: counts
            self.assertFalse(drain(request, "a" * 32)["ready"])
        namespace["_update_drain_counts"] = Mock(side_effect=RuntimeError("synthetic unavailable"))
        result = drain(request, "a" * 32)
        self.assertFalse(result["ready"])
        self.assertIsNone(result["active_jobs"])
        self.assertIsNone(result["active_workers"])
        with self.assertRaises(HTTPException) as error:
            drain(SimpleNamespace(client=SimpleNamespace(host="192.0.2.1"), headers={"X-Forwarded-For": "127.0.0.1"}), "a" * 32)
        self.assertEqual(error.exception.status_code, 403)

    def test_maintenance_write_gate_keeps_health_version_and_admin_reads(self):
        namespace = {"threading": threading, "_business_write_lock": threading.Lock(), "_business_writes": 0,
                     "maintenance_active": lambda: True, "JSONResponse": JSONResponse, "HTTPException": HTTPException,
                     "SESSION_COOKIE": "session", "os": os}
        gate = app_function("security_headers", namespace)
        seen = []
        async def next_handler(request):
            seen.append(request.url.path)
            return JSONResponse({"ok": True})
        def request(method, path):
            return SimpleNamespace(method=method, url=SimpleNamespace(path=path), cookies={})
        response = asyncio.run(gate(request("POST", "/api/orders/send"), next_handler))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(seen, [])
        for path in ("/api/health", "/api/ready", "/api/version", "/api/version/public", "/api/admin/updates"):
            self.assertEqual(asyncio.run(gate(request("GET", path), next_handler)).status_code, 200)
        self.assertEqual(namespace["_business_writes"], 0)

    def test_restore_and_watchdog_do_not_mutate_desired_during_maintenance(self):
        namespace = {"maintenance_active": lambda: True}
        self.assertIsNone(app_function("restore_desired_workers", namespace)())
        self.assertIsNone(app_function("_reserve_watchdog_transition", namespace)(1, "default", 123, "rules", "rules_ai"))
        self.assertFalse(app_function("_persist_watchdog_transition", namespace)(None, 1, "default", None, None, 0, 123))
        rows = [{"user_id": 1, "account_id": 1, "pid": None, "desired_state": "running"}]
        stop = Mock()
        namespace = {"db": SimpleNamespace(list_worker_runtimes=lambda: rows), "shutdown_all": stop,
                     "_worker_maintenance_lock": threading.RLock(),
                     "worker_manager": SimpleNamespace(running_count=lambda: 0)}
        app_function("_enter_update_maintenance", namespace)({"active": True})
        stop.assert_called_once()
        self.assertEqual(rows[0]["desired_state"], "running")

    def test_final_worker_start_maintenance_guard_never_persists_failure(self):
        checks = iter((False, False))
        persisted = Mock()
        namespace = {
            "maintenance_active": lambda: next(checks),
            "HTTPException": HTTPException,
            "_ensure_account_lease": lambda _lease: None,
            "_require_account_worker_configuration": lambda *_args, **_kwargs: None,
            "bot_status": lambda *_args: {"connected": True},
            "_prepare_account_worker_start_locked": lambda *_args, **_kwargs: None,
            "bot_start": lambda *_args: (False, "update_maintenance_active"),
            "_persist_worker_observation": persisted,
        }
        start = app_function("_start_account_worker_locked", namespace)
        with self.assertRaises(HTTPException) as error:
            start(
                {"id": 1}, {"id": 2, "account_key": "default"}, "rules", object(),
                validate_configuration=False, automatic=True,
            )
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(error.exception.detail["code"], "update_maintenance_active")
        persisted.assert_not_called()

    def test_probe_and_recovery_lifecycle_are_not_started_at_import(self):
        for node in APP_TREE.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = ast.unparse(node.value.func)
                self.assertNotIn(call, {"restore_desired_workers", "start_watchdog", "update_probe.start", "update_api.start"})
        start = next(x for x in APP_TREE.body if isinstance(x, ast.FunctionDef) and x.name == "start_services")
        source = ast.unparse(start)
        self.assertIn("SAAS_TESTING", source)
        self.assertIn("update_probe.start()", source)
        self.assertIn("update_api.start()", source)
        self.assertIn("maintenance_watcher.start()", source)
        stop = next(x for x in APP_TREE.body if isinstance(x, ast.FunctionDef) and x.name == "shutdown_services")
        stop_source = ast.unparse(stop)
        self.assertIn("update_probe.stop()", stop_source)
        self.assertIn("update_api.stop()", stop_source)
        self.assertIn("maintenance_watcher.stop()", stop_source)
        lifespan = next(x for x in APP_TREE.body if isinstance(x, ast.AsyncFunctionDef) and x.name == "service_lifespan")
        lifespan_source = ast.unparse(lifespan)
        self.assertIn("start_services()", lifespan_source)
        self.assertIn("shutdown_services()", lifespan_source)
        self.assertIn("finally", lifespan_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
