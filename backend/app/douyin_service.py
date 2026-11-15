"""抖音续火平台服务：账号管理、续火任务调度与协议发送执行。

发送链路：build_messages_by_target（模板变体 + 一言）→ run_protocol_send
（node 子进程 protocol_sender.mjs，加载官方 IM SDK bundle）→ 回执映射。
"""

import base64
import hashlib
import json
import logging
import os
import random
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import case, desc, func, select

from . import config
from .douyin_client import (
    DEFAULT_MESSAGE_VARIANTS,
    DouyinCookieError,
    DouyinRemoteError,
    build_target_payload,
    cookie_summary,
    fetch_self_profile,
    fetch_session_identity,
    format_target_spec,
    is_legacy_default_pool,
    normalize_target_specs,
    parse_cookie_input,
    require_auth_cookies,
    run_protocol_list_friends,
    run_protocol_friends_and_sparks,
    run_protocol_send,
    run_protocol_spark_state,
    summarize_status,
)
from .douyin_cookie_refresh import CookieRefreshError, refresh_cookies
from .douyin_database import DouyinDatabase
from .douyin_browser_sender import BrowserSendError, send_via_browser
from .douyin_identity import resolve_identity
from .douyin_site import active_site
from .douyin_db_models import DouyinAccountRow, DouyinLogRow, DouyinProxyRow, DouyinRunRow, DouyinSettingRow, DouyinTaskRow
from .douyin_notifier import DouyinNotifier
from .douyin_settings import (
    DouyinSettingsError,
    load_douyin_settings,
    save_douyin_settings,
)
from .owner_lookup import owner_card_type, owner_username
from .task_date_schedule import (
    get_last_effective_occurrence,
    is_task_active_on,
    normalize_task_date_rule,
    simple_due_key,
)


logger = logging.getLogger(__name__)

GLOBAL_SETTINGS_KEY = "global"
LOG_CATEGORIES = ("account", "friends", "fire", "task", "scheduler", "login", "system")
LOG_LEVELS = ("info", "success", "warning", "error")


def _iso_day(value) -> str:
    """按天聚合的 DATE() 结果统一成 ISO 日期串（MySQL 返回 date，SQLite 返回 str）。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _merge_flow_rows(rows, success_statuses, failed_statuses) -> list[dict]:
    """触发来源 × 终态交叉表，合并成 (source, stage) 唯一行，供总览桑基图使用。

    多对一映射（success / partial 都归 success）会产生同源同终态的多行，
    必须先合并，否则前端桑基图出现重复边、总量对不上。
    """
    merged: dict[tuple[str, str], int] = {}
    for source, status, value in rows:
        text = str(status or "").lower()
        if text in success_statuses:
            stage = "success"
        elif text in failed_statuses:
            stage = "failed"
        else:
            stage = "other"
        key = (str(source or "unknown"), stage)
        merged[key] = merged.get(key, 0) + int(value or 0)
    return [
        {"source": source, "stage": stage, "value": value}
        for (source, stage), value in sorted(merged.items())
    ]


# 协议栈（protocol_sender.mjs）未实现抖音网页接口必需的 a_bogus 签名，请求必然被
# 安全校验拦（CheckMessageNotPass / checkMsg.status_code=8610），每个目标白等约 70s
# 才回退浏览器通道。默认直接走浏览器；等协议侧补上 msToken/a_bogus 后可用环境变量
# DOUYIN_PROTOCOL_ENABLED=1 打开（对照 JoeanAmier/TikTokDownloader 的做法）。
PROTOCOL_CHANNEL_ENABLED = str(os.environ.get("DOUYIN_PROTOCOL_ENABLED", "0")).strip().lower() in {
    "1", "true", "yes", "on",
}
# 续火任务并发数（不同账号并行；同账号仍由账号锁串行）。小内存服务器可用环境变量
# DOUYIN_MAX_PARALLEL_ACCOUNTS=1 收回串行（每个 chromium 约 300MB）
MAX_PARALLEL_RUNS = min(max(int(os.environ.get("DOUYIN_MAX_PARALLEL_ACCOUNTS", "2") or 2), 1), 4)
# cookie 保活：每个账号多久刷新一次 + 扫描节流
# 单账号排队上限：极端堆积时丢弃而不是无限排（每个排队任务都要跑一次浏览器）
MAX_QUEUED_RUNS_PER_ACCOUNT = 20
COOKIE_REFRESH_INTERVAL_HOURS = 6
COOKIE_REFRESH_SCAN_INTERVAL_SECONDS = 60
LOGIN_BROWSER_IDLE_SECONDS = 600


class DouyinValidationError(ValueError):
    pass


class DouyinNotFound(LookupError):
    pass


class DouyinService:
    def __init__(self, database: DouyinDatabase, *, notifier: DouyinNotifier | None = None):
        self.database = database
        raw_key = config.DOUYIN_TOKEN_ENCRYPTION_KEY
        self.cipher = None
        if raw_key and raw_key != "replace-with-a-fernet-key":
            try:
                self.cipher = Fernet(raw_key.encode())
            except (ValueError, TypeError) as exc:
                raise RuntimeError("DOUYIN_TOKEN_ENCRYPTION_KEY 不是有效的 Fernet 密钥") from exc
        elif database.settings.password:
            derived = hashlib.sha256(
                ("signin:douyin:" + database.settings.password).encode()
            ).digest()
            self.cipher = Fernet(base64.urlsafe_b64encode(derived))
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_PARALLEL_RUNS, thread_name_prefix="douyin-fire"
        )
        # cookie 保活单独一条线程：起浏览器访问抖音，别堵住续火发送队列
        self._cookie_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="douyin-cookie")
        self._last_cookie_scan = 0.0
        # 好友拉取与发送分开：拉好友可能跑几十秒，不能堵住续火发送队列
        self._friends_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="douyin-friends")
        self._friends_jobs: dict[int, dict] = {}
        self._friends_jobs_guard = threading.Lock()
        self.notifier = notifier or DouyinNotifier()
        self._pending: set = set()
        self._account_locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # 同账号多任务排队：每账号一条 FIFO 队列，全局并发仍受 MAX_PARALLEL_RUNS 限制
        self._account_queues: dict[int, deque] = {}
        self._account_busy: set = set()
        self._active_runs = 0
        self._queue_guard = threading.Lock()
        self._closed = False
        # 扫码登录管理器（main 装配后回填）：调度循环借它回收空闲浏览器内核
        self.login_manager = None

    # ------------------------------------------------------------------ 基础

    def set_login_manager(self, manager):
        """由 main 装配扫码登录管理器（可选依赖，缺省不影响续火）。"""
        self.login_manager = manager

    def close(self):
        self.shutdown()

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        # wait=True：等待正在执行的续火线程退出再返回。sqlite 连接被后台
        # 线程使用时若调用方（应用关闭/测试 tearDown）立刻 dispose 引擎，
        # 会在 sqlite3 C 层触发 access violation 段错误；取消排队任务、
        # 等待在跑任务结束，保证 dispose 时无线程仍持有连接。
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._cookie_executor.shutdown(wait=True, cancel_futures=True)
        self._friends_executor.shutdown(wait=True, cancel_futures=True)

    @staticmethod
    def _owner(user: dict) -> int:
        return int(user["id"])

    @staticmethod
    def _is_admin(user: dict) -> bool:
        return user.get("role") == "admin"

    def _scope(self, stmt, model, user):
        return stmt if self._is_admin(user) else stmt.where(model.owner_user_id == self._owner(user))

    def _encrypt(self, value: str) -> str:
        if not self.cipher:
            raise DouyinValidationError("尚未配置抖音续火凭据加密密钥（DOUYIN_TOKEN_ENCRYPTION_KEY）")
        return self.cipher.encrypt(value.encode()).decode()

    def _decrypt(self, ciphertext: str, message: str) -> str:
        if not self.cipher:
            raise DouyinValidationError("尚未配置抖音续火凭据加密密钥（DOUYIN_TOKEN_ENCRYPTION_KEY）")
        try:
            return self.cipher.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise DouyinValidationError(message) from exc

    def _account_lock(self, account_id: int) -> threading.Lock:
        with self._locks_guard:
            if account_id not in self._account_locks:
                self._account_locks[account_id] = threading.Lock()
            return self._account_locks[account_id]

    def _cookies_for(self, account: DouyinAccountRow) -> list:
        raw = self._decrypt(account.cookies, "抖音 cookie 无法解密，请重新添加账号")
        try:
            cookies = json.loads(raw)
        except ValueError as exc:
            raise DouyinValidationError("抖音 cookie 数据损坏，请重新添加账号") from exc
        if not isinstance(cookies, list):
            raise DouyinValidationError("抖音 cookie 数据损坏，请重新添加账号")
        return cookies

    @staticmethod
    def _fetch_self_profile(cookies, proxy) -> dict:
        """按站点拉自身资料（昵称/抖音号/头像）。

        creator：passport 接口可直连；主站：JSON 接口需 a_bogus 签名，资料已在
        ``resolve_identity`` 的浏览器阶段拿到，这里不再重复请求。
        """
        if active_site().key == "creator":
            return fetch_self_profile(cookies, proxy) or {}
        return {}

    @staticmethod
    def _protocol_user_id(account: DouyinAccountRow | None) -> str:
        """协议层 SDK 初始化所需的平台 uid（主站链路必需）。

        占位账号（``pending_`` 前缀，cookie 未校验通过）返回空，由协议层报错。
        """
        uid = str(getattr(account, "remote_user_id", "") or "")
        return "" if uid.startswith("pending_") else uid

    @staticmethod
    def _profile_on(account: DouyinAccountRow) -> bool:
        """该账号是否启用持久浏览器 profile。

        存量行补列后是 NULL（ORM 的 python default 只在 INSERT 生效），
        语义上「未显式关闭 = 启用」，因此 None 视为 True。
        """
        return account.profile_enabled is not False

    def _write_log(self, category, level, message, *, owner_user_id=None, account_id=None):
        message = str(message or "")[:2000]
        with self.database.session() as session:
            session.add(DouyinLogRow(
                owner_user_id=owner_user_id,
                category=category if category in LOG_CATEGORIES else "system",
                level=level if level in LOG_LEVELS else "info",
                message=message,
                account_id=account_id,
            ))

    # ------------------------------------------------------------------ 账号

    def list_owned_accounts(self, user_id: int) -> list[dict]:
        """某用户名下的抖音账号展示摘要（管理员在用户详情抽屉里查看用）。

        只回吐展示字段——cookies、代理配置一律不外泄。
        """
        with self.database.session() as session:
            rows = list(session.scalars(
                select(DouyinAccountRow)
                .where(DouyinAccountRow.owner_user_id == int(user_id))
                .order_by(DouyinAccountRow.id)
            ))
        return [
            {
                "id": row.id,
                "name": row.remark or row.nickname or row.remote_user_id,
                "account": row.douyin_id or row.remote_user_id,
                "enabled": bool(row.enabled),
                "status": row.status or "",
            }
            for row in rows
        ]

    def _assert_account_quota(self, session, user: dict) -> None:
        """抖音账号额度（policy.douyin_account_limit）——只约束**新建绑定**。

        已有账号重新提交 cookie/重新校验不占新额度，故只在新建分支调用。
        额度为 None 表示不限；管理员不受限。
        """
        if self._is_admin(user):
            return
        limit = user.get("douyin_account_limit")
        if limit is None:
            return
        count = session.scalar(
            select(func.count()).select_from(DouyinAccountRow).where(
                DouyinAccountRow.owner_user_id == self._owner(user)
            )
        ) or 0
        if count >= int(limit):
            raise DouyinValidationError(
                f"抖音账号额度已满（{limit} 个），请联系管理员调整额度"
            )

    def create_account(self, payload: dict, user: dict) -> dict:
        remark = (payload.get("remark") or "").strip()
        cookie_input = payload.get("cookie_input") or ""
        proxy_id = int(payload.get("proxy_id") or 0) or None
        # 扫码登录会带上页面里已抓到的身份（主站链路），有它就不必再开一次浏览器
        hint_user_id = str(payload.get("remote_user_id") or "").strip()
        hint_profile = {
            "nickname": str(payload.get("nickname") or "").strip(),
            "douyin_id": str(payload.get("douyin_id") or "").strip(),
            "avatar": str(payload.get("avatar") or "").strip(),
        }
        try:
            cookies = parse_cookie_input(cookie_input)
            require_auth_cookies(cookies)
        except DouyinCookieError as exc:
            raise DouyinValidationError(str(exc)) from exc
        proxy_cfg = self._resolve_proxy(proxy_id, user)
        if hint_user_id:
            identity = {"user_id": hint_user_id}
            profile = {**hint_profile, "sec_uid": ""}
        else:
            resolved = resolve_identity(cookies, proxy=proxy_cfg)
            if not resolved.get("logged_in"):
                if resolved.get("auth_failed") is False and resolved.get("message"):
                    # 非鉴权类失败（网络/环境问题）→ 直接抛出，不落无效账号
                    raise DouyinRemoteError(str(resolved["message"]), retryable=True)
                exc = DouyinRemoteError(str(resolved.get("message") or "登录态校验失败"))
                # 宽容导入策略（对齐同类平台）：校验失败的 cookie 也入库并标记无效，
                # 用户补到有效 cookie 后走「校验」找回真实身份。
                with self.database.session() as session:
                    self._assert_account_quota(session, user)
                    row = DouyinAccountRow(
                        owner_user_id=self._owner(user),
                        remote_user_id=f"pending_{uuid.uuid4().hex[:20]}",
                        remark=remark,
                        proxy_id=proxy_id,
                        cookies=self._encrypt(json.dumps(cookies, ensure_ascii=False)),
                        source="cookie",
                        status="invalid",
                        enabled=False,
                        last_error=str(exc),
                    )
                    session.add(row)
                    session.flush()
                    result = self._account_dict(row)
                self._write_log(
                    "account", "warning",
                    f"cookie 校验未通过，已按无效状态导入（{result['display_name']}）",
                    owner_user_id=self._owner(user), account_id=result["id"],
                )
                return {**result, "cookie_valid": False, "message": "Cookie 校验未通过（已失效或被风控），已保存为无效账号，可在更新 Cookie 后点「校验」找回"}
            identity = {"user_id": str(resolved.get("user_id") or "").strip()}
            if not identity["user_id"]:
                raise DouyinRemoteError("登录态已通过但未取到账号身份（uid），请稍后重试或改用扫码登录")
            profile = {
                "nickname": str(resolved.get("nickname") or ""),
                "douyin_id": str(resolved.get("douyin_id") or ""),
                "avatar": str(resolved.get("avatar") or ""),
                "sec_uid": str(resolved.get("sec_uid") or ""),
            }

        with self.database.session() as session:
            row = session.scalar(
                select(DouyinAccountRow).where(DouyinAccountRow.remote_user_id == identity["user_id"])
            )
            is_new = row is None
            if is_new:
                self._assert_account_quota(session, user)
                row = DouyinAccountRow(
                    owner_user_id=self._owner(user),
                    remote_user_id=identity["user_id"],
                    proxy_id=proxy_id,
                    cookies="",
                )
                session.add(row)
                session.flush()
            elif row.owner_user_id != self._owner(user) and not self._is_admin(user):
                raise DouyinValidationError("该抖音账号已由其他用户绑定")
            elif proxy_id:
                row.proxy_id = proxy_id
            # 备注留空时自动用抖音昵称命名（而非落到 UID）；已有昵称不覆盖。
            # 顺手补抖音号与头像（均为增强信息，拉不到不影响建号）
            if not remark and (not row.nickname or not row.douyin_id or not row.avatar):
                detail = profile
                if not any(detail.get(key) for key in ("nickname", "douyin_id", "avatar")):
                    detail = self._fetch_self_profile(cookies, proxy_cfg)
                row.nickname = row.nickname or detail.get("nickname") or ""
                row.douyin_id = row.douyin_id or detail.get("douyin_id") or ""
                row.avatar = row.avatar or detail.get("avatar") or ""
            row.remark = remark or row.remark
            row.cookies = self._encrypt(json.dumps(cookies, ensure_ascii=False))
            row.source = "cookie"
            row.status = "active"
            row.last_error = ""
            row.last_verified_at = datetime.now()
            session.flush()
            result = self._account_dict(row)
        self._write_log(
            "account", "success",
            f"添加抖音账号成功（{result['display_name']}，cookie {cookie_summary(cookies)['count']} 条）",
            owner_user_id=self._owner(user), account_id=result["id"],
        )
        return result

    def list_accounts(self, user: dict) -> list[dict]:
        with self.database.session() as session:
            stmt = self._scope(select(DouyinAccountRow), DouyinAccountRow, user).order_by(
                DouyinAccountRow.created_at.desc()
            )
            return [self._account_dict(row) for row in session.scalars(stmt)]

    def account_access(self, user: dict) -> dict:
        """账号额度概览——前端据此禁用「Cookie 添加 / 扫码登录」入口。

        额度为 None（管理员或未限额）表示不限；普通用户额度满时提示删除账号后再加。
        """
        with self.database.session() as session:
            count = session.scalar(
                select(func.count()).select_from(DouyinAccountRow).where(
                    DouyinAccountRow.owner_user_id == self._owner(user)
                )
            ) or 0
        limit = None if self._is_admin(user) else user.get("douyin_account_limit")
        can_add = limit is None or count < int(limit)
        return {"account_limit": limit, "account_count": count, "can_add_account": can_add}

    def update_account(self, account_id: int, payload: dict, user: dict) -> dict:
        with self.database.session() as session:
            row = self._get_account(session, account_id, user)
            row.remark = (payload.get("remark") or "").strip()
            row.enabled = bool(payload.get("enabled", True))
            if payload.get("proxy_id") is not None:
                proxy_id = int(payload.get("proxy_id") or 0) or None
                if proxy_id:
                    self._resolve_proxy(proxy_id, user)
                row.proxy_id = proxy_id
            cookie_input = (payload.get("cookie_input") or "").strip()
            if cookie_input:
                try:
                    cookies = parse_cookie_input(cookie_input)
                    require_auth_cookies(cookies)
                except DouyinCookieError as exc:
                    raise DouyinValidationError(str(exc)) from exc
                row.cookies = self._encrypt(json.dumps(cookies, ensure_ascii=False))
                row.status = "active"
                row.last_error = ""
                row.last_verified_at = datetime.now()
            row.updated_at = datetime.now()
            session.flush()
            result = self._account_dict(row)
        if cookie_input:
            self._write_log("account", "info", f"更新账号 cookie（{result['display_name']}）",
                            owner_user_id=self._owner(user), account_id=account_id)
        return result

    def delete_account(self, account_id: int, user: dict):
        with self.database.session() as session:
            row = self._get_account(session, account_id, user)
            name = row.remark or row.nickname or row.remote_user_id
            session.delete(row)
        # P1：清掉该账号的持久浏览器 profile（磁盘 30~80MB/账号，不能留孤儿目录）
        try:
            from .douyin_profile import purge_profile  # noqa: PLC0415

            purge_profile(account_id)
        except Exception:  # noqa: BLE001 —— 清理失败不影响删除结果
            logger.info("抖音账号 %s：profile 清理异常", account_id, exc_info=True)
        self._write_log("account", "warning", f"删除抖音账号：{name}",
                        owner_user_id=self._owner(user), account_id=account_id)

    def deactivate_accounts_by_owner(self, owner_user_id: int) -> dict:
        """会员周期卡到期：停用名下抖音账号 + 删除自动任务（软处理；学习通已改为删除账号）。

        数据保留范围（均不删除，供续费后管理员重新启用继续使用）：
        - 账号行：保留，仅置 enabled=False（cookies/备注/昵称原样保留）
        - 代理配置：保留
        - 运行记录：保留（归属原用户，历史留痕）
        - 历史日志：保留（归属原用户）
        自动任务：全部删除（卡片已失效，续火任务不应再执行）。
        本方法幂等：重复触发只对「还未停用的账号/仍存在的任务」生效，
        无实际变化时不写日志，避免巡检循环刷屏。
        """
        owner_id = int(owner_user_id)
        with self.database.session() as session:
            accounts = list(session.scalars(select(DouyinAccountRow).where(
                DouyinAccountRow.owner_user_id == owner_id
            )))
            disabled = 0
            for row in accounts:
                if row.enabled:
                    row.enabled = False
                    disabled += 1
            tasks = list(session.scalars(select(DouyinTaskRow).where(
                DouyinTaskRow.owner_user_id == owner_id
            )))
            for task in tasks:
                session.delete(task)
            session.flush()
        if disabled or tasks:
            self._write_log(
                "account", "warning",
                f"会员卡到期：已停用 {disabled} 个抖音账号、删除 {len(tasks)} 个续火任务"
                "（运行记录/历史日志保留）",
                owner_user_id=owner_id,
            )
        return {"accounts": disabled, "tasks": len(tasks)}

    def verify_account(self, account_id: int, user: dict) -> dict:
        """校验 cookie 登录态。

        creator 链路走 IM user_token 接口预检；主站链路走浏览器（打开首页读
        页面自身的 profile/self 响应）。因此网络/浏览器验证放在 DB 会话之外。
        """
        with self.database.session() as session:
            account = self._get_account(session, account_id, user)
            cookies = self._cookies_for(account)
            proxy_cfg = self._resolve_proxy(account.proxy_id, user)
            display = account.remark or account.nickname or account.remote_user_id

        resolved = resolve_identity(cookies, account_id=account_id, proxy=proxy_cfg)
        if not resolved.get("logged_in"):
            exc = DouyinRemoteError(str(resolved.get("message") or "登录态校验失败"),
                                    auth_failed=bool(resolved.get("auth_failed", True)))
            self._mark_account_error_detached(account_id, str(exc), auth_failed=exc.auth_failed)
            self._write_log("account", "error", f"账号校验失败（{display}）：{exc}",
                            owner_user_id=self._owner(user), account_id=account_id)
            raise exc
        identity = {"user_id": str(resolved.get("user_id") or "")}

        with self.database.session() as session:
            account = self._get_account(session, account_id, user)
            account.status = "active"
            account.last_error = ""
            account.last_verified_at = datetime.now()
            # 校验时解析到的资料（主站链路一次拿全）优先回填，其次再走接口补
            account.nickname = account.nickname or str(resolved.get("nickname") or "")
            account.douyin_id = account.douyin_id or str(resolved.get("douyin_id") or "")
            account.avatar = account.avatar or str(resolved.get("avatar") or "")
            # 备注与昵称都为空时补拉资料（昵称/抖音号/头像），让显示名不再落到 UID
            if not account.remark and (not account.nickname or not account.douyin_id or not account.avatar):
                profile = self._fetch_self_profile(cookies, proxy_cfg)
                account.nickname = account.nickname or profile.get("nickname") or ""
                account.douyin_id = account.douyin_id or profile.get("douyin_id") or ""
                account.avatar = account.avatar or profile.get("avatar") or ""
            # 占位身份（无效导入的账号）在首次校验通过后回填真实 user_id
            if str(account.remote_user_id or "").startswith("pending_") and identity["user_id"]:
                exists = session.scalar(
                    select(DouyinAccountRow.id).where(
                        DouyinAccountRow.remote_user_id == identity["user_id"],
                        DouyinAccountRow.id != account.id,
                    )
                )
                if exists:
                    raise DouyinValidationError("该抖音账号已存在（其他记录已绑定此身份），请删除无效占位记录后再试")
                account.remote_user_id = identity["user_id"]
                account.enabled = True
            session.flush()
            result = self._account_dict(account)
        self._write_log("account", "success", f"账号校验通过（{result['display_name']}，user_id={identity['user_id'] or '-'}）",
                        owner_user_id=self._owner(user), account_id=account_id)
        return result

    # ------------------------------------------------------------ cookie 保活
    def refresh_account_cookies(self, account_id: int, user: dict) -> dict:
        """手动保活：带旧 cookie 访问抖音，把服务端重新下发的 cookie 落库。

        绝大多数「cookie 失效」只是 ttwid / msToken 这类短命字段陈旧（见
        ``douyin_cookie_refresh`` 模块注释），刷新一次即可续上；只有 sessionid
        被服务端作废时才需要重新扫码。
        """
        with self.database.session() as session:
            account = self._get_account(session, account_id, user)
            return self._refresh_account_cookies(session, account, trigger="manual")

    def _confirm_login_after_refresh(self, cookies, *, account_id: int, proxy, identity: dict) -> dict:
        """保活后的登录态确认，返回 ``{logged_in, message, auth_failed}``。

        - creator：走 IM ``user_token`` 接口预检；
        - 主站：保活访问期间页面自身已确认登录（``identity`` 非空）即通过；
          未抓到身份时退回浏览器验证（``deep=False``，只判定登录态）。
        """
        if active_site().key == "creator":
            try:
                fetch_session_identity(cookies, proxy)
                return {"logged_in": True, "message": "", "auth_failed": False}
            except DouyinRemoteError as exc:
                return {"logged_in": False, "message": str(exc), "auth_failed": bool(exc.auth_failed)}
        if identity.get("logged_in"):
            return {"logged_in": True, "message": "", "auth_failed": False}
        resolved = resolve_identity(cookies, account_id=account_id, proxy=proxy, deep=False)
        if resolved.get("logged_in"):
            return {"logged_in": True, "message": "", "auth_failed": False}
        return {
            "logged_in": False,
            "message": str(resolved.get("message") or "登录态校验失败"),
            "auth_failed": bool(resolved.get("auth_failed", True)),
        }

    def _refresh_account_cookies(self, session, account: DouyinAccountRow, *, trigger: str) -> dict:
        display = account.remark or account.nickname or account.remote_user_id
        owner = account.owner_user_id
        cookies = self._cookies_for(account)
        proxy_cfg = self._proxy_row_config(session, account.proxy_id)
        try:
            refreshed = refresh_cookies(cookies, account_id=account.id, proxy=proxy_cfg)
        except CookieRefreshError as exc:
            # 浏览器/环境问题：不动登录态（避免误标「需重登」），仅记日志
            self._write_log("account", "warning", f"[{display}] cookie 保活未执行：{exc}",
                            owner_user_id=owner, account_id=account.id)
            return {"ok": False, "auth_failed": False, "message": f"保活未执行：{exc}"}
        new_cookies = refreshed["cookies"]
        updated = sum(
            1 for old, new in zip(cookies, new_cookies)
            if isinstance(old, dict) and isinstance(new, dict) and old.get("value") != new.get("value")
        )
        now = datetime.now()
        account.cookies = self._encrypt(json.dumps(new_cookies, ensure_ascii=False))
        account.cookies_refreshed_at = now
        account.last_verified_at = now
        confirm = self._confirm_login_after_refresh(
            new_cookies, account_id=account.id, proxy=proxy_cfg,
            identity=refreshed.get("identity") or {},
        )
        if not confirm["logged_in"]:
            if confirm["auth_failed"]:
                account.status = "relogin"
            account.last_error = str(confirm["message"])[:1000]
            session.flush()
            self._write_log(
                "account", "error",
                f"[{display}] cookie 保活后校验仍未通过（{'登录态已作废，需重新扫码' if confirm['auth_failed'] else '网络/接口异常'}）：{confirm['message']}",
                owner_user_id=owner, account_id=account.id,
            )
            return {
                "ok": False,
                "auth_failed": bool(confirm["auth_failed"]),
                "total": len(new_cookies),
                "updated": updated,
                "message": str(confirm["message"]),
            }
        account.status = "active"
        account.last_error = ""
        # P1：保活走的是持久 profile，同步完成即视为 profile 已刷新
        if self._profile_on(account):
            account.profile_updated_at = now
        session.flush()
        self._write_log(
            "account", "success",
            f"[{display}] cookie 保活成功：回收 {len(new_cookies)} 条，更新 {updated} 条"
            + ("（手动）" if trigger == "manual" else ""),
            owner_user_id=owner, account_id=account.id,
        )
        return {
            "ok": True,
            "auth_failed": False,
            "total": len(new_cookies),
            "updated": updated,
            "message": f"cookie 已刷新（共 {len(new_cookies)} 条，更新 {updated} 条）",
        }

    def _maybe_refresh_cookies(self) -> None:
        """定时保活：按间隔（默认 6 小时/账号）刷新 cookie，先顺延锚点再提交防重入。"""
        now_ts = time.time()
        if now_ts - self._last_cookie_scan < COOKIE_REFRESH_SCAN_INTERVAL_SECONDS:
            return
        self._last_cookie_scan = now_ts
        now = datetime.now()
        interval = timedelta(hours=COOKIE_REFRESH_INTERVAL_HOURS)
        try:
            with self.database.session() as session:
                rows = list(session.scalars(
                    select(DouyinAccountRow).where(
                        DouyinAccountRow.enabled.is_(True),
                        DouyinAccountRow.status == "active",
                    )
                ))
                due = []
                for row in rows:
                    anchor = row.cookies_refreshed_at or row.last_verified_at or row.created_at
                    if anchor is None or (now - anchor) >= interval:
                        # 占位锚点（DB 即防重入闸门），执行完按实际结果改写
                        row.cookies_refreshed_at = now
                        due.append(row.id)
        except Exception:
            logger.exception("cookie 保活扫描失败")
            return
        for account_id in due:
            self._cookie_executor.submit(self._refresh_cookies_job, account_id)

    def _refresh_cookies_job(self, account_id: int) -> None:
        interval = timedelta(hours=COOKIE_REFRESH_INTERVAL_HOURS)
        lock = self._account_lock(account_id)
        if not lock.acquire(blocking=False):
            # 该账号正在续火：10 分钟后再试（别白等一个完整间隔）
            with self.database.session() as session:
                row = session.get(DouyinAccountRow, account_id)
                if row is not None:
                    row.cookies_refreshed_at = datetime.now() - interval + timedelta(minutes=10)
            return
        try:
            with self.database.session() as session:
                account = session.get(DouyinAccountRow, account_id)
                if account is None or not account.enabled:
                    return
                result = self._refresh_account_cookies(session, account, trigger="scheduler")
                if not result.get("ok") and not result.get("auth_failed"):
                    # 环境/网络问题：15 分钟后重试，避免等满 6 小时
                    account.cookies_refreshed_at = datetime.now() - interval + timedelta(minutes=15)
        except Exception:
            logger.exception("cookie 保活执行失败（账号 %s）", account_id)
            self._write_log("account", "error", f"cookie 保活异常（账号 {account_id}）",
                            account_id=account_id)
        finally:
            lock.release()

    def list_friends(self, account_id: int, user: dict) -> dict:
        """拉取账号的好友（creator 私信会话列表），并把结果并入发送会话缓存。

        只读操作：不发送任何消息；昵称取不到的条目标记 sendable=False。
        """
        with self.database.session() as session:
            account = self._get_account(session, account_id, user)
            cookies = self._cookies_for(account)
            display_name = account.remark or account.nickname or account.remote_user_id
            cache = list(account.protocol_targets_cache or [])
            proxy_cfg = self._proxy_row_config(session, account.proxy_id)

        try:
            result = run_protocol_list_friends(
                cookies,
                data_dir=Path(config.DOUYIN_DATA_DIR),
                protocol_targets_cache=cache,
                proxy=proxy_cfg,
                user_id=self._protocol_user_id(account),
            )
        except DouyinRemoteError as exc:
            with self.database.session() as session:
                row = session.get(DouyinAccountRow, account_id)
                if row is not None and exc.auth_failed:
                    row.status = "relogin"
                    row.last_error = str(exc)
            self._write_log("friends", "error", f"[{display_name}] 拉取好友失败：{exc}",
                            owner_user_id=self._owner(user), account_id=account_id)
            raise

        friends = []
        for item in result.get("friends") or []:
            if not isinstance(item, dict):
                continue
            friends.append(self._friend_dict(item))
        names = [item["nickname"] for item in friends if item["sendable"]]
        unnamed = len(friends) - len(names)
        preview = "、".join(names[:10]) + ("…" if len(names) > 10 else "")
        if not preview:
            preview = "未取到任何昵称"

        with self.database.session() as session:
            row = session.get(DouyinAccountRow, account_id)
            if row is not None:
                new_cache = result.get("protocol_targets_cache")
                if isinstance(new_cache, list):
                    row.protocol_targets_cache = new_cache
                row.last_verified_at = datetime.now()
                if row.status == "relogin":
                    row.status = "active"
                    row.last_error = ""
        self._write_log(
            "friends", "success",
            f"[{display_name}] 拉取好友 {len(friends)} 个（可发送 {len(names)} 个，未取到昵称 {unnamed} 个）：{preview}",
            owner_user_id=self._owner(user), account_id=account_id,
        )
        return {
            "account_id": account_id,
            "display_name": display_name,
            "count": len(friends),
            "sendable_count": len(names),
            "unnamed_count": unnamed,
            "names": names,
            "friends": friends,
            "cached": False,
            "fetched_at": datetime.now(),
        }

    def list_friends_cached(self, account_id: int, user: dict) -> dict:
        """读取上次拉取后保留的好友缓存（只访问本地库，不请求抖音）。"""
        with self.database.session() as session:
            account = self._get_account(session, account_id, user)
            display_name = account.remark or account.nickname or account.remote_user_id
            cache = [item for item in (account.protocol_targets_cache or []) if isinstance(item, dict)]
            verified_at = account.last_verified_at

        friends = []
        for item in cache:
            friends.append(self._friend_dict(item))
        names = [item["nickname"] for item in friends if item["sendable"]]
        return {
            "account_id": account_id,
            "display_name": display_name,
            "count": len(friends),
            "sendable_count": len(names),
            "unnamed_count": len(friends) - len(names),
            "names": names,
            "friends": friends,
            "cached": True,
            "fetched_at": verified_at,
        }

    def list_friends_and_sparks(self, account_id: int, user: dict) -> dict:
        """一次协议采样同时拉好友与火花（后台任务用）：好友缓存 + 火花快照一次落库。

        拉好友与读火花的数据源相同（都是会话列表），合并后只需一次 SDK 初始化，
        首次自动拉取从两次 ~22s 降到一次；手动「拉取好友」也会顺带拿到火花。
        只读操作：不发送任何消息。
        """
        with self.database.session() as session:
            account = self._get_account(session, account_id, user)
            cookies = self._cookies_for(account)
            display_name = account.remark or account.nickname or account.remote_user_id
            cache = list(account.protocol_targets_cache or [])
            proxy_cfg = self._proxy_row_config(session, account.proxy_id)

        try:
            result = run_protocol_friends_and_sparks(
                cookies,
                data_dir=Path(config.DOUYIN_DATA_DIR),
                protocol_targets_cache=cache,
                proxy=proxy_cfg,
                user_id=self._protocol_user_id(account),
            )
        except DouyinRemoteError as exc:
            with self.database.session() as session:
                row = session.get(DouyinAccountRow, account_id)
                if row is not None and exc.auth_failed:
                    row.status = "relogin"
                    row.last_error = str(exc)
            self._write_log("friends", "error", f"[{display_name}] 拉取好友与火花失败：{exc}",
                            owner_user_id=self._owner(user), account_id=account_id)
            raise

        friends = []
        for item in result.get("friends") or []:
            if not isinstance(item, dict):
                continue
            friends.append(self._friend_dict(item))
        names = [item["nickname"] for item in friends if item["sendable"]]
        unnamed = len(friends) - len(names)
        preview = "、".join(names[:10]) + ("…" if len(names) > 10 else "")
        if not preview:
            preview = "未取到任何昵称"

        sparks = [self._spark_dict(item) for item in (result.get("sparks") or []) if isinstance(item, dict)]
        pending = sum(1 for item in sparks if item["pending_today"])
        fetched_at = datetime.now()
        with self.database.session() as session:
            row = session.get(DouyinAccountRow, account_id)
            if row is not None:
                new_cache = result.get("protocol_targets_cache")
                if isinstance(new_cache, list):
                    row.protocol_targets_cache = new_cache
                row.spark_snapshot = {
                    "sparks": sparks,
                    "pending_today": pending,
                    "count": len(sparks),
                    "fetched_at": fetched_at.isoformat(timespec="seconds"),
                }
                row.spark_checked_at = fetched_at
                row.last_verified_at = fetched_at
                if row.status == "relogin":
                    row.status = "active"
                    row.last_error = ""
        self._write_log(
            "friends", "success",
            f"[{display_name}] 拉取好友 {len(friends)} 个（可发送 {len(names)} 个，未取到昵称 {unnamed} 个）：{preview}"
            f"；火花 {len(sparks)} 个有记录，今日待续 {pending} 个",
            owner_user_id=self._owner(user), account_id=account_id,
        )
        return {
            "account_id": account_id,
            "display_name": display_name,
            "count": len(friends),
            "sendable_count": len(names),
            "unnamed_count": unnamed,
            "names": names,
            "friends": friends,
            "cached": False,
            "fetched_at": fetched_at,
            # 火花载荷与 get_spark_states 同构，前端直接 buildSparkMap，无需再请求
            "spark": {
                "account_id": account_id,
                "display_name": display_name,
                "count": len(sparks),
                "pending_today": pending,
                "sparks": sparks,
                "fetched_at": fetched_at,
            },
        }

    # -------------------------------------------------------------- 平台火花状态

    @staticmethod
    def _spark_dict(item: dict) -> dict:
        """协议 spark 条目 → 前端结构（snake_case）。"""
        spark = item.get("spark") or {}
        state = int(spark.get("state") or 0)
        active = state in (1, 2, 3)
        days = int(spark.get("days") or 0)
        return {
            "nickname": str(item.get("nickname") or "").strip(),
            "unique_id": str(item.get("uniqueId") or "").strip(),
            "avatar": str(item.get("avatar") or "").strip(),
            "sec_uid": str(item.get("secUid") or "").strip(),
            "peer_user_id": str(item.get("peerUserId") or "").strip(),
            "conversation_id": str(item.get("conversationId") or "").strip(),
            # state: 0=仅历史记录 / 1=今日已续 / 2=今日未续 / 3=濒危
            "state": state,
            "active": active,
            # state=0 只有历史记录，连续天数不具"当前"含义 → 归零（与 mjs 层一致）
            "days": days if active else 0,
            "real_days": spark.get("realDays"),
            "level": str(spark.get("level") or ""),
            "text": str(spark.get("text") or ""),
            "recover_ddl": spark.get("recoverDdl"),
            "can_recover_days": spark.get("canRecoverDays"),
            "expire_time": spark.get("expireTime"),
            # 今日待续：state 2（未续）或 3（濒危）
            "pending_today": state in (2, 3),
        }

    def get_spark_states(self, account_id: int, user: dict) -> dict:
        """读取账号的平台真实火花状态（只读协议，不发消息），并落库快照。

        返回 {account_id, display_name, count, pending_today, sparks[], fetched_at}
        """
        with self.database.session() as session:
            account = self._get_account(session, account_id, user)
            cookies = self._cookies_for(account)
            display_name = account.remark or account.nickname or account.remote_user_id
            cache = list(account.protocol_targets_cache or [])
            proxy_cfg = self._proxy_row_config(session, account.proxy_id)

        try:
            result = run_protocol_spark_state(
                cookies,
                data_dir=Path(config.DOUYIN_DATA_DIR),
                protocol_targets_cache=cache,
                proxy=proxy_cfg,
                user_id=self._protocol_user_id(account),
            )
        except DouyinRemoteError as exc:
            with self.database.session() as session:
                row = session.get(DouyinAccountRow, account_id)
                if row is not None and exc.auth_failed:
                    row.status = "relogin"
                    row.last_error = str(exc)
            self._write_log("friends", "error", f"[{display_name}] 读取火花状态失败：{exc}",
                            owner_user_id=self._owner(user), account_id=account_id)
            raise

        sparks = [self._spark_dict(item) for item in (result.get("sparks") or []) if isinstance(item, dict)]
        pending = sum(1 for item in sparks if item["pending_today"])
        fetched_at = datetime.now()
        snapshot = {
            "sparks": sparks,
            "pending_today": pending,
            "count": len(sparks),
            "fetched_at": fetched_at.isoformat(timespec="seconds"),
        }
        with self.database.session() as session:
            row = session.get(DouyinAccountRow, account_id)
            if row is not None:
                row.spark_snapshot = snapshot
                row.spark_checked_at = fetched_at
                if row.status == "relogin":
                    row.status = "active"
                    row.last_error = ""
        self._write_log(
            "friends", "success",
            f"[{display_name}] 读取火花：{len(sparks)} 个会话有记录，今日待续 {pending} 个",
            owner_user_id=self._owner(user), account_id=account_id,
        )
        return {
            "account_id": account_id,
            "display_name": display_name,
            "count": len(sparks),
            "pending_today": pending,
            "sparks": sparks,
            "fetched_at": fetched_at,
        }

    def spark_states_cached(self, account_id: int, user: dict) -> dict:
        """读取本地缓存的火花快照（不请求抖音）。"""
        with self.database.session() as session:
            account = self._get_account(session, account_id, user)
            display_name = account.remark or account.nickname or account.remote_user_id
            snapshot = dict(account.spark_snapshot or {})
            checked_at = account.spark_checked_at

        sparks = [item for item in (snapshot.get("sparks") or []) if isinstance(item, dict)]
        return {
            "account_id": account_id,
            "display_name": display_name,
            "count": len(sparks),
            "pending_today": int(snapshot.get("pending_today") or 0),
            "sparks": sparks,
            "fetched_at": checked_at,
            "cached": True,
        }

    @staticmethod
    def _friend_dict(item: dict) -> dict:
        nickname = str(item.get("nickname") or "").strip()
        return {
            "nickname": nickname,
            "unique_id": str(item.get("uniqueId") or "").strip(),
            "avatar": str(item.get("avatar") or "").strip(),
            "sec_uid": str(item.get("secUid") or "").strip(),
            "peer_user_id": str(item.get("peerUserId") or "").strip(),
            "conversation_id": str(item.get("conversationId") or "").strip(),
            "conversation_short_id": str(item.get("conversationShortId") or "").strip(),
            # 协议按昵称/身份匹配目标，无昵称的条目不能作为发送目标
            "sendable": bool(nickname),
        }

    # -------------------------------------------------------------- 后台拉好友

    FRIENDS_JOB_TTL_SECONDS = 1800

    def start_friends_refresh(self, account_id: int, user: dict) -> dict:
        """后台拉取好友：立即返回任务状态，结果经 get_friends_job 轮询获取。

        前端点「重新拉取」后可以关掉弹窗继续干别的，拉取在服务端跑完并落库。
        """
        with self.database.session() as session:
            self._get_account(session, account_id, user)  # 权限校验
        with self._friends_jobs_guard:
            self._prune_friends_jobs()
            job = self._friends_jobs.get(account_id)
            if job and job["status"] == "running":
                return self._friends_job_dict(job)
            job = {
                "account_id": account_id,
                "status": "running",
                "started_at": datetime.now(),
                "finished_at": None,
                "error": "",
                "result": None,
            }
            self._friends_jobs[account_id] = job
        user_snapshot = {"id": user["id"], "role": user.get("role", "")}
        self._friends_executor.submit(self._run_friends_job, account_id, user_snapshot)
        return self._friends_job_dict(job)

    def _run_friends_job(self, account_id: int, user: dict):
        with self._friends_jobs_guard:
            job = self._friends_jobs.get(account_id)
        if job is None:
            return
        try:
            # 好友与火花一次采样完成（同一数据源，省一次 ~22s 的 SDK 初始化）
            job["result"] = self.list_friends_and_sparks(account_id, user)
            job["status"] = "success"
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(exc)
        job["finished_at"] = datetime.now()

    def get_friends_job(self, account_id: int, user: dict) -> dict:
        with self.database.session() as session:
            self._get_account(session, account_id, user)
        with self._friends_jobs_guard:
            self._prune_friends_jobs()
            job = self._friends_jobs.get(account_id)
        if job is None:
            return {
                "account_id": account_id,
                "status": "idle",
                "error": "",
                "result": None,
                "started_at": None,
                "finished_at": None,
            }
        return self._friends_job_dict(job)

    def _friends_job_dict(self, job: dict) -> dict:
        return {
            "account_id": job["account_id"],
            "status": job["status"],
            "error": job["error"],
            "result": job["result"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
        }

    def _prune_friends_jobs(self):
        cutoff = datetime.now() - timedelta(seconds=self.FRIENDS_JOB_TTL_SECONDS)
        stale = [
            account_id for account_id, job in self._friends_jobs.items()
            if job["status"] != "running" and (job["finished_at"] or job["started_at"]) < cutoff
        ]
        for account_id in stale:
            self._friends_jobs.pop(account_id, None)

    def _mark_account_error_detached(self, account_id: int, message: str, *, auth_failed: bool):
        """在独立会话中标记账号错误（调用方会话即将随异常回滚）。"""
        with self.database.session() as session:
            account = session.get(DouyinAccountRow, account_id)
            if account is None:
                return
            if auth_failed:
                account.status = "relogin"
            account.last_error = message[:1000]
            account.last_verified_at = datetime.now()

    # ------------------------------------------------------------------ 代理

    PROTOCOLS = ("http", "https", "socks5")

    def _proxy_row_config(self, session, proxy_id: int | None) -> dict | None:
        """按 id 取代理出站配置（内部调用，不做归属校验）；未启用视为直连。"""
        if not proxy_id:
            return None
        row = session.get(DouyinProxyRow, int(proxy_id))
        if row is None or not row.enabled:
            return None
        return {
            "protocol": row.protocol,
            "host": row.host,
            "port": row.port,
            "username": row.username,
            "password": row.password,
        }

    def _resolve_proxy(self, proxy_id: int | None, user: dict) -> dict | None:
        """带归属校验的代理配置解析（账号绑定/校验/扫码登录用）。"""
        if not proxy_id:
            return None
        with self.database.session() as session:
            row = session.get(DouyinProxyRow, int(proxy_id))
            if row is None:
                raise DouyinNotFound("代理不存在")
            if row.owner_user_id != self._owner(user) and not self._is_admin(user):
                raise DouyinNotFound("代理不存在")
            return self._proxy_row_config(session, row.id)

    @staticmethod
    def _proxy_dict(r: DouyinProxyRow) -> dict:
        return {
            "id": r.id,
            "owner_user_id": r.owner_user_id,
            "name": r.name,
            "protocol": r.protocol,
            "host": r.host,
            "port": r.port,
            "username": r.username,
            "has_password": bool(r.password),
            "enabled": r.enabled,
            "updated_at": r.updated_at,
        }

    def list_proxies(self, user: dict) -> list[dict]:
        with self.database.session() as session:
            rows = list(session.scalars(
                self._scope(select(DouyinProxyRow), DouyinProxyRow, user).order_by(DouyinProxyRow.id)
            ))
            bound = {}
            for account in session.scalars(select(DouyinAccountRow)):
                if account.proxy_id:
                    bound.setdefault(account.proxy_id, 0)
                    bound[account.proxy_id] += 1
            results = []
            for row in rows:
                item = self._proxy_dict(row)
                item["accounts_bound"] = bound.get(row.id, 0)
                results.append(item)
            return results

    def create_proxy(self, payload: dict, user: dict) -> dict:
        with self.database.session() as session:
            row = DouyinProxyRow(
                owner_user_id=self._owner(user),
                name=(payload.get("name") or "").strip() or "未命名代理",
                protocol=payload.get("protocol") or "http",
                host=(payload.get("host") or "").strip(),
                port=int(payload.get("port") or 0),
                username=(payload.get("username") or "").strip(),
                password=(payload.get("password") or "").strip(),
                enabled=bool(payload.get("enabled", True)),
            )
            if not row.host or not row.port:
                raise DouyinValidationError("代理主机与端口不能为空")
            if row.protocol not in self.PROTOCOLS:
                raise DouyinValidationError("协议仅支持 http / https / socks5")
            session.add(row)
            session.flush()
            result = self._proxy_dict(row)
        self._write_log("account", "info", f"添加出口代理：{result['name']}（{result['protocol']}://{result['host']}:{result['port']}）",
                        owner_user_id=self._owner(user))
        return result

    def update_proxy(self, proxy_id: int, payload: dict, user: dict) -> dict:
        with self.database.session() as session:
            row = self._scoped_proxy(session, proxy_id, user)
            row.name = (payload.get("name") or row.name or "未命名代理").strip()
            row.protocol = payload.get("protocol") or row.protocol
            row.host = (payload.get("host") or row.host or "").strip()
            row.port = int(payload.get("port") or row.port or 0)
            # 密码留空 = 保持不变（避免回显丢密码）；显式传 "-" 表示清空
            password = payload.get("password")
            if password == "-":
                row.password = ""
            elif password:
                row.password = str(password).strip()
            if "enabled" in payload:
                row.enabled = bool(payload.get("enabled"))
            if not row.host or not row.port:
                raise DouyinValidationError("代理主机与端口不能为空")
            row.updated_at = datetime.now()
            session.flush()
            result = self._proxy_dict(row)
        return result

    def delete_proxy(self, proxy_id: int, user: dict) -> dict:
        with self.database.session() as session:
            row = self._scoped_proxy(session, proxy_id, user)
            name = row.name
            # 解绑引用该代理的账号（避免悬空绑定）
            for account in session.scalars(
                select(DouyinAccountRow).where(DouyinAccountRow.proxy_id == proxy_id)
            ):
                account.proxy_id = None
            session.delete(row)
        self._write_log("account", "warning", f"删除出口代理：{name}（绑定账号已改为直连）",
                        owner_user_id=self._owner(user))
        return {"deleted": 1}

    def _scoped_proxy(self, session, proxy_id: int, user) -> DouyinProxyRow:
        row = session.get(DouyinProxyRow, int(proxy_id))
        if row is None:
            raise DouyinNotFound("代理不存在")
        if row.owner_user_id != self._owner(user) and not self._is_admin(user):
            raise DouyinNotFound("代理不存在")
        return row

    # ------------------------------------------------------------------ 总览

    def get_overview(self, user: dict) -> dict:
        with self.database.session() as session:
            accounts = list(session.scalars(self._scope(select(DouyinAccountRow), DouyinAccountRow, user)))
            tasks = list(session.scalars(self._scope(select(DouyinTaskRow), DouyinTaskRow, user)))
            today = datetime.combine(date.today(), datetime.min.time())
            runs = list(
                session.scalars(
                    self._scope(select(DouyinRunRow), DouyinRunRow, user)
                    .where(DouyinRunRow.started_at >= today)
                    .order_by(DouyinRunRow.started_at.desc())
                )
            )

            return {
                "stats": {
                    "accounts": len(accounts),
                    "enabled_tasks": sum(1 for row in tasks if row.enabled),
                    "today_runs": len(runs),
                    "today_sent": sum(int(r.response_summary.get("sent_ok") or 0) for r in runs),
                    "today_failed": sum(int(r.response_summary.get("sent_fail") or 0) for r in runs),
                    "today_skipped": sum(int(r.response_summary.get("skipped") or 0) for r in runs),
                    **self._today_pending_stats(tasks, runs),
                    **self._platform_spark_stats(accounts),
                },
            }

    @staticmethod
    def _platform_spark_stats(accounts) -> dict:
        """平台口径火花统计（读快照，零 I/O）。

        与本地口径（_today_pending_stats）并列输出，前端可切换展示：
          - spark_pending        平台标记「今日待续」的会话数（state 2/3）
          - spark_active         有进行中火花的会话数（state 1/2/3）
          - spark_total_days     各会话连续天数之和（近似「总火花天数」）
          - spark_checked_at     快照读取时刻（None = 从未读过平台火花）
          - spark_accounts       有快照的账号数
        """
        pending = active = total_days = 0
        checked_at = None
        snapshotted = 0
        for row in accounts:
            snapshot = row.spark_snapshot or {}
            sparks = [item for item in (snapshot.get("sparks") or []) if isinstance(item, dict)]
            if not sparks and not row.spark_checked_at:
                continue
            snapshotted += 1
            for item in sparks:
                if item.get("pending_today"):
                    pending += 1
                if item.get("active"):
                    active += 1
                total_days += int(item.get("days") or 0)
            row_checked = row.spark_checked_at
            if row_checked is not None and (checked_at is None or row_checked > checked_at):
                checked_at = row_checked
        return {
            "spark_pending": pending,
            "spark_active": active,
            "spark_total_days": total_days,
            "spark_checked_at": checked_at,
            "spark_accounts": snapshotted,
        }

    @staticmethod
    def _today_pending_stats(tasks, runs) -> dict:
        """火花预检（本地口径）：启用任务的目标里今天还没成功续过几个。

        本地口径：只数「今天本地成功发过几个目标」。
        平台口径见 _platform_spark_stats（读 spark_snapshot 快照）。
        """
        sent_today: set[str] = set()
        for row in runs:
            for entry in (row.response_summary or {}).get("sent", []) or []:
                if not isinstance(entry, dict) or entry.get("dry_run") or not entry.get("success"):
                    continue
                for key in (entry.get("target"), entry.get("matched")):
                    if key:
                        sent_today.add(str(key))
        total = pending = 0
        for task in tasks:
            if not task.enabled:
                continue
            for spec in normalize_target_specs(task.targets or []):
                total += 1
                names = [spec["primary"], *spec["aliases"]]
                if not any(name in sent_today for name in names):
                    pending += 1
        return {"today_targets": total, "today_pending": pending}

    # ------------------------------------------------------------------ 任务 CRUD

    def list_tasks(self, user: dict) -> list[dict]:
        with self.database.session() as session:
            # 列表按任务创建顺序（旧在上、新在下），编辑/执行都不改变位置：
            # 按 updated_at 排会每改一次任务就把它顶到最前面，用户明确不要。
            stmt = self._scope(select(DouyinTaskRow), DouyinTaskRow, user).order_by(
                DouyinTaskRow.created_at.asc(), DouyinTaskRow.id.asc()
            )
            rows = list(session.scalars(stmt))
            account_names = {
                row.id: (row.remark or row.nickname or row.remote_user_id)
                for row in session.scalars(select(DouyinAccountRow))
            }
            results = []
            for row in rows:
                item = self._task_dict(row)
                item["account_name"] = account_names.get(row.account_id, "未知账号")
                results.append(item)
            return results

    @staticmethod
    def _normalize_targets(values) -> list[str]:
        """去空白、去空项、保序去重。"""
        normalized: list[str] = []
        seen: set[str] = set()
        for item in values or []:
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return normalized[:200]

    @staticmethod
    def _normalize_delay(payload: dict) -> dict:
        """随机等待区间归一：上限 600s；保证 min ≤ max。

        区分「未设置」与「显式 0」：``0~20`` 表示 0~20 秒随机（下限 0 是合法值，
        不能像早期实现那样把 0 当未设置而用上限兜底，否则 0~20 会被改成 20~20）。
        min/max 全为 0 或全未设置 → 不等待（NULL）。
        """
        def _to_int(value):
            if value is None or value == "":
                return None
            try:
                number = int(value)
            except (TypeError, ValueError):
                return None
            return 0 if number <= 0 else min(number, 600)

        low = _to_int(payload.get("delay_min_seconds"))
        high = _to_int(payload.get("delay_max_seconds"))
        if low is None and high is None:
            return {"delay_min_seconds": None, "delay_max_seconds": None}
        low = 0 if low is None else low
        high = low if high is None else high
        if low <= 0 and high <= 0:
            return {"delay_min_seconds": None, "delay_max_seconds": None}
        if low > high:
            low, high = high, low
        return {"delay_min_seconds": low, "delay_max_seconds": high}

    def _sent_targets_today(self, session, account_id: int, *, exclude_run_id: int | None = None) -> dict[str, str]:
        """今天已经成功续过的好友 → {目标名: 发送时间文本}（本地火花预检，零成本）。

        取当天所有成功运行里的 sent 条目（含 dry_run 之外的），用于「今日已续则跳过」。
        """
        today = datetime.combine(date.today(), datetime.min.time())
        rows = list(session.scalars(
            select(DouyinRunRow).where(
                DouyinRunRow.account_id == account_id,
                DouyinRunRow.started_at >= today,
                DouyinRunRow.status.in_(("success", "partial")),
            )
        ))
        sent: dict[str, str] = {}
        for row in rows:
            if exclude_run_id is not None and row.id == exclude_run_id:
                continue
            for entry in (row.response_summary or {}).get("sent", []) or []:
                if not isinstance(entry, dict) or entry.get("dry_run") or not entry.get("success"):
                    continue
                target = str(entry.get("target") or "").strip()
                matched = str(entry.get("matched") or "").strip()
                stamp = (row.finished_at or row.started_at or datetime.now()).strftime("%H:%M")
                for key in (target, matched):
                    if key and key not in sent:
                        sent[key] = stamp
        return sent

    @staticmethod
    def _normalize_schedule_times(values) -> list[str]:
        """统一为 HH:MM:SS，排序去重；格式非法抛校验异常。"""
        normalized: set[str] = set()
        for item in values or []:
            text = str(item).strip()
            if not text:
                continue
            for fmt in ("%H:%M:%S", "%H:%M"):
                try:
                    normalized.add(datetime.strptime(text, fmt).strftime("%H:%M:%S"))
                    break
                except ValueError:
                    continue
            else:
                raise DouyinValidationError(f"签到时间格式不正确：{text}（应为 HH:MM 或 HH:MM:SS）")
        return sorted(normalized)

    def create_task(self, payload: dict, user: dict) -> dict:
        payload = dict(payload)
        payload["targets"] = self._normalize_targets(payload.get("targets"))
        payload["schedule_times"] = self._normalize_schedule_times(payload.get("schedule_times"))
        payload.update(self._normalize_date_plan(payload))
        payload.update(self._normalize_delay(payload))
        if not payload["targets"]:
            raise DouyinValidationError("请至少填写一个续火目标")
        with self.database.session() as session:
            self._get_account(session, payload["account_id"], user)
            row = DouyinTaskRow(owner_user_id=self._owner(user), **payload)
            session.add(row)
            session.flush()
            result = self._task_dict(row)
        self._write_log("task", "info", f"创建续火任务：{row.name}（{len(payload['targets'])} 个目标）",
                        owner_user_id=self._owner(user))
        return result

    def update_task(self, task_id: int, payload: dict, user: dict) -> dict:
        payload = dict(payload)
        if "targets" in payload:
            payload["targets"] = self._normalize_targets(payload.get("targets"))
        if "schedule_times" in payload:
            payload["schedule_times"] = self._normalize_schedule_times(payload.get("schedule_times"))
        if "date_mode" in payload or "run_dates" in payload or "skip_dates" in payload:
            payload.update(self._normalize_date_plan(payload))
        if "delay_min_seconds" in payload or "delay_max_seconds" in payload:
            payload.update(self._normalize_delay(payload))
        with self.database.session() as session:
            row = self._get_task(session, task_id, user)
            self._get_account(session, payload["account_id"], user)
            for key, value in payload.items():
                setattr(row, key, value)
            row.updated_at = datetime.now()
            session.flush()
            return self._task_dict(row)

    def delete_task(self, task_id: int, user: dict):
        with self.database.session() as session:
            session.delete(self._get_task(session, task_id, user))

    def batch_set_task_state(self, task_ids: list[int], enabled: bool, user: dict) -> dict:
        ids = list(dict.fromkeys(int(value) for value in task_ids))
        with self.database.session() as session:
            stmt = self._scope(select(DouyinTaskRow).where(DouyinTaskRow.id.in_(ids)), DouyinTaskRow, user)
            rows = list(session.scalars(stmt))
            if len(rows) != len(ids):
                raise DouyinValidationError("批量操作中包含不存在或无权访问的任务")
            now = datetime.now()
            for row in rows:
                row.enabled = enabled
                row.updated_at = now
            return {"updated": len(rows), "ids": ids, "enabled": enabled}

    def batch_delete_tasks(self, task_ids: list[int], user: dict) -> dict:
        ids = list(dict.fromkeys(int(value) for value in task_ids))
        with self.database.session() as session:
            stmt = self._scope(select(DouyinTaskRow).where(DouyinTaskRow.id.in_(ids)), DouyinTaskRow, user)
            rows = list(session.scalars(stmt))
            if len(rows) != len(ids):
                raise DouyinValidationError("批量删除中包含不存在或无权访问的任务")
            for row in rows:
                session.delete(row)
            return {"deleted": len(rows), "ids": ids}

    # ------------------------------------------------------------------ 运行记录

    def list_runs(self, user: dict, limit: int = 100, status: str = "", account_id: int | None = None) -> list[dict]:
        with self.database.session() as session:
            stmt = self._scope(select(DouyinRunRow), DouyinRunRow, user)
            if status:
                stmt = stmt.where(DouyinRunRow.status == status)
            if account_id:
                stmt = stmt.where(DouyinRunRow.account_id == account_id)
            stmt = stmt.order_by(DouyinRunRow.started_at.desc()).limit(min(max(limit, 1), 500))
            rows = list(session.scalars(stmt))
            account_names = {
                row.id: (row.remark or row.nickname or row.remote_user_id)
                for row in session.scalars(select(DouyinAccountRow))
            }
            task_names = {row.id: row.name for row in session.scalars(select(DouyinTaskRow))}
            return [self._run_dict(row, task_names, account_names) for row in rows]

    # 总览口径：抖音运行的 success/partial 均视为「成功续火」，失败单列，
    # skipped/running 归入「跳过或待确认」。与小签到/班级魔方的三分法保持一致。
    DASHBOARD_SUCCESS_STATUSES = ("success", "partial")
    DASHBOARD_FAILED_STATUSES = ("failed",)

    def dashboard_summary(self, start_at: datetime, end_at: datetime) -> dict:
        success_statuses = self.DASHBOARD_SUCCESS_STATUSES
        failed_statuses = self.DASHBOARD_FAILED_STATUSES
        with self.database.session() as session:
            accounts = list(session.scalars(select(DouyinAccountRow)))
            tasks = list(session.scalars(select(DouyinTaskRow)))
            base = (
                DouyinRunRow.started_at >= start_at,
                DouyinRunRow.started_at < end_at,
            )
            counts = session.execute(select(
                func.count(DouyinRunRow.id),
                func.sum(case((DouyinRunRow.status.in_(success_statuses), 1), else_=0)),
                func.sum(case((DouyinRunRow.status.in_(failed_statuses), 1), else_=0)),
            ).where(*base)).one()
            recent_rows = session.scalars(
                select(DouyinRunRow).where(*base)
                .order_by(DouyinRunRow.started_at.desc(), DouyinRunRow.id.desc())
                .limit(20)
            ).all()
            day = func.date(DouyinRunRow.started_at)
            daily_rows = session.execute(
                select(
                    day.label("day"),
                    func.count(DouyinRunRow.id),
                    func.sum(case((DouyinRunRow.status.in_(success_statuses), 1), else_=0)),
                    func.sum(case((DouyinRunRow.status.in_(failed_statuses), 1), else_=0)),
                )
                .where(*base).group_by(day).order_by(day)
            ).all()
            flow_rows = session.execute(
                select(
                    DouyinRunRow.trigger,
                    DouyinRunRow.status,
                    func.count(DouyinRunRow.id),
                )
                .where(*base)
                .group_by(DouyinRunRow.trigger, DouyinRunRow.status)
            ).all()
            rank_rows = session.execute(
                select(DouyinRunRow.account_id, func.count(DouyinRunRow.id))
                .where(*base, DouyinRunRow.status.in_(success_statuses))
                .group_by(DouyinRunRow.account_id)
                .order_by(desc(func.count(DouyinRunRow.id))).limit(10)
            ).all()
            account_names = {
                row.id: (row.remark or row.nickname or row.remote_user_id)
                for row in accounts
            }
            task_names = {row.id: row.name for row in tasks}
            return {
                "accounts": len(accounts),
                "tasks": len(tasks),
                "enabled_tasks": sum(1 for row in tasks if row.enabled),
                "executions": int(counts[0] or 0),
                "success": int(counts[1] or 0),
                "failed": int(counts[2] or 0),
                # 抖音没有「签到类型」维度，统一归为「抖音续火」
                "types": (
                    [{"mode": "douyin", "value": int(counts[0] or 0)}]
                    if int(counts[0] or 0) else []
                ),
                "recent_runs": [
                    {
                        "id": row.id,
                        "task_id": row.task_id,
                        "task_name": task_names.get(row.task_id, "") or "抖音续火",
                        "account_id": row.account_id,
                        "account_name": account_names.get(row.account_id, "未知账号"),
                        "mode": "douyin",
                        "trigger": row.trigger,
                        "status": row.status,
                        "message": row.message,
                        "started_at": row.started_at,
                        "finished_at": row.finished_at,
                    }
                    for row in recent_rows
                ],
                "ranking": [
                    {
                        "name": account_names.get(int(account_id), "未知账号"),
                        "value": int(value),
                    }
                    for account_id, value in rank_rows
                ],
                "daily": [
                    {
                        "date": _iso_day(value),
                        "executions": int(total or 0),
                        "success": int(success or 0),
                        "failed": int(failed or 0),
                    }
                    for value, total, success, failed in daily_rows
                ],
                "flow": _merge_flow_rows(
                    flow_rows, success_statuses, failed_statuses
                ),
            }

    def get_run(self, run_id: int, user: dict) -> dict:
        """单条运行记录详情（含发送回执截图，列表接口不携带以控制响应体积）。"""
        with self.database.session() as session:
            stmt = self._scope(select(DouyinRunRow).where(DouyinRunRow.id == run_id), DouyinRunRow, user)
            row = session.scalars(stmt).first()
            if row is None:
                raise DouyinNotFound("运行记录不存在")
            account_rows = {a.id: (a.remark or a.nickname or a.remote_user_id)
                            for a in session.scalars(select(DouyinAccountRow))}
            task_names = {t.id: t.name for t in session.scalars(select(DouyinTaskRow))}
            return self._run_dict(row, task_names, account_rows, include_screenshots=True)

    def retry_run_target(self, run_id: int, target: str, user: dict) -> dict:
        """对某次运行里失败的目标单独重发（新增一条 trigger=retry_target 记录，不动原记录）。

        只发这一个好友、沿用当时那条文案；别名与账号配置照旧生效。
        """
        target = str(target or "").strip()
        if not target:
            raise DouyinValidationError("请指定要重试的目标")
        with self.database.session() as session:
            stmt = self._scope(select(DouyinRunRow).where(DouyinRunRow.id == run_id), DouyinRunRow, user)
            run = session.scalars(stmt).first()
            if run is None:
                raise DouyinNotFound("运行记录不存在")
            entries = (run.response_summary or {}).get("sent") or []
            entry = next(
                (item for item in entries
                 if isinstance(item, dict) and str(item.get("target") or "") == target),
                None,
            )
            if entry is None:
                raise DouyinNotFound("该目标不在这条运行记录里")
            if entry.get("success") or entry.get("skipped"):
                raise DouyinValidationError("该目标本次已是成功/跳过状态，无需重试")
            message = str(entry.get("message") or "").strip()
            account = session.get(DouyinAccountRow, run.account_id)
            if account is None or not account.enabled:
                raise DouyinValidationError("账号不存在或已停用")
            if account.status != "active":
                raise DouyinValidationError("账号登录态异常，请先校验或等 cookie 保活后再重试")
            cookies = self._cookies_for(account)
            proxy_cfg = self._proxy_row_config(session, account.proxy_id)
            display_name = account.remark or account.nickname or account.remote_user_id
            owner = run.owner_user_id
            task = session.get(DouyinTaskRow, run.task_id) if run.task_id else None
            aliases_by_target: dict[str, list[str]] = {}
            if task is not None:
                for spec in normalize_target_specs(task.targets or []):
                    names = [spec["primary"], *spec["aliases"]]
                    if target in names:
                        aliases_by_target[target] = [name for name in names if name != target]
            retry_run = DouyinRunRow(
                owner_user_id=owner,
                task_id=run.task_id,
                account_id=run.account_id,
                schedule_key=f"retry:{uuid.uuid4().hex}",
                trigger="retry_target",
                status="running",
            )
            session.add(retry_run)
            session.flush()
            retry_run_id = retry_run.id
            task_name = task.name if task is not None else "重试"

        self._write_log("fire", "info", f"[{display_name}] 单独重试目标：{target}（run {run_id}）",
                        owner_user_id=owner, account_id=retry_run.account_id)
        status, message_text = "failed", "重试未执行"
        sent_entry = {
            "target": target,
            "message": message,
            "success": False,
            "status_code": None,
            "status_name": "retry_failed",
            "reason": "重试未执行",
            "dry_run": False,
            "retry_of_run": run_id,
        }
        lock = self._account_lock(retry_run.account_id)
        if not lock.acquire(blocking=False):
            with self.database.session() as session:
                self._finish_run(session, retry_run_id, "skipped", "该账号正在续火，稍后再重试")
            raise DouyinValidationError("该账号正在续火，请稍后再重试")
        try:
            result = self._send_with_browser_fallback(
                cookies,
                {target: message},
                data_dir=Path(config.DOUYIN_DATA_DIR),
                protocol_targets_cache=list(account.protocol_targets_cache or []),
                proxy=proxy_cfg,
                display_name=display_name,
                owner=owner,
                account_id=retry_run.account_id,
                aliases_by_target=aliases_by_target,
                user_id=self._protocol_user_id(account),
            )
        except DouyinRemoteError as exc:
            status, message_text = "failed", f"重试失败：{exc}"
            sent_entry["reason"] = str(exc)
            if exc.auth_failed:
                with self.database.session() as session:
                    row = session.get(DouyinAccountRow, retry_run.account_id)
                    if row is not None:
                        row.status = "relogin"
                        row.last_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("单独重试异常 run=%s target=%s", run_id, target)
            status, message_text = "failed", f"重试异常：{exc}"
            sent_entry["reason"] = str(exc)
        else:
            results = list(result.get("sent", []) or [])
            item = results[0] if results else {}
            ok, reason = summarize_status(item)
            sent_entry.update({
                "success": ok,
                "status_code": item.get("statusCode"),
                "status_name": str(item.get("statusName") or ""),
                "reason": reason,
                "message": str(item.get("message") or message),
            })
            if item.get("matched"):
                sent_entry["matched"] = str(item["matched"])
            if item.get("screenshot"):
                sent_entry["screenshot"] = str(item["screenshot"])
            status = "success" if ok else "failed"
            message_text = f"单独重试完成：{'成功' if ok else reason}"
        finally:
            lock.release()

        with self.database.session() as session:
            account_row = session.get(DouyinAccountRow, retry_run.account_id)
            self._finish_run(session, retry_run_id, status, message_text, {
                "sent_ok": 1 if sent_entry["success"] else 0,
                "sent_fail": 0 if sent_entry["success"] else 1,
                "sent": [sent_entry],
                "task_name": task_name,
                "retry_of_run": run_id,
            })
            account_names = {account_row.id: display_name} if account_row is not None else {}
            row = session.get(DouyinRunRow, retry_run_id)
            payload = self._run_dict(row, {task.id if task else None: task_name}, account_names,
                                     include_screenshots=True)
        self._write_log("fire", "success" if status == "success" else "error",
                        f"[{display_name}] {message_text}", owner_user_id=owner,
                        account_id=retry_run.account_id)
        return payload

    # ------------------------------------------------------------------ 日志

    def list_logs(self, user: dict, *, category="", level="", keyword="", account_id=None, limit=200) -> dict:
        with self.database.session() as session:
            stmt = self._scope(select(DouyinLogRow), DouyinLogRow, user)
            if category:
                stmt = stmt.where(DouyinLogRow.category == category)
            if level:
                stmt = stmt.where(DouyinLogRow.level == level)
            if account_id:
                stmt = stmt.where(DouyinLogRow.account_id == account_id)
            if keyword:
                stmt = stmt.where(DouyinLogRow.message.contains(keyword[:200]))
            rows = list(
                session.scalars(
                    stmt.order_by(DouyinLogRow.created_at.desc(), DouyinLogRow.id.desc())
                    .limit(min(max(limit, 1), 1000))
                )
            )
            items = [
                {
                    "id": row.id,
                    "category": row.category,
                    "level": row.level,
                    "message": row.message,
                    "account_id": row.account_id,
                    "created_at": row.created_at,
                }
                for row in rows
            ]
            counts = {"info": 0, "success": 0, "warning": 0, "error": 0}
            for row in rows:
                counts[row.level] = counts.get(row.level, 0) + 1
            return {"logs": list(reversed(items)), "counts": counts, "total": len(items)}

    def clear_logs(self, user: dict) -> dict:
        with self.database.session() as session:
            stmt = self._scope(select(DouyinLogRow), DouyinLogRow, user)
            count = 0
            for row in session.scalars(stmt):
                session.delete(row)
                count += 1
        self._write_log("system", "info", f"清空日志（{count} 条）", owner_user_id=self._owner(user))
        return {"cleared": count}

    def export_logs_text(self, user: dict) -> str:
        data = self.list_logs(user, limit=1000)
        lines = []
        for row in data["logs"]:
            stamp = row["created_at"].strftime("%Y-%m-%d %H:%M:%S") if row["created_at"] else ""
            lines.append(f"[{stamp}][{row['category']}][{row['level']}] {row['message']}")
        return "\n".join(lines) or "暂无日志"

    # ------------------------------------------------------------------ 全局设置

    def get_global_settings(self, user: dict) -> dict:
        migrated = False
        with self.database.session() as session:
            row = session.get(DouyinSettingRow, GLOBAL_SETTINGS_KEY)
            value = dict(row.value or {}) if row is not None else {}
            variants = [str(item).strip() for item in (value.get("message_variants") or []) if str(item).strip()]
            if variants and is_legacy_default_pool(variants):
                # 旧内置池含"火花/续火/打卡"等易被安全校验拦下的用词：识别到即回落到新内置池
                variants = []
                value["message_variants"] = []
                migrated = True
                if row is not None:
                    row.value = value
                    session.flush()
        if migrated:
            self._write_log(
                "system", "info",
                "检测到旧版内置文案（含高风险词），已自动切换为新版自然口语模板池",
                owner_user_id=self._owner(user),
            )
        return {
            "message_variants": variants,
            "hitokoto_types": [str(item) for item in (value.get("hitokoto_types") or [])],
            "using_builtin": not variants,
        }

    def update_global_settings(self, payload: dict, user: dict) -> dict:
        variants = self._normalize_targets(payload.get("message_variants"))
        hitokoto_types = self._normalize_targets(payload.get("hitokoto_types"))
        with self.database.session() as session:
            row = session.get(DouyinSettingRow, GLOBAL_SETTINGS_KEY)
            if row is None:
                row = DouyinSettingRow(key=GLOBAL_SETTINGS_KEY, value={})
                session.add(row)
            if not self._is_admin(user):
                # 非管理员只能点选现有文案池（当前池 ∪ 内置池），新增自定义文案仅限管理员
                existing = set(
                    (row.value or {}).get("message_variants")
                    or []
                )
                allowed = existing | set(DEFAULT_MESSAGE_VARIANTS)
                illegal = [v for v in variants if v not in allowed]
                if illegal:
                    raise DouyinValidationError(
                        "只有管理员才有权限添加自定义文案，请先取消勾选新文案后重试"
                    )
            row.value = {"message_variants": variants, "hitokoto_types": hitokoto_types}
            session.flush()
        self._write_log(
            "system", "info",
            "更新全局消息模板：" + ("内置默认池" if not variants else f"自定义 {len(variants)} 条"),
            owner_user_id=self._owner(user),
        )
        return self.get_global_settings(user)

    def get_default_variants(self) -> list[str]:
        settings = self.get_global_settings({"id": 0, "role": "admin"})
        return settings["message_variants"] or list(DEFAULT_MESSAGE_VARIANTS)

    # ------------------------------------------------------------------ 企微通知

    def get_notify_settings(self, user: dict) -> dict:
        if not self._is_admin(user):
            raise DouyinNotFound("抖音通知设置不存在")
        try:
            return load_douyin_settings()
        except DouyinSettingsError as exc:
            raise DouyinValidationError(str(exc)) from exc

    def update_notify_settings(self, payload: dict, user: dict) -> dict:
        if not self._is_admin(user):
            raise DouyinNotFound("抖音通知设置不存在")
        try:
            return save_douyin_settings(payload.get("douyin_webhook_url", ""))
        except DouyinSettingsError as exc:
            raise DouyinValidationError(str(exc)) from exc

    def _owner_username(self, owner_user_id: int | None) -> str | None:
        return owner_username(self.database, owner_user_id)

    def _owner_card_type(self, owner_user_id: int | None) -> str | None:
        """归属用户的会员卡型（企微通知里在用户名后标注「（次卡）」）。"""
        return owner_card_type(self.database, owner_user_id)

    def _send_run_notification(self, task: DouyinTaskRow | None, *, account_name: str, run_id: int, status: str, message: str, summary: dict | None):
        """任务运行结束后推送企业微信通知（未启用/未配置机器人时静默跳过）。"""
        if task is None or not task.notify_wecom:
            return
        try:
            webhook_url = load_douyin_settings().get("douyin_webhook_url", "")
            if not webhook_url:
                return
            run = None
            with self.database.session() as session:
                run = session.get(DouyinRunRow, run_id)
            payload = summary or {}
            self.notifier.send_summary(webhook_url, {
                "task_name": payload.get("task_name") or (task.name if task else "续火"),
                "account_name": account_name,
                "owner_username": self._owner_username(task.owner_user_id) or "未归属",
                "owner_card_type": self._owner_card_type(task.owner_user_id),
                "trigger": getattr(run, "trigger", "") if run is not None else "",
                "started_at": getattr(run, "started_at", None) if run is not None else None,
                "status": status,
                "message": message,
                "sent_ok": payload.get("sent_ok", 0),
                "sent_fail": payload.get("sent_fail", 0),
                "details": self._strip_screenshots(payload.get("sent", [])),
            })
            self._write_log(
                "task", "info",
                f"任务「{task.name}」企业微信通知发送成功",
                owner_user_id=task.owner_user_id,
            )
        except Exception as exc:  # noqa: BLE001 —— 通知失败不影响任务结果
            self._write_log(
                "task", "warning",
                f"任务「{task.name}」企业微信通知发送失败：{type(exc).__name__}",
                owner_user_id=getattr(task, "owner_user_id", None),
            )

    # ------------------------------------------------------------------ 执行

    def run_task(self, task_id: int, user: dict) -> dict:
        with self.database.session() as session:
            task = self._get_task(session, task_id, user)
            account = session.get(DouyinAccountRow, task.account_id)
            if account is None:
                raise DouyinValidationError("任务关联账号不存在")
            run = DouyinRunRow(
                owner_user_id=task.owner_user_id,
                task_id=task.id,
                account_id=task.account_id,
                schedule_key=f"manual:{uuid.uuid4().hex}",
                trigger="manual",
            )
            session.add(run)
            session.flush()
            run_id = run.id
            owner = task.owner_user_id
        self._submit_run(run_id, owner, task.id, account.id, "manual")
        self._write_log("task", "info", f"手动执行任务：{task.name}", owner_user_id=owner, account_id=account.id)
        return {"run_id": run_id, "status": "started"}

    def batch_run_tasks(self, user: dict) -> dict:
        """全部启用任务立即执行（跳过停用任务与不可用账号）。"""
        plan = []
        with self.database.session() as session:
            tasks = list(session.scalars(self._scope(select(DouyinTaskRow), DouyinTaskRow, user)))
            account_map = {
                row.id: row for row in session.scalars(
                    self._scope(select(DouyinAccountRow), DouyinAccountRow, user)
                )
            }
            for task in tasks:
                if not task.enabled:
                    continue
                account = account_map.get(task.account_id)
                if account is None or not account.enabled or account.status != "active":
                    continue
                run = DouyinRunRow(
                    owner_user_id=task.owner_user_id,
                    task_id=task.id,
                    account_id=task.account_id,
                    schedule_key=f"batch:{uuid.uuid4().hex}",
                    trigger="batch",
                )
                session.add(run)
                session.flush()
                plan.append({"run_id": run.id, "task_id": task.id, "account_id": account.id, "owner": task.owner_user_id})
        for item in plan:
            self._submit_run(item["run_id"], item["owner"], item["task_id"], item["account_id"], "batch")
        self._write_log("task", "info", f"批量执行续火任务（{len(plan)} 个）", owner_user_id=self._owner(user))
        return {"planned": len(plan), "runs": [{"run_id": item["run_id"], "task_id": item["task_id"]} for item in plan]}

    def run_due_tasks(self):
        # cookie 定时保活（内部带 60s 节流 + 每账号 6h 锚点防重入）
        self._maybe_refresh_cookies()
        # 扫码登录的共享浏览器空闲回收（小内存服务器不能长期养着内核）
        if self.login_manager is not None:
            self.login_manager.maybe_close_idle_browser(LOGIN_BROWSER_IDLE_SECONDS)
        now = datetime.now()
        today = now.date()
        # 先投递上一轮「随机延迟」已到点的任务（挂库，重启也不丢）
        self._flush_delayed_runs(now)
        submissions: list[tuple[int, int, int, int]] = []
        with self.database.session() as session:
            tasks = list(session.scalars(select(DouyinTaskRow).where(DouyinTaskRow.enabled.is_(True))))
            for task in tasks:
                if task.date_mode == "specific" and task.auto_disable_after_finish:
                    last_occurrence = get_last_effective_occurrence({
                        "date_mode": task.date_mode,
                        "run_dates": task.run_dates or [],
                        "skip_dates": task.skip_dates or [],
                        "skip_weekends": task.skip_weekends,
                        "start_date": task.start_date,
                        "end_date": task.end_date,
                        "schedule_times": task.schedule_times or [],
                    })
                    if last_occurrence is not None and last_occurrence < now:
                        task.enabled = False
                        task.updated_at = now
                        self._write_log(
                            "scheduler", "info", f"指定日期计划已结束，任务自动停用：{task.name}",
                            owner_user_id=task.owner_user_id, account_id=task.account_id,
                        )
                        continue
                if not self._date_allowed(task, today):
                    continue
                for configured in task.schedule_times or []:
                    key = simple_due_key(configured, now, task.last_schedule_key)
                    if key is None:
                        continue
                    task.last_schedule_key = key
                    run = DouyinRunRow(
                        owner_user_id=task.owner_user_id,
                        task_id=task.id,
                        account_id=task.account_id,
                        schedule_key=key,
                        trigger="scheduler",
                    )
                    session.add(run)
                    session.flush()
                    delay = self._task_delay_seconds(task)
                    if delay > 0:
                        # 随机延迟不再在线程里 sleep（原实现占着账号锁和线程池槽位，
                        # 最长 600s，期间同账号任务被丢弃、其他用户任务干等）。
                        # 改为把投递时刻写进 run，到点由下一次调度扫描投递。
                        run.deliver_at = now + timedelta(seconds=delay)
                        run.message = f"随机延迟 {delay:.0f}s 后发送"
                        self._write_log("scheduler", "info",
                                        f"定时触发任务：{task.name}（随机等待 {delay:.0f}s 后发送）",
                                        owner_user_id=task.owner_user_id, account_id=task.account_id)
                        continue
                    submissions.append((run.id, task.owner_user_id, task.id, task.account_id))
                    self._write_log("scheduler", "info", f"定时触发任务：{task.name}", owner_user_id=task.owner_user_id, account_id=task.account_id)
        # 事务提交后再投递后台执行：后台线程依赖 run 行已落库可读
        for run_id, owner, task_id, account_id in submissions:
            self._submit_run(run_id, owner, task_id, account_id, "scheduler")

    @staticmethod
    def _task_delay_seconds(task: DouyinTaskRow) -> float:
        """任务级「到点后随机等待」秒数：NULL/0 表示不等待。"""
        low = task.delay_min_seconds or 0
        high = task.delay_max_seconds or 0
        if low <= 0 and high <= 0:
            return 0.0
        low = max(0, low)
        high = max(low, high)
        return random.uniform(low, high) if high > low else float(low)

    def _submit_run(self, run_id: int, owner: int, task_id: int | None, account_id: int, trigger: str):
        """把一次运行排进该账号的队列：同账号串行执行，不再丢弃。

        历史行为是「该账号已有任务在跑就标 skipped」，多好友/多任务场景会静默漏发；
        改为 FIFO 排队：全局并发仍受 MAX_PARALLEL_RUNS 限制，同账号靠 busy 标记串行。
        """
        def _job():
            self._execute_run(run_id, owner, task_id, account_id)

        with self._queue_guard:
            queue = self._account_queues.setdefault(account_id, deque())
            # 前方任务数 = 队列里还没轮到的 + 正在执行的那条
            waiting = len(queue) + (1 if account_id in self._account_busy else 0)
            overflow = len(queue) >= MAX_QUEUED_RUNS_PER_ACCOUNT
            if not overflow:
                queue.append(_job)
        if overflow:
            self._write_log("scheduler", "warning",
                            f"账号排队任务过多（>{MAX_QUEUED_RUNS_PER_ACCOUNT}），本次运行跳过",
                            owner_user_id=owner, account_id=account_id)
            self._mark_run_skipped(run_id, f"该账号排队任务过多（>{MAX_QUEUED_RUNS_PER_ACCOUNT}），已跳过")
            return
        if waiting:
            # 排队可见性：列表页能看到「排队中，前方 N 个任务」，而不是假装运行中
            self._mark_run_queued(run_id, waiting)
        self._pump()

    def _pump(self) -> None:
        """按账号队列投递任务：账号忙则跳过，全局活跃数不超过并发上限。"""
        with self._queue_guard:
            if self._closed:
                return
            for account_id in list(self._account_queues):
                if self._active_runs >= MAX_PARALLEL_RUNS:
                    break
                queue = self._account_queues.get(account_id)
                if not queue or account_id in self._account_busy:
                    continue
                job = queue.popleft()
                # 先标记 busy 再投递：避免同账号第二个任务阻塞在锁上白占线程池槽位
                self._account_busy.add(account_id)
                self._active_runs += 1
                future = self._executor.submit(
                    self._run_queued, self._account_lock(account_id), job, account_id
                )
                self._pending.add(future)
                future.add_done_callback(self._pending.discard)

    def _run_queued(self, lock: threading.Lock, job, account_id: int):
        try:
            with lock:
                if not self._closed:
                    job()
        except Exception:  # noqa: BLE001 —— 兜底：异常不能让队列永久停摆
            logger.exception("续火任务执行异常 account=%s", account_id)
        finally:
            with self._queue_guard:
                self._account_busy.discard(account_id)
                self._active_runs = max(0, self._active_runs - 1)
            self._pump()

    def _mark_run_skipped(self, run_id: int, message: str):
        with self.database.session() as session:
            run = session.get(DouyinRunRow, run_id)
            if run is not None and run.status == "running":
                run.status = "skipped"
                run.message = message
                run.finished_at = datetime.now()

    def _mark_run_queued(self, run_id: int, waiting: int):
        with self.database.session() as session:
            run = session.get(DouyinRunRow, run_id)
            if run is not None and run.status == "running":
                run.message = f"排队中，前方 {waiting} 个任务"

    def _flush_delayed_runs(self, now: datetime | None = None) -> None:
        """投递到点的随机延迟任务（deliver_at 已过）。

        待投递状态挂在库里而不是内存：进程重启后仍会被下一次调度扫描捞回，
        不会把 run 永久留在 running。
        """
        now = now or datetime.now()
        try:
            with self.database.session() as session:
                rows = list(session.scalars(
                    select(DouyinRunRow).where(
                        DouyinRunRow.status == "running",
                        DouyinRunRow.deliver_at.is_not(None),
                        DouyinRunRow.deliver_at <= now,
                    )
                ))
                items = [(row.id, row.owner_user_id, row.task_id, row.account_id) for row in rows]
                for row in rows:
                    row.deliver_at = None
        except Exception:  # noqa: BLE001
            logger.exception("延迟任务投递扫描失败")
            return
        for run_id, owner, task_id, account_id in items:
            self._submit_run(run_id, owner, task_id, account_id, "scheduler")

    def wait_for_idle(self, timeout: float = 60.0):
        """等待全部已提交/排队的续火任务执行完毕（测试/关停前同步点）。"""
        deadline = time.time() + timeout
        while True:
            with self._queue_guard:
                queued = sum(len(q) for q in self._account_queues.values())
            futures = list(self._pending)
            if not futures and queued == 0:
                return
            for future in futures:
                future.result(timeout=max(0.0, deadline - time.time()))
            if time.time() >= deadline:
                return
            time.sleep(0.05)

    def _previous_messages(self, session, account_id: int, *, runs_limit: int = 5) -> dict:
        """最近几次运行已发送的内容（按目标）。同一好友反复收到同一句更易被判定机器群发。"""
        rows = session.scalars(
            select(DouyinRunRow)
            .where(DouyinRunRow.account_id == account_id, DouyinRunRow.status != "running")
            .order_by(DouyinRunRow.id.desc())
            .limit(runs_limit)
        ).all()
        previous: dict[str, str] = {}
        for run in rows:
            for entry in (run.response_summary or {}).get("sent", []) or []:
                if not isinstance(entry, dict) or entry.get("dry_run"):
                    continue
                target = str(entry.get("target") or "").strip()
                message = str(entry.get("message") or "").strip()
                if target and message and target not in previous:
                    previous[target] = message
        return previous

    def _send_with_browser_fallback(self, cookies, messages: dict, *, data_dir, protocol_targets_cache,
                                    proxy, display_name: str, owner: int, account_id: int,
                                    aliases_by_target: dict | None = None, user_id: str = "") -> dict:
        """协议通道优先；失败/未解决的目标自动改用浏览器通道重发。

        背景（2026-10-09）：协议栈发送被抖音服务端安全校验拦（CheckMessageNotPass
        / 8610），同账号同内容浏览器通道可正常发送。此处协议失败后自动兜底，
        结果结构保持与 run_protocol_send 一致（sent 条目带 channel 字段）。
        登录态失效（auth_failed）不兜底——cookie 无效时浏览器同样发不出去，
        交由上层走重新登录流程。
        """
        retry_targets: list[str] = []
        if not PROTOCOL_CHANNEL_ENABLED:
            # 协议栈缺 a_bogus 签名，必然被风控拦（CheckMessageNotPass/8610），
            # 每个目标白等约 70s；直接整批交给浏览器通道，不再空转
            self._write_log("fire", "info",
                            f"[{display_name}] 浏览器通道直发（协议通道未启用：缺少 a_bogus 签名）",
                            owner_user_id=owner, account_id=account_id)
            retry_targets = [str(t) for t in messages.keys()]
            result = {"ok": False, "dryRun": False, "sent": [], "unresolved": [],
                      "protocol_targets_cache": None}
        else:
            try:
                result = run_protocol_send(
                    cookies,
                    messages,
                    data_dir=data_dir,
                    protocol_targets_cache=protocol_targets_cache,
                    proxy=proxy,
                    user_id=user_id,
                )
            except DouyinRemoteError as exc:
                if exc.auth_failed:
                    raise
                self._write_log("fire", "warning",
                                f"[{display_name}] 协议通道失败，改用浏览器通道重试：{exc}",
                                owner_user_id=owner, account_id=account_id)
                result = {"ok": False, "dryRun": False, "sent": [], "unresolved": [],
                          "protocol_targets_cache": None}

            for entry in result.get("sent", []) or []:
                target = str(entry.get("target") or "").strip()
                ok, _ = summarize_status(entry)
                if target and not ok and not entry.get("dryRun"):
                    retry_targets.append(target)
            for item in result.get("unresolved", []) or []:
                target = str(item.get("target") or "").strip()
                if target:
                    retry_targets.append(target)

        sub_messages = {t: messages[t] for t in retry_targets if t in messages}
        if not sub_messages:
            return result

        if PROTOCOL_CHANNEL_ENABLED:
            self._write_log("fire", "info",
                            f"[{display_name}] 协议未成功 {len(sub_messages)} 个目标，切换浏览器通道重试",
                            owner_user_id=owner, account_id=account_id)
        try:
            browser_result = send_via_browser(
                cookies, sub_messages,
                account_id=account_id, proxy=proxy,
                aliases_by_target=aliases_by_target,
            )
        except BrowserSendError as exc:
            self._write_log("fire", "error", f"[{display_name}] 浏览器通道不可用：{exc}",
                            owner_user_id=owner, account_id=account_id)
            return result
        return self._merge_send_results(result, browser_result)

    @staticmethod
    def _merge_send_results(protocol_result: dict, browser_result: dict) -> dict:
        """把浏览器通道结果并入协议结果：失败项按目标替换/补齐，ok 重算。"""
        merged = dict(protocol_result or {})
        browser_entries = {
            str(item.get("target") or ""): item
            for item in browser_result.get("sent", []) or []
        }
        merged_sent: list[dict] = []
        for entry in merged.get("sent", []) or []:
            target = str(entry.get("target") or "")
            replacement = browser_entries.pop(target, None)
            ok, _ = summarize_status(entry)
            # 协议已成功的不动；失败/未成功且有浏览器结果 → 用浏览器结果替换
            merged_sent.append(replacement if (replacement is not None and not ok) else entry)
        # 协议 unresolved（或协议整体失败的）目标由浏览器补发
        merged_sent.extend(browser_entries.values())
        merged["sent"] = merged_sent
        handled = {str(item.get("target") or "") for item in merged_sent}
        merged["unresolved"] = [
            item for item in (merged.get("unresolved") or [])
            if str(item.get("target") or "") not in handled
        ]
        merged["ok"] = bool(merged_sent) and all(summarize_status(e)[0] for e in merged_sent)
        return merged

    def _execute_run(self, run_id: int, owner: int, task_id: int | None, account_id: int):
        with self.database.session() as session:
            task = session.get(DouyinTaskRow, task_id) if task_id else None
            account = session.get(DouyinAccountRow, account_id)
            if account is None:
                self._finish_run(session, run_id, "failed", "账号不存在")
                return
            if not account.enabled:
                self._finish_run(session, run_id, "skipped", "账号已停用")
                return
            if account.status != "active":
                self._finish_run(session, run_id, "failed", "账号登录态异常，请重新校验或更新 cookie")
                self._send_run_notification(
                    task,
                    account_name=account.remark or account.nickname or account.remote_user_id,
                    run_id=run_id, status="failed",
                    message="账号登录态异常，请重新校验或更新 cookie", summary=None,
                )
                return
            run = session.get(DouyinRunRow, run_id)
            if run is None:
                return
            cookies = self._cookies_for(account)
            targets = list(task.targets or []) if task else []
            display_name = account.remark or account.nickname or account.remote_user_id
            task_name = task.name if task else "续火"
            proxy_cfg = self._proxy_row_config(session, account.proxy_id)

        try:
            variants = list(task.message_variants or []) if task else []
            if not variants or is_legacy_default_pool(variants):
                variants = self.get_default_variants()
            settings = self.get_global_settings({"id": 0, "role": "admin"})
            with self.database.session() as previous_session:
                previous = self._previous_messages(previous_session, account_id)
            payload = build_target_payload(
                targets,
                variants,
                previous_messages=previous,
                hitokoto_types=settings.get("hitokoto_types") or None,
            )
            messages = payload["messages"]
            aliases_by_target = payload["aliases"]
            # 火花预检（本地）：今天已经成功续过的好友不再重复发
            skipped_today: list[dict] = []
            if task is not None and bool(task.skip_sent_today):
                with self.database.session() as today_session:
                    sent_today = self._sent_targets_today(today_session, account_id, exclude_run_id=run_id)
                for spec in payload["specs"]:
                    names = [spec["primary"], *spec["aliases"]]
                    stamp = next((sent_today[name] for name in names if sent_today.get(name)), "")
                    if not stamp:
                        continue
                    skipped_today.append({
                        "target": spec["primary"],
                        "message": messages.pop(spec["primary"], ""),
                        "success": True,
                        "status_code": None,
                        "status_name": "skipped_sent_today",
                        "reason": f"今日 {stamp} 已续过，跳过",
                        "dry_run": False,
                        "skipped": True,
                    })
            if not messages:
                with self.database.session() as session:
                    self._finish_run(session, run_id, "skipped", "今日目标已全部续过，无需重复发送", {
                        "sent_ok": 0,
                        "sent_fail": 0,
                        "skipped": len(skipped_today),
                        "sent": skipped_today,
                        "task_name": task.name if task else "续火",
                    })
                self._write_log("fire", "info", f"[{display_name}] 今日目标已全部续过，跳过本次执行",
                                owner_user_id=owner, account_id=account_id)
                return
        except Exception as exc:  # noqa: BLE001
            with self.database.session() as session:
                self._finish_run(session, run_id, "failed", f"消息构建失败：{exc}")
            self._send_run_notification(task, account_name=display_name, run_id=run_id,
                                        status="failed", message=f"消息构建失败：{exc}", summary=None)
            return

        self._write_log("fire", "info",
                        f"[{display_name}] 开始续火：{task_name}，目标 {len(messages)} 个"
                        + (f"（{len(skipped_today)} 个今日已续，跳过）" if skipped_today else ""),
                        owner_user_id=owner, account_id=account_id)
        try:
            result = self._send_with_browser_fallback(
                cookies,
                messages,
                data_dir=Path(config.DOUYIN_DATA_DIR),
                protocol_targets_cache=list(account.protocol_targets_cache or []),
                proxy=proxy_cfg,
                display_name=display_name,
                owner=owner,
                account_id=account_id,
                aliases_by_target=aliases_by_target,
                user_id=self._protocol_user_id(account),
            )
        except DouyinRemoteError as exc:
            with self.database.session() as session:
                account_row = session.get(DouyinAccountRow, account_id)
                if account_row is not None and exc.auth_failed:
                    account_row.status = "relogin"
                    account_row.last_error = str(exc)
                self._finish_run(session, run_id, "failed", str(exc))
            self._write_log("fire", "error", f"[{display_name}] 续火失败：{exc}",
                            owner_user_id=owner, account_id=account_id)
            self._send_run_notification(task, account_name=display_name, run_id=run_id,
                                        status="failed", message=str(exc), summary=None)
            return
        except Exception as exc:  # noqa: BLE001 —— 后台线程兜底
            logger.exception("抖音续火执行异常 run=%s", run_id)
            with self.database.session() as session:
                self._finish_run(session, run_id, "failed", f"执行异常：{exc}")
            self._send_run_notification(task, account_name=display_name, run_id=run_id,
                                        status="failed", message=f"执行异常：{exc}", summary=None)
            return

        sent_entries = list(skipped_today)
        sent_ok = sent_fail = 0
        renamed: dict[str, str] = {}
        for entry in result.get("sent", []) or []:
            target = str(entry.get("target") or "")
            matched = str(entry.get("matched") or "")
            if matched and matched != target:
                renamed[target] = matched
            ok, reason = summarize_status(entry)
            if ok:
                sent_ok += 1
            else:
                sent_fail += 1
            sent_entry = {
                "target": target,
                "message": str(entry.get("message") or ""),
                "success": ok,
                "status_code": entry.get("statusCode"),
                "status_name": str(entry.get("statusName") or ""),
                "reason": reason,
                "dry_run": bool(entry.get("dryRun")),
            }
            if matched:
                sent_entry["matched"] = matched
            # 浏览器通道的发送回执截图（JPEG base64），详情接口返回、列表接口剥离
            if entry.get("screenshot"):
                sent_entry["screenshot"] = str(entry["screenshot"])
            sent_entries.append(sent_entry)
            log_line = f"[{display_name}] → {target} " + ("发送成功" if ok else f"发送失败：{reason}")
            if matched and matched != target:
                log_line += f"（按别名命中「{matched}」）"
            if entry.get("dryRun"):
                log_line = f"[{display_name}] → {target} 演练完成（未真实发送）"
            self._write_log("fire", "success" if ok else "error", log_line,
                            owner_user_id=owner, account_id=account_id)

        for item in result.get("unresolved", []) or []:
            target = str(item.get("target") or "").strip()
            if not target:
                continue
            sent_fail += 1
            sent_entries.append({
                "target": target,
                "message": messages.get(target, ""),
                "success": False,
                "status_code": None,
                "status_name": "unresolved",
                "reason": "未在会话列表中找到该目标（检查昵称或先在抖音里聊过一句）",
                "dry_run": False,
            })
            self._write_log("fire", "warning", f"[{display_name}] 未找到目标：{target}",
                            owner_user_id=owner, account_id=account_id)

        # 好友改名自动升级主名：命中了别名就用新名字替换 targets 里的主名（别名保留）
        # dry_run 需在升级判断前取好（演练不回写任务目标）
        dry_run = bool(result.get("dryRun"))
        if renamed and task is not None and not dry_run:
            with self.database.session() as session:
                task_row = session.get(DouyinTaskRow, task.id)
                if task_row is not None:
                    updated_specs = []
                    for spec in normalize_target_specs(task_row.targets or []):
                        matched = renamed.get(spec["primary"])
                        if not matched:
                            updated_specs.append(spec)
                            continue
                        aliases = [spec["primary"], *spec["aliases"]]
                        updated_specs.append({
                            "primary": matched,
                            "aliases": [name for name in dict.fromkeys(aliases) if name != matched],
                            "message": spec["message"],
                            "raw": spec["raw"],
                        })
                    task_row.targets = [format_target_spec(spec) for spec in updated_specs]
                    task_row.updated_at = datetime.now()
            for old_name, new_name in renamed.items():
                self._write_log("task", "info",
                                f"好友已改名：{old_name} → {new_name}（已自动更新任务目标主名）",
                                owner_user_id=owner, account_id=account_id)

        skipped_count = len(skipped_today)
        if dry_run:
            status, message = "success", f"演练完成：{len(sent_entries)} 个目标未真实发送"
        elif not sent_entries:
            status, message = "failed", "没有任何发送结果（账号或目标异常）"
        elif sent_fail == 0:
            status, message = "success", f"续火完成：成功 {sent_ok}/{sent_ok + sent_fail}"
        elif sent_ok:
            status, message = "partial", f"部分成功：成功 {sent_ok}，失败 {sent_fail}"
        else:
            status, message = "failed", f"全部失败：0 成功，{sent_fail} 失败"
        if skipped_count:
            message += f"（另有 {skipped_count} 个今日已续，跳过）"

        with self.database.session() as session:
            account_row = session.get(DouyinAccountRow, account_id)
            if account_row is not None:
                cache = result.get("protocol_targets_cache")
                if isinstance(cache, list):
                    account_row.protocol_targets_cache = cache
                if not dry_run:
                    account_row.last_verified_at = datetime.now()
                    # 浏览器通道在持久 profile 内跑过 → 该账号的 profile 也顺带保鲜了
                    if self._profile_on(account_row):
                        account_row.profile_updated_at = datetime.now()
                    if account_row.status == "relogin":
                        account_row.status = "active"
                        account_row.last_error = ""
            self._finish_run(session, run_id, status, message, {
                "sent_ok": sent_ok,
                "sent_fail": sent_fail,
                "skipped": skipped_count,
                "dry_run": dry_run,
                "sent": sent_entries,
                "task_name": task_name,
            })
        self._write_log("fire", "success" if status == "success" else ("warning" if status == "partial" else "error"),
                        f"[{display_name}] {message}", owner_user_id=owner, account_id=account_id)
        # 发送后顺带刷新平台火花快照（只读；失败静默，不影响本次续火结果）
        if not dry_run:
            self._refresh_spark_snapshot(
                cookies, account_id, display_name,
                data_dir=Path(config.DOUYIN_DATA_DIR),
                proxy=proxy_cfg, owner=owner,
                user_id=self._protocol_user_id(account),
            )
        self._send_run_notification(task, account_name=display_name, run_id=run_id,
                                    status=status, message=message, summary={
                                        "sent_ok": sent_ok,
                                        "sent_fail": sent_fail,
                                        "task_name": task_name,
                                        "sent": sent_entries,
                                    })

    def _refresh_spark_snapshot(self, cookies, account_id: int, display_name: str, *,
                                data_dir, protocol_targets_cache=None, proxy=None, owner=None,
                                user_id: str = "") -> None:
        """读取平台火花并落库快照；任何异常都吞掉（纯附加信息，不能影响主流程）。"""
        if protocol_targets_cache is None:
            try:
                with self.database.session() as session:
                    row = session.get(DouyinAccountRow, account_id)
                    protocol_targets_cache = list(row.protocol_targets_cache or []) if row else []
            except Exception:  # noqa: BLE001
                protocol_targets_cache = []
        try:
            result = run_protocol_spark_state(
                cookies,
                data_dir=data_dir,
                protocol_targets_cache=protocol_targets_cache,
                proxy=proxy,
                user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001 —— 快照是附加信息，失败只记日志
            logger.info("火花快照刷新失败 account=%s：%s", account_id, exc)
            return
        sparks = [self._spark_dict(item) for item in (result.get("sparks") or []) if isinstance(item, dict)]
        pending = sum(1 for item in sparks if item["pending_today"])
        fetched_at = datetime.now()
        try:
            with self.database.session() as session:
                row = session.get(DouyinAccountRow, account_id)
                if row is not None:
                    row.spark_snapshot = {
                        "sparks": sparks,
                        "pending_today": pending,
                        "count": len(sparks),
                        "fetched_at": fetched_at.isoformat(timespec="seconds"),
                    }
                    row.spark_checked_at = fetched_at
        except Exception as exc:  # noqa: BLE001
            logger.info("火花快照落库失败 account=%s：%s", account_id, exc)

    def _finish_run(self, session, run_id: int, status: str, message: str, summary: dict | None = None):
        run = session.get(DouyinRunRow, run_id)
        if run is None:
            return
        run.status = status
        run.message = (message or "")[:2000]
        run.response_summary = summary or {}
        run.finished_at = datetime.now()
        session.flush()

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _normalize_date_plan(payload: dict) -> dict:
        try:
            return normalize_task_date_rule(payload)
        except ValueError as exc:
            raise DouyinValidationError(str(exc))

    @staticmethod
    def _date_allowed(task: DouyinTaskRow, value: date) -> bool:
        if not is_task_active_on({
            "date_mode": task.date_mode,
            "run_dates": task.run_dates or [],
            "skip_dates": task.skip_dates or [],
            "skip_weekends": task.skip_weekends,
        }, value):
            return False
        if task.start_date and value.isoformat() < task.start_date:
            return False
        if task.end_date and value.isoformat() > task.end_date:
            return False
        return True

    def _get_account(self, session, account_id: int, user) -> DouyinAccountRow:
        row = session.get(DouyinAccountRow, account_id)
        if not row or (row.owner_user_id != self._owner(user) and not self._is_admin(user)):
            raise DouyinNotFound("抖音账号不存在")
        return row

    def _get_task(self, session, task_id: int, user) -> DouyinTaskRow:
        row = session.get(DouyinTaskRow, task_id)
        if not row or (row.owner_user_id != self._owner(user) and not self._is_admin(user)):
            raise DouyinNotFound("抖音续火任务不存在")
        return row

    @staticmethod
    def _account_dict(r: DouyinAccountRow) -> dict:
        return {
            "id": r.id,
            "owner_user_id": r.owner_user_id,
            "remote_user_id": r.remote_user_id,
            "remark": r.remark,
            "nickname": r.nickname,
            "douyin_id": r.douyin_id,
            "avatar": r.avatar,
            "proxy_id": r.proxy_id,
            "source": r.source,
            "display_name": r.remark or r.nickname or r.remote_user_id,
            "targets_cached": len(r.protocol_targets_cache or []),
            "status": r.status,
            "enabled": r.enabled,
            "last_verified_at": r.last_verified_at,
            "cookies_refreshed_at": r.cookies_refreshed_at,
            "profile_enabled": bool(r.profile_enabled) if r.profile_enabled is not None else True,
            "profile_updated_at": r.profile_updated_at,
            "last_error": r.last_error,
            "updated_at": r.updated_at,
        }

    @staticmethod
    def _task_dict(r: DouyinTaskRow) -> dict:
        return {
            "id": r.id,
            "owner_user_id": r.owner_user_id,
            "account_id": r.account_id,
            "name": r.name,
            "enabled": r.enabled,
            "targets": r.targets or [],
            "message_variants": r.message_variants or [],
            "schedule_times": r.schedule_times or [],
            "delay_min_seconds": r.delay_min_seconds,
            "delay_max_seconds": r.delay_max_seconds,
            "skip_sent_today": bool(r.skip_sent_today),
            "start_date": r.start_date,
            "end_date": r.end_date,
            "skip_weekends": r.skip_weekends,
            "date_mode": r.date_mode,
            "run_dates": r.run_dates or [],
            "skip_dates": r.skip_dates or [],
            "auto_disable_after_finish": r.auto_disable_after_finish,
            "notify_wecom": bool(r.notify_wecom),
            "updated_at": r.updated_at,
        }

    @staticmethod
    def _strip_screenshots(entries) -> list:
        """去掉发送条目里的 base64 截图（列表响应/企微通知用，避免体积过大）。"""
        cleaned = []
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("screenshot"):
                entry = {k: v for k, v in entry.items() if k != "screenshot"}
            cleaned.append(entry)
        return cleaned

    @staticmethod
    def _run_dict(r: DouyinRunRow, task_names=None, account_names=None, include_screenshots=False) -> dict:
        summary = dict(r.response_summary or {})
        if summary.get("sent") and not include_screenshots:
            summary["sent"] = DouyinService._strip_screenshots(summary["sent"])
        return {
            "id": r.id,
            "task_id": r.task_id,
            "task_name": task_names.get(r.task_id, "") if task_names and r.task_id else "",
            "account_id": r.account_id,
            "account_name": account_names.get(r.account_id, "未知账号") if account_names else "",
            "trigger": r.trigger,
            "status": r.status,
            "message": r.message,
            "response_summary": summary,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
        }
