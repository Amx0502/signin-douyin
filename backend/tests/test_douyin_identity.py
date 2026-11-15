"""主站身份解析（passport 接口优先）的单测（不打真实网络/浏览器）。

覆盖：
- fetch_passport_identity：成功 / 会话过期(error_code=13) / 网络异常 / 非 JSON
- resolve_identity：主站优先走 passport，接口不可用时回退浏览器
- _via_passport：成功结构、登出结构、不可用返回 None
"""
import unittest
from unittest.mock import MagicMock, patch

from app.douyin_client import DouyinRemoteError, fetch_passport_identity
from app.douyin_identity import _EMPTY, _via_passport, resolve_identity
from app.douyin_site import CREATOR, WEB


def _resp(status=200, payload=None, text=None):
    r = MagicMock()
    r.status_code = status
    if payload is not None:
        r.json.return_value = payload
    else:
        r.json.side_effect = ValueError("not json")
    if text is not None:
        r.text = text
    return r


_SUCCESS = {
    "message": "success",
    "data": {
        "user_id": 7690523788908004389,
        "sec_user_id": "MS4wLjABAAAAxxxx",
        "name": "测试昵称",
        "avatar_url": "https://x.test/a.jpg",
    },
}
_EXPIRED = {
    "message": "error",
    "data": {"error_code": 13, "description": "会话过期，请重新登录", "user_id": 0},
}

COOKIES = [{"name": "sessionid", "value": "abc", "domain": ".douyin.com"}]


class FetchPassportIdentityTest(unittest.TestCase):
    def test_success(self):
        with patch("app.douyin_client.requests.get", return_value=_resp(payload=_SUCCESS)) as get:
            result = fetch_passport_identity(COOKIES, site=WEB)
        self.assertTrue(result["logged_in"])
        self.assertEqual(result["user_id"], "7690523788908004389")
        self.assertEqual(result["sec_uid"], "MS4wLjABAAAAxxxx")
        self.assertEqual(result["nickname"], "测试昵称")
        self.assertEqual(result["avatar"], "https://x.test/a.jpg")
        # 校验用了站点配置里的 passport 接口
        self.assertEqual(get.call_args[0][0], WEB.passport_info_url)

    def test_session_expired(self):
        with patch("app.douyin_client.requests.get", return_value=_resp(payload=_EXPIRED)):
            result = fetch_passport_identity(COOKIES, site=WEB)
        self.assertFalse(result["logged_in"])
        self.assertIn("会话过期", result["message"])

    def test_network_error_is_retryable(self):
        import requests
        with patch("app.douyin_client.requests.get",
                   side_effect=requests.ConnectionError("boom")):
            with self.assertRaises(DouyinRemoteError) as ctx:
                fetch_passport_identity(COOKIES, site=WEB)
        self.assertTrue(ctx.exception.retryable)

    def test_non_json_is_retryable(self):
        with patch("app.douyin_client.requests.get", return_value=_resp(text="<html>")):
            with self.assertRaises(DouyinRemoteError) as ctx:
                fetch_passport_identity(COOKIES, site=WEB)
        self.assertTrue(ctx.exception.retryable)

    def test_no_passport_url_configured(self):
        with self.assertRaises(DouyinRemoteError):
            fetch_passport_identity(COOKIES, site=CREATOR)


class ViaPassportTest(unittest.TestCase):
    def test_success_structure(self):
        with patch("app.douyin_identity.fetch_passport_identity", return_value={
            "logged_in": True, "user_id": "123", "sec_uid": "sec",
            "nickname": "nick", "avatar": "av", "message": "",
        }):
            result = _via_passport(COOKIES, None)
        self.assertEqual(result["source"], "passport_api")
        self.assertEqual(result["user_id"], "123")
        self.assertEqual(result["douyin_id"], "")

    def test_logged_out(self):
        with patch("app.douyin_identity.fetch_passport_identity", return_value={
            "logged_in": False, "message": "会话过期，请重新登录",
        }):
            result = _via_passport(COOKIES, None)
        self.assertFalse(result["user_id"])
        self.assertTrue(result["auth_failed"])
        self.assertIn("会话过期", result["message"])

    def test_unavailable_returns_none(self):
        with patch("app.douyin_identity.fetch_passport_identity",
                   side_effect=DouyinRemoteError("网络异常", retryable=True)):
            self.assertIsNone(_via_passport(COOKIES, None))


class ResolveIdentityRoutingTest(unittest.TestCase):
    def test_web_prefers_passport(self):
        quick = {"ok": True, "logged_in": True, "user_id": "1", "source": "passport_api",
                 "sec_uid": "", "nickname": "", "douyin_id": "", "avatar": "", "message": ""}
        with patch("app.douyin_identity.active_site", return_value=WEB), \
             patch("app.douyin_identity._via_passport", return_value=quick) as via_passport, \
             patch("app.douyin_identity._via_browser") as via_browser:
            result = resolve_identity(COOKIES, account_id=8)
        self.assertEqual(result["source"], "passport_api")
        via_passport.assert_called_once()
        via_browser.assert_not_called()

    def test_web_falls_back_to_browser_when_unavailable(self):
        browser = {**_EMPTY, "user_id": "9", "source": "self_profile", "logged_in": True}
        with patch("app.douyin_identity.active_site", return_value=WEB), \
             patch("app.douyin_identity._via_passport", return_value=None), \
             patch("app.douyin_identity._via_browser", return_value=browser) as via_browser:
            result = resolve_identity(COOKIES, account_id=8)
        self.assertEqual(result["source"], "self_profile")
        via_browser.assert_called_once()

    def test_creator_goes_straight_to_api(self):
        with patch("app.douyin_identity.active_site", return_value=CREATOR), \
             patch("app.douyin_identity._via_creator", return_value={"source": "creator_api"}) as via_creator, \
             patch("app.douyin_identity._via_passport") as via_passport:
            result = resolve_identity(COOKIES)
        self.assertEqual(result["source"], "creator_api")
        via_creator.assert_called_once()
        via_passport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
