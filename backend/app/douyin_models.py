from pydantic import BaseModel, Field, field_validator, model_validator

from .task_date_schedule import normalize_task_date_rule


class DouyinAccountCreate(BaseModel):
    """Cookie 添加方式：三种形态（JSON 数组 / 请求头字符串 / name=value）。"""

    remark: str = Field(default="", max_length=255)
    cookie_input: str = Field(min_length=1, max_length=200_000)
    # 出口代理（douyin_proxies.id），0/None = 直连
    proxy_id: int = 0


class DouyinAccountUpdate(BaseModel):
    remark: str = Field(default="", max_length=255)
    enabled: bool = True
    # 可选：更新 cookie
    cookie_input: str = Field(default="", max_length=200_000)
    # 可选：切换出口代理；None = 不变，0 = 直连
    proxy_id: int | None = None


class DouyinProxyPayload(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    protocol: str = Field(default="http", max_length=16)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=255)
    enabled: bool = True

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, value: str):
        cleaned = value.strip().lower()
        if cleaned not in ("http", "https", "socks5"):
            raise ValueError("协议仅支持 http / https / socks5")
        return cleaned


class DouyinRetryTargetPayload(BaseModel):
    """单目标重试：运行记录里某个失败好友单独重发。"""

    target: str = Field(min_length=1, max_length=255)


class DouyinTaskPayload(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    targets: list[str] = Field(min_length=1)
    message_variants: list[str] = Field(default_factory=list)
    schedule_times: list[str] = Field(default_factory=lambda: ["08:00:00"])
    # 到点后随机等待区间（秒）：0 或不填 = 立即执行
    delay_min_seconds: int | None = Field(default=None, ge=0, le=600)
    delay_max_seconds: int | None = Field(default=None, ge=0, le=600)
    # 今日已成功续过的好友自动跳过（避免同一天重复打扰同一个人）
    skip_sent_today: bool = True
    start_date: str | None = None
    end_date: str | None = None
    skip_weekends: bool = False
    # 执行日期计划（与班级魔方同构）
    date_mode: str = "daily"
    run_dates: list[str] = Field(default_factory=list)
    skip_dates: list[str] = Field(default_factory=list)
    auto_disable_after_finish: bool = False
    notify_wecom: bool = True

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, values: list[str]):
        cleaned = [str(item).strip() for item in values if str(item).strip()]
        if not cleaned:
            raise ValueError("至少填写一个目标好友")
        return list(dict.fromkeys(cleaned))

    @field_validator("schedule_times")
    @classmethod
    def validate_times(cls, values: list[str]):
        import datetime

        result = []
        for value in values:
            datetime.time.fromisoformat(value)
            if value not in result:
                result.append(value)
        if not result:
            raise ValueError("至少设置一个执行时间")
        return sorted(result)

    @model_validator(mode="after")
    def validate_date_plan(self):
        plan = normalize_task_date_rule({
            "date_mode": self.date_mode,
            "run_dates": self.run_dates,
            "skip_dates": self.skip_dates,
        })
        self.date_mode = plan["date_mode"]
        self.run_dates = plan["run_dates"]
        self.skip_dates = plan["skip_dates"]
        return self


class DouyinBatchDelete(BaseModel):
    ids: list[int] = Field(min_length=1)


class DouyinBatchState(BaseModel):
    ids: list[int] = Field(min_length=1)
    enabled: bool


class DouyinGlobalSettings(BaseModel):
    message_variants: list[str] = Field(default_factory=list)
    hitokoto_types: list[str] = Field(default_factory=list)


class DouyinNotifySettings(BaseModel):
    douyin_webhook_url: str = Field(default="", max_length=2048)
