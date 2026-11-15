"""抖音续火独立服务入口。

启动：
    cd backend
    python run.py                 # 或 uvicorn app.main:app --reload

首次启动会自动建库（默认 SQLite 单文件）并创建管理员账号，密码打印在控制台。
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import config
from .auth import build_auth_dependency, ensure_initial_admin, issue_token, verify_password
from .auth_models import AuthBase, UserRow
from .douyin_database import DouyinDatabase
from .douyin_login import DouyinLoginManager
from .douyin_router import create_douyin_router
from .douyin_scheduler import DouyinScheduler
from .douyin_service import DouyinService

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("douyin")

FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


def _load_auth_secret() -> str:
    """优先 AUTH_SECRET；否则落盘一个随机密钥，保证重启后 token 仍然有效。"""
    env_secret = os.getenv("AUTH_SECRET", "").strip()
    if env_secret:
        return env_secret
    path = config.DATA_DIR / "auth_secret"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return value


AUTH_SECRET = _load_auth_secret()


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    database = DouyinDatabase()
    database.initialize()
    AuthBase.metadata.create_all(database.engine)
    app.state.douyin_database = database

    created = ensure_initial_admin(database)
    if created:
        username, password = created
        logger.warning(
            "已创建初始管理员：%s / %s（登录后请尽快修改，或通过 ADMIN_PASSWORD 指定）",
            username,
            password,
        )

    service = DouyinService(database)
    app.state.douyin_service = service
    manager = DouyinLoginManager(service.create_account, service._resolve_proxy)
    app.state.douyin_login_manager = manager
    service.set_login_manager(manager)

    scheduler = DouyinScheduler(service)
    scheduler.start()
    app.state.douyin_scheduler = scheduler
    logger.info("抖音续火服务已启动（db=%s）", database.url)

    try:
        yield
    finally:
        try:
            scheduler.shutdown()
        except Exception:
            logger.exception("关闭调度器失败")
        try:
            manager.shutdown()
        except Exception:
            logger.exception("关闭登录管理器失败")
        database.dispose()


app = FastAPI(
    title="抖音续火独立服务",
    description="抖音好友续火 / 自动消息的独立后端（账号、任务、运行记录、日志）",
    version="1.0.0",
    lifespan=lifespan,
)

_origins = [
    item.strip()
    for item in os.getenv(
        "FRONTEND_ORIGIN", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 延迟到请求期取 database：此时 lifespan 已完成初始化
auth_required = build_auth_dependency(
    lambda: app.state.douyin_database, AUTH_SECRET
)

app.include_router(create_douyin_router(auth_required, lambda key: auth_required))


@app.post("/api/auth/login")
def login(payload: LoginIn):
    database = app.state.douyin_database
    with database.session() as session:
        row = session.scalar(
            select(UserRow).where(UserRow.username == payload.username.strip())
        )
        if (
            row is None
            or not row.is_active
            or not verify_password(payload.password, row.password_hash)
        ):
            raise HTTPException(
                status_code=401,
                detail={"ok": False, "error": "用户名或密码错误"},
            )
        return {
            "ok": True,
            "data": {
                "token": issue_token(row.id, AUTH_SECRET),
                "user": row.to_dict(),
            },
        }


@app.get("/api/auth/me")
def me(user=Depends(auth_required)):
    return {"ok": True, "data": user}


@app.get("/api/health")
def health():
    database = app.state.douyin_database
    return {"ok": True, "data": {"status": "up", "db": database.url.split("@")[-1]}}


# 可选：把打包好的前端 dist 一起托管，单进程即可打开页面
if FRONTEND_DIST.exists():
    app.mount(
        "/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets"
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
