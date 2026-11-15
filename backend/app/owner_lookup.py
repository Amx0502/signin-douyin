"""归属用户信息查询：企微通知里要展示「用户名」。

上游实现还会带上会员卡型（如「张三（次卡）」），独立版没有会员卡，
``owner_card_type`` 恒为 None，``owner_display_name`` 自然只显示用户名。
查询失败一律返回 None：归属信息只是通知里的附加展示，不能影响续火本身。
"""

from __future__ import annotations

from sqlalchemy import select

from .auth_models import UserRow


def owner_username(database, owner_user_id: int | None) -> str | None:
    """归属用户名；``owner_user_id`` 为空或查询失败时返回 None。"""
    return _lookup(database, owner_user_id, UserRow.username)


def owner_card_type(database, owner_user_id: int | None) -> str | None:
    """独立版没有会员卡，恒返回 None（保留签名以兼容上游调用方）。"""
    return None


def _lookup(database, owner_user_id, column):
    if owner_user_id is None:
        return None
    try:
        with database.session() as session:
            return session.scalar(
                select(column).where(UserRow.id == int(owner_user_id))
            )
    except Exception:
        return None
