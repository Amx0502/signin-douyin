"""抖音站点配置：统一管理「创作者中心」与「主站」两套链路的差异。

背景（2026-10-13 用户决策）：不再使用创作者中心，登录与发消息都走
``https://www.douyin.com/chat``。调研（``.workbuddy/douyin-web-migration-notes.md``）结论：

1. 两侧**是同一套 IM SDK**（``appKey`` 完全相同），差异仅在 ``biz`` / ``aid`` / 域名头；
2. 主站 IM 是 **Module Federation 远程包**（``@pc-im/im``），Node vm 复刻成本极高，
   因此主站侧以「浏览器页面内调用」为主；
3. 主站读取接口**零签名**（cookie 鉴权），发送由**独立安全 SDK**（``security-secsdk``）
   注入 ``bd-ticket-guard-*`` 签名 —— 在真实页面内发请求即可自动签名。

切换方式：环境变量 ``DOUYIN_SITE``（``web`` 默认 / ``creator`` 回退）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DouyinSite:
    """一套站点链路所需的全部常量。"""

    key: str
    label: str
    # 页面地址
    home_url: str
    login_url: str
    chat_url: str
    # 请求头
    origin: str
    referer: str
    # 协议层（IM SDK / 接口）参数
    biz: str
    device_platform: str
    aid: str
    app_name: str
    # cookie 保活访问页
    keepalive_urls: tuple[str, ...] = field(default_factory=tuple)
    # 登录态预检接口（返回 JSON，cookie 鉴权）
    identity_probe_url: str = ""
    # passport 账号信息接口：零签名、cookie 鉴权，返回当前登录用户的
    # user_id / sec_user_id / 昵称 / 头像；cookie 失效时返回 error_code=13。
    # 用于「不开浏览器」的快速登录态校验（creator 链路无需此接口，留空）。
    passport_info_url: str = ""
    # 浏览器通道：发送后用于判定「已登录」的页面特征
    login_markers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def host(self) -> str:
        return self.origin.split("://")[-1]


CREATOR = DouyinSite(
    key="creator",
    label="创作者中心",
    home_url="https://creator.douyin.com/creator-micro/home",
    login_url="https://creator.douyin.com/login",
    chat_url="https://creator.douyin.com/creator-micro/data/following/chat",
    origin="https://creator.douyin.com",
    referer="https://creator.douyin.com/",
    biz="douyin_creator",
    device_platform="douyin_creator",
    aid="2906",
    app_name="douyin_creator",
    keepalive_urls=(
        "https://www.douyin.com/",
        "https://creator.douyin.com/creator-micro/home",
    ),
    identity_probe_url="https://creator.douyin.com/aweme/v1/creator/im/user_token/",
    login_markers=("creator.douyin.com",),
)

# 主站（www.douyin.com）：aid=6383、device_platform=webapp（HAR 实证）
WEB = DouyinSite(
    key="web",
    label="抖音主站",
    home_url="https://www.douyin.com/",
    # 登录入口用 /user/self?modal_id=login（「我的」页 + 直开登录弹窗）：
    # ① /jingxuan 虽能渲染，但登录 SDK 是懒加载，passport 要 40s 后才请求，
    #    二维码 52~59s 才出现；② /user/self 页面几乎空（body≈253 字符，无 feed），
    #    登录 SDK 立即初始化，二维码稳定 19~20s 出现（4 次采样，2026-10-13 服务器实测）。
    # ⚠️ 不能用主动点击登录按钮加速——会走交互触发链路，风控校验更重反而 60s+。
    login_url="https://www.douyin.com/user/self?modal_id=login",
    chat_url="https://www.douyin.com/chat",
    origin="https://www.douyin.com",
    referer="https://www.douyin.com/",
    biz="douyin_web",
    device_platform="webapp",
    aid="6383",
    app_name="douyin_web",
    keepalive_urls=(
        "https://www.douyin.com/",
        "https://www.douyin.com/chat",
    ),
    # /aweme/v1/web/user/settings/ 返回当前登录用户设置（cookie 鉴权，JSON）
    identity_probe_url="https://www-hj.douyin.com/aweme/v1/web/user/settings/",
    # 零签名的登录态权威判定接口（2026-10-13 服务器实测）：
    #   有效 cookie → {"message":"success","data":{user_id,sec_user_id,name,avatar_url,…}}
    #   失效 cookie → {"message":"error","data":{"error_code":13,"description":"会话过期，请重新登录"}}
    # 两种都是 HTTP 200，语义清晰；**不触发风控**（对比首页会被拦到验证码中间页）。
    # ⚠️ 校验不要再用 site.home_url 开浏览器——首页在服务器 IP 上会命中风控验证码
    #    中间页，页面不渲染登录态 → 等满 30s 超时误报「cookie 可能已失效」。
    passport_info_url="https://www.douyin.com/passport/account/info/v2/",
    login_markers=("www.douyin.com",),
)

_SITES = {CREATOR.key: CREATOR, WEB.key: WEB}


def get_site(key: str | None = None) -> DouyinSite:
    """按 key 取站点配置；未知 key 回退默认站点。"""
    if key is None:
        key = os.getenv("DOUYIN_SITE", WEB.key).strip().lower()
    return _SITES.get(str(key or "").strip().lower(), WEB)


def active_site() -> DouyinSite:
    """当前生效站点（``DOUYIN_SITE``：web 默认 / creator 回退）。"""
    return get_site()
