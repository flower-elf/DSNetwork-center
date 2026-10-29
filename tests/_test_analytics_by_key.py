"""测指标页按 API key 归因的契约：usage 行记录调用方出示的 key，报表按 key 二次汇总，三种无归因桶、对账、空闲 key 行、模型数上限和软删除语义都要正确。用 python tests/run_all.py _test_analytics_by_key 运行，不联网，usage 日志与 settings 文件在临时目录合成。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-bykey-")
# WB_PROXY_USAGE_DIR 在 import 时读取。ACCOUNTS_DIR 不能这样配置：wb_proxy
# 把它硬编码在脚本旁边，设环境变量无效，必须在下方重新绑定。本套件会写
# settings，绑错会把夹具写进真实的 accounts/settings.json，面板里就能看到。
os.environ["WB_PROXY_USAGE_DIR"] = _TMP

import wb_proxy as P
import wb_usage as U
import wb_settings as S

P.ACCOUNTS_DIR = os.path.join(_TMP, "accounts")
os.makedirs(P.ACCOUNTS_DIR, exist_ok=True)

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


# --------------------------------------------------------------------------
# 1. 写入端始终把 key id 记在行上。
# --------------------------------------------------------------------------
U.record_usage("m-1", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
               key="k-writer")
U.record_usage("m-1", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
U.record_error("m-1", 500, "boom", key="k-writer")
U.record_error("m-1", 500, "boom")

with io.open(P.USAGE_LOG, encoding="utf-8") as fh:
    written = [json.loads(line) for line in fh if line.strip()]

check("a successful row records the key that called", written[0].get("key") == "k-writer")
check("a keyless success still writes the field", "key" in written[1] and written[1]["key"] == "")
check("a failed row records the key too", written[2].get("key") == "k-writer")
check("a keyless failure still writes the field", "key" in written[3] and written[3]["key"] == "")
check("the new field is appended, so older readers keep working",
      list(written[0].keys()).index("key") > list(written[0].keys()).index("realm"))


# --------------------------------------------------------------------------
# 2. 设置层的软删除：secret 抹掉、id 留存、历史可读，删掉的 key 不能再认证。
# --------------------------------------------------------------------------
_d = os.path.join(_TMP, "settings-accounts")
os.makedirs(_d, exist_ok=True)
S.set_api_keys(_d, [
    {"id": "k1", "name": "甲", "key": "key-alpha", "realm": "cn"},
    {"id": "k2", "name": "乙", "key": "key-beta", "realm": ""},
])
S.set_api_keys(_d, [{"id": "k1", "name": "甲改名", "key": "key-alpha", "realm": "cn"}])

live = S.api_keys(_d)
check("a key removed from the list stops being live", [e["id"] for e in live] == ["k1"])
check("renaming keeps the id, so the history stays attached",
      live[0]["name"] == "甲改名" and live[0]["id"] == "k1")

everything = S.api_keys(_d, include_deleted=True)
tomb = [e for e in everything if e["id"] == "k2"]
check("the removed key keeps a row", len(tomb) == 1, everything)
check("its name and creation date survive",
      bool(tomb) and tomb[0]["name"] == "乙" and tomb[0]["created_at"])
check("its secret is wiped", bool(tomb) and tomb[0]["key"] == "")
check("it is marked disabled and dated",
      bool(tomb) and tomb[0]["enabled"] is False and bool(tomb[0]["deleted_at"]))
check("a deleted key cannot authenticate", S.match_api_key(_d, "key-beta") is None)
check("a live key still can", (S.match_api_key(_d, "key-alpha") or {}).get("id") == "k1")
check("two tombstones do not collapse into one",
      len(S.api_keys(_d, include_deleted=True)) == 2)
check("saving again does not resurrect the tombstone",
      [e["id"] for e in S.set_api_keys(_d, [{"id": "k1", "name": "甲改名",
                                             "key": "key-alpha", "realm": "cn"}])] == ["k1"])


# --------------------------------------------------------------------------
# 3. 分析载荷。
# --------------------------------------------------------------------------
NOW = time.time()


def row(account, key, model="m-1", prompt=100, completion=50, realm="intl", tokens=None):
    r = {
        "at": NOW, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW)),
        "model": model, "stream": True, "outcome": "completed",
        "elapsed_ms": 1000, "ttft_ms": 300, "gen_ms": 700,
        "prompt_tokens": prompt, "completion_tokens": completion,
        "reasoning_tokens": 0, "cached_tokens": 0,
        "total_tokens": tokens if tokens is not None else prompt + completion,
        "credit": 1.5, "account": account, "realm": realm,
    }
    if key is not None:
        r["key"] = key
    return r


rows = [
    row("acct-A", "k1"),
    row("acct-A", "k1", model="m-2"),
    row("acct-B", "k2"),                      # 已配置但被禁用
    row("acct-B", ""),                        # 未配置 key 的部署，持续增长
    row("acct-B", None),                      # 字段引入之前写的行，只减不增
    row("acct-B", "ghost"),                   # 名单不认识的 id，settings 文件坏了
    row("acct-A", "launcher"),                # --api-key 启动参数 key
    row("acct-A", "", realm="cn"),            # 未配置 key，另一个出口
]
# 跨两个出口的 key，以及模型数超过上限的 key。
rows += [row("acct-C", "k-follow", realm="intl"), row("acct-C", "k-follow", realm="cn")]
rows += [row("acct-D", "k-wide", model="m-%d" % i, tokens=(900 - i * 100)) for i in range(1, 8)]

with io.open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")

S.set_api_keys(P.ACCOUNTS_DIR, [
    {"id": "k1", "name": "甲", "key": "key-alpha", "realm": "cn", "enabled": True},
    {"id": "k2", "name": "乙", "key": "key-beta", "realm": "", "enabled": False},
    {"id": "k-idle", "name": "闲", "key": "key-idle", "realm": "intl", "enabled": True},
    {"id": "k-follow", "name": "跟", "key": "key-follow", "realm": "", "enabled": True},
    {"id": "k-wide", "name": "宽", "key": "key-wide", "realm": "", "enabled": True},
])
P.API_KEY = "launcher-secret"

data = U._compute_usage_analytics_uncached()
by_key = {k["key"]: k for k in data["keys"]}

check("the payload carries a key axis", "keys" in data and len(data["keys"]) == len(by_key))
check("a used key is attributed", by_key["k1"]["window"]["requests"] == 2)
check("its tokens follow", by_key["k1"]["window"]["total_tokens"] == 300)
check("its model breakdown is split per model",
      sorted(m["model"] for m in by_key["k1"]["models"]) == ["m-1", "m-2"])
check("a configured key with no traffic still gets a row (0 is not 'missing')",
      by_key["k-idle"]["window"]["requests"] == 0 and by_key["k-idle"]["name"] == "闲")
check("a disabled key with traffic in the window is kept",
      by_key["k2"]["window"]["requests"] == 1 and by_key["k2"]["enabled"] is False)
# 三种桶语义不同，合成一桶会把增长的泄漏藏在缩减的历史尾巴后面。
check("rows written before the field existed are their own bucket",
      by_key[U.KEY_BUCKET_BEFORE]["window"]["requests"] == 1)
check("a keyless deployment is its own bucket, not the legacy one",
      by_key[U.KEY_BUCKET_ANON]["window"]["requests"] == 2)
check("an id the roster cannot name is folded into one row",
      by_key[U.KEY_BUCKET_UNKNOWN]["window"]["requests"] == 1)
check("the launcher key gets a row when it is configured",
      by_key["launcher"]["window"]["requests"] == 1 and by_key["launcher"]["name"] == "启动参数")
check("a key used on both exits is flagged",
      by_key["k-follow"]["cross_realm"] is True
      and sorted(by_key["k-follow"]["realms"]) == ["cn", "intl"])
check("a key that only ever used one exit is not",
      by_key["k1"]["cross_realm"] is False)
check("the model list is capped", len(by_key["k-wide"]["models"]) == U.KEY_MODEL_TOP_N)
check("the cap is accounted for rather than dropped",
      by_key["k-wide"]["models_other"]["models"] == 2
      and by_key["k-wide"]["models_other"]["tokens"] == 300 + 200)
check("the busiest models survive the cap",
      [m["model"] for m in by_key["k-wide"]["models"]] == ["m-1", "m-2", "m-3", "m-4", "m-5"])

key_reqs = sum(k["window"]["requests"] for k in data["keys"])
acct_reqs = sum(a["window"]["requests"] for a in data["accounts"])
check("the per-key table adds up to the per-account table", key_reqs == acct_reqs,
      (key_reqs, acct_reqs))
check("and to the window summary itself", key_reqs == data["summary"]["window"]["requests"])
check("every row the page renders is named", all(k.get("name") for k in data["keys"]))

# 从没跑过流量的 launcher key 不是归因维度：配置了面板 key 时它被直接拒收，
# 空行只是噪声。用过的 launcher 那行仍然显示，去掉的只是未用的场景。
with io.open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
    for r in rows:
        if r.get("key") != "launcher":
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
idle = U._compute_usage_analytics_uncached()
check("an unused launcher key gets no row at all",
      "launcher" not in [k["key"] for k in idle["keys"]])

# 出口视图不得为绑定在另一个出口上的 key 造行。
only_cn = U._compute_usage_analytics_uncached(realm="cn")
check("a key bound to the other exit is not listed in this view",
      "k-idle" not in [k["key"] for k in only_cn["keys"]])
check("a key with no binding is still listed, it can serve this exit",
      "k-follow" in [k["key"] for k in only_cn["keys"]])

# 不请求 key 维度的旧面板必须继续拿到合法数据。usd_cny 随消费列后来加入，
# 是增量字段，旧格式就是去掉 keys 与 usd_cny 两个维度之外的部分。
legacy_view = {k: v for k, v in data.items() if k not in ("keys", "usd_cny")}
check("dropping the new axis leaves the old payload intact",
      set(legacy_view) == {"window", "realm", "summary", "accounts", "models"})


# --------------------------------------------------------------------------
# 4. `(切换前)` 行的折叠开关是单个服务端布尔值。
#
# 面板可以要求折叠掉该行，选择记在网关而不是浏览器：accounts/settings.json
# 的 key_before_hidden，与 accounts_collapsed 同形。只有真布尔 true 才隐藏；
# 文件可以手工编辑，"true" 字符串或 1 必须保持该行可见，避免误删历史。
# --------------------------------------------------------------------------
class FakeRequest(object):
    """够跑保存路由的最小 handler。"""

    _handle_settings_save = P.Handler._handle_settings_save

    def __init__(self, payload):
        self.payload = payload
        self.answering = []

    def _payload_or_error(self, allow_list=False):
        return self.payload

    def _error(self, status, message, kind=""):
        self.answering.append(("error", status, message))
        return status, message

    def _json(self, status, payload):
        self.answering.append(("json", status, payload))
        return status, payload


def save(payload):
    """跑保存路由，返回它应答的错误。"""
    request = FakeRequest(payload)
    request._handle_settings_save()
    return [entry for entry in request.answering if entry[0] == "error"]


def stored():
    with io.open(S.settings_path(P.ACCOUNTS_DIR), encoding="utf-8") as fh:
        return json.load(fh)


settings_path = S.settings_path(P.ACCOUNTS_DIR)


def write_settings(data):
    with io.open(settings_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


if os.path.exists(settings_path):
    os.unlink(settings_path)

check("a fresh install shows the row", S.key_before_hidden(P.ACCOUNTS_DIR) is False)
check("the save route stores a real boolean",
      save({"key_before_hidden": True}) == []
      and S.key_before_hidden(P.ACCOUNTS_DIR) is True
      and stored()["key_before_hidden"] is True
      and P.runtime_settings_view()["key_before_hidden"] is True)
check("and stores the off choice too",
      save({"key_before_hidden": False}) == []
      and S.key_before_hidden(P.ACCOUNTS_DIR) is False
      and stored()["key_before_hidden"] is False)
check("a non-boolean is refused, not coerced",
      all(save({"key_before_hidden": bad}) for bad in ("true", "false", 1, 0, None, [], {})))
for bad in ("true", 1, [1], {"x": 1}):
    write_settings({"key_before_hidden": bad})
    check("a hand-edited %r still shows the row" % (bad,),
          S.key_before_hidden(P.ACCOUNTS_DIR) is False)
S.set_api_keys(P.ACCOUNTS_DIR, [
    {"id": "k-keep", "name": "留", "key": "sk-keep", "realm": "", "enabled": True}])
S.set_key_before_hidden(P.ACCOUNTS_DIR, True)
check("writing this key leaves the other settings alone",
      stored().get("api_keys") is not None
      and S.key_before_hidden(P.ACCOUNTS_DIR) is True)

shutil.rmtree(_TMP, ignore_errors=True)
print("\n  SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
