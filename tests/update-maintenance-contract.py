#!/usr/bin/env python3
"""Offline maintenance transitions and consumer admission contract."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from update_maintenance import (
    MaintenanceWatcher, UpdateStateError, _MAX_JSON_DEPTH, _parse_trusted_json,
    parse_maintenance_payload, read_maintenance, read_trusted_json,
    MAINTENANCE_PROTOCOL, supports_maintenance_protocol,
)
from job_consumer import JobConsumer


OPERATION = "a" * 32


def marker(active=True, operation=OPERATION):
    return {"schema": 1, "operation_id": operation, "active": active,
            "phase": "stopping" if active else "succeeded", "updated_at": 1.0}


class MaintenanceContract(unittest.TestCase):
    def test_current_module_declares_supported_protocol(self):
        self.assertEqual(MAINTENANCE_PROTOCOL, 1)
        self.assertTrue(supports_maintenance_protocol((ROOT / "backend/update_maintenance.py").read_bytes()))

    def test_protocol_check_is_static_and_accepts_only_literal_declaration(self):
        self.assertTrue(supports_maintenance_protocol(b"MAINTENANCE_PROTOCOL = 1\nraise RuntimeError('must not execute')\n"))
        for source in (
            b"", b"# MAINTENANCE_PROTOCOL = 1\n", b"MAINTENANCE_PROTOCOL = True\n",
            b"MAINTENANCE_PROTOCOL = 1.0\n", b"MAINTENANCE_PROTOCOL = 2\n",
            b"MAINTENANCE_PROTOCOL = int('1')\n", b"def f():\n    MAINTENANCE_PROTOCOL = 1\n",
            b"if True:\n    MAINTENANCE_PROTOCOL = 1\n", b"MAINTENANCE_PROTOCOL = 1\nMAINTENANCE_PROTOCOL = 1\n",
            b"MAINTENANCE_PROTOCOL = 1\nMAINTENANCE_PROTOCOL += 1\n",
            b"MAINTENANCE_PROTOCOL = 1\ndel MAINTENANCE_PROTOCOL\n",
            b"MAINTENANCE_PROTOCOL = 1\ndef f():\n    global MAINTENANCE_PROTOCOL\n    MAINTENANCE_PROTOCOL = 2\n",
            b"MAINTENANCE_PROTOCOL = other = 1\n", b"MAINTENANCE_PROTOCOL =", b"\xff",
            b"MAINTENANCE_PROTOCOL = 1\n" + b"#" * (256 * 1024), None,
        ):
            with self.subTest(source=repr(source)[:100]):
                self.assertFalse(supports_maintenance_protocol(source))

    def test_protocol_check_rejects_non_name_rebindings_and_star_import(self):
        sources = (
            b"MAINTENANCE_PROTOCOL = 1\nimport json as MAINTENANCE_PROTOCOL\n",
            b"MAINTENANCE_PROTOCOL = 1\nimport MAINTENANCE_PROTOCOL\n",
            b"MAINTENANCE_PROTOCOL = 1\nfrom json import loads as MAINTENANCE_PROTOCOL\n",
            b"MAINTENANCE_PROTOCOL = 1\nfrom package import MAINTENANCE_PROTOCOL\n",
            b"MAINTENANCE_PROTOCOL = 1\nfrom package import *\n",
            b"MAINTENANCE_PROTOCOL = 1\ndef MAINTENANCE_PROTOCOL():\n    pass\n",
            b"MAINTENANCE_PROTOCOL = 1\nasync def MAINTENANCE_PROTOCOL():\n    pass\n",
            b"MAINTENANCE_PROTOCOL = 1\nclass MAINTENANCE_PROTOCOL:\n    pass\n",
            b"MAINTENANCE_PROTOCOL = 1\ntry:\n    pass\nexcept Exception as MAINTENANCE_PROTOCOL:\n    pass\n",
            b"MAINTENANCE_PROTOCOL = 1\nmatch 1:\n    case MAINTENANCE_PROTOCOL:\n        pass\n",
            b"MAINTENANCE_PROTOCOL = 1\nmatch []:\n    case [*MAINTENANCE_PROTOCOL]:\n        pass\n",
            b"MAINTENANCE_PROTOCOL = 1\nmatch {}:\n    case {**MAINTENANCE_PROTOCOL}:\n        pass\n",
        )
        for source in sources:
            with self.subTest(source=source.decode("utf-8")):
                self.assertFalse(supports_maintenance_protocol(source))

    def test_protocol_parser_failure_is_unsupported(self):
        for error in (RecursionError(), MemoryError(), OverflowError()):
            with self.subTest(error=type(error).__name__), patch("update_maintenance.ast.parse", side_effect=error):
                self.assertFalse(supports_maintenance_protocol("MAINTENANCE_PROTOCOL = 1"))

    def test_json_guard_bounds_depth_and_requires_bom_free_utf8(self):
        def nested_payload(depth):
            arrays = depth - 1  # The outer object is the first container.
            return b'{"deep":' + b'[' * arrays + b'0' + b']' * arrays + b'}'

        boundary = nested_payload(_MAX_JSON_DEPTH)
        self.assertIsInstance(_parse_trusted_json(boundary), dict)

        string_value = "[{" * (_MAX_JSON_DEPTH + 2) + '\\"escaped"' + "]}" * (_MAX_JSON_DEPTH + 2)
        string_payload = json.dumps({"text": string_value}, separators=(",", ":")).encode("utf-8")
        self.assertEqual(_parse_trusted_json(string_payload), {"text": string_value})

        normal = json.dumps(marker(), separators=(",", ":"))
        self.assertEqual(_parse_trusted_json(normal.encode("utf-8")), marker())

        rejected = (
            ("over-boundary", nested_payload(_MAX_JSON_DEPTH + 1)),
            ("original-2000-arrays", b'{"deep":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'),
            ("utf-8-bom", b"\xef\xbb\xbf" + normal.encode("utf-8")),
            ("utf-16", normal.encode("utf-16")),
            ("utf-32", normal.encode("utf-32")),
        )
        for label, payload in rejected:
            with self.subTest(label=label), self.assertRaises(UpdateStateError):
                _parse_trusted_json(payload)

    def test_missing_marker_is_inactive(self):
        self.assertFalse(parse_maintenance_payload(None)["active"])

    def test_stale_active_marker_does_not_expire(self):
        result = parse_maintenance_payload(marker())
        self.assertTrue(result["active"])
        self.assertEqual(result["operation_id"], OPERATION)

    def test_explicit_inactive_marker(self):
        self.assertFalse(parse_maintenance_payload(marker(False))["active"])
        self.assertFalse(parse_maintenance_payload({"schema": 1, "active": False})["active"])

    def test_invalid_active_markers_are_rejected(self):
        for change in ({"schema": True}, {"schema": 1.0}, {"schema": 2}, {"active": "false"},
                       {"operation_id": "../elsewhere"}, {"operation_id": ""},
                       {"updated_at": float("nan")}, {"updated_at": True}, {"updated_at": 10 ** 1000}):
            with self.subTest(change=change), self.assertRaises(UpdateStateError):
                parse_maintenance_payload({**marker(), **change})

    def test_read_error_fails_closed(self):
        with patch("update_maintenance.read_trusted_json", side_effect=UpdateStateError()):
            result = read_maintenance()
        self.assertTrue(result["active"])
        self.assertEqual(result["error_code"], "update_maintenance_untrusted")

    def test_unexpected_parser_failures_fail_closed(self):
        for error in (RecursionError(), OverflowError()):
            with self.subTest(error=type(error).__name__), patch("update_maintenance.read_trusted_json", side_effect=error):
                self.assertTrue(read_maintenance()["active"])

    def test_watcher_reader_failure_enters_maintenance(self):
        events = []
        def broken():
            raise RecursionError()
        watcher = MaintenanceWatcher(lambda data: events.append(data["active"]),
                                     lambda: self.fail("must not resume on reader failure"), reader=broken)
        self.assertTrue(watcher.poll_once()["active"])
        self.assertEqual(events, [True])

    def test_watcher_enters_and_resumes_once(self):
        events = []
        state = {"value": marker(False)}
        watcher = MaintenanceWatcher(lambda data: events.append(("enter", data["operation_id"])),
                                     lambda: events.append(("leave", "")), reader=lambda: state["value"])
        watcher.poll_once()
        state["value"] = marker()
        watcher.poll_once()
        watcher.poll_once()
        state["value"] = marker(False)
        watcher.poll_once()
        watcher.poll_once()
        self.assertEqual(events, [("enter", OPERATION), ("leave", "")])

    def test_active_on_start_enters_maintenance(self):
        events = []
        watcher = MaintenanceWatcher(lambda value: events.append(value["operation_id"]),
                                     lambda: self.fail("cannot resume an active marker"), reader=marker)
        watcher.poll_once()
        self.assertEqual(events, [OPERATION])

    def test_failed_resume_remains_pending(self):
        state = {"value": marker()}
        attempts = []
        def leave():
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("synthetic interrupted restore")
        watcher = MaintenanceWatcher(lambda _: None, leave, reader=lambda: state["value"])
        watcher.poll_once()
        state["value"] = marker(False)
        with self.assertRaises(RuntimeError):
            watcher.poll_once()
        watcher.poll_once()
        watcher.poll_once()
        self.assertEqual(len(attempts), 2)

    @unittest.skipUnless(os.name == "posix", "POSIX ownership/fd contract requires Linux")
    def test_trusted_read_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory(prefix="update-marker-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / "maintenance.json"
            path.write_text(json.dumps(marker()), encoding="utf-8")
            path.chmod(0o644)
            self.assertEqual(read_trusted_json(path, owner_uid=os.getuid()), marker())
            link = root / "linked.json"
            link.symlink_to(path)
            with self.assertRaises(UpdateStateError):
                read_trusted_json(link, owner_uid=os.getuid())
            os.link(path, root / "hardlink.json")
            with self.assertRaises(UpdateStateError):
                read_trusted_json(path, owner_uid=os.getuid())

    @unittest.skipUnless(os.name == "posix", "POSIX ownership/fd contract requires Linux")
    def test_ambiguous_and_deep_json_are_rejected(self):
        payloads = [b'{"active":true,"active":false}', b'{"value":NaN}',
                    b'{"deep":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}']
        with tempfile.TemporaryDirectory(prefix="update-marker-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / "state.json"
            for payload in payloads:
                path.write_bytes(payload)
                path.chmod(0o644)
                with self.assertRaises(UpdateStateError):
                    read_trusted_json(path, owner_uid=os.getuid())

    @unittest.skipUnless(os.name == "posix", "POSIX ownership/fd contract requires Linux")
    def test_writable_parent_and_leaf_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="update-marker-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / "state.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o666)
            with self.assertRaises(UpdateStateError):
                read_trusted_json(path, owner_uid=os.getuid())
            path.chmod(0o644)
            root.chmod(0o777)
            with self.assertRaises(UpdateStateError):
                read_trusted_json(path, owner_uid=os.getuid())
            root.chmod(0o700)


class FakeDB:
    def __init__(self):
        self.claims = 0
        self.deferred = []
        self.rows = []

    def claim_jobs(self, owner, **kwargs):
        self.claims += 1
        rows, self.rows = self.rows, []
        return rows

    def defer_job(self, job_id, owner, delay):
        self.deferred.append((job_id, owner, delay))
        return True


class ConsumerMaintenanceContract(unittest.TestCase):
    def consumer(self, db, paused):
        return JobConsumer(db, storage=object(), owner="maintenance-contract", maintenance_func=paused)

    def test_no_claim_while_paused(self):
        db = FakeDB()
        consumer = self.consumer(db, lambda: True)
        self.assertEqual(consumer.run_once(), 0)
        self.assertEqual(db.claims, 0)

    def test_reader_error_does_not_claim(self):
        db = FakeDB()
        def broken():
            raise OSError("synthetic")
        self.assertEqual(self.consumer(db, broken).run_once(), 0)
        self.assertEqual(db.claims, 0)

    def test_pause_after_claim_defers_without_processing(self):
        db = FakeDB()
        db.rows = [{"id": 7, "kind": "shop_sync"}]
        states = iter([False, True])
        consumer = self.consumer(db, lambda: next(states))
        self.assertEqual(consumer.run_once(), 1)
        self.assertEqual(db.claims, 1)
        self.assertEqual(db.deferred[0][0], 7)

    def test_both_lanes_recheck_admission(self):
        for kind in ("shop_sync", "ops_run"):
            with self.subTest(kind=kind):
                db = FakeDB()
                consumer = self.consumer(db, lambda: True)
                self.assertEqual(consumer.process({"id": 9, "kind": kind}), "deferred")
                self.assertEqual(consumer._process({"id": 10, "kind": kind}), "deferred")
                self.assertEqual([row[0] for row in db.deferred], [9, 10])

    def test_consumption_resumes_after_explicit_clear(self):
        db = FakeDB()
        active = {"value": True}
        consumer = self.consumer(db, lambda: active["value"])
        consumer.run_once()
        active["value"] = False
        consumer.run_once()
        self.assertEqual(db.claims, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
