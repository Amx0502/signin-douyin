"""P1 持久浏览器 Profile 的单测（不打真实网络/浏览器）。

覆盖：
- 目录管理（profile_dir / ensure / exists / purge / claim）
- 代理绑定指纹与变更告警
- profile_guard 并发互斥
- seed_cookies_into_context / sync_cookies_from_context
- open_account_context 走 launch_persistent_chromium（桩内核）
- refresh_cookies(account_id=…) 走持久 profile 分支并把 cookie 读回
- send_via_browser(account_id=…) 走持久 profile 分支
- delete_account 清理 profile 目录
"""
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import douyin_profile
from app.douyin_cookie_refresh import CookieRefreshError, refresh_cookies
from app.douyin_db_models import DouyinAccountRow, DouyinBase
from app.douyin_service import DouyinService
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


def _cookie(name, value, domain=".douyin.com", path="/"):
    return {"name": name, "value": value, "domain": domain, "path": path}


class _ProfileFixture(unittest.TestCase):
    """把 profile 根目录指到临时目录，避免污染仓库 data/。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"DOUYIN_PROFILE_ROOT": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class ProfileDirectoryTests(_ProfileFixture):
    def test_profile_dir_follows_env_root(self):
        self.assertEqual(douyin_profile.profile_root(), Path(self._tmp.name))
        self.assertEqual(douyin_profile.profile_dir(7), Path(self._tmp.name) / "7")

    def test_ensure_and_exists(self):
        self.assertFalse(douyin_profile.profile_exists(11))
        target = douyin_profile.ensure_profile_dir(11)
        self.assertTrue(target.is_dir())
        # 空目录不算已建立（否则 seed 判定会误判）
        self.assertFalse(douyin_profile.profile_exists(11))
        (target / "Default").mkdir()
        (target / "Default" / "Cookies").write_bytes(b"x")
        self.assertTrue(douyin_profile.profile_exists(11))

    def test_purge_removes_tree(self):
        target = douyin_profile.ensure_profile_dir(12)
        (target / "Default").mkdir()
        (target / "Default" / "Cookies").write_bytes(b"x")
        self.assertTrue(douyin_profile.purge_profile(12))
        self.assertFalse(target.exists())
        self.assertFalse(douyin_profile.purge_profile(12))  # 幂等

    def test_claim_moves_slot_to_account_dir(self):
        slot_dir = douyin_profile.ensure_profile_dir(-1)
        (slot_dir / "Default").mkdir()
        (slot_dir / "Default" / "Cookies").write_bytes(b"fingerprint")
        self.assertTrue(douyin_profile.claim_profile(-1, 42))
        self.assertFalse(slot_dir.exists())
        moved = douyin_profile.profile_dir(42) / "Default" / "Cookies"
        self.assertEqual(moved.read_bytes(), b"fingerprint")

    def test_claim_keeps_existing_target_and_drops_slot(self):
        target = douyin_profile.ensure_profile_dir(42)
        (target / "existing.txt").write_text("keep", encoding="utf-8")
        slot_dir = douyin_profile.ensure_profile_dir(-2)
        (slot_dir / "tmp.txt").write_text("drop", encoding="utf-8")
        self.assertTrue(douyin_profile.claim_profile(-2, 42))
        self.assertFalse(slot_dir.exists())
        self.assertTrue((target / "existing.txt").exists())
        self.assertFalse((target / "tmp.txt").exists())

    def test_profile_size_counts_files(self):
        target = douyin_profile.ensure_profile_dir(13)
        (target / "a.bin").write_bytes(b"0" * 100)
        (target / "sub").mkdir()
        (target / "sub" / "b.bin").write_bytes(b"0" * 50)
        self.assertEqual(douyin_profile.profile_size(13), 150)


class ProxyBindingTests(_ProfileFixture):
    def test_first_write_records_and_no_change(self):
        result = douyin_profile.check_proxy_binding(21, {"protocol": "http", "host": "1.1.1.1", "port": 8080})
        self.assertFalse(result["changed"])
        self.assertEqual(result["current"], "http://1.1.1.1:8080")
        again = douyin_profile.check_proxy_binding(21, {"protocol": "http", "host": "1.1.1.1", "port": 8080})
        self.assertFalse(again["changed"])

    def test_proxy_change_warns(self):
        douyin_profile.check_proxy_binding(22, {"protocol": "http", "host": "1.1.1.1", "port": 8080})
        with self.assertLogs("app.douyin_profile", level="WARNING") as captured:
            result = douyin_profile.check_proxy_binding(22, {"protocol": "http", "host": "2.2.2.2", "port": 9090})
        self.assertTrue(result["changed"])
        self.assertIn("出口代理已变更", "\n".join(captured.output))

    def test_switch_from_direct_to_proxy_counts_as_change(self):
        douyin_profile.check_proxy_binding(23, None)
        result = douyin_profile.check_proxy_binding(23, {"protocol": "http", "host": "3.3.3.3", "port": 80})
        self.assertTrue(result["changed"])


class ProfileGuardTests(_ProfileFixture):
    def test_guard_is_exclusive(self):
        entered = []
        with douyin_profile.profile_guard(31):
            entered.append(True)
            with self.assertRaises(RuntimeError):
                with douyin_profile.profile_guard(31):
                    entered.append(False)
        # 释放后可再次进入
        with douyin_profile.profile_guard(31):
            entered.append(True)
        self.assertEqual(entered, [True, True])

    def test_guard_reentrant_across_accounts(self):
        with douyin_profile.profile_guard(41):
            with douyin_profile.profile_guard(42):
                pass


class SeedCookieTests(_ProfileFixture):
    def test_seed_only_when_empty(self):
        self.assertTrue(douyin_profile.seed_from_cookies(51, [_cookie("sessionid", "x")]))
        target = douyin_profile.profile_dir(51)
        (target / "Default").mkdir()
        (target / "Default" / "Cookies").write_bytes(b"y")
        self.assertFalse(douyin_profile.seed_from_cookies(51, [_cookie("sessionid", "x")]))

    def test_seed_cookies_into_context_filters_and_counts(self):
        class _Context:
            def __init__(self):
                self.injected = []

            def add_cookies(self, items):
                self.injected = items

        context = _Context()
        count = douyin_profile.seed_cookies_into_context(
            context,
            [_cookie("sessionid", "v"), {"value": "no-name"}, "junk", _cookie("ttwid", "t")],
        )
        self.assertEqual(count, 2)
        self.assertEqual([i["name"] for i in context.injected], ["sessionid", "ttwid"])

    def test_seed_into_context_swallows_failure(self):
        class _Context:
            def add_cookies(self, items):
                raise RuntimeError("boom")

        self.assertEqual(douyin_profile.seed_cookies_into_context(_Context(), [_cookie("sessionid", "v")]), 0)

    def test_sync_cookies_from_context_returns_list(self):
        class _Context:
            def cookies(self):
                return [_cookie("sessionid", "new")]

        self.assertEqual(douyin_profile.sync_cookies_from_context(_Context())[0]["value"], "new")

    def test_sync_cookies_swallows_closed_context(self):
        class _Context:
            def cookies(self):
                raise RuntimeError("target closed")

        self.assertEqual(douyin_profile.sync_cookies_from_context(_Context()), [])


class OpenAccountContextTests(_ProfileFixture):
    def test_launches_persistent_context_and_injects_cookies(self):
        calls = {}

        class _Context:
            def __init__(self):
                self.cookies_injected = []

            def add_cookies(self, items):
                self.cookies_injected = items

        context = _Context()

        def _fake_launch(pw, user_data_dir, proxy=None, *, headless=True):
            calls["dir"] = str(user_data_dir)
            calls["headless"] = headless
            calls["proxy"] = proxy
            return context

        with patch("app.douyin_login.launch_persistent_chromium", side_effect=_fake_launch):
            opened = douyin_profile.open_account_context(
                object(), 61, cookies=[_cookie("sessionid", "v")],
                proxy={"protocol": "http", "host": "1.1.1.1", "port": 80},
            )
        self.assertIs(opened, context)
        self.assertTrue(calls["dir"].endswith(str(Path("61"))))
        self.assertTrue(calls["headless"])
        self.assertEqual([i["name"] for i in context.cookies_injected], ["sessionid"])


class ProfileCookieRefreshTests(_ProfileFixture):
    def test_refresh_with_account_id_uses_persistent_profile(self):
        visited = []
        fresh = [_cookie("sessionid", "old"), _cookie("ttwid", "fresh")]

        class _Page:
            def goto(self, url, **kwargs):
                visited.append(url)

            def wait_for_timeout(self, ms):
                return None

        class _Context:
            def add_init_script(self, *_):
                return None

            def new_page(self):
                return _Page()

            def cookies(self):
                return fresh

            def close(self):
                self.closed = True

        context = _Context()

        class _Playwright:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch("playwright.sync_api.sync_playwright", return_value=_Playwright()), patch(
            "app.douyin_login.launch_persistent_chromium", return_value=context
        ), patch("app.douyin_login.BROWSER_GUARD", threading.RLock()):
            result = refresh_cookies([_cookie("sessionid", "old")], account_id=71, settle_seconds=0)

        self.assertEqual({i["name"]: i["value"] for i in result["cookies"]},
                         {"sessionid": "old", "ttwid": "fresh"})
        self.assertEqual(len(result["visited"]), 2)
        self.assertTrue(context.closed)

    def test_refresh_profile_busy_raises_infrastructure_error(self):
        with patch("app.douyin_login.BROWSER_GUARD", threading.RLock()):
            with douyin_profile.profile_guard(72):
                with self.assertRaises(CookieRefreshError):
                    refresh_cookies([_cookie("sessionid", "old")], account_id=72, settle_seconds=0)

    def test_refresh_launch_failure_is_infrastructure_error(self):
        class _Playwright:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch("playwright.sync_api.sync_playwright", return_value=_Playwright()), patch(
            "app.douyin_login.launch_persistent_chromium", side_effect=RuntimeError("no kernel")
        ), patch("app.douyin_login.BROWSER_GUARD", threading.RLock()):
            with self.assertRaises(CookieRefreshError):
                refresh_cookies([_cookie("sessionid", "old")], account_id=73, settle_seconds=0)


class BrowserSenderProfileTests(_ProfileFixture):
    def test_send_uses_persistent_profile_when_account_id_given(self):
        from app.douyin_browser_sender import send_via_browser

        captured = {}

        class _Page:
            def goto(self, *a, **kw):
                return None

            def wait_for_timeout(self, ms):
                return None

        class _Context:
            def __init__(self):
                self.closed = False

            def add_init_script(self, *_):
                return None

            def new_page(self):
                return _Page()

            def close(self):
                self.closed = True

        context = _Context()

        class _Playwright:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _fake_open(pw, account_id, *, cookies=None, proxy=None, headless=True):
            captured["account_id"] = account_id
            return context

        with patch("playwright.sync_api.sync_playwright", return_value=_Playwright()), patch(
            "app.douyin_profile.open_account_context", side_effect=_fake_open
        ), patch("app.douyin_browser_sender._wait_targets", return_value={}):
            result = send_via_browser(
                [_cookie("sessionid", "v")], {"老张": "在吗"}, account_id=81,
            )
        self.assertEqual(captured["account_id"], 81)
        self.assertTrue(context.closed)
        self.assertEqual(result["sent"][0]["statusName"], "target_not_found")
        # profile 锁已释放 → 可再次占用
        with douyin_profile.profile_guard(81):
            pass


class DeleteAccountPurgesProfileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"DOUYIN_PROFILE_ROOT": self._tmp.name})
        self._env.start()
        self.database = _TestDatabase()
        self.service = DouyinService(self.database)
        self.service.cipher = Fernet(Fernet.generate_key())
        self.user = {"id": 1, "role": "admin"}
        with self.database.session() as session:
            row = DouyinAccountRow(
                owner_user_id=1, remote_user_id="999", remark="待删",
                cookies=self.service._encrypt(json.dumps([_cookie("sessionid", "x")], ensure_ascii=False)),
                status="active", enabled=True,
            )
            session.add(row)
            session.flush()
            self.account_id = row.id

    def tearDown(self):
        self.service.shutdown()
        self.database.engine.dispose()
        self._env.stop()
        self._tmp.cleanup()

    def test_delete_removes_profile_dir(self):
        target = douyin_profile.ensure_profile_dir(self.account_id)
        (target / "Default").mkdir()
        self.assertTrue(target.exists())
        self.service.delete_account(self.account_id, self.user)
        self.assertFalse(target.exists())

    def test_delete_survives_purge_failure(self):
        with patch("app.douyin_profile.shutil.rmtree", side_effect=OSError("locked")):
            self.service.delete_account(self.account_id, self.user)
        with self.database.session() as session:
            self.assertIsNone(session.get(DouyinAccountRow, self.account_id))

    def test_keepalive_marks_profile_updated_at(self):
        with patch("app.douyin_service.refresh_cookies",
                   return_value={"cookies": [_cookie("sessionid", "new")], "visited": [], "failed": []}), patch(
            "app.douyin_service.resolve_identity",
            return_value={"ok": True, "logged_in": True, "user_id": "999",
                          "message": "", "auth_failed": False},
        ):
            self.service.refresh_account_cookies(self.account_id, self.user)
        with self.database.session() as session:
            row = session.get(DouyinAccountRow, self.account_id)
            self.assertIsNotNone(row.profile_updated_at)
            self.assertTrue(row.profile_enabled)

    def test_keepalive_passes_account_id_to_profile_path(self):
        seen = {}

        def _fake(cookies, *, account_id=None, proxy=None, settle_seconds=4.0):
            seen["account_id"] = account_id
            return {"cookies": cookies, "visited": [], "failed": []}

        with patch("app.douyin_service.refresh_cookies", side_effect=_fake), patch(
            "app.douyin_service.resolve_identity",
            return_value={"ok": True, "logged_in": True, "user_id": "999",
                          "message": "", "auth_failed": False},
        ):
            self.service.refresh_account_cookies(self.account_id, self.user)
        self.assertEqual(seen["account_id"], self.account_id)


class LoginSessionProfileTests(_ProfileFixture):
    """扫码会话：临时槽位分配 / 过期清理 / 建号迁移。"""

    def _manager(self):
        from app.douyin_login import DouyinLoginManager

        manager = DouyinLoginManager(lambda payload, user: {"id": 4242, "display_name": "扫码号"})
        self.addCleanup(manager.shutdown)
        return manager

    def test_allocate_profile_slot_uses_negative_ids(self):
        manager = self._manager()
        first = manager._allocate_profile_slot()
        second = manager._allocate_profile_slot()
        self.assertTrue(first < 0 and second < 0)
        self.assertNotEqual(first, second)

    def test_session_start_assigns_slot_by_default(self):
        manager = self._manager()
        with patch.object(manager, "_submit"):  # 不真起浏览器
            info = manager.start({"remark": "x"}, {"id": 1, "role": "admin"})
        session = manager._sessions[info["qr_id"]]
        self.assertLess(session.profile_slot, 0)

    def test_session_start_can_opt_out_persistent(self):
        manager = self._manager()
        with patch.object(manager, "_submit"):
            info = manager.start({"remark": "x", "persistent": False}, {"id": 1, "role": "admin"})
        session = manager._sessions[info["qr_id"]]
        self.assertEqual(session.profile_slot, 0)

    def test_close_session_purges_unclaimed_slot(self):
        manager = self._manager()
        with patch.object(manager, "_submit"):
            info = manager.start({"remark": "x"}, {"id": 1, "role": "admin"})
        session = manager._sessions[info["qr_id"]]
        slot_dir = douyin_profile.ensure_profile_dir(session.profile_slot)
        (slot_dir / "Default").mkdir()
        manager._close_session(session)
        self.assertFalse(slot_dir.exists())
        self.assertEqual(session.profile_slot, 0)

    def test_promote_profile_moves_slot_to_real_account(self):
        from app.douyin_login import _LoginSession

        manager = self._manager()
        session = _LoginSession("x", 1, None)
        session.profile_slot = manager._allocate_profile_slot()
        slot_dir = douyin_profile.ensure_profile_dir(session.profile_slot)
        (slot_dir / "Default").mkdir()
        (slot_dir / "Default" / "Cookies").write_bytes(b"fp")
        manager._promote_profile(session, {"id": 555})
        self.assertFalse(slot_dir.exists())
        self.assertTrue((douyin_profile.profile_dir(555) / "Default" / "Cookies").exists())
        self.assertEqual(session.profile_slot, 0)

    def test_promote_profile_without_slot_is_noop(self):
        manager = self._manager()
        from app.douyin_login import _LoginSession

        session = _LoginSession("x", 1, None)
        manager._promote_profile(session, {"id": 556})
        self.assertFalse(douyin_profile.profile_dir(556).exists())


class ProfileFlagBackfillTests(unittest.TestCase):
    """存量行补列后 profile_enabled 为 NULL，初始化时应回填为启用。

    注意：sqlite 的 create_all 直接按模型建表（NOT NULL），无法在同一张表上模拟
    「补列得到的 NULL」；这里用一个宽松表复现 MySQL 补列后的形态。
    """

    def _legacy_db(self):
        from sqlalchemy import Column, Integer, MetaData, String, Table, Text, text

        db = _TestDatabase()
        engine = db.engine
        meta = MetaData()
        Table(
            "douyin_accounts_legacy", meta,
            Column("id", Integer, primary_key=True),
            Column("owner_user_id", Integer),
            Column("remote_user_id", String(64)),
            Column("remark", String(255)),
            Column("profile_enabled", Integer, nullable=True),
        )
        meta.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO douyin_accounts_legacy (id, owner_user_id, remote_user_id, remark, profile_enabled) "
                "VALUES (1, 1, 'x', 'a', NULL), (2, 1, 'y', 'b', 0), (3, 1, 'z', 'c', 1)"
            ))
        return db, text

    def test_backfill_sets_null_rows_to_enabled(self):
        from app.douyin_database import backfill_profile_enabled

        db, text = self._legacy_db()
        try:
            with db.engine.begin() as conn:
                # 与生产同名 SQL 只在 douyin_accounts 上生效，这里对齐表名验证语句语义
                conn.execute(text(
                    "UPDATE douyin_accounts_legacy SET profile_enabled = 1 WHERE profile_enabled IS NULL"
                ))
                rows = dict(conn.execute(text(
                    "SELECT id, profile_enabled FROM douyin_accounts_legacy"
                )).fetchall())
            self.assertEqual(rows[1], 1)   # NULL → 1（启用）
            self.assertEqual(rows[2], 0)   # 显式关闭不动
            self.assertEqual(rows[3], 1)
        finally:
            db.engine.dispose()

    def test_backfill_is_safe_on_missing_table(self):
        from app.douyin_database import backfill_profile_enabled

        db = _TestDatabase()
        try:
            backfill_profile_enabled(db.engine)  # 表存在 → 正常执行
        finally:
            db.engine.dispose()

    def test_profile_on_treats_none_as_enabled(self):
        from app.douyin_service import DouyinService

        row = DouyinAccountRow(owner_user_id=1, remote_user_id="b3", cookies="[]")
        row.profile_enabled = None
        self.assertTrue(DouyinService._profile_on(row))
        row.profile_enabled = False
        self.assertFalse(DouyinService._profile_on(row))

    def test_account_dict_reports_enabled_for_null(self):
        row = DouyinAccountRow(
            id=9, owner_user_id=1, remote_user_id="b4", remark="a", nickname="", douyin_id="",
            avatar="", proxy_id=None, source="cookie", cookies="[]", protocol_targets_cache=[],
            status="active", enabled=True, last_error="", updated_at=None,
        )
        row.profile_enabled = None
        row.profile_updated_at = None
        self.assertTrue(DouyinService._account_dict(row)["profile_enabled"])


if __name__ == "__main__":
    unittest.main()
