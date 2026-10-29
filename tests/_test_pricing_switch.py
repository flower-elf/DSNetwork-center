"""测 pricing_enabled 开关的契约：默认开启、只有显式关闭才生效，关闭期间不计价也不铸策略、行不算未定价，重新开启后补计价并恢复抓取循环。用 python tests/run_all.py _test_pricing_switch 运行。
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_pricing
import wb_proxy
import wb_usage as U
import wb_settings


def _write_settings(path, data):
    with open(os.path.join(path, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


class PricingEnabledSettingTests(unittest.TestCase):
    """存储的开关默认开启，只有显式关闭才生效。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-pricing-switch-")
        self._previous = wb_pricing._settings_dir_override
        wb_pricing.set_settings_dir(self.dir)

    def tearDown(self):
        wb_pricing.set_settings_dir(self._previous)

    def test_a_settings_file_without_the_key_reads_as_on(self):
        # 从未写入该键的旧版本升级后仍保持原有行为，与发布说明一致。
        self.assertIs(wb_settings.pricing_enabled(self.dir), True)

    def test_the_switch_read_is_cached_but_never_goes_stale(self):
        # cost_for_row 会为用量页的每一行读取该开关，因此按路径、mtime 和大小缓存；
        # 面板保存 settings.json 后必须立即生效。
        self.assertIs(wb_pricing.pricing_enabled(), True)
        time.sleep(0.02)
        _write_settings(self.dir, {"pricing_enabled": False})
        self.assertIs(wb_pricing.pricing_enabled(), False)


class PricingGateTests(unittest.TestCase):
    """开关关闭时不计价，也不生成策略。"""

    def setUp(self):
        self.accounts = tempfile.mkdtemp(prefix="wb-pricing-switch-acct-")
        self.data = tempfile.mkdtemp(prefix="wb-pricing-switch-data-")
        self._prev_settings = wb_pricing._settings_dir_override
        self._prev_data = wb_pricing.data_dir()
        wb_pricing.set_settings_dir(self.accounts)
        wb_pricing.set_data_dir(self.data)
        doc = wb_pricing.load_pricing()
        self.model = sorted((doc.get("models") or {}).keys())[0]
        self.row = {"at": time.time(), "model": self.model, "realm": "intl",
                    "prompt_tokens": 1000, "completion_tokens": 500,
                    "cached_tokens": 0, "total_tokens": 1500}

    def tearDown(self):
        wb_pricing.set_settings_dir(self._prev_settings)
        wb_pricing.set_data_dir(self._prev_data)

    def test_cost_for_row_is_disabled_when_off(self):
        wb_settings.set_pricing_enabled(self.accounts, False)
        off = wb_pricing.cost_for_row(dict(self.row))
        self.assertFalse(off["known"])
        self.assertTrue(off.get("disabled"))
        self.assertEqual(off["cny"], 0.0)
        # 悬停明细保持为空，不解释虚构的价格。
        self.assertIsNone(off["rates"])

    def test_current_policy_id_and_ensure_policy_mint_nothing_when_off(self):
        wb_settings.set_pricing_enabled(self.accounts, False)
        self.assertIsNone(wb_pricing.current_policy_id(self.model))
        self.assertIsNone(wb_pricing.ensure_policy(self.model))

    def test_the_aggregate_does_not_call_a_disabled_row_unpriced(self):
        wb_settings.set_pricing_enabled(self.accounts, False)
        bucket = {}
        U._fold_cost(bucket, wb_pricing.cost_for_row(dict(self.row)),
                            self.model)
        self.assertEqual(bucket.get("cost_missing"), None)
        # 开关开启时，未定价的行仍会被报告。
        wb_settings.set_pricing_enabled(self.accounts, True)
        bucket = {}
        U._fold_cost(bucket, wb_pricing.cost_for_row(
            {"model": "no-such-model-anywhere", "prompt_tokens": 1}),
            "no-such-model-anywhere")
        self.assertEqual(bucket.get("cost_missing"), {"no-such-model-anywhere": 1})

    def test_the_usage_view_reports_no_cost_while_off(self):
        wb_settings.set_pricing_enabled(self.accounts, False)
        status = wb_pricing.PriceRefresher(5).status()
        self.assertIs(status["master_enabled"], False)
        self.assertIs(status["enabled"], False)
        wb_settings.set_pricing_enabled(self.accounts, True)
        status = wb_pricing.PriceRefresher(5).status()
        self.assertIs(status["master_enabled"], True)
        self.assertIs(status["enabled"], True)


class PricingBackfillTests(unittest.TestCase):
    """开关关闭期间的请求在重新开启后计价。

    计价发生在读取时，不会回写 usage.jsonl；没有策略引用的行在开关和价格表
    允许时立即计价。重新开启必须真正触发抓取，关闭期间首次调用的模型只有
    经过抓取注册后才有价格。
    """

    # 两个内置快照未收录的条目，另加若干已知模型。
    OR = {
        "deepseek/deepseek-r1-0528": {"prompt": "0.0000005",
                                      "completion": "0.00000215",
                                      "input_cache_read": "0.00000035"},
        "z-ai/glm-5.3-flashx": {"prompt": "0.0000002",
                                "completion": "0.0000007",
                                "input_cache_read": "0.00000004"},
    }
    LIVE_ONLY = "glm-5.3-flashx"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-pricing-backfill-")
        self.accounts = os.path.join(self.dir, "accounts")
        os.makedirs(self.accounts, exist_ok=True)
        self._prev_settings = wb_pricing._settings_dir_override
        self._prev_data = wb_pricing.data_dir()
        self._prev_live = wb_pricing._live_index["models"]
        self._prev_by_norm = wb_pricing._live_index["by_norm"]
        wb_pricing.set_settings_dir(self.accounts)
        wb_pricing.set_data_dir(self.dir)

    def tearDown(self):
        wb_pricing.set_settings_dir(self._prev_settings)
        wb_pricing.set_data_dir(self._prev_data)
        wb_pricing._live_index.update({"models": self._prev_live,
                                       "by_norm": self._prev_by_norm,
                                       "at": None})

    def _row(self, model, at=None):
        return {"at": time.time() if at is None else at, "model": model,
                "prompt_tokens": 1000, "completion_tokens": 500,
                "cached_tokens": 0, "total_tokens": 1500}

    def test_a_row_from_the_off_period_is_priced_again_once_on(self):
        row = self._row("deepseek-r1-0528")
        wb_settings.set_pricing_enabled(self.accounts, False)
        self.assertFalse(wb_pricing.cost_for_row(dict(row))["known"])
        wb_settings.set_pricing_enabled(self.accounts, True)
        back = wb_pricing.cost_for_row(dict(row))
        self.assertTrue(back["known"])
        self.assertGreater(back["cny"], 0)

    def test_a_model_first_called_while_off_is_priced_after_the_fetch(self):
        model = self.LIVE_ONLY
        self.assertNotIn(model, wb_pricing.load_pricing().get("models") or {})
        row = self._row(model)
        wb_settings.set_pricing_enabled(self.accounts, False)
        self.assertFalse(wb_pricing.cost_for_row(dict(row))["known"])

        # 重新开启会触发抓取，只有抓取才会把仅在线存在的名称注册进策略表；
        # 在此之前该行没有任何价格来源，内置快照也不认识这个名称。
        wb_settings.set_pricing_enabled(self.accounts, True)
        doc, _unpriced, _ov = wb_pricing.build_snapshot(dict(self.OR), None,
                                                       [model])
        self.assertIn(model, doc["models"])
        wb_pricing.record_policies(doc, at=time.time())

        back = wb_pricing.cost_for_row(dict(row))
        self.assertTrue(back["known"], back)
        self.assertGreater(back["cny"], 0)


class PricingRefresherSwitchTests(unittest.TestCase):
    """刷新循环在开关关闭时挂起，重新开启后恢复。"""

    def setUp(self):
        self.accounts = tempfile.mkdtemp(prefix="wb-pricing-switch-loop-acct-")
        self.data = tempfile.mkdtemp(prefix="wb-pricing-switch-loop-data-")
        self._prev_settings = wb_pricing._settings_dir_override
        self._prev_data = wb_pricing.data_dir()
        wb_pricing.set_settings_dir(self.accounts)
        wb_pricing.set_data_dir(self.data)
        self._threads = []

    def tearDown(self):
        for thread in self._threads:
            thread.stop()
            thread.join(timeout=5)
        wb_pricing.set_settings_dir(self._prev_settings)
        wb_pricing.set_data_dir(self._prev_data)

    def _seed_timeline(self):
        """非空时间线：正常重启不会重新抓取，只有开关重新开启才能解释抓取。"""
        with open(os.path.join(self.data, "pricing-timeline.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(),
                                 "policies": {"m": "p"}}) + "\n")

    def _refresher(self, interval_minutes, calls):
        refresher = wb_pricing.PriceRefresher(interval_minutes=interval_minutes)
        refresher.log = lambda msg: None
        refresher.run_once = lambda: (calls.append(time.time()), (True, ""))[1]
        self._threads.append(refresher)
        return refresher

    @staticmethod
    def _wait_for_calls(calls, count, deadline_sec):
        end = time.time() + deadline_sec
        while len(calls) < count and time.time() < end:
            time.sleep(0.05)
        return len(calls)

    def test_run_once_refuses_while_off_without_touching_the_network(self):
        def boom():
            raise AssertionError("fetch_openrouter must not run while off")
        original = wb_pricing.fetch_openrouter
        wb_pricing.fetch_openrouter = boom
        try:
            wb_settings.set_pricing_enabled(self.accounts, False)
            refresher = wb_pricing.PriceRefresher(interval_minutes=1)
            refresher.log = lambda msg: None
            self.assertEqual(refresher.run_once(), (False, "价估算已关闭"))
        finally:
            wb_pricing.fetch_openrouter = original

    def test_the_loop_does_not_fetch_while_off_and_resumes_when_on(self):
        calls = []
        wb_settings.set_pricing_enabled(self.accounts, False)
        # 0.02 分钟等于 1.2 秒；忽略开关的循环会在下方等待结束前发起调用。
        refresher = self._refresher(0.02, calls)
        refresher.start()
        time.sleep(1.8)
        self.assertEqual(calls, [])

        time.sleep(0.02)
        wb_settings.set_pricing_enabled(self.accounts, True)
        refresher.wake()
        self.assertGreaterEqual(self._wait_for_calls(calls, 1, 5), 1)

    def test_coming_back_on_fetches_at_once_even_with_a_timeline(self):
        # 间隔为 30 分钟，因此等待期内发生抓取只能是在补抓，使关闭期间的行可以计价。
        self._seed_timeline()
        calls = []
        wb_settings.set_pricing_enabled(self.accounts, False)
        refresher = self._refresher(30, calls)
        refresher.start()
        time.sleep(0.5)
        self.assertEqual(calls, [])

        wb_settings.set_pricing_enabled(self.accounts, True)
        refresher.wake()
        self.assertGreaterEqual(self._wait_for_calls(calls, 1, 5), 1)

    def test_a_normal_restart_with_a_timeline_does_not_refetch(self):
        # 已有价格的实例重启后应等待间隔结束，不发起抓取。
        self._seed_timeline()
        calls = []
        wb_settings.set_pricing_enabled(self.accounts, True)
        refresher = self._refresher(30, calls)
        refresher.start()
        time.sleep(1.0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
