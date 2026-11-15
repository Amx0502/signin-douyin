"""settings.json 的统一读写原语。

此前 class_cube_settings / miaoying_settings / ai_tools_settings /
site_settings / checkin_delay_settings 五个模块各自复制了一份
「读 → 校验 → tempfile 原子写」逻辑，统一收敛到这里。
各业务模块只保留自己的默认值、键名与校验规则。
"""

import json
import os
import tempfile
from pathlib import Path

from .config import SETTINGS_FILE


class SettingsStoreError(ValueError):
    pass


def read_json_settings(
    path: Path = SETTINGS_FILE,
    error: type[ValueError] = SettingsStoreError,
) -> dict:
    """读取 settings.json 整个对象；文件不存在视为空对象。"""
    try:
        data = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {}
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise error("settings.json 读取失败") from exc
    if not isinstance(data, dict):
        raise error("settings.json 必须是对象格式")
    return data


def write_json_settings(data: dict, path: Path = SETTINGS_FILE) -> None:
    """原子写 settings.json：先写临时文件再 replace，避免写坏存量配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        Path(temporary).replace(path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def load_webhook_setting(
    key: str,
    path: Path = SETTINGS_FILE,
    error: type[ValueError] = SettingsStoreError,
) -> dict:
    """读取单个企微 webhook 配置（平台共享同一返回结构）。"""
    value = str(read_json_settings(path, error=error).get(key, "")).strip()
    return {key: value, "webhook_configured": bool(value)}


def save_webhook_setting(
    key: str,
    webhook_url: str,
    validate,
    error: type[ValueError] = SettingsStoreError,
    path: Path = SETTINGS_FILE,
) -> dict:
    """校验并落库单个企微 webhook 配置（平台共享同一返回结构）。"""
    try:
        value = validate(webhook_url)
    except ValueError as exc:
        raise error(str(exc)) from exc
    data = read_json_settings(path, error=error)
    data[key] = value
    write_json_settings(data, path)
    return {key: value, "webhook_configured": bool(value)}
