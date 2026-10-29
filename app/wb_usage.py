"""用量记账与统计：日志折叠与窗口聚合。

每次请求写一行 JSONL；统计视图（快照、按账号、时间序列、分析、性能、最近、
趋势）都从这份日志折叠而来，增量状态落在数据目录。剩余用量估算在
wb_usage_remaining，聚合状态落盘在 wb_usage_cache。网关运行态（账号池、
目录、日志）通过 `_gw()` 读活值，测试替换网关属性后立即生效。
"""

import bisect
import hashlib
import heapq
import json
import os
import threading
import time

import wb_accounts
import wb_activity
import wb_catalog
import wb_cost
import wb_pricing
import wb_settings
from wb_models import read_cached_remote_catalog
from wb_usage_remaining import (
    LIMIT_WINDOW_SECONDS,
    _REMAINING_KEEP_SECONDS,
    _REMAINING_MAX_EVENTS,
    _estimate_daily_cache,
    _estimate_daily_sync,
    _fold_remaining,
    _pair_append,
    _pair_repair,
    _pair_trim,
    _range_sum,
    _remaining_cache,
    _remaining_cache_lock,
    _remaining_payload,
    _remaining_schedule_weight,
    _remaining_state,
    _remaining_state_lock,
    _save_account_window,
    note_limit_event,
    remaining_schedule_weights,
    remaining_usage,
    remaining_usage_etag,
)
from wb_usage_cache import (
    USAGE_FIELDS,
    _SERIES_MINUTE_FIELDS,
    _USAGE_CACHE_BUCKET_NUMBERS,
    _USAGE_CACHE_KINDS,
    _USAGE_CACHE_SCHEMA,
    _object_digest,
    _realm_inputs_key,
    _usage_cache_adopt_analytics,
    _usage_cache_adopt_by_account,
    _usage_cache_adopt_remaining,
    _usage_cache_adopt_series,
    _usage_cache_adopt_snapshot,
    _usage_cache_checkpointed,
    _usage_cache_counts_ok,
    _usage_cache_day_key_ok,
    _usage_cache_digest_memo,
    _usage_cache_forget,
    _usage_cache_hits_ok,
    _usage_cache_int,
    _usage_cache_kind_path,
    _usage_cache_last_attempt,
    _usage_cache_lock,
    _usage_cache_maybe_save,
    _usage_cache_number,
    _usage_cache_path,
    _usage_cache_progress,
    _usage_cache_read_kind,
    _usage_cache_read_legacy_file,
    _usage_cache_remaining_ok,
    _usage_cache_take,
    _usage_cache_write_kind,
)

_GATEWAY = None


def _gw():
    """网关模块对象；缓存模块本身，属性每次现读。"""
    global _GATEWAY
    if _GATEWAY is None:
        import wb_host
        _GATEWAY = wb_host.loaded_gateway("log", "POOL", "USAGE_DIR")
        if _GATEWAY is None:
            import wb_proxy as _GATEWAY
    return _GATEWAY


def _empty_stats():
    return {"requests": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
            "credit": 0.0, "cost_cny": 0.0, "cost_missing": {},
            "started": time.time(), "by_model": {},
            # 按 (model, realm) 再聚合一份，指标表可以按出口分行。
            "by_model_realm": {},
            # 再按 (model, realm, account) 聚合，同一出口上的多个账号可以分开显示。
            "by_model_acct": {},
            # 延迟累加器，用于平均值；分位仍从 JSONL 原始样本计算。
            "ttft_ms_sum": 0, "ttft_samples": 0,
            "gen_ms_sum": 0, "gen_samples": 0,
            "wall_ms_sum": 0, "wall_samples": 0}
# 进程启动时刻。快照的 "started" 与 "since" 从这里取，_usage_snapshot_uncached
# 是唯一读者；requests、ttft、by_model 等累计字段没有读者，请求路径不维护。
_usage = {"started": time.time()}
def normalize_usage_cache_aliases(usage):
    """把最可信的缓存命中值写入所有别名。

    有些响应在 prompt_tokens_details.cached_tokens 里给出真实命中，同时输出
    值为 0 的兼容别名 cache_read_input_tokens 与 cached_tokens；严格的下游
    解析器可能优先取 0 别名而丢掉命中。函数原地修改并返回 usage。
    """
    if not isinstance(usage, dict):
        return usage
    best = _best_cached_tokens(usage)
    if best <= 0:
        return usage
    usage["cache_read_input_tokens"] = best
    usage["cached_tokens"] = best
    usage["prompt_cache_hit_tokens"] = best
    prompt_details = dict(usage.get("prompt_tokens_details") or {})
    prompt_details["cached_tokens"] = best
    usage["prompt_tokens_details"] = prompt_details
    if isinstance(usage.get("input_tokens_details"), dict):
        input_details = dict(usage["input_tokens_details"])
        input_details["cached_tokens"] = best
        usage["input_tokens_details"] = input_details
    return usage

def _extract_usage(usage):
    """把上游 usage 块归一为网关跟踪的字段。"""
    if not usage:
        return {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "reasoning_tokens": details.get("reasoning_tokens") or 0,
        "cached_tokens": _best_cached_tokens(usage),
        "total_tokens": usage.get("total_tokens") or 0,
        "credit": usage.get("credit") or 0,
    }
def row_realm(row):
    """日志行所属的 realm。

    带 realm 字段的行直接返回该字段。老行先按账号归属，再按模型所属区域归属，
    顺序与 row_matches_realm 一致，过滤与按区域汇总不会对同一行给出不同答案。
    """
    r = row.get("realm")
    if r:
        return r
    acct_uid = row.get("account")
    if acct_uid and _gw().POOL:
        acc = _gw().POOL.get(acct_uid)
        if acc:
            return acc.realm
    model = row.get("model")
    if model:
        return _gw().detect_model_realm(model)
    return "intl"

def row_matches_realm(row, realm):
    # None 表示全部 realm。"all" 也按全部处理，调用方直接传字面量时不会匹配空。
    if not realm or realm == "all": return True
    return row_realm(row) == realm
def realm_scope(realm, fallback=None):
    """把调用方给的 realm 映射为日志过滤条件。

    "all" 表示全部 realm，映射为 None 并关闭过滤；直接传字面量会让
    row_matches_realm 把每行与 "all" 比较，结果什么都不匹配。空值或缺省值
    回退到第二个参数：全局开关类接口用 _gw().CURRENT_REALM，分析载荷用 None，
    后者始终统计两个区域。
    """
    if realm == "all":
        return None
    return realm or fallback

def _local_midnight(ts=None, days_back=0):
    """ts 之前 days_back 天的本地午夜，ts 缺省为当前时刻。

    mktime 会规范化越界的日号，回退跨月时仍落在真实存在的本地午夜，不会抛错。
    """
    lt = time.localtime(ts if ts is not None else time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - days_back, 0, 0, 0, 0, 0, -1))

def _local_hour_start(ts):
    """ts 所属本地整点的 epoch，分与秒归零。

    与 _local_midnight 同一套构造：mktime 把本地墙上时间换算回 epoch。
    夏令时切换小时里的时间会被规整到相邻时刻，所以调用方需要比对返回值是否
    等于原值，只有原样还原的 ts 才真正落在整点上，见 _hour_window_key。
    """
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, 0, 0, 0, 0, -1))

def _epoch_or_none(value):
    """面板传入的 epoch 秒；无法信任时返回 None。

    负数或无法解析的边界直接丢弃，不做截断。面板发送前会拒绝这些值，
    出现在这里说明是手写 URL；该侧不设边界比按 1970 年切日志更符合预期。
    """
    if value in (None, "", False):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None

def range_window(value, since=None, until=None):
    """把看板的 range 选择器解析为 since 与 until 两个边界。

    两个边界都是行 at 的截断点；None 表示该侧不设边界，两个都是 None 表示
    关闭过滤。today、week、month 是以本地午夜为锚的日历窗口，与看板 Today
    数字的口径一致；一周从周一开始。不提供 7d、30d 这类滚动别名，它们表示
    最近七天，与本周不是同一个窗口，按钮标签会在多数日期上出错。
    custom 使用面板送来的两个 epoch，任一侧可以缺省；两个边界顺序颠倒时交换，
    不拒绝。空结束时间是正常情况。
    """
    v = str(value or "").strip().lower()
    if v in ("today", "day", "1d"):
        return _local_midnight(), None
    if v in ("week", "w"):
        return _local_midnight(days_back=time.localtime().tm_wday), None
    if v in ("month", "m"):
        lt = time.localtime()
        return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)), None
    if v == "custom":
        lo, hi = _epoch_or_none(since), _epoch_or_none(until)
        if lo is not None and hi is not None and hi < lo:
            lo, hi = hi, lo
        return lo, hi
    return None, None

def range_query(query):
    """从解析后的查询串取出三个 range 参数。

    parse_qs 把每个键返回为列表，面板可能一个参数都不发，所以每次取值都回退
    到 None，range_window 将其视为该侧不设边界。
    """
    def first(name):
        values = query.get(name) or [None]
        return values[0] if values else None
    return first("range"), first("since"), first("until")
def row_outcome(row):
    """请求行的终态。

    没有 outcome 字段的老行只有 error 与 status，错误行按失败处理，其余按完成
    处理。所有读者共用这一个函数，答案不会分叉。
    """
    o = row.get("outcome")
    if o:
        return o
    return "failed" if row.get("error") else "completed"

def audit_columns(audit):
    """用量行携带的逐请求审计字段。

    请求到达时由 Handler._begin_audit 捕获一次：流式尾部或网页工具后续轮次
    可能在请求行解析很久之后才结束，那时连接可能已在处理下一个请求。
    """
    audit = audit or {}
    return {
        "key_id": audit.get("key_id") or "",
        "ip": audit.get("ip") or "",
        "api": audit.get("api") or "",
    }

_REQUEST_AUDIT = threading.local()

def bind_audit(audit):
    """把 audit 设为当前线程的请求上下文。"""
    _REQUEST_AUDIT.value = audit
    return audit

def current_audit():
    """当前线程正在处理的请求的审计上下文，没有时返回 None。"""
    return getattr(_REQUEST_AUDIT, "value", None)

CHAT_API_NAMES = {
    "/v1/chat/completions": "chat",
    "/chat/completions": "chat",
    "/v1/completions": "completions",
    "/completions": "completions",
    "/v1/responses": "responses",
    "/responses": "responses",
    "/v1/messages": "messages",
    "/messages": "messages",
}

def chat_api_name(path):
    return CHAT_API_NAMES.get(str(path or ""), "chat")
def _best_cached_tokens(usage):
    """从上游输出的所有别名里取最可信的缓存命中值。

    顺序与面板一致：prompt_tokens_details.cached_tokens、prompt_cache_hit_tokens、
    cache_read_input_tokens、cached_tokens、input_tokens_details.cached_tokens、
    completion_tokens_details.cached_tokens。第一个正值胜出，0 别名不会遮住
    真实命中。
    """
    if not isinstance(usage, dict):
        return 0
    prompt_details = usage.get("prompt_tokens_details") or {}
    input_details = usage.get("input_tokens_details") or {}
    details = usage.get("completion_tokens_details") or {}
    for value in (prompt_details.get("cached_tokens"),
                  usage.get("prompt_cache_hit_tokens"),
                  usage.get("cache_read_input_tokens"),
                  usage.get("cached_tokens"),
                  input_details.get("cached_tokens"),
                  details.get("cached_tokens")):
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            continue
        if number > 0:
            # Token 数必须保持整数：Codex 客户端严格解析 response.completed，
            # 123.0（invalid number）会被拒绝。
            return int(number)
    return 0

def record_usage(model, usage, stream=None, elapsed_ms=None, ttft_ms=None, gen_ms=None, fp=None,
                account=None, outcome="completed", key=None, effort=None,
                requested_model=None, audit=None):
    """把一次结束的请求写成一行 JSONL。

    outcome 是终态：completed、client_aborted、upstream_aborted、rate_limited、
    failed。rate_limited 表示上游限流，响应里可能带恢复时刻。
    没有 usage 块的请求也写一行并标 usage_missing，它仍计入请求数、成功率与
    延迟样本，只缺 token 数。

    key 是扣费用的客户端 API Key 的设置 id，不是密钥本身；由调用方显式传入，
    因为一个 keep-alive 线程服务多个请求，隐式通道会把花费记到错误的 Key。
    effort 只在模型有推理档位时写入。
    """
    fields = _extract_usage(usage) or {}
    usage_missing = not fields
    row = {
        "at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "stream": bool(stream),
        "outcome": outcome,
        "elapsed_ms": elapsed_ms,
        "ttft_ms": ttft_ms,
        "gen_ms": gen_ms,
    }
    if requested_model and requested_model != model:
        row["requested_model"] = requested_model
    if usage_missing:
        row["usage_missing"] = True
    row.update(fields)
    if fp:
        row.update(fp)
    if account:
        row["account"] = account
    acc = _gw().POOL.get(account) if (account and _gw().POOL) else None
    row["realm"] = acc.realm if acc else _gw().CURRENT_REALM
    # 没有提供 key 时也写入空值：空值与老行的缺失字段区分了未带 key 和
    # 字段出现之前写入，看板把它们统计为两个不同的桶。
    row["key"] = key or ""
    if audit is None:
        audit = current_audit()
    row.update(audit_columns(audit))
    if not row["key_id"]:
        row["key_id"] = row["key"]
    row["status"] = int((audit or {}).get("status") or 200)
    # 本次请求使用的价格策略，以引用形式保存，便于表去重与清扫。
    # 没有引用的行回退到时间线计价。
    row["cost_policy"] = wb_pricing.current_policy_id(model)
    # 只在请求带了推理档位时写入：面板只为带该字段的行显示标记，
    # 没有推理档位的模型没有可报的内容。
    if effort:
        row["reasoning_effort"] = effort
    if gen_ms and gen_ms > 0:
        row["tokens_per_sec"] = round(fields.get("completion_tokens", 0) / (gen_ms / 1000.0), 2)
    # 与聚合视图（compute_usage_analytics）共用分母，否则单请求行与汇总
    # 对同一份数据会算出不一致的比率。
    if fields.get("prompt_tokens", 0) > 0:
        row["cache_hit_pct"] = round(fields.get("cached_tokens", 0) * 100.0
                                     / fields["prompt_tokens"], 1)
    _persist_usage(row, "usage persist failed")
    try:
        t_tokens = fields.get("total_tokens", 0)
        dur = f" {elapsed_ms:.0f}ms" if elapsed_ms is not None else ""
        acc_tag = f" acct={account[:8]}" if account else ""
        key_tag = f" key={_gw().key_label(key)}" if key else ""
        speed_tag = f" {row.get('tokens_per_sec', 0)}t/s" if row.get("tokens_per_sec") else ""
        miss_tag = " usage=missing" if usage_missing else ""
        _gw().log(f"chat done: model={model}{acc_tag}{key_tag}{dur} tokens={t_tokens} (in={fields.get('prompt_tokens',0)} out={fields.get('completion_tokens',0)}){speed_tag}{miss_tag}", tag="chat")
    except Exception:
        pass
    return row

def record_upstream_usage(upstream, requested_model, usage, **kwargs):
    """记下上游实际计费的模型，保留客户端别名。"""
    billed_model = getattr(upstream, "_wb_billed_model", requested_model)
    record_usage(billed_model, usage, requested_model=requested_model, **kwargs)

_ENSURED_DIRS = set()


def _ensure_dir(path):
    """建目录只做一次：os.makedirs 每次都会 stat 整条路径，慢盘上代价明显。"""
    if not path or path in _ENSURED_DIRS:
        return
    os.makedirs(path, exist_ok=True)
    _ENSURED_DIRS.add(path)


def _ensure_dir_retry(directory):
    """目录被外部删掉时重建。"""
    _ENSURED_DIRS.discard(directory)
    _ensure_dir(directory)


def _open_write(path, mode="w", newline=None):
    """以写方式打开；目录被外部删掉时重建一次。"""
    try:
        return open(path, mode, encoding="utf-8", newline=newline)
    except FileNotFoundError:
        _ensure_dir_retry(os.path.dirname(path))
        return open(path, mode, encoding="utf-8", newline=newline)


def _open_append(path):
    """追加打开；目录被外部删掉时重建一次。"""
    try:
        return open(path, "a", encoding="utf-8")
    except FileNotFoundError:
        directory = os.path.dirname(path)
        _ENSURED_DIRS.discard(directory)
        _ensure_dir(directory)
        return open(path, "a", encoding="utf-8")


def _persist_usage(row, fail_label):
    """向 JSONL 追加一行用量。每个请求只追加，聚合视图各自重读日志。"""
    try:
        _ensure_dir(_gw().USAGE_DIR)
        with _open_append(_gw().USAGE_LOG) as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        _gw().log("%s: %s" % (fail_label, exc))

def _center_fold_rows(offset, fold, needle=None):
    """把 offset 之后完整的日志行交给 fold(row)。

    行是整行追加的，末尾没有换行说明读到了正在写入的一半，留给下一轮。
    返回 (new_offset, reset)；reset 为真表示日志短于 offset，调用方必须丢弃
    已累计的状态并从零折叠。
    """
    try:
        size = os.path.getsize(_gw().USAGE_LOG)
    except OSError:
        size = 0
    if offset > size:
        return 0, True
    with open(_gw().USAGE_LOG, "rb") as fh:
        fh.seek(offset)
        while True:
            pos = fh.tell()
            raw = fh.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                return pos, False
            offset = fh.tell()
            raw = raw.strip()
            if not raw:
                continue
            line = raw.decode("utf-8", "replace")
            if needle and not any(text in line for text in needle):
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                fold(row)
    return offset, False

_key_usage_cache = {}
_key_usage_lock = threading.Lock()

def _new_key_usage_stat():
    return {"requests": 0, "total_tokens": 0, "credit": 0.0, "last_used_at": 0}

def _feed_key_usage_stat(stat, row):
    stat["requests"] += 1
    tokens = row.get("total_tokens")
    if isinstance(tokens, (int, float)):
        stat["total_tokens"] += tokens
    credit = row.get("credit")
    if isinstance(credit, (int, float)):
        stat["credit"] += credit
    at = row.get("at")
    if isinstance(at, (int, float)) and at > stat["last_used_at"]:
        stat["last_used_at"] = at

def _key_usage_reset_map():
    reset_map = {}
    for entry in _gw().configured_keys():
        key_id = entry.get("id") or ""
        if key_id:
            reset_map[key_id] = float(entry.get("usage_reset_at") or 0)
    return reset_map

def _key_usage_row_id(row):
    return row.get("key_id") or row.get("key") or ""

def key_usage_snapshot():
    """每个已配置 Key 的用量，由一次共享扫描折出。

    用该 key_id 写下的行都算，失败请求也算；没有 key_id 的旧行回退到 key 字段。
    """
    reset_map = _key_usage_reset_map()
    cache_key = json.dumps(reset_map, sort_keys=True)
    now = time.time()
    with _key_usage_lock:
        entry = _key_usage_cache.get(cache_key)
        state = entry["state"] if entry is not None else {}
        state, offset = _scan_key_usage(reset_map, entry["offset"] if entry else 0, state)
        data = {key_id: dict(stat) for key_id, stat in state.items()}
        for old in [k for k, v in _key_usage_cache.items()
                    if now - v["at"] > 3600]:
            del _key_usage_cache[old]
        _key_usage_cache[cache_key] = {"at": now, "state": state, "offset": offset}
    for key_id in reset_map:
        data.setdefault(key_id, _new_key_usage_stat())
    return data

def _scan_key_usage(reset_map, offset, state):
    """把 offset 之后的日志行折进按 Key 的累加器。

    在副本上折叠：扫描中途失败时丢弃半成品，调用方保存的 state 与 offset
    仍是上一轮一致的快照，同一批行不会被重复计入。
    """
    work = {key_id: dict(stat) for key_id, stat in state.items()}

    def fold(row):
        key_id = _key_usage_row_id(row)
        if key_id not in reset_map:
            return
        at = row.get("at")
        if not isinstance(at, (int, float)) or at < reset_map[key_id]:
            return
        _feed_key_usage_stat(work.setdefault(key_id, _new_key_usage_stat()), row)

    try:
        new_offset, reset = _center_fold_rows(offset, fold)
        if reset:
            work = {}
            new_offset, _ = _center_fold_rows(0, fold)
    except FileNotFoundError:
        return {}, 0
    except Exception as exc:
        _gw().log("key usage fold failed: %s" % exc)
        return state, offset
    return work, new_offset

def key_usage(key_id, reset_at=0):
    """一个 API Key 自 reset_at 起的用量，0 表示全部历史。"""
    key_id = str(key_id or "")
    if not key_id:
        return _new_key_usage_stat()
    reset_at = float(reset_at or 0)
    for entry in _gw().configured_keys():
        if entry.get("id") == key_id:
            if float(entry.get("usage_reset_at") or 0) == reset_at:
                return dict(key_usage_snapshot().get(key_id) or _new_key_usage_stat())
            break
    return _key_usage_single(key_id, reset_at)

def _key_usage_single(key_id, reset_at):
    """面板已删除的 Key：扫全量日志算它的用量。"""
    stat = _new_key_usage_stat()

    def fold(row):
        if _key_usage_row_id(row) != key_id:
            return
        if (row.get("at") or 0) < reset_at:
            return
        _feed_key_usage_stat(stat, row)

    try:
        _center_fold_rows(0, fold, needle=_key_usage_needle(key_id))
    except FileNotFoundError:
        return _new_key_usage_stat()
    except Exception as exc:
        _gw().log("key usage scan failed: %s" % exc)
    return stat

def _key_usage_needle(key_id):
    """JSONL 行里可能出现的该 key id 写法。"""
    encoded = json.dumps(str(key_id), ensure_ascii=False)
    return ('"key_id": %s' % encoded, '"key_id":%s' % encoded,
            '"key": %s' % encoded, '"key":%s' % encoded)

def record_error(model, status, message, elapsed_ms=None, account=None,
                 usage=None, stream=None, ttft_ms=None, gen_ms=None, fp=None,
                 outcome="failed", key=None, audit=None):
    """把一次失败的请求写成一行 JSONL。

    传入 account uid 才能按事实归属失败；不传就只能按模型名猜。
    usage 带上上游在流中断前已报告的用量：中断的流只写这一行错误，同一请求
    不会既算失败又算成功。
    status 仍是 HTTP 状态码，outcome 是终态，两者含义不重叠。
    key 与 record_usage 相同，是被拒绝的请求唯一能归属到 Key 的途径。
    """
    fields = _extract_usage(usage) or {}
    row = {
        "at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "error": True,
        "outcome": outcome,
        "status": status,
        "message": str(message)[:200],
        "elapsed_ms": elapsed_ms,
    }
    if stream is not None:
        row["stream"] = bool(stream)
    if ttft_ms is not None:
        row["ttft_ms"] = ttft_ms
    if gen_ms is not None:
        row["gen_ms"] = gen_ms
    row.update(fields)
    if fp:
        row.update(fp)
    if account:
        row["account"] = account
        acc = _gw().POOL.get(account) if _gw().POOL else None
        row["realm"] = acc.realm if acc else _gw().CURRENT_REALM
    row["key"] = key or ""
    if audit is None:
        audit = current_audit()
    row.update(audit_columns(audit))
    if not row["key_id"]:
        row["key_id"] = row["key"]
    _persist_usage(row, "error persist failed")
    dur = f" {elapsed_ms:.0f}ms" if elapsed_ms is not None else ""
    # 账号与终态也带上看板日志行：与 chat done 的 acct= 对称，出错时不必回翻
    # usage.jsonl。
    acc_tag = f" acct={account[:8]}" if account else ""
    # 限流是上游给的窗口，不是故障：日志按警告记，红色错误留给真正的失败。
    level = "WARN" if outcome == "rate_limited" else "ERROR"
    _gw().log(f"request error: model={model}{dur}{acc_tag} status={status} outcome={outcome} msg={str(message)[:180]}",
        level=level, tag="chat")
    return row

def _log_complete_frontier(path=None):
    """最后一个换行之后的字节偏移：整行折叠从这里开始。

    日志尾部可能是写了一半的一行，它算样本一行但不算已折的字节，offset 停在
    这里等它写完整。向前找换行只在冷路径上做。
    """
    path = path or _gw().USAGE_LOG
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    chunk = 256 * 1024
    pos = size
    try:
        with open(path, "rb") as fh:
            while pos > 0:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                idx = fh.read(step).rfind(b"\n")
                if idx >= 0:
                    return pos + idx + 1
    except OSError:
        return 0
    return 0




_snap_cache = {}
_snap_lock = threading.Lock()
# 面板每 5 秒轮询一次。TTL 短于轮询间隔会让一半的轮询都做全量扫描；15 秒
# 意味着三次轮询最多重建一次，数字最多滞后这么久。
_STATS_TTL = float(os.environ.get("WB_STATS_TTL", 15))

# ---------------------------------------------------------------------------
# 每日用量计数。
#
# 上游对免费窗口按固定 token 预算封顶（code 6004），等到 429 时窗口已经用满。
# 这里把 usage.jsonl 折成 uid -> 本地午夜以来的 token，以及同一批行的两个
# 派生视图：uid -> 今日积分消耗（每日积分守卫）与 uid -> {model: tokens}
# （按模型每日守卫）。AccountPool 的三个 apply_daily_* 把数字写到账号上，
# ready() 据此跳过该账号，下一个请求轮换到别的账号。折叠是增量的
# （字节偏移加每天小计），热路径只读上次扫描之后新增的行。
# ---------------------------------------------------------------------------
_daily_usage = {"day": "", "totals": None, "credits": None, "models": None,
                "offset": 0, "at": 0.0}
_daily_usage_lock = threading.Lock()

def _daily_state_copy(source):
    """把缓存的按账号计数拷进新的扫描状态。

    同一批行的三个视图：uid -> 总 tokens、uid -> 已花积分、
    uid -> {model: tokens}。拷贝防止后续扫描就地改动缓存字典，
    读者手里的引用不会被改写。
    """
    return {
        "tokens": dict(source.get("totals") or {}),
        "credits": dict(source.get("credits") or {}),
        "models": {k: dict(v) for k, v in (source.get("models") or {}).items()},
    }

def _scan_daily_usage(offset, state):
    """把今天本地午夜起的行折进 `state`。

    返回 (state, new_offset)。没有行尾换行的行留给下一轮：行是整行
    追加的，尾部半行只说明这次读与写入并发。
    """
    midnight = _local_midnight()
    with open(_gw().USAGE_LOG, "rb") as fh:
        fh.seek(offset)
        while True:
            pos = fh.tell()
            raw = fh.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                return state, pos
            offset = fh.tell()
            line = raw.strip().decode("utf-8", "replace")
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            at = row.get("at")
            if not isinstance(at, (int, float)) or at < midnight:
                continue
            # 与 analytics 扫描同一规则：客户端取消不算已消费的请求，
            # 它的 token 数不完整。
            if row_outcome(row) == "client_aborted":
                continue
            uid = row.get("account")
            if not uid:
                continue
            tokens = row.get("total_tokens")
            if not isinstance(tokens, (int, float)):
                tokens = 0
            state["tokens"][uid] = state["tokens"].get(uid, 0) + tokens
            credit = row.get("credit")
            if isinstance(credit, (int, float)) and credit:
                state["credits"][uid] = state["credits"].get(uid, 0.0) + credit
            mid = row.get("model")
            if isinstance(mid, str) and mid:
                per = state["models"].setdefault(uid, {})
                per[mid] = per.get(mid, 0) + tokens
    return state, offset

def daily_usage_stats(ttl=None):
    """从日志折出的今日按账号用量，缓存 `ttl` 秒。

    返回 {"tokens": uid -> tokens, "credits": uid -> 已花积分,
    "models": uid -> {model: tokens}}；日志完全读不出时返回 None，调用方
    区分 None 与 0，读失败绝不据此跳过账号。
    """
    ttl = _STATS_TTL if ttl is None else ttl
    day = time.strftime("%Y-%m-%d")
    now = time.time()
    with _daily_usage_lock:
        c = _daily_usage
        if c["day"] == day and c["totals"] is not None and (now - c["at"]) < ttl:
            return _daily_state_copy(c)
        # 换天保留字节偏移：偏移之后的行都是今天的，午夜过滤会丢掉
        # 还没读到的旧日行。
        if c["day"] == day and c["totals"] is not None:
            state = _daily_state_copy(c)
        else:
            state = {"tokens": {}, "credits": {}, "models": {}}
        offset = int(c["offset"] or 0)
        try:
            size = os.path.getsize(_gw().USAGE_LOG)
        except OSError:
            size = 0
        if offset > size:
            state = {"tokens": {}, "credits": {}, "models": {}}
            offset = 0
        try:
            state, offset = _scan_daily_usage(offset, state)
        except Exception as exc:
            _gw().log("daily token scan failed: %s" % exc)
            _daily_usage.update({"day": day, "totals": None, "offset": offset,
                                 "at": time.time()})
            return None
        _daily_usage.update({"day": day, "totals": state["tokens"],
                             "credits": state["credits"],
                             "models": state["models"], "offset": offset,
                             "at": time.time()})
        return _daily_state_copy(_daily_usage)

def daily_tokens_by_account(ttl=None):
    """uid -> 本地午夜以来的 tokens，缓存 `ttl` 秒。

    日志完全读不出时是 None；调用方区分 None 与 0，读失败绝不据此
    跳过账号。
    """
    stats = daily_usage_stats(ttl=ttl)
    if stats is None:
        return None
    return stats["tokens"]

def daily_usage_by_account(ttl=None):
    """（uid -> 今日 tokens, uid -> 今日积分）；日志读不出时是 (None, None)。

    与 daily_tokens_by_account 共用同一次折叠；成对返回供需要两个维度的路由
    路径使用。
    """
    stats = daily_usage_stats(ttl=ttl)
    if stats is None:
        return None, None
    return dict(stats["tokens"]), dict(stats["credits"])

def seconds_until_local_midnight():
    """距离本地换天的秒数（至少一分钟）。"""
    lt = time.localtime()
    nxt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return max(60, int(nxt - time.time()))

def apply_daily_token_limit(refresh=False):
    """把每日 token 限额设置与今日计数推进池子。

    设置按 realm 求值：两个出口可以限不同数字，没有覆盖值的 realm
    仍跟随全局默认。
    """
    if _gw().POOL is None:
        return 0
    limits = wb_settings.limit_values(_gw().ACCOUNTS_DIR, "daily_token_limit")
    usage = None
    if any(value > 0 for value in limits.values()):
        usage = daily_tokens_by_account(ttl=0 if refresh else None)
    return _gw().POOL.apply_daily_token_limit(limits, usage)

def apply_daily_credit_limit(refresh=False):
    """把每日积分限额设置、今日消耗与免费模型视图推进池子。"""
    if _gw().POOL is None:
        return 0
    limits = wb_settings.limit_values(_gw().ACCOUNTS_DIR, "daily_credit_limit")
    credits = None
    free_models = None
    if any(value > 0 for value in limits.values()):
        stats = daily_usage_stats(ttl=0 if refresh else None)
        credits = stats["credits"] if stats is not None else None
        free_models = free_models_by_realm()
    return _gw().POOL.apply_daily_credit_limit(limits, credits, free_models)

def apply_model_daily_token_limit(refresh=False):
    """把按模型每日 token 限额设置与今日计数推进池子。"""
    if _gw().POOL is None:
        return 0
    limits = wb_settings.limit_values(_gw().ACCOUNTS_DIR, "model_daily_token_limit")
    per_model = None
    if any(value > 0 for value in limits.values()):
        stats = daily_usage_stats(ttl=0 if refresh else None)
        per_model = stats["models"] if stats is not None else None
    return _gw().POOL.apply_model_daily_token_limit(limits, per_model)

def apply_daily_guards(refresh=False):
    """一次推进三条每日护栏。

    三条护栏共用输入（一个设置文件、同一批日计数），这里读一次设置、折一次
    日志、推三次，免去每条护栏各读各扫。各护栏自身语义不变：按 realm 的
    限额、积分护栏的免费模型豁免，以及「没配限额 -> 计数未知，绝不跳过
    账号」规则（日志读不出时三份计数都是 None）。

    `refresh` 强制计数折叠绕过 TTL（设置保存路径用它，限额改完立即可见）。
    三个单护栏入口保留给只想要一条护栏的调用方。
    """
    if _gw().POOL is None:
        return 0
    ttl = 0 if refresh else None
    guards = wb_settings.limit_values_many(
        _gw().ACCOUNTS_DIR, ("daily_token_limit", "daily_credit_limit",
                       "model_daily_token_limit"))
    token_limits = guards["daily_token_limit"]
    credit_limits = guards["daily_credit_limit"]
    model_limits = guards["model_daily_token_limit"]
    wants = (any(v > 0 for v in token_limits.values())
             or any(v > 0 for v in credit_limits.values())
             or any(v > 0 for v in model_limits.values()))
    stats = daily_usage_stats(ttl=ttl) if wants else None
    # 池上这三个 apply_* 的返回值是它们各自的 limits 字典（不是计数），调用方
    # 谁也没用过；这里数的是「推了几条护栏」，别拿返回值做算术。
    #
    # 限额只活在账号对象的内存里，清掉旧值只能靠再推一次。设置保存路径
    # （refresh=True）对三条护栏都推送，值为 0 也推；请求热路径保持
    # 「没配限额就完全不碰池子」，不为此多付开销。
    published = 0
    if refresh or any(v > 0 for v in token_limits.values()):
        _gw().POOL.apply_daily_token_limit(
            token_limits, stats["tokens"] if stats is not None else None)
        published += 1
    if refresh or any(v > 0 for v in credit_limits.values()):
        _gw().POOL.apply_daily_credit_limit(
            credit_limits, stats["credits"] if stats is not None else None,
            free_models_by_realm())
        published += 1
    if refresh or any(v > 0 for v in model_limits.values()):
        _gw().POOL.apply_model_daily_token_limit(
            model_limits, stats["models"] if stats is not None else None)
        published += 1
    return published

# ---------------------------------------------------------------------------
# 智能选号：把每个账号今天的积分消耗折算成消耗速度，交给选号器打分
#
# usage.jsonl 只追加，所以这里用字节偏移做增量折叠；缓存键带上「今天」的
# 本地日键，跨过午夜自然从零开始。只有开关打开时才读日志。
# ---------------------------------------------------------------------------
_route_credits = {"day": "", "credits": None, "offset": 0, "at": 0.0}
_route_credits_lock = threading.Lock()

def _scan_route_credits(offset, credits):
    midnight = _local_midnight()

    def fold(row):
        if (row.get("at") or 0) < midnight:
            return
        if row_outcome(row) == "client_aborted":
            return
        uid = row.get("account")
        if not uid:
            return
        spent = row.get("credit")
        if isinstance(spent, bool) or not isinstance(spent, (int, float)):
            return
        credits[uid] = credits.get(uid, 0.0) + float(spent)

    offset, reset = _center_fold_rows(offset, fold)
    if reset:
        credits.clear()
        offset, _ = _center_fold_rows(0, fold)
    return credits, offset

def daily_credits_by_account(ttl=None):
    """（uid -> 今日消耗积分, 可读）；日志完全读不出来时第二个值为 False。"""
    ttl = _STATS_TTL if ttl is None else ttl
    with _route_credits_lock:
        now = time.time()
        day = _local_day_key(now)
        entry = _route_credits
        if entry["day"] == day and entry["credits"] is not None \
                and (now - entry["at"]) < ttl:
            return dict(entry["credits"]), True
        credits = {} if entry["day"] != day else dict(entry["credits"] or {})
        offset = 0 if entry["day"] != day else entry["offset"]
        try:
            credits, offset = _scan_route_credits(offset, credits)
        except FileNotFoundError:
            pass
        except Exception as exc:
            _gw().log("daily credit fold failed: %s" % exc)
            if entry["day"] == day and entry["credits"] is not None:
                return dict(entry["credits"]), False
            return {}, False
        _route_credits.update({"day": day, "credits": credits,
                               "offset": offset, "at": now})
        return dict(credits), True

# 本月消耗：面板在今日积分卡片下面显示一行，只需要一个总数。与今日的按账号
# 折叠分开：那份每天清零，这份按本地 1 号清零，两者共用同一套增量扫描与
# 截断保护，但各自保留自己的 offset。
_month_credit = {"month": "", "key": None, "offset": 0, "tail": b"",
                 "totals": None, "at": 0.0}
_month_credit_lock = threading.Lock()

def _scan_month_credits(offset, totals, start):
    """从 offset 起把本月行的 credit 折进 realm -> 总额。"""
    def fold(row):
        if (row.get("at") or 0) < start:
            return
        if row_outcome(row) == "client_aborted":
            return
        spent = row.get("credit")
        if isinstance(spent, bool) or not isinstance(spent, (int, float)):
            return
        if not spent:
            return
        realm = row_realm(row)
        totals[realm] = totals.get(realm, 0.0) + float(spent)

    offset, reset = _center_fold_rows(offset, fold)
    if reset:
        totals.clear()
        offset, _ = _center_fold_rows(0, fold)
    return totals, offset

def month_credits(ttl=None):
    """（realm -> 本月消耗积分, 可读）；日志完全读不出来时第二个值为 False。"""
    ttl = _STATS_TTL if ttl is None else ttl
    start = range_window("month")[0]
    month = time.strftime("%Y-%m", time.localtime(start))
    now = time.time()
    with _month_credit_lock:
        entry = _month_credit
        key = _usage_log_key()
        fresh = (entry["month"] == month
                 and _log_offset_holds(entry["key"], key, entry["offset"])
                 and entry["totals"] is not None
                 and entry["tail"] == _log_tail_signature(entry["offset"]))
        if fresh and (now - entry["at"]) < ttl:
            return dict(entry["totals"]), True
        totals = dict(entry["totals"]) if fresh else {}
        offset = entry["offset"] if fresh else 0
        try:
            totals, offset = _scan_month_credits(offset, totals, start)
        except FileNotFoundError:
            pass
        except Exception as exc:
            _gw().log("month credit fold failed: %s" % exc)
            if fresh:
                return dict(entry["totals"]), False
            return {}, False
        _month_credit.update({"month": month, "key": _usage_log_key(),
                              "offset": offset,
                              "tail": _log_tail_signature(offset),
                              "totals": totals, "at": now})
        return dict(totals), True

def month_credit_view(realm=None):
    """本月消耗的积分；realm 为空或 all 时返回两个出口的合计。"""
    r = realm_scope(realm, _gw().CURRENT_REALM)
    totals, ok = month_credits()
    credit = totals.get(r, 0.0) if r else sum(totals.values())
    return {"realm": r or "all",
            "month": time.strftime("%Y-%m"),
            "credit": round(credit, 6),
            "ok": ok}

def month_credit_etag(realm=None):
    """本月积分视图的 ETag；折叠还没跑过时没有可用的校验符。"""
    r = realm_scope(realm, _gw().CURRENT_REALM) or "all"
    with _month_credit_lock:
        at = _month_credit["at"]
        month = _month_credit["month"]
    if not at:
        return None
    return _gw()._cache_etag("month-credit", "%s|%s" % (r, month), at)

def credit_rate_scale(lt=None):
    """把「今天已消耗的积分」折算成每天消耗速度的倍数。

    刚过午夜时把几分钟的量当成一天的速率会放大几十倍，那段时间返回 1，
    直接用今日累计值当速率。
    """
    lt = time.localtime() if lt is None else lt
    elapsed = lt.tm_hour + lt.tm_min / 60.0 + lt.tm_sec / 3600.0
    return (24.0 / elapsed) if elapsed >= 1.0 else 1.0

_ROUTING_PUSH_SECONDS = 2.0
_routing_push = {"at": 0.0, "credits": None}

def apply_routing_inputs(refresh=False):
    """把每个账号最近的积分消耗推给池子，供评分选路使用。

    热路径上每个请求都会走到这里；日折叠本身有 TTL，但把结果逐个写回账号要拿
    账号池锁。两秒的推送间隔足够跟上按天计的消耗速度，refresh=True 仍然立即
    推送（启动与设置变更）。
    """
    if _gw().POOL is None or not getattr(_gw().POOL, "smart_routing", False):
        return None
    now = time.time()
    if not refresh and (now - _routing_push["at"]) < _ROUTING_PUSH_SECONDS:
        return _routing_push["credits"]
    credits, ok = daily_credits_by_account(ttl=0 if refresh else None)
    if not ok:
        return None
    scale = credit_rate_scale()
    _gw().POOL.apply_credit_rates({uid: value * scale for uid, value in credits.items()})
    _routing_push.update({"at": now, "credits": credits})
    return credits

# ---------------------------------------------------------------------------
# 积分流水：wb_accounts 在余额上升时写入，看板只读最近 30 天
# ---------------------------------------------------------------------------
def credit_events_file():
    """积分流水路径，跟随 _gw().USAGE_DIR 求值。"""
    return os.path.join(_gw().USAGE_DIR, "credit_events.jsonl")

def read_credit_events(realm=None, limit=20, offset=0):
    """积分变动流水，最新在前：{events, total, offset, limit}。"""
    realm = realm_scope(realm)
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    cutoff = time.time() - wb_accounts.CREDIT_EVENT_RETENTION_DAYS * 86400
    events = []
    try:
        with open(credit_events_file(), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if realm and row.get("realm") != realm:
                    continue
                if (row.get("at") or 0) < cutoff:
                    continue
                events.append(row)
    except FileNotFoundError:
        events = []
    events.reverse()
    return {"events": events[offset:offset + limit], "total": len(events),
            "offset": offset, "limit": limit}

_free_models_cache = {"at": 0.0, "data": None}
_FREE_MODELS_TTL = 60.0

def credits_is_free(value):
    """目录里的 credits 字符串表示「这个模型不花钱」时为 True。

    与 curate_remote_catalog() 挑免费同款用的是同一判定；缺失或解析不了的
    值不算免费，未知模型留在积分护栏之下，不会溜过去。
    """
    return str(value or "").strip().lower() in ("x0.00", "x0", "0", "0.00")

def free_models_by_realm():
    """realm -> 目录标记为免费（"x0.00"）的模型 id 集合。

    由内置快照与桌面缓存文件构建（都是本地读取，不走网络），缓存一分钟，
    请求路径零开销。每日积分护栏按 realm 用它区分付费与免费模型：同一个 id
    可能在一个出口免费、在另一个出口付费。
    """
    now = time.time()
    data = _free_models_cache.get("data")
    if data is not None and (now - _free_models_cache.get("at", 0.0)) < _FREE_MODELS_TTL:
        return data
    out = {}
    for realm, source in (("intl", getattr(wb_catalog, "STATIC_INTL_MODELS", [])),
                          ("cn", getattr(wb_catalog, "STATIC_CN_MODELS", []))):
        free = set()
        for item in source or []:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or "").strip()
            if mid and credits_is_free(item.get("credits")):
                free.add(mid)
        cached = read_cached_remote_catalog(realm)
        if cached:
            for mid, item in (cached[1] or {}).items():
                if isinstance(item, dict) and credits_is_free(item.get("credits")):
                    free.add(str(mid))
        out[realm] = free
    _free_models_cache.update({"at": now, "data": out})
    return out

def _usage_snapshot_scope(realm, range, since, until):
    """（过滤用的 realm, since, until, 缓存键）—— usage_snapshot() 与它的 ETag 共用。

    同 _perf_stats_scope()：键只允许有一个来源，否则 ETag 可能指向另一条缓存。
    """
    r = realm_scope(realm, _gw().CURRENT_REALM)
    lo, hi = range_window(range, since, until)
    # 用上下界，不用今天/全部的标志位：本周与本月窗口重叠，标志位会让
    # 一个窗口从缓存里取走另一个窗口的总计。
    key = "%s|%s|%s" % (r or "all",
                        lo if lo is not None else "", hi if hi is not None else "")
    return r, lo, hi, key

def usage_snapshot(realm=None, ttl=None, range=None, since=None, until=None):
    """缓存包装：面板每隔几秒轮询一次。

    重建刻意持锁执行：先放锁的话，缓存过期瞬间每个并发调用方都会各做一次
    JSONL 全量扫描，一次面板刷新能触发对同一文件的多次扫描。
    """
    ttl = _STATS_TTL if ttl is None else ttl
    r, lo, hi, key = _usage_snapshot_scope(realm, range, since, until)
    now = time.time()
    with _snap_lock:
        hit = _snap_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
        data = _usage_snapshot_uncached(r, since=lo, until=hi)
        _snap_cache[key] = (time.time(), data)
    return data

def usage_snapshot_etag(realm=None, range=None, since=None, until=None):
    """usage_snapshot() 当前缓存条目的 ETag；条目不存在时返回 None。

    配对调用（取数前后各一次、一致才发）由 _json_cached() 负责，理由见那里。
    """
    _r, _lo, _hi, key = _usage_snapshot_scope(realm, range, since, until)
    with _snap_lock:
        hit = _snap_cache.get(key)
    if hit is None:
        return None
    return _gw()._cache_etag("usage", key, hit[0])

def cost_estimate_view(realm=None, range=None, since=None, until=None):
    """费用估算：用当前窗口的按模型用量与设置里的官方价格折算。

    取数复用 usage_snapshot 的缓存；估算本身只是一次按模型的小循环，不单独
    缓存。没有配置价格的模型 priced=False，金额按 0 计，不进入合计。
    """
    snap = usage_snapshot(realm=realm, range=range, since=since, until=until)
    config = wb_settings.cost_estimate(_gw().ACCOUNTS_DIR)
    out = wb_cost.estimate(snap.get("by_model") or {}, config)
    lo, hi = range_window(range, since, until)
    out["realm"] = snap.get("realm") or "all"
    out["window"] = {"since": lo, "until": hi}
    return out

def _fold_cost(bucket, cost, model):
    """把一行的估算成本折进统计桶。

    没有价格的模型计入 cost_missing（id -> 次数），免得从总计里默默消失，
    面板因此能指出价格快照还没覆盖到哪些模型。总开关关闭时整项功能停用，
    不计入 cost_missing。
    """
    if cost["known"]:
        bucket["cost_cny"] = (bucket.get("cost_cny") or 0.0) + cost["cny"]
    elif not cost.get("disabled"):
        missing = bucket.setdefault("cost_missing", {})
        mid = model or "unknown"
        missing[mid] = missing.get(mid, 0) + 1

# wb_pricing 在价格文件缺失时返回的对象。没有这些常量，每次调用都拿到新的
# 空字典，下面的身份比较永不成立，没有价格文件的部署每次刷新都会重扫。
_EMPTY_POLICIES = {}
_EMPTY_TIMELINE = []

def _usage_log_key(path=None):
    """用量日志的 (size, device, inode)，文件不存在时返回零值。

    一次 stat 同时回答增量折叠的两个问题。size 表示缓存偏移计过的字节还在：
    文件被截短或被更短的文件替换都会变小。device 和 inode 用于判断文件是否
    相同：原地覆盖的日志 inode 不变，靠 size 发现；换成另一个文件时偏移会指向
    不相干行的中间。
    """
    try:
        info = os.stat(path or _gw().USAGE_LOG)
        return (info.st_size, info.st_dev, info.st_ino)
    except OSError:
        return (0, 0, 0)

def _log_offset_holds(previous, current, offset):
    """`offset` 仍落在 `previous` 所描述的文件之内时为 True。

    False 表示缓存的折叠算过的字节已经消失或现在属于另一个文件，
    必须从零重折。
    """
    return (previous is not None
            and previous[1] == current[1] and previous[2] == current[2]
            and current[0] >= offset)

# 每次刷新读回已计前缀的末尾多少字节，用来证明这段前缀还是原来的字节。
# 足够长，重写不可能碰巧吻合；足够短，轮询路径上零成本。
_TAIL_SIGNATURE = 64

def _log_tail_signature(offset, path=None):
    """`offset` 之前的最后几个字节；读不出来时是 None。

    增量折叠假设算过的字节还在原处。size 与 inode 能发现日志被换掉或被
    截短，发现不了「原地清空后又长回超过旧 offset」：同一个文件，只是更
    长，截断日志或 copytruncate 型日志轮转留下的就是这种形状。把接缝读回来
    才能区分两者：追加不动它，重写会动它。
    """
    if offset <= 0:
        return b""
    try:
        with open(path or _gw().USAGE_LOG, "rb") as fh:
            start = max(0, offset - _TAIL_SIGNATURE)
            fh.seek(start)
            return fh.read(offset - start)
    except OSError:
        return None

def _log_resume_ok(state, log_key, path=None):
    """缓存的 offset 仍落在日志当前前缀的原位时为 True。

    False 表示折叠不能续算：文件变短了、换了文件，或算过的字节被就地
    重写。
    """
    if not _log_offset_holds(state["key"], log_key, state["offset"]):
        return False
    return _log_tail_signature(state["offset"], path) == state["tail"]

# ---------------------------------------------------------------------------
# 「用到的价」状态指纹。
# 已折行的成本是历史价，后改价不改写昨天的总计；指纹只记折叠真正读到的价，
# 价表无关的变动不判废（否则重折一次十几秒）：
#   refs             折叠解析出的每个 pid 的策略内容摘要，当时查不到记 None
#                    （该 pid 必须继续缺席，出现即作废）
#   timeline         时间线依赖的投影摘要，没有依赖时为 None
#   timeline_at      无引用且成本非零行的 at 上界；timeline_models 是其模型集合
#   unresolved/backfilled  上述行里整条时间线没提到的模型 / 出现过回填解析
#   builtin          用到内置快照时，内置价目表文档的内容摘要
# 判定侧 _pricing_used_unchanged() 从当前价表重算同一份记录并逐项比对，不等就重折；
# 行级记忆与 series 不参与这套判定，聚合口径不变。
# ---------------------------------------------------------------------------
def _pricing_inputs(pricing_on=None):
    """cost_for_row() 除行本身之外读到的全部输入。

    比身份不比内容：wb_pricing 把每一项缓存在各自的文件键后，改过的文件会交回
    另一个对象，比身份让这项检查不占刷新路径——为发现变化而遍历整张价表比变化
    本身还贵。握着这些对象也是判定可靠的原因：活着的对象的 id 不会被分配给
    替身。

    总开关关着时 cost_for_row() 根本不打开价表，开关标志就是全部输入。它用与
    cost_for_row() 相同的无参调用读取——这里传目录的话，两边不一致时会从另一个
    设置文件作答，指纹就会盯着折叠根本没读过的输入。
    """
    # pricing_on 由调用方传入时不再自己问一次总开关：一次扫描里它只该被问一次
    # （见 _usage_snapshot_uncached / _compute_usage_analytics_uncached），指纹
    # 与折叠读到的因此是同一个值。
    try:
        if not (wb_pricing.pricing_enabled() if pricing_on is None else pricing_on):
            return (False, None, None, None)
        return (True,
                wb_pricing.load_policies() or _EMPTY_POLICIES,
                wb_pricing.load_timeline() or _EMPTY_TIMELINE,
                wb_pricing.load_pricing())
    except Exception as exc:
        _gw().log("pricing inputs unreadable: %s" % exc)
        return None

def _pricing_unchanged(previous, current):
    """两个 _pricing_inputs() 元组描述同一份价格时为 True。

    只留给行级结果记忆（_row_cost_remember）：那里按「价表代次」整表判定，
    保守但正确。状态指纹走 _pricing_used_unchanged()（T15 的收窄判定）。
    """
    if previous is None or current is None:
        return False
    return (previous[0] == current[0] and previous[1] is current[1]
            and previous[2] is current[2] and previous[3] is current[3])

# 记录里的固定字段（进程内构造与 checkpoint 采纳都按这一组校验）。
_PRICED_FIELDS = ("refs", "timeline", "timeline_at", "timeline_models",
                  "unresolved", "backfilled", "builtin")

def _pricing_record_new():
    """一份空的状态指纹记录（见上面一节；字段固定、可 JSON 往返）。"""
    return {"refs": {}, "timeline": None, "timeline_at": None,
            "timeline_models": [], "unresolved": [], "backfilled": False,
            "builtin": None}

def _pricing_record_ok(record):
    """状态指纹记录的形状；不合格的记录按「价表变了」处理（判废重折）。"""
    if not isinstance(record, dict):
        return False
    for field in _PRICED_FIELDS:
        if field not in record:
            return False
    refs = record["refs"]
    if not isinstance(refs, dict):
        return False
    for pid, digest in refs.items():
        if not isinstance(pid, str) or not (digest is None
                                            or isinstance(digest, str)):
            return False
    for field in ("timeline", "builtin"):
        value = record[field]
        if not (value is None or isinstance(value, str)):
            return False
    at = record["timeline_at"]
    if not (at is None or _usage_cache_number(at)):
        return False
    for field in ("timeline_models", "unresolved"):
        value = record[field]
        if not isinstance(value, list) or not all(isinstance(v, str)
                                                  for v in value):
            return False
    return isinstance(record["backfilled"], bool)

def _copy_pricing_record(record):
    """记录的深拷贝：落盘在状态锁外序列化，记录必须和活状态脱钩。"""
    if record is None:
        return None
    copy = dict(record)
    copy["refs"] = dict(record.get("refs") or {})
    copy["timeline_models"] = list(record.get("timeline_models") or ())
    copy["unresolved"] = list(record.get("unresolved") or ())
    return copy

def _timeline_at_value(value):
    """一个 at 值按数字比（坏值按 0，与折叠里的 `at or 0` 同口径）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

def _timeline_row_at(row):
    return _timeline_at_value(row.get("at"))

# 时间线投影摘要的记忆：值是一个元组（时间线对象, 参数键, 摘要），一次 get 拿全
# （与 _object_digest 的记忆同一个写法）——两个状态锁下的线程同时算也不会读到
# 拼起来的值；元组里握着时间线对象，身份不会被回收复用。
_pricing_timeline_memo = {}

def _pricing_timeline_digest(timeline, record):
    """记录里时间线依赖的投影摘要，折叠侧收尾与判定侧共用一份定义。

    投影是 [[行 at, 该行在 timeline_models 里的 {model: pid} 子字典], ...]，
    摘要取排序 JSON 的 sha1：
      * backfilled 为真时取整条时间线：那批行的解析扫过全部行，任何追加都可能
        改变结果；
      * 否则取 at <= timeline_at 的行外加最早那一行（二分夹逼 index=0 会读它），
        每行只取相关模型的子字典，无关模型的价变动不进依赖；行本身与 at 都要
        进投影，at 的增减会挪动二分结果；
      * unresolved 里提到过的行取自整条时间线，同理。

    timeline_models 为空表示没有依赖，返回 None。结果按时间线对象与记录参数
    记忆，一次刷新里 5 条状态共用同一份时间线，记录没变时只算一次。
    """
    models = record.get("timeline_models") or ()
    if not models:
        return None
    key = (record.get("timeline_at"), tuple(models),
           tuple(record.get("unresolved") or ()),
           bool(record.get("backfilled")))
    hit = _pricing_timeline_memo.get(id(timeline))
    if hit is not None and hit[0] is timeline and hit[1] == key:
        return hit[2]
    wanted = set(models)
    rows = list(timeline or ())
    parts = []
    if key[3]:
        scope = rows
    else:
        bound = _timeline_at_value(record.get("timeline_at"))
        scope = [row for row in rows if _timeline_row_at(row) <= bound]
        if rows and not scope:
            scope = rows[:1]
    parts.append([[_timeline_row_at(row),
                   {m: pid for m, pid in (row.get("policies") or {}).items()
                    if m in wanted}]
                  for row in scope])
    for model in sorted(record.get("unresolved") or ()):
        parts.append([[_timeline_row_at(row), (row.get("policies") or {})[model]]
                      for row in rows
                      if model in (row.get("policies") or {})])
    try:
        payload = json.dumps(parts, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
    except Exception:
        return None
    digest = hashlib.sha1(payload).hexdigest()
    if len(_pricing_timeline_memo) > 8:
        # 正常进程里时间线只在价格变化时换代；这里只是给长期运行兜底
        # （与 _object_digest 的记忆同一条规则）。
        _pricing_timeline_memo.clear()
    _pricing_timeline_memo[id(timeline)] = (timeline, key, digest)
    return digest

def _priced_note(priced, row, cost, pricing):
    """把一行「用到的价」记进状态指纹记录（折叠侧；见上面一节）。

    refs 收下解析出来的 pid 的内容摘要（显式引用 + 时间线解析）；无显式引用
    且成本非零的行另记时间线依赖。总开关关掉（cost 带 disabled）或价表读不
    出来（pricing 为 None）时不记——前者折叠没读任何价、后者记不完整，两种
    都由判定侧按「记录对不上」兜底作废。

    热路径开销：每行几次 dict 查找 + 按对象身份记忆的摘要（第一次之后 O(1)），
    只有无引用非零成本的行（旧前缀，一次性折过）才会碰列表；那批行里落到
    内置快照的（模型从未进过时间线）再问一次 policy_ref_at 把依赖记准。
    """
    if priced is None or pricing is None or not cost:
        return
    if cost.get("disabled"):
        return
    source = cost.get("source")
    if source and source != "builtin":
        if source not in priced["refs"]:
            # 一次扫描里价表不变，同一个 pid 的摘要只算一次：命中记忆之后
            # 这一步就只剩一次成员检查（真机 4.7 万行里只有几个不同 pid）。
            policy = (pricing[1] or {}).get(source)
            priced["refs"][source] = (_object_digest(policy)
                                      if policy is not None else None)
    else:
        # 内置快照：文档摘要一变，这些行的价就变（同一份文档只算一次摘要）。
        doc = pricing[3]
        if doc is not None and priced["builtin"] is None:
            priced["builtin"] = _object_digest(doc)
    pid = row.get("cost_policy")
    if pid:
        # 显式引用：价由 refs 看住，与时间线无关；引用的策略当前查不到时
        # 按「不存在」记，它一旦出现（内容当然也就变了）必须作废重折。
        if pid not in priced["refs"]:
            priced["refs"][pid] = None
        return
    if not cost.get("known") or not cost.get("cny"):
        return                       # 零成本：数值上不依赖价表（refs 已记）
    at = row.get("at") or 0
    bound = priced["timeline_at"]
    if bound is None or at > bound:
        priced["timeline_at"] = at
    model = row.get("model")
    if model is not None and model not in priced["timeline_models"]:
        priced["timeline_models"].append(model)
        priced["timeline_models"].sort()
    backfilled = bool(cost.get("backfilled"))
    if source == "builtin":
        # 解析结果没进 cost（pid 为空、或它的策略查不到，两条都落到内置快照）：
        # 再问一次时间线，把「解析出没出 pid / 是不是回填」记准。
        resolved, scan_backfilled = wb_pricing.policy_ref_at(at, model)
        if resolved is None:
            # 整条时间线都没提到这个模型：解析扫过全部行，之后任何时刻补一
            # 行都会改变结果，所以它按「整条时间线里提到它的行」记账。
            if model is not None and model not in priced["unresolved"]:
                priced["unresolved"].append(model)
                priced["unresolved"].sort()
        else:
            backfilled = backfilled or bool(scan_backfilled)
            if resolved not in priced["refs"]:
                priced["refs"][resolved] = None
    if backfilled:
        priced["backfilled"] = True

def _pricing_record_close(priced, pricing):
    """一次成功的折叠收尾：把时间线投影摘要刷成当前参数下的值。

    记录在折叠过程中增量更新（新行进来只加不改），摘要必须跟着参数走——
    下一次判定拿它跟「当前时间线按同一组参数算出来的投影」比。按时间线对象
    与参数记忆，参数没动时这里只是一次查表。
    """
    if priced is None or pricing is None:
        return
    priced["timeline"] = _pricing_timeline_digest(pricing[2], priced)

def _pricing_used_unchanged(record, pricing):
    """折叠行真正用到的价，与当前价表一致？（T15 的状态判废判定）

    只读记录与当前价表、不碰日志：refs 逐个 pid 比对内容摘要（查不到 = 变化；
    折叠时查不到的 pid 出现也是变化），时间线依赖重算同一份投影摘要，builtin
    比对文档摘要。任何一项对不上就返回 False（状态作废、重折）。

    pricing 为 None（价表读不出来）时不能确认，一律 False；总开关关着时折叠
    没读任何价，只有「折叠时也没用价」（record 为 None）才算一致。
    """
    if pricing is None:
        return False
    if not pricing[0]:
        return record is None
    if not _pricing_record_ok(record):
        return False
    policies = pricing[1]
    for pid, digest in record["refs"].items():
        policy = policies.get(pid) if policies else None
        current = _object_digest(policy) if policy is not None else None
        if current != digest:
            return False
    if record["timeline"] != _pricing_timeline_digest(pricing[2], record):
        return False
    if record["builtin"] is not None:
        if _object_digest(pricing[3]) != record["builtin"]:
            return False
    return True

def _realm_inputs():
    """没有 realm 字段的行归到哪个出口的依据。

    这类行（例如没有账号的错误行）按服务它的账号归属，查不到账号时归到模型的
    主区域。导入或删除账号都会挪动归属，缓存的聚合因此不能比它活得久。

    当前出口与 uid 到 realm 的映射都从磁盘同步读取，不从活账号池的内存映射
    读：池只是启动时的快照，「何时加载完」会让同一进程在不同时刻算出不同指纹，
    重启后刚写下的 checkpoint 反而被判无效。池仍要查，但只用来判断这次折叠能否
    被冷进程复现（见 _realm_fold_reproducible），不进指纹的值。
    """
    disk = _account_realm_map()
    if disk is None:
        return None
    return (_gw().CURRENT_REALM, disk)

_realm_disk_cache = {"key": None, "map": None}
_realm_disk_lock = threading.Lock()

def _account_files_key(directory):
    """账号目录的便宜指纹：文件名 + (size, mtime)；读不到返回 None。"""
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:
        return None
    entries = []
    for name in names:
        try:
            info = os.stat(os.path.join(directory, name))
        except OSError:
            return None
        entries.append((name, info.st_size, info.st_mtime_ns))
    return (directory, tuple(entries))

def _account_realm_map(directory=None):
    """账号文件里的 uid→realm 映射（排序后的元组）；读不到返回 None。

    用账号池自己的加载器推导，不在这边复制一份 uid/realm 解析逻辑：复制
    出来的那份迟早会和 Account 里的逻辑漂移，而漂移意味着指纹描述的东西和
    折叠读的东西不再是同一个。整份读一次的开销由缓存键挡着——目录下每个
    *.json 的名字/size/mtime 都没变就直接用上次的结果。
    """
    directory = _gw().ACCOUNTS_DIR if directory is None else directory
    key = _account_files_key(directory)
    if key is None:
        return None
    with _realm_disk_lock:
        cached = _realm_disk_cache
        if cached["key"] == key:
            return cached["map"]
    try:
        pool = wb_accounts.AccountPool(directory, log=None)
        pool.load()
        value = tuple(sorted((a.uid, a.realm) for a in pool.accounts if a.uid))
    except Exception as exc:
        _gw().log("account realm map unreadable: %s" % exc)
        return None
    with _realm_disk_lock:
        _realm_disk_cache.update({"key": key, "map": value})
    return value

def _realm_fold_reproducible(disk):
    """这次折叠用的归属，冷启动的进程能不能按同一份磁盘原样复现。

    折叠读的是活账号池，指纹读的是磁盘。两者一致（正常情况：池就是启动时从
    这份磁盘加载的）时没有疑问；磁盘被改过、池还没重载时两者会短暂不一致，
    那种折叠写下的 checkpoint 会被冷进程按新归属当成有效，所以不写。池整个
    不存在时折叠根本不查账号映射——只有磁盘上也没有账号，两边才一致。
    """
    if _gw().POOL is None:
        return not disk
    try:
        pool = tuple(sorted((a.uid, a.realm) for a in _gw().POOL.accounts))
    except Exception as exc:
        _gw().log("realm inputs unreadable: %s" % exc)
        return False
    return pool == disk

def _scan_usage_from(offset, fold, stop_at=None, skip=None, skip_raw=None, path=None):
    """把 offset 之后的每一行折进 fold(row)，返回 (offset, error)。

    返回的偏移是最后一行处理完的位置。行是整行追加的，末尾没有换行的行留给
    下一轮，与 _scan_daily_usage() 的规则相同。

    `skip(line)` 在解析前丢弃整行，有窗口的读者传 _line_outside_window()，
    被窗口排除的行因此只付一次子串搜索；`skip_raw(raw)` 是同一判断在解码前对
    原始字节执行，读者传 _line_outside_window_bytes()，它只对文本判断也会丢
    的行返回真，所以被它丢掉的行连解码都省了。

    `stop_at` 把读取限制在 all-time 折叠已覆盖的偏移内，窗口载荷因此与它的
    all-time 半描述同一批字节。`path` 默认为用量日志。

    日志按二进制读取，偏移按行字节数推进：每行 tell() 比解析本身还贵，而偏移
    只需是字节计数。`error` 是中断读取的异常，或 None；fold 抛异常时保留已折
    部分，返回的偏移停在该行末尾，调用方据此重新折叠该行，不从它之后续读。
    """
    if stop_at is not None and offset >= stop_at:
        return offset, None
    try:
        with open(path or _gw().USAGE_LOG, "rb") as fh:
            fh.seek(offset)
            while True:
                if stop_at is not None and offset >= stop_at:
                    break
                raw = fh.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break
                end = offset + len(raw)
                if stop_at is not None and end > stop_at:
                    # 这一行骑在已覆盖字节的边上，all-time 折叠也没见过它，
                    # 这一轮同样跳过。
                    break
                # 字节级预过滤先跑：它能精确读出并判到窗口外的行根本不会变成
                # 文本。它只对下面的文本过滤同样会丢的行作答（见
                # _line_outside_window_bytes），所以这一轮折的行与之前完全相同。
                if skip_raw is not None and skip_raw(raw):
                    offset = end
                    continue
                try:
                    # 解码先于一切，与文本模式的读者完全一致：解不了的行就地
                    # 挡住所有读者，这一轮同样停在它那里，绝不计入全量扫描从未
                    # 到达的行。剥空白按文本做（不按字节），两边对空行的判定
                    # 因此一致。
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    return end, exc
                if not line:
                    offset = end
                    continue
                if skip is not None and skip(line):
                    offset = end
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    offset = end
                    continue
                try:
                    fold(row)
                except Exception as exc:
                    return end, exc
                offset = end
    except FileNotFoundError:
        pass
    except Exception as exc:
        return offset, exc
    return offset, None

def _copy_usage_bucket(bucket):
    """拷贝一个 by_model* 桶，含它的 accounts 字典。"""
    out = dict(bucket)
    out["accounts"] = dict(bucket["accounts"])
    return out

def _copy_usage_snapshot(snap):
    """调用方可以装饰并对外发放的快照拷贝。

    折叠结果被缓存并共享，离开本模块的必须是拷贝——与
    _daily_state_copy() 之于日计数是同一个原因。只有三个按模型的视图有嵌套，
    其余值都是标量。
    """
    out = dict(snap)
    out["cost_missing"] = dict(snap["cost_missing"])
    out["by_model"] = {m: _copy_usage_bucket(b) for m, b in snap["by_model"].items()}
    out["by_model_realm"] = {m: {rr: _copy_usage_bucket(b) for rr, b in realms.items()}
                             for m, realms in snap["by_model_realm"].items()}
    out["by_model_acct"] = {
        m: {rr: {acct: _copy_usage_bucket(b) for acct, b in accts.items()}
            for rr, accts in realms.items()}
        for m, realms in snap["by_model_acct"].items()}
    return out

# ---------------------------------------------------------------------------
# 行级结果记忆：把同一行算出来的结果跨视图复用。
#
# 两个结果都是行的纯函数（日键只看 at，进程内时区固定；定价只看行的定价字段与
# 价表本身），第二个视图直接取上次的结果。记的是结果不是状态：五个聚合仍各持
# 各的折叠状态，这里只存「这行折进哪天」与「这行值多少钱」两个标量，位置与
# 指纹校验、realm 归属、日桶语义照旧。
# 键不能用行对象的 id()：三次扫描各自 json.loads 出各自的对象，旧对象释放后 id
# 会被复用。日键按 at 记；定价按它真正读取的字段记（model、cost_policy、at、
# 三个 token 数）再加价表代次，代次就是扫描开头那份 _pricing_inputs() 元组，
# 换代后旧条目自然不命中，同一代次内命中值必然等于现算值。
# 两张表按条目数封顶，满了只收不比表内最旧一条更旧的行（扫描从头往尾走，留下
# 的都是最近的行，第二个视图照样命中）。上限用 WB_ROW_MEMO_MAX 与
# WB_ROW_COST_MEMO_MAX 调整，0 表示关掉该表。不落盘，重启后重算即可。
# ---------------------------------------------------------------------------
# 条目上限按「典型日志装得下大半、内存留在个位数 MB」取值，内存宽裕把
# _ROW_COST_MEMO_MAX 调到行数以上拿满跨视图复用，内存紧调 0 关掉。两个值在
# 导入时从环境变量读一次（部署级上限开关，逐行查环境变量不划算），运行期直接
# 改模块属性。
_ROW_MEMO_MAX = 65536
_ROW_COST_MEMO_MAX = 16384

def _row_memo_env_cap(name, default):
    """导入时读一次上限；读不到或解析不出来就按默认。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default

_ROW_MEMO_MAX = _row_memo_env_cap("WB_ROW_MEMO_MAX", _ROW_MEMO_MAX)
_ROW_COST_MEMO_MAX = _row_memo_env_cap("WB_ROW_COST_MEMO_MAX", _ROW_COST_MEMO_MAX)

_row_day_memo = {}          # at -> "YYYY-MM-DD"
_row_cost_memo = {}         # (model, cost_policy, at, pt, ct, comp) -> （价表代次, 定价结果）
_row_cost_gen = None        # 定价表当前代次（_pricing_inputs() 的元组）
_row_memo_tz = time.tzname  # 日键表绑在哪个时区上（见 _row_memo_tz_changed）

def _row_memo_at(value):
    """记忆表的「新旧」比较键：只有数字能比大小，别的（None / 坏行）一律按 0。

    比大小只决定「这条记不记」，不决定结果——比不动就不比，照记。
    """
    return value if isinstance(value, (int, float)) else 0

def _row_memo_tz_changed():
    """进程时区被 tzset() 换过 → 日键表整体作废（定价表不受影响）。

    日键是本地时区的函数，跨时区复用旧值会算错天。CPython 的 time.tzset()
    每次都会重建 time.tzname 这个元组对象，所以「对象换了」就是「时区换过」；
    没换时这里只是一次身份比较。生产里进程时区不会变，这一句就是白送的。
    """
    global _row_memo_tz
    now = time.tzname
    if _row_memo_tz is now:
        return False
    _row_memo_tz = now
    _row_day_memo.clear()
    return True

def _cost_entry(pricing, cost):
    """cost_for_row(details=False) 的返回值 -> 记忆条目（定长元组）。"""
    return (pricing, cost["known"], cost["cny"], cost.get("band"),
            cost.get("rates"), cost.get("source"), cost.get("source_at"),
            cost.get("backfilled"), bool(cost.get("disabled")))

def _cost_from_memo(entry):
    """记忆条目 -> 与 cost_for_row(details=False) 逐键同序的 dict。

    两条分支的形状与原函数一一对应：总开关关掉那一支不带 rates、多一个
    disabled；有价/没价那一支带 rates。键序照抄原函数的插入顺序，别处
    按字节对拍时看到的是一样的 dict。
    """
    cost = {"cny": entry[2], "known": entry[1], "band": entry[3]}
    if entry[8]:
        cost["source"] = "builtin"
        cost["source_at"] = None
        cost["backfilled"] = False
        cost["disabled"] = True
        return cost
    cost["rates"] = entry[4]
    cost["source"] = entry[5]
    cost["source_at"] = entry[6]
    cost["backfilled"] = entry[7]
    return cost

def _row_cost(row, pricing, pricing_on):
    """一行的估算成本，跨视图复用（见「行级结果记忆」一节）。

    pricing 是本次扫描开头取到的 _pricing_inputs() 元组：命中要求条目与它
    同代。pricing 为 None（价表读不出来）时不进表，照旧每行现算——那条路径
    本来就不该被记忆；空行（falsy row）同理，它走的是原函数的短路分支。

    返回的形状与 wb_pricing.cost_for_row(..., details=False) 逐键相同；命中
    时按条目重建一份，调用方拿到的是自己的副本，与现算路径一致。
    """
    if pricing is None or not row:
        return wb_pricing.cost_for_row(row, details=False, enabled=pricing_on)
    key = (row.get("model"), row.get("cost_policy"), row.get("at"),
           row.get("prompt_tokens"), row.get("cached_tokens"),
           row.get("completion_tokens"))
    memo = _row_cost_memo
    try:
        entry = memo.get(key)
    except TypeError:
        # 不可哈希的字段（坏行）：原函数到 (doc.get("models") or {}).get(model)
        # 那一步也会抛 TypeError，这里不抢先，交给它
        return wb_pricing.cost_for_row(row, details=False, enabled=pricing_on,
                                       pricing_doc=pricing[3])
    if entry is not None and _pricing_unchanged(entry[0], pricing):
        return _cost_from_memo(entry)
    cost = wb_pricing.cost_for_row(row, details=False, enabled=pricing_on,
                                   pricing_doc=pricing[3])
    _row_cost_remember(key, pricing, cost)
    return cost

def _row_cost_remember(key, pricing, cost):
    """把一行的定价结果记进有界表；价表换代时整表作废。

    换代只可能来自价表文件被改写（策略表/时间线/价目表任一新对象），发生时
    旧条目按定义全部失效，直接清掉，别让它们白占位置。判定用的是
    _pricing_unchanged——与状态指纹同一条规则，两处不会漂移。
    """
    global _row_cost_gen
    memo = _row_cost_memo
    if _row_cost_gen is None or not _pricing_unchanged(_row_cost_gen, pricing):
        memo.clear()
        _row_cost_gen = pricing
    cap = _ROW_COST_MEMO_MAX
    if cap <= 0:
        return
    if len(memo) >= cap:
        oldest = next(iter(memo))
        if _row_memo_at(key[2]) < _row_memo_at(oldest[2]):
            # 比表里最旧的还旧：记了也会立刻被顶掉，不如不记
            return
        memo.pop(oldest, None)
    memo[key] = _cost_entry(pricing, cost)

def _fold_usage_snapshot(row, snap, r, since, until, pricing_on=None, also=None,
                         pricing=None, priced=None):
    """把一行解析结果折进用量快照。

    全时段折叠（只喂偏移之后的行）与窗口折叠（喂整份文件，窗口外的行在解析前
    丢掉）共用这一个定义。先做区域与窗口判断，下面的请求数、token、按模型与
    按账号分解因此都描述同一批日志。

    pricing_on 是本次扫描开头问过一次的总开关：聚合只读 known 与 cny，整扫不建
    悬停明细也不逐行重问，代价是设置改动从下一次扫描起生效。also 是同一行还要
    折进的其余同形状快照，过滤、算价与字段提取只做一次，最贵的价表查询与区域
    判定只付一次。

    pricing 是本次扫描开头取到的 _pricing_inputs() 元组，同时当行级记忆的代次，
    None 表示价表读不出来。priced 是本轮要填的「用到的价」记录，只有推进状态
    的折叠传它。
    """
    if r and not row_matches_realm(row, r):
        return
    at = row.get("at") or 0
    if since and at < since:
        return
    if until and at > until:
        return
    outcome = row_outcome(row)
    # 每行按它发生时生效的价格版本计价，后改价不会改写昨天的总计。
    # _row_cost 按行记忆（见「行级结果记忆」）：同一个视图里这一行只算一次，
    # 第二个视图直接取上次的结果；pricing 是本次扫描的价表代次。
    cost = _row_cost(row, pricing, pricing_on)
    if outcome != "client_aborted":
        # 折进去的每一行都把「用到的价」记进状态指纹（中止行只记 errors，
        # 不碰价，所以不记）。记的是这次折叠真正读到的价，价表无关的变动
        # 因此不再判废这条状态（见「用到的价」状态指纹一节）。
        _priced_note(priced, row, cost, pricing)
    model = row.get("model")
    targets = (snap,) if not also else (snap,) + tuple(also)
    if outcome != "completed":
        for tgt in targets:
            tgt["errors"] += 1
            # 积分是已经花掉的钱：上游计费之后才失败的请求仍然消耗了积分，
            # 这里与 analytics 页同口径求和。token 合计维持本页的
            # completed-only 规则，客户端中止跳过，它的 usage 块不完整。
            if outcome != "client_aborted":
                tgt["credit"] += (row.get("credit") or 0)
                _fold_cost(tgt, cost, model)
        return
    # 每个被统计字段只读一次：这行落进下面四个桶，每个桶都遍历同样的字段。
    present = [(k, row[k] or 0) for k in USAGE_FIELDS if k in row]
    m = model or "unknown"
    rr = row_realm(row)
    acct_id = row.get("account")
    acct_key = acct_id or "(unattributed)"
    known = cost["known"]
    cny = cost["cny"] if known else 0.0
    for tgt in targets:
        tgt["requests"] += 1
        for k, value in present:
            tgt[k] += value
        _fold_cost(tgt, cost, model)
        per = tgt["by_model"].setdefault(m, {"requests": 0, "accounts": {}, "cost_cny": 0.0, **{k: 0 for k in USAGE_FIELDS}})
        per_realm = tgt["by_model_realm"].setdefault(m, {}).setdefault(
            rr, {"requests": 0, "accounts": {}, "cost_cny": 0.0, **{k: 0 for k in USAGE_FIELDS}})
        per_acct = (tgt["by_model_acct"].setdefault(m, {})
                    .setdefault(rr, {})
                    .setdefault(acct_key, {"requests": 0, "accounts": {},
                                           "cost_cny": 0.0,
                                           **{k: 0 for k in USAGE_FIELDS}}))
        for bucket in (per, per_realm, per_acct):
            bucket["requests"] += 1
            for k, value in present:
                bucket[k] += value
            if known:
                bucket["cost_cny"] += cny
            if acct_id:
                accounts = bucket["accounts"]
                accounts[acct_id] = accounts.get(acct_id, 0) + 1

# ---------------------------------------------------------------------------
# 增量的全时段聚合。
#
# 日志每天增长上千行，面板的全时段视图不能在每次缓存失效时重读整份
# usage.jsonl。总计只在追加行时变化，所以每份聚合记住已折到的字节偏移，刷新
# 只读其后的行，与 daily_usage_stats() 统计今日用量的做法相同，也带截断保护。
# 有窗口的查询仍走文件，但预过滤会在解析前丢掉窗口外的行。
#
# 沿用旧总计的前提是折叠读到的输入都没变，因此状态里还带其他输入的指纹：
# 价表（新策略会给没有引用的行重新计价）与无 realm 字段时的归属区域。任一
# 变化就从头折叠，只看偏移会把昨天的价格一直用下去。
#
# 每份聚合按区域各存一份状态：它只含几个模型与几个账号，比重新扫描便宜得多。
# ---------------------------------------------------------------------------
_usage_snap_state = {}
_usage_snap_state_lock = threading.Lock()

def _usage_snap_state_advance(r, pricing_on):
    """取或建状态、采纳 checkpoint、判废归零、记身份指纹，不做折叠。

    调用方必须持有 _usage_snap_state_lock。返回本次扫描取到的价表代次，调用方
    把它传给折叠当行级记忆的代次，与记进状态指纹的是同一份。

    状态指纹记录折叠行真正用到的价（见「用到的价」状态指纹一节）：折叠时逐行
    更新，扫描成功后用 _pricing_record_close 刷新投影摘要，下次判废时与当前
    价表重算的结果比对。

    状态是否冷（这次要付全量重折）只看 state["offset"]：大于 0 表示可续折，
    等于 0 表示状态刚建、刚被判废或 checkpoint 采纳失败。判定不折叠任何行，
    窗口路径据此分流，重折后两边字节一致。采纳与判废只在这里定义，
    _usage_snap_state_ready 与窗口冷热判定共用，规则不会漂移。
    """
    state = _usage_snap_state.get(r)
    if state is None:
        state = _usage_snap_state[r] = {"snap": None, "offset": 0, "key": None,
                                        "pricing": None, "realm": None,
                                        "tail": b"", "days": {}, "days_floor": None,
                                        "hours_floor_at": None,
                                        "hits": [], "hits_overflow": False}
    pricing = _pricing_inputs(pricing_on)
    realm = _realm_inputs()
    log_key = _usage_log_key()
    # 重折时状态指纹从这份空记录重新长起；总开关关着 / 价表读不出来时
    # 记 None（折叠没读任何价，判定侧只认「记录也是 None」这一种一致）。
    fresh = _pricing_record_new() if (pricing is not None
                                      and pricing[0]) else None
    if state["snap"] is None:
        # 全新状态（进程刚起来）：先问 checkpoint，问不到才从零折起。
        if not _usage_cache_adopt_snapshot(r, state, log_key, pricing, realm):
            state.update({"snap": _empty_stats(), "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None, "hours_floor_at": None,
                          "hits": [], "hits_overflow": False, "pricing": fresh})
    elif (not _pricing_used_unchanged(state["pricing"], pricing)
            or state["realm"] != realm
            or not _log_resume_ok(state, log_key)):
        state.update({"snap": _empty_stats(), "offset": 0, "tail": b"",
                      "days": {}, "days_floor": None, "hours_floor_at": None,
                      "hits": [], "hits_overflow": False, "pricing": fresh})
    state.update({"realm": realm, "key": log_key})
    return pricing

def _usage_snap_state_cold(r, pricing_on):
    """窗口请求现在会不会付一次全量重折？True = 会（窗口改走冷扫描）。

    调用方必须已持有 _usage_snap_state_lock。判定就是 all-time 折叠的前半段
    （_usage_snap_state_advance），不折任何行；为真时窗口半改走按日/按小时
    分组的冷扫描（_scan_usage_snapshot_window_days / _hours，~1s 量级），
    重折留给 all-time 视图（_usage_alltime_snapshot）与预热器。

    这条路不写折叠结果，也不改「重折该不该发生」：状态里 offset 仍是 0，
    下一个 all-time 请求照样从文件头折到文件尾——窗口请求不会把状态冻在
    冷态。重折发生之后，走不走这条路给出的字节完全相同（冷扫描与日桶/小时
    桶之和本就是同一批行、同一个求和口径，见两个冷路径函数的 docstring）。
    """
    _usage_snap_state_advance(r, pricing_on)
    return _usage_snap_state[r]["offset"] <= 0

def _usage_snap_state_ready(r, pricing_on):
    """把 `r` 的 all-time 折叠推进到文件尾；返回 (state, offset, error)。

    调用方必须已持有 _usage_snap_state_lock。日桶与 all-time 折叠共用这一次
    扫描（见 _fold_usage_snapshot 的 also），所以窗口半与 all-time 半描述的
    永远是同一段字节。

    采纳 checkpoint 与判废归零在 _usage_snap_state_advance 里（窗口路径的
    冷热判定也用那一段，见 _usage_snap_state_cold），这里只管折到文件尾。
    """
    pricing = _usage_snap_state_advance(r, pricing_on)
    state = _usage_snap_state[r]
    priced = state["pricing"]
    day_buckets = _usage_day_buckets_enabled()

    def fold(row):
        # 一行折三处：all-time 半 + 它所属那天的窗口半 + 那个小时的小时桶。
        # 过滤与算价在 _fold_usage_snapshot 里只做一次。
        day = _usage_day_bucket(state, row, r, _day_snapshot_bucket, day_buckets)
        hour = None if day is None else _usage_hour_bucket(state, row, day)
        extras = () if day is None else ((day,) if hour is None else (day, hour))
        _fold_usage_snapshot(row, state["snap"], r, None, None, pricing_on,
                             also=extras, pricing=pricing, priced=priced)

    offset, error = _scan_usage_from(state["offset"], fold)
    if error is None:
        state["offset"] = offset
        state["tail"] = _log_tail_signature(offset)
        _pricing_record_close(priced, pricing)
    return state, offset, error

def _usage_alltime_snapshot(r, pricing_on=None):
    """区域过滤 `r`（None = 全部区域）的缓存 all-time 折叠的私有拷贝。

    每个区域一份状态：过滤决定折叠能看到哪些行，`intl` 与 `cn` 是两份折叠，
    谁也不能继承对方的行。状态只持有原始折叠——`started`、`since`、
    `accounts_map` 等每次调用重新派生，与全量扫描的派生方式完全一致。
    """
    with _usage_snap_state_lock:
        state, offset, error = _usage_snap_state_ready(r, pricing_on)
        if error is None:
            # 持锁期间拷贝：调用方会装饰拿到的东西，并发刷新绝不能被看到
            # 改了一半。
            snap = _copy_usage_snapshot(state["snap"])
        else:
            # 折不进去的那一行可能已经把自己折了一半，结转的总计不能从那里
            # 续算：这次调用回答已折的部分（全量扫描报的也是同样的部分总计），
            # 下次刷新从零开始——全量扫描每次调用都是这么做的。
            _gw().log(f"usage snapshot read failed: {error}")
            snap = state["snap"]
            state.update({"snap": None, "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None, "hours_floor_at": None,
                          "hits": [], "hits_overflow": False})
    # 落盘决定在锁外做（见「聚合状态落盘」一节）：写盘要逐把读三份状态，在状态
    # 锁里再取别的状态锁会构成锁序环。折叠失败的那次不写——它的 offset 已经
    # 归零，落盘函数也会跳过。
    if error is None:
        _usage_cache_maybe_save("snapshot", offset)
    return snap

def _scan_usage_snapshot_window(r, since, until, pricing_on=None):
    """扫一遍日志，只保留窗口内的行。

    窗口随钟表移动，这里不沿用任何状态，但预过滤会让窗口外的行免去解析与解码，
    多日日志上的冷启动日/周视图因此不贵。

    边界以 `or None` 传给预过滤，与折叠自身的判断一致：0 是被折叠忽略的边界，
    预过滤不能把它读成「纪元之后全部丢弃」。

    价表在这里取一次，与 all-time 折叠同一条口径，一次扫描一份快照，本次扫描的
    行因此可以和别的视图共享定价结果。
    """
    pricing = _pricing_inputs(pricing_on)
    snap = _empty_stats()
    _, error = _scan_usage_from(
        0,
        lambda row: _fold_usage_snapshot(row, snap, r, since, until, pricing_on,
                                         pricing=pricing),
        skip=lambda line: _line_outside_window(line, since or None,
                                                 until or None),
        skip_raw=lambda raw: _line_outside_window_bytes(raw, since or None,
                                                        until or None))
    if error is not None:
        _gw().log(f"usage snapshot read failed: {error}")
    return snap

def _usage_snapshot_window_from_days(r, since, until, pricing_on):
    """窗口半快照 = 窗口内的日桶之和，日桶不可用时返回 None。

    先把 all-time 折叠推进到文件尾：日桶与它同一次扫描推进，窗口半与 all-time
    半因此描述同一段字节。

    until 开放时窗口从起点到文件尾；until 给出时必须是本地午夜，窗口就是起点
    那天起、until 那天之前的日桶之和。桶按 at 严格切分，恰好落在 until 本地的
    行归 until 那天那一桶；桶切不出那一刻的行时整条路返回 None（见
    _closed_window_edge_ok），调用方接着整段扫描。

    状态冷时直接返回 None：那条路要先付一次全量重折，窗口请求不背它，调用方
    退回按日分组的冷扫描，重折留给 all-time 视图与预热器。
    """
    with _usage_snap_state_lock:
        if _usage_snap_state_cold(r, pricing_on):
            return None
        state, offset, error = _usage_snap_state_ready(r, pricing_on)
        if error is not None:
            # 折叠失败时状态已经归零，日桶跟着清空：这一次退回整段扫描，
            # 与「没有缓存」时的行为一致。
            _gw().log(f"usage snapshot read failed: {error}")
            state.update({"snap": None, "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None, "hours_floor_at": None,
                          "hits": [], "hits_overflow": False})
            return None
        day_key = _window_day_key_safe(since)
        if day_key is None or not _days_cover(state, day_key):
            return None
        until_key = None
        if until is not None:
            until_key = _window_day_key_safe(until)
            if until_key is None or not _closed_window_edge_ok(state, until):
                return None
        snap = _empty_stats()
        days = state["days"]
        for key in sorted(days):
            if key < day_key:
                continue
            if until_key is not None and key >= until_key:
                break
            _add_usage_snapshot(snap, days[key])
    _usage_cache_maybe_save("snapshot", offset)
    return snap

def _usage_snapshot_window_from_hours(r, since, until, pricing_on):
    """窗口半快照 = 整点窗口内的小时桶之和，小时层不可用时返回 None。

    与日桶版同一条规矩：先把 all-time 折叠推进到文件尾，再从存量桶求和。下列
    条件缺一不可，任何一条不成立就返回 None，调用方退回按小时分组的冷路径：
      * 窗口起点是本地整点（见 _hour_window_key）；
      * days_floor 覆盖窗口起点，因为小时桶挂在日桶上、剪枝跟着日桶走；
      * 选中的每个日桶都带小时表：某天格点不成立时整张表会被摘掉（见
        _usage_hour_bucket），这时整个窗口退回扫描，不能只跳过那一天；
      * 每个小时桶的 at 落在窗口起点加 k 小时的格点上，并与所属日桶对得上，
        这一条挡住夏令时挪动日边界的情形；
      * until 给出时也必须落在同一套小时格点上，且直到那一刻没有整秒行
        （见 _closed_window_edge_ok）。

    状态冷时直接返回 None，调用方退回按小时分组的冷扫描，重折留给 all-time
    视图与预热器。
    """
    key = _hour_window_key(since)
    if key is None:
        return None
    day_key, k0 = key
    until_key = None
    if until is not None:
        until_key = _hour_window_key(until)
        if until_key is None:
            return None
    with _usage_snap_state_lock:
        if _usage_snap_state_cold(r, pricing_on):
            return None
        state, offset, error = _usage_snap_state_ready(r, pricing_on)
        if error is not None:
            _gw().log(f"usage snapshot read failed: {error}")
            state.update({"snap": None, "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None, "hours_floor_at": None,
                          "hits": [], "hits_overflow": False})
            return None
        if not _days_cover(state, day_key):
            return None
        if not _hours_cover(state, since):
            return None
        if until_key is not None and not _closed_window_edge_ok(state, until):
            return None
        lo = int(since)
        snap = _empty_stats()
        days = state["days"]
        for key in sorted(days):
            if key < day_key:
                continue
            if until_key is not None and key > until_key[0]:
                break
            bucket = days[key]
            hours = bucket.get("hours")
            if hours is None:
                return None
            day_at = int(bucket["at"])
            for hour in sorted(hours, key=int):
                if key == day_key and int(hour) < k0:
                    # 起点那天只看起点之后的整点：更早的桶属于窗口外，跳过
                    # 它们不影响正确性（桶按 at 严格切分，见 _usage_hour_bucket）。
                    continue
                sub = hours[hour]
                at = int(sub["at"])
                if at != day_at + int(hour) * 3600:
                    return None
                if (at - lo) % 3600:
                    return None
                if until_key is not None and (key, int(hour)) >= until_key:
                    # 闭窗口：until 那一刻归后一桶（桶按 at 严格切分），
                    # 只取 at < until 的桶；那一刻有整秒行时上面已经退回。
                    break
                _add_usage_snapshot(snap, sub)
    _usage_cache_maybe_save("snapshot", offset)
    return snap

def _usage_snapshot_window(r, since, until, pricing_on=None):
    """窗口半快照：同一窗口由日桶/小时桶相加，其余保持整段扫描。

    只有「两端都落在同一套格点上」的窗口拆得成存量桶的并集：起点是本地
    午夜、终点（开放或闭）也是本地午夜时用日桶（today / week / month 与
    「到某天零点」都是），两端落在本地整点上时用小时桶（自定义区间的常见
    形态，见 _hour_window_key）。闭窗口的右边界按「at < until 的桶」切
    （见 _closed_window_edge_ok）：那一刻有整秒行时退回整段扫描，结果与
    改动前逐字节一致——闭窗口本来走的就是这条兜底。起点不落在格点上、
    或 until 不落格点的窗口同样保持整段扫描。
    """
    if until is None:
        day_key = _window_day_key_safe(since)
        if day_key is not None and _usage_day_buckets_enabled():
            snap = _usage_snapshot_window_from_days(r, since, None, pricing_on)
            if snap is not None:
                return snap
            # 日桶没盖住窗口起点（还没折过/被剪枝/折叠出错）：冷路径现折一份
            # 按日分组的窗口半，口径与日桶状态一致，两条路径的字节因此相同。
            return _scan_usage_snapshot_window_days(r, since, None, pricing_on)
        if _usage_day_buckets_enabled() and _window_hour_key_safe(since) is not None:
            snap = _usage_snapshot_window_from_hours(r, since, None, pricing_on)
            if snap is not None:
                return snap
            # 小时层没盖住窗口起点（还没折过/被剪枝/那天的小时格点不成立）：
            # 冷路径现折一份按小时分组的窗口半，口径与小时桶状态一致。
            return _scan_usage_snapshot_window_hours(r, since, None, pricing_on)
        return _scan_usage_snapshot_window(r, since, until, pricing_on)
    # 闭窗口：两端都落在格点上才有桶可借（与上面同一套格点校验）。
    if _usage_day_buckets_enabled():
        if (_window_day_key_safe(since) is not None
                and _window_day_key_safe(until) is not None):
            snap = _usage_snapshot_window_from_days(r, since, until, pricing_on)
            if snap is not None:
                return snap
            return _scan_usage_snapshot_window_days(r, since, until, pricing_on)
        if (_window_hour_key_safe(since) is not None
                and _window_hour_key_safe(until) is not None):
            snap = _usage_snapshot_window_from_hours(r, since, until, pricing_on)
            if snap is not None:
                return snap
            return _scan_usage_snapshot_window_hours(r, since, until, pricing_on)
    return _scan_usage_snapshot_window(r, since, until, pricing_on)

def _usage_snapshot_uncached(realm=None, since=None, until=None):
    # None 表示全部区域；usage_snapshot() 已经把 "all" 映射成它，
    # 所以下面的过滤直接跳过。
    r = realm
    rep = _gw().POOL.representative(realm=r) if _gw().POOL else _gw().current_account()
    # 总开关整扫只问一次，值一路传进折叠与价格指纹：每行重问一次就是每行一次
    # os.stat，2.7 万行上很可观（见 tests/_test_usage_aggregate_fold.py）。
    pricing_on = wb_pricing.pricing_enabled()
    if since is None and until is None:
        # 开放窗口覆盖所有行，而行只会追加在日志末尾，所以 all-time 总计
        # 从上次折叠结转，只读其后追加的行。
        snap = _usage_alltime_snapshot(r, pricing_on)
    else:
        snap = _usage_snapshot_window(r, since, until, pricing_on)
    snap["started"] = _usage.get("started", time.time())
    snap["since"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap.get("started", time.time())))
    snap["log_file"] = _gw().USAGE_LOG
    snap["realm"] = r or "all"
    # 成本数字是人民币；面板按这个汇率折算显示美元，不必来回换算。
    snap["usd_cny"] = wb_pricing.usd_cny()
    snap["accounts_map"] = {a.uid: {"nickname": a.nickname, "realm": a.realm} for a in _gw().POOL.accounts} if _gw().POOL else {}
    snap["account"] = {
        "uid": (rep.uid if rep else ""),
        "domain": (rep.domain if rep else ""),
        "issuer": (wb_accounts.jwt_issuer(rep.access_token) if rep else ""),
        "credential_file": (os.path.basename(rep.path) if rep and rep.path else ""),
        "expires_at": (rep.expires_at if rep else 0),
        "accounts": (len(_gw().POOL.accounts) if _gw().POOL else 0),
        "accounts_ready": (_gw().POOL.count_ready() if _gw().POOL else 0),
    }
    return snap

def _tail_lines(path, max_lines, chunk=256 * 1024):
    """返回最后 max_lines 条非空行，最旧的在前。

    用量日志一天就到 20MB，每次轮询从头扫是 /usage/* 变慢的主因，所以按 chunk
    从文件尾往前读，每块只 split 一次，buf 只带跨块的那半行。

    纯空白的行不算行，也不计入 max_lines；末尾没有换行但内容非空的残余算一行
    （record_usage 总会写换行，这种情形是写了一半）。整块的非空行用列表推导
    一次 extend 收进来，字节口径与逐行读取相同。
    """
    lines = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(lines) < max_lines:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                got = [raw for raw in parts[1:] if raw and not raw.isspace()]
                if got:
                    lines.extend(reversed(got))
                    if len(lines) > max_lines:
                        # 与逐行写法一样：收满 max_lines 就停，剩下的（更老的）
                        # 行属于更早的块，本来也不会被收进来。
                        del lines[max_lines:]
                        break
            if len(lines) < max_lines and buf.strip():
                lines.append(buf)
    except FileNotFoundError:
        return []
    except Exception as exc:
        _gw().log("tail read failed: %s" % exc)
        return []
    lines.reverse()
    return lines

_count_cache = {}
_count_lock = threading.Lock()
# 计数只喂「N 条记录」标签。轮询间隔 5 秒，TTL 同样长会让几乎每次轮询都
# 落空；30 秒把每次轮询一次全量扫描变成六次轮询一次，而记录数只会增长，
# 标签足够新。
_COUNT_TTL = float(os.environ.get("WB_COUNT_TTL", 30))

# 增量计数的状态（offset/tail/n/n_realm），由 _count_state_lock 保护。
# 与 by_account 折叠同一套位置校验（_usage_log_key + _log_offset_holds +
# _log_tail_signature）：日志被截断、被替换或同尺寸重写时从零重数，宁多扫
# 一次也不能给出错的页数。TTL 缓存按 realm 分键，这份状态则与 realm 无关
# ——一次扫描同时维护总数与每个 realm 的计数，任何 realm 的查询都复用同一
# 条进度，不再各自全量扫一遍。
_count_state_lock = threading.Lock()
_count_state = {"offset": 0, "key": None, "tail": b"", "n": 0, "n_realm": {}}

_series_cache = {}
_series_lock = threading.Lock()

def _usage_timeseries_scope(realm, range, since, until, bucket_seconds):
    """（realm, lo, hi, step, 缓存键）—— usage_timeseries() 与它的 ETag 共用。

    lo/hi 是这次真正要扫的窗口（未固定的边界在这里补成"现在/往前 24 小时"），
    键只收**固定的**边界，理由见下面 wrapper 里的注释。
    """
    r = realm_scope(realm, _gw().CURRENT_REALM)
    lo, hi = range_window(range, since, until)
    # 调用方显式给出的边界，在下面的补值之前记下。只有显式给出的边界能以值的
    # 形式进缓存键："now"（以及往前 24 小时的默认 lo）每次调用重算，键里带着它，
    # 面板轮询同一张图期间键永远不重用，缓存等于白做。"auto" 哨兵把两类边界
    # 分开，滚动窗口绝不会取到固定窗口的 series。
    pinned_lo, pinned_hi = lo, hi
    if hi is None:
        hi = time.time()
    if lo is None:
        lo = hi - 86400
    span = max(1.0, hi - lo)
    if bucket_seconds:
        step = max(60, int(bucket_seconds))
    elif span <= 6 * 3600:
        step = 60
    elif span <= 14 * 86400:
        step = 3600
    else:
        step = 86400
    # 用上下界，不用 range 标志位：本周与本月窗口重叠，标志位会让一个窗口
    # 从缓存里取走另一个窗口的 series。桶宽加入缓存键同理：显式桶宽改变每个
    # 桶的宽度，自动桶宽随跨度翻转，两个不同的 step 是两份不同的载荷。
    key = (r or "all",
           pinned_lo if pinned_lo is not None else "auto",
           pinned_hi if pinned_hi is not None else "auto",
           step)
    return r, lo, hi, step, key

def usage_timeseries(realm=None, range=None, since=None, until=None,
                     bucket_seconds=None, ttl=None):
    """分析图表用的分桶 token/积分序列。

    桶宽随窗口自动缩放：分钟（<=6h）、小时（<=14d）、其余按天；显式的
    bucket_seconds 覆盖自动值。completed 请求贡献 token；所有非
    client_aborted 的行贡献积分（钱已经花了）。最近的积分请求随载荷一起带出，
    面板不用另开接口就能显示积分历史。

    缓存包装：指标页打开时 Token 图表轮询这个接口，一次未缓存的调用就要重读
    重解整份日志。与同级接口共用同一 TTL，重建持锁执行，一波轮询不会各自
    开扫。
    """
    ttl = _STATS_TTL if ttl is None else ttl
    r, lo, hi, step, key = _usage_timeseries_scope(realm, range, since, until,
                                                   bucket_seconds)
    now = time.time()
    with _series_lock:
        hit = _series_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
        data = _usage_timeseries_uncached(r, lo, hi, step)
        _series_cache[key] = (time.time(), data)
    return data

def usage_timeseries_etag(realm=None, range=None, since=None, until=None,
                          bucket_seconds=None):
    """usage_timeseries() 当前缓存条目的 ETag；没有条目时返回 None。

    配对调用由 _json_cached() 负责。注意键里只有固定的边界，所以"滚动窗口"这一类
    请求始终落在同一条目上：条目没重建，响应体（含它自己的 until / 桶边界）就逐
    字节不变，这正是可以放心回 304 的原因。
    """
    _r, _lo, _hi, _step, key = _usage_timeseries_scope(realm, range, since, until,
                                                       bucket_seconds)
    with _series_lock:
        hit = _series_cache.get(key)
    if hit is None:
        return None
    return _gw()._cache_etag("series", repr(key), hit[0])

# 本进程写下的每行都以 `{"at": <float>,` 开头——record_usage 与
# record_error 都把时间戳放第一个，json.dumps 保持这个顺序——下面的窗口
# 预过滤因此成立。
_AT_PREFIX = '{"at": '
# 同一前缀的字节形式：_line_outside_window_bytes() 读的是未解码的行，
# 字节由文本推导，两者因此保持同步。
_AT_PREFIX_B = _AT_PREFIX.encode("ascii")

def _line_outside_window(line, lo, hi):
    """这行日志的 at 可证明落在 [lo, hi] 之外时返回 True。

    任一边界为 None 表示该侧不设限：面板要的 today、this week 只设置起始边界，
    开放的一侧不能丢行。

    扫描用它免去窗口外行的 json.loads。返回 False 表示「照旧解析」，包括形状
    匹配但落在窗口内的行，以及任何读不准的行：

    * 键序不同或手改过的行：不以 `{"at": ` 开头，或值后没有逗号；
    * 整数记号：json.loads 会给出 int，而 int 与 float 的比较在超过 2**53 后
      不再精确，所以只接受能精确往返的浮点记号；
    * 后面还有第二个 `"at"` 键：json.loads 取最后一个，这里读到的会是错的。
      字符串里的转义引号造不出假的 `"at"`，这道检查只对本来就要跳过的行执行。
    """
    if not line.startswith(_AT_PREFIX):
        return False
    end = line.find(",", len(_AT_PREFIX))
    if end < 0:
        return False
    token = line[len(_AT_PREFIX):end]
    if "." not in token and "e" not in token and "E" not in token:
        return False
    try:
        at = float(token)
    except ValueError:
        return False
    if not ((lo is not None and at < lo) or (hi is not None and at > hi)):
        return False
    return line.find('"at"', end) < 0

def _line_outside_window_bytes(raw, lo, hi):
    """在未解码的行上执行同一判断，只在文本版也会返回 True 时才返回 True。

    扫描先按字节读再解码，同一判断因此可以在解码前跑，被丢掉的行不会变成文本，
    窄窗口下的多日日志因此省掉大部分解码。规则与文本版一致，只是按字节读：
    同样的前缀、第一个逗号、浮点记号要求、窗口比较，以及后面没有第二个 at 键。

    多一条要求：整行必须是 ASCII。它保证字节读到的内容与文本版看到的一致，也
    避免把无法解码的行放进跳过路径——那种行在文本路径上会以 UnicodeDecodeError
    中断扫描，跳过它就会折进旧路径从未到达的行。

    False 只表示「照旧解码并检查」，不表示这行在窗口内，边界必须与传给
    _line_outside_window() 的相同。
    """
    if not raw.startswith(_AT_PREFIX_B):
        return False
    end = raw.find(b",", len(_AT_PREFIX_B))
    if end < 0:
        return False
    token = raw[len(_AT_PREFIX_B):end]
    # 46、101、69 分别是 `.`、`e`、`E` 的字节值。用整数针是因为在 bytes 上
    # 整数针走单字节扫描，而 b"." in token 走通用子串搜索，短记号上贵几倍。
    if 46 not in token and 101 not in token and 69 not in token:
        return False
    try:
        at = float(token)
    except ValueError:
        return False
    if not ((lo is not None and at < lo) or (hi is not None and at > hi)):
        return False
    if raw.find(b'"at"', end) >= 0:
        return False
    return raw.isascii()

def _usage_timeseries_scan(realm, lo, hi, step):
    """对日志做一次无缓存全量扫描，把行折进等宽时间桶。

    这是窗口序列的兜底路径：日/小时桶切片不成立（见 _series_slice）时用它，
    口径一字未改。
    """
    buckets = {}
    # 窗口内 credit 前 50 条：大小 50 的最小堆，堆顶是当前的第 50 名。堆把
    # 每行压到一次「比不比堆顶大」的比较，进不了前 50 的行连载荷字典都不建。
    # 堆元素是（at, -序号, 载荷）：序号取负，让相同 at 时日志靠前的行更大，与
    # 稳定排序（at 倒序、同 at 保持日志顺序）以及 _series_credit_push 的缓冲
    # 逐条一致（键唯一，堆的替换规则与稳定排序的取舍等价）。
    credits = []
    credit_seq = 0
    try:
        with open(_gw().USAGE_LOG, "rb") as fh:
            for raw in fh:
                # 解码前先跑字节级窗口预过滤：多日日志的窄窗口只覆盖一小片，
                # 被它丢掉的行不进解码也不进解析（它只对文本判断也会丢的行
                # 返回真，见 _line_outside_window_bytes）。余下的行照旧走文本
                # 判断与全量解析，末尾半行与坏 UTF-8 的处理都与文本迭代一致。
                if _line_outside_window_bytes(raw, lo, hi):
                    continue
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                if _line_outside_window(line, lo, hi):
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if realm and not row_matches_realm(row, realm):
                    continue
                at = row.get("at") or 0
                if at < lo or at > hi:
                    continue
                key = int((at - lo) // step)
                bucket = buckets.setdefault(key, {
                    "at": lo + key * step, "requests": 0, "errors": 0,
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "reasoning_tokens": 0, "cached_tokens": 0,
                    "total_tokens": 0, "credit": 0.0,
                })
                outcome = row_outcome(row)
                if outcome == "completed":
                    bucket["requests"] += 1
                    for field in ("prompt_tokens", "completion_tokens",
                                  "reasoning_tokens", "cached_tokens",
                                  "total_tokens"):
                        bucket[field] += (row.get(field) or 0)
                else:
                    bucket["errors"] += 1
                credit = row.get("credit") or 0
                if outcome != "client_aborted":
                    bucket["credit"] += credit
                if credit > 0:
                    seq = -credit_seq
                    credit_seq += 1
                    if len(credits) < _SERIES_CREDITS_KEEP:
                        heapq.heappush(credits,
                                       (at, seq, _series_credit_entry(at, row)))
                    elif (at, seq) > (credits[0][0], credits[0][1]):
                        heapq.heapreplace(credits,
                                          (at, seq, _series_credit_entry(at, row)))
    except FileNotFoundError:
        pass
    except Exception as exc:
        _gw().log("usage timeseries read failed: %s" % exc)
    # 堆里就是窗口内的前 50 条；按同一把键排出（键唯一，与稳定排序同一
    # 结果：at 倒序、同 at 保持日志顺序）。
    credits.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return {
        "ok": True, "realm": realm or "all", "bucket_seconds": step,
        "since": lo, "until": hi,
        "series": [buckets[key] for key in sorted(buckets)],
        "credits": [item[2] for item in credits],
    }

def _usage_timeseries_uncached(realm, lo, hi, step):
    """窗口序列：优先由日/小时桶/分钟环切片，不成立时整段扫描。

    切片只在「桶宽与窗口一致」时才是同一件事，所以先按桶宽判断要不要惊动
    状态：日/小时桶要求窗口起点是本地午夜，分钟环要求起点落在分钟格点上
    （lo 是 60 的整数倍）。两条都不成立的自定义区间根本不折状态，也就不会
    为它们白扫一遍。
    """
    if step in (3600, 86400):
        day_key = _window_day_key(lo)
    elif step == 60:
        # 分钟窗口：起点落在格点上才可能与存量分钟桶一一对应（见
        # _series_slice_minutes）；不在格点上的窗口连状态都不惊动。
        day_key = _series_window_day_key(lo)
        if day_key is not None and not _series_minute_grid_ok(lo):
            day_key = None
    else:
        day_key = None
    if day_key is not None and _usage_day_buckets_enabled():
        with _series_state_lock:
            state, offset, error = _usage_series_state_ready(realm)
            out = None
            if error is not None:
                # 折叠失败：状态归零（下一次从零折起），这次退回整段扫描。
                _gw().log("usage timeseries fold failed: %s" % error)
                state.update({"days": {}, "days_floor": None, "max_at": 0,
                              "credits": [], "minutes": {}, "minutes_from": None,
                              "hits": [], "hits_overflow": False,
                              "offset": 0, "tail": b""})
            else:
                out = _series_slice(state, realm, lo, hi, step, day_key)
        if error is None:
            _usage_cache_maybe_save("series", offset)
        if out is not None:
            return out
    return _usage_timeseries_scan(realm, lo, hi, step)

def count_usage_rows(realm=None):
    """缓存的行数：增量折叠 + 子串匹配，不做逐行全量解析。

    没有 `realm` 字段的老行（都是错误行）要走 row_realm 的账号/模型归属回退，
    这几行照常解析一次，发生在折叠第一次读到它们的时候。

    每次 /usage/recent 轮询都要它渲染页数标签；全量扫文件占掉该端点约一半
    开销（45 MB 日志实测），所以折叠状态跨调用保留，每次只并入上次之后新增
    的行——一次轮询的代价等于新增流量。短 TTL 仍保证数字不过时，并把这点
    代价也从轮询路径上拿掉。
    """
    # 先归一化：桶键就是 realm 字符串本身，字面 "all" 会被当成一个永远不存在
    # 的桶（realm_scope 把它映射成 None = 总数）。
    realm = realm_scope(realm)
    r = realm or ""
    now = time.time()
    with _count_lock:
        hit = _count_cache.get(r)
        if hit is not None and (now - hit[0]) < _COUNT_TTL:
            return hit[1]
    n = _count_usage_rows_uncached(realm)
    with _count_lock:
        _count_cache[r] = (time.time(), n)
    return n

# `"realm"` 的键文本。字段值按两种间距取（见 _count_usage_line），一次扫描
# 同时数完所有 realm。
_REALM_KEY = '"realm"'
_REALM_KEY_LEN = len(_REALM_KEY)

def _count_usage_line(state, line):
    """折一行：总数 + 这一行声明的 realm 桶。

    总数数的是**所有非空行**（包括解析不了的行）。带 realm 字段的行按字段值
    归桶，取值只认两种间距（`"realm": "x"` 与 `"realm":"x"`）：值读到下一个
    引号为止——查询用的 realm 不会带引号，字符串里的转义引号也拼不出
    `"realm"` 这个键文本（同 _line_outside_window 的说明），所以不会误判。
    同一行重复声明 `"realm"`（手改过的行）按每一处声明的值各计一次。只有
    完全没有该字段的老行（都是错误行）才解析 JSON 走 row_realm() 的账号/
    模型回退，解析费用在折叠时只付一次。
    """
    state["n"] += 1
    counts = state["n_realm"]
    pos = line.find(_REALM_KEY)
    if pos < 0:
        # 没有 realm 字段（老错误行）：解析回退。解析不了就只算进总数。
        try:
            realm = row_realm(json.loads(line))
        except Exception:
            return
        counts[realm] = counts.get(realm, 0) + 1
        return
    while pos >= 0:
        end = pos + _REALM_KEY_LEN
        if line.startswith(': "', end):
            start = end + 3
        elif line.startswith(':"', end):
            start = end + 2
        else:
            start = -1
        if start >= 0:
            quote = line.find('"', start)
            if quote >= 0:
                value = line[start:quote]
                counts[value] = counts.get(value, 0) + 1
        pos = line.find(_REALM_KEY, end)

def _count_usage_scan(state):
    """把日志 offset 之后的每一行折进 state；返回 (offset, error)。

    与 _scan_usage_from 同一套字节纪律（整行推进、半行留给下一趟、解码失败
    即停），但折叠的是**行文本**：计数的总口径里有一类行根本不是合法 JSON
    （总行数照数），_scan_usage_from 在 fold 之前就把它们丢掉了。逐行调用在
    这里直接写死（不走回调），全量重数一次要过几万行，每行省一次间接调用是
    这条冷路径上最便宜的一笔。
    """
    offset = state["offset"]
    try:
        with open(_gw().USAGE_LOG, "rb") as fh:
            fh.seek(offset)
            while True:
                raw = fh.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break
                end = offset + len(raw)
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    return end, exc
                if line:
                    try:
                        _count_usage_line(state, line)
                    except Exception as exc:
                        return end, exc
                offset = end
    except FileNotFoundError:
        pass
    except Exception as exc:
        return offset, exc
    return offset, None

def _count_usage_rows_uncached(realm=None):
    """增量计数：把折叠推进到文件尾，返回 realm 对应的行数。

    状态按「本进程数到哪」记账（offset/tail/n/n_realm），每次调用只数新增
    的字节；位置校验不过（截断、换文件、同尺寸重写）才从零重数。realm 为
    空返回总行数，否则返回该 realm 桶里的行数。
    """
    state = _count_state
    with _count_state_lock:
        log_key = _usage_log_key()
        if not _log_resume_ok(state, log_key):
            state.update({"offset": 0, "tail": b"", "n": 0, "n_realm": {}})
        state["key"] = log_key
        offset, error = _count_usage_scan(state)
        n = state["n"]
        counts = state["n_realm"]
        if error is None:
            state["offset"] = offset
            state["tail"] = _log_tail_signature(offset)
        else:
            # 出错的那一行（非法 UTF-8 才会走到这里）可能已经半折进去了：
            # 这份状态整个作废，下次从零重数——by_account 折叠同款处理。
            # 这一次仍返回已经折到的部分，即坏行之前的行数。
            _gw().log("usage count failed: %s" % error)
            state.update({"offset": 0, "tail": b"", "n": 0, "n_realm": {}})
        if not realm:
            return n
        return counts.get(realm, 0)





























def usage_filters(realm=None, key=None, status=None, model=None, account=None,
                  ip=None, range=None, since=None, until=None):
    """/usage/recent 的查询字段归一化成过滤字典。"""
    out = {}
    realm = realm_scope(realm)
    if realm:
        out["realm"] = realm
    for name, value in (("key", key), ("model", model), ("account", account),
                        ("ip", ip)):
        text = str(value or "").strip()
        if text:
            out[name] = text
    status = str(status or "").strip().lower()
    if status in ("ok", "fail"):
        out["status"] = status
    lo, hi = range_window(range, since, until)
    if lo is not None:
        out["since"] = lo
    if hi is not None:
        out["until"] = hi
    return out




def realm_state_file():
    """持久化 realm 开关的路径。

    每次访问时求值，不缓存成模块常量：常量在导入时按默认 _gw().ACCOUNTS_DIR
    构造，后给的 --accounts-dir（或指向别处的 Docker 卷）会让读写仍落在脚本旁
    的 realm 开关上——面板报出的出口与配置的账号库不符。
    """
    return os.path.join(_gw().ACCOUNTS_DIR, "active_realm.json")

def load_persisted_realm():
    path = realm_state_file()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
                r = d.get("realm")
                if r in ("intl", "cn"):
                    _gw().CURRENT_REALM = r
                    return _gw().CURRENT_REALM
        except Exception as e:
            _gw().log("could not load active realm: %s" % e)
    return _gw().CURRENT_REALM
def save_persisted_realm(realm):
    if realm in ("intl", "cn"):
        _gw().CURRENT_REALM = realm
        try:
            _ensure_dir(_gw().ACCOUNTS_DIR)
            with _open_write(realm_state_file()) as fh:
                json.dump({"realm": realm, "updated_at": time.time(), "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S")}, fh, indent=2)
            _gw().log("persisted active realm '%s' to disk" % realm)
        except Exception as exc:
            _gw().log("failed to persist active realm: %s" % exc)
    return _gw().CURRENT_REALM

# --------------------------------------------------------- 积分获取历史（发放记录）
# 上游对每个账号返回一份积分包清单（免费套餐、每日活跃奖励的 Bonus Pack、活动包…），
# 每个包带面额、发放时间与到期时间。看板的「积分获取历史」就是这份清单的合并视图：
# 只读内存里的账号积分快照，**不发任何上游请求**（要更新的数字先点「一键刷新积分」），
# 所以它同时给出快照时刻，让面板标明数据有多旧。
GRANT_STATUS_ACTIVE = "active"        # 上游标了 in_usage：当前正从它扣减
GRANT_STATUS_AVAILABLE = "available"  # 有剩余、未过期，只是当前不参与扣减
GRANT_STATUS_USED_UP = "used_up"
GRANT_STATUS_EXPIRED = "expired"

def _parse_stamp_epoch(text):
    """'2026-10-10 00:42:45' -> epoch；认不出来返回 None。"""
    if not text:
        return None
    try:
        return time.mktime(time.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None

def _grant_status(pkg, now):
    """一个积分包现在的状态；判定顺序是「过期 > 用完 > 在扣减 > 可用」。"""
    try:
        remain = float(pkg.get("remain") or 0)
    except (TypeError, ValueError):
        remain = 0.0
    expired = bool(pkg.get("is_expired"))
    expire_at = _parse_stamp_epoch(pkg.get("expire_time"))
    if expire_at is not None and expire_at <= now:
        expired = True
    if expired:
        return GRANT_STATUS_EXPIRED
    if remain <= 0:
        return GRANT_STATUS_USED_UP
    return GRANT_STATUS_ACTIVE if pkg.get("in_usage") else GRANT_STATUS_AVAILABLE

# 发放记录与本机动作的关联窗口：动作在发放前 2 小时内算「这次动作带来的」，
# 上游记的发放时间可能比我们那条动作记录早几秒，所以向后也留一点余量。
GRANT_ACTION_BEFORE = 2 * 3600
GRANT_ACTION_AFTER = 600

def _grant_actions():
    """uid -> [{at, ts, task, ok}]（按时间升序），只含签到与每日活跃的真实尝试。

    历史文件是磁盘上的东西：读取一律先过 wb_activity.project()，多出来的键
    （别的写入方、手工编辑、被塞进来的凭证）不会跟着载荷出去；任何读取失败都
    退化成「没有关联动作」，绝不让一次查询挂掉。
    """
    index = {}
    try:
        rows = wb_activity.load()
    except Exception:
        return index
    for raw in rows or []:
        row = wb_activity.project(raw)
        task = row["task"]
        if task not in (wb_activity.TASK_CHECKIN, wb_activity.TASK_DAILY_CHAT):
            continue
        at = wb_activity._parse_ts(row["ts"])
        if at is None:
            continue
        index.setdefault(row["uid"], []).append(
            {"at": at, "ts": row["ts"], "task": task, "ok": bool(row["ok"])})
    for items in index.values():
        items.sort(key=lambda item: item["at"])
    return index

def _grant_action(index, uid, at):
    """发放时刻之前最近的一次本机签到 / 每日活跃尝试；窗口外没有就 None。"""
    if at is None:
        return None
    for item in reversed(index.get(uid) or ()):
        if item["at"] > at + GRANT_ACTION_AFTER:
            continue
        if item["at"] < at - GRANT_ACTION_BEFORE:
            break
        return {"task": item["task"], "ts": item["ts"], "ok": item["ok"],
                "delta_seconds": int(round(at - item["at"]))}
    return None

def credit_grants(now=None):
    """每个积分包一行（新的在前）＋汇总；只读快照，不发上游请求。"""
    now = time.time() if now is None else now
    actions = _grant_actions()
    rows = []
    fetched = 0.0

    def _num(pkg, key):
        try:
            return float(pkg.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    for account in (_gw().POOL.accounts if _gw().POOL else []):
        credits = getattr(account, "credits", None) or {}
        try:
            fetched = max(fetched, float(credits.get("updated_at") or 0))
        except (TypeError, ValueError):
            pass
        for pkg in credits.get("packages") or []:
            if not isinstance(pkg, dict):
                continue
            expire_iso = pkg.get("expire_time") or pkg.get("cycle_end_time") or ""
            create_at = _parse_stamp_epoch(pkg.get("create_time"))
            rows.append({
                "uid": account.uid,
                "nickname": account.nickname or (account.uid or "")[:8],
                "realm": account.realm,
                "name": str(pkg.get("name") or pkg.get("package_code") or "Package"),
                "product": str(pkg.get("sub_product_name") or pkg.get("product_name") or ""),
                "grant_reason": str(pkg.get("grant_reason") or ""),
                "size": _num(pkg, "size"),
                "remain": _num(pkg, "remain"),
                "used": _num(pkg, "used"),
                "unit": str(pkg.get("unit") or "credit"),
                "create_at": create_at,
                "create_iso": str(pkg.get("create_time") or ""),
                "expire_at": _parse_stamp_epoch(expire_iso),
                "expire_iso": str(expire_iso),
                "no_expiry": bool(pkg.get("no_expiry")),
                "days_left": pkg.get("days_left"),
                "status": _grant_status(pkg, now),
                "action": _grant_action(actions, account.uid, create_at),
            })
    # 同一秒按账号、包名稳定排序，免得每次刷新顺序都在跳。
    rows.sort(key=lambda r: (-(r["create_at"] or 0), r["nickname"], r["name"]))
    return {
        "ok": True,
        "generated_at": now,
        "generated_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "fetched_at": fetched or None,
        "fetched_iso": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fetched))
                        if fetched else None),
        "rows": rows,
        "summary": {
            "count": len(rows),
            "size": round(sum(r["size"] for r in rows), 2),
            "remain": round(sum(r["remain"] for r in rows), 2),
            "used": round(sum(r["used"] for r in rows), 2),
            "accounts": len(set(r["uid"] for r in rows)),
        },
    }

_byacct_cache = {"at": 0.0, "data": None}
_byacct_lock = threading.Lock()

def usage_by_account(ttl=None):
    """缓存包装：对整份日志做全量聚合很贵。

    持锁重建，理由同 usage_snapshot。"""
    ttl = _STATS_TTL if ttl is None else ttl
    now = time.time()
    with _byacct_lock:
        if _byacct_cache["data"] is not None and (now - _byacct_cache["at"]) < ttl:
            return _byacct_cache["data"]
        data = _usage_by_account_uncached()
        _byacct_cache["at"] = time.time()
        _byacct_cache["data"] = data
    return data

def usage_by_account_etag():
    """usage_by_account() 当前缓存条目的 ETag；没有条目时返回 None。

    这个视图只有一条缓存（没有窗口/区域参数），所以键是常量。配对调用由
    _json_cached() 负责。
    """
    with _byacct_lock:
        if _byacct_cache["data"] is None:
            return None
        built_at = _byacct_cache["at"]
    return _gw()._cache_etag("by-account", "all", built_at)

_byacct_state = {"buckets": None, "offset": 0, "key": None, "tail": b""}
# 上面的包装持着 _byacct_lock 重建后再调进来，而 _usage_by_account_uncached()
# 也被 bench 与测试直接调用；它自己的锁防止刷新重入包装的锁——那个锁不可
# 重入，共用它会在第一次缓存未命中时挂死进程。
_byacct_state_lock = threading.Lock()

def _fold_usage_by_account(row, buckets):
    """把一行折进按账号的桶。

    这里没有窗口、没有区域过滤——这个视图描述的一直是整份日志——折叠也只跳过
    原始聚合跳过的行。
    """
    if row.get("error"):
        return
    key = row.get("account") or "(unattributed)"
    bucket = buckets.get(key)
    if bucket is None:
        bucket = buckets[key] = {
            "account": key, "requests": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "reasoning_tokens": 0,
            "cached_tokens": 0, "total_tokens": 0, "models": {},
        }
    bucket["requests"] += 1
    bucket["prompt_tokens"] += row.get("prompt_tokens") or 0
    bucket["completion_tokens"] += row.get("completion_tokens") or 0
    bucket["reasoning_tokens"] += row.get("reasoning_tokens") or 0
    bucket["cached_tokens"] += row.get("cached_tokens") or 0
    bucket["total_tokens"] += row.get("total_tokens") or 0
    model = row.get("model") or "?"
    models = bucket["models"]
    models[model] = models.get(model, 0) + 1

def _usage_by_account_buckets():
    """刷新后拷给调用方的按账号折叠缓存。

    拷贝是必须的：_usage_by_account_uncached() 会把每个桶的 `models` 字典换成
    排序列表，下次刷新必须还能在原处找到计数字典。
    """
    with _byacct_state_lock:
        log_key = _usage_log_key()
        if _byacct_state["buckets"] is None:
            # 全新状态（进程刚起来）：先问 checkpoint，问不到才从零折起。
            if not _usage_cache_adopt_by_account(_byacct_state, log_key):
                _byacct_state.update({"buckets": {}, "offset": 0, "tail": b""})
        elif not _log_resume_ok(_byacct_state, log_key):
            _byacct_state.update({"buckets": {}, "offset": 0, "tail": b""})
        _byacct_state["key"] = log_key
        offset, error = _scan_usage_from(
            _byacct_state["offset"],
            lambda row: _fold_usage_by_account(row, _byacct_state["buckets"]))
        buckets = _byacct_state["buckets"]
        if error is None:
            _byacct_state["offset"] = offset
            _byacct_state["tail"] = _log_tail_signature(offset)
        else:
            # 让折叠停下的那行可能已经把自己折了一半，这个视图下次调用从零
            # 开始——它替代的全量扫描每次调用都是这么做的。
            _gw().log("usage_by_account failed: %s" % error)
            _byacct_state.update({"buckets": {}, "offset": 0, "tail": b""})
        out = {k: dict(v) for k, v in buckets.items()}
    if error is None:
        _usage_cache_maybe_save("by_account", offset)
    return out

def _usage_by_account_uncached():
    """按账号 id 聚合 JSONL 日志。

    增量：折叠结转，用上次调用之后追加的行刷新，代价按新增流量算，
    不必付整份日志的钱。
    """
    buckets = _usage_by_account_buckets()
    out = sorted(buckets.values(), key=lambda b: -b["total_tokens"])
    for item in out:
        item["models"] = sorted(item["models"].items(), key=lambda kv: -kv[1])[:5]
    return out
_analytics_cache = {}
_analytics_lock = threading.Lock()

def _analytics_scope(realm, range, since, until):
    """（since, until, 缓存键）—— compute_usage_analytics() 与它的 ETag 共用。

    注意键里用的是**原始** realm（`realm or "all"`），与这个接口一直以来的键
    一致；折叠时用 realm_scope(realm) 另有其义。两者都不能顺手"统一"，否则就是
    换了一条缓存键。
    """
    lo, hi = range_window(range, since, until)
    cache_key = "%s|%s|%s" % (realm or "all",
                              lo if lo is not None else "", hi if hi is not None else "")
    return lo, hi, cache_key

def compute_usage_analytics(ttl=None, realm=None, range=None, since=None, until=None):
    """缓存的分析载荷。

    指标页每 5 秒轮询一次，所以与 perf_stats/usage_snapshot/usage_by_account
    共用 TTL 缓存，重建持锁执行，并行轮询不会各自扫日志。

    窗口进缓存键与其他读取方同理：载荷的窗口半是 KPI 卡片打印的数字，本周与
    本月窗口重叠，一条缓存不能两个都伺候。
    """
    ttl = _STATS_TTL if ttl is None else ttl
    now = time.time()
    lo, hi, cache_key = _analytics_scope(realm, range, since, until)
    with _analytics_lock:
        entry = _analytics_cache.get(cache_key)
        if entry is not None and (now - entry["at"]) < ttl:
            return entry["data"]
        data = _compute_usage_analytics_uncached(realm=realm_scope(realm), since=lo, until=hi)
        _analytics_cache[cache_key] = {"at": time.time(), "data": data}
    return data

def usage_analytics_etag(realm=None, range=None, since=None, until=None):
    """compute_usage_analytics() 当前缓存条目的 ETag；无条目返回 None。

    配对调用由 _json_cached() 负责，理由见那里。
    """
    _lo, _hi, cache_key = _analytics_scope(realm, range, since, until)
    with _analytics_lock:
        entry = _analytics_cache.get(cache_key)
    if entry is None:
        return None
    return _gw()._cache_etag("analytics", cache_key, entry["at"])

def _new_analytics_stat():
        return {
            "requests": 0, "errors": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
            "cached_tokens": 0, "total_tokens": 0,
            "credit": 0.0,
            "cost_cny": 0.0,
            "ttft_sum": 0.0, "ttft_n": 0,
            "speed_sum": 0.0, "speed_n": 0,
            "elapsed_sum": 0.0, "elapsed_n": 0,
        }

# 没有真实 key id 可归时，按 key 的表把流量折进这些桶。
# `before` 与 `anon` 靠行里有没有 `key` 字段区分：本功能之前写下的行没有这个
# 字段，它们的数量只会减少；没配 key 的部署则不断写入空 key 的行。两者要的
# 响应不同，永不合并。
KEY_BUCKET_BEFORE = "__before_keys__"
KEY_BUCKET_ANON = "__no_key__"
KEY_BUCKET_UNKNOWN = "__unknown_key__"
# 按 key 的模型明细封顶：50 个 key 的部署会向每 5 秒重绘的页面发几千个
# 标签。账号表可以列出每个模型：它每个上游账号一行，不按调用方计。
KEY_MODEL_TOP_N = 5

# 分析载荷的一半折进四张表：窗口汇总、按账号、按模型、按 key。all-time 半与
# 窗口半持有同样的表，只差各自拥有的 stat 字段，所以下面的行折叠只写一份，
# 由参数告诉它喂哪一半。
_ANALYTICS_ALL = ("all_time", "all_models")
_ANALYTICS_WINDOW = ("window", "window_models")

def _new_analytics_maps():
    """载荷一半所折入的四张表。"""
    return {"summary": _new_analytics_stat(), "accts": {}, "models": {}, "keys": {}}

def _analytics_row_values(row):
    """下面的折叠每行需要的数字，每行只读一次。

    在这里读一次，免去每行八个 stat 对象各自遍历同样的几个字段——那部分
    主导一次冷的 analytics 扫描。
    """
    return (row.get("prompt_tokens") or 0,
            row.get("completion_tokens") or 0,
            row.get("reasoning_tokens") or 0,
            row.get("cached_tokens") or 0,
            row.get("total_tokens") or 0,
            row.get("credit") or 0,
            row.get("ttft_ms"),
            row.get("tokens_per_sec"),
            row.get("elapsed_ms"))

def _feed_analytics(stat_obj, vals, is_err, cost_cny):
    """把一行的数字折进一个 analytics stat 对象。

    token 合计按实际消耗计：上游计费后才失败的请求仍然显示它的 token；只有
    请求/错误计数依赖终态。未计价模型传 cost_cny = 0.0，等于没加：累加器在
    _new_analytics_stat() 里以 float 起步，不需要 `cost["known"]` 守卫。
    """
    if is_err:
        stat_obj["errors"] += 1
    else:
        stat_obj["requests"] += 1
    stat_obj["prompt_tokens"] += vals[0]
    stat_obj["completion_tokens"] += vals[1]
    stat_obj["reasoning_tokens"] += vals[2]
    stat_obj["cached_tokens"] += vals[3]
    stat_obj["total_tokens"] += vals[4]
    stat_obj["credit"] += vals[5]
    stat_obj["cost_cny"] += cost_cny
    if vals[6]:
        stat_obj["ttft_sum"] += vals[6]
        stat_obj["ttft_n"] += 1
    if vals[7]:
        stat_obj["speed_sum"] += vals[7]
        stat_obj["speed_n"] += 1
    if vals[8]:
        stat_obj["elapsed_sum"] += vals[8]
        stat_obj["elapsed_n"] += 1

def _bump_analytics_models(tgt, m_id, vals, is_err, cost_cny):
    """把一行折进一个 {model: {...}} 桶。

    模型分布只数成功请求：失败的调用算到模型头上，会在调用方什么都没拿到时
    显示成对它的需求。
    """
    if is_err:
        return
    tm = tgt.get(m_id)
    if tm is None:
        tm = tgt[m_id] = {"requests": 0, "tokens": 0, "reasoning": 0, "cost_cny": 0.0}
    tm["requests"] += 1
    tm["tokens"] += vals[4]
    tm["reasoning"] += vals[2]
    tm["cost_cny"] += cost_cny

def _fold_usage_analytics(row, realm, maps, half, pricing_on=None, also=None,
                          pricing=None, priced=None):
    """把一行解析结果折进分析载荷的一半。

    `maps` 持有那一半所折入的四张表，`half` 指明那一半每个条目拥有的 stat
    字段，所以增量的 all-time 折叠（每个条目的 all_time stat）与窗口轮（同样
    条目的 window stat）共用同一份「一行贡献什么」的定义。

    key 维度与账号维度刻意分开：一个 key 可以由多个上游账号服务，一个账号也
    可以服务多个 key，两张表是同一笔花费的两个视图，并不是把花费拆开。

    `also` 是同一行还要折进的那几份 (maps, half) 的序列（按日分桶与按小时
    分桶的窗口半）。过滤、算价与字段提取只做一次，各份各自累加：折叠本来
    就要求每侧各记一笔，而 cost_for_row 与 realm 判定是每行最贵的那部分，
    只该付一次。

    `pricing` 是本次扫描开头取到的 _pricing_inputs() 元组（见
    _fold_usage_snapshot 的同名参数）。

    `priced` 是本次扫描要填的「用到的价」记录（见「用到的价」状态指纹一节），
    只有推进状态的那条折叠传它；窗口半/冷扫描传 None。
    """
    if realm and not row_matches_realm(row, realm):
        return
    # 只有真正的网关/上游故障才算错误。客户端取消不算：它的 token 数不完整，
    # 折进本页报告的比率会低估缓存命中与速度。它在 perf_stats 里计数。
    outcome = row_outcome(row)
    if outcome == "client_aborted":
        return
    is_err = outcome != "completed"
    cost = _row_cost(row, pricing, pricing_on)
    # 折进去的每一行都把「用到的价」记进状态指纹（中止行在上面整行跳过）。
    _priced_note(priced, row, cost, pricing)
    cost_cny = cost["cny"] if cost["known"] else 0.0
    vals = _analytics_row_values(row)
    acct_uid = row.get("account") or "(unattributed)"
    m_id = row.get("model") or "(unknown)"
    # 本功能之前写下的行根本没有 `key` 字段；没配过 key 的部署写下的行有
    # 这个字段，但它是空的。
    if "key" in row or "key_id" in row:
        k_id = _key_usage_row_id(row) or KEY_BUCKET_ANON
    else:
        k_id = KEY_BUCKET_BEFORE
    k_realm = row_realm(row) or ""
    at = row.get("at") or 0
    targets = [(maps, half)]
    if also is not None:
        targets.extend(also)
    for tgt, (stat_key, models_key) in targets:
        _feed_analytics(tgt["summary"], vals, is_err, cost_cny)

        entry = tgt["accts"].get(acct_uid)
        if entry is None:
            entry = tgt["accts"][acct_uid] = {
                "uid": acct_uid,
                "nickname": acct_uid,
                "realm": row.get("realm", ""),
                "domain": "",
                stat_key: _new_analytics_stat(),
                models_key: {},
            }
        _feed_analytics(entry[stat_key], vals, is_err, cost_cny)
        _bump_analytics_models(entry[models_key], m_id, vals, is_err, cost_cny)

        model = tgt["models"].get(m_id)
        if model is None:
            model = tgt["models"][m_id] = {"model": m_id,
                                           stat_key: _new_analytics_stat()}
        _feed_analytics(model[stat_key], vals, is_err, cost_cny)

        km = tgt["keys"].get(k_id)
        if km is None:
            km = tgt["keys"][k_id] = {
                "key": k_id,
                stat_key: _new_analytics_stat(),
                models_key: {},
                # realm -> 行数。绑定单一出口的 key 只见到那个出口；没有绑定
                # 的 key 跟随模型，它的积分列会把两种产品加在一起。存成
                # dict 因为最终进 JSON。
                "realms": {},
                "last_at": 0,
            }
        km["realms"][k_realm] = km["realms"].get(k_realm, 0) + 1
        if at and at > km["last_at"]:
            km["last_at"] = at
        _feed_analytics(km[stat_key], vals, is_err, cost_cny)
        _bump_analytics_models(km[models_key], m_id, vals, is_err, cost_cny)

_analytics_state = {}
_analytics_state_lock = threading.Lock()

def _analytics_all_time(realm, pricing_on=None):
    """一个区域过滤的缓存 all-time 折叠；返回 (maps, offset)。

    每个区域一份状态：过滤决定折叠能看到哪些行，两个区域是两份折叠，谁也
    不能继承对方的行。只有 all-time 半被缓存——窗口半随钟表移动——offset
    随之返回，窗口轮可以在折叠停下的地方停下。
    """
    with _analytics_state_lock:
        state, offset, error = _analytics_state_ready(realm, pricing_on)
        if error is None:
            # 持锁拷贝，与 snapshot 状态同一条规则：调用方装饰的东西
            # 不能是活的折叠。
            maps = _copy_analytics_all_time(state["maps"])
        else:
            # 与 snapshot 状态同一条规则：让折叠停下的那行可能已经把自己
            # 折了一半，状态下次调用从零开始。offset 仍回到那一行的末尾——
            # 窗口半必须看到它、停在它那里，才能描述与 all-time 半已折的
            # 同一批字节。
            _gw().log("compute_usage_analytics failed: %s" % error)
            maps = state["maps"]
            state.update({"maps": _new_analytics_maps(), "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None, "max_at": 0})
    if error is None:
        _usage_cache_maybe_save("analytics", offset)
    return maps, offset

def _analytics_state_advance(realm, pricing_on=None):
    """（不折叠）取/建状态、采纳 checkpoint、判废归零、记指纹。

    调用方必须已持有 _analytics_state_lock。返回本次扫描取到的价表代次
    （与记进状态指纹的是同一份）。状态是否「冷」（这次要付一次全量重折）看
    state["offset"]：> 0 表示有可续折的数据，0 表示折叠要从文件头开始——
    状态刚建、刚被价表 / realm / 日志判废、或 checkpoint 采纳失败。与
    snapshot 侧同一套分工：采纳/判废只有这一段定义，_analytics_state_ready
    与窗口路径的冷热判定（_analytics_state_cold）共用它，两边不会漂移。
    """
    state = _analytics_state.get(realm)
    if state is None:
        state = _analytics_state[realm] = {"maps": _new_analytics_maps(),
                                           "offset": 0, "key": None,
                                           "pricing": None, "realm": None,
                                           "tail": b"", "days": {},
                                           "days_floor": None, "max_at": 0,
                                           "hours_keep": True,
                                           "hits": [], "hits_overflow": False}
    pricing = _pricing_inputs(pricing_on)
    realm_inputs = _realm_inputs()
    log_key = _usage_log_key()
    fresh = _pricing_record_new() if (pricing is not None
                                      and pricing[0]) else None
    if state["key"] is None:
        # 全新状态（进程刚起来）：先问 checkpoint，问不到才从零折起。
        if not _usage_cache_adopt_analytics(realm, state, log_key, pricing, realm_inputs):
            state.update({"maps": _new_analytics_maps(), "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None, "max_at": 0,
                          "hits": [], "hits_overflow": False, "pricing": fresh})
    elif (not _pricing_used_unchanged(state["pricing"], pricing)
            or state["realm"] != realm_inputs
            or not _log_resume_ok(state, log_key)):
        state.update({"maps": _new_analytics_maps(), "offset": 0, "tail": b"",
                      "days": {}, "days_floor": None, "max_at": 0,
                      "hits": [], "hits_overflow": False, "pricing": fresh})
    state.update({"realm": realm_inputs, "key": log_key})
    return pricing

def _analytics_state_cold(realm, pricing_on=None):
    """窗口请求现在会不会付一次全量重折？True = 会（窗口改走冷扫描）。

    调用方必须已持有 _analytics_state_lock。语义与 _usage_snap_state_cold
    逐条相同：判定不折叠、不写结果，窗口请求不会把状态冻在冷态（offset 仍是
    0，下一个 all-time 半的请求照样从零折到文件尾），重折之后两边字节一致。
    """
    _analytics_state_advance(realm, pricing_on)
    return _analytics_state[realm]["offset"] <= 0

def _analytics_state_ready(realm, pricing_on=None):
    """把 `realm` 的 all-time 折叠推进到文件尾，返回 state、offset 与 error。

    调用方必须已持有 _analytics_state_lock。日桶、小时子表与 all-time 折叠
    共用一次扫描，见 _fold_usage_analytics 的 also 参数；窗口半与 all-time
    半因此描述同一段字节。

    `hours_keep` 标记这份状态要维护小时子表，其剪枝线比日桶短，见
    _prune_day_buckets。`max_at` 是折过的最大 at，含 realm 不匹配的行；
    小时窗口靠它判断窗口上界有没有切掉桶里的行。

    状态指纹记录折叠行真正用到的价，见「用到的价」状态指纹一节。折叠时
    逐行更新 state["pricing"]，扫描成功后刷时间线投影摘要，判废走
    _pricing_used_unchanged。采纳 checkpoint 与判废归零都在
    _analytics_state_advance 里，窗口路径的冷热判定也用那一段；这里只管
    折到文件尾。
    """
    pricing = _analytics_state_advance(realm, pricing_on)
    state = _analytics_state[realm]
    priced = state["pricing"]
    day_buckets = _usage_day_buckets_enabled()

    def fold(row):
        # 一行折三处：all-time 半、它所属那天的窗口半，以及那天里它所属
        # 那小时的窗口半；过滤与算价只做一次。
        at = row.get("at") or 0
        if at > state["max_at"]:
            state["max_at"] = at
        day = _usage_day_bucket(state, row, realm, _day_analytics_bucket,
                                day_buckets)
        hour = _analytics_hour_bucket(day, at)
        if day is None:
            also = None
        elif hour is None:
            also = ((day, _ANALYTICS_WINDOW),)
        else:
            also = ((day, _ANALYTICS_WINDOW), (hour, _ANALYTICS_WINDOW))
        _fold_usage_analytics(
            row, realm, state["maps"], _ANALYTICS_ALL, pricing_on, also=also,
            pricing=pricing, priced=priced)

    offset, error = _scan_usage_from(state["offset"], fold)
    if error is None:
        state["offset"] = offset
        state["tail"] = _log_tail_signature(offset)
        _pricing_record_close(priced, pricing)
    return state, offset, error

def _analytics_window_from_days(realm, day_key, until, pricing_on):
    """窗口半 maps 等于窗口内的日桶之和；日桶不可用时返回 None。

    先确保 all-time 折叠推进到文件尾：日桶与它同一次扫描推进，窗口半与
    all-time 半因此描述同一段字节。

    until 给出时构成闭窗口，它必须是本地午夜，调用方已按格点判过，这里再
    校验一次。窗口只取 at < until 的那批日桶：恰好落在 until 上的行归
    until 那天那一桶，桶按 at 严格切分，见 _usage_hour_bucket；桶切不出
    那一刻的行，所以那一刻有整秒行时整条路退回 None，见
    _closed_window_edge_ok，调用方接着整段扫描，结果不变。

    状态冷时直接返回 None，冷状态包括从没折过、刚被价表、realm 或日志判废、
    以及采纳失败。那条路要先付一次全量重折，窗口请求不承担它：调用方退回
    按日分组的冷扫描，同一批行、同一个求和口径，逐字节相同；重折留给
    all-time 半与预热器，见 _analytics_state_cold。
    """
    with _analytics_state_lock:
        if _analytics_state_cold(realm, pricing_on):
            return None
        state, offset, error = _analytics_state_ready(realm, pricing_on)
        if error is not None:
            _gw().log("compute_usage_analytics failed: %s" % error)
            state.update({"maps": _new_analytics_maps(), "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None, "max_at": 0,
                          "hits": [], "hits_overflow": False})
            return None
        if not _days_cover(state, day_key):
            return None
        until_key = None
        if until is not None:
            until_key = _window_day_key_safe(until)
            if until_key is None or not _closed_window_edge_ok(state, until):
                return None
        maps = _new_analytics_maps()
        days = state["days"]
        for key in sorted(days):
            if key < day_key:
                continue
            if until_key is not None and key >= until_key:
                break
            _add_analytics_window_maps(maps, days[key])
    _usage_cache_maybe_save("analytics", offset)
    return maps

def _analytics_window_from_hours(realm, since, until, pricing_on):
    """窗口半 maps 等于窗口内的小时桶之和；小时层不可用时返回 None。

    成立条件缺一不可，任何一条不成立就退回扫描，优先保证正确：
      * 窗口起点落在本地午夜起算的小时格点上，调用方已判过，这里再按桶校验；
      * until 开放，或 until 不低于折过的最大 at，否则窗口上界会切掉桶里的
        行；或 until 落在同一套小时格点上且直到那一刻没有整秒行，见
        _closed_window_edge_ok。这时窗口只取 at < until 的那批桶，恰好落在
        until 上的行由边沿账挡回整段扫描；
      * days_floor 覆盖窗口起点所在的天，更早的天没有桶；
      * 窗口起点所在的天及其后每一天都有 hours 子表；被剪枝线剪掉、或本地
        午夜落在不存在的小时里而摘掉的那天，都会让窗口整体退回扫描。

    状态冷时直接返回 None，冷状态包括从没折过、刚被价表、realm 或日志判废、
    以及采纳失败。那条路要先付一次全量重折，窗口请求不承担它：调用方退回
    按小时分组的冷扫描，同一批行、同一个求和口径，逐字节相同；重折留给
    all-time 半与预热器，见 _analytics_state_cold。
    """
    with _analytics_state_lock:
        if _analytics_state_cold(realm, pricing_on):
            return None
        state, offset, error = _analytics_state_ready(realm, pricing_on)
        if error is not None:
            _gw().log("compute_usage_analytics failed: %s" % error)
            state.update({"maps": _new_analytics_maps(), "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None, "max_at": 0,
                          "hits": [], "hits_overflow": False})
            return None
        cut_at = None
        if until is not None:
            if until < state["max_at"]:
                # 闭窗口切进桶里：until 必须落在同一套小时格点上，且直到
                # 那一刻没有整秒行，桶才切得出这个窗口。
                if (_window_hour_key_safe(until) is None
                        or not _closed_window_edge_ok(state, until)):
                    return None
                cut_at = int(until)
        day_key = _local_day_key(since)
        if not _days_cover(state, day_key):
            return None
        lo_i = int(since)
        maps = _new_analytics_maps()
        for key in sorted(state["days"]):
            if key < day_key:
                continue
            if cut_at is not None and _day_key_start(key) > cut_at:
                break
            bucket = state["days"][key]
            hours = bucket.get("hours")
            if hours is None:
                return None
            day_at = int(bucket["at"])
            for hour in sorted(hours, key=int):
                sub = hours[hour]
                if int(sub["at"]) != day_at + int(hour) * 3600:
                    return None
                # 格点校验对起点所在那天的每个桶都做，含 at < since 的桶：
                # 时区偏移不是整小时的夏令时会让格点整体错开半小时，错开的
                # 桶可能横跨起点、把 at >= since 的行关在里面；跳过它就会
                # 漏行，所以宁可整个窗口退回扫描。
                if (int(sub["at"]) - lo_i) % 3600:
                    return None
                if int(sub["at"]) < lo_i:
                    continue
                if cut_at is not None and int(sub["at"]) >= cut_at:
                    # 闭窗口：until 那一刻归后一桶，桶按 at 严格切分；
                    # 只取 at < until 的桶。那一刻有整秒行时上面已经退回。
                    break
                _add_analytics_window_maps(maps, sub)
    _usage_cache_maybe_save("analytics", offset)
    return maps

def _analytics_window(realm, since, until, stop_at, pricing_on):
    """窗口半 analytics：同一窗口由日桶或小时桶相加，其余保持整段扫描。

    与 _usage_snapshot_window 同一条规矩，外加一层小时桶。起点是本地午夜、
    终点开放或也是本地午夜的窗口拆得成整天的并集；起点落在整小时的其它
    窗口，也就是自定义区间，拆得成整小时的并集，由 analytics 自己的小时层
    回答，见「小时层」一节；其余一律保持整段扫描。闭窗口的右边界按
    at < until 的桶切，那一刻有整秒行时退回扫描，见
    _closed_window_edge_ok。
    """
    day_key = _window_day_key_safe(since)
    until_day = None if until is None else _window_day_key_safe(until)
    if (day_key is not None and _usage_day_buckets_enabled()
            and (until is None or until_day is not None)):
        maps = _analytics_window_from_days(realm, day_key, until, pricing_on)
        if maps is not None:
            return maps
        # 日桶没盖住窗口起点：冷路径现折一份按日分组的窗口半，口径一致。
        return _scan_usage_analytics_window_days(realm, since, until, stop_at,
                                                 pricing_on)
    if _window_hour_aligned(since) and _usage_day_buckets_enabled():
        maps = _analytics_window_from_hours(realm, since, until, pricing_on)
        if maps is not None:
            return maps
        # 小时层没盖住：冷路径现折一份按小时分组的窗口半，口径与小时层一致。
        return _scan_usage_analytics_window_hours(realm, since, until, stop_at,
                                                  pricing_on)
    return _scan_usage_analytics_window(realm, since, until, stop_at=stop_at,
                                        pricing_on=pricing_on)

def _scan_usage_analytics_window(realm, since, until, stop_at=None, pricing_on=None):
    """整段扫描日志，只折叠窗口内的行。

    all-time 半不在这里重建，它来自增量折叠；读取停在增量折叠停下的位置，
    所以即使折叠中途有新行落盘，载荷的两半仍描述同一段字节。

    价表在这里取一次，与 all-time 折叠同一条口径，作为行级记忆的代次传给
    折叠：这次窗口扫描里的行可以与 all-time 半共享定价结果。
    """
    pricing = _pricing_inputs(pricing_on)
    maps = _new_analytics_maps()

    def fold(row):
        # 与 /usage 和 /usage/perf 使用同一组边界，三个读者对选中区间
        # 包含哪些行保持一致。
        at = row.get("at", 0)
        if since is not None and at < since:
            return
        if until is not None and at > until:
            return
        _fold_usage_analytics(row, realm, maps, _ANALYTICS_WINDOW, pricing_on,
                              pricing=pricing)

    _, error = _scan_usage_from(
        0, fold, stop_at=stop_at,
        skip=lambda line: _line_outside_window(line, since, until),
        skip_raw=lambda raw: _line_outside_window_bytes(raw, since, until))
    if error is not None:
        _gw().log("compute_usage_analytics failed: %s" % error)
    return maps

def _copy_analytics_models(models):
    """复制一份 model 到桶的映射；内层字典只读。"""
    return {mid: dict(entry) for mid, entry in models.items()}

def _copy_analytics_all_time(maps):
    """缓存 all-time 四张表的私有副本。

    载荷构建会装饰传入的对象，finalize 把派生比例写进 stat 字典；缓存的
    折叠又会在并发刷新下继续增长。所以调用方拿到的是持状态锁期间取得的
    副本，不是活视图。
    """
    out = {"summary": dict(maps["summary"]), "accts": {}, "models": {}, "keys": {}}
    for uid, entry in maps["accts"].items():
        copy = dict(entry)
        copy["all_time"] = dict(entry["all_time"])
        copy["all_models"] = _copy_analytics_models(entry["all_models"])
        out["accts"][uid] = copy
    for m_id, entry in maps["models"].items():
        out["models"][m_id] = {"model": m_id, "all_time": dict(entry["all_time"])}
    for k_id, km in maps["keys"].items():
        copy = dict(km)
        copy["all_time"] = dict(km["all_time"])
        copy["all_models"] = _copy_analytics_models(km["all_models"])
        out["keys"][k_id] = copy
    return out

def _merge_analytics_halves(all_maps, win_maps):
    """把缓存的 all-time 折叠与窗口折叠合并成一份载荷。

    没有选择区间时 `win_maps` 就是 `all_maps` 本身：每一行都在开放窗口里，
    窗口半等于再做一遍 all-time 折叠。此时两半会被复制开，效果与再扫一遍
    相同，并保证调用方修改一半时不会影响另一半与缓存。

    只存在于窗口半的条目不会出现：窗口扫描停在 all-time 折叠停下的位置，
    它见过的每一行都已经在 all-time 半里。
    """
    shared = win_maps is all_maps
    acct_map = {}
    for uid, entry in all_maps["accts"].items():
        win = win_maps["accts"].get(uid)
        if shared:
            window_stat = dict(entry["all_time"])
            window_models = _copy_analytics_models(entry["all_models"])
        elif win is None:
            window_stat = _new_analytics_stat()
            window_models = {}
        else:
            window_stat = win["window"]
            window_models = win["window_models"]
        acct_map[uid] = {
            "uid": uid,
            "nickname": uid,
            "realm": entry["realm"],
            "domain": "",
            "window": window_stat,
            "all_time": dict(entry["all_time"]),
            "window_models": window_models,
            "all_models": _copy_analytics_models(entry["all_models"]),
        }
    model_map = {}
    for m_id, entry in all_maps["models"].items():
        stat = entry["all_time"]
        win = win_maps["models"].get(m_id)
        if shared:
            window_stat = dict(stat)
        elif win is None:
            window_stat = _new_analytics_stat()
        else:
            window_stat = win["window"]
        model_map[m_id] = {"model": m_id, "window": window_stat,
                           "all_time": dict(stat)}
    key_map = {}
    for k_id, km in all_maps["keys"].items():
        win = win_maps["keys"].get(k_id)
        if shared:
            window_stat = dict(km["all_time"])
            window_models = _copy_analytics_models(km["all_models"])
        elif win is None:
            window_stat = _new_analytics_stat()
            window_models = {}
        else:
            window_stat = win["window"]
            window_models = win["window_models"]
        key_map[k_id] = {
            "key": k_id,
            "window": window_stat,
            "all_time": dict(km["all_time"]),
            "window_models": window_models,
            "all_models": _copy_analytics_models(km["all_models"]),
            "realms": dict(km["realms"]),
            "last_at": km["last_at"],
        }
    return {"summary": dict(all_maps["summary"]),
            "window_summary": (dict(all_maps["summary"]) if shared
                               else win_maps["summary"]),
            "accts": acct_map, "models": model_map, "keys": key_map}

def _add_analytics_stat(dst, src):
    """把两份 _new_analytics_stat 字典逐字段就地相加。"""
    for field, value in src.items():
        if isinstance(value, (int, float)):
            dst[field] += value

# 日桶保留天数：月窗口最多回溯 30 天，32 天留两天余量。更早的天没有窗口够
# 得到，留着只让 checkpoint 变大。调小 WB_USAGE_DAY_KEEP_DAYS 会让更宽的窗口
# 退回扫描，结果正确只是慢。
_USAGE_DAY_KEEP_DAYS = 32

# 小时桶保留天数：小时层只服务自定义整点窗口，保留 2 天即 48 到 72 小时（剪枝
# 按天走）。每个小时桶都是完整的一份窗口半快照，含三层 by_model_acct，留着会
# 让 checkpoint 每次写盘都变大。覆盖不到的窗口退回冷路径，结果正确只是慢。
_USAGE_HOUR_KEEP_DAYS = 2

# 日桶求和要跳过的字段：started 是每份快照各自的创建时刻，不是累计量。
# 与 wb_usage_cache._usage_cache_numbers("snapshot") 同源，都是同一份构造函数的数字字段；
# 那段常量在文件更靠后，这里不能引用，所以自己推导。
_USAGE_DAY_SNAPSHOT_FIELDS = tuple(
    k for k, v in _empty_stats().items()
    if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "started")

def _usage_keep_setting(key):
    """实际生效的聚合保留值；环境变量优先，其次面板显式保存，都没有返回 None。

    每次调用都读环境变量与设置：现场调整与测试切换都要即时生效。
    """
    try:
        return wb_settings.usage_keep_value(_gw().ACCOUNTS_DIR, key)
    except Exception:
        return None

def _usage_day_keep_days():
    """日桶保留天数，默认 32；WB_USAGE_DAY_KEEP_DAYS 与面板设置优先于默认值。

    每次调用都读环境变量与设置：现场调整与测试切换都要即时生效。
    """
    value = _usage_keep_setting("day_keep_days")
    if value:
        return value
    raw = os.environ.get("WB_USAGE_DAY_KEEP_DAYS")
    if raw is not None:
        try:
            return max(1, min(int(raw), wb_settings.USAGE_LIMITS["day_keep_days"]))
        except (TypeError, ValueError):
            return _USAGE_DAY_KEEP_DAYS
    return _USAGE_DAY_KEEP_DAYS

def _usage_hour_keep_days():
    """小时桶保留天数，默认 2；WB_USAGE_HOUR_KEEP_HOURS 与面板的「小时层保留」
    决定它，其次才是 WB_USAGE_HOUR_KEEP_DAYS。

    两个状态各有一条小时层保留线，snapshot 按天、analytics 按小时；面板只
    暴露一个「小时层保留」，单位小时，所以这里跟着它走，避免调了一个另一个
    不动。取值最低为 1 天：小时层至少要能服务今天的整点窗口，否则等于没建。
    """
    hours = _usage_keep_setting("hour_keep_hours")
    if hours:
        return max(1, (hours + 23) // 24)
    raw = os.environ.get("WB_USAGE_HOUR_KEEP_DAYS")
    if raw is not None:
        try:
            cap = (wb_settings.USAGE_LIMITS["hour_keep_hours"] + 23) // 24
            return max(1, min(int(raw), cap))
        except (TypeError, ValueError):
            return _USAGE_HOUR_KEEP_DAYS
    return _USAGE_HOUR_KEEP_DAYS

def _usage_day_buckets_enabled():
    """日桶总开关，默认开；面板设置可关闭，退回逐行扫描口径。

    关掉时既不折日桶也不使用它们，窗口路径与逐行口径逐字节一致。小时层挂在
    日桶上，即每个日桶里的 hours 子表，所以这个开关一并管住小时层。
    """
    return wb_settings.usage_day_buckets(_gw().ACCOUNTS_DIR)

# 48 小时（剪枝按天走，实际保留 48～72 小时）：只服务「从昨天某个整点起」
# 这类手动窗口；更早的窗口退回扫描，结果正确只是慢。
_USAGE_HOUR_KEEP_HOURS = 48

def _usage_hour_keep_hours():
    """小时子表保留小时数，默认 48；面板设置优先，其次
    WB_USAGE_HOUR_KEEP_HOURS。

    每次调用都读环境变量与设置，理由与 _usage_day_keep_days 相同：现场调整与
    测试来回切换都要即时生效。
    """
    value = _usage_keep_setting("hour_keep_hours")
    if value:
        return value
    raw = os.environ.get("WB_USAGE_HOUR_KEEP_HOURS")
    if raw is not None:
        try:
            return max(1, min(int(raw), wb_settings.USAGE_LIMITS["hour_keep_hours"]))
        except (TypeError, ValueError):
            return _USAGE_HOUR_KEEP_HOURS
    return _USAGE_HOUR_KEEP_HOURS

def _local_day_key(ts):
    """ts 所属的本地日历日，形如 "YYYY-MM-DD"。

    日桶的键用字符串日历日，不取午夜 epoch：窗口与日桶的比较就是一次字符串
    比较，ISO 日期天然有序；查询不必再算一遍本地午夜，也不会被夏令时的日长
    变化影响。同一套本地日历日规则在折叠与查询两侧都成立。

    结果按 ts 记忆，见「行级结果记忆」：一次冷启动里三个视图各扫一遍日志，
    每一行都要问一次日键，第二、第三遍直接取。时区被 tzset 换过时表整体
    作废；不可哈希的 ts，即坏行，不进表，照旧真算并抛出同样的异常。
    """
    memo = _row_day_memo
    if _row_memo_tz is not time.tzname:
        _row_memo_tz_changed()
    try:
        hit = memo.get(ts)
    except TypeError:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    if hit is not None:
        return hit
    key = time.strftime("%Y-%m-%d", time.localtime(ts))
    cap = _ROW_MEMO_MAX
    if cap > 0:
        if len(memo) >= cap:
            # 满了：只收不比表里最旧那条更旧的 ts。扫描从头往尾走，表里
            # 留住的因此总是最近的一批行，第二遍走到尾部照样命中。
            oldest = next(iter(memo))
            if _row_memo_at(ts) < _row_memo_at(oldest):
                return key
            memo.pop(oldest, None)
        memo[ts] = key
    return key

def _day_key_start(key):
    """日键 -> 该日的本地午夜 epoch。"""
    lt = time.strptime(key, "%Y-%m-%d")
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

def _window_day_key(since):
    """窗口起点能由日桶推导时返回它的日键，否则 None。

    只有起点正好是本地午夜的窗口才拆得成整天的并集：at 不早于本地午夜
    等价于日键不早于那天的键。自定义区间的起点是任意时刻，拆不开，那条
    路径保持整段扫描，优先保证正确。
    """
    if since is None:
        return None
    if _local_midnight(since) != since:
        return None
    return _local_day_key(since)

def _hour_window_key(since):
    """窗口起点能由小时桶推导时返回日键与小时序号的元组，否则 None。

    只有起点正好是本地整点的窗口才拆得成整小时桶的并集。小时桶按离那天
    本地午夜多少个 3600 秒切格点，见 _usage_hour_bucket，所以起点必须同时
    满足两条：本地墙上时间落在整点，_local_hour_start 能原样还原；并且离
    本地午夜的距离是 3600 的整数倍。夏令时把日边界挪动，或时区按半小时
    偏移时，只要 since 不落在格点上就返回 None，退回扫描，优先保证正确。

    整点起点也可能是本地午夜，两者同时成立；调用方先问日桶，所以这条只在
    起点不是午夜时接住自定义区间的整点窗口。
    """
    if since is None:
        return None
    if _local_hour_start(since) != since:
        return None
    day_key = _local_day_key(since)
    day_at = _day_key_start(day_key)
    k = int((since - day_at) // 3600)
    if day_at + k * 3600 != since:
        return None
    return day_key, k

def _window_hour_aligned(since):
    """窗口起点是否落在本地午夜加 k*3600 的小时格点上。

    小时桶的格点是离那天的本地午夜多少秒，见 _analytics_hour_bucket，所以
    起点减它所属那天的本地午夜必须是 3600 的整数倍。自定义区间的起点是
    任意分钟时拆不开，那条路径保持整段扫描，优先保证正确；起点正好是本地
    午夜时恒成立，因为 0 是 3600 的整数倍，与 _window_day_key 相容。
    """
    if since is None:
        return False
    return (_local_midnight(since) - since) % 3600 == 0

# 闭窗口边沿账的上限：只有 at 恰好落在整秒上的行进账，真实日志里几乎为
# 零，因为 at 是 time.time 的浮点秒，落在整秒上的概率约百万分之一。手改过
# 的日志可能有很多，超过上限就停记并留痕，闭窗口退回整段扫描，优先保证
# 正确。
_CLOSED_WINDOW_HITS_MAX = 256

def _closed_window_hit_record(state, at):
    """行恰好落在整秒上时记一笔，供闭窗口的桶求和核对边沿。

    桶按 at 严格切分，见 _usage_hour_bucket：闭窗口 [since, until] 与
    at < until 的那批桶只差恰好落在 until 上的行。所以直到那一刻没有整秒行
    时，桶的和就是窗口的全部，见 _closed_window_edge_ok。这份账只收真正
    进了桶的那些行，realm 与剪枝线与折叠同一套判定，记录点因此在
    _usage_day_bucket 的过滤之后。

    at 不是有限数时，手改日志可能写出 Infinity 或 NaN，整秒判定不成立就
    跳过；`at // 1` 对这些值不抛异常，不会把一次折叠打断在这里。
    """
    if at != at // 1:
        return
    hits = state["hits"]
    if len(hits) >= _CLOSED_WINDOW_HITS_MAX:
        state["hits_overflow"] = True
        return
    hits.append(int(at))

def _closed_window_edge_ok(state, until):
    """闭窗口的右边界能否由存量桶回答：直到 until 为止没有行落在整秒上。

    满足时，「[since, until] 的行」与「at < until 的桶」是同一批；桶按 at
    严格切分，恰好落在 until 上的行归后一桶。真有一行恰好落在 until 上时
    桶切不出它，只能退回整段扫描。账记满时同样退回，见
    _closed_window_hit_record。
    """
    if state["hits_overflow"]:
        return False
    return until not in state["hits"]

def _window_day_key_safe(ts):
    """_window_day_key 的取值安全版：取值离谱时返回 None。

    手写的极大 since 或 until，例如 6e18，会让 _local_midnight 里的
    time.localtime 抛出 OSError。那只是这个窗口服务不了的一种情形，按退回
    整段扫描处理，不该把端点打成 500，与 _series_window_day_key 同一个
    用意。
    """
    try:
        return _window_day_key(ts)
    except (OSError, OverflowError, ValueError):
        return None

def _window_hour_key_safe(ts):
    """_hour_window_key 的取值安全版：取值离谱时返回 None，理由同上。"""
    try:
        return _hour_window_key(ts)
    except (OSError, OverflowError, ValueError):
        return None

def _new_window_snapshot_bucket(at):
    """一份带格点 at 的窗口半快照桶，日桶与小时桶共用这个形状。

    started 不参与求和，见 _add_usage_snapshot，形状保留只是让校验与
    all-time 半共用同一套 _usage_cache_snapshot_ok。at 是这个桶的本地格点
    epoch，取自日的午夜或小时的整点：小时桶按它切 3600 的格点，求和侧按它
    做格点校验，与时间序列的桶同一套做法，见 _new_series_bucket。
    """
    bucket = _empty_stats()
    bucket["at"] = at
    return bucket

# ---------------------------------------------------------------------------
# analytics 的小时层：自定义区间窗口的快速取值。
#
# 日桶只拆得开起点为本地午夜且 until 开放的窗口，自定义区间的起点是任意时刻，
# 只能整段扫描。analytics 的 token 口径含失败行，借不了别的层，所以在自己的
# 日桶里再挂一层小时子表。起点落在本地整点的窗口等于窗口内小时桶之和，复杂度
# 随小时数线性增长；与日桶一样，求和统一用按小时分组的口径，冷扫描、小时桶与
# 加载 checkpoint 三条路径给出的字节一致。非整小时的起点仍然整段扫描。
#
# 保留期比日桶短，由 WB_USAGE_HOUR_KEEP_HOURS 控制，默认 48 小时：每个小时桶
# 的形状与日桶一样，跟着 32 天的日桶全留会让 checkpoint 膨胀十几倍。剪枝线以外
# 的天没有 hours 字段，查询侧据此退回扫描。
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 按日分桶：窗口半的增量。
#
# all-time 半按字节偏移折叠并落盘，而 today、week、month 的窗口半仍要重扫。
# 窗口边界都是本地午夜，每行只属于一天，折叠时顺手把行折进所属那天的桶，任何
# 窗口就退化成把窗口内的日桶相加，复杂度随天数线性增长。
#
# 行的本地日只看行自己的 at（见 _local_day_key），与 range_window 用同一套本地
# 午夜；折叠时刻不参与切天，迟写或补写的行落进它时间戳所属的那天。跨夏令时同理：
# 日键是本地日历日，窗口起点是本地午夜时，日键不早于窗口日键就等价于 at 不早于
# since。已封口的日桶不再变化，剪枝只在新建桶时执行（见 _prune_day_buckets），
# 剪掉的都是任何窗口都够不到的天，够不到的天退回整段扫描。
#
# 浮点求和按日分组，与整段扫描只差结合顺序：credit、cost_cny 与 speed_sum 可能差
# 最后一两个 ulp，整数字段逐位相同。所有路径统一用这个口径，冷扫描、日桶与加载
# checkpoint 的字节一致；面板关闭日与小时分桶时整体退回逐行口径。
#
# 小时层：每个日桶里再挂一张小时子表（见 _usage_hour_bucket），起点落在本地整点的
# 窗口由小时桶之和回答。它保留期更短，覆盖不到的窗口退回按小时分组的冷路径。
# ---------------------------------------------------------------------------
def _day_snapshot_bucket(key, day_start):
    """一天一份的窗口半快照桶；形状与 _empty_stats 一致。"""
    return _new_window_snapshot_bucket(day_start)

def _day_analytics_bucket(key, day_start):
    """一天一份的窗口形状 analytics maps，带 at 与小时子表，见「小时层」
    一节。

    at 是那天的本地午夜：小时子表按离本地午夜多少秒切，行的归属因此不
    依赖折叠时刻，迟写或补写的行落回它时间戳所属的那天那小时。
    """
    bucket = _new_analytics_maps()
    bucket["at"] = day_start
    bucket["hours"] = {}
    return bucket

def _new_analytics_hour_bucket(at):
    """一小时一份的窗口形状 analytics maps，带 at，见「小时层」一节。"""
    bucket = _new_analytics_maps()
    bucket["at"] = at
    return bucket

def _analytics_hour_bucket(day, at):
    """这一行该折进的小时桶，来自 day 桶的 hours 子表；不该折时返回 None。

    小时键是离那天的本地午夜多少个整小时的字符串，与 JSON 往返后的形状
    一致，同时间序列的小时子表。本地午夜落在不存在的小时里时，即夏令时在
    午夜切换的极少数时区，这一行放不进格点：把整天的 hours 摘掉，让覆盖
    这一天的小时窗口整体退回扫描；日桶本身照常，不受影响。
    """
    if day is None:
        return None
    hours = day.get("hours")
    if hours is None:
        return None
    day_at = int(day["at"])
    if at < day_at:
        day.pop("hours", None)
        return None
    hour = str(int((at - day_at) // 3600))
    sub = hours.get(hour)
    if sub is None:
        sub = hours[hour] = _new_analytics_hour_bucket(day_at + int(hour) * 3600)
    return sub

def _day_series_bucket(key, day_start):
    """一天一份的时间序列桶，带小时子表，见 _fold_usage_series。"""
    bucket = _new_series_bucket(day_start)
    bucket["hours"] = {}
    return bucket

def _usage_day_bucket(state, row, r, factory, day_buckets=None):
    """这一行该折进哪个日桶；必要时新建并顺带剪枝，不该折时返回 None。

    行的归属只看它自己的 at：补写或迟到的行必须落进它时间戳所属的那天。
    realm 不匹配的行折叠本来就会跳过，这里也不为它建桶；总开关关掉时连
    日桶都不折，窗口路径也不会用它们，见 _usage_day_buckets_enabled。
    day_buckets 是同一开关在扫描开头取一次的结果（None 表示逐行自查），折叠
    看到扫描开始那一刻的值，改动自下一次扫描生效，与 pricing_on 同一契约。
    """
    if not (day_buckets if day_buckets is not None
            else _usage_day_buckets_enabled()):
        return None
    if r and not row_matches_realm(row, r):
        return None
    at = row.get("at") or 0
    key = _local_day_key(at)
    floor = state.get("days_floor")
    if floor is not None and key < floor:
        # 剪枝线以下的天：那些桶已经被丢掉，而且任何可服务的窗口都从
        # days_floor 那天起，这一行不在其中，跳过不影响窗口。
        return None
    _closed_window_hit_record(state, at)
    days = state["days"]
    bucket = days.get(key)
    if bucket is None:
        bucket = days[key] = factory(key, _day_key_start(key))
        _prune_day_buckets(state, days, key)
    return bucket

def _prune_day_buckets(state, days, newest_key):
    """丢掉任何窗口都够不到的旧日桶，并把 days_floor 抬到剪枝线。

    days_floor 的语义是这份状态从哪天起是完整的：它就是剪枝线，即最新那天
    往前 _usage_day_keep_days 天。没剪过时第一次折到新的一天也会把它记下
    来；比这条线更早的窗口一律退回整段扫描，例如时钟大幅回拨或日志里有
    远古补写行，宁可多扫一次，不能少算一天。

    analytics 状态还挂着一条更短的小时子表保留线，即 state["hours_keep"]：
    小时层只为近期的自定义窗口服务，只摘旧天的 "hours" 子表，日桶本身照常
    留满保留天数。被摘掉的天由查询侧按「这一天还有没有 hours」自己表达，
    见 _analytics_window_from_hours，所以这里不用另存一个 floor。
    """
    horizon = _local_day_key(_day_key_start(newest_key)
                             - _usage_day_keep_days() * 86400)
    for old in [k for k in days if k < horizon]:
        del days[old]
    floor = state.get("days_floor")
    if floor is None or floor < horizon:
        state["days_floor"] = horizon
    # 带小时层的状态，即 snapshot，见 _usage_hour_bucket，顺带剪小时表：
    # 小时层的保留期比日桶短，剪枝线要跟着一起抬。analytics 与 series 的
    # 状态没有 hours_floor_at 这个键，不会被这条带到。
    if "hours_floor_at" in state:
        _prune_hour_buckets(state, days, newest_key)
    # analytics 状态还挂着一条更短的小时子表保留线，即 state["hours_keep"]：
    # 小时层只为近期的自定义窗口服务，只摘旧天的 "hours" 子表，日桶本身照常
    # 留满保留天数。
    if state.get("hours_keep"):
        hour_horizon = _local_day_key(_day_key_start(newest_key)
                                      - _usage_hour_keep_hours() * 3600)
        for key, bucket in days.items():
            if key < hour_horizon:
                bucket.pop("hours", None)

def _days_cover(state, day_key):
    """这份状态的日桶能否服务以 day_key 为起点的窗口。

    days_floor 是剪枝线：比它早的窗口可能少了被剪掉的天，只能退回扫描。
    它要等第一次折到新的一天才会记下；没折过就是 None，此时谁都不服务，
    调用方总是先折到文件尾，所以实际不会遇到。
    """
    floor = state.get("days_floor")
    return floor is not None and day_key >= floor

def _usage_hour_bucket(state, row, day):
    """这一行该折进哪个小时桶；不该折时返回 None。

    小时桶挂在日桶里的 `hours` 子表，键是离当天本地午夜多少个 3600 秒的
    字符串，与时间序列的小时子桶同一套格点规则，见 _fold_usage_series：
    桶的 at 等于日桶 at 加序号乘 3600，所以每个桶恰好覆盖一个整点区间，
    跨桶求和不会重复也不会漏。

    at 早于日桶 at 的行放不进格点：极少数时区在本地午夜切换夏令时，午夜
    那一刻不存在。此时整张小时表摘掉并留痕，`hours` 记为 None，求和侧看到
    没有表就退回按小时分组的冷路径，日桶本身照常，日窗口不受影响。留痕是
    为了挡住摘掉之后又来了这一天的行、把表重建出来：重建的表缺了被摘掉的
    那批行，一旦被求和侧选中就是少算。

    hours_floor_at 是小时层的剪枝线：比它早的天不再折小时表。迟写或补写的
    行可能落回很久以前那天，日桶还留着，但那张表早被剪掉；重新建起来只会
    得到一张缺了其余小时的表，求和侧一旦用它就会少算，所以这里直接不折。
    求和侧另有 _hours_cover 挡住更早的窗口。
    """
    floor = state.get("hours_floor_at")
    if floor is None or day["at"] < floor:
        return None
    at = row.get("at") or 0
    day_at = day["at"]
    hours = day.get("hours")
    if hours is None and "hours" in day:
        # 这天的小时表被摘过，午夜那一刻不存在，不再重建，见 docstring。
        return None
    if at < day_at:
        day["hours"] = None
        return None
    if hours is None:
        hours = day["hours"] = {}
    hour = str(int((at - day_at) // 3600))
    sub = hours.get(hour)
    if sub is None:
        sub = hours[hour] = _new_window_snapshot_bucket(day_at + int(hour) * 3600)
    return sub

def _prune_hour_buckets(state, days, newest_key):
    """丢掉保留窗口之外的日桶里的小时表，并把 hours_floor_at 抬到剪枝线。

    小时层比日桶保留期短，只服务最近几天的整点窗口，见
    _USAGE_HOUR_KEEP_DAYS。剪枝按天走，与 _prune_day_buckets 同一套算术，
    所以实际保留在 keep_days 与 keep_days 加 1 天之间。hours_floor_at 的
    语义与 days_floor 相同：这份状态的小时层从哪个时刻起是完整的；更早的
    整点窗口一律退回冷路径，宁可多扫一次，不能少算一个桶。
    """
    horizon_at = _day_key_start(newest_key) - _usage_hour_keep_days() * 86400
    horizon = _local_day_key(horizon_at)
    for key in list(days):
        if key < horizon and "hours" in days[key]:
            # 与午夜兜底同一个留痕：表置 None，不删键。下面那条剪枝线
            # 本来也挡着重建，但留痕让这张表不可用成为桶自己的事实，求和侧
            # 与折叠侧不必再依赖两条线的一致性。
            days[key]["hours"] = None
    floor = state.get("hours_floor_at")
    if floor is None or floor < horizon_at:
        state["hours_floor_at"] = horizon_at

def _hours_cover(state, since):
    """这份状态的小时层能否服务起点为 `since` 的窗口。

    hours_floor_at 是剪枝线，见 _prune_hour_buckets：比它早的窗口可能少了
    被剪掉的小时表，只能退回扫描。它要等第一次折到新的一天才会记下；没折
    过就是 None，此时谁都不服务，调用方总是先折到文件尾。
    """
    floor = state.get("hours_floor_at")
    return floor is not None and since >= floor

def _hours_floor_on_adopt(days, stored):
    """采用 checkpoint 时的小时层剪枝线：取文件里的与按最新一天现算的
    较晚者。

    较晚者一定安全：剪枝线只会往后抬，抬高了最多是少服务几天，那些窗口
    退回冷路径；永远不会服务一张被剪过的表。两条都挡住同一条路，即拿不
    完整的表当完整，那是这个层唯一会算错数的方向。文件里那条可能是别的
    保留天数下写的，现算这条跟着当前 WB_USAGE_HOUR_KEEP_DAYS 走。
    """
    recomputed = None
    if days:
        recomputed = _day_key_start(max(days)) - _usage_hour_keep_days() * 86400
    if stored is None:
        return recomputed
    if recomputed is None:
        return stored
    return max(stored, recomputed)

def _add_usage_bucket(dst_map, key, src):
    """把一个 by_model* 桶并入同键的桶；不存在就整只拷进来。"""
    dst = dst_map.get(key)
    if dst is None:
        dst_map[key] = _copy_usage_bucket(src)
        return
    for field in _USAGE_CACHE_BUCKET_NUMBERS:
        dst[field] += src.get(field) or 0
    for acct, count in src["accounts"].items():
        dst["accounts"][acct] = dst["accounts"].get(acct, 0) + count

def _add_usage_snapshot(dst, src):
    """把一份窗口半快照按字段并入另一份，供日桶求和。

    逐字段相加，不是重折：日桶是不可变的聚合，窗口只是它们的和。started
    是每份快照各自的创建时刻，不是累计量，跳过。
    """
    for field in _USAGE_DAY_SNAPSHOT_FIELDS:
        dst[field] += src.get(field) or 0
    for mid, count in src["cost_missing"].items():
        dst["cost_missing"][mid] = dst["cost_missing"].get(mid, 0) + count
    for mid, bucket in src["by_model"].items():
        _add_usage_bucket(dst["by_model"], mid, bucket)
    for mid, realms in src["by_model_realm"].items():
        for rr, bucket in realms.items():
            _add_usage_bucket(dst["by_model_realm"].setdefault(mid, {}), rr, bucket)
    for mid, realms in src["by_model_acct"].items():
        for rr, accts in realms.items():
            tgt = dst["by_model_acct"].setdefault(mid, {}).setdefault(rr, {})
            for acct, bucket in accts.items():
                _add_usage_bucket(tgt, acct, bucket)

def _add_analytics_models(dst, src):
    """把一份 model 到 requests、tokens、reasoning、cost_cny 统计的映射
    并入另一份。"""
    for mid, stat in src.items():
        tgt = dst.get(mid)
        if tgt is None:
            dst[mid] = dict(stat)
            continue
        for field in ("requests", "tokens", "reasoning", "cost_cny"):
            tgt[field] += stat.get(field) or 0

def _add_analytics_window_maps(dst, src):
    """把一份窗口形状的 analytics maps 并入另一份，供日桶求和。

    日桶本身就是 _ANALYTICS_WINDOW 那一半的形状，即 window 与
    window_models，所以这里只动这两个键；realms 与 last_at 照并，虽然
    _merge_analytics_halves 最终取的是 all-time 侧的值。
    """
    _add_analytics_stat(dst["summary"], src["summary"])
    for uid, entry in src["accts"].items():
        tgt = dst["accts"].get(uid)
        if tgt is None:
            tgt = dst["accts"][uid] = {
                "uid": uid, "nickname": uid, "realm": entry["realm"],
                "domain": "", "window": _new_analytics_stat(),
                "window_models": {}}
        _add_analytics_stat(tgt["window"], entry["window"])
        _add_analytics_models(tgt["window_models"], entry["window_models"])
    for mid, entry in src["models"].items():
        tgt = dst["models"].get(mid)
        if tgt is None:
            tgt = dst["models"][mid] = {"model": mid,
                                        "window": _new_analytics_stat()}
        _add_analytics_stat(tgt["window"], entry["window"])
    for kid, entry in src["keys"].items():
        tgt = dst["keys"].get(kid)
        if tgt is None:
            tgt = dst["keys"][kid] = {
                "key": kid, "window": _new_analytics_stat(),
                "window_models": {}, "realms": {}, "last_at": 0}
        _add_analytics_stat(tgt["window"], entry["window"])
        _add_analytics_models(tgt["window_models"], entry["window_models"])
        for rr, count in entry["realms"].items():
            tgt["realms"][rr] = tgt["realms"].get(rr, 0) + count
        if entry["last_at"] > tgt["last_at"]:
            tgt["last_at"] = entry["last_at"]

def _copy_analytics_window_maps(maps):
    """一份窗口形状 maps 的深拷贝，落盘用，形状同 _new_analytics_maps。

    日桶与小时桶还会多带 at 与 hours 子表：照原样拷贝，键序保持折叠时的
    插入顺序。checkpoint 不排序，加载回来要与冷启动逐字节一致，见
    _usage_cache_write。
    """
    out = {"summary": dict(maps["summary"]), "accts": {}, "models": {}, "keys": {}}
    for uid, entry in maps["accts"].items():
        copy = dict(entry)
        copy["window"] = dict(entry["window"])
        copy["window_models"] = _copy_analytics_models(entry["window_models"])
        out["accts"][uid] = copy
    for mid, entry in maps["models"].items():
        out["models"][mid] = {"model": mid, "window": dict(entry["window"])}
    for kid, km in maps["keys"].items():
        copy = dict(km)
        copy["window"] = dict(km["window"])
        copy["window_models"] = _copy_analytics_models(km["window_models"])
        copy["realms"] = dict(km["realms"])
        out["keys"][kid] = copy
    if "at" in maps:
        out["at"] = maps["at"]
    hours = maps.get("hours")
    if hours is not None:
        out["hours"] = {h: _copy_analytics_window_maps(sub)
                        for h, sub in hours.items()}
    return out

def _scan_usage_snapshot_window_days(r, since, until, pricing_on=None):
    """窗口半的冷路径：整段扫一遍，先按日分桶再相加。

    日桶状态不可用时走这里，包括还没折过、被剪枝、折叠出错。结果必须与
    日桶状态给出的窗口半逐字节一致：窗口起点是本地午夜，until 给出时终点
    也是本地午夜；两边装的是同一批行、同一个按日分组的求和口径，只是桶
    一个现折、一个存量。

    状态冷时它是窗口请求的直接答案，不折状态，重折留给 all-time 视图与
    预热器，见 _usage_snap_state_cold。其余情形它紧跟一次 all-time 折叠：
    _usage_snapshot_window_from_days 先折到文件尾才发现日桶不够用。行级
    记忆几乎全中，同一批行刚在 all-time 半算过价与日键。窗口外的行由 skip
    挡在解析之前，这条路只付窗口内那批行。价表照样在本函数开头取一次。
    """
    pricing = _pricing_inputs(pricing_on)
    days = {}

    def fold(row):
        at = row.get("at") or 0
        if since and at < since:
            return
        if until and at > until:
            return
        key = _local_day_key(at)
        bucket = days.get(key)
        if bucket is None:
            bucket = days[key] = _empty_stats()
        _fold_usage_snapshot(row, bucket, r, None, None, pricing_on,
                             pricing=pricing)

    _, error = _scan_usage_from(
        0, fold,
        skip=lambda line: _line_outside_window(line, since or None,
                                               until or None),
        skip_raw=lambda raw: _line_outside_window_bytes(raw, since or None,
                                                        until or None))
    if error is not None:
        _gw().log(f"usage snapshot read failed: {error}")
    snap = _empty_stats()
    for key in sorted(days):
        _add_usage_snapshot(snap, days[key])
    return snap

def _scan_usage_snapshot_window_hours(r, since, until, pricing_on=None):
    """窗口半的冷路径：整段扫一遍，先按小时分桶再相加。

    小时层状态不可用时走这里，包括还没折过、被剪枝、折叠出错、那天的格点
    不成立。结果必须与小时桶状态给出的窗口半逐字节一致：窗口起点是本地
    整点，until 给出时终点也落在同一套格点上；两边装的是同一批行、同一个
    按日与小时分组的求和口径，只是桶一个现折、一个存量。

    分组与 _usage_hour_bucket 同一套算术，即日键加离午夜多少个 3600 秒。
    但这里对 at 早于本地午夜的行照折不误，键可以是负的：状态侧那种行会把
    整张小时表摘掉、窗口退回这条路径，所以这条路径必须把它们算进去。
    排序按日键与序号的数值序，与状态侧的遍历顺序一致。

    价表在本函数开头取一次，行级记忆，见「行级结果记忆」，跟着这一批扫描
    走：同一批行通常刚在 all-time 半算过价与日键，这里几乎全中。
    """
    pricing = _pricing_inputs(pricing_on)
    hours = {}
    day_ats = {}

    def fold(row):
        at = row.get("at") or 0
        if since and at < since:
            return
        if until and at > until:
            return
        key = _local_day_key(at)
        day_at = day_ats.get(key)
        if day_at is None:
            day_at = day_ats[key] = _day_key_start(key)
        hour = int((at - day_at) // 3600)
        bucket = hours.get((key, hour))
        if bucket is None:
            bucket = hours[(key, hour)] = _empty_stats()
        _fold_usage_snapshot(row, bucket, r, None, None, pricing_on,
                             pricing=pricing)

    _, error = _scan_usage_from(
        0, fold,
        skip=lambda line: _line_outside_window(line, since or None,
                                               until or None),
        skip_raw=lambda raw: _line_outside_window_bytes(raw, since or None,
                                                        until or None))
    if error is not None:
        _gw().log(f"usage snapshot read failed: {error}")
    snap = _empty_stats()
    for key in sorted(hours):
        _add_usage_snapshot(snap, hours[key])
    return snap

def _scan_usage_analytics_window_days(realm, since, until, stop_at=None,
                                      pricing_on=None):
    """窗口半 analytics 的冷路径：按日分桶再相加，见 snapshot 版。"""
    pricing = _pricing_inputs(pricing_on)
    days = {}

    def fold(row):
        at = row.get("at") or 0
        if since is not None and at < since:
            return
        if until is not None and at > until:
            return
        key = _local_day_key(at)
        maps = days.get(key)
        if maps is None:
            maps = days[key] = _new_analytics_maps()
        _fold_usage_analytics(row, realm, maps, _ANALYTICS_WINDOW, pricing_on,
                              pricing=pricing)

    _, error = _scan_usage_from(
        0, fold, stop_at=stop_at,
        skip=lambda line: _line_outside_window(line, since, until),
        skip_raw=lambda raw: _line_outside_window_bytes(raw, since, until))
    if error is not None:
        _gw().log("compute_usage_analytics failed: %s" % error)
    out = _new_analytics_maps()
    for key in sorted(days):
        _add_analytics_window_maps(out, days[key])
    return out

def _scan_usage_analytics_window_hours(realm, since, until, stop_at=None,
                                       pricing_on=None):
    """窗口半 analytics 的冷路径，起点整小时：按小时分桶再相加。

    小时层不可用时走这里，包括还没折过、被剪枝、折叠出错、until 切在桶
    中间。结果必须与小时桶状态给出的窗口半逐字节一致：两边装的是同一批行、
    同一个按小时分组的求和口径，只是桶一个现折、一个存量。

    分桶与折叠侧同一条规则，即离那天的本地午夜多少秒。本地午夜落在不存在
    的小时里时这一天的小时键会变成负数，求和照样把它算进去，因为分组只是
    浮点结合顺序的载体，所以这条冷路径不依赖格点是否成立。

    价表在本函数开头取一次，行级记忆，见「行级结果记忆」，跟着这一批扫描
    走。
    """
    pricing = _pricing_inputs(pricing_on)
    days = {}

    def fold(row):
        at = row.get("at") or 0
        if since is not None and at < since:
            return
        if until is not None and at > until:
            return
        key = _local_day_key(at)
        day = days.get(key)
        if day is None:
            day = days[key] = (_day_key_start(key), {})
        day_at, hours = day
        hour = str(int((at - day_at) // 3600))
        maps = hours.get(hour)
        if maps is None:
            maps = hours[hour] = _new_analytics_maps()
        _fold_usage_analytics(row, realm, maps, _ANALYTICS_WINDOW, pricing_on,
                              pricing=pricing)

    _, error = _scan_usage_from(
        0, fold, stop_at=stop_at,
        skip=lambda line: _line_outside_window(line, since, until),
        skip_raw=lambda raw: _line_outside_window_bytes(raw, since, until))
    if error is not None:
        _gw().log("compute_usage_analytics failed: %s" % error)
    out = _new_analytics_maps()
    for key in sorted(days):
        _day_at, hours = days[key]
        for hour in sorted(hours, key=int):
            _add_analytics_window_maps(out, hours[hour])
    return out

# ---------------------------------------------------------------------------
# 时间序列的日、小时与分钟桶
#
# usage_timeseries 本来就把行按时间装桶，但每次缓存未命中都要整段扫描。
# 窗口边界同样是本地午夜，而它的桶宽只有 3600 与 86400 两种能与窗口一致；
# span 不超过 6h 的窗口，也就是自定义区间，选择器见 _usage_timeseries_scope，
# 给出 step=60，边界是任意分钟格点，日桶与小时桶都答不了；这一档由最近
# 6 小时的分钟环回答，见 _fold_usage_series_minute。折叠时顺手维护这三层，
# 窗口就是它们的切片：每个输出桶恰好等于一个存量桶，不做跨桶相加，所以连
# 浮点求和都是逐位一致的；snapshot 与 analytics 的窗口半需要把日桶相加，
# 没有这个性质。
# ---------------------------------------------------------------------------
_series_state = {}
_series_state_lock = threading.Lock()
# credit 历史只保留最近这么多条，整段扫描返回的也是窗口内的前 50 条。
_SERIES_CREDITS_KEEP = 50
# 分钟环的保留窗口：6 小时，与选择器给出 step=60 的最大跨度 span<=6h 一一
# 对应。分钟桶比小时桶密 60 倍，跟着日桶的 32 天保留会让 checkpoint 翻好
# 几倍，而更早的窗口本来就该由小时层回答，即 step=3600；所以环只留最近
# 6 小时。保留线用桶的 at 严格小于 max_at 减窗口来判定，窗口服务条件写成
# lo >= max_at - 窗口；两者一配对，服务得上的窗口需要的桶恰好一个不落。
# 窗口起点必须落在分钟格点上，见 _series_minute_grid_ok。
_SERIES_MINUTE_WINDOW = 6 * 3600
# 折叠途中环的临时上限：超过就按保留线剪一次。剪枝复杂度随环大小线性增长，
# 而环的正常大小不超过 361，即 6 小时加两个端点；720 是给冷折时环里还堆着
# 更早的桶留的余量。这样整趟冷折的内存有界，满环约 200KB，不用等折完才剪。
_SERIES_MINUTE_RING_CAP = 720

def _new_series_bucket(at):
    """一个时间序列桶；键序与 _usage_timeseries_uncached 的输出逐字一致。"""
    return {"at": at, "requests": 0, "errors": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0,
            "total_tokens": 0, "credit": 0.0}

def _feed_series_bucket(bucket, row):
    """一行折进一个序列桶，可能是日桶或小时桶。

    口径与 _usage_timeseries_uncached 的折叠逐条一致：completed 计 requests
    与令牌，其它结果，含客户端中止，计 errors；credit 只要不是客户端中止
    就累加。
    """
    outcome = row_outcome(row)
    if outcome == "completed":
        bucket["requests"] += 1
        for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                      "cached_tokens", "total_tokens"):
            bucket[field] += (row.get(field) or 0)
    else:
        bucket["errors"] += 1
    if outcome != "client_aborted":
        bucket["credit"] += (row.get("credit") or 0)

def _series_credit_entry(at, row):
    """一条计费行的载荷：整段扫描与增量缓冲共用的字段表。

    两边的条目会出现在同一个响应的 credits 数组里，键序也是响应字节的
    一部分，所以只能有一份定义。
    """
    return {"at": at, "iso": row.get("iso") or "",
            "model": row.get("model") or "",
            "account": row.get("account") or "",
            "credit": row.get("credit") or 0,
            "total_tokens": row.get("total_tokens") or 0}

def _series_credit_push(credits, row, at):
    """把一条带 credit 的行放进最近 50 条的缓冲。

    整段扫描返回的 credits 是窗口内按 at 倒序的前 50 条；缓冲里始终留着
    最新的一批，至少 50 条，涨到 100 条时裁回 50。于是任何后缀窗口，即
    today、week 与 month，的前 50 条都在这批里：比缓冲最旧一条还旧的行
    不可能是窗口的前 50 条。at 相同时两边都是稳定排序，顺序与日志一致。
    """
    credits.append(_series_credit_entry(at, row))
    if len(credits) > _SERIES_CREDITS_KEEP * 2:
        credits.sort(key=lambda item: item.get("at") or 0, reverse=True)
        del credits[_SERIES_CREDITS_KEEP:]

def _series_minute_key(at):
    """行所属的分钟桶键：epoch 分钟，向下取整。

    键用 epoch 分钟，不取离本地午夜的秒数：分钟窗口的起点是任意分钟格点，
    也就是自定义区间的 since，不锚在本地午夜上，所以桶的格点也取绝对分钟。
    这样跨午夜、跨小时都是同一张表，切片不必按天分组。
    """
    return int(at // 60)

def _fold_usage_series_minute(state, row, at):
    """一行折进分钟环，与日桶、小时桶同一 realm 过滤、同一折叠口径。

    保留线以下的桶不建，建了也会被下一次剪枝丢掉：窗口要服务得上的行都在
    max_at 减 6h 之上，更早的行折进来只是白建一个马上被剪的桶。
    """
    key = _series_minute_key(at)
    if key * 60 < state["max_at"] - _SERIES_MINUTE_WINDOW:
        return
    minutes = state["minutes"]
    bucket = minutes.get(key)
    if bucket is None:
        bucket = minutes[key] = _new_series_bucket(key * 60)
        if len(minutes) > _SERIES_MINUTE_RING_CAP:
            _prune_series_minutes(state)
    _feed_series_bucket(bucket, row)

def _prune_series_minutes(state):
    """丢掉保留线 max_at 减 6h 以下的分钟桶。

    只按 max_at 剪，不跟日桶的 32 天剪枝线：分钟环的覆盖范围就是它自己的
    保留窗口，与日桶保留多少天无关。判定与 _fold_usage_series_minute 的
    跳过条件同一条，即桶的 at 严格小于保留线，两边不会出现建的桶马上被剪
    或剪了还需要的桶。
    """
    horizon = state["max_at"] - _SERIES_MINUTE_WINDOW
    minutes = state["minutes"]
    for key in [k for k in minutes if k * 60 < horizon]:
        del minutes[key]

def _fold_usage_series(row, r, state, day_buckets=None):
    """一行折进时间序列状态：日桶 + 小时桶 + 分钟环 + credit 缓冲。"""
    at = row.get("at") or 0
    if at > state["max_at"]:
        # 折过的最大 at：窗口上界 hi=now 只要不低于它，存量桶就都在窗口内。
        # 越界的行，例如时钟回拨或补写，会让它偏大，那只是更保守，方向安全。
        state["max_at"] = at
    bucket = _usage_day_bucket(state, row, r, _day_series_bucket, day_buckets)
    if bucket is None:
        return
    _feed_series_bucket(bucket, row)
    # 分钟环放在小时桶的午夜兜底之前：兜底会 return，那一天的 hours 被摘掉，
    # 而分钟环与日、小时格点无关，漏折一行就会让它的覆盖出现空洞。
    _fold_usage_series_minute(state, row, at)
    if at < bucket["at"]:
        # 本地午夜落在不存在的小时里，极少数时区的夏令时正好在午夜切换：
        # 这一天的行放不进小时格点，把 hours 摘掉让切片对这一天整体退回扫描；
        # 日桶本身照常，step=86400 的切片不受影响。
        bucket.pop("hours", None)
        return
    # 小时子桶按离那天的本地午夜多少秒切：窗口起点是本地午夜时，窗口序列的
    # 桶序号等于天序号乘 24 加小时序号。夏令时不在日边界上时也一样，因为
    # 两边都是纯 epoch 算术；日边界被夏令时挪动的情形由 _series_slice 的
    # 格点校验挡住。键写成字符串，与 JSON 往返后的形状一致。
    hour = str(int((at - bucket["at"]) // 3600))
    sub = bucket["hours"].get(hour)
    if sub is None:
        sub = bucket["hours"][hour] = _new_series_bucket(
            bucket["at"] + int(hour) * 3600)
    _feed_series_bucket(sub, row)
    if (row.get("credit") or 0) > 0:
        _series_credit_push(state["credits"], row, at)

def _usage_series_state_ready(r):
    """把 `r` 的时间序列状态推进到文件尾，返回 state、offset 与 error。

    调用方必须已持有 _series_state_lock。时间序列折叠不算价，所以状态只带
    realm 指纹；行没有 realm 字段时 row_realm 会读它。

    状态里的 `minutes_from` 是分钟环的完整线。从零折起或 refold 时它是
    None，环对整份日志完整；采用一份缺少分钟环的 checkpoint 时，环只对
    采用点之后折进来的行完整，那条线就是采用时的 max_at，见
    _usage_cache_adopt_series。
    """
    state = _series_state.get(r)
    if state is None:
        state = _series_state[r] = {
            "days": {}, "days_floor": None, "max_at": 0, "credits": [],
            "minutes": {}, "minutes_from": None,
            "hits": [], "hits_overflow": False,
            "offset": 0, "key": None, "realm": None, "tail": b""}
    realm = _realm_inputs()
    log_key = _usage_log_key()
    if state["key"] is None:
        # 全新状态，进程刚起来：先问 checkpoint，问不到才从零折起。
        if not _usage_cache_adopt_series(r, state, log_key, realm):
            state.update({"days": {}, "days_floor": None, "max_at": 0,
                          "credits": [], "minutes": {}, "minutes_from": None,
                          "hits": [], "hits_overflow": False,
                          "offset": 0, "tail": b""})
    elif state["realm"] != realm or not _log_resume_ok(state, log_key):
        state.update({"days": {}, "days_floor": None, "max_at": 0,
                      "credits": [], "minutes": {}, "minutes_from": None,
                      "hits": [], "hits_overflow": False,
                      "offset": 0, "tail": b""})
    state.update({"realm": realm, "key": log_key})
    day_buckets = _usage_day_buckets_enabled()
    offset, error = _scan_usage_from(
        state["offset"], lambda row: _fold_usage_series(row, r, state,
                                                        day_buckets))
    if error is None:
        state["offset"] = offset
        state["tail"] = _log_tail_signature(offset)
        # 折叠途中的剪枝按上限触发，收尾再剪一次，落盘的环就是保留窗口以内
        # 的那一批；状态不折时这里也不花时间，环大小不超过上限。
        _prune_series_minutes(state)
    return state, offset, error

def _series_output_bucket(bucket):
    """存量桶转成输出桶，键序固定；日桶的 hours 子表不进输出。"""
    return {"at": bucket["at"], "requests": bucket["requests"],
            "errors": bucket["errors"], "prompt_tokens": bucket["prompt_tokens"],
            "completion_tokens": bucket["completion_tokens"],
            "reasoning_tokens": bucket["reasoning_tokens"],
            "cached_tokens": bucket["cached_tokens"],
            "total_tokens": bucket["total_tokens"], "credit": bucket["credit"]}

def _series_minute_grid_ok(lo):
    """窗口起点能否落在分钟环的格点上：lo 必须是 60 的整数倍。

    整段扫描的桶边界是 lo 加 k 乘 60，存量分钟桶的边界是 epoch 分钟的整数
    倍；两组边界一一对应、可以整桶取用，当且仅当 lo 自己落在格点上。面板
    的 datetime-local 输入给的就是整分钟，fromLocalInputValue 只到分钟；
    手写的带小数 since 一律退回整段扫描。
    """
    return int(lo) == lo and int(lo) % 60 == 0

def _series_window_day_key(lo):
    """窗口起点 lo 的本地日键；取值超出平台 epoch 范围时返回 None。

    _local_day_key 走 time.localtime，手写的极大 since，例如 6e18，会让它
    抛出 OSError。那只是这个窗口服务不了的一种情形，按退回整段扫描处理，
    不该把端点打成 500。分钟切片拿这个键做 days_floor 覆盖判断，拿不到就
    退回扫描。
    """
    try:
        return _local_day_key(lo)
    except (OSError, OverflowError, ValueError):
        return None

def _series_closed_cut_ok(state, lo_i, hi, step):
    """闭窗口的右边界能否由存量桶回答：hi 落在桶格点上，且直到它没有
    整秒行。

    桶按 at 严格切分：日桶与小时桶锚在本地午夜，分钟环锚在 epoch 分钟。
    所以 hi 落在与桶同一条格点上时，[lo, hi] 的行与 at < hi 的桶只差恰好
    落在 hi 上的行；那一刻有整秒行时桶切不出它，只能退回整段扫描，见
    _closed_window_edge_ok。取值离谱的 hi，例如手写 URL，按不成立处理，
    退回整段扫描，与 _series_window_day_key 的取值保护同一个用意。
    """
    if not _closed_window_edge_ok(state, hi):
        return False
    if step == 60:
        try:
            return _series_minute_grid_ok(hi)
        except (OverflowError, ValueError):
            return False
    try:
        hi_i = int(hi)
    except (OverflowError, ValueError):
        return False
    if hi_i != hi:
        return False
    return (hi_i - lo_i) % step == 0

def _series_slice_minutes(state, realm, lo, hi, day_key):
    """分钟环的切片；不满足边界或覆盖条件时返回 None，退回整段扫描。

    成立条件缺一不可，任何一条不成立就退回整段扫描，优先保证正确：
      * 窗口起点是分钟格点，见 _series_minute_grid_ok；
      * hi 不低于折过的最大 at，否则窗口上界会切进桶里，而桶是聚合值，切
        不出一半来，与日切片、小时切片同一条。hi 切进数据里时属于闭窗口，
        还要过闭窗口闸门，见 _series_closed_cut_ok：hi 落在分钟格点上，且
        直到那一刻没有整秒行，才按 at < hi 的桶切；恰好落在 hi 上的行由
        边沿账挡回整段扫描；
      * 窗口起点不早于 max_at 减 6h，这是环的保留线，更早的窗口环里没有桶；
      * 窗口起点晚于 minutes_from，环只从那条线之后才完整；采用过缺少分钟
        环的 checkpoint 时才有这条线，见 _usage_series_state_ready；
      * 每个桶的 at 落在键乘 60 上且不晚于 max_at，这是形状校验之外的防御，
        与 _series_slice 对小时子桶的格点校验同一个用意。

    输出桶与整段扫描逐条相同，即同一批行、同一个网格、同一个累加口径；
    credits 与日切片、小时切片共用下面同一段选择。
    """
    minutes = state.get("minutes")
    if not minutes:
        # 空环，或状态里没有环：这一份状态没有任何行落在保留窗口里。空环
        # 答不出窗口里的行，也不能把窗口静默答成空序列；退回整段扫描，由
        # 扫描给出真值，正确的空序列也由它给。
        return None
    lo_i = int(lo)
    cut = False
    if hi < state["max_at"]:
        # 上界切进环里：闭窗口闸门不成立就退回整段扫描，结果不变。
        if not _series_closed_cut_ok(state, lo_i, hi, 60):
            return None
        cut = True
    if not _series_minute_grid_ok(lo):
        return None
    if lo_i < state["max_at"] - _SERIES_MINUTE_WINDOW:
        return None
    minutes_from = state.get("minutes_from")
    if minutes_from is not None and lo_i <= minutes_from:
        return None
    if day_key is None:
        # 直接调用时由测试补上；取值离谱，见 _series_window_day_key，就退回。
        day_key = _series_window_day_key(lo)
        if day_key is None:
            return None
    if not _days_cover(state, day_key):
        return None
    series = []
    for key in sorted(minutes):
        sub = minutes[key]
        at = int(sub["at"])
        if at != key * 60 or at > state["max_at"]:
            return None
        if at < lo_i:
            continue
        if cut and at >= hi:
            # 闭窗口：hi 那一刻归后一桶，桶按 at 严格切分；只取 at < hi 的
            # 桶。那一刻有整秒行时上面已经退回。
            break
        out = _series_output_bucket(sub)
        # 整段扫描的桶 at 是 lo 加 k 乘 60，类型跟着 lo 走，可能是 int 或
        # float；存量桶的 at 是整数，这里按 lo 的类型处理，两边的 JSON 字节
        # 才一致。
        out["at"] = int(at) if isinstance(lo, int) else float(at)
        series.append(out)
    return _series_payload(state, realm, lo, hi, 60, series)

def _series_payload(state, realm, lo, hi, step, series):
    """切片载荷的公共部分：窗口头与 credit 历史。

    credit 历史：缓冲按 at 倒序稳定排序后取窗口内的前 50 条，与整段扫描的
    选择逐条相同，见 _series_credit_push 的论证。窗口的两端都要过滤：缓冲
    收的是整份日志的计费行；闭窗口的上界切进缓冲里时 hi < max_at，at > hi
    的行不属于窗口，整段扫描不会收它们，切片也必须裁掉。
    """
    credits = sorted(state["credits"], key=lambda item: item.get("at") or 0,
                     reverse=True)
    credits = [c for c in credits
               if lo <= (c.get("at") or 0) <= hi][:_SERIES_CREDITS_KEEP]
    return {"ok": True, "realm": realm or "all",
            "bucket_seconds": step, "since": lo, "until": hi,
            "series": series, "credits": credits}

def _series_slice(state, realm, lo, hi, step, day_key):
    """窗口序列等于日桶、小时桶或分钟环的切片；不满足边界条件时返回 None。

    成立条件缺一不可，任何一条不成立就退回整段扫描，优先保证正确：
      * 窗口起点是本地午夜，day_key 由 _window_day_key 给出；
      * 桶宽是 3600 或 86400，即与窗口一致的两种；
      * days_floor 覆盖窗口起点，剪枝或更早的天没折过就只能整段扫；
      * hi 不低于折过的最大 at，否则窗口上界会切掉桶里的行。hi 切进数据里
        时属于闭窗口，还要过闭窗口闸门，见 _series_closed_cut_ok：hi 落在
        同一套桶格点上，且直到那一刻没有整秒行，才按 at < hi 的桶切；恰好
        落在 hi 上的行由边沿账挡回整段扫描；
      * 每个桶的 at 落在 lo 加 k 乘 step 的格点上。这一条同时挡住夏令时：
        日长不是 86400 秒的那天之后，每一天的本地午夜都不再落在 86400 的
        格点上，于是自动退回整段扫描。时区没有夏令时则恒真，例如 cn。

    桶宽 60 走分钟环，条件见 _series_slice_minutes；它不要求窗口起点是本
    地午夜，只要求落在分钟格点上。
    """
    if step == 60:
        return _series_slice_minutes(state, realm, lo, hi, day_key)
    if step not in (3600, 86400):
        return None
    lo_i = int(lo)
    cut = False
    if hi < state["max_at"]:
        # 上界切进桶里：闭窗口闸门不成立就退回整段扫描，结果不变。
        if not _series_closed_cut_ok(state, lo_i, hi, step):
            return None
        cut = True
    if not _days_cover(state, day_key):
        return None
    series = []
    done = False
    for key in sorted(state["days"]):
        if key < day_key or done:
            continue
        bucket = state["days"][key]
        day_at = int(bucket["at"])
        if (day_at - lo_i) % 86400:
            return None
        if step == 86400:
            if cut and day_at >= hi:
                # 闭窗口：hi 那一刻归后一桶（桶按 at 严格切分），只取
                # at < hi 的桶；那一刻有整秒行时上面已经退回。这一天及其后
                # 的桶都在窗口外，done 把循环收口。
                done = True
                continue
            series.append(_series_output_bucket(bucket))
            continue
        hours = bucket.get("hours")
        if hours is None:
            # 这一天的小时格点不成立（见 _fold_usage_series 的午夜兜底）：
            # 整个窗口退回整段扫描，不能只跳过这一天。
            return None
        for hour in sorted(hours, key=int):
            sub = hours[hour]
            if int(sub["at"]) != day_at + int(hour) * 3600:
                return None
            if (int(sub["at"]) - lo_i) % 3600:
                return None
            if cut and int(sub["at"]) >= hi:
                # 闭窗口的右边界，放在格点校验之后：错开的桶要先退回扫描。
                done = True
                break
            series.append(_series_output_bucket(sub))
    return _series_payload(state, realm, lo, hi, step, series)

def _top_models(bucket, top_n=KEY_MODEL_TOP_N):
    """把一个模型桶拆成最忙的 N 个模型和一条剩余项。

    剩余项用 `other` 标记识别，不靠名称判断，所以真的叫「(其他)」的模型
    不会被误认成剩余项。
    """
    items = sorted(bucket.items(), key=lambda kv: (-kv[1]["tokens"], -kv[1]["requests"], kv[0]))
    head = [{"model": mid, "requests": s["requests"], "tokens": s["tokens"],
             "reasoning": s["reasoning"]} for mid, s in items[:top_n]]
    rest = items[top_n:]
    other = None
    if rest:
        other = {
            "model": "(其他)",
            "other": True,
            "models": len(rest),
            "requests": sum(s["requests"] for _, s in rest),
            "tokens": sum(s["tokens"] for _, s in rest),
            "reasoning": sum(s["reasoning"] for _, s in rest),
        }
    return head, other

def _build_key_rows(key_map, realm=None):
    """把观察到的每 key 流量与配置里的 key 清单合并成表格行。

    面板还能看到的每个 key 都有一行，窗口内没有流量也一样：昨天用过、今天
    没用的 key 是今天支出的一部分事实，删掉这行会被读成 key 消失了。禁用且
    在窗口内空闲的 key 会被丢掉，因为这一行说明不了所选区间的任何事。

    反方向同样重要：日志里出现而清单不认识的 id 也要有一行，这样每 key 表
    永远与每账号表对得上。这些 id 合并成单独一行，无法命名的 id 是异常，
    不值得单独拆成一个维度。
    """
    key_map = key_map or {}
    rows = {}

    def empty_row(k_id, name, declared_realm, enabled, source):
        return {
            "key": k_id,
            "name": name,
            "realm": declared_realm,
            "enabled": enabled,
            "source": source,
            "window": _new_analytics_stat(),
            "all_time": _new_analytics_stat(),
            "window_models": {},
            "all_models": {},
            "realms": {},
            "last_at": 0,
        }

    for entry in wb_settings.api_keys(_gw().ACCOUNTS_DIR, include_deleted=True):
        k_id = entry.get("id") or ""
        if not k_id:
            continue
        declared = entry.get("realm") or ""
        # 绑定到另一出口的 key 在这个视图里不可能有行。
        if realm and declared and declared != realm:
            continue
        deleted = bool(entry.get("deleted_at"))
        rows[k_id] = empty_row(
            k_id,
            "已删除的 Key" if deleted else (entry.get("name") or k_id),
            declared,
            False if deleted else entry.get("enabled", True) is not False,
            "deleted" if deleted else "panel")
    # 启动参数 key 由 --api-key 或 _gw().API_KEY 提供，不在任何设置文件里，所以
    # 只在日志里以 id 形式出现。它不是归因维度：面板一旦有自己的 key，启动
    # 参数 key 就不再被接受，见 identify_key；未使用的那一行是永久的零，
    # 说明不了谁花了多少。只有日志显示它确实被用过时才给它一行，那是它唯一
    # 值得对账的历史。
    if "launcher" not in rows and "launcher" in key_map:
        rows["launcher"] = empty_row("launcher", "启动参数", "", True, "launcher")
    if "panel" in key_map:
        rows["panel"] = empty_row("panel", "面板测试台", "", False, "panel")

    def adopt(row, km):
        row["window"] = km["window"]
        row["all_time"] = km["all_time"]
        row["window_models"] = km["window_models"]
        row["all_models"] = km["all_models"]
        row["realms"] = km["realms"]
        row["last_at"] = km["last_at"]

    unknown = None
    for k_id, km in key_map.items():
        if k_id == KEY_BUCKET_BEFORE:
            rows[k_id] = empty_row(k_id, "(切换前)", "", False, "bucket")
            adopt(rows[k_id], km)
        elif k_id == KEY_BUCKET_ANON:
            rows[k_id] = empty_row(k_id, "(无 key)", "", False, "bucket")
            adopt(rows[k_id], km)
        elif k_id in rows:
            adopt(rows[k_id], km)
        else:
            if unknown is None:
                unknown = rows[KEY_BUCKET_UNKNOWN] = empty_row(
                    KEY_BUCKET_UNKNOWN, "(未知 key)", "", False, "bucket")
            _add_analytics_stat(unknown["window"], km["window"])
            _add_analytics_stat(unknown["all_time"], km["all_time"])
            for tgt, src in ((unknown["window_models"], km["window_models"]),
                             (unknown["all_models"], km["all_models"])):
                for mid, s in src.items():
                    dst = tgt.setdefault(mid, {"requests": 0, "tokens": 0, "reasoning": 0})
                    dst["requests"] += s["requests"]
                    dst["tokens"] += s["tokens"]
                    dst["reasoning"] += s["reasoning"]
            for k_realm, count in km["realms"].items():
                unknown["realms"][k_realm] = unknown["realms"].get(k_realm, 0) + count
            unknown["last_at"] = max(unknown["last_at"], km["last_at"])

    out = []
    for row in rows.values():
        if (row["source"] == "panel" and not row["enabled"]
                and not row["window"]["requests"] and not row["window"]["errors"]):
            continue
        _finalize_analytics_stat(row["window"])
        _finalize_analytics_stat(row["all_time"])
        row["models"], row["models_other"] = _top_models(row["window_models"])
        # 没有 realm 绑定的 key 跟随它被请求的模型，两个出口都可能服务——它的
        # 积分列于是把两种产品的价格加在一起。页面必须能把这一点说出来。
        row["cross_realm"] = len([x for x in row["realms"] if x]) > 1
        row["key_id"] = row["key"]
        row["key_name"] = row["name"]
        out.append(row)
    out.sort(key=lambda r: (-r["window"]["total_tokens"], -r["all_time"]["total_tokens"], r["name"]))
    return out

def _enrich_accounts_from_pool(acct_map, realm=None):
    """为没有流量的账号补上 nickname/realm/credits。"""
    if _gw().POOL:
        for a in _gw().POOL.accounts:
            if realm and a.realm != realm:
                continue
            if a.uid in acct_map:
                acct_map[a.uid]["nickname"] = a.nickname
                acct_map[a.uid]["realm"] = a.realm
                acct_map[a.uid]["domain"] = a.domain
                acct_map[a.uid]["credits"] = getattr(a, "credits", None) or {}
            else:
                    acct_map[a.uid] = {
                        "uid": a.uid,
                        "nickname": a.nickname,
                        "realm": a.realm,
                        "domain": a.domain,
                        "credits": getattr(a, "credits", None) or {},
                        "window": _new_analytics_stat(),
                        "all_time": _new_analytics_stat(),
                        "window_models": {},
                        "all_models": {},
                    }

def _finalize_analytics_stat(stat_obj):
        p = stat_obj["prompt_tokens"]
        c = stat_obj["cached_tokens"]
        out = stat_obj["completion_tokens"]
        reas = stat_obj["reasoning_tokens"]
        stat_obj["cache_hit_pct"] = round((c / p * 100), 1) if p > 0 else 0.0
        stat_obj["reasoning_ratio"] = round((reas / out * 100), 1) if out > 0 else 0.0
        stat_obj["ttft_ms_avg"] = round(stat_obj["ttft_sum"] / stat_obj["ttft_n"]) if stat_obj["ttft_n"] > 0 else 0
        stat_obj["speed_avg"] = round(stat_obj["speed_sum"] / stat_obj["speed_n"], 1) if stat_obj["speed_n"] > 0 else 0.0
        stat_obj["elapsed_ms_avg"] = round(stat_obj["elapsed_sum"] / stat_obj["elapsed_n"]) if stat_obj["elapsed_n"] > 0 else 0
        return stat_obj

def _compute_usage_analytics_uncached(realm=None, since=None, until=None):
    """Token、Cache 与 Reasoning 指标页的详细分析。"""
    # 与 /usage 快照同一条规矩：一次扫描里总开关只问一次，值传进折叠与指纹。
    pricing_on = wb_pricing.pricing_enabled()
    all_maps, offset = _analytics_all_time(realm, pricing_on)
    if since is None and until is None:
        # 开放窗口覆盖所有行，窗口半就是 all-time 折叠的再走一遍；合并时把
        # 两份拷开。
        win_maps = all_maps
    else:
        # all-time 半不重折：窗口半取自折叠推进出的日桶（取不到时，来自一轮
        # 停在该折叠停点的扫描），一页绝不混用两次读取。
        win_maps = _analytics_window(realm, since, until, offset, pricing_on)
    parts = _merge_analytics_halves(all_maps, win_maps)
    acct_map = parts["accts"]
    model_map = parts["models"]
    key_map = parts["keys"]
    _enrich_accounts_from_pool(acct_map, realm=realm)
    all_summary = parts["summary"]
    window_summary = parts["window_summary"]
    _finalize_analytics_stat(all_summary)
    _finalize_analytics_stat(window_summary)
    for a in acct_map.values():
        _finalize_analytics_stat(a["window"])
        _finalize_analytics_stat(a["all_time"])
    for m in model_map.values():
        _finalize_analytics_stat(m["window"])
        _finalize_analytics_stat(m["all_time"])
    accts_list = sorted(acct_map.values(), key=lambda a: (-a["window"]["total_tokens"], -a["all_time"]["total_tokens"]))
    models_list = sorted(model_map.values(), key=lambda m: (-m["window"]["total_tokens"], -m["all_time"]["total_tokens"]))
    keys_list = _build_key_rows(key_map, realm=realm)
    return {
        # 解析后的窗口随载荷一起下发，页面第一列按服务端实际生效的窗口标注，
        # 不按面板以为发出的。
        "window": {"since": since, "until": until},
        "realm": realm or "all",
        # 成本数字是人民币；面板按这个汇率折算显示美元。
        "usd_cny": wb_pricing.usd_cny(),
        "summary": {"window": window_summary, "all_time": all_summary},
        "accounts": accts_list,
        "models": models_list,
        # 同样的行，只是折叠维度从服务调用的上游账号换成调用网关的 API Key。
        # 一个 key 可以由多个账号服务，一个账号也可以服务多个 key，这是同一笔
        # 花费的第二个视图，并不是把花费拆开。
        "keys": keys_list,
    }

wb_accounts.set_credit_events_provider(credit_events_file)
