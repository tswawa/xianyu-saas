#!/usr/bin/env python3
"""Temporary-database HTTP contract: resources, administrator policy and cards."""
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="xianyu-resources-api-")
os.environ.update({"SAAS_DB": str(Path(TEMP.name) / "control.db"),
    "SAAS_TENANTS_DIR": str(Path(TEMP.name) / "tenants"), "SAAS_COOKIE_SECURE": "0",
    "SAAS_RESTORE_WORKERS": "0", "SAAS_TESTING": "1", "SAAS_MAX_BOTS": "3", "SAAS_BOT_MEM_MB": "400",
    "SAAS_ADMIN_TOKEN": "resource-emergency-fixture"})
sys.path.insert(0, str(ROOT / "backend"))
from fastapi.testclient import TestClient
import app
import shop_sync


def no_network(*_args, **_kwargs):
    raise AssertionError("external network is forbidden")


def login(name, role):
    uid = app.db.create_user(name, "Resource-test-password!", role=role, initializer=app._new_user_initializer({}))
    client = TestClient(app.app)
    logged = client.post("/api/auth/login", json={"username": name, "password": "Resource-test-password!"})
    assert logged.status_code == 200, logged.text
    client.cookies.set(app.SESSION_COOKIE, logged.cookies.get(app.SESSION_COOKIE), path="/")
    return client, uid


def main():
    admin, admin_id = login("resources-admin", "admin")
    owner, owner_id = login("resources-owner", "owner")
    outsider = TestClient(app.app)
    assert outsider.get("/api/bot/resources").status_code == 401
    assert owner.get("/api/admin/resource-settings").status_code == 403
    policy = admin.get("/api/admin/resource-settings")
    assert policy.status_code == 200, policy.text
    assert policy.json()["settings"]["max_running_workers"] == 3
    assert policy.json()["settings"]["worker_memory_mib"] == 400
    values = {"expected_revision": 0, "max_shop_accounts": 2, "max_running_workers": 4, "worker_memory_mib": 768}
    before = app.db.con.total_changes
    assert owner.put("/api/admin/resource-settings", json=values).status_code == 403
    assert app.db.con.total_changes == before
    for bad in ({**values, "max_shop_accounts": True}, {**values, "worker_memory_mib": "768"},
                {**values, "max_running_workers": 0}, {**values, "pid": 123}, {**values, "expected_revision": None}):
        assert admin.put("/api/admin/resource-settings", json=bad).status_code == 422
    assert admin.get("/api/admin/resource-settings").json()["settings"]["revision"] == 0
    with patch.dict(os.environ, {"SAAS_TESTING": "0"}):
        rejected = admin.put("/api/admin/resource-settings", json=values)
        assert rejected.status_code == 403, rejected.text
    with patch.object(app.worker_manager, "start") as start, patch.object(app.worker_manager, "stop") as stop:
        saved = admin.put("/api/admin/resource-settings", json=values)
        assert saved.status_code == 200, saved.text
        assert saved.json()["restart_performed"] is False
        start.assert_not_called()
        stop.assert_not_called()
    assert saved.json()["settings"]["revision"] == 1
    assert admin.put("/api/admin/resource-settings", json=values).status_code == 409
    audit = app.db.con.execute("SELECT metadata_json FROM audit_log WHERE event_type='platform.resource_settings_changed'").fetchone()
    assert json.loads(audit[0])["worker_memory_mib"] == 768

    added = owner.post("/api/bot/accounts", json={"key": "second", "name": "资源测试二店"})
    assert added.status_code == 200, added.text
    second_id = added.json()["account"]["id"]
    denied = owner.post("/api/bot/accounts", json={"key": "third"})
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "shop_limit_reached"
    baseline = app.db.con.total_changes
    page = owner.get("/api/bot/resources?limit=1")
    assert page.status_code == 200, page.text
    result = page.json()
    assert result["scope"] == "own_shops" and result["total"] == 2
    assert result["next_cursor"] is not None
    assert result["limits"]["worker_memory_mib"] == 768
    assert result["memory_limit_kind"] == "address_space" and result["cpu_basis"] == "one_core"
    next_page = owner.get(f"/api/bot/resources?limit=1&cursor={result['next_cursor']}").json()
    assert [row["account_id"] for row in next_page["accounts"]] == [second_id]
    owner_accounts = {row["id"] for row in app.db.list_shop_accounts(owner_id, include_disabled=True)}
    # Exclude the legacy accounts helper's deliberate ensure_default commit
    # when checking the new resource endpoint's passive behavior.
    before = app.db.con.total_changes
    own_rows = owner.get("/api/bot/resources", headers={"X-Shop-Account": "someone-else"}).json()["accounts"]
    assert {row["account_id"] for row in own_rows} == owner_accounts
    assert app.db.con.total_changes == before
    assert all(row["rss_bytes"] == 0 and row["pending_restart"] is False for row in own_rows)
    assert "pid" not in json.dumps(own_rows) and "identity_token" not in json.dumps(own_rows)
    for query in ("pid=1", "user_id=1", "path=x", "cursor=0&cursor=1"):
        assert owner.get("/api/bot/resources?" + query).status_code == 400
    for query in ("cursor=-1", "limit=101", "limit=0"):
        assert owner.get("/api/bot/resources?" + query).status_code == 422
    lower = admin.put("/api/admin/resource-settings", json={**values, "expected_revision": 1, "max_shop_accounts": 1})
    assert lower.status_code == 200, lower.text
    assert owner.get("/api/bot/resources").json()["total"] == 2

    # The new card summary distinguishes all types without exporting source data.
    cookie = "unb=123456; _m_h5_tk=resource-fixture_token"
    app.write_secret(owner_id, "cookies.txt", cookie, "default")
    _, cookies = shop_sync.parse_cookie_header(cookie)
    snapshot = {"version": 1, "account_ref": shop_sync.account_ref(cookies), "nickname": "资源测试店",
        "products": [{"id": str(i), "title": "测试商品", "description": "", "price": "1", "status": "在售", "source": "cookie"} for i in (101, 102, 103)],
        "product_count": 3, "synced_at": "2026-09-01T00:00:00+0800", "truncated": False}
    shop_sync.save_snapshot(owner_id, snapshot, "default")
    document = {"version": 1, "types": [
        {"id": "material-one", "item_ids": ["101"], "delivery": "material", "payload": "PRIVATE_MATERIAL", "enabled": True},
        {"id": "pan-one", "item_ids": ["102"], "delivery": "pan", "resource_match": ["PRIVATE_TAG"], "enabled": False},
        {"id": "redeem-one", "item_ids": ["103"], "delivery": "redeem", "enabled": True},
    ]}
    app.write_secret(owner_id, "products_config.json", json.dumps(document), "default")
    delivery = owner.get("/api/bot/products/delivery-status")
    assert delivery.status_code == 200, delivery.text
    items = {row["item_id"]: row for row in delivery.json()["items"]}
    assert [items[str(i)]["delivery"] for i in (101, 102, 103)] == ["material", "pan", "redeem"]
    assert items["102"]["configured"] and not items["102"]["enabled"]
    assert "PRIVATE_MATERIAL" not in delivery.text and "PRIVATE_TAG" not in delivery.text
    assert admin.get("/api/bot/products/delivery-status").json()["items"] == []
    document["types"].append({"delivery": "redeem", "item_ids": ["101"]})
    app.write_secret(owner_id, "products_config.json", json.dumps(document), "default")
    conflicting = owner.get("/api/bot/products/delivery-status").json()["items"]
    assert next(row for row in conflicting if row["item_id"] == "101")["delivery"] == "conflict"
    assert outsider.put("/api/admin/resource-settings", json={**values, "expected_revision": 2, "emergency_admin": True}).status_code == 403
    emergency = outsider.put("/api/admin/resource-settings", headers={"X-Admin-Token": "resource-emergency-fixture"},
                            json={**values, "expected_revision": 2})
    assert emergency.status_code == 200, emergency.text
    assert emergency.json()["settings"]["revision"] == 3
    print("resources API: admin CAS/CSRF/emergency, safe limits, tenant pagination, passive monitoring and private delivery summaries passed")


if __name__ == "__main__":
    with patch.object(socket, "create_connection", side_effect=no_network):
        main()
