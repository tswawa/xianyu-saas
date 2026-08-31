#!/usr/bin/env python3
"""Offline contract for account-local, idempotent manual reply outboxes."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-saas-manual-reply-contract-"))
os.environ.update(
    {
        "SAAS_DB": str(RUN_DIR / "saas.db"),
        "SAAS_TENANTS_DIR": str(RUN_DIR / "tenants"),
        "SAAS_COOKIE_SECURE": "0",
        "SAAS_TESTING": "1",
        "SAAS_ALLOW_REGISTRATION": "1",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
from account_storage import AccountStorage  # noqa: E402


def seed_chat(root: Path, buyer: str, item: str, *, legacy_draft: bool = False) -> None:
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
            """
        )
        con.execute(
            """INSERT INTO messages(
                   user_id, item_id, role, content, timestamp, chat_id, source_id
               ) VALUES (?, ?, 'user', 'contract buyer message',
                         '2026-08-17 10:00:00', 'shared-chat', ?)""",
            (buyer, item, f"buyer:{buyer}"),
        )
        con.execute(
            """INSERT INTO messages(
                   user_id, item_id, role, content, timestamp, chat_id, source_id
               ) VALUES (?, ?, 'user', 'another contract message',
                         '2026-08-17 10:01:00', 'no-takeover', ?)""",
            (buyer, item, f"buyer:{buyer}:other"),
        )
        if legacy_draft:
            con.executescript(
                """
                CREATE TABLE manual_reply_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at DATETIME NOT NULL,
                    chat_id TEXT NOT NULL
                );
                """
            )
            con.execute(
                """INSERT INTO manual_reply_drafts(
                       user_id, item_id, content, created_at, chat_id
                   ) VALUES ('1', ?, 'legacy unsent draft',
                             '2026-08-17 09:00:00', 'shared-chat')""",
                (item,),
            )


def main() -> None:
    client = TestClient(app.app)
    app.db.create_user(
        "reply-owner",
        "password-123",
        role="owner",
        initializer=app._new_user_initializer({}),
    )
    login = client.post(
        "/api/auth/login",
        json={"username": "reply-owner", "password": "password-123"},
    )
    assert login.status_code == 200
    client.cookies.set(
        "xianyu_saas_session",
        login.cookies.get("xianyu_saas_session"),
        path="/",
    )
    user_id = int(app.db.get_user("reply-owner")["id"])
    with app.db._lock:
        app.db.con.execute(
            "UPDATE users SET expires_at = 4102444800 WHERE id = ?",
            (user_id,),
        )
        app.db.con.commit()
    app.db.create_shop_account(user_id, "secondary", "Secondary")

    storage = AccountStorage(str(RUN_DIR / "tenants"))
    default_root = storage.ensure_account_dir(user_id, "default")
    secondary_root = storage.ensure_account_dir(user_id, "secondary")
    seed_chat(default_root, "buyer-default", "item-default", legacy_draft=True)
    seed_chat(secondary_root, "buyer-secondary", "item-secondary")

    for account in ("default", "secondary"):
        response = client.post(
            "/api/bot/conversations/shared-chat/takeover",
            headers={"X-Shop-Account": account},
            json={"enabled": True},
        )
        assert response.status_code == 200

    common_headers = {"Idempotency-Key": "shared-request-0001"}
    first = client.post(
        "/api/bot/messages/reply",
        headers=common_headers,
        json={"chat_id": "shared-chat", "content": "default seller reply"},
    )
    assert first.status_code == 200
    assert first.json()["reply"]["status"] == "queued"
    assert first.json()["platform_acknowledged"] is False

    replay = client.post(
        "/api/bot/messages/reply",
        headers=common_headers,
        json={"chat_id": "shared-chat", "content": "default seller reply"},
    )
    assert replay.status_code == 200
    assert replay.json()["message"]["outbox_id"] == first.json()["message"]["outbox_id"]
    conflict = client.post(
        "/api/bot/messages/reply",
        headers=common_headers,
        json={"chat_id": "shared-chat", "content": "changed seller reply"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"

    image_upload = client.post(
        "/api/bot/messages/image?chat_id=shared-chat",
        headers={
            "Content-Type": "image/jpeg",
            "X-File-Name": "contract-image.jpg",
        },
        content=b"\xff\xd8\xff\xd9",
    )
    assert image_upload.status_code == 200
    image_media = image_upload.json()["media"]
    assert image_media["type"] == "image"
    image_reply = client.post(
        "/api/bot/messages/reply",
        headers={"Idempotency-Key": "image-request-0001"},
        json={"chat_id": "shared-chat", "content": "图片回复", "media": [image_media]},
    )
    assert image_reply.status_code == 200
    assert image_reply.json()["message"]["content_type"] == "image"
    assert image_reply.json()["message"]["media"][0]["type"] == "image"

    secondary = client.post(
        "/api/bot/messages/reply",
        headers={
            "X-Shop-Account": "secondary",
            "Idempotency-Key": "shared-request-0001",
        },
        json={"chat_id": "shared-chat", "content": "secondary seller reply"},
    )
    assert secondary.status_code == 200
    assert secondary.json()["message"]["item_id"] == "item-secondary"

    blocked = client.post(
        "/api/bot/messages/reply",
        headers={"Idempotency-Key": "no-takeover-request-0001"},
        json={"chat_id": "no-takeover", "content": "must not queue"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "manual_takeover_required"

    status = client.get("/api/bot/messages/reply/shared-request-0001")
    assert status.status_code == 200
    assert status.json()["reply"]["status"] == "queued"
    assert "default seller reply" not in status.text
    default_only = client.post(
        "/api/bot/messages/reply",
        headers={"Idempotency-Key": "default-only-request-0001"},
        json={"chat_id": "shared-chat", "content": "default only reply"},
    )
    assert default_only.status_code == 200
    missing_scope = client.get(
        "/api/bot/messages/reply/default-only-request-0001",
        headers={"X-Shop-Account": "secondary"},
    )
    assert missing_scope.status_code == 404

    with sqlite3.connect(default_root / "chat_history.db") as con:
        rows = con.execute(
            """SELECT status, request_id, recipient_id, content
               FROM manual_reply_drafts ORDER BY id"""
        ).fetchall()
        assert rows == [
            ("draft", None, None, "legacy unsent draft"),
            ("queued", "shared-request-0001", "buyer-default", "default seller reply"),
            ("queued", "image-request-0001", "buyer-default", "图片回复"),
            ("queued", "default-only-request-0001", "buyer-default", "default only reply"),
        ]
    with sqlite3.connect(secondary_root / "chat_history.db") as con:
        rows = con.execute(
            """SELECT status, request_id, recipient_id, content
               FROM manual_reply_drafts"""
        ).fetchall()
        assert rows == [
            ("queued", "shared-request-0001", "buyer-secondary", "secondary seller reply")
        ]
    with app.db._lock:
        payloads = [str(row[0] or "") for row in app.db.con.execute("SELECT payload_json FROM jobs")]
    assert all("seller reply" not in payload for payload in payloads)

    # Retained request IDs must remain idempotent even after the bounded
    # legacy-draft cleanup threshold is crossed.
    with sqlite3.connect(default_root / "chat_history.db") as con:
        con.execute(
            "UPDATE manual_reply_drafts SET status = 'acknowledged' WHERE request_id = ?",
            ("shared-request-0001",),
        )
        con.executemany(
            """INSERT INTO manual_reply_drafts(
                   user_id, item_id, content, created_at, chat_id,
                   request_id, recipient_id, status, attempts, max_attempts,
                   available_at, updated_at
               ) VALUES ('1', 'item-default', 'retained reply', '2026-08-17 10:00:00',
                         'shared-chat', ?, 'buyer-default', 'acknowledged', 1, 10, 0, 0)""",
            [(f"retained-{index:04d}",) for index in range(2001)],
        )
        con.commit()
    retained = client.post(
        "/api/bot/messages/reply",
        headers={"Idempotency-Key": "retention-request-0001"},
        json={"chat_id": "shared-chat", "content": "retention check"},
    )
    assert retained.status_code == 200
    with sqlite3.connect(default_root / "chat_history.db") as con:
        tombstone = con.execute(
            """SELECT content, payload_digest, recipient_id, chat_id
               FROM manual_reply_drafts WHERE request_id = ?""",
            ("shared-request-0001",),
        ).fetchone()
        assert tombstone is not None
        assert tombstone[0] == ""
        assert len(tombstone[1]) == 64
        assert tombstone[2] is None
        assert tombstone[3] == ""
        full_rows = con.execute(
            """SELECT COUNT(*) FROM manual_reply_drafts
               WHERE status = 'acknowledged' AND content != ''"""
        ).fetchone()[0]
        assert full_rows == 2000
    compacted_replay = client.post(
        "/api/bot/messages/reply",
        headers={"Idempotency-Key": "shared-request-0001"},
        json={"chat_id": "shared-chat", "content": "default seller reply"},
    )
    assert compacted_replay.status_code == 200
    assert compacted_replay.json()["reply"]["status"] == "acknowledged"
    assert compacted_replay.json()["message"]["content"] == "default seller reply"
    compacted_conflict = client.post(
        "/api/bot/messages/reply",
        headers={"Idempotency-Key": "shared-request-0001"},
        json={"chat_id": "shared-chat", "content": "changed after compaction"},
    )
    assert compacted_conflict.status_code == 409

    # The metadata-only window is large but still bounded.  This deliberately
    # exceeds it so a future cleanup regression cannot grow the table forever.
    with sqlite3.connect(default_root / "chat_history.db") as con:
        con.executemany(
            """INSERT INTO manual_reply_drafts(
                   user_id, item_id, content, created_at, chat_id,
                   request_id, payload_digest, recipient_id, status, attempts,
                   max_attempts, available_at, updated_at
               ) VALUES ('', '', '', '2026-08-17 10:00:00', '', ?, ?, NULL,
                         'acknowledged', 1, 10, 0, 0)""",
            [(f"tombstone-{index:05d}", "0" * 64) for index in range(50_001)],
        )
        con.commit()
    bounded = client.post(
        "/api/bot/messages/reply",
        headers={"Idempotency-Key": "bounded-window-request-0001"},
        json={"chat_id": "shared-chat", "content": "bounded window trigger"},
    )
    assert bounded.status_code == 200
    with sqlite3.connect(default_root / "chat_history.db") as con:
        tombstone_count = con.execute(
            """SELECT COUNT(*) FROM manual_reply_drafts
               WHERE status = 'acknowledged' AND content = ''
                 AND COALESCE(payload_digest, '') != ''"""
        ).fetchone()[0]
        assert tombstone_count == 50_000

    print("manual reply contract: idempotency, bounded tombstones, takeover and account isolation passed")


if __name__ == "__main__":
    main()
