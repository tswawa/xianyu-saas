#!/usr/bin/env python3
"""Offline contract for redacted, account-scoped member analytics."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-saas-analytics-contract-"))
os.environ.update(
    {
        "SAAS_DB": str(RUN_DIR / "saas.db"),
        "SAAS_TENANTS_DIR": str(RUN_DIR / "tenants"),
        "SAAS_COOKIE_SECURE": "0",
        "SAAS_RESTORE_WORKERS": "0",
        "SAAS_TESTING": "1",
        "SAAS_ALLOW_REGISTRATION": "1",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
from account_storage import AccountStorage  # noqa: E402


def local_epoch(day: datetime, hour: int = 12) -> float:
    return time.mktime((day.year, day.month, day.day, hour, 0, 0, 0, 0, -1))


def seed_account(root: Path, today: datetime) -> None:
    yesterday = today - timedelta(days=1)
    with sqlite3.connect(root / "chat_history.db") as con:
        con.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp DATETIME,
                chat_id TEXT,
                source_id TEXT
            );
            CREATE TABLE assistant_outcomes (
                source_id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE conversation_controls (
                event_id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                action TEXT NOT NULL,
                enabled INTEGER,
                created_at REAL NOT NULL,
                applied_at REAL,
               status TEXT NOT NULL DEFAULT 'applied'
            );
            CREATE TABLE manual_reply_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                request_id TEXT,
                content TEXT NOT NULL
            );
            """
        )
        day = today.strftime("%Y-%m-%d")
        old_day = yesterday.strftime("%Y-%m-%d")
        con.executemany(
            """INSERT INTO messages(
                   user_id, item_id, role, content, timestamp, chat_id, source_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                ("buyer-a", "item-a", "user", "private buyer text", f"{day} 10:00:00", "chat-a", "in-1"),
                ("seller", "item-a", "assistant", "private reply text", f"{day} 10:01:00", "chat-a", "auto-1"),
                ("buyer-b", "item-b", "user", "old message", f"{old_day} 10:00:00", "chat-b", "old-1"),
                ("buyer-c", "item-c", "user", "newest private message", f"{day} 15:00:00", "chat-c", "in-2"),
                ("seller", "item-c", "assistant", "manual seller reply", f"{day} 15:00:00", "chat-c", "manual_reply:42"),
            ],
        )
        con.executemany(
            "INSERT INTO manual_reply_drafts(status, request_id, content) VALUES (?, ?, ?)",
            [
                ("retry", "retry-request", "retry private reply"),
                ("manual_review", "review-request", "review private reply"),
            ],
        )
        # This outcome has no message row, modelling a pruned bounded history.
        con.execute(
            """INSERT INTO assistant_outcomes(
                   source_id, chat_id, user_id, item_id, role, content, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("auto-pruned", "chat-c", "seller", "item-c", "assistant", "private outcome", f"{day}T11:00:00", f"{day}T11:00:00"),
        )
        con.execute(
            """INSERT INTO conversation_controls(
                   event_id, chat_id, action, enabled, created_at, applied_at, status
               ) VALUES (?, ?, 'takeover', 1, ?, ?, 'applied')""",
            ("legacy-takeover", "chat-a", local_epoch(today, 9), local_epoch(today, 9)),
        )

    with sqlite3.connect(root / "delivery_state.db") as con:
        con.executescript(
            """
            CREATE TABLE delivery_events (
                order_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                delivered_at REAL
            );
            CREATE TABLE manual_control_events (
                control_key TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE manual_reviews (
                order_key TEXT PRIMARY KEY,
                status TEXT NOT NULL
            );
            """
        )
        con.executemany(
            "INSERT INTO delivery_events(order_key,status,created_at,updated_at,delivered_at) VALUES (?,?,?,?,?)",
            [
                ("success-order", "delivered", local_epoch(today, 12), local_epoch(today, 12), local_epoch(today, 12)),
                ("failed-order", "manual_review", local_epoch(today, 13), local_epoch(today, 13), None),
                ("resolved-order", "manual_review", local_epoch(today, 14), local_epoch(today, 14), None),
                ("old-order", "delivered", local_epoch(yesterday, 12), local_epoch(yesterday, 12), local_epoch(yesterday, 12)),
            ],
        )
        con.executemany(
            "INSERT INTO manual_reviews(order_key,status) VALUES (?,?)",
            [("failed-order", "open"), ("resolved-order", "resolved")],
        )
        # The worker ledger is authoritative.  The same API action may also be
        # mirrored in conversation_controls; it must not be counted twice.
        con.execute(
            "INSERT INTO manual_control_events(control_key,chat_id,mode,created_at) VALUES (?,?,?,?)",
            ("worker-takeover", "chat-a", "manual", local_epoch(today, 9)),
        )


def main() -> None:
    client = TestClient(app.app)
    register = client.post(
        "/api/auth/register", json={"username": "analytics-owner", "password": "password-123"}
    )
    assert register.status_code == 200, register.text
    login = client.post(
        "/api/auth/login", json={"username": "analytics-owner", "password": "password-123"}
    )
    assert login.status_code == 200, login.text
    client.cookies.set("xianyu_saas_session", login.cookies.get("xianyu_saas_session"), path="/")
    user_id = int(app.db.get_user("analytics-owner")["id"])
    with app.db._lock:
        app.db.con.execute("UPDATE users SET expires_at = 4102444800 WHERE id = ?", (user_id,))
        app.db.con.commit()

    storage = AccountStorage(str(RUN_DIR / "tenants"))
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    default_root = storage.ensure_account_dir(user_id, "default")
    created = client.post("/api/bot/accounts", json={"key": "second", "name": "第二店铺"})
    assert created.status_code == 200, created.text
    second_root = storage.ensure_account_dir(user_id, "second")
    seed_account(default_root, today)
    # Empty database files model a worker initialization interrupted before
    # schema creation.  Statistics must treat them as empty, not return 500.
    for name in ("chat_history.db", "delivery_state.db"):
        sqlite3.connect(second_root / name).close()

    one_day = client.get("/api/bot/analytics?period=1")
    assert one_day.status_code == 200, one_day.text
    payload = one_day.json()
    assert payload["period"]["days"] == 1
    assert payload["summary"]["messages_total"] == 5
    assert payload["summary"]["attention_total"] == 3
    assert payload["summary"]["last_activity"] == today.strftime("%Y-%m-%d 15:00:00")
    assert payload["messages_total"] == payload["summary"]["messages_total"]
    totals = payload["totals"]
    assert totals["messages_total"] == 4
    assert totals["buyer_messages_total"] == 2
    assert payload["buckets"][0]["buyer_messages_total"] == 2
    assert totals["auto_replies_total"] == 2  # one bounded row + one durable outcome
    assert totals["manual_takeovers_total"] == 1  # worker/API mirror is deduplicated
    assert totals["fulfillment_success_total"] == 1
    assert totals["fulfillment_failed_total"] == 1
    assert totals["unread_conversations_total"] == 3
    assert totals["unread_messages_total"] == 3
    assert len(payload["buckets"]) == 1
    serialized = str(payload)
    for secret in (
        "private buyer text",
        "private reply text",
        "private outcome",
        "newest private message",
        "manual seller reply",
        "retry private reply",
        "review private reply",
        "success-order",
        "resolved-order",
    ):
        assert secret not in serialized
    for forbidden in ("order_key", "chat_id", "source_id", "content", "delivery_payload", "inventory"):
        assert forbidden not in payload["totals"]

    attention = client.get("/api/bot/attention")
    assert attention.status_code == 200, attention.text
    attention_codes = {item["code"] for item in attention.json()["items"]}
    assert {"manual_reply_retry", "manual_reply_review"}.issubset(attention_codes)
    attention_text = str(attention.json())
    for forbidden in ("retry-request", "review-request", "retry private reply", "review private reply", "chat_id", "source_id"):
        assert forbidden not in attention_text

    seven_day = client.get("/api/bot/analytics?period=7")
    assert seven_day.status_code == 200
    assert len(seven_day.json()["buckets"]) == 7
    assert seven_day.json()["totals"]["fulfillment_success_total"] == 2
    assert client.get("/api/bot/analytics?period=2").status_code == 400

    # Account scope must be applied before any records read.
    second = client.get("/api/bot/analytics", headers={"X-Shop-Account": "second"})
    assert second.status_code == 200
    assert second.json()["totals"]["messages_total"] == 0
    assert second.json()["summary"]["orders_total"] == 0
    assert client.get("/api/bot/analytics", headers={"X-Shop-Account": "missing"}).status_code == 404

    # Analytics remains available when the legacy expiry field is zero.
    with app.db._lock:
        app.db.con.execute("UPDATE users SET expires_at = 0 WHERE id = ?", (user_id,))
        app.db.con.commit()
    free_analytics = client.get("/api/bot/analytics")
    assert free_analytics.status_code == 200
    assert isinstance(free_analytics.json()["totals"], dict)
    print("analytics contract: scoped redacted aggregates, buckets, permissions and compatibility passed")


if __name__ == "__main__":
    main()
