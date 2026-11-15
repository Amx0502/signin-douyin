"""抖音 cookie 保活刷新（带旧 cookie 访问抖音，回收服务端重新下发的 cookie）。

背景（2026-10-11 调研，对照开源实现）：
抖音 cookie 里 ``sessionid`` 家族寿命约 7~30 天，而 ``ttwid`` / ``msToken`` /
``odin_tt`` 这类短命字段几天就会陈旧，一陈旧就被判未登录或触发风控——用户看到的现象
是「登录过几天 cookie 就失效」。Gitee 项目 ``fox-glaze/hl-douyin-plugin`` 的做法
（``#抖音刷新ck``）是：带旧 cookie 开一次浏览器访问抖音，把服务端重新下发的整套
cookie 收回来落盘即可续期，**只有 sessionid 真被服务端作废时才需要重新扫码**。

本模块只负责「访问 + 回收」这一段（纯函数式，便于单测）；
登录态判定与落库交给 service 层（复用 ``douyin_client.fetch_session_identity``）。
"""
from __future__ import annotations

import logging
import time

from .douyin_site import active_site

logger = logging.getLogger(__name__)

# 保活访问页随站点配置：主站（www.douyin.com + 私信页）下发 ttwid/msToken/odin_tt；
# 创作者中心模式沿用主站 + creator home（私信链路同域）。
VISIT_URLS = active_site().keepalive_urls
GOTO_TIMEOUT_MS = 20_000
# 每页停留时间：让站点的风控/埋点脚本跑完，确保 set-cookie 全部下发
SETTLE_SECONDS = 4.0

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_LAUNCH_ARGS = [
    "--no-first-run",
    "--disable-extensions",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]


class CookieRefreshError(RuntimeError):
    """浏览器基础设施不可用（playwright/内核缺失、启动失败）——不代表 cookie 失效。"""


def _cookie_key(item: dict) -> tuple[str, str, str]:
    return (
        str(item.get("domain") or ""),
        str(item.get("path") or "/"),
        str(item.get("name") or ""),
    )


def _is_alive(item: dict, now: float | None = None) -> bool:
    """过期时间已到的 cookie 不再保留（Playwright 回收时不会带上它们）。"""
    expires = item.get("expires")
    if expires in (None, "", -1, 0):
        return True
    try:
        value = float(expires)
    except (TypeError, ValueError):
        return True
    if value <= 0:
        return True
    return value > (now if now is not None else time.time())


def merge_cookies(old: list[dict], new: list[dict], *, now: float | None = None) -> list[dict]:
    """按 (domain, path, name) 合并：新集合优先，旧集合里仍有效且未被回传的保留。

    顺序沿用旧集合（前端/日志展示稳定），新出现的字段追加在末尾。
    """
    fresh = {
        _cookie_key(item): item
        for item in new or []
        if isinstance(item, dict) and item.get("name")
    }
    merged: list[dict] = []
    for item in old or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        replacement = fresh.pop(_cookie_key(item), None)
        if replacement is not None:
            merged.append(replacement)
        elif _is_alive(item, now):
            merged.append(item)
    merged.extend(fresh.values())
    return merged


def refresh_cookies(cookies: list[dict], *, account_id: int | None = None, proxy=None,
                    settle_seconds: float = SETTLE_SECONDS) -> dict:
    """带旧 cookie 访问抖音，回收新 cookie。

    传入 ``account_id`` 时走**持久 profile**（P1）：用该账号固定的浏览器用户目录打开
    持久上下文，登录态由浏览器自己写盘续命，cookie 不再是唯一凭证；跑完把 profile 里的
    cookie 读回（``cookies`` 字段），调用方 merge 后落库。

    返回 ``{"cookies": [...], "visited": [...], "failed": [...]}``；
    浏览器不可用时抛 ``CookieRefreshError``（调用方据此区分「环境问题」与「登录态失效」）。
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415 —— 可选依赖，延迟导入
    except Exception as exc:  # noqa: BLE001
        raise CookieRefreshError(f"playwright 不可用：{exc}") from exc

    from .douyin_login import BROWSER_GUARD  # noqa: PLC0415 —— 全局浏览器锁

    visited: list[str] = []
    failed: list[str] = []
    if account_id is not None:
        return _refresh_with_profile(
            cookies, account_id=int(account_id), proxy=proxy, settle_seconds=settle_seconds,
            sync_playwright=sync_playwright, guard=BROWSER_GUARD,
        )

    from .douyin_login import _launch_chromium  # noqa: PLC0415 —— 复用内核降级逻辑

    with BROWSER_GUARD:
        with sync_playwright() as p:
            try:
                browser = _launch_chromium(p, proxy)
            except Exception as exc:  # noqa: BLE001
                raise CookieRefreshError(f"chromium 启动失败：{exc}") from exc
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800}, user_agent=BROWSER_UA)
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                # add_cookies 只接受 name/value/domain/path（带 sameSite=None 时要求 secure，
                # 注入阶段省略这些属性最稳；站点重新下发时会带回完整属性）
                context.add_cookies(_pw_cookie_items(cookies))
                page = context.new_page()
                for url in VISIT_URLS:
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
                        page.wait_for_timeout(int(settle_seconds * 1000))
                        visited.append(url)
                    except Exception as exc:  # noqa: BLE001 —— 单页失败不致命，继续下一站
                        logger.warning("cookie 保活访问失败 url=%s err=%s", url, exc)
                        failed.append(url)
                fresh = context.cookies()
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    logger.debug("保活浏览器关闭失败", exc_info=True)
    return {
        "cookies": merge_cookies(cookies, fresh),
        "visited": visited,
        "failed": failed,
    }


def _pw_cookie_items(cookies) -> list[dict]:
    """账号 cookie → Playwright add_cookies 形态（只留 name/value/domain/path）。"""
    return [
        {
            "name": str(item["name"]),
            "value": str(item.get("value") or ""),
            "domain": str(item.get("domain") or ".douyin.com"),
            "path": str(item.get("path") or "/"),
        }
        for item in cookies or []
        if isinstance(item, dict) and item.get("name")
    ]


def _refresh_with_profile(cookies, *, account_id: int, proxy, settle_seconds: float,
                          sync_playwright, guard) -> dict:
    """持久 profile 版保活：同账号固定 user_data_dir，跑完把 cookie 读回。

    顺带抓取身份（主站链路）：访问页面时页面自身会请求 ``profile/self``，
    我们从响应/SSR 数据里读一次登录用户信息，供调用方判定登录态 —— 零额外开销。
    """
    from .douyin_profile import open_account_context, profile_guard  # noqa: PLC0415

    visited: list[str] = []
    failed: list[str] = []
    identity: dict = {}
    try:
        with guard, profile_guard(account_id):
            with sync_playwright() as p:
                try:
                    context = open_account_context(
                        p, account_id, cookies=cookies, proxy=proxy, headless=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    raise CookieRefreshError(f"chromium 启动失败：{exc}") from exc
                try:
                    context.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                    )
                    _hook_identity_capture(context)
                    page = context.new_page()
                    for url in VISIT_URLS:
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
                            page.wait_for_timeout(int(settle_seconds * 1000))
                            visited.append(url)
                        except Exception as exc:  # noqa: BLE001 —— 单页失败不致命
                            logger.warning("cookie 保活访问失败 url=%s err=%s", url, exc)
                            failed.append(url)
                    identity = _collect_identity(page)
                    fresh = context.cookies()
                finally:
                    try:
                        context.close()
                    except Exception:  # noqa: BLE001
                        logger.debug("保活持久上下文关闭失败", exc_info=True)
    except CookieRefreshError:
        raise
    except RuntimeError as exc:
        # profile 被占用等：属基础设施问题，不动登录态
        raise CookieRefreshError(f"持久 profile 不可用：{exc}") from exc
    return {
        "cookies": merge_cookies(cookies, fresh),
        "visited": visited,
        "failed": failed,
        "identity": identity,
    }


# 页面自身会请求的「自己资料」接口（主站），从响应里读登录用户信息
_SELF_PROFILE_PATH = "/aweme/v1/web/user/profile/self/"


def _hook_identity_capture(context) -> None:
    """挂响应监听：捕获页面自身触发的 profile/self 响应（失败静默）。"""
    def _on_response(response):
        try:
            if _SELF_PROFILE_PATH not in str(response.url or ""):
                return
            payload = response.json()
        except Exception:  # noqa: BLE001 —— 非 JSON / 页面跳转中
            return
        if ((payload or {}).get("user") or {}).get("uid"):
            context.__dict__.setdefault("_dy_identity_payload", payload)
            context._dy_identity_payload = payload

    try:
        context.on("response", _on_response)
    except Exception:  # noqa: BLE001
        logger.debug("身份响应监听注册失败", exc_info=True)


def _collect_identity(page) -> dict:
    """从响应缓存/SSR 数据里取登录身份（尽力而为，失败返回空 dict）。"""
    try:
        from .douyin_identity import _profile_from_self_payload, _read_ssr_identity  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    payload = None
    ctx = getattr(page, "context", None)
    if ctx is not None:
        payload = getattr(ctx, "_dy_identity_payload", None)
    if payload:
        return _profile_from_self_payload(payload)
    try:
        return _read_ssr_identity(page) or {}
    except Exception:  # noqa: BLE001
        return {}
