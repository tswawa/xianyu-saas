#!/usr/bin/env python3
"""Offline Agent-native protocol, full-history, transport and error contracts.

No app import, real model calls, credential files or production tenant data.
"""
from __future__ import annotations

import base64
import copy
import json
import socket
import ssl
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import ai_customer_service as service_module
from ai_customer_service import AIService, AIServiceError, AgentProviderError
from ai_provider_adapters import (
    ProviderAdapterError, agent_json_loads, build_agent_request,
    parse_agent_response, provider_catalog,
)

PROVIDERS = [item["code"] for item in provider_catalog()]
# Short, deliberately invalid credential; still exercises URL/base64 redaction.
TEST_KEY = "sk-test-8z+y/abc="
BASE = "https://provider.example/v1"
MODEL = "not-in-any-local-model-list"
TOOLS = [
    {"name": "lookup", "description": "按条件查找测试对象。", "parameters": {
        "type": "object", "properties": {
            "query": {"type": "string"},
            "options": {"type": "object", "properties": {
                "page": {"type": "integer", "minimum": 1},
                "label": {"type": ["string", "null"], "enum": ["甲", "乙", None]},
            }, "required": ["page"], "additionalProperties": False},
        }, "required": ["query"], "additionalProperties": False,
    }},
    {"name": "save_note", "description": "保存测试内容，不访问任何业务数据。", "parameters": {
        "type": "object", "properties": {
            "note": {"type": "string"}, "enabled": {"type": "boolean"},
            "revision": {"type": "integer", "minimum": 0},
        }, "required": ["note", "enabled", "revision"], "additionalProperties": False,
    }},
]
ARGUMENTS = [
    {"query": "甲"},
    {"query": "乙", "options": {"page": 2, "label": None}},
    {"note": "原文资料" * 5000, "enabled": True, "revision": 7},
]
NAMES = ["lookup", "lookup", "save_note"]
LONG_TEXT = "  原文开始\n" + "绝不截断资料" * 25_000 + "\n原文结束  "
HISTORY = [{"role": "system", "content": "  只使用已注册函数。\n"}]
for index in range(14):
    HISTORY.extend([
        {"role": "user", "content": f"历史{index}:" + (LONG_TEXT if index == 0 else "继续")},
        {"role": "assistant", "content": f"历史回复{index}", "tool_calls": []},
    ])
HISTORY.append({"role": "user", "content": LONG_TEXT})


def raw_turn(provider, *, final=False, text="  已核对真实回执。\n"):
    args = copy.deepcopy(ARGUMENTS)
    if provider in {"openai_chat_completions", "openai_responses"}:
        args[0]["options"] = None  # Wire-only nullable optional placeholder.
    if provider == "openai_chat_completions":
        message = {"role": "assistant", "content": text if final else None}
        if not final:
            message.update(reasoning_content="PRIVATE_REASONING_DO_NOT_DISPLAY", tool_calls=[
                {"id": f"call-{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(arg, ensure_ascii=False)}}
                for i, (name, arg) in enumerate(zip(NAMES, args))
            ])
        return {"choices": [{"message": message, "finish_reason": "stop" if final else "tool_calls"}]}
    if provider == "openai_responses":
        output = [{"type": "message", "id": "msg-final", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}] if final else [
            {"type": "reasoning", "id": "rs-1", "summary": [], "encrypted_content": "OPAQUE_ENCRYPTED_CONTINUATION"},
            *[{"type": "function_call", "id": f"fc-{i}", "call_id": f"call-{i}", "name": name, "arguments": json.dumps(arg, ensure_ascii=False), "status": "completed"}
              for i, (name, arg) in enumerate(zip(NAMES, args))],
        ]
        return {"id": "resp-test", "status": "completed", "output": output}
    if provider == "anthropic_messages":
        blocks = [{"type": "text", "text": text}] if final else [
            {"type": "thinking", "thinking": "PRIVATE_REASONING_DO_NOT_DISPLAY", "signature": "SIGNED_CLAUDE_CONTINUATION"},
            {"type": "redacted_thinking", "data": "OPAQUE_REDACTED_CONTINUATION"},
            *[{"type": "tool_use", "id": f"call-{i}", "name": name, "input": arg} for i, (name, arg) in enumerate(zip(NAMES, args))],
        ]
        return {"role": "assistant", "content": blocks, "stop_reason": "end_turn" if final else "tool_use"}
    if provider == "google_gemini":
        parts = [{"text": text}] if final else [
            {"text": "PRIVATE_REASONING_DO_NOT_DISPLAY", "thought": True, "thoughtSignature": "THOUGHT_TEXT_SIGNATURE"},
            *[{"functionCall": {**({"id": f"call-{i}"} if i != 1 else {}), "name": name, "args": arg},
               **({"thoughtSignature": "SIGNED_FUNCTION_CONTINUATION"} if i == 0 else {})}
              for i, (name, arg) in enumerate(zip(NAMES, args))],
        ]
        return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}]}
    message = {"role": "assistant", "content": text if final else ""}
    if not final:
        message.update(thinking="PRIVATE_REASONING_DO_NOT_DISPLAY", tool_calls=[
            {"function": {"index": i, "name": name, "arguments": arg}} for i, (name, arg) in enumerate(zip(NAMES, args))
        ])
    return {"message": message, "done": True, "done_reason": "stop"}


def mutate_arguments(provider, response, value, index=0):
    if provider == "openai_chat_completions":
        response["choices"][0]["message"]["tool_calls"][index]["function"]["arguments"] = value
    elif provider == "openai_responses":
        response["output"][index + 1]["arguments"] = value
    elif provider == "anthropic_messages":
        response["content"][index + 2]["input"] = value
    elif provider == "google_gemini":
        response["candidates"][0]["content"]["parts"][index + 1]["functionCall"]["args"] = value
    else:
        response["message"]["tool_calls"][index]["function"]["arguments"] = value


def resolver(_host, port, type=None):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class UnifiedConnection:
    def __init__(self, provider):
        self.provider, self.revision = provider, 1
        self.status = "verified"

    def initialized(self, uid):
        return True

    def read(self, uid):
        return {"scope": "user", "revision": self.revision, "key_revision": self.revision, "connection_status": self.status}

    def runtime(self, uid):
        if self.status != "verified":
            raise AIServiceError("connection_unconfigured", 503)
        return {"provider": self.provider, "base_url": BASE, "model": MODEL, "api_key": TEST_KEY}


class AgentContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="offline-agent-contract-")
        self.addCleanup(self.temp.cleanup)

    def service(self, provider="openai_chat_completions", requester=None, environ=None):
        service = AIService(Path(self.temp.name) / "tenants", environ=environ or {}, resolver=resolver, requester=requester)
        service.user_connections = UnifiedConnection(provider)
        return service

    def test_five_provider_multi_tool_two_turns_and_complete_history(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                observed = []
                first_raw, last_raw = raw_turn(provider), raw_turn(provider, final=True)

                def requester(url, key, payload, headers):
                    observed.append(copy.deepcopy(payload))
                    self.assertEqual(key, TEST_KEY)
                    self.assertEqual(payload.get("model", MODEL), MODEL)
                    return copy.deepcopy(first_raw if len(observed) == 1 else last_raw)

                service = self.service(provider, requester)
                untouched_history, untouched_tools = copy.deepcopy(HISTORY), copy.deepcopy(TOOLS)
                assistant = service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
                self.assertEqual(set(assistant), {"role", "content", "tool_calls", "provider_data"})
                self.assertEqual(assistant["role"], "assistant")
                self.assertEqual(assistant["content"], "")  # Empty content is a valid tool response.
                self.assertEqual([call["arguments"] for call in assistant["tool_calls"]], ARGUMENTS)
                self.assertEqual(len({call["id"] for call in assistant["tool_calls"]}), 3)
                tool_messages = [{"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": {"ok": True, "position": i}}
                                 for i, call in enumerate(assistant["tool_calls"])]
                # Out-of-order local completions must still associate by ID,
                # including Gemini/Ollama duplicate names without native IDs.
                history = HISTORY + [assistant] + list(reversed(tool_messages))
                final = service.agent_turn(11, 3, "shop-test", history, TOOLS)
                self.assertEqual(final["content"], "  已核对真实回执。\n")
                self.assertEqual(final["tool_calls"], [])
                self.assertEqual(HISTORY, untouched_history)
                self.assertEqual(TOOLS, untouched_tools)
                for payload in observed:
                    serialized = json.dumps(payload, ensure_ascii=False)
                    self.assertGreater(len(serialized.encode()), service_module.MAX_REQUEST_BYTES)
                    self.assertIn(LONG_TEXT, self.collect_text(payload))
                    self.assertIn("历史0:" + LONG_TEXT, self.collect_text(payload))
                    self.assertIn("历史回复0", self.collect_text(payload))
                    self.assertIn("历史回复13", self.collect_text(payload))
                self.check_roundtrip_shape(provider, first_raw, observed, assistant)
                # Final assistant provider_data is also replayable on a third request.
                build_agent_request(provider, BASE, MODEL, TEST_KEY, history + [final, {"role": "user", "content": "继续"}], TOOLS)

    @staticmethod
    def collect_text(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [text for item in value for text in AgentContracts.collect_text(item)]
        if isinstance(value, dict):
            return [text for item in value.values() for text in AgentContracts.collect_text(item)]
        return []

    def check_roundtrip_shape(self, provider, raw, observed, assistant):
        first, second = observed
        if provider in {"openai_chat_completions", "openai_responses"}:
            function = first["tools"][0].get("function", first["tools"][0])
            self.assertIs(function["strict"], True)
            schema = function["parameters"]
            self.assertEqual(schema["required"], ["query", "options"])
            nested = schema["properties"]["options"]["anyOf"][0]
            self.assertEqual(nested["required"], ["page", "label"])
            self.assertIs(nested["additionalProperties"], False)
        if provider == "openai_chat_completions":
            self.assertNotIn("max_tokens", first)
            self.assertNotIn("max_completion_tokens", first)
            replay = second["messages"][-4]
            self.assertEqual(replay["reasoning_content"], "PRIVATE_REASONING_DO_NOT_DISPLAY")
            self.assertEqual(replay["tool_calls"], raw["choices"][0]["message"]["tool_calls"])
            self.assertEqual([item["tool_call_id"] for item in second["messages"][-3:]], [call["id"] for call in assistant["tool_calls"]])
        elif provider == "openai_responses":
            self.assertEqual(first["include"], ["reasoning.encrypted_content"])
            self.assertEqual(first["truncation"], "disabled")
            self.assertIs(first["store"], False)
            self.assertNotIn("max_output_tokens", first)
            self.assertEqual(second["input"][-7:-3], raw["output"])
            self.assertEqual([item["call_id"] for item in second["input"][-3:]], ["call-0", "call-1", "call-2"])
            self.assertTrue(all(item["type"] == "function_call_output" for item in second["input"][-3:]))
        elif provider == "anthropic_messages":
            self.assertGreater(first["max_tokens"], 4096)
            self.assertIs(first["tools"][0]["strict"], True)
            self.assertEqual(first["tools"][0]["input_schema"]["required"], ["query"])
            self.assertEqual(second["messages"][-2]["content"], raw["content"])
            results = second["messages"][-1]
            self.assertEqual(results["role"], "user")
            self.assertEqual([item["tool_use_id"] for item in results["content"]], ["call-0", "call-1", "call-2"])
        elif provider == "google_gemini":
            self.assertIn("parametersJsonSchema", first["tools"][0]["functionDeclarations"][0])
            self.assertNotIn("parameters", first["tools"][0]["functionDeclarations"][0])
            self.assertEqual(first["toolConfig"]["functionCallingConfig"]["mode"], "VALIDATED")
            self.assertNotIn("generationConfig", first)
            self.assertEqual(second["contents"][-2], raw["candidates"][0]["content"])
            results = second["contents"][-1]["parts"]
            self.assertEqual(results[0]["functionResponse"]["id"], "call-0")
            self.assertNotIn("id", results[1]["functionResponse"])
            self.assertEqual([item["functionResponse"]["response"]["position"] for item in results], [0, 1, 2])
            self.assertTrue(build_agent_request(provider, BASE, MODEL, TEST_KEY, HISTORY, TOOLS)["url"].endswith(":generateContent"))
        else:
            self.assertNotIn("strict", first["tools"][0]["function"])
            self.assertNotIn("options", first)
            self.assertEqual(second["messages"][-4], raw["message"])
            results = second["messages"][-3:]
            self.assertEqual([item["tool_name"] for item in results], NAMES)
            self.assertEqual([json.loads(item["content"])["position"] for item in results], [0, 1, 2])

    def test_invalid_parameters_and_non_json_model_prose_never_execute(self):
        invalid_values = [[], "{bad", {"query": 7}, {"query": "甲", "extra": True},
                          {"query": "甲", "options": {"page": True}}, {"query": "甲", "options": {"page": 0}}]
        for provider in PROVIDERS:
            for invalid in invalid_values:
                with self.subTest(provider=provider, invalid=invalid):
                    response = raw_turn(provider)
                    value = json.dumps(invalid) if provider in {"openai_chat_completions", "openai_responses"} else invalid
                    mutate_arguments(provider, response, value)
                    with self.assertRaises(ProviderAdapterError) as raised:
                        parse_agent_response(provider, response, TOOLS)
                    self.assertEqual(raised.exception.code, "tool_arguments_invalid")
            prose = '{"name":"save_note","arguments":{"note":"MUST NOT EXECUTE"}}'
            result = parse_agent_response(provider, raw_turn(provider, final=True, text=prose), TOOLS)
            self.assertEqual(result["tool_calls"], [])
            self.assertEqual(result["content"], prose)
            # No 32k legacy response truncation/rejection on the Agent path.
            long = "自然语言正文" * 8000
            self.assertEqual(parse_agent_response(provider, raw_turn(provider, final=True, text=long), TOOLS)["content"], long)
        for provider in {"openai_chat_completions", "openai_responses"}:
            for text in ('{"query":"甲","query":"乙"}', '{"query":NaN}', '{} trailing'):
                response = raw_turn(provider)
                mutate_arguments(provider, response, text)
                with self.assertRaises(ProviderAdapterError):
                    parse_agent_response(provider, response, TOOLS)
        for body in (b'{"args":{"a":1,"a":2}}', b'{"args":{"a":NaN}}', b'{"args":{"a":1e999}}'):
            with self.assertRaises(ProviderAdapterError):
                agent_json_loads(body)

    def test_registry_and_history_fail_closed(self):
        for tools in ([{"type": "web_search"}], [{"type": "function", **TOOLS[0]}], TOOLS + [TOOLS[0]]):
            with self.assertRaises(ProviderAdapterError):
                build_agent_request(PROVIDERS[0], BASE, MODEL, TEST_KEY, HISTORY, tools)
        for provider in PROVIDERS:
            assistant = parse_agent_response(provider, raw_turn(provider), TOOLS)
            with self.assertRaises(ProviderAdapterError):
                parse_agent_response(provider, raw_turn(provider), [])
            with self.assertRaises(ProviderAdapterError):
                build_agent_request(provider, BASE, MODEL, TEST_KEY, HISTORY + [assistant], TOOLS)
            results = [{"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": {}}
                       for call in assistant["tool_calls"]]
            for change in ("wrong_name", "wrong_id", "duplicate", "not_object"):
                broken = copy.deepcopy(results)
                if change == "wrong_name":
                    broken[0]["name"] = "save_note"
                elif change == "wrong_id":
                    broken[0]["tool_call_id"] = "unknown"
                elif change == "not_object":
                    broken[0]["content"] = "not a dict"
                else:
                    broken[1] = copy.deepcopy(broken[0])
                with self.assertRaises(ProviderAdapterError):
                    build_agent_request(provider, BASE, MODEL, TEST_KEY, HISTORY + [assistant] + broken, TOOLS)
            changed = copy.deepcopy(assistant)
            changed["tool_calls"][0]["arguments"]["query"] = "forged"
            with self.assertRaises(ProviderAdapterError) as raised:
                build_agent_request(provider, BASE, MODEL, TEST_KEY, HISTORY + [changed] + results, TOOLS)
            self.assertEqual(raised.exception.code, "continuation_mismatch")
        builtin = raw_turn("openai_responses")
        builtin["output"].append({"type": "web_search_call", "id": "forbidden"})
        with self.assertRaises(ProviderAdapterError):
            parse_agent_response("openai_responses", builtin, TOOLS)

    def test_incomplete_native_turns_and_items_never_become_executable(self):
        for provider, field, values in (
            ("openai_chat_completions", "finish_reason", (None, "length", "content_filter")),
            ("openai_responses", "status", ("queued", "in_progress", "incomplete", "failed", "cancelled")),
            ("anthropic_messages", "stop_reason", (None, "max_tokens", "pause_turn")),
            ("google_gemini", "finishReason", (None, "MAX_TOKENS", "FINISH_REASON_UNSPECIFIED")),
            ("ollama_chat", "done", (False, None, "true", 1)),
        ):
            for value in values:
                with self.subTest(provider=provider, field=field, value=value):
                    response = raw_turn(provider)
                    target = (response["choices"][0] if provider == "openai_chat_completions" else
                              response["candidates"][0] if provider == "google_gemini" else response)
                    target[field] = value
                    service = self.service(provider, lambda *_, body=response: (200, body, {}))
                    with self.assertRaises(AgentProviderError) as raised:
                        service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
                    self.assertEqual(raised.exception.code, "generation_incomplete")
                    self.assertEqual(raised.exception.public_detail()["source"], "provider")
                    self.assertEqual(raised.exception.public_detail()["upstream_status"], 200)
        for index in range(4):
            for status in ("in_progress", "incomplete"):
                with self.subTest(provider="openai_responses", item=index, status=status):
                    response = raw_turn("openai_responses")
                    response["output"][index]["status"] = status
                    with self.assertRaises(ProviderAdapterError) as raised:
                        parse_agent_response("openai_responses", response, TOOLS)
                    self.assertEqual(raised.exception.code, "generation_incomplete")
        response = raw_turn("openai_responses")
        response["output"].append({"type": "message", "role": "assistant", "status": "incomplete",
                                   "content": [{"type": "output_text", "text": "尚未核对"}]})
        with self.assertRaises(ProviderAdapterError):
            parse_agent_response("openai_responses", response, TOOLS)

    def test_entire_multi_call_batch_rejects_invalid_last_call_and_wire_duplicates(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                response = raw_turn(provider)
                invalid = {"note": "不得执行此前合法调用", "enabled": True, "revision": "7"}
                mutate_arguments(provider, response, json.dumps(invalid) if provider in {
                    "openai_chat_completions", "openai_responses"} else invalid, index=2)
                service = self.service(provider, lambda *_, body=response: (200, body, {}))
                with self.assertRaises(AgentProviderError) as raised:
                    service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
                self.assertEqual(raised.exception.code, "tool_arguments_invalid")
                response = raw_turn(provider)
                if provider in {"openai_chat_completions", "openai_responses"}:
                    mutate_arguments(provider, response, '{"query":"甲","query":"乙"}')
                    wire = json.dumps(response, ensure_ascii=False).encode("utf-8")
                else:
                    wire = json.dumps(response, ensure_ascii=False).replace('"query": "甲"', '"query": "甲", "query": "乙"').encode("utf-8")
                service.requester = lambda *_, body=wire: (200, body, {})
                with self.assertRaises(AgentProviderError) as raised:
                    service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
                self.assertIn(raised.exception.code, {"tool_arguments_invalid", "response_invalid"})
                self.assertEqual(raised.exception.public_detail()["source"], "provider")

    def test_optional_generation_budget_is_not_a_context_limit(self):
        for provider in PROVIDERS:
            observed = []
            service = self.service(provider, lambda url, key, payload, headers: observed.append(payload) or raw_turn(provider, final=True),
                                   {"SAAS_AI_AGENT_MAX_OUTPUT_TOKENS": "100000"})
            service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
            payload = observed[0]
            if provider == "openai_chat_completions":
                self.assertEqual(payload["max_completion_tokens"], 100000)
            elif provider == "openai_responses":
                self.assertEqual(payload["max_output_tokens"], 100000)
            elif provider == "anthropic_messages":
                self.assertEqual(payload["max_tokens"], 100000)
            elif provider == "google_gemini":
                self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 100000)
            else:
                self.assertEqual(payload["options"]["num_predict"], 100000)
            self.assertIn(LONG_TEXT, self.collect_text(payload))
        for value in ("0", "-1", "not-a-number", True):
            service = self.service(requester=lambda *_: self.fail("invalid config must not request"), environ={"SAAS_AI_AGENT_MAX_OUTPUT_TOKENS": value})
            with self.assertRaises(AgentProviderError) as raised:
                service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
            self.assertEqual(raised.exception.public_detail()["source"], "application")

    def test_upstream_http_errors_preserve_reason_without_secrets_or_local_401(self):
        explanation = "Tools not supported; context length exceeded."
        sensitive = (f"\nAPI key: {TEST_KEY}\nAuthorization: Bearer {TEST_KEY}\nCookie: session=COOKIE_SECRET"
                     f"\npath: C:\\private\\tenant\\secret.json; /srv/tenants/private/key.json"
                     f"\nURL https://provider.example/internal/path?api_key={urllib.parse.quote(TEST_KEY, safe='')}"
                     f"\nencoded {base64.b64encode(TEST_KEY.encode()).decode()}")
        for provider in PROVIDERS:
            for status in (400, 401, 403, 429, 500, 503):
                with self.subTest(provider=provider, status=status):
                    body = {"error": {"message": explanation + sensitive, "code": "tool_not_supported", "type": "invalid_request_error"}}
                    service = self.service(provider, lambda *_, s=status, b=body: (s, json.dumps(b).encode(), {"x-request-id": "request-test", "set-cookie": "ignored"}))
                    with self.assertRaises(AgentProviderError) as raised:
                        service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
                    error = raised.exception
                    detail = error.public_detail()
                    self.assertEqual(error.status_code, 502)
                    self.assertEqual(detail["source"], "provider")
                    self.assertEqual(detail["upstream_status"], status)
                    self.assertEqual(detail["upstream_code"], "tool_not_supported")
                    self.assertEqual(detail["upstream_type"], "invalid_request_error")
                    self.assertEqual(detail["upstream_request_id"], "request-test")
                    self.assertIn(explanation, detail["message"])
                    self.assertNotEqual(error.code, "service_unavailable")
                    self.assert_sanitized(json.dumps(detail, ensure_ascii=False))
                    detail["message"] = "mutated"
                    self.assertNotEqual(error.public_detail()["message"], "mutated")
        for body in (b"model tools not supported", f"<html><script>COOKIE_SECRET</script><h1>{explanation}</h1><p>{TEST_KEY}</p></html>".encode()):
            service = self.service(requester=lambda *_: (400, body, {}))
            with self.assertRaises(AgentProviderError) as raised:
                service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
            self.assertNotIn("<", raised.exception.public_detail()["message"])
            self.assert_sanitized(json.dumps(raised.exception.public_detail()))
        google = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "context too long"}}
        service = self.service("google_gemini", lambda *_: (400, google, {}))
        with self.assertRaises(AgentProviderError) as raised:
            service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
        self.assertEqual(raised.exception.public_detail()["upstream_type"], "INVALID_ARGUMENT")
        self.assertEqual(raised.exception.public_detail()["upstream_code"], "400")

    def test_error_json_headers_and_root_metadata_are_safely_preserved(self):
        explanation = "Function tools are not supported by this model."
        sensitive = json.dumps({"Cookie": "session=COOKIE_SECRET", "Authorization": "Basic PRIVATE_HEADER",
                                "api_key": "PRIVATE_CREDENTIAL", "password": "PRIVATE_PASSWORD"})
        body = {"error": explanation + " " + sensitive, "code": "tools_unsupported", "type": "model_capability_error"}
        service = self.service(requester=lambda *_: (403, body, {"X-Request-Id": "diag-header-case"}))
        with self.assertRaises(AgentProviderError) as raised:
            service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
        detail = raised.exception.public_detail()
        self.assertIn(explanation, detail["message"])
        for forbidden in ("COOKIE_SECRET", "PRIVATE_HEADER", "PRIVATE_CREDENTIAL", "PRIVATE_PASSWORD"):
            self.assertNotIn(forbidden, json.dumps(detail))
        self.assertEqual(detail["upstream_code"], "tools_unsupported")
        self.assertEqual(detail["upstream_type"], "model_capability_error")
        self.assertEqual(detail["upstream_request_id"], "diag-header-case")
        for headers, expected in (({"X-Request-Id": "diag-header-case"}, "diag-header-case"),
                                  ({"x-amzn-requestid": "diag-amzn"}, "diag-amzn")):
            service.requester = lambda *_, value=headers: (200, b"not a native JSON response", value)
            with self.assertRaises(AgentProviderError) as raised:
                service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
            self.assertEqual(raised.exception.public_detail()["upstream_request_id"], expected)

    def assert_sanitized(self, text):
        for forbidden in (TEST_KEY, urllib.parse.quote(TEST_KEY, safe=""), "COOKIE_SECRET", "private", "/srv/tenants", "Authorization: Bearer", base64.b64encode(TEST_KEY.encode()).decode()):
            self.assertNotIn(forbidden, text)

    def test_application_transport_errors_and_late_generation_discard(self):
        for exc in (TimeoutError(TEST_KEY), RuntimeError("C:\\private\\key " + TEST_KEY)):
            def fail(*_):
                raise exc
            service = self.service(requester=fail)
            with self.assertRaises(AgentProviderError) as raised:
                service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
            self.assertEqual(raised.exception.public_detail()["source"], "transport")
            self.assert_sanitized(str(raised.exception))
        for provider in PROVIDERS:
            service = self.service(provider)
            def stale(*_):
                service.user_connections.revision += 1
                service.user_connections.status = "unconfigured"
                return raw_turn(provider)
            service.requester = stale
            with self.assertRaises(AgentProviderError) as raised:
                service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
            self.assertEqual(raised.exception.code, "revision_conflict")
            self.assertEqual(raised.exception.public_detail()["source"], "application")
            with self.assertRaises(AgentProviderError) as raised:
                service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
            self.assertEqual(raised.exception.code, "connection_unconfigured")
            self.assertEqual(raised.exception.public_detail()["source"], "application")
        service = self.service(requester=lambda *_: self.fail("unsafe target must never be requested"))
        service.resolver = lambda host, port, type=None: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        with self.assertRaises(AgentProviderError) as raised:
            service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
        self.assertEqual(raised.exception.code, "address_unsafe")

    def test_agent_real_transport_uses_pinned_dns_and_large_body_not_legacy_cap(self):
        dns_calls, socket_calls, tls_calls, requests = [], [], [], []
        state = {"status": 200, "body": json.dumps(raw_turn("openai_chat_completions")).encode()}

        def dns(host, port, type=None):
            dns_calls.append((host, port))
            return resolver(host, port, type)

        class FakeSocket:
            def __init__(self, family, socktype, protocol):
                pass
            def settimeout(self, timeout):
                assert 0 < timeout <= 120
            def connect(self, address):
                socket_calls.append(address)
            def close(self):
                pass
            def shutdown(self, how):
                pass

        class FakeTLS:
            check_hostname, verify_mode = True, ssl.CERT_REQUIRED
            def wrap_socket(self, sock, server_hostname):
                tls_calls.append(server_hostname)
                return sock

        class FakeResponse:
            @property
            def status(self):
                return state["status"]
            def read(self, maximum):
                assert maximum == service_module.AGENT_MAX_RESPONSE_BYTES + 1
                return state["body"]
            def getheader(self, key):
                return "pinned-request" if key == "x-request-id" else None

        class FakeConnection:
            def __init__(self, host, port, timeout):
                self.sock = None
            def request(self, method, target, body, headers):
                assert self.sock is not None
                requests.append({"target": target, "body": body, "headers": headers})
            def getresponse(self):
                return FakeResponse()
            def close(self):
                self.sock.close()

        service = self.service()
        service.resolver = dns
        with patch.object(service_module.socket, "socket", FakeSocket), patch.object(service_module.ssl, "create_default_context", return_value=FakeTLS()), patch.object(service_module.http.client, "HTTPConnection", FakeConnection):
            result = service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
            self.assertEqual(len(result["tool_calls"]), 3)
            self.assertEqual(dns_calls, [("provider.example", 443)])
            self.assertEqual(socket_calls, [("93.184.216.34", 443)])
            self.assertEqual(tls_calls, ["provider.example"])
            self.assertGreater(len(requests[0]["body"]), 128 * 1024)
            self.assertEqual(requests[0]["headers"]["Host"], "provider.example")
            self.assertEqual(requests[0]["target"], "/v1/chat/completions")
            state.update(status=401, body=json.dumps({"error": {"message": "key invalid " + TEST_KEY, "code": "auth_error"}}).encode())
            with self.assertRaises(AgentProviderError) as raised:
                service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
            self.assertEqual(raised.exception.status_code, 502)
            self.assertEqual(raised.exception.public_detail()["upstream_request_id"], "pinned-request")
            self.assert_sanitized(str(raised.exception))
            state.update(status=302, body=b"{}")
            with self.assertRaises(AgentProviderError) as raised:
                service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
            self.assertEqual(raised.exception.code, "address_unsafe")
            state.update(status=200, body=b"x" * (service_module.AGENT_MAX_RESPONSE_BYTES + 1))
            with self.assertRaises(AgentProviderError) as raised:
                service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
            self.assertEqual(raised.exception.code, "transport_response_too_large")
            self.assertEqual(len(dns_calls), 4)
            self.assertEqual(len(socket_calls), 4)
        # Explicit transmission rejection, not silent history slicing.
        with patch.object(service_module, "AGENT_MAX_REQUEST_BYTES", 1024):
            service.requester = lambda *_: self.fail("oversized request must not be sent")
            with self.assertRaises(AgentProviderError) as raised:
                service.agent_turn(11, 3, "shop-test", HISTORY, TOOLS)
            self.assertEqual(raised.exception.code, "transport_request_too_large")


if __name__ == "__main__":
    unittest.main(verbosity=2)
