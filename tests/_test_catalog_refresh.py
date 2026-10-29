"""面板「刷新模型清单」手动刷新的契约：realm 隔离、冷却与失败保留，另有来源
追踪、每日兜底、落盘存档三组用例。不联网：取数一律打桩，落盘存档的用例改打桩
wb_accounts.urlopen 走真实取数流程。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-catalog-refresh-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.environ["WB_PROXY_CATALOG_DIR"] = os.path.join(_TMP, "catalog")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy as P
import wb_models as M


CN_LIVE = (["hy4-preview-f", "hy3", "deepseek-v4.1-flash", "glm-5.3"],
           {"hy4-preview-f": {"credits": "x0.00"},
            "hy3": {"credits": "x0.00"},
            "deepseek-v4.1-flash": {"credits": "x0.11"},
            "glm-5.3": {"credits": "x0.79"}})
INTL_LIVE = (["grok-4.7"], {"grok-4.7": {"credits": "x1.90"}})


def _clear_gateway_catalog():
    """24h 内的落盘副本会直接答清单，出处也就成了 cache；这些用例要验
    远程/回退分支，先把副本清掉。"""
    for realm in ("intl", "cn"):
        for path in (M.catalog_snapshot_path(realm), M.catalog_cache_path(realm)):
            try:
                os.remove(path)
            except OSError:
                pass


class CatalogRefreshTests(unittest.TestCase):
    """refresh_catalog() 的契约，面板按钮 POST /catalog/refresh 最终调到它：

    - realm 隔离：cn 的刷新只驱动 cn 的取数（fetch_remote_product_config("cn")），
      intl 同理。跨区取数是用户点名要避免的事故：拿国内账号的 token 去拉国际
      版目录（或反过来），清单会串、还会撞上游的风控口径。
    - 发布语义：刷新成功后 _models_cache[realm] 被立刻替换，/v1/models 下一次
      读取就看到新清单，无需等 300s 缓存过期。
    - 冷却：同一 realm 30s 内第二次触发被拒且不发起任何取数（含失败后的重试
      连点）；冷却按 realm 各记各的。
    - 失败保留：上游没给目录时返回 ok=False，旧缓存原样保留；且不读桌面缓存
      文件——手动按钮承诺的是「真的从上游 API 取到了」。
    - 非法 realm 拒绝且零副作用。
    """

    def setUp(self):
        M._models_cache["intl"] = {"at": 0.0, "data": None}
        M._models_cache["cn"] = {"at": 0.0, "data": None}
        M._catalog_meta.clear()
        M._catalog_refresh_at.clear()
        self._orig = (M.fetch_remote_product_config,
                      M.read_cached_remote_catalog)
        self.calls = []

    def tearDown(self):
        (M.fetch_remote_product_config,
         M.read_cached_remote_catalog) = self._orig

    def stub(self, by_realm):
        """取数打桩：记录被问到的 realm，返回 None 表示取数失败。"""
        def fake(realm):
            self.calls.append(realm)
            return by_realm.get(realm)
        M.fetch_remote_product_config = fake

    def test_refresh_uses_only_the_named_realm_and_publishes(self):
        self.stub({"cn": CN_LIVE, "intl": INTL_LIVE})
        out = M.refresh_catalog("cn")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["realm"], "cn")
        self.assertEqual(out["source"], "remote")
        self.assertGreaterEqual(out["count"], len(CN_LIVE[0]))
        # 只驱动了 cn 的取数——绝不碰 intl。
        self.assertEqual(self.calls, ["cn"])
        # 发布语义：/v1/models 的下一次读取立刻拿到新清单（不再触发取数）。
        entries = dict(M.fetch_models("cn"))
        self.assertIn("glm-5.3", entries)
        self.assertEqual(self.calls, ["cn"])
        # intl 的缓存没被动过：还是空，取数也没碰它。
        self.assertIsNone(M._models_cache["intl"]["data"])

    def test_refresh_replaces_the_cached_list_immediately(self):
        # 旧清单用内置快照之外的模型做标记：快照条目无论刷新与否都并入清单，
        # 拿它们当旧数据看不出替换效果。
        M._models_cache["cn"] = {"at": time.time(),
                                 "data": [("zz-old-model", {"credits": "x0.01"})]}
        self.stub({"cn": (["grok-4.7"], {"grok-4.7": {"credits": "x1.90"}})})
        out = M.refresh_catalog("cn")
        self.assertTrue(out["ok"], out)
        entries = dict(M.fetch_models("cn"))
        self.assertIn("grok-4.7", entries)
        # 发布是整体替换：旧清单独有的条目必须消失，合并语义会把它留在 /v1/models。
        self.assertNotIn("zz-old-model", entries)

    def test_cooldown_blocks_a_second_press_and_sends_nothing(self):
        self.stub({"cn": CN_LIVE})
        first = M.refresh_catalog("cn")
        self.assertTrue(first["ok"], first)
        second = M.refresh_catalog("cn")
        self.assertFalse(second["ok"])
        self.assertTrue(second.get("cooldown"))
        self.assertGreater(second["retry_after"], 0)
        # 第二次没有发起任何取数。
        self.assertEqual(self.calls, ["cn"])

    def test_cooldown_is_per_realm(self):
        self.stub({"cn": CN_LIVE, "intl": INTL_LIVE})
        self.assertTrue(M.refresh_catalog("cn")["ok"])
        out = M.refresh_catalog("intl")
        self.assertTrue(out["ok"], out)   # cn 的冷却不拦 intl
        self.assertEqual(self.calls, ["cn", "intl"])

    def test_failed_refresh_keeps_the_previous_list(self):
        self.stub({"cn": None})
        M._models_cache["cn"] = {"at": 0.0,
                                 "data": [("old-model", {"credits": "x0.50"})]}
        out = M.refresh_catalog("cn")
        self.assertFalse(out["ok"])
        self.assertIn("保留", out["msg"])
        self.assertEqual(M._models_cache["cn"]["data"],
                         [("old-model", {"credits": "x0.50"})])

    def test_failed_refresh_never_falls_back_to_the_desktop_cache(self):
        """手动按钮承诺的是上游 API 的实时数据，不是本机缓存文件。"""
        seen = []

        def cached(realm):
            seen.append(realm)
            return CN_LIVE

        M.read_cached_remote_catalog = cached
        self.stub({"cn": None})
        out = M.refresh_catalog("cn")
        self.assertFalse(out["ok"])
        self.assertEqual(seen, [])

    def test_cooldown_also_covers_a_failed_attempt(self):
        """失败也进冷却：连点重试不该变成对上游的高频轰炸。"""
        self.stub({"cn": None})
        self.assertFalse(M.refresh_catalog("cn")["ok"])
        second = M.refresh_catalog("cn")
        self.assertTrue(second.get("cooldown"), second)

    def test_invalid_realm_is_rejected_without_side_effects(self):
        self.stub({"cn": CN_LIVE, "intl": INTL_LIVE})
        for bad in ("all", "", None, "CN ", "cn2"):
            out = M.refresh_catalog(bad)
            self.assertFalse(out["ok"], bad)
            self.assertIsNone(out["realm"], bad)
        self.assertEqual(self.calls, [])

    def test_refresh_records_remote_provenance(self):
        """手动刷新成功后，来源记录为 remote，来源行显示「上游 API 获取」。"""
        self.stub({"cn": CN_LIVE})
        self.assertTrue(M.refresh_catalog("cn")["ok"])
        meta = M.catalog_meta("cn")
        self.assertEqual(meta["source"], "remote")
        self.assertIsNone(meta["file_mtime"])
        self.assertAlmostEqual(meta["fetched_at"], time.time(), delta=10)


class CatalogProvenanceTests(unittest.TestCase):
    """catalog_meta()：清单每批数据的出处与各自的时间戳。

    面板的来源行要如实回答「这批清单是刚刚通过 API 拿的，还是从本机配置
    文件读的（那份文件什么时候写的），还是只有内置快照」，窄端点兜底记为
    endpoint。字段错一格，展示就会把旧缓存说成实时数据；来源横幅靠它区分
    「通过 API 获取」与「项目默认/缓存文件数据」。
    """

    def setUp(self):
        self._orig = (M.fetch_remote_product_config,
                      M.read_cached_remote_catalog,
                      M.read_product_config_models,
                      M.fetch_endpoint_models,
                      M.product_config_path)
        M._models_cache["intl"] = {"at": 0.0, "data": None}
        M._models_cache["cn"] = {"at": 0.0, "data": None}
        M._catalog_meta.clear()
        _clear_gateway_catalog()

    def tearDown(self):
        (M.fetch_remote_product_config, M.read_cached_remote_catalog,
         M.read_product_config_models, M.fetch_endpoint_models,
         M.product_config_path) = self._orig

    def test_api_fetch_is_recorded_as_remote(self):
        M.fetch_remote_product_config = (
            lambda realm: CN_LIVE if realm == "cn" else None)
        M.read_cached_remote_catalog = lambda realm: None
        M.fetch_models("cn")
        meta = M.catalog_meta("cn")
        self.assertEqual(meta["source"], "remote")
        self.assertIsNone(meta["file_mtime"])
        self.assertAlmostEqual(meta["fetched_at"], time.time(), delta=10)

    def test_config_file_fallback_carries_the_write_and_read_times(self):
        """回退读配置文件时：file_mtime 是文件自己的写入时间（可能很旧），
        fetched_at 是网关这次读取的时间——两者分开记，面板才能说清
        「这份数据什么时候写下的」和「什么时候被读进网关」。"""
        path = os.path.join(_TMP, "acc-product-config-v3.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.utime(path, (1700000000.0, 1700000000.0))
        M.fetch_remote_product_config = lambda realm: None
        M.read_cached_remote_catalog = lambda realm: CN_LIVE
        M.read_product_config_models = lambda realm=None: []
        M.product_config_path = lambda realm: path
        M.fetch_models("cn")
        meta = M.catalog_meta("cn")
        self.assertEqual(meta["source"], "cache")
        self.assertEqual(meta["file_mtime"], 1700000000.0)
        self.assertGreater(meta["fetched_at"], meta["file_mtime"])
        # 数据本身还是来自那份文件（hy3 等都在）。
        self.assertIn("hy3", dict(M.fetch_models("cn")))

    def test_legacy_reader_also_counts_as_a_config_file(self):
        path = os.path.join(_TMP, "acc-product-config-v3.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.utime(path, (1710000000.0, 1710000000.0))
        M.fetch_remote_product_config = lambda realm: None
        M.read_cached_remote_catalog = lambda realm: None
        M.read_product_config_models = lambda realm=None: [
            ("glm-5.3", {"credits": "x0.79"})]
        M.product_config_path = lambda realm: path
        M.fetch_models("cn")
        meta = M.catalog_meta("cn")
        self.assertEqual(meta["source"], "legacy")
        self.assertEqual(meta["file_mtime"], 1710000000.0)

    def test_upstream_empty_is_recorded_as_bundled_snapshot(self):
        M.fetch_remote_product_config = lambda realm: None
        M.read_cached_remote_catalog = lambda realm: None
        M.read_product_config_models = lambda realm=None: []
        entries = M.fetch_models("cn")
        self.assertTrue(entries)   # 内置快照仍然给出清单
        meta = M.catalog_meta("cn")
        self.assertEqual(meta["source"], "bundled")
        self.assertIsNone(meta["file_mtime"])

    def test_narrow_endpoint_fallback_is_not_reported_as_remote(self):
        """窄端点（/v1/models）兜底同样算一次 API 获取，但拿不到桌面目录，
        来源如实写成 endpoint，不写 remote。"""
        M.fetch_remote_product_config = lambda realm: None
        M.read_cached_remote_catalog = lambda realm: None
        M.read_product_config_models = lambda realm=None: []
        M.fetch_endpoint_models = lambda: ["gpt-5.5"]
        entries = dict(M.fetch_models("intl"))
        self.assertIn("gpt-5.5", entries)
        self.assertEqual(M.catalog_meta("intl")["source"], "endpoint")

    def test_provenance_is_per_realm(self):
        M.fetch_remote_product_config = (
            lambda realm: CN_LIVE if realm == "cn" else None)
        M.read_cached_remote_catalog = lambda realm: None
        M.read_product_config_models = lambda realm=None: []
        M.fetch_models("cn")
        M.fetch_models("intl")     # intl：全无 → bundled
        self.assertEqual(M.catalog_meta("cn")["source"], "remote")
        self.assertEqual(M.catalog_meta("intl")["source"], "bundled")


class CatalogDailyTests(unittest.TestCase):
    """目录的「每天至少从上游 API 取一次」兜底：到期判定与补取范围。

    只测可确定的部分：什么算到期（距上次成功 ≥ 24h / 从未成功）、成功
    打点后 24h 内不重复、失败不打点（下一轮继续补）、补取只碰到期且
    有可用账号的 realm。线程本身（60s 首查 + 每小时巡检）不在这测。
    """

    class _FakePool(object):
        def representative(self, realm=None):
            return object()

    def setUp(self):
        self._orig = (M.fetch_remote_product_config,
                      M.read_cached_remote_catalog,
                      M.read_product_config_models,
                      M.product_config_path,
                      P.POOL,
                      M.refresh_catalog)
        M._models_cache["intl"] = {"at": 0.0, "data": None}
        M._models_cache["cn"] = {"at": 0.0, "data": None}
        M._catalog_meta.clear()
        M._catalog_last_remote.clear()
        M._catalog_refresh_at.clear()
        _clear_gateway_catalog()
        P.POOL = self._FakePool()

    def tearDown(self):
        (M.fetch_remote_product_config, M.read_cached_remote_catalog,
         M.read_product_config_models, M.product_config_path,
         P.POOL, M.refresh_catalog) = self._orig

    def test_both_realms_are_due_before_any_successful_fetch(self):
        self.assertEqual(M.catalog_daily_due(), ["intl", "cn"])

    def test_a_successful_api_fetch_clears_its_realm_for_a_day(self):
        M.fetch_remote_product_config = (
            lambda realm: CN_LIVE if realm == "cn" else None)
        M.read_cached_remote_catalog = lambda realm: None
        M.read_product_config_models = lambda realm=None: []
        M.fetch_models("cn")
        now = time.time()
        self.assertNotIn("cn", M.catalog_daily_due(now))
        self.assertIn("intl", M.catalog_daily_due(now))   # 没成功过的仍然到期
        self.assertIn("cn", M.catalog_daily_due(now + M.CATALOG_DAILY_INTERVAL))

    def test_cache_and_bundled_fallbacks_do_not_clear_the_due_flag(self):
        """读了本机配置文件也算「没从 API 拿到」：下一轮还要补取。"""
        M.fetch_remote_product_config = lambda realm: None
        M.read_cached_remote_catalog = lambda realm: CN_LIVE
        M.product_config_path = lambda realm: os.path.join(_TMP, "nope.json")
        M.fetch_models("cn")      # 此时来源是 cache
        self.assertEqual(M.catalog_meta("cn")["source"], "cache")
        self.assertIn("cn", M.catalog_daily_due())

    def test_a_failed_refresh_does_not_mark_the_day(self):
        M.fetch_remote_product_config = lambda realm: None
        self.assertFalse(M.refresh_catalog("cn")["ok"])
        self.assertIn("cn", M.catalog_daily_due())

    def test_refresh_success_also_marks_the_day(self):
        M.fetch_remote_product_config = (
            lambda realm: CN_LIVE if realm == "cn" else None)
        self.assertTrue(M.refresh_catalog("cn")["ok"])
        self.assertNotIn("cn", M.catalog_daily_due())

    def test_daily_pass_only_touches_due_realms(self):
        calls = []
        M.refresh_catalog = (
            lambda realm: (calls.append(realm)
                           or {"ok": True, "realm": realm, "msg": "x"}))
        M.note_remote_catalog("cn")     # cn 今天已经成功过
        done = M.catalog_daily_pass()
        self.assertEqual(calls, ["intl"])
        self.assertEqual([r for r, _ in done], ["intl"])

    def test_daily_pass_skips_a_realm_without_usable_accounts(self):
        calls = []

        class EmptyPool(object):
            def representative(self, realm=None):
                return None

        P.POOL = EmptyPool()
        M.refresh_catalog = lambda realm: calls.append(realm) or {"ok": True}
        self.assertEqual(M.catalog_daily_pass(), [])
        self.assertEqual(calls, [])     # 静默跳过：不是错误，也不打上游

    def test_note_records_only_known_realms(self):
        M.note_remote_catalog("all")
        M.note_remote_catalog("")
        self.assertEqual(M.catalog_daily_due(), ["intl", "cn"])
        M.note_remote_catalog("cn")
        self.assertNotIn("cn", M.catalog_daily_due())


def config_payload(ids):
    """/v3/config 响应体的形状，与端点返回一致。"""
    return {"code": 0,
            "data": {"agents": [{"name": "cli", "models": list(ids)}]}}


class _FakeResponse(object):
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Account(object):
    access_token = "tok"
    uid = "u-1"
    proxy = None

    def auth_headers(self):
        return {"Authorization": "Bearer " + self.access_token}


class _FakePool(object):
    def representative(self, realm=None):
        return _Account()


class CatalogDiskDumpTests(unittest.TestCase):
    """成功拉取后把 /v3/config 响应体原样落盘一份，纯存档。

    文件落在 CATALOG_CACHE_DIR（默认 <script>/catalog，gitignored、运行时
    产生、更新不覆盖）。网关自己不读它，所以这里只验证两件事：成功一次就有
    一份最新副本（内容与响应体一致、无 .tmp 残留）、写盘失败静默不伤取数。
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="wb-catalog-dump-")
        self._orig = (M.CATALOG_CACHE_DIR, P.POOL, P.wb_accounts.urlopen)
        M.CATALOG_CACHE_DIR = self._dir
        P.POOL = _FakePool()

    def tearDown(self):
        M.CATALOG_CACHE_DIR, P.POOL, P.wb_accounts.urlopen = self._orig
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_a_successful_fetch_dumps_the_payload(self):
        payload = config_payload(["glm-5.3", "hy3"])

        def fake_urlopen(req, timeout=10, proxy=None):
            return _FakeResponse(payload)

        P.wb_accounts.urlopen = fake_urlopen
        parsed = M.fetch_remote_product_config("cn")
        self.assertIsNotNone(parsed)
        path = M.catalog_cache_path("cn")
        self.assertTrue(os.path.exists(path), path)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), payload)
        # 原子写不残留 .tmp；目录里只有这一份存档。
        self.assertEqual(os.listdir(self._dir), ["product-config-cn.json"])

    def test_a_failed_fetch_dumps_nothing(self):
        def fake_urlopen(req, timeout=10, proxy=None):
            raise OSError("boom")

        P.wb_accounts.urlopen = fake_urlopen
        self.assertIsNone(M.fetch_remote_product_config("cn"))
        self.assertEqual(os.listdir(self._dir), [])

    def test_persist_failure_is_silent(self):
        """写盘失败不是取数失败：不抛异常，也不留下半份文件。"""
        blocker = os.path.join(self._dir, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        M.CATALOG_CACHE_DIR = blocker      # makedirs 必败（父路径是文件）
        M.persist_remote_catalog("cn", config_payload(["glm-5.3"]))
        self.assertTrue(os.path.isfile(blocker))
        self.assertEqual(os.listdir(self._dir), ["blocker"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
