from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class DouyinBase(DeclarativeBase):
    pass


PK = BigInteger().with_variant(Integer, "sqlite")


class DouyinAccountRow(DouyinBase):
    __tablename__ = "douyin_accounts"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # 平台身份：creator IM user_token 接口返回的 user_id（同号唯一）
    remote_user_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    remark: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    nickname: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    douyin_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # 账号头像 URL（passport/profile 接口回填）
    avatar: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # 登录方式标记：cookie / qrcode
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="cookie")
    # 出口代理（douyin_proxies.id），解决服务器机房 IP 风控；空 = 直连
    proxy_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cookies: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 发送通道的会话缓存（secUid/peerUserId），由 protocol sender 回写
    protocol_targets_cache: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 平台真实火花快照（protocol spark_state 模式回写）：
    # {sparks: [{nickname, sec_uid, spark:{state,days,text,...}}], pending_today: int}
    spark_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # 快照对应的读取时刻（None = 从未读过平台火花）
    spark_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # cookie 保活（带旧 cookie 访问抖音回收新 cookie）的上次成功/尝试时刻
    cookies_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # P1 持久浏览器 profile：该账号是否启用固定用户目录（登录态与设备指纹一起续命）
    profile_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # profile 上次同步时刻（建号/保活/发送写盘后回填）
    profile_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class DouyinTaskRow(DouyinBase):
    __tablename__ = "douyin_tasks"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("douyin_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 目标好友：昵称或抖音号（发送通道按昵称在会话列表中解析）
    targets: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 模板变体池：为空时回退全局设置，再回退内置默认
    message_variants: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    schedule_times: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 到点后随机等待区间（秒，NULL/0 表示不等待）：像真人一样错开固定时刻
    delay_min_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delay_max_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 今日已成功续过的好友自动跳过（默认开），避免同一天重复发同一个人
    skip_sent_today: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 运行结束后是否推送企业微信通知（机器人地址在全局通知设置里配置）
    notify_wecom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    start_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    end_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    skip_weekends: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 执行日期计划（与班级魔方同构）：daily=每天 / specific=指定日期
    date_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="daily")
    run_dates: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    skip_dates: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    auto_disable_after_finish: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_schedule_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class DouyinRunRow(DouyinBase):
    __tablename__ = "douyin_runs"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    schedule_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    trigger: Mapped[str] = mapped_column(String(24), nullable=False, default="scheduler")
    # success / partial / failed / skipped / running
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # sent: [{target, message, success, status_code, status_name, reason, dry_run}]
    response_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 随机延迟：到点后才投递执行（NULL=立即）。挂库而非内存，进程重启不丢待投递任务
    deliver_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DouyinLogRow(DouyinBase):
    __tablename__ = "douyin_logs"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    # account / friends / fire / task / scheduler / login / system
    category: Mapped[str] = mapped_column(String(24), nullable=False, default="system", index=True)
    # info / success / warning / error
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info", index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now, index=True)


class DouyinSettingRow(DouyinBase):
    __tablename__ = "douyin_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class DouyinProxyRow(DouyinBase):
    __tablename__ = "douyin_proxies"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # http / https / socks5（socks5 出站依赖 PySocks，未装时 requests 会报错）
    protocol: Mapped[str] = mapped_column(String(16), nullable=False, default="http")
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    password: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
