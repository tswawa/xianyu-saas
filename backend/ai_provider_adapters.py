"""Provider adapters for account-scoped AI customer-service requests.

The worker speaks one bounded internal chat protocol.  This module converts that
protocol to a small, explicit set of upstream provider formats and normalizes
successful responses back to an OpenAI-shaped response for the existing worker.
"""

from __future__ import annotations

import copy
import json
import math
import re
import uuid
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
        # DeepSeek V4 defaults to thinking. A tiny connection probe must not
        # exhaust its output budget before producing the actual reply.
        if clean_model.lower() in {"deepseek-v4-flash", "deepseek-v4-pro"}:
            thinking = payload.get("thinking")
            if isinstance(thinking, dict) and thinking.get("type") in {"enabled", "disabled"}:
                upstream["thinking"] = {"type": thinking["type"]}
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


# Agent is a separate protocol: no customer-service text/token/history limits.
# These shape/depth checks are serialization safety, not a context budget.
_AGENT_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_AGENT_JSON_DEPTH = 96


def agent_json_loads(value: str | bytes) -> Any:
    """Decode protocol JSON only; never extract JSON from model prose."""
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def constant(_value):
        raise ValueError("non-finite JSON number")

    try:
        result = json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
        _agent_json(result, "response_invalid")
        return result
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise ProviderAdapterError("response_invalid", "模型返回的协议 JSON 无效或含重复字段") from exc


def _agent_json(value: Any, code: str = "invalid_payload", depth: int = 0) -> Any:
    if depth > _AGENT_JSON_DEPTH:
        raise ProviderAdapterError(code, "JSON 嵌套超过传输安全深度，未裁剪内容")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        for item in value:
            _agent_json(item, code, depth + 1)
        return value
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _agent_json(item, code, depth + 1)
        return value
    raise ProviderAdapterError(code, "JSON 字段类型无效")


def _agent_text(value: Any, code: str = "invalid_payload") -> str:
    if not isinstance(value, str) or _TEXT_CONTROL_RE.search(value):
        raise ProviderAdapterError(code, "Agent 消息文本类型无效")
    return value  # Preserve even leading/trailing whitespace and empty tool turns.


def _agent_name(value: Any, code: str = "invalid_payload") -> str:
    if not isinstance(value, str) or not _AGENT_NAME_RE.fullmatch(value):
        raise ProviderAdapterError(code, "工具名格式无效")
    return value


def _agent_id(value: Any, code: str = "invalid_payload") -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or _URL_CONTROL_RE.search(value):
        raise ProviderAdapterError(code, "工具调用标识无效")
    return value


def _schema_ref(schema: dict, root: dict) -> dict:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ProviderAdapterError("tool_schema_invalid", "工具 schema 仅允许本地 JSON 引用")
    target = root
    try:
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
    except (KeyError, TypeError):
        raise ProviderAdapterError("tool_schema_invalid", "工具 schema 引用不存在") from None
    if not isinstance(target, dict):
        raise ProviderAdapterError("tool_schema_invalid", "工具 schema 引用无效")
    return target


def _agent_schema(schema: Any, root: dict | None = None) -> dict:
    """Validate the registry's supported JSON Schema subset; fail closed on others."""
    if not isinstance(schema, dict):
        raise ProviderAdapterError("tool_schema_invalid", "工具参数必须是 JSON Schema 对象")
    root = schema if root is None else root
    allowed = {
        "type", "properties", "required", "additionalProperties", "items", "enum", "const",
        "anyOf", "oneOf", "allOf", "$ref", "$defs", "definitions", "description", "title",
        "default", "examples", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "multipleOf", "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
        "minProperties", "maxProperties", "$schema",
    }
    if set(schema) - allowed:
        raise ProviderAdapterError("tool_schema_invalid", "工具 schema 含本站尚不支持的校验字段")
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if not isinstance(kinds, list) or not all(isinstance(kind, str) and kind in {
        "object", "array", "string", "integer", "number", "boolean", "null"
    } for kind in kinds):
        raise ProviderAdapterError("tool_schema_invalid", "工具 schema 类型无效")
    if "$ref" in schema:
        _schema_ref(schema, root)
    if "object" in kinds or "properties" in schema:
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (not isinstance(properties, dict) or not isinstance(required, list)
                or not all(isinstance(key, str) and key in properties for key in required)
                or len(set(required)) != len(required)
                or schema.get("additionalProperties", False) is not False):
            raise ProviderAdapterError("tool_schema_invalid", "工具对象必须显式列出参数且不允许额外字段")
        for child in properties.values():
            _agent_schema(child, root)
    if "array" in kinds and "items" not in schema:
        raise ProviderAdapterError("tool_schema_invalid", "工具数组缺少 items schema")
    if "items" in schema:
        _agent_schema(schema["items"], root)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                raise ProviderAdapterError("tool_schema_invalid", "工具 schema 分支无效")
            for child in schema[key]:
                _agent_schema(child, root)
    for key in ("$defs", "definitions"):
        if key in schema:
            if not isinstance(schema[key], dict):
                raise ProviderAdapterError("tool_schema_invalid", "工具 schema 定义无效")
            for child in schema[key].values():
                _agent_schema(child, root)
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ProviderAdapterError("tool_schema_invalid", "工具 enum 无效")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        if key in schema and (isinstance(schema[key], bool) or not isinstance(schema[key], (int, float))
                              or not math.isfinite(schema[key]) or (key == "multipleOf" and schema[key] <= 0)):
            raise ProviderAdapterError("tool_schema_invalid", "工具数值约束无效")
    for key in ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ProviderAdapterError("tool_schema_invalid", "工具长度约束无效")
    if "uniqueItems" in schema and type(schema["uniqueItems"]) is not bool:
        raise ProviderAdapterError("tool_schema_invalid", "工具数组唯一性约束无效")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (TypeError, re.error):
            raise ProviderAdapterError("tool_schema_invalid", "工具字符串约束无效") from None
    return schema


def _schema_matches(value: Any, schema: dict, root: dict, depth: int = 0) -> bool:
    if depth > _AGENT_JSON_DEPTH:
        raise ProviderAdapterError("tool_arguments_invalid", "工具参数嵌套超过安全深度")
    if "$ref" in schema and not _schema_matches(value, _schema_ref(schema, root), root, depth + 1):
        return False
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            matches = sum(_schema_matches(value, child, root, depth + 1) for child in schema[key])
            if ((key == "anyOf" and not matches) or (key == "oneOf" and matches != 1)
                    or (key == "allOf" and matches != len(schema[key]))):
                return False
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    checks = {
        "null": value is None, "boolean": type(value) is bool, "string": isinstance(value, str),
        "integer": type(value) is int, "number": type(value) in (int, float),
        "object": isinstance(value, dict), "array": isinstance(value, list),
    }
    if kinds and not any(checks[kind] for kind in kinds):
        return False
    # JSON equality must not confuse true with 1.
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if "enum" in schema and not any(encoded == json.dumps(item, sort_keys=True, ensure_ascii=False, allow_nan=False)
                                     for item in schema["enum"]):
        return False
    if "const" in schema and encoded != json.dumps(schema["const"], sort_keys=True, ensure_ascii=False, allow_nan=False):
        return False
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if "properties" in schema or "object" in kinds:
            if set(value) - set(properties) or set(schema.get("required", [])) - set(value):
                return False
            if any(not _schema_matches(item, properties[key], root, depth + 1) for key, item in value.items()):
                return False
        if len(value) < schema.get("minProperties", 0) or len(value) > schema.get("maxProperties", math.inf):
            return False
    if isinstance(value, list):
        if "items" in schema and any(not _schema_matches(item, schema["items"], root, depth + 1) for item in value):
            return False
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            return False
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value}) != len(value):
            return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False
    if type(value) in (int, float):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            return False
        if value <= schema.get("exclusiveMinimum", -math.inf) or value >= schema.get("exclusiveMaximum", math.inf):
            return False
        if "multipleOf" in schema and not math.isclose(value / schema["multipleOf"], round(value / schema["multipleOf"]), abs_tol=1e-9):
            return False
    return True


def _agent_tools(tools: Any) -> dict[str, dict]:
    if not isinstance(tools, list):
        raise ProviderAdapterError("invalid_payload", "Agent tools 必须是函数定义数组")
    _agent_json(tools)
    result = {}
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {"name", "description", "parameters"}:
            raise ProviderAdapterError("invalid_payload", "Agent 仅接受自定义 JSON Schema 函数，不接受内置工具")
        name = _agent_name(tool["name"])
        if name in result:
            raise ProviderAdapterError("invalid_payload", "工具名称重复")
        _agent_text(tool["description"])
        schema = _agent_schema(tool["parameters"])
        if schema.get("type") != "object":
            raise ProviderAdapterError("tool_schema_invalid", "工具参数根 schema 必须为 object")
        result[name] = tool
    return result


def _wire_schema(schema: dict, provider: str) -> dict:
    result = copy.deepcopy(schema)
    for key in ("properties", "$defs", "definitions"):
        if key in result:
            result[key] = {name: _wire_schema(child, provider) for name, child in result[key].items()}
    for key in ("anyOf", "oneOf", "allOf"):
        if key in result:
            result[key] = [_wire_schema(child, provider) for child in result[key]]
    if "items" in result:
        result["items"] = _wire_schema(result["items"], provider)
    kinds = result.get("type", [])
    if kinds == "object" or "object" in kinds or "properties" in result:
        result["additionalProperties"] = False
        result.setdefault("properties", {})
        if provider in {"openai_chat_completions", "openai_responses"}:
            required = result.get("required", [])
            for name, child in result["properties"].items():
                if name not in required:
                    result["properties"][name] = {"anyOf": [child, {"type": "null"}]}
            result["required"] = list(result["properties"])
    # Claude strict accepts optional fields, unlike OpenAI. Constraints outside
    # its grammar subset remain enforced locally and are described to the model.
    if provider == "anthropic_messages":
        constraints = {}
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                    "minLength", "maxLength", "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties"):
            if key in result:
                constraints[key] = result.pop(key)
        if constraints:
            result["description"] = str(result.get("description", "")) + " Local validation: " + json.dumps(constraints)
    for key in ("$schema", "default", "examples"):
        result.pop(key, None)
    return result


def _optional_nulls(value: Any, schema: dict, root: dict, depth: int = 0) -> Any:
    """Undo only OpenAI's wire-only nullable placeholders, not explicit nulls."""
    if depth > _AGENT_JSON_DEPTH:
        raise ProviderAdapterError("tool_arguments_invalid", "工具参数嵌套超过安全深度")
    if "$ref" in schema:
        value = _optional_nulls(value, _schema_ref(schema, root), root, depth + 1)
    for key in ("anyOf", "oneOf"):
        for branch in schema.get(key, []):
            candidate = _optional_nulls(value, branch, root, depth + 1)
            if _schema_matches(candidate, branch, root):
                return candidate
    if isinstance(value, dict) and "properties" in schema:
        result = dict(value)
        for key, child in schema["properties"].items():
            if key not in result:
                continue
            if result[key] is None and key not in schema.get("required", []) and not _schema_matches(None, child, root):
                del result[key]
            else:
                result[key] = _optional_nulls(result[key], child, root, depth + 1)
        return result
    if isinstance(value, list) and "items" in schema:
        return [_optional_nulls(item, schema["items"], root, depth + 1) for item in value]
    return value


def _agent_calls(calls: Any, code: str = "invalid_payload") -> list[dict]:
    if not isinstance(calls, list):
        raise ProviderAdapterError(code, "工具调用必须为数组")
    seen = set()
    for call in calls:
        if not isinstance(call, dict) or set(call) != {"id", "name", "arguments"}:
            raise ProviderAdapterError(code, "工具调用结构无效")
        call_id = _agent_id(call["id"], code)
        _agent_name(call["name"], code)
        if call_id in seen or not isinstance(call["arguments"], dict):
            raise ProviderAdapterError(code, "工具调用标识重复或参数不是对象")
        _agent_json(call["arguments"], code)
        seen.add(call_id)
    return calls


def _agent_messages(messages: Any) -> list[dict]:
    if not isinstance(messages, list) or not messages:
        raise ProviderAdapterError("invalid_payload", "Agent messages 必须为非空数组")
    _agent_json(messages)
    result, pending, tool_results, seen = [], {}, {}, set()
    for item in messages:
        if not isinstance(item, dict):
            raise ProviderAdapterError("invalid_payload", "Agent 消息结构无效")
        role = item.get("role")
        if role == "tool":
            if set(item) != {"role", "tool_call_id", "name", "content"} or not isinstance(item["content"], dict):
                raise ProviderAdapterError("invalid_payload", "工具结果必须是规范对象")
            call_id = _agent_id(item["tool_call_id"])
            if call_id not in pending or call_id in tool_results or item["name"] != pending[call_id]:
                raise ProviderAdapterError("invalid_payload", "工具结果没有对应调用、名称不匹配或重复")
            tool_results[call_id] = copy.deepcopy(item)
            if len(tool_results) == len(pending):
                # Ollama without native IDs and Gemini legacy calls correlate
                # repeated names by order; reorder complete batches by call ID.
                result.extend(tool_results[key] for key in pending)
                pending, tool_results = {}, {}
            continue
        if pending:
            raise ProviderAdapterError("invalid_payload", "历史工具调用缺少完整结果，未删除历史")
        if role not in {"user", "system", "assistant"}:
            raise ProviderAdapterError("invalid_payload", "Agent 消息角色无效")
        allowed = {"role", "content", "tool_calls", "provider_data"} if role == "assistant" else {"role", "content"}
        if set(item) - allowed:
            raise ProviderAdapterError("invalid_payload", "Agent 消息含额外字段")
        clean = {"role": role, "content": _agent_text(item.get("content"))}
        if role == "assistant":
            clean["tool_calls"] = copy.deepcopy(_agent_calls(item.get("tool_calls", [])))
            data = item.get("provider_data", {})
            if not isinstance(data, dict):
                raise ProviderAdapterError("invalid_payload", "Provider 续传数据无效")
            clean["provider_data"] = copy.deepcopy(data)
            for call in clean["tool_calls"]:
                if call["id"] in seen:
                    raise ProviderAdapterError("invalid_payload", "历史工具调用标识重复")
                seen.add(call["id"])
                pending[call["id"]] = call["name"]
        result.append(clean)
    if pending:
        raise ProviderAdapterError("invalid_payload", "历史工具调用缺少完整结果，未删除历史")
    return result


def _agent_blocks(value: Any) -> list[dict]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ProviderAdapterError("response_invalid", "模型响应块结构无效")
    return value


def _agent_response_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _agent_text(value, "response_invalid")
    texts = []
    for block in _agent_blocks(value):
        if block.get("type") not in {"text", "output_text", "refusal"}:
            raise ProviderAdapterError("response_invalid", "模型返回不支持的消息内容类型")
        texts.append(_agent_text(block.get("refusal") if block.get("type") == "refusal" else block.get("text"), "response_invalid"))
    return "".join(texts)


def parse_agent_response(provider: Any, response: Any, tools: list | None = None, *, _call_ids: list | None = None) -> dict:
    """Return an assistant turn, never a command extracted from text.

    provider_data is private conversation state, not user-visible reasoning.
    Only the exact function-call fields below can become executable requests.
    """
    provider = normalize_provider(provider)
    _agent_json(response, "response_invalid")
    if not isinstance(response, dict):
        raise ProviderAdapterError("response_invalid", "模型响应必须为对象")
    catalog = _agent_tools(tools) if tools is not None else None
    calls, text, data = [], "", {"provider": provider}

    def add(call_id, name, arguments, *, encoded=False, allow_missing_id=False):
        if encoded:
            if not isinstance(arguments, str):
                raise ProviderAdapterError("tool_arguments_invalid", "原生工具参数必须为 JSON 字符串")
            try:
                arguments = agent_json_loads(arguments)
            except ProviderAdapterError as exc:
                raise ProviderAdapterError("tool_arguments_invalid", "原生工具参数不是有效 JSON 对象或含重复字段") from exc
        if not isinstance(arguments, dict):
            raise ProviderAdapterError("tool_arguments_invalid", "原生工具参数必须为对象")
        _agent_json(arguments, "tool_arguments_invalid")
        name = _agent_name(name, "tool_arguments_invalid")
        if allow_missing_id and not call_id:
            call_id = (_call_ids[len(calls)] if _call_ids is not None and len(_call_ids) > len(calls)
                       else "call_" + uuid.uuid4().hex)
        calls.append({"id": _agent_id(call_id, "response_invalid"), "name": name, "arguments": copy.deepcopy(arguments)})

    if provider in {"openai_chat_completions", "ollama_chat"}:
        if provider == "openai_chat_completions":
            choices = _agent_blocks(response.get("choices"))
            if not choices:
                raise ProviderAdapterError("response_invalid", "模型未返回候选消息")
            choice = choices[0]
            if "finish_reason" in choice and choice["finish_reason"] not in ("stop", "tool_calls"):
                raise ProviderAdapterError("generation_incomplete", "模型生成未完成（输出预算、内容过滤或仍在生成），未执行不完整工具")
            message = choice.get("message")
        else:
            if "done" in response and response["done"] is not True:
                raise ProviderAdapterError("generation_incomplete", "Ollama 响应尚未完成，未执行不完整工具")
            if response.get("done_reason") == "length":
                raise ProviderAdapterError("generation_incomplete", "模型输出达到生成预算，未执行不完整工具")
            message = response.get("message")
        if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
            raise ProviderAdapterError("response_invalid", "模型助手消息无效")
        if message.get("function_call"):
            raise ProviderAdapterError("response_invalid", "Agent 不支持旧式无调用标识的 function_call 协议")
        text = _agent_response_text(message.get("content"))
        if not text and message.get("refusal"):
            text = _agent_text(message["refusal"], "response_invalid")
        native = {"role": "assistant", "content": message.get("content")}
        if native["content"] is None and provider == "ollama_chat":
            native["content"] = ""
        for key in (("reasoning_content", "reasoning_details", "refusal") if provider == "openai_chat_completions" else ("thinking",)):
            if key in message:
                native[key] = copy.deepcopy(message[key])
        if "tool_calls" in message:
            native["tool_calls"] = []
            for call in _agent_blocks(message["tool_calls"]):
                if call.get("type", "function") != "function" or not isinstance(call.get("function"), dict):
                    raise ProviderAdapterError("response_invalid", "Agent 仅接受原生自定义函数调用")
                function = call["function"]
                add(call.get("id"), function.get("name"), function.get("arguments"),
                    encoded=provider == "openai_chat_completions", allow_missing_id=provider == "ollama_chat")
                saved = {"function": {"name": function["name"], "arguments": copy.deepcopy(function["arguments"])}}
                if provider == "openai_chat_completions":
                    saved.update(type="function", id=call["id"])
                else:
                    for key in ("id", "type"):
                        if key in call:
                            saved[key] = call[key]
                    if "index" in function:
                        if type(function["index"]) is not int or function["index"] < 0:
                            raise ProviderAdapterError("response_invalid", "Ollama 工具调用序号无效")
                        saved["function"]["index"] = function["index"]
                native["tool_calls"].append(saved)
        data["message"] = native
        if provider == "ollama_chat":
            data["call_ids"] = [call["id"] for call in calls]
    elif provider == "openai_responses":
        if "status" in response and response["status"] != "completed":
            raise ProviderAdapterError("generation_incomplete", "Responses 生成未完成，未执行不完整工具")
        output = _agent_blocks(response.get("output"))
        texts, native = [], []
        for item in output:
            kind = item.get("type")
            if item.get("status") not in (None, "completed"):
                raise ProviderAdapterError("generation_incomplete", "Responses 响应项尚未完成，未执行不完整工具")
            if kind == "function_call":
                add(item.get("call_id"), item.get("name"), item.get("arguments"), encoded=True)
                native.append({key: copy.deepcopy(item[key]) for key in ("type", "id", "call_id", "name", "arguments", "status") if key in item})
            elif kind == "message":
                if item.get("role", "assistant") != "assistant":
                    raise ProviderAdapterError("response_invalid", "Responses 助手消息角色无效")
                texts.append(_agent_response_text(item.get("content")))
                native.append({key: copy.deepcopy(item[key]) for key in ("type", "id", "role", "content", "status", "phase") if key in item})
            elif kind == "reasoning":
                native.append({key: copy.deepcopy(item[key]) for key in ("type", "id", "summary", "content", "encrypted_content", "status") if key in item})
            else:
                raise ProviderAdapterError("response_invalid", "Responses 返回未注册的工具或内容类型")
        text, data["output"] = "".join(texts), native
    elif provider == "anthropic_messages":
        if "stop_reason" in response and response["stop_reason"] not in ("end_turn", "tool_use", "stop_sequence", "refusal"):
            raise ProviderAdapterError("generation_incomplete", "Claude 生成未完成，未执行不完整工具")
        blocks = _agent_blocks(response.get("content"))
        texts = []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                texts.append(_agent_text(block.get("text"), "response_invalid"))
            elif kind == "tool_use":
                add(block.get("id"), block.get("name"), block.get("input"))
            elif kind not in {"thinking", "redacted_thinking"}:
                raise ProviderAdapterError("response_invalid", "Claude 返回未注册的工具或内容类型")
        text, data["content"] = "".join(texts), copy.deepcopy(blocks)
    else:  # generateContent, deliberately NOT the Interactions API.
        candidates = _agent_blocks(response.get("candidates"))
        if not candidates:
            raise ProviderAdapterError("response_invalid", "Gemini 未返回候选消息")
        candidate = candidates[0]
        if "finishReason" in candidate and candidate["finishReason"] != "STOP":
            raise ProviderAdapterError("generation_incomplete", "Gemini 生成未完成（输出预算、工具格式或安全过滤）")
        content = candidate.get("content")
        if not isinstance(content, dict) or content.get("role", "model") != "model":
            raise ProviderAdapterError("response_invalid", "Gemini 模型消息无效")
        texts = []
        for part in _agent_blocks(content.get("parts")):
            if set(part) - {"text", "functionCall", "thought", "thoughtSignature", "partMetadata"}:
                raise ProviderAdapterError("response_invalid", "Gemini 返回未注册的内置工具或媒体")
            if "text" in part:
                value = _agent_text(part["text"], "response_invalid")
                if part.get("thought") is not True:
                    texts.append(value)
            if "functionCall" in part:
                call = part["functionCall"]
                if not isinstance(call, dict):
                    raise ProviderAdapterError("response_invalid", "Gemini functionCall 无效")
                add(call.get("id"), call.get("name"), call.get("args", {}), allow_missing_id=True)
        text = "".join(texts)
        data.update(content=copy.deepcopy(content), call_ids=[call["id"] for call in calls])
    _agent_calls(calls, "response_invalid")
    if catalog is not None:
        for call in calls:
            if call["name"] not in catalog:
                raise ProviderAdapterError("tool_not_allowed", "模型请求的工具不在本轮允许的函数列表内")
            schema = catalog[call["name"]]["parameters"]
            if provider in {"openai_chat_completions", "openai_responses"}:
                call["arguments"] = _optional_nulls(call["arguments"], schema, schema)
            if not _schema_matches(call["arguments"], schema, schema):
                raise ProviderAdapterError("tool_arguments_invalid", "模型原生工具参数未通过本地严格 schema 校验")
    if not text and not calls:
        raise ProviderAdapterError("response_invalid", "模型未返回可见文本或原生工具调用")
    return {"role": "assistant", "content": text, "tool_calls": calls, "provider_data": data}


def _same_agent_arguments(native: Any, canonical: Any) -> bool:
    if isinstance(native, dict) and isinstance(canonical, dict):
        return (not (set(canonical) - set(native))
                and all(_same_agent_arguments(value, canonical[key]) if key in canonical else value is None
                        for key, value in native.items()))
    if isinstance(native, list) and isinstance(canonical, list):
        return len(native) == len(canonical) and all(_same_agent_arguments(a, b) for a, b in zip(native, canonical))
    return type(native) is type(canonical) and native == canonical


def _agent_replay(message: dict, provider: str) -> dict:
    data = message.get("provider_data", {})
    if not data:
        return {}
    if data.get("provider") != provider:
        raise ProviderAdapterError("continuation_mismatch", "历史 Provider 续传数据与当前连接不一致，请新建对话")
    fixtures = {
        "openai_chat_completions": {"choices": [{"message": data.get("message")}]},
        "openai_responses": {"output": data.get("output")},
        "anthropic_messages": {"content": data.get("content")},
        "google_gemini": {"candidates": [{"content": data.get("content")}]},
        "ollama_chat": {"message": data.get("message")},
    }
    ids = data.get("call_ids")
    if provider in {"google_gemini", "ollama_chat"} and ids != [call["id"] for call in message["tool_calls"]]:
        raise ProviderAdapterError("continuation_mismatch", "工具续传标识与服务端历史不一致")
    parsed = parse_agent_response(provider, fixtures[provider], _call_ids=ids)
    expected, actual = message["tool_calls"], parsed["tool_calls"]
    if (parsed["content"] != message["content"] or len(expected) != len(actual)
            or any(a["id"] != b["id"] or a["name"] != b["name"] or not _same_agent_arguments(b["arguments"], a["arguments"])
                   for a, b in zip(expected, actual))):
        raise ProviderAdapterError("continuation_mismatch", "Provider 续传消息与服务端规范历史不一致")
    return parsed["provider_data"]


def build_agent_request(provider: Any, base_url: str, model: str, api_key: str,
                        messages: list, tools: list, *, output_tokens: int | None = None) -> dict:
    """Build a native, functions-only request with complete canonical history.

    output_tokens is a generation budget, never a context/history quota. Most
    providers need no explicit cap; Claude requires max_tokens (default 16384).
    """
    provider = normalize_provider(provider)
    spec = provider_spec(provider)
    model = _clean_model(model, path_component=spec.endpoint == "generate_content")
    if not isinstance(api_key, str):
        raise ProviderAdapterError("invalid_payload", "AI 凭据无效")
    if output_tokens is not None and (type(output_tokens) is not int or output_tokens <= 0):
        raise ProviderAdapterError("invalid_payload", "Agent 输出生成预算必须为正整数")
    catalog = _agent_tools(tools)
    history = _agent_messages(messages)
    upstream = {"model": model, "stream": False}
    conversation, system = [], []
    native_ids = {}
    dump = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def append_parts(role, parts, *, merge=False, field="content"):
        if merge and conversation and conversation[-1]["role"] == role:
            conversation[-1][field].extend(parts)
        else:
            conversation.append({"role": role, field: parts})

    for item in history:
        role, content = item["role"], item["content"]
        calls = item.get("tool_calls", [])
        data = _agent_replay(item, provider) if role == "assistant" else {}
        if provider in {"openai_chat_completions", "ollama_chat"}:
            if role == "assistant":
                native = data.get("message") or {"role": "assistant", "content": content}
                if not data and calls:
                    native["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                        "name": call["name"], "arguments": dump(call["arguments"]) if provider == "openai_chat_completions" else call["arguments"]}}
                        for call in calls]
                if provider == "ollama_chat":
                    for call, saved in zip(calls, native.get("tool_calls", [])):
                        native_ids[call["id"]] = saved.get("id")
                conversation.append(native)
            elif role == "tool":
                native = {"role": "tool", "content": dump(content)}
                if provider == "openai_chat_completions":
                    native["tool_call_id"] = item["tool_call_id"]
                else:
                    native["tool_name"] = item["name"]
                    if native_ids.get(item["tool_call_id"]):
                        native["tool_call_id"] = native_ids[item["tool_call_id"]]
                conversation.append(native)
            else:
                conversation.append({"role": role, "content": content})
        elif provider == "openai_responses":
            if role == "assistant":
                if data:
                    conversation.extend(data["output"])
                else:
                    if content:
                        conversation.append({"role": "assistant", "content": content})
                    conversation.extend({"type": "function_call", "call_id": call["id"], "name": call["name"], "arguments": dump(call["arguments"])} for call in calls)
            elif role == "tool":
                conversation.append({"type": "function_call_output", "call_id": item["tool_call_id"], "output": dump(content)})
            else:
                conversation.append({"role": role, "content": content})
        elif role == "system":
            system.append({"text": content})
        elif provider == "anthropic_messages":
            if role == "assistant":
                parts = data.get("content") if data else ([{"type": "text", "text": content}] if content else [])
                if not data:
                    parts.extend({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]} for call in calls)
                append_parts("assistant", parts, merge=True)
            elif role == "tool":
                append_parts("user", [{"type": "tool_result", "tool_use_id": item["tool_call_id"], "content": dump(content)}], merge=True)
            else:
                append_parts("user", [{"type": "text", "text": content}], merge=True)
        else:  # Google generateContent parts, including untouched signatures.
            if role == "assistant":
                parts = data["content"]["parts"] if data else ([{"text": content}] if content else [])
                if not data:
                    parts.extend({"functionCall": {"id": call["id"], "name": call["name"], "args": call["arguments"]}} for call in calls)
                raw_calls = [part["functionCall"] for part in parts if "functionCall" in part]
                for call, raw in zip(calls, raw_calls):
                    native_ids[call["id"]] = raw.get("id")
                append_parts("model", parts, field="parts")
            elif role == "tool":
                function_response = {"name": item["name"], "response": content}
                if native_ids.get(item["tool_call_id"]):
                    function_response["id"] = native_ids[item["tool_call_id"]]
                append_parts("user", [{"functionResponse": function_response}], merge=True, field="parts")
            else:
                append_parts("user", [{"text": content}], field="parts")
    declarations = []
    for tool in catalog.values():
        function = {"name": tool["name"], "description": tool["description"], "parameters": _wire_schema(tool["parameters"], provider)}
        if provider in {"openai_chat_completions", "openai_responses"}:
            function["strict"] = True
        if provider in {"openai_chat_completions", "ollama_chat"}:
            declarations.append({"type": "function", "function": function})
        elif provider == "openai_responses":
            declarations.append({"type": "function", **function})
        elif provider == "anthropic_messages":
            declarations.append({"name": tool["name"], "description": tool["description"], "input_schema": function["parameters"], "strict": True})
        else:
            declarations.append({"name": tool["name"], "description": tool["description"], "parametersJsonSchema": function["parameters"]})
    if provider == "openai_responses":
        upstream.update(input=conversation, store=False, include=["reasoning.encrypted_content"], truncation="disabled")
        if output_tokens is not None:
            upstream["max_output_tokens"] = output_tokens
    elif provider == "google_gemini":
        upstream = {"contents": conversation}
        if system:
            upstream["systemInstruction"] = {"parts": system}
        if output_tokens is not None:
            upstream["generationConfig"] = {"maxOutputTokens": output_tokens}
    else:
        upstream["messages"] = conversation
        if provider == "anthropic_messages":
            upstream["max_tokens"] = output_tokens if output_tokens is not None else 16_384
            if system:
                upstream["system"] = [{"type": "text", **part} for part in system]
        elif output_tokens is not None:
            if provider == "ollama_chat":
                upstream["options"] = {"num_predict": output_tokens}
            else:
                upstream["max_completion_tokens"] = output_tokens
    if declarations:
        upstream["tools"] = [{"functionDeclarations": declarations}] if provider == "google_gemini" else declarations
        if provider == "google_gemini":
            upstream["toolConfig"] = {"functionCallingConfig": {"mode": "VALIDATED"}}
    return {"url": _endpoint(base_url, spec, model), "headers": _headers(spec, api_key), "payload": upstream}


def is_api_key_required(provider: Any) -> bool:
    return provider_spec(provider).requires_api_key


__all__ = [
    "ProviderAdapterError",
    "ProviderSpec",
    "build_request",
    "build_agent_request",
    "parse_agent_response",
    "agent_json_loads",
    "is_api_key_required",
    "normalize_base_url",
    "normalize_provider",
    "parse_response",
    "provider_catalog",
    "provider_spec",
]
