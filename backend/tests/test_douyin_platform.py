import json
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import (
    Boolean, Column, DateTime, Integer, JSON, MetaData, select, String, Table, Text, create_engine,
)
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.douyin_client import (
    DEFAULT_MESSAGE_VARIANTS,
    DouyinCookieError,
    auth_cookie_names,
    build_messages_by_target,
    normalize_cookie,
    parse_cookie_input,
    render_template,
    require_auth_cookies,
    run_protocol_send,
    summarize_status,
    to_cookie_string,
)
from app.douyin_browser_sender import BrowserSendError
from app.douyin_database import add_missing_columns
from app.douyin_db_models import DouyinAccountRow, DouyinBase, DouyinRunRow, DouyinTaskRow
from app.douyin_service import DouyinNotFound, DouyinService, DouyinValidationError


class _TestSettings:
    name = "signin"
    password = "test-password"


class _TestDatabase:
    def __init__(self):
        self.settings = _TestSettings()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        DouyinBase.metadata.create_all(self.engine)

    @contextmanager
    def session(self):
        session = self.factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self):
        self.engine.dispose()


class CookieParseTests(unittest.TestCase):
    def test_parse_header_string(self):
        cookies = parse_cookie_input("sessionid=abc; ttwid=1;  uid_tt_ss= xyz ")
        self.assertEqual(len(cookies), 3)
        self.assertEqual(cookies[0], {"name": "sessionid", "value": "abc", "domain": ".douyin.com", "path": "/", "expires": -1})
        self.assertEqual(cookies[2]["value"], "xyz")

    def test_parse_json_array_and_cookie_prefix(self):
        raw = 'cookie: [{"name":"sessionid","value":"v1"}]'
        cookies = parse_cookie_input(raw)
        self.assertEqual(cookies[0]["name"], "sessionid")
        self.assertEqual(cookies[0]["domain"], ".douyin.com")

    def test_parse_json_wrapper_object(self):
        raw = json.dumps({"cookies": [{"name": "sessionid_ss", "value": "v"}]})
        cookies = parse_cookie_input(raw)
        self.assertEqual(cookies[0]["name"], "sessionid_ss")

    def test_missing_auth_cookie_rejected(self):
        with self.assertRaises(DouyinCookieError) as ctx:
            require_auth_cookies(parse_cookie_input("ttwid=1; msToken=x"))
        self.assertIn("sessionid", str(ctx.exception))

    def test_empty_input_rejected(self):
        with self.assertRaises(DouyinCookieError):
            parse_cookie_input("   ")
        with self.assertRaises(DouyinCookieError):
            parse_cookie_input(None)

    def test_normalize_cookie_expires_and_domain(self):
        cookie = normalize_cookie({"name": "a", "value": "b", "domain": "douyin.com", "expires": "123.9"})
        self.assertEqual(cookie["domain"], ".douyin.com")
        self.assertEqual(cookie["expires"], 123.9)

    def test_cookie_string_roundtrip(self):
        cookies = parse_cookie_input("sessionid=abc; uid_tt=1")
        self.assertEqual(to_cookie_string(cookies), "sessionid=abc; uid_tt=1")
        self.assertIn("sessionid", auth_cookie_names(cookies))


class TemplateTests(unittest.TestCase):
    def test_render_placeholders(self):
        from datetime import date

        result = render_template("[data] [weekday] 续火", today=date(2026, 9, 28))
        self.assertEqual(result, "2026年09月28日 周一 续火")

    def test_render_hitokoto_replaced_even_on_api_error(self):
        with patch("app.douyin_client.fetch_hitokoto", return_value="一句话"):
            result = render_template("hi [API]")
        self.assertEqual(result, "hi 一句话")

    def test_crlf_normalized(self):
        result = render_template("a\\r\\nb\r\nc")
        self.assertEqual(result, "a\nb\nc")

    def test_sanitize_strips_zero_width_and_truncates(self):
        from app.douyin_client import MAX_MESSAGE_LENGTH, sanitize_message

        self.assertEqual(sanitize_message("  在\u200b吗   "), "在吗")
        self.assertEqual(sanitize_message("a\r\n\r\n  b  "), "a\nb")
        truncated = sanitize_message("字" * (MAX_MESSAGE_LENGTH + 40))
        self.assertEqual(len(truncated), MAX_MESSAGE_LENGTH + 1)
        self.assertTrue(truncated.endswith("…"))

    def test_default_pool_free_of_risk_words(self):
        """内置池不得出现高频灰产词与模板痕迹占位符（安全校验命中的主要来源）。"""
        for word in ("火花", "续火", "打卡", "[data]", "[weekday]"):
            for item in DEFAULT_MESSAGE_VARIANTS:
                self.assertNotIn(word, item)

    def test_legacy_pool_detection(self):
        from app.douyin_client import LEGACY_DEFAULT_MESSAGE_VARIANTS, is_legacy_default_pool

        self.assertTrue(is_legacy_default_pool(list(reversed(LEGACY_DEFAULT_MESSAGE_VARIANTS))))
        self.assertFalse(is_legacy_default_pool(["在吗", "干嘛呢"]))
        self.assertFalse(is_legacy_default_pool([]))


class BuildMessagesTests(unittest.TestCase):
    def test_targets_dedup_and_order_preserved_without_shuffle(self):
        messages = build_messages_by_target(["a", "b", "a", ""], ["m1"], shuffle=False)
        self.assertEqual(list(messages), ["a", "b"])

    def test_adjacent_messages_differ_when_pool_allows(self):
        variants = ["一", "二", "三"]
        messages = build_messages_by_target(["a", "b", "c", "d"], variants, shuffle=False)
        values = list(messages.values())
        for first, second in zip(values, values[1:]):
            self.assertNotEqual(first, second)

    def test_avoids_previous_message(self):
        messages = build_messages_by_target(
            ["a"], ["一", "二"], shuffle=False, previous_messages={"a": {"message": "一"}}
        )
        self.assertEqual(messages["a"], "二")

    def test_multiline_variant_kept_as_one_message(self):
        """整段话模板：内部换行必须原样保留（多条消息由空行分隔，由前端拆分）。"""
        paragraph = "想你了~\n愿我们的友谊天长地久❇️\n第1次自动助手"
        messages = build_messages_by_target(["a"], [paragraph], shuffle=False)
        self.assertEqual(messages["a"], paragraph)

    def test_multiline_with_placeholders_and_blank_lines(self):
        from datetime import date

        rendered = render_template("[data][weekday]\n\n第1次自动助手", today=date(2026, 9, 28))
        self.assertEqual(rendered, "2026年09月28日周一\n第1次自动助手")

    def test_empty_variants_fall_back_to_builtin(self):
        messages = build_messages_by_target(["a"], [], shuffle=False)
        self.assertTrue(messages["a"])


class SummarizeStatusTests(unittest.TestCase):
    def test_success(self):
        ok, reason = summarize_status({"success": True, "statusCode": 0})
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_known_failure_codes(self):
        cases = {
            1: "对方不是你的好友",
            3: "安全校验拦截",
            4: "仅自己可见",
            5: "拉黑",
        }
        for code, fragment in cases.items():
            ok, reason = summarize_status({"success": False, "statusCode": code})
            self.assertFalse(ok)
            self.assertIn(fragment, reason)

    def test_dry_run(self):
        ok, reason = summarize_status({"dryRun": True, "success": True, "statusCode": 0})
        self.assertFalse(ok)
        self.assertIn("演练", reason)

    def test_unknown_code_uses_status_msg(self):
        ok, reason = summarize_status({"success": False, "statusCode": 99, "statusMsg": "风控"})
        self.assertFalse(ok)
        self.assertEqual(reason, "风控")


class ProtocolSendTests(unittest.TestCase):
    def test_missing_node_raises_retryable(self):
        import app.douyin_client as client
        with patch.object(client.shutil, "which", return_value=None):
            with self.assertRaises(Exception) as ctx:
                run_protocol_send([{"name": "sessionid", "value": "v"}], {"a": "hi"}, data_dir="/tmp/x")
        self.assertIn("node", str(ctx.exception))


class DouyinServiceTests(unittest.TestCase):
    """账号/任务 CRUD、续火执行与日志（client 全部 mock，走 sqlite）。"""

    def setUp(self):
        self.database = _TestDatabase()
        self.service = DouyinService(self.database)
        self.user = {"id": 1, "role": "user"}
        # 这批用例覆盖的是「协议发送链路」；线上默认因缺 a_bogus 签名而跳过协议
        # 直走浏览器通道（见 test_douyin_cookie_refresh.ProtocolSkipTests），
        # 这里显式打开协议通道，保持用例原意。
        self._protocol_flag = patch("app.douyin_service.PROTOCOL_CHANNEL_ENABLED", True)
        self._protocol_flag.start()
        self.addCleanup(self._protocol_flag.stop)
        # 协议失败会自动兜底浏览器通道；本机装有 Edge/Chromium 时真起浏览器要等 40s+，
        # 全类统一桩掉，既避免全量单测被拖慢，也让结果可预期。
        self._browser_channel = patch(
            "app.douyin_service.send_via_browser",
            side_effect=BrowserSendError("测试环境不启用浏览器通道"),
        )
        self._browser_channel.start()
        self.addCleanup(self._browser_channel.stop)
        # 模板里的 {hitokoto} 会真去请求一言 API（带代理时单次 3s+）；全类固定返回值。
        self._hitokoto = patch("app.douyin_client.fetch_hitokoto", return_value="测试用一言")
        self._hitokoto.start()
        self.addCleanup(self._hitokoto.stop)

    def tearDown(self):
        self.service.shutdown()
        self.database.engine.dispose()

    def _add_account(self, remote_user_id="dy1", owner=1, status="active", enabled=True):
        with self.database.session() as session:
            session.add(
                DouyinAccountRow(
                    owner_user_id=owner,
                    remote_user_id=remote_user_id,
                    remark="小号A",
                    nickname="昵称A",
                    cookies=self.service._encrypt(json.dumps([{"name": "sessionid", "value": "v"}])),
                    status=status,
                    enabled=enabled,
                )
            )

    def _add_task(self, account_id=1, name="每日续火", enabled=True, **overrides):
        payload = {
            "account_id": account_id,
            "name": name,
            "enabled": enabled,
            "targets": ["好友一", "好友二"],
            "schedule_times": ["08:00:00"],
        }
        payload.update(overrides)
        with self.database.session() as session:
            session.add(DouyinTaskRow(owner_user_id=1, **payload))

    def test_create_account_parses_cookie_and_verifies(self):
        with patch("app.douyin_service.resolve_identity",
                   return_value=_identity_ok("dy9")):
            result = self.service.create_account(
                {"remark": "新号", "cookie_input": "sessionid=abc; uid_tt=1"},
                self.user,
            )
        self.assertEqual(result["remote_user_id"], "dy9")
        self.assertEqual(result["display_name"], "新号")
        with self.database.session() as session:
            row = session.query(DouyinAccountRow).filter_by(remote_user_id="dy9").one()
            cookies = json.loads(self.service._decrypt(row.cookies, ""))
            self.assertEqual(cookies[0]["name"], "sessionid")

    def test_create_account_without_auth_cookie_raises(self):
        with self.assertRaises(DouyinValidationError):
            self.service.create_account(
                {"remark": "x", "cookie_input": "ttwid=1"},
                self.user,
            )

    def test_create_account_duplicate_binds_same_owner(self):
        self._add_account()
        with patch("app.douyin_service.resolve_identity",
                   return_value=_identity_ok("dy1")):
            result = self.service.create_account(
                {"remark": "小号A-新", "cookie_input": "sessionid=abc"},
                self.user,
            )
        self.assertEqual(result["id"], 1)
        self.assertEqual(result["remark"], "小号A-新")

    def test_ownership_scoped_access(self):
        self._add_account(owner=2)
        with self.assertRaises(DouyinNotFound):
            self.service.verify_account(1, self.user)

    def test_verify_account_marks_relogin_on_auth_error(self):
        self._add_account()
        from app.douyin_client import DouyinRemoteError

        with patch("app.douyin_service.resolve_identity", return_value={
            "ok": False, "logged_in": False, "user_id": "", "message": "登录态校验失败",
            "auth_failed": True, "source": "none",
        }):
            with self.assertRaises(DouyinRemoteError):
                self.service.verify_account(1, self.user)
        with self.database.session() as session:
            row = session.get(DouyinAccountRow, 1)
            self.assertEqual(row.status, "relogin")

    def test_task_crud_and_batch(self):
        self._add_account()
        task = self.service.create_task(
            {
                "account_id": 1,
                "name": "任务B",
                "enabled": True,
                "targets": ["  好友A ", "好友A", ""],
                "schedule_times": ["09:00:00", "08:00:00", "09:00:00"],
            },
            self.user,
        )
        self.assertEqual(task["targets"], ["好友A"])
        self.assertEqual(task["schedule_times"], ["08:00:00", "09:00:00"])
        updated = self.service.update_task(task["id"], {**task, "name": "任务B2"}, self.user)
        self.assertEqual(updated["name"], "任务B2")
        state = self.service.batch_set_task_state([task["id"]], False, self.user)
        self.assertEqual(state["updated"], 1)
        deleted = self.service.batch_delete_tasks([task["id"]], self.user)
        self.assertEqual(deleted["deleted"], 1)

    def test_get_overview_counts_today(self):
        self._add_account()
        self._add_task()
        with self.database.session() as session:
            session.add(DouyinRunRow(
                owner_user_id=1, task_id=1, account_id=1,
                status="success", started_at=datetime.now(),
                response_summary={"sent_ok": 2, "sent_fail": 1},
            ))
        overview = self.service.get_overview(self.user)
        self.assertEqual(overview["stats"]["accounts"], 1)
        self.assertEqual(overview["stats"]["enabled_tasks"], 1)
        self.assertEqual(overview["stats"]["today_runs"], 1)
        self.assertEqual(overview["stats"]["today_sent"], 2)
        self.assertEqual(overview["stats"]["today_failed"], 1)
        self.assertNotIn("accounts", overview)
        self.assertNotIn("recent_runs", overview)

    def test_run_flow_success_updates_cache_and_run(self):
        self._add_account()
        self._add_task()
        protocol_result = {
            "ok": True,
            "dryRun": False,
            "protocol_targets_cache": [{"nickname": "好友一", "secUid": "s1", "peerUserId": "p1"}],
            "sent": [
                {"target": "好友一", "message": "hi", "success": True, "statusCode": 0},
                {"target": "好友二", "message": "hi", "success": False, "statusCode": 3},
            ],
            "unresolved": [],
        }
        with patch("app.douyin_service.run_protocol_send", return_value=protocol_result) as send, patch(
            # 本机可能装有 Edge/Chrome 内核：不桩浏览器通道会真起浏览器，结果不可控
            "app.douyin_service.send_via_browser",
            side_effect=BrowserSendError("测试环境不启用浏览器通道"),
        ):
            self.service.run_task(1, self.user)
            self.service.wait_for_idle()
            send.assert_called_once()
        with self.database.session() as session:
            run = session.get(DouyinRunRow, 1)
            self.assertEqual(run.status, "partial")
            self.assertIn("部分成功", run.message)
            self.assertEqual(run.response_summary["sent_ok"], 1)
            self.assertEqual(run.response_summary["sent_fail"], 1)
            reasons = {item["target"]: item["reason"] for item in run.response_summary["sent"]}
            self.assertIn("安全校验拦截", reasons["好友二"])
            account = session.get(DouyinAccountRow, 1)
            self.assertEqual(len(account.protocol_targets_cache), 1)

    def test_run_flow_marks_account_relogin_on_auth_error(self):
        self._add_account()
        self._add_task()
        from app.douyin_client import DouyinRemoteError

        with patch("app.douyin_service.run_protocol_send") as send:
            send.side_effect = DouyinRemoteError("登录态校验失败", auth_failed=True)
            self.service.run_task(1, self.user)
            self.service.wait_for_idle()
        with self.database.session() as session:
            run = session.get(DouyinRunRow, 1)
            self.assertEqual(run.status, "failed")
            account = session.get(DouyinAccountRow, 1)
            self.assertEqual(account.status, "relogin")

    def test_list_friends_updates_cache_and_logs(self):
        self._add_account()
        protocol_result = {
            "ok": True,
            "mode": "list_friends",
            "count": 2,
            "friends": [
                {"nickname": "好友A", "secUid": "s1", "peerUserId": "p1", "conversationId": "c1"},
                {"nickname": "", "secUid": "s2", "peerUserId": "p2", "conversationId": "c2"},
            ],
            "protocol_targets_cache": [{"nickname": "好友A", "secUid": "s1", "peerUserId": "p1"}],
        }
        with patch("app.douyin_service.run_protocol_list_friends", return_value=protocol_result) as fetch:
            result = self.service.list_friends(1, self.user)
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.kwargs["protocol_targets_cache"], [])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["sendable_count"], 1)
        self.assertEqual(result["unnamed_count"], 1)
        self.assertEqual(result["names"], ["好友A"])
        self.assertEqual(result["friends"][1]["sendable"], False)
        with self.database.session() as session:
            account = session.get(DouyinAccountRow, 1)
            self.assertEqual(len(account.protocol_targets_cache), 1)
            self.assertIsNotNone(account.last_verified_at)
        logs = self.service.list_logs(self.user, category="friends")["logs"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["level"], "success")
        self.assertIn("拉取好友 2 个", logs[0]["message"])
        self.assertIn("未取到昵称 1 个", logs[0]["message"])

    def test_list_friends_auth_error_marks_relogin_and_logs(self):
        self._add_account()
        from app.douyin_client import DouyinRemoteError

        with patch("app.douyin_service.run_protocol_list_friends") as fetch:
            fetch.side_effect = DouyinRemoteError("登录态校验失败", auth_failed=True)
            with self.assertRaises(DouyinRemoteError):
                self.service.list_friends(1, self.user)
        with self.database.session() as session:
            account = session.get(DouyinAccountRow, 1)
            self.assertEqual(account.status, "relogin")
        logs = self.service.list_logs(self.user, category="friends")["logs"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["level"], "error")

    def test_list_friends_scoped_by_owner(self):
        self._add_account(owner=2)
        with self.assertRaises(DouyinNotFound):
            self.service.list_friends(1, self.user)

    def test_list_friends_marks_live_result_uncached(self):
        self._add_account()
        protocol_result = {"ok": True, "friends": [], "protocol_targets_cache": []}
        with patch("app.douyin_service.run_protocol_list_friends", return_value=protocol_result):
            result = self.service.list_friends(1, self.user)
        self.assertIs(result["cached"], False)

    def test_list_friends_cached_reads_local_without_protocol(self):
        self._add_account()
        with self.database.session() as session:
            account = session.get(DouyinAccountRow, 1)
            account.protocol_targets_cache = [
                {
                    "nickname": "好友A", "secUid": "s1", "peerUserId": "p1",
                    "conversationId": "c1", "conversationShortId": "cs1",
                },
                {"nickname": "", "secUid": "s2", "peerUserId": "p2", "conversationId": "c2"},
            ]
            account.last_verified_at = datetime(2026, 9, 28, 19, 0)
        with patch("app.douyin_service.run_protocol_list_friends") as fetch:
            result = self.service.list_friends_cached(1, self.user)
            fetch.assert_not_called()
        self.assertIs(result["cached"], True)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["sendable_count"], 1)
        self.assertEqual(result["unnamed_count"], 1)
        self.assertEqual(result["names"], ["好友A"])
        self.assertFalse(result["friends"][1]["sendable"])
        self.assertEqual(result["fetched_at"], datetime(2026, 9, 28, 19, 0))

    def test_list_friends_cached_scoped_by_owner(self):
        self._add_account(owner=2)
        with self.assertRaises(DouyinNotFound):
            self.service.list_friends_cached(1, self.user)

    def test_run_flow_unresolved_targets_counted(self):
        self._add_account()
        self._add_task()
        protocol_result = {
            "ok": True,
            "dryRun": False,
            "protocol_targets_cache": [],
            "sent": [],
            "unresolved": [{"target": "路人"}],
        }
        with patch("app.douyin_service.run_protocol_send", return_value=protocol_result):
            self.service.run_task(1, self.user)
            self.service.wait_for_idle()
        with self.database.session() as session:
            run = session.get(DouyinRunRow, 1)
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.response_summary["sent_fail"], 1)
            self.assertIn("未在会话列表", run.response_summary["sent"][0]["reason"])

    def test_run_skipped_when_account_disabled(self):
        self._add_account(enabled=False)
        self._add_task()
        with patch("app.douyin_service.run_protocol_send") as send:
            self.service.run_task(1, self.user)
            self.service.wait_for_idle()
            send.assert_not_called()
        with self.database.session() as session:
            run = session.get(DouyinRunRow, 1)
            self.assertEqual(run.status, "skipped")

    def test_run_uses_global_settings_fallback_variants(self):
        self._add_account()
        self._add_task(message_variants=[])
        captured = {}

        def fake_send(cookies, messages, **kwargs):
            captured["messages"] = messages
            return {"ok": True, "dryRun": False, "sent": [], "unresolved": [], "protocol_targets_cache": []}

        with patch("app.douyin_service.run_protocol_send", side_effect=fake_send):
            self.service.run_task(1, self.user)
            self.service.wait_for_idle()
        self.assertEqual(set(captured["messages"]), {"好友一", "好友二"})

    def test_batch_run_skips_disabled(self):
        self._add_account()
        self._add_task(name="任务A", enabled=True)
        self._add_task(account_id=1, name="任务B", enabled=False)
        with patch("app.douyin_service.run_protocol_send", return_value={"ok": True, "results": []}):
            result = self.service.batch_run_tasks(self.user)
        self.assertEqual(result["planned"], 1)

    def test_run_due_tasks_marks_schedule_key_and_creates_run(self):
        self._add_account()
        self._add_task()
        with patch("app.douyin_service.simple_due_key", return_value="douyin:2026-09-28:08:00:00"), \
             patch("app.douyin_service.run_protocol_send", return_value={"ok": True, "results": []}):
            self.service.run_due_tasks()
            self.service.wait_for_idle()
        with self.database.session() as session:
            task = session.get(DouyinTaskRow, 1)
            self.assertEqual(task.last_schedule_key, "douyin:2026-09-28:08:00:00")
            self.assertEqual(session.query(DouyinRunRow).count(), 1)

    def test_run_due_tasks_skips_weekends(self):
        self._add_account()
        self._add_task(skip_weekends=True)
        # 固定「今天」为 2026-09-28（周一），否则用例在周六/周日跑必然失败
        # （simple_due_key 已打桩，这里只需让 _date_allowed 看到工作日）
        class _MondayDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 28, 8, 0, 0)

        with patch("app.douyin_service.datetime", _MondayDateTime), \
             patch("app.douyin_service.simple_due_key", return_value="key-1"), \
             patch("app.douyin_service.run_protocol_send", return_value={"ok": True, "results": []}):
            self.service.run_due_tasks()
            self.service.wait_for_idle()
        self.assertNotEqual(self.service.list_runs(self.user)[0]["status"], "skipped")

    def test_date_window_filters(self):
        self._add_account()
        # 起始日期取「明天」，避免硬编码日期在跨日后失效
        from datetime import date as _date, timedelta as _td

        tomorrow = (_date.today() + _td(days=1)).isoformat()
        self._add_task(start_date=tomorrow)
        with patch("app.douyin_service.simple_due_key", return_value="key-1"), \
             patch("app.douyin_service.run_protocol_send", return_value={"ok": True, "results": []}):
            self.service.run_due_tasks()
        with self.database.session() as session:
            task = session.get(DouyinTaskRow, 1)
            self.assertEqual(task.last_schedule_key, "")

    def test_specific_date_plan_allows_and_blocks_dates(self):
        from datetime import date as _date

        self._add_account()
        self._add_task(
            date_mode="specific",
            run_dates=["2026-09-29", "2026-09-30"],
            skip_dates=["2026-09-30"],
        )
        with self.database.session() as session:
            row = session.get(DouyinTaskRow, 1)
            # 2026-09-29 命中 run_dates → 允许
            self.assertTrue(self.service._date_allowed(row, _date(2026, 9, 29)))
            # 2026-09-28 未指定 → 拒绝；2026-09-30 被 skip_dates 排除
            self.assertFalse(self.service._date_allowed(row, _date(2026, 9, 28)))
            self.assertFalse(self.service._date_allowed(row, _date(2026, 9, 30)))

    def test_auto_disable_after_specific_plan_finished(self):
        from datetime import datetime as _datetime, timedelta as _timedelta

        self._add_account()
        yesterday = (datetime.now() - _timedelta(days=1)).date().isoformat()
        self._add_task(
            date_mode="specific",
            run_dates=[yesterday],
            auto_disable_after_finish=True,
            schedule_times=["00:00:01"],
        )
        self.service.run_due_tasks()
        with self.database.session() as session:
            task = session.get(DouyinTaskRow, 1)
            self.assertFalse(task.enabled)
            self.assertEqual(session.query(DouyinRunRow).count(), 0)

    def test_payload_rejects_specific_plan_without_dates(self):
        from app.douyin_models import DouyinTaskPayload

        with self.assertRaises(ValueError):
            DouyinTaskPayload(
                account_id=1, name="t", targets=["好友一"],
                date_mode="specific",
            )

    def test_logs_filters_and_clear(self):
        self.service._write_log("fire", "error", "续火失败", owner_user_id=1)
        self.service._write_log("account", "info", "添加账号", owner_user_id=1)
        self.service._write_log("account", "info", "他人日志", owner_user_id=2)
        data = self.service.list_logs(self.user, category="fire")
        self.assertEqual(len(data["logs"]), 1)
        data = self.service.list_logs(self.user, keyword="添加")
        self.assertEqual(len(data["logs"]), 1)
        data = self.service.list_logs(self.user)
        self.assertEqual(len(data["logs"]), 2)
        cleared = self.service.clear_logs(self.user)
        self.assertEqual(cleared["cleared"], 2)
        self.assertEqual(self.service.list_logs(self.user)["total"], 1)  # 清空操作自身也记一条

    def test_export_logs_text(self):
        self.service._write_log("fire", "success", "发送成功", owner_user_id=1)
        text = self.service.export_logs_text(self.user)
        self.assertIn("fire", text)
        self.assertIn("发送成功", text)

    def test_global_settings_roundtrip(self):
        admin = {"id": 1, "role": "admin"}
        settings = self.service.get_global_settings(self.user)
        self.assertTrue(settings["using_builtin"])
        saved = self.service.update_global_settings({"message_variants": ["一", "一", "二"], "hitokoto_types": ["文学"]}, admin)
        self.assertEqual(saved["message_variants"], ["一", "二"])
        self.assertFalse(saved["using_builtin"])
        self.assertEqual(self.service.get_default_variants(), ["一", "二"])
        # 非管理员不能新增自定义文案，只能点选现有池（当前池 ∪ 内置池）
        with self.assertRaises(DouyinValidationError):
            self.service.update_global_settings(
                {"message_variants": ["一", "二", "三号新文案"]}, self.user
            )
        # 非管理员点选现有池内文案可以保存
        saved = self.service.update_global_settings({"message_variants": ["一"]}, self.user)
        self.assertEqual(saved["message_variants"], ["一"])
        # 回退内置
        self.service.update_global_settings({"message_variants": []}, admin)
        self.assertEqual(self.service.get_default_variants(), list(DEFAULT_MESSAGE_VARIANTS))

    def test_legacy_default_pool_auto_upgraded(self):
        from app.douyin_client import LEGACY_DEFAULT_MESSAGE_VARIANTS

        self.service.update_global_settings(
            {"message_variants": list(LEGACY_DEFAULT_MESSAGE_VARIANTS)}, {"id": 1, "role": "admin"}
        )
        settings = self.service.get_global_settings(self.user)
        self.assertTrue(settings["using_builtin"])
        self.assertEqual(settings["message_variants"], [])
        self.assertEqual(self.service.get_default_variants(), list(DEFAULT_MESSAGE_VARIANTS))
        # 幂等：再次读取仍是内置池
        self.assertTrue(self.service.get_global_settings(self.user)["using_builtin"])

    def test_repeat_run_avoids_same_message_for_same_target(self):
        """连续两次运行同一目标时不应重复同一句（重复文案更易被判机器群发）。

        默认「今日已续则跳过」会让第二次运行直接跳过该好友（另有用例覆盖），
        这里关掉该开关以继续验证文案避重逻辑本身。
        """
        self._add_account()
        self._add_task(targets=["好友一"], message_variants=["一", "二", "三"], skip_sent_today=False)
        captured = []

        def fake_send(cookies, messages, **kwargs):
            captured.append(dict(messages))
            return {
                "ok": True,
                "dryRun": False,
                "sent": [
                    {"target": target, "message": message, "success": True, "statusCode": 0}
                    for target, message in messages.items()
                ],
                "unresolved": [],
            }

        with patch("app.douyin_service.run_protocol_send", side_effect=fake_send):
            self.service.run_task(1, self.user)
            self.service.wait_for_idle()
            self.service.run_task(1, self.user)
            self.service.wait_for_idle()

        self.assertEqual(len(captured), 2)
        self.assertNotEqual(captured[0]["好友一"], captured[1]["好友一"])

    def test_run_skips_targets_already_sent_today(self):
        """火花预检（本地）：今天已经成功续过的好友不再重复发，只补发失败的那个。"""
        self._add_account()
        self._add_task(targets=["好友一", "好友二"], message_variants=["打个招呼"])
        calls = []

        def fake_send(cookies, messages, **kwargs):
            calls.append(dict(messages))
            return {
                "ok": True,
                "dryRun": False,
                # 好友一成功、好友二失败：下一轮只需要补发好友二
                "sent": [
                    {"target": target, "message": message, "success": target == "好友一", "statusCode": 0}
                    for target, message in messages.items()
                ],
                "unresolved": [],
            }

        with patch("app.douyin_service.run_protocol_send", side_effect=fake_send):
            self.service.run_task(1, self.user)
            self.service.wait_for_idle()
            self.service.run_task(1, self.user)
            self.service.wait_for_idle()

        self.assertEqual(len(calls), 2)
        self.assertEqual(sorted(calls[0]), ["好友一", "好友二"])
        self.assertEqual(sorted(calls[1]), ["好友二"])
        with self.database.session() as session:
            run = session.scalars(select(DouyinRunRow).order_by(DouyinRunRow.id.desc())).first()
        self.assertEqual(int(run.response_summary.get("skipped") or 0), 1)
        self.assertTrue(any(entry.get("skipped") for entry in run.response_summary.get("sent", [])))

    def test_run_all_skipped_when_everyone_already_sent_today(self):
        """全部目标今天都续过 → 本轮标记 skipped，不再打扰任何人。"""
        self._add_account()
        self._add_task(targets=["好友一"], message_variants=["打个招呼"])
        calls = []

        def fake_send(cookies, messages, **kwargs):
            calls.append(dict(messages))
            return {
                "ok": True,
                "dryRun": False,
                "sent": [
                    {"target": target, "message": message, "success": True, "statusCode": 0}
                    for target, message in messages.items()
                ],
                "unresolved": [],
            }

        with patch("app.douyin_service.run_protocol_send", side_effect=fake_send):
            self.service.run_task(1, self.user)
            self.service.wait_for_idle()
            self.service.run_task(1, self.user)
            self.service.wait_for_idle()

        # 第二轮不再发起任何真实发送
        self.assertEqual(len(calls), 1)
        with self.database.session() as session:
            run = session.scalars(select(DouyinRunRow).order_by(DouyinRunRow.id.desc())).first()
        self.assertEqual(run.status, "skipped")
        self.assertEqual(int(run.response_summary.get("skipped") or 0), 1)


class DouyinMigrationTests(unittest.TestCase):
    """存量表漂移：create_all 不补旧表缺列，initialize 须自动 ADD COLUMN。"""

    def test_missing_columns_added_and_insertable(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        # 造一张按旧模型建的 douyin_accounts（缺 douyin_id / source）
        legacy = MetaData()
        Table(
            "douyin_accounts", legacy,
            Column("id", Integer, primary_key=True),
            Column("owner_user_id", Integer),
            Column("remote_user_id", String(64)),
            Column("remark", String(255)),
            Column("nickname", String(255)),
            Column("cookies", JSON),
            Column("protocol_targets_cache", JSON),
            Column("status", String(24)),
            Column("enabled", Boolean),
            Column("last_verified_at", DateTime),
            Column("last_error", Text),
            Column("created_at", DateTime),
            Column("updated_at", DateTime),
        )
        legacy.create_all(engine)

        from sqlalchemy import inspect as sql_inspect
        add_missing_columns(engine, DouyinBase.metadata)

        names = {c["name"] for c in sql_inspect(engine).get_columns("douyin_accounts")}
        self.assertIn("douyin_id", names)
        self.assertIn("source", names)
        # 补列后 ORM 可正常插入（幂等：再跑一次不报错）
        add_missing_columns(engine, DouyinBase.metadata)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as session:
            session.add(DouyinAccountRow(
                owner_user_id=1, remote_user_id="dy-x", cookies=[],
            ))
            session.commit()
            row = session.query(DouyinAccountRow).filter_by(remote_user_id="dy-x").one()
            self.assertEqual(row.douyin_id, "")
            self.assertEqual(row.source, "cookie")
        engine.dispose()

class DouyinNotifyTests(unittest.TestCase):
    """企微通知：markdown 文案组装、通知设置读写与任务通知开关。"""

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def test_send_summary_builds_markdown(self):
        from app.douyin_notifier import DouyinNotifier

        captured = {}

        class _FakeHttp:
            def post(self, url, json=None, **kwargs):
                captured["url"] = url
                captured["content"] = json["markdown"]["content"]
                return DouyinNotifyTests._FakeResponse({"errcode": 0})

        notifier = DouyinNotifier(http=_FakeHttp())
        notifier.send_summary(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "a" * 32,
            {
                "task_name": "每日续火",
                "account_name": "小号A",
                "owner_username": "admin",
                "trigger": "scheduler",
                "started_at": datetime(2026, 9, 28, 8, 0, 0),
                "status": "partial",
                "message": "部分成功：成功 1，失败 1",
                "sent_ok": 1,
                "sent_fail": 1,
                "details": [
                    {"target": "好友一", "success": True, "dry_run": False},
                    {"target": "好友二", "success": False, "reason": "内容被安全校验拦截", "dry_run": False},
                ],
            },
        )
        content = captured["content"]
        self.assertIn("抖音续火通知", content)
        self.assertIn("任务：每日续火", content)
        self.assertIn("触发：定时自动执行", content)
        self.assertIn("成功：1 个｜失败：1 个", content)
        self.assertIn("✅ 好友一：发送成功", content)
        self.assertIn("❌ 好友二：内容被安全校验拦截", content)

    def test_send_summary_invalid_webhook_rejected(self):
        from app.douyin_notifier import DouyinNotifier

        notifier = DouyinNotifier()
        with self.assertRaises(Exception):
            notifier.send_summary("https://evil.example.com/cgi-bin/webhook/send?key=abc", {})

    def test_notify_settings_admin_only_and_roundtrip(self):
        database = _TestDatabase()
        service = DouyinService(database)
        try:
            with self.assertRaises(DouyinNotFound):
                service.get_notify_settings({"id": 1, "role": "user"})
            result = service.update_notify_settings({"douyin_webhook_url": ""}, {"id": 0, "role": "admin"})
            self.assertFalse(result["webhook_configured"])
            self.assertIn("douyin_webhook_url", result)
        finally:
            service.shutdown()
            database.engine.dispose()

    def test_send_run_notification_respects_task_flag(self):
        from app.douyin_notifier import DouyinNotifier

        database = _TestDatabase()
        sent = []

        class _StubNotifier(DouyinNotifier):
            def send_summary(self, webhook_url, summary):
                sent.append((webhook_url, summary))

        service = DouyinService(database, notifier=_StubNotifier())
        try:
            task_off = _TaskProxy(notify_wecom=False)
            service._send_run_notification(
                task_off, account_name="小号A", run_id=1,
                status="success", message="续火完成", summary={"sent_ok": 2, "sent_fail": 0},
            )
            self.assertEqual(sent, [])

            task_on = _TaskProxy(notify_wecom=True)
            webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "a" * 32
            with patch("app.douyin_service.load_douyin_settings", return_value={
                "douyin_webhook_url": webhook,
                "webhook_configured": True,
            }):
                service._send_run_notification(
                    task_on, account_name="小号A", run_id=None,
                    status="success", message="续火完成", summary={"sent_ok": 2, "sent_fail": 0, "sent": []},
                )
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], webhook)
            self.assertEqual(sent[0][1]["task_name"], "续火")
        finally:
            service.shutdown()
            database.engine.dispose()


class _TaskProxy:
    """最小任务替身：只暴露通知开关与归属。"""

    def __init__(self, notify_wecom=True):
        self.notify_wecom = notify_wecom
        self.name = "续火"
        self.owner_user_id = 1


class SparkStateTests(unittest.TestCase):
    """平台真实火花状态（P2）：client 封装 + service 解析/快照/总览口径。"""

    def setUp(self):
        self.database = _TestDatabase()
        self.service = DouyinService(self.database)
        self.user = {"id": 1, "role": "user"}

    def tearDown(self):
        self.service.shutdown()
        self.database.engine.dispose()

    def _add_account(self, remote_user_id="dy1", owner=1, status="active", enabled=True):
        with self.database.session() as session:
            session.add(
                DouyinAccountRow(
                    owner_user_id=owner,
                    remote_user_id=remote_user_id,
                    remark="小号A",
                    nickname="昵称A",
                    cookies=self.service._encrypt(json.dumps([{"name": "sessionid", "value": "v"}])),
                    status=status,
                    enabled=enabled,
                )
            )

    def test_run_protocol_spark_state_payload(self):
        """run_protocol_spark_state 传给 node 的 mode/repoRoot/account 正确。"""
        from app.douyin_client import run_protocol_spark_state

        with patch("app.douyin_client._run_protocol", return_value={"ok": True}) as runner:
            run_protocol_spark_state(
                [{"name": "sessionid", "value": "v"}],
                data_dir="/tmp/dy-data",
            )
            payload = runner.call_args.args[0]
            self.assertEqual(payload["mode"], "spark_state")
            self.assertEqual(payload["repoRoot"], "/tmp/dy-data")
            self.assertEqual(payload["account"]["cookies"][0]["name"], "sessionid")

    def test_get_spark_states_parses_and_persists_snapshot(self):
        self._add_account()
        protocol_result = {
            "ok": True,
            "mode": "spark_state",
            "count": 2,
            "pendingToday": 1,
            "sparks": [
                {"nickname": "好友A", "secUid": "s1", "peerUserId": "p1",
                 "spark": {"state": 2, "days": 1, "text": "点亮中 1/3", "level": "recover_normal"}},
                {"nickname": "好友B", "secUid": "s2", "peerUserId": "p2",
                 "spark": {"state": 1, "days": 505, "text": "505", "level": "normal|normal"}},
            ],
        }
        with patch("app.douyin_service.run_protocol_spark_state", return_value=protocol_result):
            result = self.service.get_spark_states(1, self.user)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["pending_today"], 1)
        states = {item["nickname"]: item for item in result["sparks"]}
        self.assertTrue(states["好友A"]["pending_today"])
        self.assertFalse(states["好友B"]["pending_today"])
        self.assertEqual(states["好友B"]["days"], 505)
        # 快照落库
        with self.database.session() as session:
            account = session.get(DouyinAccountRow, 1)
            self.assertEqual(account.spark_snapshot["pending_today"], 1)
            self.assertEqual(account.spark_snapshot["count"], 2)
            self.assertIsNotNone(account.spark_checked_at)

    def test_spark_states_cached_reads_snapshot_without_remote(self):
        self._add_account()
        with self.database.session() as session:
            account = session.get(DouyinAccountRow, 1)
            account.spark_snapshot = {
                "sparks": [{"nickname": "好友A", "days": 3, "pending_today": False, "active": True}],
                "pending_today": 0,
                "count": 1,
            }
            account.spark_checked_at = datetime.now()
        with patch("app.douyin_service.run_protocol_spark_state") as remote:
            result = self.service.spark_states_cached(1, self.user)
            remote.assert_not_called()
        self.assertTrue(result["cached"])
        self.assertEqual(result["count"], 1)

    def test_spark_state_zero_days_when_inactive(self):
        """state=0（仅历史记录）时 days 归零、active=False、pending=False。"""
        item = {"spark": {"state": 0, "days": 1}}
        parsed = self.service._spark_dict(item)
        self.assertFalse(parsed["active"])
        self.assertFalse(parsed["pending_today"])
        self.assertEqual(parsed["days"], 0)

    def test_overview_includes_platform_spark_stats(self):
        self._add_account()
        with self.database.session() as session:
            account = session.get(DouyinAccountRow, 1)
            account.spark_snapshot = {
                "sparks": [
                    {"days": 505, "pending_today": False, "active": True},
                    {"days": 3, "pending_today": True, "active": True},
                    {"days": 0, "pending_today": False, "active": False},
                ],
                "pending_today": 1,
            }
            account.spark_checked_at = datetime.now()
        overview = self.service.get_overview(self.user)
        stats = overview["stats"]
        self.assertEqual(stats["spark_pending"], 1)
        self.assertEqual(stats["spark_active"], 2)
        self.assertEqual(stats["spark_total_days"], 508)
        self.assertEqual(stats["spark_accounts"], 1)
        self.assertIsNotNone(stats["spark_checked_at"])

    def test_overview_spark_stats_empty_without_snapshot(self):
        self._add_account()
        stats = self.service.get_overview(self.user)["stats"]
        self.assertEqual(stats["spark_pending"], 0)
        self.assertEqual(stats["spark_accounts"], 0)
        self.assertIsNone(stats["spark_checked_at"])

    def test_refresh_snapshot_failure_is_silent(self):
        """快照刷新异常不能冒泡（纯附加信息）。"""
        from app.douyin_client import DouyinRemoteError

        self._add_account()
        with patch("app.douyin_service.run_protocol_spark_state",
                   side_effect=DouyinRemoteError("boom")):
            self.service._refresh_spark_snapshot(
                [{"name": "sessionid", "value": "v"}], 1, "小号A",
                data_dir="/tmp/dy-data",
            )
        with self.database.session() as session:
            account = session.get(DouyinAccountRow, 1)
            self.assertEqual(account.spark_snapshot or {}, {})


def _identity_ok(user_id="dy9", **extra):
    """``resolve_identity`` 的成功返回（create/verify 用例复用）。"""
    base = {
        "ok": True, "logged_in": True, "user_id": user_id, "sec_uid": "",
        "nickname": "", "douyin_id": "", "avatar": "", "source": "creator_api", "message": "",
    }
    base.update(extra)
    return base

if __name__ == "__main__":
    unittest.main()
