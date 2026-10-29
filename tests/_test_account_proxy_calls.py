"""每个带账号身份的调用都必须从账号的代理发出：漏传 proxy 参数会泄漏宿主 IP，
这里用替身助手让这种错误直接失败。不访问网络。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)
# checkin() 会往账号活动历史写一行（issue #34），所以用例要把用量目录指到
# 可丢弃的临时位置，不能用仓库自己的 usage/ 目录。
_TMP = isolated_data_dirs("wb-account-proxy-calls-")

import wb_accounts

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


DATA = {
    "uid": "acct-1", "nickname": "n", "domain": "www.workbuddy.ai",
    "realm": "intl", "accessToken": "a.b.c", "refreshToken": "r",
    "expiresAt": 4102444800, "enabled": True,
}

PROXY = "http://10.0.0.1:17901"

seen = []


def fake_http_json(url, **kwargs):
    seen.append(kwargs.get("proxy", None))
    raise RuntimeError("stop-after-recording")


real_http_json = wb_accounts.http_json
wb_accounts.http_json = fake_http_json

try:
    acct = wb_accounts.Account(dict(DATA))
    acct.proxy = PROXY

    print("[1] refresh()")
    del seen[:]
    try:
        acct.refresh(force=True)
    except Exception:
        pass
    check("refresh() issued a request", bool(seen))
    check("refresh() passes the account proxy", seen and seen[0] == PROXY,
          repr(seen[:1]))

    print("[2] fetch_credits()")
    del seen[:]
    try:
        acct.fetch_credits()
    except Exception:
        pass
    check("fetch_credits() issued a request", bool(seen))
    check("fetch_credits() passes the account proxy", seen and seen[0] == PROXY,
          repr(seen[:1]))

    print("[3] checkin() (cn realm only)")
    cn_proxy = "http://10.0.0.2:17902"
    cn = wb_accounts.Account(dict(DATA, realm="cn", domain="www.codebuddy.cn"))
    cn.proxy = cn_proxy
    del seen[:]
    try:
        cn.checkin()
    except Exception:
        pass
    check("checkin() issued a request", bool(seen))
    check("checkin() passes the account proxy", seen and seen[0] == cn_proxy,
          repr(seen[:1]))

    print("[4] an unproxied account passes empty, not a stale value")
    plain = wb_accounts.Account(dict(DATA, uid="acct-2"))
    plain.proxy = ""
    del seen[:]
    try:
        plain.refresh(force=True)
    except Exception:
        pass
    check("direct account sends proxy=''", seen and seen[0] == "", repr(seen[:1]))
finally:
    wb_accounts.http_json = real_http_json

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
