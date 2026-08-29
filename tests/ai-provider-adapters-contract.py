#!/usr/bin/env python3
"""Offline contracts for supported AI provider request and response adapters."""

from __future__ import annotations

import base64
import json
import socket
import ssl
import tempfile
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import ai_customer_service as ai_service_module  # noqa: E402
from ai_customer_service import AIService, AIServiceError  # noqa: E402
from ai_provider_adapters import (  # noqa: E402
    ProviderAdapterError,
    build_request,
    normalize_base_url,
    normalize_provider,
    parse_response,
    provider_catalog,
)


MESSAGES = [
    {"role": "system", "content": "准确回答。"},
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好。"},
    {"role": "user", "content": "怎么使用？"},
]


def assert_adapter_shapes() -> None:
    providers = {item["code"] for item in provider_catalog()}
    assert providers == {
        "openai_chat_completions",
        "openai_responses",
        "anthropic_messages",
        "google_gemini",
        "ollama_chat",
    }
    assert normalize_provider("gemini_generate_content") == "google_gemini"

    openai_chat = build_request(
        "openai_chat_completions", "https://example.com/v1", "vendor/model", "secret-chat",
        {"messages": MESSAGES, "max_tokens": 40, "temperature": 0.2},
    )
    assert openai_chat["url"] == "https://example.com/v1/chat/completions"
    assert openai_chat["headers"]["Authorization"] == "Bearer secret-chat"
    assert openai_chat["payload"]["messages"] == MESSAGES
    assert openai_chat["payload"]["messages"][-1] == {"role": "user", "content": "怎么使用？"}
    assert parse_response("openai_chat_completions", {"choices": [{"message": {"content": "openai-chat"}}]})["choices"][0]["message"]["content"] == "openai-chat"

    openai_responses = build_request(
        "openai_responses", "https://api.openai.com/v1", "gpt-test", "secret-responses",
        {"messages": MESSAGES, "max_tokens": 40, "reasoning_effort": "low"},
    )
    assert openai_responses["url"] == "https://api.openai.com/v1/responses"
    assert openai_responses["headers"]["Authorization"] == "Bearer secret-responses"
    assert openai_responses["payload"]["input"] == MESSAGES
    assert openai_responses["payload"]["input"][-1] == {"role": "user", "content": "怎么使用？"}
    assert openai_responses["payload"]["max_output_tokens"] == 40
    assert openai_responses["payload"]["reasoning"] == {"effort": "low"}
    assert parse_response("openai_responses", {"output_text": "openai-responses"})["choices"][0]["message"]["content"] == "openai-responses"
    raw_responses = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "raw-response"}],
            }
        ]
    }
    assert parse_response("openai_responses", raw_responses)["choices"][0]["message"]["content"] == "raw-response"
    compatible_responses = {"choices": [{"message": {"role": "assistant", "content": "compatible-response"}}]}
    assert parse_response("openai_responses", compatible_responses)["choices"][0]["message"]["content"] == "compatible-response"

    anthropic = build_request(
        "anthropic_messages", "https://api.anthropic.com/v1", "claude-model", "secret",
        {"messages": MESSAGES, "max_tokens": 40},
    )
    assert anthropic["url"] == "https://api.anthropic.com/v1/messages"
    assert anthropic["headers"]["x-api-key"] == "secret"
    assert anthropic["headers"]["anthropic-version"] == "2023-06-01"
    assert anthropic["payload"]["system"] == "准确回答。"
    assert all(item["role"] != "system" for item in anthropic["payload"]["messages"])
    assert anthropic["payload"]["messages"][-1] == {"role": "user", "content": "怎么使用？"}
    assert parse_response("anthropic_messages", {"content": [{"type": "text", "text": "anthropic"}]})["choices"][0]["message"]["content"] == "anthropic"

    gemini = build_request(
        "google_gemini", "https://generativelanguage.googleapis.com/v1beta", "gemini-test", "secret",
        {"messages": MESSAGES, "max_tokens": 40},
    )
    assert gemini["url"].endswith("/v1beta/models/gemini-test:generateContent")
    assert gemini["headers"]["x-goog-api-key"] == "secret"
    assert gemini["payload"]["systemInstruction"]["parts"][0]["text"] == "准确回答。"
    assert {item["role"] for item in gemini["payload"]["contents"]} == {"user", "model"}
    assert gemini["payload"]["contents"][-1] == {"role": "user", "parts": [{"text": "怎么使用？"}]}
    assert parse_response("google_gemini", {"candidates": [{"content": {"parts": [{"text": "gemini"}]}}]})["choices"][0]["message"]["content"] == "gemini"

    ollama = build_request(
        "ollama_chat", "https://ollama.example/api", "qwen2.5:7b", "",
        {"messages": MESSAGES, "max_tokens": 40},
    )
    assert ollama["url"] == "https://ollama.example/api/chat"
    assert "Authorization" not in ollama["headers"]
    assert ollama["payload"]["options"]["num_predict"] == 40
    assert ollama["payload"]["messages"][-1] == {"role": "user", "content": "怎么使用？"}
    assert parse_response("ollama_chat", {"message": {"role": "assistant", "content": "ollama"}})["choices"][0]["message"]["content"] == "ollama"
    decision_text = '{"decision":"no_reply","reply":"","reason_code":"needs_owner"}'
    assert parse_response(
        "openai_responses", {"output_text": decision_text}
    )["choices"][0]["message"]["content"] == decision_text

    assert normalize_base_url("https://api.openai.com", "openai_responses") == "https://api.openai.com/v1"
    assert normalize_base_url("https://api.anthropic.com", "anthropic_messages") == "https://api.anthropic.com/v1"
    assert normalize_base_url("https://generativelanguage.googleapis.com", "google_gemini") == "https://generativelanguage.googleapis.com/v1beta"
    assert normalize_base_url("https://ollama.example", "ollama_chat") == "https://ollama.example/api"
    for provider, url in (
        ("openai_chat_completions", "https://example.com/v1/chat/completions"),
        ("openai_responses", "https://example.com/v1/responses"),
        ("anthropic_messages", "https://example.com/v1/messages"),
        ("google_gemini", "https://example.com/v1beta/models/x:generateContent"),
        ("ollama_chat", "https://example.com/api/chat"),
    ):
        try:
            normalize_base_url(url, provider)
            raise AssertionError("complete upstream endpoint must be rejected")
        except ProviderAdapterError as error:
            assert error.code == "address_unsafe"

    for provider, response in (
        ("openai_chat_completions", {"choices": []}),
        ("openai_responses", {"output": []}),
        ("anthropic_messages", {"content": []}),
        ("google_gemini", {"candidates": []}),
        ("ollama_chat", {"message": {"content": ""}}),
    ):
        try:
            parse_response(provider, response)
            raise AssertionError("empty provider responses must be rejected")
        except ProviderAdapterError as error:
            assert error.code == "response_invalid"


def assert_service_provider_flow() -> None:
    calls: list[dict] = []

    def resolver(_host, port, type=None):
        return [(None, None, None, None, ("93.184.216.34", port))]

    def requester(url, api_key, payload, headers):
        calls.append({"url": url, "api_key": api_key, "payload": payload, "headers": headers})
        if url.endswith("/chat/completions"):
            return {"choices": [{"message": {"content": "openai-chat"}}]}
        if url.endswith("/responses"):
            return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "openai-responses"}]}]}
        if url.endswith("/messages"):
            return {"content": [{"type": "text", "text": "anthropic"}]}
        if url.endswith(":generateContent"):
            return {"candidates": [{"content": {"parts": [{"text": "gemini"}]}}]}
        if url.endswith("/chat"):
            return {"message": {"role": "assistant", "content": "ollama"}}
        raise AssertionError(url)

    env = {"SAAS_AI_MASTER_KEY": base64.b64encode(b"p" * 32).decode("ascii")}
    with tempfile.TemporaryDirectory(prefix="xianyu-provider-contract-") as root:
        service = AIService(Path(root) / "tenants", environ=env, resolver=resolver, requester=requester)
        formats = [
            ("openai_chat_completions", "https://openai-chat.example/v1", "openai/model", "key-openai-chat"),
            ("openai_responses", "https://openai-responses.example/v1", "responses-model", "key-openai-responses"),
            ("anthropic_messages", "https://anthropic.example/v1", "claude-model", "key-anthropic"),
            ("google_gemini", "https://gemini.example/v1beta", "gemini-model", "key-gemini"),
            ("ollama_chat", "https://ollama.example/api", "local-model", ""),
        ]
        for index, (provider, base_url, model, api_key) in enumerate(formats, 1):
            scope = (11, index, f"shop-{index}")
            tested = service.test_connection(
                *scope,
                provider=provider,
                base_url=base_url,
                model=model,
                api_key=api_key,
                expected_revision=0,
            )
            saved = service.save_connection(
                *scope,
                provider=provider,
                base_url=base_url,
                model=model,
                api_key=api_key,
                verification_token=tested["verification_token"],
                expected_revision=0,
            )
            assert saved["provider"] == provider
            assert saved["api_key_configured"] is bool(api_key)
            status, body = service.forward_payload(
                *scope,
                {"messages": [{"role": "user", "content": "你好"}], "max_tokens": 20},
            )
            assert status == 200 and b'"choices"' in body

        first = service.get_connection(11, 1, "shop-1")
        try:
            service.test_connection(
                11,
                1,
                "shop-1",
                provider="anthropic_messages",
                base_url="https://anthropic.example/v1",
                model="claude-model",
                api_key="",
                expected_revision=first["revision"],
            )
            raise AssertionError("provider changes must not reuse the previous provider key")
        except AIServiceError as error:
            assert error.code == "connection_unconfigured"

        candidate = service.test_connection(
            11,
            1,
            "shop-1",
            provider="anthropic_messages",
            base_url="https://anthropic.example/v1",
            model="claude-model",
            api_key="replacement-key",
            expected_revision=first["revision"],
        )
        try:
            service.save_connection(
                11,
                1,
                "shop-1",
                provider="openai_chat_completions",
                base_url="https://openai-chat.example/v1",
                model="openai/model",
                api_key="replacement-key",
                verification_token=candidate["verification_token"],
                expected_revision=first["revision"],
            )
            raise AssertionError("verification tokens must bind the provider format")
        except AIServiceError as error:
            assert error.code == "verification_invalid"

        responses_calls = [call for call in calls if call["url"].endswith("/responses")]
        assert responses_calls[0]["payload"]["max_output_tokens"] == 256
        assert responses_calls[-1]["payload"]["max_output_tokens"] == 20
        assert all("input" in call["payload"] for call in responses_calls)
        assert any(call["headers"].get("x-api-key") == "key-anthropic" for call in calls)
        assert any(call["headers"].get("x-goog-api-key") == "key-gemini" for call in calls)
        assert any(call["url"].endswith("/api/chat") and not call["api_key"] for call in calls)


def assert_pinned_real_network_path() -> None:
    """The non-mock path must connect to the exact address it validated."""
    dns_calls: list[tuple[str, int]] = []
    socket_calls: list[tuple] = []
    request_calls: list[dict] = []
    tls_calls: list[str] = []
    response_state = {
        "status": 200,
        "body": json.dumps({"choices": [{"message": {"content": "pinned"}}]}).encode(),
    }

    def resolver(host, port, type=None):
        assert type == socket.SOCK_STREAM
        dns_calls.append((host, port))
        if host == "127.0.0.1":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    class FakeSocket:
        def __init__(self, family, socktype, protocol):
            self.family = family
            self.socktype = socktype
            self.protocol = protocol
            self.closed = False

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, sockaddr):
            socket_calls.append(sockaddr)

        def close(self):
            self.closed = True

    class FakeTLSContext:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED

        def wrap_socket(self, raw_socket, server_hostname):
            assert self.check_hostname is True
            assert self.verify_mode == ssl.CERT_REQUIRED
            tls_calls.append(server_hostname)
            return raw_socket

    class FakeResponse:
        @property
        def status(self):
            return response_state["status"]

        def read(self, maximum):
            assert maximum == ai_service_module.MAX_RESPONSE_BYTES + 1
            return response_state["body"]

    class FakeConnection:
        def __init__(self, host, port, timeout):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.sock = None

        def request(self, method, target, body=None, headers=None):
            assert self.sock is not None
            request_calls.append({
                "host": self.host,
                "port": self.port,
                "method": method,
                "target": target,
                "body": body,
                "headers": dict(headers or {}),
            })

        def getresponse(self):
            return FakeResponse()

        def close(self):
            if self.sock is not None:
                self.sock.close()

    env = {
        "SAAS_AI_MASTER_KEY": base64.b64encode(b"n" * 32).decode("ascii"),
        "SAAS_AI_ALLOW_OLLAMA_LOCAL": "1",
    }
    with tempfile.TemporaryDirectory(prefix="xianyu-provider-pinned-") as root:
        service = AIService(Path(root) / "tenants", environ=env, resolver=resolver)
        with (
            patch.object(ai_service_module.socket, "socket", FakeSocket),
            patch.object(ai_service_module.ssl, "create_default_context", return_value=FakeTLSContext()),
            patch.object(ai_service_module.http.client, "HTTPConnection", FakeConnection),
        ):
            https_result = service._request_json(
                "openai_chat_completions",
                "https://provider.example/v1",
                "model",
                "key",
                {"messages": [{"role": "user", "content": "hello"}]},
            )
            assert https_result["choices"][0]["message"]["content"] == "pinned"
            assert dns_calls == [("provider.example", 443)]
            assert socket_calls == [("93.184.216.34", 443)]
            assert tls_calls == ["provider.example"]
            assert request_calls[-1]["headers"]["Host"] == "provider.example"
            assert request_calls[-1]["target"] == "/v1/chat/completions"

            response_state["body"] = json.dumps(
                {"message": {"role": "assistant", "content": "local-pinned"}}
            ).encode()
            http_result = service._request_json(
                "ollama_chat",
                "http://127.0.0.1:11434/api",
                "model",
                "",
                {"messages": [{"role": "user", "content": "hello"}]},
            )
            assert http_result["choices"][0]["message"]["content"] == "local-pinned"
            assert dns_calls[-1] == ("127.0.0.1", 11434)
            assert socket_calls[-1] == ("127.0.0.1", 11434)
            assert tls_calls == ["provider.example"]
            assert request_calls[-1]["headers"]["Host"] == "127.0.0.1:11434"

            response_state.update(status=302, body=b"{}")
            try:
                service._request_json(
                    "openai_chat_completions",
                    "https://provider.example/v1",
                    "model",
                    "key",
                    {"messages": [{"role": "user", "content": "redirect"}]},
                )
                raise AssertionError("redirects must not be followed")
            except AIServiceError as error:
                assert error.code == "address_unsafe"

            response_state.update(
                status=200,
                body=b"x" * (ai_service_module.MAX_RESPONSE_BYTES + 1),
            )
            try:
                service._request_json(
                    "openai_chat_completions",
                    "https://provider.example/v1",
                    "model",
                    "key",
                    {"messages": [{"role": "user", "content": "large"}]},
                )
                raise AssertionError("oversized responses must be rejected")
            except AIServiceError as error:
                assert error.code == "response_invalid"

        # One resolver call per real request proves no validation/connection
        # re-resolution occurs. Every connect used the address from that call.
        assert dns_calls == [
            ("provider.example", 443),
            ("127.0.0.1", 11434),
            ("provider.example", 443),
            ("provider.example", 443),
        ]
        assert len(socket_calls) == len(dns_calls)


def main() -> None:
    assert_adapter_shapes()
    assert_service_provider_flow()
    assert_pinned_real_network_path()
    print("AI provider adapter contract: five formats, pinned DNS connections and unified responses passed")


if __name__ == "__main__":
    main()
