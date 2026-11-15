"""最小用户表：抖音任务用 ``owner_user_id`` 归属，通知里要显示用户名。

独立版不再维护会员卡/平台权限等业务表，只保留登录与归属展示所需字段。
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class AuthBase(DeclarativeBase):
    pass


# SQLite 只有 INTEGER PRIMARY KEY 才自增，MySQL 用 BIGINT
PK = BigInteger().with_variant(Integer, "sqlite")


class UserRow(AuthBase):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "is_active": bool(self.is_active),
        }
