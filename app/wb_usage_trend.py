"""看板趋势图的按桶统计：usage.jsonl 按天或按小时分桶，无数据的桶补零。

网关运行态（日志路径与日志输出）经 wb_usage 的 _gw() 读活值，测试替换网关
属性后立即生效。
"""

import json
import threading
import time

import wb_usage
import wb_usage_recent
from wb_usage import _gw

_trend_cache = {}
_trend_cache_lock = threading.Lock()
_range = range


def _trend_day_start(ts):
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def usage_trend(days=None, realm=None, range=None, since=None, until=None, ttl=None):
    """看板趋势图的按桶统计；无数据的桶补零。"""
    ttl = wb_usage._STATS_TTL if ttl is None else float(ttl)
    now = time.time()
    realm = wb_usage.realm_scope(realm) or "all"
    range_key = str(range or "").strip().lower()
    step = 86400
    granularity = "day"
    bucket_start = wb_usage._local_midnight(days_back=13)
    count = 14
    window_until = now

    if days not in (None, ""):
        try:
            count = max(1, min(90, int(days)))
        except (TypeError, ValueError):
            count = 14
        bucket_start = int(wb_usage._local_midnight(days_back=count - 1))
    elif range_key == "today":
        step = 3600
        granularity = "hour"
        bucket_start = _trend_day_start(now)
        count = 24
        window_until = bucket_start + 86400
    elif range_key in ("week", "month"):
        lo, _hi = wb_usage.range_window(range_key)
        bucket_start = int(lo)
        count = int((_trend_day_start(now) - bucket_start) // 86400) + 1
    elif range_key == "all":
        count = 90
        bucket_start = int(wb_usage._local_midnight(days_back=count - 1))
    elif range_key == "custom":
        try:
            start_raw = float(since) if since is not None else now
        except (TypeError, ValueError):
            start_raw = now
        bucket_start = _trend_day_start(start_raw)
        if until is not None:
            try:
                window_until = float(until)
            except (TypeError, ValueError):
                window_until = now
        count = max(1, int((_trend_day_start(window_until) - bucket_start) // 86400) + 1)
    bucket_start = int(bucket_start)

    cache_key = (granularity, bucket_start, count, realm, window_until)
    with _trend_cache_lock:
        hit = _trend_cache.get(cache_key)
        if hit is not None and ttl > 0 and (now - hit[0]) < ttl:
            return hit[1]

    buckets = []
    for i in _range(count):
        start = bucket_start + i * step
        label = time.strftime("%m-%d" if step >= 86400 else "%H:00",
                              time.localtime(start))
        buckets.append({"start": start, "label": label, "requests": 0,
                        "errors": 0, "total_tokens": 0, "credit": 0.0})
    try:
        with open(_gw().USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict) or not wb_usage.row_matches_realm(row, realm):
                    continue
                at = row.get("at") or 0
                if at < bucket_start or at > window_until:
                    continue
                idx = int((at - bucket_start) // step)
                if idx < 0 or idx >= count:
                    continue
                if wb_usage.row_outcome(row) == "client_aborted":
                    continue
                bucket = buckets[idx]
                if wb_usage_recent._recent_row_failed(row):
                    bucket["errors"] += 1
                else:
                    bucket["requests"] += 1
                bucket["total_tokens"] += row.get("total_tokens") or 0
                spent = row.get("credit")
                if isinstance(spent, (int, float)) and not isinstance(spent, bool):
                    bucket["credit"] += float(spent)
    except FileNotFoundError:
        pass
    except Exception as exc:
        _gw().log("usage trend read failed: %s" % exc)
    for bucket in buckets:
        bucket["credit"] = round(bucket["credit"], 6)
    payload = {"realm": realm, "granularity": granularity,
               "window": {"since": bucket_start, "until": window_until},
               "buckets": buckets}
    if ttl > 0:
        with _trend_cache_lock:
            _trend_cache[cache_key] = (now, payload)
    return payload
