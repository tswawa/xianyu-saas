#!/usr/bin/env python3
"""Offline contracts for v2 content-driven AI customer service."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ai_customer_service import (  # noqa: E402
    AIService,
    AIServiceError,
    empty_store_config,
    ensure_development_master_key,
    facts_fingerprint,
    identity_fingerprint,
    knowledge_has_content,
    normalize_knowledge,
    normalize_store_config,
    store_config_has_content,
)


def write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o600)


def assert_error(code: str, callback) -> None:
    try:
        callback()
        raise AssertionError(f"expected {code}")
    except AIServiceError as error:
        assert error.code == code, (error.code, str(error))


def assert_development_master_key() -> None:
    with tempfile.TemporaryDirectory(prefix="xianyu-ai-dev-key-") as root:
        base = Path(root)
        key_path = base / ".local" / "ai-master-key"
        tenants = base / ".local" / "tenants"
        generated = ensure_development_master_key(key_path, tenants)
        assert len(base64.b64decode(generated, validate=True)) == 32
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert ensure_development_master_key(key_path, tenants) == generated
        os.chmod(key_path, 0o644)
        assert_error("credential_store_unavailable", lambda: ensure_development_master_key(key_path, tenants))

    with tempfile.TemporaryDirectory(prefix="xianyu-ai-existing-secret-") as root:
        base = Path(root)
        tenants = base / "tenants"
        write_private_json(tenants / "account" / "ai_connection_secret.json", {"ciphertext": "fixture"})
        assert_error(
            "credential_store_unavailable",
            lambda: ensure_development_master_key(base / "ai-master-key", tenants),
        )


def assert_sandbox_preview() -> None:
    """All provider traffic is fake; preview must be read-only even on failure."""
    with tempfile.TemporaryDirectory(prefix="xianyu-ai-sandbox-") as root:
        tenants = Path(root) / "tenants"
        requests = []
        response = {"decision": "reply", "reply": "我是小喵客服，可以帮你解答问题。", "reason_code": "answered"}
        provider_failure = {"code": ""}
        during_request = {"callback": None}

        def requester(_url, _key, payload):
            requests.append(payload)
            if during_request["callback"]:
                during_request["callback"]()
            if provider_failure["code"]:
                raise AIServiceError(provider_failure["code"], 503)
            return {"choices": [{"message": {"content": json.dumps(response, ensure_ascii=False)}}]}

        service = AIService(
            tenants,
            environ={"SAAS_AI_MASTER_KEY": base64.b64encode(b"s" * 32).decode("ascii")},
            resolver=lambda _host, port, type=None: [(None, None, None, None, ("93.184.216.34", port))],
            requester=requester,
        )
        scope = (17, 31, "sandbox-a")
        other_scope = (17, 32, "sandbox-b")
        metadata = {
            "version": 2, "provider": "openai_chat_completions", "base_url": "https://example.com/v1",
            "model": "fixture-model", "api_key_configured": True, "connection_status": "verified",
            "revision": 1, "key_revision": 1,
        }
        for selected_scope in (scope, other_scope):
            service._write_root_json(selected_scope, "ai_connection.json", metadata)
            service._write_root_json(
                selected_scope, "ai_connection_secret.json",
                service._encrypt_key(selected_scope, "sandbox-fixture-key-only", 1, 1),
            )
        product = {"id": "1001", "title": "模拟商品甲", "description": "仅用于合同", "price": 12}
        service._write_root_json(scope, "shop_snapshot.json", {"products": [product]})
        service._write_root_json(other_scope, "shop_snapshot.json", {"products": [{**product, "id": "2002"}]})
        service._write_root_json(scope, "session_fixture.json", {"messages": []})

        def snapshot():
            return {
                str(path.relative_to(tenants)): (
                    path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode),
                    path.read_bytes() if path.is_file() else None,
                )
                for path in (tenants, *tenants.rglob("*"))
            }

        def readonly(callback):
            before = snapshot()
            output = io.StringIO()
            handler = logging.StreamHandler(output)
            logging.getLogger().addHandler(handler)
            try:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                        patch.object(service.storage, "ensure_account_dir", side_effect=AssertionError("read created directory")), \
                        patch.object(service, "_write_root_json", side_effect=AssertionError("preview wrote settings")), \
                        patch.object(service, "_write_path_json", side_effect=AssertionError("preview wrote knowledge")):
                    return callback()
            finally:
                logging.getLogger().removeHandler(handler)
                assert snapshot() == before, "preview changed files, directories, modes or mtimes"
                assert output.getvalue() == "", "preview wrote request content to logs/output"

        def preview(**kwargs):
            result = readonly(lambda: service.preview(*scope, buyer_message="你是谁", **kwargs))
            assert result["sent"] is False
            assert "prompt" not in result and "api_key" not in result
            return result

        # No saved store configuration, no product and no enabled bot are required.
        result = preview()
        assert result["decision"] == "reply" and len(requests) == 1
        assert result["config_source"] == "sandbox_defaults" and result["hit_level"] == ""
        assert result["sources"] == [] and "config_revision" not in result
        assert requests[-1]["messages"][-1] == {"role": "user", "content": "你是谁"}
        prompt = json.dumps(requests[-1]["messages"], ensure_ascii=False)
        assert "小喵客服" in prompt and "不得编造店铺" in prompt
        assert "当前没有可用的商品资料，不得猜测具体商品事实" in prompt
        readonly(lambda: assert_error(
            "ai_unconfigured", lambda: service._effective_reply_inputs(
                scope, item_id=None, item_context=None, store_config_override=None,
                knowledge_override=None, require_enabled=False,
            ),
        ))
        for empty in ({}, empty_store_config()):
            result = preview(store_config_override=empty)
            assert result["decision"] == "reply" and result["config_source"] == "draft_override"
            assert result["candidate_revision"] == 0 and "store_content" not in result["sources"]

        draft = {**empty_store_config(), "persona_name": "沙盘小蓝", "store_content": "草稿专属服务说明，不得保存。"}
        history = [{"role": "user", "content": "沙盘前序问题"}, {"role": "assistant", "content": "沙盘前序回答"}]
        result = preview(store_config_override=draft, history=history)
        assert result["config_source"] == "draft_override" and result["candidate_revision"] == 0
        assert "草稿专属服务说明" in requests[-1]["messages"][1]["content"]
        assert "沙盘小蓝" in requests[-1]["messages"][1]["content"]
        assert requests[-1]["messages"][-3:-1] == history
        result = preview(item_id="1001", knowledge_override={"content": "商品草稿专属说明，不得保存。"})
        assert result["knowledge_status"] == "draft_override"
        assert "商品草稿专属说明" in requests[-1]["messages"][2]["content"]
        assert "实时价格：12" in requests[-1]["messages"][2]["content"]
        result = preview(item_id="1001")
        assert result["knowledge_status"] == "unconfigured"
        assert "商品草稿专属说明" not in json.dumps(requests[-1], ensure_ascii=False)

        calls = len(requests)
        for invalid in (
            {"item_id": "2002", "knowledge_override": {"content": "跨店铺伪造"}},
            {"item_id": "9999"},
        ):
            readonly(lambda invalid=invalid: assert_error(
                "item_not_found", lambda: service.preview(*scope, buyer_message="你是谁", **invalid),
            ))
        for invalid in (
            {"store_config_override": {"api_key": "never-allowed"}},
            {"store_config_override": {"store_content": 123}},
            {"item_id": "1001", "knowledge_override": {"content": 123}},
            {"item_id": "../2002"},
        ):
            readonly(lambda invalid=invalid: assert_error(
                "invalid_payload", lambda: service.preview(*scope, buyer_message="你是谁", **invalid),
            ))
        readonly(lambda: assert_error(
            "item_not_found", lambda: service.preview(18, 31, "sandbox-a", buyer_message="你是谁", item_id="1001"),
        ))
        assert len(requests) == calls

        # Production and publishing stay fail-closed for empty/draft/disabled stores.
        for config, action, code in (
            (None, None, "ai_unconfigured"),
            ({**draft, "enabled": True}, "draft", "ai_unconfigured"),
            ({**draft, "enabled": False}, "save", "ai_disabled"),
        ):
            if action:
                service.save_config(*scope, config=config, action=action, expected_revision=service.get_config(*scope)["revision"])
            readonly(lambda code=code: assert_error(code, lambda: service.reply(*scope, message="你是谁")))
            readonly(lambda code=code: assert_error(code, lambda: service.ensure_reply_ready(*scope)))
            assert preview()["decision"] == "reply"
        readonly(lambda: assert_error(
            "invalid_payload", lambda: service.save_config(
                *scope, config=empty_store_config(), action="publish", expected_revision=service.get_config(*scope)["revision"],
            ),
        ))
        result = preview()
        assert result["config_source"] == "published" and result["config_revision"] == 2
        result = preview(store_config_override={})
        assert result["candidate_revision"] == 2 and result["config_source"] == "draft_override"
        assert "草稿专属服务说明" not in requests[-1]["messages"][1]["content"]
        saved_settings = service.get_config(*scope)
        service._write_root_json(scope, "ai_settings.json", {
            **saved_settings,
            "published": {**saved_settings["published"], "config": {**empty_store_config(), "enabled": True}},
        })
        readonly(lambda: assert_error("ai_unconfigured", lambda: service.reply(*scope, message="你是谁")))
        readonly(lambda: assert_error("ai_unconfigured", lambda: service.ensure_reply_ready(*scope)))
        assert preview()["decision"] == "reply"
        service._write_root_json(scope, "ai_settings.json", saved_settings)

        # Published product knowledge is used only when no explicit product draft exists.
        service.save_knowledge(*scope, "1001", knowledge={"content": "已保存商品参考"}, expected_revision=0)
        result = preview(item_id="1001", knowledge_override={"content": "只在当前请求有效"})
        assert result["knowledge_status"] == "draft_override"
        assert "已保存商品参考" not in requests[-1]["messages"][2]["content"]
        result = preview(item_id="1001", knowledge_override={})
        assert "product_content" not in result["sources"]
        result = preview(item_id="1001")
        assert result["knowledge_status"] == "published"
        assert "已保存商品参考" in requests[-1]["messages"][2]["content"]

        # Failures and rejected provider output do not write state or leak text.
        provider_failure["code"] = "service_unavailable"
        result = preview(store_config_override=draft, item_id="1001", knowledge_override={"content": "失败请求草稿"})
        assert result["decision"] == "no_reply" and result["reason_code"] == "service_unavailable"
        provider_failure["code"] = ""
        response["reply"] = "加我微信处理"
        result = preview()
        assert result["decision"] == "no_reply" and result["reason_code"] == "reply_off_platform_contact"
        response["reply"] = "我是小喵客服，可以帮你解答问题。"
        result = preview(recent_assistant_replies=[response["reply"]])
        assert result["reason_code"] == "reply_recent_duplicate"

        # A missing/invalid connection still blocks model traffic on an empty sandbox.
        calls = len(requests)
        result = readonly(lambda: service.preview(19, 33, "no-connection", buyer_message="你是谁"))
        assert result["sent"] is False and result["reason_code"] == "connection_unconfigured"
        assert len(requests) == calls
        for bad_metadata, code in (
            ({**metadata, "connection_status": "unverified"}, "connection_unverified"),
            ({**metadata, "key_revision": 99}, "credential_unavailable"),
        ):
            service._write_root_json(scope, "ai_connection.json", bad_metadata)
            assert preview(store_config_override={})["reason_code"] == code
            assert len(requests) == calls
        service._write_root_json(scope, "ai_connection.json", metadata)

        # A connection generation change discards the late result without writes.
        generation = {"revision": 1}
        during_request["callback"] = lambda: generation.update(revision=2)
        with patch.object(service, "_connection_generation", side_effect=lambda _scope: (generation["revision"],)):
            result = preview(store_config_override={})
        assert result["decision"] == "no_reply" and result["reason_code"] == "revision_conflict"
        during_request["callback"] = None

        # Read-only path resolution must still reject a directory symlink into another shop.
        knowledge_dir = service.storage.account_dir(scope[0], scope[2]) / "ai_knowledge"
        other_knowledge = service.storage.account_dir(other_scope[0], other_scope[2]) / "ai_knowledge"
        other_knowledge.symlink_to(knowledge_dir, target_is_directory=True)
        readonly(lambda: assert_error(
            "credential_unavailable", lambda: service.preview(*other_scope, buyer_message="你是谁", item_id="2002"),
        ))

    print("AI sandbox contract: defaults, drafts, isolation, production guards, read-only failures and generation passed")


def main() -> None:
    assert_sandbox_preview()
    assert_development_master_key()
    defaults = empty_store_config()
    assert defaults["version"] == 2
    assert defaults["enabled"] is False
    assert defaults["persona_preset"] == "catgirl"
    assert defaults["store_content"] == ""
    assert store_config_has_content(defaults) is False
    assert knowledge_has_content({"content": "   "}) is False
    assert knowledge_has_content({"content": "……！？---"}) is False
    assert store_config_has_content({**defaults, "store_content": "仅有实质店铺说明"}) is True

    legacy_store = normalize_store_config({
        "enabled": True,
        "persona_preset": "friendly",
        "persona_name": "旧客服",
        "persona_instruction": "准确回答",
        "tone": "friendly",
        "buyer_address": "亲",
        "reply_length": "short",
        "emoji_level": "low",
        "common_knowledge": "旧店铺主营软件使用指导，只在闲鱼站内沟通。",
        "forbidden_claims": ["永久稳定"],
        "handoff_rules": ["退款争议"],
        "fallback_reply": "旧兜底不会进入运行时",
    })
    assert legacy_store["version"] == 2
    assert legacy_store["store_content"].startswith("旧店铺主营")
    assert legacy_store["common_knowledge"] == legacy_store["store_content"]
    assert legacy_store["fallback_reply"] == ""
    assert legacy_store["forbidden_claims"] == "永久稳定"

    legacy_knowledge = normalize_knowledge({
        "summary": "适用于新手用户",
        "selling_points": ["按说明操作"],
        "specifications": [],
        "price_policy": "价格以页面实时标价为准",
        "delivery_notes": "订单核验后处理",
        "usage_notes": "先阅读商品说明",
        "after_sales": "异常转人工",
        "faqs": [{"question": "怎么用？", "answer": "请按说明操作", "keywords": ["使用"]}],
        "forbidden_answers": [],
        "handoff_rules": ["事实无法确认时"],
        "custom_notes": "",
    })
    assert legacy_knowledge["version"] == 2
    assert legacy_knowledge["imported_from_v1"] is True
    assert "怎么用" in legacy_knowledge["content"]
    assert knowledge_has_content(legacy_knowledge) is True
    assert knowledge_has_content(normalize_knowledge({
        "summary": "", "selling_points": [], "specifications": [], "price_policy": "",
        "delivery_notes": "", "usage_notes": "", "after_sales": "", "faqs": [],
        "forbidden_answers": [], "handoff_rules": [], "custom_notes": "",
    })) is False

    with tempfile.TemporaryDirectory(prefix="xianyu-ai-contract-") as root:
        tenants = Path(root) / "tenants"
        env = {"SAAS_AI_MASTER_KEY": base64.b64encode(b"m" * 32).decode("ascii")}
        requests: list[dict] = []
        reply_output = {
            "value": json.dumps(
                {"decision": "reply", "reply": "实时价格以商品页面为准，使用时请按说明操作。", "reason_code": "answered"},
                ensure_ascii=False,
            )
        }
        extract_output = {
            "value": "适合新手使用。请先阅读商品说明，价格以实时页面为准；异常情况转人工处理。"
        }
        provider_error = {"value": ""}

        def resolver(_host, port, type=None):
            return [(None, None, None, None, ("93.184.216.34", port))]

        def requester(_url, _key, payload):
            requests.append(payload)
            messages = payload.get("messages") if isinstance(payload, dict) else None
            system_text = "\n".join(
                str(message.get("content") or "") for message in (messages or [])
                if isinstance(message, dict) and message.get("role") == "system"
            )
            if "Return only OK" in system_text:
                content = "OK"
            elif "整理为一段" in system_text:
                content = extract_output["value"]
            else:
                if provider_error["value"]:
                    raise AIServiceError(provider_error["value"], 503)
                content = reply_output["value"]
            return {"choices": [{"message": {"content": content}}]}

        service = AIService(tenants, environ=env, resolver=resolver, requester=requester)
        untouched = service.get_config(9, 21, "shop-default")
        assert untouched["revision"] == 0 and untouched["status"] == "unconfigured"
        assert untouched["draft"]["enabled"] is False
        assert untouched["published"] is None and untouched["content_valid"] is False
        legacy_empty_scope = (9, 22, "legacy-empty")
        legacy_empty_settings = Path(service.runtime_paths(*legacy_empty_scope)["settings_file"])
        write_private_json(legacy_empty_settings, {
            "version": 1,
            "revision": 3,
            "status": "published",
            "draft": {
                "enabled": True,
                "persona_preset": "catgirl",
                "persona_name": "小喵客服",
                "persona_instruction": "默认人格",
                "tone": "friendly",
                "buyer_address": "亲",
                "reply_length": "short",
                "emoji_level": "low",
                "common_knowledge": "",
                "forbidden_claims": [],
                "handoff_rules": [],
                "fallback_reply": "默认兜底",
            },
            "published": {
                "revision": 3,
                "published_at": "2026-08-01T00:00:00Z",
                "config": {
                    "enabled": True,
                    "persona_preset": "catgirl",
                    "persona_name": "小喵客服",
                    "persona_instruction": "默认人格",
                    "tone": "friendly",
                    "buyer_address": "亲",
                    "reply_length": "short",
                    "emoji_level": "low",
                    "common_knowledge": "",
                    "forbidden_claims": [],
                    "handoff_rules": [],
                    "fallback_reply": "默认兜底",
                },
            },
            "history": [],
        })
        migrated_empty = service.get_config(*legacy_empty_scope)
        assert migrated_empty["status"] == "needs_content"
        assert migrated_empty["published"]["content_valid"] is False
        assert migrated_empty["published"]["config"]["enabled"] is True

        product = {
            "id": "1001",
            "title": "合同商品",
            "description": "仅用于离线合同，适合新手。",
            "price": 12,
            "quantity": 3,
            "status": "on",
            "skus": [{"name": "标准版", "price": 12, "stock": 3}],
        }
        scopes = ((7, 11, "shop-a"), (7, 12, "shop-b"))
        for scope in scopes:
            snapshot = Path(service.runtime_paths(*scope)["products_snapshot_file"])
            write_private_json(snapshot, {"products": [product]})
            tested = service.test_connection(
                *scope,
                base_url="https://example.com/v1",
                model="fixture-model",
                api_key="fixture-" + "x" * 16,
                expected_revision=0,
            )
            service.save_connection(
                *scope,
                base_url="https://example.com/v1",
                model="fixture-model",
                api_key="fixture-" + "x" * 16,
                verification_token=tested["verification_token"],
                expected_revision=0,
            )
            public = service.get_connection(*scope)
            assert public["api_key_configured"] is True and "api_key" not in public
            secret = Path(service.runtime_paths(*scope)["settings_file"]).parent / "ai_connection_secret.json"
            assert secret.exists() and stat.S_IMODE(secret.stat().st_mode) == 0o600
            assert "fixture-" not in secret.read_text(encoding="utf-8")

        empty_v1_path = Path(service.runtime_paths(7, 12, "shop-b")["knowledge_dir"]) / "1001.json"
        write_private_json(empty_v1_path, {
            "version": 1,
            "item_id": "1001",
            "revision": 2,
            "status": "published",
            "draft": {
                "summary": "", "selling_points": [], "specifications": [], "price_policy": "",
                "delivery_notes": "", "usage_notes": "", "after_sales": "", "faqs": [],
                "forbidden_answers": [], "handoff_rules": [], "custom_notes": "",
            },
            "published": {
                "revision": 2,
                "published_at": "2026-08-01T00:00:00Z",
                "snapshot_fingerprint": facts_fingerprint(product),
                "knowledge": {
                    "summary": "", "selling_points": [], "specifications": [], "price_policy": "",
                    "delivery_notes": "", "usage_notes": "", "after_sales": "", "faqs": [],
                    "forbidden_answers": [], "handoff_rules": [], "custom_notes": "",
                },
            },
            "disabled": False,
            "history": [],
        })
        migrated_empty_knowledge = service.get_knowledge(7, 12, "shop-b", "1001")
        assert migrated_empty_knowledge["status"] == "unconfigured"
        assert migrated_empty_knowledge["published"]["content_valid"] is False

        assert_error(
            "invalid_payload",
            lambda: service.save_config(
                7, 11, "shop-a", config={**defaults, "enabled": True, "store_content": "   "},
                expected_revision=0, action="publish",
            ),
        )
        assert_error(
            "invalid_payload",
            lambda: service.save_config(
                7, 11, "shop-a", config={**defaults, "enabled": True, "store_content": "……！？"},
                expected_revision=0, action="save",
            ),
        )
        store_config = {
            **defaults,
            "enabled": True,
            "store_content": "本店提供软件使用指导，只在闲鱼站内沟通。价格、库存和状态以商品实时页面为准。",
            "common_knowledge": "本店提供软件使用指导，只在闲鱼站内沟通。价格、库存和状态以商品实时页面为准。",
            "forbidden_claims": "永久稳定\n保证立即发货",
            "handoff_rules": "退款争议\n付款或订单状态无法核实时",
        }
        saved_config = service.save_config(
            7, 11, "shop-a", config=store_config, expected_revision=0, action="save"
        )
        assert saved_config["version"] == 2 and saved_config["status"] == "published"
        assert saved_config["content_valid"] is True and service.is_reply_ready(7, 11, "shop-a") is True
        assert service.is_reply_ready(7, 12, "shop-b") is False

        template = service.save_template(7, 11, "shop-a", name="内容模板", config=store_config)
        template_file = Path(service.runtime_paths(7, 11, "shop-a")["templates_file"])
        assert template_file.exists() and "fixture-" not in template_file.read_text(encoding="utf-8")
        assert service.get_templates(7, 12, "shop-b") == []
        assert_error(
            "invalid_payload",
            lambda: service.save_template(
                7, 11, "shop-a", name="空模板", config={**defaults, "store_content": "……！？"}
            ),
        )
        assert_error(
            "invalid_payload",
            lambda: service.save_template(
                7, 11, "shop-a", name="敏感模板", config={**store_config, "api_key": "must-not-persist"}
            ),
        )
        service.delete_template(7, 11, "shop-a", template["id"])

        for empty_value in (
            {"content": ""},
            {"content": "  "},
            {"content": "……--！？"},
            {
                "summary": "", "selling_points": [], "specifications": [], "price_policy": "",
                "delivery_notes": "", "usage_notes": "", "after_sales": "", "faqs": [],
                "forbidden_answers": [], "handoff_rules": [], "custom_notes": "",
            },
        ):
            assert_error(
                "invalid_payload",
                lambda value=empty_value: service.save_knowledge(
                    7, 11, "shop-a", "1001", knowledge=value, expected_revision=0
                ),
            )

        saved_knowledge = service.save_knowledge(
            7, 11, "shop-a", "1001", knowledge=legacy_knowledge, expected_revision=0
        )
        assert saved_knowledge["version"] == 2
        assert saved_knowledge["status"] == "published" and saved_knowledge["revision"] == 1
        assert saved_knowledge["published"]["knowledge"]["imported_from_v1"] is True
        assert saved_knowledge["review_recommended"] is True
        assert saved_knowledge["stale"] is False

        price_changed = {**product, "price": 13, "quantity": 2, "status": "paused"}
        price_changed["skus"] = [{"name": "标准版", "price": 13, "stock": 2}]
        snapshot_path = Path(service.runtime_paths(7, 11, "shop-a")["products_snapshot_file"])
        write_private_json(snapshot_path, {"products": [price_changed]})
        live_changed = service.get_knowledge(7, 11, "shop-a", "1001")
        assert live_changed["status"] == "published"
        assert live_changed["stale"] is False and live_changed["facts_changed"] is True
        assert live_changed["identity_fingerprint"] == saved_knowledge["identity_fingerprint"]
        assert live_changed["facts_fingerprint"] != saved_knowledge["facts_fingerprint"]
        assert facts_fingerprint(product) != facts_fingerprint(price_changed)
        assert identity_fingerprint(product) == identity_fingerprint(price_changed)

        identity_changed = {**price_changed, "title": "合同商品全新版"}
        write_private_json(snapshot_path, {"products": [identity_changed]})
        stale = service.get_knowledge(7, 11, "shop-a", "1001")
        assert stale["status"] == "stale" and stale["needs_confirmation"] is True
        assert identity_fingerprint(price_changed) != identity_fingerprint(identity_changed)

        # Saving again confirms the new identity and immediately reactivates content.
        confirmed = service.save_knowledge(
            7, 11, "shop-a", "1001", knowledge={"content": "新版商品适合新手，按页面实时价格和库存回答。"},
            expected_revision=stale["revision"],
        )
        assert confirmed["status"] == "published" and confirmed["stale"] is False
        disabled = service.disable_knowledge(
            7, 11, "shop-a", "1001", expected_revision=confirmed["revision"]
        )
        assert disabled["status"] == "disabled"
        republished = service.publish_knowledge(
            7, 11, "shop-a", "1001", expected_revision=disabled["revision"]
        )
        assert republished["status"] == "published"

        before_extract = service.get_knowledge(7, 11, "shop-a", "1001")
        extracted = service.extract_knowledge(
            7, 11, "shop-a", "1001", "真实资料：适合新手，先阅读说明，异常时转人工。"
        )
        assert set(extracted) == {"content", "saved"}
        assert "适合新手" in extracted["content"]
        assert extracted["saved"] is False
        assert service.get_knowledge(7, 11, "shop-a", "1001")["revision"] == before_extract["revision"]
        extract_output["value"] = '```json\n{"content":"坏内容"}\n```'
        assert_error(
            "response_invalid",
            lambda: service.extract_knowledge(7, 11, "shop-a", "1001", "再次整理"),
        )
        extract_output["value"] = "适合新手使用。"

        # One compiler is shared by preview and live reply. Current question is
        # always the final user message and store/product/facts/history are layered.
        reply_output["value"] = json.dumps(
            {"decision": "reply", "reply": "当前实时价格是 13，适合新手按说明操作。", "reason_code": "answered"},
            ensure_ascii=False,
        )
        history = [
            {"role": "user", "content": "我刚才问过版本"},
            {"role": "assistant", "content": "请问具体想了解什么？"},
        ]
        preview = service.preview(
            7, 11, "shop-a", buyer_message="现在多少钱，怎么用？", item_id="1001", history=history
        )
        preview_messages = requests[-1]["messages"]
        assert preview_messages[-1] == {"role": "user", "content": "现在多少钱，怎么用？"}
        assert preview_messages[-3:-1] == history
        assert "店铺客服内容" in preview_messages[1]["content"]
        assert "实时商品事实" in preview_messages[2]["content"]
        assert "商品补充内容" in preview_messages[2]["content"]
        live = service.reply(
            7,
            11,
            "shop-a",
            message="现在多少钱，怎么用？",
            history=history,
            item_id="1001",
            item_context={**identity_changed, "price": 13, "quantity": 2},
            recent_assistant_replies=[],
        )
        live_messages = requests[-1]["messages"]
        assert live_messages == preview_messages
        assert live == {
            "decision": "reply",
            "reply": "当前实时价格是 13，适合新手按说明操作。",
            "reason_code": "answered",
            "sources": ["store_content", "real_time_product_facts", "product_content", "conversation_history"],
            "knowledge_status": "published",
            "config_revision": preview["config_revision"],
        }
        assert service.ensure_reply_ready(
            7, 11, "shop-a", expected_config_revision=live["config_revision"]
        ) == {"config_revision": live["config_revision"]}
        assert_error(
            "ai_disabled",
            lambda: service.ensure_reply_ready(
                7, 11, "shop-a", expected_config_revision=live["config_revision"] + 1
            ),
        )
        assert preview["decision"] == live["decision"]
        assert preview["reply"] == live["reply"]
        assert preview["sources"] == live["sources"]
        assert "prompt" not in preview and "api_key" not in preview

        unsafe_cases = (
            ("```json\n{}\n```", "response_code_block"),
            (json.dumps({"decision": "reply", "reply": '{"secret":true}', "reason_code": "x"}), "reply_json"),
            (json.dumps({"decision": "reply", "reply": "[TRIAL]", "reason_code": "x"}), "reply_magic_marker"),
            (json.dumps({"decision": "reply", "reply": "加我微信处理", "reason_code": "x"}), "reply_off_platform_contact"),
            (json.dumps({"decision": "reply", "reply": "我已经确认付款并马上发货", "reason_code": "x"}), "reply_dangerous_fulfillment"),
            (json.dumps({"decision": "reply", "reply": "这个服务永久稳定", "reason_code": "x"}), "reply_forbidden_claim"),
        )
        for raw, reason in unsafe_cases:
            reply_output["value"] = raw
            decision = service.reply(7, 11, "shop-a", message="测试", item_id="1001")
            assert decision["decision"] == "no_reply" and decision["reply"] == ""
            assert decision["reason_code"] == reason, (decision, reason, raw)

        reply_output["value"] = json.dumps(
            {"decision": "reply", "reply": "这是一条近期重复回复", "reason_code": "answered"}, ensure_ascii=False
        )
        duplicate = service.reply(
            7, 11, "shop-a", message="另一个问题", item_id="1001",
            recent_assistant_replies=["这是一条近期重复回复。"],
        )
        assert duplicate["decision"] == "no_reply"
        assert duplicate["reason_code"] == "reply_recent_duplicate"
        reply_output["value"] = json.dumps(
            {"decision": "handoff", "reply": "不得发送的文本", "reason_code": "needs_owner"}, ensure_ascii=False
        )
        handoff = service.reply(7, 11, "shop-a", message="退款争议", item_id="1001")
        assert handoff["decision"] == "handoff" and handoff["reply"] == ""
        provider_error["value"] = "service_unavailable"
        failed_provider = service.reply(7, 11, "shop-a", message="另一个问题", item_id="1001")
        assert failed_provider["decision"] == "no_reply"
        assert failed_provider["reply"] == "" and failed_provider["reason_code"] == "service_unavailable"
        provider_error["value"] = ""

        disabled_store = service.save_config(
            7,
            11,
            "shop-a",
            config={**store_config, "enabled": False},
            expected_revision=saved_config["revision"],
            action="save",
        )
        assert disabled_store["status"] == "disabled"
        assert service.is_reply_ready(7, 11, "shop-a") is False
        assert_error(
            "ai_disabled",
            lambda: service.reply(7, 11, "shop-a", message="停用后仍来消息", item_id="1001"),
        )

        assert service.get_knowledge(7, 12, "shop-b", "1001")["status"] == "unconfigured"
        assert_error("item_not_found", lambda: service.get_knowledge(7, 11, "shop-a", "9999"))

    print("AI customer-service contract: v2 content, atomic activation, fingerprints and unified reply decisions passed")


if __name__ == "__main__":
    main()
