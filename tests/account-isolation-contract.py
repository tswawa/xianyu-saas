#!/usr/bin/env python3
"""Offline contract for two-account storage and API isolation.

All platform values in this file are synthetic fixtures.  No real Cookie,
order, inventory or buyer data is used.
"""

from __future__ import annotations

import hashlib
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
        # Keep synthetic login fixtures from spawning an installed live Worker
        # inside a test container before this suite seeds its own record schema.
        "SAAS_BOT_ROOT": str(RUN_DIR / "worker-not-installed"),
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
    app.db.create_user(
        "isolation-owner",
        "password-123",
        role="owner",
        initializer=app._new_user_initializer({}),
    )
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


def assert_order_queries(client, user_id, storage):
    key = "orders-page"
    created = client.post("/api/bot/accounts", json={"key": key, "name": "订单分页测试"})
    assert created.status_code == 200, created.text
    headers = {"X-Shop-Account": key}
    root = storage.account_dir(user_id, key)
    path = root / "delivery_state.db"
    empty = client.get("/api/bot/orders?page=1&page_size=15", headers=headers).json()
    assert empty["total"] == 0 and empty["total_pages"] == 0 and not path.exists()
    statuses = ["awaiting_binding", "verified", "reserved", "sending", "delivered", "manual_review",
                "retry", "cancelled", "expired", "failed", "future_state", "manual_review"]
    base = 1788602400
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE delivery_events (
                order_key TEXT PRIMARY KEY, status TEXT, created_at REAL, updated_at REAL,
                platform_order_id TEXT, item_id TEXT, buyer_id TEXT, chat_id TEXT,
                quantity INTEGER, paid_amount TEXT, platform_status TEXT, verified_at REAL,
                delivered_at REAL, platform_shipped_at REAL, delivery_type TEXT,
                last_error TEXT, delivery_payload TEXT, inventory_ids TEXT
            );
            CREATE TABLE manual_reviews (order_key TEXT PRIMARY KEY, status TEXT, resolution TEXT);
        """)
        for index in range(240):
            record_key = f"same-first-14--{index:05}"
            con.execute("INSERT INTO delivery_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                record_key, statuses[index % len(statuses)], base + index // 3, base + index // 3,
                str(429175119103700000 + index), "100001", "buyer-%_\\literal" if index == 2 else "buyer-001",
                "order-chat", 1, "0.00" if index == 0 else None if index == 1 else "0.18", "2", base,
                base + 30 if index % 12 == 4 else None, None, "redeem",
                "inventory_empty" if index % 12 == 5 else "private-error-SECRET" if index % 12 == 10 else None,
                "private-delivery-SECRET", "private-inventory-SECRET",
            ))
            if index % 12 == 11:
                con.execute("INSERT INTO manual_reviews VALUES (?, 'resolved', 'fulfilled_manually')", (record_key,))
    with sqlite3.connect(root / "chat_history.db") as con:
        con.execute("CREATE TABLE messages (chat_id TEXT)")
        con.execute("INSERT INTO messages VALUES ('order-chat')")
    cookie = "unb=810001; _m_h5_tk=fixture-signature_demo"
    storage.write_text(user_id, key, "cookies.txt", cookie)
    snapshot = fake_shop_sync(cookie)
    snapshot["products"][0].update(price="9999.00", image_url="https://user:secret@bad.example/item.png")
    shop_sync.save_snapshot(user_id, snapshot, key)
    path_before = path.read_bytes()
    page = client.get("/api/bot/orders?page=1&page_size=15", headers=headers)
    assert page.status_code == 200, page.text
    data = page.json()
    assert data["account_key"] == key and data["total"] == 240 and data["total_pages"] == 16
    assert data["status_counts"] == {"all": 240, "processing": 80, "sent": 20, "manual": 20, "retry": 20, "ended": 60, "exception": 40}
    assert data["orders"][0]["order_key"] == "same-first-14--00239"
    assert data["orders"][0]["status_label"] == "已人工处理"
    assert data["orders"][0]["conversation_available"] is True
    assert len(client.get("/api/bot/orders?limit=200", headers=headers).json()["orders"]) == 200
    keys = []
    for number in range(1, 17):
        part = client.get(f"/api/bot/orders?page={number}&page_size=15", headers=headers).json()
        keys.extend(row["order_key"] for row in part["orders"])
    assert len(keys) == len(set(keys)) == 240 and keys == sorted(keys, reverse=True)
    for size in (15, 30, 50):
        last = client.get(f"/api/bot/orders?page=1000000&page_size={size}", headers=headers).json()
        assert last["page"] == (240 + size - 1) // size and last["orders"][-1]["order_key"] == "same-first-14--00000"
    manual = client.get("/api/bot/orders?page=1&status=manual", headers=headers).json()
    assert manual["total"] == 20 and manual["status_counts"]["all"] == 240
    assert all(row["review_status"] == "open" and row["reason_label"] for row in manual["orders"])
    for status, count in data["status_counts"].items():
        filtered = client.get("/api/bot/orders", params={"page": 1, "status": status}, headers=headers).json()
        assert filtered["total"] == count
    for keyword, field, expected in (("same-first-14--00000", "order_id", 1), ("429175119103700002", "order_id", 1),
                                     ("%_\\literal", "buyer_id", 1), ("' OR 1=1 --", "all", 0), ("100001", "item_id", 240)):
        search = client.get("/api/bot/orders", params={"page": 1, "q": keyword, "search_field": field}, headers=headers).json()
        assert search["total"] == expected, search
        assert search["status_counts"]["all"] == expected
    first = client.get("/api/bot/orders", params={"page": 1, "q": "same-first-14--00000"}, headers=headers).json()["orders"][0]
    assert first["paid_amount"] == "0.00" and first["platform_order_id"] == "429175119103700000"
    assert first["item_title"] == "商品-810001" and first["item_image_url"] == ""
    assert first["paid_amount"] != snapshot["products"][0]["price"], "never substitute a product price for the paid amount"
    assert len(first["order_key"]) > 14 and first["created_at"].endswith("+00:00")
    second = client.get("/api/bot/orders", params={"page": 1, "q": "same-first-14--00001"}, headers=headers).json()["orders"][0]
    assert second["paid_amount"] is None
    from datetime import datetime, timezone
    bounds = {"page": 1, "created_from": datetime.fromtimestamp(base, timezone.utc).isoformat(),
              "created_to": datetime.fromtimestamp(base + 1, timezone.utc).isoformat()}
    assert client.get("/api/bot/orders", params=bounds, headers=headers).json()["total"] == 3
    for params in ({"page": "bad"}, {"page": 0}, {"page": 1000001}, {"page_size": 200}, {"page_size": 0},
                   {"status": "refund"}, {"search_field": "delivery_payload"}, {"q": "x" * 129}, {"q": "\x00"},
                   {"created_from": "2026-09-05"}, {**bounds, "created_to": bounds["created_from"]}):
        bad = client.get("/api/bot/orders", params=params, headers=headers)
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "invalid_order_query", bad.text
    serialized = json.dumps(data, ensure_ascii=False)
    assert all(secret not in serialized for secret in ("SECRET", "inventory_ids", "delivery_payload", "last_error"))
    legacy = client.get("/api/bot/orders?page=1", headers={"X-Shop-Account": "second"}).json()
    assert legacy["total"] == 1 and legacy["orders"][0]["buyer_id"] == "" and legacy["orders"][0]["status_label"] == "已发送"
    assert client.get("/api/bot/orders", params={"page": 1, "q": "same-first-14"}).json()["total"] == 0
    assert client.get("/api/bot/orders?page=1", headers={"X-Shop-Account": "../orders-page"}).status_code == 404
    assert path.read_bytes() == path_before, "GET order queries must never mutate the delivery database"
    other_id = app.db.create_user("other-orders-owner", "Other-Orders-Pass-123!", role="owner", initializer=app._new_user_initializer({}))
    other = TestClient(app.app)
    login_response = other.post("/api/auth/login", json={"username": "other-orders-owner", "password": "Other-Orders-Pass-123!"})
    assert login_response.status_code == 200
    other.cookies.set("xianyu_saas_session", login_response.cookies.get("xianyu_saas_session"), path="/")
    assert other.get("/api/bot/orders?page=1", headers=headers).status_code == 404
    assert other.get("/api/bot/orders?page=1").json()["total"] == 0
    assert not (storage.account_dir(other_id) / "delivery_state.db").exists()
    assert other.post("/api/bot/accounts", json={"key": key, "name": "同键不同用户"}).status_code == 200
    seed_records(storage, other_id, key, "other-private-chat", "same-first-14--00000")
    other_orders = other.get("/api/bot/orders?page=1", headers=headers).json()
    assert other_orders["total"] == 1 and other_orders["orders"][0]["platform_order_id"] == ""
    assert client.get("/api/bot/orders?page=1", headers=headers).json()["total"] == 240
    assert TestClient(app.app).get("/api/bot/orders?page=1", headers=headers).status_code == 401
    with patch.object(records, "_orders_connection", side_effect=PermissionError("private path")):
        unavailable = client.get("/api/bot/orders?page=1", headers=headers)
        assert unavailable.status_code == 503 and "private path" not in unavailable.text
    path.write_bytes(b"corrupt sqlite data")
    try:
        damaged = client.get("/api/bot/orders?page=1", headers=headers)
        assert damaged.status_code == 503 and damaged.json()["detail"]["code"] == "orders_unavailable"
    finally:
        path.write_bytes(path_before)
    # Missing optional tables are fine; a missing required schema is not.
    with sqlite3.connect(root / "incomplete.db") as con:
        con.execute("CREATE TABLE delivery_events (order_key TEXT)")
    incomplete = (root / "incomplete.db").read_bytes()
    path.write_bytes(incomplete)
    try:
        assert client.get("/api/bot/orders?page=1", headers=headers).status_code == 503
    finally:
        path.write_bytes(path_before)
    if hasattr(os, "symlink") and os.name != "nt":
        path.rename(root / "original.db")
        path.symlink_to(root / "original.db")
        try:
            assert client.get("/api/bot/orders?page=1", headers=headers).status_code == 503
        finally:
            path.unlink()
            (root / "original.db").rename(path)
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(path_before).digest()


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

    # 停止无法确认时绝不能伪装成已断开：返回 409，保留降级运行态与身份记录。
    stuck_response = client.post(
        "/api/bot/accounts", json={"key": "stuck-stop", "name": "未停止店铺"}
    )
    assert stuck_response.status_code == 200, stuck_response.text
    stuck_account = app.db.get_shop_account(user_id, account_key="stuck-stop")
    app.db.persist_worker_runtime(
        user_id,
        stuck_account["id"],
        desired_state="running",
        mode="rules",
        state="running",
        pid=55001,
        generation=1,
        expected_generation=0,
    )
    with (
        patch.object(app, "bot_process_id", return_value=55001),
        patch.object(app, "bot_stop", return_value=(False, "stop_failed")) as stuck_stop,
        patch.object(app, "bot_terminate_pid") as same_pid_stop,
    ):
        stuck_delete = client.delete("/api/bot/accounts/stuck-stop")
    assert stuck_delete.status_code == 409, stuck_delete.text
    assert stuck_delete.json()["detail"]["code"] == "worker_stop_unconfirmed"
    assert stuck_stop.call_count == 2
    same_pid_stop.assert_not_called()
    stuck_runtime = app.db.get_worker_runtime(user_id, stuck_account["id"])
    assert stuck_runtime["desired_state"] == "stopped"
    assert stuck_runtime["state"] == "degraded"
    assert stuck_runtime["pid"] == 55001
    assert app.db.get_shop_account(user_id, account_key="stuck-stop")["enabled"] == 1

    # Both local and durable PIDs fail the first stop. Neither one succeeding
    # alone on retry is enough to disable the account or discard the other PID.
    for local_stopped, durable_stopped in ((False, True), (True, False), (True, True)):
        previous = app.db.get_worker_runtime(user_id, stuck_account["id"])
        generation = int(previous["generation"])
        app.db.persist_worker_runtime(
            user_id,
            stuck_account["id"],
            desired_state="running",
            mode="rules",
            state="running",
            pid=55002,
            generation=generation + 1,
            expected_generation=generation,
        )
        with (
            patch.object(app, "bot_process_id", return_value=55001),
            patch.object(app, "bot_stop", side_effect=[
                (False, "stop_failed"),
                (local_stopped, "stopped" if local_stopped else "stop_failed"),
            ]) as local_stop,
            patch.object(app, "bot_terminate_pid", side_effect=[
                (False, "still_alive"),
                (durable_stopped, "stopped" if durable_stopped else "still_alive"),
            ]) as durable_stop,
        ):
            two_pid_delete = client.delete("/api/bot/accounts/stuck-stop")
        assert local_stop.call_count == 2
        assert durable_stop.call_count == 2
        assert all(call.args == (user_id, 55002, "stuck-stop") for call in durable_stop.call_args_list)
        both_stopped = local_stopped and durable_stopped
        assert two_pid_delete.status_code == (200 if both_stopped else 409), two_pid_delete.text
        two_pid_runtime = app.db.get_worker_runtime(user_id, stuck_account["id"])
        assert two_pid_runtime["state"] == ("stopped" if both_stopped else "degraded")
        assert two_pid_runtime["pid"] == (None if both_stopped else 55001 if durable_stopped else 55002)
        assert app.db.get_shop_account(user_id, account_key="stuck-stop")["enabled"] == (0 if both_stopped else 1)

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

    # Losing the lease before stopping, before retrying, or before committing
    # the stop intent must not change auto_resume or clear the connection.
    default_account = app.db.get_shop_account(user_id, account_key="default")
    for fail_at in (1, 3, 5):
        app.db.set_worker_auto_resume(user_id, default_account["id"], 1)
        lease_checks = {"count": 0}

        def expire_stop_lease(*_args, **_kwargs):
            lease_checks["count"] += 1
            if lease_checks["count"] == fail_at:
                raise app.HTTPException(503, "synthetic expired lease")

        with (
            patch.object(app, "_ensure_account_lease", side_effect=expire_stop_lease),
            patch.object(app, "bot_process_id", return_value=None),
            patch.object(app, "bot_stop", return_value=(False, "stop_failed")) as lease_stop,
            patch.object(app.db, "set_worker_auto_resume", wraps=app.db.set_worker_auto_resume) as set_auto_resume,
        ):
            expired_delete = client.delete("/api/bot/accounts/default")
        assert expired_delete.status_code == 503, expired_delete.text
        assert lease_stop.call_count == (fail_at - 1) // 2
        set_auto_resume.assert_not_called()
        assert app.db.get_worker_runtime(user_id, default_account["id"])["auto_resume"] == 1
        assert default_root.joinpath("cookies.txt").read_text() == default_cookie
        assert app.db.get_shop_account(user_id, account_key="default")["status"] == default_account["status"]

    # 停止异常且无可查询 PID：不得据“无 PID”推断为已停止，也不得清 cookie/连接。
    with (
        patch.object(app, "bot_process_id", return_value=None),
        patch.object(app, "bot_stop", return_value=(False, "stop_failed")),
        patch.object(app, "bot_terminate_pid", return_value=(True, "stopped")) as default_stop,
    ):
        default_delete = client.delete("/api/bot/accounts/default")
    assert default_delete.status_code == 409, default_delete.text
    assert default_delete.json()["detail"]["code"] == "worker_stop_unconfirmed"
    default_stop.assert_not_called()
    assert default_root.joinpath("cookies.txt").read_text() == default_cookie
    assert client.get("/api/bot/status").json()["shop_name"] == "店铺-111111"

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

    assert_order_queries(client, user_id, storage)
    print("account-isolation contract: cookies, snapshots, automation, jobs, attention, orders pagination, records and worker paths passed")


if __name__ == "__main__":
    main()
