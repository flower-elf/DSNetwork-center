"""请求日志的多维筛选：realm / key / status / model / account / ip / 时间窗与分页。

返回筛选后的总数与页数，每行补 key_name；旧行缺少 key_id / ip / status 时照旧读出，按 Key 筛选会漏掉它们；同一条件连打两次也正常返回。
"""

import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-recent-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy as proxy
import wb_usage as U
import wb_usage_recent as R
import wb_settings

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


wb_settings.set_api_keys(os.environ["ACCOUNTS_DIR"], [
    {"id": "k1", "name": "主力 Key", "key": "key-alpha"},
    {"id": "k2", "name": "备份 Key", "key": "key-beta"},
])

now = time.time()
_lt = time.localtime(now)
TODAY0 = time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 0, 0, 0, 0, 0, -1))


def row(at, model, realm="intl", key_id="k1", ip="10.0.0.5", account="acct-a",
        total_tokens=100, status=200, error=False):
    item = {
        "at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
        "model": model, "stream": False, "outcome": "failed" if error else "completed",
        "total_tokens": total_tokens, "credit": 0.1,
        "realm": realm, "key_id": key_id, "ip": ip, "api": "chat",
        "status": status, "account": account,
    }
    if error:
        item["error"] = True
        item["message"] = "boom"
    return item


rows = [
    # 昨天的一条，用来验证时间窗
    row(TODAY0 - 3600, "deepseek-v4.1-flash", key_id="k1", ip="10.0.0.5",
        account="acct-a", status=200),
    row(TODAY0 + 100, "deepseek-v4.1-flash", key_id="k1", ip="10.0.0.5",
        account="acct-a", status=200),
    row(TODAY0 + 200, "gpt-6-astra", realm="cn", key_id="panel", ip="127.0.0.1",
        account="acct-b", status=200),
    row(TODAY0 + 300, "gpt-6-astra", realm="cn", key_id="k2", ip="192.168.1.9",
        account="acct-b", status=500, error=True),
    row(TODAY0 + 400, "DeepSeek-V4.1-Flash", key_id="gone", ip="10.0.0.7",
        account="acct-a", status=200),
    # 旧行：没有 key_id / ip / status 字段
    {"at": TODAY0 + 500, "iso": "old", "model": "legacy-model", "realm": "intl",
     "outcome": "completed", "total_tokens": 7, "account": "acct-a"},
]
with io.open(proxy.USAGE_LOG, "w", encoding="utf-8") as fh:
    for item in rows:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")

print("[1] 不带筛选：原有字段 + 总数与页数")

page = R.recent_usage(100)
check("所有行都在", page["total"] == 6, page["total"])
check("返回字段齐全",
      all(k in page for k in ("total", "page", "limit", "total_pages", "pages", "rows")),
      sorted(page))
check("pages 与 total_pages 一致", page["pages"] == page["total_pages"] == 1, page)
check("最新的行在前", page["rows"][0]["model"] == "legacy-model", page["rows"][0])
check("每行都有 key_name", all("key_name" in r for r in page["rows"]))
check("旧行不被丢掉", any(r["model"] == "legacy-model" for r in page["rows"]))

print()
print("[2] key_name 按当前 Key 列表解析")

by_model = {r["model"]: r for r in page["rows"]}
check("配置里的 Key 用它的名字", by_model["deepseek-v4.1-flash"]["key_name"] == "主力 Key",
      by_model["deepseek-v4.1-flash"])
check("面板测试台的流量标成面板测试台",
      by_model["gpt-6-astra"]["key_name"] in ("面板测试台", "备份 Key"), by_model["gpt-6-astra"])
check("已删除的 Key 有明确标注",
      [r["key_name"] for r in page["rows"] if r.get("key_id") == "gone"] == ["已删除的 Key"])
check("没有 Key 的旧行 key_name 是空串",
      by_model["legacy-model"]["key_name"] == "", by_model["legacy-model"])

print()
print("[3] realm 筛选")

intl = R.recent_usage(100, realm="intl")
check("国际版只算国际版的行", intl["total"] == 4, intl["total"])
check("国际版里没有国内版模型",
      all(r["realm"] == "intl" for r in intl["rows"]), intl["rows"])
cn = R.recent_usage(100, realm="cn")
check("国内版只算国内版的行", cn["total"] == 2, cn["total"])
check("realm=all 等于不筛", R.recent_usage(100, realm="all")["total"] == 6)

print()
print("[4] key / status / model / account / ip 筛选")

keyed = R.recent_usage(100, filters=U.usage_filters(key="k1"))
check("按 Key 筛选只留该 Key 的行", keyed["total"] == 2, keyed["total"])
check("按 Key 筛选出的行 Key 名正确",
      all(r["key_name"] == "主力 Key" for r in keyed["rows"]), keyed["rows"])
panel = R.recent_usage(100, filters=U.usage_filters(key="panel"))
check("panel 是合法的筛选值", panel["total"] == 1 and panel["rows"][0]["ip"] == "127.0.0.1",
      panel)
check("按 Key 筛选会漏掉没有 key_id 的旧行",
      all(r["model"] != "legacy-model" for r in R.recent_usage(
          100, filters=U.usage_filters(key="k1"))["rows"]))

fails = R.recent_usage(100, filters=U.usage_filters(status="fail"))
check("status=fail 只留失败行", fails["total"] == 1, (fails["total"], fails["rows"]))
check("失败行是 500 那条",
      [r["status"] for r in fails["rows"]] == [500], fails["rows"])
oks = R.recent_usage(100, filters=U.usage_filters(status="ok"))
check("status=ok 排除失败行", oks["total"] == 5, oks["total"])
check("status 取值非法时当作不筛",
      R.recent_usage(100, filters=U.usage_filters(status="maybe"))["total"] == 6)

by_model = R.recent_usage(100, filters=U.usage_filters(model="astra"))
check("model 是子串匹配", by_model["total"] == 2, by_model["total"])
check("model 匹配不区分大小写",
      R.recent_usage(100, filters=U.usage_filters(model="deepseek"))["total"] == 3,
      R.recent_usage(100, filters=U.usage_filters(model="deepseek"))["total"])

by_acct = R.recent_usage(100, filters=U.usage_filters(account="acct-b"))
check("按账号 uid 筛选", by_acct["total"] == 2, by_acct["total"])
by_ip = R.recent_usage(100, filters=U.usage_filters(ip="192.168."))
check("按 IP 子串筛选", by_ip["total"] == 1, by_ip["total"])
check("IP 子串命中整行", by_ip["rows"][0]["key_id"] == "k2", by_ip["rows"])
# 子串可以在值的中间或末尾出现：预筛只要求行里有 ip 字段。
tail_ip = R.recent_usage(100, filters=U.usage_filters(ip="0.0.5"))
check("IP 后缀子串也能筛到", tail_ip["total"] == 2 and len(tail_ip["rows"]) == 2,
      (tail_ip["total"], len(tail_ip["rows"])))
check("IP 后缀子串总数与行数一致", tail_ip["total"] == len(tail_ip["rows"]), tail_ip)
mid_ip = R.recent_usage(100, filters=U.usage_filters(ip="0.0."))
check("IP 中段子串筛到四行", mid_ip["total"] == 4, mid_ip["total"])

print()
print("[5] 多个条件是并且的关系")

both = R.recent_usage(100, filters=U.usage_filters(
    realm="cn", status="fail", model="gpt"))
check("realm + status + model 同时生效", both["total"] == 1, both["total"])
none = R.recent_usage(100, filters=U.usage_filters(realm="intl", key="k2"))
check("没有交集就是 0 条", none["total"] == 0, none["total"])
check("0 条时 rows 为空且页数为 1", none["rows"] == [] and none["pages"] == 1, none)

print()
print("[6] 时间窗")

today = R.recent_usage(100, filters=U.usage_filters(range="today"))
check("range=today 排除昨天的行", today["total"] == 5, today["total"])
custom = R.recent_usage(100, filters=U.usage_filters(
    range="custom", since=TODAY0 + 150, until=TODAY0 + 350))
check("custom 窗口闭区间", custom["total"] == 2, custom["total"])
check("custom 窗口内的行按时间倒序",
      [r["status"] for r in custom["rows"]] == [500, 200], custom["rows"])

print()
print("[7] 分页")

page1 = R.recent_usage(2, filters=U.usage_filters(status="ok"))
check("第一页取最新的两条", page1["total"] == 5 and page1["pages"] == 3, page1)
check("第一页行数等于 limit", len(page1["rows"]) == 2, page1["rows"])
page3 = R.recent_usage(2, filters=U.usage_filters(status="ok"), page=3)
check("最后一页只剩一条", len(page3["rows"]) == 1, page3["rows"])
check("页码被夹在范围内", page3["page"] == 3, page3["page"])
check("超出范围的页码回到最后一页",
      R.recent_usage(2, filters=U.usage_filters(status="ok"), page=99)["page"] == 3)
check("第二页与第一页不重复",
      not ({r["at"] for r in page1["rows"]} & {r["at"] for r in page3["rows"]}))

print()
print("[8] 同一条件连打两次")

filters = U.usage_filters(realm="all", status="fail", range="custom",
                              since=TODAY0 - 86400, until=None)
first = R.recent_usage(20, filters=filters)
second = R.recent_usage(20, filters=filters)
check("第二次仍然返回同样的总数", first["total"] == second["total"] == 1,
      (first["total"], second["total"]))
check("第二次仍然带着行", len(second["rows"]) == 1, second["rows"])

print()
print("[9] HTTP 查询串到筛选条件的接线")


class FakeRequest(object):
    _get_usage_recent = proxy.Handler._get_usage_recent
    headers = {}

    def _authorized(self):
        return True

    def _panel_ok(self):
        return False

    def _json(self, status, payload):
        return status, payload


query = {"key": ["k1"], "status": ["ok"], "model": ["deepseek"], "account": ["acct-a"],
         "ip": ["10.0.0.5"], "range": ["today"], "page": ["1"], "limit": ["20"],
         "realm": ["all"]}
status, payload = FakeRequest()._get_usage_recent(query)
check("查询串被逐项接上", status == 200 and payload["total"] == 1,
      (status, payload.get("total")))
check("limit 被读进来", payload["limit"] == 20, payload["limit"])
status, payload = FakeRequest()._get_usage_recent({"realm": ["intl"], "limit": ["abc"]})
check("limit 非法时改用 100", payload["limit"] == 100, payload["limit"])
check("未给条件时返回全部", payload["total"] == 4, payload["total"])

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
