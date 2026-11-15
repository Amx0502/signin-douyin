"""抖音扫码登录管理器。

无头浏览器（playwright，默认 msedge channel，缺浏览器时自动降级
chrome / playwright 自带 chromium）打开登录页（站点由 ``DOUYIN_SITE`` 决定：
默认 ``www.douyin.com`` 主站，``creator`` 为创作者中心回退），
定位二维码（data:image PNG / canvas / 登录弹窗内的二维码图，正方形 100~380px，
按接近 178px 评分），截图 base64 供前端展示；前端轮询 poll()，每次用 context.cookies()
调用身份解析判定登录，确认后自动建号并关闭会话。

主站首页未登录时只给「登录」入口：会先点开入口（``_maybe_trigger_login``），
若弹窗停在验证码登录再切到扫码 tab（``_maybe_switch_to_qr_tab``）。

所有 playwright 操作都提交到内置单线程 executor（串行），
确认登录时在 executor 线程内直接关闭会话（不能再 _submit，会死锁）。
"""

import base64
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .douyin_client import DouyinRemoteError, fetch_self_profile, fetch_session_identity
from .douyin_site import active_site

logger = logging.getLogger(__name__)

# 登录地址随站点配置（DOUYIN_SITE=web 走 www.douyin.com；creator 为回退）
LOGIN_URL = active_site().login_url
# 网络留痕过滤的目标域（只记本域业务请求，避免第三方 SDK 刷屏）
LOG_HOST = active_site().host
# 会话有效期：慢渲染（二维码最长等 45s）+ 扫码 + 短信验证码环节需要更宽裕的窗口
SESSION_TTL_SECONDS = 480
# 扫码登录排查用调试产物目录（挂在宿主 tools/dy-debug，可直接查看截图）
DEBUG_DIR = "/app/tools/dy-debug"
# ⚠️ 调试快照默认关闭（DOUYIN_LOGIN_DEBUG=1 打开）。
# 原因（2026-10-15 线上实测）：每张快照 = 全页截图（timeout 10s，最大 1MB）
# ＋ page.title()（**Playwright 默认 30s 超时**，抖音反调试专门拖死
# Runtime.evaluate 通道）＋ 全量 cookies，而全部跑在登录单线程 executor 上。
# 一次登录会落 6~16 张（等待期每 30s 一张、最多 12 张），把「提交验证码→建号」
# 这件事排在后面，是「提交慢 / 拿到账号慢」的主因。
LOGIN_DEBUG_ENABLED = (os.environ.get("DOUYIN_LOGIN_DEBUG") or "").strip().lower() in (
    "1", "true", "yes", "on",
)
# 调试产物保留天数（超期在管理器初始化时清理；历史上无人清理，堆到 54MB/386 文件）
DEBUG_RETENTION_DAYS = 3
# 共享浏览器空闲多久后关闭（小内存服务器上不能长期养着一个 chromium）
BROWSER_IDLE_SECONDS = 600
LOGIN_GOTO_TIMEOUT_MS = 15_000
QR_POLL_INTERVAL_MS = 200
QR_WAIT_SECONDS = 45
# 「等页面 UI 变化」类循环的轮询间隔（登录弹窗出现 / 发码结果 / 原设备确认）。
# ⚠️ 比二维码捕获慢一档是刻意的（2026-10-15）：这些循环每轮要做 6~8 次
# ``locator.count()`` / ``bounding_box()``（每次都是一次 CDP 往返），200ms 间隔
# 就是 ~40 次 CDP/秒，而此刻页面自己正在渲染登录弹窗 / 发短信请求——两边抢同一条
# CDP 与主线程，实测弹窗要 22s 才出来。降到 500ms 把压力压掉 60%，主观延迟无感。
UI_POLL_INTERVAL_MS = 500
# 登录页 SSR 身份抓取（page.evaluate，无超时参数）的最小重试间隔；见 _identity_hint
IDENTITY_HINT_MIN_INTERVAL_SECONDS = 5

# 全局浏览器互斥：扫码登录与 cookie 保活都要起 chromium，
# 小内存服务器（1.6G）上并发起多个内核容易 OOM，这里串行化（发送通道另有自己的实例）
BROWSER_GUARD = threading.RLock()

# 浏览器通道降级顺序：可用环境变量 DOUYIN_BROWSER_CHANNEL 指定首选通道
# （msedge / chrome / 留空表示 playwright 自带 chromium），找不到时自动尝试后续。
_BROWSER_CHANNELS = ["msedge", "chrome", ""]


class DouyinLoginError(RuntimeError):
    pass


BROWSER_LAUNCH_ARGS = [
    "--no-first-run",
    "--disable-extensions",
    # 容器环境（/dev/shm 过小、无 GPU）防崩溃
    "--disable-dev-shm-usage",
    "--disable-gpu",
    # 抖音风控：隐藏 navigator.webdriver 自动化特征，
    # 否则登录页会返回空壳文档（有 title 无 body、二维码不渲染）
    "--disable-blink-features=AutomationControlled",
    # 扫码只需要登录 SDK：关掉视频解码与后台节流，省 CPU。
    # ⚠️ 2026-10-13 实测：`--autoplay-policy=user-gesture-required` 会让
    #    抖音判定「用户未交互」，登录弹窗不自动弹出 → 不启用。
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--mute-audio",
    "--no-sandbox",
]



def _browser_channels() -> list[str]:
    """内核降级顺序：可用环境变量 DOUYIN_BROWSER_CHANNEL 指定首选通道。"""
    preferred = os.getenv("DOUYIN_BROWSER_CHANNEL", "").strip()
    return ([preferred] if preferred and preferred not in _BROWSER_CHANNELS else []) + _BROWSER_CHANNELS


def _launch_kwargs(proxy=None, *, headless: bool = True) -> dict:
    """启动参数（launch / launch_persistent_context 共用），含代理与反自动化特征。"""
    kwargs = {
        "headless": headless,
        "timeout": 30_000,
        "args": list(BROWSER_LAUNCH_ARGS),
    }
    if proxy and proxy.get("host"):
        if str(proxy.get("protocol") or "").lower() == "socks5":
            raise DouyinLoginError("扫码登录暂不支持 socks5 代理，请改用 http/https 代理")
        server = f"{proxy['protocol'] or 'http'}://{proxy['host']}:{int(proxy['port'])}"
        kwargs["proxy"] = {"server": server}
        if proxy.get("username"):
            kwargs["proxy"]["username"] = proxy["username"]
            kwargs["proxy"]["password"] = proxy.get("password") or ""
    return kwargs


def _launch_chromium(playwright, proxy=None):
    """按降级顺序启动浏览器，全部失败时抛出带指引的 DouyinLoginError。

    proxy 为 service 层解析出的 {protocol,host,port,username,password}；
    仅支持 http/https 代理（socks5 由调用方拦截）。
    """
    last_error: Exception | None = None
    for channel in _browser_channels():
        try:
            kwargs = _launch_kwargs(proxy)
            if channel:
                kwargs["channel"] = channel
            return playwright.chromium.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001 —— 尝试下一个通道
            last_error = exc
    raise DouyinLoginError(
        "服务器未找到可用的浏览器内核（尝试了 Edge/Chrome/内置 Chromium）。"
        "请在服务器执行 playwright install chromium，或安装 Edge/Chrome 后重试。"
        f"最后错误：{last_error}"
    )


def launch_persistent_chromium(playwright, user_data_dir, proxy=None, *, headless: bool = True):
    """按降级顺序启动**持久化**上下文（user_data_dir 即浏览器 profile 目录）。

    返回 Playwright 的 ``BrowserContext``（persistent context 没有独立的
    ``Browser`` 对象，close 上下文即关内核）。调用方负责 close。

    注意：透传 ``user_data_dir`` 时不能再传 ``user_agent``（内核自持指纹），
    也**不要**指定 channel 以外的 profile 参数，否则指纹不稳定。
    """
    last_error: Exception | None = None
    for channel in _browser_channels():
        try:
            kwargs = _launch_kwargs(proxy, headless=headless)
            if channel:
                kwargs["channel"] = channel
            return playwright.chromium.launch_persistent_context(str(user_data_dir), **kwargs)
        except Exception as exc:  # noqa: BLE001 —— 尝试下一个通道
            last_error = exc
    raise DouyinLoginError(
        "服务器未找到可用的浏览器内核（尝试了 Edge/Chrome/内置 Chromium）。"
        "请在服务器执行 playwright install chromium，或安装 Edge/Chrome 后重试。"
        f"最后错误：{last_error}"
    )


class _LoginSession:
    def __init__(self, remark: str, owner_user_id: int, proxy=None):
        self.qr_id = uuid.uuid4().hex
        self.remark = remark
        self.owner_user_id = owner_user_id
        self.proxy = proxy
        self.created_at = time.time()
        self.status = "waiting"  # waiting / confirmed / expired / error / sms_sent / captcha
        self.mode = "qr"         # qr=扫码登录 / sms=手机号验证码登录
        self.phone = ""
        self.qr_image = ""       # data:image/png;base64,...
        self.message = ""
        self.verification_image = ""  # 触发安全验证时的页面截图（data URL）
        self.playwright = None
        self.context = None
        self.page = None
        self.result = None       # 确认后的账号 dict
        # P1 持久 profile：扫码期间用预分配槽位建目录，建号后由 service 迁移到真实 account_id
        self.profile_slot = 0
        self.profile_guard = None
        # poll() 去重标记：所有 playwright 操作共用单线程 executor，无脑入队会把
        # 「提交验证码 / 建号」排在几十个重复检查后面（实测表现为「提交半天没反应」）。
        # 由 poll() 置 True、_check_login() 在 finally 复位。
        self.checking = False


class DouyinLoginManager:
    # 未进入终态（confirmed / expired / error）的状态集合：超过 TTL 一律作废。
    # 2026-10-15：原实现只把 ``waiting`` 判过期，实测有会话在短信/验证环节挂满
    # 186s（远超检查次数）仍被反复入队检查，白白占着登录单线程队列；
    # app 层的 ``_cleanup_expired_locked`` 也复用同一份集合，避免两处漂移。
    _EXPIRABLE_STATUSES = ("waiting", "verification_required", "sms_sent", "captcha")

    def __init__(self, create_account_callback, resolve_proxy_callback=None):
        """create_account_callback(payload, user) -> dict：登录确认后建号。

        resolve_proxy_callback(proxy_id, user) -> dict|None：登录会话出站代理。
        """
        self._create_account = create_account_callback
        self._resolve_proxy = resolve_proxy_callback or (lambda proxy_id, user: None)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="douyin-login")
        self._sessions: dict[str, _LoginSession] = {}
        self._guard = threading.Lock()
        self._closed = False
        # 共享浏览器（扫码提速）：全部只在 _executor 线程内使用
        self._playwright = None
        self._browser = None
        self._browser_proxy_key = None
        self._browser_last_used = 0.0
        # 历史上无人清理调试产物，宿主目录曾堆到 54MB / 386 文件（198 张 PNG）。
        # 启动时清一次超期文件；失败静默（见 _purge_debug_dir）。
        self._purge_debug_dir()

    # ------------------------------------------------------------------ 对外

    def start(self, payload: dict, user: dict) -> dict:
        remark = str(payload.get("remark") or "").strip()
        proxy_id = int(payload.get("proxy_id") or 0) or None
        proxy = self._resolve_proxy(proxy_id, user) if proxy_id else None
        session = _LoginSession(remark, int(user.get("id") or 0), proxy)
        # P1 持久 profile（可用 ?persistent=0 关闭，便于排障时退回一次性上下文）
        use_profile = payload.get("persistent")
        session.profile_slot = self._allocate_profile_slot() if use_profile is None or use_profile else 0
        with self._guard:
            self._cleanup_expired_locked()
            self._sessions[session.qr_id] = session
        self._submit(self._open_and_capture, session)
        return {"qr_id": session.qr_id, "status": session.status, "ttl": SESSION_TTL_SECONDS}

    def _allocate_profile_slot(self) -> int:
        """给尚未建号的扫码会话预分配 profile 槽位（负数段，避开真实 account_id）。"""
        with self._guard:
            self._profile_slot_seq = getattr(self, "_profile_slot_seq", 0) + 1
            return -int(self._profile_slot_seq)

    # ---------------------------------------------------------- 手机号登录

    def start_sms(self, payload: dict, user: dict) -> dict:
        """手机号验证码登录：开浏览器 → 切「验证码登录」→ 填手机号 → 点「获取验证码」。

        2026-10-14 实测（用户抓包）：验证码提交后**必有一次「原设备确认」**——
        无人脸账号直接弹「使用原设备扫码」，有人脸账号先弹「身份验证」选择
        （刷脸/原设备扫码）。确认二维码由本会话截图给前端，用户用原手机 APP
        扫一下即完成；之后的登录判定/建号复用扫码链路的 poll()。
        """
        remark = str(payload.get("remark") or "").strip()
        phone = str(payload.get("phone") or "").strip()
        digits = "".join(ch for ch in phone if ch.isdigit())
        if not digits.startswith("1") or len(digits) != 11:
            raise DouyinLoginError("请输入 11 位大陆手机号")
        proxy_id = int(payload.get("proxy_id") or 0) or None
        proxy = self._resolve_proxy(proxy_id, user) if proxy_id else None
        session = _LoginSession(remark, int(user.get("id") or 0), proxy)
        session.mode = "sms"
        session.phone = digits
        session.profile_slot = self._allocate_profile_slot()
        with self._guard:
            self._cleanup_expired_locked()
            self._sessions[session.qr_id] = session
        self._submit(self._open_sms_and_fill, session)
        return {"qr_id": session.qr_id, "status": "starting", "ttl": SESSION_TTL_SECONDS}

    def submit_sms_code(self, qr_id: str, code: str) -> dict:
        """把用户收到的短信验证码填入并点「登录」，随后处理「原设备确认」弹窗。"""
        with self._guard:
            session = self._sessions.get(qr_id)
        if session is None or session.mode != "sms":
            raise DouyinLoginError("登录会话不存在或已过期，请重新发起")
        if session.status not in ("sms_sent", "waiting", "error", "captcha", "starting"):
            raise DouyinLoginError(f"当前状态（{session.status}）不能提交验证码")
        code = "".join(ch for ch in str(code or "") if ch.isdigit())
        if not code:
            raise DouyinLoginError("请输入短信验证码")
        self._submit(self._submit_sms_login, session, code)
        return {"qr_id": session.qr_id, "status": "submitting", "message": "验证码提交中"}

    def poll(self, qr_id: str, user: dict) -> dict:
        """前端轮询入口（2.5s 一次）。

        ⚠️ **去重是关键**：所有 playwright 操作都排在同一个单线程 executor 上，
        而单次 ``_check_login`` 可能因网络/页面卡住耗时数秒。若每个 poll 都无脑
        入队，队列会被重复的检查灌满，用户的「提交验证码」「建号」只能排在后面
        ——实测表现为「点了提交半天没反应」。这里用 ``session.checking`` 保证
        同一会话同时只有一个检查在跑/在队列里。
        """
        with self._guard:
            session = self._sessions.get(qr_id)
        if session is None:
            raise DouyinLoginError("登录会话不存在或已过期，请重新发起")
        expired = (
            time.time() - session.created_at > SESSION_TTL_SECONDS
            and session.status in self._EXPIRABLE_STATUSES
        )
        need_check = False
        if session.status in ("waiting", "verification_required") and not session.checking:
            session.checking = True
            need_check = True
        if expired:
            self._submit(self._expire, session)
        if need_check and not self._submit(self._check_login, session):
            session.checking = False
        return {
            "qr_id": session.qr_id,
            "status": session.status,
            "qr_image": session.qr_image,
            "message": session.message,
            "verification_image": session.verification_image,
            "account": session.result,
        }

    def close(self, qr_id: str):
        with self._guard:
            session = self._sessions.pop(qr_id, None)
        if session is not None:
            self._submit(self._close_session, session)

    def submit_verification(self, qr_id: str, code: str) -> dict:
        """扫码触发安全验证时，把用户输入的验证码填入登录页并提交。"""
        with self._guard:
            session = self._sessions.get(qr_id)
        if session is None:
            raise DouyinLoginError("登录会话不存在或已过期，请重新发起")
        code = str(code or "").strip()
        if not code:
            raise DouyinLoginError("请输入验证码")
        self._submit(self._submit_verification_code, session, code)
        return {"qr_id": session.qr_id, "status": session.status, "message": "验证码已提交，请等待确认"}

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        with self._guard:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self._submit(self._close_session, session)
        # 关掉共享内核（含 playwright 实例）；必须在 executor 线程内做（playwright 有线程归属）
        self._executor.submit(self._safe, self._stop_browser)
        self._executor.shutdown(wait=False)

    def close_all(self):
        with self._guard:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self._submit(self._close_session, session)

    # ------------------------------------------------------------------ 内部

    def _submit(self, fn, *args):
        """把操作丢进登录单线程队列，返回是否成功入队。

        线程池关闭后 ``submit`` 会抛 RuntimeError；``poll()`` 依赖返回值复位
        ``session.checking``，否则一次入队失败会把去重标记永久留在 True，
        该会话再也不会被检查（前端表现是「一直等待扫码」而不是报错）。
        """
        try:
            self._executor.submit(self._safe, fn, *args)
            return True
        except RuntimeError:
            logger.debug("抖音登录线程池已关闭，丢弃任务 %s", getattr(fn, "__name__", fn))
            return False

    @staticmethod
    def _safe(fn, *args):
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 —— 后台线程兜底
            logger.exception("抖音扫码登录后台操作失败")

    def _cleanup_expired_locked(self):
        now = time.time()
        expired = [
            qr_id for qr_id, session in self._sessions.items()
            if now - session.created_at > SESSION_TTL_SECONDS
            and session.status in self._EXPIRABLE_STATUSES + ("expired", "error")
        ]
        for qr_id in expired:
            session = self._sessions.pop(qr_id, None)
            if session is not None:
                # 必须真正关闭浏览器：只从字典里移除会让无头浏览器一直空转
                # （占内存 + 100% CPU，小内存服务器上曾把整机拖垮，2026-10-09）
                self._submit(self._close_session, session)

    def _open_and_capture(self, session: _LoginSession):
        started = time.time()

        def step(message: str):
            logger.info("抖音扫码[%s] %s (+%.1fs)", session.qr_id[:8], message, time.time() - started)
            session.message = message

        try:
            step("正在启动浏览器内核…")
            if session.profile_slot:
                # P1：扫码即建立持久 profile（登录态由浏览器写盘续命，cookie 不再是单点）
                session.context, session.profile_guard = self._new_profile_context(session)
            else:
                browser = self._ensure_browser(session.proxy)
                session.context = self._new_context(browser)
            step("正在打开抖音登录页…")
            # 双保险：注入脚本抹掉 navigator.webdriver（部分内核版本该启动参数不完全生效）
            session.context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            # 网络留痕：短信验证等关键请求记录状态与响应片段（排查「提交无反应」）
            session.context.on("response", self._log_key_response)
            session.context.on("requestfailed", self._log_failed_request)
            session.context.on("request", self._log_request_start)
            # 身份捕获：主站登录成功后页面会请求 profile/self，零成本拿到 uid
            self._hook_identity_capture(session)
            page = session.context.new_page()
            page.on("console", self._log_console)
            page.on("pageerror", self._log_page_error)
            session.page = page
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=LOGIN_GOTO_TIMEOUT_MS)
            logger.info("抖音扫码[%s] 登录页已打开 url=%s (+%.1fs)", session.qr_id[:8], page.url, time.time() - started)
            step("正在等待二维码渲染…")
            qr_image = ""
            # ⚠️ 2026-10-13 实测：主站**不要**提前点登录入口 —— 主动点开弹窗会走
            #    「交互触发」链路，passport 侧风控校验更重，实测 19s → 60s+。
            #    被动等页面自行弹出登录弹窗是最快路径（本站点会自动弹）。
            try:
                qr_image = self._capture_qr(page, timeout_seconds=25)
            except DouyinLoginError:
                # 兜底：页面迟迟不自动弹出登录弹窗时，才主动点一次入口
                if self._maybe_trigger_login(page):
                    step("已点开登录弹窗，等待二维码…")
                    try:
                        qr_image = self._capture_qr(page, timeout_seconds=15)
                    except DouyinLoginError:
                        if self._maybe_switch_to_qr_tab(page):
                            step("已切到扫码登录，等待二维码…")
                            qr_image = self._capture_qr(page)
                        else:
                            raise
                else:
                    raise
            session.qr_image = qr_image
            session.status = "waiting"
            session.message = "请使用抖音 App 扫码登录"
            logger.info("抖音扫码[%s] 二维码已捕获 (+%.1fs)", session.qr_id[:8], time.time() - started)
            self._dump_debug(session, "qr")
        except Exception as exc:  # noqa: BLE001
            logger.exception("抖音扫码登录二维码获取失败")
            # 共享内核可能已被 OOM 杀掉：丢掉它，下次扫码冷启动重来
            self._stop_browser()
            session.status = "error"
            session.message = f"二维码获取失败：{exc}"
            self._dump_debug(session, "fail")
            self._close_session(session)

    def _hook_sms_login(self, session: _LoginSession):
        """监听 ``/passport/web/sms_login/`` 响应：验证码提交是否被受理。

        HAR 实证三种结果：①受理 → ``account_flow=verify``（进二次验证，随后
        lite get_qrcode 出确认码）；②受理+直接登录（无二次验证）→ 下发 cookie；
        ③拒绝（验证码错误/过期）→ ``error_code`` 非零 + message。
        """
        def _on_response(response):
            try:
                url = str(response.url or "")
            except Exception:  # noqa: BLE001
                return
            if "/passport/web/sms_login/" not in url:
                return
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                return
            data = (payload or {}).get("data") or {}
            error_code = int(payload.get("error_code") or data.get("error_code") or 0)
            message = str(payload.get("message") or "")
            flow = str(data.get("account_flow") or "")
            logger.info("抖音短信[%s] sms_login 响应：message=%s error_code=%s flow=%s",
                        session.qr_id[:8], message, error_code, flow)
            # ⚠️ 顺序关键：`account_flow=verify` 是「需要二次验证」的**正常**信号，
            # 会同时带 error_code=2046（实测），必须先判 flow 再判 error_code，
            # 否则会误报「验证码被拒绝」。
            if flow == "verify":
                session.message = "抖音要求二次验证，正在获取原设备确认二维码…"
            elif error_code:
                session.status = "sms_sent"
                session.message = f"验证码被拒绝：{message or error_code}（可重新输入提交）"

        try:
            session.context.on("response", _on_response)
        except Exception as exc:  # noqa: BLE001
            logger.info("抖音短信[%s] sms_login 监听注册失败：%s", session.qr_id[:8], exc)

    def _hook_confirm_qr(self, session: _LoginSession):
        """监听「原设备确认」二维码：直接取响应里的 base64 PNG，比截图可靠。

        2026-10-14 用户 HAR 实证：验证码登录后页面 passport **lite** SDK 会调
        ``POST /passport/web/get_qrcode/?passport_jssdk_type=lite``，响应
        ``data.qrcode`` 就是 base64 PNG（iVBORw0KGgo…），随后每 3s 轮询
        ``/passport/mobile/validate_by_qrcode/`` 直到 ``status=confirmed``。
        轮询由页面 SDK 自己完成，我们只负责把二维码交给前端给用户扫。
        """
        def _on_response(response):
            try:
                url = str(response.url or "")
            except Exception:  # noqa: BLE001
                return
            if "/passport/web/get_qrcode/" not in url or "type=lite" not in url:
                return
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                return
            qrcode = str(((payload or {}).get("data") or {}).get("qrcode") or "").strip()
            if not qrcode:
                return
            session.qr_image = f"data:image/png;base64,{qrcode}"
            if session.status in ("waiting", "sms_sent", "captcha", "error"):
                session.status = "waiting"
                session.message = "请用**原手机**抖音 APP 扫码确认（原设备确认码，不是登录码）"
            logger.info("抖音短信[%s] 已从接口取到原设备确认二维码", session.qr_id[:8])

        try:
            session.context.on("response", _on_response)
        except Exception as exc:  # noqa: BLE001
            logger.info("抖音短信[%s] 确认二维码监听注册失败：%s", session.qr_id[:8], exc)

    def _open_sms_and_fill(self, session: _LoginSession):
        """短信登录第一步：开浏览器 → 切「验证码登录」→ 填手机号 → 发验证码。"""
        started = time.time()

        def step(message: str):
            logger.info("抖音短信[%s] %s (+%.1fs)", session.qr_id[:8], message, time.time() - started)
            session.message = message

        try:
            step("正在启动浏览器内核…")
            session.context, session.profile_guard = self._new_profile_context(session)
            session.context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            session.context.on("response", self._log_key_response)
            session.context.on("requestfailed", self._log_failed_request)
            self._hook_identity_capture(session)
            self._hook_confirm_qr(session)
            self._hook_sms_login(session)
            page = session.context.new_page()
            page.on("console", self._log_console)
            page.on("pageerror", self._log_page_error)
            session.page = page
            step("正在打开抖音登录页…")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=LOGIN_GOTO_TIMEOUT_MS)
            step("正在切换到验证码登录…")
            if not self._switch_to_sms_tab(page):
                raise DouyinLoginError("登录弹窗未出现「验证码登录」入口，页面结构可能已变化")
            step("正在填写手机号…")
            self._fill_sms_phone(page, session.phone)
            step("正在请求发送验证码…")
            self._click_send_sms_code(page)
            # 等待发码结果：成功（倒计时文案）/ 滑块 / 错误提示，最长 20s
            outcome = self._wait_sms_send_outcome(page, timeout_seconds=20)
            if outcome == "sent":
                session.status = "sms_sent"
                session.message = "验证码已发送，请查收短信并输入"
                logger.info("抖音短信[%s] 验证码已发送 (+%.1fs)", session.qr_id[:8], time.time() - started)
            elif outcome == "captcha":
                session.status = "captcha"
                session.verification_image = self._capture_state_screenshot(page)
                session.message = "发送验证码触发滑块验证，无头环境无法完成；请改用扫码登录"
            else:
                session.status = "error"
                session.message = "验证码发送失败（可能操作频繁或触发风控），请稍后重试或改用扫码登录"
            self._dump_debug(session, f"sms-{outcome}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("抖音短信登录发起失败")
            session.status = "error"
            session.message = f"短信登录发起失败：{exc}"
            self._dump_debug(session, "fail")
            self._close_session(session)

    @staticmethod
    def _switch_to_sms_tab(page, timeout_seconds=30) -> bool:
        """等登录弹窗出现并点「验证码登录」tab。

        ⚠️ 2026-10-15 改：原实现是 node 侧 200ms 死转，每轮对 3 个候选各做
        ``count()`` + ``bounding_box()``（≈15 次 CDP 往返/秒），与页面自己渲染
        登录弹窗抢带宽——线上实测弹窗要 22s 才出来。改用 ``wait_for_selector``：
        轮询发生在页面内（rAF + 变更观察），node 侧一次调用等到底，CDP 往返从
        每秒十几次降到每秒 3 次。仍为「候选轮转」而不是「逐个耗尽预算」，
        否则首个候选不存在时会把 30s 全吃光。
        """
        candidates = ['text="验证码登录"', 'text="短信登录"', 'text="验证码"']
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            for selector in candidates:
                budget_ms = int(min(1_000, (deadline - time.time()) * 1000))
                if budget_ms <= 0:
                    return False
                try:
                    node = page.wait_for_selector(selector, state="visible", timeout=budget_ms)
                except Exception:  # noqa: BLE001 —— 本轮没出现，换下一个候选
                    continue
                if node is None:
                    continue
                try:
                    node.click(timeout=3_000)
                except Exception:  # noqa: BLE001 —— 点不中就当没命中，换候选
                    continue
                page.wait_for_timeout(600)
                return True
        return False

    @staticmethod
    def _fill_sms_phone(page, phone: str):
        """把手机号填入登录弹窗的手机号输入框（placeholder 多候选）。"""
        locator = None
        for placeholder in ("请输入手机号", "手机号"):
            try:
                candidate = page.get_by_placeholder(placeholder)
                if candidate.count() > 0:
                    locator = candidate.first
                    break
            except Exception:  # noqa: BLE001
                continue
        if locator is None:
            raise DouyinLoginError("页面上未找到手机号输入框")
        locator.click(timeout=8_000, force=True)
        try:
            locator.press_sequentially(phone, delay=40)
        except AttributeError:  # 老版本 playwright 兜底
            locator.type(phone, delay=40)
        page.wait_for_timeout(400)

    @staticmethod
    def _click_send_sms_code(page):
        """点「获取验证码」按钮（多候选文案）。"""
        candidates = [
            'text="获取验证码"', 'text="获取短信验证码"', 'text="发送验证码"',
            'text="获取验证码登录"',
        ]
        for selector in candidates:
            try:
                node = page.locator(selector).first
                if node.count() == 0:
                    continue
                if not node.bounding_box():
                    continue
                node.click(timeout=5_000)
                return
            except Exception:  # noqa: BLE001 —— 换下一个候选
                continue
        raise DouyinLoginError("页面上未找到「获取验证码」按钮")

    @staticmethod
    def _wait_sms_send_outcome(page, *, timeout_seconds: int) -> str:
        """判定发码结果：sent（出现倒计时/重新发送）/ captcha（可见滑块）/ error。

        ⚠️ 2026-10-14 实测教训：页面会**常驻预加载隐藏的 rmc-nocaptcha iframe**
        （就算不触发滑块也存在），所以 ①「已发码」文案判定必须最优先；
        ② captcha 判定要求 iframe **可见**（bounding_box 非空），否则会误报
        「触发滑块」——用户实际已收到验证码。
        """
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                # 最强信号：按钮变成「xx秒后重新发送」= 已发码。
                # ⚠️ get_by_text 默认是**子串**匹配，所以「重新发送」一条就覆盖了
                # 「秒后重新发送 / 后重新发送」；原来写三条是三次多余的 CDP 往返。
                if page.get_by_text("重新发送").count() > 0:
                    return "sent"
                # 滑块：只认「可见」的 captcha iframe（隐藏预加载的不算）
                for frame in page.locator('iframe[src*="captcha"]').all():
                    if frame.bounding_box():
                        return "captcha"
                # 常见错误 toast
                for err in ("操作过于频繁", "请稍后再试", "网络异常", "图片验证"):
                    if page.get_by_text(err).count() > 0:
                        return "error"
            except Exception:  # noqa: BLE001 —— 页面可能正在跳转
                pass
            page.wait_for_timeout(UI_POLL_INTERVAL_MS)
        return "error"

    def _submit_sms_login(self, session: _LoginSession, code: str):
        """短信登录第二步：填验证码 → 点「登录」→ 处理「原设备确认」弹窗。

        ⚠️ 2026-10-14 实测：验证码能填进去但按钮点不中——页面右上角导航也有
        「登录」按钮，``text="登录"`` 取 last 会命中错元素。改为收集所有可见的
        「登录」button，点**面积最大**的（弹窗主按钮 ~181×44 vs 右上角 ~56×32）。
        并用 sms_login 响应钩子确认提交是否真的发出。
        """
        page = self._active_page(session)
        if page is None:
            session.status = "error"
            session.message = "浏览器已关闭，请重新发起手机号登录"
            return
        qr8 = session.qr_id[:8]
        try:
            input_node = None
            for placeholder in ("请输入验证码", "验证码"):
                try:
                    candidate = page.get_by_placeholder(placeholder)
                    if candidate.count() > 0:
                        input_node = candidate.first
                        break
                except Exception:  # noqa: BLE001
                    continue
            if input_node is None:
                raise DouyinLoginError("页面上未找到验证码输入框")
            input_node.click(timeout=8_000, force=True)
            try:
                input_node.press_sequentially(code, delay=80)
            except AttributeError:  # 老版本 playwright 兜底
                input_node.type(code, delay=80)
            page.wait_for_timeout(600)

            # DOM 侦察 + 坐标点击（不依赖元素标签/类名/可点性）
            clicked = self._click_largest_login_button(page, input_node)
            if not clicked:
                page.keyboard.press("Enter")
                clicked = "enter"
            logger.info("抖音短信[%s] 验证码已提交（点击方式=%s）", qr8, clicked)
            session.message = "验证码已提交，等待二次验证…"
            self._dump_debug(session, "sms-submitted")
        except Exception as exc:  # noqa: BLE001 —— 提交失败保留会话供重试
            session.message = f"验证码提交失败：{exc}"
            logger.info("抖音短信[%s] 验证码提交失败：%s", qr8, exc)
            return
        # 等二次验证弹窗 / 确认二维码（最长 25s）。
        # 首选：接口钩子已拿到 base64 二维码（`_hook_confirm_qr`）；
        # 兜底：DOM 层面的「使用原设备扫码」点击 + 截图。
        self._await_device_confirm(page, session)

    @staticmethod
    def _click_largest_login_button(page, input_node) -> str:
        """DOM 侦察所有文本=「登录」的可见元素，坐标点击面积最大者。

        v4/v5 教训：主按钮既不是 ``<button>`` 也不叫 .semi-button / role=button
        （页面上唯一的 ``<button>``「登录」是右上角导航 3344px²）。干脆用 JS
        枚举**任意标签**、自身直接文本为「登录」的可见元素拿真实 rect，取面积
        最大者用 ``mouse.click``（坐标点击无视可点性判定/overlay）。
        """
        script = """
        () => {
          const out = [];
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
          let node;
          while ((node = walker.nextNode())) {
            const el = node;
            const own = Array.from(el.childNodes)
              .filter(n => n.nodeType === 3)
              .map(n => n.textContent.trim()).join('');
            if (own !== '登录' && own !== '登 录') continue;
            const rect = el.getBoundingClientRect();
            if (rect.width < 8 || rect.height < 8) continue;
            const style = getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none') continue;
            out.push({
              tag: el.tagName.toLowerCase(),
              cls: String(el.className || '').slice(0, 80),
              x: rect.x, y: rect.y, w: rect.width, h: rect.height,
            });
          }
          return out;
        }
        """
        try:
            hits = page.evaluate(script) or []
        except Exception as exc:  # noqa: BLE001
            logger.info("登录按钮 DOM 侦察失败：%s", exc)
            hits = []
        if hits:
            preview = [
                f"{h['tag']}.{h['cls'][:24]}({int(h['w'])}x{int(h['h'])}@{int(h['x'])},{int(h['y'])})"
                for h in hits[:8]
            ]
            logger.info("登录元素侦察（%d 个）：%s", len(hits), " | ".join(preview))
            best = max(hits, key=lambda h: h["w"] * h["h"])
            area = int(best["w"] * best["h"])
            try:
                page.mouse.click(best["x"] + best["w"] / 2, best["y"] + best["h"] / 2)
                return f"dom-scan({best['tag']}.{best['cls'][:20]} {area}px²)"
            except Exception as exc:  # noqa: BLE001
                logger.info("坐标点击登录元素失败：%s", exc)
        # 兜底：验证码输入框正下方 ~72px（弹窗主按钮位置）
        try:
            box = input_node.bounding_box()
            if box:
                page.mouse.click(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] + 72,
                )
                return "coords(+72px)"
        except Exception as exc:  # noqa: BLE001
            logger.info("坐标点击登录按钮失败：%s", exc)
        return ""

    def _await_device_confirm(self, page, session: _LoginSession):
        """等待/处理「原设备确认」，成功后 session.status 保持 waiting 由 poll 轮询结果。"""
        deadline = time.time() + 25
        while time.time() < deadline:
            if session.qr_image:
                session.status = "waiting"
                return
            self._handle_device_confirm_step(page, session)
            if session.qr_image:
                session.status = "waiting"
                return
            page.wait_for_timeout(UI_POLL_INTERVAL_MS)
        # 兜底结束后仍未拿到确认码：交给 poll/_check_login 继续判
        session.status = "waiting"
        if not session.qr_image:
            session.message = "未识别到原设备确认页面；若抖音未要求确认，登录可能已直接完成"

    def _handle_device_confirm_step(self, page, session: _LoginSession):
        """单步处理「身份验证」选择面板（有人脸账号会先弹二选一）。"""
        try:
            scan_option = page.get_by_text("使用原设备扫码", exact=True)
            if scan_option.count() > 0:
                try:
                    scan_option.first.click(timeout=3_000)
                    logger.info("抖音短信[%s] 已点「使用原设备扫码」", session.qr_id[:8])
                except Exception:  # noqa: BLE001 —— 有的形态不用点，本身就是扫码页
                    pass
                self._capture_confirm_qr_fallback(page, session)
        except Exception:  # noqa: BLE001
            pass

    def _capture_confirm_qr_fallback(self, page, session: _LoginSession):
        """截图兜底：接口钩子没拿到二维码时才用（尽力而为，失败不阻断）。"""
        if session.qr_image:
            return
        try:
            qr_image = self._capture_qr(page, timeout_seconds=15)
        except Exception as exc:  # noqa: BLE001
            logger.info("抖音短信[%s] 确认二维码截图兜底失败：%s", session.qr_id[:8], exc)
            return
        session.qr_image = qr_image
        session.status = "waiting"
        session.message = "请用**原手机**抖音 APP 扫码确认（原设备确认码）"
        self._dump_debug(session, "confirm-qr-fallback")
        logger.info("抖音短信[%s] 已用截图兜底取到确认二维码", session.qr_id[:8])

    @staticmethod
    def _maybe_trigger_login(page) -> bool:
        """主站首页未登录时先点开「登录」入口（二维码在弹窗内）。

        多候选选择器 + 逐个尝试；命中即点击并等弹窗渲染。返回是否点到了。
        """
        candidates = [
            'text="登录"',
            'button:has-text("登录")',
            '[class*="login"]:has-text("登录")',
            'text="Log in"',
        ]
        for selector in candidates:
            try:
                node = page.locator(selector).first
                if node.count() == 0:
                    continue
                box = node.bounding_box()
                if not box:
                    continue
                node.click(timeout=3_000)
                logger.info("抖音扫码：已点击登录入口（selector=%s）", selector)
                page.wait_for_timeout(1_500)  # 等弹窗动画与二维码请求
                return True
            except Exception:  # noqa: BLE001 —— 换下一个候选
                continue
        return False

    @staticmethod
    def _new_context(browser):
        """一次性内存上下文（旧行为，保留给不建 profile 的场景）。"""
        return browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )

    def _new_profile_context(self, session: _LoginSession):
        """扫码会话专用的持久 Profile 上下文（P1）。

        登录成功后 profile 已把登录态写盘，后续 cookie 保活/发送都复用同一目录，
        cookie 掉线的根因（短命字段陈旧）不再致命。返回 (context, holder)：
        ``holder`` 是 ``profile_guard`` 的上下文管理器，close 时释放账号锁。
        """
        from .douyin_profile import open_account_context, profile_guard  # noqa: PLC0415

        guard = profile_guard(self._profile_account_id(session))
        guard.__enter__()
        try:
            context = open_account_context(
                self._ensure_playwright(), self._profile_account_id(session),
                cookies=None, proxy=session.proxy, headless=True,
            )
        except Exception:
            guard.__exit__(None, None, None)
            raise
        return context, guard

    def _ensure_playwright(self):
        """按需启动 playwright 运行时并返回（持久 profile 路径不经过 _ensure_browser）。

        捕获手机号验证码等旧路径由 ``_ensure_browser`` 顺带启动；P1 的持久
        profile 路径绕过它，因此必须自持，否则 ``self._playwright`` 为 None
        会直接报「未找到可用的浏览器内核」。
        """
        if self._playwright is None:
            from playwright.sync_api import sync_playwright  # noqa: PLC0415

            self._playwright = sync_playwright().start()
        return self._playwright

    def _profile_account_id(self, session: _LoginSession) -> int:
        """扫码阶段还没有 account_id：用「当前会话预分配 id」占位。

        建号成功后会由 service 层把 profile 目录**迁移**到真实 account_id
        （见 ``douyin_service.create_account`` 的 ``claim_profile``）。
        """
        return int(getattr(session, "profile_slot", 0) or 0)

    @staticmethod
    def _maybe_switch_to_qr_tab(page) -> bool:
        """登录弹窗可能默认停在「验证码登录」：点一下扫码 tab 切到二维码。

        多候选文案（不同版本用词有差异）；命中即点击并等二维码渲染。
        """
        candidates = ['text="扫码登录"', 'text="二维码登录"', 'text="扫码"', 'text="二维码"']
        for selector in candidates:
            try:
                node = page.locator(selector).first
                if node.count() == 0:
                    continue
                if not node.bounding_box():
                    continue
                node.click(timeout=3_000)
                logger.info("抖音扫码：已切换到扫码登录 tab（selector=%s）", selector)
                page.wait_for_timeout(1_200)
                return True
            except Exception:  # noqa: BLE001 —— 换下一个候选
                continue
        return False

    def _capture_qr(self, page, timeout_seconds=QR_WAIT_SECONDS) -> str:
        """定位二维码并返回 data URL。优先直接读 img src（本身就是 data URL）。"""
        qr_locator = self._wait_for_qr(page, timeout_seconds)
        try:
            src = qr_locator.get_attribute("src", timeout=5_000) or ""
        except Exception:  # noqa: BLE001
            src = ""
        if src.startswith("data:image/"):
            return src
        image_bytes = qr_locator.screenshot(timeout=10_000)
        return "data:image/png;base64," + base64.b64encode(image_bytes).decode()

    @staticmethod
    def _wait_for_qr(page, timeout_seconds=QR_WAIT_SECONDS):
        """轮询等待二维码渲染（固定等待不可靠）。

        2026-10-09 实测：小内存服务器上登录页渲染较慢，二维码 20~55s 才出现
        （反爬脚本占满主线程时会拖更久）；2026-10-11 起共享浏览器内核 + 200ms
        轮询粒度，常态已能明显提前命中，等待窗口收到 45s。

        元素形态按优先级：data URL 图片（创作者中心）→ canvas → 登录弹窗内的
        二维码图片（主站 ``www.douyin.com`` 的二维码常是普通 URL 图）。
        """
        # 选择器按可靠性排序：data URL 最稳，其次 canvas，最后主站弹窗内的图片
        selectors = (
            'img[src^="data:image"]',
            "canvas",
            'img[src*="qrcode"], img[src*="qr_code"], img[src*="qr-code"]',
            '[class*="qrcode"] img, [class*="qr-code"] img, [class*="qrCode"] img, [class*="QrCode"] img',
            '[class*="login"] img, [class*="Login"] img',
        )
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            for selector in selectors:
                best = None
                try:
                    candidates = page.locator(selector)
                    count = candidates.count()
                except Exception:  # noqa: BLE001 —— 页面可能正在跳转
                    continue
                for index in range(count):
                    node = candidates.nth(index)
                    try:
                        box = node.bounding_box()
                    except Exception:
                        continue
                    if not box:
                        continue
                    width = box.get("width") or 0
                    height = box.get("height") or 0
                    if not (100 <= width <= 380 and 100 <= height <= 380):
                        continue
                    if abs(width - height) > 30:
                        continue
                    score = -abs(width - 178)
                    if best is None or score > best[0]:
                        best = (score, node)
                if best is not None:
                    return best[1]
            page.wait_for_timeout(QR_POLL_INTERVAL_MS)
        try:
            img_count = page.locator('img[src^="data:image"]').count()
            canvas_count = page.locator("canvas").count()
            any_img = page.locator("img").count()
        except Exception:  # noqa: BLE001
            img_count = canvas_count = any_img = "?"
        raise DouyinLoginError(
            f"未在登录页找到二维码（等待 {timeout_seconds}s，页面上 data 图片 "
            f"{img_count} 个、canvas {canvas_count} 个、普通图片 {any_img} 个，页面结构可能已变化）"
        )

    def _check_login(self, session: _LoginSession):
        """poll() 入队的登录态检查（在单线程 executor 内执行）。

        ``session.checking`` 必须在**所有**出口复位，所以用 ``finally`` 包住：
        内部有七八条 return 路径（无 cookie / 身份未解析 / 建号失败…），漏一条
        就会把去重标记永久锁死，后续 poll 再也不入队，前端将永远停在「等待扫码」。
        """
        try:
            self._check_login_impl(session)
        finally:
            session.checking = False

    def _check_login_impl(self, session: _LoginSession):
        if session.status not in ("waiting", "verification_required"):
            return
        if session.context is None:
            # 浏览器已断开但状态还是等待中：明确告知，避免前端一直停在「等待扫码」
            session.status = "error"
            session.message = "浏览器会话已断开，请重新发起扫码登录"
            return
        try:
            cookies = session.context.cookies()
        except Exception:
            return
        # 只要有登录态 cookie 家族才值得调预检接口
        names = {item.get("name") for item in cookies}
        login_cookie = any(
            name in names for name in ("sessionid", "sessionid_ss", "sid_guard", "sid_tt")
        )
        self._probe_waiting(session, names, login_cookie)
        if not login_cookie:
            # 扫码确认后可能被风控要求短信二次验证（「身份验证」弹窗）——
            # 自动请求验证码并切换为待输入状态，否则前端会一直停在「等待扫码」
            self._handle_post_scan_verification(session)
            self._notice_submit_stall(session)
            return
        if not getattr(session, "login_seen", False):
            session.login_seen = True
            logger.info("抖音扫码[%s] 检测到登录 cookie：%s", session.qr_id[:8], sorted(names))
            self._dump_debug(session, "confirm")
        try:
            resolved = self._resolve_login_identity(session, cookies)
        except Exception:  # noqa: BLE001
            return
        if not resolved.get("user_id"):
            # 各链路都没认出身份：页面若停在安全验证则切验证态；否则给用户进度反馈，
            # 避免手机端确认后前端永远停在「等待扫码」无任何反应。
            logger.info("抖音扫码[%s] 身份解析未通过：%s",
                        session.qr_id[:8], resolved.get("message") or "-")
            self._maybe_mark_verification(session)
            if session.status == "waiting":
                session.check_fails = getattr(session, "check_fails", 0) + 1
                session.message = f"已确认登录，正在校验登录态…（重试 {session.check_fails}）"
            return
        identity = {"user_id": resolved["user_id"], "token": ""}
        hint = resolved.get("hint") or {}
        try:
            account = self._create_account(
                {
                    "remark": session.remark,
                    "cookie_input": "; ".join(
                        f"{item.get('name')}={item.get('value')}"
                        for item in cookies
                        if item.get("name")
                    ),
                    # 主站链路：顺手把页面上的身份带进建号（省掉一次浏览器验证）
                    **hint,
                },
                {"id": session.owner_user_id, "role": "admin"},
            )
        except Exception as exc:  # noqa: BLE001
            session.status = "error"
            session.message = f"登录成功但建号失败：{exc}"
            self._close_session(session)
            return
        session.status = "confirmed"
        session.message = "登录成功"
        session.result = account
        # P1：把扫码期间建的临时 profile 迁移到真实 account_id 目录，
        # 后续 cookie 保活/发送都复用同一份设备指纹与登录态。
        self._promote_profile(session, account)
        # 在 executor 线程内直接关闭（不能再 _submit，会死锁）
        self._close_session(session)
        logger.info("抖音扫码登录成功 qr_id=%s user_id=%s", session.qr_id, identity.get("user_id"))

    @staticmethod
    def _hook_identity_capture(session: _LoginSession):
        """注册响应监听：捕获页面自身的 ``profile/self``（主站登录后触发）。

        拿到即说明「已登录 + 身份已知」，建号无需再开浏览器或调接口。
        """
        context = session.context
        if context is None:
            return

        def _on_response(response):
            try:
                if "/aweme/v1/web/user/profile/self/" not in str(response.url or ""):
                    return
                payload = response.json()
            except Exception:  # noqa: BLE001
                return
            if ((payload or {}).get("user") or {}).get("uid"):
                session.identity_payload = payload
                logger.info("抖音扫码[%s] 已从页面响应捕获登录身份", session.qr_id[:8])

        try:
            context.on("response", _on_response)
        except Exception:  # noqa: BLE001
            logger.debug("身份响应监听注册失败", exc_info=True)

    def _resolve_login_identity(self, session: _LoginSession, cookies) -> dict:
        """登录确认时的身份解析，返回 ``{user_id, hint, message}``。

        优先级：
        1. 页面响应捕获的 ``profile/self``（主站登录后触发，最直接）；
        2. 页面 SSR 数据（``_identity_hint``）；
        3. creator 接口预检（``user_token``，passport 资料兜底）。
        """
        payload = getattr(session, "identity_payload", None)
        if payload:
            from .douyin_identity import _profile_from_self_payload  # noqa: PLC0415

            profile = _profile_from_self_payload(payload)
            if profile.get("user_id"):
                logger.info("抖音扫码[%s] 身份来自页面响应 user_id=%s",
                            session.qr_id[:8], profile["user_id"])
                return {"user_id": profile["user_id"], "message": "", "hint": {
                    "remote_user_id": profile["user_id"],
                    "nickname": profile.get("nickname") or "",
                    "douyin_id": profile.get("douyin_id") or "",
                    "avatar": profile.get("avatar") or "",
                }}
        hint = self._identity_hint(session)
        if hint.get("remote_user_id"):
            logger.info("抖音扫码[%s] 身份来自页面 SSR user_id=%s",
                        session.qr_id[:8], hint["remote_user_id"])
            return {"user_id": hint["remote_user_id"], "message": "", "hint": hint}
        try:
            identity = fetch_session_identity(cookies)
        except DouyinRemoteError as exc:
            # 预检被拒 ≠ 登录失败：手机端已确认时 cookie 是新鲜的，多半是接口被风控。
            # 用 passport 自身资料接口兜底（不同链路）。
            logger.info("抖音扫码[%s] 身份预检被拒：%s；改走资料接口", session.qr_id[:8], exc)
            profile = fetch_self_profile(cookies)
            if profile.get("user_id"):
                logger.info("抖音扫码[%s] 资料接口兜底成功 user_id=%s",
                            session.qr_id[:8], profile["user_id"])
                return {"user_id": str(profile["user_id"]), "message": "", "hint": {
                    "remote_user_id": str(profile["user_id"]),
                    "nickname": profile.get("nickname") or "",
                    "douyin_id": profile.get("douyin_id") or "",
                    "avatar": profile.get("avatar") or "",
                }}
            return {"user_id": "", "message": str(exc), "hint": {}}
        profile = fetch_self_profile(cookies)
        return {"user_id": str(identity["user_id"]), "message": "", "hint": {
            "remote_user_id": str(identity["user_id"]),
            "nickname": profile.get("nickname") or "",
            "douyin_id": profile.get("douyin_id") or "",
            "avatar": profile.get("avatar") or "",
        }}

    def _identity_hint(self, session: _LoginSession) -> dict:
        """从登录页面顺手抓当前用户身份（主站链路省掉一次浏览器验证）。

        尽力而为：失败返回空 dict，建号流程会回退到接口/浏览器校验。

        ⚠️ 2026-10-15 加缓存 + 失败节流（``IDENTITY_HINT_MIN_INTERVAL_SECONDS``）：
        ① ``page.evaluate`` **没有 timeout 参数**（Playwright 不把默认超时作用于
        evaluate），而抖音反调试会拖死 Runtime.evaluate 通道——真卡住就是整个
        登录单线程被占死，「提交验证码 / 拿账号」全部排队；
        ② 即便正常，这段脚本每次要把 ``_ROUTER_DATA`` 等 4 个全局对象各
        ``JSON.stringify(...).slice(0, 300000)`` 回传（最多 ~1.2MB），再在 Python
        侧 json.loads + 深搜——每 2.5s 一次纯属浪费。
        身份在一次登录内不会变，所以命中即缓存；未命中则 5s 内只试一次，
        期间直接返回空（调用方本来就准备好走接口兜底）。
        """
        cached = getattr(session, "identity_hint_cache", None)
        if cached:
            return cached
        now = time.time()
        if now - getattr(session, "identity_hint_at", 0.0) < IDENTITY_HINT_MIN_INTERVAL_SECONDS:
            return {}
        session.identity_hint_at = now
        page = self._active_page(session)
        if page is None:
            return {}
        try:
            from .douyin_identity import _read_ssr_identity  # noqa: PLC0415

            identity = _read_ssr_identity(page) or {}
        except Exception:  # noqa: BLE001 —— 抓取失败不影响建号
            logger.debug("登录页身份抓取失败", exc_info=True)
            return {}
        hint = {}
        if identity.get("user_id"):
            hint["remote_user_id"] = str(identity["user_id"])
        for src, dst in (("nickname", "nickname"), ("avatar", "avatar"), ("douyin_id", "douyin_id")):
            if identity.get(src):
                hint[dst] = str(identity[src])
        if hint:
            session.identity_hint_cache = hint
            logger.info("抖音扫码：已从页面抓到身份 user_id=%s", hint.get("remote_user_id") or "-")
        return hint

    def _promote_profile(self, session: _LoginSession, account: dict) -> None:
        """把临时 profile（负槽位）改名为真实 account_id（尽力而为）。"""
        if not session.profile_slot:
            return
        account_id = int((account or {}).get("id") or 0)
        if not account_id:
            return
        from .douyin_profile import claim_profile  # noqa: PLC0415

        # 先释放锁再改名（锁对象按 slot 建，改名后由 account_id 段接管）
        guard = session.profile_guard
        session.profile_guard = None
        if guard is not None:
            try:
                guard.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        try:
            claim_profile(session.profile_slot, account_id)
        except Exception:  # noqa: BLE001 —— 迁移失败不影响登录结果（下次打开会新建目录）
            logger.warning("抖音扫码：profile 迁移失败 slot=%s account=%s",
                           session.profile_slot, account_id, exc_info=True)
        session.profile_slot = 0

    # --------------------------------------------------------------- 诊断辅助

    def _probe_waiting(self, session: _LoginSession, names: set, login_cookie: bool):
        """等待扫码期间的定期探针：只记一行日志（15s 节流）。

        注意：不要用 page.evaluate 测「主线程响应」——抖音反调试会专门拖死
        Runtime.evaluate 通道（实测恒超时），是误导性指标；而真实事件处理
        （fill/click 的框架回调）是正常的。
        ⚠️ 这里**不再**周期性落调试截图（2026-10-15 移除）：等待期每 30s 一张
        全页截图 + page.title()，全部占用登录单线程，直接拖慢登录判定与建号。
        """
        now = time.time()
        if now - getattr(session, "probe_at", 0.0) < 15:
            return
        session.probe_at = now
        page = self._active_page(session)
        url = "-"
        if page is not None:
            try:
                url = str(page.url or "-")[:96]
            except Exception:  # noqa: BLE001
                url = "?"
        logger.info(
            "抖音扫码[%s] 等待中：url=%s cookies=%d 登录族=%s",
            session.qr_id[:8], url, len(names), login_cookie,
        )

    def _dump_debug(self, session: _LoginSession, tag: str):
        """落一张现场快照（截图 + URL/cookie 清单）到调试目录。

        **默认关闭**，只有 ``DOUYIN_LOGIN_DEBUG=1`` 时才会真正执行——见文件头
        ``LOGIN_DEBUG_ENABLED`` 的说明：这是一条很贵的诊断路径，且在单线程
        executor 上与「提交验证码 / 建号」抢同一个线程。
        """
        if not LOGIN_DEBUG_ENABLED:
            return
        page = self._active_page(session)
        if page is None:
            return
        try:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            base = os.path.join(DEBUG_DIR, f"{session.qr_id[:8]}-{tag}-{int(time.time())}")
            try:
                # 只截视口 + jpeg 压缩：诊断够用，且比全页 PNG 快一个量级
                data = page.screenshot(timeout=5_000, animations="disabled", type="jpeg", quality=60)
                with open(f"{base}.jpg", "wb") as fh:
                    fh.write(data)
            except Exception:  # noqa: BLE001
                pass
            try:
                # ⚠️ 不要调 page.title()：它走 Runtime.evaluate 且**没有超时参数**
                # （吃 Playwright 默认 30s），抖音反调试会把它拖死。要标题就取
                # 已经拿得到的 document.title（下面的 evaluate 也做超时保护）。
                info = {
                    "url": page.url,
                    "cookies": sorted(c.get("name") for c in session.context.cookies()),
                }
                with open(f"{base}.txt", "w", encoding="utf-8") as fh:
                    fh.write(repr(info))
            except Exception:  # noqa: BLE001
                pass
            logger.info("抖音扫码[%s] 调试快照已保存：%s.*", session.qr_id[:8], base)
        except Exception:  # noqa: BLE001 —— 诊断失败不影响主流程
            return

    @staticmethod
    def _purge_debug_dir() -> None:
        """清理超期调试产物（默认保留 3 天）。失败静默，绝不阻塞登录。"""
        try:
            cutoff = time.time() - DEBUG_RETENTION_DAYS * 86400
            removed = 0
            for name in os.listdir(DEBUG_DIR):
                path = os.path.join(DEBUG_DIR, name)
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError:
                    continue
            if removed:
                logger.info("抖音登录调试产物已清理 %d 个（保留 %d 天）", removed, DEBUG_RETENTION_DAYS)
        except Exception:  # noqa: BLE001
            return

    def _log_key_response(self, response):
        """上下文级响应日志：creator 域全部接口请求都记录（方法/状态/请求体/响应片段）。

        用于定位「提交无反应」：无论页面把校验提交发到哪个接口（verify/sms/mfa/
        challenge…），都能看到它是否发出、返回什么。
        """
        try:
            url = str(response.url or "")
        except Exception:  # noqa: BLE001
            return
        low = url.lower()
        if LOG_HOST not in low:
            return
        if any(skip in low for skip in (".js", ".css", ".png", ".jpg", ".jpeg", ".svg",
                                        ".woff", ".ico", "/static", "monitor")):
            return
        method = "?"
        post = ""
        status = "?"
        try:
            request = response.request
            method = request.method
            if method == "POST":
                post = str(request.post_data or "")[:200].replace("\n", " ")
        except Exception:  # noqa: BLE001
            pass
        try:
            status = response.status
        except Exception:  # noqa: BLE001
            pass
        snippet = ""
        try:
            ctype = str(response.headers.get("content-type") or "")
            if "json" in ctype:
                snippet = (response.text() or "")[:160].replace("\n", " ")
        except Exception:  # noqa: BLE001
            snippet = ""
        logger.info("[net] %s %s %s | post=%s | %s",
                    method, status, url[:120], post or "-", snippet)

    def _log_failed_request(self, request):
        """记录 creator 域失败/被中止的请求（提交无反应时可能被客户端拦截）。"""
        try:
            url = str(request.url or "")
            if LOG_HOST not in url.lower():
                return
            logger.info("[net] FAILED %s %s", url[:120], request.failure)
        except Exception:  # noqa: BLE001
            return

    def _log_request_start(self, request):
        """记录 creator 域请求起点（抓「发出但一直挂起」的请求）+ 校验请求时间戳。"""
        try:
            url = str(request.url or "")
            if LOG_HOST not in url.lower():
                return
            low = url.lower()
            if any(skip in low for skip in (".js", ".css", ".png", ".jpg", ".jpeg", ".svg",
                                            ".woff", ".ico", "/static", "monitor")):
                return
            if "passport" in low and "check_qrconnect" not in low:
                self._last_passport_req_at = time.time()
            logger.info("[net] → %s %s", request.method, url[:110])
        except Exception:  # noqa: BLE001
            return

    def _log_console(self, message):
        """记录页面 console 输出（上限截断，避免刷屏）。"""
        try:
            count = getattr(self, "_console_count", 0)
            if count >= 40:
                return
            self._console_count = count + 1
            logger.info("[console] %s %s", message.type,
                        str(message.text or "")[:160].replace("\n", " "))
        except Exception:  # noqa: BLE001
            return

    def _log_page_error(self, error):
        """记录页面 JS 未捕获异常（提交处理崩溃时会在这里出现）。"""
        try:
            logger.info("[pageerror] %s", str(error)[:200])
        except Exception:  # noqa: BLE001
            return

    def _active_page(self, session: _LoginSession):
        if session.page is not None:
            return session.page
        try:
            return session.context.pages[-1] if session.context.pages else None
        except Exception:  # noqa: BLE001
            return None

    def _handle_post_scan_verification(self, session: _LoginSession) -> None:
        """处理扫码确认后的「身份验证」短信二次验证弹窗（2026-10-09 实测形态）。

        抖音风控触发时页面弹出「身份验证：接收短信验证码 / 发送短信验证」。
        自动点「接收短信验证码」（短信发到用户手机），把会话切到
        verification_required，让用户在我们的界面输入收到的验证码。
        """
        if session.status == "verification_required":
            return
        page = self._active_page(session)
        if page is None:
            return
        try:
            receive = page.get_by_text("接收短信验证码", exact=True)
            if receive.count() == 0:
                return
        except Exception:  # noqa: BLE001
            return
        if not getattr(session, "sms_requested", False):
            try:
                receive.first.click(timeout=5_000)
                session.sms_requested = True
                logger.info("抖音扫码[%s] 已点击「接收短信验证码」", session.qr_id[:8])
            except Exception as exc:  # noqa: BLE001
                logger.info("抖音扫码[%s] 点击「接收短信验证码」失败：%s", session.qr_id[:8], exc)
        # 等验证码输入框出现（点击后弹窗切换为输入界面）
        deadline = time.time() + 8
        has_input = False
        while time.time() < deadline:
            try:
                if page.locator("input:visible").count() > 0:
                    has_input = True
                    break
            except Exception:  # noqa: BLE001
                break
            page.wait_for_timeout(400)
        session.status = "verification_required"
        session.message = (
            "抖音要求短信验证：验证码已发往你的抖音绑定手机，请输入收到的验证码"
            if has_input else
            "抖音弹出身份验证，已请求发送验证码，请稍候刷新或输入收到的验证码"
        )
        session.verification_image = self._capture_state_screenshot(page)
        self._dump_debug(session, "sms")
        logger.info("抖音扫码[%s] 进入短信验证环节（输入框=%s）", session.qr_id[:8], has_input)

    def _notice_submit_stall(self, session: _LoginSession) -> None:
        """提交验证后迟迟没有登录态：提示用户可能验证码有误（一次性）。

        正常提交→登录成功约 8~25 秒（2026-10-09 实测），30 秒仍未完成才提示，
        避免慢网络/慢渲染时误报（曾设 15s，成功案例只需 ~8s，太快了）。
        """
        submit_at = getattr(session, "submit_at", 0.0)
        if not submit_at or getattr(session, "submit_notice", False):
            return
        if time.time() - submit_at < 30:
            return
        session.submit_notice = True
        session.message = "验证码可能未通过：请使用最新一条短信的验证码重新提交"
        logger.info("抖音扫码[%s] 提交后 30s 无登录态，已提示用户重试", session.qr_id[:8])

    def _maybe_mark_verification(self, session: _LoginSession):
        """登录 cookie 出现但预检失败时，检测页面是否停留在安全验证环节。"""
        if session.status == "verification_required" and session.verification_image:
            return
        page = self._active_page(session)
        if page is None:
            return
        try:
            has_input = page.locator("input:visible").count() > 0
            hit_keyword = any(
                page.get_by_text(keyword).count() > 0
                for keyword in ("验证码", "安全验证", "请输入")
            )
        except Exception:  # noqa: BLE001 —— 页面可能已跳转或关闭
            return
        if not (has_input and hit_keyword):
            return
        session.status = "verification_required"
        session.message = "检测到安全验证：请在抖音 App 中查看验证码并输入"
        session.verification_image = self._capture_state_screenshot(page)
        logger.info("抖音扫码[%s] 进入安全验证环节", session.qr_id[:8])

    @staticmethod
    def _capture_state_screenshot(page) -> str:
        try:
            data = page.screenshot(timeout=15_000, animations="disabled", full_page=False)
            return "data:image/png;base64," + base64.b64encode(data).decode()
        except Exception:  # noqa: BLE001 —— 截图失败时前端仅显示文字提示
            return ""

    def _submit_verification_code(self, session: _LoginSession, code: str):
        page = self._active_page(session)
        if page is None:
            session.status = "error"
            session.message = "浏览器已关闭，请重新发起扫码登录"
            return
        qr8 = session.qr_id[:8]
        try:
            # 弹窗是模态层：必须精准定位到弹窗内的验证码输入框。
            # 页面背景的登录表单里也有同名占位符输入框——取所有候选中
            # 距视口中心最近者（弹窗居中、背景表单在右侧）。
            # （2026-10-09 实测教训：取「第一个可见/占位符匹配」输入框会命中
            # 背景表单 → 验证码填进背景输入框、弹窗输入框始终为空、
            # 「验证」按钮不激活 → 提交无任何反应）
            target = None
            target_hint = ""
            candidates = []
            for placeholder in ("请输入验证码", "验证码"):
                try:
                    locator = page.get_by_placeholder(placeholder)
                    count = locator.count()
                except Exception:  # noqa: BLE001
                    continue
                if count <= 0:
                    continue
                for index in range(count):
                    node = locator.nth(index)
                    try:
                        box = node.bounding_box()
                    except Exception:  # noqa: BLE001
                        box = None
                    if not box:
                        continue
                    cx = box["x"] + box["width"] / 2
                    cy = box["y"] + box["height"] / 2
                    candidates.append((cx, cy, placeholder, index, node))
                if candidates:
                    break
            if candidates:
                vx, vy = 640.0, 400.0  # 登录窗口 1280x800 的中心
                info = "；".join(
                    f"{p}#{i}@({int(cx)},{int(cy)})"
                    for cx, cy, p, i, _ in candidates
                )
                logger.info("抖音扫码[%s] 验证码输入候选：%s", qr8, info)
                candidates.sort(key=lambda item: (item[0] - vx) ** 2 + (item[1] - vy) ** 2)
                _, _, placeholder, index, target = candidates[0]
                target_hint = f"占位符:{placeholder}#{index}"
            if target is None:
                # 兜底：取最后一个可见文本输入框（弹窗在 DOM 末端、背景表单在前）
                inputs = page.locator("input:visible")
                for index in range(inputs.count() - 1, -1, -1):
                    node = inputs.nth(index)
                    input_type = (node.get_attribute("type") or "text").lower()
                    if input_type in ("text", "number", "tel"):
                        target = node
                        target_hint = f"兜底#{index}"
                        break
            if target is None:
                raise DouyinLoginError("页面上未找到验证码输入框")
            logger.info("抖音扫码[%s] 验证码填入开始（定位=%s）", qr8, target_hint)

            def _readback() -> str:
                try:
                    return str(target.input_value(timeout=5_000) or "")
                except Exception:  # noqa: BLE001
                    return ""

            # 主路径：真实键盘逐字输入（isTrusted 按键事件；兼容「需要真实按键
            # 才激活验证按钮」的受控组件——2026-10-09 实测 fill 后按钮有时仍灰）
            try:
                target.click(timeout=8_000, force=True)
                try:
                    target.press_sequentially(code, delay=110)
                except AttributeError:  # 老版本 playwright 兜底
                    target.type(code, delay=110)
            except Exception as exc:  # noqa: BLE001
                logger.info("抖音扫码[%s] 逐字输入异常(%s)，回退 fill", qr8, exc)
            value = _readback()
            if not value.strip():
                logger.info("抖音扫码[%s] 逐字输入未生效，回退 fill", qr8)
                target.fill(code, timeout=20_000)
                value = _readback()
            logger.info("抖音扫码[%s] 验证码回读=%r", qr8, value[:20])
            page.wait_for_timeout(800)  # 等受控组件激活「验证」按钮
            value_before = _readback()  # 提交前输入框状态（定位输入被清空的时刻）
            btn_state = "?"
            try:
                node = page.get_by_text("验证", exact=True)
                if node.count() > 0:
                    el = node.last
                    btn_state = (f"class={str(el.get_attribute('class'))[:58]} "
                                 f"aria-disabled={el.get_attribute('aria-disabled')}")
            except Exception:  # noqa: BLE001
                pass
            click_ts = time.time()
            page.keyboard.press("Enter")
            clicked = False
            try:
                node = page.get_by_text("验证", exact=True)
                if node.count() > 0:
                    el = node.last
                    try:
                        el.click(timeout=8_000)  # 先按正常点击（按钮启用时最可靠）
                        clicked = True
                    except Exception:  # noqa: BLE001 —— 命中判定被拖慢时退回 force
                        el.click(timeout=8_000, force=True)
                        clicked = True
            except Exception as exc:  # noqa: BLE001
                logger.info("抖音扫码[%s] 「验证」按钮点击异常：%s", qr8, exc)
            if not clicked:
                for keyword in ("验证", "提交", "确定", "确认", "下一步"):
                    try:
                        button = page.get_by_role("button", name=keyword)
                        if button.count() > 0:
                            button.first.click(timeout=8_000, force=True)
                            clicked = True
                            break
                    except Exception:  # noqa: BLE001
                        continue
            page.wait_for_timeout(1_200)
            value_after = _readback()
            if getattr(self, "_last_passport_req_at", 0.0) < click_ts:
                logger.info("抖音扫码[%s] 点击后未观察到 passport 校验请求", qr8)
            session.submit_at = time.time()
            session.submit_notice = False
            session.message = "验证码已提交，正在完成登录（约 10~30 秒），请稍候…"
            logger.info(
                "抖音扫码[%s] 验证码已提交（定位=%s，按钮命中=%s，按钮状态=%s，提交前=%r 提交后=%r）",
                qr8, target_hint, clicked, btn_state, value_before[:20], value_after[:20],
            )
            # 提交后即时快照（可能带错误提示 toast），供事后还原
            self._dump_debug(session, "aftersubmit")
        except Exception as exc:  # noqa: BLE001 —— 提交失败保留会话供重试
            session.message = f"验证码提交失败：{exc}"
            logger.info("抖音扫码[%s] 验证码提交失败：%s", qr8, exc)

    def _expire(self, session: _LoginSession):
        """超时会话作废。只处理非终态，避免把已 confirmed 的会话改回 expired。"""
        if session.status not in self._EXPIRABLE_STATUSES:
            return
        if session.status == "waiting" and session.mode == "qr":
            session.message = "二维码已过期，请重新发起"
        else:
            session.message = "登录会话已超时，请重新发起"
        session.status = "expired"
        self._close_session(session)

    def _close_session(self, session: _LoginSession):
        """只关会话自己的 context；浏览器内核留给下一次扫码复用（_ensure_browser）。

        持久 profile 场景没有可复用的共享内核：关掉上下文即释放，并交还 profile 锁。
        """
        context = session.context
        session.context = None
        session.playwright = None
        session.page = None
        try:
            if context is not None:
                context.close()
        except Exception:  # noqa: BLE001
            pass
        guard = session.profile_guard
        session.profile_guard = None
        if guard is not None:
            try:
                guard.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        # 未走完建号的临时槽位（过期/失败/用户取消）要清掉目录，避免孤儿 profile 攒磁盘
        if session.profile_slot:
            try:
                from .douyin_profile import purge_profile  # noqa: PLC0415

                purge_profile(session.profile_slot)
            except Exception:  # noqa: BLE001
                logger.debug("临时 profile 清理失败 slot=%s", session.profile_slot, exc_info=True)
            session.profile_slot = 0

    # ------------------------------------------------------- 共享浏览器（提速）
    def _ensure_browser(self, proxy=None):
        """懒加载共享浏览器内核：省掉每次扫码 3~8s 的启动开销。

        代理变更或内核已断开时重建；空闲回收由 ``maybe_close_idle_browser`` 负责。
        只能在 ``_executor`` 线程内调用（playwright 对象有线程归属）。
        """
        key = json.dumps(proxy or {}, sort_keys=True, ensure_ascii=False)
        if self._browser is not None:
            try:
                if self._browser.is_connected() and key == self._browser_proxy_key:
                    self._browser_last_used = time.time()
                    return self._browser
            except Exception:  # noqa: BLE001 —— 内核已死，重建
                logger.info("抖音扫码：共享浏览器已断开，重新启动内核")
            self._stop_browser()
        self._browser = _launch_chromium(self._ensure_playwright(), proxy)
        self._browser_proxy_key = key
        self._browser_last_used = time.time()
        logger.info("抖音扫码：浏览器内核已就绪（复用启动）")
        return self._browser

    def _stop_browser(self):
        browser, self._browser = self._browser, None
        self._browser_proxy_key = None
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                logger.debug("共享浏览器关闭失败", exc_info=True)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                logger.debug("playwright 停止失败", exc_info=True)
            self._playwright = None

    def maybe_close_idle_browser(self, idle_seconds: float = BROWSER_IDLE_SECONDS):
        """空闲回收共享浏览器（小内存服务器不能长期养着内核）；由调度循环定期调用。"""
        if self._closed:
            return
        with self._guard:
            has_session = bool(self._sessions)
        if has_session:
            return
        self._submit(self._close_idle_browser_locked, idle_seconds)

    def _close_idle_browser_locked(self, idle_seconds: float):
        if self._browser is None:
            return
        if time.time() - self._browser_last_used < idle_seconds:
            return
        logger.info("抖音扫码：共享浏览器空闲超 %.0fs，回收内核", idle_seconds)
        self._stop_browser()


def format_login_time(value: datetime) -> str:
    return value.strftime("%H:%M:%S") if value else ""
