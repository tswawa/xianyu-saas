import asyncio
import base64
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from context_manager import ChatContextManager
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

    def enqueue(self, *, status="queued", attempts=0, request_id="request-0001", media_json="[]"):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """INSERT INTO manual_reply_drafts(
                       user_id, item_id, content, created_at, chat_id,
                       request_id, recipient_id, status, attempts, max_attempts,
                       available_at, updated_at, media_json
                   ) VALUES ('1', 'item-1', 'seller reply', '2026-08-17 10:00:00',
                             'chat-1', ?, 'buyer-1', ?, ?, 10, ?, ?, ?)""",
                (request_id, status, attempts, self.clock[0], self.clock[0], media_json),
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
        agent.is_manual_mode = lambda chat_id: chat_id == "chat-1"
        return agent

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
        original_acknowledge = self.manager.acknowledge_manual_reply

        def crash_before_local_ack(*_args, **_kwargs):
            raise SystemExit("simulated process crash")

        self.manager.acknowledge_manual_reply = crash_before_local_ack
        with self.assertRaises(SystemExit):
            asyncio.run(first.process_manual_outbox_once())
        self.manager.acknowledge_manual_reply = original_acknowledge

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
        image_path = Path(self.temp_dir.name) / "manual_reply.jpg"
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
        reply_id = self.enqueue(media_json=media_json)
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

    def test_uploaded_manual_image_dimensions_are_parsed(self):
        image_path = Path(self.temp_dir.name) / "manual_reply.png"
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

            summary = await agent.send_msg(
                AckSocket(),
                "chat-1",
                "buyer-1",
                "图片不应变成文本",
                media=[{"type": "image", "path": "manual_reply.jpg"}],
            )
            custom = sent[0]["body"][0]["content"]["custom"]
            content = json.loads(base64.b64decode(custom["data"]).decode("utf-8"))
            self.assertEqual(custom["type"], 2)
            self.assertEqual(content["contentType"], 2)
            self.assertEqual(content["image"]["pics"][0]["type"], 0)
            self.assertEqual(content["image"]["pics"][0]["url"], "https://cdn.example/uploaded.png")
            self.assertNotIn("manual_reply.jpg", json.dumps(content))
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
