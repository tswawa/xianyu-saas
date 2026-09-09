#!/usr/bin/env python3
"""Offline durable-runner contracts using native agent_turn and receipt tools.

Uses a real temporary control DB, two independent connections where relevant,
and no app import, HTTP request, production files or model credentials.
"""
from __future__ import annotations

import copy
import hashlib
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ai_customer_service import AIServiceError  # noqa: E402
from db import DB, TOKEN_TTL_SECONDS  # noqa: E402
import operations  # noqa: E402
from operations import CLAIM_SECONDS, OperationsError, OperationsService, PAGE_SIZE  # noqa: E402


def assistant(content="查询完成。", calls=None, **extra):
    return {"role": "assistant", "content": content, "tool_calls": calls or [], **extra}


def call(identity, name="products_search", **arguments):
    return {"id": identity, "name": name, "arguments": arguments}


class InjectedCrash(BaseException):
    pass


class UpstreamError(AIServiceError):
    def __init__(self, status=401):
        super().__init__("upstream_error", 502, "模型拒绝了本次请求")
        self.upstream_status = status

    def public_detail(self):
        return {"source": "provider", "code": "upstream_error", "message": "模型不存在或上下文过长；sk-test-secret0123456789",
                "upstream_status": self.upstream_status, "upstream_code": "model_not_found", "upstream_type": "invalid_request_error",
                "upstream_request_id": "req-upstream-1"}


class StubAI:
    def __init__(self, fixture):
        self.fixture = fixture
        self.clock = lambda: fixture.now
        self.revision = 1
        self.calls = []
        self.outputs = []
        self.responder = None

    def _connection_generation(self, _scope):
        return ("user", self.revision, self.revision, "verified")

    def agent_turn(self, uid, sid, key, messages, tools):
        self.calls.append((uid, sid, key, copy.deepcopy(messages), copy.deepcopy(tools)))
        if self.responder:
            return self.responder(uid, sid, key, messages, tools)
        value = self.outputs.pop(0) if self.outputs else assistant()
        if isinstance(value, BaseException):
            raise value
        return value


class StubTools:
    """One small durable receipt store emulates the tools' atomic-write boundary."""
    def __init__(self, db, ai):
        self.db, self.ai = db, ai
        self.calls = []
        self.before = None
        self.after = None
        self.error_by_target = {}
        self.crash_target = None
        with db._lock, db.con:
            db.con.execute("""CREATE TABLE IF NOT EXISTS contract_receipts (
                run_id TEXT NOT NULL,call_id TEXT NOT NULL,args_json TEXT NOT NULL,result_json TEXT NOT NULL,
                PRIMARY KEY(run_id,call_id))""")

    def catalog(self, run):
        names = ["products_search", "products_get", "knowledge_get"]
        if "knowledge" in run["allowed_domains"]:
            names.extend(["knowledge_save", "knowledge_set_enabled"])
        return [{"name": name, "description": name,
                 "parameters": {"type": "object", "properties": {}, "additionalProperties": False}} for name in names]

    def execute(self, run, call_id, name, arguments, ensure_current):
        digest = json.dumps([name, arguments], ensure_ascii=False, sort_keys=True)
        with self.db._lock:
            old = self.db.con.execute("SELECT * FROM contract_receipts WHERE run_id=? AND call_id=?", (run["id"], call_id)).fetchone()
        if old:
            if old["args_json"] != digest:
                raise OperationsError("call_conflict", 409)
            return json.loads(old["result_json"])
        if self.before:
            self.before(run, call_id, name, arguments)
        ensure_current()
        # Receipt-only probes stop above and are not a second tool execution.
        self.calls.append((run["id"], call_id, name, copy.deepcopy(arguments)))
        if set(arguments) - {"target", "query", "revision"}:
            raise OperationsError("invalid_arguments")
        if arguments.get("target") in self.error_by_target:
            error = self.error_by_target[arguments["target"]]
            raise OperationsError(error, 503 if error == "storage_unavailable" else 409)
        changed = name in {"knowledge_save", "knowledge_set_enabled"}
        result = {"data": {"version": arguments.get("revision", 1), "target": arguments.get("target", "catalog")},
                  "summary": "配置已生效" if changed else "读取已完成", "changed": changed,
                  "targets": [{"id": arguments.get("target", "catalog"), "name": "测试商品"}]}
        with self.db._lock, self.db.con:
            self.db.con.execute("INSERT INTO contract_receipts VALUES (?,?,?,?)", (run["id"], call_id, digest, json.dumps(result, ensure_ascii=False)))
        if self.crash_target is not None and self.crash_target == arguments.get("target"):
            self.crash_target = None
            raise InjectedCrash("file committed before runner receipt")
        if self.after:
            self.after(run, call_id, name, arguments)
        return result


class Fixture:
    def __init__(self, directory):
        self.path = Path(directory) / "control.db"
        self.now = time.time() + 0.1
        self.db = DB(str(self.path))
        # Trusted test fixtures avoid expensive password hashing unrelated to
        # execution semantics. Production tables/schema are still used verbatim.
        with self.db._lock, self.db.con:
            self.db.con.executemany("INSERT INTO users(id,username,password_hash,role,disabled_at,expires_at,created_at) VALUES (?,?,?,'owner',NULL,?,?)",
                                   [(1, "owner-one", "unused", self.now + 86400, self.now), (2, "owner-two", "unused", self.now + 86400, self.now)])
            self.db.con.executemany("INSERT INTO shop_accounts(id,user_id,account_key,account_ref,generation,enabled,created_at,updated_at) VALUES (?,?,?,?,0,1,?,?)",
                                   [(11, 1, "shop-one", "verified-shop-A", self.now, self.now), (12, 1, "shop-two", "verified-shop-B", self.now, self.now), (21, 2, "shop-one", "verified-shop-C", self.now, self.now)])
            self.db.con.execute("CREATE TABLE ops_plans(id TEXT PRIMARY KEY,body TEXT)")
            self.db.con.execute("INSERT INTO ops_plans VALUES ('old-plan','legacy-keep')")
            self.db.con.execute("CREATE TABLE ops_items(id TEXT PRIMARY KEY,body TEXT)")
            self.db.con.execute("INSERT INTO ops_items VALUES ('old-item','legacy-item-keep')")
        self.token = "contract-token-user-one-" + "1" * 40
        self.token2 = "contract-token-user-two-" + "2" * 40
        with self.db._lock, self.db.con:
            self.db.con.executemany("INSERT INTO tokens(token,user_id,created_at) VALUES (?,?,?)", [(self.token, 1, self.now), (self.token2, 2, self.now)])
        self.auth = hashlib.sha256(self.token.encode()).hexdigest()
        self.auth2 = hashlib.sha256(self.token2.encode()).hexdigest()
        self.scope = 1, 11, "shop-one"
        self.ai = StubAI(self)
        self.service = OperationsService(self.db, self.ai)
        self.sequence = 0
        self.extra_dbs = []

    def open(self):
        db = DB(str(self.path))
        self.extra_dbs.append(db)
        ai = StubAI(self)
        return db, ai, OperationsService(db, ai)

    def chat(self, message="查询本店商品", **kwargs):
        self.sequence += 1
        kwargs.setdefault("request_id", f"request-{self.sequence}")
        kwargs.setdefault("auth_session_hash", self.auth)
        return self.service.chat(*self.scope, message=message, **kwargs)

    def execute(self, message="查询本店商品", **kwargs):
        run = self.chat(message, **kwargs)
        return self.service.process_run(run["run_id"])

    def row(self, run_id):
        with self.db._lock:
            return self.db.con.execute("SELECT * FROM ops_runs WHERE id=?", (run_id,)).fetchone()

    def close(self):
        for db in self.extra_dbs:
            db.con.close()
        self.db.con.close()


class RuntimeContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ops-runtime-")
        self.tools_patch = patch.object(operations, "OperationsTools", StubTools)
        self.tools_patch.start()
        self.network_patch = patch.object(socket, "create_connection", side_effect=AssertionError("offline contract"))
        self.network_patch.start()
        self.f = Fixture(self.temp.name)

    def tearDown(self):
        self.f.close()
        self.network_patch.stop()
        self.tools_patch.stop()
        self.temp.cleanup()

    def assert_code(self, expected, callback):
        with self.assertRaises(OperationsError) as caught:
            callback()
        self.assertEqual(caught.exception.code, expected)

    def test_read_only_get_and_atomic_chat_preserve_legacy_tables(self):
        f = self.f
        self.assertEqual(f.service.current_session(*f.scope), {"session": None, "active_run": None})
        self.assertEqual(f.db.con.execute("SELECT COUNT(*) FROM ops_sessions").fetchone()[0], 0)
        with patch.object(f.db, "enqueue_job", side_effect=AssertionError("internal commit forbidden")):
            run = f.chat("原文\n" + "长消息" * 3000)
        job = f.db.con.execute("SELECT * FROM jobs WHERE kind='ops_run'").fetchone()
        self.assertEqual(json.loads(job["payload_json"]), {"run_id": run["run_id"]})
        self.assertEqual((job["user_id"], job["account_id"], job["status"]), (1, 11, "queued"))
        self.assertEqual(f.row(run["run_id"])["auth_session_hash"], f.auth)
        for table in ["ops_sessions", "ops_runs", "ops_requests", "ops_messages", "ops_events", "ops_turns", "ops_dispatches"]:
            self.assertNotIn(f.token, repr([tuple(row) for row in f.db.con.execute("SELECT * FROM " + table)]))
        self.assertEqual(f.db.con.execute("SELECT body FROM ops_plans").fetchone()[0], "legacy-keep")
        self.assertEqual(f.db.con.execute("SELECT body FROM ops_items").fetchone()[0], "legacy-item-keep")
        self.assertNotIn("app", sys.modules)

    def test_enqueue_failure_rolls_back_every_chat_record(self):
        f = self.f
        with f.db.con:
            f.db.con.execute("CREATE TRIGGER fail_ops_queue BEFORE INSERT ON jobs WHEN NEW.kind='ops_run' BEGIN SELECT RAISE(ABORT,'queue unavailable'); END")
        with self.assertRaises(Exception):
            f.chat()
        for table in ["ops_sessions", "ops_runs", "ops_requests", "ops_messages", "ops_events"]:
            self.assertEqual(f.db.con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0, table)

    def test_idempotency_and_scope_ownership_survive_new_service(self):
        f = self.f
        run = f.chat(request_id="same-request")
        _db, _ai, service = f.open()
        duplicate = service.chat(*f.scope, request_id="same-request", message="查询本店商品", auth_session_hash=f.auth)
        self.assertEqual(run, duplicate)
        self.assertEqual(f.db.con.execute("SELECT COUNT(*) FROM jobs WHERE kind='ops_run'").fetchone()[0], 1)
        self.assert_code("request_conflict", lambda: f.chat("不同请求", request_id="same-request"))
        self.assert_code("run_not_found", lambda: service.get_run(1, 12, "shop-two", run["run_id"]))
        self.assert_code("run_not_found", lambda: service.get_run(2, 21, "shop-one", run["run_id"]))
        self.assert_code("session_not_found", lambda: service.messages(2, 21, "shop-one", run["session_id"]))
        self.assert_code("scope_invalid", lambda: service.get_run(2, 11, "shop-one", run["run_id"]))

    def test_single_active_run_and_new_conversation_never_delete_history(self):
        f = self.f
        run = f.chat()
        self.assert_code("session_busy", lambda: f.chat(session_id=run["session_id"]))
        created = f.service.create_session(*f.scope)
        self.assertNotEqual(created["session"]["id"], run["session_id"])
        self.assertEqual(f.service.current_session(*f.scope)["session"]["id"], created["session"]["id"])
        f.service.process_run(run["run_id"])
        self.assertEqual(f.service.current_session(*f.scope)["session"]["id"], created["session"]["id"])
        self.assertEqual(f.service.messages(*f.scope, run["session_id"])["messages"][0]["content"], "查询本店商品")

    def test_native_turn_roundtrip_private_data_and_receipt_counts(self):
        f = self.f
        f.ai.outputs = [assistant("", [call("lookup"), call("save", "knowledge_save", target="101")], provider_data={"opaque": "private-continuation"}), assistant("配置已按回执完成。")]
        result = f.execute("给商品101更新客服知识")
        self.assertEqual((result["status"], result["changed_count"], result["failed_count"]), ("succeeded", 1, 0))
        history = f.ai.calls[1][3]
        self.assertEqual([message["role"] for message in history], ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual(history[2]["provider_data"], {"opaque": "private-continuation"})
        self.assertEqual([item["tool_call_id"] for item in history[3:]], ["lookup", "save"])
        self.assertTrue(all(isinstance(item["content"], dict) for item in history[3:]))
        messages = f.service.messages(*f.scope, result["session_id"])
        public = json.dumps([messages, result], ensure_ascii=False)
        self.assertNotIn("private-continuation", public)
        self.assertNotIn('"arguments"', public)
        self.assertEqual(len([item for item in messages["messages"] if item.get("kind") == "tool"]), 2)
        self.assertEqual(f.service.process_run(result["id"])["changed_count"], 1)
        self.assertEqual(len(f.service.tools.calls), 2)

    def test_more_than_twenty_writes_and_unlimited_distinct_turns(self):
        f = self.f
        f.ai.outputs = [assistant("", [call(f"save-{index}", "knowledge_save", target=str(index))]) for index in range(32)] + [assistant("已按回执完成所有商品。")]
        result = f.execute("给所有商品更新客服知识")
        self.assertEqual((result["status"], result["changed_count"]), ("succeeded", 32))
        self.assertEqual(len(f.ai.calls), 33)
        self.assertEqual(len(f.service.tools.calls), 32)
        cursor, events = 0, []
        while True:
            page = f.service.get_run(*f.scope, result["id"], after_seq=cursor)
            events.extend(page["events"])
            if page["next_seq"] == cursor:
                break
            cursor = page["next_seq"]
        self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))

    def test_full_server_history_and_page_cursor_do_not_clip(self):
        f = self.f
        first = f.chat("first-marker:" + "首条长内容" * 2200)
        f.service.process_run(first["run_id"])
        # Seed additional authenticated server history, not browser-provided
        # history. This is deliberately longer than both legacy and page limits.
        with f.service._transaction():
            run = f.row(first["run_id"])
            for index in range(PAGE_SIZE + 35):
                content = f"older-user-{index}:" + "完整" * 2500
                f.service._message(run, role="user", content=content, canonical={"role": "user", "content": content}, dedupe=f"seed-{index}")
        result = f.execute("current-marker:" + "当前原文" * 2200, session_id=first["session_id"])
        history = f.ai.calls[-1][3]
        users = [item["content"] for item in history if item["role"] == "user"]
        self.assertEqual(len(users), PAGE_SIZE + 37)
        self.assertTrue(users[0].startswith("first-marker:"))
        self.assertEqual(len(users[0]), len("first-marker:" + "首条长内容" * 2200))
        self.assertEqual(users[-1], "current-marker:" + "当前原文" * 2200)
        all_messages, cursor = [], 0
        while True:
            page = f.service.messages(*f.scope, result["session_id"], cursor=cursor)
            all_messages.extend(page["messages"])
            if page["next_cursor"] is None:
                break
            self.assertGreater(page["next_cursor"], cursor)
            cursor = page["next_cursor"]
        self.assertGreater(len(all_messages), PAGE_SIZE)
        self.assertEqual(len({item["id"] for item in all_messages}), len(all_messages))
        self.assertEqual(all_messages[0]["content"], users[0])

    def test_readonly_request_cannot_dispatch_write_even_model_requests_it(self):
        f = self.f
        f.ai.outputs = [assistant("", [call("unsafe-write", "knowledge_save", target="101")]), assistant("无法执行未授权修改。")]
        result = f.execute("只读分析客服知识，不要修改")
        self.assertEqual((result["status"], result["changed_count"], result["failed_count"]), ("failed", 0, 1))
        self.assertEqual(f.service.tools.calls, [])
        self.assertFalse(f.ai.calls[1][3][-1]["content"]["ok"])
        self.assertEqual(f.ai.calls[1][3][-1]["content"]["error"]["code"], "tool_not_allowed")

    def test_unknown_tools_bad_arguments_and_no_progress_are_visible_failures(self):
        f = self.f
        f.ai.outputs = [assistant("", [call("unknown", "execute_shell", command="bad")]), assistant("未执行。")]
        result = f.execute("查询商品")
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(f.service.tools.calls, [])
        f.ai.outputs = [assistant("", [call(f"loop-{index}", "products_search", forbidden="extra")]) for index in range(5)]
        result = f.execute("查看商品")
        self.assertEqual(result["error"]["code"], "no_progress")
        self.assertEqual(result["changed_count"], 0)
        self.assertEqual(result["failed_count"], 3)

    def test_repeated_same_write_with_new_call_id_is_not_replayed(self):
        f = self.f
        f.ai.outputs = [assistant("", [call(f"repeat-{index}", "knowledge_save", target="101")]) for index in range(5)]
        result = f.execute("更新商品101客服知识")
        self.assertEqual(result["error"]["code"], "no_progress")
        self.assertEqual(result["changed_count"], 1)
        self.assertEqual(len(f.service.tools.calls), 1)
        self.assertEqual(result["status"], "partial_failed")

    def test_pure_text_write_claim_is_not_a_success_receipt_and_can_clarify(self):
        f = self.f
        f.ai.outputs = [assistant("已全部修改完成。")]
        result = f.execute("更新商品客服知识")
        self.assertEqual((result["status"], result["changed_count"]), ("waiting_user", 0))
        f.ai.outputs = [assistant("", [call("clarified-save", "knowledge_save", target="101")]), assistant("配置回执已确认。")]
        continued = f.execute("内容是新使用说明，继续", session_id=result["session_id"])
        self.assertEqual((continued["status"], continued["changed_count"]), ("succeeded", 1))
        self.assertEqual(json.loads(f.row(continued["id"])["allowed_domains_json"]), ["knowledge"])
        f.ai.outputs = [assistant("只读分析结果。")]
        read = f.execute("只读查看知识，不要修改", session_id=result["session_id"])
        self.assertEqual(json.loads(f.row(read["id"])["allowed_domains_json"]), [])
        self.assertEqual(read["changed_count"], 0)

    def test_provider_error_statuses_are_not_site_auth_and_explicit_retry(self):
        f = self.f
        for status in (400, 401, 403, 429, 500):
            f.ai.outputs = [UpstreamError(status)]
            result = f.execute()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["source"], "provider")
            self.assertEqual(result["error"]["upstream_status"], status)
            self.assertEqual(result["error"]["upstream_code"], "model_not_found")
            self.assertNotIn("sk-test", result["error"]["message"])
            self.assertEqual(len(f.ai.outputs), 0)
        calls = len(f.ai.calls)
        self.assertEqual(f.service.process_run(result["id"])["status"], "failed")
        self.assertEqual(len(f.ai.calls), calls)
        retry = f.service.retry(*f.scope, result["id"], "retry-model", f.auth)
        self.assertEqual(retry["run_id"], result["id"])
        self.assertEqual(f.service.process_run(result["id"])["status"], "succeeded")
        self.assertEqual(len(f.ai.calls), calls + 1)
        self.assertEqual(f.service.retry(*f.scope, result["id"], "retry-model", f.auth)["status"], "succeeded")

    def test_partial_failure_keeps_receipts_and_retry_same_step_id_only(self):
        f = self.f
        f.service.tools.error_by_target = {"102": "storage_unavailable"}
        f.ai.outputs = [assistant("", [call("first-save", "knowledge_save", target="101"), call("second-save", "knowledge_save", target="102")]), assistant("部分失败。")]
        result = f.execute("更新全部商品客服知识")
        self.assertEqual((result["status"], result["changed_count"], result["failed_count"]), ("partial_failed", 1, 1))
        del f.service.tools.error_by_target["102"]
        f.ai.outputs = [assistant("恢复已完成。")]
        f.service.retry(*f.scope, result["id"], "retry-partial", f.auth)
        recovered = f.service.process_run(result["id"])
        self.assertEqual((recovered["status"], recovered["changed_count"], recovered["failed_count"]), ("succeeded", 2, 0))
        identities = [item[1] for item in f.service.tools.calls]
        self.assertEqual(identities.count("first-save"), 1)
        self.assertEqual(identities.count("second-save"), 2)
        self.assertEqual(len(f.ai.calls), 3)
        self.assertEqual(f.db.con.execute("SELECT COUNT(*) FROM contract_receipts WHERE run_id=?", (result["id"],)).fetchone()[0], 2)

    def test_cancel_between_atomic_steps_keeps_first_and_never_starts_second(self):
        f = self.f
        f.ai.outputs = [assistant("", [call("first", "knowledge_save", target="101"), call("second", "knowledge_save", target="102")])]
        f.service.tools.after = lambda run, *_args: f.service.cancel(*f.scope, run["id"])
        result = f.execute("更新全部商品客服知识")
        self.assertEqual((result["status"], result["changed_count"]), ("cancelled", 1))
        self.assertEqual([item[1] for item in f.service.tools.calls], ["first"])
        f.service.tools.after = None
        f.ai.outputs = [assistant("已恢复剩余步骤。")]
        f.service.retry(*f.scope, result["id"], "retry-stopped", f.auth)
        recovered = f.service.process_run(result["id"])
        self.assertEqual((recovered["status"], recovered["changed_count"]), ("succeeded", 2))
        self.assertEqual([item[1] for item in f.service.tools.calls], ["first", "second"])

    def test_cancel_during_prewrite_callback_records_no_false_atomic_write(self):
        f = self.f
        f.ai.outputs = [assistant("", [call("first", "knowledge_save", target="101")])]
        f.service.tools.before = lambda run, *_args: f.service.cancel(*f.scope, run["id"])
        result = f.execute("更新客服知识")
        self.assertEqual((result["status"], result["changed_count"]), ("cancelled", 0))
        self.assertEqual(f.db.con.execute("SELECT COUNT(*) FROM contract_receipts").fetchone()[0], 0)

    def test_queued_cancel_and_session_tokens_fail_closed(self):
        f = self.f
        self.assert_code("session_invalid", lambda: f.chat(auth_session_hash=""))
        self.assert_code("session_invalid", lambda: f.chat(auth_session_hash=f.auth2))
        run = f.chat()
        result = f.service.cancel(*f.scope, run["run_id"])
        self.assertEqual(result["status"], "cancelled")
        f.service.process_run(run["run_id"])
        self.assertEqual(f.ai.calls, [])
        run = f.chat()
        with f.db.con:
            f.db.con.execute("DELETE FROM tokens WHERE token=?", (f.token,))
        result = f.service.process_run(run["run_id"])
        self.assertEqual(result["error"]["code"], "session_invalid")
        self.assertEqual(f.ai.calls, [])

    def test_token_expiry_rechecked_after_model_before_tools(self):
        f = self.f
        def model(*_args):
            f.now += TOKEN_TTL_SECONDS + 1
            return assistant("", [call("must-not-write", "knowledge_save", target="101")])
        f.ai.responder = model
        result = f.execute("更新客服知识")
        self.assertEqual(result["error"]["code"], "session_invalid")
        self.assertEqual(f.service.tools.calls, [])

    def test_scope_connection_permission_disabled_fences_after_model(self):
        f = self.f
        actions = {
            "scope_invalid": lambda: f.db.con.execute("UPDATE shop_accounts SET generation=generation+1 WHERE id=11"),
            "connection_changed": lambda: setattr(f.ai, "revision", f.ai.revision + 1),
            "permission_denied": lambda: None,
        }
        for code, mutate in actions.items():
            f.ai.responder = None
            run = f.chat("更新客服知识")
            def model(*_args):
                with f.db.con:
                    mutate()
                return assistant("", [call("forbidden", "knowledge_save", target="101")])
            f.ai.responder = model
            if code == "permission_denied":
                revoked = False
                def revoke(*_args):
                    nonlocal revoked
                    revoked = True
                    return assistant("", [call("forbidden", "knowledge_save", target="101")])
                f.ai.responder = revoke
                # Persisting a response does not let it bypass revoked access.
                real = operations.has_permission
                def permission(*args, **kwargs):
                    return False if revoked else real(*args, **kwargs)
                with patch.object(operations, "has_permission", side_effect=permission):
                    result = f.service.process_run(run["run_id"])
            else:
                result = f.service.process_run(run["run_id"])
            self.assertEqual(result["error"]["code"], code)
            self.assertEqual(f.service.tools.calls, [])
        f.ai.responder = None
        run = f.chat()
        with f.db.con:
            f.db.con.execute("UPDATE users SET disabled_at=? WHERE id=1", (f.now,))
        self.assertEqual(f.service.process_run(run["run_id"])["error"]["code"], "scope_invalid")

    def test_generation_history_is_readable_but_never_rebound(self):
        f = self.f
        result = f.execute()
        with f.db.con:
            f.db.con.execute("UPDATE shop_accounts SET generation=generation+1,account_ref='replacement-shop' WHERE id=11")
        self.assertEqual(f.service.get_run(*f.scope, result["id"])["id"], result["id"])
        self.assertEqual(f.service.messages(*f.scope, result["session_id"])["messages"][0]["content"], "查询本店商品")
        self.assert_code("session_stale", lambda: f.chat(session_id=result["session_id"]))
        new_session = f.service.create_session(*f.scope)["session"]["id"]
        new_run = f.chat(session_id=new_session)
        self.assertEqual(f.row(new_run["run_id"])["generation"], 1)
        self.assertEqual(f.row(result["id"])["account_ref"], "verified-shop-A")

    def test_crash_after_file_commit_recovers_same_tool_without_second_model(self):
        f = self.f
        f.ai.outputs = [assistant("", [call("first", "knowledge_save", target="101"), call("second", "knowledge_save", target="102")])]
        f.service.tools.crash_target = "102"
        run = f.chat("更新全部客服知识")
        with self.assertRaises(InjectedCrash):
            f.service.process_run(run["run_id"])
        self.assertEqual(f.db.con.execute("SELECT COUNT(*) FROM contract_receipts").fetchone()[0], 2)
        self.assertEqual(f.row(run["run_id"])["phase"], "dispatch")
        f.now += CLAIM_SECONDS + 1
        _db, ai, recovered_service = f.open()
        ai.outputs = [assistant("全部回执核对完成。")]
        result = recovered_service.process_run(run["run_id"])
        self.assertEqual((result["status"], result["changed_count"]), ("succeeded", 2))
        self.assertEqual(recovered_service.tools.calls, [])
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual([item["tool_call_id"] for item in ai.calls[0][3] if item["role"] == "tool"], ["first", "second"])

    def test_cancel_after_crash_still_recovers_already_committed_receipt(self):
        f = self.f
        f.ai.outputs = [assistant("", [call("first", "knowledge_save", target="101"), call("second", "knowledge_save", target="102")])]
        f.service.tools.crash_target = "101"
        run = f.chat("更新全部客服知识")
        with self.assertRaises(InjectedCrash):
            f.service.process_run(run["run_id"])
        self.assertEqual(f.service.cancel(*f.scope, run["run_id"])["status"], "cancel_requested")
        f.now += CLAIM_SECONDS + 1
        _db, ai, service = f.open()
        result = service.process_run(run["run_id"])
        self.assertEqual((result["status"], result["changed_count"]), ("cancelled", 1))
        self.assertEqual(ai.calls, [])
        self.assertEqual(service.tools.calls, [])

    def test_two_consumers_claim_one_run_even_while_model_is_blocked(self):
        f = self.f
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        def model(*_args):
            entered.set()
            if not release.wait(10):
                raise AssertionError("test synchronization timeout")
            return assistant()
        f.ai.responder = model
        run = f.chat()
        _db, other_ai, other = f.open()
        def work():
            try:
                results.append(f.service.process_run(run["run_id"]))
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=work)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            competing = other.process_run(run["run_id"])
            self.assertEqual(competing["status"], "running")
            self.assertEqual(other_ai.calls, [])
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["status"], "succeeded")
        self.assertEqual(len(f.ai.calls), 1)

    def test_live_job_heartbeat_fences_shorter_run_lease_and_replaces_dead_owner(self):
        f = self.f
        entered, release = threading.Event(), threading.Event()
        first_results = []
        f.ai.responder = lambda *_args: (entered.set(), release.wait(10), assistant("late response"))[-1]
        run = f.chat()
        job_id = f.row(run["run_id"])["job_id"]
        self.assertIsNotNone(f.db.claim_job(job_id, "owner-A", lease_seconds=3600, now=f.now))
        thread = threading.Thread(target=lambda: first_results.append(f.service.process_run(run["run_id"])))
        thread.start()
        _db, ai, second = f.open()
        try:
            self.assertTrue(entered.wait(10))
            f.now += CLAIM_SECONDS + 1
            self.assertEqual(second.process_run(run["run_id"])["status"], "running")
            self.assertEqual(ai.calls, [])
            with f.db.con:
                f.db.con.execute("UPDATE jobs SET lease_owner='owner-B',lease_until=? WHERE id=?", (f.now + 3600, job_id))
            self.assertEqual(second.process_run(run["run_id"])["status"], "succeeded")
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(ai.calls), 1)
        messages = f.service.messages(*f.scope, run["session_id"])["messages"]
        self.assertFalse(any(item["content"] == "late response" for item in messages))

    def test_lease_loss_returns_durable_active_state_without_false_completion(self):
        f = self.f
        run = f.chat()
        result = f.service.process_run(run["run_id"], ensure_job=lambda: False)
        self.assertEqual(result["status"], "running")
        self.assertEqual(f.ai.calls, [])
        self.assertEqual(f.row(run["run_id"])["claim_until"], 0)
        self.assertEqual(f.service.process_run(run["run_id"])["status"], "succeeded")

    def assert_protocol(self, history):
        pending = []
        for message in history:
            if message["role"] == "tool":
                self.assertTrue(pending, message)
                self.assertEqual(message["tool_call_id"], pending.pop(0))
            else:
                self.assertEqual(pending, [], message)
                pending = [item["id"] for item in message.get("tool_calls", [])]
        self.assertEqual(pending, [])

    def test_same_physical_receipt_across_distinct_arguments_counts_once(self):
        f = self.f
        original = f.service.tools.execute
        def receipt(*args):
            value = original(*args)
            value["data"]["receipt_id"] = "one-physical-write"
            return value
        f.ai.outputs = [assistant("", [call("alias-A", "knowledge_save", target="ref-A"), call("alias-B", "knowledge_save", target="ref-B")]), assistant("回执已核对。")]
        with patch.object(f.service.tools, "execute", side_effect=receipt):
            result = f.execute("更新全部商品客服知识")
        self.assertEqual((result["status"], result["changed_count"]), ("succeeded", 1))
        rows = f.db.con.execute("SELECT * FROM ops_dispatches ORDER BY rowid").fetchall()
        self.assertNotEqual(rows[0]["signature"], rows[1]["signature"])
        self.assertEqual((rows[0]["changed"], rows[1]["changed"], rows[1]["replay_of"]), (1, 0, rows[0]["id"]))
        self.assertEqual(f.db.con.execute("SELECT COUNT(*) FROM ops_receipts").fetchone()[0], 1)
        audits = [json.loads(row[0]) for row in f.db.con.execute("SELECT metadata_json FROM audit_log")]
        self.assertEqual(sum(row["changed"] for row in audits), 1)
        self.assert_protocol(f.ai.calls[-1][3])

    def test_cancel_after_model_persists_calls_before_stop_and_retry(self):
        f = self.f
        run = f.chat("更新全部商品客服知识")
        def model(*_args):
            f.service.cancel(*f.scope, run["run_id"])
            return assistant("", [call("pending-A", "knowledge_save", target="101"), call("pending-B", "knowledge_save", target="102")], provider_data={"opaque": "native-secret"})
        f.ai.responder = model
        stopped = f.service.process_run(run["run_id"])
        self.assertEqual((stopped["status"], stopped["changed_count"], stopped["recoverable"]), ("cancelled", 0, True))
        self.assertEqual(f.service.tools.calls, [])
        history = f.service._history(f.row(run["run_id"]))
        self.assert_protocol(history)
        self.assertEqual([item["tool_call_id"] for item in history if item["role"] == "tool"], ["pending-A", "pending-B"])
        self.assertTrue(all(item["content"]["not_executed"] for item in history if item["role"] == "tool"))
        self.assertNotIn("native-secret", json.dumps(f.service.messages(*f.scope, run["session_id"])))
        intents = [tuple(row) for row in f.db.con.execute("SELECT id,call_id,arguments_json,signature FROM ops_dispatches ORDER BY rowid")]
        f.ai.responder = None
        f.ai.outputs = [assistant("恢复回执已确认。")]
        f.service.retry(*f.scope, run["run_id"], "resume-persisted-model", f.auth)
        result = f.service.process_run(run["run_id"])
        self.assertEqual((result["status"], result["changed_count"]), ("succeeded", 2))
        self.assertEqual(len(f.ai.calls), 2)
        self.assertEqual(intents, [tuple(row) for row in f.db.con.execute("SELECT id,call_id,arguments_json,signature FROM ops_dispatches ORDER BY rowid")])
        resumed = f.ai.calls[-1][3]
        self.assert_protocol(resumed)
        self.assertTrue(all(item["content"]["ok"] for item in resumed if item["role"] == "tool"))
        self.assertEqual(resumed[2]["provider_data"], {"opaque": "native-secret"})

    def test_consumer_interrupt_after_model_resumes_dispatch_without_rebilling(self):
        f = self.f
        for reason in ("runner_interrupted", "job_lease_lost"):
            with self.subTest(reason=reason):
                initial_calls = len(f.ai.calls)
                f.ai.outputs = [assistant("", [call(reason, "knowledge_save", target=reason)], provider_data={"opaque": reason})]
                run = f.chat("更新全部商品客服知识")
                job_id = f.row(run["run_id"])["job_id"]
                self.assertIsNotNone(f.db.claim_job(job_id, reason, lease_seconds=3600, now=f.now))
                def ensure_job():
                    if len(f.ai.calls) > initial_calls:
                        raise RuntimeError(reason)
                paused = f.service.process_run(run["run_id"], ensure_job)
                row = f.row(run["run_id"])
                self.assertEqual((paused["status"], row["phase"], row["claim_token"], row["job_owner"]), ("running", "dispatch", None, ""))
                self.assertEqual(f.db.con.execute("SELECT status FROM ops_dispatches WHERE run_id=?", (run["run_id"],)).fetchone()[0], "pending")
                _db, ai, service = f.open()
                ai.outputs = [assistant("恢复结果已完成。")]
                result = service.process_run(run["run_id"])
                self.assertEqual((result["status"], result["changed_count"]), ("succeeded", 1))
                self.assertEqual(len(ai.calls), 1)
                self.assert_protocol(ai.calls[0][3])
                self.assertEqual(ai.calls[0][3][2]["provider_data"], {"opaque": reason})
                self.assertEqual(len(service.tools.calls), 1)

    def test_queue_lease_replaced_after_response_checkpoints_before_new_run_claim(self):
        f = self.f
        run = f.chat("更新全部商品客服知识")
        job_id = f.row(run["run_id"])["job_id"]
        self.assertIsNotNone(f.db.claim_job(job_id, "owner-A", lease_seconds=3600, now=f.now))
        def model(*_args):
            with f.db.con:
                f.db.con.execute("UPDATE jobs SET lease_owner='owner-B',lease_until=? WHERE id=?", (f.now + 3600, job_id))
            return assistant("", [call("already-paid", "knowledge_save", target="101")])
        f.ai.responder = model
        paused = f.service.process_run(run["run_id"])
        self.assertEqual((paused["status"], f.row(run["run_id"])["phase"], f.row(run["run_id"])["claim_token"]), ("running", "dispatch", None))
        self.assertEqual(f.service.tools.calls, [])
        _db, ai, service = f.open()
        ai.outputs = [assistant("已恢复收到的工具回合。")]
        result = service.process_run(run["run_id"])
        self.assertEqual((result["status"], result["changed_count"]), ("succeeded", 1))
        self.assertEqual(len(ai.calls), 1)
        self.assert_protocol(ai.calls[0][3])
        self.assertEqual([item["tool_call_id"] for item in ai.calls[0][3] if item["role"] == "tool"], ["already-paid"])

    def test_replaced_ops_claim_rejects_old_response_while_new_owner_is_running(self):
        f = self.f
        entered_a, entered_b, release_a, release_b = (threading.Event() for _ in range(4))
        results_a, results_b, errors = [], [], []
        def old_model(*_args):
            entered_a.set()
            if not release_a.wait(10):
                raise AssertionError("old model synchronization timeout")
            return assistant("old-owner-response", [call("old-owner-call")])
        def new_model(*_args):
            entered_b.set()
            if not release_b.wait(10):
                raise AssertionError("new model synchronization timeout")
            return assistant("new-owner-response")
        f.ai.responder = old_model
        run = f.chat()
        job_id = f.row(run["run_id"])["job_id"]
        self.assertIsNotNone(f.db.claim_job(job_id, "owner-A", lease_seconds=3600, now=f.now))
        _db, ai, second = f.open()
        ai.responder = new_model
        def work(service, results):
            try:
                results.append(service.process_run(run["run_id"]))
            except BaseException as error:
                errors.append(error)
        thread_a = threading.Thread(target=work, args=(f.service, results_a))
        thread_b = threading.Thread(target=work, args=(second, results_b))
        thread_a.start()
        started_b = False
        try:
            self.assertTrue(entered_a.wait(10))
            with f.db.con:
                f.db.con.execute("UPDATE jobs SET lease_owner='owner-B',lease_until=? WHERE id=?", (f.now + 3600, job_id))
            thread_b.start()
            started_b = True
            self.assertTrue(entered_b.wait(10))
            claim_b = f.row(run["run_id"])["claim_token"]
            release_a.set()
            thread_a.join(10)
            self.assertFalse(thread_a.is_alive())
            self.assertEqual(results_a[0]["status"], "running")
            self.assertEqual(f.row(run["run_id"])["claim_token"], claim_b)
            turn = f.db.con.execute("SELECT assistant_json FROM ops_turns WHERE run_id=?", (run["run_id"],)).fetchone()
            self.assertIsNone(turn[0])
            self.assertEqual(f.db.con.execute("SELECT COUNT(*) FROM ops_dispatches WHERE run_id=?", (run["run_id"],)).fetchone()[0], 0)
        finally:
            release_a.set()
            release_b.set()
            thread_a.join(10)
            if started_b:
                thread_b.join(10)
        self.assertEqual(errors, [])
        self.assertFalse(thread_b.is_alive())
        self.assertEqual(results_b[0]["status"], "succeeded")
        public = json.dumps(f.service.messages(*f.scope, run["session_id"]))
        self.assertNotIn("old-owner-response", public)
        self.assertIn("new-owner-response", public)

    def test_received_terminal_text_survives_consumer_stop_without_another_model(self):
        f = self.f
        f.ai.outputs = [assistant("原模型最终答复。", provider_data={"private": "continuation"})]
        run = f.chat()
        paused = f.service.process_run(run["run_id"], lambda: not bool(f.ai.calls))
        self.assertEqual(paused["status"], "running")
        _db, ai, service = f.open()
        ai.responder = lambda *_args: self.fail("已收到的最终答复不得重复请求模型")
        self.assertEqual(service.process_run(run["run_id"])["status"], "succeeded")
        self.assertEqual(ai.calls, [])
        contents = [item["content"] for item in service.messages(*f.scope, run["session_id"])["messages"]]
        self.assertEqual(contents.count("原模型最终答复。"), 1)

    def test_cancel_released_claim_reconciles_receipt_then_restores_original_protocol(self):
        f = self.f
        f.ai.outputs = [assistant("", [call("committed", "knowledge_save", target="101"), call("unstarted", "knowledge_save", target="102")])]
        f.service.tools.after = lambda *_args: (_ for _ in ()).throw(operations._LostClaim())
        run = f.chat("更新全部商品客服知识")
        paused = f.service.process_run(run["run_id"])
        self.assertEqual((paused["status"], paused["changed_count"], f.row(run["run_id"])["claim_token"]), ("running", 0, None))
        self.assertEqual(f.service.cancel(*f.scope, run["run_id"])["status"], "cancel_requested")
        _db, ai, service = f.open()
        stopped = service.process_run(run["run_id"])
        self.assertEqual((stopped["status"], stopped["changed_count"], stopped["recoverable"]), ("cancelled", 1, True))
        self.assertEqual(ai.calls, [])
        self.assertEqual(service.tools.calls, [])
        self.assert_protocol(service._history(f.row(run["run_id"])))
        service.retry(*f.scope, run["run_id"], "resume-reconciled", f.auth)
        ai.outputs = [assistant("两步完成。")]
        finished = service.process_run(run["run_id"])
        self.assertEqual((finished["status"], finished["changed_count"]), ("succeeded", 2))
        self.assertEqual([item[1] for item in service.tools.calls], ["unstarted"])
        self.assert_protocol(ai.calls[0][3])

    def test_retry_dispatch_cancel_after_crash_reconciles_later_receipt_first(self):
        f = self.f
        f.service.tools.error_by_target = {"102": "storage_unavailable"}
        f.ai.outputs = [assistant("", [call("first", "knowledge_save", target="101"), call("second", "knowledge_save", target="102")]), assistant("第二步暂时失败。")]
        result = f.execute("更新全部商品客服知识")
        f.service.tools.error_by_target.clear()
        f.service.retry(*f.scope, result["id"], "resume-crash", f.auth)
        f.service.tools.crash_target = "102"
        with self.assertRaises(InjectedCrash):
            f.service.process_run(result["id"])
        self.assertEqual(f.row(result["id"])["phase"], "retry_dispatch")
        f.service.cancel(*f.scope, result["id"])
        f.now += CLAIM_SECONDS + 1
        _db, ai, service = f.open()
        stopped = service.process_run(result["id"])
        self.assertEqual((stopped["status"], stopped["changed_count"], stopped["failed_count"]), ("cancelled", 2, 0))
        self.assertEqual(ai.calls, [])
        self.assertEqual(service.tools.calls, [])
        self.assert_protocol(service._history(f.row(result["id"])))

    def test_retry_keeps_failure_events_but_updates_original_tool_result_links(self):
        f = self.f
        f.service.tools.error_by_target = {"102": "storage_unavailable"}
        f.ai.outputs = [assistant("", [call("first", "knowledge_save", target="101"), call("second", "knowledge_save", target="102")]), assistant("部分失败。")]
        result = f.execute("更新全部商品客服知识")
        original = [(item["id"], item["content"]) for item in f.service.messages(*f.scope, result["session_id"])["messages"] if item.get("status") == "failed"]
        self.assertTrue(original)
        f.service.tools.error_by_target.clear()
        f.service.retry(*f.scope, result["id"], "resume-protocol", f.auth)
        f.ai.outputs = [assistant("恢复已完成。")]
        f.service.process_run(result["id"])
        self.assert_protocol(f.ai.calls[-1][3])
        self.assertEqual([item["tool_call_id"] for item in f.ai.calls[-1][3] if item["role"] == "tool"], ["first", "second"])
        self.assertTrue(all(item["content"]["ok"] for item in f.ai.calls[-1][3] if item["role"] == "tool"))
        messages = f.service.messages(*f.scope, result["session_id"])["messages"]
        self.assertTrue(all(any(item["id"] == identity and item["content"] == content for item in messages) for identity, content in original))

    def test_retry_summary_tracks_connection_identity_and_superseding_turn(self):
        f = self.f
        f.ai.outputs = [UpstreamError()]
        result = f.execute()
        self.assertTrue(result["recoverable"])
        f.ai.revision += 1
        self.assertFalse(f.service.get_run(*f.scope, result["id"])["recoverable"])
        self.assert_code("connection_changed", lambda: f.service.retry(*f.scope, result["id"], "wrong-connection", f.auth))
        f.ai.revision -= 1
        with f.db.con:
            f.db.con.execute("UPDATE shop_accounts SET generation=1 WHERE id=11")
        self.assertFalse(f.service.get_run(*f.scope, result["id"])["recoverable"])
        self.assert_code("scope_invalid", lambda: f.service.retry(*f.scope, result["id"], "wrong-generation", f.auth))
        with f.db.con:
            f.db.con.execute("UPDATE shop_accounts SET generation=0 WHERE id=11")
        f.execute(session_id=result["session_id"])
        self.assertFalse(f.service.get_run(*f.scope, result["id"])["recoverable"])
        self.assert_code("retry_not_allowed", lambda: f.service.retry(*f.scope, result["id"], "superseded", f.auth))

    def test_retry_enqueue_failure_restores_previous_attempt_and_failed_step(self):
        f = self.f
        f.service.tools.error_by_target = {"101": "storage_unavailable"}
        f.ai.outputs = [assistant("", [call("first", "knowledge_save", target="101")]), assistant("暂时失败。")]
        result = f.execute("更新全部商品客服知识")
        before = dict(f.row(result["id"]))
        with f.db.con:
            f.db.con.execute("CREATE TRIGGER fail_retry_queue BEFORE INSERT ON jobs WHEN NEW.kind='ops_run' BEGIN SELECT RAISE(ABORT,'queue unavailable'); END")
        with self.assertRaises(Exception):
            f.service.retry(*f.scope, result["id"], "queue-failed-retry", f.auth)
        self.assertEqual(dict(f.row(result["id"])), before)
        self.assertEqual(f.db.con.execute("SELECT status FROM ops_dispatches").fetchone()[0], "failed")
        self.assertEqual(f.db.con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(f.db.con.execute("SELECT COUNT(*) FROM ops_requests WHERE request_id='queue-failed-retry'").fetchone()[0], 0)

    def test_cancel_at_turn_completion_retains_final_reply_for_free_resume(self):
        f = self.f
        f.ai.outputs = [assistant("已收到并保存的最终答复。")]
        run = f.chat()
        complete = f.service._complete_turn
        def cancel_before_complete(row, claim, reply):
            f.service.cancel(*f.scope, row["id"])
            return complete(row, claim, reply)
        with patch.object(f.service, "_complete_turn", side_effect=cancel_before_complete):
            stopped = f.service.process_run(run["run_id"])
        self.assertEqual((stopped["status"], stopped["recoverable"]), ("cancelled", True))
        self.assertEqual(f.row(run["run_id"])["phase"], "dispatch")
        f.service.retry(*f.scope, run["run_id"], "resume-final", f.auth)
        f.ai.responder = lambda *_args: self.fail("取消边界已收到的最终答复不能重复请求")
        self.assertEqual(f.service.process_run(run["run_id"])["status"], "succeeded")
        self.assertEqual(len(f.ai.calls), 1)

    def test_unreviewable_error_and_bad_retry_requests_remain_closed(self):
        f = self.f
        f.service.tools.error_by_target = {"101": "needs_review"}
        f.ai.outputs = [assistant("", [call("review", "knowledge_save", target="101")])]
        result = f.execute("更新客服知识")
        self.assertEqual(result["status"], "needs_review")
        self.assert_code("retry_not_allowed", lambda: f.service.retry(*f.scope, result["id"], "review-retry", f.auth))
        self.assert_code("invalid_payload", lambda: f.service.messages(*f.scope, result["session_id"], cursor=-1))
        self.assert_code("invalid_payload", lambda: f.service.get_run(*f.scope, result["id"], after_seq=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
