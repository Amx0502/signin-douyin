from pathlib import Path

from .class_cube_settings import validate_wecom_webhook
from .config import SETTINGS_FILE
from .settings_store import load_webhook_setting, save_webhook_setting


class DouyinSettingsError(ValueError):
    pass


_WEBHOOK_KEY = "douyin_webhook_url"


def load_douyin_settings(path: Path = SETTINGS_FILE) -> dict:
    return load_webhook_setting(_WEBHOOK_KEY, path, error=DouyinSettingsError)


def save_douyin_settings(webhook_url: str, path: Path = SETTINGS_FILE) -> dict:
    return save_webhook_setting(
        _WEBHOOK_KEY,
        webhook_url,
        validate_wecom_webhook,
        error=DouyinSettingsError,
        path=path,
    )
