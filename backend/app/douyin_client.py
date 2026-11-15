"""抖音续火平台客户端。

发送主通道：protocol_sender.mjs（Node vm 沙箱加载抖音创作者中心官方 IM SDK
bundle，sha256 强校验后缓存于数据目录 .im_sdk_cache/），无浏览器、无签名依赖。
Cookie 解析移植自 douyin-sparkflow core/cookies.py；消息构建移植自
core/msg_builder.py（模板变体 + [API] 一言 + [data] 日期 + [weekday] 星期）。
Cookie 是凭据：任何日志/回显都不得携带 cookie 值。
"""

import json
import logging
import os
import random
import shutil
import subprocess
from datetime import date
from pathlib import Path

import requests

from .douyin_site import active_site

logger = logging.getLogger(__name__)

AUTH_COOKIE_NAMES = frozenset({
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "uid_tt",
    "uid_tt_ss",
})

DEFAULT_COOKIE_DOMAIN = ".douyin.com"
DEFAULT_COOKIE_PATH = "/"
_MAX_INPUT_LENGTH = 200_000
_MAX_COOKIES = 200
_PAIR_RE = None  # 延迟编译见 _pair_regex

ASSETS_DIR = Path(__file__).resolve().parent / "douyin_assets"
PROTOCOL_SCRIPT = ASSETS_DIR / "protocol_sender.mjs"
DEFAULT_PROTOCOL_TIMEOUT_SECONDS = 600
# 拉取好友只读会话列表 + 逐个补昵称，比发送轻量
DEFAULT_FRIENDS_TIMEOUT_SECONDS = 300

CREATOR_CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 发送结果 statusCode → 中文原因（douyin-sparkflow SEND_MESSAGE_STATUS_NAMES）
SEND_STATUS_REASONS = {
    0: "发送成功",
    1: "对方不是你的好友或已解除关系",
    2: "会话校验未通过，请检查目标是否仍可私信",
    3: "内容被抖音安全校验拦截，建议改用自然口语短句，避免「火花 / 续火 / 打卡」等词与固定句式",
    4: "内容被拦截（仅自己可见），多为敏感词或模板痕迹过重，建议换成真人日常对话文案",
    5: "对方已把你拉黑或加入黑名单",
}

# 内置默认模板池。
# 抗风控要点：像真人随手发的短句，长度参差、口语化、不带"火花/续火/打卡/祝福"这类高频灰产词，
# 也不要出现日期/来源后缀这类明显的模板痕迹（status 3/4 就是内容被判机器群发）。
DEFAULT_MESSAGE_VARIANTS = [
    "[API]",
    "在吗",
    "最近咋样呀",
    "好久没聊了，最近忙不忙",
    "干嘛呢",
    "今天过得怎么样",
    "刚忙完，歇一会儿",
    "哈哈哈哈哈",
    "吃饭了没",
    "我这边刚下班，你那边呢",
    "突然想起你了，在忙吗",
    "睡了吗",
]

# 旧版内置池（含"续火/火花/打卡"等高风险用词）：仅用于识别存量配置并自动升级
LEGACY_DEFAULT_MESSAGE_VARIANTS = [
    "[API]",
    "日常问候，今天也要开心呀",
    "来续个火花，别让它灭了",
    "打卡，愿一切顺利",
    "[data] 好，记得回我呀",
    "裂开了，火花要掉了，快聊两句",
    "随便聊聊，今天过得怎么样？",
]

# 抖音私信单条上限约 500 字，留出安全余量；过长内容更容易被判群发
MAX_MESSAGE_LENGTH = 300

_ZERO_WIDTH_CHARS = ("\u200b", "\u200c", "\u200d", "\ufeff", "\u00ad")

HITOKOTO_API = "https://v1.hitokoto.cn/"
HITOKOTO_TYPES = {
    "动画": "a",
    "漫画": "b",
    "游戏": "c",
    "文学": "d",
    "原创": "e",
    "来自网络": "f",
    "其他": "g",
    "影视": "h",
    "诗词": "i",
    "哲学": "k",
    "抖机灵": "l",
}

WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class DouyinCookieError(ValueError):
    def __init__(self, message, *, category="cookie_format_invalid"):
        super().__init__(message)
        self.category = category


class DouyinRemoteError(RuntimeError):
    def __init__(self, message, *, retryable=False, auth_failed=False):
        super().__init__(message)
        self.retryable = retryable
        self.auth_failed = auth_failed


def _pair_regex():
    global _PAIR_RE
    if _PAIR_RE is None:
        import re
        _PAIR_RE = re.compile(r"^\s*(?P<name>[^\s=;]+)\s*=\s*(?P<value>[^;]*)\s*$")
    return _PAIR_RE


def _strip_quotes(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def normalize_cookie(entry):
    """单个 cookie 条目归一化为存储结构 {name,value,domain,path,expires}。"""
    if not isinstance(entry, dict):
        raise DouyinCookieError("cookie 条目必须是包含 name/value 的对象")
    name = str(entry.get("name") or "").strip()
    if not name:
        raise DouyinCookieError("cookie 条目缺少 name")
    if "value" not in entry:
        raise DouyinCookieError(f"cookie {name!r} 缺少 value")
    value = str(entry.get("value") or "")
    domain = str(entry.get("domain") or "").strip()
    if not domain:
        domain = DEFAULT_COOKIE_DOMAIN
    elif not domain.startswith(".") and "." in domain:
        domain = f".{domain}"
    path = str(entry.get("path") or "").strip() or DEFAULT_COOKIE_PATH
    if not path.startswith("/"):
        path = DEFAULT_COOKIE_PATH
    cookie = {"name": name, "value": value, "domain": domain, "path": path}
    expiry = None
    for key in ("expires", "expirationDate", "expiry", "expiresAt"):
        if entry.get(key) is not None:
            expiry = entry.get(key)
            break
    try:
        expires = float(expiry) if expiry is not None else -1
    except (TypeError, ValueError):
        expires = -1
    cookie["expires"] = expires if expires > 0 else -1
    for flag in ("httpOnly", "secure"):
        if flag in entry:
            cookie[flag] = bool(entry.get(flag))
    same_site = entry.get("sameSite")
    if isinstance(same_site, str) and same_site.strip():
        cookie["sameSite"] = same_site.strip().capitalize()
    return cookie


def _parse_json_cookies(text):
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict):
        for key in ("cookies", "cookie"):
            nested = payload.get(key)
            if isinstance(nested, list):
                payload = nested
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise DouyinCookieError("JSON cookie 必须是对象数组")
    if not payload:
        raise DouyinCookieError("JSON cookie 数组为空")
    return payload


def _parse_header_cookies(text):
    stripped = text.strip()
    if stripped.lower().startswith("cookie:"):
        stripped = stripped.split(":", 1)[1]
    entries = []
    for chunk in stripped.split(";"):
        if not chunk.strip():
            continue
        match = _pair_regex().match(chunk)
        if not match:
            # 浏览器复制的 cookie 常混入无 "=" 的残片（如孤立的 "douyin.com"），
            # 宽容跳过而不是整体拒绝；全部无效时由下方 entries 空判断报错。
            continue
        entries.append({
            "name": match.group("name").strip(),
            "value": _strip_quotes(match.group("value")),
        })
    if not entries:
        raise DouyinCookieError("cookie 字符串不含任何 name=value 对")
    return entries


def parse_cookie_input(raw):
    """三种形态全收：浏览器扩展 JSON 数组 / 请求头字符串 / 单个 name=value。"""
    if raw is None:
        raise DouyinCookieError("cookie 输入为空")
    text = str(raw).strip()
    if not text:
        raise DouyinCookieError("cookie 输入为空")
    if len(text) > _MAX_INPUT_LENGTH:
        raise DouyinCookieError("cookie 输入过大")
    entries = None
    stripped_lower = text.lower()
    if stripped_lower.startswith("cookie:") and text[0] not in "[{":
        # "cookie: {...}" / "cookie: name=value; ..." 形式，剥前缀后再判形态
        text = text.split(":", 1)[1].strip()
        if not text:
            raise DouyinCookieError("cookie 输入为空")
    if text[0] in "[{":
        entries = _parse_json_cookies(text)
    if entries is None:
        if "=" not in text:
            raise DouyinCookieError("cookie 输入必须包含 name=value 对")
        entries = _parse_header_cookies(text)
    cookies = []
    seen = set()
    for entry in entries:
        cookie = normalize_cookie(entry)
        key = (cookie["name"], cookie["domain"], cookie["path"])
        if key in seen:
            continue
        seen.add(key)
        cookies.append(cookie)
    if not cookies:
        raise DouyinCookieError("cookie 输入不含可用项")
    return cookies[:_MAX_COOKIES]


def auth_cookie_names(cookies):
    names = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        if name in AUTH_COOKIE_NAMES and name not in names:
            names.append(name)
    return names


def require_auth_cookies(cookies):
    names = auth_cookie_names(cookies)
    if not names:
        raise DouyinCookieError(
            "cookie 中没有抖音登录态（需要 sessionid 家族任一：sessionid/sessionid_ss/sid_guard/sid_tt/uid_tt/uid_tt_ss）",
            category="cookie_auth_missing",
        )
    return names


def cookie_summary(cookies):
    """用于日志与 UI 的摘要，绝不携带 cookie 值。"""
    return {"count": len(cookies or []), "auth_names": auth_cookie_names(cookies)}


def to_cookie_string(cookies):
    return "; ".join(
        f"{item['name']}={item['value']}"
        for item in (cookies or [])
        if item.get("name") and item.get("value") is not None
    )


def cookie_map(cookies):
    return {
        item["name"]: item["value"]
        for item in (cookies or [])
        if item.get("name") and item.get("value") is not None
    }


def requests_proxies(proxy):
    """代理配置 → requests proxies dict。

    proxy 形如 {"protocol": "http", "host": "1.2.3.4", "port": 8080,
    "username": "", "password": ""}；为空返回 None（直连）。
    """
    if not proxy or not proxy.get("host") or not proxy.get("port"):
        return None
    scheme = str(proxy.get("protocol") or "http").strip().lower() or "http"
    auth = ""
    if proxy.get("username"):
        from urllib.parse import quote

        auth = quote(str(proxy["username"]), safe="")
        if proxy.get("password"):
            auth += ":" + quote(str(proxy["password"]), safe="")
        auth += "@"
    url = f"{scheme}://{auth}{proxy['host']}:{int(proxy['port'])}"
    return {"http://": url, "https://": url}


def fetch_session_identity(cookies, proxy=None, site=None):
    """登录预检：creator IM user_token 接口。

    返回 {user_id, token}；Cookie 失效抛 DouyinRemoteError(auth_failed=True)。

    注意：这是**创作者中心**链路的预检。主站（www.douyin.com）没有等效的零签名
    接口（JSON 接口均需 a_bogus 签名），主站身份请走 ``douyin_identity``
    的浏览器验证通道。
    """
    from .douyin_site import active_site  # noqa: PLC0415 —— 避免模块级循环导入

    site = site or active_site()
    if site.key != "creator":
        raise DouyinRemoteError(
            "主站链路不支持接口级登录预检（需要浏览器签名），请使用浏览器验证",
            auth_failed=False,
        )
    params = {
        "aid": site.aid,
        "app_name": "aweme_creator_platform",
        "device_platform": "web",
        "referer": "",
        "user_agent": USER_AGENT,
        "cookie_enabled": "true",
        "screen_width": "1280",
        "screen_height": "720",
        "browser_language": "en-US@posix",
        "browser_platform": "Win32",
        "browser_name": "Mozilla",
        "browser_version": USER_AGENT,
        "browser_online": "true",
        "timezone_name": "Asia/Shanghai",
    }
    cmap = cookie_map(cookies)
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"{site.origin}/",
        "Origin": site.origin,
        "Accept": "application/json, text/javascript",
        "Cookie": to_cookie_string(cookies),
        "x-tt-passport-csrf-token": cmap.get("passport_csrf_token", ""),
    }
    try:
        response = requests.get(
            f"{site.origin}/aweme/v1/creator/im/user_token/",
            params=params,
            headers=headers,
            timeout=15,
            proxies=requests_proxies(proxy),
        )
    except requests.RequestException as exc:
        raise DouyinRemoteError(f"抖音服务网络异常：{exc}", retryable=True) from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200 or data.get("status_code") != 0 or not data.get("user_id"):
        raise DouyinRemoteError("登录态校验失败（cookie 已失效或被抖音风控）", auth_failed=True)
    return {"user_id": str(data["user_id"]), "token": str(data.get("token") or "")}


def fetch_passport_identity(cookies, proxy=None, site=None):
    """**零签名**的登录态权威判定（主站 passport 账号信息接口）。

    这是主站校验的最优路径：不开浏览器、不触发风控，1s 内返回。语义（2026-10-13 实测）::

        有效 cookie → {"message": "success", "data": {user_id, sec_user_id, name, avatar_url, …}}
        失效 cookie → {"message": "error",   "data": {"error_code": 13, "description": "会话过期，请重新登录"}}

    返回 ``{"logged_in", "user_id", "sec_uid", "nickname", "avatar", "message"}``。
    网络异常抛 ``DouyinRemoteError(retryable=True)``（调用方可回退浏览器链路）。
    """
    from .douyin_site import active_site  # noqa: PLC0415 —— 避免模块级循环导入

    site = site or active_site()
    url = site.passport_info_url
    if not url:
        raise DouyinRemoteError("当前站点未配置 passport 校验接口", auth_failed=False)

    cmap = cookie_map(cookies)
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"{site.origin}/",
        "Accept": "application/json, text/javascript",
        "Cookie": to_cookie_string(cookies),
        "x-tt-passport-csrf-token": cmap.get("passport_csrf_token", ""),
    }
    try:
        response = requests.get(
            url,
            params={"aid": site.aid, "account_sdk_source": "web", "version": "2.0"},
            headers=headers,
            timeout=15,
            proxies=requests_proxies(proxy),
        )
    except requests.RequestException as exc:
        raise DouyinRemoteError(f"抖音服务网络异常：{exc}", retryable=True) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise DouyinRemoteError("抖音 passport 接口返回非 JSON", retryable=True) from exc

    payload = data.get("data") or {}
    if response.status_code == 200 and data.get("message") == "success" and payload.get("user_id"):
        return {
            "logged_in": True,
            "user_id": str(payload.get("user_id") or ""),
            "sec_uid": str(payload.get("sec_user_id") or payload.get("sec_uid") or ""),
            "nickname": str(payload.get("name") or payload.get("screen_name") or "").strip(),
            "avatar": str(payload.get("avatar_url") or "").strip(),
            "message": "",
        }
    # error_code=13 = 会话过期（cookie 确实失效，不是网络问题）
    description = str(payload.get("description") or data.get("status_msg") or "").strip()
    return {
        "logged_in": False,
        "user_id": "",
        "sec_uid": "",
        "nickname": "",
        "avatar": "",
        "message": description or "登录态已失效，请重新扫码（cookie 已过期）",
    }


def _fetch_profile_by_sec_uid(cookies, sec_uid, proxy=None):
    """按 sec_uid 拉主页资料：昵称/抖音号/头像（与协议脚本同接口，无需签名）。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"https://www.douyin.com/user/{sec_uid}",
        "Accept": "application/json, text/javascript",
        "Cookie": to_cookie_string(cookies),
    }
    try:
        response = requests.get(
            "https://www.douyin.com/aweme/v1/web/user/profile/other/",
            params={"sec_user_id": sec_uid},
            headers=headers,
            timeout=8,
            proxies=requests_proxies(proxy),
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return {}
    user = (data or {}).get("user") or {}
    if response.status_code != 200 or data.get("status_code") != 0:
        return {}
    avatar_list = ((user.get("avatar_thumb") or {}).get("url_list")) or []
    return {
        "nickname": str(user.get("nickname") or "").strip(),
        "douyin_id": str(user.get("unique_id") or "").strip(),
        "avatar": str(avatar_list[-1] if avatar_list else "").strip(),
    }


def fetch_self_profile(cookies, proxy=None):
    """拉取账号自身资料（昵称/抖音号/头像），无需浏览器签名。

    链路：passport account/info（user_id/昵称，可能带 sec_user_id）→
    主页 profile/other（补抖音号与头像）。任何失败返回空字段 dict
    （资料是增强信息，不应阻塞建号流程，由调用方决定是否回填）。
    """
    empty = {"user_id": "", "nickname": "", "douyin_id": "", "sec_uid": "", "avatar": ""}
    from .douyin_site import active_site  # noqa: PLC0415

    site = active_site()
    cmap = cookie_map(cookies)
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"{site.origin}/",
        "Origin": site.origin,
        "Accept": "application/json, text/javascript",
        "Cookie": to_cookie_string(cookies),
        "x-tt-passport-csrf-token": cmap.get("passport_csrf_token", ""),
    }
    try:
        response = requests.get(
            f"{site.origin}/passport/account/info/v2/",
            params={"aid": site.aid, "account_sdk_source": "web", "version": "2.0"},
            headers=headers,
            timeout=8,
            proxies=requests_proxies(proxy),
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return empty
    payload = data.get("data") or {}
    if response.status_code != 200 or data.get("message") != "success":
        return empty
    profile = {
        "user_id": str(payload.get("user_id") or ""),
        "nickname": str(payload.get("name") or payload.get("screen_name") or "").strip(),
        "sec_uid": str(payload.get("sec_user_id") or payload.get("sec_uid") or cmap.get("sec_uid") or "").strip(),
        "douyin_id": "",
        "avatar": str(payload.get("avatar_url") or "").strip(),
    }
    if profile["sec_uid"]:
        detail = _fetch_profile_by_sec_uid(cookies, profile["sec_uid"], proxy)
        profile["nickname"] = detail.get("nickname") or profile["nickname"]
        profile["douyin_id"] = detail.get("douyin_id") or ""
        profile["avatar"] = detail.get("avatar") or profile["avatar"]
    return profile


def fetch_hitokoto(types=None):
    """请求一言 API；types 为空时取全部类型。失败返回占位文本不抛错。"""
    api_url = HITOKOTO_API
    selected = [
        code for label, code in HITOKOTO_TYPES.items()
        if not types or label in types
    ]
    if selected and "?" not in api_url:
        api_url += "?"
    for index, code in enumerate(selected):
        api_url += ("&" if index else "") + f"c={code}"
    try:
        response = requests.get(api_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        # 只取句子本体：附带"—— 来源 (作者)"会变成一眼可辨的机器内容，容易被安全校验拦下
        sentence = str(data.get("hitokoto") or "").strip()
        return sanitize_message(sentence) or "今天也要保持好心情"
    except Exception:
        return "今天也要保持好心情"


def sanitize_message(text: str) -> str:
    """清洗待发送文案：统一换行、去零宽/控制符、合并行内空白、截断超长。

    目的不是改写语义，而是去掉明显的"模板痕迹"（多余空白、隐藏字符、超长段落）。
    """
    message = str(text or "")
    message = message.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    cleaned_lines = []
    for line in message.split("\n"):
        line = "".join(
            ch for ch in line
            if ch not in _ZERO_WIDTH_CHARS and (ch >= " " or ch == "\t")
        )
        line = " ".join(line.replace("\t", " ").split())
        if line:
            cleaned_lines.append(line)
    result = "\n".join(cleaned_lines).strip()
    if len(result) > MAX_MESSAGE_LENGTH:
        result = result[:MAX_MESSAGE_LENGTH].rstrip() + "…"
    return result


def is_legacy_default_pool(variants) -> bool:
    """判断一组模板是否就是旧版内置池（用于把存量配置平滑升级到新文案）。"""
    items = [str(item).strip() for item in (variants or []) if str(item).strip()]
    if not items:
        return False
    return sorted(items) == sorted(LEGACY_DEFAULT_MESSAGE_VARIANTS)


def render_template(template: str, *, hitokoto_types=None, today: date | None = None) -> str:
    """渲染模板占位符：[API] 一言 / [data] 日期 / [weekday] 星期。"""
    today = today or date.today()
    message = str(template or "")
    message = message.replace("\\r\\n", "\n").replace("\r\n", "\n")
    if "[API]" in message:
        message = message.replace("[API]", fetch_hitokoto(hitokoto_types))
    if "[data]" in message:
        message = message.replace("[data]", today.strftime("%Y年%m月%d日"))
    if "[weekday]" in message:
        message = message.replace("[weekday]", WEEKDAY_LABELS[today.weekday()])
    return sanitize_message(message)


def build_messages_by_target(
    targets,
    variants,
    *,
    shuffle=True,
    previous_messages=None,
    hitokoto_types=None,
):
    """为目标好友逐个生成消息：目标乱序 + 相邻不重复 + 避开上次发送内容。"""
    ordered = []
    seen = set()
    for target in targets or []:
        normalized = str(target or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    if shuffle and len(ordered) > 1:
        random.shuffle(ordered)

    candidates = [str(item).strip() for item in (variants or []) if str(item).strip()]
    rendered = []
    for template in candidates:
        message = render_template(template, hitokoto_types=hitokoto_types)
        if message and message not in rendered:
            rendered.append(message)
    if not rendered:
        # 兜底不依赖网络（一言走远程），直接取一条自然口语文案
        natural = [item for item in DEFAULT_MESSAGE_VARIANTS if "[API]" not in item]
        rendered = [random.choice(natural or DEFAULT_MESSAGE_VARIANTS)]

    def choose(previous_message, last_message):
        for pool in (
            [m for m in rendered if m != previous_message and m != last_message],
            [m for m in rendered if m != previous_message],
            [m for m in rendered if m != last_message],
            rendered,
        ):
            if pool:
                return random.choice(pool)
        return rendered[0]

    previous = previous_messages or {}
    planned = {}
    last_message = ""
    for target in ordered:
        previous_message = ""
        raw = previous.get(target, "")
        if isinstance(raw, dict):
            previous_message = str(raw.get("message") or "").strip()
        else:
            previous_message = str(raw or "").strip()
        message = choose(previous_message, last_message)
        planned[target] = message
        last_message = message
    return planned


# 目标写法分隔符：半角/全角都认，避免用户切输入法
_TARGET_ALIAS_SEPARATORS = ("=", "＝")
_TARGET_MESSAGE_SEPARATOR = ("|", "｜")


def parse_target_spec(value) -> dict:
    """解析目标写法，返回 ``{primary, aliases, message, raw}``。

    - ``张三``：只按主名匹配；
    - ``张三=三哥=小三``：主名找不到时按顺序试别名（好友改名不再断火）；
    - ``张三=三哥|今天喝奶茶了吗``：专属文案，覆盖模板池。
    分隔符兼容全角 ``＝`` / ``｜``；文案内部要用竖线请用全角 ``｜``。
    """
    raw = str(value or "").strip()
    message = ""
    head = raw
    for sep in _TARGET_MESSAGE_SEPARATOR:
        if sep in head:
            head, _, tail = head.partition(sep)
            message = tail.strip()
            break
    names: list[str] = []
    for chunk in _split_any(head, _TARGET_ALIAS_SEPARATORS):
        text = chunk.strip()
        if text and text not in names:
            names.append(text)
    primary = names[0] if names else ""
    return {
        "primary": primary,
        "aliases": names[1:],
        "message": message,
        "raw": raw,
    }


def _split_any(text: str, separators: tuple[str, ...]) -> list[str]:
    parts = [text]
    for sep in separators:
        next_parts: list[str] = []
        for part in parts:
            next_parts.extend(part.split(sep))
        parts = next_parts
    return parts


def format_target_spec(spec: dict) -> str:
    """把解析结果拼回一行写法（主名升级时原地改写 targets 用）。"""
    names = [str(spec.get("primary") or "").strip()] + [
        str(item).strip() for item in (spec.get("aliases") or []) if str(item).strip()
    ]
    names = [item for item in names if item]
    text = "=".join(dict.fromkeys(names))
    message = str(spec.get("message") or "").strip()
    return f"{text}|{message}" if message else text


def normalize_target_specs(values, *, limit: int = 200) -> list[dict]:
    """去空白、按主名保序去重（别名仍按原顺序保留）。"""
    specs: list[dict] = []
    seen: set[str] = set()
    for item in values or []:
        if isinstance(item, dict):
            spec = {
                "primary": str(item.get("primary") or "").strip(),
                "aliases": [str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()],
                "message": str(item.get("message") or "").strip(),
                "raw": str(item.get("raw") or ""),
            }
        else:
            spec = parse_target_spec(item)
        primary = spec["primary"]
        if not primary or primary in seen:
            continue
        seen.add(primary)
        specs.append(spec)
        if len(specs) >= limit:
            break
    return specs


def build_target_payload(
    targets,
    variants,
    *,
    shuffle=True,
    previous_messages=None,
    hitokoto_types=None,
):
    """目标 + 文案 + 别名 一次性构建（供发送通道使用）。

    - 带专属文案的目标直接用其文案（仍走占位符渲染与安全清洗，空文案回落到模板池）；
    - 其余目标沿用既有「乱序 + 相邻不重复 + 避开上次内容」策略；
    - 返回 ``{specs, messages, aliases}``，messages 按 targets 原顺序排列。
    """
    specs = normalize_target_specs(targets)
    plain = [spec["primary"] for spec in specs if not spec["message"]]
    messages = build_messages_by_target(
        plain,
        variants,
        shuffle=shuffle,
        previous_messages=previous_messages,
        hitokoto_types=hitokoto_types,
    )
    for spec in specs:
        if not spec["message"]:
            continue
        rendered = render_template(spec["message"], hitokoto_types=hitokoto_types)
        if rendered:
            messages[spec["primary"]] = rendered
    ordered_messages = {
        spec["primary"]: messages[spec["primary"]]
        for spec in specs
        if messages.get(spec["primary"])
    }
    return {
        "specs": specs,
        "messages": ordered_messages,
        "aliases": {spec["primary"]: spec["aliases"] for spec in specs if spec["aliases"]},
    }


def normalize_target_name(value):
    """目标名归一：全角转半角折叠 + 去零宽 + 空白合并（与 casefold 方向一致）。"""
    import unicodedata
    text = unicodedata.normalize("NFKC", str(value or ""))
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        text = text.replace(ch, "")
    return " ".join(text.split()).strip().casefold()


def _run_protocol(payload, *, timeout=None, action="协议发送"):
    """调用 node protocol_sender.mjs：stdin JSON → stdout JSON（发送/拉好友共用）。"""
    if not PROTOCOL_SCRIPT.exists():
        raise DouyinRemoteError("protocol_sender.mjs 缺失，请检查 douyin_assets 目录")
    node_path = os.environ.get("DOUYIN_NODE_PATH", "").strip() or shutil.which("node")
    if not node_path:
        raise DouyinRemoteError(
            "未找到 node 运行时，无法执行协议发送；请在后端环境安装 Node.js 18+"
            "（Docker 容器内可执行：apt-get update && apt-get install -y nodejs），"
            "或通过环境变量 DOUYIN_NODE_PATH 指定 node 可执行文件路径"
        )
    timeout = timeout or DEFAULT_PROTOCOL_TIMEOUT_SECONDS
    try:
        process = subprocess.run(
            [node_path, str(PROTOCOL_SCRIPT)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DouyinRemoteError(f"{action}超时（>{timeout}s）", retryable=True) from exc
    except OSError as exc:
        raise DouyinRemoteError(f"无法启动 node 子进程：{exc}", retryable=True) from exc

    stdout = (process.stdout or "").strip()
    if not stdout:
        raise DouyinRemoteError(f"{action}无输出：{(process.stderr or '')[:500]}")
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise DouyinRemoteError(f"{action}输出不是有效 JSON：{stdout[:500]}") from exc
    if process.returncode != 0 or not data.get("ok"):
        raise DouyinRemoteError(f"{action}失败：{data.get('error') or (process.stderr or '')[:500]}")
    return data


def _protocol_account(cookies, protocol_targets_cache=None, proxy=None, user_id=None):
    account = {
        "username": "",
        "cookies": list(cookies or []),
        "protocol_targets_cache": list(protocol_targets_cache or []),
    }
    if user_id:
        # 主站链路（biz=douyin_web）无法从 creator user_token 接口取 id，
        # 由后端把账号已解析的 user_id 传进来（SDK 初始化必需）
        account["userId"] = str(user_id)
    if proxy and proxy.get("host") and proxy.get("port"):
        account["proxy"] = {
            "protocol": str(proxy.get("protocol") or "http").lower(),
            "host": proxy["host"],
            "port": int(proxy["port"]),
            "username": str(proxy.get("username") or ""),
            "password": str(proxy.get("password") or ""),
        }
    return account


def run_protocol_send(
    cookies,
    messages_by_target,
    *,
    data_dir,
    protocol_targets_cache=None,
    target_identities=None,
    dry_run=False,
    interval_min=0,
    interval_max=0,
    timeout=None,
    proxy=None,
    user_id=None,
):
    """调用 node protocol_sender.mjs 发送续火消息。

    返回 {ok, username, user_id, sent[], resolved[], unresolved[], protocol_targets_cache[]}
    """
    payload = {
        "mode": "send",
        "site": active_site().key,
        "repoRoot": str(data_dir),
        "dryRun": bool(dry_run),
        "account": _protocol_account(cookies, protocol_targets_cache, proxy, user_id),
        "messagesByTarget": dict(messages_by_target or {}),
        "targetIdentities": dict(target_identities or {}),
        "sendStrategy": {
            "messageIntervalSecondsMin": max(0, int(interval_min)),
            "messageIntervalSecondsMax": max(int(interval_min), int(interval_max)),
        },
    }
    return _run_protocol(payload, timeout=timeout, action="协议发送")


def run_protocol_list_friends(
    cookies,
    *,
    data_dir,
    protocol_targets_cache=None,
    timeout=None,
    proxy=None,
    user_id=None,
):
    """调用 protocol_sender.mjs 的 list_friends 模式：只拉会话列表，不发送消息。

    返回 {ok, mode, user_id, count, sendable_count, friends[], protocol_targets_cache[]}
    """
    payload = {
        "mode": "list_friends",
        "site": active_site().key,
        "repoRoot": str(data_dir),
        "account": _protocol_account(cookies, protocol_targets_cache, proxy, user_id),
    }
    return _run_protocol(
        payload,
        timeout=timeout or DEFAULT_FRIENDS_TIMEOUT_SECONDS,
        action="拉取好友",
    )


def run_protocol_spark_state(
    cookies,
    *,
    data_dir,
    protocol_targets_cache=None,
    timeout=None,
    proxy=None,
    user_id=None,
):
    """调用 protocol_sender.mjs 的 spark_state 模式：读取平台真实火花状态（只读）。

    返回 {ok, mode, user_id, count, pending_today, sparks[]}。
    sparks[i] = {nickname, unique_id, sec_uid, conversation_id, spark: {state, days, text, ...}}
      spark.state: 0=仅历史记录 / 1=今日已续 / 2=今日未续 / 3=濒危
    """
    payload = {
        "mode": "spark_state",
        "site": active_site().key,
        "repoRoot": str(data_dir),
        "account": _protocol_account(cookies, protocol_targets_cache, proxy, user_id),
    }
    return _run_protocol(
        payload,
        timeout=timeout or DEFAULT_FRIENDS_TIMEOUT_SECONDS,
        action="读取火花状态",
    )


def run_protocol_friends_and_sparks(
    cookies,
    *,
    data_dir,
    protocol_targets_cache=None,
    timeout=None,
    proxy=None,
    user_id=None,
):
    """调用 protocol_sender.mjs 的 friends_and_sparks 模式：一次采样同时拉好友与火花。

    拉好友与读火花的数据源相同（都是会话列表），合并后只需一次 SDK 初始化，
    「首次自动拉好友+火花」从两次 ~22s 降到一次。

    返回 {ok, mode, user_id, count, sendable_count, friends[],
          spark_count, pending_today, sparks[], protocol_targets_cache[]}
    """
    payload = {
        "mode": "friends_and_sparks",
        "site": active_site().key,
        "repoRoot": str(data_dir),
        "account": _protocol_account(cookies, protocol_targets_cache, proxy, user_id),
    }
    return _run_protocol(
        payload,
        timeout=timeout or DEFAULT_FRIENDS_TIMEOUT_SECONDS,
        action="拉取好友与火花",
    )


def summarize_status(entry):
    """单条发送结果 → (ok, reason)。"""
    if entry.get("dryRun"):
        return False, "演练模式，未真实发送"
    if entry.get("success") is True and entry.get("statusCode") in (0, "0"):
        return True, ""
    code = entry.get("statusCode")
    reason = SEND_STATUS_REASONS.get(code if isinstance(code, int) else None)
    if not reason:
        status_name = str(entry.get("statusName") or "").strip()
        status_msg = str(entry.get("statusMsg") or "").strip()
        reason = status_msg or status_name or (f"发送失败（statusCode={code}）" if code is not None else "发送失败")
    return False, reason
