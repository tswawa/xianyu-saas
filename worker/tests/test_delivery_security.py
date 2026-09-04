import asyncio
import base64
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from loguru import logger
from auth_state import AuthStateStore
from context_manager import ChatContextManager
from delivery_store import DeliveryStore, DeliveryStoreError
from XianyuAgent import LLMEmptyResponseError, LLMNotReadyError
from XianyuApis import XianyuApiError, XianyuAuthenticationError
from platform_profile import CHROME_MAJOR, DINGTALK_REGISTRATION_UA, ORIGIN, PLATFORM, REFERER
from main import (
    AuthenticationUnavailableError,
    ManualTakeoverError,
    XianyuLive,
    load_json_file,
    read_number_env,
    validate_runtime_env,
)


API_ITEM_ID = "1001"
PAN_ITEM_ID = "2002"
SESSION_ID = "3003"
BUYER_ID = "4004"
ORDER_ID = "5005"


class FakeApi:
    def __init__(self):
        self.session = SimpleNamespace(cookies={})
        self.token_calls = 0
        self.token_result = {
            "ret": ["SUCCESS::调用成功"],
            "data": {"accessToken": "test-token"},
        }
        self.token_error = None
        self.item_calls = 0
        self.head_calls = 0
        self.detail_calls = 0
        self.order_id = ORDER_ID
        self.head_order_id = ORDER_ID
        self.order_item_id = API_ITEM_ID
        self.order_buyer_id = BUYER_ID
        self.head_seller = True
        self.order_seller = True
        self.order_status = 2
        self.order_quantity = 1
        self.paid_amount = "5.00"
        self.head_error = None
        self.detail_error = None
        self.consign_calls = 0
        self.consign_error = None

    def update_cookies(self, cookies):
        self.session.cookies.update(cookies)

    def cookie_header_snapshot(self):
        return "; ".join(
            f"{name}={value}" for name, value in self.session.cookies.items()
        )

    def get_token(self, _device_id):
        self.token_calls += 1
        if self.token_error is not None:
            raise self.token_error
        return self.token_result

    def consign_dummy(self, order_id):
        self.consign_calls += 1
        if self.consign_error is not None:
            raise self.consign_error
        return {"ret": ["SUCCESS::调用成功"]}

    def get_item_info(self, item_id):
        self.item_calls += 1
        return {
            "data": {
                "itemDO": {
                    "title": "test item",
                    "desc": "test description",
                    "soldPrice": 5,
                    "quantity": 1,
                    "skuList": [],
                }
            }
        }

    def get_message_head_info(self, session_id, item_id):
        self.head_calls += 1
        if self.head_error is not None:
            raise self.head_error
        return {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "commonData": {
                    "orderId": self.head_order_id,
                    "itemId": self.order_item_id,
                    "seller": self.head_seller,
                }
            },
        }

    def get_order_detail(self, order_id):
        self.detail_calls += 1
        if self.detail_error is not None:
            raise self.detail_error
        return {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "orderId": self.order_id,
                "itemId": self.order_item_id,
                "peerUserId": self.order_buyer_id,
                "seller": self.order_seller,
                "status": self.order_status,
                "utArgs": {"orderStatus": "WAIT_SELLER_SEND_GOODS"},
                "components": [
                    {
                        "render": "orderInfoVO",
                        "data": {
                            "itemInfo": {"buyAmount": self.order_quantity},
                            "priceInfo": {
                                "amount": {"value": self.paid_amount}
                            },
                        },
                    }
                ],
            },
        }


class FakeBot:
    def __init__(self, reply="-", decision="reply", reason_code="ok", config_revision=7):
        self.reply = reply
        self.last_intent = decision
        self.last_reason_code = reason_code
        self.config_revision = config_revision
        self.calls = 0
        self.ready_calls = 0

    def generate_reply_result(self, *_args, **_kwargs):
        self.calls += 1
        return {
            "reply": self.reply,
            "decision": self.last_intent,
            "reason_code": self.last_reason_code,
            "config_revision": self.config_revision,
        }

    def generate_reply(self, *_args, **_kwargs):
        return self.generate_reply_result(*_args, **_kwargs)["reply"]

    def ensure_ready(self, expected_config_revision):
        self.ready_calls += 1
        if expected_config_revision != self.config_revision:
            raise LLMNotReadyError("revision changed")
        return self.config_revision


def write_json(path, payload):
    path = Path(path)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def paid_event(chat_id, timestamp_ms):
    return {
        "1": f"{chat_id}@goofish",
        "2": 1,
        "3": {"redReminder": "等待卖家发货", "redReminderStyle": "1"},
        "4": timestamp_ms,
    }


def chat_message(chat_id, sender_id, item_id, content, timestamp_ms=None):
    return {
        "1": {
            "2": f"{chat_id}@goofish",
            "5": timestamp_ms or int(time.time() * 1000),
            "10": {
                "senderUserId": sender_id,
                "reminderContent": content,
                "reminderUrl": f"https://www.goofish.test/chat?itemId={item_id}",
            },
        }
    }


class AgentTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tempdir.name)
        write_json(
            self.state_dir / "redeem_codes.json",
            [
                {"code": "REDEEM-A", "used": False},
                {"code": "REDEEM-B", "used": False},
                {"code": "REDEEM-C", "used": False},
            ],
        )
        write_json(
            self.state_dir / "pan_links.json",
            {
                "links": [
                    {
                        "url": "https://files.example.invalid/resource",
                        "code": "ABCD",
                        "remark": "test resource",
                        "match": ["resource"],
                    }
                ]
            },
        )
        write_json(
            self.state_dir / "reply_rules.json",
            {"version": 1, "rules": []},
        )
        write_json(
            self.state_dir / "automation_settings.json",
            {"version": 1, "strategy": "standard", "enabled": True},
        )
        self.products_path = self.state_dir / "products_config.json"
        write_json(
            self.products_path,
            {
                "types": [
                    {
                        "id": "api",
                        "item_ids": [API_ITEM_ID],
                        "delivery": "redeem",
                    },
                    {
                        "id": "pan",
                        "item_ids": [PAN_ITEM_ID],
                        "delivery": "pan",
                        "resource_match": ["resource"],
                    },
                ]
            },
        )
        self.api = FakeApi()
        self.agent = XianyuLive(
            "unb=seller-test; token=not-a-secret",
            reply_bot=FakeBot(),
            api_client=self.api,
            data_dir=str(self.state_dir),
            products_config_path=str(self.products_path),
            automation_mode="rules_ai",
        )
        async def acknowledge_send(*_args, **kwargs):
            before_attempt = kwargs.get("before_attempt")
            if before_attempt is not None:
                await before_attempt()

        self.agent.send_text_reliably = AsyncMock(side_effect=acknowledge_send)

    async def asyncTearDown(self):
        for task in tuple(self.agent.message_tasks | self.agent.delivery_tasks):
            task.cancel()
        await asyncio.gather(
            *tuple(self.agent.message_tasks | self.agent.delivery_tasks),
            return_exceptions=True,
        )
        self.tempdir.cleanup()

    def bind(self, chat_id=SESSION_ID, buyer_id=BUYER_ID, item_id=API_ITEM_ID):
        self.agent.delivery_store.record_chat_binding(chat_id, buyer_id, item_id)

    async def test_init_never_fetches_token_after_websocket_handshake(self):
        self.agent.refresh_token = AsyncMock(return_value=None)
        with self.assertRaises(AuthenticationUnavailableError) as raised:
            await self.agent.init(SimpleNamespace(send=AsyncMock()))
        self.assertEqual(raised.exception.code, "token_unavailable")
        self.agent.refresh_token.assert_not_awaited()

    async def test_ten_concurrent_token_refreshes_are_single_flight(self):
        started = threading.Event()
        release = threading.Event()

        def get_token(_device_id):
            self.api.token_calls += 1
            started.set()
            release.wait(1)
            self.api.session.cookies["_m_h5_tk"] = "rotated_123"
            return {
                "ret": ["SUCCESS::调用成功"],
                "data": {"accessToken": "single-flight-token"},
            }

        self.api.get_token = get_token
        tasks = [asyncio.create_task(self.agent.refresh_token()) for _ in range(10)]
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        release.set()
        self.assertEqual(
            await asyncio.gather(*tasks),
            ["single-flight-token"] * 10,
        )
        self.assertEqual(self.api.token_calls, 1)
        self.assertIn("_m_h5_tk=rotated_123", self.agent.cookies_str)
        self.assertGreater(
            self.agent.next_token_refresh_at,
            self.agent.last_token_refresh_time,
        )

    async def test_transient_token_failures_back_off_and_preserve_registered_websocket(self):
        self.api.token_error = XianyuApiError("network_error")
        self.agent.token_retry_interval = 300
        self.agent.token_refresh_jitter_seconds = 15
        self.agent.ws = AsyncMock()
        self.agent.connection_ready.set()

        with patch("main.random.uniform", side_effect=[5.0, 10.0]):
            for expected_delay in (305.0, 610.0):
                before = time.time()
                self.assertIsNone(await self.agent.refresh_token())
                self.assertGreaterEqual(
                    self.agent.next_token_refresh_at, before + expected_delay - 1
                )
                self.assertLessEqual(
                    self.agent.next_token_refresh_at, before + expected_delay + 1
                )
                self.agent.next_token_refresh_at = 0

        self.assertEqual(self.api.token_calls, 2)
        self.assertFalse(self.agent.token_circuit_open)
        self.agent.ws.close.assert_not_awaited()
        status = json.loads((self.state_dir / "auth_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["version"], 2)
        self.assertEqual(status["phase"], "DEGRADED")
        self.assertEqual(status["mtop_token"]["state"], "DEGRADED")
        self.assertEqual(status["websocket"]["state"], "REGISTERED")
        self.assertEqual(status["code"], "network_error")
        self.assertFalse(status["needs_human"])
        self.assertEqual((self.state_dir / "auth_status.json").stat().st_mode & 0o777, 0o600)

    async def test_auth_logs_and_status_do_not_contain_credentials_or_business_text(self):
        self.api.token_error = XianyuApiError("platform_busy")
        captured = io.StringIO()
        sink = logger.add(captured, format="{message}")
        try:
            self.assertIsNone(await self.agent.refresh_token())
        finally:
            logger.remove(sink)

        combined = captured.getvalue() + (
            self.state_dir / "auth_status.json"
        ).read_text(encoding="utf-8")
        for secret in (
            "not-a-secret",
            "seller-test",
            "accessToken",
            "buyer-message-body",
            "order-body",
        ):
            self.assertNotIn(secret, combined)
        self.assertIn("platform_busy", combined)

    async def test_session_expiry_writes_safe_status_closes_and_stops_future_requests(self):
        self.api.token_error = XianyuAuthenticationError("session_expired")
        self.agent.ws = AsyncMock()

        with self.assertRaises(AuthenticationUnavailableError) as raised:
            await self.agent.refresh_token()
        self.assertEqual(raised.exception.code, "session_expired")
        self.agent.ws.close.assert_awaited_once()
        status_path = self.state_dir / "auth_status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["version"], 2)
        self.assertEqual(status["phase"], "NEEDS_HUMAN")
        self.assertEqual(status["session"]["state"], "EXPIRED")
        self.assertEqual(status["code"], "session_expired")
        self.assertTrue(status["needs_human"])
        self.assertTrue(status["reauthorization_required"])
        self.assertNotIn("not-a-secret", status_path.read_text(encoding="utf-8"))
        with self.assertRaises(AuthenticationUnavailableError):
            await self.agent.refresh_token()
        self.assertEqual(self.api.token_calls, 1)

    async def test_restart_with_needs_human_status_makes_zero_platform_requests(self):
        self.agent._write_auth_status("risk_control", True)
        restarted_api = FakeApi()

        with self.assertRaises(AuthenticationUnavailableError) as raised:
            XianyuLive(
                "unb=seller-test; token=not-a-secret",
                reply_bot=FakeBot(),
                api_client=restarted_api,
                data_dir=str(self.state_dir),
                products_config_path=str(self.products_path),
                automation_mode="rules_ai",
            )
        self.assertEqual(raised.exception.code, "risk_control")
        self.assertEqual(restarted_api.token_calls, 0)

    async def test_rgv587_stops_timer_and_concurrent_followup_requests(self):
        self.api.token_error = XianyuAuthenticationError("risk_control")
        self.agent.ws = AsyncMock()
        self.agent.connection_ready.set()

        with self.assertRaises(AuthenticationUnavailableError) as raised:
            await self.agent.refresh_token()
        self.assertEqual(raised.exception.code, "risk_control")
        self.assertEqual(self.api.token_calls, 1)

        results = await asyncio.gather(
            *(self.agent.refresh_token() for _ in range(10)),
            return_exceptions=True,
        )
        self.assertTrue(
            all(isinstance(result, AuthenticationUnavailableError) for result in results)
        )
        await self.agent.token_refresh_loop()
        self.assertEqual(self.api.token_calls, 1)
        self.agent.ws.close.assert_awaited_once()

    async def test_two_successful_refresh_rounds_each_request_and_reconnect_once(self):
        tokens = iter(("token-one", "token-two"))

        def get_token(_device_id):
            self.api.token_calls += 1
            return {
                "ret": ["SUCCESS::调用成功"],
                "data": {"accessToken": next(tokens)},
            }

        self.api.get_token = get_token
        first_ws = AsyncMock()
        self.agent.ws = first_ws
        self.agent.connection_ready.set()

        self.assertEqual(await self.agent.refresh_token(), "token-one")
        first_generation = self.agent._token_refresh_generation
        self.assertTrue(await self.agent._request_controlled_reconnect(first_generation))
        self.assertFalse(await self.agent._request_controlled_reconnect(first_generation))
        first_ws.close.assert_awaited_once()

        second_ws = AsyncMock()
        self.agent.ws = second_ws
        self.agent.connection_ready.set()
        self.agent.connection_restart_flag = False
        self.agent.next_token_refresh_at = 0
        self.assertEqual(await self.agent.refresh_token(), "token-two")
        second_generation = self.agent._token_refresh_generation
        self.assertTrue(await self.agent._request_controlled_reconnect(second_generation))
        self.assertFalse(await self.agent._request_controlled_reconnect(second_generation))
        second_ws.close.assert_awaited_once()
        self.assertEqual(self.api.token_calls, 2)

    async def test_initial_token_and_rotated_cookie_precede_websocket_handshake(self):
        order = []

        def get_token(_device_id):
            order.append("token")
            self.api.token_calls += 1
            self.api.session.cookies["_m_h5_tk"] = "latest_456"
            return {
                "ret": ["SUCCESS::调用成功"],
                "data": {"accessToken": "startup-token"},
            }

        self.api.get_token = get_token

        sent_messages = []

        class FakeWebSocket:
            async def send(self, message):
                sent_messages.append(json.loads(message))

            async def close(self):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise AuthenticationUnavailableError("token_unavailable")

        websocket = FakeWebSocket()

        class FakeConnection:
            async def __aenter__(self):
                return websocket

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        def connect(_url, *, extra_headers):
            order.append(("connect", extra_headers["Cookie"], extra_headers))
            return FakeConnection()

        self.agent.websocket_registration_wait_seconds = 0
        with patch("main.websockets.connect", side_effect=connect):
            with self.assertRaises(AuthenticationUnavailableError):
                await asyncio.wait_for(self.agent.main(), timeout=2)

        self.assertIsNone(self.agent.heartbeat_task)
        self.assertIsNone(self.agent.token_refresh_task)
        self.assertIsNone(self.agent.inbound_recovery_task)
        self.assertIsNone(self.agent.manual_outbox_task)
        self.assertEqual(order[0], "token")
        self.assertEqual(order[1][0], "connect")
        self.assertIn("_m_h5_tk=latest_456", order[1][1])
        handshake = order[1][2]
        self.assertIn(f"Chrome/{CHROME_MAJOR}.", handshake["User-Agent"])
        self.assertEqual(handshake["Origin"], ORIGIN)
        self.assertEqual(handshake["Referer"], REFERER)
        self.assertEqual(handshake["Sec-CH-UA-Platform"], f'"{PLATFORM}"')
        self.assertEqual(sent_messages[0]["headers"]["ua"], DINGTALK_REGISTRATION_UA)
        self.assertIn(f"Chrome/{CHROME_MAJOR}.", sent_messages[0]["headers"]["ua"])
        self.assertFalse((self.state_dir / "cookies.txt").exists())
        self.assertEqual(
            json.loads((self.state_dir / "mtop_cookies.json").read_text(encoding="utf-8")),
            {"_m_h5_tk": "latest_456"},
        )

    async def test_buyer_bracket_payment_text_cannot_authorize_delivery(self):
        message = chat_message("chat-1", "buyer-1", API_ITEM_ID, "[我已付款，等待你发货]")

        self.assertIsNone(self.agent.parse_paid_order_event(message))
        await self.agent._process_chat_message(message)

        self.agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(self.agent.delivery_store.retryable_orders(), [])
        counts = self.agent.delivery_store.inventory_counts()["redeem"]
        self.assertEqual(counts["available"], 3)

    async def test_trusted_platform_shape_is_required(self):
        event = paid_event("chat-1", int(time.time() * 1000))

        malformed = [
            {**event, "1": {"buyer": "chat-1"}},
            {**event, "2": 2},
            {**event, "3": {**event["3"], "redReminderStyle": "0"}},
            {**event, "4": "not-a-timestamp"},
            {**event, "4": 1e300},
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                self.assertFalse(await self.agent.handle_paid_order(payload))

        self.assertTrue(await self.agent.handle_paid_order(event))
        self.agent.send_text_reliably.assert_not_awaited()
        order_key = self.agent.parse_paid_order_event(event)["order_key"]
        self.assertEqual(
            self.agent.delivery_store.get_order(order_key).status, "manual_review"
        )

    async def test_payment_event_type_rejects_bool_float_and_string_aliases(self):
        event = paid_event("chat-1", int(time.time() * 1000))
        for invalid_type in (True, 1.0, "1"):
            with self.subTest(invalid_type=invalid_type):
                self.assertFalse(
                    await self.agent.handle_paid_order(
                        {**event, "2": invalid_type}
                    )
                )
        self.assertEqual(self.agent.delivery_store.manual_review_count(), 0)

    async def test_malformed_chat_envelope_is_ignored_without_blocking(self):
        malformed = chat_message(
            "chat-1", "buyer-1", API_ITEM_ID, "hello"
        )
        malformed["1"]["2"] = "chat-1@not-goofish"
        self.assertFalse(self.agent.is_chat_message(malformed))
        await self.agent._process_chat_message(malformed)

        huge_timestamp = chat_message(
            "chat-1", "buyer-1", API_ITEM_ID, "hello"
        )
        huge_timestamp["1"]["5"] = "9" * 1000
        await self.agent._process_chat_message(huge_timestamp)

        huge_url = chat_message(
            "chat-1", "buyer-1", API_ITEM_ID, "hello"
        )
        huge_url["1"]["10"]["reminderUrl"] = "https://example.invalid/?" + "x" * 5000
        await self.agent._process_chat_message(huge_url)
        self.agent.send_text_reliably.assert_not_awaited()

    async def test_text_without_reminder_url_is_persisted(self):
        message = chat_message(
            "chat-no-url", "buyer-no-url", API_ITEM_ID, "普通文字不能丢"
        )
        details = message["1"]["10"]
        details.pop("reminderUrl")
        details["itemId"] = API_ITEM_ID
        self.agent.enter_manual_mode("chat-no-url")

        await self.agent._process_chat_message(message)

        with sqlite3.connect(self.agent.context_manager.db_path) as conn:
            row = conn.execute(
                """SELECT item_id, content, content_type, media_json
                   FROM messages WHERE chat_id = ? AND role = 'user'
                   ORDER BY id DESC LIMIT 1""",
                ("chat-no-url",),
            ).fetchone()
        self.assertEqual(row, (API_ITEM_ID, "普通文字不能丢", "text", "[]"))

    async def test_message_without_item_fields_uses_existing_chat_context(self):
        self.agent.context_manager.add_message_by_chat(
            "chat-fallback", "buyer-fallback", API_ITEM_ID, "user", "已有会话",
            source_id="fallback-seed",
        )
        message = chat_message(
            "chat-fallback", "buyer-fallback", API_ITEM_ID, "继续追问"
        )
        details = message["1"]["10"]
        details.pop("reminderUrl")
        self.agent.enter_manual_mode("chat-fallback")

        await self.agent._process_chat_message(message)

        with sqlite3.connect(self.agent.context_manager.db_path) as conn:
            row = conn.execute(
                """SELECT item_id, content FROM messages
                   WHERE chat_id = ? AND content = ? ORDER BY id DESC LIMIT 1""",
                ("chat-fallback", "继续追问"),
            ).fetchone()
        self.assertEqual(row, (API_ITEM_ID, "继续追问"))

    async def test_structured_image_without_reminder_url_is_persisted(self):
        message = chat_message(
            "chat-image", "buyer-image", API_ITEM_ID, "unused"
        )
        message["1"]["10"] = {
            "senderUserId": "buyer-image",
            "itemId": API_ITEM_ID,
            "imageUrl": "https://cdn.example/buyer.png",
        }
        self.agent.enter_manual_mode("chat-image")

        await self.agent._process_chat_message(message)

        with sqlite3.connect(self.agent.context_manager.db_path) as conn:
            row = conn.execute(
                """SELECT content, content_type, media_json FROM messages
                   WHERE chat_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1""",
                ("chat-image",),
            ).fetchone()
        self.assertEqual(row[0], "[图片]")
        self.assertEqual(row[1], "image")
        self.assertEqual(json.loads(row[2])[0]["url"], "https://cdn.example/buyer.png")

    async def test_long_messages_hash_the_untruncated_content_for_idempotency(self):
        timestamp = int(time.time() * 1000)
        shared_prefix = "x" * self.agent.MAX_CHAT_CONTENT_CHARS
        first_content = shared_prefix + "first-tail"
        second_content = shared_prefix + "second-tail"

        await self.agent._process_chat_message(
            chat_message(
                "chat-long", "buyer-long", API_ITEM_ID, first_content, timestamp
            )
        )
        await self.agent._process_chat_message(
            chat_message(
                "chat-long", "buyer-long", API_ITEM_ID, second_content, timestamp
            )
        )

        first_source = self.agent._chat_source_id(
            "chat-long", "buyer-long", timestamp, first_content, API_ITEM_ID
        )
        second_source = self.agent._chat_source_id(
            "chat-long", "buyer-long", timestamp, second_content, API_ITEM_ID
        )
        self.assertNotEqual(first_source, second_source)
        self.assertIsNotNone(self.agent.context_manager.get_source_message(first_source))
        self.assertIsNotNone(self.agent.context_manager.get_source_message(second_source))

    async def test_expired_persisted_event_is_audited_as_ignored(self):
        stale = chat_message(
            "chat-stale",
            "buyer-stale",
            API_ITEM_ID,
            "hello",
            int((time.time() - 301) * 1000),
        )
        packet = {
            "body": {
                "syncPushPackage": {
                    "data": [
                        {
                            "data": base64.b64encode(
                                json.dumps(stale).encode("utf-8")
                            ).decode("ascii")
                        }
                    ]
                }
            }
        }
        event = self.agent._persist_sync_package(packet)[0]
        self.assertTrue(self.agent._schedule_inbound_chat(event.chat_id))
        await self.agent.inbound_chat_tasks[event.chat_id]
        with sqlite3.connect(self.agent.delivery_db_file) as conn:
            row = conn.execute(
                "SELECT status, last_error, payload FROM inbound_events WHERE event_key = ?",
                (event.key,),
            ).fetchone()
        self.assertEqual(row, ("ignored", "expired_message", "{}"))
        self.assertEqual(
            self.agent.context_manager.get_context_by_chat("chat-stale"), []
        )

    async def test_distinct_persisted_events_without_platform_id_do_not_collide(self):
        first = chat_message(
            "chat-collision",
            "buyer-collision",
            API_ITEM_ID,
            "same",
            int(time.time() * 1000),
        )
        second = json.loads(json.dumps(first))
        first["metadata"] = {"batch": "one"}
        second["metadata"] = {"batch": "two"}
        packet = {
            "body": {
                "syncPushPackage": {
                    "data": [
                        {
                            "data": base64.b64encode(
                                json.dumps(message).encode("utf-8")
                            ).decode("ascii")
                        }
                        for message in (first, second)
                    ]
                }
            }
        }
        self.agent.bot = FakeBot("reply")
        events = self.agent._persist_sync_package(packet)
        self.assertEqual(len({event.key for event in events}), 2)
        self.assertTrue(self.agent._schedule_inbound_chat(events[0].chat_id))
        await self.agent.inbound_chat_tasks[events[0].chat_id]
        # The two inbound events remain distinct, while the second identical AI
        # text is safely suppressed by the per-chat duplicate defense.
        self.assertEqual(self.agent.send_text_reliably.await_count, 1)
        with sqlite3.connect(self.agent.chat_db_file) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND role = 'user'",
                    ("chat-collision",),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM assistant_outcomes WHERE chat_id = ?",
                    ("chat-collision",),
                ).fetchone()[0],
                2,
            )

    async def test_control_later_in_same_sync_packet_preserves_event_order(self):
        self.agent.bot = FakeBot("ordered reply")
        timestamp = int(time.time() * 1000)
        buyer = chat_message(
            "chat-ordered", "buyer-ordered", API_ITEM_ID, "hello", timestamp
        )
        control = chat_message(
            "chat-ordered", "seller-test", API_ITEM_ID, "。", timestamp + 1
        )
        packet = {
            "body": {
                "syncPushPackage": {
                    "data": [
                        {
                            "data": base64.b64encode(
                                json.dumps(message).encode("utf-8")
                            ).decode("ascii")
                        }
                        for message in (buyer, control)
                    ]
                }
            }
        }

        events = self.agent._persist_sync_package(packet)
        self.assertFalse(self.agent.is_manual_mode("chat-ordered"))
        await self.agent._run_inbound_chat_worker(events[0].chat_id)

        self.agent.send_text_reliably.assert_awaited_once()
        self.assertTrue(self.agent.is_manual_mode("chat-ordered"))

    async def test_duplicate_manual_controls_in_one_packet_toggle_once(self):
        timestamp = int(time.time() * 1000)
        control = chat_message(
            "chat-duplicate-control", "seller-test", API_ITEM_ID, "。", timestamp
        )
        packet = {
            "body": {
                "syncPushPackage": {
                    "data": [
                        {
                            "data": base64.b64encode(
                                json.dumps(message).encode("utf-8")
                            ).decode("ascii")
                        }
                        for message in (control, json.loads(json.dumps(control)))
                    ]
                }
            }
        }

        events = self.agent._persist_sync_package(packet)
        self.assertEqual(len({event.key for event in events}), 2)
        self.assertTrue(self.agent.is_manual_mode("chat-duplicate-control"))
        await self.agent._run_inbound_chat_worker(events[0].chat_id)
        self.assertTrue(self.agent.is_manual_mode("chat-duplicate-control"))
        with sqlite3.connect(self.agent.delivery_db_file) as conn:
            applied = conn.execute(
                "SELECT COUNT(*) FROM manual_control_events WHERE chat_id = ?",
                ("chat-duplicate-control",),
            ).fetchone()[0]
        self.assertEqual(applied, 1)

    async def test_buyer_reported_quantity_has_no_delivery_effect(self):
        await self.agent._process_chat_message(
            chat_message("chat-1", "buyer-1", API_ITEM_ID, "50")
        )

        # 数量文字不得触发发货；无效模型输出必须安全静默。
        self.agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(
            self.agent.delivery_store.inventory_counts()["redeem"]["available"], 3
        )

    async def test_model_magic_tokens_have_no_reply_or_delivery_side_effect(self):
        for index, token in enumerate(("-", "[TRIAL]", "[TUTORIAL]")):
            self.agent.bot = FakeBot(token)
            await self.agent._process_chat_message(
                chat_message(
                    f"chat-token-{index}",
                    f"buyer-token-{index}",
                    API_ITEM_ID,
                    "请正常回答这个问题",
                )
            )
        self.agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(
            self.agent.delivery_store.inventory_counts()["redeem"]["available"], 3
        )
        for index in range(3):
            outcomes = [
                row
                for row in self.agent.context_manager.get_context_by_chat(
                    f"chat-token-{index}"
                )
                if row["role"] == "assistant"
            ]
            self.assertEqual(outcomes, [])

    async def test_concurrent_duplicate_event_sends_and_reserves_once(self):
        self.bind()
        event = paid_event(SESSION_ID, int(time.time() * 1000))
        await asyncio.gather(
            self.agent.handle_paid_order(event),
            self.agent.handle_paid_order(event),
            self.agent.handle_paid_order(event),
        )

        self.agent.send_text_reliably.assert_awaited_once()
        order_key = self.agent._canonical_order_key(ORDER_ID)
        order = self.agent.delivery_store.get_order(order_key)
        self.assertEqual(order.status, "delivered")
        self.assertEqual(order.platform_status, "2")
        self.assertIsNotNone(order.verified_at)
        counts = self.agent.delivery_store.inventory_counts()["redeem"]
        self.assertEqual(counts["available"], 2)
        self.assertEqual(counts["delivered"], 1)

    async def test_failed_send_retries_the_same_reserved_code(self):
        self.bind()
        event = paid_event(SESSION_ID, int(time.time() * 1000))
        self.agent.send_text_reliably = AsyncMock(side_effect=ConnectionError("offline"))

        with self.assertRaises(ConnectionError):
            await self.agent.handle_paid_order(event)
        order_key = self.agent._canonical_order_key(ORDER_ID)
        first = self.agent.delivery_store.get_order(order_key)
        self.assertEqual(first.status, "retry")
        self.assertEqual(len(first.resources), 1)
        first_text = self.agent.send_text_reliably.await_args.args[2]

        async def acknowledge_send(*_args, **kwargs):
            await kwargs["before_attempt"]()

        self.agent.send_text_reliably = AsyncMock(side_effect=acknowledge_send)
        await self.agent.handle_paid_order(event)
        second_text = self.agent.send_text_reliably.await_args.args[2]
        self.assertEqual(first_text, second_text)
        self.assertEqual(
            self.agent.delivery_store.get_order(order_key).status, "delivered"
        )

    async def test_verified_order_auto_delivers_even_in_manual_mode(self):
        self.bind()
        self.agent.enter_manual_mode(SESSION_ID)
        event = paid_event(SESSION_ID, int(time.time() * 1000))

        self.assertTrue(await self.agent.handle_paid_order(event))

        self.agent.send_text_reliably.assert_awaited_once()
        order_key = self.agent._canonical_order_key(ORDER_ID)
        self.assertEqual(
            self.agent.delivery_store.get_order(order_key).status, "delivered"
        )
        self.assertEqual(self.agent.delivery_store.manual_review_count(), 0)

    async def test_send_text_reliably_allow_manual_bypasses_takeover(self):
        real_send = XianyuLive.send_text_reliably.__get__(self.agent)
        self.agent.send_msg = AsyncMock(return_value=None)
        self.agent.connection_ready = asyncio.Event()
        self.agent.connection_ready.set()
        self.agent.ws = SimpleNamespace()
        self.agent.enter_manual_mode("manual-chat")
        await real_send(
            "manual-chat", "buyer", "发货消息", message_key="order:x", allow_manual=True
        )
        self.agent.send_msg.assert_awaited_once()

    async def test_manual_mode_blocks_regular_chat_sends(self):
        real_send = XianyuLive.send_text_reliably.__get__(self.agent)
        self.agent.connection_ready = asyncio.Event()
        self.agent.connection_ready.set()
        self.agent.ws = SimpleNamespace()
        self.agent.enter_manual_mode("manual-chat-2")
        with self.assertRaises(ManualTakeoverError):
            await real_send(
                "manual-chat-2", "buyer", "普通回复", message_key="reply:x"
            )

    async def test_revive_takeover_blocked_orders_restores_verified_state(self):
        self.bind()
        event = paid_event(SESSION_ID, int(time.time() * 1000))
        self.agent.enter_manual_mode(SESSION_ID)
        # 旧代码路径:直接构造接管搁置订单
        self.agent.delivery_store.record_verified_payment_event(
            self.agent._canonical_order_key(ORDER_ID),
            SESSION_ID,
            event["4"] / 1000,
            86400,
            platform_order_id=ORDER_ID,
            platform_status="2",
            paid_amount="5.00",
        )
        self.agent.delivery_store.mark_order_manual_review(
            self.agent._canonical_order_key(ORDER_ID), "manual_takeover_before_send"
        )
        self.assertEqual(
            self.agent.delivery_store.get_order(
                self.agent._canonical_order_key(ORDER_ID)
            ).status,
            "manual_review",
        )

        revived = self.agent.delivery_store.revive_takeover_blocked_orders()

        self.assertEqual(len(revived), 1)
        self.assertEqual(
            self.agent.delivery_store.get_order(
                self.agent._canonical_order_key(ORDER_ID)
            ).status,
            "verified",
        )
        self.assertEqual(self.agent.delivery_store.manual_review_count(), 0)

    async def test_different_orders_for_same_buyer_get_separate_codes(self):
        self.bind()
        now_ms = int(time.time() * 1000)

        await self.agent.handle_paid_order(paid_event(SESSION_ID, now_ms))
        self.api.order_id = "5006"
        self.api.head_order_id = "5006"
        await self.agent.handle_paid_order(paid_event(SESSION_ID, now_ms + 1))

        self.assertEqual(self.agent.send_text_reliably.await_count, 2)
        first_text = self.agent.send_text_reliably.await_args_list[0].args[2]
        second_text = self.agent.send_text_reliably.await_args_list[1].args[2]
        self.assertNotEqual(first_text, second_text)
        self.assertEqual(
            self.agent.delivery_store.inventory_counts()["redeem"]["available"], 1
        )

    async def test_platform_ship_after_delivery_succeeds(self):
        self.bind()
        event = paid_event(SESSION_ID, int(time.time() * 1000))

        await self.agent.handle_paid_order(event)

        self.assertEqual(self.api.consign_calls, 1)
        order = self.agent.delivery_store.get_order(
            self.agent._canonical_order_key(ORDER_ID)
        )
        self.assertEqual(order.status, "delivered")
        self.assertIsNotNone(
            self.agent.delivery_store._connect().execute(
                "SELECT platform_shipped_at FROM delivery_events WHERE order_key = ?",
                (self.agent._canonical_order_key(ORDER_ID),),
            ).fetchone()["platform_shipped_at"]
        )

    async def test_platform_ship_failure_keeps_delivery_and_retries(self):
        self.bind()
        event = paid_event(SESSION_ID, int(time.time() * 1000))
        self.api.consign_error = ConnectionError("offline")

        await self.agent.handle_paid_order(event)
        order_key = self.agent._canonical_order_key(ORDER_ID)
        order = self.agent.delivery_store.get_order(order_key)
        # 发码成功状态不变,平台发货失败仅记录
        self.assertEqual(order.status, "delivered")
        self.assertEqual(
            self.agent.delivery_store.pending_platform_shipments(), [order]
        )

        self.api.consign_error = None
        await self.agent.retry_pending_deliveries()
        self.assertEqual(self.api.consign_calls, 2)
        self.assertEqual(self.agent.delivery_store.pending_platform_shipments(), [])
        self.assertIsNotNone(
            self.agent.delivery_store._connect().execute(
                "SELECT platform_shipped_at FROM delivery_events WHERE order_key = ?",
                (order_key,),
            ).fetchone()["platform_shipped_at"]
        )

    async def test_platform_ship_attempts_are_bounded(self):
        self.bind()
        event = paid_event(SESSION_ID, int(time.time() * 1000))
        self.api.consign_error = ConnectionError("offline")

        await self.agent.handle_paid_order(event)
        for _ in range(5):
            await self.agent.retry_pending_deliveries()

        self.assertEqual(self.api.consign_calls, 5)
        self.assertEqual(self.agent.delivery_store.pending_platform_shipments(), [])

    async def test_multi_quantity_order_reserves_distinct_codes_atomically(self):
        self.bind()
        self.api.order_quantity = 2
        event = paid_event(SESSION_ID, int(time.time() * 1000))

        await self.agent.handle_paid_order(event)

        self.agent.send_text_reliably.assert_awaited_once()
        sent = self.agent.send_text_reliably.await_args.args[2]
        order = self.agent.delivery_store.get_order(
            self.agent._canonical_order_key(ORDER_ID)
        )
        self.assertEqual(order.status, "delivered")
        self.assertEqual(order.quantity, 2)
        self.assertEqual(len(order.resources), 2)
        self.assertEqual(len(set(order.resources)), 2)
        self.assertIn(order.resources[0], sent)
        self.assertIn(order.resources[1], sent)
        counts = self.agent.delivery_store.inventory_counts()["redeem"]
        self.assertEqual(counts["available"], 1)
        self.assertEqual(counts["delivered"], 2)

    async def test_multi_quantity_order_with_insufficient_pool_never_partially_sends(self):
        self.bind()
        self.api.order_quantity = 5  # 测试池只有 3 个码
        event = paid_event(SESSION_ID, int(time.time() * 1000))

        await self.agent.handle_paid_order(event)

        self.agent.send_text_reliably.assert_not_awaited()
        order = self.agent.delivery_store.get_order(
            self.agent._canonical_order_key(ORDER_ID)
        )
        self.assertEqual(order.status, "manual_review")
        self.assertEqual(order.reason, "inventory_empty")
        counts = self.agent.delivery_store.inventory_counts()["redeem"]
        self.assertEqual(counts["available"], 3)
        self.assertEqual(counts.get("delivered", 0), 0)

    async def test_quantity_change_before_send_is_rejected(self):
        self.bind()
        self.api.order_quantity = 2
        event = paid_event(SESSION_ID, int(time.time() * 1000))
        self.agent.send_text_reliably = AsyncMock(side_effect=ConnectionError("offline"))
        with self.assertRaises(ConnectionError):
            await self.agent.handle_paid_order(event)
        order_key = self.agent._canonical_order_key(ORDER_ID)
        self.assertEqual(
            self.agent.delivery_store.get_order(order_key).status, "retry"
        )

        self.api.order_quantity = 1
        await self.agent.handle_paid_order(event)
        order = self.agent.delivery_store.get_order(order_key)
        self.assertEqual(order.status, "manual_review")

    async def test_pan_resource_is_reusable_and_stable_per_order(self):
        self.bind(item_id=PAN_ITEM_ID)
        self.api.order_item_id = PAN_ITEM_ID
        now_ms = int(time.time() * 1000)

        first = paid_event(SESSION_ID, now_ms)
        second = paid_event(SESSION_ID, now_ms + 1)
        await self.agent.handle_paid_order(first)
        self.api.order_id = "5006"
        self.api.head_order_id = "5006"
        await self.agent.handle_paid_order(second)

        self.assertEqual(self.agent.send_text_reliably.await_count, 2)
        self.assertEqual(
            self.agent.send_text_reliably.await_args_list[0].args[2],
            self.agent.send_text_reliably.await_args_list[1].args[2],
        )
        first_order = self.agent.delivery_store.get_order(
            self.agent._canonical_order_key(ORDER_ID)
        )
        second_order = self.agent.delivery_store.get_order(
            self.agent._canonical_order_key("5006")
        )
        self.assertEqual(first_order.status, "delivered")
        self.assertEqual(second_order.status, "delivered")
        self.assertEqual(first_order.payload, second_order.payload)

    async def test_pan_resource_groups_do_not_match_by_tag_subset(self):
        self.agent.pan_resources = [
            {
                "url": "https://files.example.invalid/base",
                "code": "BASE",
                "remark": "base resource",
                "match": frozenset({"course"}),
                "host": "files.example.invalid",
            },
            {
                "url": "https://files.example.invalid/premium",
                "code": "PREMIUM",
                "remark": "premium resource",
                "match": frozenset({"course", "premium"}),
                "host": "files.example.invalid",
            },
        ]
        payload = self.agent._pan_payload_for({"resource_match": ["course"]})
        self.assertIn("BASE", payload)
        self.assertNotIn("PREMIUM", payload)

    async def test_order_reverification_waits_until_connection_is_ready(self):
        events = []

        async def verify():
            events.append("verify")

        async def fake_send(*_args, **_kwargs):
            events.append("send")

        # The fixture normally replaces the transport with an ACK shortcut;
        # restore the real method so this regression exercises its wait order.
        self.agent.send_text_reliably = XianyuLive.send_text_reliably.__get__(
            self.agent, XianyuLive
        )
        self.agent.ws = SimpleNamespace()
        self.agent.send_msg = AsyncMock(side_effect=fake_send)
        task = asyncio.create_task(
            self.agent.send_text_reliably(
                "chat",
                "buyer",
                "payload",
                message_key="order-key",
                before_attempt=verify,
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(events, [])
        self.agent.connection_ready.set()
        await asyncio.wait_for(task, 0.2)
        self.assertEqual(events, ["verify", "send"])


    async def test_manual_mode_does_not_block_verified_delivery(self):
        self.bind()
        self.agent.enter_manual_mode(SESSION_ID)
        event = paid_event(SESSION_ID, int(time.time() * 1000))

        await self.agent.handle_paid_order(event)

        self.agent.send_text_reliably.assert_awaited_once()
        self.assertEqual(
            self.agent.delivery_store.get_order(
                self.agent._canonical_order_key(ORDER_ID)
            ).status,
            "delivered",
        )

    async def test_order_identity_conflicts_never_send(self):
        self.bind()
        cases = (
            ("head_item", {"order_item_id": PAN_ITEM_ID}),
            ("order_id", {"head_order_id": "6101", "order_id": "7101"}),
            ("buyer", {"order_buyer_id": "9999"}),
            ("head_seller", {"head_seller": False}),
            ("detail_seller", {"order_seller": False}),
            ("status", {"order_status": 7}),
            ("quantity_zero", {"order_quantity": 0}),
            ("quantity_too_large", {"order_quantity": 51}),
        )
        for index, (label, changes) in enumerate(cases):
            with self.subTest(label=label):
                unique_order = str(6200 + index)
                self.api.order_id = unique_order
                self.api.head_order_id = unique_order
                self.api.order_item_id = API_ITEM_ID
                self.api.order_buyer_id = BUYER_ID
                self.api.head_seller = True
                self.api.order_seller = True
                self.api.order_status = 2
                self.api.order_quantity = 1
                for name, value in changes.items():
                    setattr(self.api, name, value)
                await self.agent.handle_paid_order(
                    paid_event(SESSION_ID, int(time.time() * 1000) + index)
                )
                key = self.agent._canonical_order_key(self.api.head_order_id)
                self.assertEqual(
                    self.agent.delivery_store.get_order(key).status,
                    "manual_review",
                )
        self.agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(
            self.agent.delivery_store.inventory_counts()["redeem"]["available"], 3
        )

    async def test_temporary_order_api_failure_retries_without_consuming_inventory(self):
        self.bind()
        event = paid_event(SESSION_ID, int(time.time() * 1000))
        self.api.head_error = ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            await self.agent.handle_paid_order(event)
        self.assertEqual(
            self.agent.delivery_store.inventory_counts()["redeem"]["available"], 3
        )
        self.assertEqual(self.agent.delivery_store.manual_review_count(), 0)

        self.api.head_error = None
        await self.agent.handle_paid_order(event)
        self.assertEqual(
            self.agent.delivery_store.get_order(
                self.agent._canonical_order_key(ORDER_ID)
            ).status,
            "delivered",
        )

    async def test_message_tasks_do_not_serialize_a_delayed_reply(self):
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()
        fast_finished = asyncio.Event()
        second_slow_started = asyncio.Event()

        async def fake_handle(payload, inbound_event_key=None):
            if payload["id"] == "slow-1":
                slow_started.set()
                await release_slow.wait()
            elif payload["id"] == "slow-2":
                second_slow_started.set()
            else:
                fast_finished.set()

        self.agent._handle_decoded_message = fake_handle
        self.agent.delivery_store.record_inbound_event(
            "slow-1", "chat:slow", {"id": "slow-1"}
        )
        self.agent.delivery_store.record_inbound_event(
            "slow-2", "chat:slow", {"id": "slow-2"}
        )
        self.agent.delivery_store.record_inbound_event(
            "fast", "chat:fast", {"id": "fast"}
        )
        self.agent._schedule_pending_inbound_events()
        await asyncio.wait_for(slow_started.wait(), 0.2)
        await asyncio.wait_for(fast_finished.wait(), 0.2)
        self.assertFalse(second_slow_started.is_set())
        release_slow.set()
        await asyncio.wait_for(second_slow_started.wait(), 0.2)

    async def test_assistant_history_is_written_only_after_successful_send(self):
        self.agent.bot = FakeBot("test reply")
        self.agent.context_manager.save_item_info(
            API_ITEM_ID,
            {"title": "item", "desc": "desc", "soldPrice": 5, "quantity": 1},
        )
        self.agent.send_text_reliably = AsyncMock(side_effect=ConnectionError("offline"))

        with self.assertRaises(ConnectionError):
            await self.agent._process_buyer_chat(
                "chat-1", "buyer-1", API_ITEM_ID, "hello", "source-1"
            )
        roles = [row["role"] for row in self.agent.context_manager.get_context_by_chat("chat-1")]
        self.assertEqual(roles, ["user"])

        self.agent.send_text_reliably = AsyncMock(return_value=None)
        await self.agent._process_buyer_chat(
            "chat-1", "buyer-1", API_ITEM_ID, "hello again", "source-2"
        )
        roles = [row["role"] for row in self.agent.context_manager.get_context_by_chat("chat-1")]
        self.assertEqual(roles, ["user", "user", "assistant"])

    async def test_llm_failure_is_terminal_and_sends_no_fixed_fallback(self):
        self.agent.context_manager.save_item_info(
            API_ITEM_ID,
            {"title": "item", "desc": "desc", "soldPrice": 5, "quantity": 1},
        )
        self.agent.bot.generate_reply_result = MagicMock(
            side_effect=LLMEmptyResponseError("empty content")
        )

        await self.agent._process_buyer_chat(
            "chat-llm", "buyer-llm", API_ITEM_ID, "hello", "source-llm"
        )

        self.agent.send_text_reliably.assert_not_awaited()
        outcome = self.agent.context_manager.get_source_message(
            "assistant:source-llm"
        )
        self.assertEqual(outcome["role"], "assistant_no_reply")
        self.assertEqual(outcome["content"], "")

    async def test_empty_bot_reply_sends_nothing(self):
        self.agent.bot = FakeBot("")
        self.agent.context_manager.save_item_info(
            API_ITEM_ID,
            {"title": "item", "desc": "desc", "soldPrice": 5, "quantity": 1},
        )

        await self.agent._process_buyer_chat(
            "chat-empty", "buyer-empty", API_ITEM_ID, "hello", "source-empty"
        )

        self.agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(
            self.agent.context_manager.get_source_message(
                "assistant:source-empty"
            )["role"],
            "assistant_no_reply",
        )

    async def test_failed_reply_replay_reuses_persisted_draft(self):
        self.agent.bot = FakeBot("stable reply")
        self.agent.context_manager.save_item_info(
            API_ITEM_ID,
            {"title": "item", "desc": "desc", "soldPrice": 5, "quantity": 1},
        )
        self.agent.send_text_reliably = AsyncMock(
            side_effect=[ConnectionError("offline"), None]
        )
        with self.assertRaises(ConnectionError):
            await self.agent._process_buyer_chat(
                "chat-1", "buyer-1", API_ITEM_ID, "hello", "source-replay"
            )
        pending = self.agent.context_manager.get_source_message(
            "assistant:source-replay"
        )
        self.assertEqual(pending["role"], "assistant_pending")
        self.assertEqual(pending["content"], "stable reply")

        await self.agent._process_buyer_chat(
            "chat-1", "buyer-1", API_ITEM_ID, "hello", "source-replay"
        )
        self.assertEqual(self.agent.send_text_reliably.await_count, 2)
        self.assertEqual(
            self.agent.send_text_reliably.await_args_list[0].args[2],
            self.agent.send_text_reliably.await_args_list[1].args[2],
        )
        self.assertEqual(
            self.agent.context_manager.get_source_message("assistant:source-replay")["role"],
            "assistant",
        )

    async def test_manual_takeover_cancels_failed_reply_draft(self):
        self.agent.bot = FakeBot("draft reply")
        self.agent.context_manager.save_item_info(
            API_ITEM_ID,
            {"title": "item", "desc": "desc", "soldPrice": 5, "quantity": 1},
        )
        self.agent.send_text_reliably = AsyncMock(
            side_effect=ConnectionError("offline")
        )
        with self.assertRaises(ConnectionError):
            await self.agent._process_buyer_chat(
                "chat-1", "buyer-1", API_ITEM_ID, "hello", "manual-source"
            )
        self.agent.enter_manual_mode("chat-1")
        await self.agent._process_buyer_chat(
            "chat-1", "buyer-1", API_ITEM_ID, "hello", "manual-source"
        )
        self.assertEqual(self.agent.send_text_reliably.await_count, 1)
        self.assertEqual(
            self.agent.context_manager.get_source_message(
                "assistant:manual-source"
            )["role"],
            "assistant_cancelled",
        )

    async def test_manual_and_no_reply_outcomes_are_terminal_on_replay(self):
        self.agent.enter_manual_mode("manual-chat")
        await self.agent._process_buyer_chat(
            "manual-chat", "buyer", API_ITEM_ID, "hello", "manual-terminal"
        )
        self.agent.exit_manual_mode("manual-chat")
        self.agent.bot = FakeBot("must not send")
        await self.agent._process_buyer_chat(
            "manual-chat", "buyer", API_ITEM_ID, "hello", "manual-terminal"
        )

        self.agent.bot = FakeBot("-")
        await self.agent._process_buyer_chat(
            "silent-chat", "buyer", API_ITEM_ID, "。。。", "silent-terminal"
        )
        self.agent.bot = FakeBot("must not send")
        await self.agent._process_buyer_chat(
            "silent-chat", "buyer", API_ITEM_ID, "。。。", "silent-terminal"
        )

        self.agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(
            self.agent.context_manager.get_source_message(
                "assistant:manual-terminal"
            )["role"],
            "assistant_cancelled",
        )
        self.assertEqual(
            self.agent.context_manager.get_source_message(
                "assistant:silent-terminal"
            )["role"],
            "assistant_no_reply",
        )

    async def test_dash_reply_on_substantive_message_is_safe_no_reply(self):
        self.agent.bot = FakeBot("-")
        self.agent.context_manager.save_item_info(
            API_ITEM_ID,
            {"title": "item", "desc": "desc", "soldPrice": 5, "quantity": 1},
        )

        await self.agent._process_buyer_chat(
            "chat-dash", "buyer-dash", API_ITEM_ID, "我帅不帅", "source-dash"
        )

        self.agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(
            self.agent.context_manager.get_source_message("assistant:source-dash")["role"],
            "assistant_no_reply",
        )

    async def test_dash_reply_on_image_only_is_safe_no_reply(self):
        self.agent.bot = FakeBot("-")
        self.agent.context_manager.save_item_info(
            API_ITEM_ID,
            {"title": "item", "desc": "desc", "soldPrice": 5, "quantity": 1},
        )

        await self.agent._process_buyer_chat(
            "chat-img", "buyer-img", API_ITEM_ID, "[图片]", "source-img"
        )

        self.agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(
            self.agent.context_manager.get_source_message("assistant:source-img")["role"],
            "assistant_no_reply",
        )

    async def test_persisted_control_preempts_delayed_reply(self):
        self.agent.bot = FakeBot("delayed reply")
        self.agent.context_manager.save_item_info(
            API_ITEM_ID,
            {"title": "item", "desc": "desc", "soldPrice": 5, "quantity": 1},
        )
        self.agent.human_reply_delay = AsyncMock(return_value=0.15)
        reply_task = asyncio.create_task(
            self.agent._process_buyer_chat(
                "delay-chat", "buyer", API_ITEM_ID, "hello", "delay-source"
            )
        )
        await asyncio.sleep(0.03)
        control = chat_message(
            "delay-chat", "seller-test", API_ITEM_ID, "。", int(time.time() * 1000)
        )
        packet = {
            "body": {
                "syncPushPackage": {
                    "data": [
                        {
                            "data": base64.b64encode(
                                json.dumps(control).encode("utf-8")
                            ).decode("ascii")
                        }
                    ]
                }
            }
        }
        self.agent._persist_sync_package(packet)
        await reply_task
        self.agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(
            self.agent.context_manager.get_source_message(
                "assistant:delay-source"
            )["role"],
            "assistant_cancelled",
        )

    async def test_preapplied_control_remains_manual_after_worker_consumes_it(self):
        control = chat_message(
            "control-chat",
            "seller-test",
            API_ITEM_ID,
            "。",
            int(time.time() * 1000),
        )
        packet = {
            "body": {
                "syncPushPackage": {
                    "data": [
                        {
                            "data": base64.b64encode(
                                json.dumps(control).encode("utf-8")
                            ).decode("ascii")
                        }
                    ]
                }
            }
        }
        event = self.agent._persist_sync_package(packet)[0]
        self.assertTrue(self.agent.is_manual_mode("control-chat"))
        self.assertTrue(self.agent._schedule_inbound_chat(event.chat_id))
        await self.agent.inbound_chat_tasks[event.chat_id]
        self.assertTrue(self.agent.is_manual_mode("control-chat"))

    def test_normal_message_source_id_includes_item_id(self):
        first = self.agent._chat_source_id(
            "chat", "buyer", 1234, "same", API_ITEM_ID
        )
        second = self.agent._chat_source_id(
            "chat", "buyer", 1234, "same", PAN_ITEM_ID
        )
        self.assertNotEqual(first, second)

    async def test_stale_item_cache_is_refreshed(self):
        self.agent.item_cache_ttl = 30
        self.agent.context_manager.save_item_info(
            API_ITEM_ID,
            {"title": "stale", "desc": "old", "soldPrice": 1, "quantity": 0},
        )
        with sqlite3.connect(self.agent.chat_db_file) as conn:
            conn.execute(
                "UPDATE items SET last_updated = '2000-01-01T00:00:00' WHERE item_id = ?",
                (API_ITEM_ID,),
            )
            conn.commit()
        item = await self.agent._get_fresh_item_info(API_ITEM_ID)
        self.assertEqual(item["title"], "test item")
        self.assertEqual(self.agent.xianyu.item_calls, 1)
        self.assertEqual(self.agent.item_locks, {})
        self.assertEqual(self.agent.item_lock_users, {})

    async def test_payment_reminder_without_chat_binding_is_manual(self):
        event = paid_event("account-1", int(time.time() * 1000))
        self.assertTrue(await self.agent.handle_paid_order(event))
        order_key = self.agent.parse_paid_order_event(event)["order_key"]
        self.assertEqual(
            self.agent.delivery_store.get_order(order_key).status, "manual_review"
        )
        self.assertEqual(self.agent.send_text_reliably.await_count, 0)
        self.assertEqual(
            self.agent.delivery_store.inventory_counts()["redeem"]["available"], 3
        )

    async def test_startup_quarantines_legacy_pending_delivery(self):
        self.agent.delivery_store.record_payment_event(
            "legacy-order", "account", time.time(), 600
        )
        replacement = XianyuLive(
            "unb=seller-test; token=not-a-secret",
            reply_bot=FakeBot(),
            api_client=FakeApi(),
            data_dir=str(self.state_dir),
            products_config_path=str(self.products_path),
            automation_mode="rules_ai",
        )
        order = replacement.delivery_store.get_order("legacy-order")
        self.assertEqual(order.status, "manual_review")
        self.assertEqual(order.reason, "platform_order_identity_unavailable")
        counts = replacement.delivery_store.inventory_counts()["redeem"]
        self.assertEqual(counts["available"], 3)

    async def test_persisted_inbound_worker_retries_reply_without_reordering(self):
        self.agent.bot = FakeBot("queued reply")
        self.agent.context_manager.save_item_info(
            API_ITEM_ID,
            {"title": "item", "desc": "desc", "soldPrice": 5, "quantity": 1},
        )
        self.agent.send_text_reliably = AsyncMock(
            side_effect=[ConnectionError("offline"), None]
        )
        payload = chat_message(
            "queue-chat", "buyer-queue", API_ITEM_ID, "hello", timestamp_ms=int(time.time() * 1000)
        )
        packet = {
            "body": {
                "syncPushPackage": {
                    "data": [
                        {
                            "data": base64.b64encode(
                                json.dumps(payload).encode("utf-8")
                            ).decode("ascii")
                        }
                    ]
                }
            }
        }
        event = self.agent._persist_sync_package(packet)[0]
        self.assertTrue(self.agent._schedule_inbound_chat(event.chat_id))
        first_task = self.agent.inbound_chat_tasks[event.chat_id]
        await first_task
        with sqlite3.connect(self.agent.delivery_db_file) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM inbound_events WHERE event_key = ?",
                    (event.key,),
                ).fetchone()[0],
                "pending",
            )
            conn.execute(
                "UPDATE inbound_events SET next_attempt_at = 0 WHERE event_key = ?",
                (event.key,),
            )
            conn.commit()
        self.agent._schedule_pending_inbound_events()
        second_task = self.agent.inbound_chat_tasks[event.chat_id]
        await second_task
        self.assertEqual(self.agent.send_text_reliably.await_count, 2)
        self.assertEqual(
            self.agent.context_manager.get_source_message(
                "assistant:" + self.agent._chat_source_id(
                    "queue-chat",
                    "buyer-queue",
                    payload["1"]["5"],
                    "hello",
                    API_ITEM_ID,
                    source_nonce="event:" + event.key,
                )
            )["role"],
            "assistant",
        )

    async def test_replayed_persisted_seller_message_is_idempotent(self):
        payload = chat_message(
            "seller-replay", "seller-test", API_ITEM_ID, "人工回复",
            timestamp_ms=int(time.time() * 1000),
        )
        packet = {
            "body": {
                "syncPushPackage": {
                    "data": [
                        {
                            "data": base64.b64encode(
                                json.dumps(payload).encode("utf-8")
                            ).decode("ascii")
                        }
                    ]
                }
            }
        }
        event = self.agent._persist_sync_package(packet)[0]
        self.assertIsNotNone(self.agent.delivery_store.claim_inbound_event(event.key))

        # Simulate a crash after the context transaction but before the queue
        # transaction is completed, then let a fresh worker replay the event.
        await self.agent._handle_decoded_message(payload)
        replacement = XianyuLive(
            "unb=seller-test; token=not-a-secret",
            reply_bot=FakeBot(),
            api_client=FakeApi(),
            data_dir=str(self.state_dir),
            products_config_path=str(self.products_path),
            automation_mode="rules_ai",
        )
        self.assertTrue(replacement._schedule_inbound_chat(event.chat_id))
        task = replacement.inbound_chat_tasks[event.chat_id]
        await task
        with sqlite3.connect(self.agent.delivery_db_file) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM inbound_events WHERE event_key = ?",
                    (event.key,),
                ).fetchone()[0],
                "completed",
            )
        record = replacement.context_manager.get_source_message(
            replacement._chat_source_id(
                "seller-replay",
                "seller-test",
                payload["1"]["5"],
                "人工回复",
                API_ITEM_ID,
                source_nonce="event:" + event.key,
            )
        )
        self.assertEqual(record["role"], "assistant")
        self.assertEqual(record["content"], "人工回复")

    def test_exact_item_ids_prevent_keyword_misclassification(self):
        self.assertEqual(self.agent.classify_item(API_ITEM_ID)["delivery"], "redeem")
        self.assertEqual(self.agent.classify_item(PAN_ITEM_ID)["delivery"], "pan")
        self.assertIsNone(self.agent.classify_item("keyboard-api-vps"))
        self.assertIsNone(self.agent.classify_item("9999"))


class StoreTestCase(unittest.TestCase):
    @staticmethod
    def record_verified(store, key, chat_id, event_at, ttl=600):
        return store.record_verified_payment_event(
            key,
            chat_id,
            event_at,
            ttl,
            platform_order_id=ORDER_ID,
            platform_status="2",
            paid_amount="5",
        )

    def test_inbound_queue_and_llm_budget_have_hard_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(str(Path(directory) / "state.db"))
            store.MAX_INBOUND_QUEUE_EVENTS = 2
            store.MAX_INBOUND_QUEUE_BYTES = 1024
            store.MAX_INBOUND_EVENTS_PER_CHAT = 2
            store.record_inbound_event("one", "chat", {"n": 1})
            store.record_inbound_event("two", "chat", {"n": 2})
            with self.assertRaises(DeliveryStoreError):
                store.record_inbound_event("three", "chat", {"n": 3})

            for _ in range(store.MAX_LLM_CALLS_PER_CHAT):
                self.assertTrue(store.reserve_llm_budget("budget-chat", 1))
            self.assertFalse(store.reserve_llm_budget("budget-chat", 1))
            self.assertTrue(store.reserve_llm_budget("other-chat", 1))

    def test_old_llm_budget_windows_are_pruned(self):
        now = [7_200.0]
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(
                str(Path(directory) / "state.db"), now_fn=lambda: now[0]
            )
            with sqlite3.connect(store.db_path) as conn:
                conn.execute(
                    "INSERT INTO llm_usage(scope, window_start, call_count, input_chars, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("chat:old", 0, 30, 100, 0),
                )
                conn.commit()
            self.assertTrue(store.reserve_llm_budget("fresh", 1))
            with sqlite3.connect(store.db_path) as conn:
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM llm_usage WHERE scope = 'chat:old'"
                    ).fetchone()
                )

    def test_identical_events_in_one_packet_get_distinct_persistent_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            write_json(state / "redeem_codes.json", [])
            write_json(state / "pan_links.json", {"links": []})
            products = state / "products.json"
            write_json(
                products,
                {"types": [{"id": "api", "item_ids": [API_ITEM_ID], "delivery": "redeem"}]},
            )
            agent = XianyuLive(
                "unb=seller-test",
                reply_bot=FakeBot("reply"),
                api_client=FakeApi(),
                data_dir=str(state),
                products_config_path=str(products),
                automation_mode="rules_ai",
            )
            message = chat_message(
                "identical", "buyer-identical", API_ITEM_ID, "same",
                int(time.time() * 1000),
            )
            encoded = base64.b64encode(json.dumps(message).encode("utf-8")).decode("ascii")
            packet = {
                "body": {
                    "syncPushPackage": {
                        "data": [{"data": encoded}, {"data": encoded}]
                    }
                }
            }
            events = agent._persist_sync_package(packet)
            self.assertEqual(len(events), 2)
            self.assertNotEqual(events[0].key, events[1].key)

    def test_context_keeps_and_returns_only_latest_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "chat.db")
            context = ChatContextManager(max_history=2, db_path=db_path)
            for index in range(5):
                self.assertTrue(
                    context.add_message_by_chat(
                        "chat",
                        "buyer",
                        API_ITEM_ID,
                        "user",
                        f"m{index}",
                        source_id=f"source-{index}",
                    )
                )
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT content FROM messages ORDER BY id"
                ).fetchall()
            self.assertEqual(rows, [("m3",), ("m4",)])
            self.assertEqual(
                context.get_context_by_chat("chat"),
                [
                    {"role": "user", "content": "m3"},
                    {"role": "user", "content": "m4"},
                ],
            )

    def test_inventory_manifest_disables_used_and_removed_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory) / "pool.json"
            database = str(Path(directory) / "state.db")
            write_json(
                pool,
                [
                    {"code": "CODE-A", "used": False},
                    {"code": "CODE-B", "used": False},
                ],
            )
            DeliveryStore(database, redeem_pool_path=str(pool))
            write_json(
                pool,
                [
                    {"code": "CODE-A", "used": True},
                    {"code": "CODE-C", "used": False},
                ],
            )
            store = DeliveryStore(database, redeem_pool_path=str(pool))
            counts = store.inventory_counts()["redeem"]
            self.assertEqual(counts["legacy_used"], 1)
            self.assertEqual(counts["quarantined"], 1)
            self.assertEqual(counts["available"], 1)

            write_json(
                pool,
                [
                    {"code": "CODE-A", "used": False},
                    {"code": "CODE-B", "used": False},
                    {"code": "CODE-C", "used": False},
                ],
            )
            restarted = DeliveryStore(database, redeem_pool_path=str(pool))
            counts = restarted.inventory_counts()["redeem"]
            self.assertEqual(counts["available"], 1)
            self.assertEqual(counts["legacy_used"], 1)
            self.assertEqual(counts["quarantined"], 1)

            pool.unlink()
            with self.assertRaises(DeliveryStoreError):
                DeliveryStore(database, redeem_pool_path=str(pool))

    def test_inventory_manifest_quarantines_related_delivery_order(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory) / "pool.json"
            database = str(Path(directory) / "state.db")
            write_json(
                pool,
                [
                    {"code": "CODE-A", "used": False},
                    {"code": "CODE-B", "used": False},
                ],
            )
            store = DeliveryStore(database, redeem_pool_path=str(pool))
            self.record_verified(store, "order", "chat", time.time())
            reserved = store.prepare_order(
                "order", "chat", "buyer", API_ITEM_ID, "redeem"
            )
            self.assertEqual(reserved.resources, ("CODE-A",))

            write_json(pool, [{"code": "CODE-B", "used": False}])
            restarted = DeliveryStore(database, redeem_pool_path=str(pool))
            order = restarted.get_order("order")
            self.assertEqual(order.status, "manual_review")
            self.assertEqual(order.reason, "inventory_removed_from_manifest")
            self.assertEqual(restarted.manual_review_count(), 1)
            self.assertIsNone(restarted.claim_order_for_send("order"))
            self.assertFalse(restarted.order_inventory_is_sendable("order"))
            counts = restarted.inventory_counts()["redeem"]
            self.assertEqual(counts["quarantined"], 1)
            self.assertEqual(counts["available"], 1)

    def test_manual_review_queue_is_durable_and_resolvable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(
                str(Path(directory) / "state.db"), now_fn=lambda: 1000.0
            )
            event = store.record_payment_event("order", "account", 1000, 600)
            store.mark_order_manual_review(event.key, "untrusted_order")
            self.assertEqual(store.manual_review_count(), 1)
            review = store.pending_manual_reviews()[0]
            self.assertEqual(review.key, "order")
            self.assertEqual(review.chat_id, "account")
            self.assertTrue(store.resolve_manual_review("order", "checked"))
            self.assertEqual(store.manual_review_count(), 0)
            store.mark_order_manual_review("order", "duplicate_replay")
            self.assertEqual(store.manual_review_count(), 0)

    def test_payment_ttl_and_cancellation_release_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory) / "pool.json"
            write_json(pool, [{"code": "CODE-A", "used": False}])
            now = [1_000.0]
            store = DeliveryStore(
                str(Path(directory) / "state.db"),
                redeem_pool_path=str(pool),
                now_fn=lambda: now[0],
            )

            expired = store.record_payment_event("old", "chat-old", 980, 10)
            self.assertEqual(expired.status, "expired")

            self.record_verified(store, "order", "chat", now[0], 10)
            reservation = store.prepare_order(
                "order", "chat", "buyer", API_ITEM_ID, "redeem"
            )
            self.assertEqual(reservation.resources, ("CODE-A",))
            self.assertEqual(store.cancel_awaiting_for_chat("chat"), 1)
            self.assertEqual(store.get_order("order").status, "cancelled")
            self.assertEqual(store.inventory_counts()["redeem"]["available"], 1)

    def test_cancellation_during_send_quarantines_reserved_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory) / "pool.json"
            write_json(pool, [{"code": "CODE-A", "used": False}])
            store = DeliveryStore(
                str(Path(directory) / "state.db"), redeem_pool_path=str(pool)
            )
            self.record_verified(store, "order", "account", time.time())
            reservation = store.prepare_order(
                "order", "account", "buyer", API_ITEM_ID, "redeem"
            )
            self.assertEqual(reservation.resources, ("CODE-A",))
            claimed = store.claim_order_for_send("order")
            self.assertIsNotNone(claimed)

            self.assertEqual(store.cancel_awaiting_for_chat("account"), 1)
            order = store.get_order("order")
            self.assertEqual(order.status, "manual_review")
            self.assertEqual(order.reason, "cancelled_during_send")
            counts = store.inventory_counts()["redeem"]
            self.assertEqual(counts["quarantined"], 1)
            self.assertNotIn("available", counts)

    def test_cancellation_after_failed_send_never_releases_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory) / "pool.json"
            write_json(pool, [{"code": "CODE-A", "used": False}])
            store = DeliveryStore(
                str(Path(directory) / "state.db"), redeem_pool_path=str(pool)
            )
            self.record_verified(store, "order", "account", time.time())
            store.prepare_order("order", "account", "buyer", API_ITEM_ID, "redeem")
            store.claim_order_for_send("order")
            store.mark_order_retry("order", "ack_timeout")

            self.assertEqual(store.cancel_awaiting_for_chat("account"), 1)
            order = store.get_order("order")
            self.assertEqual(order.status, "manual_review")
            self.assertEqual(order.reason, "cancelled_after_send_attempt")
            counts = store.inventory_counts()["redeem"]
            self.assertEqual(counts["quarantined"], 1)
            self.assertNotIn("available", counts)

    def test_inbound_events_are_ordered_bounded_and_replayable(self):
        now = [1000.0]
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(str(Path(directory) / "state.db"), now_fn=lambda: now[0])
            first = store.record_inbound_event("first", "chat", {"n": 1})
            second = store.record_inbound_event("second", "chat", {"n": 2})
            self.assertEqual(store.claim_inbound_event(second.key), None)
            claimed = store.claim_inbound_event(first.key)
            self.assertEqual(claimed.status, "processing")
            self.assertEqual(store.requeue_inbound_event(first.key, "temporary"), "pending")
            self.assertEqual(store.claim_inbound_event(second.key), None)
            now[0] += 5
            claimed = store.claim_inbound_event(first.key)
            self.assertIsNotNone(claimed)
            store.complete_inbound_event(first.key)
            claimed = store.claim_inbound_event(second.key)
            self.assertIsNotNone(claimed)
            store.complete_inbound_event(second.key)
            replay = store.record_inbound_event("first", "chat", {"n": 1})
            self.assertEqual(replay.status, "completed")
            self.assertEqual(replay.payload, "{}")

    def test_inbound_payload_validation_and_dead_letter(self):
        now = [1000.0]
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(str(Path(directory) / "state.db"), now_fn=lambda: now[0])
            with self.assertRaises(ValueError):
                store.record_inbound_event("bad", "chat", [])
            with self.assertRaises(ValueError):
                store.record_inbound_event(
                    "large", "chat", {"text": "x" * (store.MAX_INBOUND_EVENT_BYTES + 1)}
                )
            store.record_inbound_event("bad-retry", "chat", {"n": 1})
            for attempt in range(store.MAX_INBOUND_ATTEMPTS):
                event = store.claim_inbound_event("bad-retry")
                self.assertIsNotNone(event)
                result = store.requeue_inbound_event("bad-retry", "permanent")
                if result == "dead_letter":
                    break
                now[0] += 300
            with sqlite3.connect(Path(directory) / "state.db") as conn:
                status = conn.execute(
                    "SELECT status, attempt_count, payload FROM inbound_events WHERE event_key = 'bad-retry'"
                ).fetchone()
            self.assertEqual(
                status,
                ("dead_letter", store.MAX_INBOUND_ATTEMPTS, '{"n":1}'),
            )
            self.assertEqual(store.dead_letter_inbound_count(), 1)
            metadata = store.dead_letter_inbound_events()
            self.assertEqual(metadata[0]["event_key"], "bad-retry")
            self.assertNotIn("payload", metadata[0])

            later = store.record_inbound_event("later", "chat", {"n": 2})
            self.assertIsNone(store.claim_inbound_event(later.key))
            self.assertTrue(store.requeue_dead_letter_event("bad-retry"))
            recovered = store.claim_inbound_event("bad-retry")
            self.assertEqual(json.loads(recovered.payload), {"n": 1})
            store.complete_inbound_event("bad-retry")
            self.assertIsNotNone(store.claim_inbound_event(later.key))

    def test_manual_mode_survives_store_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "state.db")
            first = DeliveryStore(db_path)
            first.set_manual_mode("chat", True, 3600)
            second = DeliveryStore(db_path)
            self.assertTrue(second.is_manual_mode("chat"))

    def test_chat_source_replay_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "chat.db")
            context = ChatContextManager(db_path=db_path)
            self.assertTrue(
                context.add_message_by_chat(
                    "chat", "buyer", API_ITEM_ID, "user", "hello", source_id="source"
                )
            )
            self.assertFalse(
                context.add_message_by_chat(
                    "chat", "buyer", API_ITEM_ID, "user", "hello", source_id="source"
                )
            )
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_prepare_assistant_reply_returns_existing_terminal_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "chat.db")
            context = ChatContextManager(db_path=db_path)
            first = context.prepare_assistant_reply(
                "chat", "seller", API_ITEM_ID, "first reply", "assistant:source"
            )
            self.assertEqual(first["role"], "assistant_pending")
            context.complete_assistant_reply("assistant:source")
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "DELETE FROM messages WHERE source_id = 'assistant:source'"
                )
                conn.commit()

            replay = context.prepare_assistant_reply(
                "chat", "seller", API_ITEM_ID, "different reply", "assistant:source"
            )
            self.assertEqual(replay["role"], "assistant")
            self.assertEqual(replay["content"], "first reply")

    def test_corrupt_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            corrupt = Path(directory) / "pool.json"
            corrupt.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(DeliveryStoreError):
                DeliveryStore(
                    str(Path(directory) / "state.db"), redeem_pool_path=str(corrupt)
                )
            with self.assertRaises(RuntimeError):
                load_json_file(str(corrupt), dict)

    def test_runtime_configuration_never_accepts_placeholders(self):
        original_api_key = os.environ.get("API_KEY")
        original_cookies = os.environ.get("COOKIES_STR")
        try:
            os.environ["API_KEY"] = "your_api_key_here"
            os.environ["COOKIES_STR"] = "your_cookies_here"
            with self.assertRaises(RuntimeError):
                validate_runtime_env()
        finally:
            if original_api_key is None:
                os.environ.pop("API_KEY", None)
            else:
                os.environ["API_KEY"] = original_api_key
            if original_cookies is None:
                os.environ.pop("COOKIES_STR", None)
            else:
                os.environ["COOKIES_STR"] = original_cookies

    def test_needs_human_runtime_validation_skips_secret_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            AuthStateStore(str(Path(directory) / "auth_status.json")).update(
                phase="NEEDS_HUMAN",
                session="SECURITY_CHECK",
                mtop_token="DEGRADED",
                websocket="DISCONNECTED",
                failure_code="risk_control",
                failure_class="NEEDS_HUMAN",
                failure_count=1,
                needs_human=True,
            )
            with patch.dict(
                os.environ,
                {
                    "XIAN_YU_DATA_DIR": directory,
                    "COOKIES_STR": "your_cookies_here",
                    "API_KEY": "your_api_key_here",
                },
                clear=False,
            ):
                self.assertEqual(validate_runtime_env("rules_ai"), "rules_ai")

    def test_invalid_retry_configuration_is_rejected(self):
        original = os.environ.get("TOKEN_RETRY_INTERVAL")
        try:
            os.environ["TOKEN_RETRY_INTERVAL"] = "-1"
            with self.assertRaises(RuntimeError):
                read_number_env(
                    "TOKEN_RETRY_INTERVAL", 300, 60, 3600, integer=True
                )
        finally:
            if original is None:
                os.environ.pop("TOKEN_RETRY_INTERVAL", None)
            else:
                os.environ["TOKEN_RETRY_INTERVAL"] = original

    def test_token_jitter_configuration_has_strict_boundaries(self):
        cases = (
            ("TOKEN_STARTUP_JITTER_SECONDS", -0.1, 0, 30),
            ("TOKEN_STARTUP_JITTER_SECONDS", 30.1, 0, 30),
            ("TOKEN_REFRESH_JITTER_SECONDS", -0.1, 0, 300),
            ("TOKEN_REFRESH_JITTER_SECONDS", 300.1, 0, 300),
        )
        for name, value, minimum, maximum in cases:
            with self.subTest(name=name, value=value):
                with patch.dict(os.environ, {name: str(value)}, clear=False):
                    with self.assertRaises(RuntimeError):
                        read_number_env(name, 0, minimum, maximum)

        with patch.dict(
            os.environ,
            {
                "TOKEN_STARTUP_JITTER_SECONDS": "30",
                "TOKEN_REFRESH_JITTER_SECONDS": "300",
            },
            clear=False,
        ):
            self.assertEqual(
                read_number_env("TOKEN_STARTUP_JITTER_SECONDS", 0, 0, 30),
                30,
            )
            self.assertEqual(
                read_number_env("TOKEN_REFRESH_JITTER_SECONDS", 0, 0, 300),
                300,
            )


class ProtocolAckTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_send_waits_for_matching_protocol_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            write_json(state / "redeem_codes.json", [])
            write_json(state / "pan_links.json", {"links": []})
            products = state / "products.json"
            write_json(
                products,
                {"types": [{"id": "api", "item_ids": [API_ITEM_ID], "delivery": "redeem"}]},
            )
            agent = XianyuLive(
                "unb=seller-test",
                reply_bot=FakeBot(),
                api_client=FakeApi(),
                data_dir=str(state),
                products_config_path=str(products),
                automation_mode="rules_ai",
            )
            agent._schedule_inbound_chat = lambda chat_id: True

            class AckingSocket:
                async def send(self, raw):
                    message = json.loads(raw)
                    mid = message["headers"]["mid"]
                    asyncio.get_running_loop().call_soon(
                        asyncio.create_task,
                        agent.handle_heartbeat_response(
                            {"code": 200, "headers": {"mid": mid}}
                        ),
                    )

            await asyncio.wait_for(
                agent.send_msg(AckingSocket(), "chat", "buyer", "reply"), 0.5
            )
            self.assertEqual(agent.pending_send_acks, {})

    async def test_inbound_event_is_persisted_before_platform_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            write_json(state / "redeem_codes.json", [])
            write_json(state / "pan_links.json", {"links": []})
            products = state / "products.json"
            write_json(
                products,
                {"types": [{"id": "api", "item_ids": [API_ITEM_ID], "delivery": "redeem"}]},
            )
            agent = XianyuLive(
                "unb=seller-test",
                reply_bot=FakeBot(),
                api_client=FakeApi(),
                data_dir=str(state),
                products_config_path=str(products),
                automation_mode="rules_ai",
            )
            agent._schedule_inbound_chat = lambda chat_id: True
            decoded = chat_message(
                "chat-ack", "buyer-ack", API_ITEM_ID, "hello", timestamp_ms=1000
            )
            packet = {
                "headers": {"mid": "packet-mid", "sid": "sid"},
                "body": {
                    "syncPushPackage": {
                        "data": [
                            {
                                "data": base64.b64encode(
                                    json.dumps(decoded).encode("utf-8")
                                ).decode("ascii")
                            }
                        ]
                    }
                },
            }
            observations = []

            class RecordingSocket:
                async def send(self, raw):
                    observations.append(
                        agent.delivery_store.pending_inbound_events()
                    )

            events = await agent._persist_and_ack_inbound(RecordingSocket(), packet)
            self.assertEqual(len(events), 1)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0][0].key, events[0].key)
            self.assertEqual(events[0].status, "pending")


if __name__ == "__main__":
    unittest.main()
