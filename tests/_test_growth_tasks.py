"""国内版成长任务: 夜猫子窗口按固定 UTC+8 判定、查询失败要留日志、credits 为 None 不能抛异常。

无网络访问: 出站请求全部走替身 urlopen。
"""

import calendar
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_accounts
import wb_tasks as T

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


class ShiftedClock(object):
    """time 模块替身: now 是 UTC 时间戳, localtime() 报 now + local_offset, gmtime() 不变。"""

    def __init__(self, real, now, local_offset):
        self._real = real
        self._now = now
        self._local_offset = local_offset

    def set_now(self, ts):
        self._now = ts

    def time(self):
        return self._now

    def gmtime(self, ts=None):
        return self._real.gmtime(self._now if ts is None else ts)

    def localtime(self, ts=None):
        return self._real.gmtime((self._now if ts is None else ts) + self._local_offset)

    def strftime(self, fmt, t=None):
        if t is None:
            t = self._real.localtime()
        return self._real.strftime(fmt, t)

    def __getattr__(self, name):
        return getattr(self._real, name)


def utc_from_cst(year, month, day, hour, minute=0):
    """CST 墙上时间 -> UTC 时间戳。"""
    return calendar.timegm((year, month, day, hour, minute, 0, 0, 0, 0)) - 8 * 3600


class FakeResponse(object):
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


TASK_LIST = [
    {"task_code": "create_canvas", "title": "创建设计任务", "accept_status": "claimed",
     "progress": {"current": 1, "target": 1}},
    {"task_code": "chat_5", "title": "发起 5 次对话", "accept_status": "claimed",
     "progress": {"current": 5, "target": 5}},
]


def dead_urlopen(req, timeout=30, proxy=""):
    """替身 urlopen: 所有端点都 404, 该状态不重试。"""
    url = getattr(req, "full_url", str(req))
    raise urllib.error.HTTPError(url, 404, "Not Found", None, None)


def offline_urlopen(req, timeout=30, proxy=""):
    """替身 urlopen: 只回答成长任务清单, 其它端点一律 404。"""
    url = getattr(req, "full_url", str(req))
    if url.endswith("/v2/activity/growth/tasks"):
        return FakeResponse(json.dumps({"data": {"tasks": TASK_LIST}}).encode("utf-8"))
    raise urllib.error.HTTPError(url, 404, "Not Found", None, None)


def make_account(uid="u-cn"):
    return wb_accounts.Account({
        "uid": uid, "nickname": "cn-acct", "realm": "cn",
        "domain": "copilot.tencent.com", "accessToken": "a.b.c",
        "refreshToken": "r", "expiresAt": 4102444800, "enabled": True,
    })


print("[1] in_night_window() 按固定 UTC+8")

real_time = T.time
# 替身本机时区 UTC-12: 本机小时与 CST 小时相差 12, 两者不可能同时处于窗口内或窗口外
clock = ShiftedClock(real_time, utc_from_cst(2026, 5, 1, 23, 30), -12 * 3600)
T.time = clock
try:
    cases = [
        ((2026, 5, 1, 23, 30), True, "CST 23:30 在窗口内"),
        ((2026, 5, 1, 23, 0), True, "CST 23:00 是窗口起点"),
        ((2026, 5, 2, 7, 30), True, "CST 07:30 在窗口内"),
        ((2026, 5, 2, 6, 59), True, "CST 06:59 在窗口内"),
        ((2026, 5, 2, 8, 0), False, "CST 08:00 是窗口终点"),
        ((2026, 5, 1, 22, 0), False, "CST 22:00 在窗口外"),
        ((2026, 5, 1, 12, 0), False, "CST 12:00 在窗口外"),
    ]
    for (year, month, day, hour, minute), expected, label in cases:
        clock.set_now(utc_from_cst(year, month, day, hour, minute))
        got = T.in_night_window()
        check(label, got is expected, "got %r" % (got,))
finally:
    T.time = real_time

print()
print("[2] fetch_growth_tasks() 的查询异常要留痕")

account = make_account()
logs = []
real_urlopen = wb_accounts.urlopen
T.set_logger(logs.append)
wb_accounts.urlopen = dead_urlopen
try:
    tasks = T.fetch_growth_tasks(account)
finally:
    wb_accounts.urlopen = real_urlopen
    T.set_logger(lambda msg: None)

check("查询失败读成空列表", tasks == [], tasks)
check("日志里点出失败的端点",
      any("growth/tasks" in line and "404" in line for line in logs), logs)

print()
print("[2b] 上游 200 正文里的业务错误码按查询失败处理")

business_logs = []


def rejected_urlopen(req, timeout=30, proxy=""):
    return FakeResponse(json.dumps({"code": 11110, "msg": "login expired"}).encode("utf-8"))


T.set_logger(business_logs.append)
wb_accounts.urlopen = rejected_urlopen
try:
    ok, tasks = T.query_growth_tasks(account)
finally:
    wb_accounts.urlopen = real_urlopen
    T.set_logger(lambda msg: None)

check("业务错误码不算成功", ok is False, ok)
check("业务错误码不返回任务", tasks == [], tasks)
check("日志点出错误码", any("11110" in line for line in business_logs), business_logs)

print()
print("[2c] 任务列表标注可自动化与需手动操作")


def tasks_urlopen(req, timeout=30, proxy=""):
    return FakeResponse(json.dumps({"code": 0, "data": {"tasks": [
        {"task_code": "Future_Task_X"},
        {"task_code": "wb_wechat_oa_subscribe_task"},
        {"task_code": "chat_5"},
    ]}}).encode("utf-8"))


wb_accounts.urlopen = tasks_urlopen
try:
    ok, tasks = T.query_growth_tasks(account)
finally:
    wb_accounts.urlopen = real_urlopen

by_code = {t["task_code"]: t for t in tasks}
check("任务清单读取成功", ok is True, ok)
check("未登记任务标为需手动操作",
      by_code["Future_Task_X"]["manual"] is True, by_code["Future_Task_X"])
check("未登记任务给出手动原因",
      by_code["Future_Task_X"]["manual_reason"] == "暂未登记自动化",
      by_code["Future_Task_X"])
check("不可伪造任务标为需手动操作",
      by_code["wb_wechat_oa_subscribe_task"]["manual"] is True,
      by_code["wb_wechat_oa_subscribe_task"])
check("可自动推进的任务不标手动",
      by_code["chat_5"]["manual"] is False, by_code["chat_5"])

print()
print("[3] run_growth_tasks() 在 credits 为 None 时不能抛 AttributeError")

account = make_account("u-credits")
check("前提: 账号初始 credits 为空", account.credits is None, account.credits)

logs = []
T.set_logger(logs.append)
wb_accounts.urlopen = offline_urlopen
try:
    res = T.run_growth_tasks(account, gap=0.01)
except Exception as exc:
    res = None
    check("不再抛 AttributeError", False, repr(exc))
finally:
    wb_accounts.urlopen = real_urlopen
    T.set_logger(lambda msg: None)

if res is not None:
    check("任务正常收尾", res.get("ok") is True, res)
    check("余额读不回来时报 0 积分",
          any("当前总剩余 0 积分" in line for line in res.get("logs") or []),
          res.get("logs"))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
