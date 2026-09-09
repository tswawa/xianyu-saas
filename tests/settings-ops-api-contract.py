#!/usr/bin/env python3
"""Offline FastAPI contracts: user AI settings and persistent, shop-bound Agent runs."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import socket
import sqlite3
import sys
import tempfile
from contextlib import ExitStack, closing
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
CHAT = {"message": "请说明本店客服配置能力，不修改任何配置", "request_id": "chat-contract-1"}


def no_network(*_args, **_kwargs):
    raise AssertionError("external network and DNS are forbidden")


def result(response, status=200, code=None):
    assert response.status_code == status, (response.request.url.path, response.status_code, response.text)
    payload = response.json()
    if code:
        assert payload["detail"]["code"] == code, payload
    for secret in (CONNECTION["api_key"], "synthetic-legacy-key"):
        assert secret not in response.text
    return payload


def files(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
            for p in root.rglob("*") if p.is_file()}


class Fixture:
    def __init__(self, app):
        self.app, self.db, self.ai = app, app.db, app.ai_service
        assert self.ai.storage.root == RUN / "tenants"
        self.uid = self.db.create_user("ops-owner", "Contract-Owner-123!", role="owner")
        self.other_uid = self.db.create_user("ops-other", "Contract-Owner-123!", role="owner")
        self.first = self.db.ensure_default_shop_account(self.uid)
        self.second = self.db.create_shop_account(self.uid, "second", "第二店铺")
        other_shop = self.db.ensure_default_shop_account(self.other_uid)
        for uid, shop in ((self.uid, self.first), (self.uid, self.second), (self.other_uid, other_shop)):
            self.db.update_shop_account(uid, shop["id"], account_ref=f"offline-account-{shop['id']}")
        self.scopes = [(self.uid, row["id"], row["account_key"]) for row in (self.first, self.second)]
        self.owner, self.other, self.guest = (TestClient(app.app) for _ in range(3))
        self.token = self.login(self.owner, "ops-owner")
        self.login(self.other, "ops-other")
        self.calls, self.catalogs = [], []
        self.reply = "这是当前店铺的配置能力说明，未修改任何配置。"

    def login(self, client, username):
        response = client.post("/api/auth/login", json={"username": username, "password": "Contract-Owner-123!"})
        result(response)
        token = response.cookies.get(self.app.SESSION_COOKIE)
        assert token
        client.cookies.clear()
        client.cookies.set(self.app.SESSION_COOKIE, token, path="/")
        return token

    def send(self, method, path, payload=None, *, client=None, shop="default", **kwargs):
        return (client or self.owner).request(method, path, json=payload,
            headers={"X-Shop-Account": shop, **kwargs.pop("headers", {})}, **kwargs)

    def fake_connection(self, provider, base_url, model, api_key, payload, **kwargs):
        assert (provider, base_url, model) == tuple(CONNECTION[k] for k in ("provider", "base_url", "model"))
        assert api_key in {CONNECTION["api_key"], "synthetic-legacy-key"}
        assert payload["messages"][0]["content"] == "Return only OK."
        assert kwargs["routing_session"].startswith("saas-")
        return {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}

    def fake_agent_request(self, url, api_key, payload, headers):
        assert url == CONNECTION["base_url"] + "/chat/completions"
        assert api_key == CONNECTION["api_key"] and payload["model"] == CONNECTION["model"]
        self.calls.append(copy.deepcopy(payload["messages"]))
        self.catalogs.append({tool["function"]["name"] for tool in payload.get("tools", [])})
        return {"choices": [{"message": {"role": "assistant", "content": self.reply}, "finish_reason": "stop"}]}

    def chat(self, request_id, session_id="", message=None, **kwargs):
        return self.send("POST", "/api/ops/chat", {"session_id": session_id,
            "request_id": request_id, "message": CHAT["message"] if message is None else message}, **kwargs)

    def durable(self, run):
        # A separate SQLite reader proves HTTP 202 followed a committed enqueue.
        with closing(sqlite3.connect(RUN / "saas.db")) as con:
            con.row_factory = sqlite3.Row
            binding = dict(con.execute("SELECT * FROM ops_runs WHERE id=?", (run["run_id"],)).fetchone())
            job = dict(con.execute("SELECT kind,payload_json FROM jobs WHERE id=?", (binding["job_id"],)).fetchone())
            assert con.execute("SELECT 1 FROM ops_messages WHERE run_id=? AND role='user'",
                               (run["run_id"],)).fetchone() is not None
        assert binding["status"] == run["status"] == "queued"
        assert binding["session_id"] == run["session_id"]
        assert binding["auth_session_hash"] == hashlib.sha256(self.token.encode()).hexdigest()
        assert job["kind"] == "ops_run" and json.loads(job["payload_json"]) == {"run_id": run["run_id"]}
        for secret in (self.token, CONNECTION["api_key"], "synthetic-legacy-key"):
            assert secret not in json.dumps(binding) and secret not in job["payload_json"]

    def finish(self, run):
        self.app.operations_service.process_run(run["run_id"])
        return result(self.send("GET", f"/api/ops/runs/{run['run_id']}"))

    def messages(self, session_id):
        rows, cursor = [], 0
        self.last_message_page_count = 0
        while True:
            page = result(self.send("GET", f"/api/ops/sessions/{session_id}/messages?cursor={cursor}"))
            self.last_message_page_count += 1
            sequences = [message["seq"] for message in page["messages"]]
            assert sequences == sorted(set(sequences)) and all(seq > cursor for seq in sequences)
            rows.extend(page["messages"])
            following = page["next_cursor"]
            if following is None:
                return rows
            assert following > cursor, "message pagination did not advance"
            cursor = following

    def close(self):
        for client in (self.owner, self.other, self.guest):
            client.close()


def settings_contract(f):
    for path in ("/api/settings/ai/connection", "/api/ops/sessions/current"):
        result(f.guest.get(path), 401)
    result(f.send("GET", "/api/ops/sessions/current", client=f.other, shop="second"), 404)
    with patch.object(f.app, "has_permission", return_value=False):
        result(f.owner.get("/api/ops/sessions/current"), 403)
        result(f.owner.get("/api/settings/ai/connection"), 403)
    before, changes = files(f.ai.storage.root), f.db.con.total_changes
    for _ in range(2):
        assert result(f.owner.get("/api/ops/sessions/current")) == {"session": None, "active_run": None}
    assert f.db.con.total_changes == changes and files(f.ai.storage.root) == before
    with patch.object(f.ai, "_master_keys", side_effect=AssertionError("metadata must not read keys")):
        metadata = result(f.owner.get("/api/settings/ai/connection"))
    assert set(metadata) == PUBLIC | {"providers"}
    assert metadata["initialized"] is False and metadata["revision"] == 0
    queued = result(f.chat("missing-connection"), 202)
    f.durable(queued)
    missing = f.finish(queued)
    assert missing["status"] == "failed" and missing["error"]["code"] == "connection_unconfigured"
    assert missing["error"]["source"] == "application" and not f.calls
    incomplete = {key: value for key, value in CONNECTION.items() if key != "expected_revision"}
    errors = result(f.send("POST", "/api/settings/ai/connection/test", incomplete), 422)["detail"]
    assert any(error["loc"] == ["body", "expected_revision"] and error["type"] == "missing" for error in errors)
    assert all(set(error) == {"loc", "type", "msg"} for error in errors)
    result(f.owner.get("/api/settings/ai/connection/legacy-sources"), 404)
    for source in ({"source_account_key": "default"}, {"source_revision": 0}):
        result(f.send("POST", "/api/settings/ai/connection/test", {**CONNECTION, **source}), 422)
    assert files(f.ai.storage.root) == before

    with patch.object(f.ai, "_request_json", side_effect=f.fake_connection):
        legacy = {**CONNECTION, "api_key": "synthetic-legacy-key"}
        token = result(f.send("POST", "/api/bot/ai/connection/test", legacy))["verification_token"]
        result(f.send("PUT", "/api/bot/ai/connection", {**legacy, "verification_token": token}))
        assert f.ai.get_runtime_connection(*f.scopes[0])["api_key"] == legacy["api_key"]
        for scope, content in zip(f.scopes, ("第一店铺只提供软件使用说明。", "第二店铺只提供入门教程。")):
            result(f.send("PUT", "/api/bot/ai/config", {"store_content": content, "expected_revision": 0}, shop=scope[2]))
        before = files(f.ai.storage.root)
        token = result(f.send("POST", "/api/settings/ai/connection/test", CONNECTION))["verification_token"]
        assert result(f.owner.get("/api/settings/ai/connection"))["initialized"] is False
        save = {**CONNECTION, "verification_token": token, "confirm": True}
        result(f.send("PUT", "/api/settings/ai/connection", save, client=f.other), 409, "verification_invalid")
        assert result(f.other.get("/api/settings/ai/connection"))["initialized"] is False
        result(f.send("PUT", "/api/settings/ai/connection", {**save, "confirm": False}), 409, "confirmation_required")
        for value in ("true", 1):
            result(f.send("PUT", "/api/settings/ai/connection", {**save, "confirm": value}), 422)
        for source in ({"source_account_key": "default"}, {"source_revision": 0}, {"user_id": f.other_uid}):
            result(f.send("PUT", "/api/settings/ai/connection", {**save, **source}), 422)
        shared = result(f.send("PUT", "/api/settings/ai/connection", save))["connection"]
        assert set(shared) == PUBLIC and shared["connection_status"] == "verified"
        result(f.send("PUT", "/api/settings/ai/connection", save), 409, "revision_conflict")
        assert files(f.ai.storage.root) == before
        with patch.object(f.ai, "_master_keys", side_effect=AssertionError("metadata must not read keys")):
            metadata = result(f.owner.get("/api/settings/ai/connection"))
            assert metadata["api_key_configured"] is True
            for shop in ("default", "second", "nonexistent-shop"):
                assert result(f.send("GET", "/api/settings/ai/connection", shop=shop)) == metadata
            effective = [result(f.send("GET", "/api/bot/ai/connection", shop=s[2])) for s in f.scopes]
        assert effective[0] == effective[1] and effective[0]["scope"] == "user"
        runtime = [f.ai.get_runtime_connection(*scope) for scope in f.scopes]
        assert runtime[0] == runtime[1] == {k: v for k, v in CONNECTION.items() if k != "expected_revision"} | {"revision": 1}
        content = [result(f.send("GET", "/api/bot/ai/config", shop=s[2]))["config"]["store_content"] for s in f.scopes]
        assert content == ["第一店铺只提供软件使用说明。", "第二店铺只提供入门教程。"]
        for method, path, payload in (("POST", "/api/bot/ai/connection/test", CONNECTION),
                                      ("PUT", "/api/bot/ai/connection", {**CONNECTION, "verification_token": token}),
                                      ("DELETE", "/api/bot/ai/connection/key", {"confirm": True, "expected_revision": 1})):
            result(f.send(method, path, payload), 409, "connection_managed_in_settings")
        assert files(f.ai.storage.root) == before
        # Give the other user its own valid connection so cross-session denials test ownership, not missing keys.
        token = result(f.send("POST", "/api/settings/ai/connection/test", CONNECTION, client=f.other))["verification_token"]
        result(f.send("PUT", "/api/settings/ai/connection", {**CONNECTION, "verification_token": token, "confirm": True}, client=f.other))


def sessions_contract(f):
    before = files(f.ai.storage.root)
    session = result(f.send("POST", "/api/ops/sessions"), 201)["session"]
    sid = session["id"]
    assert result(f.owner.get("/api/ops/sessions/current"))["session"]["id"] == sid
    assert f.messages(sid) == []
    long_message = " \n原文开始：" + "完整内容" * 1300 + "原文结尾。\n  "
    f.reply = "原样答复开始：" + "历史答复" * 1100 + "答复结尾。"
    assert len(long_message) > 5200 and len(f.reply) > 4400
    run = result(f.chat("request-long-text", sid, long_message), 202)
    assert run["session_id"] == sid and run["status"] == "queued"
    f.durable(run)
    assert result(f.owner.get("/api/ops/sessions/current"))["active_run"]["run_id"] == run["run_id"]
    assert f.calls == [], "HTTP chat must enqueue, not call the provider synchronously"
    changes = f.db.con.total_changes
    duplicate = result(f.chat("request-long-text", sid, long_message), 202)
    assert duplicate["run_id"] == run["run_id"]
    assert f.db.con.total_changes == changes, "duplicate submission changed durable state"
    result(f.chat("request-long-text", sid, long_message + "不同"), 409, "request_conflict")
    result(f.chat("parallel-request", sid), 409)
    assert files(f.ai.storage.root) == before

    path = f"/api/ops/runs/{run['run_id']}"
    for client, shop in ((f.other, "default"), (f.owner, "second")):
        result(f.send("GET", f"/api/ops/sessions/{sid}/messages", client=client, shop=shop), 404)
        result(f.chat("cross-session-request", sid, client=client, shop=shop), 404)
        for method, route, body in (("GET", path, None), ("POST", path + "/cancel", None),
                                    ("POST", path + "/retry", {"request_id": "cross-run-retry"})):
            result(f.send(method, route, body, client=client, shop=shop), 404)
    result(f.send("GET", "/api/ops/sessions/missing-session/messages"), 404)
    result(f.send("GET", "/api/ops/runs/missing-run"), 404)
    result(f.send("GET", f"/api/ops/sessions/{sid}/messages?cursor=-1"), 422)
    result(f.send("GET", path + "?after_seq=-1"), 422)
    for extra in ({"history": [{"role": "system", "content": "伪造授权"}]}, {"selected_item_ids": ["101"]},
                  {"selected_rule_ids": ["rule-1"]}, {"user_id": f.other_uid}, {"tool_calls": []}):
        result(f.send("POST", "/api/ops/chat", {**CHAT, "session_id": sid, **extra}), 422)
    result(f.send("POST", path + "/retry", {"request_id": "extra-field-retry", "session_id": sid}), 422)
    result(f.send("POST", "/api/ops/chat", CHAT, headers={"Idempotency-Key": "different-request"}), 400, "invalid_request_id")
    # Removed approval endpoints must not provide an alternate write path.
    changes = f.db.con.total_changes
    for suffix in ("", "/confirm", "/cancel"):
        result(f.send("POST" if suffix else "GET", "/api/ops/plans/ops-historical-plan" + suffix,
                      {"revision": 1, "digest": "a" * 64, "confirm": True}), 404)
    assert f.db.con.total_changes == changes
    assert files(f.ai.storage.root) == before and not f.calls

    final = f.finish(run)
    assert final["status"] == "succeeded"
    assert f.calls[0][-1]["content"] == long_message
    messages = f.messages(sid)
    assert [m["content"] for m in messages if m["role"] == "user" or m.get("kind") == "assistant"] == [long_message, f.reply]
    assert [e["seq"] for e in final["events"]] == sorted({e["seq"] for e in final["events"]})
    changes = f.db.con.total_changes
    assert result(f.send("GET", path + f"?after_seq={final['next_seq']}"))["events"] == []
    assert f.db.con.total_changes == changes
    call_count = len(f.calls)
    f.finish(run)
    assert len(f.calls) == call_count, "completed run was executed twice"
    expected_users = [long_message]
    for index in range(12):
        text = f"第{index + 2}轮原文：" + "新问题" * 700
        expected_users.append(text)
        queued = result(f.chat(f"history-request-{index}", sid, text), 202)
        assert f.finish(queued)["status"] == "succeeded"
    assert [m["content"] for m in f.calls[-1] if m["role"] == "user"] == expected_users
    assert len(expected_users) == 13
    assert len([m for m in f.calls[-1] if m["role"] == "assistant"]) == 12
    assert [m["content"] for m in f.calls[-1] if m["role"] == "assistant"] == [f.reply] * 12
    stored = f.messages(sid)
    assert [m["content"] for m in stored if m["role"] == "user"] == expected_users
    assert all(m["content"] == f.reply for m in stored if m.get("kind") == "assistant")
    import operations
    with patch.object(operations, "PAGE_SIZE", 5):
        assert f.messages(sid) == stored  # Smaller presentation pages cannot discard original messages.
        assert f.last_message_page_count > 1
    assert files(f.ai.storage.root) == before, "read-only conversations changed shop files"

    fresh = result(f.send("POST", "/api/ops/sessions"), 201)["session"]["id"]
    assert fresh != sid and f.messages(fresh) == []
    result(f.chat("request-long-text", fresh, long_message), 409, "request_conflict")
    assert f.messages(sid) == stored, "new conversation destroyed old messages"
    fresh_run = result(f.chat("fresh-session-request", fresh, "新对话专用正文"), 202)
    assert f.finish(fresh_run)["status"] == "succeeded"
    assert [m["content"] for m in f.calls[-1] if m["role"] == "user"] == ["新对话专用正文"]
    assert result(f.send("GET", "/api/ops/sessions/current", shop="second"))["session"] is None
    second = result(f.chat("second-shop-request", message="第二店铺专用正文", shop="second"), 202)
    f.app.operations_service.process_run(second["run_id"])
    assert result(f.send("GET", f"/api/ops/runs/{second['run_id']}", shop="second"))["status"] == "succeeded"
    assert [m["content"] for m in f.calls[-1] if m["role"] == "user"] == ["第二店铺专用正文"]
    assert result(f.owner.get("/api/ops/sessions/current"))["session"]["id"] == fresh

    cancelled = result(f.chat("cancelled-request", fresh), 202)
    count = len(f.calls)
    cancelled_path = f"/api/ops/runs/{cancelled['run_id']}"
    result(f.send("POST", cancelled_path + "/cancel"))
    assert f.finish(cancelled)["status"] == "cancelled" and len(f.calls) == count
    assert files(f.ai.storage.root) == before
    return fresh


def failures_contract(f, sid):
    # Exercise the real native adapter error parser, not a fabricated runtime error.
    def denied(url, key, payload, headers):
        return (401, {"error": {"code": "invalid_api_key", "type": "authentication_error",
                     "message": "Invalid credentials: " + CONNECTION["api_key"]}}, {"x-request-id": "req-offline-401"})
    queued = result(f.chat("provider-error-request", sid), 202)
    with patch.object(f.ai, "requester", new=denied):
        failed = f.finish(queued)
    assert failed["status"] == "failed"
    assert failed["error"]["source"] == "provider" and failed["error"]["upstream_status"] == 401
    assert failed["error"]["code"] == "provider_error"
    assert failed["error"]["upstream_code"] == "invalid_api_key"
    assert failed["error"]["upstream_type"] == "authentication_error"
    assert failed["error"]["upstream_request_id"] == "req-offline-401"
    assert "Invalid credentials" in failed["error"]["message"]
    assert any(m.get("error") == failed["error"] for m in f.messages(sid))
    assert result(f.send("GET", f"/api/ops/runs/{queued['run_id']}"))["error"] == failed["error"]
    assert f.db.get_token_user(f.token) == f.uid
    result(f.owner.get("/api/me"))
    result(f.guest.get(f"/api/ops/runs/{queued['run_id']}"), 401)
    retried = result(f.send("POST", f"/api/ops/runs/{queued['run_id']}/retry", {"request_id": "provider-error-retry"}), 202)
    recovered = f.finish(retried)
    assert recovered["status"] == "succeeded", {k: recovered[k] for k in ("status", "changed_count", "failed_count")}
    assert not {"knowledge_save", "knowledge_set_enabled", "rule_upsert", "rule_set_enabled", "delivery_configure"} & f.catalogs[-1]
    duplicate = result(f.send("POST", f"/api/ops/runs/{queued['run_id']}/retry", {"request_id": "provider-error-retry"}), 202)
    assert duplicate["run_id"] == retried["run_id"]

    before = files(f.ai.storage.root)
    with patch.dict(os.environ, {"SAAS_TESTING": "0"}):
        headers = {"Origin": "http://evil.invalid", "X-SaaS-Browser-Intent": "browser-write",
                   "Referer": "http://testserver/xianyu-saas/"}
        changes = f.db.con.total_changes
        for method, path, body in (("POST", "/api/ops/chat", {**CHAT, "session_id": sid}),
                                  ("POST", "/api/ops/sessions", None),
                                  ("DELETE", "/api/settings/ai/connection", {"expected_revision": 1, "confirm": True})):
            result(f.send(method, path, body, headers=headers), 403, "browser_origin_mismatch")
        assert f.db.con.total_changes == changes
    assert files(f.ai.storage.root) == before

    queued = result(f.chat("disabled-user-request", sid), 202)
    f.db.update_platform_user(f.uid, enabled=False)
    result(f.send("GET", f"/api/ops/runs/{queued['run_id']}"), 401)
    count = len(f.calls)
    f.app.operations_service.process_run(queued["run_id"])
    assert len(f.calls) == count and files(f.ai.storage.root) == before
    f.db.update_platform_user(f.uid, enabled=True)
    f.token = f.login(f.owner, "ops-owner")
    assert result(f.send("GET", f"/api/ops/runs/{queued['run_id']}"))["status"] == "failed"

    for value in (False, "true", 1):
        status = 409 if value is False else 422
        result(f.send("DELETE", "/api/settings/ai/connection", {"expected_revision": 1, "confirm": value}), status)
    tombstone = result(f.send("DELETE", "/api/settings/ai/connection", {"expected_revision": 1, "confirm": True}))["connection"]
    assert tombstone["initialized"] and tombstone["revision"] == 2 and not tombstone["api_key_configured"]
    for scope in f.scopes:
        assert result(f.send("GET", "/api/bot/ai/connection", shop=scope[2]))["connection_status"] == "unconfigured"
        try:
            f.ai.get_runtime_connection(*scope)
        except f.app.AIServiceError as error:
            assert error.code == "connection_unconfigured"
        else:
            raise AssertionError("deleted shared connection resurrected a legacy key")
    assert f.ai.get_connection(*f.scopes[0])["connection_status"] == "verified"
    count = len(f.calls)
    queued = result(f.chat("after-delete-request", sid), 202)
    missing = f.finish(queued)
    assert missing["status"] == "failed" and missing["error"]["source"] == "application"
    assert missing["error"]["code"] == "connection_unconfigured" and len(f.calls) == count
    assert result(f.other.get("/api/settings/ai/connection"))["connection_status"] == "verified"
    assert files(f.ai.storage.root) == before


def in_flight_fences_contract(f):
    # Only synthetic fixtures are written. The provider, runner and tools stay real.
    revision = result(f.owner.get("/api/settings/ai/connection"))["revision"]
    connection = {**CONNECTION, "expected_revision": revision}
    with patch.object(f.ai, "_request_json", side_effect=f.fake_connection):
        token = result(f.send("POST", "/api/settings/ai/connection/test", connection))["verification_token"]
        saved = result(f.send("PUT", "/api/settings/ai/connection",
                             {**connection, "verification_token": token, "confirm": True}))["connection"]
    f.ai._write_root_json(f.scopes[0], "shop_snapshot.json", {
        "version": 1, "account_ref": f"offline-account-{f.first['id']}",
        "products": [{"item_id": "90101", "title": "HTTP守卫商品", "status": "on_sale",
                      "price": "9.90", "description": "离线契约商品，不连接真实店铺"}],
    })
    service = f.app.operations_service
    # AIService rejects a changed connection before the runner receives its late result.
    scenarios = (("control", None), ("cancel", None), ("generation", "scope_invalid"),
                 ("revoked-session", "session_invalid"), ("disabled-shop", "scope_invalid"),
                 ("deleted-connection", "revision_conflict"))
    for scenario, error_code in scenarios:
        content = f"HTTP守卫{scenario}的临时客服说明。"
        queued = result(f.chat(f"fence-{scenario}", message=f"给商品90101改写客服知识，内容：{content}"), 202)
        f.durable(queued)
        path = f"/api/ops/runs/{queued['run_id']}"
        before, turns, target = files(f.ai.storage.root), [], []

        def tool_reply(call_id, name, arguments):
            return {"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": call_id, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]},
                "finish_reason": "tool_calls"}]}

        def requester(url, key, payload, headers):
            f.fake_agent_request(url, key, payload, headers)
            turns.append(payload)
            assert "knowledge_save" in f.catalogs[-1], "fixture must authorize the tested write"
            if len(turns) == 1:
                return tool_reply("fence-search", "products_search", {"query": "90101"})
            last = payload["messages"][-1]
            assert last["role"] == "tool"
            receipt = json.loads(last["content"])
            assert receipt["ok"] is True, receipt
            if len(turns) == 2:
                product = receipt["data"]["products"][0]
                assert product["item_id"] == "90101" and product["write_scope_resolved"] is True
                target.append(product["target_ref"])
                return tool_reply("fence-read", "knowledge_get", {"target_ref": target[0]})
            if len(turns) == 3:
                arguments = {"target_ref": target[0], "expected_revision": receipt["data"]["expected_revision"],
                             "content": content}
                if scenario == "cancel":
                    assert result(f.send("POST", path + "/cancel"))["status"] == "cancel_requested"
                elif scenario == "generation":
                    with f.db._lock, f.db.con:
                        f.db.con.execute("UPDATE shop_accounts SET generation=generation+1 WHERE user_id=? AND id=?",
                                         (f.uid, f.first["id"]))
                elif scenario == "revoked-session":
                    with f.db._lock, f.db.con:
                        f.db.con.execute("DELETE FROM tokens WHERE token=?", (f.token,))
                elif scenario == "disabled-shop":
                    with f.db._lock, f.db.con:
                        f.db.con.execute("UPDATE shop_accounts SET enabled=0 WHERE user_id=? AND id=?",
                                         (f.uid, f.first["id"]))
                elif scenario == "deleted-connection":
                    deleted = result(f.send("DELETE", "/api/settings/ai/connection",
                        {"expected_revision": saved["revision"], "confirm": True}))["connection"]
                    assert deleted["initialized"] and not deleted["api_key_configured"]
                return tool_reply("fence-write", "knowledge_save", arguments)
            assert scenario == "control" and len(turns) == 4, "a fenced run reached another provider call"
            assert receipt["changed"] is True
            return {"choices": [{"message": {"role": "assistant", "content": "客服知识已按真实回执保存。"},
                                 "finish_reason": "stop"}]}

        with patch.object(f.ai, "requester", new=requester), \
                patch.object(service.tools, "execute", wraps=service.tools.execute) as execute:
            outcome = service.process_run(queued["run_id"])
        if scenario == "revoked-session":
            result(f.send("GET", path), 401)
            result(f.owner.get("/api/me"), 401)
            f.token = f.login(f.owner, "ops-owner")
        elif scenario == "disabled-shop":
            with f.db._lock, f.db.con:
                f.db.con.execute("UPDATE shop_accounts SET enabled=1 WHERE user_id=? AND id=?",
                                 (f.uid, f.first["id"]))
        final = result(f.send("GET", path))
        assert final == outcome
        names = [call.args[2] for call in execute.call_args_list]
        after = files(f.ai.storage.root)
        if scenario == "control":
            assert final["status"] == "succeeded" and final["changed_count"] == 1
            assert names == ["products_search", "knowledge_get", "knowledge_save"] and len(turns) == 4
            changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
            assert len(changed) == 1
            name = next(iter(changed))
            assert "ai_knowledge" in name and name.endswith("90101.json")
            assert json.loads(after[name][0])["published"]["knowledge"]["content"] == content
        else:
            assert names == ["products_search", "knowledge_get"] and len(turns) == 3
            assert after == before, f"{scenario} allowed a business-file write"
            assert final["changed_count"] == 0
            assert final["status"] == ("cancelled" if scenario == "cancel" else "failed"), final
            if error_code:
                assert final["error"]["source"] == "application" and final["error"]["code"] == error_code, final
                assert any(message.get("error") == final["error"] for message in f.messages(queued["session_id"]))
                result(f.send("POST", path + "/retry", {"request_id": f"fenced-retry-{scenario}"}), 409, "retry_not_allowed")
            if scenario == "generation":
                result(f.chat("stale-generation-chat", queued["session_id"]), 409, "session_stale")
        count = len(f.calls)
        assert f.finish(queued) == final and len(f.calls) == count
        assert files(f.ai.storage.root) == after


def legacy_lock_contract(f):
    from automation import rules_document
    rules = rules_document([{"keywords": ["使用"], "reply": "请阅读商品介绍"}])
    payload = {"keywords_json": json.dumps(rules)}
    key = f"automation-save:{f.uid}:{f.first['id']}"
    assert f.db.acquire_control_lease(key, "contract-blocker", lease_seconds=60, cooldown_seconds=0) == "acquired"
    before = files(f.ai.storage.root)
    try:
        result(f.send("PUT", "/api/config", payload), 409)
        assert files(f.ai.storage.root) == before
    finally:
        f.db.release_control_lease(key, "contract-blocker")
    real_write = f.app.write_secret

    def checked_write(*args, **kwargs):
        assert f.db.acquire_control_lease(key, "contract-contender", lease_seconds=1, cooldown_seconds=0) == "busy"
        return real_write(*args, **kwargs)

    with patch.object(f.app, "write_secret", side_effect=checked_write) as write:
        result(f.send("PUT", "/api/config", payload))
        write.assert_called_once()


def session_input_contract(f):
    # First messages omit session_id or use ''; null is not a valid strict string.
    before, count, changes = files(f.ai.storage.root), len(f.calls), f.db.con.total_changes
    errors = result(f.chat("null-session-rejected", session_id=None), 422)["detail"]
    assert any(error["loc"] == ["body", "session_id"] and error["type"] == "string_type" for error in errors)
    assert f.db.con.total_changes == changes and len(f.calls) == count
    assert files(f.ai.storage.root) == before
    for form in ("omitted", "empty"):
        payload = {**CHAT, "request_id": f"first-session-{form}"}
        if form == "empty":
            payload["session_id"] = ""
        queued = result(f.send("POST", "/api/ops/chat", payload), 202)
        f.durable(queued)
        failed = f.finish(queued)
        assert failed["status"] == "failed" and failed["error"]["code"] == "connection_unconfigured"
        assert failed["error"]["source"] == "application" and len(f.calls) == count
    assert files(f.ai.storage.root) == before


def main():
    with ExitStack() as guard:
        for target, attribute in ((socket, "create_connection"), (socket, "getaddrinfo"),
                                  (socket.socket, "connect"), (socket.socket, "connect_ex")):
            guard.enter_context(patch.object(target, attribute, no_network))
        import app
        fixture = None
        try:
            fixture = Fixture(app)
            resolver = lambda host, port, type=None: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
            with patch.object(fixture.ai, "requester", new=fixture.fake_agent_request), patch.object(fixture.ai, "resolver", new=resolver):
                settings_contract(fixture)
                print("settings/ops API: shared settings and durable missing-connection failure ok")
                session_id = sessions_contract(fixture)
                print("settings/ops API: sessions, full history, pagination and isolation ok")
                failures_contract(fixture, session_id)
                print("settings/ops API: provider errors, retry, auth and connection tombstone ok")
                in_flight_fences_contract(fixture)
                print("settings/ops API: real write control and in-flight stop/generation/revocation fences ok")
            legacy_lock_contract(fixture)
            session_input_contract(fixture)
            print("settings/ops API contract: ok (shared settings, durable runs, full history, isolation, errors, write fences; offline)")
        finally:
            if fixture is not None:
                fixture.close()
            app.db.con.close()
            app._api_process_lock.close()
            TEMP.cleanup()


if __name__ == "__main__":
    main()
