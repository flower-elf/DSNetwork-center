"""测计价刷新间隔的契约：存储单位是分钟，旧的小时键一次性迁移。迁移按 x60 换算、写入新键并删除旧键，只做一次；还要断言 0 表示停用、上限、非法值回落，以及刷新循环的等待算术。不联网，抓取被替身接管，循环测试只观察一个周期的时长。用 python tests/run_all.py _test_pricing_refresh 运行。
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_pricing
import wb_settings


def _read_settings(path):
    with open(os.path.join(path, "settings.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _write_settings(path, data):
    with open(os.path.join(path, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


class PricingRefreshSettingTests(unittest.TestCase):
    """存储单位是分钟，旧的小时值会被换算。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-refresh-setting-")

    def test_default_is_five_minutes(self):
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 5.0)

    def test_a_legacy_hours_value_is_converted_once(self):
        _write_settings(self.dir, {"pricing_refresh_hours": 6})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 360.0)
        stored = _read_settings(self.dir)
        self.assertEqual(stored["pricing_refresh_minutes"], 360.0)
        self.assertNotIn("pricing_refresh_hours", stored)

    def test_the_conversion_does_not_run_again(self):
        _write_settings(self.dir, {"pricing_refresh_hours": 6})
        wb_settings.pricing_refresh_minutes(self.dir)
        # 回滚安装可能写出过期的旧键，它不得把小时值复活到已迁移的新值之上。
        data = _read_settings(self.dir)
        data["pricing_refresh_hours"] = 12
        _write_settings(self.dir, data)
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 360.0)

    def test_a_legacy_zero_still_means_off(self):
        _write_settings(self.dir, {"pricing_refresh_hours": 0})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 0.0)
        self.assertEqual(_read_settings(self.dir)["pricing_refresh_minutes"], 0.0)

    def test_fractional_legacy_hours_convert(self):
        _write_settings(self.dir, {"pricing_refresh_hours": 0.5})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 30.0)

    def test_the_old_cap_remeasures_to_a_month_of_minutes(self):
        # 旧上限是 720 小时，新单位下是 43200 分钟，必须原样保留，
        # 不能按同一个裸数截断。
        _write_settings(self.dir, {"pricing_refresh_hours": 720})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 43200.0)

    def test_a_legacy_value_that_is_not_a_number_falls_back(self):
        _write_settings(self.dir, {"pricing_refresh_hours": "soon"})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 5.0)
        stored = _read_settings(self.dir)
        self.assertNotIn("pricing_refresh_hours", stored)
        self.assertNotIn("pricing_refresh_minutes", stored)

    def test_the_minutes_key_wins_over_a_leftover_legacy_key(self):
        _write_settings(self.dir, {"pricing_refresh_minutes": 15,
                                   "pricing_refresh_hours": 6})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 15.0)

    def test_a_negative_or_unparsable_minutes_value(self):
        _write_settings(self.dir, {"pricing_refresh_minutes": -5})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 0.0)
        _write_settings(self.dir, {"pricing_refresh_minutes": "often"})
        self.assertEqual(wb_settings.pricing_refresh_minutes(self.dir), 5.0)

    def test_the_setter_stores_minutes_and_drops_the_legacy_key(self):
        _write_settings(self.dir, {"pricing_refresh_hours": 6})
        self.assertEqual(
            wb_settings.set_pricing_refresh_minutes(self.dir, 20), 20.0)
        stored = _read_settings(self.dir)
        self.assertEqual(stored["pricing_refresh_minutes"], 20.0)
        self.assertNotIn("pricing_refresh_hours", stored)

    def test_the_setter_clamps_in_minutes(self):
        self.assertEqual(
            wb_settings.set_pricing_refresh_minutes(self.dir, 10 ** 9), 43200.0)
        self.assertEqual(
            wb_settings.set_pricing_refresh_minutes(self.dir, -3), 0.0)

    def test_the_setter_keeps_the_stored_value_for_garbage_input(self):
        _write_settings(self.dir, {"pricing_refresh_minutes": 30})
        self.assertEqual(
            wb_settings.set_pricing_refresh_minutes(self.dir, "x"), 30.0)


class PriceRefresherTests(unittest.TestCase):
    """刷新器按分钟等待，改动当场生效。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-refresher-")
        self._previous = wb_pricing.data_dir()
        wb_pricing.set_data_dir(self.dir)
        self._threads = []

    def tearDown(self):
        for thread in self._threads:
            thread.stop()
            thread.join(timeout=5)
        wb_pricing.set_data_dir(self._previous)

    def _refresher(self, interval_minutes):
        refresher = wb_pricing.PriceRefresher(interval_minutes=interval_minutes)
        refresher.log = lambda msg: None
        self.calls = []
        refresher.run_once = lambda: (self.calls.append(time.time()),
                                      (True, ""))[1]
        self._threads.append(refresher)
        return refresher

    @staticmethod
    def _wait_for_calls(calls, count, deadline_sec):
        end = time.time() + deadline_sec
        while len(calls) < count and time.time() < end:
            time.sleep(0.05)
        return len(calls)

    def test_the_unit_is_minutes(self):
        refresher = wb_pricing.PriceRefresher(interval_minutes=5)
        self.assertEqual(refresher.interval_minutes, 5)
        self.assertAlmostEqual(refresher.interval_seconds(), 300.0)
        self.assertEqual(wb_pricing.DEFAULT_REFRESH_MINUTES, 5)

    def test_status_reports_minutes(self):
        status = wb_pricing.PriceRefresher(interval_minutes=7).status()
        self.assertEqual(status["interval_minutes"], 7)
        self.assertTrue(status["enabled"])
        self.assertNotIn("interval_hours", status)

    def test_the_loop_cycles_on_minutes(self):
        # 0.02 分钟等于 1.2 秒。循环如果还按小时乘 3600 睡，截止时间内
        # 不会有第二次调用。
        refresher = self._refresher(0.02)
        refresher.start()
        self.assertGreaterEqual(self._wait_for_calls(self.calls, 2, 8.0), 2)
        self.assertLess(self.calls[1] - self.calls[0], 5.0)

    def test_set_interval_cuts_a_long_wait_short(self):
        refresher = self._refresher(60)
        refresher.start()
        # 启动时先立即抓取一次，然后循环停等一小时。
        time.sleep(0.3)
        self.assertEqual(len(self.calls), 1)
        refresher.set_interval(0.02)
        self.assertGreaterEqual(self._wait_for_calls(self.calls, 2, 8.0), 2)

    def test_zero_parks_instead_of_fetching(self):
        refresher = self._refresher(0)
        refresher.start()
        time.sleep(0.4)
        self.assertEqual(self.calls, [])
        self.assertFalse(refresher.status()["enabled"])
        self.assertIsNone(refresher.next_run)

    def test_zero_then_a_value_resumes(self):
        refresher = self._refresher(0)
        refresher.start()
        time.sleep(0.2)
        self.assertEqual(self.calls, [])
        refresher.set_interval(0.02)
        self.assertGreaterEqual(self._wait_for_calls(self.calls, 1, 8.0), 1)


if __name__ == "__main__":
    unittest.main()
