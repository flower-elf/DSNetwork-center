"""每行请求记录都带审计字段 key_id / ip / status / api，字段在请求开始时固定，迟到的写入仍沿用原值。

用线程内的审计上下文模拟客户端读完之后才写记录的这段间隔；旧行没有这些字段时读取方按缺失处理，不允许报错。
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-audit-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy as proxy
import wb_usage as U
import wb_usage_recent as R

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def rows():
    out = []
    with open(proxy.USAGE_LOG, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def reset_log():
    with open(proxy.USAGE_LOG, "w", encoding="utf-8") as fh:
        fh.write("")


class FakeHandler(object):
    """只带 _begin_audit 需要的几个属性。"""

    _begin_audit = proxy.Handler._begin_audit

    def __init__(self, panel=False, key_entry=None, ip="10.1.2.3"):
        self._panel = panel
        self.key_entry = key_entry
        self.client_address = (ip, 51000)
        self.audit = None

    def _panel_ok(self):
        return self._panel


print("[1] 请求开始时固定下来的字段")

panel = FakeHandler(panel=True, key_entry={"id": "should-not-win", "name": "x"})
audit = panel._begin_audit("chat")
check("面板会话的 key_id 是 panel", audit["key_id"] == "panel", audit)
check("来源地址取自客户端", audit["ip"] == "10.1.2.3", audit)
check("接口名被记下", audit["api"] == "chat", audit)
check("状态码留到真正写出时再定", audit["status"] is None, audit)
check("审计上下文同时绑定到当前线程", U.current_audit() is audit, U.current_audit())

keyed = FakeHandler(key_entry={"id": "k-7", "name": "客户端 A"})
check("普通 Key 请求记下 Key 的 id", keyed._begin_audit("messages")["key_id"] == "k-7")
unkeyed = FakeHandler()
check("没有 Key 时记空串", unkeyed._begin_audit("responses")["key_id"] == "")
BareHandler = type("Bare", (object,), {"_panel_ok": lambda self: False, "key_entry": None,
                                       "client_address": None, "audit": None})
check("client_address 缺失时来源地址为空",
      proxy.Handler._begin_audit(BareHandler(), "chat")["ip"] == "")

print()
print("[2] 路径到 api 名的映射")

for path, want in (("/v1/chat/completions", "chat"), ("/chat/completions", "chat"),
                   ("/v1/completions", "completions"), ("/completions", "completions"),
                   ("/v1/responses", "responses"), ("/responses", "responses"),
                   ("/v1/messages", "messages"), ("/messages", "messages")):
    check("%s -> %s" % (path, want), U.chat_api_name(path) == want, U.chat_api_name(path))
check("未知路径按 chat 处理", U.chat_api_name("/v1/chat/other") == "chat")

print()
print("[3] record_usage 写出新字段")

reset_log()
U.record_usage("m-1", {"total_tokens": 10}, stream=False, elapsed_ms=12,
                   audit={"key_id": "k-1", "ip": "1.2.3.4", "api": "chat", "status": None})
row = rows()[-1]
check("key_id 写入", row.get("key_id") == "k-1", row)
check("ip 写入", row.get("ip") == "1.2.3.4", row)
check("api 写入", row.get("api") == "chat", row)
check("成功行状态码是 200", row.get("status") == 200, row)

reset_log()
U.bind_audit(None)          # 每个请求开始时由 handle_one_request 清空
U.record_usage("m-2", {"total_tokens": 1})
row = rows()[-1]
check("没有审计上下文时字段仍在", (row.get("key_id"), row.get("ip"), row.get("api")) == ("", "", ""), row)
check("没有审计上下文时状态码仍是 200", row.get("status") == 200, row)

print()
print("[4] record_error 写出新字段且保留自己的状态码")

reset_log()
U.record_error("m-3", 429, "rate limited", elapsed_ms=5,
                   audit={"key_id": "panel", "ip": "127.0.0.1", "api": "messages"})
row = rows()[-1]
check("错误行的 key_id 写入", row.get("key_id") == "panel", row)
check("错误行的 ip 写入", row.get("ip") == "127.0.0.1", row)
check("错误行的 api 写入", row.get("api") == "messages", row)
check("错误行保留 HTTP 状态码", row.get("status") == 429, row)

reset_log()
U.bind_audit(None)
U.record_error("m-4", 502, "boom")
row = rows()[-1]
check("没有审计上下文时字段仍存在",
      (row.get("key_id"), row.get("ip"), row.get("api")) == ("", "", ""), row)

print()
print("[5] 迟到的写入沿用请求开始时的上下文")

reset_log()
late = FakeHandler(key_entry={"id": "k-late", "name": "迟到"})
late._begin_audit("responses")
# 模拟代跑的多轮请求：客户端早已收到响应，这里才写出记录。
U.record_usage("m-5", {"total_tokens": 3}, stream=True,
                   elapsed_ms=900, account="acct-1")
row = rows()[-1]
check("迟到的记录带着原 Key", row.get("key_id") == "k-late", row)
check("迟到的记录带着原来源地址", row.get("ip") == "10.1.2.3", row)
check("迟到的记录带着原接口名", row.get("api") == "responses", row)

reset_log()
# 下一个请求开始：handle_one_request 清掉上一个请求的上下文
U.bind_audit(None)
U.record_usage("m-6", {"total_tokens": 3})
row = rows()[-1]
check("绑定被清掉之后不再沿用上一个请求",
      (row.get("key_id"), row.get("ip"), row.get("api")) == ("", "", ""), row)

print()
print("[6] 旧行没有这些字段也不能出错")

old = {"at": time.time() - 10, "model": "m-old", "total_tokens": 5, "error": True,
       "status": 500, "account": "acct-old", "realm": "intl"}
reset_log()
with open(proxy.USAGE_LOG, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(old, ensure_ascii=False) + "\n")
page = R.recent_usage(10)
check("旧行照旧读得出来", page["rows"][0]["model"] == "m-old", page["rows"])
check("旧行补上的 key_name 是空串", page["rows"][0].get("key_name") == "", page["rows"][0])

print()
print("[7] /usage/analytics 的 keys 分组")

import wb_settings

wb_settings.set_api_keys(os.environ["ACCOUNTS_DIR"], [
    {"id": "k1", "name": "主力 Key", "key": "key-alpha"},
    {"id": "k2", "name": "闲置 Key", "key": "key-beta"},
    {"id": "gone", "name": "旧 Key", "key": "key-gone"},
])
# 先配置再删除：软删除留下墓碑，历史花费仍然按名字可查。
wb_settings.set_api_keys(os.environ["ACCOUNTS_DIR"], [
    {"id": "k1", "name": "主力 Key", "key": "key-alpha"},
    {"id": "k2", "name": "闲置 Key", "key": "key-beta"},
], delete_ids=["gone"])
U.bind_audit(None)
reset_log()
stamp = time.time()
with open(proxy.USAGE_LOG, "w", encoding="utf-8") as fh:
    for item in (
        {"at": stamp - 5, "model": "m-a", "realm": "intl", "key_id": "k1", "ip": "10.0.0.1",
         "api": "chat", "status": 200, "outcome": "completed", "total_tokens": 100,
         "credit": 0.5, "prompt_tokens": 80, "cached_tokens": 40, "completion_tokens": 20},
        {"at": stamp - 4, "model": "m-a", "realm": "intl", "key_id": "k1", "ip": "10.0.0.1",
         "api": "messages", "status": 500, "outcome": "failed", "error": True,
         "total_tokens": 30, "credit": 0.1},
        {"at": stamp - 3, "model": "m-b", "realm": "intl", "key_id": "panel",
         "ip": "127.0.0.1", "api": "chat", "status": 200, "outcome": "completed",
         "total_tokens": 7, "credit": 0.02},
        {"at": stamp - 2, "model": "m-b", "realm": "intl", "key_id": "gone",
         "ip": "10.0.0.2", "api": "chat", "status": 200, "outcome": "completed",
         "total_tokens": 11, "credit": 0.03},
        {"at": stamp - 1.5, "model": "m-b", "realm": "intl", "key": "ghost",
         "ip": "10.0.0.3", "api": "chat", "status": 200, "outcome": "completed",
         "total_tokens": 4, "credit": 0.04},
        {"at": stamp - 1, "model": "m-c", "realm": "intl", "outcome": "completed",
         "total_tokens": 5, "credit": 0.01},
    ):
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")

analytics = U.compute_usage_analytics(ttl=0, realm="all")
check("analytics 带 keys 列表", isinstance(analytics.get("keys"), list), sorted(analytics))
by_key = {k["key_id"]: k for k in analytics["keys"]}
check("用过的 Key 在列", "k1" in by_key and "panel" in by_key, sorted(by_key))
check("闲置的 Key 也在列表里且为零", by_key.get("k2", {}).get("window", {}).get("total_tokens") == 0,
      by_key.get("k2"))
check("配置后删除的 Key 标注为已删除",
      by_key["gone"]["key_name"] == "已删除的 Key", by_key["gone"]["key_name"])
check("名单外无法命名的 Key 折进未知分组",
      by_key[U.KEY_BUCKET_UNKNOWN]["key_name"] == "(未知 key)",
      by_key[U.KEY_BUCKET_UNKNOWN])
check("面板测试台的名字", by_key["panel"]["key_name"] == "面板测试台", by_key["panel"])
check("配置里的 Key 用它的名字", by_key["k1"]["key_name"] == "主力 Key", by_key["k1"])
check("统计形状与 models 一致",
      set(by_key["k1"]["all_time"]) == set(analytics["models"][0]["all_time"]),
      (sorted(by_key["k1"]["all_time"]), sorted(analytics["models"][0]["all_time"])))
check("k1 记下两次请求", by_key["k1"]["all_time"]["requests"] == 1
      and by_key["k1"]["all_time"]["errors"] == 1, by_key["k1"]["all_time"])
check("k1 的 token 合计", by_key["k1"]["all_time"]["total_tokens"] == 130,
      by_key["k1"]["all_time"])
check("k1 的积分合计", abs(by_key["k1"]["all_time"]["credit"] - 0.6) < 1e-9,
      by_key["k1"]["all_time"])
check("没有 key_id 的旧行不计入任何 Key",
      sum(k["all_time"]["total_tokens"] for k in analytics["keys"]
          if k["key_id"] != U.KEY_BUCKET_BEFORE) == 152
      and any(k["key_id"] == U.KEY_BUCKET_BEFORE
              and k["all_time"]["total_tokens"] == 5 for k in analytics["keys"]),
      [(k["key_id"], k["all_time"]["total_tokens"]) for k in analytics["keys"]])
check("窗口桶跟着请求的范围",
      U.compute_usage_analytics(ttl=0, realm="all", range="all")["keys"][0]["window"]["total_tokens"]
      == by_key["k1"]["window"]["total_tokens"], by_key["k1"]["window"])

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
