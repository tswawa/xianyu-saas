"""Server-side subscription and permission policy for the SaaS."""

from __future__ import annotations

import time


FREE_PERMISSIONS = frozenset(
    {
        "shop.configure",
        "products.manage",
        "automation.rules",
        "fulfillment.basic",
        "fulfillment.manage",
        "records.read",
        "runtime.logs",
        "analytics.read",
    }
)
MEMBER_PERMISSIONS = frozenset(
    {
        "automation.ai",
    }
)
ALL_PERMISSIONS = FREE_PERMISSIONS | MEMBER_PERMISSIONS

PERMISSION_LABELS = {
    "shop.configure": "店铺管理",
    "products.manage": "商品管理",
    "automation.rules": "关键词自动回复",
    "fulfillment.basic": "订单自动发资料",
    "automation.ai": "AI 智能客服",
    "fulfillment.manage": "卡券与履约",
    "records.read": "对话与订单记录",
    "runtime.logs": "运行日志",
    "analytics.read": "统计查询",
}


def plan_for(expires_at: float | int | None, now: float | None = None) -> str:
    """Calculate the plan at request time; never trust a client-supplied plan."""
    if now is None:
        now = time.time()
    try:
        return "member" if float(expires_at or 0) > now else "free"
    except (TypeError, ValueError):
        return "free"


def permissions_for(plan: str) -> frozenset[str]:
    """Return the self-use permission set; legacy plan fields are compatibility only."""
    return ALL_PERMISSIONS


def has_permission(user_or_expires_at, permission: str, now: float | None = None) -> bool:
    if isinstance(user_or_expires_at, (dict,)):
        expires_at = user_or_expires_at.get("expires_at")
    else:
        try:
            expires_at = user_or_expires_at["expires_at"]
        except (KeyError, TypeError, IndexError):
            expires_at = user_or_expires_at
    return permission in permissions_for(plan_for(expires_at, now))


def account_payload(user, now: float | None = None) -> dict:
    if now is None:
        now = time.time()
    plan = plan_for(user["expires_at"], now)
    permissions = sorted(permissions_for(plan))
    return {
        "username": user["username"],
        "expires_at": user["expires_at"],
        "active": plan == "member",
        "plan": plan,
        "plan_label": "会员" if plan == "member" else "免费",
        "permissions": permissions,
        "permission_labels": [PERMISSION_LABELS[item] for item in permissions],
    }
