"""目录直接调用桌面端的 product-config 端点 GET /v3/config 并按 issue #85 的规则筛选：
丢弃别名，-sg/-x 视为付费版本，同名有免费 x0.00 变体时取免费的那个。

不联网：payload 合成，取数流程打桩。
"""
import os
import json
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-remote-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.environ["WB_PROXY_CATALOG_DIR"] = os.path.join(_TMP, "catalog")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy as P
import wb_models as M
import wb_settings


def payload(ids, meta=None):
    """/v3/config 响应体的形状，与真实端点返回一致。"""
    return {
        "code": 0,
        "data": {
            "agents": [{"name": "cli", "models": list(ids)}],
            "products": [{"models": [dict({"id": mid}, **(meta or {}).get(mid, {}))
                                      for mid in ids]}],
        },
    }


INTL_IDS = [
    "default-model", "fast-model", "balanced-model", "primary-model",
    "deep-model", "hy4-preview-f", "hy3", "deepseek-v4.1-flash",
    "deepseek-v4.1-flash-sg", "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra",
    "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "grok-4.7", "gemini-3.5-flash",
    "glm-5.3-flash", "glm-5.3", "glm-5.2", "kimi-k3", "kimi-k2.6",
    "kimi-k2.8-preview",
]
INTL_META = {
    "deepseek-v4.1-flash": {"credits": "x0.00"},
    "deepseek-v4.1-flash-sg": {"credits": "x0.03"},
    "hy4-preview-f": {"credits": "x0.00"},
    "hy4-preview": {"credits": "x0.29"},
    "grok-4.7": {"credits": "x1.90"},
    "hy3": {"credits": "x0.00"},
    "glm-5.3": {"credits": "x0.79"},
}

CN_IDS = [
    "auto", "hy4-preview", "hy3", "hy3-x", "deepseek-v4.1-flash",
    "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "minimax-m3", "kimi-k3-1", "kimi-k2.8-preview", "kimi-k2.7",
    "kimi-k2.6", "deepseek-v4-pro",
]
CN_META = {
    "hy4-preview": {"credits": "x0.29"},
    "hy3": {"credits": "x0.00"},
    "hy3-x": {"credits": "x0.05"},
    "deepseek-v4.1-flash": {"credits": "x0.11"},
}


class RemoteCatalogTests(unittest.TestCase):
    def setUp(self):
        M._models_cache["intl"] = {"at": 0.0, "data": None}
        M._models_cache["cn"] = {"at": 0.0, "data": None}
        for realm in ("intl", "cn"):
            for cache in (M.catalog_cache_path(realm), M.catalog_snapshot_path(realm)):
                if os.path.exists(cache):
                    os.remove(cache)
        self._orig = (M.fetch_remote_product_config,
                                M.read_product_config_models,
                                M.fetch_endpoint_models,
                                M.product_config_path)

    def tearDown(self):
        (M.fetch_remote_product_config, M.read_product_config_models,
         M.fetch_endpoint_models, M.product_config_path) = self._orig
        for name in os.listdir(os.environ["ACCOUNTS_DIR"]):
            if name == "settings.json":
                os.remove(os.path.join(os.environ["ACCOUNTS_DIR"], name))

    def served(self, realm, force=False):
        return [m for m, _ in M.fetch_models(realm, force=force)]

    def test_credits_follow_the_endpoint_and_fall_back_for_a_pinned_model(self):
        """倍率角标取值的来源判定。

        端点仍在列的模型跟随端点报价；端点已下线的模型保留内置值。issue #85 起
        cn 免费 hy4-preview-f 由筛选表固定，端点下线它之后角标只能显示内置值，
        端点重新上架前无法跟随变价。断言同时覆盖面板读取的 model_entry() 结果。
        """
        # 1. 端点为筛选表里的模型报出新价时，以端点为准。
        meta = dict(INTL_META)
        meta["hy4-preview-f"] = {"credits": "x0.29"}
        M.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, meta) if realm == "intl" else None)
        entries = dict(M.fetch_models("intl"))
        self.assertEqual(entries["hy4-preview-f"]["credits"], "x0.29")
        self.assertEqual(
            M.model_entry("hy4-preview-f", entries["hy4-preview-f"])["credits"],
            "x0.29")

        # 2. 端点下线该模型后，内置值保留，面板显示的就是内置值。
        ids = [m for m in INTL_IDS if m != "hy4-preview-f"]
        meta = dict(INTL_META)
        meta.pop("hy4-preview-f", None)
        M.fetch_remote_product_config = (
            lambda realm: (ids, meta) if realm == "intl" else None)
        M.write_catalog_cache({"realms": {"intl": {
            "fetched_at": time.time() - M.CATALOG_CACHE_TTL - 60}}})
        M._models_cache["intl"] = {"at": 0.0, "data": None}
        entries = dict(M.fetch_models("intl"))
        self.assertIn("hy4-preview-f", entries)
        self.assertEqual(entries["hy4-preview-f"]["credits"], "x0.00")
        self.assertEqual(
            M.model_entry("hy4-preview-f", entries["hy4-preview-f"])["credits"],
            "x0.00")

    def test_parse_keeps_order_and_metadata(self):
        ids, meta = M.parse_remote_catalog(payload(INTL_IDS, INTL_META))
        self.assertEqual(ids, INTL_IDS)
        self.assertEqual(meta["grok-4.7"]["credits"], "x1.90")

    def test_parse_rejects_empty_and_odd_shapes(self):
        self.assertIsNone(M.parse_remote_catalog({"data": {"agents": []}}))
        self.assertIsNone(M.parse_remote_catalog(
            {"data": {"agents": [{"models": []}]}}))
        self.assertIsNone(M.parse_remote_catalog({}))
        self.assertIsNone(M.parse_remote_catalog(None))
        ids, _ = M.parse_remote_catalog(
            {"data": {"agents": {"cli": {"models": ["a", "b"]}}}})
        self.assertEqual(ids, ["a", "b"])

    def test_curate_applies_the_rules(self):
        curated = M.curate_remote_catalog("intl", INTL_IDS, INTL_META)
        self.assertEqual(curated, [m for m in INTL_IDS if m not in (
            "default-model", "fast-model", "balanced-model", "primary-model",
            "deep-model", "deepseek-v4.1-flash-sg")])
        self.assertIn("grok-4.7", curated)
        self.assertIn("hy4-preview-f", curated)

    def test_curate_keeps_live_variants(self):
        ids = ["hy4-preview", "hy4-preview-f", "hy3", "hy3-x"]
        meta = {"hy4-preview": {"credits": "x0.29"},
             "hy4-preview-f": {"credits": "x0.00"},
             "hy3": {"credits": "x0.00"}, "hy3-x": {"credits": "x0.05"}}
        self.assertEqual(M.curate_remote_catalog("intl", ids, meta),
                    ["hy4-preview", "hy4-preview-f", "hy3"])

    def test_curate_keeps_a_paid_model_with_no_free_sibling(self):
        ids = ["grok-4.7", "glm-5.3"]
        meta = {"grok-4.7": {"credits": "x1.90"},
             "glm-5.3": {"credits": "x0.79"}}
        self.assertEqual(M.curate_remote_catalog("intl", ids, meta), ids)

    def test_fetch_models_uses_the_remote_and_orders_by_the_table(self):
        M.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)
        ids = self.served("intl")
        self.assertEqual(ids, [
            "hy4-preview-f", "hy3", "deepseek-v4.1-flash", "gpt-6-astra",
            "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
            "gpt-5.4", "grok-4.7", "gemini-3.5-flash", "glm-5.3-flash",
            "glm-5.3", "glm-5.2", "kimi-k3", "kimi-k2.6", "kimi-k2.8-preview"])
        M.fetch_models("intl")
        info = M.catalog_meta("intl")
        self.assertEqual(info["source"], "remote")
        self.assertTrue(info["fetched_at"])

    def test_cn_keeps_the_pinned_free_variant(self):
        # 以实时 meta 为准：上游已下线 hy4-preview-f 且 hy4-preview 收费 0.29x，对外如实给出 hy4-preview
        M.fetch_remote_product_config = (
            lambda realm: (CN_IDS, CN_META) if realm == "cn" else None)
        ids = [m for m, _ in M.fetch_models("cn")]
        self.assertEqual(ids[0], "hy4-preview")
        self.assertIn("hy4-preview", ids)
        self.assertNotIn("hy4-preview-f", ids)
        self.assertNotIn("hy3-x", ids)         # -x 变体
        self.assertNotIn("auto", ids)          # auto 路由别名

    def test_saved_catalogue_answers_without_the_server(self):
        """一次成功获取后 24 小时内直接用磁盘缓存回答，不联系服务器。"""
        calls = []

        def remote(realm):
            calls.append(realm)
            return (INTL_IDS, INTL_META) if realm == "intl" else None

        M.fetch_remote_product_config = remote
        self.assertEqual(self.served("intl")[0], "hy4-preview-f")
        self.assertEqual(calls, ["intl"])
        # 清掉内存副本，模拟重启后只剩磁盘缓存
        M._models_cache["intl"] = {"at": 0.0, "data": None}
        ids = M.fetch_models("intl")
        info = M.catalog_meta("intl")
        self.assertEqual([m for m, _ in ids][0], "hy4-preview-f")
        self.assertEqual(calls, ["intl"])
        self.assertEqual(info["source"], "cache")

    def test_expired_cache_is_refetched(self):
        M.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)
        self.served("intl")
        data = M.read_catalog_cache()
        data["realms"]["intl"]["fetched_at"] = time.time() - M.CATALOG_CACHE_TTL - 60
        M.write_catalog_cache(data)
        calls = []

        def remote(realm):
            calls.append(realm)
            return (INTL_IDS, INTL_META) if realm == "intl" else None

        M.fetch_remote_product_config = remote
        M._models_cache["intl"] = {"at": 0.0, "data": None}
        M.fetch_models("intl")
        info = M.catalog_meta("intl")
        self.assertEqual(calls, ["intl"])
        self.assertEqual(info["source"], "remote")

    def test_expired_cache_still_answers_when_the_server_fails(self):
        M.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)
        self.served("intl")
        data = M.read_catalog_cache()
        data["realms"]["intl"]["fetched_at"] = time.time() - M.CATALOG_CACHE_TTL - 60
        M.write_catalog_cache(data)
        M.fetch_remote_product_config = lambda realm: None
        M._models_cache["intl"] = {"at": 0.0, "data": None}
        ids = M.fetch_models("intl")
        info = M.catalog_meta("intl")
        self.assertEqual(info["source"], "cache")
        self.assertIn("grok-4.7", [m for m, _ in ids])

    def test_forced_refresh_asks_the_server_again(self):
        M.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)
        self.served("intl")
        calls = []

        def remote(realm):
            calls.append(realm)
            return (INTL_IDS, INTL_META) if realm == "intl" else None

        M.fetch_remote_product_config = remote
        self.served("intl", force=True)
        self.assertEqual(calls, ["intl"])

    def test_remote_failure_falls_back_and_never_leaks_new_names(self):
        calls = []

        def boom(realm):
            calls.append(realm)
            return None

        M.fetch_remote_product_config = boom
        M.read_product_config_models = lambda realm=None: []
        M.product_config_path = lambda realm: os.path.join(_TMP, "absent.json")
        M.fetch_endpoint_models = lambda: ["kimi-k3", "internal-only-model"]
        ids = self.served("intl")
        self.assertEqual(calls, ["intl"])
        self.assertIn("kimi-k3", ids)
        self.assertIn("grok-4.7", ids)   # 内置快照仍会补齐名称
        self.assertNotIn("internal-only-model", ids)  # 窄端点结果仍受筛选表约束

    def test_live_reasoning_efforts_win_over_the_bundled_table(self):
        """reasoning 档位以端点返回的 reasoning 块为准，内置表不参与改写。"""
        meta = dict(INTL_META)
        meta["deepseek-v4.1-flash"] = {
            "credits": "x0.00",
            "reasoning": {"supportedEfforts": ["low", "medium", "high"],
                          "defaultEffort": "medium"},
        }
        M.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, meta) if realm == "intl" else None)
        entries = dict(M.fetch_models("intl"))
        item = M.model_entry("deepseek-v4.1-flash",
                             entries["deepseek-v4.1-flash"])
        self.assertEqual(item["reasoning_efforts"], ["low", "medium", "high"])
        self.assertEqual(item["reasoning_default_effort"], "medium")

    def test_bundled_efforts_fill_in_when_the_live_entry_has_none(self):
        """实时条目缺 reasoning 时回退内置快照，并且只记一次日志。"""
        logged = []
        original_log = P.log
        P.log = lambda message, **kw: logged.append(message)
        try:
            M.fetch_remote_product_config = (
                lambda realm: (INTL_IDS, dict(INTL_META)) if realm == "intl" else None)
            M._catalog_fallback_log.pop("intl", None)
            entries = dict(M.fetch_models("intl"))
        finally:
            P.log = original_log
        item = M.model_entry("deepseek-v4.1-flash",
                             entries["deepseek-v4.1-flash"])
        self.assertEqual(item["reasoning_efforts"], ["low", "high", "max"])
        self.assertEqual(item["reasoning_default_effort"], "high")
        self.assertTrue(any("bundled table" in m for m in logged), logged)

    def test_bare_live_effort_keeps_the_model_selectable(self):
        """issue #170：实时块里的裸 `effort` 只作默认档，不锁定档位。

        端点对 deepseek-v4.1-flash 只答 {"effort": "high"}，但档位可选；
        /v1/models 仍要列出可选档位，实际转发的档位跟随客户端。
        """
        meta = dict(INTL_META)
        meta["deepseek-v4.1-flash"] = {
            "credits": "x0.00",
            "reasoning": {"effort": "high", "summary": "auto"},
        }
        logged = []
        original_log = P.log
        P.log = lambda message, **kw: logged.append(message)
        try:
            M.fetch_remote_product_config = (
                lambda realm: (INTL_IDS, meta) if realm == "intl" else None)
            M._catalog_fallback_log.pop("intl", None)
            entries = dict(M.fetch_models("intl"))
        finally:
            P.log = original_log
        item = M.model_entry("deepseek-v4.1-flash",
                             entries["deepseek-v4.1-flash"])
        self.assertEqual(item["reasoning_efforts"], ["low", "high", "max"])
        self.assertEqual(item["reasoning_default_effort"], "high")
        self.assertNotIn("reasoning_fixed_effort", item)
        # 实际转发的档位跟随客户端。
        self.assertIsNone(P.model_fixed_effort("deepseek-v4.1-flash"))
        self.assertEqual(
            P.upstream_effort_of({"reasoning_effort": "max"},
                                 "deepseek-v4.1-flash"),
            "max")
        # 实时目录真正锁定档位的模型不受影响：快照没有 supportedEfforts 可恢复，
        # 裸 effort 仍然锁定档位。
        pinned = M.model_entry("gemini-3.5-flash", entries["gemini-3.5-flash"])
        self.assertEqual(pinned["reasoning_fixed_effort"], "medium")
        self.assertNotIn("reasoning_efforts", pinned)
        # 回退到内置表会写日志，不静默发生。
        self.assertTrue(any("bundled table" in m for m in logged), logged)

    def test_parse_reads_the_desktop_cache_shape(self):
        """缓存文件是同一份文档，去掉 "data" 外层。"""
        ids, _ = M.parse_remote_catalog(
            {"agents": [{"name": "compact", "models": ["lite"]},
                        {"name": "cli", "models": ["a", "b"]}]})
        self.assertEqual(ids, ["a", "b"])   # 多个 agent 时以 cli 为准
        ids, _ = M.parse_remote_catalog(
            {"agents": [{"name": "planner", "models": ["a", "b", "c"]}]})
        self.assertEqual(ids, ["a", "b", "c"])

    def test_cached_desktop_file_drives_the_list_without_the_endpoint(self):
        M.fetch_remote_product_config = lambda realm: None
        payload = {"agents": [{"name": "cli",
                               "models": CN_IDS + ["new-model-9", "uncredited-model"]}],
                   "models": [{"id": "hy3", "credits": "x0.00"},
                              {"id": "new-model-9", "credits": "x1.50"}]}
        path = os.path.join(_TMP, "acc-product-config-v3.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        M.product_config_path = lambda realm: path
        M._models_cache["cn"] = {"at": 0.0, "data": None}
        ids = [m for m, _ in M.fetch_models("cn")]
        self.assertEqual(ids[0], "hy4-preview")
        self.assertNotIn("auto", ids)
        self.assertIn("new-model-9", ids)   # 仅本地文件出现且配了价格的模型直接提供，无需发版
        self.assertNotIn("uncredited-model", ids)  # 未配置价格的模型先隐藏


class FakePanelRequest(object):
    def __init__(self):
        self.replies = []

    def _json(self, status, payload):
        self.replies.append((status, payload))
        return self.replies[-1]


class PanelModelListTests(unittest.TestCase):
    """面板的模型表：被筛选规则排除的模型单独列出，读取不改变路由状态。"""

    EXCLUDED = ["default-model", "fast-model", "balanced-model",
                "primary-model", "deep-model", "deepseek-v4.1-flash-sg"]

    def setUp(self):
        M._models_cache["intl"] = {"at": 0.0, "data": None}
        for realm in ("intl", "cn"):
            for cache in (M.catalog_cache_path(realm), M.catalog_snapshot_path(realm)):
                if os.path.exists(cache):
                    os.remove(cache)
        settings = wb_settings.settings_path(P.ACCOUNTS_DIR)
        if os.path.exists(settings):
            os.remove(settings)
        self._orig = M.fetch_remote_product_config
        M.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)

    def tearDown(self):
        M.fetch_remote_product_config = self._orig

    def view(self, **query):
        request = FakePanelRequest()
        P.Handler._get_panel_models(request, {k: [v] for k, v in query.items()})
        status, payload = request.replies[-1]
        self.assertEqual(status, 200)
        return payload

    def row(self, payload, mid):
        for item in payload["data"]:
            if item["id"] == mid:
                return item
        self.fail("%s not in the panel list" % mid)

    def test_excluded_models_start_switched_off(self):
        payload = self.view(realm="intl")
        self.assertEqual(payload["source"], "server")
        for mid in self.EXCLUDED:
            item = self.row(payload, mid)
            self.assertTrue(item["excluded"], mid)
            self.assertFalse(item["enabled"], mid)
            self.assertFalse(wb_settings.model_served(P.ACCOUNTS_DIR, mid), mid)
        served = self.row(payload, "grok-4.7")
        self.assertTrue(served["enabled"])
        self.assertFalse(served["excluded"])
        self.assertEqual(
            sorted(m.lower() for m in wb_settings.disabled_models(P.ACCOUNTS_DIR)),
            sorted(m.lower() for m in self.EXCLUDED))

    def test_a_checked_excluded_model_stays_checked(self):
        """操作者重新勾选后，下一次加载不会又被默认排除关掉。"""
        self.view(realm="intl")
        keep = [m for m in wb_settings.disabled_models(P.ACCOUNTS_DIR)
                if m != "deepseek-v4.1-flash-sg"]
        wb_settings.set_disabled_models(P.ACCOUNTS_DIR, keep)
        payload = self.view(realm="intl")
        item = self.row(payload, "deepseek-v4.1-flash-sg")
        self.assertTrue(item["excluded"])
        self.assertTrue(item["enabled"])
        self.assertTrue(wb_settings.model_served(P.ACCOUNTS_DIR,
                                                 "deepseek-v4.1-flash-sg"))
        self.assertNotIn("deepseek-v4.1-flash-sg",
                         wb_settings.disabled_models(P.ACCOUNTS_DIR))

    def test_a_virtual_alias_can_be_re_enabled(self):
        self.view(realm="intl")   # 首次加载按默认排除规则播种禁用名单
        keep = [m for m in wb_settings.disabled_models(P.ACCOUNTS_DIR)
                if m != "fast-model"]
        wb_settings.set_disabled_models(P.ACCOUNTS_DIR, keep)
        payload = self.view(realm="intl")
        self.assertTrue(self.row(payload, "fast-model")["enabled"])
        self.assertTrue(wb_settings.model_served(P.ACCOUNTS_DIR, "fast-model"))
        self.assertFalse(self.row(payload, "balanced-model")["enabled"])

    def test_refresh_reports_whether_the_server_answered(self):
        payload = self.view(realm="intl", refresh="1")
        self.assertTrue(payload["refresh_ok"])
        self.assertEqual(payload["source"], "server")
        M.fetch_remote_product_config = lambda realm: None
        M._models_cache["intl"] = {"at": 0.0, "data": None}
        payload = self.view(realm="intl", refresh="1")
        self.assertFalse(payload["refresh_ok"])
        self.assertEqual(payload["source"], "cache")
        self.assertTrue(self.row(payload, "grok-4.7")["enabled"])


if __name__ == "__main__":
    unittest.main()
