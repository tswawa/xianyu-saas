#!/usr/bin/env python3
"""Offline contract for account-scoped delivery templates and card pool APIs."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-saas-templates-cards-"))
os.environ.update(
    {
        "SAAS_DB": str(RUN_DIR / "saas.db"),
        "SAAS_TENANTS_DIR": str(RUN_DIR / "tenants"),
        "SAAS_COOKIE_SECURE": "0",
        "SAAS_RESTORE_WORKERS": "0",
        "SAAS_TESTING": "1",
        "SAAS_ALLOW_REGISTRATION": "1",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import shop_sync  # noqa: E402
from account_storage import AccountStorage  # noqa: E402


COOKIE = "unb=123456; _m_h5_tk=contract-templates_value"


def snapshot(cookie: str) -> dict:
    _, cookies = shop_sync.parse_cookie_header(cookie)
    return {
        "version": 1,
        "account_ref": shop_sync.account_ref(cookies),
        "nickname": "合同店铺",
        "products": [
            {"id": "100001", "title": "商品一", "description": "一", "price": 1, "status": "在售"},
            {"id": "100002", "title": "商品二", "description": "二", "price": 2, "status": "在售"},
            {"id": "100003", "title": "商品三", "description": "三", "price": 3, "status": "在售"},
        ],
        "product_count": 3,
        "synced_at": "2026-08-18T10:00:00+0800",
        "truncated": False,
    }


def read_products_config(user_id: int, account_key: str = "default") -> dict:
    raw = app.read_secret(user_id, "products_config.json", account_key)
    return json.loads(raw or '{"version": 1, "types": []}')


def read_codes(user_id: int, account_key: str = "default") -> list:
    raw = app.read_secret(user_id, "redeem_codes.json", account_key)
    return json.loads(raw or "[]")


def main() -> None:
    client = TestClient(app.app)
    app.db.create_user(
        "templates-owner",
        "password-123",
        role="owner",
        initializer=app._new_user_initializer({}),
    )
    login = client.post(
        "/api/auth/login", json={"username": "templates-owner", "password": "password-123"}
    )
    assert login.status_code == 200, login.text
    client.cookies.set("xianyu_saas_session", login.cookies.get("xianyu_saas_session"), path="/")
    user_id = int(app.db.get_user("templates-owner")["id"])
    with app.db._lock:
        app.db.con.execute("UPDATE users SET expires_at = 4102444800 WHERE id = ?", (user_id,))
        app.db.con.commit()

    # One self-use owner exercises every endpoint under the same permission path.
    storage = AccountStorage(str(RUN_DIR / "tenants"))
    default_root = storage.ensure_account_dir(user_id, "default")
    app.write_secret(user_id, "cookies.txt", COOKIE, "default")
    shop_sync.save_snapshot(user_id, snapshot(COOKIE), "default")
    app.write_secret(
        user_id,
        "products_config.json",
        json.dumps({"version": 1, "types": []}),
        "default",
    )

    # GET starts empty when the config document has no delivery templates.
    empty = client.get("/api/bot/templates")
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"templates": []}

    # Create a redeem template. The generated id is a template-<uuid8> style.
    redeem = client.put(
        "/api/bot/templates",
        json={
            "template": {
                "name": "兑换码模板",
                "description": "购买后自动发卡",
                "price": "5.00",
                "delivery": "redeem",
                "item_ids": ["100001", "100002"],
                "enabled": True,
            }
        },
    )
    assert redeem.status_code == 200, redeem.text
    redeem_payload = redeem.json()
    assert redeem_payload["ok"] is True
    redeem_template = redeem_payload["template"]
    assert redeem_template["name"] == "兑换码模板"
    assert redeem_template["delivery"] == "redeem"
    assert redeem_template["item_ids"] == ["100001", "100002"]
    assert redeem_template["item_count"] == 2
    assert redeem_template["description"] == "购买后自动发卡"
    assert redeem_template["price"] == "5.00"
    assert redeem_template["enabled"] is True
    assert redeem_template["id"].startswith("template-")
    assert "payload_set" not in redeem_template

    # Create a pan template with a non-empty resource_match.
    pan = client.put(
        "/api/bot/templates",
        json={
            "template": {
                "name": "网盘模板",
                "delivery": "pan",
                "item_ids": ["100003"],
                "resource_match": ["新款", "资料"],
                "enabled": True,
            }
        },
    )
    assert pan.status_code == 200, pan.text
    pan_payload = pan.json()
    assert pan_payload["ok"] is True
    pan_template = pan_payload["template"]
    assert pan_template["name"] == "网盘模板"
    assert pan_template["delivery"] == "pan"
    assert pan_template["item_ids"] == ["100003"]
    assert pan_template["item_count"] == 1
    assert pan_template["resource_match"] == ["新款", "资料"]

    # GET returns both templates without exposing payload text.
    templates = client.get("/api/bot/templates")
    assert templates.status_code == 200, templates.text
    assert templates.json()["templates"][0]["id"] == redeem_template["id"]
    assert templates.json()["templates"][1]["id"] == pan_template["id"]
    assert all("payload" not in item for item in templates.json()["templates"])
    assert "payload_set" not in templates.json()["templates"][0]

    # Updating an existing template by id preserves the id and replaces fields.
    updated = client.put(
        "/api/bot/templates",
        json={
            "template": {
                "id": redeem_template["id"],
                "name": "兑换码模板（改）",
                "delivery": "redeem",
                "item_ids": ["100001"],
                "enabled": False,
            }
        },
    )
    assert updated.status_code == 200, updated.text
    updated_template = updated.json()["template"]
    assert updated_template["id"] == redeem_template["id"]
    assert updated_template["name"] == "兑换码模板（改）"
    assert updated_template["item_ids"] == ["100001"]
    assert updated_template["item_count"] == 1
    assert updated_template["enabled"] is False
    assert "description" not in updated_template
    assert "price" not in updated_template

    # A truncated catalog may omit a still-valid legacy binding. Editing can
    # preserve that exact existing ID, but must not introduce a new unknown ID.
    truncated_legacy_id = "900001"
    document = read_products_config(user_id)
    stored_redeem = next(
        item for item in document["types"] if item.get("id") == redeem_template["id"]
    )
    stored_redeem["item_ids"] = ["100001", truncated_legacy_id]
    app.write_secret(
        user_id,
        "products_config.json",
        json.dumps(document, ensure_ascii=False),
        "default",
    )
    partial_snapshot = snapshot(COOKIE)
    partial_snapshot["truncated"] = True
    partial_snapshot["product_count"] = 4
    shop_sync.save_snapshot(user_id, partial_snapshot, "default")

    preserved_truncated = client.put(
        "/api/bot/templates",
        json={
            "template": {
                "id": redeem_template["id"],
                "name": "兑换码模板（截断目录）",
                "delivery": "redeem",
                "item_ids": ["100001", truncated_legacy_id],
                "enabled": False,
            }
        },
    )
    assert preserved_truncated.status_code == 200, preserved_truncated.text
    assert preserved_truncated.json()["template"]["item_ids"] == [
        "100001",
        truncated_legacy_id,
    ]

    injected_unknown = client.put(
        "/api/bot/templates",
        json={
            "template": {
                "id": redeem_template["id"],
                "name": "兑换码模板（截断目录）",
                "delivery": "redeem",
                "item_ids": ["100001", truncated_legacy_id, "900002"],
                "enabled": False,
            }
        },
    )
    assert injected_unknown.status_code == 400, injected_unknown.text

    # Once the catalog is known complete, the omitted ID is stale and must be
    # removed rather than silently carried forward.
    shop_sync.save_snapshot(user_id, snapshot(COOKIE), "default")
    stale_complete = client.put(
        "/api/bot/templates",
        json={
            "template": {
                "id": redeem_template["id"],
                "name": "兑换码模板（完整目录）",
                "delivery": "redeem",
                "item_ids": ["100001", truncated_legacy_id],
                "enabled": False,
            }
        },
    )
    assert stale_complete.status_code == 400, stale_complete.text
    cleaned_complete = client.put(
        "/api/bot/templates",
        json={
            "template": {
                "id": redeem_template["id"],
                "name": "兑换码模板（完整目录）",
                "delivery": "redeem",
                "item_ids": ["100001"],
                "enabled": False,
            }
        },
    )
    assert cleaned_complete.status_code == 200, cleaned_complete.text

    # A non-existent id falls back to creating a new template per contract.
    another = client.put(
        "/api/bot/templates",
        json={
            "template": {
                "id": "does-not-exist",
                "name": "新模板",
                "delivery": "redeem",
                "item_ids": [],
            }
        },
    )
    assert another.status_code == 200, another.text
    assert another.json()["template"]["id"].startswith("template-")
    assert another.json()["template"]["item_ids"] == []

    # Invalid item_id is rejected; snapshot scoping is enforced.
    bad = client.put(
        "/api/bot/templates",
        json={
            "template": {
                "name": "坏商品",
                "delivery": "redeem",
                "item_ids": ["999999"],
            }
        },
    )
    assert bad.status_code == 400, bad.text
    assert "商品" in bad.json()["detail"]

    # DELETE removes an existing template and returns ok.
    deleted = client.delete(f"/api/bot/templates/{pan_template['id']}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True}
    after_delete = client.get("/api/bot/templates")
    ids = [item["id"] for item in after_delete.json()["templates"]]
    assert pan_template["id"] not in ids
    assert redeem_template["id"] in ids

    # DELETE of a missing template returns 404.
    missing = client.delete(f"/api/bot/templates/{pan_template['id']}")
    assert missing.status_code == 404, missing.text

    # Card pool starts empty when the file does not exist.
    cards_empty = client.get("/api/bot/cards")
    assert cards_empty.status_code == 200, cards_empty.text
    assert cards_empty.json() == {
        "pool": {
            "id": "redeem",
            "name": "兑换码池",
            "note": "",
            "total": 0,
            "available": 0,
            "used": 0,
            "enabled": True,
        },
        "stats": {"pools": 1, "total": 0, "available": 0, "reserved": 0, "used": 0},
    }

    # PUT cards writes a deduplicated pool and returns current stats.
    saved_cards = client.put(
        "/api/bot/cards",
        json={
            "name": "我的兑换池",
            "note": "兑换卡密",
            "codes": [
                {"code": "ABC123", "used": False},
                {"code": "DEF456", "used": True},
                {"code": "ABC123", "used": True},
            ],
        },
    )
    assert saved_cards.status_code == 200, saved_cards.text
    saved_payload = saved_cards.json()
    assert saved_payload["ok"] is True
    assert saved_payload["pool"]["name"] == "我的兑换池"
    assert saved_payload["pool"]["note"] == "兑换卡密"
    assert saved_payload["pool"]["total"] == 2
    assert saved_payload["pool"]["available"] == 0
    assert saved_payload["pool"]["used"] == 2
    assert saved_payload["stats"] == {
        "pools": 1,
        "total": 2,
        "available": 0,
        "reserved": 0,
        "used": 2,
    }
    stored_codes = read_codes(user_id)
    assert len(stored_codes) == 2
    by_code = {item["code"]: item for item in stored_codes}
    assert by_code["ABC123"]["used"] is True
    assert by_code["DEF456"]["used"] is True

    # GET reflects the saved pool name/note and current stats.
    cards = client.get("/api/bot/cards")
    assert cards.status_code == 200, cards.text
    assert cards.json()["pool"]["total"] == 2
    assert cards.json()["pool"]["available"] == 0
    assert cards.json()["pool"]["used"] == 2
    assert cards.json()["stats"] == saved_payload["stats"]
    assert cards.json()["pool"]["name"] == "我的兑换池"
    assert cards.json()["pool"]["note"] == "兑换卡密"

    # Import is append-only: a later batch cannot silently replace old stock.
    appended = client.put(
        "/api/bot/cards",
        json={"name": "我的兑换池", "note": "兑换卡密", "codes": [{"code": "GHI789"}]},
    )
    assert appended.status_code == 200, appended.text
    assert appended.json()["pool"]["total"] == 3
    appended_codes = {item["code"]: item for item in read_codes(user_id)}
    assert set(appended_codes) == {"ABC123", "DEF456", "GHI789"}
    assert appended_codes["ABC123"]["used"] is True
    assert appended_codes["GHI789"]["used"] is False

    # Creating a new empty pool is allowed and preserves existing inventory.
    empty_pool = client.put("/api/bot/cards", json={"name": "新卡池", "note": "尚未导入", "codes": []})
    assert empty_pool.status_code == 200, empty_pool.text
    assert empty_pool.json()["pool"]["total"] == 3
    assert empty_pool.json()["pool"]["name"] == "新卡池"

    # Inventory cannot change underneath a running worker; pause first so the
    # next controlled start imports and reconciles the manifest.
    with patch.object(app, "bot_status", return_value={"running": True}):
        running_update = client.put(
            "/api/bot/cards",
            json={"name": "新卡池", "note": "运行中", "codes": [{"code": "BLOCKED"}]},
        )
    assert running_update.status_code == 409, running_update.text
    assert "BLOCKED" not in {item["code"] for item in read_codes(user_id)}

    # Basic input validation stays stable and does not leak payloads.
    for bad in (
        {"template": {"name": "", "delivery": "redeem", "item_ids": []}},
        {"template": {"name": "x", "delivery": "other", "item_ids": []}},
        {"template": {"name": "x", "delivery": "pan", "item_ids": []}},
        {"template": {"name": "x" * 121, "delivery": "redeem", "item_ids": []}},
        {"template": {"name": "x", "description": "d" * 501, "delivery": "redeem", "item_ids": []}},
    ):
        assert client.put("/api/bot/templates", json=bad).status_code == 400
    for bad_cards in (
        {"name": "", "note": "", "codes": [{"code": "X"}]},
        {"name": "x", "note": "n" * 501, "codes": [{"code": "X"}]},
        {"name": "x", "note": "", "codes": [{"code": "Y" * 513}]},
    ):
        assert client.put("/api/bot/cards", json=bad_cards).status_code == 400

    # Legacy expiry values do not remove fulfillment.manage in self-use mode.
    with app.db._lock:
        app.db.con.execute("UPDATE users SET expires_at = 0 WHERE id = ?", (user_id,))
        app.db.con.commit()
    free_templates = client.get("/api/bot/templates")
    assert free_templates.status_code == 200, free_templates.text
    free_cards = client.get("/api/bot/cards")
    assert free_cards.status_code == 200, free_cards.text

    del default_root
    print("templates-cards contract: templates CRUD, card pool stats, scoped validation and free permission passed")


if __name__ == "__main__":
    main()