#!/usr/bin/env python3
"""List open payment-review records without exposing account IDs by default."""

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def stable_ref(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]


def format_timestamp(value):
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return "invalid"


def safe_reason(value):
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
    return value if isinstance(value, str) and value in safe_types else "stored_error"


def _query_reviews(database, limit=100):
    database_path = Path(database).resolve()
    if not database_path.is_file():
        raise RuntimeError("delivery database does not exist")
    uri = f"file:{database_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        total_open = conn.execute(
            "SELECT COUNT(*) FROM manual_reviews WHERE status = 'open'"
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT reviews.order_key, events.chat_id, reviews.reason,
                   events.event_at, reviews.created_at
            FROM manual_reviews AS reviews
            JOIN delivery_events AS events
              ON events.order_key = reviews.order_key
            WHERE reviews.status = 'open'
            ORDER BY reviews.created_at, reviews.order_key
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return total_open, rows


def _format_reviews(rows, show_account=False):
    """Format reviews without ever returning raw platform account IDs.

    ``show_account`` remains an ignored compatibility argument for callers
    from an older release; the CLI and library now have one redacted shape.
    """
    records = []
    for row in rows:
        record = {
            "order_key": row["order_key"],
            "account_ref": stable_ref(row["chat_id"]),
            "reason": safe_reason(row["reason"]),
            "event_at": format_timestamp(row["event_at"]),
            "queued_at": format_timestamp(row["created_at"]),
        }
        records.append(record)
    return records


def list_reviews(database, limit=100, show_account=False):
    _total_open, rows = _query_reviews(database, limit)
    return _format_reviews(rows, show_account)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default="/var/lib/xianyu-autoagent/delivery_state.db",
    )
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")
    total_open, rows = _query_reviews(args.database, args.limit)
    records = _format_reviews(rows)
    print(
        json.dumps(
            {
                "total_open": total_open,
                "returned_count": len(records),
                "reviews": records,
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
