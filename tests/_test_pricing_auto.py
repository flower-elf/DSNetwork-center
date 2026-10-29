"""测上游新增模型自动定价的契约：抓取输入、按需定价、渠道后缀继承与未定价报告。用 python tests/run_all.py _test_pricing_auto 运行，不联网。
"""

# 这套契约有四块：
#   - 抓取输入是内置目录与网关实时目录的并集，按 /v1/models 的方式过滤，
#     别名和付费变体不进计价输入；
#   - 两次抓取之间被调用的模型按需定价，用上次抓取留在内存里的索引，请求
#     路径不出网，幂等且不重写已存内容；
#   - 带渠道后缀的名字（-lkeap）从基础名继承，仅当基础名唯一匹配
#     OpenRouter，策略记录继承来源；设置可以关掉整条规则；
#   - 面板读的未定价报告：每个模型的原因，以及数据目录里人工录入的映射。
# 不联网：OpenRouter 是夹具，实时目录是替身。真实目录由部署检查另外覆盖。
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-price-auto-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.environ["WB_PROXY_CATALOG_DIR"] = os.path.join(_TMP, "catalog")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_pricing
import wb_proxy as P
import wb_models as M
import wb_settings


def rate(prompt, completion, cache=None):
    """一条 OpenRouter 条目，按 token 的 USD 字符串，与 API 下发的形态一致。"""
    return {"prompt": str(prompt), "completion": str(completion),
            "input_cache_read": str(prompt if cache is None else cache)}


# 实时目录的替身：内置目录里变体名需要的基础模型，外加一个内置目录没见过的
# 名字。
OR = {
    "deepseek/deepseek-v4.1-flash": rate(0.00000003, 0.0000005, 0.00000001),
    "deepseek/deepseek-r1-0528": rate(0.0000005, 0.00000215, 0.00000035),
    "deepseek/deepseek-chat-v3.1": rate(0.00000025, 0.00000095, 0.00000013),
    "deepseek/deepseek-chat-v3-0324": rate(0.00000029, 0.00000114, 0.00000011),
    "z-ai/glm-5.3-flash": rate(0.00000015, 0.0000005, 0.00000003),
    "z-ai/glm-5.3-flashx": rate(0.0000002, 0.0000007, 0.00000004),
    "moonshotai/kimi-k2.6": rate(0.00000043415, 0.000001828, 0.00000007312),
}

# 内置目录里的五个虚拟别名：不是模型，从不定价，也不算覆盖缺口。
ALIASES = ("default-model", "fast-model", "balanced-model", "primary-model",
           "deep-model")


class PricingTestCase(unittest.TestCase):
    """独立的数据目录，加上每个用例都可能弄脏的模块级状态。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-price-auto-case-")
        self.accounts = os.path.join(self.dir, "accounts")
        os.makedirs(self.accounts, exist_ok=True)
        self._previous_dir = wb_pricing.data_dir()
        self._previous_settings = wb_pricing._settings_dir_override
        wb_pricing.set_data_dir(self.dir)
        wb_pricing.set_settings_dir(self.accounts)
        self._fetch = wb_pricing.fetch_openrouter
        self._live = wb_pricing._live_index["models"]
        wb_pricing.fetch_openrouter = lambda: dict(OR)

    def tearDown(self):
        wb_pricing.fetch_openrouter = self._fetch
        wb_pricing.set_settings_dir(self._previous_settings)
        wb_pricing.set_data_dir(self._previous_dir)
        wb_pricing.remember_live_index(None)
        if self._live is not None:
            wb_pricing.remember_live_index(self._live)

    def _lines(self, name):
        path = os.path.join(self.dir, name)
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [line for line in fh.read().splitlines() if line.strip()]

    def _refresher(self, interval=5):
        refresher = wb_pricing.PriceRefresher(interval)
        refresher.log = lambda msg: None
        return refresher


class UnionInputTests(PricingTestCase):
    """抓取输入是内置目录加上实时新增。"""

    def test_the_static_baseline_is_unchanged(self):
        ids = wb_pricing.hub_model_ids()
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("hy4-preview-f", ids)
        # 只有实时目录有的名字不得混进基线，这正是 priced_model_ids()
        # 单独存在的原因。
        self.assertNotIn("glm-5.3-flashx", ids)

    def test_extra_ids_are_appended_in_order_without_duplicates(self):
        static = wb_pricing.hub_model_ids()
        ids = wb_pricing.priced_model_ids(
            ["glm-5.3-flashx", "deepseek-v4.1-flash", "glm-5.3-flashx", "  "])
        self.assertEqual(ids[:len(static)], static)
        self.assertEqual(ids[len(static):], ["glm-5.3-flashx"])

    def test_the_snapshot_covers_a_live_only_model(self):
        doc, unpriced, overridden = wb_pricing.build_snapshot(
            OR, None, ["glm-5.3-flashx"])
        self.assertIn("glm-5.3-flashx", doc["models"])
        self.assertEqual(doc["models"]["glm-5.3-flashx"]["or_id"],
                         "z-ai/glm-5.3-flashx")
        self.assertNotIn("glm-5.3-flashx", unpriced)
        # 它不是人工录入的，所以不算 override。
        self.assertNotIn("glm-5.3-flashx", overridden)

    def test_a_failed_snapshot_fetch_keeps_previous_prices(self):
        doc, unpriced, _ov = wb_pricing.build_snapshot(
            OR, None, ["glm-5.3-flashx"])
        previous = doc["models"]
        again, unpriced2, _ov2 = wb_pricing.build_snapshot(
            None, previous, ["glm-5.3-flashx"])
        self.assertEqual(again["models"]["glm-5.3-flashx"],
                         previous["glm-5.3-flashx"])
        # 有价的 id 全部保留，只报告从没定过价的那些。
        self.assertNotIn("glm-5.3-flashx", unpriced2)
        self.assertEqual(sorted(unpriced2), sorted(unpriced))

    def test_run_once_prices_a_live_only_model(self):
        original = M.read_cached_remote_catalog
        M.read_cached_remote_catalog = lambda realm: (["glm-5.3-flashx"], {})
        try:
            refresher = self._refresher()
            ok, message = refresher.run_once()
        finally:
            M.read_cached_remote_catalog = original
        self.assertTrue(ok, message)
        self.assertEqual(refresher.last_extra, 1)
        _at, assignment = wb_pricing.current_assignment()
        self.assertIn("glm-5.3-flashx", assignment)
        self.assertEqual(refresher.last_models, len(assignment))

    def test_a_live_read_failure_keeps_the_static_coverage(self):
        def boom(realm):
            raise RuntimeError("live catalogue unavailable")

        original = M.curated_live_sources
        M.curated_live_sources = boom
        try:
            refresher = self._refresher()
            ok, message = refresher.run_once()
        finally:
            M.curated_live_sources = original
        self.assertTrue(ok, message)
        self.assertEqual(refresher.last_extra, 0)
        # 覆盖范围恰好是静态基线，不少于它。
        static_doc, _unpriced, _ov = wb_pricing.build_snapshot(OR)
        self.assertEqual(refresher.last_models, len(static_doc["models"]))
        self.assertGreater(refresher.last_models, 3)

    def test_live_ids_use_the_same_filter_as_the_model_list(self):
        original = M.read_cached_remote_catalog
        M.read_cached_remote_catalog = lambda realm: (
            ["default-model", "auto", "lite", "codewise-inline",
             "glm-5.3-flashx", "hy4-preview-sg", "glm-5.3-flash"], {})
        try:
            ids = self._refresher().live_ids()
        finally:
            M.read_cached_remote_catalog = original
        # 新名字进来；
        self.assertIn("glm-5.3-flashx", ids)
        # 虚拟别名、非聊天辅助项和付费的 "-sg" 构建被网关自己的甄选挡掉，
        # 内置目录已有的名字不会重复。
        for dropped in ("default-model", "auto", "lite", "codewise-inline",
                        "hy4-preview-sg", "glm-5.3-flash"):
            self.assertNotIn(dropped, ids, dropped)

    def test_live_ids_ask_both_realms_and_dedupe(self):
        asked = []
        original = M.curated_live_sources

        def fake(realm):
            asked.append(realm)
            if realm == "cn":
                return [("glm-5.3-flashx", {}), ("kimi-k2.6", {})], True, "remote"
            return [("glm-5.3-flashx", {}), ("glm-5.3-flash", {})], True, "remote"

        M.curated_live_sources = fake
        try:
            ids = self._refresher().live_ids()
        finally:
            M.curated_live_sources = original
        # 两个出口都问一遍：远程目录按出口抓取，任一出口的新增都属于计价输入。
        self.assertEqual(asked, ["intl", "cn"])
        self.assertEqual(ids, ["glm-5.3-flashx"])

    def test_one_failing_realm_does_not_hide_the_other(self):
        original = M.curated_live_sources

        def fake(realm):
            if realm == "cn":
                raise RuntimeError("cn catalogue down")
            return [("glm-5.3-flashx", {})], True, "remote"

        M.curated_live_sources = fake
        try:
            refresher = wb_pricing.PriceRefresher(5)
            ids = refresher.live_ids()
        finally:
            M.curated_live_sources = original
        self.assertEqual(ids, ["glm-5.3-flashx"])
        self.assertTrue(any("cn" in line for line in refresher.logs), refresher.logs)

    def test_an_extra_id_never_pollutes_the_alias_rows(self):
        # 实时目录若仍然报出别名，不得给它定价，也不得记成 override。
        doc, unpriced, overridden = wb_pricing.build_snapshot(
            OR, None, list(ALIASES))
        for alias in ALIASES:
            self.assertNotIn(alias, doc["models"])
            self.assertIn(alias, unpriced)  # 别名始终留在未定价列表里
        for alias in ALIASES:
            self.assertNotIn(alias, overridden)


# 在全新解释器里跑：搭一个顶替 __main__ 的网关，再 import wb_pricing，看
# sys.modules 里留下了什么。__ROOT__ 是 wb_pricing 自身被导入的目录，所以
# 测试旁边的改过的副本才是探针加载的那份。
_TWIN_IMPORT_PROBE = '''\
import sys
sys.path.insert(0, __ROOT__)


class FakeGateway(object):
    """Stands in for the running `python wb_proxy.py` instance."""

    VIRTUAL_ALIAS_MODELS = ("default-model", "auto")

    def curated_live_sources(self, realm):
        return ([], True, None)

    def add_log_entry(self, message, tag=None):
        pass


sys.modules["__main__"] = FakeGateway()

import wb_pricing  # noqa: E402

problems = []
if wb_pricing.gateway_module() is not sys.modules["__main__"]:
    problems.append("gateway_module() did not resolve to the running __main__, "
                    "so this probe is not measuring what it thinks it is")
if "wb_proxy" in sys.modules:
    problems.append("importing wb_pricing executed wb_proxy a second time")
if problems:
    print("\\n".join(problems))
    raise SystemExit(1)
'''


class SingleGatewayInstanceTests(PricingTestCase):
    """计价必须与服务器真正运行的网关模块对话。

    容器以 python wb_proxy.py 启动，运行中的实例是 __main__。进程里再
    import wb_proxy 会得到一个没有账号池的副本：实时目录静默读成空，日志
    缓冲也是自己的，计价行到不了面板。线上出过这两种症状：刷新报无实时
    新增而 /v1/models 仍列出它们，定价标签的整页日志是空的。这里用
    __main__ 顶替服务器，wb_proxy 在旁边按普通方式导入。
    """

    @staticmethod
    def _fake_gateway():
        class FakeGateway(object):
            VIRTUAL_ALIAS_MODELS = ("default-model", "auto")

            def __init__(self):
                self.realms = []
                self.lines = []

            def curated_live_sources(self, realm):
                self.realms.append(realm)
                return ([("gpt-6-sol", {})], True, "remote")

            def add_log_entry(self, message, tag=None):
                self.lines.append((message, tag))

        return FakeGateway()

    def test_the_live_catalogue_is_read_from_the_catalog_module(self):
        seen = []

        def fake_sources(realm):
            seen.append(realm)
            return ([("gpt-6-sol", {})], True, "remote")

        with unittest.mock.patch.object(M, "curated_live_sources", fake_sources):
            ids = self._refresher().live_ids()
        self.assertEqual(seen, ["intl", "cn"])
        self.assertEqual(ids, ["gpt-6-sol"])

    def test_the_pricing_lines_reach_the_running_gateway(self):
        fake = self._fake_gateway()
        refresher = wb_pricing.PriceRefresher(5)
        with unittest.mock.patch.dict(sys.modules, {"__main__": fake}):
            refresher.log("已取价：1 个模型")
            wb_pricing._log_pricing("按需补价：gpt-6-sol")
        self.assertEqual(fake.lines, [("[定价] 已取价：1 个模型", "pricing"),
                                      ("[定价] 按需补价：gpt-6-sol", "pricing")])

    def test_the_alias_set_comes_from_the_catalog_module(self):
        with unittest.mock.patch.object(M, "VIRTUAL_ALIAS_MODELS",
                                        {"default-model", "auto"}):
            self.assertEqual(wb_pricing.virtual_alias_names(),
                             {"default-model", "auto"})

    def test_a_stale_second_copy_is_never_preferred(self):
        running, twin = self._fake_gateway(), self._fake_gateway()
        with unittest.mock.patch.dict(sys.modules, {"__main__": running,
                                                    "wb_proxy": twin}):
            self.assertIs(wb_pricing.gateway_module(), running)

    def test_a_plain_process_uses_the_imported_wb_proxy_module(self):
        with unittest.mock.patch.dict(
                sys.modules, {"__main__": types.ModuleType("__main__")}):
            self.assertIs(wb_pricing.gateway_module(), P)

    def test_nothing_imports_wb_proxy_behind_the_resolver(self):
        """导入 wb_pricing 不得顺带引入第二个 wb_proxy。

        网关以 python wb_proxy.py 运行，运行中的实例是 __main__；wb_pricing
        导入期间执行的任何 import wb_proxy 都会把那份文件再跑一遍，造出没有
        账号池、带自己日志缓冲的副本。文本扫描只能说明那行字在文件里，说明
        不了它什么时候跑：把 gateway_module() 的惰性回退挪到模块级，那行字
        节不变；from wb_proxy import Handler 根本不是那段文本——两种改法都会
        造出副本而扫描照样绿，引用了那行字的 docstring 还会误报。让真正的
        解释器报告它实际导入了什么，这些问题都没有。
        """
        probe = _TWIN_IMPORT_PROBE.replace(
            "__ROOT__",
            repr(os.path.dirname(os.path.abspath(wb_pricing.__file__))))
        result = subprocess.run([sys.executable, "-c", probe],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(
            result.returncode, 0,
            "importing wb_pricing pulled in a second wb_proxy:\n"
            + (result.stdout or "") + (result.stderr or ""))


class OnDemandPricingTests(PricingTestCase):
    """两次抓取之间被调用的模型，在第一行上就能拿到价。"""

    def setUp(self):
        super().setUp()
        wb_pricing.remember_live_index(OR)

    def test_a_hit_registers_a_policy_and_returns_its_id(self):
        pid = wb_pricing.ensure_policy("glm-5.3-flashx")
        self.assertIsNotNone(pid)
        policy = wb_pricing.load_policies()[pid]
        self.assertEqual(policy["model"], "glm-5.3-flashx")
        self.assertEqual(policy["or_id"], "z-ai/glm-5.3-flashx")
        self.assertEqual(policy["via"], "direct")
        self.assertEqual(wb_pricing.current_policy_id("glm-5.3-flashx"), pid)

    def test_the_write_path_prices_the_first_row_of_a_live_only_model(self):
        # wb_proxy 在写入时给每行盖 current_policy_id(model)；这个调用必须
        # 当场凭内存里的索引铸出价格。
        pid = wb_pricing.current_policy_id("glm-5.3-flashx")
        self.assertIsNotNone(pid)
        row = {"model": "glm-5.3-flashx", "at": time.time(),
               "cost_policy": pid, "prompt_tokens": 1000000,
               "completion_tokens": 0, "cached_tokens": 0}
        cost = wb_pricing.cost_for_row(row)
        self.assertTrue(cost["known"])
        self.assertAlmostEqual(cost["cny"], 0.2 * wb_pricing.USD_CNY, places=6)
        self.assertEqual(cost["source"], pid)

    def test_the_registered_price_is_what_the_row_is_stamped_with(self):
        pid = wb_pricing.ensure_policy("glm-5.3-flashx")
        row = {"model": "glm-5.3-flashx", "at": time.time(),
               "prompt_tokens": 1000000, "completion_tokens": 0,
               "cached_tokens": 0}
        cost = wb_pricing.cost_for_row(row)
        self.assertTrue(cost["known"])
        self.assertAlmostEqual(cost["cny"], 0.2 * wb_pricing.USD_CNY, places=6)
        self.assertEqual(cost["source"], pid)

    def test_a_miss_returns_none_and_writes_nothing(self):
        self.assertIsNone(wb_pricing.ensure_policy("kimi-k2.8-preview"))
        self.assertIsNone(wb_pricing.ensure_policy("no-such-model"))
        self.assertEqual(self._lines("pricing-policies.jsonl"), [])
        self.assertEqual(self._lines("pricing-timeline.jsonl"), [])

    def test_a_cold_index_degrades_to_none(self):
        wb_pricing.remember_live_index(None)
        wb_pricing._live_index.update({"models": None, "by_norm": None, "at": None})
        self.assertIsNone(wb_pricing.ensure_policy("glm-5.3-flashx"))
        self.assertEqual(self._lines("pricing-policies.jsonl"), [])

    def test_the_second_call_writes_nothing_again(self):
        first = wb_pricing.ensure_policy("deepseek-r1-0528-lkeap")
        second = wb_pricing.ensure_policy("deepseek-r1-0528-lkeap")
        self.assertEqual(first, second)
        self.assertEqual(len(self._lines("pricing-policies.jsonl")), 1)
        self.assertEqual(len(self._lines("pricing-timeline.jsonl")), 1)

    def test_a_variant_hit_records_where_the_price_came_from(self):
        pid = wb_pricing.ensure_policy("deepseek-r1-0528-lkeap")
        policy = wb_pricing.load_policies()[pid]
        self.assertEqual(policy["via"], "variant")
        self.assertEqual(policy["inherited_from"], "deepseek-r1-0528")
        self.assertEqual(policy["or_id"], "deepseek/deepseek-r1-0528")

    def test_an_inherited_price_still_prices_the_row(self):
        wb_pricing.ensure_policy("deepseek-v3-1-lkeap")
        row = {"model": "deepseek-v3-1-lkeap", "at": time.time(),
               "prompt_tokens": 1000000, "completion_tokens": 0,
               "cached_tokens": 0}
        cost = wb_pricing.cost_for_row(row)
        self.assertTrue(cost["known"])
        self.assertAlmostEqual(cost["cny"], 0.25 * wb_pricing.USD_CNY, places=6)

    def test_concurrent_first_requests_register_once(self):
        results = []
        lock = threading.Lock()

        def work():
            pid = wb_pricing.ensure_policy("glm-5.3-flashx")
            with lock:
                results.append(pid)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(pid == results[0] for pid in results), results)
        # 一行策略、一行时间线，每行都是合法 JSON，没有交错的半截写入。
        for name in ("pricing-policies.jsonl", "pricing-timeline.jsonl"):
            lines = self._lines(name)
            self.assertEqual(len(lines), 1, name)
            json.loads(lines[0])

    def test_an_on_demand_row_keeps_the_other_models_in_force(self):
        # 单个模型的登记不得缩小时间线对生效范围的视图，它并入上一次指派。
        wb_pricing.record_policies(wb_pricing.build_snapshot(OR)[0])
        at, before = wb_pricing.current_assignment()
        self.assertGreater(len(before), 3)
        wb_pricing.ensure_policy("glm-5.3-flashx")
        _at, after = wb_pricing.current_assignment()
        for model in before:
            self.assertEqual(after.get(model), before[model], model)
        self.assertIn("glm-5.3-flashx", after)


class VariantInheritanceTests(PricingTestCase):
    """后缀规则：只从唯一的基础名继承，且仅在开关打开时。"""

    def setUp(self):
        super().setUp()
        self.by_norm = wb_pricing.index_openrouter(OR)

    def test_the_three_suffix_names_inherit_from_their_bases(self):
        for mid, ref in (("deepseek-r1-0528-lkeap", "deepseek/deepseek-r1-0528"),
                         ("deepseek-v3-1-lkeap", "deepseek/deepseek-chat-v3.1"),
                         ("deepseek-v3-0324-lkeap",
                          "deepseek/deepseek-chat-v3-0324")):
            self.assertEqual(
                wb_pricing.resolve(mid, OR, self.by_norm, variants=True)[0],
                ref, mid)

    def test_the_two_names_without_a_counterpart_stay_unpriced(self):
        # kimi-k2-instruct-taiji 剥掉后缀后是 OpenRouter 不发布的基础名，
        # kimi-k2.8-preview 没有可剥的后缀，两者都不能猜。
        for mid in ("kimi-k2-instruct-taiji", "kimi-k2.8-preview"):
            self.assertIsNone(
                wb_pricing.resolve(mid, OR, self.by_norm, variants=True)[0], mid)

    def test_a_base_that_is_not_unique_is_not_inherited(self):
        two_vendors = dict(OR)
        two_vendors["othervendor/deepseek-r1-0528"] = rate(0.1, 0.1)
        by_norm = wb_pricing.index_openrouter(two_vendors)
        self.assertIsNone(wb_pricing.resolve("deepseek-r1-0528-lkeap",
                                            two_vendors, by_norm,
                                            variants=True)[0])

    def test_a_base_missing_from_the_list_is_not_inherited(self):
        self.assertIsNone(wb_pricing.resolve(
            "glm-5.3-flash-f", OR, self.by_norm, variants=True)[0])

    def test_the_uncertain_suffixes_are_not_peeled(self):
        # -f / -dev / -x 从不剥：hub 用它们区分自己的档位，剥掉会静默丢掉
        # 档位差价。
        for mid in ("hy3-x", "hy4-preview-f", "hy4-preview-dev"):
            self.assertEqual(list(wb_pricing.variant_bases(mid)), [], mid)
        self.assertEqual(list(wb_pricing.variant_bases("deepseek-r1-0528-lkeap")),
                         ["deepseek-r1-0528"])

    def test_the_switch_off_restores_the_old_behaviour(self):
        self.assertIsNone(wb_pricing.resolve("deepseek-r1-0528-lkeap", OR,
                                            self.by_norm, variants=False)[0])
        doc, unpriced, _ov = wb_pricing.build_snapshot(
            OR, None, None, variants=False)
        self.assertNotIn("deepseek-r1-0528-lkeap", doc["models"])
        self.assertIn("deepseek-r1-0528-lkeap", unpriced)
        doc_on, unpriced_on, _ov = wb_pricing.build_snapshot(
            OR, None, None, variants=True)
        self.assertIn("deepseek-r1-0528-lkeap", doc_on["models"])
        self.assertNotIn("deepseek-r1-0528-lkeap", unpriced_on)

    def test_the_setting_defaults_to_on_and_is_persisted(self):
        self.assertTrue(wb_settings.pricing_variant_inherit(self.accounts))
        self.assertTrue(wb_pricing.variant_inherit_enabled(self.accounts))
        self.assertFalse(wb_settings.set_pricing_variant_inherit(
            self.accounts, False))
        self.assertFalse(wb_settings.pricing_variant_inherit(self.accounts))
        self.assertFalse(wb_pricing.variant_inherit_enabled(self.accounts))
        with open(os.path.join(self.accounts, "settings.json"),
                  encoding="utf-8") as fh:
            self.assertIs(json.load(fh)["pricing_variant_inherit"], False)
        wb_settings.set_pricing_variant_inherit(self.accounts, True)
        self.assertTrue(wb_pricing.variant_inherit_enabled(self.accounts))

    def test_a_settings_file_without_the_key_reads_as_on(self):
        # 从没写过该键的版本升级上来，读出的默认值仍是打开，与发布说明一致。
        with open(os.path.join(self.accounts, "settings.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"pricing_refresh_minutes": 5}, fh)
        self.assertTrue(wb_pricing.variant_inherit_enabled(self.accounts))

    def test_the_switch_read_is_cached_but_never_goes_stale(self):
        # ensure_policy 每次为未定价模型请求都要读它，所以按（路径、mtime、
        # 大小）缓存；外部改写（面板保存 settings.json）仍必须被察觉。
        path = os.path.join(self.accounts, "settings.json")
        self.assertTrue(wb_pricing.variant_inherit_enabled(self.accounts))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pricing_variant_inherit": False}, fh)
        self.assertFalse(wb_pricing.variant_inherit_enabled(self.accounts))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pricing_variant_inherit": True}, fh)
        self.assertTrue(wb_pricing.variant_inherit_enabled(self.accounts))


class GapReportTests(PricingTestCase):
    """面板对还没价的模型显示什么。"""

    def test_the_report_categorises_every_kind(self):
        items, summary = wb_pricing.gap_items(OR, None, None, variants=True)
        by_model = {item["model"]: item for item in items}
        for alias in ALIASES:
            self.assertEqual(by_model[alias]["reason"], "alias", alias)
            self.assertNotIn("candidates", by_model[alias])
        self.assertEqual(by_model["kimi-k2.8-preview"]["reason"], "or_missing")
        self.assertEqual(by_model["kimi-k2-instruct-taiji"]["reason"],
                         "variant_unmatched")
        # 别名会列出来，但从不计入缺口合计，运营盯的是那个数。
        self.assertEqual(summary["aliases"], len(ALIASES))
        self.assertEqual(summary["total"],
                         summary["or_missing"] + summary["variant_unmatched"])
        self.assertTrue(summary["variants_enabled"])
        self.assertEqual(summary["total"], len(items) - len(ALIASES))

    def test_a_variant_that_resolves_is_not_reported_at_all(self):
        items, _summary = wb_pricing.gap_items(OR, None, None, variants=True)
        reported = {item["model"] for item in items}
        for priced in ("deepseek-r1-0528-lkeap", "deepseek-v3-1-lkeap",
                       "deepseek-v3-0324-lkeap"):
            self.assertNotIn(priced, reported, priced)

    def test_with_the_switch_off_the_variant_names_are_reported(self):
        items, _summary = wb_pricing.gap_items(OR, None, None, variants=False)
        by_model = {item["model"]: item for item in items}
        self.assertEqual(by_model["deepseek-r1-0528-lkeap"]["reason"],
                         "variant_unmatched")
        # 出厂快照里的模型不受开关影响，始终有价。
        self.assertNotIn("deepseek-v4.1-flash", by_model)

    def test_candidates_are_suggestions_never_applied(self):
        cands = wb_pricing.suggest_matches("glm-5.3-flash-20260101", OR)
        self.assertEqual(cands[0], "z-ai/glm-5.3-flash")
        # 它只给建议：不建策略、不写映射、不定价。
        self.assertIsNone(wb_pricing.ensure_policy("glm-5.3-flash-20260101"))
        self.assertEqual(wb_pricing.runtime_overrides(), {})

    def test_a_hand_entered_mapping_prices_a_model(self):
        wb_pricing.remember_live_index(OR)
        self.assertIsNone(wb_pricing.ensure_policy("kimi-k2.8-preview"))
        wb_pricing.save_runtime_override("kimi-k2.8-preview",
                                        "moonshotai/kimi-k2.6")
        self.assertEqual(wb_pricing.overrides_path(),
                         os.path.join(self.dir, "pricing-overrides.json"))
        by_norm = wb_pricing.index_openrouter(OR)
        self.assertEqual(
            wb_pricing.resolve("kimi-k2.8-preview", OR, by_norm)[0],
            "moonshotai/kimi-k2.6")
        pid = wb_pricing.ensure_policy("kimi-k2.8-preview")
        self.assertIsNotNone(pid)
        self.assertEqual(wb_pricing.load_policies()[pid]["or_id"],
                         "moonshotai/kimi-k2.6")

    def test_a_hand_entered_mapping_that_matches_nothing_is_ignored(self):
        wb_pricing.save_runtime_override("kimi-k2.8-preview",
                                        "moonshotai/not-in-this-list")
        by_norm = wb_pricing.index_openrouter(OR)
        self.assertIsNone(
            wb_pricing.resolve("kimi-k2.8-preview", OR, by_norm)[0])

    def test_a_mapping_can_be_cleared(self):
        wb_pricing.save_runtime_override("kimi-k2.8-preview",
                                        "moonshotai/kimi-k2.6")
        wb_pricing.save_runtime_override("kimi-k2.8-preview", "")
        self.assertEqual(wb_pricing.runtime_overrides(), {})
        by_norm = wb_pricing.index_openrouter(OR)
        self.assertIsNone(
            wb_pricing.resolve("kimi-k2.8-preview", OR, by_norm)[0])

    def test_the_report_covers_a_live_only_name_that_could_not_be_priced(self):
        original = M.curated_live_sources
        M.curated_live_sources = lambda realm: (
            ([("glm-5.3-flash-20260101", {})] if realm == "intl"
             else [("glm-5.3-flashx", {})]), True, "remote")
        try:
            refresher = self._refresher()
            ok, message = refresher.run_once()
        finally:
            M.curated_live_sources = original
        self.assertTrue(ok, message)
        items, summary = refresher.gap_report(ttl=0)
        by_model = {item["model"]: item for item in items}
        # OpenRouter 没有的实时名字可见，附上最接近的条目供人工映射，并计入合计。
        row = by_model["glm-5.3-flash-20260101"]
        self.assertEqual(row["reason"], "or_missing")
        self.assertEqual(row["candidates"][0], "z-ai/glm-5.3-flash")
        self.assertEqual(summary["total"], len(items) - len(ALIASES))
        # 已经解析成功的实时名字在同一个周期里就定了价，根本不算缺口。
        self.assertNotIn("glm-5.3-flashx", by_model)
        self.assertIsNotNone(wb_pricing.ensure_policy("glm-5.3-flashx"))

    def test_the_status_payload_carries_the_report(self):
        wb_pricing.remember_live_index(OR)
        refresher = self._refresher()
        refresher.run_once()
        status = refresher.status()
        self.assertIn("gap_summary", status)
        self.assertIsInstance(status["gaps"], list)
        self.assertIn("variant_inherit", status)
        # 按需定价路径用的就是刚跑过的那次抓取留下的索引。
        self.assertTrue(wb_pricing.ensure_policy("glm-5.3-flashx"))
        models = {item["model"] for item in status["gaps"]}
        self.assertNotIn("glm-5.3-flashx", models)

    def test_the_report_never_counts_a_price_that_is_already_stored(self):
        wb_pricing.remember_live_index(OR)
        wb_pricing.ensure_policy("kimi-k2.8-preview")
        wb_pricing.save_runtime_override("kimi-k2.8-preview",
                                        "moonshotai/kimi-k2.6")
        wb_pricing.ensure_policy("kimi-k2.8-preview")
        refresher = self._refresher()
        items, _summary = refresher.gap_report(ttl=0)
        self.assertNotIn("kimi-k2.8-preview", {i["model"] for i in items})


class PricingEndpointTests(PricingTestCase):
    """面板的两个计价写入口，走 handler 方法驱动。"""

    def _handler(self, payload, pricing=None):
        class Stub(object):
            _handle_settings_save = P.Handler._handle_settings_save
            _route_pricing_mapping = P.Handler._route_pricing_mapping

            def __init__(self):
                self.replies = []

            def _payload_or_error(self, allow_list=()):
                return payload

            def _json(self, status, data):
                self.replies.append((status, data))
                return status, data

            def _error(self, status, message, kind=""):
                self.replies.append((status, message))
                return status, message

        return Stub()

    def test_the_variant_switch_is_persisted_and_kicks_a_refresh(self):
        started = threading.Event()

        class Pricing(object):
            def invalidate_gaps(self):
                pass

            def run_once(self):
                started.set()
                return True, ""

        with unittest.mock.patch.object(P, "ACCOUNTS_DIR", self.accounts), \
                unittest.mock.patch.object(P, "PRICING", Pricing()):
            handler = self._handler({"pricing_variant_inherit": False})
            status, data = handler._handle_settings_save()
        self.assertEqual(status, 200)
        self.assertIs(data["pricing_variant_inherit"], False)
        self.assertTrue(data["pricing_refresh_started"])
        self.assertTrue(started.wait(5.0), "the switch must trigger a re-price")
        self.assertFalse(wb_settings.pricing_variant_inherit(self.accounts))

    def test_the_variant_switch_rejects_a_string(self):
        with unittest.mock.patch.object(P, "ACCOUNTS_DIR", self.accounts):
            handler = self._handler({"pricing_variant_inherit": "false"})
            status, _data = handler._handle_settings_save()
        self.assertEqual(status, 400)
        # 必须是真布尔：字符串会被当成 true，想关掉功能的运营会关不掉。
        self.assertTrue(wb_settings.pricing_variant_inherit(self.accounts))

    def test_an_unchanged_switch_does_not_kick_a_refresh(self):
        wb_settings.set_pricing_variant_inherit(self.accounts, False)
        with unittest.mock.patch.object(P, "ACCOUNTS_DIR", self.accounts), \
                unittest.mock.patch.object(P, "PRICING", None):
            handler = self._handler({"pricing_variant_inherit": False})
            status, data = handler._handle_settings_save()
        self.assertEqual(status, 200)
        self.assertNotIn("pricing_refresh_started", data)

    def test_a_hand_entered_mapping_is_written_and_a_bad_id_is_refused(self):
        class Pricing(object):
            def invalidate_gaps(self):
                pass

            def run_once(self):
                return True, ""

        with unittest.mock.patch.object(P, "PRICING", Pricing()):
            handler = self._handler(None)
            status, data = handler._route_pricing_mapping(
                {"model": "kimi-k2.8-preview", "or_id": "moonshotai/kimi-k2.6"})
            self.assertEqual(status, 200)
            self.assertIn("msg", data)
            # 形状不像 OpenRouter id 的值直接拒绝
            status, _data = handler._route_pricing_mapping(
                {"model": "kimi-k2.8-preview", "or_id": "not an id"})
            self.assertEqual(status, 400)
            # 留空 = 删除映射
            status, _data = handler._route_pricing_mapping(
                {"model": "kimi-k2.8-preview", "or_id": ""})
            self.assertEqual(status, 200)
        self.assertEqual(wb_pricing.runtime_overrides(), {})


if __name__ == "__main__":
    unittest.main()
