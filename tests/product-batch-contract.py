#!/usr/bin/env python3
"""Offline contract for account-scoped product material batch updates."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-saas-product-batch-contract-"))
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


COOKIE_ONE = "unb=123456; _m_h5_tk=contract-one_value"
COOKIE_TWO = "unb=654321; _m_h5_tk=contract-two_value"


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
            {"id": "100004", "title": "商品四", "description": "四", "price": 4, "status": "在售"},
        ],
        "product_count": 4,
        "synced_at": "2026-08-17T10:00:00+0800",
        "truncated": False,
    }


def config_payload() -> dict:
    return {
        "version": 1,
        "types": [
            {
                "id": "basic-100001",
                "name": "商品一",
                "item_ids": ["100001"],
                "delivery": "material",
                "enabled": True,
                "payload": "旧资料",
            },
            {
                "id": "redeem-100002",
                "name": "商品二兑换码",
                "item_ids": ["100002"],
                "delivery": "redeem",
                "enabled": True,
            },
            {
                "id": "basic-100003",
                "name": "商品三",
                "item_ids": ["100003"],
                "delivery": "material",
                "enabled": True,
                "payload": "保留资料",
            },
            {
                "id": "pan-100004",
                "name": "商品四网盘",
                "item_ids": ["100004"],
                "delivery": "pan",
                "enabled": True,
                "resource_match": ["商品四"],
            },
        ],
    }


def read_config(user_id: int, account_key: str = "default") -> dict:
    raw = app.read_secret(user_id, "products_config.json", account_key)
    return json.loads(raw or '{"version": 1, "types": []}')


def main() -> None:
    client = TestClient(app.app)
    app.db.create_user(
        "batch-owner",
        "password-123",
        role="owner",
        initializer=app._new_user_initializer({}),
    )
    login = client.post(
        "/api/auth/login", json={"username": "batch-owner", "password": "password-123"}
    )
    assert login.status_code == 200, login.text
    client.cookies.set("xianyu_saas_session", login.cookies.get("xianyu_saas_session"), path="/")
    user_id = int(app.db.get_user("batch-owner")["id"])
    with app.db._lock:
        app.db.con.execute("UPDATE users SET expires_at = 4102444800 WHERE id = ?", (user_id,))
        app.db.con.commit()

    storage = AccountStorage(str(RUN_DIR / "tenants"))
    default_root = storage.ensure_account_dir(user_id, "default")
    initial_rules = app.read_secret(user_id, "reply_rules.json", "default")
    initial_settings = app.read_secret(user_id, "automation_settings.json", "default")
    assert json.loads(initial_rules) == {"version": 1, "rules": []}
    assert json.loads(initial_settings) == {
        "version": 1,
        "strategy": "standard",
        "enabled": True,
    }
    assert (default_root / "reply_rules.json").stat().st_mode & 0o777 == 0o600
    assert (default_root / "automation_settings.json").stat().st_mode & 0o777 == 0o600
    app.write_secret(user_id, "cookies.txt", COOKIE_ONE, "default")
    shop_sync.save_snapshot(user_id, snapshot(COOKIE_ONE), "default")
    app.write_secret(user_id, "products_config.json", json.dumps(config_payload(), ensure_ascii=False), "default")
    scoped_updates = [{"item_id": "100001", "enabled": True, "material": "同一资料"}]
    assert app._material_batch_token(
        {"id": 1, "account_key": "default"}, snapshot(COOKIE_ONE), config_payload(), scoped_updates
    ) != app._material_batch_token(
        {"id": 2, "account_key": "default"}, snapshot(COOKIE_ONE), config_payload(), scoped_updates
    )

    created = client.post("/api/bot/accounts", json={"key": "second", "name": "第二店铺"})
    assert created.status_code == 200, created.text
    second_root = storage.ensure_account_dir(user_id, "second")
    app.write_secret(user_id, "cookies.txt", COOKIE_TWO, "second")
    shop_sync.save_snapshot(user_id, snapshot(COOKIE_TWO), "second")
    app.write_secret(
        user_id,
        "products_config.json",
        json.dumps({"version": 1, "types": []}),
        "second",
    )

    original = app.read_secret(user_id, "products_config.json", "default")
    preview_request = {"item_ids": ["100001", "100003"], "material": "新资料", "enabled": True}
    preview = client.post("/api/bot/products/batch/preview", json=preview_request)
    assert preview.status_code == 200, preview.text
    preview_payload = preview.json()
    assert set(preview_payload) == {"ok", "preview"}
    preview_info = preview_payload["preview"]
    assert preview_info["selected_count"] == 2
    assert preview_info["change_count"] == 2
    assert preview_info["unchanged_count"] == 0
    assert "新资料" not in preview.text
    assert app.read_secret(user_id, "products_config.json", "default") == original
    assert app.read_secret(user_id, "reply_rules.json", "default") == initial_rules
    assert app.read_secret(user_id, "automation_settings.json", "default") == initial_settings

    commit = client.post(
        "/api/bot/products/batch/commit",
        json={**preview_request, "preview_token": preview_info["preview_token"]},
    )
    assert commit.status_code == 200, commit.text
    assert set(commit.json()) == {"ok", "preview", "automation"}
    assert "新资料" not in commit.text
    committed = read_config(user_id)
    by_id = {
        str(item_id): item
        for item in committed["types"]
        for item_id in item.get("item_ids", [])
    }
    assert by_id["100001"]["payload"] == "新资料"
    assert by_id["100003"]["payload"] == "新资料"
    assert by_id["100002"]["delivery"] == "redeem"
    assert by_id["100004"]["delivery"] == "pan"
    assert app.read_secret(user_id, "reply_rules.json", "default") == initial_rules
    assert app.read_secret(user_id, "automation_settings.json", "default") == initial_settings

    # Replaying the exact commit after the first successful write is idempotent
    # even though the configuration revision has changed.
    replay = client.post(
        "/api/bot/products/batch/commit",
        json={**preview_request, "preview_token": preview_info["preview_token"]},
    )
    assert replay.status_code == 200, replay.text
    assert read_config(user_id) == committed

    # Re-previewing the same effective state is a no-op and remains idempotent.
    same_preview = client.post("/api/bot/products/batch/preview", json=preview_request)
    assert same_preview.status_code == 200
    assert same_preview.json()["preview"]["change_count"] == 0
    same_commit = client.post(
        "/api/bot/products/batch/commit",
        json={**preview_request, "preview_token": same_preview.json()["preview"]["preview_token"]},
    )
    assert same_commit.status_code == 200, same_commit.text
    assert read_config(user_id) == committed

    # Disabling preserves the selected material正文 but prevents delivery.
    disable_request = {"item_ids": ["100001"], "material": "", "enabled": False}
    disable_preview = client.post("/api/bot/products/batch/preview", json=disable_request)
    assert disable_preview.status_code == 200
    assert disable_preview.json()["preview"]["change_count"] == 1
    disabled = client.post(
        "/api/bot/products/batch/commit",
        json={**disable_request, "preview_token": disable_preview.json()["preview"]["preview_token"]},
    )
    assert disabled.status_code == 200, disabled.text
    disabled_by_id = {
        str(item_id): item
        for item in read_config(user_id)["types"]
        for item_id in item.get("item_ids", [])
    }
    assert disabled_by_id["100001"]["enabled"] is False
    assert disabled_by_id["100001"]["payload"] == "新资料"
    assert disabled_by_id["100003"]["enabled"] is True

    # Concurrent tabs cannot merge from the same stale file state and let the
    # later atomic rename overwrite an unrelated first change.
    leased_request = {"item_ids": ["100003"], "material": "租约资料", "enabled": True}
    leased_preview = client.post("/api/bot/products/batch/preview", json=leased_request)
    assert leased_preview.status_code == 200
    default_account = app.db.get_shop_account(user_id, account_key="default")
    lease_key = f"products-config:{user_id}:{int(default_account['id'])}"
    assert app.db.acquire_control_lease(
        lease_key, "contract-holder", lease_seconds=45, cooldown_seconds=0
    ) == "acquired"
    blocked_commit = client.post(
        "/api/bot/products/batch/commit",
        json={**leased_request, "preview_token": leased_preview.json()["preview"]["preview_token"]},
    )
    assert blocked_commit.status_code == 409
    assert client.put(
        "/api/automation",
        json={"deliveries": [{"item_id": "100003", "enabled": True, "material": "并发资料"}]},
    ).status_code == 409
    assert client.put(
        "/api/bot/templates",
        json={
            "template": {
                "name": "并发模板",
                "description": "锁内不应写入",
                "delivery": "redeem",
                "item_ids": ["100003"],
            }
        },
    ).status_code == 409
    assert client.put("/api/bot/products", json={"products": {"types": []}}).status_code == 409
    assert app.db.release_control_lease(lease_key, "contract-holder") is True
    leased_commit = client.post(
        "/api/bot/products/batch/commit",
        json={**leased_request, "preview_token": leased_preview.json()["preview"]["preview_token"]},
    )
    assert leased_commit.status_code == 200, leased_commit.text

    # A legacy group containing multiple delivery types keeps a selected
    # redeem/pan mapping when another member of the group is updated.
    grouped = {
        "version": 1,
        "types": [
            {
                "id": "mixed",
                "item_ids": ["100002", "100003"],
                "delivery": "redeem",
                "enabled": True,
                "resource_match": ["商品二"],
            }
        ],
    }
    app.write_secret(user_id, "products_config.json", json.dumps(grouped), "default")
    mixed_request = {"item_ids": ["100002", "100003"], "material": "混合资料", "enabled": False}
    mixed_preview = client.post("/api/bot/products/batch/preview", json=mixed_request)
    assert mixed_preview.status_code == 200
    mixed_commit = client.post(
        "/api/bot/products/batch/commit",
        json={**mixed_request, "preview_token": mixed_preview.json()["preview"]["preview_token"]},
    )
    assert mixed_commit.status_code == 200, mixed_commit.text
    mixed_types = read_config(user_id)["types"]
    assert any(item.get("delivery") == "redeem" and "100002" in item.get("item_ids", []) for item in mixed_types)
    assert not any(item.get("delivery") == "material" and "100003" in item.get("item_ids", []) for item in mixed_types)

    # Any configuration change invalidates an old preview token.
    stale_request = {"item_ids": ["100003"], "material": "暂存资料", "enabled": True}
    stale_preview = client.post("/api/bot/products/batch/preview", json=stale_request)
    assert stale_preview.status_code == 200
    app.write_secret(
        user_id,
        "products_config.json",
        json.dumps({"version": 1, "types": read_config(user_id)["types"] + [{"id": "unrelated", "item_ids": [], "delivery": "material", "enabled": False, "payload": "x"}]}),
        "default",
    )
    stale_commit = client.post(
        "/api/bot/products/batch/commit",
        json={**stale_request, "preview_token": stale_preview.json()["preview"]["preview_token"]},
    )
    assert stale_commit.status_code == 409, stale_commit.text

    # The same product IDs in another shop are isolated by the account header.
    second_request = {"item_ids": ["100001"], "material": "第二店资料", "enabled": True}
    second_preview = client.post(
        "/api/bot/products/batch/preview", headers={"X-Shop-Account": "second"}, json=second_request
    )
    assert second_preview.status_code == 200, second_preview.text
    second_commit = client.post(
        "/api/bot/products/batch/commit",
        headers={"X-Shop-Account": "second"},
        json={**second_request, "preview_token": second_preview.json()["preview"]["preview_token"]},
    )
    assert second_commit.status_code == 200, second_commit.text
    assert read_config(user_id, "second")["types"]
    assert "第二店资料" not in second_commit.text
    assert read_config(user_id, "default")["types"] != read_config(user_id, "second")["types"]
    cross_account = client.post(
        "/api/bot/products/batch/commit",
        json={**second_request, "preview_token": second_preview.json()["preview"]["preview_token"]},
    )
    assert cross_account.status_code == 409

    # Input and account state gates.
    for bad in (
        {"item_ids": [], "material": "x", "enabled": True},
        {"item_ids": ["999999"], "material": "x", "enabled": True},
        {"item_ids": ["100001", "100001"], "material": "x", "enabled": True},
        {"item_ids": ["100001"], "material": "", "enabled": True},
        {"item_ids": ["100001"], "material": "x\x00y", "enabled": True},
        {"item_ids": ["100001"], "material": "x" * 8001, "enabled": True},
    ):
        assert client.post("/api/bot/products/batch/preview", json=bad).status_code == 400
    empty = client.post("/api/bot/accounts", json={"key": "empty", "name": "空店铺"})
    assert empty.status_code == 200
    assert client.post(
        "/api/bot/products/batch/preview",
        headers={"X-Shop-Account": "empty"},
        json=preview_request,
    ).status_code == 409
    with app.db._lock:
        app.db.con.execute("UPDATE users SET expires_at = 0 WHERE id = ?", (user_id,))
        app.db.con.commit()
    assert float(app.db.get_user_by_id(user_id)["expires_at"] or 0) == 0
    free_response = client.post("/api/bot/products/batch/preview", json=preview_request)
    assert free_response.status_code == 200, free_response.text
    del default_root, second_root
    print("product-batch contract: preview, stale token, local merge, idempotency and account isolation passed")


if __name__ == "__main__":
    main()
