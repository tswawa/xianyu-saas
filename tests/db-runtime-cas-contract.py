#!/usr/bin/env python3
"""Focused cross-connection worker runtime CAS regression checks."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from db import DB  # noqa: E402


class WorkerRuntimeCASTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.tempdir.name) / "saas.db")
        self.primary = DB(self.database_path)
        self.user_id = self.primary.create_user("runtime-cas-user", "password")
        self.account = self.primary.create_shop_account(
            self.user_id, "runtime-cas", "Runtime CAS"
        )

    def tearDown(self):
        self.primary.con.close()
        self.tempdir.cleanup()

    def test_expected_generation_is_atomic_across_db_instances_on_first_insert(self):
        contenders = [DB(self.database_path), DB(self.database_path)]
        gate = sqlite3.connect(self.database_path, check_same_thread=False, timeout=30)
        gate.execute("PRAGMA busy_timeout=30000")
        generation_reads = [threading.Event(), threading.Event()]
        for database, read_event in zip(contenders, generation_reads):
            database.con.set_trace_callback(
                lambda statement, event=read_event: event.set()
                if "SELECT generation FROM worker_runtimes" in statement
                else None
            )

        gate.execute("BEGIN IMMEDIATE")
        start = threading.Barrier(3)
        results = []
        errors = []

        def compete(database, pid):
            start.wait()
            try:
                results.append(
                    database.persist_worker_runtime(
                        self.user_id,
                        self.account["id"],
                        desired_state="running",
                        mode="rules",
                        state="running",
                        pid=pid,
                        generation=1,
                        expected_generation=0,
                    )
                )
            except BaseException as error:  # pragma: no cover - assertion reports it
                errors.append(error)

        threads = [
            threading.Thread(target=compete, args=(contenders[0], 51001)),
            threading.Thread(target=compete, args=(contenders[1], 51002)),
        ]
        try:
            for thread in threads:
                thread.start()
            start.wait()
            deadline = time.time() + 0.5
            while time.time() < deadline and not all(
                event.is_set() for event in generation_reads
            ):
                time.sleep(0.01)
            gate.commit()
            for thread in threads:
                thread.join(5)

            self.assertFalse(errors, errors)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(results), 2)
            self.assertEqual(sum(row is not None for row in results), 1)
            stored = self.primary.get_worker_runtime(self.user_id, self.account["id"])
            self.assertEqual(int(stored["generation"]), 1)
            self.assertIn(int(stored["pid"]), {51001, 51002})
        finally:
            if gate.in_transaction:
                gate.rollback()
            gate.close()
            for database in contenders:
                database.con.close()

    def test_failed_write_rolls_back_and_connection_remains_usable(self):
        database = DB(self.database_path)
        try:
            with self.assertRaisesRegex(ValueError, "account does not belong"):
                database.persist_worker_runtime(
                    self.user_id,
                    self.account["id"] + 1000,
                    desired_state="running",
                    generation=1,
                    expected_generation=0,
                )
            self.assertFalse(database.con.in_transaction)

            row = database.persist_worker_runtime(
                self.user_id,
                self.account["id"],
                desired_state="running",
                state="running",
                pid=52001,
                generation=1,
                expected_generation=0,
            )
            self.assertIsNotNone(row)
            self.assertEqual(int(row["generation"]), 1)

            stale = database.persist_worker_runtime(
                self.user_id,
                self.account["id"],
                desired_state="stopped",
                generation=2,
                expected_generation=0,
            )
            self.assertIsNone(stale)
            self.assertFalse(database.con.in_transaction)
            recovered = database.persist_worker_runtime(
                self.user_id,
                self.account["id"],
                desired_state="stopped",
                generation=2,
                expected_generation=1,
            )
            self.assertIsNotNone(recovered)
            self.assertEqual(int(recovered["generation"]), 2)
        finally:
            database.con.close()


if __name__ == "__main__":
    unittest.main()
