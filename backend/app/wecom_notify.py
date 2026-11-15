"""企业微信 markdown 消息发送的共享实现。

三个平台的 Notifier（class_cube/miaoying/ai_tools）共用同一套
「post + errcode 校验」逻辑，只保留各自的文案模板。
"""

import requests


# 秒应 / AI工具签共用的触发方式文案（班级魔方在 class_cube_labels
# 里另有运行记录/通知两套口径，键与文案不同，不在此合并）。
WECOM_TRIGGER_NAMES = {
    "manual": "手动立即执行",
    "scheduler": "定时自动执行",
}


def post_markdown(
    url: str,
    content: str,
    error: type[Exception],
    message: str = "企业微信通知发送失败",
    *,
    with_errmsg: bool = False,
    http=None,
) -> None:
    """发送 markdown 消息；HTTP 失败或 errcode 非 0 时抛 error。

    with_errmsg=True 时在失败信息里附带企微返回的 errmsg（用于测试通知）。
    http 参数供测试注入替身。
    """
    http = http or requests
    try:
        response = http.post(
            url,
            json={"msgtype": "markdown", "markdown": {"content": content}},
            timeout=10,
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise error(message) from exc
    if not isinstance(payload, dict) or payload.get("errcode") != 0:
        if with_errmsg:
            detail = (
                str(payload.get("errmsg") or "企业微信拒绝了该 Webhook")
                if isinstance(payload, dict)
                else "企业微信返回异常"
            )
            raise error(f"{message}：{detail}")
        raise error(message)
