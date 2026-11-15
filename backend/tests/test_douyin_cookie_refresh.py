"""抖音 cookie 保活与发送提速的单测（不打真实网络/浏览器）。"""
import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.douyin_browser_sender import _entry
from app.douyin_cookie_refresh import (
    CookieRefreshError,
    _is_alive,
    merge_cookies,
    refresh_cookies,
)
from app.douyin_db_models import DouyinAccountRow, DouyinBase, DouyinLogRow, DouyinTaskRow
from app.douyin_service import COOKIE_REFRESH_INTERVAL_HOURS, DouyinService
from cryptography.fernet import Fernet


class _TestSettings:
    password = "test-secret"


class _TestDatabase:
    def __init__(self):
        self.settings = _TestSettings()
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        DouyinBase.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)

    @contextmanager
    def session(self):
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _cookie(name, value, domain=".douyin.com", path="/", expires=None):
    item = {"name": name, "value": value, "domain": domain, "path": path}
    if expires is not None:
        item["expires"] = expires
    return item


class MergeCookieTests(unittest.TestCase):
    def test_fresh_value_wins_and_new_keys_append(self):
        old = [_cookie("sessionid", "old"), _cookie("ttwid", "stale")]
        new = [_cookie("ttwid", "fresh"), _cookie("msToken", "brand-new")]
        merged = merge_cookies(old, new, now=1000)
        by_name = {item["name"]: item["value"] for item in merged}
        # 同名新值优先；旧集合里未被回传但仍有效的保留；新出现的追加
        self.assertEqual(by_name, {"sessionid": "old", "ttwid": "fresh", "msToken": "brand-new"})
        # 顺序沿用旧集合，新键在末尾
        self.assertEqual([item["name"] for item in merged], ["sessionid", "ttwid", "msToken"])

    def test_expired_cookies_dropped_when_not_returned(self):
        old = [_cookie("dead", "x", expires=500), _cookie("keep", "y", expires=None)]
        merged = merge_cookies(old, [], now=1000)
        self.assertEqual([item["name"] for item in merged], ["keep"])

    def test_domain_and_path_partition_keys(self):
        old = [_cookie("sid", "a", domain=".douyin.com"), _cookie("sid", "b", domain="creator.douyin.com")]
        new = [_cookie("sid", "c", domain=".douyin.com")]
        merged = merge_cookies(old, new, now=1000)
        self.assertEqual(
            sorted((item["domain"], item["value"]) for item in merged),
            [(".douyin.com", "c"), ("creator.douyin.com", "b")],
        )

    def test_is_alive_accepts_session_cookies(self):
        for value in (None, "", -1, 0, "0"):
            self.assertTrue(_is_alive({"name": "n", "expires": value}, now=1000))
        self.assertFalse(_is_alive({"name": "n", "expires": 999}, now=1000))
        self.assertTrue(_is_alive({"name": "n", "expires": 1001}, now=1000))

    def test_refresh_cookies_returns_merged_set(self):
        old = [_cookie("sessionid", "old"), _cookie("ttwid", "stale")]
        fake_context_cookies = [_cookie("sessionid", "old"), _cookie("ttwid", "fresh")]

        class _Page:
            def __init__(self):
                self.visited = []

            def goto(self, url, **kwargs):
                self.visited.append(url)

            def wait_for_timeout(self, ms):
                return None

        class _Context:
            def __init__(self):
                self.page = _Page()

            def add_init_script(self, *_):
                return None

            def add_cookies(self, items):
                self.injected = items

            def new_page(self):
                return self.page

            def cookies(self):
                return fake_context_cookies

        class _Browser:
            def __init__(self):
                self.context = _Context()
                self.closed = False

            def new_context(self, **kwargs):
                return self.context

            def close(self):
                self.closed = True

        class _Playwright:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        browser = _Browser()
        with patch("playwright.sync_api.sync_playwright", return_value=_Playwright()), patch(
            "app.douyin_login._launch_chromium", return_value=browser
        ), patch("app.douyin_login.BROWSER_GUARD", __import__("threading").RLock()):
            result = refresh_cookies(old, settle_seconds=0)
        self.assertEqual({i["name"]: i["value"] for i in result["cookies"]},
                         {"sessionid": "old", "ttwid": "fresh"})
        self.assertTrue(browser.closed)
        self.assertEqual(len(result["visited"]), 2)

    def test_refresh_cookies_reports_missing_browser(self):
        import sys

        with patch.dict(sys.modules, {"playwright.sync_api": None}):
            with self.assertRaises(CookieRefreshError):
                refresh_cookies([_cookie("sessionid", "x")])


class CookieKeepAliveServiceTests(unittest.TestCase):
    def setUp(self):
        self.database = _TestDatabase()
        self.service = DouyinService(self.database)
        self.service.cipher = Fernet(Fernet.generate_key())
        self.user = {"id": 1, "role": "admin"}
        with self.database.session() as session:
            self.account = DouyinAccountRow(
                owner_user_id=1,
                remote_user_id="3991702063024905",
                remark="轻签网",
                cookies=self.service._encrypt(json.dumps([_cookie("sessionid", "old")], ensure_ascii=False)),
                status="active",
                enabled=True,
            )
            session.add(self.account)
            session.flush()
            self.account_id = self.account.id

    def tearDown(self):
        self.service.shutdown()
        self.database.engine.dispose()

    def _refreshed(self, cookies=None):
        return {"cookies": cookies or [_cookie("sessionid", "old"), _cookie("ttwid", "fresh")],
                "visited": ["https://www.douyin.com/"], "failed": []}

    def test_manual_refresh_persists_cookies_and_anchor(self):
        with patch("app.douyin_service.refresh_cookies", return_value=self._refreshed()), patch(
            "app.douyin_service.resolve_identity",
            return_value={"ok": True, "logged_in": True, "user_id": "3991702063024905",
                          "message": "", "auth_failed": False},
        ):
            result = self.service.refresh_account_cookies(self.account_id, self.user)
        self.assertTrue(result["ok"])
        with self.database.session() as session:
            row = session.get(DouyinAccountRow, self.account_id)
            self.assertIsNotNone(row.cookies_refreshed_at)
            self.assertEqual(row.status, "active")
            stored = json.loads(self.service._decrypt(row.cookies, "x"))
        self.assertEqual({i["name"]: i["value"] for i in stored}, {"sessionid": "old", "ttwid": "fresh"})

    def test_refresh_marks_relogin_when_session_invalid(self):
        from app.douyin_client import DouyinRemoteError

        with patch("app.douyin_service.refresh_cookies", return_value=self._refreshed()), patch(
            "app.douyin_service.resolve_identity",
            return_value={"ok": False, "logged_in": False, "user_id": "",
                          "message": "cookie 已失效", "auth_failed": True},
        ):
            result = self.service.refresh_account_cookies(self.account_id, self.user)
        self.assertFalse(result["ok"])
        self.assertTrue(result["auth_failed"])
        with self.database.session() as session:
            row = session.get(DouyinAccountRow, self.account_id)
            self.assertEqual(row.status, "relogin")
            # 即便失效也把回收到的 cookie 存下来（下次扫码/更新后再用）
            self.assertIsNotNone(row.cookies_refreshed_at)

    def test_browser_unavailable_does_not_touch_login_state(self):
        with patch("app.douyin_service.refresh_cookies", side_effect=CookieRefreshError("无内核")):
            result = self.service.refresh_account_cookies(self.account_id, self.user)
        self.assertFalse(result["ok"])
        with self.database.session() as session:
            row = session.get(DouyinAccountRow, self.account_id)
            self.assertEqual(row.status, "active")
            self.assertIsNone(row.cookies_refreshed_at)

    def test_scheduler_refreshes_only_due_accounts(self):
        fresh_at = datetime.now() - timedelta(hours=COOKIE_REFRESH_INTERVAL_HOURS - 1)
        with self.database.session() as session:
            session.get(DouyinAccountRow, self.account_id).cookies_refreshed_at = fresh_at
        submitted = []
        with patch.object(self.service._cookie_executor, "submit", side_effect=lambda fn, *a: submitted.append(a)):
            self.service._last_cookie_scan = 0.0
            self.service._maybe_refresh_cookies()
            self.assertEqual(submitted, [])

            # 超过间隔 → 到期，且锚点先顺延占位（防重入）
            with self.database.session() as session:
                session.get(DouyinAccountRow, self.account_id).cookies_refreshed_at = (
                    datetime.now() - timedelta(hours=COOKIE_REFRESH_INTERVAL_HOURS + 1)
                )
            self.service._last_cookie_scan = 0.0
            self.service._maybe_refresh_cookies()
        self.assertEqual(len(submitted), 1)
        with self.database.session() as session:
            self.assertIsNotNone(session.get(DouyinAccountRow, self.account_id).cookies_refreshed_at)


class ProtocolSkipTests(unittest.TestCase):
    def setUp(self):
        self.database = _TestDatabase()
        self.service = DouyinService(self.database)

    def tearDown(self):
        self.service.shutdown()
        self.database.engine.dispose()

    def test_protocol_channel_skipped_by_default(self):
        """协议栈缺 a_bogus 签名必然被拦：默认直接走浏览器，省掉每目标 ~70s 空转。"""
        called = {"protocol": 0}

        def _protocol(*args, **kwargs):
            called["protocol"] += 1
            return {"ok": True, "sent": [], "unresolved": []}

        browser_args = {}

        def _browser(cookies, messages, **kwargs):
            browser_args.update({"messages": messages})
            # 用生产同款条目结构（summarize_status 依赖 statusCode/statusMsg）
            return {"ok": True, "sent": [_entry(t, m, True, status_name="SucceededByBrowser") for t, m in messages.items()]}

        with patch("app.douyin_service.run_protocol_send", side_effect=_protocol), patch(
            "app.douyin_service.send_via_browser", side_effect=_browser
        ), patch("app.douyin_service.PROTOCOL_CHANNEL_ENABLED", False):
            result = self.service._send_with_browser_fallback(
                [_cookie("sessionid", "x")],
                {"轻签网": "火花"},
                data_dir=".",
                protocol_targets_cache=[],
                proxy=None,
                display_name="测试号",
                owner=1,
                account_id=1,
            )
        self.assertEqual(called["protocol"], 0)
        self.assertEqual(browser_args["messages"], {"轻签网": "火花"})
        self.assertTrue(result["ok"])

    def test_protocol_used_when_explicitly_enabled(self):
        called = {"protocol": 0}

        def _protocol(*args, **kwargs):
            called["protocol"] += 1
            return {"ok": True, "sent": [_entry("轻签网", "火花", True, status_name="Succeeded")], "unresolved": []}

        with patch("app.douyin_service.run_protocol_send", side_effect=_protocol), patch(
            "app.douyin_service.PROTOCOL_CHANNEL_ENABLED", True
        ):
            result = self.service._send_with_browser_fallback(
                [_cookie("sessionid", "x")],
                {"轻签网": "火花"},
                data_dir=".",
                protocol_targets_cache=[],
                proxy=None,
                display_name="测试号",
                owner=1,
                account_id=1,
            )
        self.assertEqual(called["protocol"], 1)
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
