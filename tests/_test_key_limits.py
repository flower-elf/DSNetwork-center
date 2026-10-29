"""API Key 的限额：字段校验、省略即保留、过期 / IP / 配额拦截、用量与重置。

过期 401、IP 不在白名单 403、配额用尽 429；Messages 用 Anthropic 错误外壳，面板会话不受限制；用量只累加 usage_reset_at 之后的记录。
"""

import json
import os
import sys
import tempfile
import time

from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-keylimits-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)


import wb_usage as U
import wb_proxy as proxy
import wb_settings as S

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


class FakeRequest(object):
    """够用的处理器替身，用来驱动真实的设置保存与限额判定。"""

    _handle_settings_save = proxy.Handler._handle_settings_save
    _key_limits_ok = proxy.Handler._key_limits_ok
    _refuse_key_limit = proxy.Handler._refuse_key_limit

    def __init__(self, payload=None, key_entry=None, panel=False, ip="127.0.0.1"):
        self.payload = payload or {}
        self.key_entry = key_entry
        self._panel = panel
        self.client_address = (ip, 51000)
        self.status = None
        self.body = None

    def _payload_or_error(self, allow_list=False):
        return self.payload

    def _panel_ok(self):
        return self._panel

    def _json(self, status, payload):
        self.status, self.body = status, payload
        return status, payload

    def _error(self, code, message, err_type="server_error"):
        self.status = code
        self.body = {"error": {"message": message, "type": err_type}}
        return code, self.body

    def _anthropic_error(self, code, message, err_type=None, retry_after=None):
        self.status = code
        self.body = {"type": "error", "error": {"type": err_type, "message": message}}
        return code, self.body


def write_rows(items, mode="a"):
    os.makedirs(proxy.USAGE_DIR, exist_ok=True)
    with open(proxy.USAGE_LOG, mode, encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def usage_row(key_id, total_tokens=100, credit=0.5, at=None):
    return {"at": at if at is not None else time.time(), "model": "m", "key_id": key_id,
            "ip": "127.0.0.1", "api": "chat", "status": 200, "outcome": "completed",
            "total_tokens": total_tokens, "credit": credit, "realm": "intl"}


print("[1] IP 白名单的解析与判定")

entries, problem = S.clean_ip_allowlist(["10.0.0.5", "192.168.1.0/24", " 10.0.0.5 "])
check("单个地址与网段都收下", entries == ["10.0.0.5", "192.168.1.0/24"] and not problem,
      (entries, problem))
entries, problem = S.clean_ip_allowlist("10.0.0.5, 172.16.0.0/12")
check("逗号分隔的字符串也认", entries == ["10.0.0.5", "172.16.0.0/12"], (entries, problem))
entries, problem = S.clean_ip_allowlist(["10.0.0.5/24"])
check("网段被归一化成网络地址", entries == ["10.0.0.0/24"], entries)
entries, problem = S.clean_ip_allowlist(["10.0.0.5/32"])
check("单地址不带掩码写回", entries == ["10.0.0.5"], entries)
entries, problem = S.clean_ip_allowlist(["10.0.0.5", "10.0.0.5/32"])
check("重复项被去掉", entries == ["10.0.0.5"], entries)
entries, problem = S.clean_ip_allowlist(["10.0.0.300"])
check("非法地址给出原因", entries == [] and "10.0.0.300" in problem, (entries, problem))
entries, problem = S.clean_ip_allowlist(["10.0.0.5", "not-an-ip"])
check("一项非法整份拒绝", entries == [] and "not-an-ip" in problem, (entries, problem))
entries, problem = S.clean_ip_allowlist(None)
check("空值表示不限制", entries == [] and not problem, (entries, problem))
entries, problem = S.clean_ip_allowlist({"a": 1})
check("类型不对给出原因", entries == [] and problem, (entries, problem))

check("空名单放行所有地址", S.ip_allowed([], "8.8.8.8") is True)
check("名单内的地址放行", S.ip_allowed(["10.0.0.5"], "10.0.0.5") is True)
check("名单外的地址拒绝", S.ip_allowed(["10.0.0.5"], "10.0.0.6") is False)
check("网段覆盖其中的地址", S.ip_allowed(["192.168.1.0/24"], "192.168.1.44") is True)
check("网段之外仍然拒绝", S.ip_allowed(["192.168.1.0/24"], "192.168.2.44") is False)
check("IPv6 也按网段判定", S.ip_allowed(["fd00::/8"], "fd00::1") is True)
check("有名单时读不出的来源地址拒绝", S.ip_allowed(["10.0.0.5"], "") is False)
check("读坏的值在读取时被丢掉",
      S.stored_ip_allowlist(["10.0.0.5", "junk"]) == ["10.0.0.5"])

print()
print("[2] 数值字段的校验")

check("空值是 0", S.clean_epoch_field(None, "expires_at") == (0, ""))
check("数字字符串认", S.clean_epoch_field("1790000000", "expires_at") == (1790000000, ""))
check("负时间戳拒绝", S.clean_epoch_field(-5, "expires_at")[0] == 0)
check("负时间戳的原因点出字段", "expires_at" in S.clean_epoch_field(-5, "expires_at")[1])
check("乱写的字符串拒绝", S.clean_epoch_field("tomorrow", "expires_at")[1] != "")
check("布尔值拒绝", S.clean_epoch_field(True, "expires_at")[1] != "")
check("配额是整数", S.clean_quota_tokens("1000") == (1000, ""))
check("配额负数拒绝", S.clean_quota_tokens(-1)[1] != "")
check("积分可以是小数", S.clean_quota_credit("1.5") == (1.5, ""))
check("积分负数拒绝", S.clean_quota_credit(-0.5)[1] != "")

print()
print("[3] 字段写入与读回")

d = tempfile.mkdtemp(prefix="wb-keylimits-d-")
S.set_api_keys(d, [{"id": "k1", "name": "限额", "key": "key-one", "expires_at": 1790000000,
                    "quota_tokens": 1000, "quota_credit": 2.5,
                    "ip_allowlist": ["10.0.0.0/24", "127.0.0.1"],
                    "usage_reset_at": 1780000000}])
entry = S.api_keys(d)[0]
check("过期时间写入", entry["expires_at"] == 1790000000, entry)
check("token 配额写入", entry["quota_tokens"] == 1000, entry)
check("积分配额写入", entry["quota_credit"] == 2.5, entry)
check("IP 白名单写入", entry["ip_allowlist"] == ["10.0.0.0/24", "127.0.0.1"], entry)
check("重置时间写入", entry["usage_reset_at"] == 1780000000, entry)
S.set_api_keys(d, [{"id": "k1", "name": "旧行", "key": "key-one"}])
entry = S.api_keys(d)[0]
check("旧行没有这些字段时读回无限制",
      (entry["expires_at"], entry["quota_tokens"], entry["quota_credit"],
       entry["ip_allowlist"], entry["usage_reset_at"]) == (0, 0, 0.0, [], 0), entry)

print()
print("[4] /settings/save 的校验")

with tempfile.TemporaryDirectory(prefix="wb-keylimits-") as directory:
    with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory,
                                                    POOL=None, SCHEDULER=None):
        S.set_api_keys(directory, [{"id": "k1", "name": "限额", "key": "key-one",
                                    "realm": "cn", "models": ["deepseek/*"],
                                    "quota_tokens": 500, "expires_at": 1790000000}])

        def save(payload):
            request = FakeRequest(payload=payload)
            proxy.Handler._handle_settings_save(request)
            return request

        bad_ip = save({"api_keys": [{"id": "k1", "key": "", "ip_allowlist": ["10.0.0.999"]}]})
        check("非法 IP 返回 400", bad_ip.status == 400, bad_ip.status)
        check("非法 IP 的原因写明", "10.0.0.999" in bad_ip.body["error"]["message"],
              bad_ip.body)
        bad_quota = save({"api_keys": [{"id": "k1", "key": "", "quota_tokens": -3}]})
        check("负配额返回 400", bad_quota.status == 400, bad_quota.status)
        check("负配额的原因点出字段",
              "quota_tokens" in bad_quota.body["error"]["message"], bad_quota.body)
        bad_expiry = save({"api_keys": [{"id": "k1", "key": "", "expires_at": "never"}]})
        check("时间戳写错返回 400", bad_expiry.status == 400, bad_expiry.status)
        bad_credit = save({"api_keys": [{"id": "k1", "key": "", "quota_credit": -1.5}]})
        check("负积分返回 400", bad_credit.status == 400, bad_credit.status)

        kept = S.api_keys(directory)[0]
        check("被拒绝的保存没有改动已存的值",
              kept["quota_tokens"] == 500 and kept["expires_at"] == 1790000000
              and kept["ip_allowlist"] == [], kept)

        ok = save({"api_keys": [{"id": "k1", "key": "", "expires_at": 0,
                                 "quota_tokens": 0, "quota_credit": 0,
                                 "ip_allowlist": []}]})
        check("合法保存返回 200", ok.status == 200, ok.status)
        cleared = S.api_keys(directory)[0]
        check("显式 0 清掉限额",
              (cleared["expires_at"], cleared["quota_tokens"], cleared["quota_credit"],
               cleared["ip_allowlist"]) == (0, 0, 0.0, []), cleared)
        check("省略的 models 保留原值", cleared["models"] == ["deepseek/*"], cleared)
        check("省略的 realm 保留原值", cleared["realm"] == "cn", cleared)
        cleared_exit = save({"api_keys": [{"id": "k1", "key": "", "realm": ""}]})
        check("显式空串清掉出口", cleared_exit.status == 200
              and S.api_keys(directory)[0]["realm"] == "", S.api_keys(directory)[0])
        # 恢复出口，后面的检查还要用
        save({"api_keys": [{"id": "k1", "key": "", "realm": "cn"}]})

        again = save({"api_keys": [{"id": "k1", "key": "", "expires_at": 1890000000,
                                    "quota_tokens": 42, "ip_allowlist": ["10.0.0.0/24"]}]})
        check("新字段可单独更新", again.status == 200, again.status)
        updated = S.api_keys(directory)[0]
        check("过期时间更新", updated["expires_at"] == 1890000000, updated)
        check("配额更新", updated["quota_tokens"] == 42, updated)
        check("白名单更新", updated["ip_allowlist"] == ["10.0.0.0/24"], updated)
        check("面板读回的条目带限额与状态",
              updated["id"] in [k["id"] for k in again.body["api_keys"]], again.body)

        view = proxy.runtime_settings_view()
        item = [k for k in view["api_keys"] if k["id"] == "k1"][0]
        check("设置视图带 expires_at", item["expires_at"] == 1890000000, item)
        check("设置视图带 quota_tokens", item["quota_tokens"] == 42, item)
        check("设置视图带 ip_allowlist", item["ip_allowlist"] == ["10.0.0.0/24"], item)
        check("设置视图带 usage 四项",
              set(item["usage"]) == {"requests", "total_tokens", "credit", "last_used_at"},
              item["usage"])
        check("设置视图带 status", item["status"] == "ok", item)

        missing = save({"reset_key_usage": "nope"})
        check("重置不存在的 Key 返回 404", missing.status == 404, missing.status)

print()
print("[5] 用量的累计与重置")

with tempfile.TemporaryDirectory(prefix="wb-keylimits-") as directory:
    with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory,
                                                    POOL=None, SCHEDULER=None):
        open(proxy.USAGE_LOG, "w", encoding="utf-8").close()
        S.set_api_keys(directory, [{"id": "k1", "name": "one", "key": "key-one"},
                                   {"id": "k2", "name": "two", "key": "key-two"}])
        now = time.time()
        write_rows([usage_row("k1", 100, 0.5, at=now - 60),
                    usage_row("k1", 50, 0.25, at=now - 30),
                    usage_row("k2", 999, 9.0, at=now - 20),
                    usage_row("panel", 7, 0.01, at=now - 10),
                    {"at": now - 5, "model": "m", "total_tokens": 3, "credit": 0.02,
                     "realm": "intl"}])
        stat = U.key_usage("k1")
        check("按 Key 归集请求数", stat["requests"] == 2, stat)
        check("按 Key 累加 token", stat["total_tokens"] == 150, stat)
        check("按 Key 累加积分", abs(stat["credit"] - 0.75) < 1e-9, stat)
        check("最后一次使用时间取最大", stat["last_used_at"] == now - 30, stat)
        check("别的 Key 不混进来", U.key_usage("k2")["total_tokens"] == 999)
        check("没有 key_id 的旧行不计入", U.key_usage("k1")["requests"] == 2)
        check("没出现过的 Key 是 0", U.key_usage("k-none")["total_tokens"] == 0)

        # 新增的行按增量折叠进来
        write_rows([usage_row("k1", 25, 0.05, at=now - 1)])
        stat = U.key_usage("k1")
        check("后写的行也累加进来", stat["requests"] == 3 and stat["total_tokens"] == 175, stat)

        # 重置：把 usage_reset_at 推到当前时间，之前的行不计入
        request = FakeRequest(payload={"reset_key_usage": "k1"})
        proxy.Handler._handle_settings_save(request)
        check("重置返回 200", request.status == 200, request.status)
        reset_at = S.api_keys(directory)[0]["usage_reset_at"]
        check("usage_reset_at 被写成当前时间", reset_at >= int(now), reset_at)
        stat = U.key_usage("k1", reset_at)
        check("重置后旧记录不再计入", stat["requests"] == 0 and stat["total_tokens"] == 0, stat)
        write_rows([usage_row("k1", 40, 0.2, at=reset_at + 5)])
        stat = U.key_usage("k1", reset_at)
        check("重置之后的行重新累计", stat["requests"] == 1 and stat["total_tokens"] == 40, stat)
        check("重置后 last_used_at 跟着更新", stat["last_used_at"] == reset_at + 5, stat)

print()
print("[6] 过期 / IP / 配额的拦截")

with tempfile.TemporaryDirectory(prefix="wb-keylimits-") as directory:
    with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory,
                                                    POOL=None, SCHEDULER=None):
        open(proxy.USAGE_LOG, "w", encoding="utf-8").close()
        now = time.time()
        expired = {"id": "e1", "name": "过期", "expires_at": int(now) - 60, "enabled": True}
        request = FakeRequest(key_entry=expired)
        request._key_limits_ok()
        check("过期 Key 返回 401", request.status == 401, request.status)
        check("过期说明写着过期时间", "过期" in request.body["error"]["message"],
              request.body)
        check("过期用 OpenAI 错误外壳",
              request.body["error"]["type"] == "invalid_request_error", request.body)

        request = FakeRequest(key_entry=expired)
        request._key_limits_ok(is_messages=True)
        check("Messages 接口的过期也是 401", request.status == 401, request.status)
        check("Messages 用 Anthropic 错误外壳", request.body["type"] == "error"
              and request.body["error"]["type"] == "authentication_error", request.body)

        future = {"id": "f1", "name": "有效", "expires_at": int(now) + 3600}
        request = FakeRequest(key_entry=future)
        check("未到期的 Key 放行", request._key_limits_ok() is True, request.status)

        allowlisted = {"id": "i1", "name": "白名单", "ip_allowlist": ["10.0.0.0/24"]}
        request = FakeRequest(key_entry=allowlisted, ip="10.0.0.9")
        check("白名单内的地址放行", request._key_limits_ok() is True, request.status)
        request = FakeRequest(key_entry=allowlisted, ip="192.168.1.5")
        request._key_limits_ok()
        check("白名单外的地址返回 403", request.status == 403, request.status)
        check("403 说明写着来源地址", "192.168.1.5" in request.body["error"]["message"],
              request.body)
        request = FakeRequest(key_entry=allowlisted, ip="192.168.1.5")
        request._key_limits_ok(is_messages=True)
        check("Messages 的 IP 拦截也是 403 且用 Anthropic 外壳",
              request.status == 403 and request.body["error"]["type"] == "permission_error",
              request.body)

        write_rows([usage_row("q1", 1000, 0.5)])
        quota = {"id": "q1", "name": "配额", "quota_tokens": 1000}
        request = FakeRequest(key_entry=quota)
        request._key_limits_ok()
        check("token 配额用尽返回 429", request.status == 429, request.status)
        check("429 说明写明配额与已用",
              "1000" in request.body["error"]["message"], request.body)
        request = FakeRequest(key_entry=quota, )
        request._key_limits_ok(is_messages=True)
        check("Messages 的配额拦截用 rate_limit_error",
              request.status == 429 and request.body["error"]["type"] == "rate_limit_error",
              request.body)

        credit_quota = {"id": "q1", "name": "配额", "quota_credit": 0.4}
        request = FakeRequest(key_entry=credit_quota)
        request._key_limits_ok()
        check("积分配额用尽同样 429", request.status == 429, request.status)

        roomy = {"id": "q1", "name": "配额", "quota_tokens": 100000}
        request = FakeRequest(key_entry=roomy)
        check("配额没用完时放行", request._key_limits_ok() is True, request.status)
        request = FakeRequest(key_entry=None)
        check("没有 Key 的请求不受限额约束", request._key_limits_ok() is True)
        request = FakeRequest(key_entry=quota, panel=True)
        check("面板会话不受 Key 限额约束", request._key_limits_ok() is True, request.status)

print()
print("[7] 状态分档")

now = time.time()
check("启用的普通 Key 是 ok", S.key_status({"enabled": True}, {}, now) == "ok")
check("停用的 Key 是 disabled",
      S.key_status({"enabled": False, "expires_at": int(now) - 1}, {}, now) == "disabled")
check("过期优先于配额",
      S.key_status({"enabled": True, "expires_at": int(now) - 1, "quota_tokens": 1},
                   {"total_tokens": 5}, now) == "expired")
check("配额用尽是 quota_exceeded",
      S.key_status({"enabled": True, "quota_tokens": 5}, {"total_tokens": 5}, now)
      == "quota_exceeded")
check("积分用尽也是 quota_exceeded",
      S.key_status({"enabled": True, "quota_credit": 1.0}, {"credit": 1.0}, now)
      == "quota_exceeded")
check("还没到配额是 ok",
      S.key_status({"enabled": True, "quota_tokens": 5}, {"total_tokens": 4}, now) == "ok")

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
