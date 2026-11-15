"""抖音续火的企业微信通知文案（与班级魔方/秒应同一套 post_markdown 通道）。"""

from datetime import datetime

from .class_cube_settings import validate_wecom_webhook
from .membership_constants import owner_display_name
from .wecom_notify import post_markdown


class DouyinNotificationError(RuntimeError):
    pass


_TRIGGER_NAMES = {
    "manual": "手动立即执行",
    "batch": "批量立即执行",
    "scheduler": "定时自动执行",
}

_STATUS_TEXT = {
    "success": "成功",
    "partial": "部分成功",
    "failed": "失败",
    "skipped": "跳过",
    "running": "运行中",
}


class DouyinNotifier:
    def __init__(self, http=None):
        # http 供测试注入替身；None 时 wecom_notify 内部使用 requests
        self.http = http

    @staticmethod
    def _time_text(value) -> str:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M")
        text = str(value or datetime.now().isoformat())
        try:
            return datetime.fromisoformat(
                text.replace("Z", "+00:00")
            ).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return text.replace("T", " ")[:16]

    def send_summary(self, webhook_url: str, summary: dict) -> None:
        url = validate_wecom_webhook(webhook_url)
        if not url:
            return
        status = str(summary.get("status") or "")
        status_text = _STATUS_TEXT.get(status, status or "未知")
        ok_count = int(summary.get("sent_ok", 0) or 0)
        fail_count = int(summary.get("sent_fail", 0) or 0)

        detail_lines = []
        for detail in summary.get("details", []):
            if not isinstance(detail, dict):
                detail_lines.append(f"- {detail}")
                continue
            if detail.get("dry_run"):
                icon, note = "🧪", "演练（未真实发送）"
            elif detail.get("success"):
                icon, note = "✅", "发送成功"
            else:
                icon = "❌"
                note = str(detail.get("reason") or "发送失败")
            target = str(detail.get("target") or "-")
            detail_lines.append(f"- {icon} {target}：{note}")
        details = "\n".join(detail_lines)

        trigger = _TRIGGER_NAMES.get(
            str(summary.get("trigger") or ""), str(summary.get("trigger") or "-")
        )
        content = (
            "## 🔥 抖音续火通知\n\n"
            f"时间：{self._time_text(summary.get('started_at'))}\n\n"
            f"系统用户：{owner_display_name(summary.get('owner_username'), summary.get('owner_card_type'))}\n\n"
            f"任务：{summary.get('task_name', '-')}\n\n"
            f"账号：{summary.get('account_name', '-')}\n\n"
            f"触发：{trigger}\n\n"
            f"结果：{status_text}\n\n"
            f"{summary.get('message', '')}\n\n"
            f"成功：{ok_count} 个｜失败：{fail_count} 个"
        )
        if details:
            content = f"{content}\n\n{details}"
        post_markdown(
            url,
            content,
            DouyinNotificationError,
            http=self.http,
        )
