"""抖音持久浏览器 Profile（P1，解决 cookie 掉线的根因）。

背景（2026-10-13 方案第 1 节）：
此前三处浏览器通道（扫码登录 / cookie 保活 / 浏览器发送）都是
``browser.new_context() + add_cookies()`` —— 每次全新内存上下文，靠 cookie 单点
承载登录态；cookie 一陈旧就掉线。改成 **``launch_persistent_context(user_data_dir=…)``**
后，每个账号有固定的浏览器用户目录（Cookies / Local Storage / IndexedDB / 设备指纹），
浏览器自己把登录态写盘、下次启动直接复用，cookie 不再是唯一凭证。

存储布局（挂载卷内）::

    <DOUYIN_PROFILE_ROOT>/<account_id>/
      ├─ Default/Cookies
      ├─ Default/Local Storage/
      └─ …

关键约束（方案第 1.2 节「必须写进实现」）：
1. **一个 profile 同时只能被一个浏览器占用** —— 文件锁（``.lock`` 所在目录带
   pid 信息）阻止并发；抖音三条通道本身也在 ``BROWSER_GUARD`` 内串行。
2. **代理必须与 profile 绑定** —— profile 指纹 + 出口 IP 固定；代理变了打告警日志
   （代理变更后风控会重新识别，等价于换设备）。
3. **headless 优先** —— 默认无头，省内存；noVNC 场景才用 headless=False。
4. 账号删除时 ``purge_profile`` 清理目录（磁盘占用 30~80MB/账号）。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# 默认落在数据目录旁的 douyin-profiles/（容器内 DOUYIN_DATA_DIR=/app/backend/data/douyin）
DEFAULT_PROFILE_ROOT = Path(__file__).resolve().parent.parent / "data" / "douyin-profiles"

# profile 目录内我们自己写的元数据文件（记录绑定过的代理指纹，用于变更告警）
_META_FILE = ".profile-meta.json"
# 进程内锁：同一进程内并发调用同一账号时快速失败（跨进程由 BROWSER_GUARD + 单实例保证）
_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def profile_root() -> Path:
    """profile 根目录：``DOUYIN_PROFILE_ROOT`` 优先，否则数据目录旁的 douyin-profiles。"""
    raw = os.getenv("DOUYIN_PROFILE_ROOT", "").strip()
    return Path(raw) if raw else DEFAULT_PROFILE_ROOT


def profile_dir(account_id: int) -> Path:
    """某账号的 profile 目录（不创建）。"""
    return profile_root() / str(int(account_id))


def ensure_profile_dir(account_id: int) -> Path:
    """确保 profile 目录存在（含父目录），返回路径。"""
    target = profile_dir(account_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def profile_exists(account_id: int) -> bool:
    """profile 目录是否已建立（有内容才算，空目录视为未建立）。"""
    target = profile_dir(account_id)
    try:
        return target.is_dir() and any(target.iterdir())
    except OSError:
        return False


def _account_lock(account_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        if account_id not in _LOCKS:
            _LOCKS[account_id] = threading.Lock()
        return _LOCKS[account_id]


@contextmanager
def profile_guard(account_id: int):
    """占用某账号 profile（进程内互斥）。

    三条浏览器通道已在 ``BROWSER_GUARD`` 内串行，这里的锁是**额外**防护：
    防止未来改造引入并发时两个内核同时打开同一个 user_data_dir
    （Chromium 会因 profile 被占用而静默失败或损坏数据）。
    """
    lock = _account_lock(int(account_id))
    if not lock.acquire(blocking=False):
        raise RuntimeError(f"账号 {account_id} 的浏览器 profile 正在被占用（另一个浏览器会话未结束）")
    try:
        yield lock
    finally:
        lock.release()


def _proxy_fingerprint(proxy) -> str:
    """代理指纹：只取出口标识（host:port），不落密码。"""
    if not proxy or not proxy.get("host"):
        return ""
    return f"{proxy.get('protocol') or 'http'}://{proxy['host']}:{int(proxy.get('port') or 0)}"


def check_proxy_binding(account_id: int, proxy=None) -> dict:
    """校验当前代理是否与该 profile 上次使用时一致；变更则告警（不阻断）。

    返回 ``{"changed": bool, "previous": str, "current": str}``。
    首次写入不算变更；「直连 → 代理」「代理 → 直连」「换代理」都算变更（出口 IP 变了）。
    """
    current = _proxy_fingerprint(proxy)
    target = profile_dir(account_id)
    meta_path = target / _META_FILE
    first_time = not meta_path.exists()
    previous = ""
    if not first_time:
        try:
            previous = str(json.loads(meta_path.read_text(encoding="utf-8")).get("proxy") or "")
        except Exception:  # noqa: BLE001 —— 元数据损坏不影响主流程
            previous = ""
    changed = (not first_time) and previous != current
    if changed:
        logger.warning(
            "抖音账号 %s 的 profile 出口代理已变更（%s → %s）：指纹与 IP 不再匹配，"
            "平台可能重新识别设备，建议固定使用同一代理",
            account_id, previous or "直连", current or "直连",
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps({"proxy": current, "account_id": int(account_id)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.debug("profile 元数据写入失败 account=%s", account_id, exc_info=True)
    return {"changed": changed, "previous": previous, "current": current}


def seed_from_cookies(account_id: int, cookies, *, proxy=None) -> bool:
    """目录为空时用账号已存 cookie 预置（对齐 sparkflow ``seedCookiesWhenEmpty``）。

    只有「profile 尚未建立」时才需要：首启若不带 cookie，站点会把用户当未登录，
    要求重新扫码；带上已存 cookie 则能直接续上。返回是否实际预置过。
    """
    if profile_exists(account_id):
        return False
    ensure_profile_dir(account_id)
    # 预置信息落元数据（真正的 cookie 注入由调用方在 context 上 add_cookies 完成）
    check_proxy_binding(account_id, proxy)
    logger.info("抖音账号 %s：profile 目录已初始化（将用已存 cookie 预置登录态）", account_id)
    return True


def _pw_cookies(cookies) -> list[dict]:
    """账号 cookie → Playwright add_cookies 可接受的形态（只留 name/value/domain/path）。"""
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


def seed_cookies_into_context(context, cookies) -> int:
    """把账号 cookie 注入（持久）上下文，返回注入条数。

    每次打开都注入一遍：profile 里已有的同名 cookie 会被覆盖为数据库里的最新值
    （数据库是「保活回收」的落点，可能比 profile 更新）。

    ⚠️ 2026-10-13 实测：55 条里混一条坏 cookie（非法 domain/value）会让
    ``add_cookies`` **整批失败**——之前整批调用导致 sessionid 全部丢失、
    校验误报「cookie 失效」。改为先整批、失败再逐条兜底，跳过坏条目。
    """
    items = _pw_cookies(cookies)
    if not items:
        return 0
    try:
        context.add_cookies(items)
        return len(items)
    except Exception as exc:
        logger.warning("profile cookie 整批注入失败，改逐条兜底：%s", str(exc)[:200])
    # 逐条兜底：单条非法不影响其余
    ok = 0
    for item in items:
        try:
            context.add_cookies([item])
            ok += 1
        except Exception:
            logger.warning("cookie 注入失败已跳过：name=%s domain=%s",
                           item.get("name"), item.get("domain"))
    return ok


def sync_cookies_from_context(context) -> list[dict]:
    """从（持久）上下文读回 cookie（对齐 ``refreshStoredCookiesAfterLogin``）。

    返回 Playwright 原始 cookie 列表；调用方负责与库内旧集合 merge 后落库。
    """
    try:
        return list(context.cookies() or [])
    except Exception as exc:  # noqa: BLE001 —— 浏览器可能已关
        logger.info("profile cookie 回收失败：%s", exc)
        return []


def claim_profile(slot: int, account_id: int) -> bool:
    """把扫码期间建的临时 profile（负槽位）改名为真实 account_id 目录。

    已有目标目录时（例如同号二次登录）保留目标目录，丢弃临时目录。
    """
    slot, account_id = int(slot), int(account_id)
    if slot == account_id:
        return False
    source = profile_dir(slot)
    target = profile_dir(account_id)
    if not source.exists():
        return False
    try:
        if target.exists():
            shutil.rmtree(source, ignore_errors=True)
            logger.info("抖音账号 %s：profile 已存在，丢弃临时目录 slot=%s", account_id, slot)
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        logger.info("抖音账号 %s：profile 已从临时槽位 %s 迁入", account_id, slot)
        return True
    except Exception:  # noqa: BLE001 —— 迁移失败不影响登录（下次打开会新建）
        logger.warning("抖音 profile 迁移失败 slot=%s account=%s", slot, account_id, exc_info=True)
        return False


def purge_profile(account_id: int) -> bool:
    """删除账号时清理 profile 目录（尽力而为，失败只记日志）。"""
    target = profile_dir(account_id)
    if not target.exists():
        return False
    try:
        shutil.rmtree(target, ignore_errors=True)
        logger.info("抖音账号 %s：profile 目录已清理", account_id)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("抖音账号 %s：profile 目录清理失败", account_id, exc_info=True)
        return False


def open_account_context(playwright, account_id: int, *, cookies=None, proxy=None,
                         headless: bool = True):
    """统一入口：打开某账号的持久化浏览器上下文（三条通道共用）。

    返回 ``(context, seed_counts)``；调用方必须 ``context.close()``
    （persistent context 关闭即释放内核，没有独立 browser 对象）。

    使用方式::

        with profile_guard(account_id):
            context, _ = open_account_context(p, account_id, cookies=cookies, proxy=proxy)
            try:
                ...
            finally:
                context.close()
    """
    from .douyin_login import launch_persistent_chromium  # noqa: PLC0415 —— 避免循环导入

    check_proxy_binding(account_id, proxy)
    target = ensure_profile_dir(account_id)
    context = launch_persistent_chromium(playwright, target, proxy, headless=headless)
    injected = seed_cookies_into_context(context, cookies)
    logger.info("抖音账号 %s：持久 profile 已打开（%s，预置 cookie %d 条）",
                account_id, target, injected)
    return context


def profile_size(account_id: int) -> int:
    """profile 目录占用字节数（概览/运维用）。"""
    target = profile_dir(account_id)
    if not target.is_dir():
        return 0
    total = 0
    for path in target.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total
