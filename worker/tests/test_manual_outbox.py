import asyncio
import base64
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from context_manager import ChatContextManager, normalize_media
from main import PlatformMessageRejected, XianyuLive


class ManualOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.clock = [1_000.0]
        self.db_path = str(Path(self.temp_dir.name) / "chat_history.db")
        self.manager = ChatContextManager(
            db_path=self.db_path,
            now_fn=lambda: self.clock[0],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def enqueue(
        self,
        *,
        status="queued",
        attempts=0,
        request_id="request-0001",
        content="seller reply",
        media_json="[]",
    ):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """INSERT INTO manual_reply_drafts(
                       user_id, item_id, content, created_at, chat_id,
                       request_id, recipient_id, status, attempts, max_attempts,
                       available_at, updated_at, media_json
                   ) VALUES ('1', 'item-1', ?, '2026-08-17 10:00:00',
                             'chat-1', ?, 'buyer-1', ?, ?, 10, ?, ?, ?)""",
                (content, request_id, status, attempts, self.clock[0], self.clock[0], media_json),
            )
            return int(cursor.lastrowid)

    def build_agent(self, owner="worker:test"):
        agent = object.__new__(XianyuLive)
        agent.context_manager = self.manager
        agent.manual_reply_owner = owner
        agent.manual_reply_lease_seconds = 300
        agent.manual_reply_lease_heartbeat_interval = 0.01
        agent.account_key = "default"
        agent.myid = "seller-1"
        agent.state_dir = self.temp_dir.name
        agent.is_manual_mode = lambda chat_id: chat_id == "chat-1"
        return agent

    def image_media(self, count, *, start=1):
        items = []
        for offset in range(count):
            index = start + offset
            name = f"manual_reply_{index:032x}.jpg"
            (Path(self.temp_dir.name) / name).write_bytes(b"fake-image")
            items.append({"type": "image", "path": name, "label": f"image-{index}.jpg"})
        return items

    @staticmethod
    def sent_image(media):
        name = str(media[0]["path"])
        return {
            "type": "image",
            "url": f"https://cdn.example/{name}",
            "alt": "图片",
            "label": "图片",
            "width": 640,
            "height": 480,
        }

    def test_legacy_draft_is_never_claimed(self):
        reply_id = self.enqueue(status="draft")
        self.assertEqual(self.manager.claim_manual_replies("worker:a"), [])
        self.assertEqual(self.manager.get_manual_reply(reply_id)["status"], "draft")

    def test_claim_retry_recovery_and_acknowledgement_are_atomic(self):
        reply_id = self.enqueue()
        first = self.manager.claim_manual_replies("worker:a", lease_seconds=30)
        self.assertEqual([row["id"] for row in first], [reply_id])
        self.assertEqual(first[0]["attempts"], 1)
        self.assertEqual(self.manager.claim_manual_replies("worker:b"), [])
        self.assertIsNone(
            self.manager.fail_manual_reply(reply_id, "worker:b", "connection_unavailable")
        )

        self.clock[0] += 31
        recovered = self.manager.claim_manual_replies("worker:b", lease_seconds=30)
        self.assertEqual([row["id"] for row in recovered], [reply_id])
        self.assertEqual(recovered[0]["attempts"], 2)
        self.assertTrue(
            self.manager.acknowledge_manual_reply(reply_id, "worker:b", "seller-1")
        )
        self.assertEqual(self.manager.get_manual_reply(reply_id)["status"], "acknowledged")
        self.assertEqual(self.manager.claim_manual_replies("worker:c"), [])
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT role, content FROM messages WHERE source_id = ?",
                (f"manual_reply:{reply_id}",),
            ).fetchone()
        self.assertEqual(row, ("assistant", "seller reply"))

    def test_lease_renewal_requires_current_owner_and_unexpired_lease(self):
        reply_id = self.enqueue()
        self.manager.claim_manual_replies("worker:a", lease_seconds=30)
        self.clock[0] += 20
        self.assertFalse(
            self.manager.renew_manual_reply_lease(reply_id, "worker:b", 30)
        )
        self.assertTrue(
            self.manager.renew_manual_reply_lease(reply_id, "worker:a", 30)
        )
        self.assertEqual(self.manager.get_manual_reply(reply_id)["lease_until"], 1050.0)
        self.clock[0] += 31
        self.assertFalse(
            self.manager.renew_manual_reply_lease(reply_id, "worker:a", 30)
        )
        recovered = self.manager.claim_manual_replies("worker:b", lease_seconds=30)
        self.assertEqual([row["id"] for row in recovered], [reply_id])
        with self.assertRaises(ValueError):
            self.manager.renew_manual_reply_lease(reply_id, "", 30)
        with self.assertRaises(ValueError):
            self.manager.renew_manual_reply_lease(reply_id, "worker:b", 29)

    def test_expired_owner_cannot_acknowledge_or_fail_a_sending_reply(self):
        reply_id = self.enqueue()
        self.manager.claim_manual_replies("worker:old", lease_seconds=30)
        self.clock[0] += 31
        self.assertFalse(
            self.manager.acknowledge_manual_reply(reply_id, "worker:old", "seller-1")
        )
        self.assertIsNone(
            self.manager.fail_manual_reply(
                reply_id,
                "worker:old",
                "connection_unavailable",
            )
        )
        self.assertEqual(self.manager.get_manual_reply(reply_id)["status"], "sending")

        recovered = self.manager.claim_manual_replies(
            "worker:new",
            lease_seconds=30,
        )
        self.assertEqual([row["id"] for row in recovered], [reply_id])
        self.assertFalse(
            self.manager.acknowledge_manual_reply(reply_id, "worker:old", "seller-1")
        )
        self.assertIsNone(
            self.manager.fail_manual_reply(
                reply_id,
                "worker:old",
                "connection_unavailable",
            )
        )
        self.assertTrue(
            self.manager.acknowledge_manual_reply(reply_id, "worker:new", "seller-1")
        )

    def test_retry_is_bounded_at_ten_claims(self):
        reply_id = self.enqueue()
        for attempt in range(1, 11):
            claimed = self.manager.claim_manual_replies("worker:a")
            self.assertEqual(claimed[0]["attempts"], attempt)
            status = self.manager.fail_manual_reply(
                reply_id,
                "worker:a",
                "connection_unavailable",
                retry_delay=1,
            )
            self.clock[0] += 2
        self.assertEqual(status, "manual_review")
        self.assertEqual(self.manager.claim_manual_replies("worker:a"), [])
        self.assertEqual(self.manager.get_manual_reply(reply_id)["attempts"], 10)

    def test_agent_sends_once_with_stable_key_and_records_platform_ack(self):
        reply_id = self.enqueue()
        agent = self.build_agent()
        calls = []

        async def send(cid, toid, content, **kwargs):
            calls.append((cid, toid, content, kwargs))
            await kwargs["before_attempt"]()

        agent.send_text_reliably = send
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "empty")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0:3], ("chat-1", "buyer-1", "seller reply"))
        self.assertTrue(calls[0][3]["allow_manual"])
        self.assertEqual(
            calls[0][3]["message_key"],
            f"manual_reply:default:{reply_id}",
        )

    def test_send_heartbeat_keeps_lease_alive_until_acknowledgement(self):
        reply_id = self.enqueue()
        agent = self.build_agent()
        agent.manual_reply_lease_seconds = 30

        async def slow_send(_cid, _toid, _content, **kwargs):
            await kwargs["before_attempt"]()
            for _ in range(4):
                self.clock[0] += 9
                await asyncio.sleep(0.02)

        agent.send_text_reliably = slow_send
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(self.manager.get_manual_reply(reply_id)["status"], "acknowledged")

    def test_agent_reuses_key_after_retry_and_stops_when_takeover_ends(self):
        reply_id = self.enqueue()
        agent = self.build_agent()
        keys = []

        async def fail_send(_cid, _toid, _content, **kwargs):
            keys.append(kwargs["message_key"])
            raise ConnectionError("offline")

        agent.send_text_reliably = fail_send
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "retry")
        self.clock[0] += 3

        async def succeed_send(_cid, _toid, _content, **kwargs):
            keys.append(kwargs["message_key"])
            await kwargs["before_attempt"]()

        agent.send_text_reliably = succeed_send
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(keys, [f"manual_reply:default:{reply_id}"] * 2)

        second_id = self.enqueue(request_id="request-0002")
        agent.is_manual_mode = lambda _chat_id: False
        self.clock[0] += 1
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "manual_review")
        self.assertEqual(self.manager.get_manual_reply(second_id)["status"], "manual_review")

    def test_ack_crash_recovery_reuses_the_same_platform_message_uuid(self):
        reply_id = self.enqueue()
        sent_uuids = []

        def configure_transport(agent):
            agent.connection_ready = asyncio.Event()
            agent.connection_ready.set()
            agent.ws = object()

            async def send_msg(_ws, _cid, _toid, _content, message_uuid=None):
                sent_uuids.append(message_uuid)

            agent.send_msg = send_msg

        first = self.build_agent("worker:first")
        configure_transport(first)
        original_acknowledge = self.manager.acknowledge_manual_reply_part

        def crash_before_local_ack(*_args, **_kwargs):
            raise SystemExit("simulated process crash")

        self.manager.acknowledge_manual_reply_part = crash_before_local_ack
        with self.assertRaises(SystemExit):
            asyncio.run(first.process_manual_outbox_once())
        self.manager.acknowledge_manual_reply_part = original_acknowledge

        self.clock[0] += 301
        recovered = self.build_agent("worker:recovered")
        configure_transport(recovered)
        self.assertEqual(
            asyncio.run(recovered.process_manual_outbox_once()),
            "acknowledged",
        )
        expected = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"xianyu:manual_reply:default:{reply_id}",
            )
        )
        self.assertEqual(sent_uuids, [expected, expected])

    def test_manual_image_is_sent_and_cleaned_after_ack(self):
        image_path = Path(self.temp_dir.name) / f"manual_reply_{100:032x}.jpg"
        image_path.write_bytes(b"fake-image")
        media_json = json.dumps(
            [{
                "type": "image",
                "path": image_path.name,
                "name": "manual_reply.jpg",
                "label": "manual_reply.jpg",
            }],
            ensure_ascii=False,
        )
        reply_id = self.enqueue(content="", media_json=media_json)
        agent = self.build_agent()
        agent.state_dir = self.temp_dir.name
        calls = []

        async def send(cid, toid, content, **kwargs):
            calls.append((cid, toid, content, kwargs))
            await kwargs["before_attempt"]()
            return {
                "type": "image",
                "url": "https://cdn.example/sent.jpg",
                "alt": "图片",
                "label": "图片",
                "width": 640,
                "height": 480,
            }

        agent.send_text_reliably = send
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(calls[0][3]["media"][0]["path"], image_path.name)
        self.assertFalse(image_path.exists())
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT content_type, media_json FROM messages WHERE source_id = ?",
                (f"manual_reply:{reply_id}",),
            ).fetchone()
        persisted = json.loads(row[1])
        self.assertEqual(row[0], "image")
        self.assertEqual(persisted[0]["type"], "image")
        self.assertEqual(persisted[0]["url"], "https://cdn.example/sent.jpg")
        self.assertNotIn("path", persisted[0])
        self.assertNotIn(image_path.name, row[1])

    def test_three_images_then_text_are_sent_in_order(self):
        media = self.image_media(3)
        reply_id = self.enqueue(media_json=json.dumps(media, ensure_ascii=False))
        agent = self.build_agent()
        calls = []

        async def send(cid, toid, content, **kwargs):
            await kwargs["before_attempt"]()
            calls.append((cid, toid, content, kwargs.get("message_key"), kwargs.get("media")))
            return self.sent_image(kwargs["media"]) if kwargs.get("media") else None

        agent.send_text_reliably = send
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(
            [call[3] for call in calls],
            [
                f"manual_reply:default:{reply_id}",
                f"manual_reply:default:{reply_id}:image:2",
                f"manual_reply:default:{reply_id}:image:3",
                f"manual_reply:default:{reply_id}:text",
            ],
        )
        self.assertEqual([call[2] for call in calls], ["", "", "", "seller reply"])
        self.assertEqual([bool(call[4]) for call in calls], [True, True, True, False])
        self.assertTrue(all(not (Path(self.temp_dir.name) / item["path"]).exists() for item in media))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT source_id, content, content_type FROM messages ORDER BY id"
            ).fetchall()
        self.assertEqual(rows, [
            (f"manual_reply:{reply_id}", "", "image"),
            (f"manual_reply:{reply_id}:image:2", "", "image"),
            (f"manual_reply:{reply_id}:image:3", "", "image"),
            (f"manual_reply:{reply_id}:text", "seller reply", "text"),
        ])

    def test_second_image_failure_retries_only_remaining_parts(self):
        media = self.image_media(3)
        reply_id = self.enqueue(media_json=json.dumps(media, ensure_ascii=False))
        agent = self.build_agent()
        calls = []
        fail_second = [True]

        async def send(_cid, _toid, content, **kwargs):
            await kwargs["before_attempt"]()
            key = kwargs["message_key"]
            calls.append((key, content, kwargs.get("media")))
            if key.endswith(":image:2") and fail_second[0]:
                fail_second[0] = False
                raise ConnectionError("image two failed")
            return self.sent_image(kwargs["media"]) if kwargs.get("media") else None

        agent.send_text_reliably = send
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "retry")
        first_state = self.manager.get_manual_reply(reply_id)
        self.assertEqual(
            [part["acknowledged_at"] is not None for part in first_state["parts"]],
            [True, False, False, False],
        )
        self.clock[0] += 3
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(
            [call[0] for call in calls],
            [
                f"manual_reply:default:{reply_id}",
                f"manual_reply:default:{reply_id}:image:2",
                f"manual_reply:default:{reply_id}:image:2",
                f"manual_reply:default:{reply_id}:image:3",
                f"manual_reply:default:{reply_id}:text",
            ],
        )

    def test_text_failure_after_images_retries_only_text(self):
        media = self.image_media(2)
        reply_id = self.enqueue(media_json=json.dumps(media, ensure_ascii=False))
        agent = self.build_agent()
        calls = []
        fail_text = [True]

        async def send(_cid, _toid, content, **kwargs):
            await kwargs["before_attempt"]()
            key = kwargs["message_key"]
            calls.append((key, content, kwargs.get("media")))
            if key.endswith(":text") and fail_text[0]:
                fail_text[0] = False
                raise ConnectionError("text failed")
            return self.sent_image(kwargs["media"]) if kwargs.get("media") else None

        agent.send_text_reliably = send
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "retry")
        first_state = self.manager.get_manual_reply(reply_id)
        self.assertEqual(
            [part["acknowledged_at"] is not None for part in first_state["parts"]],
            [True, True, False],
        )
        self.clock[0] += 3
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(
            [call[0] for call in calls],
            [
                f"manual_reply:default:{reply_id}",
                f"manual_reply:default:{reply_id}:image:2",
                f"manual_reply:default:{reply_id}:text",
                f"manual_reply:default:{reply_id}:text",
            ],
        )

    def test_recovery_after_acknowledged_image_starts_with_next_part(self):
        media = self.image_media(2)
        reply_id = self.enqueue(media_json=json.dumps(media, ensure_ascii=False))
        claimed = self.manager.claim_manual_replies("worker:old", lease_seconds=30)
        self.assertEqual(len(claimed), 1)
        acknowledged = self.manager.acknowledge_manual_reply_part(
            reply_id,
            "worker:old",
            "seller-1",
            0,
            sent_media=self.sent_image([media[0]]),
        )
        self.assertFalse(acknowledged["complete"])
        self.clock[0] += 31
        agent = self.build_agent("worker:new")
        calls = []

        async def send(_cid, _toid, content, **kwargs):
            await kwargs["before_attempt"]()
            calls.append((kwargs["message_key"], content, kwargs.get("media")))
            return self.sent_image(kwargs["media"]) if kwargs.get("media") else None

        agent.send_text_reliably = send
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(
            [call[0] for call in calls],
            [
                f"manual_reply:default:{reply_id}:image:2",
                f"manual_reply:default:{reply_id}:text",
            ],
        )
        self.assertFalse((Path(self.temp_dir.name) / media[0]["path"]).exists())

    def test_shared_image_is_deleted_only_after_last_active_reference_ack(self):
        media = self.image_media(1, start=200)
        image_path = Path(self.temp_dir.name) / media[0]["path"]
        first_id = self.enqueue(
            request_id="shared-image-0001",
            content="",
            media_json=json.dumps(media, ensure_ascii=False),
        )
        second_id = self.enqueue(
            request_id="shared-image-0002",
            content="",
            media_json=json.dumps(media, ensure_ascii=False),
        )
        agent = self.build_agent()
        cleanup_statuses = []
        original_cleanup = self.manager.cleanup_manual_reply_image

        def record_cleanup(path):
            status = original_cleanup(path)
            cleanup_statuses.append(status)
            return status

        self.manager.cleanup_manual_reply_image = record_cleanup

        async def send(_cid, _toid, _content, **kwargs):
            await kwargs["before_attempt"]()
            return self.sent_image(kwargs["media"])

        agent.send_text_reliably = send
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(self.manager.get_manual_reply(first_id)["status"], "acknowledged")
        self.assertTrue(image_path.exists())
        self.assertIn("active", cleanup_statuses)

        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(self.manager.get_manual_reply(second_id)["status"], "acknowledged")
        self.assertFalse(image_path.exists())
        self.assertEqual(cleanup_statuses[-1], "deleted")

    def test_manual_image_cleanup_rejects_bad_names_links_and_non_files(self):
        root = Path(self.temp_dir.name)
        target = root / "ordinary-target.jpg"
        target.write_bytes(b"keep")
        symlink_name = f"manual_reply_{300:032x}.jpg"
        (root / symlink_name).symlink_to(target.name)
        directory_name = f"manual_reply_{301:032x}.png"
        (root / directory_name).mkdir()

        self.assertEqual(
            self.manager.cleanup_manual_reply_image("../manual_reply_" + "0" * 32 + ".jpg"),
            "invalid",
        )
        self.assertEqual(
            self.manager.cleanup_manual_reply_image("manual_reply.jpg"),
            "invalid",
        )
        self.assertEqual(
            self.manager.cleanup_manual_reply_image(symlink_name),
            "invalid",
        )
        self.assertEqual(
            self.manager.cleanup_manual_reply_image(directory_name),
            "invalid",
        )
        self.assertTrue(target.exists())
        self.assertTrue((root / symlink_name).is_symlink())
        self.assertTrue((root / directory_name).is_dir())

    def test_duplicate_ack_is_idempotent_only_for_exact_sender_and_content(self):
        image = self.image_media(1, start=400)
        image_id = self.enqueue(
            request_id="duplicate-image-ack",
            content="later text",
            media_json=json.dumps(image, ensure_ascii=False),
        )
        self.manager.claim_manual_replies("worker:image", lease_seconds=30)
        summary = self.sent_image(image)
        first = self.manager.acknowledge_manual_reply_part(
            image_id, "worker:image", "seller-1", 0, sent_media=summary
        )
        self.assertFalse(first["complete"])
        self.clock[0] += 31
        replay = self.manager.acknowledge_manual_reply_part(
            image_id, "worker:replay", "seller-1", 0, sent_media=summary
        )
        self.assertFalse(replay["complete"])

        for field, value in (
            ("url", "https://cdn.example/conflict.jpg"),
            ("width", 641),
            ("label", "另一张图片"),
        ):
            conflicting = dict(summary)
            conflicting[field] = value
            with self.subTest(image_conflict=field):
                with self.assertRaises(RuntimeError):
                    self.manager.acknowledge_manual_reply_part(
                        image_id,
                        "worker:replay",
                        "seller-1",
                        0,
                        sent_media=conflicting,
                    )
        with self.assertRaises(RuntimeError):
            self.manager.acknowledge_manual_reply_part(
                image_id, "worker:replay", "seller-2", 0, sent_media=summary
            )
        recovered = self.manager.claim_manual_replies("worker:image-recovered", lease_seconds=30)
        self.assertEqual([row["id"] for row in recovered], [image_id])
        self.assertTrue(
            self.manager.acknowledge_manual_reply_part(
                image_id, "worker:image-recovered", "seller-1", 1
            )["complete"]
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE manual_reply_parts SET kind = 'text', media_index = NULL
                   WHERE outbox_id = ? AND part_index = 0""",
                (image_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.manager.acknowledge_manual_reply_part(
                image_id, "worker:replay", "seller-1", 0, sent_media=summary
            )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE manual_reply_parts SET kind = 'image', media_index = 0
                   WHERE outbox_id = ? AND part_index = 0""",
                (image_id,),
            )

        text_id = self.enqueue(request_id="duplicate-text-ack", content="original body")
        self.manager.claim_manual_replies("worker:text", lease_seconds=30)
        self.assertTrue(
            self.manager.acknowledge_manual_reply_part(
                text_id, "worker:text", "seller-1", 0
            )["complete"]
        )
        self.assertTrue(
            self.manager.acknowledge_manual_reply_part(
                text_id, "worker:replay", "seller-1", 0
            )["complete"]
        )
        with self.assertRaises(ValueError):
            self.manager.acknowledge_manual_reply_part(
                text_id, "worker:replay", "seller-1", 0, sent_media=summary
            )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE messages SET content = 'changed body' WHERE source_id = ?",
                (f"manual_reply:{text_id}",),
            )
        with self.assertRaises(RuntimeError):
            self.manager.acknowledge_manual_reply_part(
                text_id, "worker:replay", "seller-1", 0
            )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE messages SET content = 'original body', content_type = 'image'
                   WHERE source_id = ?""",
                (f"manual_reply:{text_id}",),
            )
        with self.assertRaises(RuntimeError):
            self.manager.acknowledge_manual_reply_part(
                text_id, "worker:replay", "seller-1", 0
            )

    def test_eight_images_send_but_malformed_rows_do_not_block_next_reply(self):
        eight = self.image_media(8, start=500)
        self.assertEqual(len(normalize_media(eight)), 8)
        eight_id = self.enqueue(
            request_id="eight-images",
            content="after eight",
            media_json=json.dumps(eight, ensure_ascii=False),
        )
        agent = self.build_agent()
        keys = []

        async def send_eight(_cid, _toid, _content, **kwargs):
            await kwargs["before_attempt"]()
            keys.append(kwargs["message_key"])
            return self.sent_image(kwargs["media"]) if kwargs.get("media") else None

        agent.send_text_reliably = send_eight
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(len(keys), 9)
        self.assertEqual(keys[0], f"manual_reply:default:{eight_id}")
        self.assertEqual(keys[-1], f"manual_reply:default:{eight_id}:text")

        nine = self.image_media(9, start=600)
        self.assertEqual(len(normalize_media(nine)), 9)
        nine_id = self.enqueue(
            request_id="nine-images",
            content="must not truncate",
            media_json=json.dumps(nine, ensure_ascii=False),
        )
        non_image = self.image_media(1, start=700)
        non_image[0]["type"] = "file"
        non_image_id = self.enqueue(
            request_id="non-image",
            content="must not send",
            media_json=json.dumps(non_image, ensure_ascii=False),
        )
        mismatch = self.image_media(1, start=800)
        mismatch_id = self.enqueue(
            request_id="parts-mismatch",
            content="",
            media_json=json.dumps(mismatch, ensure_ascii=False),
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO manual_reply_parts(
                       outbox_id, part_index, kind, media_index,
                       acknowledged_at, sent_media_json
                   ) VALUES (?, 0, 'text', NULL, NULL, '[]')""",
                (mismatch_id,),
            )
        good_id = self.enqueue(request_id="good-after-bad", content="send me")
        sent_contents = []

        async def send_good(_cid, _toid, content, **kwargs):
            await kwargs["before_attempt"]()
            sent_contents.append(content)

        agent.send_text_reliably = send_good
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(sent_contents, ["send me"])
        self.assertEqual(self.manager.get_manual_reply(good_id)["status"], "acknowledged")
        for bad_id in (nine_id, non_image_id, mismatch_id):
            bad_reply = self.manager.get_manual_reply(bad_id)
            self.assertEqual(bad_reply["status"], "manual_review")
            self.assertEqual(bad_reply["last_error_code"], "invalid_payload")
        with sqlite3.connect(self.db_path) as conn:
            bad_rows = conn.execute(
                """SELECT id, status, last_error_code FROM manual_reply_drafts
                   WHERE id IN (?, ?, ?) ORDER BY id""",
                (nine_id, non_image_id, mismatch_id),
            ).fetchall()
            bad_messages = conn.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE source_id LIKE ? OR source_id LIKE ? OR source_id LIKE ?""",
                (
                    f"manual_reply:{nine_id}%",
                    f"manual_reply:{non_image_id}%",
                    f"manual_reply:{mismatch_id}%",
                ),
            ).fetchone()[0]
        self.assertEqual(
            bad_rows,
            [
                (nine_id, "manual_review", "invalid_payload"),
                (non_image_id, "manual_review", "invalid_payload"),
                (mismatch_id, "manual_review", "invalid_payload"),
            ],
        )
        self.assertEqual(bad_messages, 0)

    def test_completed_ack_cleanup_recovers_on_restart_without_resending(self):
        media = self.image_media(1, start=850)
        image_path = Path(self.temp_dir.name) / media[0]["path"]
        reply_id = self.enqueue(
            request_id="completed-cleanup-restart",
            content="",
            media_json=json.dumps(media, ensure_ascii=False),
        )
        sends = []
        first = self.build_agent("worker:first")

        async def send(_cid, _toid, _content, **kwargs):
            await kwargs["before_attempt"]()
            sends.append(kwargs["message_key"])
            return self.sent_image(kwargs["media"])

        first.send_text_reliably = send
        first._cleanup_manual_media = lambda _media: []
        self.assertEqual(asyncio.run(first.process_manual_outbox_once()), "acknowledged")
        self.assertEqual(self.manager.get_manual_reply(reply_id)["status"], "acknowledged")
        self.assertTrue(image_path.exists())
        self.assertEqual(sends, [f"manual_reply:default:{reply_id}"])

        restarted = self.build_agent("worker:restarted")

        async def must_not_send(*_args, **_kwargs):
            self.fail("completed cleanup must not resend the acknowledged parent")

        restarted.send_text_reliably = must_not_send
        self.assertEqual(asyncio.run(restarted.process_manual_outbox_once()), "empty")
        self.assertFalse(image_path.exists())
        self.assertEqual(sends, [f"manual_reply:default:{reply_id}"])

    def test_uploaded_manual_image_dimensions_are_parsed(self):
        image_path = Path(self.temp_dir.name) / f"manual_reply_{101:032x}.png"
        image_path.write_bytes(b"fake-image")

        uploaded_paths = []

        class FakeApi:
            def upload_media(self, path):
                uploaded_paths.append(path)
                return {"object": {"url": "https://cdn.example/uploaded.png", "pix": "640x480"}}

        agent = object.__new__(XianyuLive)
        agent.xianyu = FakeApi()
        agent.state_dir = self.temp_dir.name
        payload = asyncio.run(
            agent._outgoing_image_content([{"type": "image", "path": image_path.name}])
        )
        self.assertEqual(uploaded_paths, [str(image_path)])
        self.assertEqual(payload["contentType"], 2)
        self.assertEqual(payload["image"]["pics"], [{
            "type": 0,
            "url": "https://cdn.example/uploaded.png",
            "width": 640,
            "height": 480,
        }])

    def test_send_msg_marks_manual_image_as_image_payload(self):
        async def exercise():
            agent = object.__new__(XianyuLive)
            agent.myid = "seller-1"
            agent.pending_send_acks = {}
            agent.send_ack_timeout = 1

            async def outgoing_image(_media):
                return {
                    "contentType": 2,
                    "image": {
                        "pics": [{
                            "type": 0,
                            "url": "https://cdn.example/uploaded.png",
                            "width": 640,
                            "height": 480,
                        }],
                    },
                }

            agent._outgoing_image_content = outgoing_image
            sent = []

            class AckSocket:
                async def send(self, raw):
                    message = json.loads(raw)
                    sent.append(message)
                    mid = message["headers"]["mid"]
                    agent.pending_send_acks[mid].set_result(None)

            image_name = f"manual_reply_{900:032x}.jpg"
            summary = await agent.send_msg(
                AckSocket(),
                "chat-1",
                "buyer-1",
                "图片不应变成文本",
                media=[{"type": "image", "path": image_name}],
            )
            custom = sent[0]["body"][0]["content"]["custom"]
            content = json.loads(base64.b64decode(custom["data"]).decode("utf-8"))
            self.assertEqual(custom["type"], 2)
            self.assertEqual(content["contentType"], 2)
            self.assertEqual(content["image"]["pics"][0]["type"], 0)
            self.assertEqual(content["image"]["pics"][0]["url"], "https://cdn.example/uploaded.png")
            self.assertNotIn(image_name, json.dumps(content))
            self.assertEqual(summary, {
                "type": "image",
                "url": "https://cdn.example/uploaded.png",
                "alt": "图片",
                "label": "图片",
                "width": 640,
                "height": 480,
            })

        asyncio.run(exercise())

    def test_non_success_protocol_ack_rejects_pending_send(self):
        async def exercise():
            agent = object.__new__(XianyuLive)
            future = asyncio.get_running_loop().create_future()
            agent.pending_send_acks = {"message-mid": future}
            agent.heartbeat_mids = set()
            handled = await agent.handle_heartbeat_response(
                {"code": 500, "headers": {"mid": "message-mid"}}
            )
            self.assertTrue(handled)
            with self.assertRaises(PlatformMessageRejected):
                await future

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
