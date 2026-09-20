#!/usr/bin/env python3
"""Offline contract for the account-scoped unified inbox controls."""

from __future__ import annotations

import hashlib
import json
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

SCOPE_COOKIE = "unb=123456; _m_h5_tk=offline_scope; cookie2=offline-scope"


def seed_owned_snapshot(user_id, item_ids):
    app.write_secret(user_id, "shop_snapshot.json", json.dumps({
        "version": 1, "account_ref": hashlib.sha256(b"123456").hexdigest()[:16],
        "nickname": "offline-owned-shop", "products": [{"id": item_id, "title": "本店商品"} for item_id in item_ids],
        "product_count": len(item_ids), "synced_at": "offline", "truncated": False,
    }))


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


def check_customer_conversation_scope(client, user_id, root):
    """Only positively identified customer chats are visible; history is retained."""
    cookie = SCOPE_COOKIE
    app.write_secret(user_id, "cookies.txt", cookie)
    # A current explicit foreign decision takes precedence over snapshot membership.
    seed_owned_snapshot(user_id, ["100001", "100002", "700006"])
    account_ref = hashlib.sha256(b"123456").hexdigest()[:16]
    other_ref = hashlib.sha256(b"654321").hexdigest()[:16]
    baseline_unread = client.get("/api/bot/conversations").json()["unread_messages_total"]
    samples = [
        ("scope-owned", "700001", "owned", "700001", account_ref),
        ("scope-unknown", "700002", "unknown", "700002", account_ref),
        ("scope-old-identity", "700003", "owned", "700003", other_ref),
        ("scope-old-item", "700004", "owned", "799999", account_ref),
        ("scope-no-item", "", "owned", "700005", account_ref),
        ("scope-foreign", "700006", "foreign", "700006", account_ref),
    ]
    with sqlite3.connect(root / "chat_history.db") as con:
        con.execute("""CREATE TABLE customer_conversation_scope (
            chat_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, account_ref TEXT NOT NULL,
            scope TEXT NOT NULL CHECK(scope IN ('owned', 'foreign', 'unknown')),
            checked_at REAL NOT NULL)""")
        for chat_id, item_id, scope, scope_item, identity in samples:
            con.execute("""INSERT INTO messages(user_id, item_id, role, content, timestamp, chat_id, source_id)
                VALUES (?, ?, 'user', '归属检查消息', '2026-09-19 10:00:00', ?, ?)""",
                ("peer-scope", item_id, chat_id, chat_id))
            con.execute("INSERT INTO customer_conversation_scope VALUES (?, ?, ?, ?, ?)",
                        (chat_id, scope_item, identity, scope, 2_000_000_000))
        # An image/follow-up without item metadata keeps the latest nonempty context.
        con.execute("""INSERT INTO messages(user_id, item_id, role, content, timestamp, chat_id, source_id)
            VALUES ('peer-scope', '', 'user', '外店后续消息', '2026-09-19 10:01:00', 'scope-foreign', 'scope-foreign-followup')""")
        history_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    visible = client.get("/api/bot/conversations?limit=20").json()
    visible_ids = {row["chat_id"] for row in visible["conversations"]}
    assert visible_ids == {"chat-alpha", "chat-beta", "scope-owned"}
    assert visible["unread_messages_total"] == baseline_unread + 1
    assert client.get("/api/bot/conversations?limit=1").json()["conversations"][0]["chat_id"] == "scope-owned", "unknown and foreign chats must be filtered before LIMIT"
    assert client.get("/api/bot/conversations?search=外店后续消息").json()["conversations"] == []
    default_messages = client.get("/api/bot/messages").json()["messages"]
    assert default_messages and all(row["chat_id"] == "scope-owned" for row in default_messages)
    assert [row["chat_id"] for row in client.get("/api/bot/conversations?search=归属检查消息").json()["conversations"]] == ["scope-owned"]
    for chat_id, *_rest in samples[1:]:
        hidden_messages = client.get(f"/api/bot/messages?chat_id={chat_id}&search=消息").json()
        assert hidden_messages["messages"] == [] and hidden_messages["match_count"] == 0
        assert app.records.conversation_exists(user_id, chat_id) is False
        assert app.records.append_manual_draft(user_id, "不得写入", chat_id) is None
        for action, payload in (("read", {"read": True}), ("takeover", {"enabled": True})):
            assert client.post(f"/api/bot/conversations/{chat_id}/{action}", json=payload).status_code == 404
        assert client.post(f"/api/bot/messages/image?chat_id={chat_id}", content=b"\x89PNG\r\n\x1a\ncontract-image",
                           headers={"Content-Type": "image/png", "X-File-Name": "blocked.png"}).status_code == 404
        reply = client.post("/api/bot/messages/reply", json={"chat_id": chat_id, "content": "不得发送"},
                            headers={"Idempotency-Key": f"scope-blocked-reply-{chat_id}"})
        assert reply.status_code == 404 and reply.json()["detail"]["code"] == "conversation_not_found"
    with sqlite3.connect(root / "chat_history.db") as con:
        assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == history_count
        assert con.execute("SELECT COUNT(*) FROM manual_reply_drafts WHERE chat_id LIKE 'scope-%'").fetchone()[0] == 0
    seed_owned_snapshot(user_id, ["100001", "100002", "700002", "700006"])
    assert app.records.conversation_exists(user_id, "scope-unknown") is True, "a verified snapshot is independent positive ownership evidence"
    assert app.records.conversation_exists(user_id, "scope-foreign") is False, "explicit foreign scope overrides snapshot membership"
    seed_owned_snapshot(user_id, ["100001", "100002", "700006"])
    app.write_secret(user_id, "cookies.txt", "unb=not-a-platform-id; _m_h5_tk=offline_scope")
    assert client.get("/api/bot/conversations").json()["conversations"] == [], "owned scopes require a valid current shop identity"
    app.write_secret(user_id, "cookies.txt", cookie)
    # Previously queued work is no longer reachable through an idempotent retry.
    with sqlite3.connect(root / "chat_history.db") as con:
        con.execute("INSERT INTO customer_conversation_scope VALUES (?, ?, ?, 'foreign', ?)",
                    ("chat-alpha", "100001", account_ref, 2_000_000_000))
    assert client.get("/api/bot/messages/reply/inbox-image-reply-0001").status_code == 404
    retry = client.post("/api/bot/messages/reply", json={"chat_id": "chat-alpha", "content": "不得重试"},
                        headers={"Idempotency-Key": "inbox-image-reply-0001"})
    assert retry.status_code == 404
    # Neither an old scope nor a snapshot for another identity proves current ownership.
    app.write_secret(user_id, "cookies.txt", "unb=654321; _m_h5_tk=offline_other; cookie2=offline-other-scope")
    assert app.records.conversation_exists(user_id, "scope-foreign") is False
    assert app.records.conversation_exists(user_id, "scope-owned") is False
    assert app.records.conversation_exists(user_id, "chat-beta") is False
    assert app.records.conversation_exists(user_id, "scope-old-identity") is True
    app.write_secret(user_id, "cookies.txt", cookie)
    with sqlite3.connect(root / "chat_history.db") as con:
        con.execute("""INSERT INTO messages(user_id, item_id, role, content, timestamp, chat_id, source_id)
            VALUES ('peer-scope', '700007', 'user', '新商品上下文', '2026-09-19 10:02:00', 'scope-foreign', 'scope-item-changed')""")
    assert app.records.conversation_exists(user_id, "scope-foreign") is False, "a new item still needs positive ownership evidence"
    seed_owned_snapshot(user_id, ["100001", "100002", "700006", "700007"])
    assert app.records.conversation_exists(user_id, "scope-foreign") is True, "an old foreign item must not override a verified new item"
    assert client.get("/api/bot/messages?chat_id=scope-foreign").json()["messages"]
    # When every chat is foreign, default selection must not fall back to its drafts.
    with sqlite3.connect(root / "chat_history.db") as con:
        con.execute("""INSERT INTO messages(user_id, item_id, role, content, timestamp, chat_id, source_id)
            VALUES ('peer-scope', '700008', 'user', '补全商品上下文', '2026-09-19 10:03:00', 'scope-no-item', 'scope-now-has-item')""")
        for (chat_id,) in con.execute("SELECT DISTINCT chat_id FROM messages WHERE COALESCE(chat_id, '') != ''").fetchall():
            item_id = con.execute("SELECT item_id FROM messages WHERE chat_id=? AND item_id!='' ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()[0]
            con.execute("""INSERT INTO customer_conversation_scope VALUES (?, ?, ?, 'foreign', ?)
                ON CONFLICT(chat_id) DO UPDATE SET item_id=excluded.item_id, account_ref=excluded.account_ref,
                    scope=excluded.scope, checked_at=excluded.checked_at""", (chat_id, item_id, account_ref, 2_000_000_000))
    assert client.get("/api/bot/conversations").json()["conversations"] == []
    assert client.get("/api/bot/messages").json()["messages"] == []


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
    app.write_secret(user_id, "cookies.txt", SCOPE_COOKIE)
    with sqlite3.connect(root / "chat_history.db") as con:
        assert con.execute("SELECT 1 FROM sqlite_master WHERE name='customer_conversation_scope'").fetchone() is None
    missing_proof = client.get("/api/bot/conversations").json()
    assert missing_proof["conversations"] == [] and missing_proof["unread_messages_total"] == 0
    assert client.get("/api/bot/messages?chat_id=chat-alpha").json()["messages"] == []
    seed_owned_snapshot(user_id, ["100001", "100002"])

    initial = client.get("/api/bot/conversations?limit=20")
    assert initial.status_code == 200
    rows = initial.json()["conversations"]
    assert [row["chat_id"] for row in rows] == ["chat-beta", "chat-alpha"]
    assert rows[0]["unread"] is True and rows[0]["unread_count"] == 2
    assert rows[1]["unread"] is True and rows[1]["unread_count"] == 1
    assert initial.json()["unread_total"] == 2
    assert initial.json()["unread_messages_total"] == 3

    # Names belong to an exact chat/peer pair; no name keeps the existing fallback.
    with sqlite3.connect(root / "chat_history.db") as con:
        con.execute("""CREATE TABLE conversation_peer_names (
            chat_id TEXT NOT NULL, peer_id TEXT NOT NULL, nickname TEXT NOT NULL,
            observed_at REAL NOT NULL, PRIMARY KEY(chat_id, peer_id))""")
        con.executemany("INSERT INTO conversation_peer_names VALUES (?, ?, ?, ?)", [
            ("chat-alpha", "buyer-alpha", "小鹿🦌", 2_000_000_000),
            ("chat-beta", "some-other-peer", "不应串用的名字", 2_000_000_000),
        ])
    named_rows = {row["chat_id"]: row for row in client.get("/api/bot/conversations").json()["conversations"]}
    assert named_rows["chat-alpha"]["buyer_label"] == "买家 · 小鹿🦌"
    assert named_rows["chat-beta"]["buyer_label"] == rows[0]["buyer_label"]
    by_name = client.get("/api/bot/conversations", params={"search": "小鹿"})
    assert [row["chat_id"] for row in by_name.json()["conversations"]] == ["chat-alpha"]

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
    check_customer_conversation_scope(client, user_id, root)
    print("inbox contract: search, unread cursor, takeover persistence, account scope and positive customer-conversation ownership passed")


if __name__ == "__main__":
    main()
