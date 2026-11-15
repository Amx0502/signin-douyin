"""抖音续火新增能力单测：目标写法（别名/专属文案）、发送延迟、单目标重试、运行排队。"""
import json
import threading
import time
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import select

from app.douyin_browser_sender import _candidates, _wait_targets
from app.douyin_client import (
    build_target_payload,
    format_target_spec,
    normalize_target_specs,
    parse_target_spec,
)
from app.douyin_db_models import DouyinAccountRow, DouyinRunRow, DouyinTaskRow
from app.douyin_service import DouyinNotFound, DouyinService, DouyinValidationError
from app.douyin_site import active_site
from tests.test_douyin_platform import _TestDatabase


class TargetSpecTests(unittest.TestCase):
    def test_parse_plain_name(self):
        spec = parse_target_spec("张三")
        self.assertEqual(spec["primary"], "张三")
        self.assertEqual(spec["aliases"], [])
        self.assertEqual(spec["message"], "")

    def test_parse_aliases(self):
        spec = parse_target_spec("张三=三哥=小三")
        self.assertEqual(spec["primary"], "张三")
        self.assertEqual(spec["aliases"], ["三哥", "小三"])
        self.assertEqual(spec["message"], "")

    def test_parse_custom_message(self):
        spec = parse_target_spec("张三=三哥|今天喝奶茶了吗")
        self.assertEqual(spec["primary"], "张三")
        self.assertEqual(spec["aliases"], ["三哥"])
        self.assertEqual(spec["message"], "今天喝奶茶了吗")

    def test_parse_full_width_separators(self):
        spec = parse_target_spec("张三＝三哥｜晚上好")
        self.assertEqual(spec["aliases"], ["三哥"])
        self.assertEqual(spec["message"], "晚上好")

    def test_format_round_trip(self):
        spec = parse_target_spec("张三=三哥|在吗")
        self.assertEqual(format_target_spec(spec), "张三=三哥|在吗")
        # 主名升级：把命中的别名提到主名，旧主名降级为别名
        upgraded = {"primary": "三哥", "aliases": ["张三"], "message": "在吗"}
        self.assertEqual(format_target_spec(upgraded), "三哥=张三|在吗")

    def test_normalize_dedupes_by_primary(self):
        specs = normalize_target_specs(["张三", "张三=三哥", "李四|早"])
        self.assertEqual([spec["primary"] for spec in specs], ["张三", "李四"])
        self.assertEqual(specs[0]["aliases"], [])

    def test_build_payload_prefers_custom_message(self):
        payload = build_target_payload(["张三|专属问候", "李四"], ["模板一", "模板二"])
        self.assertEqual(payload["messages"]["张三"], "专属问候")
        self.assertIn(payload["messages"]["李四"], {"模板一", "模板二"})
        self.assertEqual(payload["aliases"], {})

    def test_build_payload_collects_aliases(self):
        payload = build_target_payload(["张三=三哥", "李四"], ["模板一"])
        self.assertEqual(payload["aliases"], {"张三": ["三哥"]})
        self.assertEqual(sorted(payload["messages"]), ["张三", "李四"])


class _DouyinCase(unittest.TestCase):
    """最小 service 测试基建（sqlite 内存库 + 协议通道打开，便于 mock 发送）。"""

    def setUp(self):
        self.database = _TestDatabase()
        self.service = DouyinService(self.database)
        self.user = {"id": 1, "role": "user"}
        self._protocol_flag = patch("app.douyin_service.PROTOCOL_CHANNEL_ENABLED", True)
        self._protocol_flag.start()
        self.addCleanup(self._protocol_flag.stop)

    def tearDown(self):
        self.service.shutdown()
        self.database.engine.dispose()

    def _add_account(self, remote_user_id="dy1", owner=1, status="active", enabled=True):
        with self.database.session() as session:
            session.add(DouyinAccountRow(
                owner_user_id=owner,
                remote_user_id=remote_user_id,
                remark="小号A",
                nickname="昵称A",
                cookies=self.service._encrypt(json.dumps([{"name": "sessionid", "value": "v"}])),
                status=status,
                enabled=enabled,
            ))

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


class TaskOrderTests(_DouyinCase):
    """任务列表顺序：按创建时间（旧在上、新在下），且编辑任务不改变它的位置。"""

    def _seed(self, name, account_id, created_at, updated_at):
        with self.database.session() as session:
            session.add(DouyinTaskRow(
                owner_user_id=1,
                account_id=account_id,
                name=name,
                enabled=True,
                targets=["好友一"],
                schedule_times=["08:00:00"],
                created_at=created_at,
                updated_at=updated_at,
            ))

    def _names(self):
        return [row["name"] for row in self.service.list_tasks(self.user)]

    def test_list_tasks_orders_by_creation(self):
        self._add_account()
        # updated_at 的顺序刻意与 created_at 相反：按 updated_at 倒序会把「最后动过的」顶到最前
        self._seed("先建的", 1, datetime(2026, 1, 1, 8, 0), datetime(2026, 1, 1, 8, 0))
        self._seed("后建的", 1, datetime(2026, 2, 1, 8, 0), datetime(2099, 1, 1, 8, 0))
        self.assertEqual(self._names(), ["先建的", "后建的"])

    def test_editing_task_does_not_move_it(self):
        self._add_account()
        self._seed("先建的", 1, datetime(2026, 1, 1, 8, 0), datetime(2026, 1, 1, 8, 0))
        self._seed("后建的", 1, datetime(2026, 2, 1, 8, 0), datetime(2026, 2, 1, 8, 0))
        last = self.service.list_tasks(self.user)[-1]
        # 编辑会把 updated_at 刷成当前时间；旧实现按 updated_at 倒序，改完这条会跳到最前面
        self.service.update_task(last["id"], {"account_id": 1, "name": "后建的（改名）"}, self.user)
        self.assertEqual(self._names(), ["先建的", "后建的（改名）"])


class DelayTests(_DouyinCase):
    def test_normalize_delay_clamps_and_orders(self):
        self.assertEqual(self.service._normalize_delay({"delay_min_seconds": 0, "delay_max_seconds": 0}),
                         {"delay_min_seconds": None, "delay_max_seconds": None})
        self.assertEqual(self.service._normalize_delay({"delay_min_seconds": 200, "delay_max_seconds": 60}),
                         {"delay_min_seconds": 60, "delay_max_seconds": 200})
        # 只填上限：视为 0~上限 随机（早期实现会变成 600~600 固定等待）
        self.assertEqual(self.service._normalize_delay({"delay_min_seconds": None, "delay_max_seconds": 999}),
                         {"delay_min_seconds": 0, "delay_max_seconds": 600})
        # 显式 0 下限必须保留：0~20 不能被改成 20~20
        self.assertEqual(self.service._normalize_delay({"delay_min_seconds": 0, "delay_max_seconds": 20}),
                         {"delay_min_seconds": 0, "delay_max_seconds": 20})
        self.assertEqual(self.service._normalize_delay({"delay_min_seconds": "abc", "delay_max_seconds": None}),
                         {"delay_min_seconds": None, "delay_max_seconds": None})

    def test_task_delay_seconds_inside_range(self):
        self._add_account()
        self._add_task(delay_min_seconds=5, delay_max_seconds=15)
        with self.database.session() as session:
            task = session.get(DouyinTaskRow, 1)
            for _ in range(8):
                delay = self.service._task_delay_seconds(task)
                self.assertTrue(5 <= delay <= 15)
            task.delay_min_seconds = None
            task.delay_max_seconds = None
            self.assertEqual(self.service._task_delay_seconds(task), 0.0)

    def test_delayed_run_is_scheduled_not_slept(self):
        """随机延迟任务挂 deliver_at，到点才投递；期间不执行、不占账号锁。"""
        self._add_account()
        self._add_task(delay_min_seconds=30, delay_max_seconds=30)
        calls = []
        with patch("app.douyin_service.simple_due_key", return_value="key-delay"), \
             patch.object(self.service, "_execute_run", side_effect=lambda *a, **kw: calls.append(a[0])):
            self.service.run_due_tasks()
            self.service.wait_for_idle(timeout=2)
            self.assertEqual(calls, [], "延迟未到点不应执行")
            with self.database.session() as session:
                run = session.get(DouyinRunRow, 1)
                self.assertIsNotNone(run.deliver_at)
                self.assertEqual(run.status, "running")
                self.assertIn("随机延迟", run.message)
            # 未到点扫描：仍不投递
            self.service._flush_delayed_runs(datetime(2026, 1, 1, 0, 0, 0))
            self.service.wait_for_idle(timeout=2)
            self.assertEqual(calls, [])
            # 到点扫描：投递并执行
            self.service._flush_delayed_runs(datetime(2099, 1, 1, 0, 0, 0))
            self.service.wait_for_idle(timeout=5)
            self.assertEqual(len(calls), 1)
            with self.database.session() as session:
                self.assertIsNone(session.get(DouyinRunRow, 1).deliver_at, "投递后应清空避免重复")


class RunQueueTests(_DouyinCase):
    """同账号多任务排队串行（不再丢弃），全局并发受上限约束。"""

    def _stub_execute(self, calls, hold=0.05):
        def _fake(run_id, owner, task_id, account_id):
            calls.append(run_id)
            time.sleep(hold)
        return _fake

    def test_same_account_runs_queue_instead_of_skip(self):
        self._add_account()
        calls = []
        with self.database.session() as session:
            session.add(DouyinRunRow(owner_user_id=1, account_id=1, trigger="scheduler"))
            session.add(DouyinRunRow(owner_user_id=1, account_id=1, trigger="scheduler"))
        with patch.object(self.service, "_execute_run", side_effect=self._stub_execute(calls)):
            self.service._submit_run(1, 1, None, 1, "scheduler")
            self.service._submit_run(2, 1, None, 1, "scheduler")
            self.service.wait_for_idle(timeout=10)
        self.assertEqual(sorted(calls), [1, 2], "同账号第二条应排队执行而不是被跳过")
        with self.database.session() as session:
            self.assertEqual(session.get(DouyinRunRow, 2).message, "排队中，前方 1 个任务")

    def test_global_parallel_limit(self):
        for idx in range(4):
            self._add_account(remote_user_id=f"dy{idx}")
        calls = []
        peak = []

        def _fake(run_id, owner, task_id, account_id):
            calls.append(run_id)
            peak.append(sum(1 for _ in calls))  # 近似并发计数
            time.sleep(0.08)

        with patch.object(self.service, "_execute_run", side_effect=_fake):
            for run_id in range(1, 5):
                with self.database.session() as session:
                    session.add(DouyinRunRow(owner_user_id=1, account_id=run_id, trigger="scheduler"))
                self.service._submit_run(run_id, 1, None, run_id, "scheduler")
            self.service.wait_for_idle(timeout=15)
        self.assertEqual(len(calls), 4, "4 个不同账号都应执行")
        self.assertLessEqual(self.service._active_runs, 2)

    def test_queue_overflow_skips_with_reason(self):
        self._add_account()
        calls = []
        with self.database.session() as session:
            for _ in range(25):
                session.add(DouyinRunRow(owner_user_id=1, account_id=1, trigger="scheduler"))
        blocker = threading.Event()

        def _fake(run_id, owner, task_id, account_id):
            calls.append(run_id)
            blocker.wait(5)

        with patch.object(self.service, "_execute_run", side_effect=_fake):
            for run_id in range(1, 25):
                self.service._submit_run(run_id, 1, None, 1, "scheduler")
            blocker.set()
            self.service.wait_for_idle(timeout=20)
        with self.database.session() as session:
            skipped = [row for row in session.scalars(select(DouyinRunRow)) if row.status == "skipped"]
        self.assertTrue(skipped, "超排队上限的应被跳过而不是无限堆积")
        self.assertIn("排队任务过多", skipped[0].message)


class BrowserAliasTests(unittest.TestCase):
    """浏览器通道：会话列表按「主名 + 别名」命中。"""

    class _Page:
        """最小 page 桩：只提供 _visible_text_node 需要的 count/nth/is_visible/bounding_box。"""

        def __init__(self, visible: set[str], hidden: set[str] | None = None):
            self.visible = visible
            self.hidden = hidden or set()
            self.waits = 0

        def wait_for_timeout(self, ms):
            self.waits += 1

        def get_by_text(self, text, exact=False):
            return BrowserAliasTests._Loc(self, text)

    class _Loc:
        def __init__(self, page, text: str):
            self._page = page
            self._text = text

        def count(self):
            return (1 if self._text in self._page.visible else 0) + (
                1 if self._text in self._page.hidden else 0
            )

        def nth(self, index: int):
            # 隐藏节点排在前面：验证实现会跳过不可见命中
            if index == 0 and self._text in self._page.hidden:
                return BrowserAliasTests._Node(False)
            return BrowserAliasTests._Node(self._text in self._page.visible)

    class _Node:
        def __init__(self, visible: bool):
            self._visible = visible

        def is_visible(self):
            return self._visible

        def bounding_box(self):
            return {"x": 10.0, "y": 20.0, "width": 60.0, "height": 20.0} if self._visible else None

    def test_candidates_dedupe_and_keep_order(self):
        self.assertEqual(_candidates("张三", ["三哥", "张三", "小三"]), ["张三", "三哥", "小三"])

    def test_wait_targets_matches_alias(self):
        page = self._Page({"三哥"})
        found = _wait_targets(page, ["张三"], 1.0, {"张三": ["三哥"]})
        self.assertEqual(found, {"张三": "三哥"})

    def test_wait_targets_skips_hidden_duplicate(self):
        """同名节点里有隐藏项（折叠面板/预渲染骨架）时不能算命中。"""
        page = self._Page(set(), hidden={"张三"})
        self.assertEqual(_wait_targets(page, ["张三"], 0.5), {})

    def test_wait_targets_accepts_visible_after_hidden(self):
        page = self._Page({"张三"}, hidden={"张三"})
        self.assertEqual(_wait_targets(page, ["张三"], 0.5), {"张三": "张三"})

    def test_wait_targets_reports_missing(self):
        page = self._Page({"别人"})
        found = _wait_targets(page, ["张三"], 0.5, {"张三": ["三哥"]})
        self.assertEqual(found, {})


class RetryTargetTests(_DouyinCase):
    def _seed_run(self):
        """落一条运行记录：好友一失败、好友二成功（用来验证守卫条件）。"""
        self._add_account()
        self._add_task(targets=["好友一", "好友二"], message_variants=["打个招呼"])
        with self.database.session() as session:
            run = DouyinRunRow(
                owner_user_id=1, task_id=1, account_id=1, schedule_key="k", trigger="scheduler",
                status="partial", message="部分成功",
                response_summary={
                    "sent_ok": 1,
                    "sent_fail": 1,
                    "sent": [
                        {"target": "好友一", "message": "打个招呼", "success": False,
                         "status_code": None, "status_name": "send_unconfirmed", "reason": "未确认"},
                        {"target": "好友二", "message": "打个招呼", "success": True,
                         "status_code": 0, "status_name": "Succeeded", "reason": ""},
                    ],
                },
            )
            session.add(run)
            session.flush()
            return run.id

    def test_retry_sends_only_that_target(self):
        run_id = self._seed_run()
        calls = []

        def fake_send(cookies, messages, **kwargs):
            calls.append(dict(messages))
            return {
                "ok": True, "dryRun": False,
                "sent": [{"target": t, "message": m, "success": True, "statusCode": 0,
                          "matched": t, "statusName": "Succeeded"}
                         for t, m in messages.items()],
                "unresolved": [],
            }

        with patch("app.douyin_service.run_protocol_send", side_effect=fake_send):
            result = self.service.retry_run_target(run_id, "好友一", self.user)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], {"好友一": "打个招呼"})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["trigger"], "retry_target")
        self.assertTrue(result["response_summary"]["sent"][0]["success"])

    def test_retry_rejects_unknown_and_successful_target(self):
        run_id = self._seed_run()
        with self.assertRaises(DouyinNotFound):
            self.service.retry_run_target(run_id, "不存在的人", self.user)
        with self.assertRaises(DouyinValidationError):
            self.service.retry_run_target(run_id, "好友二", self.user)


class FriendsAndSparksMergeTests(_DouyinCase):
    """拉好友与读火花合并为一次协议采样（同一数据源，省一次 SDK 初始化）。"""

    FAKE_PROTOCOL_RESULT = {
        "ok": True,
        "mode": "friends_and_sparks",
        "userId": "42",
        "count": 2,
        "sendableCount": 1,
        "friends": [
            {"nickname": "好友一", "uniqueId": "dy1", "avatar": "", "peerUserId": "100",
             "secUid": "sec1", "conversationId": "700:100", "conversationShortId": "700"},
            {"nickname": "", "uniqueId": "", "avatar": "", "peerUserId": "101",
             "secUid": "sec2", "conversationId": "700:101", "conversationShortId": "701"},
        ],
        "sparkCount": 1,
        "pendingToday": 1,
        "sparks": [
            {"nickname": "好友一", "uniqueId": "dy1", "avatar": "", "peerUserId": "100",
             "secUid": "sec1", "conversationId": "700:100",
             "spark": {"state": 2, "active": True, "days": 3, "text": "待续 3 天"}},
        ],
        "protocol_targets_cache": [
            {"nickname": "好友一", "secUid": "sec1", "peerUserId": "100",
             "conversationId": "700:100", "conversationShortId": "700"},
        ],
    }

    def test_client_payload_uses_merged_mode(self):
        from app.douyin_client import run_protocol_friends_and_sparks

        captured = {}

        def fake_run(payload, *, timeout=None, action=""):
            captured["payload"] = payload
            captured["timeout"] = timeout
            captured["action"] = action
            return {"ok": True}

        with patch("app.douyin_client._run_protocol", side_effect=fake_run):
            run_protocol_friends_and_sparks(
                [{"name": "sessionid", "value": "v"}],
                data_dir="/tmp/douyin-test",
                protocol_targets_cache=[],
            )
        self.assertEqual(captured["payload"]["mode"], "friends_and_sparks")
        self.assertEqual(captured["payload"]["site"], active_site().key)
        self.assertEqual(captured["action"], "拉取好友与火花")

    def test_merged_pull_writes_friends_cache_and_spark_snapshot(self):
        self._add_account()
        calls = []

        def fake_protocol(cookies, **kwargs):
            calls.append(kwargs)
            return dict(self.FAKE_PROTOCOL_RESULT)

        with patch("app.douyin_service.run_protocol_friends_and_sparks", side_effect=fake_protocol):
            result = self.service.list_friends_and_sparks(1, self.user)

        # 只调用一次协议（合并的核心：好友与火花不再各拉一次）
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["sendable_count"], 1)
        self.assertEqual(result["unnamed_count"], 1)
        self.assertTrue(all(row["sendable"] == bool(row["nickname"]) for row in result["friends"]))
        # 火花载荷与 get_spark_states 同构，前端可直接建索引
        self.assertEqual(result["spark"]["count"], 1)
        self.assertEqual(result["spark"]["pending_today"], 1)
        self.assertEqual(result["spark"]["sparks"][0]["state"], 2)

        with self.database.session() as session:
            row = session.get(DouyinAccountRow, 1)
            self.assertEqual([e["nickname"] for e in row.protocol_targets_cache], ["好友一"])
            snapshot = row.spark_snapshot
            self.assertEqual(snapshot["count"], 1)
            self.assertEqual(snapshot["sparks"][0]["nickname"], "好友一")
            self.assertIsNotNone(row.spark_checked_at)
            self.assertIsNotNone(row.last_verified_at)

    def test_friends_job_runs_merged_pull(self):
        self._add_account()
        with patch("app.douyin_service.run_protocol_friends_and_sparks",
                   side_effect=lambda cookies, **kw: dict(self.FAKE_PROTOCOL_RESULT)):
            self.service.start_friends_refresh(1, self.user)
            job = None
            for _ in range(100):
                job = self.service.get_friends_job(1, self.user)
                if job["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(job["status"], "success")
        self.assertEqual(job["result"]["count"], 2)
        # 任务结果里直接带火花，前端无需再发一次拉取
        self.assertEqual(job["result"]["spark"]["pending_today"], 1)


if __name__ == "__main__":
    unittest.main()
