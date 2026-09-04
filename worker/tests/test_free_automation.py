import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from XianyuAgent import LLMNotReadyError, LLMServiceError, LLMTimeoutError
from main import XianyuLive, load_automation_settings, load_reply_rules, validate_runtime_env, within_business_hours


API_ITEM = "1001"
PAN_ITEM = "2002"
MATERIAL_ITEM = "3003"
CHAT_ID = "4004"
BUYER_ID = "5005"
ORDER_ID = "6006"


def write_json(path, value):
    path = Path(path)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


class FakeApi:
    def __init__(self):
        self.session = SimpleNamespace(cookies={})
        self.order_quantity = 1

    def get_item_info(self, _item_id):
        return {
            "data": {
                "itemDO": {
                    "title": "item",
                    "desc": "description",
                    "soldPrice": 5,
                    "quantity": 1,
                    "skuList": [],
                }
            }
        }

    def get_message_head_info(self, _chat_id, _item_id):
        return {
            "data": {
                "commonData": {
                    "orderId": ORDER_ID,
                    "itemId": MATERIAL_ITEM,
                    "seller": True,
                }
            }
        }

    def get_order_detail(self, _order_id):
        return {
            "data": {
                "orderId": ORDER_ID,
                "itemId": MATERIAL_ITEM,
                "peerUserId": BUYER_ID,
                "seller": True,
                "status": 2,
                "utArgs": {"orderStatus": "WAIT_SELLER_SEND_GOODS"},
                "components": [
                    {
                        "render": "orderInfoVO",
                        "data": {
                            "itemInfo": {"buyAmount": self.order_quantity},
                            "priceInfo": {"amount": {"value": "5.00"}},
                        },
                    }
                ],
            }
        }

    def consign_dummy(self, _order_id):
        return {"ret": ["SUCCESS::ok"]}


class FakeBot:
    def __init__(
        self,
        reply="fallback",
        decision="reply",
        reason_code="ok",
        ready_error=None,
        config_revision=7,
    ):
        self.reply = reply
        self.calls = 0
        self.ready_calls = 0
        self.ready_expected_revisions = []
        self.ready_error = ready_error
        self.config_revision = config_revision
        self.ready_revision = config_revision
        self.last_intent = decision
        self.last_reason_code = reason_code
        self.requests = []

    def generate_reply_result(self, *_args, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        return {
            "reply": self.reply,
            "decision": self.last_intent,
            "reason_code": self.last_reason_code,
            "config_revision": self.config_revision,
        }

    def generate_reply(self, *_args, **kwargs):
        return self.generate_reply_result(*_args, **kwargs)["reply"]

    def ensure_ready(self, expected_config_revision):
        self.ready_calls += 1
        self.ready_expected_revisions.append(expected_config_revision)
        if self.ready_error is not None:
            raise self.ready_error
        if self.ready_revision != expected_config_revision:
            raise LLMNotReadyError("revision changed")
        return self.ready_revision


class ReplyRuleLoaderTests(unittest.TestCase):
    def test_first_match_and_unicode_casefold(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            write_json(
                path,
                {
                    "version": 1,
                    "rules": [
                        {"id": "first", "name": "指定商品问候", "item_id": API_ITEM, "keywords": ["HELLO"], "reply": "one"},
                        {"id": "second", "keywords": ["hello"], "reply": "two"},
                    ],
                },
            )
            rules = load_reply_rules(str(path))
            self.assertEqual(rules[0]["name"], "指定商品问候")
            self.assertEqual(rules[0]["item_id"], API_ITEM)
            self.assertEqual(rules[0]["reply"], "one")
            self.assertEqual(rules[0]["keywords"], ("hello",))

    def test_malformed_rule_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            write_json(path, {"rules": []})
            with self.assertRaises(RuntimeError):
                load_reply_rules(str(path))

    def test_unknown_fields_and_duplicate_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            base = {"version": 1, "rules": []}
            for payload in (
                {**base, "extra": True},
                {
                    "version": 1,
                    "rules": [
                        {"id": "same", "keywords": ["a"], "reply": "a"},
                        {"id": "same", "keywords": ["b"], "reply": "b"},
                    ],
                },
            ):
                write_json(path, payload)
                with self.assertRaises(RuntimeError):
                    load_reply_rules(str(path))


class FreeAutomationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state = Path(self.tempdir.name)
        write_json(
            self.state / "pan_links.json",
            {
                "links": [
                    {
                        "url": "https://files.example.invalid/resource",
                        "code": "TEST",
                        "remark": "resource",
                        "match": ["resource"],
                    }
                ]
            },
        )
        self.products = self.state / "products.json"
        write_json(
            self.products,
            {
                "types": [
                    {"id": "api", "item_ids": [API_ITEM], "delivery": "redeem"},
                    {
                        "id": "pan",
                        "item_ids": [PAN_ITEM],
                        "delivery": "pan",
                        "resource_match": ["resource"],
                    },
                    {
                        "id": "material",
                        "item_ids": [MATERIAL_ITEM],
                        "delivery": "material",
                        "payload": "material payload",
                    },
                ]
            },
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def make_agent(
        self,
        mode="rules",
        rules=None,
        bot=None,
        settings=None,
        seed_rules=True,
        seed_settings=True,
    ):
        rules_path = self.state / "reply_rules.json"
        if not seed_rules:
            rules_path.unlink(missing_ok=True)
        elif rules is not None:
            write_json(rules_path, rules)
        elif not rules_path.exists():
            write_json(rules_path, {"version": 1, "rules": []})
        settings_path = self.state / "automation_settings.json"
        if not seed_settings:
            settings_path.unlink(missing_ok=True)
        elif settings is not None:
            write_json(settings_path, settings)
        elif not settings_path.exists():
            write_json(
                settings_path,
                {"version": 1, "strategy": "standard", "enabled": True},
            )
        env = {
            "AUTOMATION_MODE": mode,
            "AUTOMATION_SETTINGS_FILE": str(settings_path),
        }
        return patch.dict(os.environ, env, clear=False), bot or FakeBot()

    def build_agent(
        self,
        mode="rules",
        rules=None,
        bot=None,
        settings=None,
        seed_rules=True,
        seed_settings=True,
    ):
        if mode == "rules_ai":
            write_json(self.state / "redeem_codes.json", [])
        env_patch, bot = self.make_agent(
            mode,
            rules,
            bot,
            settings,
            seed_rules=seed_rules,
            seed_settings=seed_settings,
        )
        env_patch.start()
        self.addAsyncCleanup(env_patch.stop)
        agent = XianyuLive(
            "unb=seller; token=test",
            reply_bot=bot,
            api_client=FakeApi(),
            data_dir=str(self.state),
            products_config_path=str(self.products),
            automation_mode=mode,
        )
        agent.send_text_reliably = AsyncMock()
        return agent, bot

    async def test_rules_mode_initializes_without_ai_client_or_paid_inventory(self):
        with patch.dict(os.environ, {"AUTOMATION_MODE": "rules"}, clear=True):
            with patch("main.XianyuReplyBot") as bot_constructor:
                agent = XianyuLive(
                    "unb=seller; token=test",
                    api_client=FakeApi(),
                    data_dir=str(self.state),
                    products_config_path=str(self.products),
                    automation_mode="rules",
                )
        bot_constructor.assert_not_called()
        self.assertIsNone(agent.bot)
        self.assertEqual(agent.automation_mode, "rules")

    def test_rules_ai_runtime_validation_keeps_fixed_rules_available_without_ai_token(self):
        with patch.dict(
            os.environ,
            {"AUTOMATION_MODE": "rules_ai", "COOKIES_STR": "unb=seller"},
            clear=True,
        ):
            self.assertEqual(validate_runtime_env(), "rules_ai")

    async def test_group_readable_material_config_is_rejected(self):
        write_json(
            self.products,
            {
                "types": [
                    {
                        "id": "material",
                        "item_ids": [MATERIAL_ITEM],
                        "delivery": "material",
                        "payload": "must stay out of the image",
                    }
                ]
            },
        )
        self.products.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "0600"):
            self.build_agent(mode="rules")

    async def test_base_mapping_without_payload_does_not_authorize_material(self):
        write_json(
            self.products,
            {
                "types": [
                    {
                        "id": "material",
                        "item_ids": [MATERIAL_ITEM],
                        "delivery": "material",
                    }
                ]
            },
        )
        self.products.chmod(0o644)
        agent, _bot = self.build_agent(mode="rules")
        self.assertIsNone(agent.classify_item(MATERIAL_ITEM))

    async def test_symlinked_material_config_is_rejected(self):
        target = self.state / "tenant-products.json"
        write_json(
            target,
            {
                "types": [
                    {
                        "id": "material",
                        "item_ids": [MATERIAL_ITEM],
                        "delivery": "material",
                        "payload": "material payload",
                    }
                ]
            },
        )
        linked = self.state / "linked-products.json"
        linked.symlink_to(target)
        with self.assertRaises(RuntimeError):
            XianyuLive(
                "unb=seller; token=test",
                api_client=FakeApi(),
                data_dir=str(self.state),
                products_config_path=str(linked),
                automation_mode="rules",
            )

    async def test_rules_mode_is_deterministic_and_idempotent(self):
        agent, bot = self.build_agent(
            rules={
                "version": 1,
                "rules": [
                    {
                        "id": "greeting",
                        "keywords": ["HELLO"],
                        "reply": "literal [TRIAL] text",
                    }
                ],
            }
        )
        await agent._process_buyer_chat(
            CHAT_ID, BUYER_ID, API_ITEM, "hello", "source-1"
        )
        await agent._process_buyer_chat(
            CHAT_ID, BUYER_ID, API_ITEM, "hello", "source-1"
        )
        agent.send_text_reliably.assert_awaited_once()
        self.assertEqual(agent.send_text_reliably.await_args.args[2], "literal [TRIAL] text")
        self.assertEqual(bot.calls, 0)

    async def test_item_scoped_rule_only_matches_the_associated_product(self):
        agent, _bot = self.build_agent(
            rules={
                "version": 1,
                "rules": [
                    {
                        "id": "generic-rule",
                        "name": "通用规则",
                        "item_id": "",
                        "keywords": ["价格"],
                        "reply": "通用回复",
                    },
                    {
                        "id": "item-rule",
                        "name": "指定商品规则",
                        "item_id": API_ITEM,
                        "keywords": ["价格"],
                        "reply": "指定商品回复",
                    },
                ],
            }
        )
        self.assertEqual(agent._match_reply_rule("价格多少", API_ITEM), "指定商品回复")
        self.assertEqual(agent._match_reply_rule("价格多少", PAN_ITEM), "通用回复")

    async def test_rules_mode_unmatched_message_is_terminal_without_llm(self):
        agent, bot = self.build_agent(
            rules={
                "version": 1,
                "rules": [{"id": "x", "keywords": ["x"], "reply": "y"}],
            }
        )
        await agent._process_buyer_chat(
            CHAT_ID, BUYER_ID, API_ITEM, "unmatched", "source-2"
        )
        agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(bot.calls, 0)
        outcome = agent.context_manager.get_source_message("assistant:source-2")
        self.assertEqual(outcome["role"], "assistant_no_reply")

    async def test_rules_mode_uses_gemini_first_and_fallback_replies(self):
        agent, bot = self.build_agent(
            rules={"version": 1, "rules": []},
            settings={
                "version": 1,
                "strategy": "standard",
                "enabled": True,
                "first_reply": "你好，在的，本店商品均支持付款后自动发货。",
                "fallback_reply": "这个问题我稍后人工为您解答。",
                "delay_min_seconds": 0,
                "delay_max_seconds": 0,
                "business_hours_enabled": False,
                "business_start": "09:00",
                "business_end": "23:30",
            },
        )
        await agent._process_buyer_chat(CHAT_ID, BUYER_ID, API_ITEM, "第一次咨询", "source-3")
        agent.send_text_reliably.assert_awaited_once()
        self.assertEqual(agent.send_text_reliably.await_args.args[2], "你好，在的，本店商品均支持付款后自动发货。")

        agent.send_text_reliably.reset_mock()
        await agent._process_buyer_chat(CHAT_ID, BUYER_ID, API_ITEM, "另一个没命中规则的问题", "source-4")
        agent.send_text_reliably.assert_awaited_once()
        self.assertEqual(agent.send_text_reliably.await_args.args[2], "这个问题我稍后人工为您解答。")

    def test_business_hours_validation(self):
        settings = {"business_hours_enabled": True, "business_start": "09:00", "business_end": "17:00"}
        self.assertTrue(within_business_hours(settings, datetime(2026, 8, 18, 10, 0)))
        self.assertFalse(within_business_hours(settings, datetime(2026, 8, 18, 18, 0)))
        self.assertTrue(
            within_business_hours(
                settings, datetime(2026, 8, 18, 1, 30, tzinfo=timezone.utc)
            )
        )
        self.assertFalse(
            within_business_hours(
                settings, datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc)
            )
        )
        overnight = {"business_hours_enabled": True, "business_start": "22:00", "business_end": "06:00"}
        self.assertTrue(within_business_hours(overnight, datetime(2026, 8, 18, 23, 0)))
        self.assertTrue(within_business_hours(overnight, datetime(2026, 8, 19, 5, 30)))
        self.assertFalse(within_business_hours(overnight, datetime(2026, 8, 19, 12, 0)))
        self.assertTrue(within_business_hours({"business_hours_enabled": False}, datetime(2026, 8, 18, 18, 0)))

    async def test_configured_delay_uses_base_plus_random_component(self):
        agent, _bot = self.build_agent(
            settings={
                "delay_min_seconds": 2,
                "delay_max_seconds": 3,
                "trigger_cooldown_seconds": 0,
                "manual_takeover_cooldown_seconds": 0,
            }
        )
        with patch("main.random.uniform", return_value=1.5):
            delay = await agent.human_reply_delay("买家消息", "自动回复", CHAT_ID)
        self.assertEqual(delay, 3.5)
        agent.automation_settings["delay_min_seconds"] = 60
        agent.automation_settings["delay_max_seconds"] = 60
        with patch("main.random.uniform", return_value=60):
            self.assertEqual(await agent.human_reply_delay("买家消息", "自动回复", CHAT_ID), 120.0)

    async def test_trigger_cooldown_suppresses_follow_up_auto_reply(self):
        agent, _bot = self.build_agent(
            rules={"version": 1, "rules": []},
            settings={
                "first_reply": "首次回复",
                "fallback_reply": "兜底回复",
                "trigger_cooldown_seconds": 30,
            },
        )
        await agent._process_buyer_chat(CHAT_ID, BUYER_ID, API_ITEM, "第一次咨询", "cooldown-1")
        agent.send_text_reliably.assert_awaited_once()

        agent.send_text_reliably.reset_mock()
        await agent._process_buyer_chat(CHAT_ID, BUYER_ID, API_ITEM, "紧接着追问", "cooldown-2")
        agent.send_text_reliably.assert_not_awaited()
        outcome = agent.context_manager.get_source_message("assistant:cooldown-2")
        self.assertEqual(outcome["role"], "assistant_no_reply")

    async def test_manual_exit_during_delay_is_rechecked_before_send(self):
        agent, _bot = self.build_agent(
            rules={"version": 1, "rules": []},
            settings={
                "first_reply": "首次回复",
                "manual_takeover_cooldown_seconds": 30,
            },
        )
        agent.human_reply_delay = AsyncMock(return_value=0.01)

        async def change_takeover_during_delay(_seconds):
            agent.enter_manual_mode(CHAT_ID)
            agent.exit_manual_mode(CHAT_ID)

        async def send_with_verification(_chat_id, _buyer_id, _reply, **kwargs):
            await kwargs["before_attempt"]()

        agent.send_text_reliably = AsyncMock(side_effect=send_with_verification)
        with patch("main.asyncio.sleep", side_effect=change_takeover_during_delay):
            await agent._process_buyer_chat(CHAT_ID, BUYER_ID, API_ITEM, "第一次咨询", "delay-manual-1")
        outcome = agent.context_manager.get_source_message("assistant:delay-manual-1")
        self.assertEqual(outcome["role"], "assistant_no_reply")

    async def test_trigger_cooldown_is_rechecked_before_send(self):
        agent, _bot = self.build_agent(
            rules={"version": 1, "rules": []},
            settings={
                "first_reply": "首次回复",
                "trigger_cooldown_seconds": 30,
            },
        )
        agent.human_reply_delay = AsyncMock(return_value=0.01)

        async def record_other_reply_during_delay(_seconds):
            agent.delivery_store.mark_automation_reply_sent(CHAT_ID)

        async def send_with_verification(_chat_id, _buyer_id, _reply, **kwargs):
            await kwargs["before_attempt"]()

        agent.send_text_reliably = AsyncMock(side_effect=send_with_verification)
        with patch("main.asyncio.sleep", side_effect=record_other_reply_during_delay):
            await agent._process_buyer_chat(CHAT_ID, BUYER_ID, API_ITEM, "第一次咨询", "delay-trigger-1")
        outcome = agent.context_manager.get_source_message("assistant:delay-trigger-1")
        self.assertEqual(outcome["role"], "assistant_no_reply")

    async def test_send_attempt_suppression_does_not_close_the_websocket(self):
        agent, _bot = self.build_agent(settings={"enabled": True})
        websocket = AsyncMock()
        agent.ws = websocket
        agent.connection_ready.set()
        agent.send_text_reliably = XianyuLive.send_text_reliably.__get__(agent, XianyuLive)

        async def suppress_before_attempt():
            from main import AutomationReplySuppressed
            raise AutomationReplySuppressed("trigger_cooldown")

        from main import AutomationReplySuppressed
        with self.assertRaises(AutomationReplySuppressed):
            await agent.send_text_reliably(
                CHAT_ID,
                BUYER_ID,
                "不会发送",
                message_key="suppressed-send",
                before_attempt=suppress_before_attempt,
            )
        websocket.close.assert_not_awaited()
        websocket.send.assert_not_awaited()

    async def test_trigger_cooldown_survives_worker_restart(self):
        settings = {
            "version": 1,
            "strategy": "standard",
            "enabled": True,
            "first_reply": "首次回复",
            "fallback_reply": "兜底回复",
            "trigger_cooldown_seconds": 30,
        }
        first_agent, _bot = self.build_agent(
            rules={"version": 1, "rules": []},
            settings=settings,
        )
        await first_agent._process_buyer_chat(CHAT_ID, BUYER_ID, API_ITEM, "第一次咨询", "restart-cooldown-1")
        first_agent.send_text_reliably.assert_awaited_once()

        second_agent, _bot = self.build_agent(
            rules={"version": 1, "rules": []},
            settings=settings,
        )
        await second_agent._process_buyer_chat(CHAT_ID, BUYER_ID, API_ITEM, "重启后紧接着追问", "restart-cooldown-2")
        second_agent.send_text_reliably.assert_not_awaited()
        outcome = second_agent.context_manager.get_source_message("assistant:restart-cooldown-2")
        self.assertEqual(outcome["role"], "assistant_no_reply")

    async def test_manual_takeover_cooldown_suppresses_auto_reply_after_exit(self):
        agent, _bot = self.build_agent(
            rules={"version": 1, "rules": []},
            settings={
                "first_reply": "首次回复",
                "fallback_reply": "兜底回复",
                "manual_takeover_cooldown_seconds": 30,
            },
        )
        agent.enter_manual_mode(CHAT_ID)
        agent.exit_manual_mode(CHAT_ID)
        await agent._process_buyer_chat(CHAT_ID, BUYER_ID, API_ITEM, "刚刚结束人工接管", "manual-cooldown-1")
        agent.send_text_reliably.assert_not_awaited()
        outcome = agent.context_manager.get_source_message("assistant:manual-cooldown-1")
        self.assertEqual(outcome["role"], "assistant_no_reply")

    async def test_external_manual_exit_event_starts_cooldown(self):
        agent, _bot = self.build_agent(
            rules={"version": 1, "rules": []},
            settings={
                "first_reply": "首次回复",
                "fallback_reply": "兜底回复",
                "manual_takeover_cooldown_seconds": 30,
            },
        )
        self.assertEqual(agent.delivery_store.toggle_manual_mode_once("enter", CHAT_ID, 3600)[0], "manual")
        self.assertEqual(agent.delivery_store.toggle_manual_mode_once("exit", CHAT_ID, 3600)[0], "auto")
        await agent._process_buyer_chat(CHAT_ID, BUYER_ID, API_ITEM, "API刚恢复自动模式", "manual-cooldown-2")
        agent.send_text_reliably.assert_not_awaited()
        outcome = agent.context_manager.get_source_message("assistant:manual-cooldown-2")
        self.assertEqual(outcome["role"], "assistant_no_reply")

    async def test_expired_manual_mode_starts_cooldown_at_expiry(self):
        agent, _bot = self.build_agent(
            rules={"version": 1, "rules": []},
            settings={
                "first_reply": "首次回复",
                "fallback_reply": "兜底回复",
                "manual_takeover_cooldown_seconds": 30,
            },
        )
        clock = [1000.0]
        agent.delivery_store.now_fn = lambda: clock[0]
        agent.delivery_store.set_manual_mode(CHAT_ID, True, 10)
        clock[0] = 1011.0
        await agent._process_buyer_chat(CHAT_ID, BUYER_ID, API_ITEM, "接管刚自动过期", "manual-cooldown-3")
        agent.send_text_reliably.assert_not_awaited()
        outcome = agent.context_manager.get_source_message("assistant:manual-cooldown-3")
        self.assertEqual(outcome["role"], "assistant_no_reply")

    async def test_missing_reply_rules_at_startup_fail_closed_and_hot_recovers(self):
        agent, _bot = self.build_agent(
            settings={"version": 1, "strategy": "standard", "enabled": True},
            seed_rules=False,
        )
        self.assertFalse(agent.reply_rules_available)
        self.assertEqual(
            agent.automatic_reply_suppression_reason(CHAT_ID), "reply_rules_invalid"
        )

        write_json(
            self.state / "reply_rules.json",
            {
                "version": 1,
                "rules": [{"id": "restored", "keywords": ["恢复"], "reply": "已恢复"}],
            },
        )
        self.assertEqual(agent._match_reply_rule("配置恢复"), "已恢复")
        self.assertTrue(agent.reply_rules_available)
        self.assertIsNone(agent.automatic_reply_suppression_reason(CHAT_ID))

    async def test_missing_automation_settings_at_startup_fail_closed_and_hot_recovers(self):
        agent, _bot = self.build_agent(
            rules={
                "version": 1,
                "rules": [{"id": "ready", "keywords": ["测试"], "reply": "规则回复"}],
            },
            seed_settings=False,
        )
        self.assertTrue(agent.reply_rules_available)
        self.assertFalse(agent.automation_settings["enabled"])
        self.assertEqual(
            agent.automatic_reply_suppression_reason(CHAT_ID), "automation_disabled"
        )

        write_json(
            self.state / "automation_settings.json",
            {"version": 1, "strategy": "standard", "enabled": True},
        )
        self.assertIsNone(agent.automatic_reply_suppression_reason(CHAT_ID))
        self.assertTrue(agent.automation_settings["enabled"])
        self.assertEqual(agent._match_reply_rule("测试"), "规则回复")

    async def test_invalid_automation_settings_fail_closed(self):
        path = self.state / "automation_settings.json"
        invalid_documents = [
            {"version": 1, "strategy": "standard", "enabled": "yes"},
            {"version": 1, "strategy": "standard", "enabled": True, "delay_max_seconds": 61},
            {"version": 1, "strategy": "standard", "enabled": True, "manual_takeover_cooldown_seconds": 301},
            {"version": 1, "strategy": "standard", "enabled": True, "business_start": "25:00"},
            {"version": 1, "strategy": "standard", "enabled": True, "unknown": "field"},
        ]
        for payload in invalid_documents:
            with self.subTest(payload=payload):
                write_json(path, payload)
                with self.assertRaises(RuntimeError):
                    load_automation_settings(str(path))

        write_json(path, invalid_documents[0])
        agent, _bot = self.build_agent(rules={"version": 1, "rules": []})
        self.assertFalse(agent.automation_settings["enabled"])

    async def test_hot_reload_zero_delay_disables_startup_typing_fallback(self):
        settings_path = self.state / "automation_settings.json"
        agent, _bot = self.build_agent(
            settings={
                "version": 1,
                "strategy": "standard",
                "enabled": True,
                "delay_min_seconds": 2,
                "delay_max_seconds": 3,
            }
        )
        agent.simulate_human_typing = True
        write_json(settings_path, {
            "version": 1,
            "strategy": "standard",
            "enabled": True,
            "delay_min_seconds": 0,
            "delay_max_seconds": 0,
        })
        agent._refresh_runtime_config()
        self.assertEqual(await agent.human_reply_delay("买家消息", "自动回复", CHAT_ID), 0.0)

    async def test_reply_rules_hot_reload_and_invalid_update_fail_closed(self):
        initial = {
            "version": 1,
            "rules": [{"id": "x", "keywords": ["old"], "reply": "old reply"}],
        }
        agent, _bot = self.build_agent(rules=initial)
        self.assertEqual(agent._match_reply_rule("old"), "old reply")

        replacement = {
            "version": 1,
            "rules": [{"id": "x", "keywords": ["new"], "reply": "new reply"}],
        }
        write_json(self.state / "reply_rules.json", replacement)
        self.assertEqual(agent._match_reply_rule("new"), "new reply")
        self.assertIsNone(agent._match_reply_rule("old"))

        write_json(self.state / "reply_rules.json", {"version": "bad", "rules": []})
        self.assertIsNone(agent._match_reply_rule("new"))
        self.assertFalse(agent.reply_rules_available)
        self.assertEqual(
            agent.automatic_reply_suppression_reason(CHAT_ID), "reply_rules_invalid"
        )

        write_json(self.state / "reply_rules.json", replacement)
        self.assertEqual(agent._match_reply_rule("new"), "new reply")
        self.assertTrue(agent.reply_rules_available)

    async def test_corrupt_reply_rules_block_ai_until_signature_is_repaired(self):
        agent, bot = self.build_agent(
            mode="rules_ai",
            bot=FakeBot("ai reply"),
            rules={"version": 1, "rules": []},
        )
        write_json(self.state / "reply_rules.json", {"version": "bad", "rules": []})
        await agent._process_buyer_chat(
            CHAT_ID, BUYER_ID, API_ITEM, "真实问题", "rules-corrupt"
        )
        self.assertEqual(bot.calls, 0)
        agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(
            agent.context_manager.get_source_message("assistant:rules-corrupt")["role"],
            "assistant_no_reply",
        )

        write_json(self.state / "reply_rules.json", {"version": 1, "rules": []})
        await agent._process_buyer_chat(
            CHAT_ID, BUYER_ID, API_ITEM, "修复后的问题", "rules-repaired"
        )
        self.assertEqual(bot.calls, 1)
        agent.send_text_reliably.assert_awaited_once()

    async def test_rules_ai_keeps_rule_priority_and_first_question_calls_ai(self):
        agent, bot = self.build_agent(
            mode="rules_ai",
            bot=FakeBot("ai reply"),
            rules={
                "version": 1,
                "rules": [
                    {"id": "rule", "keywords": ["命中"], "reply": "规则回复"}
                ],
            },
            settings={
                "first_reply": "首次回复",
                "fallback_reply": "兜底回复",
            },
        )
        async def send_with_verification(_chat_id, _buyer_id, _reply, **kwargs):
            await kwargs["before_attempt"]()

        agent.send_text_reliably = AsyncMock(side_effect=send_with_verification)
        await agent._process_buyer_chat(
            CHAT_ID, BUYER_ID, API_ITEM, "命中规则", "rules-ai-rule"
        )
        self.assertEqual(bot.ready_calls, 0)
        await agent._process_buyer_chat(
            "4005", BUYER_ID, API_ITEM, "首次咨询", "rules-ai-first"
        )
        self.assertEqual(
            [call.args[2] for call in agent.send_text_reliably.await_args_list],
            ["规则回复", "ai reply"],
        )
        self.assertEqual(bot.calls, 1)
        self.assertEqual(bot.ready_calls, 1)
        self.assertEqual(bot.ready_expected_revisions, [7])
        self.assertEqual(bot.requests[0]["item_id"], API_ITEM)
        self.assertEqual(bot.requests[0]["recent_assistant_replies"], [])

    async def test_rules_ai_disable_during_delay_cancels_generated_reply(self):
        agent, bot = self.build_agent(
            mode="rules_ai",
            bot=FakeBot("迟到的 AI 回复"),
            rules={"version": 1, "rules": []},
            settings={"enabled": True, "fallback_reply": "不得发送的兜底"},
        )
        agent.human_reply_delay = AsyncMock(return_value=0.01)

        async def disable_during_delay(_seconds):
            write_json(
                self.state / "automation_settings.json",
                {"version": 1, "strategy": "standard", "enabled": False},
            )

        with patch("main.asyncio.sleep", side_effect=disable_during_delay):
            await agent._process_buyer_chat(
                CHAT_ID, BUYER_ID, API_ITEM, "真实问题", "ai-disabled-during-delay"
            )

        self.assertEqual(bot.calls, 1)
        self.assertEqual(bot.ready_calls, 0)
        agent.send_text_reliably.assert_not_awaited()
        outcome = agent.context_manager.get_source_message(
            "assistant:ai-disabled-during-delay"
        )
        self.assertEqual(outcome["role"], "assistant_no_reply")

    async def test_rules_ai_rule_corruption_during_delay_cancels_generated_reply(self):
        agent, bot = self.build_agent(
            mode="rules_ai",
            bot=FakeBot("迟到的 AI 回复"),
            rules={"version": 1, "rules": []},
            settings={"enabled": True, "fallback_reply": "不得发送的兜底"},
        )
        agent.human_reply_delay = AsyncMock(return_value=0.01)

        async def corrupt_rules_during_delay(_seconds):
            write_json(
                self.state / "reply_rules.json",
                {"version": "invalid", "rules": []},
            )

        with patch("main.asyncio.sleep", side_effect=corrupt_rules_during_delay):
            await agent._process_buyer_chat(
                CHAT_ID, BUYER_ID, API_ITEM, "真实问题", "rules-corrupt-during-delay"
            )

        self.assertEqual(bot.calls, 1)
        self.assertEqual(bot.ready_calls, 0)
        agent.send_text_reliably.assert_not_awaited()
        outcome = agent.context_manager.get_source_message(
            "assistant:rules-corrupt-during-delay"
        )
        self.assertEqual(outcome["role"], "assistant_no_reply")

    async def test_rules_ai_valid_rule_change_during_delay_cancels_generated_reply(self):
        agent, bot = self.build_agent(
            mode="rules_ai",
            bot=FakeBot("旧规则版本下生成的 AI 回复"),
            rules={"version": 1, "rules": []},
        )
        agent.human_reply_delay = AsyncMock(return_value=0.01)

        async def add_matching_rule_during_delay(_seconds):
            write_json(
                self.state / "reply_rules.json",
                {
                    "version": 1,
                    "rules": [
                        {
                            "id": "new-priority-rule",
                            "keywords": ["真实问题"],
                            "reply": "新规则回复",
                        }
                    ],
                },
            )

        with patch("main.asyncio.sleep", side_effect=add_matching_rule_during_delay):
            await agent._process_buyer_chat(
                CHAT_ID,
                BUYER_ID,
                API_ITEM,
                "真实问题",
                "rules-valid-change-during-delay",
            )

        self.assertEqual(bot.calls, 1)
        self.assertEqual(bot.ready_calls, 0)
        agent.send_text_reliably.assert_not_awaited()
        outcome = agent.context_manager.get_source_message(
            "assistant:rules-valid-change-during-delay"
        )
        self.assertEqual(outcome["role"], "assistant_no_reply")

    async def test_rules_ai_valid_config_revision_change_cancels_old_generated_reply(self):
        agent, bot = self.build_agent(
            mode="rules_ai",
            bot=FakeBot("版本 7 生成的回复", config_revision=7),
            rules={"version": 1, "rules": []},
        )
        agent.human_reply_delay = AsyncMock(return_value=0.01)

        async def publish_new_valid_config(_seconds):
            bot.ready_revision = 8

        async def send_with_verification(_chat_id, _buyer_id, _reply, **kwargs):
            await kwargs["before_attempt"]()

        agent.send_text_reliably = AsyncMock(side_effect=send_with_verification)
        with patch("main.asyncio.sleep", side_effect=publish_new_valid_config):
            await agent._process_buyer_chat(
                CHAT_ID,
                BUYER_ID,
                API_ITEM,
                "真实问题",
                "ai-valid-revision-changed",
            )

        self.assertEqual(bot.calls, 1)
        self.assertEqual(bot.ready_expected_revisions, [7])
        outcome = agent.context_manager.get_source_message(
            "assistant:ai-valid-revision-changed"
        )
        self.assertEqual(outcome["role"], "assistant_cancelled")
        self.assertIsNone(
            agent._assistant_draft_provenance(
                "assistant:ai-valid-revision-changed"
            )
        )

    async def test_ai_draft_replay_after_worker_restart_keeps_original_revision(self):
        first_bot = FakeBot("稳定版本 7 草稿", config_revision=7)
        first_agent, _ = self.build_agent(
            mode="rules_ai",
            bot=first_bot,
            rules={"version": 1, "rules": []},
        )
        first_agent.send_text_reliably = AsyncMock(
            side_effect=ConnectionError("offline")
        )
        with self.assertRaises(ConnectionError):
            await first_agent._process_buyer_chat(
                CHAT_ID,
                BUYER_ID,
                API_ITEM,
                "需要稳定回复的问题",
                "revision-restart",
            )
        first_provenance = first_agent._assistant_draft_provenance(
            "assistant:revision-restart"
        )
        self.assertEqual(first_provenance["origin"], "ai")
        self.assertEqual(first_provenance["config_revision"], 7)
        self.assertEqual(len(first_provenance["automation_revision"]), 64)

        second_bot = FakeBot("不得重新生成", config_revision=8)
        second_bot.ready_revision = 7
        second_agent, _ = self.build_agent(
            mode="rules_ai",
            bot=second_bot,
            rules={"version": 1, "rules": []},
        )

        async def send_with_verification(_chat_id, _buyer_id, _reply, **kwargs):
            await kwargs["before_attempt"]()

        second_agent.send_text_reliably = AsyncMock(
            side_effect=send_with_verification
        )
        await second_agent._process_buyer_chat(
            CHAT_ID,
            BUYER_ID,
            API_ITEM,
            "需要稳定回复的问题",
            "revision-restart",
        )

        self.assertEqual(second_bot.calls, 0)
        self.assertEqual(second_bot.ready_expected_revisions, [7])
        self.assertEqual(
            second_agent.send_text_reliably.await_args.args[2],
            "稳定版本 7 草稿",
        )
        self.assertIsNone(
            second_agent._assistant_draft_provenance(
                "assistant:revision-restart"
            )
        )

    async def test_rule_draft_replay_after_valid_rule_change_is_cancelled(self):
        first_agent, _ = self.build_agent(
            mode="rules",
            rules={
                "version": 1,
                "rules": [
                    {"id": "old-rule", "keywords": ["问题"], "reply": "旧规则回复"}
                ],
            },
        )
        first_agent.send_text_reliably = AsyncMock(
            side_effect=ConnectionError("offline")
        )
        with self.assertRaises(ConnectionError):
            await first_agent._process_buyer_chat(
                CHAT_ID,
                BUYER_ID,
                API_ITEM,
                "问题",
                "rule-revision-restart",
            )
        first_provenance = first_agent._assistant_draft_provenance(
            "assistant:rule-revision-restart"
        )
        self.assertEqual(first_provenance["origin"], "rule")
        self.assertIsNone(first_provenance["config_revision"])
        self.assertEqual(len(first_provenance["automation_revision"]), 64)

        write_json(
            self.state / "reply_rules.json",
            {
                "version": 1,
                "rules": [
                    {"id": "new-rule", "keywords": ["问题"], "reply": "新规则回复"}
                ],
            },
        )
        second_agent, _ = self.build_agent(mode="rules")
        await second_agent._process_buyer_chat(
            CHAT_ID,
            BUYER_ID,
            API_ITEM,
            "问题",
            "rule-revision-restart",
        )

        second_agent.send_text_reliably.assert_not_awaited()
        outcome = second_agent.context_manager.get_source_message(
            "assistant:rule-revision-restart"
        )
        self.assertEqual(outcome["role"], "assistant_cancelled")
        self.assertIsNone(
            second_agent._assistant_draft_provenance(
                "assistant:rule-revision-restart"
            )
        )

    async def test_concurrent_ai_replies_keep_request_local_config_revisions(self):
        class PerRequestRevisionBot(FakeBot):
            def generate_reply_result(self, user_msg, *_args, **kwargs):
                self.calls += 1
                self.requests.append(kwargs)
                revision = 7 if user_msg == "版本七问题" else 8
                return {
                    "reply": f"版本 {revision} 回复",
                    "decision": "reply",
                    "reason_code": "ok",
                    "config_revision": revision,
                }

            def ensure_ready(self, expected_config_revision):
                self.ready_calls += 1
                self.ready_expected_revisions.append(expected_config_revision)
                return expected_config_revision

        bot = PerRequestRevisionBot()
        agent, _ = self.build_agent(
            mode="rules_ai",
            bot=bot,
            rules={"version": 1, "rules": []},
        )

        async def send_with_verification(_chat_id, _buyer_id, _reply, **kwargs):
            await kwargs["before_attempt"]()

        agent.send_text_reliably = AsyncMock(side_effect=send_with_verification)
        await asyncio.gather(
            agent._process_buyer_chat(
                "revision-chat-7",
                BUYER_ID,
                API_ITEM,
                "版本七问题",
                "revision-source-7",
            ),
            agent._process_buyer_chat(
                "revision-chat-8",
                BUYER_ID,
                API_ITEM,
                "版本八问题",
                "revision-source-8",
            ),
        )

        self.assertCountEqual(bot.ready_expected_revisions, [7, 8])
        self.assertEqual(bot.ready_calls, 2)
        self.assertEqual(agent.send_text_reliably.await_count, 2)

    async def test_rules_ai_readiness_failures_cancel_without_closing_websocket(self):
        for index, error in enumerate(
            (
                LLMNotReadyError("disabled"),
                LLMServiceError("unavailable"),
                LLMTimeoutError("timeout"),
            )
        ):
            with self.subTest(error=type(error).__name__):
                bot = FakeBot("不得发送的 AI 回复", ready_error=error)
                agent, _ = self.build_agent(
                    mode="rules_ai",
                    bot=bot,
                    rules={"version": 1, "rules": []},
                )
                websocket = AsyncMock()
                agent.ws = websocket
                agent.connection_ready.set()
                agent.send_text_reliably = XianyuLive.send_text_reliably.__get__(
                    agent, XianyuLive
                )
                source_id = f"ready-failure-{index}"

                await agent._process_buyer_chat(
                    f"ready-chat-{index}", BUYER_ID, API_ITEM, "真实问题", source_id
                )

                self.assertEqual(bot.ready_calls, 1)
                websocket.send.assert_not_awaited()
                websocket.close.assert_not_awaited()
                outcome = agent.context_manager.get_source_message(
                    f"assistant:{source_id}"
                )
                self.assertEqual(outcome["role"], "assistant_cancelled")

    async def test_rules_ai_readiness_runs_before_every_reconnect_send_attempt(self):
        agent, bot = self.build_agent(
            mode="rules_ai",
            bot=FakeBot("稳定 AI 草稿"),
            rules={"version": 1, "rules": []},
        )
        websocket = AsyncMock()
        agent.ws = websocket
        agent.connection_ready.set()
        websocket.close.side_effect = lambda: agent.connection_ready.set()
        agent.send_msg = AsyncMock(side_effect=[ConnectionError("offline"), None])
        agent.send_text_reliably = XianyuLive.send_text_reliably.__get__(agent, XianyuLive)

        await agent._process_buyer_chat(
            CHAT_ID, BUYER_ID, API_ITEM, "真实问题", "ready-reconnect"
        )

        self.assertEqual(bot.calls, 1)
        self.assertEqual(bot.ready_calls, 2)
        self.assertEqual(bot.ready_expected_revisions, [7, 7])
        self.assertEqual(agent.send_msg.await_count, 2)
        first_uuid = agent.send_msg.await_args_list[0].kwargs["message_uuid"]
        second_uuid = agent.send_msg.await_args_list[1].kwargs["message_uuid"]
        self.assertEqual(first_uuid, second_uuid)
        self.assertEqual(
            agent.context_manager.get_source_message("assistant:ready-reconnect")["role"],
            "assistant",
        )

    async def test_rules_ai_failures_and_empty_reply_never_send_fixed_fallback(self):
        agent, _bot = self.build_agent(
            mode="rules_ai",
            rules={"version": 1, "rules": []},
            settings={"fallback_reply": "兜底回复"},
        )
        agent._generate_llm_reply = AsyncMock(
            side_effect=[
                asyncio.TimeoutError(),
                RuntimeError("temporary failure"),
                ("   ", "reply", "ok", 7),
            ]
        )

        for index in range(3):
            await agent._process_buyer_chat(
                CHAT_ID,
                BUYER_ID,
                API_ITEM,
                f"未命中问题 {index}",
                f"rules-ai-fallback-{index}",
            )

        self.assertEqual(agent._generate_llm_reply.await_count, 3)
        agent.send_text_reliably.assert_not_awaited()
        for index in range(3):
            outcome = agent.context_manager.get_source_message(
                f"assistant:rules-ai-fallback-{index}"
            )
            self.assertEqual(outcome["role"], "assistant_no_reply")

    async def test_rules_ai_budget_exhaustion_is_terminal_no_reply(self):
        agent, _bot = self.build_agent(
            mode="rules_ai",
            rules={"version": 1, "rules": []},
            settings={"fallback_reply": "预算兜底回复"},
        )
        agent._generate_llm_reply = AsyncMock(return_value=("不应调用", "reply", "ok", 7))

        with patch.object(agent.delivery_store, "reserve_llm_budget", return_value=False):
            await agent._process_buyer_chat(
                CHAT_ID,
                BUYER_ID,
                API_ITEM,
                "预算已用完时的问题",
                "rules-ai-budget-fallback",
            )

        agent._generate_llm_reply.assert_not_awaited()
        agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(
            agent.context_manager.get_source_message(
                "assistant:rules-ai-budget-fallback"
            )["role"],
            "assistant_no_reply",
        )

    async def test_rules_ai_uses_existing_internal_reply_client(self):
        agent, bot = self.build_agent(mode="rules_ai", bot=FakeBot("ai reply"))
        await agent._process_buyer_chat(
            CHAT_ID, BUYER_ID, API_ITEM, "unmatched", "source-3"
        )
        agent.send_text_reliably.assert_awaited_once()
        self.assertEqual(agent.send_text_reliably.await_args.args[2], "ai reply")
        self.assertEqual(bot.calls, 1)

    async def test_recent_duplicate_ai_reply_retries_once_then_suppresses(self):
        agent, _bot = self.build_agent(
            mode="rules_ai",
            rules={"version": 1, "rules": []},
        )
        agent.context_manager.add_message_by_chat(
            CHAT_ID,
            agent.myid,
            API_ITEM,
            "assistant",
            "这是一条已经发送的相同回复",
            source_id="prior-assistant",
        )
        agent._generate_llm_reply = AsyncMock(
            side_effect=[
                ("这是一条已经发送的相同回复", "reply", "ok", 7),
                ("这是一条已经发送的相同回复", "reply", "ok", 7),
            ]
        )

        await agent._process_buyer_chat(
            CHAT_ID, BUYER_ID, API_ITEM, "另一个真实问题", "duplicate-ai"
        )

        self.assertEqual(agent._generate_llm_reply.await_count, 2)
        agent.send_text_reliably.assert_not_awaited()
        second_recent = agent._generate_llm_reply.await_args_list[1].args[4]
        self.assertIn("这是一条已经发送的相同回复", second_recent)
        self.assertEqual(
            agent.context_manager.get_source_message("assistant:duplicate-ai")["role"],
            "assistant_no_reply",
        )

    async def test_duplicate_retry_reserves_a_second_model_call_budget(self):
        agent, _bot = self.build_agent(
            mode="rules_ai",
            rules={"version": 1, "rules": []},
        )
        agent.context_manager.add_message_by_chat(
            CHAT_ID,
            agent.myid,
            API_ITEM,
            "assistant",
            "这是一条已经发送的相同回复",
            source_id="prior-assistant-budget",
        )
        agent._generate_llm_reply = AsyncMock(
            return_value=("这是一条已经发送的相同回复", "reply", "ok", 7)
        )

        with patch.object(
            agent.delivery_store,
            "reserve_llm_budget",
            side_effect=[True, False],
        ) as reserve_budget:
            await agent._process_buyer_chat(
                CHAT_ID,
                BUYER_ID,
                API_ITEM,
                "另一个需要去重的问题",
                "duplicate-ai-budget",
            )

        self.assertEqual(reserve_budget.call_count, 2)
        agent._generate_llm_reply.assert_awaited_once()
        agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(
            agent.context_manager.get_source_message(
                "assistant:duplicate-ai-budget"
            )["role"],
            "assistant_no_reply",
        )

    async def test_inbound_recovery_loop_continues_after_one_sqlite_error(self):
        agent, _bot = self.build_agent()
        agent.last_delivery_retry_at = time.time()
        agent.last_manual_review_alert = time.time()
        schedule_calls = 0

        def schedule_pending():
            nonlocal schedule_calls
            schedule_calls += 1
            if schedule_calls == 1:
                raise sqlite3.OperationalError("database is locked")
            raise asyncio.CancelledError()

        agent._schedule_pending_inbound_events = schedule_pending
        with patch("main.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await agent._inbound_recovery_loop()

        self.assertEqual(schedule_calls, 2)
        sleep.assert_awaited_once_with(agent.inbound_retry_interval)

    async def test_rules_mode_filters_paid_delivery_types(self):
        agent, _bot = self.build_agent(mode="rules")
        self.assertIsNone(agent.classify_item(API_ITEM))
        self.assertIsNone(agent.classify_item(PAN_ITEM))
        self.assertEqual(agent.classify_item(MATERIAL_ITEM)["delivery"], "material")

    async def test_rules_ai_keeps_paid_delivery_types(self):
        agent, _bot = self.build_agent(mode="rules_ai")
        self.assertEqual(agent.classify_item(API_ITEM)["delivery"], "redeem")
        self.assertEqual(agent.classify_item(PAN_ITEM)["delivery"], "pan")
        self.assertEqual(agent.classify_item(MATERIAL_ITEM)["delivery"], "material")

    async def test_disabled_material_entry_does_not_authorize_delivery(self):
        write_json(
            self.products,
            {
                "types": [
                    {
                        "id": "material",
                        "item_ids": [MATERIAL_ITEM],
                        "delivery": "material",
                        "enabled": False,
                        "payload": "material payload",
                    }
                ]
            },
        )
        agent, _bot = self.build_agent(mode="rules")
        self.assertIsNone(agent.classify_item(MATERIAL_ITEM))

    async def test_verified_material_order_is_sent_once_and_persisted(self):
        agent, _bot = self.build_agent(mode="rules")
        agent.delivery_store.record_chat_binding(CHAT_ID, BUYER_ID, MATERIAL_ITEM)
        event = {
            "1": f"{CHAT_ID}@goofish",
            "2": 1,
            "3": {"redReminder": "等待卖家发货", "redReminderStyle": "1"},
            "4": int(time.time() * 1000),
        }
        self.assertTrue(await agent.handle_paid_order(event))
        self.assertTrue(await agent.handle_paid_order(event))
        agent.send_text_reliably.assert_awaited_once()
        self.assertEqual(agent.send_text_reliably.await_args.args[2], "material payload")
        order = agent.delivery_store.get_order(agent._canonical_order_key(ORDER_ID))
        self.assertEqual(order.status, "delivered")
        self.assertEqual(order.payload, "material payload")

    async def test_material_multi_unit_order_requires_manual_review(self):
        agent, _bot = self.build_agent(mode="rules")
        agent.xianyu.order_quantity = 2
        agent.delivery_store.record_chat_binding(CHAT_ID, BUYER_ID, MATERIAL_ITEM)
        event = {
            "1": f"{CHAT_ID}@goofish",
            "2": 1,
            "3": {"redReminder": "等待卖家发货", "redReminderStyle": "1"},
            "4": int(time.time() * 1000),
        }
        self.assertTrue(await agent.handle_paid_order(event))
        agent.send_text_reliably.assert_not_awaited()
        order = agent.delivery_store.get_order(agent._canonical_order_key(ORDER_ID))
        self.assertEqual(order.status, "manual_review")
        self.assertEqual(order.reason, "unsupported_quantity")


if __name__ == "__main__":
    unittest.main()
