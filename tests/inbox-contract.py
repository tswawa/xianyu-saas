#!/usr/bin/env python3
"""Offline contract for the account-scoped unified inbox controls."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-saas-inbox-contract-"))
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


def seed_chat(root: Path) -> None:
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
        con.executemany(
            """INSERT INTO messages(
                   user_id, item_id, role, content, timestamp, chat_id, source_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                ("buyer-alpha", "100001", "user", "想了解教程", "2026-08-17 10:00:00", "chat-alpha", "a-1"),
                ("buyer-alpha", "100001", "assistant", "付款后自动发送", "2026-08-17 10:01:00", "chat-alpha", "a-2"),
                ("buyer-beta", "100002", "user", "请问还有库存吗", "2026-08-17 10:02:00", "chat-beta", "b-1"),
                ("buyer-beta", "100002", "user", "可以马上发货吗", "2026-08-17 10:03:00", "chat-beta", "b-2"),
            ],
        )


def main() -> None:
    client = TestClient(app.app)
    app.db.create_user(
        "inbox-owner",
        "password-123",
        role="owner",
        initializer=app._new_user_initializer({}),
    )
    login = client.post(
        "/api/auth/login", json={"username": "inbox-owner", "password": "password-123"}
    )
    assert login.status_code == 200
    client.cookies.set("xianyu_saas_session", login.cookies.get("xianyu_saas_session"), path="/")
    user_id = int(app.db.get_user("inbox-owner")["id"])
    with app.db._lock:
        app.db.con.execute("UPDATE users SET expires_at = 4102444800 WHERE id = ?", (user_id,))
        app.db.con.commit()
    storage = AccountStorage(str(RUN_DIR / "tenants"))
    root = storage.ensure_account_dir(user_id, "default")
    seed_chat(root)

    initial = client.get("/api/bot/conversations?limit=20")
    assert initial.status_code == 200
    rows = initial.json()["conversations"]
    assert [row["chat_id"] for row in rows] == ["chat-beta", "chat-alpha"]
    assert rows[0]["unread"] is True and rows[0]["unread_count"] == 2
    assert rows[1]["unread"] is True and rows[1]["unread_count"] == 1
    assert initial.json()["unread_total"] == 2
    assert initial.json()["unread_messages_total"] == 3

    # The original alpha message must remain searchable after it falls outside
    # the latest 200 rows returned by the message endpoint.
    with sqlite3.connect(root / "chat_history.db") as con:
        con.executemany(
            """INSERT INTO messages(
                   user_id, item_id, role, content, timestamp, chat_id, source_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    "seller",
                    "100001",
                    "assistant",
                    f"历史填充消息 {index}",
                    f"2026-08-17 11:{index // 60:02d}:{index % 60:02d}",
                    "chat-alpha",
                    f"alpha-fill-{index}",
                )
                for index in range(205)
            ],
        )
        con.commit()
    full_history = client.get("/api/bot/conversations?search=教程")
    assert [row["chat_id"] for row in full_history.json()["conversations"]] == ["chat-alpha"]
    message_search = client.get("/api/bot/messages?chat_id=chat-alpha&search=教程&limit=200")
    assert message_search.status_code == 200
    assert message_search.json()["search"] == "教程"
    assert message_search.json()["match_count"] == 1
    assert len(message_search.json()["messages"]) == 1
    assert message_search.json()["messages"][0]["matched"] is True

    searched = client.get("/api/bot/conversations?search=发货")
    assert [row["chat_id"] for row in searched.json()["conversations"]] == ["chat-beta"]
    unread = client.get("/api/bot/conversations?unread_only=true")
    assert len(unread.json()["conversations"]) == 2

    marked = client.post("/api/bot/conversations/chat-beta/read", json={"read": True})
    assert marked.status_code == 200
    assert marked.json()["conversation"]["unread_count"] == 0
    unread_after = client.get("/api/bot/conversations?unread_only=true")
    assert [row["chat_id"] for row in unread_after.json()["conversations"]] == ["chat-alpha"]

    with sqlite3.connect(root / "chat_history.db") as con:
        con.execute("ALTER TABLE messages ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'")
        con.execute("ALTER TABLE messages ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]'")
        con.execute(
            """INSERT INTO messages(
                   user_id, item_id, role, content, timestamp, chat_id, source_id,
                   content_type, media_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "buyer-alpha", "100001", "user", "[图片] [表情]", "2026-08-17 10:04:00",
                "chat-alpha", "a-media", "rich",
                '[{"type":"image","url":"https://cdn.example/image.jpg","label":"买家图片"},{"type":"emoji","url":"","label":"开心表情"}]',
            ),
        )
        con.commit()
    rich_messages = client.get("/api/bot/messages?chat_id=chat-alpha")
    assert rich_messages.status_code == 200
    rich = next(item for item in rich_messages.json()["messages"] if item.get("content_type") == "rich")
    assert [item["type"] for item in rich["media"]] == ["image", "emoji"]
    assert rich["media"][0]["url"] == "https://cdn.example/image.jpg"

    taken = client.post("/api/bot/conversations/chat-alpha/takeover", json={"enabled": True})
    assert taken.status_code == 200
    assert taken.json()["enabled"] is True
    assert taken.json()["conversation"]["takeover"] is True
    with sqlite3.connect(root / "delivery_state.db") as con:
        mode = con.execute("SELECT chat_id, expires_at FROM manual_modes WHERE chat_id = ?", ("chat-alpha",)).fetchone()
        assert mode is not None and float(mode[1]) > 0

    resumed = client.post("/api/bot/conversations/chat-alpha/takeover", json={"enabled": False})
    assert resumed.status_code == 200
    assert resumed.json()["enabled"] is False
    with sqlite3.connect(root / "delivery_state.db") as con:
        assert con.execute("SELECT 1 FROM manual_modes WHERE chat_id = ?", ("chat-alpha",)).fetchone() is None
        exit_event = con.execute(
            "SELECT mode, created_at FROM manual_control_events WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1",
            ("chat-alpha",),
        ).fetchone()
        assert exit_event is not None and exit_event[0] == "auto" and float(exit_event[1]) > 0

    assert client.post(
        "/api/bot/conversations/chat-alpha/takeover", json={"enabled": True}
    ).status_code == 200
    image_upload = client.post(
        "/api/bot/messages/image?chat_id=chat-alpha",
        content=b"\x89PNG\r\n\x1a\ncontract-image",
        headers={"Content-Type": "image/png", "X-File-Name": "reply.png"},
    )
    assert image_upload.status_code == 200
    uploaded_media = image_upload.json()["media"]
    assert uploaded_media["type"] == "image" and uploaded_media["mime"] == "image/png"
    stored_image = root / uploaded_media["path"]
    assert stored_image.is_file() and stored_image.stat().st_mode & 0o777 == 0o600
    image_reply = client.post(
        "/api/bot/messages/reply",
        json={"content": "", "chat_id": "chat-alpha", "media": [uploaded_media]},
        headers={"Idempotency-Key": "inbox-image-reply-0001"},
    )
    assert image_reply.status_code == 200
    assert image_reply.json()["message"]["content_type"] == "image"
    assert image_reply.json()["message"]["media"][0]["type"] == "image"
    assert "path" not in image_reply.json()["message"]["media"][0]
    queued_messages = client.get("/api/bot/messages?chat_id=chat-alpha").json()["messages"]
    queued_image = next(item for item in queued_messages if item.get("reply_id") == "inbox-image-reply-0001")
    assert queued_image["delivery_status"] == "queued" and queued_image["content_type"] == "image"

    assert client.post("/api/bot/conversations/other-account/read", json={"read": True}).status_code == 404
    assert client.get("/api/bot/conversations", headers={"X-Shop-Account": "missing"}).status_code == 404
    print("inbox contract: search, unread cursor, takeover persistence and account scope passed")


if __name__ == "__main__":
    main()
