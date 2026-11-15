"""抖音登录链路「提交慢 / 拿账号慢」修复的回归测试（2026-10-15）。

线上实测根因：``_dump_debug`` 每张快照 = 全页截图（10s 超时）＋ ``page.title()``
（无超时参数，吃 Playwright 默认 30s）＋ 全量 cookies，一次登录落 6~16 张，且全部
跑在登录**单线程 executor** 上；叠加 ``poll()`` 无去重地把 ``_check_login`` 灌满
队列 —— 用户的「提交验证码」「建号」只能排在这些任务后面。

这里覆盖修复后的四条不变量：
1. ``poll()`` 去重：同会话同时只有一个检查在跑/在队列里，且跑完能再入队；
2. ``_check_login`` 无论早退/异常都复位 ``session.checking``（否则永久锁死）；
3. 调试快照默认不执行（零成本）；
4. 非终态会话超过 TTL 一律作废（原来只处理 ``waiting``）。
"""

import threading
import unittest
from unittest import mock

from app import douyin_login as dl


def _fake_session(status="waiting", mode="qr"):
    session = dl._LoginSession("回归测试", 1)
    session.status = status
    session.mode = mode
    return session


class _RecorderManager(dl.DouyinLoginManager):
    """把 _submit 换成同步记录，避免测试里真起线程（线程会让断言不确定）。"""

    def __init__(self, submit_result=True):
        super().__init__(lambda payload, user: {"id": 1})
        self.submitted = []
        self._submit_result = submit_result

    def _submit(self, fn, *args):
        self.submitted.append((fn, args))
        return self._submit_result

    def register(self, session):
        self._sessions[session.qr_id] = session
        return session.qr_id


class PollDedupTests(unittest.TestCase):
    def setUp(self):
        self.manager = _RecorderManager()

    def _checks(self):
        return [call for call in self.manager.submitted if call[0] == self.manager._check_login]

    def test_poll_does_not_enqueue_twice_while_check_running(self):
        session = _fake_session()
        qr_id = self.manager.register(session)
        self.manager.poll(qr_id, {})
        self.assertEqual(len(self._checks()), 1)
        self.assertTrue(session.checking)
        # 检查还在跑（flag 未复位）时连续 poll 不应再入队
        self.manager.poll(qr_id, {})
        self.manager.poll(qr_id, {})
        self.assertEqual(len(self._checks()), 1)

    def test_poll_enqueues_again_after_check_finished(self):
        session = _fake_session()
        qr_id = self.manager.register(session)
        self.manager.poll(qr_id, {})
        session.checking = False  # 模拟 _check_login 的 finally 已复位
        self.manager.poll(qr_id, {})
        self.assertEqual(len(self._checks()), 2)

    def test_poll_resets_flag_when_submit_fails(self):
        manager = _RecorderManager(submit_result=False)
        session = _fake_session()
        qr_id = manager.register(session)
        manager.poll(qr_id, {})
        self.assertFalse(session.checking, "入队失败必须复位，否则该会话永久不再被检查")

    def test_poll_skips_check_for_terminal_status(self):
        session = _fake_session(status="confirmed")
        qr_id = self.manager.register(session)
        self.manager.poll(qr_id, {})
        self.assertEqual(self._checks(), [])

    def test_poll_raises_for_unknown_session(self):
        with self.assertRaises(dl.DouyinLoginError):
            self.manager.poll("nope", {})


class CheckLoginFlagTests(unittest.TestCase):
    def setUp(self):
        self.manager = _RecorderManager()

    def test_flag_reset_on_early_return(self):
        session = _fake_session(status="confirmed")
        session.checking = True
        self.manager._check_login(session)
        self.assertFalse(session.checking)

    def test_flag_reset_on_missing_context(self):
        session = _fake_session()
        session.checking = True
        session.context = None
        self.manager._check_login(session)
        self.assertFalse(session.checking)
        self.assertEqual(session.status, "error")

    def test_flag_reset_on_exception(self):
        session = _fake_session()
        session.checking = True
        with mock.patch.object(
            dl.DouyinLoginManager, "_check_login_impl", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.manager._check_login(session)
        self.assertFalse(session.checking)

    def test_new_session_defaults_to_not_checking(self):
        self.assertFalse(_fake_session().checking)


class DebugDumpTests(unittest.TestCase):
    def setUp(self):
        self.manager = _RecorderManager()

    def test_dump_debug_is_noop_when_disabled(self):
        if dl.LOGIN_DEBUG_ENABLED:
            self.skipTest("当前环境显式开启了 DOUYIN_LOGIN_DEBUG，跳过默认关闭断言")
        session = _fake_session()
        page = mock.Mock()
        session.page = page
        session.context = mock.Mock()
        self.manager._dump_debug(session, "probe")
        page.screenshot.assert_not_called()
        session.context.cookies.assert_not_called()

    def test_purge_debug_dir_survives_missing_directory(self):
        with mock.patch.object(dl, "DEBUG_DIR", "/definitely/not/here/dy-debug"):
            dl.DouyinLoginManager._purge_debug_dir()  # 不应抛异常

    def test_manager_init_purges_debug_dir(self):
        with mock.patch.object(dl.DouyinLoginManager, "_purge_debug_dir") as purge:
            dl.DouyinLoginManager(lambda payload, user: {"id": 1})
        purge.assert_called_once()


class ExpireTests(unittest.TestCase):
    def setUp(self):
        self.manager = _RecorderManager()

    def test_sms_session_expires(self):
        session = _fake_session(status="sms_sent", mode="sms")
        self.manager._expire(session)
        self.assertEqual(session.status, "expired")
        self.assertIn("超时", session.message)

    def test_qr_waiting_session_keeps_qr_copy(self):
        session = _fake_session(status="waiting", mode="qr")
        self.manager._expire(session)
        self.assertEqual(session.status, "expired")
        self.assertIn("二维码", session.message)

    def test_verification_required_expires(self):
        session = _fake_session(status="verification_required")
        self.manager._expire(session)
        self.assertEqual(session.status, "expired")

    def test_terminal_statuses_untouched(self):
        for status in ("confirmed", "error", "expired"):
            session = _fake_session(status=status)
            self.manager._expire(session)
            self.assertEqual(session.status, status)

    def test_expirable_statuses_match_poll_and_cleanup(self):
        """poll() 的过期判定与 _cleanup_expired_locked 必须共用同一份集合。"""
        session = _fake_session(status="sms_sent", mode="sms")
        session.created_at -= dl.SESSION_TTL_SECONDS + 1
        manager = _RecorderManager()
        qr_id = manager.register(session)
        manager.poll(qr_id, {})
        self.assertIn((manager._expire, (session,)), manager.submitted)
        manager._cleanup_expired_locked()
        self.assertNotIn(qr_id, manager._sessions)


class IdentityHintTests(unittest.TestCase):
    def setUp(self):
        self.manager = _RecorderManager()

    def _session_with_page(self):
        session = _fake_session()
        session.page = mock.Mock()
        return session

    def test_hint_is_cached_after_success(self):
        session = self._session_with_page()
        with mock.patch(
            "app.douyin_identity._read_ssr_identity",
            return_value={"user_id": "42", "nickname": "昵称"},
        ) as reader:
            first = self.manager._identity_hint(session)
            second = self.manager._identity_hint(session)
        self.assertEqual(first.get("remote_user_id"), "42")
        self.assertEqual(second.get("remote_user_id"), "42")
        self.assertEqual(reader.call_count, 1, "身份在一次登录内不变，命中后不应再 evaluate")

    def test_hint_failure_is_throttled(self):
        session = self._session_with_page()
        with mock.patch("app.douyin_identity._read_ssr_identity", return_value={}) as reader:
            self.assertEqual(self.manager._identity_hint(session), {})
            self.assertEqual(self.manager._identity_hint(session), {})
        self.assertEqual(
            reader.call_count,
            1,
            "失败也必须节流：page.evaluate 无超时参数，每 2.5s 一次会占死单线程",
        )

    def test_hint_retries_after_interval(self):
        session = self._session_with_page()
        with mock.patch("app.douyin_identity._read_ssr_identity", return_value={}) as reader:
            self.manager._identity_hint(session)
            session.identity_hint_at -= dl.IDENTITY_HINT_MIN_INTERVAL_SECONDS + 1
            self.manager._identity_hint(session)
        self.assertEqual(reader.call_count, 2)

    def test_hint_without_page_returns_empty(self):
        session = _fake_session()
        session.page = None
        session.context = None
        self.assertEqual(self.manager._identity_hint(session), {})


class SubmitTests(unittest.TestCase):
    def test_submit_reports_false_after_shutdown(self):
        manager = dl.DouyinLoginManager(lambda payload, user: {"id": 1})
        manager._executor.shutdown(wait=True)
        self.assertFalse(manager._submit(lambda: None))

    def test_submit_reports_true_while_running(self):
        manager = dl.DouyinLoginManager(lambda payload, user: {"id": 1})
        done = threading.Event()
        self.assertTrue(manager._submit(done.set))
        self.assertTrue(done.wait(5))
        manager._executor.shutdown(wait=True)


class PollIntervalConstantsTests(unittest.TestCase):
    def test_ui_poll_interval_is_gentler_than_qr(self):
        self.assertGreaterEqual(dl.UI_POLL_INTERVAL_MS, 2 * dl.QR_POLL_INTERVAL_MS)
        self.assertGreaterEqual(dl.IDENTITY_HINT_MIN_INTERVAL_SECONDS, 2)


if __name__ == "__main__":
    unittest.main()
