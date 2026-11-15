import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .douyin_client import DouyinRemoteError
from .douyin_models import (
    DouyinAccountCreate,
    DouyinAccountUpdate,
    DouyinBatchDelete,
    DouyinBatchState,
    DouyinGlobalSettings,
    DouyinNotifySettings,
    DouyinProxyPayload,
    DouyinRetryTargetPayload,
    DouyinTaskPayload,
)
from .douyin_service import DouyinNotFound, DouyinValidationError


logger = logging.getLogger(__name__)


def create_douyin_router(auth_dependency, menu_dependency=None):
    router = APIRouter(prefix="/api/douyin", tags=["douyin"])
    guard = lambda key: menu_dependency(key) if menu_dependency else auth_dependency

    def invoke(request, operation, *args, **kwargs):
        try:
            return {"ok": True, "data": operation(*args, **kwargs)}
        except DouyinNotFound as exc:
            return JSONResponse(status_code=404, content={"ok": False, "error": str(exc)})
        except DouyinValidationError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        except DouyinRemoteError as exc:
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": str(exc), "retryable": exc.retryable, "auth_failed": exc.auth_failed},
            )
        except Exception:
            logger.exception("抖音续火接口执行失败：%s", getattr(operation, "__name__", operation))
            return JSONResponse(status_code=500, content={"ok": False, "error": "抖音续火服务内部错误"})

    # ---------------------------------------------------------------- 账号

    @router.post("/accounts")
    def create_account(payload: DouyinAccountCreate, request: Request, user=Depends(guard("douyin.accounts"))):
        return invoke(request, request.app.state.douyin_service.create_account, payload.model_dump(), user)

    @router.get("/accounts")
    def accounts(request: Request, user=Depends(guard(("douyin.overview", "douyin.accounts", "douyin.auto")))):
        return invoke(request, request.app.state.douyin_service.list_accounts, user)

    @router.get("/accounts/access")
    def account_access(request: Request, user=Depends(guard(("douyin.overview", "douyin.accounts", "douyin.auto")))):
        """账号额度概览（额度/已用/是否可继续添加），供前端禁用添加入口。"""
        return invoke(request, request.app.state.douyin_service.account_access, user)

    @router.put("/accounts/{account_id}")
    def update_account(account_id: int, payload: DouyinAccountUpdate, request: Request, user=Depends(guard("douyin.accounts"))):
        return invoke(request, request.app.state.douyin_service.update_account, account_id, payload.model_dump(), user)

    @router.delete("/accounts/{account_id}")
    def delete_account(account_id: int, request: Request, user=Depends(guard("douyin.accounts"))):
        return invoke(request, request.app.state.douyin_service.delete_account, account_id, user)

    @router.post("/accounts/{account_id}/verify")
    def verify_account(account_id: int, request: Request, user=Depends(guard(("douyin.accounts", "douyin.overview")))):
        return invoke(request, request.app.state.douyin_service.verify_account, account_id, user)

    @router.get("/accounts/{account_id}/friends")
    def account_friends_cache(account_id: int, request: Request, user=Depends(guard(("douyin.accounts", "douyin.auto", "douyin.overview")))):
        """读取上次拉取后保留的好友缓存（不请求抖音）。"""
        return invoke(request, request.app.state.douyin_service.list_friends_cached, account_id, user)

    @router.get("/accounts/{account_id}/spark")
    def account_spark_cache(account_id: int, request: Request, user=Depends(guard(("douyin.accounts", "douyin.auto", "douyin.overview")))):
        """读取本地缓存的平台火花快照（不请求抖音）。"""
        return invoke(request, request.app.state.douyin_service.spark_states_cached, account_id, user)

    @router.post("/accounts/{account_id}/spark")
    def account_spark(account_id: int, request: Request, user=Depends(guard(("douyin.accounts", "douyin.auto", "douyin.overview")))):
        """实时读取该账号的平台真实火花状态（只读会话列表，不发送消息）。"""
        return invoke(request, request.app.state.douyin_service.get_spark_states, account_id, user)

    @router.post("/accounts/{account_id}/friends")
    def account_friends(account_id: int, request: Request, user=Depends(guard(("douyin.accounts", "douyin.auto", "douyin.overview")))):
        """实时拉取该账号的可私信好友（只读会话列表，不发送消息），并更新缓存。"""
        return invoke(request, request.app.state.douyin_service.list_friends, account_id, user)

    @router.post("/accounts/{account_id}/refresh-cookies")
    def account_refresh_cookies(account_id: int, request: Request, user=Depends(guard(("douyin.accounts", "douyin.overview")))):
        """cookie 保活：带旧 cookie 访问抖音回收新 cookie（多数「登录失效」不必重扫码）。"""
        return invoke(request, request.app.state.douyin_service.refresh_account_cookies, account_id, user)

    @router.post("/accounts/{account_id}/friends/refresh")
    def account_friends_refresh(account_id: int, request: Request, user=Depends(guard(("douyin.accounts", "douyin.auto", "douyin.overview")))):
        """后台拉取好友：立即返回任务状态，结果经 GET friends/job 轮询。"""
        return invoke(request, request.app.state.douyin_service.start_friends_refresh, account_id, user)

    @router.get("/accounts/{account_id}/friends/job")
    def account_friends_job(account_id: int, request: Request, user=Depends(guard(("douyin.accounts", "douyin.auto", "douyin.overview")))):
        """查询后台好友拉取任务状态（running / success / error / idle）。"""
        return invoke(request, request.app.state.douyin_service.get_friends_job, account_id, user)

    # ---------------------------------------------------------------- 代理

    @router.get("/proxies")
    def proxies(request: Request, user=Depends(guard(("douyin.accounts", "douyin.overview")))):
        return invoke(request, request.app.state.douyin_service.list_proxies, user)

    @router.post("/proxies")
    def create_proxy(payload: DouyinProxyPayload, request: Request, user=Depends(guard("douyin.accounts"))):
        return invoke(request, request.app.state.douyin_service.create_proxy, payload.model_dump(), user)

    @router.put("/proxies/{proxy_id}")
    def update_proxy(proxy_id: int, payload: DouyinProxyPayload, request: Request, user=Depends(guard("douyin.accounts"))):
        return invoke(request, request.app.state.douyin_service.update_proxy, proxy_id, payload.model_dump(), user)

    @router.delete("/proxies/{proxy_id}")
    def delete_proxy(proxy_id: int, request: Request, user=Depends(guard("douyin.accounts"))):
        return invoke(request, request.app.state.douyin_service.delete_proxy, proxy_id, user)

    # ---------------------------------------------------------------- 扫码登录

    @router.post("/login-qr")
    async def start_login_qr(request: Request, user=Depends(guard("douyin.accounts"))):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return invoke(request, request.app.state.douyin_login_manager.start, payload or {}, user)

    @router.post("/login-sms")
    async def start_login_sms(request: Request, user=Depends(guard("douyin.accounts"))):
        """手机号验证码登录：填手机号 + 请求发送短信验证码。"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return invoke(request, request.app.state.douyin_login_manager.start_sms, payload or {}, user)

    @router.post("/login-sms/{qr_id}/code")
    async def submit_login_sms_code(qr_id: str, request: Request, user=Depends(guard("douyin.accounts"))):
        """提交短信验证码，随后处理「原设备确认」弹窗。"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        code = str((payload or {}).get("code") or "").strip()
        return invoke(
            request,
            request.app.state.douyin_login_manager.submit_sms_code,
            qr_id, code,
        )

    @router.get("/login-qr/{qr_id}")
    def poll_login_qr(qr_id: str, request: Request, user=Depends(guard("douyin.accounts"))):
        return invoke(request, request.app.state.douyin_login_manager.poll, qr_id, user)

    @router.post("/login-qr/{qr_id}/verification")
    async def submit_login_verification(qr_id: str, request: Request, user=Depends(guard("douyin.accounts"))):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        code = str((payload or {}).get("code") or "").strip()
        return invoke(
            request,
            request.app.state.douyin_login_manager.submit_verification,
            qr_id, code,
        )

    @router.delete("/login-qr/{qr_id}")
    def close_login_qr(qr_id: str, request: Request, user=Depends(guard("douyin.accounts"))):
        return invoke(request, request.app.state.douyin_login_manager.close, qr_id)

    # ---------------------------------------------------------------- 总览/任务

    @router.get("/overview")
    def overview(request: Request, user=Depends(guard("douyin.overview"))):
        return invoke(request, request.app.state.douyin_service.get_overview, user)

    @router.get("/tasks")
    def tasks(request: Request, user=Depends(guard(("douyin.overview", "douyin.auto")))):
        return invoke(request, request.app.state.douyin_service.list_tasks, user)

    @router.post("/tasks")
    def create_task(payload: DouyinTaskPayload, request: Request, user=Depends(guard("douyin.auto"))):
        return invoke(request, request.app.state.douyin_service.create_task, payload.model_dump(), user)

    @router.post("/tasks/batch-state")
    def batch_state(payload: DouyinBatchState, request: Request, user=Depends(guard("douyin.auto"))):
        return invoke(request, request.app.state.douyin_service.batch_set_task_state, payload.ids, payload.enabled, user)

    @router.post("/tasks/batch-delete")
    def batch_delete(payload: DouyinBatchDelete, request: Request, user=Depends(guard("douyin.auto"))):
        return invoke(request, request.app.state.douyin_service.batch_delete_tasks, payload.ids, user)

    @router.post("/tasks/batch-run")
    def batch_run(request: Request, user=Depends(guard("douyin.auto"))):
        return invoke(request, request.app.state.douyin_service.batch_run_tasks, user)

    @router.put("/tasks/{task_id}")
    def update_task(task_id: int, payload: DouyinTaskPayload, request: Request, user=Depends(guard("douyin.auto"))):
        return invoke(request, request.app.state.douyin_service.update_task, task_id, payload.model_dump(), user)

    @router.delete("/tasks/{task_id}")
    def delete_task(task_id: int, request: Request, user=Depends(guard("douyin.auto"))):
        return invoke(request, request.app.state.douyin_service.delete_task, task_id, user)

    @router.post("/tasks/{task_id}/run")
    def run_task(task_id: int, request: Request, user=Depends(guard("douyin.auto"))):
        return invoke(request, request.app.state.douyin_service.run_task, task_id, user)

    # ---------------------------------------------------------------- 运行/日志/设置

    @router.get("/runs")
    def runs(
        request: Request,
        limit: int = Query(100, ge=1, le=500),
        status: str = Query(""),
        account_id: int = Query(0, ge=0),
        user=Depends(guard(("douyin.runs", "douyin.overview"))),
    ):
        return invoke(
            request,
            request.app.state.douyin_service.list_runs,
            user, limit, status, account_id or None,
        )

    @router.get("/runs/{run_id}")
    def run_detail(run_id: int, request: Request, user=Depends(guard(("douyin.runs", "douyin.overview")))):
        return invoke(request, request.app.state.douyin_service.get_run, run_id, user)

    @router.post("/runs/{run_id}/retry-target")
    def run_retry_target(run_id: int, payload: DouyinRetryTargetPayload, request: Request,
                         user=Depends(guard(("douyin.runs", "douyin.overview", "douyin.auto")))):
        """对某次运行里失败的目标单独重发（只发这一个好友）。"""
        return invoke(request, request.app.state.douyin_service.retry_run_target,
                      run_id, payload.target, user)

    @router.get("/logs")
    def logs(
        request: Request,
        category: str = Query(""),
        level: str = Query(""),
        keyword: str = Query(""),
        account_id: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        user=Depends(guard(("douyin.logs", "douyin.overview", "douyin.accounts", "douyin.auto", "douyin.runs"))),
    ):
        return invoke(
            request,
            request.app.state.douyin_service.list_logs,
            user,
            category=category, level=level, keyword=keyword,
            account_id=account_id or None, limit=limit,
        )

    @router.delete("/logs")
    def clear_logs(request: Request, user=Depends(guard("douyin.logs"))):
        return invoke(request, request.app.state.douyin_service.clear_logs, user)

    @router.get("/logs/export")
    def export_logs(request: Request, user=Depends(guard(("douyin.logs", "douyin.overview")))):
        text = request.app.state.douyin_service.export_logs_text(user)
        return PlainTextResponse(
            text,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=douyin-logs.txt"},
        )

    @router.get("/settings")
    def get_settings(request: Request, user=Depends(guard(("douyin.overview", "douyin.accounts", "douyin.auto")))):
        return invoke(request, request.app.state.douyin_service.get_global_settings, user)

    @router.put("/settings")
    def update_settings(payload: DouyinGlobalSettings, request: Request, user=Depends(guard("douyin.accounts"))):
        return invoke(request, request.app.state.douyin_service.update_global_settings, payload.model_dump(), user)

    @router.get("/notify-settings")
    def get_notify_settings(request: Request, user=Depends(guard(("douyin.overview", "douyin.accounts", "douyin.auto")))):
        return invoke(request, request.app.state.douyin_service.get_notify_settings, user)

    @router.put("/notify-settings")
    def update_notify_settings(payload: DouyinNotifySettings, request: Request, user=Depends(guard("douyin.accounts"))):
        return invoke(request, request.app.state.douyin_service.update_notify_settings, payload.model_dump(), user)

    @router.get("/builtin-messages")
    def builtin_messages(request: Request, user=Depends(guard(("douyin.overview", "douyin.accounts", "douyin.auto")))):
        return invoke(request, lambda: {"variants": request.app.state.douyin_service.get_default_variants()})

    return router
