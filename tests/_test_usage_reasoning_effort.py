"""测 usage 行的 reasoning_effort 契约：effort 由 build_upstream_body() 解析、open_upstream() 返回并由 record_usage() 写入行，无 reasoning 控制的模型和旧记录不写该字段。用 python tests/run_all.py _test_usage_reasoning_effort 运行；不联网，日志写入临时目录。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-effort-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_proxy as P
import wb_usage as U
import wb_usage_recent as R

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, extra))


def rows():
    with open(P.USAGE_LOG, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 3,
         "cached_tokens": 4, "total_tokens": 15}

U.record_usage("deepseek-v4.1-flash", USAGE, stream=True, elapsed_ms=1200, ttft_ms=800,
               gen_ms=400, account=None, effort="high")
row = rows()[-1]
check("the resolved effort is written on the row",
      row.get("reasoning_effort") == "high", row.get("reasoning_effort"))

U.record_usage("hy3", USAGE, stream=False, elapsed_ms=900, effort=None)
check("a request without an effort writes no field",
      "reasoning_effort" not in rows()[-1], rows()[-1].get("reasoning_effort"))

U.record_usage("gemini-3.5-flash", USAGE, stream=False, elapsed_ms=900, effort="medium")
check("a fixed-effort model records its fixed value",
      rows()[-1].get("reasoning_effort") == "medium", rows()[-1].get("reasoning_effort"))

# 看板经 recent_usage() 读行，字段必须穿过它保留下来。
got = R.recent_usage(limit=10, realm=P.CURRENT_REALM)
latest = got["rows"][0]
check("recent_usage passes the field through",
      latest.get("reasoning_effort") == "medium", latest.get("reasoning_effort"))
check("recent_usage still returns the other fields",
      latest.get("model") == "gemini-3.5-flash" and latest.get("total_tokens") == 15,
      latest)
old = [r for r in got["rows"] if r.get("model") == "hy3"][0]
check("a row written without the field stays without it",
      "reasoning_effort" not in old, old)

# body 构建同时认两种客户端拼写，只有请求什么都不给时才填模型默认档；
# camelCase 请求保留 reasoningEffort 键，不会多出 snake_case 键。只读 snake_case
# 拼写会把实际跑在 max 的请求误报成 None。
camel = P.build_upstream_body({"model": "deepseek-v4.1-flash",
                               "messages": [{"role": "user", "content": "hi"}],
                               "reasoningEffort": "max"})
check("a camelCase request keeps the camelCase key",
      "reasoning_effort" not in camel, camel.get("reasoning_effort"))
check("the effective effort is read from the camelCase spelling",
      P.upstream_effort_of(camel) == "max", P.upstream_effort_of(camel))
snake = P.build_upstream_body({"model": "deepseek-v4.1-flash",
                               "messages": [{"role": "user", "content": "hi"}],
                               "reasoning_effort": "low"})
check("the snake-case spelling still wins when the client sends it",
      P.upstream_effort_of(snake) == "low", P.upstream_effort_of(snake))
quiet = P.build_upstream_body({"model": "deepseek-v4.1-flash",
                               "messages": [{"role": "user", "content": "hi"}]})
check("a request that asks for nothing gets the model default",
      P.upstream_effort_of(quiet) == (P.model_default_effort("deepseek-v4.1-flash") or "high"),
      P.upstream_effort_of(quiet))


class _StubPool(object):
    """open_upstream() 会用到的账号池接口子集。"""

    def __init__(self, accounts):
        self.accounts = accounts

    def count_ready(self, realm, model=None):
        return sum(a.ready(model=model) for a in self.accounts)

    def pick_for_session(self, realm, session_key=None, exclude=(), model=None,
                         page=None, claim=None):
        return next((a for a in self.accounts if a.uid not in exclude
                     and a.realm == realm and a.ready(model=model)
                     and (claim is None or claim(a))), None)

    def apply_daily_token_limit(self, value=None, usage=None):
        return value or 0

    def apply_daily_credit_limit(self, value=None, credits=None, free_models=None):
        return value or 0

    def apply_model_daily_token_limit(self, value=None, per_model=None):
        return value or 0

    def apply_remaining_weights(self, weights=None):
        # 优先调度的权重表也由请求路径推给池；桩只负责接住调用。
        return weights or {}


class _FakeResponse(object):
    def close(self):
        pass


account = wb_accounts.Account({"uid": "uid-effort", "accessToken": "t", "realm": "intl"})


def open_with(payload):
    """用桩化的账号池和传输层跑真实的 open_upstream()，只返回 effort。"""
    old_pool, old_urlopen = P.POOL, wb_accounts.urlopen
    P.POOL = _StubPool([account])
    wb_accounts.urlopen = lambda req, timeout=None, proxy=None: _FakeResponse()
    try:
        _resp, _acct, effort = P.open_upstream(payload, target_realm="intl")
        return effort
    finally:
        P.POOL = old_pool
        wb_accounts.urlopen = old_urlopen


def chat(model, **extra):
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    payload.update(extra)
    return payload


# 端到端走真实 open_upstream()：它返回的 effort 就是 record_usage() 写进 usage 行的值，
# camelCase 请求也能在行上看到自己的档位。
effort = open_with(chat("deepseek-v4.1-flash", reasoningEffort="max"))
check("open_upstream hands back the camelCase effort", effort == "max", effort)
U.record_usage("deepseek-v4.1-flash", USAGE, stream=False, elapsed_ms=900, effort=effort)
check("and that value lands on the usage row",
      rows()[-1].get("reasoning_effort") == "max", rows()[-1].get("reasoning_effort"))

# 目录固定档位的模型无论请求带不带值都跑在固定档；网关不往 body 写 effort，
# 所以单看 body 查不到 effort。
check("the catalog pins gemini-3.5-flash", P.model_fixed_effort("gemini-3.5-flash") == "medium",
      P.model_fixed_effort("gemini-3.5-flash"))
check("and it declares no default",
      P.model_default_effort("gemini-3.5-flash") is None,
      P.model_default_effort("gemini-3.5-flash"))
plain = P.build_upstream_body(chat("gemini-3.5-flash"))
check("a plain request to it carries no effort in the body",
      P.client_effort_of(plain) is None, P.client_effort_of(plain))
effort = open_with(chat("gemini-3.5-flash"))
check("open_upstream still reports the pinned level", effort == "medium", effort)
U.record_usage("gemini-3.5-flash", USAGE, stream=False, elapsed_ms=900, effort=effort)
check("so the pinned level reaches the usage row",
      rows()[-1].get("reasoning_effort") == "medium", rows()[-1].get("reasoning_effort"))
check("kimi-k3 is pinned the same way", open_with(chat("kimi-k3")) == "medium",
      open_with(chat("kimi-k3")))
check("a pinned model ignores a level the request carries anyway",
      open_with(chat("gemini-3.5-flash", reasoning_effort="max")) == "medium",
      open_with(chat("gemini-3.5-flash", reasoning_effort="max")))

# 可选档位的模型在请求不给值时跑声明的默认档；请求自带值时以请求为准。
check("gpt-6-astra declares a default", P.model_default_effort("gpt-6-astra") == "high",
      P.model_default_effort("gpt-6-astra"))
check("a selectable model falls back to that default",
      open_with(chat("gpt-6-astra")) == "high", open_with(chat("gpt-6-astra")))
check("the client's own value still wins",
      open_with(chat("gpt-6-astra", reasoning_effort="low")) == "low",
      open_with(chat("gpt-6-astra", reasoning_effort="low")))

# 显式关闭 thinking 是明确答案，必须返回 none，不能用模型默认档补上。
check("a request that disables thinking reports none",
      open_with(chat("gpt-6-astra", thinking={"type": "disabled"})) == "none",
      open_with(chat("gpt-6-astra", thinking={"type": "disabled"})))
check("an explicit none reports none",
      open_with(chat("gpt-6-astra", reasoning_effort="none")) == "none",
      open_with(chat("gpt-6-astra", reasoning_effort="none")))
check("a model with no reasoning metadata stays unknown",
      P.upstream_effort_of(chat("hy3"), "hy3") in (None, "low", "high"),
      P.upstream_effort_of(chat("hy3"), "hy3"))

print("")
print("  PASS=%d FAIL=%d" % (PASS, FAIL))
if FAIL:
    sys.exit(1)
print("OK")
