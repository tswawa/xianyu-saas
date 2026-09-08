#!/usr/bin/env python3
"""Offline FastAPI integration: shared AI settings, scoped proposals and write leases."""
from __future__ import annotations

import base64
import copy
import json
import os
import socket
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="settings-ops-api-")
RUN = Path(TEMP.name)
sys.dont_write_bytecode = True
os.environ.update({
    "SAAS_ENV": "development", "NODE_ENV": "development", "SAAS_TESTING": "1",
    "SAAS_DB": str(RUN / "saas.db"), "SAAS_TENANTS_DIR": str(RUN / "tenants"),
    "SAAS_COOKIE_SECURE": "0", "SAAS_RESTORE_WORKERS": "0", "SAAS_ALLOW_REGISTRATION": "0",
    "SAAS_ADMIN_TOKEN": "", "SAAS_AUDIT_HMAC_KEY": "settings-ops-contract-audit",
    "SAAS_AI_MASTER_KEY": base64.b64encode(b"s" * 32).decode("ascii"),
    "SAAS_PUBLIC_ORIGIN": "http://testserver", "SAAS_TRUSTED_HOSTS": "testserver",
})
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

CONNECTION = {"provider": "openai_chat_completions", "base_url": "https://models.example.test/v1",
              "model": "contract-model", "api_key": "synthetic-shared-contract-key", "expected_revision": 0}
PUBLIC = {"scope", "initialized", "provider", "base_url", "model", "api_key_configured",
          "connection_status", "revision", "key_revision", "last_error_code"}
CHAT = {"message": "补充所选商品的客服说明", "selected_item_ids": ["101"], "request_id": "chat-contract-1"}
ACTION = {"tool": "knowledge.save", "target": "101", "content": "仅提供本站商品的使用说明，请在站内咨询。"}


def no_network(*_args, **_kwargs):
    raise AssertionError("external network and DNS are forbidden")


def result(response, status=200, code=None):
    assert response.status_code == status, (response.request.url.path, response.status_code, response.text)
    payload = response.json()
    if code:
        assert payload["detail"]["code"] == code, payload
    assert CONNECTION["api_key"] not in response.text
    return payload


def files(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
            for p in root.rglob("*") if p.is_file()}


def run(app):
    ai = app.ai_service
    assert ai.storage.root == RUN / "tenants"
    owner_id = app.db.create_user("ops-owner", "Contract-Owner-123!", role="owner")
    other_id = app.db.create_user("ops-other", "Contract-Owner-123!", role="owner")
    first = app.db.ensure_default_shop_account(owner_id)
    second = app.db.create_shop_account(owner_id, "second", "第二店铺")
    app.db.ensure_default_shop_account(other_id)
    scopes = [(owner_id, row["id"], row["account_key"]) for row in (first, second)]
    owner, other, guest = (TestClient(app.app) for _ in range(3))
    for client, username in ((owner, "ops-owner"), (other, "ops-other")):
        response = client.post("/api/auth/login", json={"username": username, "password": "Contract-Owner-123!"})
        result(response)
        client.cookies.set(app.SESSION_COOKIE, response.cookies.get(app.SESSION_COOKIE), path="/")

    output = {"reply": "仅生成待确认提案，尚未保存。", "actions": [ACTION]}
    requests = []

    def fake_request(provider, base_url, model, api_key, payload, **kwargs):
        assert (provider, base_url, model) == tuple(CONNECTION[k] for k in ("provider", "base_url", "model"))
        assert api_key in {CONNECTION["api_key"], "synthetic-legacy-key"}
        assert kwargs["routing_session"].startswith("saas-")
        requests.append(copy.deepcopy(payload))
        content = "OK" if payload["messages"][0]["content"] == "Return only OK." else json.dumps(output, ensure_ascii=False)
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    def send(method, path, payload=None, client=owner, shop="default", **kwargs):
        return client.request(method, path, json=payload, headers={"X-Shop-Account": shop, **kwargs.pop("headers", {})}, **kwargs)

    sequence = 0

    def propose(actions=None, **overrides):
        nonlocal sequence
        sequence += 1
        output["actions"] = [ACTION] if actions is None else actions
        return result(send("POST", "/api/ops/chat", {**CHAT, "request_id": f"proposal-{sequence}", **overrides}))["plan"]

    def confirmation(plan, **overrides):
        return {"revision": plan["revision"], "digest": plan["digest"], "confirm": True,
                "request_id": "confirm-" + plan["id"], **overrides}

    def confirm(plan, **kwargs):
        return send("POST", f"/api/ops/plans/{plan['id']}/confirm", confirmation(plan, **kwargs))

    with patch.object(ai, "_request_json", side_effect=fake_request) as model:
        # Real sessions and account selection; no permission or auth dependency overrides.
        for path in ("/api/settings/ai/connection", "/api/ops/context"):
            result(guest.get(path), 401)
        result(send("GET", "/api/ops/context", client=other, shop="second"), 404)
        with patch.object(app, "has_permission", return_value=False):
            result(owner.get("/api/ops/context"), 403)
            result(owner.get("/api/settings/ai/connection"), 403)
        before = files(ai.storage.root)
        with patch.object(ai, "_master_keys", side_effect=AssertionError("metadata must not read keys")):
            metadata = result(owner.get("/api/settings/ai/connection"))
        assert set(metadata) == PUBLIC | {"providers"}
        assert metadata["initialized"] is False and metadata["revision"] == 0
        assert files(ai.storage.root) == before
        context = result(owner.get("/api/ops/context"))
        assert context["connection"]["connection_status"] == "unconfigured"
        assert context["diagnostics"]["scope"] == "current_shop"
        assert context["products"] == []
        result(send("POST", "/api/ops/chat", CHAT), 409, "connection_unconfigured")
        incomplete = {key: value for key, value in CONNECTION.items() if key != "expected_revision"}
        errors = result(send("POST", "/api/settings/ai/connection/test", incomplete), 422)["detail"]
        assert any(error["loc"] == ["body", "expected_revision"] and error["type"] == "missing" for error in errors)
        assert all(set(error) == {"loc", "type", "msg"} for error in errors)
        assert files(ai.storage.root) == before
        model.assert_not_called()

        # Seed a genuine legacy connection through its API, not invented on-disk JSON.
        legacy = {**CONNECTION, "api_key": "synthetic-legacy-key"}
        token = result(send("POST", "/api/bot/ai/connection/test", legacy))["verification_token"]
        result(send("PUT", "/api/bot/ai/connection", {**legacy, "verification_token": token}))
        assert ai.get_runtime_connection(*scopes[0])["api_key"] == legacy["api_key"]
        for scope, content in zip(scopes, ("第一店铺只提供软件使用说明。", "第二店铺只提供入门教程。")):
            result(send("PUT", "/api/bot/ai/config", {"store_content": content, "expected_revision": 0}, shop=scope[2]))
        body_before = files(ai.storage.root)
        token = result(send("POST", "/api/settings/ai/connection/test", CONNECTION))["verification_token"]
        assert result(owner.get("/api/settings/ai/connection"))["initialized"] is False
        save = {**CONNECTION, "verification_token": token, "confirm": True}
        result(send("PUT", "/api/settings/ai/connection", save, client=other), 409, "verification_invalid")
        assert result(other.get("/api/settings/ai/connection"))["initialized"] is False
        result(send("PUT", "/api/settings/ai/connection", {**save, "confirm": False}), 409, "confirmation_required")
        for non_boolean in ("true", 1):
            result(send("PUT", "/api/settings/ai/connection", {**save, "confirm": non_boolean}), 422)
        assert result(owner.get("/api/settings/ai/connection"))["initialized"] is False
        shared = result(send("PUT", "/api/settings/ai/connection", save))["connection"]
        assert set(shared) == PUBLIC and shared["connection_status"] == "verified"
        assert files(ai.storage.root) == body_before
        with patch.object(ai, "_master_keys", side_effect=AssertionError("metadata must not read keys")):
            metadata = result(owner.get("/api/settings/ai/connection"))
            assert metadata["api_key_configured"] is True
            for shop in ("default", "second", "nonexistent-shop"):
                assert result(send("GET", "/api/settings/ai/connection", shop=shop)) == metadata
            effective = [result(send("GET", "/api/bot/ai/connection", shop=s[2])) for s in scopes]
        assert effective[0] == effective[1] and effective[0]["scope"] == "user"
        runtime = [ai.get_runtime_connection(*scope) for scope in scopes]
        assert runtime[0] == runtime[1] == {k: v for k, v in CONNECTION.items() if k != "expected_revision"} | {"revision": 1}
        content = [result(send("GET", "/api/bot/ai/config", shop=s[2]))["config"]["store_content"] for s in scopes]
        assert content == ["第一店铺只提供软件使用说明。", "第二店铺只提供入门教程。"]
        for method, path, payload in (("POST", "/api/bot/ai/connection/test", CONNECTION),
                                      ("PUT", "/api/bot/ai/connection", {**CONNECTION, "verification_token": token}),
                                      ("DELETE", "/api/bot/ai/connection/key", {"confirm": True, "expected_revision": 1})):
            result(send(method, path, payload), 409, "connection_managed_in_settings")
        assert files(ai.storage.root) == body_before

        # OperationsService reads the existing snapshot/rule schemas; all fixtures are temporary.
        from automation import rules_document
        directory = ai.storage.account_dir(owner_id, "default")
        app.db.update_shop_account(owner_id, first["id"], account_ref="contract-shop-one")
        ai._write_root_json(scopes[0], "shop_snapshot.json", {"version": 1, "account_ref": "contract-shop-one",
                            "products": [{"id": "101", "title": "使用说明", "price": 19}]})
        rules = rules_document([{"name": "售前", "item_id": "101", "keywords": ["使用"], "reply": "请阅读商品介绍"}])
        ai._write_root_json(scopes[0], "reply_rules.json", rules)
        before = files(ai.storage.root)
        directories_before = {p for p in ai.storage.root.rglob("*") if p.is_dir()}
        assert result(owner.get("/api/ops/context"))["products"][0]["item_id"] == "101"
        assert result(send("GET", "/api/ops/context", shop="second"))["products"] == []
        with patch.object(ai, "_request_json", side_effect=app.AIServiceError("service_unavailable", 503)):
            result(send("POST", "/api/ops/chat", CHAT), 503, "service_unavailable")
        plan = propose()
        assert plan["status"] == "proposed" and plan["items"][0]["after"]["revision"] == 1
        assert result(owner.get(f"/api/ops/plans/{plan['id']}")) == plan
        after = files(ai.storage.root)
        assert after == before, [name for name in before.keys() | after.keys() if before.get(name) != after.get(name)]
        assert {p for p in ai.storage.root.rglob("*") if p.is_dir()} == directories_before
        assert not (directory / "ai_knowledge/101.json").exists()  # Login may initialize an empty directory.
        assert "private" not in json.dumps(plan) and "after_document" not in json.dumps(plan)
        call_count = model.call_count
        duplicate = result(send("POST", "/api/ops/chat", {**CHAT, "request_id": "proposal-1"}))["plan"]
        assert duplicate == plan and model.call_count == call_count
        result(send("POST", "/api/ops/chat", {**CHAT, "request_id": "proposal-1", "message": "其他请求"}), 409, "request_conflict")
        for client, shop in ((other, "default"), (owner, "second")):
            path = f"/api/ops/plans/{plan['id']}"
            result(send("GET", path, client=client, shop=shop), 404, "plan_not_found")
            result(send("POST", path + "/confirm", confirmation(plan), client=client, shop=shop), 404, "plan_not_found")
            result(send("POST", path + "/cancel", {"revision": plan["revision"], "digest": plan["digest"]}, client=client, shop=shop), 404, "plan_not_found")
        result(confirm(plan, confirm=False), 400, "invalid_payload")
        for non_boolean in ("true", 1):
            result(confirm(plan, confirm=non_boolean), 422)
        result(confirm(plan, digest="0" * 64), 409, "plan_conflict")
        result(confirm(plan, revision=2), 409, "plan_conflict")
        for method, path, payload in (
            ("POST", "/api/ops/chat", {**CHAT, "confirm": True}),
            ("POST", "/api/ops/chat", {**CHAT, "history": [{"role": "user", "content": "继续", "tool": "write"}]}),
            ("POST", f"/api/ops/plans/{plan['id']}/confirm", {**confirmation(plan), "actions": [ACTION]}),
            ("POST", f"/api/ops/plans/{plan['id']}/cancel", {"revision": 1, "digest": plan["digest"], "confirm": True}),
            ("PUT", "/api/settings/ai/connection", {**save, "user_id": other_id}),
        ):
            result(send(method, path, payload), 422)
        result(send("POST", "/api/ops/chat", CHAT, headers={"Idempotency-Key": "different-request"}), 400, "invalid_request_id")
        assert files(ai.storage.root) == before

        # CSRF checks are exercised with the testing bypass explicitly disabled.
        with patch.dict(os.environ, {"SAAS_TESTING": "0"}):
            bad_origin = {"Origin": "http://evil.invalid", "X-SaaS-Browser-Intent": "browser-write",
                          "Referer": "http://testserver/xianyu-saas/"}
            for method, path, payload in (
                ("PUT", "/api/settings/ai/connection", save),
                ("DELETE", "/api/settings/ai/connection", {"expected_revision": 1, "confirm": True}),
                ("POST", f"/api/ops/plans/{plan['id']}/confirm", confirmation(plan)),
                ("PUT", "/api/config", {"keywords_json": json.dumps(rules)}),
            ):
                result(send(method, path, payload, headers=bad_origin), 403, "browser_origin_mismatch")
        assert result(owner.get("/api/settings/ai/connection"))["revision"] == 1
        assert files(ai.storage.root) == before

        # A real DB lease blocks both routes; wrapping the real writer proves ownership at write time.
        lock_key = f"automation-save:{owner_id}:{first['id']}"
        assert app.db.acquire_control_lease(lock_key, "contract-blocker", lease_seconds=60, cooldown_seconds=0) == "acquired"
        try:
            result(send("PUT", "/api/config", {"keywords_json": json.dumps(rules)}), 409)
            result(confirm(plan), 409)
            assert files(ai.storage.root) == before
            assert result(owner.get(f"/api/ops/plans/{plan['id']}"))["status"] == "proposed"
        finally:
            app.db.release_control_lease(lock_key, "contract-blocker")
        writes = []
        real_write = ai.storage.atomic_write_path

        def checked_write(path, data):
            assert app.db.acquire_control_lease(lock_key, "contract-contender", lease_seconds=1, cooldown_seconds=0) == "busy", "writer lacks automation-save lease"
            writes.append(Path(path))
            return real_write(path, data)

        with patch.object(ai.storage, "atomic_write_path", side_effect=checked_write):
            done = result(confirm(plan))
            assert done["status"] == "succeeded" and len(writes) == 1
            applied = files(ai.storage.root)
            assert result(confirm(plan)) == done
            assert result(confirm(plan, request_id="another-confirm-request")) == done
            assert files(ai.storage.root) == applied and len(writes) == 1
        assert model.call_count == call_count
        knowledge = json.loads((directory / "ai_knowledge/101.json").read_text())
        assert knowledge["revision"] == 1 and knowledge["published"]["knowledge"]["content"] == ACTION["content"]
        assert knowledge["_ops_receipt"]["plan_id"] == plan["id"]
        assert not (ai.storage.account_dir(owner_id, "second") / "ai_knowledge").exists()

        # Legacy config writes and rules.replace confirmations must hold that same lease.
        actual_secret_write = app.write_secret

        def checked_secret(uid, name, data, key):
            assert name == "reply_rules.json"
            assert app.db.acquire_control_lease(lock_key, "contract-contender", lease_seconds=1, cooldown_seconds=0) == "busy"
            return actual_secret_write(uid, name, data, key)

        with patch.object(app, "write_secret", side_effect=checked_secret) as legacy_write:
            result(send("PUT", "/api/config", {"keywords_json": json.dumps(rules)}))
            legacy_write.assert_called_once()
        changed = copy.deepcopy(rules)
        changed["rules"][0]["reply"] = "请在站内咨询使用问题"
        rule_plan = propose([{"tool": "rules.replace", "target": "rules", "content": changed}], selected_rule_ids=["rule-1"])
        with patch.object(ai.storage, "atomic_write_path", side_effect=checked_write):
            assert result(confirm(rule_plan))["status"] == "succeeded"
        assert writes[-1] == directory / "reply_rules.json"
        assert result(owner.get("/api/ops/context"))["rules"] == changed["rules"]
        assert app.db.acquire_control_lease(lock_key, "contract-released", lease_seconds=1, cooldown_seconds=0) == "acquired"
        app.db.release_control_lease(lock_key, "contract-released")

        for state in ("cancelled", "expired", "conflict"):
            pending = propose()
            if state == "cancelled":
                result(send("POST", f"/api/ops/plans/{pending['id']}/cancel", {"revision": pending["revision"], "digest": pending["digest"]}))
            elif state == "expired":
                with app.db._lock, app.db.con:
                    app.db.con.execute("UPDATE ops_plans SET expires_at=0 WHERE id=?", (pending["id"],))
            else:
                # Edit a real stored document to simulate a concurrent writer.
                path = directory / "ai_knowledge/101.json"
                document = json.loads(path.read_text())
                document["revision"] += 1
                ai._write_path_json(path, document)
            before = files(ai.storage.root)
            with patch.object(ai.storage, "atomic_write_path", wraps=real_write) as write:
                terminal = result(confirm(pending))
                assert terminal["status"] == ("needs_review" if state == "conflict" else state)
                if state == "conflict":
                    assert terminal["items"][0]["error_code"] == "content_conflict"
                write.assert_not_called()
            assert files(ai.storage.root) == before

        # Revoking the real user after preview invalidates its session before confirmation.
        pending = propose()
        before, call_count = files(ai.storage.root), model.call_count
        app.db.update_platform_user(owner_id, enabled=False)
        with patch.object(ai.storage, "atomic_write_path", wraps=real_write) as write:
            result(confirm(pending), 401)
            write.assert_not_called()
        assert files(ai.storage.root) == before and model.call_count == call_count
        app.db.update_platform_user(owner_id, enabled=True)
        response = owner.post("/api/auth/login", json={"username": "ops-owner", "password": "Contract-Owner-123!"})
        result(response)
        owner.cookies.clear()
        owner.cookies.set(app.SESSION_COOKIE, response.cookies.get(app.SESSION_COOKIE), path="/")
        assert result(owner.get(f"/api/ops/plans/{pending['id']}"))["status"] == "proposed"

        # Permanent shared tombstone cannot resurrect the still-valid legacy credential.
        before = files(ai.storage.root)
        result(send("DELETE", "/api/settings/ai/connection", {"expected_revision": 1, "confirm": False}), 409, "confirmation_required")
        for non_boolean in ("true", 1):
            result(send("DELETE", "/api/settings/ai/connection", {"expected_revision": 1, "confirm": non_boolean}), 422)
        assert result(owner.get("/api/settings/ai/connection"))["revision"] == 1
        deleted = result(send("DELETE", "/api/settings/ai/connection", {"expected_revision": 1, "confirm": True}))["connection"]
        assert deleted["initialized"] is True and deleted["revision"] == 2 and deleted["api_key_configured"] is False
        for scope in scopes:
            assert result(send("GET", "/api/bot/ai/connection", shop=scope[2]))["connection_status"] == "unconfigured"
            try:
                ai.get_runtime_connection(*scope)
            except app.AIServiceError as error:
                assert error.code == "connection_unconfigured"
            else:
                raise AssertionError("deleted shared connection fell back to a legacy key")
        assert ai.get_connection(*scopes[0])["connection_status"] == "verified"
        call_count = model.call_count
        result(send("POST", "/api/ops/chat", {**CHAT, "request_id": "after-delete-chat"}), 503, "connection_unconfigured")
        result(send("POST", "/api/bot/ai/connection/test", CONNECTION), 409, "connection_managed_in_settings")
        assert model.call_count == call_count and files(ai.storage.root) == before
    for client in (owner, other, guest):
        client.close()
    print("settings/ops API contract: ok (shared isolation, previews, confirmation, CSRF, real write leases; offline)")


def main():
    with ExitStack() as guard:
        for target, attribute in ((socket, "create_connection"), (socket, "getaddrinfo"),
                                  (socket.socket, "connect"), (socket.socket, "connect_ex")):
            guard.enter_context(patch.object(target, attribute, no_network))
        import app
        try:
            run(app)
        finally:
            app.db.con.close()
            app._api_process_lock.close()
            TEMP.cleanup()


if __name__ == "__main__":
    main()
