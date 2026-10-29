"""后台排程时段可配置：默认保持 09:00/21:00、22:00、01:00，面板保存后立即生效。

总开关的运行时行为：开关关闭后排程周期不执行任务；面板切换路由翻转运行中的
调度器并把开关写进设置。

不联网：调度器只做时段计算与巡检循环推演，不启动线程。
"""
import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-scheduler-hours-")

import wb_proxy as P  # noqa: E402
import wb_scheduler  # noqa: E402
import wb_settings  # noqa: E402
import wb_tasks  # noqa: E402


class FakePool(object):
    def __init__(self, directory):
        self.dir = directory
        self.accounts = []


class SchedulerHoursSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = os.environ["ACCOUNTS_DIR"]
        with wb_settings._lock:
            data = wb_settings.load(self.directory)
            data.pop("scheduler_hours", None)
            wb_settings.save(self.directory, data)

    def test_defaults_match_the_shipped_schedule(self):
        self.assertEqual(wb_settings.scheduler_hours(self.directory), {
            "checkin": [9, 21],
            "travel": [9, 21],
            "keepalive": [22],
            "cat": [1],
        })

    def test_values_are_stored_sorted_and_deduplicated(self):
        saved = wb_settings.set_scheduler_hours(self.directory, {
            "checkin": ["21", 9, 9], "travel": [9, 21],
            "keepalive": [], "cat": [1],
        })
        self.assertEqual(saved["checkin"], [9, 21])
        self.assertEqual(saved["keepalive"], [])
        self.assertEqual(wb_settings.scheduler_hours(self.directory), saved)

    def test_an_all_empty_schedule_is_refused(self):
        with self.assertRaises(ValueError):
            wb_settings.set_scheduler_hours(
                self.directory,
                {group: [] for group in wb_settings.SCHEDULER_GROUPS})

    def test_stored_all_empty_hours_fall_back_to_defaults(self):
        with wb_settings._lock:
            data = wb_settings.load(self.directory)
            data["scheduler_hours"] = {group: []
                                       for group in wb_settings.SCHEDULER_GROUPS}
            wb_settings.save(self.directory, data)
        self.assertEqual(wb_settings.scheduler_hours(self.directory),
                         wb_settings.DEFAULT_SCHEDULER_HOURS)

    def test_the_scheduler_switch_defaults_to_on_and_persists(self):
        self.assertTrue(wb_settings.scheduler_enabled(self.directory))
        wb_settings.set_scheduler_enabled(self.directory, False)
        self.assertFalse(wb_settings.scheduler_enabled(self.directory))
        wb_settings.set_scheduler_enabled(self.directory, True)
        self.assertTrue(wb_settings.scheduler_enabled(self.directory))

    def test_out_of_range_hours_are_refused(self):
        with self.assertRaises(ValueError):
            wb_settings.set_scheduler_hours(self.directory, {"checkin": [24]})
        with self.assertRaises(ValueError):
            wb_settings.set_scheduler_hours(self.directory, {"checkin": ["soon"]})
        with self.assertRaises(ValueError):
            wb_settings.set_scheduler_hours(self.directory, "9,21")

    def test_a_broken_group_falls_back_to_the_default(self):
        with wb_settings._lock:
            data = wb_settings.load(self.directory)
            data["scheduler_hours"] = {"checkin": "abc", "cat": []}
            wb_settings.save(self.directory, data)
        hours = wb_settings.scheduler_hours(self.directory)
        self.assertEqual(hours["checkin"], [9, 21])
        self.assertEqual(hours["cat"], [])
        self.assertEqual(hours["keepalive"], [22])


class SchedulerHoursTests(unittest.TestCase):
    def test_scheduler_reads_the_saved_schedule(self):
        directory = os.environ["ACCOUNTS_DIR"]
        wb_settings.set_scheduler_hours(directory, {
            "checkin": [8, 20], "travel": [9], "keepalive": [], "cat": [3],
        })
        scheduler = wb_scheduler.Scheduler(FakePool(directory))
        self.assertEqual(scheduler.checkin_hours, [8, 20])
        self.assertEqual(scheduler.travel_hours, [9])
        self.assertEqual(scheduler.keepalive_hours, [])
        self.assertEqual(scheduler.cat_hours, [3])
        self.assertEqual(scheduler.all_hours, [3, 8, 9, 20])
        status = scheduler.status()
        self.assertIn("08:00/20:00", status["mode_cn"])
        self.assertIn("09:00", status["mode_cn"])
        self.assertIn("关闭", status["mode_cn"])

    def test_apply_schedule_takes_effect_without_a_restart(self):
        scheduler = wb_scheduler.Scheduler(FakePool(os.environ["ACCOUNTS_DIR"]))
        scheduler.apply_schedule({"checkin": [7], "travel": [6],
                                  "keepalive": [23], "cat": [0]})
        self.assertEqual(scheduler.all_hours, [0, 6, 7, 23])
        self.assertEqual(scheduler.next_run_time is not None, True)

    def test_an_all_empty_schedule_falls_back_to_defaults(self):
        scheduler = wb_scheduler.Scheduler(FakePool(os.environ["ACCOUNTS_DIR"]))
        scheduler.apply_schedule({"checkin": [], "travel": [],
                                  "keepalive": [], "cat": []})
        self.assertEqual(scheduler.all_hours, [1, 9, 21, 22])
        self.assertNotEqual(scheduler.next_run_time, "未设置")

    def test_a_cycle_restores_the_previous_task_logger(self):
        scheduler = wb_scheduler.Scheduler(FakePool(os.environ["ACCOUNTS_DIR"]))
        sentinel = lambda msg: None
        previous = wb_tasks.set_logger(sentinel)
        try:
            scheduler._run_cycle()
            self.assertIs(wb_tasks._log, sentinel)
        finally:
            wb_tasks.set_logger(previous)


class _ScheduledAccount(object):
    """只记录调用次数的国内版账号，不联网。"""

    def __init__(self):
        self.uid = "sched-uid-0001"
        self.realm = "cn"
        self.enabled = True
        self.expires_at = 0.0
        self.last_error = ""
        self.checkins = 0
        self.reports = 0

    def can_checkin(self):
        return True

    def checkin(self, trigger=""):
        self.checkins += 1
        return {"ok": True, "msg": "签到完成"}

    def can_report_activity(self):
        return True

    def refresh(self):
        return True


class SchedulerGroupTests(unittest.TestCase):
    """每个任务只在自己的配置整点执行；手动触发强制跑完整轮。"""

    def setUp(self):
        self.directory = os.environ["ACCOUNTS_DIR"]
        wb_settings.set_scheduler_hours(self.directory, {
            "checkin": [10], "travel": [9], "keepalive": [22], "cat": [1],
        })

    def _run(self, hour, force=False, calls=None):
        account = _ScheduledAccount()
        pool = FakePool(self.directory)
        pool.accounts = [account]
        scheduler = wb_scheduler.Scheduler(pool)
        calls = calls if calls is not None else []
        stamp = time.struct_time((2026, 10, 20, hour, 0, 0, 0, 0, -1))

        def fake_report(acc):
            acc.reports += 1
            return {"ok": True, "msg": "活跃上报完成"}

        with mock.patch.object(wb_scheduler.wb_tasks, "cst_now", return_value=stamp), \
                mock.patch.object(wb_scheduler.wb_tasks, "report_activity", fake_report), \
                mock.patch.object(wb_scheduler, "do_cat_travel",
                                  lambda acc: calls.append("travel") or
                                  {"ok": True, "msg": "猫猫状态正常"}), \
                mock.patch.object(wb_scheduler.wb_tasks, "run_streak_bonus",
                                  lambda acc: calls.append("streak") or {"logs": []}), \
                mock.patch.object(wb_scheduler.wb_tasks, "run_night_growth",
                                  lambda acc: calls.append("night") or {"logs": []}):
            scheduler._run_cycle(force=force)
        return account, calls

    def test_only_the_checkin_group_runs_at_its_hour(self):
        account, calls = self._run(10)
        self.assertEqual(account.checkins, 1)
        self.assertEqual(account.reports, 1)
        self.assertEqual(calls, [], "签到小时不该顺带跑猫猫旅行与夜猫任务")

    def test_manual_trigger_runs_every_group(self):
        account, calls = self._run(10, force=True)
        self.assertEqual(account.checkins, 1)
        self.assertEqual(calls, ["travel", "streak", "night"])


class _RouteHandler(P.Handler):
    """路由方法的替身：继承 Handler 只记录应答；不调用基类初始化，不走 HTTP。"""

    def __init__(self):
        self.code = None
        self.payload = None

    def _error(self, code, message, err_type="server_error"):
        self.code = code
        self.payload = message
        return None

    def _json(self, code, obj):
        self.code = code
        self.payload = obj
        return None


class _ScriptedStopEvent(object):
    """停止信号替身：放行指定次数的等待后结束巡检循环，全程不启动线程。"""

    def __init__(self, passes):
        self._passes = passes
        self._set = False

    def wait(self, timeout):
        if self._set:
            return True
        if self._passes <= 0:
            self._set = True
            return True
        self._passes -= 1
        return False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True


class SchedulerSwitchTests(unittest.TestCase):
    """总开关关闭时排程周期不执行任务；面板切换翻转运行中的调度器并保存开关。"""

    def setUp(self):
        self.directory = os.environ["ACCOUNTS_DIR"]
        wb_settings.set_scheduler_hours(self.directory, {
            "checkin": [10], "travel": [9], "keepalive": [22], "cat": [1],
        })
        wb_settings.set_scheduler_enabled(self.directory, True)
        self.account = _ScheduledAccount()
        self.pool = FakePool(self.directory)
        self.pool.accounts = [self.account]
        self.scheduler = wb_scheduler.Scheduler(self.pool)
        self.calls = []

    def _toggle(self):
        handler = _RouteHandler()
        with mock.patch.object(P, "SCHEDULER", self.scheduler):
            P.Handler._route_scheduler_toggle(handler, {})
        self.assertEqual(handler.code, 200)
        return handler.payload

    def _run_one_loop_pass(self):
        """巡检循环按脚本走一轮：整点停在 10:00，任务调用全部换成本地替身。"""
        stamp = time.struct_time((2026, 10, 20, 10, 0, 0, 0, 0, -1))

        def fake_report(acc):
            self.calls.append("report")
            return {"ok": True, "msg": "活跃上报完成"}

        with mock.patch.object(wb_scheduler.wb_tasks, "cst_now",
                               return_value=stamp), \
                mock.patch.object(wb_scheduler.wb_tasks, "report_activity",
                                  fake_report), \
                mock.patch.object(wb_scheduler, "do_cat_travel",
                                  lambda acc: self.calls.append("travel")
                                  or {"ok": True, "msg": "猫猫状态正常"}), \
                mock.patch.object(wb_scheduler.wb_tasks, "run_streak_bonus",
                                  lambda acc: self.calls.append("streak")
                                  or {"logs": []}), \
                mock.patch.object(wb_scheduler.wb_tasks, "run_night_growth",
                                  lambda acc: self.calls.append("night")
                                  or {"logs": []}), \
                mock.patch.object(wb_scheduler.time, "sleep",
                                  lambda *args: None):
            self.scheduler._run_loop(_ScriptedStopEvent(1))

    def test_pausing_at_the_panel_keeps_the_cycle_from_running_tasks(self):
        self._toggle()
        self.assertFalse(self.scheduler.enabled)
        self.assertFalse(wb_settings.scheduler_enabled(self.directory))
        self._run_one_loop_pass()
        self.assertEqual(self.account.checkins, 0)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.scheduler.last_run_time)

    def test_the_switch_can_be_turned_back_on_at_the_panel(self):
        self._toggle()
        self._toggle()
        self.assertTrue(self.scheduler.enabled)
        self.assertTrue(wb_settings.scheduler_enabled(self.directory))
        self._run_one_loop_pass()
        self.assertGreaterEqual(self.account.checkins, 1)
        self.assertIn("report", self.calls)

    def test_toggle_response_reports_the_new_switch_state(self):
        self.assertIs(self._toggle()["enabled"], False)
        self.assertIs(self._toggle()["enabled"], True)
        self.assertIs(self.scheduler.status()["enabled"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
