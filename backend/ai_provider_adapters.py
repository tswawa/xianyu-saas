"""Provider adapters for account-scoped AI customer-service requests.

The worker speaks one bounded internal chat protocol.  This module converts that
protocol to a small, explicit set of upstream provider formats and normalizes
successful responses back to an OpenAI-shaped response for the existing worker.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any


_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_MODEL_LENGTH = 200
_MAX_CONTENT_LENGTH = 16_000


class ProviderAdapterError(RuntimeError):
    def __init__(self, code: str = "invalid_payload", message: str = "AI 接口配置无效"):
        self.code = str(code or "invalid_payload")[:80]
        super().__init__(message)


@dataclass(frozen=True)
class ProviderSpec:
    code: str
    label: str
    default_path: str
    endpoint: str
    auth: str
    requires_api_key: bool = True


_PROVIDER_SPECS = {
    "openai_chat_completions": ProviderSpec(
        "openai_chat_completions", "OpenAI / 兼容接口", "/v1", "chat_completions", "bearer"
    ),
    "openai_responses": ProviderSpec(
        "openai_responses", "OpenAI Responses", "/v1", "responses", "bearer"
    ),
    "anthropic_messages": ProviderSpec(
        "anthropic_messages", "Anthropic Claude", "/v1", "messages", "anthropic"
    ),
    "google_gemini": ProviderSpec(
        "google_gemini", "Google Gemini", "/v1beta", "generate_content", "google"
    ),
    "ollama_chat": ProviderSpec(
        "ollama_chat", "Ollama 本地服务", "/api", "ollama_chat", "optional_bearer", False
    ),
}

_PROVIDER_ALIASES = {
    "openai": "openai_chat_completions",
    "openai-compatible": "openai_chat_completions",
    "openai_compatible": "openai_chat_completions",
    "chat_completions": "openai_chat_completions",
    "responses": "openai_responses",
    "anthropic": "anthropic_messages",
    "claude": "anthropic_messages",
    "gemini": "google_gemini",
    "gemini_generate_content": "google_gemini",
    "google": "google_gemini",
    "ollama": "ollama_chat",
}


def normalize_provider(value: Any) -> str:
    text = str(value or "openai_chat_completions").strip().lower()
    text = _PROVIDER_ALIASES.get(text, text)
    if text not in _PROVIDER_SPECS:
        raise ProviderAdapterError("invalid_payload", "暂不支持这种 AI 接口格式")
    return text


def provider_spec(provider: Any) -> ProviderSpec:
    return _PROVIDER_SPECS[normalize_provider(provider)]


def provider_catalog() -> list[dict[str, Any]]:
    return [
        {
            "code": spec.code,
            "label": spec.label,
            "requires_api_key": spec.requires_api_key,
        }
        for spec in _PROVIDER_SPECS.values()
    ]


def _clean_text(value: Any, limit: int = _MAX_CONTENT_LENGTH) -> str:
    if not isinstance(value, str):
        raise ProviderAdapterError("invalid_payload", "AI 消息内容无效")
    text = value.strip()
    if not text or len(text) > limit or _TEXT_CONTROL_RE.search(text):
        raise ProviderAdapterError("invalid_payload", "AI 消息内容无效")
    return text


def _clean_model(value: Any, *, path_component: bool = False) -> str:
    model = _clean_text(value, _MAX_MODEL_LENGTH)
    if path_component and any(char in model for char in "/?#"):
        raise ProviderAdapterError("invalid_payload", "模型名无效")
    return model


def _messages(payload: dict) -> list[dict[str, str]]:
    raw = payload.get("messages")
    if not isinstance(raw, list) or not raw:
        raise ProviderAdapterError("invalid_payload", "messages 数量无效")
    result: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant"}:
            raise ProviderAdapterError("invalid_payload", "消息角色无效")
        result.append({"role": item["role"], "content": _clean_text(item.get("content"))})
    return result


def _number(payload: dict, key: str, default: int | float | None = None) -> int | float | None:
    value = payload.get(key, default)
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


def _max_tokens(payload: dict, default: int = 1024) -> int:
    value = _number(payload, "max_tokens", default)
    if not isinstance(value, (int, float)):
        return default
    return max(1, min(int(value), 4096))


def normalize_base_url(value: Any, provider: Any, *, allow_http: bool = False) -> str:
    spec = provider_spec(provider)
    if not isinstance(value, str):
        raise ProviderAdapterError("address_unsafe", "连接地址不符合安全要求")
    text = value.strip()
    if len(text) > 2048 or _URL_CONTROL_RE.search(text):
        raise ProviderAdapterError("address_unsafe", "连接地址不符合安全要求")
    try:
        parsed = urllib.parse.urlsplit(text)
        port = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise ProviderAdapterError("address_unsafe", "连接地址不符合安全要求") from exc
    scheme = parsed.scheme.lower()
    if scheme != "https" and not (scheme == "http" and allow_http):
        raise ProviderAdapterError("address_unsafe", "连接地址不符合安全要求")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderAdapterError("address_unsafe", "连接地址不符合安全要求")
    if not parsed.hostname or port == 0:
        raise ProviderAdapterError("address_unsafe", "连接地址不符合安全要求")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ProviderAdapterError("address_unsafe", "连接地址不符合安全要求") from exc
    if len(host) > 253:
        raise ProviderAdapterError("address_unsafe", "连接地址不符合安全要求")
    path = re.sub(r"/{2,}", "/", parsed.path or "").rstrip("/")
    endpoint_markers = {
        "chat_completions": "/chat/completions",
        "responses": "/responses",
        "messages": "/messages",
        "generate_content": ":generatecontent",
        "ollama_chat": "/chat",
    }
    marker = endpoint_markers[spec.endpoint]
    if path.lower().endswith(marker) or marker in path.lower():
        raise ProviderAdapterError("address_unsafe", "请填写服务基础地址，不要填写完整接口路径")
    if not path:
        path = spec.default_path
    elif any(part in {".", ".."} for part in path.split("/")):
        raise ProviderAdapterError("address_unsafe", "连接地址不符合安全要求")
    if len(path) > 512:
        raise ProviderAdapterError("address_unsafe", "连接地址不符合安全要求")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))


def _endpoint(base_url: str, spec: ProviderSpec, model: str) -> str:
    base = base_url.rstrip("/")
    if spec.endpoint == "chat_completions":
        return base + "/chat/completions"
    if spec.endpoint == "responses":
        return base + "/responses"
    if spec.endpoint == "messages":
        return base + "/messages"
    if spec.endpoint == "generate_content":
        return base + "/models/" + urllib.parse.quote(model, safe="-_.") + ":generateContent"
    if spec.endpoint == "ollama_chat":
        return base + "/chat"
    raise ProviderAdapterError("invalid_payload", "AI 接口格式无效")


def _headers(spec: ProviderSpec, api_key: str) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if spec.auth == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif spec.auth == "optional_bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif spec.auth == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif spec.auth == "google":
        headers["x-goog-api-key"] = api_key
    return headers


def _system_and_conversation(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    system = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
    conversation = [item for item in messages if item["role"] != "system"]
    if not conversation:
        conversation = [{"role": "user", "content": "Connection test."}]
    return system, conversation


def build_request(provider: Any, base_url: str, model: str, api_key: str, payload: dict) -> dict[str, Any]:
    spec = provider_spec(provider)
    clean_model = _clean_model(model, path_component=spec.endpoint == "generate_content")
    messages = _messages(payload)
    if not isinstance(api_key, str):
        raise ProviderAdapterError("invalid_payload", "AI 凭据无效")
    endpoint = _endpoint(base_url, spec, clean_model)
    headers = _headers(spec, api_key)
    if spec.endpoint == "chat_completions":
        upstream = {
            "model": clean_model,
            "stream": False,
            "messages": messages,
            "max_tokens": _max_tokens(payload, 512),
        }
        for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
            value = _number(payload, key)
            if value is not None:
                upstream[key] = value
        if isinstance(payload.get("reasoning_effort"), str) and payload["reasoning_effort"] in {"low", "medium", "high"}:
            upstream["reasoning_effort"] = payload["reasoning_effort"]
    elif spec.endpoint == "responses":
        upstream = {
            "model": clean_model,
            "stream": False,
            "input": messages,
            "max_output_tokens": _max_tokens(payload, 512),
        }
        for key in ("temperature", "top_p"):
            value = _number(payload, key)
            if value is not None:
                upstream[key] = value
        if isinstance(payload.get("reasoning_effort"), str) and payload["reasoning_effort"] in {"low", "medium", "high"}:
            upstream["reasoning"] = {"effort": payload["reasoning_effort"]}
    elif spec.endpoint == "messages":
        system, conversation = _system_and_conversation(messages)
        upstream = {
            "model": clean_model,
            "max_tokens": _max_tokens(payload),
            "messages": conversation,
        }
        if system:
            upstream["system"] = system
        for key in ("temperature", "top_p"):
            value = _number(payload, key)
            if value is not None:
                upstream[key] = value
    elif spec.endpoint == "generate_content":
        system, conversation = _system_and_conversation(messages)
        contents = [
            {
                "role": "model" if item["role"] == "assistant" else "user",
                "parts": [{"text": item["content"]}],
            }
            for item in conversation
        ]
        upstream = {"contents": contents}
        if system:
            upstream["systemInstruction"] = {"parts": [{"text": system}]}
        generation_config = {"maxOutputTokens": _max_tokens(payload)}
        for source, target in (("temperature", "temperature"), ("top_p", "topP")):
            value = _number(payload, source)
            if value is not None:
                generation_config[target] = value
        upstream["generationConfig"] = generation_config
    elif spec.endpoint == "ollama_chat":
        upstream = {
            "model": clean_model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": _max_tokens(payload)},
        }
        for source, target in (("temperature", "temperature"), ("top_p", "top_p")):
            value = _number(payload, source)
            if value is not None:
                upstream["options"][target] = value
    else:  # pragma: no cover - registry prevents this
        raise ProviderAdapterError("invalid_payload", "AI 接口格式无效")
    return {"url": endpoint, "headers": headers, "payload": upstream}


def _text_from_content(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {None, "text", "output_text"}:
                text = block.get("text")
                if isinstance(text, dict):
                    text = text.get("value")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if parts:
            return "\n".join(parts)
    raise ProviderAdapterError("response_invalid", "模型响应格式无效")


def _responses_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    output = response.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "output_text":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
                continue
            content = item.get("content")
            try:
                parts.append(_text_from_content(content))
            except ProviderAdapterError:
                continue
        if parts:
            return "\n".join(parts)

    # Some OpenAI-compatible gateways accept /responses requests but return a
    # Chat Completions-shaped success body. Keep the request protocol strict,
    # while accepting this common response compatibility shape.
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            return _text_from_content(message.get("content"))
    raise ProviderAdapterError("response_invalid", "模型响应格式无效")


def _normalized(text: str) -> dict[str, Any]:
    clean = _clean_text(text, 32_000)
    return {
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": clean}, "finish_reason": "stop"}],
    }


def parse_response(provider: Any, response: Any) -> dict[str, Any]:
    spec = provider_spec(provider)
    if not isinstance(response, dict):
        raise ProviderAdapterError("response_invalid", "模型响应格式无效")
    if spec.endpoint == "chat_completions":
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderAdapterError("response_invalid", "模型响应格式无效")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ProviderAdapterError("response_invalid", "模型响应格式无效")
        return _normalized(_text_from_content(message.get("content")))
    if spec.endpoint == "responses":
        return _normalized(_responses_text(response))
    if spec.endpoint == "messages":
        return _normalized(_text_from_content(response.get("content")))
    if spec.endpoint == "generate_content":
        candidates = response.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise ProviderAdapterError("response_invalid", "模型响应格式无效")
        content = candidates[0].get("content")
        if not isinstance(content, dict):
            raise ProviderAdapterError("response_invalid", "模型响应格式无效")
        return _normalized(_text_from_content(content.get("parts")))
    if spec.endpoint == "ollama_chat":
        message = response.get("message")
        if not isinstance(message, dict):
            raise ProviderAdapterError("response_invalid", "模型响应格式无效")
        return _normalized(_text_from_content(message.get("content")))
    raise ProviderAdapterError("response_invalid", "模型响应格式无效")


def is_api_key_required(provider: Any) -> bool:
    return provider_spec(provider).requires_api_key


__all__ = [
    "ProviderAdapterError",
    "ProviderSpec",
    "build_request",
    "is_api_key_required",
    "normalize_base_url",
    "normalize_provider",
    "parse_response",
    "provider_catalog",
    "provider_spec",
]
