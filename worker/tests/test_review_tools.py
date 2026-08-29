import tempfile
import sqlite3
import unittest
from pathlib import Path

from delivery_store import DeliveryStore
from scripts.list_manual_reviews import list_reviews, safe_reason
from scripts.manage_inbound_dead_letters import (
    discard_dead_letter,
    list_dead_letters,
    requeue_dead_letter,
    safe_error,
)
from scripts.resolve_manual_review import resolve_review


class ManualReviewToolTests(unittest.TestCase):
    def test_safe_order_reasons_are_visible_without_echoing_arbitrary_text(self):
        for formatter in (safe_reason, safe_error):
            self.assertEqual(formatter("order_buyer_mismatch"), "order_buyer_mismatch")
            self.assertEqual(formatter("secret buyer text"), "stored_error")

    def test_list_redacts_account_and_resolve_closes_only_queue_record(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "state.db")
            store = DeliveryStore(database, now_fn=lambda: 1000.0)
            key = "goofish:" + "a" * 64
            store.record_payment_event(key, "sensitive-account", 1000, 600)
            store.mark_order_manual_review(key, "untrusted_order")

            redacted = list_reviews(database)
            self.assertEqual(len(redacted), 1)
            self.assertNotIn("account_id", redacted[0])
            self.assertNotEqual(redacted[0]["account_ref"], "sensitive-account")
            revealed = list_reviews(database, show_account=True)
            self.assertNotIn("account_id", revealed[0])
            self.assertEqual(revealed[0]["account_ref"], redacted[0]["account_ref"])

            with sqlite3.connect(database) as conn:
                conn.execute(
                    "UPDATE delivery_events SET event_at = ? WHERE order_key = ?",
                    (1e300, key),
                )
                conn.commit()
            self.assertEqual(list_reviews(database)[0]["event_at"], "invalid")

            self.assertTrue(resolve_review(database, key, "fulfilled_manually"))
            self.assertEqual(list_reviews(database), [])
            self.assertEqual(store.get_order(key).status, "manual_review")

    def test_dead_letter_tools_never_list_payload_and_require_explicit_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "state.db")
            now = [1000.0]
            store = DeliveryStore(database, now_fn=lambda: now[0])
            event_key = "sync:" + "b" * 64
            store.record_inbound_event(
                event_key, "sensitive-chat", {"message": "sensitive buyer text"}
            )
            for _attempt in range(store.MAX_INBOUND_ATTEMPTS):
                self.assertIsNotNone(store.claim_inbound_event(event_key))
                status = store.requeue_inbound_event(event_key, "ConnectionError")
                if status == "dead_letter":
                    break
                now[0] += 300

            listed = list_dead_letters(database)
            self.assertEqual(listed["total_dead_letters"], 1)
            self.assertEqual(listed["events"][0]["event_key"], event_key)
            self.assertNotIn("payload", listed["events"][0])
            self.assertNotIn("sensitive buyer text", str(listed))

            self.assertTrue(requeue_dead_letter(database, event_key))
            with sqlite3.connect(database) as conn:
                row = conn.execute(
                    "SELECT status, attempt_count, payload FROM inbound_events WHERE event_key = ?",
                    (event_key,),
                ).fetchone()
            self.assertEqual(row, ("pending", 0, '{"message":"sensitive buyer text"}'))

            with sqlite3.connect(database) as conn:
                conn.execute(
                    "UPDATE inbound_events SET status = 'dead_letter' WHERE event_key = ?",
                    (event_key,),
                )
            self.assertTrue(discard_dead_letter(database, event_key, "not_actionable"))
            with sqlite3.connect(database) as conn:
                row = conn.execute(
                    "SELECT status, payload, last_error FROM inbound_events WHERE event_key = ?",
                    (event_key,),
                ).fetchone()
            self.assertEqual(row, ("completed", "{}", "discarded:not_actionable"))


if __name__ == "__main__":
    unittest.main()
