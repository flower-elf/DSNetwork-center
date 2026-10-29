"""代理出口槽：探测出口地址与归属，汇总每个槽绑定的账号数，供面板的代理页使用。

网关本身不建连，探测直接走槽里的代理地址取 api.ipify.org；归属查询是一次独立
的公网查询，两步各自成败，调用方据此决定写回哪些字段。
"""

import os
import time
import urllib.request

import wb_accounts
import wb_ipintel
import wb_settings

PROXY_DISCOVER_HOST = os.environ.get("WB_PROXY_DISCOVER_HOST") or "cli-proxy-mihomo"


def _proxy_port_range():
    raw = os.environ.get("WB_PROXY_DISCOVER_PORTS") or "17901-17910"
    if "-" in raw:
        lo, _, hi = raw.partition("-")
        if lo.strip().isdigit() and hi.strip().isdigit():
            return range(int(lo), int(hi) + 1)
    if raw.strip().isdigit():
        return [int(raw)]
    return range(17901, 17911)


def probe_proxy_exit(proxy_url, timeout=12):
    """探测一个代理的出口地址，返回 (exit_ip, error)。"""
    try:
        opener = wb_accounts.opener_for_proxy(proxy_url)
        if opener is None:
            return "", "empty proxy url"
        req = urllib.request.Request("https://api.ipify.org", method="GET")
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace").strip(), ""
    except Exception as exc:
        return "", str(exc)[:160]


def probe_proxy_intel(proxy_url, timeout=12):
    """探测一个代理：出口地址，以及这个出口在哪里、属于什么线路。

    latency_ms 只算代理这一跳，归属查询直接发出，计入就会把延迟算高。
    ok 只表示代理可用；intel_ok 单独表示归属查询是否给出结果，调用方才能
    区分“查询失败”和“查询成功但字段为空”。
    """
    started = time.time()
    exit_ip, error = probe_proxy_exit(proxy_url, timeout=timeout)
    latency_ms = int((time.time() - started) * 1000)
    intel = wb_ipintel.lookup(exit_ip) if exit_ip else wb_ipintel.empty()
    intel_ok = any(str(intel.get(field) or "").strip() for field in wb_ipintel.FIELDS)
    return {
        "ok": not error,
        "exit_ip": exit_ip,
        "latency_ms": latency_ms,
        "error": error,
        "intel_ok": intel_ok,
        "country": intel["country"],
        "country_code": intel["country_code"],
        "ip_type": intel["ip_type"],
        "isp": intel["isp"],
        "asn": intel["asn"],
    }


def discover_proxy_slots():
    """探测配置好的 mihomo 主机与端口，列出可连通的出口。"""
    out = []
    for port in _proxy_port_range():
        url = "http://%s:%d" % (PROXY_DISCOVER_HOST, port)
        probe = probe_proxy_intel(url)
        probe["url"] = url
        probe["reachable"] = probe["ok"]
        out.append(probe)
    return out


def slot_label(entry):
    """槽的显示名：运维填写的名称，其次出口归属，最后是槽 id。"""
    name = str((entry or {}).get("name") or "").strip()
    if name:
        return name
    auto = wb_ipintel.slot_name(entry.get("country"), entry.get("ip_type"))
    return auto or str((entry or {}).get("id") or "")


def proxy_slots_view(pool, accounts_dir):
    """代理槽列表，附上每个槽绑定的启用账号数。

    账号池与账号目录由调用方显式传入，本模块不反向查找网关。
    """
    counts = {}
    if pool:
        for account in pool.accounts:
            slot_id = account.proxy_slot
            if slot_id and account.enabled:
                counts[slot_id] = counts.get(slot_id, 0) + 1
    out = []
    for entry in wb_settings.proxy_slots(accounts_dir):
        item = dict(entry)
        item["bound"] = counts.get(entry["id"], 0)
        item["label"] = slot_label(entry)
        out.append(item)
    return out
