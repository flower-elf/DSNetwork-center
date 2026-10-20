# 费用估算：官方价格设置、按模型折算、积分折算支出与本月消耗。
# 不联网：用量日志与设置都在临时目录里合成。
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-cost-")
ACCOUNTS = os.path.join(_TMP, "accounts")
os.environ["ACCOUNTS_DIR"] = ACCOUNTS
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(ACCOUNTS, exist_ok=True)

import wb_cost
import wb_proxy as P
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


def row(at, model, prompt, completion, cached=0, credit=0):
    return {
        "at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
        "model": model, "stream": True, "outcome": "completed",
        "prompt_tokens": prompt, "completion_tokens": completion,
        "reasoning_tokens": 0, "cached_tokens": cached,
        "total_tokens": prompt + completion, "credit": credit,
        "account": "acct-A", "realm": "intl",
    }


now = time.time()
_lt = time.localtime(now)
TODAY0 = time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 0, 0, 0, 0, 0, -1))
MONTH0 = time.mktime((_lt.tm_year, _lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
with io.open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
    for item in (
        row(MONTH0 + 3600, "deepseek-v4.1-flash", 1000000, 1000000, credit=5),
        row(now - 120, "deepseek-v4.1-flash", 1000000, 0, cached=500000, credit=2),
        row(now - 60, "glm-5.3", 500000, 0, credit=3),
    ):
        fh.write(json.dumps(item, ensure_ascii=False) + chr(10))

print("[1] 默认价格表只有 deepseek-v4.1-flash")
defaults = wb_settings.cost_estimate(ACCOUNTS)
check("默认积分折算单价为 0", defaults["credit_price"] == 0, defaults)
check("默认只有一项价格", [r["model"] for r in defaults["models"]] == ["deepseek-v4.1-flash"], defaults)

print("[2] 设置读写与校验")
saved = wb_settings.set_cost_estimate(ACCOUNTS, {
    "credit_price": 0.5,
    "models": [{"model": "deepseek-v4.1-flash", "input": 2, "cached_input": 0.04, "output": 8}],
})
check("保存后原样读回", wb_settings.cost_estimate(ACCOUNTS) == saved, saved)
for label, payload in (
    ("负价格被拒绝", {"credit_price": -1, "models": []}),
    ("缺少模型名却被填了价格", {"credit_price": 0, "models": [{"input": 1}]}),
    ("重复模型名被拒绝", {"credit_price": 0, "models": [
        {"model": "glm-5.3", "input": 1},
        {"model": "GLM-5.3", "input": 1},
    ]}),
):
    try:
        wb_settings.set_cost_estimate(ACCOUNTS, payload)
        check(label, False, "没有抛出错误")
    except ValueError:
        check(label, True)

check("缓存价留空时按输入价计", wb_cost.clean({"models": [
    {"model": "glm-5.3", "input": 2, "output": 3}]})["models"][0]["cached_input"] == 2)

print("[3] 按模型折算：官方价格、积分折算支出与省了多少")
view = P.cost_estimate_view(realm="intl", range="today")
by_model = {item["model"]: item for item in view["models"]}
deepseek = by_model["deepseek-v4.1-flash"]
check("未命中 50 万 token 与命中 50 万 token 合计 1.02 元",
      abs(deepseek["official_cny"] - 1.02) < 1e-9, deepseek["official_cny"])
check("积分 2 按 0.5 元折算为 1 元", abs(deepseek["paid_cny"] - 1.0) < 1e-9, deepseek["paid_cny"])
check("省了多少 = 官方价格减积分折合", abs(deepseek["saved_cny"] - 0.02) < 1e-9, deepseek["saved_cny"])
check("未配置的模型不参与合计", by_model["glm-5.3"]["priced"] is False, by_model["glm-5.3"])
check("合计只算已定价模型", abs(view["summary"]["official_cny"] - 1.02) < 1e-9,
      view["summary"]["official_cny"])
check("未定价模型列出来", view["unpriced_models"] == ["glm-5.3"], view["unpriced_models"])

print("[4] 本月消耗包含本月早些时候的请求")
analytics = P.compute_usage_analytics(realm="intl", range="today")
check("今日窗口只有今天两行", analytics["summary"]["window"]["credit"] == 5,
      analytics["summary"]["window"]["credit"])
month = P.month_credit_view(realm="intl")
check("本月消耗为本月全部三行", month["credit"] == 10, month)
check("另一个出口没有消耗", P.month_credit_view(realm="cn")["credit"] == 0)
check("两个出口合计为本月全部三行", P.month_credit_view(realm="all")["credit"] == 10)

# 追加一行后必须从旧 offset 继续，不能因为文件变大就从头重扫。
_ = P.month_credits(ttl=0)
_start_offset = P._month_credit["offset"]
with io.open(P.USAGE_LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row(now - 30, "deepseek-v4.1-flash", 1000, 0, credit=2),
                        ensure_ascii=False) + chr(10))
_calls = []
_real_center = P._center_fold_rows


def _spy_center(offset, fold, *args, **kwargs):
    _calls.append(offset)
    return _real_center(offset, fold, *args, **kwargs)


P._center_fold_rows = _spy_center
try:
    totals, ok = P.month_credits(ttl=0)
finally:
    P._center_fold_rows = _real_center
check("追加行后从旧 offset 继续折叠", _calls == [_start_offset], _calls)
check("追加行的积分进入本月合计", ok and totals.get("intl") == 12,
      (totals, ok))

_after_offset = P._month_credit["offset"]
_calls = []
P._center_fold_rows = _spy_center
try:
    totals, ok = P.month_credits(ttl=0)
finally:
    P._center_fold_rows = _real_center
check("第二次读取从文件尾开始", _calls == [_after_offset], _calls)
check("第二次读取不重复累加", ok and totals.get("intl") == 12, (totals, ok))

print("[5] 调用量化为 0 的模型")
empty = P.cost_estimate_view(realm="cn", range="today")
check("没有请求时合计为 0", empty["summary"]["official_cny"] == 0, empty["summary"])
check("没有请求时模型列表为空", empty["models"] == [], empty["models"])

print("[6] 设置损坏时抛出读取错误")
with io.open(wb_settings.settings_path(ACCOUNTS), "w", encoding="utf-8") as fh:
    fh.write("{")
try:
    wb_settings.cost_estimate(ACCOUNTS)
    check("损坏的设置必须抛出错误", False, "返回了内置价格")
except wb_settings.SettingsError as exc:
    check("损坏的设置必须抛出错误", True, exc)

print("")
print("PASS %d  FAIL %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
