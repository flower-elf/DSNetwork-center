"""测定价引擎把 usage 行折算成 OpenRouter 同口径 CNY 的契约：cache-hit/miss 拆分、快照汇率换算、条件 bands 取最紧匹配、快照形状、unknown model 返回 known=False，以及新模型的自动匹配。用 python tests/run_all.py _test_pricing 运行。
"""
import calendar
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-price-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_pricing
import wb_proxy as P
import wb_usage as U
import wb_usage_recent as R


def utc_ts(text):
    """把 UTC 墙上时间解析成 epoch；上游的 bands 用 UTC 表述。"""
    return calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


# 与时间 bands 无关的行统一用这个固定时刻。快照日后若新增覆盖该时刻的 band，测试应当直接失败，结果不能随墙上时钟变化。
_AT = utc_ts("2026-09-30 20:00:00")


class PricingEngineTests(unittest.TestCase):
    def setUp(self):
        self.pricing = wb_pricing.load_pricing()

    def test_snapshot_loads_with_its_models(self):
        # 随包快照被裁断时唯一能发现的检查：锚点模型少于十个时其余用例照样通过。
        self.assertIn("models", self.pricing)
        self.assertGreater(len(self.pricing["models"]), 10)

    def test_every_entry_is_usd_with_a_base_price(self):
        # 单一来源、统一形状。面板把这个数字标成 OpenRouter 的价格，残留的 vendor schedule 或混入的 CNY 条目会让标注失真。
        for mid, entry in self.pricing["models"].items():
            self.assertEqual(entry.get("currency"), "USD", mid)
            self.assertNotIn("schedule", entry, mid)
            self.assertNotIn("peak", entry, mid)
            self.assertIsInstance(entry.get("flat"), dict, mid)
            for key in ("input_cache_hit", "input_cache_miss", "output"):
                self.assertIn(key, entry["flat"], "%s.%s" % (mid, key))
            for band in entry.get("bands") or []:
                self.assertIsInstance(band.get("flat"), dict, mid)
                # 一个 band 只带一个条件：长度阈值或 UTC 时间窗，二者不可兼有、不可全无。
                if "min_prompt_tokens" in band:
                    self.assertGreater(band["min_prompt_tokens"], 0, mid)
                    self.assertNotIn("start", band, mid)
                else:
                    self.assertLess(band["start"], band["end"], mid)

    def test_snapshot_keeps_the_sources_conditional_bands(self):
        # 这些模型在上游按条件计价。bands 若在传输中丢失，这些模型的估算会静默退回 base price，结果必错。
        for mid in ("hy4-preview-f", "hy3", "hy3-x", "gpt-6-astra",
                    "gpt-5.5", "grok-4.7"):
            entry = self.pricing["models"].get(mid)
            self.assertIsNotNone(entry, mid)
            self.assertIn("bands", entry, mid)

    def test_time_banded_model_picks_its_band(self):
        entry = self.pricing["models"]["hy4-preview-f"]
        bands = entry["bands"]
        factor = self.pricing["meta"]["usd_cny"]
        # 两档必须真的不同价，否则命中其中一档什么也证明不了。
        self.assertNotEqual(bands[0]["flat"]["input_cache_miss"],
                            bands[1]["flat"]["input_cache_miss"])
        row = {"model": "hy4-preview-f", "prompt_tokens": 1000000,
               "cached_tokens": 0, "completion_tokens": 0}
        # 02:00 UTC 落在第一档，17:00 UTC 落在第二档。
        row["at"] = utc_ts("2026-09-30 02:00:00")
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertEqual(got["band"], 0)
        self.assertAlmostEqual(
            got["cny"], bands[0]["flat"]["input_cache_miss"] * factor, places=6)
        row["at"] = utc_ts("2026-09-30 17:00:00")
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertEqual(got["band"], 1)
        self.assertAlmostEqual(
            got["cny"], bands[1]["flat"]["input_cache_miss"] * factor, places=6)

    def test_band_lookup_is_by_utc_window(self):
        entry = {
            "flat": {"input_cache_miss": 1.0},
            "bands": [
                {"days": ["sat", "sun"], "start": 0, "end": 2400,
                 "flat": {"input_cache_miss": 2.0}},
                {"days": None, "start": 100, "end": 400,
                 "flat": {"input_cache_miss": 3.0}},
            ],
        }
        # 周六 12:00 UTC：周末档，它不限定小时区间。
        table, index = wb_pricing.band_for(entry, utc_ts("2026-10-03 12:00:00"))
        self.assertEqual(index, 0)
        self.assertEqual(table["input_cache_miss"], 2.0)
        # 周三 02:00 UTC 落在 [01:00, 04:00) 内。
        self.assertEqual(
            wb_pricing.band_for(entry, utc_ts("2026-09-30 02:00:00"))[1], 1)
        # 周三 05:00 UTC 不命中任何档，用基准价。
        self.assertIsNone(
            wb_pricing.band_for(entry, utc_ts("2026-09-30 05:00:00"))[1])

    def test_length_bands_take_the_tightest_match(self):
        # 上游按阈值升序排列长度档，所以最后一个适用的档就是最紧的档。
        entry = {"flat": {"input_cache_miss": 1.0}, "bands": [
            {"min_prompt_tokens": 32000, "flat": {"input_cache_miss": 2.0}},
            {"min_prompt_tokens": 128000, "flat": {"input_cache_miss": 3.0}},
        ]}
        table, index = wb_pricing.band_for(entry, _AT, 40000)
        self.assertEqual(index, 0)
        self.assertEqual(table["input_cache_miss"], 2.0)
        table, index = wb_pricing.band_for(entry, _AT, 128000)
        self.assertEqual(index, 1)
        self.assertEqual(table["input_cache_miss"], 3.0)
        table, index = wb_pricing.band_for(entry, _AT, 200000)
        self.assertEqual(index, 1)
        self.assertEqual(table["input_cache_miss"], 3.0)
        # 低于最低阈值时用基准价。
        self.assertIsNone(wb_pricing.band_for(entry, _AT, 1000)[1])

    def test_length_banded_model_picks_its_band(self):
        entry = self.pricing["models"]["gpt-6-astra"]
        bands = entry["bands"]
        factor = self.pricing["meta"]["usd_cny"]
        threshold = bands[0]["min_prompt_tokens"]
        base = {"model": "gpt-6-astra", "at": _AT, "cached_tokens": 0,
                "completion_tokens": 0}
        # 刚好低于阈值时用模型自己的基准价。
        below = threshold - 1
        got = wb_pricing.compute_row(dict(base, prompt_tokens=below),
                                     pricing=self.pricing)
        self.assertIsNone(got["band"])
        self.assertAlmostEqual(
            got["cny"],
            below * entry["flat"]["input_cache_miss"] / 1000000.0 * factor,
            places=6)
        # 达到阈值后较贵的档接管。
        got = wb_pricing.compute_row(dict(base, prompt_tokens=threshold),
                                     pricing=self.pricing)
        self.assertEqual(got["band"], 0)
        self.assertAlmostEqual(
            got["cny"],
            threshold * bands[0]["flat"]["input_cache_miss"] / 1000000.0 * factor,
            places=6)
        self.assertNotEqual(entry["flat"]["input_cache_miss"],
                            bands[0]["flat"]["input_cache_miss"])

    def test_band_end_zero_means_end_of_day(self):
        # 上游把最后一档的收尾边界写成 0。
        entry = {"flat": {}, "bands": [
            {"days": None, "start": 1600, "end": 0,
             "flat": {"input_cache_miss": 5.0}}]}
        table, index = wb_pricing.band_for(entry, utc_ts("2026-09-30 20:00:00"))
        self.assertEqual(index, 0)
        self.assertEqual(table["input_cache_miss"], 5.0)
        self.assertIsNone(
            wb_pricing.band_for(entry, utc_ts("2026-09-30 10:00:00"))[1])

    def test_flat_model_reports_no_band(self):
        got = wb_pricing.compute_row(
            {"model": "deepseek-v4.1-flash", "at": _AT,
             "prompt_tokens": 1000000}, pricing=self.pricing)
        self.assertTrue(got["known"])
        self.assertIsNone(got["band"])

    def test_cache_split_and_usd_conversion(self):
        rate = self.pricing["models"]["deepseek-v4.1-flash"]["flat"]
        factor = self.pricing["meta"]["usd_cny"]
        row = {"model": "deepseek-v4.1-flash", "at": _AT,
               "prompt_tokens": 1000000, "cached_tokens": 0,
               "completion_tokens": 0}
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertTrue(got["known"])
        # 100 万 cache-miss 输入按快照的 miss 单价折成人民币。
        self.assertAlmostEqual(
            got["cny"], rate["input_cache_miss"] * factor, places=6)
        # 其中 80 万命中缓存，按 hit 单价计。
        row["cached_tokens"] = 800000
        expected = (0.2 * rate["input_cache_miss"]
                    + 0.8 * rate["input_cache_hit"]) * factor
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertAlmostEqual(got["cny"], expected, places=6)
        # 输出 token 单独按 output 单价计。
        row["completion_tokens"] = 1000000
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertAlmostEqual(got["cny"], expected + rate["output"] * factor,
                               places=6)

    def test_unknown_model_is_reported_not_guessed(self):
        got = wb_pricing.compute_row(
            {"model": "no-such-model", "at": _AT,
             "prompt_tokens": 1000000}, pricing=self.pricing)
        self.assertFalse(got["known"])
        self.assertEqual(got["cny"], 0.0)
        total = wb_pricing.sum_rows([
            {"model": "no-such-model", "at": _AT, "prompt_tokens": 1},
            {"model": "deepseek-v4.1-flash", "at": _AT,
             "prompt_tokens": 1000000},
        ], pricing=self.pricing)
        self.assertEqual(total["missing"], {"no-such-model": 1})
        self.assertEqual(total["priced"], 1)
        rate = self.pricing["models"]["deepseek-v4.1-flash"]["flat"]
        self.assertAlmostEqual(
            total["cny"], rate["input_cache_miss"] * self.pricing["meta"]["usd_cny"],
            places=4)

    def test_reasoning_tokens_ride_inside_completion(self):
        # 前提：上游把 reasoning tokens 计在 completion_tokens 里。2026-10-02
        # 实测，一次思考请求返回 completion=242、reasoning=238，且
        # total_tokens = prompt + completion；1.5 万行日志里 reasoning 从未
        # 超过 completion，其中 154 行全是 reasoning。所以 output 单价只对
        # completion 计一次；再加 reasoning_tokens 会把同一批 token 计两次费，
        # 行上的 reasoning 数不得影响估算。
        rate = self.pricing["models"]["deepseek-v4.1-flash"]["flat"]
        factor = self.pricing["meta"]["usd_cny"]
        row = {"model": "deepseek-v4.1-flash", "at": _AT, "prompt_tokens": 0,
               "cached_tokens": 0, "completion_tokens": 1000}
        plain = wb_pricing.compute_row(dict(row), pricing=self.pricing)
        self.assertAlmostEqual(
            plain["cny"], 1000 * rate["output"] / 1000000.0 * factor, places=9)
        with_reasoning = wb_pricing.compute_row(
            dict(row, reasoning_tokens=900), pricing=self.pricing)
        self.assertAlmostEqual(with_reasoning["cny"], plain["cny"], places=12)

    def test_dirty_cache_counts_cannot_exceed_prompt(self):
        row = {"model": "deepseek-v4.1-flash", "at": _AT,
               "prompt_tokens": 1000, "cached_tokens": 5000,
               "completion_tokens": 0}
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        # 1000 全按 hit 单价计，多余的 cached 被截掉，不产生负的 miss token。
        rate = self.pricing["models"]["deepseek-v4.1-flash"]["flat"]
        self.assertAlmostEqual(
            got["cny"], 1000 * rate["input_cache_hit"] / 1000000
            * self.pricing["meta"]["usd_cny"], places=9)


class PricingMatcherTests(unittest.TestCase):
    """抓取器必须给目录里新增的模型自动定价，不需要改代码。

    这里只测纯辅助函数，不碰网络。
    """

    def test_auto_match_wants_an_exact_unique_name(self):
        by_norm = wb_pricing.index_openrouter({
            "vendor/some-model": {},
            "other/another": {},
        })
        self.assertEqual(wb_pricing.auto_match("some-model", by_norm), "vendor/some-model")
        self.assertEqual(wb_pricing.auto_match("Some.Model", by_norm), "vendor/some-model")
        # 两个厂商发布同名的叶子名，太含糊，不能猜。
        ambiguous = wb_pricing.index_openrouter({"a/dup": {}, "b/dup": {}})
        self.assertIsNone(wb_pricing.auto_match("dup", ambiguous))
        self.assertIsNone(wb_pricing.auto_match("no-such-model", by_norm))

    def test_index_drops_variant_suffixes(self):
        # :free / :batch 是各自计价的独立商品；保留它们会让同名的基础模型
        # 变得含糊。
        self.assertEqual(
            wb_pricing.index_openrouter({"vendor/m:free": {}, "vendor/m:batch": {}}), {})

    def test_resolve_prefers_the_override_table(self):
        or_models = {"tencent/hy4-preview": {"prompt": "0.000001"}}
        by_norm = wb_pricing.index_openrouter(or_models)
        self.assertEqual(wb_pricing.resolve("hy4-preview-f", or_models, by_norm),
                         ("tencent/hy4-preview", True))
        # 映射表里有、抓取清单里没有的条目落到“未定价”，不会指向不存在的条目。
        self.assertEqual(wb_pricing.resolve("hy4-preview-f", {}, {}), (None, False))

    def test_overrides_point_at_openrouter_shaped_ids(self):
        # 映射表里的拼写错误会让模型静默变成未定价。
        for hub_id, ref in wb_pricing.OVERRIDES.items():
            self.assertIn("/", ref, hub_id)
            self.assertNotIn(":", ref, hub_id)

    def test_bands_cover_both_kinds_of_condition(self):
        bands = wb_pricing.bands_from_overrides({"overrides": [
            {"min_prompt_tokens": 200000, "prompt": "0.000006",
             "completion": "0.0000225"},
            {"utc_start": 0, "utc_end": 1600, "prompt": "0.000000834",
             "completion": "0.000002501", "input_cache_read": "0.000000042"},
            {"utc_days": ["monday", "friday"], "utc_start": 1600, "utc_end": 0,
             "prompt": "0.0000007506", "completion": "0.0000022509",
             "input_cache_read": "0.0000000378"},
        ]})
        self.assertEqual(len(bands), 3)
        # 长度档只带阈值，不带时段。
        self.assertEqual(bands[0]["min_prompt_tokens"], 200000)
        self.assertNotIn("start", bands[0])
        self.assertEqual(bands[0]["flat"]["input_cache_miss"], 6.0)
        # 时段档只带时段，不带阈值。
        self.assertNotIn("min_prompt_tokens", bands[1])
        self.assertIsNone(bands[1]["days"])
        self.assertEqual((bands[1]["start"], bands[1]["end"]), (0, 1600))
        self.assertEqual(bands[1]["flat"]["input_cache_miss"], 0.834)
        self.assertEqual(bands[1]["flat"]["input_cache_hit"], 0.042)
        # 收尾边界 0 表示一天结束，星期名存成引擎比较用的缩写。
        self.assertEqual(bands[2]["days"], ["mon", "fri"])
        self.assertEqual((bands[2]["start"], bands[2]["end"]), (1600, 2400))

    def test_bands_need_a_condition(self):
        self.assertEqual(wb_pricing.bands_from_overrides({}), [])
        # 不带条件的条目只是重复基准价，不算档。
        self.assertEqual(wb_pricing.bands_from_overrides({"overrides": [
            {"prompt": "0.00001", "completion": "0.00005"}]}), [])
        # 本地 usage 日志里没有 audio/cache-write 的 token 计数，只按它们计价
        # 的条目也不算档。
        self.assertEqual(wb_pricing.bands_from_overrides({"overrides": [
            {"audio": "0.00002", "input_audio_cache": "0.000004"}]}), [])

    def test_snapshot_reflects_the_matchers_choice(self):
        # hy4-preview-f 在上游只有另一个名字，必须通过映射表进入快照。
        entry = self._snapshot_entry("hy4-preview-f")
        self.assertEqual(entry["or_id"], wb_pricing.OVERRIDES["hy4-preview-f"])
        self.assertEqual(entry["source"], "openrouter")
        # 目录里有、OpenRouter 同名的模型自动匹配，背后没有映射。
        self.assertNotIn("kimi-k2.6", wb_pricing.OVERRIDES)
        auto = self._snapshot_entry("kimi-k2.6")
        self.assertEqual(auto["or_id"], "moonshotai/kimi-k2.6")

    def test_catalogue_ids_are_unique(self):
        ids = wb_pricing.hub_model_ids()
        self.assertIn("hy4-preview-f", ids)
        self.assertEqual(len(ids), len(set(ids)))

    def _snapshot_entry(self, mid):
        entry = wb_pricing.load_pricing()["models"].get(mid)
        self.assertIsNotNone(entry, "snapshot has no entry for %s" % mid)
        return entry


class PricingPolicyTests(unittest.TestCase):
    """价格按模型存储、去重，行通过引用指向策略。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-price-policy-")
        self._previous = wb_pricing.data_dir()
        wb_pricing.set_data_dir(self.dir)
        # 用真实、较新的时间戳：刷新器用墙钟打时间，编造的纪元时刻看起来像
        # 遥远的过去。
        self.now = time.time()

    def tearDown(self):
        wb_pricing.set_data_dir(self._previous)

    def _record(self, models, at):
        """像刷新器那样登记一份抓取到的快照。"""
        doc = {"meta": {"base_currency": "CNY", "usd_cny": 7.1}, "models": models}
        return wb_pricing.record_policies(doc, at=at)

    @staticmethod
    def _entry(rate, or_id="vendor/m"):
        return {"display": "m", "source": "openrouter", "currency": "USD",
                "unit": 1000000, "or_id": or_id,
                "flat": {"input_cache_hit": rate, "input_cache_miss": rate,
                         "output": rate}}

    def test_a_price_seen_twice_is_stored_once(self):
        # A -> B -> A：三次抓取、两条策略，第三条指回第一条，不写副本。
        _n, moved, first = self._record({"m": self._entry(1.0)}, self.now - 7200)
        _n, _c, second = self._record({"m": self._entry(2.0)}, self.now - 3600)
        _n, back, third = self._record({"m": self._entry(1.0)}, self.now - 60)
        self.assertTrue(moved and back)
        self.assertEqual(len(wb_pricing.load_policies()), 2)
        self.assertEqual(first["m"], third["m"])
        self.assertNotEqual(first["m"], second["m"])
        # 切回去也算一次真实变化，三步都留在时间线上；共享的是策略本身，
        # 历史各记各的。
        self.assertEqual(len(wb_pricing.load_timeline()), 3)

    def test_an_unchanged_price_does_not_grow_the_timeline(self):
        self._record({"m": self._entry(1.0)}, self.now - 3600)
        added, changed, _a = self._record({"m": self._entry(1.0)}, self.now - 60)
        self.assertEqual(added, 0)
        self.assertFalse(changed)
        self.assertEqual(len(wb_pricing.load_timeline()), 1)

    def test_the_current_assignment_says_what_is_in_force(self):
        self._record({"m": self._entry(1.0)}, self.now - 3600)
        _n, _c, second = self._record({"m": self._entry(2.0)}, self.now - 60)
        at, assignment = wb_pricing.current_assignment()
        self.assertEqual(assignment["m"], second["m"])
        self.assertEqual(wb_pricing.current_policy_id("m"), second["m"])
        self.assertAlmostEqual(at, self.now - 60, places=3)

    def test_a_row_is_priced_by_the_policy_it_references(self):
        self._record({"m": self._entry(1.0)}, self.now - 7200)
        _n, _c, second = self._record({"m": self._entry(2.0)}, self.now - 3600)
        older = [p for p in wb_pricing.load_policies().values()
                 if p["flat"]["input_cache_miss"] == 1.0][0]
        # 带旧引用的行保留它当时的价，即便新策略已生效——引用的意义就在这里。
        got = wb_pricing.cost_for_row(
            {"model": "m", "at": self.now - 60, "prompt_tokens": 1000000,
             "cost_policy": older["id"]})
        self.assertAlmostEqual(got["cny"], 1.0 * 7.1, places=6)
        self.assertEqual(got["source"], older["id"])
        self.assertFalse(got["backfilled"])
        # 没有引用的行按时间线解析。
        got = wb_pricing.cost_for_row(
            {"model": "m", "at": self.now - 60, "prompt_tokens": 1000000})
        self.assertAlmostEqual(got["cny"], 2.0 * 7.1, places=6)
        self.assertEqual(got["source"], second["m"])

    def test_a_row_from_before_a_policy_existed_is_backfilled(self):
        # 该时刻 "m" 还没有价，用它第一条价顶上，行上打标记供面板说明。
        self._record({"other": self._entry(9.0, "vendor/o")}, self.now - 7200)
        self._record({"m": self._entry(5.0)}, self.now - 3600)
        got = wb_pricing.cost_for_row(
            {"model": "m", "at": self.now - 5400, "prompt_tokens": 1000000})
        self.assertTrue(got["backfilled"])
        self.assertAlmostEqual(got["cny"], 5.0 * 7.1, places=6)

    def test_no_policies_at_all_falls_back_to_the_factory_snapshot(self):
        got = wb_pricing.cost_for_row(
            {"model": "deepseek-v4.1-flash", "at": self.now,
             "prompt_tokens": 1000000})
        self.assertTrue(got["known"])
        self.assertEqual(got["source"], "builtin")
        self.assertIsNone(got["source_at"])

    def test_prune_spares_the_live_policy(self):
        # "m" 有两条策略，都没有行引用；生效的那条留下。
        self._record({"m": self._entry(1.0)}, self.now - 7200)
        _n, _c, live = self._record({"m": self._entry(2.0)}, self.now - 3600)
        removed = wb_pricing.prune_policies()
        self.assertEqual(len(removed), 1)
        self.assertIn(live["m"], wb_pricing.load_policies())

    def test_prune_never_leaves_a_model_without_a_policy(self):
        # "gone" 已不在生效指派里，也没有行引用，但它是该模型唯一的策略，
        # 必须保留。
        _n, _c, dropped = self._record({"gone": self._entry(3.0)}, self.now - 7200)
        self._record({"m": self._entry(1.0)}, self.now - 3600)
        self.assertEqual(wb_pricing.prune_policies(), [])
        self.assertIn(dropped["gone"], wb_pricing.load_policies())

    def test_prune_keeps_a_referenced_policy(self):
        _n, _c, first = self._record({"m": self._entry(1.0)}, self.now - 7200)
        self._record({"m": self._entry(2.0)}, self.now - 3600)
        with open(wb_pricing.usage_log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"model": "m", "cost_policy": first["m"]}) + "\n")
        self.assertEqual(wb_pricing.prune_policies(), [])
        self.assertIn(first["m"], wb_pricing.load_policies())

    def test_prune_spares_the_batch_just_fetched(self):
        # 最新一批被显式保护，抓取刚产出的策略不会被紧跟的清扫删掉。
        _n, _c, older = self._record({"m": self._entry(1.0)}, self.now - 7200)
        _n, _c, newer = self._record({"m": self._entry(2.0)}, self.now - 3600)
        removed = wb_pricing.prune_policies(protect={older["m"]})
        self.assertEqual(removed, [])
        policies = wb_pricing.load_policies()
        self.assertIn(older["m"], policies)
        self.assertIn(newer["m"], policies)


class PricingIntegrationTests(unittest.TestCase):
    """用量读取接口在它已有的行上挂出费用。"""

    # 这里用只有一个基准价的模型，重点是接线；档查找由引擎测试在带档的模型上覆盖。
    MODEL = "deepseek-v4.1-flash"

    def setUp(self):
        with U._daily_usage_lock:
            U._daily_usage.update({"day": "", "totals": None, "credits": None,
                                   "models": None, "offset": 0, "at": 0.0})
        self.usd_rate = wb_pricing.load_pricing()["models"][self.MODEL]["flat"][
            "input_cache_miss"]
        row = {"at": time.time() - 120, "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "model": self.MODEL, "stream": True, "outcome": "completed",
               "elapsed_ms": 1200, "ttft_ms": 300, "gen_ms": 900,
               "prompt_tokens": 1000000, "completion_tokens": 0,
               "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 1000000,
               "credit": 1.5, "account": "uid-cost", "realm": "intl"}
        with open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.write(json.dumps(dict(row, model="no-such-model", credit=0),
                                ensure_ascii=False) + "\n")

    def test_recent_rows_carry_the_estimate_and_the_rate(self):
        page = R.recent_usage(limit=10)
        self.assertGreater(page["usd_cny"], 1.0)
        by_model = {r["model"]: r for r in page["rows"]}
        expected = self.usd_rate * page["usd_cny"]
        self.assertAlmostEqual(by_model[self.MODEL]["cost_cny"], expected,
                               places=4)
        # 只有一个基准价意味着没有档，字段仍然在。
        self.assertIsNone(by_model[self.MODEL]["cost_band"])
        # 这个沙箱没有任何策略，行按出厂快照计价并如实标注。
        self.assertEqual(by_model[self.MODEL]["cost_source"], "builtin")
        self.assertIsNone(by_model[self.MODEL]["cost_source_at"])
        self.assertFalse(by_model[self.MODEL]["cost_backfilled"])
        # 未定价模型的回答保持 None，不编造 0。
        self.assertIsNone(by_model["no-such-model"]["cost_cny"])

    def test_snapshot_totals_include_cost_and_missing(self):
        snap = U.usage_snapshot(ttl=0, range="all")
        self.assertAlmostEqual(snap["cost_cny"], self.usd_rate * snap["usd_cny"],
                               places=3)
        self.assertEqual(snap["cost_missing"], {"no-such-model": 1})
        per = snap["by_model"][self.MODEL]
        self.assertAlmostEqual(per["cost_cny"], self.usd_rate * snap["usd_cny"],
                               places=3)


if __name__ == "__main__":
    unittest.main()
