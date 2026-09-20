"""Focused offline tests for pending-order reconciliation.

Run from `worker/`: ``python -m unittest tests.test_pending_order_scan -v``
(part of ``npm run test:worker``). These use synthetic platform data only; no
network, no real cookies, no real shipments.
"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from delivery_store import DeliveryStore
from main import AuthenticationUnavailableError, XianyuLive
from XianyuApis import XianyuApis, XianyuApiError


def make_agent(*, binding="chat-1"):
    agent = object.__new__(XianyuLive)
    agent.xianyu = mock.Mock()
    agent.products = {"111": {"delivery": "redeem"}}
    agent.payment_notice_retention = 100
    agent.delivery_store = mock.Mock()
    agent.delivery_store.get_order.return_value = None
    agent.delivery_store.find_chat_binding.return_value = binding
    agent.myid = "seller-1"
    agent.ws = None
    agent.connection_ready = asyncio.Event()
    agent.pending_conversation_requests = {}
    agent.conversation_create_timeout = 1
    return agent


def verified_detail(**overrides):
    detail = {
        "order_id": "1",
        "item_id": "111",
        "buyer_id": "9",
        "seller": "seller-1",
        "status": 2,
        "quantity": 1,
        "paid_amount": "9.90",
        "ut_status": "x",
    }
    detail.update(overrides)
    return detail


PENDING_ORDER = {
    "order_id": "1",
    "item_id": "111",
    "buyer_id": "9",
    "quantity": 1,
    "paid_amount": "9.90",
    "in_refund": False,
}


class ParseSoldOrderTest(unittest.TestCase):
    def test_parses_verified_facts(self):
        parsed = XianyuLive._parse_sold_order({
            "commonData": {"orderId": 123, "itemId": 111, "inRefund": False},
            "buyerInfoVO": {"buyerId": 999},
            "priceVO": {"totalPrice": "9.90", "buyNum": 2},
        })
        self.assertEqual(parsed["order_id"], "123")
        self.assertEqual(parsed["item_id"], "111")
        self.assertEqual(parsed["buyer_id"], "999")
        self.assertEqual(parsed["quantity"], 2)
        self.assertEqual(parsed["paid_amount"], "9.90")
        self.assertFalse(parsed["in_refund"])

    def test_rejects_missing_identity(self):
        with self.assertRaises(RuntimeError):
            XianyuLive._parse_sold_order({
                "commonData": {"orderId": 1, "itemId": 111},
                "buyerInfoVO": {},
                "priceVO": {"buyNum": 1},
            })

    def test_rejects_out_of_range_quantity(self):
        with self.assertRaises(RuntimeError):
            XianyuLive._parse_sold_order({
                "commonData": {"orderId": 1, "itemId": 111},
                "buyerInfoVO": {"buyerId": 9},
                "priceVO": {"buyNum": 0},
            })


class ReconcilePendingOrderTest(unittest.TestCase):
    def test_no_binding_resolves_real_conversation_id(self):
        agent = make_agent(binding=None)
        agent._call_xianyu_api = mock.AsyncMock(return_value={})
        agent._parse_order_detail = mock.Mock(return_value=verified_detail())
        agent._fulfill_order = mock.AsyncMock()
        agent._create_order_conversation = mock.AsyncMock(return_value="real-cid")
        asyncio.run(agent._reconcile_pending_order(dict(PENDING_ORDER)))
        # 会话 ID 来自平台创建响应，而不是 buyer_id 猜测。
        agent._create_order_conversation.assert_awaited_once_with("9", "111")
        agent.delivery_store.record_chat_binding.assert_called_once()
        self.assertEqual(
            agent.delivery_store.record_chat_binding.call_args.args[0], "real-cid"
        )
        agent.delivery_store.record_verified_payment_event.assert_called_once()
        self.assertEqual(
            agent.delivery_store.record_verified_payment_event.call_args.args[1],
            "real-cid",
        )
        agent._fulfill_order.assert_awaited_once()
        self.assertEqual(agent._fulfill_order.await_args.kwargs["buyer_id"], "9")
        self.assertEqual(agent._fulfill_order.await_args.kwargs["item_id"], "111")

    def test_no_binding_conversation_failure_is_recoverable(self):
        agent = make_agent(binding=None)
        agent._call_xianyu_api = mock.AsyncMock(return_value={})
        agent._parse_order_detail = mock.Mock(return_value=verified_detail())
        agent._fulfill_order = mock.AsyncMock()
        agent._create_order_conversation = mock.AsyncMock(return_value=None)
        asyncio.run(agent._reconcile_pending_order(dict(PENDING_ORDER)))
        agent.delivery_store.record_chat_binding.assert_not_called()
        agent.delivery_store.record_verified_payment_event.assert_not_called()
        agent._fulfill_order.assert_not_awaited()

    def test_invalid_orders_never_create_conversation(self):
        refund_agent = make_agent(binding=None)
        refund_agent._create_order_conversation = mock.AsyncMock(return_value="real-cid")
        asyncio.run(
            refund_agent._reconcile_pending_order(dict(PENDING_ORDER, in_refund=True))
        )
        refund_agent._create_order_conversation.assert_not_called()

        mismatch_agent = make_agent(binding=None)
        mismatch_agent._call_xianyu_api = mock.AsyncMock(return_value={})
        mismatch_agent._parse_order_detail = mock.Mock(
            return_value=verified_detail(buyer_id="8")
        )
        mismatch_agent._fulfill_order = mock.AsyncMock()
        mismatch_agent._create_order_conversation = mock.AsyncMock(return_value="real-cid")
        asyncio.run(mismatch_agent._reconcile_pending_order(dict(PENDING_ORDER)))
        mismatch_agent._create_order_conversation.assert_not_called()
        mismatch_agent._fulfill_order.assert_not_awaited()

    def test_refund_order_not_sent(self):
        agent = make_agent()
        order = dict(PENDING_ORDER, in_refund=True)
        asyncio.run(agent._reconcile_pending_order(order))
        agent.delivery_store.record_verified_payment_event.assert_not_called()

    def test_unmapped_item_not_sent(self):
        agent = make_agent()
        order = dict(PENDING_ORDER, item_id="222")
        asyncio.run(agent._reconcile_pending_order(order))
        agent.delivery_store.record_verified_payment_event.assert_not_called()

    def test_identity_mismatch_not_sent(self):
        agent = make_agent()
        agent._call_xianyu_api = mock.AsyncMock(return_value={})
        agent._parse_order_detail = mock.Mock(return_value=verified_detail(buyer_id="8"))
        agent._fulfill_order = mock.AsyncMock()
        asyncio.run(agent._reconcile_pending_order(dict(PENDING_ORDER)))
        agent.delivery_store.record_verified_payment_event.assert_not_called()
        agent._fulfill_order.assert_not_awaited()

    def test_foreign_seller_or_not_awaiting_not_sent(self):
        for detail in (verified_detail(seller=""), verified_detail(status=3)):
            agent = make_agent()
            agent._call_xianyu_api = mock.AsyncMock(return_value={})
            agent._parse_order_detail = mock.Mock(return_value=detail)
            agent._fulfill_order = mock.AsyncMock()
            asyncio.run(agent._reconcile_pending_order(dict(PENDING_ORDER)))
            agent._fulfill_order.assert_not_awaited()

    def test_verified_order_reuses_existing_fulfil_path(self):
        agent = make_agent()
        agent._call_xianyu_api = mock.AsyncMock(return_value={})
        agent._parse_order_detail = mock.Mock(return_value=verified_detail())
        agent._fulfill_order = mock.AsyncMock()
        asyncio.run(agent._reconcile_pending_order(dict(PENDING_ORDER)))
        agent.delivery_store.record_verified_payment_event.assert_called_once()
        agent._fulfill_order.assert_awaited_once()
        order_key = agent._canonical_order_key("1")
        self.assertEqual(agent._fulfill_order.await_args.args[0], order_key)

    def test_terminal_order_is_not_reprocessed(self):
        agent = make_agent()
        agent.delivery_store.get_order.return_value = mock.Mock(status="delivered")
        asyncio.run(agent._reconcile_pending_order(dict(PENDING_ORDER)))
        agent.delivery_store.find_chat_binding.assert_not_called()
        agent.delivery_store.record_verified_payment_event.assert_not_called()

    def test_auth_failure_during_detail_is_not_swallowed(self):
        agent = make_agent()
        agent._call_xianyu_api = mock.AsyncMock(
            side_effect=AuthenticationUnavailableError("session_expired")
        )
        with self.assertRaises(AuthenticationUnavailableError):
            asyncio.run(agent._reconcile_pending_order(dict(PENDING_ORDER)))
        agent.delivery_store.record_verified_payment_event.assert_not_called()


class ConversationCreateTest(unittest.TestCase):
    """Offline protocol tests for the SingleChatConversation/create request."""

    def make_conv_agent(self):
        agent = object.__new__(XianyuLive)
        agent.myid = "seller-1"
        agent.pending_conversation_requests = {}
        agent.conversation_create_timeout = 1
        agent.connection_ready = asyncio.Event()
        agent.connection_ready.set()
        agent.ws = mock.Mock()
        agent.ws.send = mock.AsyncMock()
        return agent

    async def _await_send(self, agent, count=1):
        for _ in range(100):
            await asyncio.sleep(0)
            if agent.ws.send.await_count >= count:
                return
        raise AssertionError("conversation request was not sent")

    def test_create_resolves_real_cid_and_correlates_mid(self):
        agent = self.make_conv_agent()

        async def run():
            task = asyncio.create_task(
                agent._create_order_conversation("buyer-1", "111")
            )
            await self._await_send(agent)
            sent = json.loads(agent.ws.send.call_args.args[0])
            mid = sent["headers"]["mid"]
            self.assertFalse(
                agent._resolve_conversation_response(
                    {"headers": {"mid": "other-mid"}, "body": {"singleChatConversation": {"cid": "x"}}}
                ),
                "a response with a different mid must not resolve the request",
            )
            self.assertTrue(
                agent._resolve_conversation_response(
                    {"headers": {"mid": mid}, "body": {"singleChatConversation": {"cid": "cid-1@goofish"}}}
                )
            )
            return await task, sent

        cid, sent = asyncio.run(run())
        self.assertEqual(cid, "cid-1")
        self.assertEqual(sent["lwp"], "/r/SingleChatConversation/create")
        body = sent["body"][0]
        self.assertEqual(body["pairFirst"], "buyer-1@goofish")
        self.assertEqual(body["pairSecond"], "seller-1@goofish")
        self.assertEqual(body["bizType"], "1")
        self.assertEqual(body["extension"], {"itemId": "111"})
        self.assertEqual(body["ctx"], {"appVersion": "1.0", "platform": "web"})
        self.assertEqual(agent.pending_conversation_requests, {})

    def test_missing_cid_does_not_resolve(self):
        agent = self.make_conv_agent()

        async def run():
            task = asyncio.create_task(
                agent._create_order_conversation("buyer-1", "111")
            )
            await self._await_send(agent)
            mid = json.loads(agent.ws.send.call_args.args[0])["headers"]["mid"]
            self.assertTrue(
                agent._resolve_conversation_response(
                    {"headers": {"mid": mid}, "body": {"singleChatConversation": {}}}
                )
            )
            return await task

        self.assertIsNone(asyncio.run(run()))

    def test_timeout_does_not_resolve(self):
        agent = self.make_conv_agent()
        agent.conversation_create_timeout = 0.05

        async def run():
            return await agent._create_order_conversation("buyer-1", "111")

        self.assertIsNone(asyncio.run(run()))
        self.assertEqual(agent.pending_conversation_requests, {})

    def test_blocked_send_times_out_and_cleans_response_future(self):
        agent = self.make_conv_agent()
        agent.conversation_create_timeout = 0.05

        async def run():
            blocked = asyncio.Event()
            send_cancelled = asyncio.Event()

            async def blocked_send(_message):
                try:
                    await blocked.wait()
                finally:
                    send_cancelled.set()

            agent.ws.send = mock.AsyncMock(side_effect=blocked_send)
            task = asyncio.create_task(
                agent._create_order_conversation("buyer-1", "111")
            )
            await self._await_send(agent)
            response_future = next(iter(agent.pending_conversation_requests.values()))
            result = await asyncio.wait_for(task, timeout=0.5)
            self.assertTrue(send_cancelled.is_set())
            self.assertTrue(response_future.cancelled())
            return result

        self.assertIsNone(asyncio.run(run()))
        self.assertEqual(agent.pending_conversation_requests, {})

    def test_concurrent_requests_correlate_by_mid(self):
        agent = self.make_conv_agent()

        async def run():
            first = asyncio.create_task(
                agent._create_order_conversation("buyer-1", "111")
            )
            second = asyncio.create_task(
                agent._create_order_conversation("buyer-2", "222")
            )
            await self._await_send(agent, count=2)
            mids = {}
            for call in agent.ws.send.call_args_list:
                msg = json.loads(call.args[0])
                mids[msg["body"][0]["pairFirst"]] = msg["headers"]["mid"]
            agent._resolve_conversation_response(
                {"headers": {"mid": mids["buyer-2@goofish"]}, "body": {"singleChatConversation": {"cid": "cid-2"}}}
            )
            agent._resolve_conversation_response(
                {"headers": {"mid": mids["buyer-1@goofish"]}, "body": {"singleChatConversation": {"cid": "cid-1"}}}
            )
            return await first, await second

        first_cid, second_cid = asyncio.run(run())
        self.assertEqual(first_cid, "cid-1")
        self.assertEqual(second_cid, "cid-2")


def make_scan_agent(**overrides):
    agent = object.__new__(XianyuLive)
    agent.xianyu = mock.Mock()
    agent.authentication_failure_code = None
    agent.token_circuit_open = False
    agent.products = {"111": {"delivery": "redeem"}}
    agent._pending_order_scan_running = False
    agent.pending_order_scan_pages = 1
    agent.pending_order_scan_rows = 50
    agent._refresh_runtime_config = mock.Mock()
    agent._call_xianyu_api = mock.AsyncMock()
    for name, value in overrides.items():
        setattr(agent, name, value)
    return agent


class ScanPendingOrdersTest(unittest.TestCase):
    def test_single_flight_and_capped_scan_is_explicit(self):
        agent = make_scan_agent()
        seen = []

        async def fake_reconcile(order):
            seen.append(order["order_id"])

        agent._reconcile_pending_order = fake_reconcile
        agent._call_xianyu_api.return_value = {
            "items": [{"commonData": {"orderId": 1, "itemId": 111}, "buyerInfoVO": {"buyerId": 9},
                       "priceVO": {"buyNum": 1, "totalPrice": "1.00"}}],
            "next_page": True,  # more pages exist but the cap is 1 -> must be reported
        }
        asyncio.run(agent.scan_pending_orders())
        self.assertEqual(seen, ["1"])
        self.assertFalse(agent._pending_order_scan_running)

    def test_single_flight_guard_skips_overlap(self):
        agent = make_scan_agent(_pending_order_scan_running=True)
        asyncio.run(agent.scan_pending_orders())
        agent._call_xianyu_api.assert_not_awaited()

    def test_no_mapped_product_never_calls_platform(self):
        agent = make_scan_agent(products={})
        asyncio.run(agent.scan_pending_orders())
        agent._call_xianyu_api.assert_not_awaited()
        self.assertFalse(agent._pending_order_scan_running)

    def test_auth_blocked_never_calls_platform(self):
        agent = make_scan_agent(
            authentication_failure_code="session_expired", token_circuit_open=True
        )
        asyncio.run(agent.scan_pending_orders())
        agent._call_xianyu_api.assert_not_awaited()

    def test_duplicate_ids_within_a_sweep_reconcile_once(self):
        agent = make_scan_agent()
        seen = []

        async def fake_reconcile(order):
            seen.append(order["order_id"])

        agent._reconcile_pending_order = fake_reconcile
        duplicate_item = {
            "commonData": {"orderId": 1, "itemId": 111},
            "buyerInfoVO": {"buyerId": 9},
            "priceVO": {"buyNum": 1, "totalPrice": "1.00"},
        }
        agent._call_xianyu_api.return_value = {
            "items": [duplicate_item, dict(duplicate_item)],
            "next_page": False,
        }
        asyncio.run(agent.scan_pending_orders())
        self.assertEqual(seen, ["1"])

    def test_bad_item_does_not_abort_the_sweep(self):
        agent = make_scan_agent()
        seen = []

        async def fake_reconcile(order):
            seen.append(order["order_id"])

        agent._reconcile_pending_order = fake_reconcile
        agent._call_xianyu_api.return_value = {
            "items": [
                {"commonData": {"orderId": 1, "itemId": 111}, "buyerInfoVO": {}},
                {"commonData": {"orderId": 2, "itemId": 111}, "buyerInfoVO": {"buyerId": 9},
                 "priceVO": {"buyNum": 1, "totalPrice": "1.00"}},
            ],
            "next_page": False,
        }
        asyncio.run(agent.scan_pending_orders())
        self.assertEqual(seen, ["2"])


class ScanLoopTest(unittest.TestCase):
    def test_api_failure_backs_off_then_cancel_stops_cleanly(self):
        agent = object.__new__(XianyuLive)
        agent.pending_order_scan_interval = 60
        calls = []

        async def failing_scan():
            calls.append("scan")
            if len(calls) == 1:
                raise XianyuApiError("network_error")
            raise asyncio.CancelledError()

        agent.scan_pending_orders = failing_scan
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        original_sleep = asyncio.sleep
        asyncio.sleep = fake_sleep
        try:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(agent._pending_order_scan_loop())
        finally:
            asyncio.sleep = original_sleep
        self.assertEqual(calls, ["scan", "scan"])
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 60)

    def test_auth_unavailable_ends_scanner_without_crash(self):
        agent = object.__new__(XianyuLive)
        agent.pending_order_scan_interval = 60

        async def auth_failing_scan():
            raise AuthenticationUnavailableError("session_expired")

        agent.scan_pending_orders = auth_failing_scan
        asyncio.run(agent._pending_order_scan_loop())


class SoldOrdersApiTest(unittest.TestCase):
    def test_request_shape_and_module_parsing(self):
        api = object.__new__(XianyuApis)
        api.trade_max_attempts = 3
        api._signed_mtop_request = mock.Mock(return_value={
            "data": {"module": {
                "items": [{"commonData": {"orderId": 1}}],
                "nextPage": True,
                "totalCount": 42,
            }}
        })
        result = api.get_sold_orders(1, 50, "NOT_SHIP")
        self.assertEqual(result["items"], [{"commonData": {"orderId": 1}}])
        self.assertTrue(result["next_page"])
        self.assertEqual(result["total_count"], 42)
        call = api._signed_mtop_request.call_args
        self.assertEqual(call.args[0], "mtop.taobao.idle.trade.merchant.sold.get")
        self.assertEqual(call.kwargs["headers"], XianyuApis.SELLER_ORIGIN_HEADERS)
        self.assertEqual(call.kwargs["value_type"], "string")
        payload = call.args[2]
        self.assertEqual(payload["queryCode"], "NOT_SHIP")
        self.assertEqual(payload["orderSearchParam"], "{}")
        self.assertEqual(payload["pageNumber"], 1)
        self.assertEqual(payload["rowsPerPage"], 50)

    def test_missing_module_is_empty_not_error(self):
        api = object.__new__(XianyuApis)
        api.trade_max_attempts = 3
        api._signed_mtop_request = mock.Mock(return_value={})
        self.assertEqual(
            api.get_sold_orders(),
            {"items": [], "next_page": False, "total_count": 0},
        )

    def test_out_of_range_rows_rejected(self):
        api = object.__new__(XianyuApis)
        api.trade_max_attempts = 3
        with self.assertRaises(ValueError):
            api.get_sold_orders(1, 51, "NOT_SHIP")
        with self.assertRaises(ValueError):
            api.get_sold_orders(1, 50, "NOT_A_CODE")


class ReconcileWithRealStoreTest(unittest.TestCase):
    def test_offline_missed_order_verifies_without_prior_chat_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeliveryStore(os.path.join(tmp, "scan.db"))
            store.record_chat_binding("chat-1", "9", "111")
            agent = object.__new__(XianyuLive)
            agent.xianyu = mock.Mock()
            agent.products = {"111": {"delivery": "redeem"}}
            agent.payment_notice_retention = 100
            agent.delivery_store = store
            agent._call_xianyu_api = mock.AsyncMock(return_value={})
            agent._parse_order_detail = mock.Mock(return_value=verified_detail())
            agent._fulfill_order = mock.AsyncMock()
            asyncio.run(agent._reconcile_pending_order(dict(PENDING_ORDER)))
            agent._fulfill_order.assert_awaited_once()
            order_key = XianyuLive._canonical_order_key("1")
            reservation = store.get_order(order_key)
            self.assertIsNotNone(reservation)
            self.assertEqual(reservation.platform_order_id, "1")
            self.assertEqual(reservation.status, "verified")
            # A second sweep must not clear the verified proof or re-reserve.
            asyncio.run(agent._reconcile_pending_order(dict(PENDING_ORDER)))
            self.assertEqual(store.get_order(order_key).status, "verified")


class FindChatBindingTest(unittest.TestCase):
    def test_unique_binding_wins_and_ambiguity_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeliveryStore(os.path.join(tmp, "bindings.db"))
            store.record_chat_binding("c1", "9", "111")
            self.assertEqual(store.find_chat_binding("9", "111"), "c1")
            store.record_chat_binding("c2", "9", "111")
            self.assertIsNone(store.find_chat_binding("9", "111"))
            self.assertIsNone(store.find_chat_binding("9", "222"))
            self.assertIsNone(store.find_chat_binding("", ""))


if __name__ == "__main__":
    unittest.main()
