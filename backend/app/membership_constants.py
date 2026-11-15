import datetime as dt

CARD_SINGLE = "single"
CARD_WEEKLY = "weekly"
CARD_MONTHLY = "monthly"
CARD_QUARTERLY = "quarterly"
CARD_YEARLY = "yearly"
CARD_TYPES = frozenset(
    {CARD_SINGLE, CARD_WEEKLY, CARD_MONTHLY, CARD_QUARTERLY, CARD_YEARLY}
)

# 周期卡：首次登录系统时激活，按月卡天数（monthly_card_days 字段，历史字段名
# 周/季/年卡复用）计算有效期，到期即「核销」，走 card_delete_due_at 清理通道
PERIODIC_CARD_TYPES = frozenset(
    {CARD_WEEKLY, CARD_MONTHLY, CARD_QUARTERLY, CARD_YEARLY}
)
PERIODIC_DEFAULT_DAYS = {
    CARD_WEEKLY: 7,
    CARD_MONTHLY: 30,
    CARD_QUARTERLY: 90,
    CARD_YEARLY: 365,
}
MONTHLY_CARD_DAYS = 30

DEFAULT_MONTHLY_CARD_DAYS = 30
# 周期卡有效天数统一下限；上限按卡型见 PERIODIC_DAYS_BOUNDS
# （MAX_MONTHLY_CARD_DAYS 保留供 pydantic 全局上界与历史月卡长周期兼容）
MIN_CARD_DURATION_DAYS = 1
MAX_CARD_DURATION_DAYS = 400
MIN_MONTHLY_CARD_DAYS = MIN_CARD_DURATION_DAYS
MAX_MONTHLY_CARD_DAYS = 3650

# 各卡型有效天数边界：月/季/年卡沿用历史全范围（管理员可自由设定），
# 仅周卡保留 1~31 天的合理区间防止误填成年卡
PERIODIC_DAYS_BOUNDS = {
    CARD_WEEKLY: (1, 31),
    CARD_MONTHLY: (1, MAX_MONTHLY_CARD_DAYS),
    CARD_QUARTERLY: (1, MAX_MONTHLY_CARD_DAYS),
    CARD_YEARLY: (1, MAX_MONTHLY_CARD_DAYS),
}
PERIODIC_LABELS = {
    CARD_WEEKLY: "周卡",
    CARD_MONTHLY: "月卡",
    CARD_QUARTERLY: "季卡",
    CARD_YEARLY: "年卡",
}
# 卡型中文标签（含次卡）：企微通知文案等统一取这里，与前端 constants/membership.js 的
# CARD_TYPE_LABELS 保持一致
CARD_TYPE_LABELS = {CARD_SINGLE: "次卡", **PERIODIC_LABELS}


def card_type_label(card_type) -> str:
    """卡型中文标签；空值/未知卡型返回空串（调用方据此决定是否加括号）。"""
    return CARD_TYPE_LABELS.get(str(card_type or "").strip(), "")


def owner_display_name(username, card_type=None) -> str:
    """企微通知里的「系统用户」显示名：``用户名（卡型）``；无卡/未知卡型只显示用户名。"""
    name = str(username or "").strip() or "未归属"
    label = card_type_label(card_type)
    return f"{name}（{label}）" if label else name


PLATFORM_ALL = "all"
PLATFORM_XXQD = "xxqd"
PLATFORM_CLASS_CUBE = "class_cube"
PLATFORM_XUEXITONG = "xuexitong"
PLATFORM_DOUYIN = "douyin"
PLATFORM_JIELONG = "jielong"
PLATFORM_JRXY = "jrxy"
# 有序版本：`models.py` 用它拼 pydantic 的 Literal 联合类型，
# 保证「新增平台」只需改这一处，不会出现某个接口的枚举漏了新平台而 422。
PLATFORM_SCOPE_ORDER = (
    PLATFORM_ALL,
    PLATFORM_XXQD,
    PLATFORM_CLASS_CUBE,
    PLATFORM_XUEXITONG,
    PLATFORM_DOUYIN,
    PLATFORM_JIELONG,
    PLATFORM_JRXY,
)
PLATFORM_SCOPES = frozenset(PLATFORM_SCOPE_ORDER)

# 平台中文名（导出 CSV/Excel、通知文案共用一份）。前端对应
# frontend/src/utils/userMembership.js 的 PLATFORM_META[].label，改名字要两处同步。
PLATFORM_LABELS = {
    PLATFORM_ALL: "全部平台",
    PLATFORM_XXQD: "小小签到",
    PLATFORM_CLASS_CUBE: "班级魔方",
    PLATFORM_XUEXITONG: "学习通",
    PLATFORM_DOUYIN: "抖音续火",
    PLATFORM_JIELONG: "接龙管家",
    PLATFORM_JRXY: "今日校园",
}

# 账号删除/核销原因（user_removal_logs.reason）→ 中文，前端与通知共用
REMOVAL_REASON_LABELS = {
    "admin_delete": "管理员删除",
    "card_exhausted": "次卡用尽自动核销",
    "card_expired": "会员卡到期自动核销",
    "idle_unused": "连续 10 天未登录自动核销",
}


def removal_reason_label(reason: str | None) -> str:
    return REMOVAL_REASON_LABELS.get(str(reason or ""), "已删除")


def _as_datetime(value):
    """users.expires_at 可能是 datetime，也可能是接口里的 ISO 字符串。"""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    text = str(value).strip().replace("T", " ")
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def removal_reason_for_user(user, now: dt.datetime | None = None) -> str:
    """管理员删除会员时的留痕原因。

    已经是「核销状态」的会员被删，记核销原因而不是「管理员删除」——
    菜单修改记录、通知等页面要能区分这两种来源；正常（卡未到期、次数未用尽）
    的会员被删仍是 admin_delete。

    - 次卡用尽（已用 ≥ 总数）→ card_exhausted
    - 到期 / 已进入核销清理通道 → 次卡为 idle_unused，否则 card_expired
    - 无卡或卡仍有效 → admin_delete
    """
    user = user or {}
    card_type = str(user.get("card_type") or "")
    if not card_type:
        return "admin_delete"
    total = int(user.get("card_total_uses") or 0)
    used = int(user.get("card_used_count") or 0)
    if card_type == CARD_SINGLE and total and used >= total:
        return "card_exhausted"
    current = now or dt.datetime.now()
    expires_at = _as_datetime(user.get("expires_at"))
    settled = (
        user.get("card_status") == "expired"
        or bool(user.get("is_expired"))
        or bool(user.get("card_delete_due_at"))
        or (expires_at is not None and expires_at <= current)
    )
    if not settled:
        return "admin_delete"
    # 次卡没用完就被核销 = 长期未登录触发（用没用过都算）
    if card_type == CARD_SINGLE:
        return "idle_unused"
    return "card_expired"


DEFAULT_CARD_DELETE_DELAY_SECONDS = 60
MIN_CARD_DELETE_DELAY_SECONDS = 0
MAX_CARD_DELETE_DELAY_SECONDS = 24 * 60 * 60

DEFAULT_CARD_TOTAL_USES = 1
MIN_CARD_TOTAL_USES = 1
MAX_CARD_TOTAL_USES = 999

DEFAULT_LOCATION_SEARCH_DAILY_LIMIT = 100

# 次卡激活后连续未登录天数上限：超过即视为核销（停用访问、清理平台账号并归档）。
# 「激活」= 用户登录过一次；从未登录的卡不参与（白扣一张卡不合理）。
# 与「次卡用完次数后延迟失效」互补——那条管用完了，这条管激活后长期没人登录。
SINGLE_CARD_IDLE_DAYS = 10

# 用户管理页「临期提醒」默认窗口（天）：周期卡距到期不足该天数即计入徽标。
# 前端 UserManagement.vue 的 EXPIRING_WINDOW_DAYS 必须与此保持一致。
DEFAULT_EXPIRING_WINDOW_DAYS = 3
