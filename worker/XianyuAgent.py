import json
import os
import re
from urllib.parse import urlparse

import requests


class LLMServiceError(RuntimeError):
    """内部 AI 服务当前无法生成可用回复。"""


class LLMTimeoutError(LLMServiceError):
    """内部 AI 服务调用超时。"""


class LLMRateLimitError(LLMServiceError):
    """内部 AI 服务暂时限流。"""


class LLMConfigurationError(LLMServiceError):
    """内部 AI 客户端配置无效。"""


class LLMNotReadyError(LLMConfigurationError):
    """内部 AI 或自动回复状态已不允许发送。"""


class LLMResponseFormatError(LLMServiceError):
    """内部 AI 服务返回了无效业务协议。"""


class LLMEmptyResponseError(LLMResponseFormatError):
    """内部 AI 服务选择回复，但没有返回可发送文本。"""


class XianyuReplyBot:
    """账号作用域的内部 AI 回复接口同步客户端。"""

    MAX_HISTORY_MESSAGES = 30
    MAX_HISTORY_CHARS = 24_000
    MAX_ITEM_CONTEXT_CHARS = 16_000
    MAX_REPLY_CHARS = 4096
    MAX_RECENT_REPLIES = 8
    MAX_RESPONSE_BYTES = 64 * 1024
    READY_TIMEOUT = (2.0, 5.0)
    ALLOWED_DECISIONS = frozenset({"reply", "handoff", "no_reply"})
    SAFE_REASON_RE = re.compile(r"^[a-z0-9_:-]{1,80}$")
    SAFE_SOURCE_RE = re.compile(r"^[a-z0-9_:-]{1,80}$")
    LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

    def __init__(self, session=None):
        token = os.getenv("API_KEY", "").strip()
        if not token:
            raise LLMConfigurationError("缺少内部 AI 短期令牌")
        self.endpoint = self._reply_endpoint(os.getenv("MODEL_BASE_URL", ""))
        self.ready_endpoint = self._ready_endpoint(self.endpoint)
        self.account_key = os.getenv("XIAN_YU_ACCOUNT_KEY", "default").strip() or "default"
        if (
            len(self.account_key) > 80
            or not self.account_key.isascii()
            or any(not (character.isalnum() or character in "-_") for character in self.account_key)
        ):
            raise LLMConfigurationError("店铺作用域无效")
        self.session = session or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Shop-Account": self.account_key,
        }
        self.last_intent = None
        self.last_reason_code = ""
        self.last_sources = []
        self.last_knowledge_status = ""

    @classmethod
    def _reply_endpoint(cls, raw_base_url):
        base_url = str(raw_base_url or "http://127.0.0.1:8096/internal/v1").strip().rstrip("/")
        try:
            parsed = urlparse(base_url)
        except ValueError as exc:
            raise LLMConfigurationError("内部 AI 地址无效") from exc
        if parsed.scheme != "http" or parsed.hostname not in cls.LOOPBACK_HOSTS:
            raise LLMConfigurationError("内部 AI 地址必须是本机 HTTP 地址")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise LLMConfigurationError("内部 AI 地址无效")
        if parsed.path.rstrip("/") == "/internal/v1/ai/reply":
            return base_url
        if parsed.path.rstrip("/") != "/internal/v1":
            raise LLMConfigurationError("内部 AI 地址必须指向 /internal/v1")
        return base_url + "/ai/reply"

    @classmethod
    def _ready_endpoint(cls, reply_endpoint):
        parsed = urlparse(reply_endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in cls.LOOPBACK_HOSTS
            or parsed.path.rstrip("/") != "/internal/v1/ai/reply"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise LLMConfigurationError("内部 AI 回复地址无效")
        return parsed._replace(path="/internal/v1/ai/ready").geturl()

    @staticmethod
    def _clean_text(value, limit, *, required=False):
        if not isinstance(value, str):
            if required:
                raise LLMConfigurationError("AI 请求文本字段无效")
            return ""
        clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value).strip()
        if required and not clean:
            raise LLMConfigurationError("AI 请求缺少当前问题")
        return clean[:limit]

    @classmethod
    def _clean_history(cls, context):
        if context is None:
            return []
        if not isinstance(context, (list, tuple)):
            raise LLMConfigurationError("AI 对话历史无效")
        result = []
        total = 0
        for message in context[-cls.MAX_HISTORY_MESSAGES :]:
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                continue
            content = cls._clean_text(message.get("content"), 8192)
            if not content:
                continue
            remaining = cls.MAX_HISTORY_CHARS - total
            if remaining <= 0:
                break
            content = content[:remaining]
            result.append({"role": message["role"], "content": content})
            total += len(content)
        return result

    @classmethod
    def _clean_item_context(cls, item_context, legacy_item_desc):
        if item_context is None:
            if isinstance(legacy_item_desc, dict):
                item_context = legacy_item_desc
            elif isinstance(legacy_item_desc, str) and legacy_item_desc.strip():
                item_context = {"description": cls._clean_text(legacy_item_desc, 8000)}
            else:
                item_context = {}
        if not isinstance(item_context, dict):
            raise LLMConfigurationError("AI 商品事实无效")
        try:
            encoded = json.dumps(item_context, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise LLMConfigurationError("AI 商品事实无法序列化") from exc
        if len(encoded) > cls.MAX_ITEM_CONTEXT_CHARS:
            raise LLMConfigurationError("AI 商品事实超过限制")
        return item_context

    @classmethod
    def _clean_recent_replies(cls, replies):
        if replies is None:
            return []
        if not isinstance(replies, (list, tuple)):
            raise LLMConfigurationError("近期回复列表无效")
        result = []
        for reply in replies[-cls.MAX_RECENT_REPLIES :]:
            clean = cls._clean_text(reply, cls.MAX_REPLY_CHARS)
            if clean and clean not in result:
                result.append(clean)
        return result

    @classmethod
    def _safe_reason(cls, value):
        if not isinstance(value, str):
            raise LLMResponseFormatError("内部 AI reason_code 无效")
        clean = value.strip().lower()
        if not cls.SAFE_REASON_RE.fullmatch(clean):
            raise LLMResponseFormatError("内部 AI reason_code 无效")
        return clean

    @classmethod
    def _parse_response(cls, payload):
        if not isinstance(payload, dict):
            raise LLMResponseFormatError("内部 AI 响应必须是对象")
        decision = payload.get("decision")
        if decision not in cls.ALLOWED_DECISIONS:
            raise LLMResponseFormatError("内部 AI decision 无效")
        reply = payload.get("reply")
        if not isinstance(reply, str):
            raise LLMResponseFormatError("内部 AI reply 无效")
        reply = reply.strip()
        if len(reply) > cls.MAX_REPLY_CHARS:
            raise LLMResponseFormatError("内部 AI reply 超过限制")
        if decision == "reply" and not reply:
            raise LLMEmptyResponseError("内部 AI 返回空回复")
        if decision != "reply" and reply:
            raise LLMResponseFormatError("非回复决策不得携带回复正文")
        reason_code = cls._safe_reason(payload.get("reason_code"))
        sources = payload.get("sources")
        if not isinstance(sources, list) or len(sources) > 16:
            raise LLMResponseFormatError("内部 AI sources 无效")
        clean_sources = []
        for source in sources:
            if not isinstance(source, str) or not cls.SAFE_SOURCE_RE.fullmatch(source.strip().lower()):
                raise LLMResponseFormatError("内部 AI source 无效")
            clean = source.strip().lower()
            if clean not in clean_sources:
                clean_sources.append(clean)
        knowledge_status = payload.get("knowledge_status")
        if (
            not isinstance(knowledge_status, str)
            or not knowledge_status.strip()
            or len(knowledge_status.strip()) > 80
        ):
            raise LLMResponseFormatError("内部 AI knowledge_status 无效")
        config_revision = payload.get("config_revision")
        if (
            isinstance(config_revision, bool)
            or not isinstance(config_revision, int)
            or config_revision < 0
        ):
            raise LLMResponseFormatError("内部 AI config_revision 无效")
        return {
            "decision": decision,
            "reply": reply,
            "reason_code": reason_code,
            "sources": clean_sources,
            "knowledge_status": knowledge_status.strip(),
            "config_revision": config_revision,
        }

    def ensure_ready(self, expected_config_revision):
        """Fail closed unless the exact generated configuration is still current."""
        if (
            isinstance(expected_config_revision, bool)
            or not isinstance(expected_config_revision, int)
            or expected_config_revision < 0
        ):
            raise LLMConfigurationError("内部 AI 期望配置版本无效")
        try:
            response = self.session.post(
                self.ready_endpoint,
                headers=self.headers,
                json={"expected_config_revision": expected_config_revision},
                timeout=self.READY_TIMEOUT,
            )
        except requests.Timeout as exc:
            raise LLMTimeoutError("内部 AI 状态复核超时") from exc
        except requests.RequestException as exc:
            raise LLMServiceError("内部 AI 状态复核失败") from exc

        if response.status_code == 429:
            raise LLMRateLimitError("内部 AI 状态复核限流")
        if response.status_code in {408, 504}:
            raise LLMTimeoutError("内部 AI 状态复核超时")
        if response.status_code in {401, 403}:
            raise LLMConfigurationError("内部 AI 凭据或店铺作用域无效")
        if response.status_code == 409:
            raise LLMNotReadyError("内部 AI 或自动回复已停用")
        if response.status_code >= 500:
            raise LLMServiceError("内部 AI 状态服务暂不可用")
        if response.status_code < 200 or response.status_code >= 300:
            raise LLMConfigurationError("内部 AI 状态复核被拒绝")
        content = getattr(response, "content", b"")
        if isinstance(content, bytes) and len(content) > self.MAX_RESPONSE_BYTES:
            raise LLMResponseFormatError("内部 AI 状态响应过大")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMResponseFormatError("内部 AI 状态响应不是 JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"ok", "config_revision"}:
            raise LLMResponseFormatError("内部 AI 状态响应结构无效")
        revision = payload.get("config_revision")
        if (
            payload.get("ok") is not True
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise LLMResponseFormatError("内部 AI 状态响应字段无效")
        if revision != expected_config_revision:
            raise LLMNotReadyError("内部 AI 配置版本已变化")
        return revision

    def generate_reply_result(
        self,
        user_msg,
        item_desc="",
        context=None,
        system_context="",
        *,
        item_id="",
        item_context=None,
        recent_assistant_replies=None,
    ):
        """Return one reply decision with its request-local configuration revision."""
        del system_context
        message = self._clean_text(user_msg, 8192, required=True)
        selected_item_id = str(item_id or "").strip()
        if selected_item_id and (
            len(selected_item_id) > 64
            or not selected_item_id.isascii()
            or not selected_item_id.isdigit()
        ):
            raise LLMConfigurationError("AI item_id 无效")
        payload = {
            "message": message,
            "history": self._clean_history(context),
            "item_id": selected_item_id,
            "item_context": self._clean_item_context(item_context, item_desc),
            "recent_assistant_replies": self._clean_recent_replies(recent_assistant_replies),
        }
        try:
            response = self.session.post(
                self.endpoint,
                headers=self.headers,
                json=payload,
                timeout=(5.0, 45.0),
            )
        except requests.Timeout as exc:
            raise LLMTimeoutError("内部 AI 请求超时") from exc
        except requests.RequestException as exc:
            raise LLMServiceError("内部 AI 请求失败") from exc

        if response.status_code == 429:
            raise LLMRateLimitError("内部 AI 服务限流")
        if response.status_code in {408, 504}:
            raise LLMTimeoutError("内部 AI 请求超时")
        if response.status_code in {401, 403}:
            raise LLMConfigurationError("内部 AI 凭据或店铺作用域无效")
        if response.status_code >= 500:
            raise LLMServiceError("内部 AI 服务暂不可用")
        if response.status_code < 200 or response.status_code >= 300:
            raise LLMConfigurationError("内部 AI 请求被拒绝")
        content = getattr(response, "content", b"")
        if isinstance(content, bytes) and len(content) > self.MAX_RESPONSE_BYTES:
            raise LLMResponseFormatError("内部 AI 响应过大")
        try:
            result = self._parse_response(response.json())
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMResponseFormatError("内部 AI 响应不是 JSON") from exc
        return result

    def generate_reply(self, *args, **kwargs):
        """Backward-compatible text-only entry for non-worker callers."""
        result = self.generate_reply_result(*args, **kwargs)
        self.last_intent = result["decision"]
        self.last_reason_code = result["reason_code"]
        self.last_sources = result["sources"]
        self.last_knowledge_status = result["knowledge_status"]
        return result["reply"]


__all__ = [
    "LLMConfigurationError",
    "LLMEmptyResponseError",
    "LLMNotReadyError",
    "LLMRateLimitError",
    "LLMResponseFormatError",
    "LLMServiceError",
    "LLMTimeoutError",
    "XianyuReplyBot",
]
