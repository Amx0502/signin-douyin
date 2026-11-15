import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

from .config import SETTINGS_FILE
from .settings_store import load_webhook_setting, save_webhook_setting


class ClassCubeSettingsError(ValueError):
    pass


_WEBHOOK_KEY = "class_cube_webhook_url"
_WECOM_WEBHOOK_HOST = "qyapi.weixin.qq.com"
_WECOM_WEBHOOK_PATH = "/cgi-bin/webhook/send"
_WECOM_WEBHOOK_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def validate_wecom_webhook(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
        query = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
        port = parsed.port
    except ValueError as exc:
        raise ClassCubeSettingsError("企业微信机器人地址无效") from exc
    if not (
        parsed.scheme.lower() == "https"
        and parsed.hostname == _WECOM_WEBHOOK_HOST
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == _WECOM_WEBHOOK_PATH
        and not parsed.params
        and not parsed.fragment
        and len(query) == 1
        and query[0][0] == "key"
        and _WECOM_WEBHOOK_KEY.fullmatch(query[0][1])
    ):
        raise ClassCubeSettingsError("企业微信机器人地址无效")
    return (
        f"https://{_WECOM_WEBHOOK_HOST}{_WECOM_WEBHOOK_PATH}?"
        f"{urlencode(query)}"
    )


def load_class_cube_settings(path: Path = SETTINGS_FILE) -> dict:
    return load_webhook_setting(
        _WEBHOOK_KEY, path, error=ClassCubeSettingsError
    )


def save_class_cube_settings(
    webhook_url: str,
    path: Path = SETTINGS_FILE,
) -> dict:
    return save_webhook_setting(
        _WEBHOOK_KEY,
        webhook_url,
        validate_wecom_webhook,
        error=ClassCubeSettingsError,
        path=path,
    )

