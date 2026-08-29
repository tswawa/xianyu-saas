#!/usr/bin/env python3
"""List, requeue, or explicitly discard retained inbound dead letters."""

import argparse
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


DISCARD_REASONS = (
    "invalid_event",
    "duplicate_event",
    "not_actionable",
    "operator_cancelled",
)
EVENT_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,512}")


def stable_ref(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]


def format_timestamp(value):
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return "invalid"


def safe_error(value):
    safe_types = {
        "stored_error",
        "expired_message",
        "worker_cancelled",
        "interrupted_send",
        "platform_order_identity_unavailable",
        "chat_binding_unavailable",
        "unsupported_item",
        "headinfo_invalid",
        "headinfo_item_mismatch",
        "order_detail_invalid",
        "order_identity_mismatch",
        "order_item_mismatch",
        "order_buyer_mismatch",
        "seller_identity_mismatch",
        "order_not_awaiting_shipment",
        "unsupported_quantity",
        "order_reverification_failed",
        "pan_resource_unavailable",
        "inventory_empty",
        "inventory_marked_used",
        "inventory_removed_from_manifest",
        "cancelled_during_send",
        "cancelled_after_send_attempt",
        "manual_takeover_before_send",
        "trial_state_commit_failed",
        "ack_timeout",
        "untrusted_order",
        "duplicate_replay",
        "temporary",
        "permanent",
        "invalid_event",
        "duplicate_event",
        "not_actionable",
        "operator_cancelled",
        "ConnectionError",
        "TimeoutError",
        "CancelledError",
        "ValueError",
        "RuntimeError",
        "DeliveryStoreError",
        "LLMServiceError",
        "LLMEmptyResponseError",
        "ManualTakeoverError",
    }
    if isinstance(value, str) and value in safe_types:
        return value
    return "stored_error"


def _database_path(database):
    path = Path(database).resolve()
    if not path.is_file():
        raise RuntimeError("delivery database does not exist")
    return path


def _validate_event_key(event_key):
    if not isinstance(event_key, str) or EVENT_KEY_PATTERN.fullmatch(event_key) is None:
        raise ValueError("invalid inbound event key")
    return event_key


def list_dead_letters(database, limit=100):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("dead-letter limit is invalid")
    path = _database_path(database)
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute(
            "SELECT COUNT(*) FROM inbound_events WHERE status = 'dead_letter'"
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT event_key, chat_id, attempt_count, last_error,
                   created_at, updated_at
            FROM inbound_events
            WHERE status = 'dead_letter'
            ORDER BY created_at, rowid
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    records = [
        {
            "event_key": row["event_key"],
            "chat_ref": stable_ref(row["chat_id"]),
            "attempt_count": row["attempt_count"],
            "last_error": safe_error(row["last_error"]),
            "created_at": format_timestamp(row["created_at"]),
            "updated_at": format_timestamp(row["updated_at"]),
        }
        for row in rows
    ]
    return {"total_dead_letters": total, "returned_count": len(records), "events": records}


def requeue_dead_letter(database, event_key):
    path = _database_path(database)
    event_key = _validate_event_key(event_key)
    now = time.time()
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE inbound_events
            SET status = 'pending', attempt_count = 0,
                next_attempt_at = 0, last_error = NULL,
                updated_at = ?, completed_at = NULL
            WHERE event_key = ? AND status = 'dead_letter'
            """,
            (now, event_key),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("dead-letter event was not found")
    return True


def discard_dead_letter(database, event_key, reason):
    path = _database_path(database)
    event_key = _validate_event_key(event_key)
    if reason not in DISCARD_REASONS:
        raise ValueError("invalid discard reason")
    now = time.time()
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE inbound_events
            SET status = 'completed', payload = '{}',
                last_error = ?, updated_at = ?, completed_at = ?
            WHERE event_key = ? AND status = 'dead_letter'
            """,
            (f"discarded:{reason}", now, now, event_key),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("dead-letter event was not found")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default="/var/lib/xianyu-autoagent/delivery_state.db",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--limit", type=int, default=100)

    requeue_parser = subparsers.add_parser("requeue")
    requeue_parser.add_argument("event_key")

    discard_parser = subparsers.add_parser("discard")
    discard_parser.add_argument("event_key")
    discard_parser.add_argument("reason", choices=DISCARD_REASONS)

    args = parser.parse_args()
    if args.command == "list":
        result = list_dead_letters(args.database, args.limit)
        print(json.dumps(result, ensure_ascii=True))
    elif args.command == "requeue":
        requeue_dead_letter(args.database, args.event_key)
        print("dead-letter event requeued")
    else:
        discard_dead_letter(args.database, args.event_key, args.reason)
        print("dead-letter event discarded")


if __name__ == "__main__":
    main()
