"""运行时配置：路径、数据目录与加密密钥。

独立版只保留抖音续火实际用到的配置项，其余业务配置不在此仓库。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# backend/ 目录；.env 可放 backend/.env 或仓库根 .env
APP_DIR = Path(__file__).resolve().parent.parent
load_dotenv(APP_DIR / ".env", override=False)
load_dotenv(APP_DIR.parent / ".env", override=False)

SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", str(APP_DIR / "settings.json")))
LOG_DIR = APP_DIR / "logs"
DATA_DIR = APP_DIR / "data"

# 抖音账号 cookie / 浏览器 profile 落盘目录（profile 体积较大）
DOUYIN_DATA_DIR = Path(os.getenv("DOUYIN_DATA_DIR", str(DATA_DIR / "douyin")))
# cookie 加密密钥（Fernet）。不配置时明文存库，仅供本地调试；生产务必配置：
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
DOUYIN_TOKEN_ENCRYPTION_KEY = os.getenv("DOUYIN_TOKEN_ENCRYPTION_KEY", "").strip()

# 默认企微群机器人 webhook（可在设置页覆盖，落 SETTINGS_FILE）
DEFAULT_WEBHOOK_URL = os.getenv("DEFAULT_WEBHOOK_URL", "").strip()

# ---------------------------------------------------------------- 数据库
# 优先完整 URL（SQLite / MySQL 均可）；未配置时回退 SQLite 单文件，零依赖即可跑
_DEFAULT_SQLITE = DATA_DIR / "douyin.db"
DOUYIN_DB_URL = os.getenv("DOUYIN_DB_URL", "").strip() or (
    f"sqlite:///{_DEFAULT_SQLITE.as_posix()}"
)


def ensure_dirs() -> None:
    """确保运行期目录存在（数据、日志、抖音 profile、设置文件父目录）。"""
    for path in (APP_DIR, DATA_DIR, DOUYIN_DATA_DIR, LOG_DIR, SETTINGS_FILE.parent):
        path.mkdir(parents=True, exist_ok=True)
