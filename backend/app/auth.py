"""极简账号体系：用户名 + 密码登录，HMAC 签名 token。

只为独立运行的抖音续火服务提供「谁在操作 / 任务归属谁」，
不包含原系统的会员卡、平台额度、菜单权限等业务逻辑。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from .auth_models import UserRow

TOKEN_TTL_SECONDS = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", str(7 * 24 * 3600)))
PBKDF2_ROUNDS = 240_000


# ------------------------------------------------------------------ 密码
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_b64, digest_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, int(rounds)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------ token
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_token(user_id: int, secret: str, ttl: int = TOKEN_TTL_SECONDS) -> str:
    payload = {"uid": int(user_id), "exp": int(time.time()) + int(ttl)}
    body = _b64e(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(signature)}"


def decode_token(token: str, secret: str) -> int | None:
    """校验签名与有效期，返回 user_id；任何异常都按未登录处理。"""
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(
            secret.encode(), body.encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64d(signature), expected):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return int(payload["uid"])
    except Exception:
        return None


# ------------------------------------------------------------------ 依赖
def build_auth_dependency(database_provider, secret: str):
    """返回 FastAPI 依赖：解析 Bearer token，取出用户 dict。

    ``database_provider`` 是返回 ``DouyinDatabase`` 的 callable，延迟到请求期
    才取值 —— 此时 lifespan 已完成初始化。抖音路由工厂
    ``create_douyin_router(auth_dependency, menu_dependency)`` 需要的正是它。
    """
    bearer = HTTPBearer(auto_error=False)

    def get_current_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> dict:
        unauthorized = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"ok": False, "error": "登录已过期，请重新登录"},
            headers={"WWW-Authenticate": "Bearer"},
        )
        if credentials is None or not credentials.credentials:
            raise unauthorized
        user_id = decode_token(credentials.credentials, secret)
        if user_id is None:
            raise unauthorized
        database = database_provider()
        with database.session() as session:
            row = session.get(UserRow, user_id)
            if row is None or not row.is_active:
                raise unauthorized
            return row.to_dict()

    return get_current_user


# ------------------------------------------------------------------ 初始化
def ensure_initial_admin(database) -> tuple[str, str] | None:
    """库里没有用户时创建初始管理员；返回 (username, 明文密码)。

    密码取 ``ADMIN_PASSWORD``，未配置则随机生成并打印一次。
    """
    with database.session() as session:
        existing = session.scalar(select(UserRow.id).limit(1))
        if existing is not None:
            return None
        username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
        password = os.getenv("ADMIN_PASSWORD", "").strip() or secrets.token_urlsafe(12)
        session.add(
            UserRow(
                username=username,
                password_hash=hash_password(password),
                role="admin",
                is_active=True,
            )
        )
        return username, password
