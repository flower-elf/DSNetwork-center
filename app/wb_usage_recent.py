"""recent 分页行的取数：行偏移索引把页深换成一次 seek，答不了时退回尾部扫描。

索引与日志同生命周期，位置校验复用用量模块的日志指纹。
"""
import json
import os
import threading
import time

import wb_pricing
import wb_settings

from wb_usage_cache import _usage_cache_counts_ok, _usage_cache_int

from wb_usage import (
    _account_realm_map, _count_usage_line, _ensure_dir, _gw, _key_usage_row_id,
    _log_offset_holds, _log_tail_signature, _realm_fold_reproducible,
    _usage_log_key, count_usage_rows, realm_scope, row_matches_realm, row_realm,
)

# 索引单独落盘，不并进 checkpoint：对象只有日志本身，校验与写盘策略跟着日志走。
_RECENT_INDEX_SCHEMA = 1
# 索引粒度：每 64KB 记一条行边界，4 万行的日志只有 350 条上下。
_RECENT_INDEX_SEGMENT = 65536
# realm 表容量：真实日志只有 intl/cn，超过说明 realm 字段被乱写，索引直接不用
# （扫描路径照常给出正确结果）。
_RECENT_INDEX_REALM_CAP = 6
# realm 值长度上限：手改过的行可以带出很长的值。
_RECENT_INDEX_REALM_MAX = 128
def _recent_index_enabled():
    """索引总开关，默认关；面板设置可打开。"""
    return wb_settings.recent_index(_gw().ACCOUNTS_DIR)

def _recent_index_min_bytes():
    """写盘节流：推进不足这么多字节就不写，默认 256KB。

    阈值就是重启后最多回放多少：真机 1MB ≈ 0.14 s、256KB ≈ 35 ms。索引只有几
    KB、写盘便宜（日志每天涨 6 MB → 一天几十次写），阈值比 checkpoint 小一档。
    """
    try:
        return max(0, int(os.environ.get("WB_RECENT_INDEX_MIN_BYTES", 256 * 1024)))
    except (TypeError, ValueError):
        return 256 * 1024

def _recent_index_min_seconds():
    """写盘节流：距上次尝试不足这么多秒就不写。默认 900s。"""
    try:
        return max(0.0, float(os.environ.get("WB_RECENT_INDEX_MIN_SECONDS", 900)))
    except (TypeError, ValueError):
        return 900.0

def _recent_index_stamp():
    """索引文件的（路径, mtime_ns, size）；不在时后两项为 None。路径也在戳里。"""
    try:
        st = os.stat(_gw().RECENT_INDEX_FILE)
        return (_gw().RECENT_INDEX_FILE, st.st_mtime_ns, st.st_size)
    except OSError:
        return (_gw().RECENT_INDEX_FILE, None, None)

def _recent_index_realm_map_json(disk):
    """磁盘那份 uid→realm 的 JSON 形态（与索引文件里的比）。"""
    if disk is None:
        return None
    return [[uid, realm] for uid, realm in disk]

def _recent_index_realm_name(value):
    """realm 表里的键：只收能出现在查询里的短字符串，其余（手改行带出的数字或
    超长值）让整份索引作废；扫描路径照常给出正确结果。
    """
    if isinstance(value, str) and len(value) <= _RECENT_INDEX_REALM_MAX:
        return value
    return None

def _recent_index_realm_ok(state):
    """realm 数字能不能信这份索引：只校验「没带 realm 字段、带 account」的行。

    这类行的归属查活池（别的行由行内字段决定，任何时刻都一样）。有这类行时
    要求折进来时的 uid→realm 与现在一致、且活池与磁盘一致（_realm_fold_reproducible），
    两者合起来把归属固定，索引的 realm 计数与现算逐字相同。
    """
    if not state["legacy"]:
        return True
    realm_map = state["realm_map"]
    if realm_map is None:
        return False
    disk = _account_realm_map()
    if _recent_index_realm_map_json(disk) != realm_map:
        return False
    return _realm_fold_reproducible(disk)

def _recent_index_blank():
    """一份空索引的核心字段（写盘形态与内存形态共用）。"""
    return {"key": None, "size": 0, "tail": b"", "realm_map": None,
            "legacy": False, "realms": [], "counts": {}, "row_counts": {},
            "lines": 0, "rows": 0, "segs": []}

_recent_index_lock = threading.Lock()
# 索引状态（_recent_index_lock 保护）：stamp 是索引文件戳（外部改动靠它发现）；
# usable 表示位置校验通过、可服务查询；giveup 是扫描撞上处理不了的行、本进程
# 不再重试（日志换掉才重来）；key/size/tail 与聚合同款位置信息，索引覆盖到 size；
# realm_map 是折进来时的磁盘 uid→realm；legacy 表示有归属要查活池的行；
# realms/counts/row_counts 是 realm 表与两个口径的计数（recent 口径按段存）；
# segs 是 [offset, rows, <row_counts 各列>] 累计值；wrote_size/at 是写盘节流。
_recent_index_state = {
    "stamp": None, "usable": False, "giveup": False, "giveup_key": None,
    "key": None, "size": 0, "tail": b"", "realm_map": None, "legacy": False,
    "realms": [], "counts": {}, "row_counts": {}, "lines": 0, "rows": 0,
    "segs": [], "wrote_size": 0, "wrote_at": 0.0,
}

def _recent_index_restart(state, realm_map):
    """清回零重新折（保留文件戳与写盘节流）：位置或归属口径变了才走这里。"""
    state.update(_recent_index_blank())
    state.update({"usable": False, "giveup": False, "giveup_key": None,
                  "realm_map": realm_map})

def _recent_index_add_realm(state, name):
    """把一个新 realm 值加进表，段数组同步补一列零（补零不改变任何累计值）。"""
    if name in state["realms"]:
        return True
    if len(state["realms"]) >= _RECENT_INDEX_REALM_CAP:
        return False
    state["realms"].append(name)
    for seg in state["segs"]:
        seg.append(0)
    state["counts"].setdefault(name, 0)
    state["row_counts"].setdefault(name, 0)
    return True

def _recent_index_dirty(state):
    """扫描撞上索引处理不了的行：整份丢掉，本进程不再重试。

    _count_usage_line 可能已写了半个计数，续读会得到错的累计值；日志被换掉
    （key 变）才再试，否则每次调用都重扫比扫描路径还贵。
    """
    state.update(_recent_index_blank())
    state.update({"usable": False, "giveup": True,
                  "giveup_key": _usage_log_key()})
    return False

def _recent_index_fold_line(state, line):
    """一行折进两个口径；返回 False 表示这行让索引不能用了（见上）。"""
    counts = state["counts"]
    before = len(counts)
    shim = {"n": state["lines"], "n_realm": counts}
    try:
        _count_usage_line(shim, line)
    except Exception:
        return _recent_index_dirty(state)
    state["lines"] = shim["n"]
    if len(counts) != before:
        for name in list(counts):
            if name in state["realms"]:
                continue
            good = _recent_index_realm_name(name)
            if good is None or not _recent_index_add_realm(state, good):
                return _recent_index_dirty(state)
    try:
        row = json.loads(line)
    except Exception:
        return True                     # 坏 JSON：算行数、不算记录（D5 同款）
    if not isinstance(row, dict):
        # 计价只接受对象行；出现别的形状就作废索引，退回逐页扫描。
        return _recent_index_dirty(state)
    if not row.get("realm") and row.get("account"):
        state["legacy"] = True          # 这一行的归属要查活池
    try:
        name = row_realm(row)
    except Exception:
        return _recent_index_dirty(state)
    name = _recent_index_realm_name(name)
    if name is None or not _recent_index_add_realm(state, name):
        return _recent_index_dirty(state)
    state["rows"] += 1
    state["row_counts"][name] = state["row_counts"].get(name, 0) + 1
    return True

def _recent_index_extend(state):
    """把索引推进到日志尾部；返回是否可用。

    与 _count_usage_scan 同一套字节纪律：整行推进、半行留给下一趟、解码失败即停；
    停下的位置就是 size，半行与坏字节只让索引暂时不可用。
    """
    if state["giveup"]:
        return False
    offset = state["size"]
    try:
        with open(_gw().USAGE_LOG, "rb") as fh:
            fh.seek(offset)
            while True:
                raw = fh.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    state["size"] = offset      # 半行（写入中/掉电碎片）
                    return False
                end = offset + len(raw)
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError:
                    state["size"] = offset      # 解码失败：索引停在这行之前
                    return False
                if line and not _recent_index_fold_line(state, line):
                    if not state["giveup"]:
                        state["size"] = offset
                    return False
                offset = end
                last = state["segs"][-1][0] if state["segs"] else 0
                if offset - last >= _RECENT_INDEX_SEGMENT:
                    state["segs"].append(
                        [offset, state["rows"]]
                        + [state["row_counts"].get(r, 0)
                           for r in state["realms"]])
    except OSError:
        state["size"] = offset
        return False
    except Exception:
        state["size"] = offset
        return False
    state["size"] = offset
    state["key"] = _usage_log_key()
    state["tail"] = _log_tail_signature(offset)
    if state["tail"] is None:
        return False                        # 签名读不回来：位置校验立不住
    state["usable"] = True
    return True

def _recent_index_payload(state):
    """索引的 JSON 形态：段用增量写（offset 差 + 各列计数差），累计值加载时加回。

    增量的数字都小，4 万行的日志整份不到 10KB。不排序键：段数组位置敏感、形状固定。
    """
    segs = []
    prev_off = 0
    prev = [0] * (2 + len(state["realms"]))
    for seg in state["segs"]:
        row = [seg[0] - prev_off]
        for i in range(1, len(seg)):
            row.append(seg[i] - prev[i])
        segs.append(row)
        prev_off = seg[0]
        prev = seg
    return {
        "schema": _RECENT_INDEX_SCHEMA,
        "seg": _RECENT_INDEX_SEGMENT,
        "key": list(state["key"] or (0, 0, 0)),
        "tail": state["tail"].hex(),
        "realm_map": state["realm_map"],
        "legacy": bool(state["legacy"]),
        "realms": list(state["realms"]),
        "totals": {"lines": state["lines"], "rows": state["rows"],
                   "counts": dict(state["counts"]),
                   "row_counts": dict(state["row_counts"])},
        "segs": segs,
    }

def _recent_index_write(state):
    """原子写索引（同目录 .tmp + os.replace，同 checkpoint / estimate-daily）。"""
    _ensure_dir(_gw().USAGE_DIR)
    tmp = _gw().RECENT_INDEX_FILE + ".tmp"
    text = json.dumps(_recent_index_payload(state), ensure_ascii=False,
                      separators=(",", ":"))
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, _gw().RECENT_INDEX_FILE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _recent_index_parse(data):
    """磁盘上的索引 → 内存状态；任何一处不合形状都返回 None（当作没有索引）。"""
    if not isinstance(data, dict) or data.get("schema") != _RECENT_INDEX_SCHEMA:
        return None
    if data.get("seg") != _RECENT_INDEX_SEGMENT:
        return None
    realms = data.get("realms")
    if not isinstance(realms, list) or len(realms) > _RECENT_INDEX_REALM_CAP:
        return None
    for name in realms:
        if _recent_index_realm_name(name) is None:
            return None
    if len(set(realms)) != len(realms):
        return None
    totals = data.get("totals")
    if not isinstance(totals, dict):
        return None
    lines = totals.get("lines")
    rows = totals.get("rows")
    counts = totals.get("counts")
    row_counts = totals.get("row_counts")
    if (not _usage_cache_int(lines) or lines < 0
            or not _usage_cache_int(rows) or rows < 0):
        return None
    if not _usage_cache_counts_ok(counts) or not _usage_cache_counts_ok(row_counts):
        return None
    key = data.get("key")
    if (not isinstance(key, list) or len(key) != 3
            or not all(_usage_cache_int(v) and v >= 0 for v in key)):
        return None
    tail = data.get("tail")
    if not isinstance(tail, str):
        return None
    try:
        tail_bytes = bytes.fromhex(tail)
    except ValueError:
        return None
    realm_map = data.get("realm_map")
    if realm_map is not None:
        if not isinstance(realm_map, list):
            return None
        for item in realm_map:
            if (not isinstance(item, list) or len(item) != 2
                    or not isinstance(item[0], str)
                    or not isinstance(item[1], str)):
                return None
    if not isinstance(data.get("legacy"), bool):
        # legacy 决定 realm 数字信不信这份索引，写成别的一律当坏文件（宁可重扫）。
        return None
    segs_raw = data.get("segs")
    if not isinstance(segs_raw, list):
        return None
    width = 2 + len(realms)
    segs = []
    off = 0
    prev = [0] * width
    for raw in segs_raw:
        if (not isinstance(raw, list) or len(raw) != width
                or not all(_usage_cache_int(v) and v >= 0 for v in raw)):
            return None
        off += raw[0]
        cur = [off]
        for i in range(1, width):
            cur.append(prev[i] + raw[i])
        segs.append(cur)
        prev = cur
    state = _recent_index_blank()
    state.update({
        "key": tuple(key), "size": key[0], "tail": tail_bytes,
        "realm_map": realm_map, "legacy": data.get("legacy") is True,
        "realms": list(realms), "counts": dict(counts),
        "row_counts": dict(row_counts), "lines": lines, "rows": rows,
        "segs": segs,
    })
    return state

def _recent_index_adopt(state, stamp):
    """文件戳变了：把磁盘那份读进来，只接受「比内存这份更新」的文件。

    内存那份是同一份日志的续读，回退到更短的磁盘版本等于白扫；读不出来或校验
    不过就当没有索引，下一趟从零重建（坏文件不报错）。
    """
    data = None
    if stamp[1] is not None:
        try:
            with open(_gw().RECENT_INDEX_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            data = None
    loaded = _recent_index_parse(data) if data is not None else None
    if loaded is not None:
        log_key = _usage_log_key()
        if (_log_offset_holds(loaded["key"], log_key, loaded["size"])
                and _log_tail_signature(loaded["size"]) == loaded["tail"]):
            if not state["usable"] or loaded["size"] > state["size"]:
                state.update(loaded)
                state.update({"usable": True, "giveup": False,
                              "giveup_key": None, "stamp": stamp,
                              "wrote_size": loaded["size"],
                              "wrote_at": time.time()})
                return
    state["stamp"] = stamp
    if not state["usable"]:
        _recent_index_restart(
            state, _recent_index_realm_map_json(_account_realm_map()))

def _recent_index_maybe_write(state):
    """按节流阈值把索引落盘（默认 256KB / 900 s）；第一次建起来立即写。

    记「尝试」而非成功（只读文件系统上失败会一直成立，不能每次刷新都试写）；
    归属复现不了的索引不写，写下去别的进程也不敢用。
    """
    now = time.time()
    size = state["size"]
    if size <= state["wrote_size"]:
        return
    if (size - state["wrote_size"] < _recent_index_min_bytes()
            and (now - state["wrote_at"]) < _recent_index_min_seconds()):
        return
    if not _recent_index_realm_ok(state):
        return
    state["wrote_at"] = now
    state["wrote_size"] = size
    try:
        _recent_index_write(state)
    except Exception as exc:
        _gw().log("recent index write failed: %s" % exc)
        return
    state["stamp"] = _recent_index_stamp()

def _recent_index_snapshot(state):
    """给调用方的只读拷贝（锁外读页时状态可能被别的线程推进）。"""
    return {
        "size": state["size"],
        "realms": list(state["realms"]),
        "counts": dict(state["counts"]),
        "row_counts": dict(state["row_counts"]),
        "lines": state["lines"],
        "rows": state["rows"],
        "segs": [list(seg) for seg in state["segs"]],
    }

def _recent_index_resume_ok(state, log_key):
    """位置校验：索引覆盖的字节还是这份日志的前缀（_log_offset_holds + 尾部签名）。"""
    return (_log_offset_holds(state["key"], log_key, state["size"])
            and _log_tail_signature(state["size"]) == state["tail"])

def _recent_index_ready(realm_filtered=False):
    """扩到文件尾的索引快照；None 表示这次查询走扫描路径（count + 反向读）。

    顺序固定：先看文件戳（外部改动）→ 位置校验（截断/替换/同尺寸重写）→
    归属口径（realm_map 变了整份重折）→ 续读到尾 → realm 查询再验一次归属。
    """
    if not _recent_index_enabled():
        return None
    with _recent_index_lock:
        state = _recent_index_state
        stamp = _recent_index_stamp()
        if state["stamp"] != stamp:
            _recent_index_adopt(state, stamp)
        log_key = _usage_log_key()
        if state["giveup"]:
            if state["giveup_key"] == log_key:
                return None
            state["giveup"] = False             # 日志被换掉了：再给一次机会
        if state["size"] > 0:
            if not _recent_index_resume_ok(state, log_key):
                _recent_index_restart(
                    state, _recent_index_realm_map_json(_account_realm_map()))
            elif state["legacy"]:
                realm_map = _recent_index_realm_map_json(_account_realm_map())
                if state["realm_map"] != realm_map:
                    # 归属口径变了（uid→realm 改过）：整份重折，与聚合折叠同款。
                    _recent_index_restart(state, realm_map)
        if not state["usable"] and not state["segs"] and not state["lines"]:
            # 从零开始：把这次的归属口径记下来（后面续读都按它校验）
            state["realm_map"] = _recent_index_realm_map_json(
                _account_realm_map())
        # 每次都续读到文件尾：日志只会追加，续读的代价就是新字节。
        if not _recent_index_extend(state):
            return None
        if realm_filtered and not _recent_index_realm_ok(state):
            return None
        _recent_index_maybe_write(state)
        return _recent_index_snapshot(state)

def _recent_index_total(snap, realm):
    """页数标签：口径与 count_usage_rows 完全一致（总数含坏 JSON 的非空行）。"""
    if not realm:
        return snap["lines"]
    return snap["counts"].get(realm, 0)

def _recent_index_page(snap, realm, page, limit):
    """第 page 页的记录（新的在前）；None 表示索引答不了，调用方走扫描路径。

    页 = 从尾部往回数第 (page-1)*limit+1 .. page*limit 条记录，切片夹在「真的有
    这么多条」上（页数标签含坏行、记录数不含，最后几页可能短或空）。换算成从头
    数的序号后，页的字节区间是 [第 K_lo 条起点, 第 K_hi 条终点]。
    """
    if realm:
        if realm not in snap["realms"]:
            return []                        # 一条也不匹配：扫描路径也是空页
        col = 2 + snap["realms"].index(realm)
        total_rows = snap["row_counts"].get(realm, 0)
    else:
        col = 1
        total_rows = snap["rows"]
    target = page * limit
    k_hi = total_rows - (page - 1) * limit
    if k_hi < 1:
        return []                            # 页落在记录总数之外：扫描路径是空切片
    k_lo = max(1, total_rows - target + 1)
    segs = snap["segs"]
    # 二分：第一个「到该段起点为止的记录数 >= K_lo」的段；它前一段就是起点
    lo, hi = 0, len(segs)
    while lo < hi:
        mid = (lo + hi) // 2
        if segs[mid][col] < k_lo:
            lo = mid + 1
        else:
            hi = mid
    start = segs[lo - 1][0] if lo > 0 else 0
    have = segs[lo - 1][col] if lo > 0 else 0
    # 段里第一条是第 have+1 条，跳过 k_lo-have-1 条正好到第 k_lo 条。
    rows = _recent_index_read(start, realm, k_lo - have - 1, k_hi - k_lo + 1)
    if rows is None:
        return None
    rows.reverse()
    return rows

def _recent_index_read(start, realm, skip, take):
    """从 start 顺序读到第 take 条匹配记录；对不上返回 None（走扫描路径）。

    读取量 = 一段（64KB）+ 页本身的字节，与页深无关。读到的行与扫描路径逐字
    一致：同一套 strip/解码/json.loads，realm 过滤走同一个 row_matches_realm。
    """
    rows = []
    seen = 0
    try:
        with open(_gw().USAGE_LOG, "rb") as fh:
            fh.seek(start)
            buf = b""
            while len(rows) < take:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                buf += chunk
                parts = buf.split(b"\n")
                buf = parts.pop()
                for raw in parts:
                    st = raw.strip()
                    if not st:
                        continue
                    try:
                        item = json.loads(st.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    if not isinstance(item, dict):
                        return None     # 索引覆盖的字节里不该有这种行
                    if realm and not row_matches_realm(item, realm):
                        continue
                    if seen < skip:
                        seen += 1
                        continue
                    rows.append(item)
                    if len(rows) >= take:
                        break
    except Exception as exc:
        _gw().log("recent index read failed: %s" % exc)
        return None
    if len(rows) < take:
        return None
    return rows

def _recent_backward_page(realm, page, limit):
    """扫描路径：从尾部按 256KB 块往回读，凑够 page*limit 条再切片。

    页深越大读得越多，所以索引可用时不走这里；索引答不了时它是唯一正确答案。
    """
    target_count = page * limit
    matching = []
    chunk = 256 * 1024
    try:
        with open(_gw().USAGE_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(matching) < target_count:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    st = raw.strip()
                    if not st:
                        continue
                    try:
                        item = json.loads(st.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    if realm and not row_matches_realm(item, realm):
                        continue
                    matching.append(item)
                    if len(matching) >= target_count:
                        break
            if len(matching) < target_count and buf.strip():
                try:
                    item = json.loads(buf.strip().decode("utf-8", "replace"))
                    if not realm or row_matches_realm(item, realm):
                        matching.append(item)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception as exc:
        _gw().log("recent_usage read failed: %s" % exc)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    return matching[start_idx:end_idx]

def _recent_row_failed(row):
    if row.get("error"):
        return True
    status = row.get("status")
    if isinstance(status, int) and status >= 400:
        return True
    return row.get("outcome") in ("failed", "upstream_aborted")

def _recent_row_matches(row, filters):
    key = filters.get("key")
    if key and _key_usage_row_id(row) != key:
        return False
    status = filters.get("status")
    if status == "ok" and _recent_row_failed(row):
        return False
    if status == "fail" and not _recent_row_failed(row):
        return False
    model = filters.get("model")
    if model and model.lower() not in str(row.get("model") or "").lower():
        return False
    account = filters.get("account")
    if account and str(row.get("account") or "") != account:
        return False
    ip = filters.get("ip")
    if ip and ip not in str(row.get("ip") or ""):
        return False
    at = row.get("at") or 0
    if filters.get("since") is not None and at < filters["since"]:
        return False
    if filters.get("until") is not None and at > filters["until"]:
        return False
    return True

def _recent_filtered_rows(realm, filters):
    """所有匹配行，最新在前；设置列筛选时走这条路径。"""
    effective_realm = realm or (filters or {}).get("realm")
    rows = []
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
                if not isinstance(row, dict):
                    continue
                if effective_realm and not row_matches_realm(row, effective_realm):
                    continue
                if filters and not _recent_row_matches(row, filters):
                    continue
                rows.append(row)
    except FileNotFoundError:
        return []
    except Exception as exc:
        _gw().log("recent_usage filtered read failed: %s" % exc)
        return []
    rows.reverse()
    return rows

def recent_usage(limit=100, realm=None, page=1, filters=None):
    """日志尾部的分页行，第 1 页最新；索引可用时从索引直接定位。

    索引答不了就整段退回 _recent_backward_page()，两条路的输出逐字相同；
    取页开销在毫秒量级。
    """
    try:
        limit = max(1, int(limit))
    except Exception:
        limit = 100
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    realm = realm_scope(realm)
    if filters:
        rows = _recent_filtered_rows(realm, filters)
        total = len(rows)
        total_pages = max(1, (total + limit - 1) // limit) if total > 0 else 1
        page = min(page, total_pages)
        start = (page - 1) * limit
        page_rows = rows[start:start + limit]
        index = None
    else:
        index = _recent_index_ready(realm_filtered=bool(realm))
        page_rows = None
        if index is not None:
            total = _recent_index_total(index, realm)
            total_pages = max(1, (total + limit - 1) // limit) if total > 0 else 1
            page = min(page, total_pages)
            page_rows = _recent_index_page(index, realm, page, limit)
            if page_rows is None:
                index = None
    if not filters and index is None:
        total = count_usage_rows(realm)
        total_pages = max(1, (total + limit - 1) // limit) if total > 0 else 1
        page = min(page, total_pages)
        page_rows = _recent_backward_page(realm, page, limit)
    # 每行按 OpenRouter 牌价折算等效 token 费用，计价按行发生时生效的价表版本。
    # 无法定价的模型 cost_cny / cost_band 保持 None；cost_rates / cost_unit /
    # cost_currency / cost_usd_cny / cost_or_id / cost_via / cost_inherited_from /
    # cost_override_from / cost_band_note / cost_via_derived 把同一数字拆开，行未
    # 定价时全部是 None（面板必须如实显示，不能写成 0）。价目表整页取一次共用。
    pricing_doc = wb_pricing.load_pricing()
    for r in page_rows:
        # usage.jsonl 里存的是 key 的 id，key_name 补一个可读显示名（_gw().key_label）。
        r["key_name"] = _gw().key_label(_key_usage_row_id(r))
        cost = wb_pricing.cost_for_row(r, pricing_doc=pricing_doc)
        r["cost_cny"] = round(cost["cny"], 6) if cost["known"] else None
        r["cost_band"] = cost["band"] if cost["known"] else None
        # cost_policy 保留行记录的策略 id；cost_source 是实际解析到的来源（同 id 或 builtin）。
        r["cost_source"] = cost["source"] if cost["known"] else None
        r["cost_source_at"] = cost["source_at"] if cost["known"] else None
        r["cost_backfilled"] = bool(cost["backfilled"]) if cost["known"] else False
        r["cost_rates"] = cost["rates"] if cost["known"] else None
        r["cost_unit"] = cost["unit"] if cost["known"] else None
        r["cost_currency"] = cost["currency"] if cost["known"] else None
        r["cost_usd_cny"] = cost["usd_cny"] if cost["known"] else None
        r["cost_or_id"] = cost["or_id"] if cost["known"] else None
        r["cost_via"] = cost["via"] if cost["known"] else None
        r["cost_inherited_from"] = (cost["inherited_from"] if cost["known"]
                                    else None)
        r["cost_override_from"] = (cost["override_from"] if cost["known"]
                                   else None)
        r["cost_band_note"] = cost["band_note"] if cost["known"] else None
        r["cost_via_derived"] = (bool(cost["via_derived"]) if cost["known"]
                                 else None)
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "pages": total_pages,
        "usd_cny": wb_pricing.usd_cny(),
        "rows": page_rows
    }
