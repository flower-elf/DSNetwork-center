"""性能统计：延迟分位、速率与模型维度矩阵。

按 (sample, realm, 窗口) 维护环形样本，TTL 过期后只并入新增行；口径与整段
重扫一致。数据源是 usage.jsonl 的尾部样本。
"""

import json
import os
import threading
import time
from collections import deque

import wb_usage
from wb_usage import (
    _AT_PREFIX, _gw, _line_outside_window, _log_complete_frontier,
    _log_resume_ok, _log_tail_signature, _tail_lines, _usage_log_key, range_window,
    realm_scope, row_matches_realm, row_outcome, row_realm,
)

def _perf_block(vals):
    """一个指标的汇总块，直接由原始样本算出。

    分位必须从原始样本算，因此收的是数值序列，list 与 deque 都行。排序一次
    得出三个分位；avg 与 max 读原序列，浮点求和顺序与逐次排序的写法一致。
    """
    if not vals:
        return None
    ordered = sorted(vals)
    count = len(ordered)

    def rank(q):
        idx = int(round((q / 100.0) * (count - 1)))
        return ordered[max(0, min(count - 1, idx))]

    return {
        "avg": round(sum(vals) / count, 1),
        "p50": rank(50),
        "p90": rank(90),
        "p99": rank(99),
        "max": max(vals),
        "samples": count,
    }
_perf_cache = {}
_perf_lock = threading.Lock()

def _perf_stats_scope(sample, realm, range, since, until):
    """（过滤用的 realm, since, until, 缓存键），perf_stats() 与它的 ETag 共用。

    取数与派生 ETag 必须用同一套键：两边各写一份，只要有一处不同，校验符
    就会落到别的缓存条目上。
    """
    r = realm_scope(realm, _gw().CURRENT_REALM)
    lo, hi = range_window(range, since, until)
    try:
        # 缓存键带上解析后的时间边界，不带 today/all 标志：本周与本月互相
        # 重叠，标志区分不开，一个窗口的延迟会被算进另一个窗口的标签。
        key = (int(sample), r or "all",
               lo if lo is not None else -1, hi if hi is not None else -1)
    except Exception:
        key = (5000, r or "all",
               lo if lo is not None else -1, hi if hi is not None else -1)
    return r, lo, hi, key

def perf_stats(sample=5000, realm=None, ttl=None, range=None, since=None, until=None):
    """带缓存的包装：解析上万行很吃 CPU，而面板每隔几秒轮询一次。

    重建在锁内做，突发轮询不会各自扫一遍日志。重建本身是增量的，
    _perf_stats_incremental 按 (sample, realm, 窗口) 维持最近 sample 行的环，
    只折叠上次重建后追加的字节。
    """
    ttl = wb_usage._STATS_TTL if ttl is None else ttl
    r, lo, hi, key = _perf_stats_scope(sample, realm, range, since, until)
    now = time.time()
    with _perf_lock:
        hit = _perf_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
        data = _perf_stats_incremental(key, key[0], r, lo, hi)
        _perf_cache[key] = (time.time(), data)
    return data

def perf_stats_etag(sample=5000, realm=None, range=None, since=None, until=None):
    """perf_stats() 当前缓存条目的 ETag，条目还不存在时返回 None。

    _json_cached() 在取数前后各调用一次并比对，两次一致才发出 tag，调用方
    不必自己安排顺序。
    """
    _r, _lo, _hi, key = _perf_stats_scope(sample, realm, range, since, until)
    with _perf_lock:
        hit = _perf_cache.get(key)
    if hit is None:
        return None
    return _gw()._cache_etag("perf", repr(key), hit[0])

def _perf_stats_uncached(sample=5000, realm=None, since=None, until=None):
    """延迟分位与派生速率，从 JSONL 日志逐行计算。"""
    ttfts, gens, walls, hits, tok_rates = [], [], [], [], []
    total = ok = err = aborted = 0
    m_buckets = {}
    # 同一份聚合也按（模型, 区域）分桶：一个模型走两个区域时分别报告延迟。
    mr_buckets = {}
    # 再按（模型, 区域, 账号）分桶：同一区域的多个账号各占一行。
    ma_buckets = {}
    # 只读日志末尾 sample 行：readlines() 会把整份日志读进内存。
    rows = [raw.decode("utf-8", "replace") for raw in _tail_lines(_gw().USAGE_LOG, sample)]
    # 尾部读取只取 sample 行，窗口宽于样本时只有最新的请求得到描述。两个事实
    # 都要上报，矩阵才能说明延迟列只覆盖部分片段。
    sample_capped = len(rows) >= sample
    sample_from = None
    for line in rows:
        line = line.strip()
        if not line:
            continue
        # 解析前先做子串预过滤，落窗口外的行不必付 json.loads。它只丢下面
        # 本来也会丢的行，口径不变；没有窗口时不做检查。sample_from 还没定
        # 下来时不过滤，否则报告范围会从窗口内第一行开始。
        if (since or until) and sample_from is not None and \
                _line_outside_window(line, since or None, until or None):
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if sample_from is None:
            sample_from = r.get("at")
        if realm and not row_matches_realm(r, realm):
            continue
        # 与用量快照同一个窗口：矩阵的延迟和速度列与 token 列描述同一批请求。
        at = r.get("at") or 0
        if since and at < since:
            continue
        if until and at > until:
            continue
        total += 1
        outcome = row_outcome(r)
        # 不管结果如何每行都进模型桶：只有取消请求的模型也以成功数 0 出现在
        # 按模型表里，不会消失。
        m_id = r.get("model") or "unknown"
        r_realm = row_realm(r)
        mb = m_buckets.setdefault(m_id, {"total": 0, "ok": 0, "err": 0, "aborted": 0,
                                        "ttfts": [], "gens": [], "walls": [],
                                        "tok_rates": [], "hits": []})
        rb = mr_buckets.setdefault(m_id, {}).setdefault(
            r_realm, {"total": 0, "ok": 0, "err": 0, "aborted": 0,
                      "ttfts": [], "gens": [], "walls": [],
                      "tok_rates": [], "hits": []})
        ab = ma_buckets.setdefault(m_id, {}).setdefault(r_realm, {}).setdefault(
            r.get("account") or "(unattributed)",
            {"total": 0, "ok": 0, "err": 0, "aborted": 0,
             "ttfts": [], "gens": [], "walls": [],
             "tok_rates": [], "hits": []})
        mb["total"] += 1
        rb["total"] += 1
        ab["total"] += 1
        # 客户端主动断开不算网关失败：不计入成功也不计入失败，单独进一个桶，
        # 不压低成功率。
        if outcome == "client_aborted":
            aborted += 1
            for b in (mb, rb, ab):
                b["aborted"] += 1
                if r.get("elapsed_ms"):
                    b["walls"].append(r["elapsed_ms"])
            if r.get("elapsed_ms"):
                walls.append(r["elapsed_ms"])
            continue
        if outcome != "completed":
            err += 1
            for b in (mb, rb, ab):
                b["err"] += 1
                if r.get("elapsed_ms"):
                    b["walls"].append(r["elapsed_ms"])
            if r.get("elapsed_ms"):
                walls.append(r["elapsed_ms"])
            continue
        ok += 1
        for b in (mb, rb, ab):
            b["ok"] += 1
        if r.get("ttft_ms") is not None:
            ttfts.append(r["ttft_ms"])
            for b in (mb, rb, ab):
                b["ttfts"].append(r["ttft_ms"])
        if r.get("gen_ms") is not None:
            gens.append(r["gen_ms"])
            for b in (mb, rb, ab):
                b["gens"].append(r["gen_ms"])
        if r.get("elapsed_ms") is not None:
            walls.append(r["elapsed_ms"])
            for b in (mb, rb, ab):
                b["walls"].append(r["elapsed_ms"])
        if r.get("tokens_per_sec"):
            tok_rates.append(r["tokens_per_sec"])
            for b in (mb, rb, ab):
                b["tok_rates"].append(r["tokens_per_sec"])
        if r.get("cache_hit_pct") is not None:
            hits.append(r["cache_hit_pct"])
            for b in (mb, rb, ab):
                b["hits"].append(r["cache_hit_pct"])
    block = _perf_block
    return {
        "sampled": total,
        # 样本切片从哪里开始、是否被截断：周/月视图据此说明延迟列并未覆盖
        # 整个窗口。
        "sample_from": sample_from,
        "sample_capped": sample_capped,
        "success": ok,
        "errors": err,
        "client_aborted": aborted,
        # 成功率只对网关实际处理完的请求计算；客户端取消单独上报，不计为失败。
        "success_rate_pct": round(ok * 100.0 / (ok + err), 1) if (ok + err) else None,
        "ttft_ms": block(ttfts),
        "generation_ms": block(gens),
        "wall_ms": block(walls),
        "tokens_per_sec": block(tok_rates),
        "cache_hit_pct": block(hits),
        "by_model": {
            mid: {
                "requests": mb["total"],
                "errors": mb["err"],
                "client_aborted": mb.get("aborted", 0),
                "success_rate_pct": round(mb["ok"] * 100.0 / (mb["ok"] + mb["err"]), 1)
                                     if (mb["ok"] + mb["err"]) else None,
                "ttft_ms": block(mb["ttfts"]),
                "generation_ms": block(mb["gens"]),
                "wall_ms": block(mb["walls"]),
                "tokens_per_sec": block(mb["tok_rates"]),
                "cache_hit_pct": block(mb["hits"]),
            } for mid, mb in m_buckets.items()
        },
        "by_model_realm": {
            mid: {
                realm: {
                    "requests": rb["total"],
                    "errors": rb["err"],
                    "client_aborted": rb.get("aborted", 0),
                    "success_rate_pct": round(rb["ok"] * 100.0 / (rb["ok"] + rb["err"]), 1)
                                         if (rb["ok"] + rb["err"]) else None,
                    "ttft_ms": block(rb["ttfts"]),
                    "generation_ms": block(rb["gens"]),
                    "wall_ms": block(rb["walls"]),
                    "tokens_per_sec": block(rb["tok_rates"]),
                    "cache_hit_pct": block(rb["hits"]),
                } for realm, rb in realms.items()
            } for mid, realms in mr_buckets.items()
        },
        "by_model_acct": {
            mid: {
                realm: {
                    acct: {
                        "requests": ab["total"],
                        "errors": ab["err"],
                        "client_aborted": ab.get("aborted", 0),
                        "success_rate_pct": round(ab["ok"] * 100.0 / (ab["ok"] + ab["err"]), 1)
                                             if (ab["ok"] + ab["err"]) else None,
                        "ttft_ms": block(ab["ttfts"]),
                        "generation_ms": block(ab["gens"]),
                        "wall_ms": block(ab["walls"]),
                        "tokens_per_sec": block(ab["tok_rates"]),
                        "cache_hit_pct": block(ab["hits"]),
                    } for acct, ab in accts.items()
                } for realm, accts in realms.items()
            } for mid, realms in ma_buckets.items()
        }
    }

# ---------------------------------------------------------------------------
# perf 的增量样本。
#
# perf_stats 每次重建都要重读日志末尾 sample 行、逐行解析、把五个数值往全局
# 与三层模型桶各追加一次，再排序求分位。这里按（sample, realm, 窗口）记一份
# 环形样本状态：ring 是最近 sample 条非空行的折叠结果，agg 是这些行对聚合的
# 贡献，offset 是已折到的字节位置。TTL 过期后的重建只并入 offset 之后的新行、
# 撤销滑出样本的行；位置校验不过（截断、换文件、同尺寸重写）才从日志尾部重建。
# 口径与全量扫描一致，状态不落盘。
_perf_state = {}
# 面板用到的键只有两三个（不带窗口的一个、带窗口的一个），多出来的按最近使用
# 淘汰，避免来回切 range 时状态无限堆积。单份状态的内存与 sample 成正比，
# 上限取 4 份；淘汰只让下次重建一次，不影响输出。
_PERF_STATE_MAX = 4
# ring 条目的形状，见 _perf_line_entry：短元组 = 不贡献聚合的行。
_PERF_REC_LEN = 10

def _perf_new_bucket():
    return {"total": 0, "ok": 0, "err": 0, "aborted": 0,
            "ttfts": deque(), "gens": deque(), "walls": deque(),
            "tok_rates": deque(), "hits": deque()}

def _perf_new_agg():
    return {"total": 0, "ok": 0, "err": 0, "aborted": 0,
            "ttfts": deque(), "gens": deque(), "walls": deque(),
            "tok_rates": deque(), "hits": deque(),
            "m": {}, "mr": {}, "ma": {}}

def _perf_put(vals, value, add, front):
    """一个数值贡献：并入时追加，撤销时从对应那一头弹出。

    滑出样本的是最旧的行（front），重折上一轮末尾半行时摘掉的是最新的行
    （back）。每一头只对应同一行，撤销因此是 O(1)。
    """
    if add > 0:
        vals.append(value)
    elif front:
        vals.popleft()
    else:
        vals.pop()

def _perf_apply(state, entry, add, front):
    """并入（add=1）或撤销（add=-1）一行对聚合的贡献。

    撤销只回退并入做过的事：计数减回去、数值表从并进的那一头弹出、桶的 total
    归零就删掉，重建时桶的存在与否只看样本里还有没有它的行。删掉的桶后来再有
    行进来会重建在末尾，与重建时的首次出现顺序一致。判定条件与
    _perf_stats_uncached 的逐行循环逐句对应。
    """
    if entry is None or len(entry) < _PERF_REC_LEN:
        return
    _at, m_id, r_realm, acct, outcome, elapsed, ttft, gen, tps, hit = entry
    agg = state["agg"]
    if add > 0:
        mb = agg["m"].get(m_id)
        if mb is None:
            mb = agg["m"][m_id] = _perf_new_bucket()
        realms = agg["mr"].get(m_id)
        if realms is None:
            realms = agg["mr"][m_id] = {}
        rb = realms.get(r_realm)
        if rb is None:
            rb = realms[r_realm] = _perf_new_bucket()
        accts = agg["ma"].get(m_id)
        if accts is None:
            accts = agg["ma"][m_id] = {}
        by_realm = accts.get(r_realm)
        if by_realm is None:
            by_realm = accts[r_realm] = {}
        ab = by_realm.get(acct)
        if ab is None:
            ab = by_realm[acct] = _perf_new_bucket()
    else:
        mb = agg["m"][m_id]
        rb = agg["mr"][m_id][r_realm]
        ab = agg["ma"][m_id][r_realm][acct]
    agg["total"] += add
    for b in (mb, rb, ab):
        b["total"] += add
    # 客户端主动断开不算网关失败：单独计入 aborted，不压低成功率，与全量
    # 扫描口径相同。
    if outcome == "client_aborted":
        agg["aborted"] += add
        for b in (mb, rb, ab):
            b["aborted"] += add
            if elapsed:
                _perf_put(b["walls"], elapsed, add, front)
        if elapsed:
            _perf_put(agg["walls"], elapsed, add, front)
    elif outcome != "completed":
        agg["err"] += add
        for b in (mb, rb, ab):
            b["err"] += add
            if elapsed:
                _perf_put(b["walls"], elapsed, add, front)
        if elapsed:
            _perf_put(agg["walls"], elapsed, add, front)
    else:
        agg["ok"] += add
        for b in (mb, rb, ab):
            b["ok"] += add
        if ttft is not None:
            _perf_put(agg["ttfts"], ttft, add, front)
            for b in (mb, rb, ab):
                _perf_put(b["ttfts"], ttft, add, front)
        if gen is not None:
            _perf_put(agg["gens"], gen, add, front)
            for b in (mb, rb, ab):
                _perf_put(b["gens"], gen, add, front)
        if elapsed is not None:
            _perf_put(agg["walls"], elapsed, add, front)
            for b in (mb, rb, ab):
                _perf_put(b["walls"], elapsed, add, front)
        if tps:
            _perf_put(agg["tok_rates"], tps, add, front)
            for b in (mb, rb, ab):
                _perf_put(b["tok_rates"], tps, add, front)
        if hit is not None:
            _perf_put(agg["hits"], hit, add, front)
            for b in (mb, rb, ab):
                _perf_put(b["hits"], hit, add, front)
    if add < 0:
        if not mb["total"]:
            del agg["m"][m_id]
        if not rb["total"]:
            del agg["mr"][m_id][r_realm]
            if not agg["mr"][m_id]:
                del agg["mr"][m_id]
        if not ab["total"]:
            del agg["ma"][m_id][r_realm][acct]
            if not agg["ma"][m_id][r_realm]:
                del agg["ma"][m_id][r_realm]
            if not agg["ma"][m_id]:
                del agg["ma"][m_id]

def _perf_at_token(line):
    """预过滤认可的写法里 at 的浮点值，读不到返回 None。

    _line_outside_window 判定为窗外的行，形状已被它逐条核过：以 `{"at": ` 开头、
    值以逗号收尾、是浮点字面量、后面没有第二个 at 键，所以这里读到的值与
    json.loads 相同。只有被预过滤跳过的行需要它，用来确定 sample_from。
    """
    if not line.startswith(_AT_PREFIX):
        return None
    end = line.find(",", len(_AT_PREFIX))
    if end < 0:
        return None
    token = line[len(_AT_PREFIX):end]
    if "." not in token and "e" not in token and "E" not in token:
        return None
    try:
        return float(token)
    except ValueError:
        return None

def _perf_line_entry(state, line):
    """把一行日志折成 ring 条目，与 _perf_stats_uncached 的逐行循环逐句对应。

    None 表示没有可用的行数据；`(at,)` 表示解析成功但被 realm 或窗口过滤掉，
    它不贡献聚合，但仍决定 sample_from；10 元组表示通过全部过滤，计入聚合。
    """
    since = state["since"]
    until = state["until"]
    realm = state["realm"]
    # 廉价预过滤只丢窗口判断本来也会丢的行，口径不变；样本里还没有 at 非空
    # 的行（n_at 为 0）时不过滤。跳过的行仍要记下它声明的 at 供 sample_from
    # 用，所以额外要求 line.endswith("}")：写了一半的行走不了跳过分支，照旧
    # 进 json.loads，sample_from 不会采信未写完的行。完整对象行总以 } 收尾，
    # 这道判断不改变合法行的过滤结果。
    if (since or until) and state["n_at"] and line.endswith("}") and \
            _line_outside_window(line, since or None, until or None):
        return (_perf_at_token(line),)
    try:
        r = json.loads(line)
    except Exception:
        return None
    at = r.get("at")
    if realm and not row_matches_realm(r, realm):
        return (at,)
    at_value = at or 0
    if since and at_value < since:
        return (at,)
    if until and at_value > until:
        return (at,)
    return (at, r.get("model") or "unknown", row_realm(r),
            r.get("account") or "(unattributed)", row_outcome(r),
            r.get("elapsed_ms"), r.get("ttft_ms"), r.get("gen_ms"),
            r.get("tokens_per_sec"), r.get("cache_hit_pct"))

def _perf_drop_oldest(state):
    """滑出环里最旧的一行（含它对聚合的贡献）。"""
    old = state["ring"].popleft()
    if old is not None and old[0] is not None:
        state["n_at"] -= 1
    _perf_apply(state, old, -1, True)

def _perf_push(state, line):
    """把一行并进环：先腾位置，再折行、并入。

    腾位置必须在折行之前：预过滤的开关是“样本里已有行的 at 非空”，滑出的行
    不再算数；先折后淘汰会让新行按旧样本的口径被跳过，sample_from 跟着变。
    """
    sample = state["sample"]
    if sample <= 0:
        return
    ring = state["ring"]
    while len(ring) >= sample:
        _perf_drop_oldest(state)
    entry = _perf_line_entry(state, line)
    if entry is not None and entry[0] is not None:
        state["n_at"] += 1
    _perf_apply(state, entry, 1, False)
    ring.append(entry)

def _perf_pop_last(state):
    """摘掉环尾的条目（上一轮的半行，重折前先撤掉它的贡献）。"""
    ring = state["ring"]
    if not ring:
        return
    old = ring.pop()
    if old is not None and old[0] is not None:
        state["n_at"] -= 1
    _perf_apply(state, old, -1, False)

def _perf_fold(state):
    """把 offset 之后的新行并进状态，尾部半行也作为样本的最后一行并进去。

    整行推进 offset，半行不推进（内容可能被写完整，下次从同一位置重折）；半行
    在样本里算一行，所以它进环，重折前先摘掉上一轮那一份。

    返回样本是否变化，没变时渲染可以整段跳过。
    """
    if state["pending"]:
        _perf_pop_last(state)
        state["pending"] = False
    offset = state["offset"]
    pending_raw = None
    try:
        with open(_gw().USAGE_LOG, "rb") as fh:
            fh.seek(offset)
            while True:
                raw = fh.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    pending_raw = raw
                    break
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    _perf_push(state, line)
                offset += len(raw)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _gw().log("perf incremental read failed: %s" % exc)
    changed = offset != state["offset"] or pending_raw != state["pending_raw"]
    state["offset"] = offset
    state["pending_raw"] = pending_raw
    if pending_raw is not None and pending_raw.strip() and state["sample"] > 0:
        line = pending_raw.decode("utf-8", "replace").strip()
        if line:
            _perf_push(state, line)
            state["pending"] = True
    return changed

def _perf_payload(state):
    """把环形样本的聚合渲染成载荷，键序与字段和全量扫描逐字一致。

    三层桶的顺序取各桶在样本里第一次出现的顺序。增量维护要在每次淘汰时重排，
    比渲染时扫一遍环更贵；这次扫描只读环、不解析，桶见齐就停。
    """
    agg = state["agg"]
    order = {}          # model -> realm -> account（插入序 = 第一次出现序）
    sample_from = None
    want = len(agg["m"])
    for realms in agg["mr"].values():
        want += len(realms)
    for realms in agg["ma"].values():
        for accts in realms.values():
            want += len(accts)
    seen = 0
    for entry in state["ring"]:
        if entry is None:
            continue
        if sample_from is None and entry[0] is not None:
            sample_from = entry[0]
        if len(entry) < _PERF_REC_LEN:
            continue
        realms = order.get(entry[1])
        if realms is None:
            realms = order[entry[1]] = {}
            seen += 1
        accts = realms.get(entry[2])
        if accts is None:
            accts = realms[entry[2]] = {}
            seen += 1
        if entry[3] not in accts:
            accts[entry[3]] = None
            seen += 1
        if seen >= want:
            break
    by_model = {}
    by_model_realm = {}
    by_model_acct = {}
    for mid, realms in order.items():
        mb = agg["m"][mid]
        by_model[mid] = {
            "requests": mb["total"],
            "errors": mb["err"],
            "client_aborted": mb["aborted"],
            "success_rate_pct": round(mb["ok"] * 100.0 / (mb["ok"] + mb["err"]), 1)
                                 if (mb["ok"] + mb["err"]) else None,
            "ttft_ms": _perf_block(mb["ttfts"]),
            "generation_ms": _perf_block(mb["gens"]),
            "wall_ms": _perf_block(mb["walls"]),
            "tokens_per_sec": _perf_block(mb["tok_rates"]),
            "cache_hit_pct": _perf_block(mb["hits"]),
        }
        realm_out = {}
        acct_out = {}
        for realm, accts in realms.items():
            rb = agg["mr"][mid][realm]
            realm_out[realm] = {
                "requests": rb["total"],
                "errors": rb["err"],
                "client_aborted": rb["aborted"],
                "success_rate_pct": round(rb["ok"] * 100.0 / (rb["ok"] + rb["err"]), 1)
                                     if (rb["ok"] + rb["err"]) else None,
                "ttft_ms": _perf_block(rb["ttfts"]),
                "generation_ms": _perf_block(rb["gens"]),
                "wall_ms": _perf_block(rb["walls"]),
                "tokens_per_sec": _perf_block(rb["tok_rates"]),
                "cache_hit_pct": _perf_block(rb["hits"]),
            }
            accts_out = {}
            for acct in accts:
                ab = agg["ma"][mid][realm][acct]
                accts_out[acct] = {
                    "requests": ab["total"],
                    "errors": ab["err"],
                    "client_aborted": ab["aborted"],
                    "success_rate_pct": round(ab["ok"] * 100.0 / (ab["ok"] + ab["err"]), 1)
                                         if (ab["ok"] + ab["err"]) else None,
                    "ttft_ms": _perf_block(ab["ttfts"]),
                    "generation_ms": _perf_block(ab["gens"]),
                    "wall_ms": _perf_block(ab["walls"]),
                    "tokens_per_sec": _perf_block(ab["tok_rates"]),
                    "cache_hit_pct": _perf_block(ab["hits"]),
                }
            acct_out[realm] = accts_out
        by_model_realm[mid] = realm_out
        by_model_acct[mid] = acct_out
    return {
        "sampled": agg["total"],
        # 样本切片从哪里开始、是否被截断：周/月视图据此说明延迟列并未覆盖
        # 整个窗口。
        "sample_from": sample_from,
        "sample_capped": len(state["ring"]) >= state["sample"],
        "success": agg["ok"],
        "errors": agg["err"],
        "client_aborted": agg["aborted"],
        # 成功率只对网关实际处理完的请求计算；客户端取消单独上报，不计为失败。
        "success_rate_pct": round(agg["ok"] * 100.0 / (agg["ok"] + agg["err"]), 1)
                             if (agg["ok"] + agg["err"]) else None,
        "ttft_ms": _perf_block(agg["ttfts"]),
        "generation_ms": _perf_block(agg["gens"]),
        "wall_ms": _perf_block(agg["walls"]),
        "tokens_per_sec": _perf_block(agg["tok_rates"]),
        "cache_hit_pct": _perf_block(agg["hits"]),
        "by_model": by_model,
        "by_model_realm": by_model_realm,
        "by_model_acct": by_model_acct,
    }

def _perf_rebuild(key, sample, realm, since, until):
    """冷启动 / 位置校验不过：从日志尾部的 sample 行重建环形状态。"""
    state = {"sample": sample, "realm": realm, "since": since, "until": until,
             "key": None, "offset": 0, "tail": b"", "ring": deque(),
             "n_at": 0, "pending": False, "pending_raw": None,
             "agg": _perf_new_agg(), "at": 0.0,
             "dirty": True, "payload": None}
    raw_lines = _tail_lines(_gw().USAGE_LOG, sample)
    frontier = _log_complete_frontier()
    try:
        size = os.path.getsize(_gw().USAGE_LOG)
    except OSError:
        size = 0
    pending_raw = None
    if size > frontier:
        try:
            with open(_gw().USAGE_LOG, "rb") as fh:
                fh.seek(frontier)
                pending_raw = fh.read()
        except OSError:
            pending_raw = None
    # 尾部半行由 _tail_lines 算进样本的最后一行（末尾残余非空白时），这里把它
    # 从"完整行"里摘出来单独折——下面 offset 停在它前面，重折路径与它一致。
    if raw_lines and pending_raw is not None and raw_lines[-1] == pending_raw:
        raw_lines = raw_lines[:-1]
    for raw in raw_lines:
        line = raw.decode("utf-8", "replace").strip()
        if line:
            _perf_push(state, line)
    state["offset"] = frontier
    if pending_raw is not None and pending_raw.strip() and sample > 0:
        line = pending_raw.decode("utf-8", "replace").strip()
        if line:
            _perf_push(state, line)
            state["pending"] = True
    state["key"] = _usage_log_key()
    state["tail"] = _log_tail_signature(frontier)
    state["at"] = time.time()
    _perf_state[key] = state
    return state

def _perf_trim_states():
    """状态数超上限时按最近使用淘汰（载荷缓存另有 TTL，不受影响）。"""
    while len(_perf_state) > _PERF_STATE_MAX:
        oldest = min(_perf_state, key=lambda k: _perf_state[k]["at"])
        del _perf_state[oldest]

def _perf_stats_incremental(key, sample, realm, since, until):
    """perf_stats 的增量重建：输出与 _perf_stats_uncached 逐字节一致。

    必须在 _perf_lock 里调用（perf_stats 就是这么调的）：状态与载荷缓存由
    同一把锁保护。
    """
    try:
        state = _perf_state.get(key)
        log_key = _usage_log_key()
        if state is None or not _log_resume_ok(state, log_key):
            # 没有状态，或日志被截断、换掉、同尺寸重写（位置校验见
            # _log_resume_ok）：从尾部样本重建。
            state = _perf_rebuild(key, sample, realm, since, until)
        else:
            if _perf_fold(state):
                state["dirty"] = True
            state["key"] = _usage_log_key()
            state["tail"] = _log_tail_signature(state["offset"])
        if state["dirty"] or state["payload"] is None:
            # 样本没变就不重渲染：0 新字节的 TTL 重算只花几十微秒，不对每个
            # 桶重复排序求分位。
            state["payload"] = _perf_payload(state)
            state["dirty"] = False
        payload = state["payload"]
        state["at"] = time.time()
        _perf_trim_states()
        return payload
    except Exception as exc:
        # 任何意外都退回整段重建：状态作废，不留半折的账，这一次仍然给出
        # 正确载荷。
        _perf_state.pop(key, None)
        _gw().log("perf incremental failed, falling back to full scan: %s" % exc)
        return _perf_stats_uncached(sample, realm, since=since, until=until)
