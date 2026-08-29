#!/usr/bin/env python3
"""Resolve one payment-review record after an operator checks the platform."""

import argparse
import re
import sqlite3
import time
from pathlib import Path


RESOLUTIONS = (
    "fulfilled_manually",
    "closed_on_platform",
    "duplicate_notice",
    "not_actionable",
)


def resolve_review(database, order_key, resolution):
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,512}", order_key):
        raise ValueError("invalid review key")
    if resolution not in RESOLUTIONS:
        raise ValueError("invalid resolution")
    database_path = Path(database).resolve()
    if not database_path.is_file():
        raise RuntimeError("delivery database does not exist")
    now = time.time()
    with sqlite3.connect(database_path, timeout=10) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE manual_reviews
            SET status = 'resolved', resolution = ?,
                updated_at = ?, resolved_at = ?
            WHERE order_key = ? AND status = 'open'
            """,
            (resolution, now, now, order_key),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("open review was not found")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("order_key")
    parser.add_argument("resolution", choices=RESOLUTIONS)
    parser.add_argument(
        "--database",
        default="/var/lib/xianyu-autoagent/delivery_state.db",
    )
    args = parser.parse_args()
    resolve_review(args.database, args.order_key, args.resolution)
    print("manual review resolved")


if __name__ == "__main__":
    main()
