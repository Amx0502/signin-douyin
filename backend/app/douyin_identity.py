"""抖音身份解析：统一 creator（接口）与主站（接口优先 + 浏览器兜底）两条链路。

背景（2026-10-13）：
- **创作者中心**：``/aweme/v1/creator/im/user_token/`` 零签名返回 user_id，直接 ``requests`` 调用；
- **主站 www.douyin.com**：绝大多数 JSON 接口带 ``a_bogus`` 签名，但
  ``/passport/account/info/v2/`` 是**零签名**的（cookie 鉴权），可直接判定登录态并
  拿到 user_id/sec_user_id/昵称/头像 —— 校验主链路改为走它（1s 内、不触发风控）。
  仅当该接口不可用时才回退浏览器：打开页面监听 ``/aweme/v1/web/user/profile/self/``
  响应（含 uid/昵称/头像），仍拿不到再回退页面 SSR 数据。
  ⚠️ 浏览器兜底**不可用首页** ``site.home_url``：服务器 IP 访问首页会被风控拦到
  验证码中间页（``title: 验证码中间页``），页面不渲染登录态 → 等满超时后误报。

统一出口 ``resolve_identity()`` 返回：
``{ok, logged_in, user_id, sec_uid, nickname, douyin_id, avatar, source, message}``
"""
from __future__ import annotations

import json
import logging
import time

from .douyin_client import (
    DouyinRemoteError,
    fetch_passport_identity,
    fetch_self_profile,
    fetch_session_identity,
)
from .douyin_site import active_site

logger = logging.getLogger(__name__)

# 主站「自己资料」接口（浏览器内由页面触发；我们从响应里取数据）
SELF_PROFILE_PATH = "/aweme/v1/web/user/profile/self/"
# 打开首页后等待自己资料接口的最长时间（服务器渲染慢，给足余量）
VERIFY_TIMEOUT_SECONDS = 30

_EMPTY = {
    "logged_in": False,
    "user_id": "",
    "sec_uid": "",
    "nickname": "",
    "douyin_id": "",
    "avatar": "",
    "source": "none",
}


def resolve_identity(cookies, *, account_id: int | None = None, proxy=None,
                     deep: bool = True) -> dict:
    """统一身份解析入口。

    - ``deep=True``：creator 走接口（user_token + passport 资料）；web 走浏览器并抓完整资料。
    - ``deep=False``：只判定登录态（web 只监听 profile/self，不做 SSR 兜底）。

    **主站链路（2026-10-13 改）**：优先走零签名 passport 接口（``_via_passport``，
    1s 内返回、不触发风控）；仅当该接口不可用（网络异常/未配置）时才回退浏览器。
    """
    site = active_site()
    if site.key == "creator":
        return _via_creator(cookies, proxy, deep=deep)
    if site.passport_info_url:
        quick = _via_passport(cookies, proxy)
        if quick is not None:
            return quick
    return _via_browser(cookies, account_id=account_id, proxy=proxy, deep=deep)


def _via_passport(cookies, proxy) -> dict | None:
    """主站快速校验：零签名 passport 接口。

    返回统一身份结构；接口**不可用**（网络异常/返回非 JSON）返回 ``None``，
    由调用方回退浏览器链路。cookie 明确失效（error_code=13）返回登出结构。
    """
    try:
        result = fetch_passport_identity(cookies, proxy=proxy)
    except DouyinRemoteError as exc:
        # retryable=True（网络类）→ 回退浏览器；auth_failed（配置类）→ 视为不可用
        logger.info("主站 passport 校验不可用，回退浏览器链路：%s", exc)
        return None
    if not result.get("logged_in"):
        return {
            **_EMPTY,
            "message": str(result.get("message") or "登录态已失效，请重新扫码"),
            "auth_failed": True,
        }
    # passport 接口不返回抖音号（unique_id），deep 时用主页接口补全
    return {
        "ok": True,
        "logged_in": True,
        "user_id": str(result.get("user_id") or ""),
        "sec_uid": str(result.get("sec_uid") or ""),
        "nickname": str(result.get("nickname") or "").strip(),
        "douyin_id": "",
        "avatar": str(result.get("avatar") or "").strip(),
        "source": "passport_api",
        "message": "",
    }


def _via_creator(cookies, proxy, *, deep: bool) -> dict:
    """创作者中心链路：接口级预检 + 资料补全。"""
    try:
        identity = fetch_session_identity(cookies, proxy)
    except DouyinRemoteError as exc:
        return {**_EMPTY, "message": str(exc), "auth_failed": bool(exc.auth_failed)}
    profile = fetch_self_profile(cookies, proxy) if deep else {}
    return {
        "ok": True,
        "logged_in": True,
        "user_id": str(identity.get("user_id") or ""),
        "sec_uid": str(profile.get("sec_uid") or ""),
        "nickname": str(profile.get("nickname") or ""),
        "douyin_id": str(profile.get("douyin_id") or ""),
        "avatar": str(profile.get("avatar") or ""),
        "source": "creator_api",
        "message": "",
    }


def _via_browser(cookies=None, *, account_id: int | None, proxy, deep: bool) -> dict:
    """主站浏览器兜底：打开页面从响应/SSR 数据解析身份（passport 接口不可用时才走）。

    ⚠️ 打开的是 ``/user/self``（「我的」页）而**不是** ``site.home_url`` 首页：
    2026-10-13 实测服务器 IP 访问首页会被风控拦到「验证码中间页」
    （title=验证码中间页、cookie 被清到 26 条、profile/self 不触发），而
    ``/user/self`` 正常渲染（title=抖音-记录美好生活、cookie 完整 50 条）。

    ``cookies`` 必须注入：cookie 导入的账号其持久 profile 里可能没有登录态，
    不注入会打开未登录页面 → 等满超时后误报「cookie 可能已失效」。
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415 —— 可选依赖
    except Exception as exc:  # noqa: BLE001
        return {**_EMPTY, "message": f"playwright 不可用：{exc}"}

    from .douyin_login import BROWSER_GUARD  # noqa: PLC0415
    from .douyin_profile import open_account_context, profile_guard  # noqa: PLC0415

    site = active_site()
    result = dict(_EMPTY)
    captured: dict = {}

    def _on_response(response):
        try:
            url = str(response.url or "")
        except Exception:  # noqa: BLE001
            return
        if SELF_PROFILE_PATH not in url:
            return
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            return
        user = (payload or {}).get("user") or {}
        if user.get("uid"):
            captured["payload"] = payload

    slot = account_id if account_id else -999
    holder = None
    try:
        holder = profile_guard(int(slot))
        holder.__enter__()
    except RuntimeError as exc:
        return {**_EMPTY, "message": f"浏览器 profile 被占用：{exc}"}
    try:
        with BROWSER_GUARD:
            with sync_playwright() as p:
                try:
                    context = open_account_context(
                        p, int(slot), cookies=cookies, proxy=proxy, headless=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    return {**_EMPTY, "message": f"浏览器启动失败：{exc}"}
                try:
                    context.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                    )
                    context.on("response", _on_response)
                    page = context.new_page()
                    # 用 /user/self（「我的」页）而非首页：首页在服务器 IP 上被风控拦截
                    verify_url = f"{site.origin}/user/self"
                    try:
                        page.goto(verify_url, wait_until="domcontentloaded", timeout=30_000)
                    except Exception as exc:  # noqa: BLE001 —— 超时不致命，继续从 SSR 取
                        logger.info("主站校验页加载异常：%s", exc)
                    deadline = time.time() + VERIFY_TIMEOUT_SECONDS
                    while time.time() < deadline and not captured.get("payload"):
                        page.wait_for_timeout(600)
                    payload = captured.get("payload") or {}
                    if payload:
                        result.update(_profile_from_self_payload(payload))
                    elif deep:
                        ssr = _read_ssr_identity(page)
                        if ssr:
                            result.update(ssr)
                finally:
                    try:
                        context.close()
                    except Exception:  # noqa: BLE001
                        logger.debug("主站验证上下文关闭失败", exc_info=True)
    finally:
        if holder is not None:
            try:
                holder.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    if not result["user_id"]:
        result["message"] = result.get("message") or "页面未返回登录用户信息（cookie 可能已失效）"
        result["auth_failed"] = True
    return result


def _profile_from_self_payload(payload: dict) -> dict:
    """``/aweme/v1/web/user/profile/self/`` 响应 → 统一身份结构。"""
    user = (payload or {}).get("user") or {}
    avatar = ((user.get("avatar_larger") or {}).get("url_list") or []) or []
    return {
        "ok": True,
        "logged_in": True,
        "user_id": str(user.get("uid") or ""),
        "sec_uid": str(user.get("sec_uid") or ""),
        "nickname": str(user.get("nickname") or "").strip(),
        "douyin_id": str(user.get("unique_id") or user.get("short_id") or "").strip(),
        "avatar": str(avatar[-1] if avatar else "").strip(),
        "source": "self_profile",
        "message": "",
    }


def _read_ssr_identity(page) -> dict:
    """兜底：从页面 SSR 数据里找当前用户 uid（主站常见 ``_ROUTER_DATA`` 等）。"""
    script = """
    () => {
      const keys = ['_ROUTER_DATA', '__INITIAL_STATE__', '_SSR_DATA_', 'RENDER_DATA'];
      const out = {};
      for (const k of keys) {
        try {
          const v = window[k];
          if (v) out[k] = JSON.stringify(v).slice(0, 300000);
        } catch (e) { /* 忽略不可序列化项 */ }
      }
      return out;
    }
    """
    try:
        data = page.evaluate(script) or {}
    except Exception:  # noqa: BLE001
        return {}
    for key, text in data.items():
        try:
            payload = json.loads(text)
        except Exception:  # noqa: BLE001
            continue
        user = _deep_find_user(payload)
        if user and user.get("uid"):
            logger.info("主站身份：从 %s 的 SSR 数据解析到 uid", key)
            return _profile_from_self_payload({"user": user})
    return {}


def _deep_find_user(node, depth: int = 0):
    """在嵌套结构里找形如用户对象的节点（含 uid + nickname）。"""
    if depth > 6 or node is None:
        return None
    if isinstance(node, dict):
        if node.get("uid") and ("nickname" in node or "sec_uid" in node):
            return node
        for value in node.values():
            hit = _deep_find_user(value, depth + 1)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for item in node[:40]:
            hit = _deep_find_user(item, depth + 1)
            if hit is not None:
                return hit
    return None
