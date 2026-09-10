#!/usr/bin/env python3
"""Contract checks for the read-only mtop shop synchronizer."""

from __future__ import annotations

import io
import json
import os
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch


RUN_DIR = tempfile.mkdtemp(prefix="xianyu-shop-sync-contract-")
os.environ["SAAS_TENANTS_DIR"] = os.path.join(RUN_DIR, "tenants")
os.environ["SAAS_DB"] = os.path.join(RUN_DIR, "saas.db")
os.environ["SAAS_BOT_ROOT"] = os.path.join(RUN_DIR, "worker-not-installed")
os.environ["SAAS_TESTING"] = "1"
os.environ["SAAS_SHOP_SYNC_COOLDOWN_SECONDS"] = "1"

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import platform_profile
import shop_sync


def success(data):
    return {"ret": ["SUCCESS::调用成功"], "data": data}


def classification_contract():
    import bot_manager

    cases = (
        ({"ret": ["FAIL_SYS_BUSY::被挤爆啦"]}, "platform_busy"),
        ({"ret": ["UNKNOWN::被挤爆"]}, "platform_busy"),
        ({"ret": ["RGV587_ERROR::被挤爆啦"]}, "risk_control"),
        ({"ret": ["USER_VALIDATE"]}, "risk_control"),
        ({"ret": ["LOGIN_CHECK"]}, "risk_control"),
        ({"ret": ["CAPTCHA"]}, "verification_required"),
        ({"ret": ["FAIL_SYS_SECURITY_CHECK"]}, "verification_required"),
        ({"ret": ["RGV587_USER_VALIDATE::请先完成安全验证"]}, "verification_required"),
        ({"content": {"success": False, "captchaRequired": True}}, "verification_required"),
        ({"data": {"verificationRequired": True}}, "verification_required"),
        ({"ret": ["FAIL_SYS_BUSY"], "data": {"title": "captcha SECURITY_CHECK RGV587", "code": "CAPTCHA"}}, "platform_busy"),
        ({"ret": ["UNKNOWN"], "data": {"description": "请先完成安全验证", "captchaRequired": "true"}}, "platform_error"),
        ({"ret": ["FAIL_SYS_NOT_CAPTCHA_ERROR"]}, "platform_error"),
        ({"ret": ["CAPTCHA_NOT_REQUIRED"]}, "platform_error"),
        ({"ret": ["RGV587::不需要完成安全验证"]}, "risk_control"),
        ({"ret": ["USER_VALIDATE::无需完成安全验证"]}, "risk_control"),
        ({"ret": ["UNKNOWN::NO CAPTCHA REQUIRED"]}, "platform_error"),
        ({"ret": ["UNKNOWN::NOT SECURITY CHECK REQUIRED"]}, "platform_error"),
        ({"ret": ["NOT_SUCCESS::调用成功"]}, "platform_error"),
        ({"ret": ["FAIL_WITH_SUCCESS::调用成功"]}, "platform_error"),
        ({"ret": ["SUCCESS::调用成功", "CAPTCHA"]}, "verification_required"),
        ({"ret": ["FAIL_SYS_SESSION_EXPIRED"], "data": {"title": "CAPTCHA"}}, "cookie_expired"),
        ({"ret": ["PUBLISH_FORBIDDEN"], "data": {"title": "CAPTCHA"}}, "account_restricted"),
    )
    for payload, expected in cases:
        with patch.object(shop_sync, "_trip_circuit") as trip:
            try:
                shop_sync._classify_response(payload)
            except shop_sync.ShopSyncError as error:
                assert error.code == expected, (payload, error.code)
            else:
                raise AssertionError((payload, "failure was accepted"))
            assert trip.call_count == int(expected in {"risk_control", "verification_required"})
    business = {"title": "CAPTCHA RGV587 被挤爆", "description": "请先完成安全验证"}
    assert shop_sync._classify_response(success(business)) == business

    # An HTTP JSON error body is still an envelope, not searchable business data.
    for payload, expected in (
        ({"data": {"title": "CAPTCHA RGV587"}}, "platform_busy"),
        ({"ret": ["RGV587_USER_VALIDATE::被挤爆啦"]}, "risk_control"),
        ({"ret": ["CAPTCHA"]}, "verification_required"),
    ):
        failure = urllib.error.HTTPError("https://mock.invalid", 429, "mock", {}, io.BytesIO(json.dumps(payload).encode()))
        with (
            patch.object(shop_sync, "_circuit_until", return_value=0),
            patch.object(shop_sync, "_trip_circuit"),
            patch.object(shop_sync.time, "sleep"),
            patch.object(shop_sync.urllib.request, "urlopen", side_effect=failure) as request,
        ):
            try:
                shop_sync._request("mock", {"_m_h5_tk": "mock_1"}, "mock", {}, "mock")
            except shop_sync.ShopSyncError as error:
                assert error.code == expected
            else:
                raise AssertionError("HTTP failure must not succeed")
            assert request.call_count == 1

    with (
        patch.object(shop_sync, "_circuit_until", return_value=float("inf")),
        patch.object(shop_sync.urllib.request, "urlopen") as request,
    ):
        try:
            shop_sync._request("mock", {"_m_h5_tk": "mock_1"}, "mock", {}, "mock")
        except shop_sync.ShopSyncError as error:
            assert error.code == "risk_cooldown"
            assert str(error) == shop_sync.SYNC_STATUS_CATALOG["risk_cooldown"]["message"]
        else:
            raise AssertionError("local cooldown must keep requests paused")
        request.assert_not_called()

    for code in ("risk_control", "risk_cooldown", "verification_required"):
        expected = shop_sync.SYNC_STATUS_CATALOG[code]
        status = shop_sync.sync_status_payload(code, "闲鱼App要求安全验证")
        assert status["message"] == expected["message"]
        root = Path(shop_sync._account_root(99, create=True))
        state_path = root / shop_sync.SYNC_STATE_NAME
        legacy = {"version": 1, "code": code, "message": "闲鱼App要求安全验证", "checked_at": "old", "account_ref": "mock"}
        state_path.write_text(json.dumps(legacy), encoding="utf-8")
        original = state_path.read_bytes()
        loaded = shop_sync.load_sync_state(99)
        assert loaded["message"] == expected["message"]
        assert state_path.read_bytes() == original
        assert loaded["checked_at"] == "old" and loaded["account_ref"] == "mock"
        for snapshot in (None, {"products": [{"id": "1"}]}):
            view = bot_manager._status_view(code, True, snapshot, int(snapshot is not None))
            assert view["connection_state"] == ("security_check" if code == "verification_required" else "degraded")
            assert view["catalog_state"] == ("stale" if snapshot else "blocked")
            assert view["capabilities"]["view_products"] == (snapshot is not None)
            assert view["attention"][0]["title"] == expected["label"]
    shop_sync.save_sync_state(99, "verification_required", "old", account_ref_value="mock")
    assert shop_sync.load_sync_state(99)["code"] == "verification_required"
    assert "verification_required" in shop_sync.PERSISTED_SYNC_CODES


def main():
    classification_contract()
    calls = []

    def fake_request(api, data, spm):
        calls.append((api, data, spm))
        if api == shop_sync.PROFILE_API:
            return success({"nick": "合同闲鱼账号"})
        assert data["userId"] == "123456"
        assert data["pageNumber"] == 1
        return success(
            {
                "topItem": {
                    "id": "100",
                    "titleSummary": {"text": "置顶商品"},
                    "priceInfo": {"price": "¥9.90"},
                    "picInfo": {"picUrl": "https://img.example.invalid/top.jpg"},
                    "itemStatus": "0",
                },
                "cardList": [
                    {
                        "cardData": {
                            "itemId": "101",
                            "title": "普通商品",
                            "price": "12",
                            "itemLabelDataVO": {
                                "labelData": {
                                    "left": {"tagList": [{"data": {"type": "text", "content": "包邮"}}]}
                                }
                            },
                            "itemStatus": "1",
                        }
                    }
                ],
                "nextPage": "false",
            }
        )

    result = shop_sync.sync_shop(
        "Cookie: unb=123456; _m_h5_tk=token-value_abc; sid=contract",
        request_func=fake_request,
    )
    assert result["nickname"] == "合同闲鱼账号"
    assert [item["id"] for item in result["products"]] == ["100", "101"]
    assert result["products"][0]["price"] == "9.90"
    assert result["products"][0]["image_url"] == "https://img.example.invalid/top.jpg"
    assert result["products"][1]["description"] == "包邮"
    assert result["products"][1]["status"] == "已下架"
    invalid_image = shop_sync.extract_product(
        {"id": "102", "title": "无效图片", "picInfo": {"picUrl": "javascript:alert(1)"}},
        "",
    )
    assert invalid_image is not None and "image_url" not in invalid_image
    assert [item[0] for item in calls] == [shop_sync.PROFILE_API, shop_sync.PRODUCTS_API]
    assert shop_sync.SYNC_MAX_SECONDS < 70
    headers = platform_profile.browser_headers()
    assert f"Chrome/{platform_profile.CHROME_VERSION}" in headers["User-Agent"]
    assert f'v="{platform_profile.CHROME_MAJOR}"' in headers["sec-ch-ua"]
    assert headers["Origin"] == "https://www.goofish.com"
    assert headers["Referer"] == "https://www.goofish.com/"

    def nested_profile(api, _data, _spm):
        if api == shop_sync.PROFILE_API:
            return success({
                "data": json.dumps({
                    "userInfo": {"nickName": "嵌套闲鱼账号", "userId": "123456"}
                })
            })
        return success({"cardList": [], "nextPage": False})

    nested = shop_sync.sync_shop(
        "unb=123456; _m_h5_tk=token-value_abc",
        request_func=nested_profile,
    )
    assert nested["nickname"] == "嵌套闲鱼账号"

    def alternate_profile(api, _data, _spm):
        if api == shop_sync.PROFILE_API:
            return success({"nickName": "备用字段账号", "uid": 123456})
        return success({"cardList": [], "nextPage": False})

    alternate = shop_sync.sync_shop(
        "unb=123456; _m_h5_tk=token-value_abc",
        request_func=alternate_profile,
    )
    assert alternate["nickname"] == "备用字段账号"

    def cookie_name_profile(api, _data, _spm):
        if api == shop_sync.PROFILE_API:
            return success({})
        return success({"cardList": [], "nextPage": False})

    cookie_name = shop_sync.sync_shop(
        "unb=123456; tracknick=%E6%89%AB%E7%A0%81%E5%BA%97%E9%93%BA; _m_h5_tk=token-value_abc",
        request_func=cookie_name_profile,
    )
    assert cookie_name["nickname"] == "扫码店铺"

    def empty_profile(api, _data, _spm):
        if api == shop_sync.PROFILE_API:
            return success({})
        return success({"cardList": [], "nextPage": False})

    try:
        shop_sync.sync_shop(
            "unb=123456; _m_h5_tk=token-value_abc",
            request_func=empty_profile,
        )
    except shop_sync.ShopSyncError as error:
        assert error.code == "profile_missing"
    else:
        raise AssertionError("an empty profile must fail closed")

    def mismatched_profile(_api, _data, _spm):
        return success({"nick": "错误账号", "userId": "999999"})

    try:
        shop_sync.sync_shop(
            "unb=123456; _m_h5_tk=token-value_abc",
            request_func=mismatched_profile,
        )
    except shop_sync.ShopSyncError as error:
        assert error.code == "cookie_invalid"
    else:
        raise AssertionError("profile ID mismatch must reject a mixed Cookie header")

    held = shop_sync._sync_gate.acquire(blocking=False)
    assert held
    try:
        try:
            shop_sync.sync_shop(
                "unb=123456; _m_h5_tk=token-value_abc",
                request_func=fake_request,
            )
        except shop_sync.ShopSyncError as error:
            assert error.code == "sync_busy"
        else:
            raise AssertionError("concurrent sync must be rejected")
    finally:
        shop_sync._sync_gate.release()

    try:
        shop_sync.parse_cookie_header("sid=missing-login")
    except shop_sync.ShopSyncError as error:
        assert error.code == "cookie_incomplete"
    else:
        raise AssertionError("incomplete Cookie must be rejected")

    def risk_request(_api, _data, _spm):
        return {"ret": ["RGV587_USER_VALIDATE"]}

    try:
        shop_sync.sync_shop("unb=123456; _m_h5_tk=token-value_abc", request_func=risk_request)
    except shop_sync.ShopSyncError as error:
        assert error.code == "risk_control"
    else:
        raise AssertionError("risk response must trip a safe error")

    def restricted_request(_api, _data, _spm):
        return {"ret": ["ITEM_PUBLISH_FORBIDDEN"]}

    try:
        shop_sync.sync_shop("unb=123456; _m_h5_tk=token-value_abc", request_func=restricted_request)
    except shop_sync.ShopSyncError as error:
        assert error.code == "account_restricted"
        assert "暂时不能发布商品" in str(error)
    else:
        raise AssertionError("publishing restriction must be classified separately from a security challenge")

    def expired_request(_api, _data, _spm):
        return {"ret": ["fail_sys_session_expired::session_expired"]}

    try:
        shop_sync.sync_shop("unb=123456; _m_h5_tk=token-value_abc", request_func=expired_request)
    except shop_sync.ShopSyncError as error:
        assert error.code == "cookie_expired"
    else:
        raise AssertionError("expired session must be classified separately from risk control")

    def busy_request(_api, _data, _spm):
        return {"ret": ["FAIL_SYS_BUSY::temporary"]}

    try:
        shop_sync.sync_shop("unb=123456; _m_h5_tk=token-value_abc", request_func=busy_request)
    except shop_sync.ShopSyncError as error:
        assert error.code == "platform_busy"
    else:
        raise AssertionError("platform busy must degrade without becoming a security challenge")

    assert shop_sync._failure_code_from_text("HTTP 429 TOO_MANY_REQUESTS") == "platform_busy"
    assert shop_sync._failure_code_from_text("RGV587_USER_VALIDATE") == "risk_control"

    print("shop-sync-contract: parsing, pagination, normalization and risk boundary passed")


if __name__ == "__main__":
    main()
