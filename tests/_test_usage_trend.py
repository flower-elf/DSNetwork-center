"""用量趋势的分桶：按天 / 按小时、零填充、realm 过滤与窗口。

没有调用的桶也返回 0，桶起点是当地自然日或整点的秒级时间戳；客户端主动中断的行不计入 requests / errors；窗口与粒度不同时不共用缓存。
"""

import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-trend-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)


import wb_usage as U
import wb_proxy as proxy
import wb_usage_trend as T

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


now = time.time()
_lt = time.localtime(now)
TODAY0 = int(time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 0, 0, 0, 0, 0, -1)))
# 用例把「今天」的行放在 01:00：真实运行到零点后一小时内，这些时间点还在
# 未来，会被窗口过滤掉。把进程时钟固定在当天 03:00，断言与运行时刻无关。
time.time = lambda: TODAY0 + 3 * 3600
YESTERDAY0 = int(U._local_midnight(days_back=1))
DAY3 = int(U._local_midnight(days_back=3))


def row(at, realm="intl", total_tokens=100, credit=0.5, error=False, outcome=None):
    item = {"at": at, "model": "m", "realm": realm, "total_tokens": total_tokens,
            "credit": credit, "account": "acct-a"}
    if error:
        item["error"] = True
        item["status"] = 500
        item["outcome"] = outcome or "failed"
    elif outcome:
        item["outcome"] = outcome
    return item


rows = [
    row(DAY3 + 3600),                                     # 三天前
    row(YESTERDAY0 + 7200, total_tokens=40, credit=0.2),  # 昨天
    row(YESTERDAY0 + 9000, total_tokens=10, credit=0.1, error=True),   # 昨天的失败
    row(TODAY0 + 3600, total_tokens=500, credit=1.5),     # 今天 01:00
    row(TODAY0 + 3700, realm="cn", total_tokens=20, credit=0.05),
    row(TODAY0 + 7300, total_tokens=999, credit=9.0, outcome="client_aborted"),
]
with io.open(proxy.USAGE_LOG, "w", encoding="utf-8") as fh:
    for item in rows:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")

print("[1] 按天分桶：最近 N 天含今天，空桶补 0")

trend = T.usage_trend(days=3, realm="all", ttl=0)
check("粒度是按天", trend["granularity"] == "day", trend["granularity"])
check("返回 N 个桶", len(trend["buckets"]) == 3, len(trend["buckets"]))
check("桶按时间升序", [b["start"] for b in trend["buckets"]] == sorted(
    b["start"] for b in trend["buckets"]), [b["start"] for b in trend["buckets"]])
check("最后一个桶是今天",
      trend["buckets"][-1]["start"] == TODAY0, trend["buckets"][-1])
check("桶的起点是当地零点",
      trend["buckets"][0]["start"] == int(U._local_midnight(days_back=2)),
      trend["buckets"][0])
check("窗口从第一个桶起算", trend["window"]["since"] == trend["buckets"][0]["start"], trend["window"])
check("标签是月-日", trend["buckets"][-1]["label"] == time.strftime("%m-%d", time.localtime(TODAY0)),
      trend["buckets"][-1]["label"])

by_day = {b["start"]: b for b in trend["buckets"]}
today_bucket = by_day[TODAY0]
yesterday_bucket = by_day[YESTERDAY0]
check("今天的请求数", today_bucket["requests"] == 2, today_bucket)
check("今天不含主动中断的行(它只有 token)", today_bucket["total_tokens"] == 520,
      today_bucket)
check("今天的积分合计", abs(today_bucket["credit"] - 1.55) < 1e-9, today_bucket)
check("昨天成功一条", yesterday_bucket["requests"] == 1, yesterday_bucket)
check("昨天的失败计进 errors", yesterday_bucket["errors"] == 1, yesterday_bucket)
check("昨天 token 合计", yesterday_bucket["total_tokens"] == 50, yesterday_bucket)
check("排序在前的桶是空的（三天前那天不在窗口里）",
      len([b for b in trend["buckets"] if b["requests"] == 0 and b["errors"] == 0]) == 1,
      trend["buckets"])

print()
print("[2] 今天按小时分桶")

hours = T.usage_trend(range="today", realm="all", ttl=0)
check("粒度是按小时", hours["granularity"] == "hour", hours["granularity"])
check("固定 24 个桶", len(hours["buckets"]) == 24, len(hours["buckets"]))
check("第一个桶是今天零点", hours["buckets"][0]["start"] == TODAY0, hours["buckets"][0])
check("桶间隔一小时",
      [b["start"] - hours["buckets"][0]["start"] for b in hours["buckets"]][:3] == [0, 3600, 7200])
check("标签是整点", hours["buckets"][1]["label"] == time.strftime("%H:00", time.localtime(TODAY0 + 3600)),
      hours["buckets"][1]["label"])
check("窗口到明天零点", hours["window"]["until"] == TODAY0 + 86400, hours["window"])
by_hour = {b["start"]: b for b in hours["buckets"]}
check("01 点那桶有两条成功请求", by_hour[TODAY0 + 3600]["requests"] == 2,
      by_hour[TODAY0 + 3600])
check("02 点那桶被补成 0",
      by_hour[TODAY0 + 7200]["requests"] == 0 and by_hour[TODAY0 + 7200]["total_tokens"] == 0,
      by_hour.get(TODAY0 + 7200))
check("02 点那桶里没有主动中断的 token",
      all(b["total_tokens"] == 0 for b in hours["buckets"] if b["start"] > TODAY0 + 7200),
      [b for b in hours["buckets"] if b["total_tokens"]])
check("零填充的桶字段齐全",
      all(set(b) == {"start", "label", "requests", "errors", "total_tokens", "credit"}
          for b in hours["buckets"]), hours["buckets"][0])

print()
print("[3] realm 过滤")

cn = T.usage_trend(range="today", realm="cn", ttl=0)
check("国内版只剩国内版那一条",
      sum(b["requests"] for b in cn["buckets"]) == 1, sum(b["requests"] for b in cn["buckets"]))
check("国内版 token 只算自己的",
      sum(b["total_tokens"] for b in cn["buckets"]) == 20,
      sum(b["total_tokens"] for b in cn["buckets"]))
intl = T.usage_trend(range="today", realm="intl", ttl=0)
check("国际版看不到国内版那一条",
      sum(b["total_tokens"] for b in intl["buckets"]) == 500,
      sum(b["total_tokens"] for b in intl["buckets"]))
check("realm 回显在结果里", intl["realm"] == "intl", intl["realm"])
check("realm=all 回显 all",
      T.usage_trend(range="today", realm="all", ttl=0)["realm"] == "all")

print()
print("[4] week / month / custom 窗口")

week = T.usage_trend(range="week", realm="all", ttl=0)
check("week 的粒度是按天", week["granularity"] == "day", week["granularity"])
check("week 从当地周一起算", week["buckets"][0]["start"] == int(U.range_window("week")[0]),
      week["buckets"][0])
check("week 的桶覆盖到今天", week["buckets"][-1]["start"] == TODAY0, week["buckets"][-1])
month = T.usage_trend(range="month", realm="all", ttl=0)
check("month 从当地 1 号起算", month["buckets"][0]["start"] == int(U.range_window("month")[0]),
      month["buckets"][0])

custom = T.usage_trend(range="custom", since=YESTERDAY0, until=TODAY0 + 100,
                           realm="all", ttl=0)
check("custom 只覆盖给定的日期", len(custom["buckets"]) == 2, custom["buckets"])
check("custom 的窗口原样回显", custom["window"] == {"since": YESTERDAY0, "until": TODAY0 + 100},
      custom["window"])
check("custom 里昨天的合计正确",
      custom["buckets"][0]["requests"] == 1 and custom["buckets"][0]["errors"] == 1,
      custom["buckets"][0])
wide = T.usage_trend(range="custom", since=YESTERDAY0, until=TODAY0 + 4000,
                        realm="all", ttl=0)
check("窗口上界之后的记录不算进来",
      custom["buckets"][1]["total_tokens"] == 0, custom["buckets"][1])
check("放宽上界后今天 01:00 的两条算进来",
      wide["buckets"][1]["total_tokens"] == 520 and wide["buckets"][1]["requests"] == 2,
      wide["buckets"][1])
open_end = T.usage_trend(range="custom", since=YESTERDAY0, realm="all", ttl=0)
check("custom 只给起点时算到最近一天",
      len(open_end["buckets"]) == 2 and open_end["buckets"][-1]["start"] == TODAY0,
      open_end["buckets"])

print()
print("[5] 天数与粒度的边界")

check("days 超过 90 夹到 90", len(T.usage_trend(days=500, ttl=0)["buckets"]) == 90,
      len(T.usage_trend(days=500, ttl=0)["buckets"]))
check("days=0 至少一天", len(T.usage_trend(days=0, ttl=0)["buckets"]) == 1)
check("days 传字符串也认", len(T.usage_trend(days="2", ttl=0)["buckets"]) == 2)
check("不传任何参数时默认两周", len(T.usage_trend(ttl=0)["buckets"]) == 14)
check("未知 range 时用默认值", len(T.usage_trend(range="last-year", ttl=0)["buckets"]) == 14)
check("range=all 给满上限", len(T.usage_trend(range="all", ttl=0)["buckets"]) == 90)
check("days 优先于 range", T.usage_trend(days=3, range="today", ttl=0)["granularity"] == "day")

print()
print("[6] 缓存按窗口与粒度分开")

cached_week = T.usage_trend(range="week", realm="all", ttl=60)
cached_today = T.usage_trend(range="today", realm="all", ttl=60)
check("同一次调用里两个粒度不串味",
      cached_week["granularity"] == "day" and cached_today["granularity"] == "hour",
      (cached_week["granularity"], cached_today["granularity"]))
check("缓存过的按天合计不变",
      sum(b["total_tokens"] for b in T.usage_trend(range="week", realm="all", ttl=60)["buckets"])
      == sum(b["total_tokens"] for b in cached_week["buckets"]))

print()
print("[7] HTTP 查询串到趋势的接线")


class FakeRequest(object):
    _get_usage_trend = proxy.Handler._get_usage_trend

    def _authorized(self):
        return True

    def _json(self, status, payload):
        return status, payload


status, payload = FakeRequest()._get_usage_trend({"days": ["3"], "realm": ["all"]})
check("days 查询串被接上", status == 200 and len(payload["buckets"]) == 3,
      (status, len(payload.get("buckets", []))))
status, payload = FakeRequest()._get_usage_trend({"range": ["today"], "realm": ["cn"]})
check("range=today 查询串按小时",
      payload["granularity"] == "hour" and payload["realm"] == "cn", payload["granularity"])
status, payload = FakeRequest()._get_usage_trend({"range": ["custom"], "since": ["%d" % DAY3],
                                                  "until": ["%d" % TODAY0]})
check("custom 的 since/until 查询串被接上", len(payload["buckets"]) == 4,
      len(payload["buckets"]))
check("没有参数时也能返回", len(FakeRequest()._get_usage_trend({})[1]["buckets"]) == 14)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
