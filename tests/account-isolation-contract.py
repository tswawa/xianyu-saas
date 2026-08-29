#!/usr/bin/env python3
"""Offline contract for two-account storage and API isolation.

All platform values in this file are synthetic fixtures.  No real Cookie,
order, inventory or buyer data is used.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-account-isolation-"))
os.environ.update(
    {
        "SAAS_DB": str(RUN_DIR / "saas.db"),
        "SAAS_TENANTS_DIR": str(RUN_DIR / "tenants"),
        "SAAS_COOKIE_SECURE": "0",
        "SAAS_RESTORE_WORKERS": "0",
        "SAAS_TESTING": "1",
        "SAAS_ALLOW_REGISTRATION": "1",
        "SAAS_PLATFORM_AI_BASE_URL": "",
        "SAAS_PLATFORM_AI_MODEL": "",
        "SAAS_PLATFORM_AI_KEY": "",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import bot_manager  # noqa: E402
import records  # noqa: E402
import shop_sync  # noqa: E402
from account_storage import AccountStorage  # noqa: E402
from connector_handoff import HandoffStore  # noqa: E402


def fake_shop_sync(cookie_header: str) -> dict:
    """Return a deterministic snapshot keyed by the synthetic seller id."""
    _, cookies = shop_sync.parse_cookie_header(cookie_header)
    seller = cookies["unb"]
    return {
        "version": 1,
        "account_ref": shop_sync.account_ref(cookies),
        "nickname": f"店铺-{seller}",
        "products": [
            {
                "id": "100001",
                "title": f"商品-{seller}",
                "description": "隔离合同商品",
                "price": "1.00",
                "status": "在售",
                "source": "cookie",
                "updated_at": "2026-08-16T00:00:00+0800",
            }
        ],
        "product_count": 1,
        "synced_at": "2026-08-16T00:00:00+0800",
        "truncated": False,
    }


def login(client: TestClient) -> int:
    assert client.post(
        "/api/auth/register", json={"username": "isolation-owner", "password": "password-123"}
    ).status_code == 200
    response = client.post(
        "/api/auth/login", json={"username": "isolation-owner", "password": "password-123"}
    )
    assert response.status_code == 200
    client.cookies.set("xianyu_saas_session", response.cookies.get("xianyu_saas_session"), path="/")
    user_id = int(app.db.get_user("isolation-owner")["id"])
    with app.db._lock:
        app.db.con.execute("UPDATE users SET expires_at = 4102444800 WHERE id = ?", (user_id,))
        app.db.con.commit()
    return user_id


def seed_records(storage: AccountStorage, user_id: int, account_key: str, chat_id: str, order_key: str) -> None:
    root = storage.ensure_account_dir(user_id, account_key)
    with sqlite3.connect(root / "chat_history.db") as con:
        con.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp DATETIME,
                chat_id TEXT,
                source_id TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO messages(user_id,item_id,role,content,timestamp,chat_id,source_id) VALUES (?,?,?,?,?,?,?)",
            ("buyer", "100001", "user", f"{account_key}-message", "2026-08-16 10:00:00", chat_id, f"source-{account_key}"),
        )
    with sqlite3.connect(root / "delivery_state.db") as con:
        con.execute(
            """
            CREATE TABLE delivery_events (
                order_key TEXT, status TEXT, item_id TEXT, quantity INTEGER,
                platform_status TEXT, paid_amount REAL, delivered_at REAL, created_at REAL
            )
            """
        )
        con.execute(
            "INSERT INTO delivery_events VALUES (?,?,?,?,?,?,?,?)",
            (order_key, "delivered", "100001", 1, "shipped", 1.0, 1000.0, 1000.0),
        )


def main() -> None:
    app.sync_shop = fake_shop_sync
    client = TestClient(app.app)
    user_id = login(client)

    default = app.db.ensure_default_shop_account(user_id)
    second_response = client.post("/api/bot/accounts", json={"key": "second", "name": "第二店铺"})
    assert second_response.status_code == 200, second_response.text
    second = app.db.get_shop_account(user_id, account_key="second")
    assert second is not None and int(second["id"]) != int(default["id"])
    generated_response = client.post("/api/bot/accounts", json={"name": "备用店铺"})
    assert generated_response.status_code == 200, generated_response.text
    generated = generated_response.json()["account"]
    assert generated["key"].startswith("shop-")
    assert generated["name"] == "备用店铺"

    # A failed private-directory initialization must not leave a visible,
    # usable-looking account row behind.
    original_ensure_dir = bot_manager.ensure_dir
    bot_manager.ensure_dir = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("synthetic directory failure")
    )
    try:
        failed_create = client.post("/api/bot/accounts", json={"name": "失败店铺"})
    finally:
        bot_manager.ensure_dir = original_ensure_dir
    assert failed_create.status_code == 400
    assert all(item["name"] != "失败店铺" for item in client.get("/api/bot/accounts").json()["accounts"])

    # Deleting from a non-owner API process must terminate the durable worker
    # PID before the account is disabled; a local empty supervisor is not proof
    # that the account is safe to remove.
    delete_remote_response = client.post(
        "/api/bot/accounts", json={"key": "delete-remote", "name": "待删除店铺"}
    )
    assert delete_remote_response.status_code == 200, delete_remote_response.text
    delete_remote = app.db.get_shop_account(user_id, account_key="delete-remote")
    app.db.persist_worker_runtime(
        user_id,
        delete_remote["id"],
        desired_state="running",
        mode="rules",
        state="running",
        pid=44001,
        generation=1,
        expected_generation=0,
    )
    with (
        patch.object(app, "bot_process_id", return_value=None),
        patch.object(app, "bot_stop", return_value=(False, "not_running")),
        patch.object(app, "bot_terminate_pid", return_value=(True, "stopped")) as delete_remote_stop,
    ):
        deleted_remote = client.delete("/api/bot/accounts/delete-remote")
    assert deleted_remote.status_code == 200, deleted_remote.text
    delete_remote_stop.assert_called_once_with(user_id, 44001, "delete-remote")
    deleted_remote_runtime = app.db.get_worker_runtime(user_id, delete_remote["id"])
    assert deleted_remote_runtime["desired_state"] == "stopped"
    assert deleted_remote_runtime["pid"] is None
    assert app.db.get_shop_account(user_id, account_key="delete-remote")["enabled"] == 0

    # A stale sync callback must not overwrite a name entered concurrently by
    # the owner after the sync captured its account row.
    rename_race = client.post("/api/bot/accounts", json={"key": "rename-race", "name": ""})
    assert rename_race.status_code == 200
    rename_key = "rename-race"
    stale_account = app.db.get_shop_account(user_id, account_key=rename_key)
    assert client.patch(
        f"/api/bot/accounts/{rename_key}", json={"name": "我设置的名称"}
    ).status_code == 200
    app._sync_account_state(user_id, "verified", fake_shop_sync("unb=333333; _m_h5_tk=rename-race"), stale_account)
    assert app.db.get_shop_account(user_id, account_key=rename_key)["display_name"] == "我设置的名称"
    second_headers = {"X-Shop-Account": "second"}

    default_cookie = "unb=111111; _m_h5_tk=token-default_value; sid=default"
    second_cookie = "unb=222222; _m_h5_tk=token-second_value; sid=second"
    assert client.put("/api/bot/cookies", json={"cookies": default_cookie}).status_code == 200
    assert client.put("/api/bot/cookies", headers=second_headers, json={"cookies": second_cookie}).status_code == 200

    default_status = client.get("/api/bot/status").json()
    second_status = client.get("/api/bot/status", headers=second_headers).json()
    assert default_status["shop_name"] == "店铺-111111"
    assert second_status["shop_name"] == "店铺-222222"
    assert default_status["shop_name"] != second_status["shop_name"]

    storage = AccountStorage(os.environ["SAAS_TENANTS_DIR"])
    default_root = storage.account_dir(user_id, "default")
    second_root = storage.account_dir(user_id, "second")
    assert default_root != second_root
    assert default_root.joinpath("cookies.txt").read_text() == default_cookie
    assert second_root.joinpath("cookies.txt").read_text() == second_cookie
    assert json.loads(default_root.joinpath("shop_snapshot.json").read_text())["products"][0]["title"] == "商品-111111"
    assert json.loads(second_root.joinpath("shop_snapshot.json").read_text())["products"][0]["title"] == "商品-222222"

    # Automation rules and delivery mappings are account-local.  The second
    # account must not inherit the first account's keyword or material.
    default_rule = {"rules": [{"keywords": ["default-only"], "reply": "default reply"}]}
    second_rule = {"rules": [{"keywords": ["second-only"], "reply": "second reply"}]}
    assert client.put("/api/automation", json=default_rule).status_code == 200
    assert client.put("/api/automation", headers=second_headers, json=second_rule).status_code == 200
    assert client.get("/api/automation").json()["rules"][0]["keywords"] == ["default-only"]
    assert client.get("/api/automation", headers=second_headers).json()["rules"][0]["keywords"] == ["second-only"]

    # Quick replies are durable per shop and must never leak across account
    # switches, even when their titles are identical.
    default_quick = [{"id": "default-quick", "title": "常用", "content": "default quick reply"}]
    second_quick = [{"id": "second-quick", "title": "常用", "content": "second quick reply"}]
    assert client.put("/api/bot/quick-replies", json={"quick_replies": default_quick}).status_code == 200
    assert client.put(
        "/api/bot/quick-replies", headers=second_headers, json={"quick_replies": second_quick}
    ).status_code == 200
    assert client.get("/api/bot/quick-replies").json()["quick_replies"] == default_quick
    assert client.get(
        "/api/bot/quick-replies", headers=second_headers
    ).json()["quick_replies"] == second_quick

    default_config = {"keywords_json": json.dumps(default_rule, ensure_ascii=False)}
    assert client.put("/api/config", json=default_config).status_code == 200
    second_config = client.get("/api/config", headers=second_headers).json()
    assert "default-only" not in json.dumps(second_config, ensure_ascii=False)

    # Same business key is valid once per account, but not twice in one
    # account.  This is the durable idempotency boundary for background work.
    first_job = app.db.enqueue_job(user_id, "shop_sync", "same-key", account_id=default["id"])
    duplicate_job = app.db.enqueue_job(user_id, "shop_sync", "same-key", account_id=default["id"])
    second_job = app.db.enqueue_job(user_id, "shop_sync", "same-key", account_id=second["id"])
    assert first_job["id"] == duplicate_job["id"]
    assert first_job["id"] != second_job["id"]
    assert app.db.acquire_control_lease(f"shop-sync:{user_id}:{default['id']}", "one", cooldown_seconds=0) == "acquired"
    assert app.db.acquire_control_lease(f"shop-sync:{user_id}:{second['id']}", "two", cooldown_seconds=0) == "acquired"

    # Attention is filtered by the selected account at the API boundary.
    app.db.set_worker_desired(user_id, True, account_id=default["id"])
    app.db.update_worker_runtime(user_id, default["id"], state="degraded", last_error="default")
    app.db.set_worker_desired(user_id, True, account_id=second["id"])
    app.db.update_worker_runtime(user_id, second["id"], state="degraded", last_error="second")
    default_attention = client.get("/api/bot/attention").json()["items"]
    second_attention = client.get("/api/bot/attention", headers=second_headers).json()["items"]
    assert {item["account_id"] for item in default_attention} == {int(default["id"])}
    assert {item["account_id"] for item in second_attention} == {int(second["id"])}
    default_worker_attention = next(item for item in default_attention if item["kind"] == "worker")
    second_worker_attention = next(item for item in second_attention if item["kind"] == "worker")
    assert default_worker_attention["id"] != second_worker_attention["id"]
    resolved_default = client.put(
        f"/api/bot/attention/{default_worker_attention['id']}", json={"resolved": True}
    )
    assert resolved_default.status_code == 200
    assert next(
        item for item in resolved_default.json()["items"]
        if item["id"] == default_worker_attention["id"]
    )["resolved"] is True
    assert next(
        item for item in client.get("/api/bot/attention", headers=second_headers).json()["items"]
        if item["id"] == second_worker_attention["id"]
    )["resolved"] is False
    assert client.put(
        f"/api/bot/attention/{default_worker_attention['id']}",
        headers=second_headers,
        json={"resolved": True},
    ).status_code == 404
    # A real state change keeps the logical warning id but invalidates the old
    # acknowledgement so the updated issue becomes pending again.
    app.db.set_worker_desired(user_id, False, account_id=default["id"])
    reopened_default = client.get("/api/bot/attention").json()["items"]
    reopened_worker = next(item for item in reopened_default if item["id"] == default_worker_attention["id"])
    assert reopened_worker["resolved"] is False
    assert reopened_worker["resolved_at"] is None

    seed_records(storage, user_id, "default", "chat-default", "same-order")
    seed_records(storage, user_id, "second", "chat-second", "same-order")
    assert client.get("/api/bot/conversations").json()["conversations"][0]["chat_id"] == "chat-default"
    assert client.get("/api/bot/conversations", headers=second_headers).json()["conversations"][0]["chat_id"] == "chat-second"
    assert client.get("/api/bot/conversations?search=second-message").json()["conversations"] == []
    assert client.get(
        "/api/bot/conversations?search=second-message", headers=second_headers
    ).json()["conversations"][0]["chat_id"] == "chat-second"
    assert client.get("/api/bot/messages?chat_id=chat-second&search=second-message").json()["match_count"] == 0
    second_message_search = client.get(
        "/api/bot/messages?chat_id=chat-second&search=second-message", headers=second_headers
    ).json()
    assert second_message_search["match_count"] == 1
    assert len(second_message_search["messages"]) == 1
    assert client.get("/api/bot/orders").json()["orders"][0]["order_key"] == "same-order"
    assert client.get("/api/bot/orders", headers=second_headers).json()["orders"][0]["order_key"] == "same-order"

    default_env = bot_manager._env_for(user_id, automation_mode="rules", account_key="default")
    second_env = bot_manager._env_for(user_id, automation_mode="rules", account_key="second")
    assert default_env["XIAN_YU_DATA_DIR"] != second_env["XIAN_YU_DATA_DIR"]
    assert default_env["XIAN_YU_ACCOUNT_KEY"] == "default"
    assert second_env["XIAN_YU_ACCOUNT_KEY"] == "second"
    assert default_env["PRODUCTS_CONFIG_FILE"] != second_env["PRODUCTS_CONFIG_FILE"]
    assert 0 <= int(default_env["TOKEN_STARTUP_JITTER_SECONDS"]) <= 30
    assert 0 <= int(default_env["TOKEN_REFRESH_JITTER_SECONDS"]) <= 300
    assert default_env["TOKEN_STARTUP_JITTER_SECONDS"] == bot_manager._env_for(
        user_id, automation_mode="rules", account_key="default"
    )["TOKEN_STARTUP_JITTER_SECONDS"]
    assert second_env["TOKEN_REFRESH_JITTER_SECONDS"] == bot_manager._env_for(
        user_id, automation_mode="rules", account_key="second"
    )["TOKEN_REFRESH_JITTER_SECONDS"]

    bot_manager.write_secret(
        user_id,
        "auth_status.json",
        json.dumps(
            {
                "code": "session_expired",
                "reauthorization_required": True,
                "updated_at": 1234,
            }
        ),
        "second",
    )
    expired_status = bot_manager.status(user_id, "second")
    assert expired_status["auth_code"] == "session_expired"
    assert expired_status["reauthorization_required"] is True
    assert expired_status["sync_status"] == "cookie_expired"
    assert expired_status["connected"] is False
    bot_manager.clear_auth_status(user_id, "second")
    assert bot_manager.status(user_id, "second")["auth_code"] == "ok"

    # Connector bridge tokens are scoped to the account and revoked together
    # on logout; a token from one shop cannot be claimed for the other.
    handoffs = HandoffStore(clock=lambda: 1000.0)
    default_handoff, _ = handoffs.issue(user_id, "default")
    second_handoff, _ = handoffs.issue(user_id, "second")
    assert handoffs.begin_with_scope(default_handoff) == ("ok", user_id, "default")
    handoffs.finish(default_handoff, success=False)
    handoffs.clear_user(user_id)
    assert handoffs.begin_with_scope(default_handoff)[0] == "handoff_invalid"
    assert handoffs.begin_with_scope(second_handoff)[0] == "handoff_invalid"

    print("account-isolation contract: cookies, snapshots, automation, jobs, attention, records and worker paths passed")


if __name__ == "__main__":
    main()
