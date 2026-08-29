import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.migrate_state import migrate


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload), encoding="utf-8")


class MigrationTests(unittest.TestCase):
    def make_source(self, root):
        source = Path(root) / "legacy"
        (source / "data").mkdir(parents=True)
        write_json(
            source / "redeem_codes.json",
            [
                {"code": "USED-CODE", "used": True},
                {"code": "AVAILABLE-CODE", "used": False},
            ],
        )
        write_json(
            source / "trial_codes.json",
            [{"code": "TRIAL-CODE", "used": False}],
        )
        write_json(
            source / "pan_links.json",
            {"links": [{"url": "https://example.invalid", "code": "ABCD"}]},
        )
        with sqlite3.connect(source / "data" / "chat_history.db") as conn:
            conn.execute(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    chat_id TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO messages(user_id, item_id, role, content, chat_id) VALUES (?, ?, ?, ?, ?)",
                ("buyer", "item", "user", "legacy message", "chat"),
            )
        return source

    def test_isolated_migration_preserves_state_and_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            source = self.make_source(root)
            destination = Path(root) / "runtime"

            summary = migrate(str(source), str(destination))

            self.assertEqual(summary["chat_messages"], 1)
            self.assertEqual(summary["inventory"]["redeem"]["legacy_used"], 1)
            self.assertEqual(summary["inventory"]["redeem"]["available"], 1)
            self.assertEqual(os.stat(destination).st_mode & 0o777, 0o700)
            for name in (
                "redeem_codes.json",
                "trial_codes.json",
                "pan_links.json",
                "chat_history.db",
                "delivery_state.db",
            ):
                with self.subTest(name=name):
                    self.assertEqual(os.stat(destination / name).st_mode & 0o777, 0o600)

            with sqlite3.connect(destination / "chat_history.db") as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
                self.assertIn("source_id", columns)
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            with sqlite3.connect(destination / "delivery_state.db") as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertIsNone(conn.execute("PRAGMA foreign_key_check").fetchone())

    def test_migration_refuses_overwrite_and_corrupt_input(self):
        with tempfile.TemporaryDirectory() as root:
            source = self.make_source(root)
            destination = Path(root) / "runtime"
            migrate(str(source), str(destination))
            with self.assertRaises(RuntimeError):
                migrate(str(source), str(destination))

        with tempfile.TemporaryDirectory() as root:
            source = self.make_source(root)
            (source / "redeem_codes.json").write_text("not json", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                migrate(str(source), str(Path(root) / "runtime"))

    def test_legacy_send_ledgers_are_quarantined_as_metadata_only(self):
        with tempfile.TemporaryDirectory() as root:
            source = self.make_source(root)
            write_json(source / "redeem_sent.json", {"buyer": "secret-code"})
            write_json(source / "pan_sent.json", [{"buyer": "secret-link"}])
            destination = Path(root) / "runtime"

            migrate(str(source), str(destination))

            quarantine = destination / "legacy_delivery_ledger.json"
            self.assertTrue(quarantine.is_file())
            self.assertEqual(os.stat(quarantine).st_mode & 0o777, 0o600)
            payload = json.loads(quarantine.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "quarantined")
            self.assertEqual(payload["reason"], "automatic_delivery_disabled")
            self.assertEqual(payload["files"]["redeem_sent.json"]["records"], 1)
            self.assertEqual(payload["files"]["pan_sent.json"]["records"], 1)
            self.assertNotIn("secret-code", quarantine.read_text(encoding="utf-8"))
            self.assertNotIn("secret-link", quarantine.read_text(encoding="utf-8"))

    def test_failed_commit_rolls_back_and_can_resume(self):
        with tempfile.TemporaryDirectory() as root:
            source = self.make_source(root)
            destination = Path(root) / "runtime"
            import scripts.migrate_state as migration

            original_replace = migration.os.replace
            calls = [0]

            def fail_second_replace(source_path, target_path):
                calls[0] += 1
                if calls[0] == 2:
                    raise OSError("injected commit failure")
                return original_replace(source_path, target_path)

            with patch.object(migration.os, "replace", side_effect=fail_second_replace):
                with self.assertRaises(OSError):
                    migrate(str(source), str(destination))

            self.assertFalse((destination / ".migration-manifest.json").exists())
            self.assertEqual(
                {
                    path.name
                    for path in destination.iterdir()
                    if path.name != ".migration-manifest.json"
                },
                set(),
            )
            migrate(str(source), str(destination))
            self.assertTrue((destination / "delivery_state.db").is_file())


if __name__ == "__main__":
    unittest.main()
