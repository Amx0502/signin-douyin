"""抖音续火数据库：建表、补列，并提供会话上下文。

独立版把数据库地址收敛成一个 SQLAlchemy URL（见 ``config.DOUYIN_DB_URL``）：
默认 SQLite 单文件，零依赖即可跑；填 MySQL URL 也能直接切换。
"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from . import config
from .douyin_db_models import DouyinBase


def orphan_not_null_columns(
    live_columns: list[dict], model_columns: set[str]
) -> list[dict]:
    """挑出存量库里「模型已删除、NOT NULL 且无默认值」的列。

    create_all 不会改动已存在的表，早期版本留下的历史列会一直躺在库里；
    SQLAlchemy 只按模型列拼 INSERT，于是这类列直接让写入失败。
    """
    return [
        column
        for column in live_columns
        if column.get("name")
        and column["name"] not in model_columns
        and not column.get("nullable", True)
        and column.get("default") is None
    ]


def add_missing_columns(engine: Engine, metadata: MetaData) -> None:
    """给存量表补齐模型新增的列（create_all 只建新表、从不改已有表）。

    新列一律按可空添加（ORM 侧有 python default 兜底），幂等，可重复执行。
    """
    try:
        inspector = inspect(engine)
    except Exception:
        return
    for table in metadata.sorted_tables:
        try:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
        except Exception:
            continue
        for column in table.columns:
            if column.name in existing:
                continue
            try:
                col_type = column.type.compile(dialect=engine.dialect)
                with engine.begin() as conn:
                    conn.execute(text(
                        f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type} NULL"
                    ))
            except Exception:
                continue


def backfill_profile_enabled(engine: Engine) -> None:
    """把存量账号的 profile_enabled 从 NULL 回填为 1（补列后 ORM default 不生效）。"""
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE douyin_accounts SET profile_enabled = 1 WHERE profile_enabled IS NULL"
            ))
    except Exception:
        return


def relax_orphan_columns(engine: Engine, metadata: MetaData) -> None:
    """把存量表里「模型已删除、NOT NULL 且无默认值」的历史列放宽为可空。

    不删列、不读写，只解除非空约束，避免 INSERT 被历史列拦死。
    MySQL 专用语法；SQLite 或权限不足时静默跳过。
    """
    try:
        inspector = inspect(engine)
    except Exception:
        return
    for table in metadata.sorted_tables:
        try:
            if not inspector.has_table(table.name):
                continue
            live = inspector.get_columns(table.name)
        except Exception:
            continue
        model_columns = set(table.columns.keys())
        for column in orphan_not_null_columns(live, model_columns):
            try:
                raw_type = str(column.get("type") or "TEXT")
                with engine.begin() as conn:
                    conn.execute(text(
                        f"ALTER TABLE {table.name} MODIFY {column['name']} {raw_type} NULL"
                    ))
            except Exception:
                continue


@dataclass(frozen=True)
class _UrlSettings:
    """把 SQLAlchemy URL 里的密码包成上游期望的 ``settings.password``。"""

    password: str = ""


def _engine_kwargs(url: str) -> dict:
    """SQLite 需要放开跨线程使用；其它方言用连接前探活。"""
    if url.startswith("sqlite"):
        return {
            "connect_args": {"check_same_thread": False, "timeout": 30},
            "pool_pre_ping": False,
        }
    return {"pool_pre_ping": True}


def _prepare_sqlite(engine: Engine) -> None:
    """文件型 SQLite 开 WAL：读不阻塞写，轮询接口不易报 database is locked。"""
    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA busy_timeout=30000"))
    except Exception:
        return


class DouyinDatabase:
    def __init__(self, url: str | None = None):
        self.url = (url or config.DOUYIN_DB_URL).strip()
        self.engine: Engine | None = None
        self.factory: sessionmaker[Session] | None = None

    @property
    def settings(self) -> "_UrlSettings":
        """兼容上游实现：未显式配置加密密钥时，用它派生 Fernet key。

        SQLite 没有密码，派生值为空 —— 那种情况请通过
        ``DOUYIN_TOKEN_ENCRYPTION_KEY`` 指定密钥，否则 cookie 明文入库。
        """
        try:
            password = make_url(self.url).password or ""
        except Exception:
            password = ""
        return _UrlSettings(password=str(password))

    def initialize(self) -> None:
        self._ensure_sqlite_dir()
        self.engine = create_engine(self.url, **_engine_kwargs(self.url))
        if self.url.startswith("sqlite"):
            _prepare_sqlite(self.engine)
        self.factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        DouyinBase.metadata.create_all(self.engine)
        add_missing_columns(self.engine, DouyinBase.metadata)
        relax_orphan_columns(self.engine, DouyinBase.metadata)
        backfill_profile_enabled(self.engine)

    def _ensure_sqlite_dir(self) -> None:
        prefix = "sqlite:///"
        if not self.url.startswith(prefix):
            return
        raw = self.url[len(prefix):]
        if not raw or raw == ":memory:":
            return
        Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def session(self) -> Iterator[Session]:
        if self.factory is None:
            raise RuntimeError("抖音续火数据库尚未初始化")
        session = self.factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
        self.engine = None
        self.factory = None
