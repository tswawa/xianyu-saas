import os
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from XianyuAgent import (
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMNotReadyError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMServiceError,
    LLMTimeoutError,
    XianyuReplyBot,
)
from XianyuApis import XianyuApiError, XianyuApis, XianyuAuthenticationError
from ai_runtime import AIRuntimeFormatError, AIRuntimeMissingError, load_published_context
from platform_profile import CHROME_MAJOR, ORIGIN, PLATFORM, REFERER, USER_AGENT


def completion_response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def completion_response_with_reasoning(content, reasoning_content="internal"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content, reasoning_content=reasoning_content
                )
            )
        ]
    )


def json_response(payload, headers=None, status_code=200):
    response = MagicMock()
    response.json.return_value = payload
    response.headers = headers or {}
    response.status_code = status_code
    return response


class LLMResilienceTests(unittest.TestCase):
    def make_bot(self, response_payload=None, status_code=200):
        session = MagicMock()
        response = MagicMock()
        response.status_code = status_code
        response.content = b"{}"
        payload = dict(response_payload) if response_payload is not None else {
            "decision": "reply",
            "reply": "usable reply",
            "reason_code": "ok",
            "sources": ["store_content", "real_time_product_facts"],
            "knowledge_status": "active",
        }
        payload.setdefault("config_revision", 7)
        response.json.return_value = payload
        session.post.return_value = response
        env = {
            "API_KEY": "short-lived-token",
            "MODEL_BASE_URL": "http://127.0.0.1:8096/internal/v1",
            "XIAN_YU_ACCOUNT_KEY": "secondary",
        }
        with patch.dict(os.environ, env, clear=False):
            bot = XianyuReplyBot(session=session)
        return bot, session

    def test_client_posts_exact_internal_business_contract(self):
        bot, session = self.make_bot()
        reply = bot.generate_reply(
            "现在多少钱",
            "legacy description",
            [
                {"role": "user", "content": "前一个问题"},
                {"role": "system", "content": "不得转发"},
            ],
            "legacy system context must be ignored",
            item_id="1001",
            item_context={"item_id": "1001", "price": 5},
            recent_assistant_replies=["上一条回复"],
        )
        self.assertEqual(reply, "usable reply")
        call = session.post.call_args
        self.assertEqual(call.args[0], "http://127.0.0.1:8096/internal/v1/ai/reply")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer short-lived-token")
        self.assertEqual(call.kwargs["headers"]["X-Shop-Account"], "secondary")
        self.assertEqual(call.kwargs["timeout"], (5.0, 45.0))
        self.assertEqual(
            set(call.kwargs["json"]),
            {"message", "history", "item_id", "item_context", "recent_assistant_replies"},
        )
        self.assertEqual(call.kwargs["json"]["history"], [{"role": "user", "content": "前一个问题"}])
        self.assertNotIn("legacy system context", json.dumps(call.kwargs["json"], ensure_ascii=False))

    def test_readiness_posts_to_safely_derived_account_scoped_endpoint(self):
        bot, session = self.make_bot()
        response = MagicMock()
        response.status_code = 200
        response.content = b"{}"
        response.json.return_value = {"ok": True, "config_revision": 7}
        session.post.return_value = response

        self.assertEqual(bot.ensure_ready(7), 7)

        call = session.post.call_args
        self.assertEqual(call.args[0], "http://127.0.0.1:8096/internal/v1/ai/ready")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer short-lived-token")
        self.assertEqual(call.kwargs["headers"]["X-Shop-Account"], "secondary")
        self.assertEqual(call.kwargs["json"], {"expected_config_revision": 7})
        self.assertEqual(call.kwargs["timeout"], (2.0, 5.0))

    def test_readiness_derives_from_an_existing_reply_url(self):
        env = {
            "API_KEY": "short-lived-token",
            "MODEL_BASE_URL": "http://localhost:8096/internal/v1/ai/reply",
            "XIAN_YU_ACCOUNT_KEY": "secondary",
        }
        with patch.dict(os.environ, env, clear=False):
            bot = XianyuReplyBot(session=MagicMock())
        self.assertEqual(
            bot.ready_endpoint,
            "http://localhost:8096/internal/v1/ai/ready",
        )

    def test_readiness_failures_are_typed_without_response_body_leakage(self):
        cases = (
            (409, LLMNotReadyError),
            (500, LLMServiceError),
            (504, LLMTimeoutError),
        )
        for status, error in cases:
            with self.subTest(status=status):
                bot, session = self.make_bot()
                response = MagicMock()
                response.status_code = status
                response.content = b'{"detail":"sensitive upstream body"}'
                response.json.return_value = {"detail": "sensitive upstream body"}
                session.post.return_value = response
                with self.assertRaises(error) as raised:
                    bot.ensure_ready(7)
                self.assertNotIn("sensitive upstream body", str(raised.exception))

        bot, session = self.make_bot()
        session.post.side_effect = requests.Timeout("sensitive timeout detail")
        with self.assertRaises(LLMTimeoutError) as raised:
            bot.ensure_ready(7)
        self.assertNotIn("sensitive timeout detail", str(raised.exception))

    def test_readiness_rejects_invalid_success_protocol(self):
        for payload in (
            {"ok": False, "config_revision": 7},
            {"ok": True, "config_revision": True},
            {"ok": True, "config_revision": 7, "extra": "field"},
        ):
            with self.subTest(payload=payload):
                bot, session = self.make_bot()
                response = MagicMock()
                response.status_code = 200
                response.content = b"{}"
                response.json.return_value = payload
                session.post.return_value = response
                with self.assertRaises(LLMResponseFormatError):
                    bot.ensure_ready(7)

    def test_readiness_rejects_changed_or_invalid_expected_revision(self):
        bot, session = self.make_bot()
        response = MagicMock()
        response.status_code = 200
        response.content = b"{}"
        response.json.return_value = {"ok": True, "config_revision": 8}
        session.post.return_value = response
        with self.assertRaises(LLMNotReadyError):
            bot.ensure_ready(7)
        for revision in (True, -1, "7", None):
            with self.subTest(revision=revision):
                with self.assertRaises(LLMConfigurationError):
                    bot.ensure_ready(revision)
        self.assertEqual(session.post.call_count, 1)

    def test_reply_result_carries_request_local_config_revision(self):
        bot, _session = self.make_bot()
        result = bot.generate_reply_result("问题", item_id="1001")
        self.assertEqual(result["reply"], "usable reply")
        self.assertEqual(result["config_revision"], 7)

    def test_no_reply_and_handoff_return_empty_without_magic_interpretation(self):
        for decision in ("no_reply", "handoff"):
            with self.subTest(decision=decision):
                bot, _ = self.make_bot({
                    "decision": decision,
                    "reply": "",
                    "reason_code": "needs_human",
                    "sources": [],
                    "knowledge_status": "missing",
                })
                self.assertEqual(bot.generate_reply("问题", item_id="1001"), "")
                self.assertEqual(bot.last_intent, decision)
                self.assertEqual(bot.last_reason_code, "needs_human")

    def test_empty_reply_and_invalid_protocol_raise_typed_errors(self):
        cases = (
            ({"decision": "reply", "reply": "", "reason_code": "ok", "sources": [], "knowledge_status": "active"}, LLMEmptyResponseError),
            ({"decision": "reply", "reply": "ok", "reason_code": "BAD VALUE", "sources": [], "knowledge_status": "active"}, LLMResponseFormatError),
            ({"decision": "other", "reply": "", "reason_code": "ok", "sources": [], "knowledge_status": "active"}, LLMResponseFormatError),
            ({"decision": "reply", "reply": "ok", "reason_code": "ok", "sources": [], "knowledge_status": "active", "config_revision": True}, LLMResponseFormatError),
        )
        for payload, error in cases:
            with self.subTest(payload=payload):
                bot, _ = self.make_bot(payload)
                with self.assertRaises(error):
                    bot.generate_reply("问题", item_id="1001")

    def test_status_codes_are_mapped_without_response_body_leakage(self):
        for status, error in (
            (429, LLMRateLimitError),
            (401, LLMConfigurationError),
            (500, LLMServiceError),
        ):
            with self.subTest(status=status):
                bot, _ = self.make_bot(status_code=status)
                with self.assertRaises(error):
                    bot.generate_reply("问题", item_id="1001")

    def test_external_or_malformed_base_url_is_rejected(self):
        for url in (
            "https://example.com/internal/v1",
            "http://127.0.0.1:8096/v1",
            "http://user:pass@127.0.0.1:8096/internal/v1",
        ):
            with self.subTest(url=url):
                with patch.dict(os.environ, {"API_KEY": "token", "MODEL_BASE_URL": url}, clear=False):
                    with self.assertRaises(LLMConfigurationError):
                        XianyuReplyBot(session=MagicMock())

    def test_timed_out_sync_call_does_not_overlap_the_next_call(self):
        import asyncio

        from main import XianyuLive

        bot = MagicMock()
        bot.last_intent = "reply"
        bot.last_reason_code = "ok"
        active = {"count": 0, "maximum": 0}
        finished = threading.Event()

        def slow_reply(*_args, **_kwargs):
            active["count"] += 1
            active["maximum"] = max(active["maximum"], active["count"])
            time.sleep(0.08)
            active["count"] -= 1
            finished.set()
            return {
                "reply": "reply",
                "decision": "reply",
                "reason_code": "ok",
                "config_revision": 7,
            }

        bot.generate_reply_result.side_effect = slow_reply

        async def exercise():
            agent = object.__new__(XianyuLive)
            agent.bot = bot
            agent.automation_mode = "rules_ai"
            agent.llm_lock = asyncio.Lock()
            agent.llm_tasks = set()
            agent.llm_timeout = 0.01
            args = ("a", "1001", {"item_id": "1001"}, [], [])
            with self.assertRaises(asyncio.TimeoutError):
                await agent._generate_llm_reply(*args)
            agent.llm_timeout = 0.2
            second = asyncio.create_task(agent._generate_llm_reply(*args))
            await asyncio.sleep(0.02)
            self.assertFalse(second.done())
            await second
            self.assertTrue(finished.is_set())
            self.assertEqual(active["maximum"], 1)

        asyncio.run(exercise())

    def test_timed_out_readiness_call_does_not_overlap_next_model_call(self):
        import asyncio

        from main import AutomationReplySuppressed, XianyuLive

        bot = MagicMock()
        active = {"count": 0, "maximum": 0}
        readiness_finished = threading.Event()

        def slow_readiness(_expected_revision):
            active["count"] += 1
            active["maximum"] = max(active["maximum"], active["count"])
            time.sleep(0.08)
            active["count"] -= 1
            readiness_finished.set()
            return 7

        def model_reply(*_args, **_kwargs):
            active["count"] += 1
            active["maximum"] = max(active["maximum"], active["count"])
            active["count"] -= 1
            return {
                "reply": "reply",
                "decision": "reply",
                "reason_code": "ok",
                "config_revision": 7,
            }

        bot.ensure_ready.side_effect = slow_readiness
        bot.generate_reply_result.side_effect = model_reply

        async def exercise():
            agent = object.__new__(XianyuLive)
            agent.bot = bot
            agent.automation_mode = "rules_ai"
            agent.llm_lock = asyncio.Lock()
            agent.llm_tasks = set()
            agent.llm_timeout = 0.2
            agent.ai_readiness_lock_timeout = 0.2
            agent.ai_readiness_timeout = 0.01
            with self.assertRaises(AutomationReplySuppressed):
                await agent._ensure_ai_reply_ready("chat", 7)
            second = asyncio.create_task(
                agent._generate_llm_reply(
                    "a", "1001", {"item_id": "1001"}, [], []
                )
            )
            await asyncio.sleep(0.02)
            self.assertFalse(second.done())
            await second
            self.assertTrue(readiness_finished.is_set())
            self.assertEqual(active["maximum"], 1)

        asyncio.run(exercise())

    def test_base_exception_from_sync_call_releases_llm_slot(self):
        import asyncio

        from main import XianyuLive

        class WorkerBaseException(BaseException):
            pass

        bot = MagicMock()
        bot.last_intent = "reply"
        bot.last_reason_code = "ok"
        finished = threading.Event()

        def aborting_reply(*_args, **_kwargs):
            try:
                raise WorkerBaseException("worker aborted")
            finally:
                finished.set()

        bot.generate_reply_result.side_effect = aborting_reply

        async def exercise():
            agent = object.__new__(XianyuLive)
            agent.bot = bot
            agent.automation_mode = "rules_ai"
            agent.llm_lock = asyncio.Lock()
            agent.llm_tasks = set()
            agent.llm_timeout = 0.5
            with self.assertRaises(WorkerBaseException):
                await agent._generate_llm_reply("a", "1001", {"item_id": "1001"}, [], [])
            await asyncio.sleep(0)
            self.assertTrue(finished.is_set())
            self.assertFalse(agent.llm_lock.locked())

        asyncio.run(exercise())


class LegacyAIRuntimeTests(unittest.TestCase):
    def test_missing_or_corrupt_legacy_context_raises_typed_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = os.path.join(directory, "missing.json")
            with self.assertRaises(AIRuntimeMissingError):
                load_published_context(
                    missing,
                    directory,
                    "1001",
                    {"item_id": "1001"},
                    missing,
                )

            corrupt = os.path.join(directory, "settings.json")
            with open(corrupt, "w", encoding="utf-8") as stream:
                stream.write("not json")
            os.chmod(corrupt, 0o600)
            with self.assertRaises(AIRuntimeFormatError):
                load_published_context(
                    corrupt,
                    directory,
                    "1001",
                    {"item_id": "1001"},
                    corrupt,
                )


class XianyuApiResilienceTests(unittest.TestCase):
    def make_api(self, **kwargs):
        sleeps = []
        api = XianyuApis(sleep_func=sleeps.append, **kwargs)
        session = MagicMock()
        session.cookies = requests.cookies.RequestsCookieJar()
        api.session = session
        return api, session, sleeps

    def test_upload_media_uses_concrete_image_mime_type(self):
        api, session, _ = self.make_api(request_timeout=(1.0, 2.0))
        session.post.return_value = json_response(
            {"object": {"url": "https://cdn.example/uploaded.png"}}
        )
        with tempfile.NamedTemporaryFile(suffix=".png") as image:
            image.write(b"fake-image")
            image.flush()

            result = api.upload_media(image.name)

        upload = session.post.call_args
        self.assertEqual(result["object"]["url"], "https://cdn.example/uploaded.png")
        self.assertEqual(upload.kwargs["files"]["file"][2], "image/png")
        self.assertEqual(upload.kwargs["timeout"], (1.0, 2.0))

    def test_every_post_path_has_client_timeout(self):
        cases = (
            (
                "login",
                lambda api: api.hasLogin("device"),
                {"content": {"success": True}},
            ),
            (
                "token",
                lambda api: api.get_token("device"),
                {"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "test"}},
            ),
            (
                "item",
                lambda api: api.get_item_info("1001"),
                {"ret": ["SUCCESS::调用成功"], "data": {}},
            ),
        )

        for name, invoke, payload in cases:
            with self.subTest(path=name):
                api, session, _ = self.make_api(request_timeout=(1.0, 2.0))
                session.post.return_value = json_response(payload)
                invoke(api)
                self.assertEqual(session.post.call_args.kwargs["timeout"], (1.0, 2.0))

    def test_http_login_token_and_mtop_share_one_browser_fingerprint(self):
        api = XianyuApis(token_max_attempts=1, item_max_attempts=1)
        session = MagicMock()
        session.cookies = requests.cookies.RequestsCookieJar()
        session.headers = dict(api.session.headers)
        session.post.side_effect = [
            json_response({"content": {"success": True}}),
            json_response(
                {"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "token"}}
            ),
            json_response({"ret": ["SUCCESS::调用成功"], "data": {}}),
        ]
        api.session = session

        self.assertTrue(api.hasLogin(device_id="stable-device"))
        api.get_token("stable-device")
        api.get_item_info("1001")

        login_call = session.post.call_args_list[0]
        login_headers = login_call.kwargs["headers"]
        token_headers = session.post.call_args_list[1].kwargs["headers"]
        self.assertEqual(login_call.kwargs["data"]["deviceId"], "stable-device")
        for headers in (session.headers, login_headers, token_headers):
            normalized = {str(key).lower(): value for key, value in headers.items()}
            self.assertIn(f"Chrome/{CHROME_MAJOR}.", normalized["user-agent"])
            self.assertEqual(normalized["sec-ch-ua-platform"], f'"{PLATFORM}"')
            self.assertEqual(normalized["origin"], ORIGIN)
            self.assertEqual(normalized["referer"], REFERER)
        self.assertEqual(session.headers["user-agent"], USER_AGENT)

    def test_generic_token_failure_is_one_classified_request_without_login_probe(self):
        api, session, sleeps = self.make_api(token_max_attempts=3)
        session.post.return_value = json_response(
            {"ret": ["FAIL_SYS_BUSY::temporary"]}
        )

        with self.assertRaises(XianyuApiError) as raised:
            api.get_token("device")

        self.assertEqual(raised.exception.code, "platform_busy")
        token_calls = [
            call for call in session.post.call_args_list if call.args[0] == api.url
        ]
        login_calls = [
            call
            for call in session.post.call_args_list
            if call.args[0].endswith("/newlogin/hasLogin.do")
        ]
        self.assertEqual(len(token_calls), 1)
        self.assertEqual(len(login_calls), 0)
        self.assertEqual(sleeps, [])

    def test_expired_mtop_session_is_recovered_once_then_token_retried_once(self):
        api, session, sleeps = self.make_api(token_max_attempts=3)
        session.post.side_effect = [
            json_response({"ret": ["FAIL_SYS_SESSION_EXPIRED::SESSION_EXPIRED"]}),
            json_response({"content": {"success": True}}),
            json_response(
                {"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "test"}}
            ),
        ]

        result = api.get_token("device")

        self.assertEqual(result["data"]["accessToken"], "test")
        token_calls = [
            call for call in session.post.call_args_list if call.args[0] == api.url
        ]
        login_calls = [
            call
            for call in session.post.call_args_list
            if call.args[0].endswith("/newlogin/hasLogin.do")
        ]
        self.assertEqual(len(token_calls), 2)
        self.assertEqual(len(login_calls), 1)
        self.assertEqual(sleeps, [])

    def test_transient_failure_after_session_recovery_stops_the_round(self):
        api, session, sleeps = self.make_api(token_max_attempts=3)
        session.post.side_effect = [
            json_response({"ret": ["FAIL_SYS_SESSION_EXPIRED::SESSION_EXPIRED"]}),
            json_response({"content": {"success": True}}),
            json_response({"ret": ["FAIL_SYS_BUSY::temporary"]}),
        ]

        with self.assertRaises(XianyuApiError) as raised:
            api.get_token("device")

        self.assertEqual(raised.exception.code, "platform_busy")
        token_calls = [
            call for call in session.post.call_args_list if call.args[0] == api.url
        ]
        login_calls = [
            call
            for call in session.post.call_args_list
            if call.args[0].endswith("/newlogin/hasLogin.do")
        ]
        self.assertEqual(len(token_calls), 2)
        self.assertEqual(len(login_calls), 1)
        self.assertEqual(sleeps, [])

    def test_session_recovery_failure_raises_safe_code_after_one_login_call(self):
        api, session, sleeps = self.make_api(
            token_max_attempts=3,
            login_max_attempts=1,
            token_backoff=(3, 10, 30),
        )
        session.post.side_effect = [
            json_response({"ret": ["FAIL_SYS_SESSION_EXPIRED::sensitive-body"]}),
            json_response({"content": {"success": False, "secret": "hidden"}}),
        ]

        with self.assertRaises(XianyuAuthenticationError) as raised:
            api.get_token("device")

        self.assertEqual(raised.exception.code, "session_expired")
        self.assertEqual(str(raised.exception), "session_expired")
        self.assertNotIn("sensitive-body", str(raised.exception))
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(sleeps, [])

    def test_second_session_expiry_after_recovery_does_not_probe_login_again(self):
        api, session, sleeps = self.make_api(
            token_max_attempts=3,
            login_max_attempts=1,
            token_backoff=(3, 10, 30),
        )
        session.post.side_effect = [
            json_response({"ret": ["FAIL_SYS_SESSION_EXPIRED::first"]}),
            json_response({"content": {"success": True}}),
            json_response({"ret": ["FAIL_SYS_SESSION_EXPIRED::second"]}),
        ]

        with self.assertRaises(XianyuAuthenticationError) as raised:
            api.get_token("device")

        self.assertEqual(raised.exception.code, "session_expired")
        token_calls = [
            call for call in session.post.call_args_list if call.args[0] == api.url
        ]
        login_calls = [
            call
            for call in session.post.call_args_list
            if call.args[0].endswith("/newlogin/hasLogin.do")
        ]
        self.assertEqual(len(token_calls), 2)
        self.assertEqual(len(login_calls), 1)
        self.assertEqual(sleeps, [])

    def test_risk_control_is_safe_and_not_retried(self):
        api, session, sleeps = self.make_api(token_max_attempts=3)
        session.post.return_value = json_response(
            {"ret": ["RGV587_ERROR::platform-response-must-not-leak"]}
        )

        with self.assertRaises(XianyuAuthenticationError) as raised:
            api.get_token("device")

        self.assertEqual(raised.exception.code, "risk_control")
        self.assertEqual(str(raised.exception), "risk_control")
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_token_response_errors_have_stable_distinct_codes(self):
        cases = (
            (json_response({"ret": ["FAIL_SYS_BUSY::temporary"]}), "platform_busy"),
            (json_response({"ret": ["UNKNOWN::failure"]}, status_code=429), "platform_busy"),
            (json_response({"ret": ["PUBLISH_FORBIDDEN::limited"]}), "account_restricted"),
            (json_response({"ret": ["SUCCESS::调用成功"], "data": {}}), "token_unavailable"),
        )
        for response, expected in cases:
            with self.subTest(expected=expected):
                api, session, _ = self.make_api(token_max_attempts=1)
                session.post.return_value = response
                with self.assertRaises(XianyuApiError) as raised:
                    api.get_token("device")
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(session.post.call_count, 1)

        api, session, _ = self.make_api(token_max_attempts=1)
        response = MagicMock()
        response.json.side_effect = ValueError("invalid json body")
        session.post.return_value = response
        with self.assertRaises(XianyuApiError) as raised:
            api.get_token("device")
        self.assertEqual(raised.exception.code, "response_invalid")

    def test_cookie_header_snapshot_uses_latest_session_values(self):
        api = XianyuApis(sleep_func=lambda _delay: None)
        api.update_cookies({"unb": "seller", "cookie2": "long-lived"})
        api.session.cookies.set("_m_h5_tk", "old_1", domain="first.invalid")
        api.session.cookies.set("_m_h5_tk", "new_2", domain="second.invalid")

        snapshot = api.cookie_header_snapshot()

        self.assertIn("unb=seller", snapshot)
        self.assertIn("cookie2=long-lived", snapshot)
        self.assertIn("_m_h5_tk=new_2", snapshot)
        self.assertNotIn("old_1", snapshot)
        self.assertEqual(snapshot.count("_m_h5_tk="), 1)

    def test_token_network_error_is_one_classified_request(self):
        api, session, sleeps = self.make_api(token_max_attempts=3)
        session.post.side_effect = requests.ConnectionError("offline")

        with self.assertRaises(XianyuApiError) as raised:
            api.get_token("device")
        self.assertEqual(raised.exception.code, "network_error")
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_login_network_error_is_one_classified_request(self):
        api, session, sleeps = self.make_api(login_max_attempts=2)
        session.post.side_effect = requests.ConnectionError("offline")

        with self.assertRaises(XianyuApiError) as raised:
            api.hasLogin(device_id="device")
        self.assertEqual(raised.exception.code, "network_error")
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_direct_api_identifiers_are_validated_before_network_io(self):
        api, session, _ = self.make_api()
        for device_id in (None, "", "bad\nvalue", 'bad"value', "x" * 257):
            with self.subTest(device_id=device_id):
                with self.assertRaises(ValueError):
                    api.get_token(device_id)
        for item_id in (None, "", "item", "1 OR 1", "1" * 65):
            with self.subTest(item_id=item_id):
                with self.assertRaises(ValueError):
                    api.get_item_info(item_id)
        self.assertEqual(session.post.call_count, 0)

    def test_shared_session_requests_are_serialized(self):
        api = XianyuApis(
            sleep_func=lambda _delay: None,
            token_max_attempts=1,
            item_max_attempts=1,
        )

        class TrackingSession:
            def __init__(self):
                self.cookies = requests.cookies.RequestsCookieJar()
                self._lock = threading.Lock()
                self.active = 0
                self.max_active = 0

            def post(self, url, **_kwargs):
                with self._lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.03)
                with self._lock:
                    self.active -= 1
                if url == api.url:
                    return json_response(
                        {"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "test"}}
                    )
                return json_response(
                    {"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {}}}
                )

        session = TrackingSession()
        api.session = session
        with ThreadPoolExecutor(max_workers=2) as executor:
            token = executor.submit(api.get_token, "device")
            item = executor.submit(api.get_item_info, "1001")
            self.assertIn("data", token.result())
            self.assertIn("data", item.result())
        self.assertEqual(session.max_active, 1)

    def test_trade_endpoints_use_bounded_signed_requests(self):
        api, session, _ = self.make_api(
            trade_max_attempts=1, request_timeout=(1.0, 2.0)
        )
        session.cookies.set("_m_h5_tk", "token_123")
        session.post.side_effect = [
            json_response(
                {
                    "ret": ["SUCCESS::调用成功"],
                    "data": {"commonData": {"orderId": "5005"}},
                }
            ),
            json_response({"ret": ["SUCCESS::调用成功"], "data": {}}),
        ]
        api.get_message_head_info("3003", "1001")
        api.get_order_detail("5005")
        head_call, detail_call = session.post.call_args_list
        self.assertIn("headinfo", head_call.args[0])
        self.assertIn("order.detail", detail_call.args[0])
        self.assertEqual(head_call.kwargs["timeout"], (1.0, 2.0))
        self.assertEqual(detail_call.kwargs["timeout"], (1.0, 2.0))
        self.assertEqual(
            json.loads(head_call.kwargs["data"]["data"]),
            {"itemId": "1001", "sessionId": 3003, "sessionType": 1},
        )
        self.assertEqual(
            json.loads(detail_call.kwargs["data"]["data"]), {"tid": "5005"}
        )

    def test_trade_identifiers_are_validated_before_network_io(self):
        api, session, _ = self.make_api(trade_max_attempts=1)
        for session_id, item_id in (("chat", "1001"), ("3003", "item"), ("", "1001")):
            with self.subTest(session_id=session_id, item_id=item_id):
                with self.assertRaises(ValueError):
                    api.get_message_head_info(session_id, item_id)
        with self.assertRaises(ValueError):
            api.get_order_detail("order")
        self.assertEqual(session.post.call_count, 0)


if __name__ == "__main__":
    unittest.main()
