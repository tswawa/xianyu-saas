#!/usr/bin/env python3
"""Contract checks for the read-only mtop shop synchronizer."""

from __future__ import annotations

import json
import os
import tempfile


RUN_DIR = tempfile.mkdtemp(prefix="xianyu-shop-sync-contract-")
os.environ["SAAS_TENANTS_DIR"] = os.path.join(RUN_DIR, "tenants")
os.environ["SAAS_SHOP_SYNC_COOLDOWN_SECONDS"] = "1"

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import platform_profile
import shop_sync


def success(data):
    return {"ret": ["SUCCESS::调用成功"], "data": data}


def main():
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
