"""测 token 时间序列与积分历史的统计通路（M4 D5）。不用上游凭据，也不出网。用 python tests/run_all.py _test_usage_timeseries 运行。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="usage-series-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy
import wb_usage as U


def row(at, total_tokens=100, credit=0.0, error=False, outcome="completed",
        realm="intl"):
    return {"at": at, "model": "m1", "realm": realm, "total_tokens": total_tokens,
            "prompt_tokens": 60, "completion_tokens": 30, "reasoning_tokens": 10,
            "cached_tokens": 5, "credit": credit, "error": error,
            "outcome": outcome}


class TimeseriesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.dir.name, "usage.jsonl")
        self.patch = mock.patch.object(wb_proxy, "USAGE_LOG", self.log)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.dir.cleanup)

    def write(self, rows):
        with open(self.log, "w", encoding="utf-8") as fh:
            for item in rows:
                fh.write(json.dumps(item) + "\n")

    def test_bucket_size_auto_scales(self):
        now = time.time()
        one_hour = U.usage_timeseries(range="custom", since=now - 3600, until=now)
        self.assertEqual(one_hour["bucket_seconds"], 60)
        three_days = U.usage_timeseries(range="custom", since=now - 3 * 86400, until=now)
        self.assertEqual(three_days["bucket_seconds"], 3600)
        thirty_days = U.usage_timeseries(range="custom", since=now - 30 * 86400, until=now)
        self.assertEqual(thirty_days["bucket_seconds"], 86400)
        explicit = U.usage_timeseries(range="custom", since=now - 3600, until=now,
                                             bucket_seconds=120)
        self.assertEqual(explicit["bucket_seconds"], 120)

    def test_aggregation_counts_tokens_and_credit(self):
        now = time.time()
        self.write([
            row(now - 150, total_tokens=100, credit=2.0),
            row(now - 120, total_tokens=0, credit=1.0, error=True,
                outcome="failed"),
            row(now - 90, total_tokens=0, credit=99.0, outcome="client_aborted"),
            row(now - 60, total_tokens=50, credit=0.5),
        ])
        result = U.usage_timeseries(range="custom", since=now - 180, until=now,
                                           bucket_seconds=60)
        totals = {
            "requests": sum(b["requests"] for b in result["series"]),
            "errors": sum(b["errors"] for b in result["series"]),
            "tokens": sum(b["total_tokens"] for b in result["series"]),
            "credit": sum(b["credit"] for b in result["series"]),
        }
        self.assertEqual(totals["requests"], 2)
        # usage_snapshot 把所有非 completed 行都算作错误，包括客户端中止；
        # 时间序列沿用同一定义。
        self.assertEqual(totals["errors"], 2)
        self.assertEqual(totals["tokens"], 150)
        self.assertAlmostEqual(totals["credit"], 3.5)

    def test_realm_filter(self):
        now = time.time()
        self.write([
            row(now - 30, total_tokens=100, realm="cn"),
            row(now - 20, total_tokens=200, realm="intl"),
        ])
        cn = U.usage_timeseries(realm="cn", range="custom", since=now - 60, until=now)
        self.assertEqual(sum(b["total_tokens"] for b in cn["series"]), 100)
        all_realms = U.usage_timeseries(realm="all", range="custom", since=now - 60,
                                               until=now)
        self.assertEqual(sum(b["total_tokens"] for b in all_realms["series"]), 300)

    def credits_window(self, rows):
        """写日志 + 清时间序列状态（切片/扫描两条路径都从零起）。"""
        self.write(rows)
        U._series_cache.clear()
        U._series_state.clear()

    def test_credits_top50_ties_keep_log_order(self):
        # 70 条 at 完全相同的计费行跨第 50 名边界：选择必须是「at 倒序、
        # 相同 at 保持日志顺序」——丢掉的只能是同 at 组里日志靠后的那些。
        # 窗口从昨天本地午夜起：切片路径成立，行也全在过去。
        midnight = U._local_midnight(time.time() - 86400)
        T = midnight + 3600.0
        rows = [row(T, credit=100.0 + i) for i in range(70)]
        rows += [row(T + 600, credit=200.0 + i) for i in range(5)]
        rows += [row(T - 600, credit=1.0 + i) for i in range(20)]
        self.credits_window(rows)
        # 规范：Python 稳定排序（at 倒序 + 同 at 日志顺序）的前 50 条。
        want = sorted([r for r in rows if r["credit"] > 0],
                      key=lambda r: r["at"], reverse=True)[:50]
        got = U.usage_timeseries(realm="all", range="custom", since=midnight,
                                        until=None, ttl=0)
        self.assertEqual([(c["at"], c["credit"]) for c in got["credits"]],
                         [(r["at"], r["credit"]) for r in want])
        # 第 50 名落在同 at 组内部：留下的正是组里日志靠前的 45 条。
        self.assertEqual(len(got["credits"]), 50)
        self.assertEqual(got["credits"][5]["credit"], 100.0)
        self.assertEqual(got["credits"][-1]["credit"], 144.0)
        # 载荷的字段与键序是响应字节的一部分。
        self.assertEqual(list(got["credits"][0].keys()),
                         ["at", "iso", "model", "account", "credit", "total_tokens"])

    def test_credits_scan_builds_only_the_top50_entries(self):
        # 计数桩：整段扫描只为最终进前 50 的行建载荷。前 50 行就是窗口里
        # 最新的 50 条，之后的 1950 行都进不了堆——朴素做法会把 2000 条全部
        # 建成字典再排序切片。
        midnight = U._local_midnight(time.time() - 86400)
        T = midnight + 7200.0
        rows = [row(T + 1000 - i, credit=500.0 + i) for i in range(50)]
        rows += [row(T - i, credit=1.0 + i) for i in range(1950)]
        self.credits_window(rows)
        os.environ["WB_USAGE_DAY_BUCKETS"] = "0"  # 强制整段扫描路径
        try:
            with mock.patch.object(U, "_series_credit_entry",
                                   wraps=U._series_credit_entry) as entry:
                got = U.usage_timeseries(realm="all", range="custom",
                                                since=midnight, until=None, ttl=0)
        finally:
            os.environ.pop("WB_USAGE_DAY_BUCKETS", None)
        self.assertEqual(entry.call_count, 50)
        self.assertEqual([c["credit"] for c in got["credits"]],
                         [500.0 + i for i in range(50)])

    def test_credits_slice_and_scan_are_byte_identical(self):
        # 同一份日志、同一个窗口：日桶切片与整段扫描（关掉日桶）逐字节一致。
        midnight = U._local_midnight(time.time() - 86400)
        T = midnight + 3600.0
        rows = [row(T + i * 3, credit=10.0 + i) for i in range(80)]
        rows += [row(T + 100, credit=90.0) for _ in range(30)]  # 同 at 一组
        self.credits_window(rows)
        os.environ["WB_USAGE_DAY_BUCKETS"] = "0"
        try:
            U._series_cache.clear()
            U._series_state.clear()
            scan = U.usage_timeseries(realm="all", range="custom",
                                             since=midnight, until=None, ttl=0)
        finally:
            os.environ.pop("WB_USAGE_DAY_BUCKETS", None)
        U._series_cache.clear()
        U._series_state.clear()
        sliced = U.usage_timeseries(realm="all", range="custom",
                                           since=midnight, until=None, ttl=0)
        self.assertEqual(json.dumps(sliced["credits"], ensure_ascii=False),
                         json.dumps(scan["credits"], ensure_ascii=False))
        self.assertEqual(json.dumps(sliced["series"], ensure_ascii=False),
                         json.dumps(scan["series"], ensure_ascii=False))


class TimeseriesRouteTests(unittest.TestCase):
    def make_handler(self, authorized=True):
        class Handler(wb_proxy.Handler):
            def __init__(self):
                self.captured = None

            def _authorized(self):
                return authorized

            def _json(self, code, obj):
                self.captured = (code, obj)

        return Handler()

    def test_route_parses_realm_range_and_bucket(self):
        captured = {}

        def fake_series(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "series": []}

        handler = self.make_handler()
        with mock.patch.object(U, "usage_timeseries", side_effect=fake_series):
            handler._get_usage_timeseries({
                "realm": ["cn"], "range": ["custom"],
                "since": ["1000"], "until": ["2000"], "bucket": ["120"]})
        code, _obj = handler.captured
        self.assertEqual(code, 200)
        self.assertEqual(captured["realm"], "cn")
        self.assertEqual(captured["range"], "custom")
        self.assertEqual(captured["since"], "1000")
        self.assertEqual(captured["until"], "2000")
        self.assertEqual(captured["bucket_seconds"], 120)

    def test_route_requires_auth(self):
        handler = self.make_handler(authorized=False)
        handler._get_usage_timeseries({})
        self.assertIsNone(handler.captured)


if __name__ == "__main__":
    unittest.main()
