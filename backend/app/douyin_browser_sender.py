"""抖音续火——浏览器发送通道（协议通道被平台拦截时的兜底）。

背景（2026-10-09 线上实测）：
协议栈（逆向 BytedIM，protocol_sender.mjs）发送被服务端安全校验拦截：
``statusCode=3 CheckMessageNotPass``（``checkMsg.status_code=8610``）；同一账号、
同一会话、同样内容用真实浏览器打开 creator 私信管理页可正常发送。
因此发送链路增加浏览器通道：Playwright 打开私信页 → 点击目标会话 →
输入内容 → Enter → 以「聊天区出现该消息」确认送达。

约定：
- 返回结构与 ``douyin_client.run_protocol_send`` 对齐（sent 条目额外带 ``channel``
  字段），供 ``douyin_service`` 合并；
- 本模块同步实现，须在无 asyncio 事件循环的线程中调用（服务层线程池已满足）；
- 只有「浏览器基础设施不可用」（playwright/浏览器缺失、启动失败）抛
  ``BrowserSendError``；页面级失败记入各目标的失败结果。
"""
from __future__ import annotations

import base64
import logging
import random
import time

from .douyin_site import active_site

logger = logging.getLogger(__name__)

# 私信页随站点配置：主站 https://www.douyin.com/chat；creator 为回退
CHAT_URL = active_site().chat_url
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 服务器渲染慢（实测会话列表 ~20s 出现），给足余量
LIST_RENDER_TIMEOUT = 45
# 会话列表轮询粒度：2.5s → 0.8s（列表一出现就能尽早开始发送）
LIST_POLL_INTERVAL_MS = 800
INPUT_WAIT_TIMEOUT = 15
# 点开会话后等聊天输入框（取代固定 sleep 3s）
CHAT_READY_TIMEOUT = 6
# 气泡渲染可能明显滞后于实际发出（线上实测 >18s 仍会渲染出来），给足确认窗口
SEND_CONFIRM_TIMEOUT = 35
# 发送确认轮询粒度
SEND_CONFIRM_POLL_MS = 900

# 聊天区（右侧）粗略左边界：排除左侧会话列表（预览文本会干扰命中判定）
CHAT_AREA_MIN_X = 300
# 「陌生人私信」分类的候选文案（不同站点/版本略有差异）
STRANGER_TABS = ("陌生人私信", "陌生人消息", "陌生消息")

_LAUNCH_ARGS = [
    "--no-first-run",
    "--disable-extensions",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]


class BrowserSendError(Exception):
    """浏览器通道基础设施不可用（playwright/浏览器缺失、启动失败）。"""


def _to_pw_cookies(cookies) -> list[dict]:
    items: list[dict] = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict) or not cookie.get("name"):
            continue
        items.append({
            "name": str(cookie["name"]),
            "value": str(cookie.get("value") or ""),
            "domain": str(cookie.get("domain") or ".douyin.com"),
            "path": str(cookie.get("path") or "/"),
        })
    return items


def _entry(target: str, message: str, success: bool, *, status_name: str, reason: str = "") -> dict:
    return {
        "target": target,
        "message": message,
        "success": success,
        "statusCode": 0 if success else None,
        "statusName": status_name,
        # summarize_status 优先取 statusMsg 作为展示原因（statusName 仅内部标记）
        "statusMsg": reason,
        "reason": reason,
        "dryRun": False,
        "channel": "browser",
    }


def _candidates(target: str, aliases=None) -> list[str]:
    """主名 + 别名（去重保序）：好友改名后仍能命中。"""
    names: list[str] = []
    for item in [target, *(aliases or [])]:
        text = str(item or "").strip()
        if text and text not in names:
            names.append(text)
    return names


def _visible_text_node(page, name: str):
    """定位「可见」的同名文本节点，返回 ``(node_locator, count)``。

    抖音私信页一个昵称常对应多个 DOM 节点（折叠面板、预渲染骨架、聊天区标题、
    消息预览），其中一部分是隐藏的——直接 ``.first`` 会命中不可见节点并卡在点击。
    多个可见时取最左侧：会话列表在左，可避开聊天区/预览里的同名文本。
    """
    try:
        locator = page.get_by_text(name, exact=False)
        count = locator.count()
    except Exception:  # noqa: BLE001
        return None, 0
    picks: list[tuple[float, object]] = []
    for index in range(min(count, 8)):
        node = locator.nth(index)
        try:
            if not node.is_visible():
                continue
            box = node.bounding_box()
        except Exception:  # noqa: BLE001
            continue
        if box:
            picks.append((float(box.get("x") or 0.0), node))
    if not picks:
        return None, count
    picks.sort(key=lambda item: item[0])
    return picks[0][1], count


def _click_node(node, timeout_ms: int = 8000) -> None:
    """点击节点：滚动到可视 → 普通点击 → force → dispatch 事件，逐级降级。"""
    try:
        node.scroll_into_view_if_needed(timeout=2000)
    except Exception:  # noqa: BLE001
        pass
    try:
        node.click(timeout=timeout_ms)
        return
    except Exception:  # noqa: BLE001 —— 被遮挡/不稳定，退一步用 force
        pass
    try:
        node.click(timeout=timeout_ms, force=True)
        return
    except Exception:  # noqa: BLE001
        pass
    node.dispatch_event("click")


def _wait_targets(page, targets: list[str], timeout: float, aliases_by_target=None) -> dict:
    """轮询等待会话列表出现全部目标（含别名），返回 ``{target: 命中的名字}``。"""
    aliases_by_target = aliases_by_target or {}
    found: dict[str, str] = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        for target in targets:
            if target in found:
                continue
            for name in _candidates(target, aliases_by_target.get(target)):
                try:
                    node, _count = _visible_text_node(page, name)
                    if node is not None:
                        found[target] = name
                        break
                except Exception:  # noqa: BLE001
                    continue
        if len(found) == len(targets):
            break
        page.wait_for_timeout(LIST_POLL_INTERVAL_MS)
    return found


def _wait_chat_ready(page, timeout: float = CHAT_READY_TIMEOUT):
    """点开会话后等消息输入框就绪（条件等待，取代固定 sleep 3s）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        edit = _chat_input(page)
        if edit is not None:
            return edit
        page.wait_for_timeout(300)
    return None


def _wait_text(page, target: str, timeout: float = CHAT_READY_TIMEOUT) -> bool:
    """等待某个文本出现在页面上（用于「陌生人私信」分类切换）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            node, _count = _visible_text_node(page, target)
            if node is not None:
                return True
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(300)
    return False


def _type_message(page, edit, message: str) -> None:
    """逐字输入消息（真人节奏），多级降级兼容各种输入框。

    富文本编辑器（ProseMirror/Slate 类）对 ``fill()`` 的一次性赋值常不触发
    框架事件，导致「输入框有字但发送无反应」；逐字键入可规避。
    """
    delay = int(random.uniform(25, 60))
    try:
        edit.type(message, delay=delay)
        return
    except Exception:  # noqa: BLE001 —— 退到键盘级输入
        pass
    try:
        page.keyboard.type(message, delay=delay)
        return
    except Exception:  # noqa: BLE001 —— 最后退到一次性赋值
        pass
    edit.fill(message)


def _chat_input(page):
    """会话页底部的消息输入框（取纵坐标最大者，排除顶部搜索框）。"""
    candidates = page.locator("textarea, [contenteditable='true'], input[type='text']")
    best = None
    best_y = -1.0
    for index in range(candidates.count()):
        node = candidates.nth(index)
        try:
            if not node.is_visible():
                continue
            box = node.bounding_box()
        except Exception:  # noqa: BLE001
            continue
        if not box:
            continue
        if box["y"] > best_y:
            best_y = box["y"]
            best = node
    return best


def _norm_text(value) -> str:
    """压平所有空白（空格/换行）并剔除零宽字符，用于比较输入框残留内容。

    主站富文本编辑器会留 ``\\u200b`` 占位符：不过滤会让「输入框已清空」判定失真。
    """
    text = str(value or "")
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        text = text.replace(ch, "")
    return "".join(text.split())


def _input_text(node) -> str:
    """读输入框当前文本（input → input_value，contenteditable → text_content）。"""
    for attr in ("input_value", "text_content"):
        try:
            value = getattr(node, attr)()
        except Exception:  # noqa: BLE001
            continue
        if value:
            return value
    return ""


def _input_emptied(page, message: str) -> bool:
    """发送后输入框是否已不再保留原消息（被清空 = 消息已被页面接受发出）。"""
    edit = _chat_input(page)
    if edit is None:
        return False
    return _norm_text(_input_text(edit)) != _norm_text(message)


def _message_visible_in_chat(page, message: str) -> bool:
    """消息气泡是否出现在聊天区（x > 左边栏），排除左侧会话列表预览。"""
    try:
        bubbles = page.get_by_text(message, exact=False)
        count = bubbles.count()
    except Exception:  # noqa: BLE001
        return False
    for index in range(count):
        try:
            box = bubbles.nth(index).bounding_box()
        except Exception:  # noqa: BLE001
            continue
        if box and box.get("x", 0) >= CHAT_AREA_MIN_X and box.get("y", 0) >= 90:
            return True
    return False


def _capture_shot(page) -> str:
    """当前页面截图（JPEG base64），作为发送回执证据；失败返回空串不拖垮发送。"""
    try:
        raw = page.screenshot(type="jpeg", quality=60)
        return base64.b64encode(raw).decode("ascii")
    except Exception:  # noqa: BLE001
        logger.exception("浏览器通道截图失败")
        return ""


def _send_one(page, target: str, message: str, *, aliases=None) -> dict:
    """点开会话 → 输入 → 发送 → 确认。返回 sent 条目（命中别名时带 matched_name）。"""
    def done(success: bool, status_name: str, reason: str = "") -> dict:
        entry = _entry(target, message, success, status_name=status_name, reason=reason)
        entry["matched"] = matched
        return entry

    names = _candidates(target, aliases)
    node, _count = _visible_text_node(page, names[0])
    matched = names[0] if node is not None else ""
    if not matched:
        # 主名没命中：按顺序试别名（好友改名场景）
        for name in names[1:]:
            candidate, _c = _visible_text_node(page, name)
            if candidate is not None:
                node, matched = candidate, name
                break
    if not matched:
        # 对方未互关时可能收在「陌生人私信」分类（文案随站点/版本略有差异）
        try:
            tab = None
            for label in STRANGER_TABS:
                tab, _t = _visible_text_node(page, label)
                if tab is not None:
                    break
            if tab is not None:
                _click_node(tab, timeout_ms=5000)
                for name in names:
                    if _wait_text(page, name, CHAT_READY_TIMEOUT):
                        candidate, _c = _visible_text_node(page, name)
                        if candidate is not None:
                            node, matched = candidate, name
                            break
        except Exception:  # noqa: BLE001
            pass
    if node is None or not matched:
        return done(False, "target_not_found", "会话列表中未找到该好友（先互相聊过一句再试）")

    try:
        _click_node(node)
    except Exception as exc:  # noqa: BLE001
        return done(False, "click_failed", f"点击会话失败：{exc}")

    edit = _wait_chat_ready(page)
    deadline = time.time() + INPUT_WAIT_TIMEOUT
    while edit is None and time.time() < deadline:
        edit = _chat_input(page)
        if edit is None:
            page.wait_for_timeout(500)
    if edit is None:
        return done(False, "input_not_found", "会话页未找到消息输入框")

    try:
        edit.click(timeout=5000)
        # 逐字键入更贴近真人输入：富文本编辑器（ProseMirror 类）对 fill() 的
        # 一次性赋值时常不触发框架事件，导致「输入框有字但发送键无反应」
        _type_message(page, edit, message)
        page.wait_for_timeout(500)
        page.keyboard.press("Enter")
    except Exception as exc:  # noqa: BLE001
        # 退一步 ①：焦点不稳定时再敲一次回车（主站 www.douyin.com/chat 用 Enter 发送）
        clicked = False
        try:
            page.keyboard.press("Enter")
            page.wait_for_timeout(400)
            clicked = not _norm_text(_input_text(edit))
        except Exception:  # noqa: BLE001
            pass
        # 退一步 ②：文案/图标发送按钮（部分站点形态）
        if not clicked:
            for selector in ('text="发送"', 'text="Send"', '[class*="sendBtn"]',
                             '[class*="send-btn"]', '[class*="sendButton"]'):
                try:
                    button = page.locator(selector).last
                    if button.count() == 0 or not button.bounding_box():
                        continue
                    button.click(timeout=4000)
                    clicked = True
                    break
                except Exception:  # noqa: BLE001
                    continue
        if not clicked:
            return done(False, "send_action_failed", f"发送操作失败：{exc}")

    # 确认：聊天区出现该消息；输入框被清空是强辅助信号（消息已被页面接受）；
    # 出现「发送失败」字样则明确判失败
    input_emptied = False
    deadline = time.time() + SEND_CONFIRM_TIMEOUT
    while time.time() < deadline:
        page.wait_for_timeout(SEND_CONFIRM_POLL_MS)
        if _message_visible_in_chat(page, message):
            return done(True, "SucceededByBrowser")
        if not input_emptied and _input_emptied(page, message):
            input_emptied = True
        try:
            if page.get_by_text("发送失败", exact=False).count() > 0:
                return done(False, "send_failed", "页面提示发送失败（可能触发风控，建议稍后再试）")
        except Exception:  # noqa: BLE001
            pass
    if input_emptied:
        # 气泡渲染滞后时（线上实测可超 18s），只要输入框已清空且无失败提示就判成功
        return done(True, "SucceededByBrowser")
    return done(False, "send_unconfirmed", "发送后既未见消息气泡、输入框也未被清空（可能被平台拦截）")


def send_via_browser(
    cookies,
    messages_by_target: dict[str, str],
    *,
    account_id: int | None = None,
    proxy=None,
    gap_seconds: tuple[float, float] = (1.5, 3.0),
    aliases_by_target: dict[str, list[str]] | None = None,
) -> dict:
    """用浏览器通道给多个目标发送消息。

    参数：
        cookies: 账号 cookie 列表（dict 项，含 name/value[/domain/path]）
        messages_by_target: ``{目标昵称: 消息文本}``
        account_id: 传入则走**持久 profile**（P1）——同账号固定浏览器用户目录，
            登录态与设备指纹一起续命；为 None 时退回一次性内存上下文（兼容旧调用）
        aliases_by_target: ``{目标昵称: [别名...]}``，好友改名时的候选名

    返回：``{ok, dryRun, sent[], unresolved[], protocol_targets_cache, channel}``
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415 —— 可选依赖，延迟导入
    except Exception as exc:  # noqa: BLE001
        raise BrowserSendError(f"playwright 不可用：{exc}") from exc

    targets = [str(t) for t in messages_by_target.keys()]
    sent: list[dict] = []
    if not targets:
        return {"ok": True, "dryRun": False, "sent": [], "unresolved": [],
                "protocol_targets_cache": None, "channel": "browser"}

    pw_cookies = _to_pw_cookies(cookies)
    logger.info("浏览器通道：启动 chromium，目标 %s（持久 profile=%s）",
                targets, bool(account_id))
    try:
        with sync_playwright() as p:
            opened = _open_context(p, account_id=account_id, cookies=pw_cookies, proxy=proxy)
            context = opened["context"]
            try:
                page = context.new_page()
                page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30_000)

                found = _wait_targets(page, targets, LIST_RENDER_TIMEOUT, aliases_by_target)
                for target in targets:
                    if target not in found:
                        entry = _entry(target, messages_by_target[target], False,
                                       status_name="target_not_found",
                                       reason="会话列表中未找到该好友（含别名，先互相聊过一句再试）")
                        entry["matched"] = ""
                        sent.append(entry)
                        continue
                    try:
                        entry = _send_one(page, target, messages_by_target[target],
                                          aliases=(aliases_by_target or {}).get(target))
                        # 会话打开过的都截图留证（target_not_found 时聊天区尚未打开，无证据价值）
                        if entry.get("statusName") != "target_not_found":
                            shot = _capture_shot(page)
                            if shot:
                                entry["screenshot"] = shot
                        sent.append(entry)
                    except Exception as exc:  # noqa: BLE001 —— 单目标失败不拖垮其余
                        logger.exception("浏览器通道发送异常 target=%s", target)
                        entry = _entry(target, messages_by_target[target], False,
                                       status_name="browser_exception",
                                       reason=f"浏览器发送异常：{exc}")
                        entry["matched"] = str(found.get(target) or "")
                        sent.append(entry)
                    page.wait_for_timeout(int(random.uniform(*gap_seconds) * 1000))
            finally:
                opened["close"]()
    except BrowserSendError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 页面/会话级失败，记入结果不抛
        logger.exception("浏览器通道页面级失败")
        done = {str(item.get("target")) for item in sent}
        for target in targets:
            if target not in done:
                sent.append(_entry(target, messages_by_target[target], False,
                                   status_name="browser_page_error",
                                   reason=f"浏览器页面异常：{exc}"))

    ok = bool(sent) and all(item.get("success") for item in sent)
    logger.info("浏览器通道完成：%s", [(item["target"], item["success"]) for item in sent])
    return {
        "ok": ok,
        "dryRun": False,
        "sent": sent,
        "unresolved": [],
        "protocol_targets_cache": None,
        "channel": "browser",
    }


def _open_context(p, *, account_id, cookies, proxy) -> dict:
    """打开发送用的浏览器上下文：有 account_id 走持久 profile，否则退回内存上下文。

    返回 ``{"context": context, "close": callable}``；持久上下文 close 即释放内核，
    一次性上下文需额外关 browser。
    """
    if account_id:
        from .douyin_profile import open_account_context, profile_guard  # noqa: PLC0415

        held = profile_guard(int(account_id))
        held.__enter__()
        try:
            context = open_account_context(
                p, int(account_id), cookies=cookies, proxy=proxy, headless=True,
            )
        except Exception:
            held.__exit__(None, None, None)
            raise
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        def _close():
            try:
                context.close()
            finally:
                held.__exit__(None, None, None)

        return {"context": context, "close": _close}

    try:
        browser = p.chromium.launch(headless=True, args=list(_LAUNCH_ARGS))
    except Exception as exc:  # noqa: BLE001
        raise BrowserSendError(f"chromium 启动失败：{exc}") from exc
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=BROWSER_UA,
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    context.add_cookies(cookies)

    def _close_memory():
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            logger.debug("发送浏览器关闭失败", exc_info=True)

    return {"context": context, "close": _close_memory}
