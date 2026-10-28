import os
import sys
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "app"), ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import wb_accounts  # noqa: E402
import wb_proxy  # noqa: E402
import wb_tasks  # noqa: E402
import wb_tasks as T  # noqa: E402


class FakeAccount(object):
    def __init__(self):
        self.uid = "u-batch"
        self.nickname = "batch"
        self.realm = "cn"
        self.proxy = ""
        self.access_token = "tok"
        self.credits = {"remain": 0}

    def headers(self, kind="chat"):
        return {}

    def fetch_credits(self):
        pass


def _task(code, **over):
    t = {"task_code": code, "status": "accepted", "current": 0, "target": 1,
         "name": code, "jump_url": "", "task_type": "single", "unforgeable": False}
    t.update(over)
    return t


class GrowthBatchSettleTests(unittest.TestCase):
    def setUp(self):
        self._orig = (T.fetch_growth_tasks, T.accept_tasks, T.claim_task,
                      T.report_events, T.do_cat_travel, T.TASK_SPECS)

    def tearDown(self):
        (T.fetch_growth_tasks, T.accept_tasks, T.claim_task,
         T.report_events, T.do_cat_travel, T.TASK_SPECS) = self._orig

    def test_shared_poll_claims_all_reported(self):
        # 两个已接取任务一起上报后，结算共享同一份列表轮询，拉取次数与任务数无关。
        fetch_calls = {"n": 0}
        state = {"create_canvas": 0, "chat_5": 0}
        claimed = []

        def fetch(acc):
            fetch_calls["n"] += 1
            return [_task("create_canvas", status="accepted",
                          current=state["create_canvas"], target=1),
                    _task("chat_5", status="accepted",
                          current=state["chat_5"], target=5)]
        T.fetch_growth_tasks = fetch

        def report(acc, evs, base=None):
            code = evs[0].get("eventCode")
            if code == "wbx_design_canvas_task_create":
                state["create_canvas"] = 1
            elif code == "chat_request_send":
                state["chat_5"] = 5
            return True
        T.report_events = report

        def claim(acc, code):
            claimed.append(code)
            return {"ok": True, "credit": 100}
        T.claim_task = claim
        T.accept_tasks = lambda acc, codes, chunk=20: {
            "ok": True, "accepted": list(codes), "failed": [], "reasons": []}
        T.do_cat_travel = lambda acc: {"ok": True, "msg": "ok"}

        res = T.run_growth_tasks(FakeAccount(), gap=0)
        self.assertEqual(sorted(claimed), ["chat_5", "create_canvas"])
        self.assertEqual(res["earned_credit"], 200)
        # 两个任务均已接取且在初始列表里，流程只拉两次任务列表：开头取一次、
        # 结算共享轮询一次；取代「每任务各拉满 6 次」。
        self.assertEqual(fetch_calls["n"], 2, fetch_calls["n"])

    def test_runtime_target_overrides_static_spec(self):
        # create_canvas 的静态 spec 目标为 1，但上游返回运行时目标 3。首轮进度
        # 1 未达运行时目标不得领奖，涨到 3 才领，证明用的是账号返回的目标。
        polls = {"n": 0}
        claimed = []

        def fetch(acc):
            polls["n"] += 1
            cur = 1 if polls["n"] == 1 else 3
            return [_task("create_canvas", status="accepted",
                          current=cur, target=3)]
        T.fetch_growth_tasks = fetch
        T.claim_task = lambda acc, code: (claimed.append(code)
                                          or {"ok": True, "credit": 50})

        logs = []
        earned = T._settle_reported(
            FakeAccount(), {"create_canvas": (0, 3, "创建设计任务")}, logs, 0)
        self.assertGreaterEqual(polls["n"], 2, "首轮进度 1 未达目标应继续轮询")
        self.assertEqual(claimed, ["create_canvas"])
        self.assertEqual(earned, 50)

    def test_unknown_task_is_accepted_and_is_named(self):
        # 所有未接任务一律接取，包括新上线、季节性任务与不可伪造任务；
        # 事件还没登记时，日志点名说明本轮未推进。
        accepted = []
        T.fetch_growth_tasks = lambda acc: [
            _task("Future_Task_X", status="not_accepted"),
            _task("wb_wechat_oa_subscribe_task", status="not_accepted",
                  unforgeable=True)]
        T.accept_tasks = lambda acc, codes, chunk=20: (
            accepted.extend(codes) or
            {"ok": True, "accepted": list(codes), "failed": [], "reasons": []})
        T.claim_task = lambda acc, code: {"ok": True, "credit": 0}
        T.do_cat_travel = lambda acc: {"ok": True, "msg": "ok"}

        res = T.run_growth_tasks(FakeAccount(), gap=0)
        self.assertIn("Future_Task_X", accepted, "未登记任务也要接取")
        self.assertIn("wb_wechat_oa_subscribe_task", accepted,
                      "不可伪造任务也要接取")
        self.assertTrue(any("需手动操作" in line and "Future_Task_X" in line
                            for line in res["logs"]), res["logs"])

    def test_registered_expert_task_uses_its_own_id(self):
        events = []
        T.report_events = lambda acc, evs, base=None: (events.extend(evs) or True)
        T._emit_events(FakeAccount(),
                       _task("WorkBuddy_Valorant", status="accepted"), [], 0)
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0]["id"], "ex_xQHHD1u5Jkua")
        self.assertEqual(events[0]["name"], "无畏契约拼豆专家")

    def test_new_expert_tasks_use_registered_expert_ids(self):
        cases = {
            "expert_use_maidehao": ("ex_GDl31WahyIH0", "中国电商运营专家"),
            "expert_use_douyinyin": ("ex_FbY5nlWTqDod", "抖音策略师"),
            "expert_use_shangjia_xuanchuan": ("ex_Weo3x2EZuSDm",
                                             "商家宣传页与海报设计师"),
            "expert_use_bowangwang": ("ex_WZY136wV5aMt", "直播电商教练"),
        }
        for code, (expert_id, name) in cases.items():
            with self.subTest(code=code):
                self.assertEqual(T.TASK_SPECS[code]["expert"], (expert_id, name))
                self.assertEqual(T.automation_for({"task_code": code}),
                                 ("auto", ""))
                events = []
                T.report_events = lambda acc, evs, base=None: (
                    events.extend(evs) or True)
                T._emit_events(FakeAccount(), _task(code), [], 0)
                self.assertEqual(len(events), 1, events)
                self.assertEqual(events[0]["id"], expert_id)
                self.assertEqual(events[0]["name"], name)

    def test_already_claimed_not_reclaimed(self):
        # 刷新后状态已是 claimed，直接移出，不再调用 claim_task。
        claimed = []
        T.fetch_growth_tasks = lambda acc: [
            _task("chat_5", status="claimed", current=5, target=5)]
        T.claim_task = lambda acc, code: (claimed.append(code)
                                         or {"ok": True, "credit": 100})

        logs = []
        earned = T._settle_reported(
            FakeAccount(), {"chat_5": (4, 5, "发起 5 次对话")}, logs, 0)
        self.assertEqual(claimed, [])
        self.assertEqual(earned, 0)


class GrowthRunGuardTests(unittest.TestCase):
    def setUp(self):
        self._orig = (T.fetch_growth_tasks, T.accept_tasks, T.claim_task,
                      T.report_events, T.do_cat_travel, T.growth_guard)
        T._gates.clear()

    def tearDown(self):
        (T.fetch_growth_tasks, T.accept_tasks, T.claim_task,
         T.report_events, T.do_cat_travel, T.growth_guard) = self._orig
        T._gates.clear()

    def _blocking_worker(self, started, release, calls):
        @T.growth_guard("该账号的成长任务正在执行，请稍后再试")
        def worker(account, gap=1.0):
            calls.append(account.uid)
            started.set()
            release.wait(5)
            return {"ok": True, "logs": [], "earned_credit": 0}
        return worker

    def test_overlapping_call_is_rejected(self):
        # 首轮真正阻塞在闸门内时，另一线程的调用必须被拒绝且不进入函数体。
        started, release, calls = threading.Event(), threading.Event(), []
        worker = self._blocking_worker(started, release, calls)
        acc = FakeAccount()

        first = {}
        t1 = threading.Thread(target=lambda: first.update(worker(acc, gap=0)))
        t1.start()
        try:
            self.assertTrue(started.wait(5), "首轮应该已经进入闸门")
            second = worker(acc, gap=0)
            self.assertFalse(second["ok"])
            self.assertTrue(second["busy"])
            self.assertIn("正在执行", second["msg"])
            self.assertEqual(calls, [acc.uid], "被拒绝的调用不得进入函数体")
        finally:
            release.set()
            t1.join(5)
        self.assertFalse(t1.is_alive())
        self.assertTrue(first.get("ok"))

    def test_gate_released_after_failure(self):
        # 函数抛错后闸门必须释放，下一次调用能正常进入。
        calls = []

        @T.growth_guard("忙碌")
        def boom(account):
            calls.append(account.uid)
            raise RuntimeError("boom")

        acc = FakeAccount()
        with self.assertRaises(RuntimeError):
            boom(acc)
        self.assertEqual(calls, [acc.uid])
        with self.assertRaises(RuntimeError):
            boom(acc)
        self.assertEqual(calls, [acc.uid, acc.uid], "异常路径不得卡住闸门")

    def test_nested_same_account_is_allowed(self):
        # 全任务执行内部还会跑猫猫旅行，同一线程的嵌套获取必须放行。
        calls = []

        @T.growth_guard("外层忙碌")
        def outer(account):
            calls.append("outer")
            return inner(account)

        @T.growth_guard("内层忙碌")
        def inner(account):
            calls.append("inner")
            return {"ok": True}

        res = outer(FakeAccount())
        self.assertTrue(res["ok"])
        self.assertEqual(calls, ["outer", "inner"])


class TasksRunRouteTests(unittest.TestCase):
    class Handler(wb_proxy.Handler):
        def __init__(self):
            self.captured = None

        def _json(self, code, obj):
            self.captured = (code, obj)

    def setUp(self):
        self.accounts = [
            _RouteAccount("c1", "国内一"),
            _RouteAccount("c2", "国内二"),
        ]
        self.pool = _RoutePool(self.accounts)
        for target, value in (("POOL", self.pool),
                              ("log", lambda *a, **k: None),
                              ("add_log_entry", lambda *a, **k: None),
                              ("invalidate_tasks_cache", lambda: None)):
            patcher = mock.patch.object(wb_proxy, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, payload):
        handler = self.Handler()
        handler._route_tasks_run(payload)
        code, body = handler.captured
        self.assertEqual(code, 200)
        return body

    def test_busy_account_is_reported_as_failure(self):
        # 拦截层返回忙碌时，聚合响应不能谎报成功。
        def busy(account, gap=1.0):
            return {"ok": False, "busy": True, "msg": "该账号的成长任务正在执行，请稍后再试",
                    "logs": ["⏭ 该账号的成长任务正在执行，跳过本次"], "earned_credit": 0}
        with mock.patch.object(wb_tasks, "run_growth_tasks", busy):
            body = self.call({"uid": "c1"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["busy_accounts"], ["c1"])
        self.assertIn("成长任务", body["msg"])

    def test_partial_busy_keeps_ok_and_lists_busy(self):
        def mixed(account, gap=1.0):
            if account.uid == "c1":
                return {"ok": False, "busy": True, "msg": "忙碌", "logs": [], "earned_credit": 0}
            return {"ok": True, "logs": [], "earned_credit": 50}
        with mock.patch.object(wb_tasks, "run_growth_tasks", mixed):
            body = self.call({})
        self.assertTrue(body["ok"])
        self.assertEqual(body["busy_accounts"], ["c1"])
        self.assertEqual(body["credit_added"], 50)

    def test_travel_reports_busy_as_failure(self):
        def busy(account):
            return {"ok": False, "busy": True, "msg": "该账号的猫猫旅行正在执行，请稍后再试"}
        with mock.patch.object(wb_tasks, "do_cat_travel", busy):
            handler = self.Handler()
            handler._route_tasks_travel({"uid": "c1"})
        body = handler.captured[1]
        self.assertFalse(body["ok"])
        self.assertEqual(body["busy_accounts"], ["c1"])
        self.assertTrue(body["results"][0]["busy"])

    def test_streak_reports_busy_as_failure(self):
        def busy(account):
            return {"ok": False, "busy": True, "msg": "该账号的连登管家正在执行，请稍后再试"}
        with mock.patch.object(wb_tasks, "run_streak_bonus", busy):
            handler = self.Handler()
            handler._route_tasks_streak_bonus({"uid": "c1"})
        body = handler.captured[1]
        self.assertFalse(body["ok"])
        self.assertEqual(body["busy_accounts"], ["c1"])
        self.assertIn("成长任务", body["msg"])


class ActivityReportGateTests(unittest.TestCase):
    """活跃上报与成长任务共用账号闸门：并发时必须拒绝其中一方。"""

    def setUp(self):
        self._orig = (wb_tasks.build_event, wb_tasks.report_events,
                      wb_tasks.fetch_streak_days)
        T._gates.clear()
        self.calls = []

    def tearDown(self):
        (wb_tasks.build_event, wb_tasks.report_events,
         wb_tasks.fetch_streak_days) = self._orig
        T._gates.clear()

    def _account(self):
        acc = wb_accounts.Account.__new__(wb_accounts.Account)
        acc.uid = "u-activity"
        acc.realm = "cn"
        acc.path = ""
        acc.is_api_key = False
        acc.is_enterprise = lambda: False
        return acc

    def _stub_upstream(self):
        wb_tasks.build_event = lambda account, kind: {"eventCode": "chat_request_send"}
        wb_tasks.report_events = lambda account, events, base=None: (
            self.calls.append("report") or True)
        wb_tasks.fetch_streak_days = lambda account: 5

    def test_overlapping_growth_run_rejects_activity_report(self):
        # 用真实公开入口 run_growth_tasks 持锁：查询被阻塞在闸门内时，活跃上报必须被拒绝。
        self._stub_upstream()
        started, release = threading.Event(), threading.Event()

        def blocking_fetch(account):
            started.set()
            release.wait(5)
            return []

        wb_tasks.fetch_growth_tasks = blocking_fetch
        run_account = FakeAccount()
        run_account.uid = "u-activity"
        outcome = {}
        runner = threading.Thread(
            target=lambda: outcome.update(wb_tasks.run_growth_tasks(run_account, gap=0)))
        runner.start()
        try:
            self.assertTrue(started.wait(5), "任务执行应该已经持锁进入查询")
            res = self._account().report_activity()
            self.assertTrue(res.get("busy"))
            self.assertFalse(res.get("ok"))
            self.assertEqual(self.calls, [], "被拒绝的上报不得发出任何请求")
        finally:
            release.set()
            runner.join(5)
        self.assertFalse(runner.is_alive())
        self.assertFalse(outcome.get("ok", True))

    def test_activity_report_runs_after_release(self):
        self._stub_upstream()
        res = self._account().report_activity()
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("streak_days"), 5)
        self.assertEqual(self.calls, ["report"])


class NightTaskTests(unittest.TestCase):
    """夜猫任务上下线不影响其他任务：缺席不算失败，改名后按 night 标记仍能识别。"""

    def setUp(self):
        self._orig = (T.query_growth_tasks, T.accept_tasks, T.claim_task,
                      T.report_events, T.in_night_window, T.TASK_SPECS)
        T.in_night_window = lambda: True
        T.TASK_SPECS = dict(T.TASK_SPECS)

    def tearDown(self):
        (T.query_growth_tasks, T.accept_tasks, T.claim_task,
         T.report_events, T.in_night_window, T.TASK_SPECS) = self._orig

    def test_missing_task_is_not_a_failure(self):
        T.query_growth_tasks = lambda acc: (True, [_task("chat_5")])
        T.claim_task = lambda acc, code: {"ok": True, "credit": 1}
        res = T.run_night_growth(FakeAccount())
        self.assertTrue(res["ok"])
        self.assertEqual(res["earned_credit"], 0)
        self.assertIn("未提供夜猫子任务", " ".join(res["logs"]))

    def test_query_failure_is_reported(self):
        # 查询失败与任务缺席必须区分：失败保留 ok False，不报「未提供」。
        T.query_growth_tasks = lambda acc: (False, [])
        res = T.run_night_growth(FakeAccount())
        self.assertFalse(res["ok"])
        logs = " ".join(res["logs"])
        self.assertIn("查询失败", logs)
        self.assertNotIn("未提供", logs)

    def test_renamed_task_with_night_marker_is_recognized(self):
        T.TASK_SPECS["black_cat_next"] = {"kind": "cat", "night": True, "target": 3,
                                          "reward": 100, "name": "夜猫子任务"}
        T.query_growth_tasks = lambda acc: (True, [
            _task("black_cat_next", status="claimed", current=3, target=3)])
        T.claim_task = lambda acc, code: self.fail("已领奖的任务不得再次领奖")
        res = T.run_night_growth(FakeAccount())
        self.assertTrue(res["ok"])
        self.assertIn("已领奖", " ".join(res["logs"]))

    def test_other_cat_task_is_not_treated_as_night_task(self):
        # 普通 cat 类任务没有 night 标记，不能当成夜猫任务接取与领奖。
        T.TASK_SPECS["normal_cat"] = {"kind": "cat", "target": 1,
                                      "reward": 10, "name": "普通猫任务"}
        T.query_growth_tasks = lambda acc: (True, [
            _task("normal_cat", status="not_accepted", current=0, target=1)])
        T.accept_tasks = lambda acc, codes, chunk=20: self.fail("不应接取普通 cat 任务")
        res = T.run_night_growth(FakeAccount())
        self.assertTrue(res["ok"])
        self.assertIn("未提供夜猫子任务", " ".join(res["logs"]))


class _RouteAccount(object):
    def __init__(self, uid, nickname):
        self.uid = uid
        self.nickname = nickname
        self.realm = "cn"
        self.enabled = True


class _RoutePool(object):
    def __init__(self, accounts):
        self.accounts = accounts

    def get(self, uid):
        return next((a for a in self.accounts if a.uid == uid), None)


if __name__ == "__main__":
    unittest.main()
