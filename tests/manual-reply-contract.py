#!/usr/bin/env python3
"""Offline contract for account-local, idempotent manual reply outboxes."""

from __future__ import annotations

import json
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


def acknowledge_part(
    root: Path,
    request_id: str,
    part_index: int,
    *,
    next_status: str = "sending",
) -> dict | None:
    """Persist the same public message/part progress produced by a Worker ACK."""
    with sqlite3.connect(root / "chat_history.db") as con:
        con.row_factory = sqlite3.Row
        message_columns = {
            str(row[1]) for row in con.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "content_type" not in message_columns:
            con.execute("ALTER TABLE messages ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'")
        if "media_json" not in message_columns:
            con.execute("ALTER TABLE messages ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]'")
        parent = con.execute(
            """SELECT id, user_id, item_id, content, chat_id
               FROM manual_reply_drafts WHERE request_id = ?""",
            (request_id,),
        ).fetchone()
        assert parent is not None
        parts = con.execute(
            """SELECT part_index, kind, media_index, acknowledged_at
               FROM manual_reply_parts WHERE outbox_id = ? ORDER BY part_index""",
            (int(parent["id"]),),
        ).fetchall()
        selected = next(
            (part for part in parts if int(part["part_index"]) == int(part_index)),
            None,
        )
        assert selected is not None and selected["acknowledged_at"] is None
        assert next(
            int(part["part_index"])
            for part in parts
            if part["acknowledged_at"] is None
        ) == int(part_index)

        kind = str(selected["kind"])
        if kind == "image":
            media_index = int(selected["media_index"])
            source_id = (
                f"manual_reply:{int(parent['id'])}"
                if media_index == 0
                else f"manual_reply:{int(parent['id'])}:image:{media_index + 1}"
            )
            sent_media = {
                "type": "image",
                "url": f"https://media.example.invalid/{int(parent['id'])}-{media_index}.jpg",
                "alt": f"已发送图片 {media_index + 1}",
                "width": 640,
                "height": 480,
                "duration_ms": 0,
                "label": "图片",
            }
            content = ""
            content_type = "image"
            sent_media_json = json.dumps(
                [sent_media], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        else:
            assert kind == "text"
            has_images = any(str(part["kind"]) == "image" for part in parts)
            source_id = (
                f"manual_reply:{int(parent['id'])}:text"
                if has_images
                else f"manual_reply:{int(parent['id'])}"
            )
            sent_media = None
            content = str(parent["content"] or "")
            content_type = "text"
            sent_media_json = "[]"

        acknowledged_at = 1_788_454_400.0 + int(part_index)
        con.execute(
            """INSERT INTO messages(
                   user_id, item_id, role, content, timestamp, chat_id, source_id,
                   content_type, media_json
               ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?)""",
            (
                str(parent["user_id"] or "seller"),
                str(parent["item_id"] or ""),
                content,
                f"2026-09-03 12:00:{int(part_index):02d}",
                str(parent["chat_id"] or ""),
                source_id,
                content_type,
                sent_media_json,
            ),
        )
        con.execute(
            """UPDATE manual_reply_parts
               SET acknowledged_at = ?, sent_media_json = ?
               WHERE outbox_id = ? AND part_index = ?""",
            (acknowledged_at, sent_media_json, int(parent["id"]), int(part_index)),
        )
        remaining = int(
            con.execute(
                """SELECT COUNT(*) FROM manual_reply_parts
                   WHERE outbox_id = ? AND acknowledged_at IS NULL""",
                (int(parent["id"]),),
            ).fetchone()[0]
        )
        parent_status = "acknowledged" if remaining == 0 else next_status
        con.execute(
            """UPDATE manual_reply_drafts
               SET status = ?, attempts = CASE WHEN ? = 'retry' THEN 1 ELSE attempts END,
                   updated_at = ?, acknowledged_at = ?
               WHERE id = ?""",
            (
                parent_status,
                parent_status,
                acknowledged_at,
                acknowledged_at if remaining == 0 else None,
                int(parent["id"]),
            ),
        )
        con.commit()
        return sent_media


def seed_malformed_manual_replies(root: Path) -> dict:
    """Seed isolated corrupt parents/parts with values that must never be exposed."""
    def private_image(index: int) -> dict:
        filename = f"secret-original-{index}.jpg"
        return {
            "type": "image",
            "url": f"https://private.example.invalid/{filename}",
            "path": f"manual_reply_{index:032x}.jpg",
            "alt": filename,
            "label": filename,
            "mime": "image/jpeg",
            "name": filename,
        }

    nine_images = [private_image(index) for index in range(1, 10)]
    non_image = [{**private_image(101), "type": "file"}]
    mismatch_media = [private_image(201)]
    cases = [
        (
            "malformed-nine-images",
            "secret nine-image reply body",
            "sending",
            2,
            nine_images,
            "secret-nine-lease-owner",
        ),
        (
            "malformed-non-image",
            "secret non-image reply body",
            "retry",
            1,
            non_image,
            "secret-non-image-lease-owner",
        ),
        (
            "malformed-parts-mismatch",
            "secret parts-mismatch reply body",
            "queued",
            0,
            mismatch_media,
            "secret-mismatch-lease-owner",
        ),
    ]
    ids = {}
    with sqlite3.connect(root / "chat_history.db") as con:
        for offset, (request_id, content, status, attempts, media, lease_owner) in enumerate(cases):
            cursor = con.execute(
                """INSERT INTO manual_reply_drafts(
                       user_id, item_id, content, created_at, chat_id,
                       request_id, payload_digest, recipient_id, status, attempts,
                       max_attempts, available_at, lease_owner, lease_until,
                       last_error_code, media_json, updated_at, acknowledged_at
                   ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 10, 0, ?, ?, NULL, ?, ?, NULL)""",
                (
                    "1",
                    "item-default",
                    content,
                    f"2026-09-03 13:00:{offset:02d}",
                    "shared-chat",
                    request_id,
                    "buyer-default",
                    status,
                    attempts,
                    lease_owner,
                    4_102_444_800.0,
                    json.dumps(media, ensure_ascii=False, separators=(",", ":")),
                    1_788_458_400.0 + offset,
                ),
            )
            ids[request_id] = int(cursor.lastrowid)
        corrupt_part = {
            "type": "image",
            "url": "https://private.example.invalid/secret-part.jpg",
            "path": "secret/private/part.jpg",
            "alt": "secret-part.jpg",
            "label": "secret-part.jpg",
            "name": "secret-part.jpg",
        }
        con.execute(
            """INSERT INTO manual_reply_parts(
                   outbox_id, part_index, kind, media_index,
                   acknowledged_at, sent_media_json
               ) VALUES (?, 0, 'text', NULL, NULL, ?)""",
            (
                ids["malformed-parts-mismatch"],
                json.dumps([corrupt_part], ensure_ascii=False, separators=(",", ":")),
            ),
        )
        con.commit()
    secrets = [
        *[case[1] for case in cases],
        *[case[5] for case in cases],
        *[item["url"] for item in nine_images],
        *[item["path"] for item in nine_images],
        *[item["name"] for item in nine_images],
        non_image[0]["url"],
        non_image[0]["path"],
        non_image[0]["name"],
        mismatch_media[0]["url"],
        mismatch_media[0]["path"],
        mismatch_media[0]["name"],
        corrupt_part["url"],
        corrupt_part["path"],
        corrupt_part["name"],
    ]
    return {"ids": ids, "request_ids": list(ids), "secrets": secrets}


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
    assert first.json()["reply"]["current_part"] == 0
    assert first.json()["reply"]["parts"] == [
        {"index": 0, "kind": "text", "status": "queued"}
    ]
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
    image_payload = image_reply.json()
    image_outbox_id = image_payload["message"]["outbox_id"]
    assert image_payload["message"]["content_type"] == "image"
    assert image_payload["message"]["media"][0]["type"] == "image"
    assert "path" not in image_payload["message"]["media"][0]
    assert image_payload["reply"]["current_part"] == 0
    assert image_payload["reply"]["parts"] == [
        {"index": 0, "kind": "image", "status": "queued"},
        {"index": 1, "kind": "text", "status": "waiting"},
    ]
    active_delete = client.request(
        "DELETE", "/api/bot/messages/image", json={"path": image_media["path"]}
    )
    assert active_delete.status_code == 409
    assert active_delete.json()["detail"]["code"] == "image_in_use"

    sent_image = acknowledge_part(
        default_root, "image-request-0001", 0, next_status="retry"
    )
    partial_status = client.get("/api/bot/messages/reply/image-request-0001")
    assert partial_status.status_code == 200
    partial_reply = partial_status.json()["reply"]
    assert partial_reply["status"] == "retry"
    assert partial_reply["platform_acknowledged"] is False
    assert partial_reply["current_part"] == 1
    assert partial_reply["parts"] == [
        {"index": 0, "kind": "image", "status": "acknowledged"},
        {"index": 1, "kind": "text", "status": "retry"},
    ]
    partial_text = json.dumps(partial_status.json(), ensure_ascii=False)
    assert "path" not in partial_text
    assert image_media["path"] not in partial_text
    assert sent_image["url"] not in partial_text

    partial_messages = client.get("/api/bot/messages?chat_id=shared-chat&limit=50")
    assert partial_messages.status_code == 200
    partial_rows = partial_messages.json()["messages"]
    acknowledged_image = next(
        row
        for row in partial_rows
        if row.get("media") and row["media"][0].get("url") == sent_image["url"]
    )
    assert acknowledged_image["role"] == "assistant_manual"
    assert acknowledged_image["content"] == ""
    assert acknowledged_image["delivery_status"] == "acknowledged"
    pending_text = next(
        row for row in partial_rows if row.get("reply_id") == "image-request-0001"
    )
    assert pending_text["outbox_id"] == image_outbox_id
    assert pending_text["content"] == "图片回复"
    assert pending_text["media"] == []
    assert pending_text["delivery_status"] == "retry"
    assert pending_text["current_part"] == 1

    released_delete = client.request(
        "DELETE", "/api/bot/messages/image", json={"path": image_media["path"]}
    )
    assert released_delete.status_code == 200
    assert released_delete.json() == {"ok": True, "deleted": True}
    assert not (default_root / image_media["path"]).exists()

    acknowledge_part(default_root, "image-request-0001", 1)
    final_status = client.get("/api/bot/messages/reply/image-request-0001")
    assert final_status.status_code == 200
    assert final_status.json()["reply"]["status"] == "acknowledged"
    assert final_status.json()["reply"]["platform_acknowledged"] is True
    assert final_status.json()["reply"]["current_part"] is None
    assert final_status.json()["reply"]["parts"] == [
        {"index": 0, "kind": "image", "status": "acknowledged"},
        {"index": 1, "kind": "text", "status": "acknowledged"},
    ]
    final_rows = client.get("/api/bot/messages?chat_id=shared-chat&limit=50").json()["messages"]
    final_manual = [
        row
        for row in final_rows
        if row.get("role") == "assistant_manual"
        and (
            row.get("content") == "图片回复"
            or (row.get("media") and row["media"][0].get("url") == sent_image["url"])
        )
    ]
    assert [row["content_type"] for row in final_manual] == ["image", "text"]
    assert not any(row.get("reply_id") == "image-request-0001" for row in final_rows)

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

    eight_media = []
    for index in range(8):
        uploaded = client.post(
            "/api/bot/messages/image?chat_id=shared-chat",
            headers={
                "Content-Type": "image/jpeg",
                "X-File-Name": f"ordered-{index + 1}.jpg",
            },
            content=b"\xff\xd8\xff\xd9",
        )
        assert uploaded.status_code == 200
        eight_media.append(uploaded.json()["media"])
    assert len({item["path"] for item in eight_media}) == 8
    eight_reply = client.post(
        "/api/bot/messages/reply",
        headers={"Idempotency-Key": "eight-image-request-0001"},
        json={"chat_id": "shared-chat", "content": "八图后发送文字", "media": eight_media},
    )
    assert eight_reply.status_code == 200
    eight_payload = eight_reply.json()
    assert eight_payload["reply"]["current_part"] == 0
    assert eight_payload["reply"]["parts"] == [
        *[
            {
                "index": index,
                "kind": "image",
                "status": "queued" if index == 0 else "waiting",
            }
            for index in range(8)
        ],
        {"index": 8, "kind": "text", "status": "waiting"},
    ]
    assert [item["label"] for item in eight_payload["message"]["media"]] == ["图片"] * 8
    assert all("path" not in item for item in eight_payload["message"]["media"])
    eight_replay = client.post(
        "/api/bot/messages/reply",
        headers={"Idempotency-Key": "eight-image-request-0001"},
        json={"chat_id": "shared-chat", "content": "八图后发送文字", "media": eight_media},
    )
    assert eight_replay.status_code == 200
    assert eight_replay.json()["message"]["outbox_id"] == eight_payload["message"]["outbox_id"]
    reordered_conflict = client.post(
        "/api/bot/messages/reply",
        headers={"Idempotency-Key": "eight-image-request-0001"},
        json={
            "chat_id": "shared-chat",
            "content": "八图后发送文字",
            "media": list(reversed(eight_media)),
        },
    )
    assert reordered_conflict.status_code == 409
    assert reordered_conflict.json()["detail"]["code"] == "idempotency_conflict"
    ninth_rejected = client.post(
        "/api/bot/messages/reply",
        headers={"Idempotency-Key": "nine-image-request-0001"},
        json={
            "chat_id": "shared-chat",
            "content": "第九张必须拒绝",
            "media": [*eight_media, eight_media[0]],
        },
    )
    assert ninth_rejected.status_code == 400
    assert ninth_rejected.json()["detail"]["code"] == "invalid_media"
    non_image_rejected = client.post(
        "/api/bot/messages/reply",
        headers={"Idempotency-Key": "non-image-request-0001"},
        json={
            "chat_id": "shared-chat",
            "content": "非图片必须拒绝",
            "media": [{**eight_media[0], "type": "file"}],
        },
    )
    assert non_image_rejected.status_code == 400
    assert non_image_rejected.json()["detail"]["code"] == "invalid_media"
    with sqlite3.connect(default_root / "chat_history.db") as con:
        assert con.execute(
            "SELECT COUNT(*) FROM manual_reply_drafts WHERE request_id = ?",
            ("eight-image-request-0001",),
        ).fetchone()[0] == 1
        stored_media = json.loads(
            con.execute(
                "SELECT media_json FROM manual_reply_drafts WHERE request_id = ?",
                ("eight-image-request-0001",),
            ).fetchone()[0]
        )
        assert [item["path"] for item in stored_media] == [item["path"] for item in eight_media]
        assert con.execute(
            """SELECT COUNT(*) FROM manual_reply_parts
               WHERE outbox_id = ?""",
            (eight_payload["message"]["outbox_id"],),
        ).fetchone()[0] == 9
        assert con.execute(
            "SELECT COUNT(*) FROM manual_reply_drafts WHERE request_id IN (?, ?)",
            ("nine-image-request-0001", "non-image-request-0001"),
        ).fetchone()[0] == 0

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
            ("acknowledged", "image-request-0001", "buyer-default", "图片回复"),
            ("queued", "eight-image-request-0001", "buyer-default", "八图后发送文字"),
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

    malformed = seed_malformed_manual_replies(default_root)
    status_before_messages = client.get(
        "/api/bot/messages/reply/malformed-nine-images"
    )
    assert status_before_messages.status_code == 200
    assert status_before_messages.json()["reply"]["status"] == "manual_review"
    assert status_before_messages.json()["reply"]["current_part"] is None
    assert status_before_messages.json()["reply"]["parts"] == []
    assert all(
        secret not in json.dumps(status_before_messages.json(), ensure_ascii=False)
        for secret in malformed["secrets"]
    )

    mixed_history = client.get("/api/bot/messages?chat_id=shared-chat&limit=200")
    assert mixed_history.status_code == 200
    mixed_rows = mixed_history.json()["messages"]
    assert any(row.get("content") == "contract buyer message" for row in mixed_rows)
    assert any(row.get("content") == "default seller reply" for row in mixed_rows)
    assert any(row.get("reply_id") == "eight-image-request-0001" for row in mixed_rows)
    for request_id in malformed["request_ids"]:
        redacted = next(row for row in mixed_rows if row.get("reply_id") == request_id)
        assert redacted["role"] == "assistant_manual"
        assert redacted["delivery_status"] == "manual_review"
        assert redacted["content"] == ""
        assert redacted["media"] == []
        assert redacted["current_part"] is None
        assert redacted["parts"] == []
    mixed_text = json.dumps(mixed_history.json(), ensure_ascii=False)
    assert "media_json" not in mixed_text
    assert all(secret not in mixed_text for secret in malformed["secrets"])

    searched_history = client.get(
        "/api/bot/messages?chat_id=shared-chat&limit=200&search=reply"
    )
    assert searched_history.status_code == 200
    assert searched_history.json()["match_count"] >= 1
    assert any(
        row.get("content") == "default seller reply"
        for row in searched_history.json()["messages"]
    )
    assert all(
        row.get("reply_id") not in malformed["request_ids"]
        for row in searched_history.json()["messages"]
    )
    searched_text = json.dumps(searched_history.json(), ensure_ascii=False)
    assert all(secret not in searched_text for secret in malformed["secrets"])

    eight_status = client.get("/api/bot/messages/reply/eight-image-request-0001")
    assert eight_status.status_code == 200
    assert eight_status.json()["reply"]["status"] == "queued"
    assert eight_status.json()["reply"]["current_part"] == 0
    assert len(eight_status.json()["reply"]["parts"]) == 9
    for request_id in malformed["request_ids"]:
        bad_status = client.get(f"/api/bot/messages/reply/{request_id}")
        assert bad_status.status_code == 200
        bad_reply = bad_status.json()["reply"]
        assert bad_reply["status"] == "manual_review"
        assert bad_reply["platform_acknowledged"] is False
        assert bad_reply["current_part"] is None
        assert bad_reply["parts"] == []
        assert "content" not in bad_reply
        assert "media" not in bad_reply
        bad_text = json.dumps(bad_status.json(), ensure_ascii=False)
        assert "media_json" not in bad_text
        assert all(secret not in bad_text for secret in malformed["secrets"])

    with sqlite3.connect(default_root / "chat_history.db") as con:
        quarantined = {
            row[0]: row[1:]
            for row in con.execute(
                """SELECT request_id, status, last_error_code, lease_owner, lease_until
                   FROM manual_reply_drafts
                   WHERE request_id IN (?, ?, ?)""",
                tuple(malformed["request_ids"]),
            ).fetchall()
        }
        assert quarantined == {
            request_id: ("manual_review", "invalid_payload", None, None)
            for request_id in malformed["request_ids"]
        }
        normal_states = {
            row[0]: row[1:]
            for row in con.execute(
                """SELECT request_id, status, last_error_code
                   FROM manual_reply_drafts
                   WHERE request_id IN (?, ?, ?)""",
                (
                    "shared-request-0001",
                    "image-request-0001",
                    "eight-image-request-0001",
                ),
            ).fetchall()
        }
        assert normal_states == {
            "shared-request-0001": ("queued", None),
            "image-request-0001": ("acknowledged", None),
            "eight-image-request-0001": ("queued", None),
        }

    # Retained request IDs must remain idempotent even after the bounded
    # legacy-draft cleanup threshold is crossed. Complete the modern parent via
    # its real text part instead of creating an impossible parent/part mismatch.
    acknowledge_part(default_root, "shared-request-0001", 0)
    with sqlite3.connect(default_root / "chat_history.db") as con:
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
