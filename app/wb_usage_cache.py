"""聚合状态 checkpoint 的读取、校验与写盘。

checkpoint 只是加速：读写失败一律静默降级。网关运行态每次现读，测试替换
网关属性后立即生效。
"""
import hashlib
import json
import os
import threading
import time

import wb_settings

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


def _host():
    """用量记账模块 wb_usage；属性每次现读。"""
    import wb_usage as module
    return module

# checkpoint 文件与日志同生命周期，跟着 sysupgrade 一起走。
# 数据目录下的文件名。usage/*.json 已被 .gitignore 覆盖。
# 带 <kind> 的是 v4 布局（一类一份）；不带的是升级前的单文件，只读不写。
_USAGE_CACHE_NAME = "usage-aggregate-cache.json"
_USAGE_CACHE_KIND_NAME = "usage-aggregate-cache-%s.json"
# 状态结构或折叠口径一变就 +1，旧文件整体作废：键顺序算结构的一部分。v2 写盘
# 按插入顺序；v3 加按日分桶与日/小时时间序列；v4 一类一份文件（快照/analytics 的
# 日桶带小时子表、series 带分钟环）；v5 "pricing" 记录折叠行真正用到的价（T15）。
# 形状对不上就整份作废：升级后第一次刷新全量重折一次，之后 checkpoint 照旧；
# 价表再变只按「用到的价」判废。升级前的单文件是 v3，只读兼容，作为各类还没有
# 自己文件时的回退来源。
_USAGE_CACHE_SCHEMA = 5
# 升级前唯一布局的版本号：v3 单文件。只出现在只读回退路径上。
_USAGE_CACHE_LEGACY_SCHEMA = 3
_USAGE_CACHE_KINDS = ("snapshot", "by_account", "analytics", "series",
                      "remaining")

def _usage_cache_enabled():
    """落盘总开关，默认开；每次调用都现读设置，现场切换下个刷新周期生效。"""
    return wb_settings.usage_cache(_gw().ACCOUNTS_DIR)

def _usage_cache_path():
    """升级前单文件（v3）的路径。每次调用重算，不能定成常量：--usage-dir、测试
    与探针脚本在导入之后才改 _gw().USAGE_DIR。
    """
    return os.path.join(_gw().USAGE_DIR, _USAGE_CACHE_NAME)

def _usage_cache_kind_path(kind):
    return os.path.join(_gw().USAGE_DIR, _USAGE_CACHE_KIND_NAME % kind)

def _usage_cache_min_bytes():
    """写盘节流阈值，默认 1MB：等于重启后最多回放的量（路由器扫描约 0.5 s/MB）。
    日志每天涨约 6 MB；写小了费 flash 寿命，写大了拖慢重启。
    """
    try:
        return max(0, int(os.environ.get("WB_USAGE_CACHE_MIN_BYTES", 1024 * 1024)))
    except (TypeError, ValueError):
        return 1024 * 1024

def _usage_cache_min_seconds():
    """写盘节流：距上次尝试不足这么多秒就不写，默认 900 s。兜住「字节阈值没到、
    offset 已推进」的日志，一天最多约百次写盘；重启回放上限是 min（1MB, 15 分钟流量）。
    """
    try:
        return max(0.0, float(os.environ.get("WB_USAGE_CACHE_MIN_SECONDS", 900)))
    except (TypeError, ValueError):
        return 900.0

_usage_cache_lock = threading.Lock()
# 写盘串行锁：只被 _usage_cache_maybe_save() 拿一次且永远最外层，不参与锁序环。
_usage_cache_write_lock = threading.Lock()
# 按「类」只读一次 checkpoint，读不到也记 None：坏文件、只读文件系统不该让每次
# 刷新都解析一遍。v3 单文件同样只读一次，作为缺文件的类的回退来源。
_usage_cache_kind_cache = {}
_USAGE_CACHE_LEGACY_UNREAD = object()
_usage_cache_legacy_data = _USAGE_CACHE_LEGACY_UNREAD
# 节流状态，全部由 _usage_cache_lock 保护：progress 是本进程见过的最远 offset，
# checkpointed 是上次「尝试」落盘的 offset（失败也记，只读文件系统上不能每次刷新
# 都试写），last_attempt 是上次尝试时刻。按类记账：刚采用过 checkpoint 的类不能被
# 别的类的推进带着白写。
_usage_cache_progress = {}
_usage_cache_checkpointed = {}
_usage_cache_last_attempt = time.time()

def _usage_cache_forget():
    """忘掉本进程读过的 checkpoint（模拟重启；测试与探针用）。

    调用点不得持有状态锁：本函数自己取 _usage_cache_lock（与读取路径同一把）。
    """
    global _usage_cache_legacy_data
    with _usage_cache_lock:
        _usage_cache_kind_cache.clear()
        _usage_cache_legacy_data = _USAGE_CACHE_LEGACY_UNREAD

def _usage_cache_kind_entries(kind):
    """某一类状态的条目列表（读一次、按类记忆）；没有可用来源时返回 None。

    本类文件缺失或读不出时回退到 v3 单文件里对应的那一节；本类文件一旦存在
    就是唯一来源。调用点可能在状态锁内，本函数取 _usage_cache_lock；反向锁序
    不存在，写盘路径取状态锁前一定先放开本锁。
    """
    if not _usage_cache_enabled():
        return None
    with _usage_cache_lock:
        if kind not in _usage_cache_kind_cache:
            entries = _usage_cache_read_kind(kind)
            if entries is None:
                entries = _usage_cache_read_legacy_kind(kind)
            _usage_cache_kind_cache[kind] = entries
        return _usage_cache_kind_cache[kind]

def _usage_cache_read_kind(kind):
    """读一类的 checkpoint 文件并做顶层校验；任何一步不成立都返回 None。

    None 与「文件不存在」同义，调用方按没有缓存处理，绝不报错。调用方持有
    _usage_cache_lock。
    """
    try:
        with open(_usage_cache_kind_path(kind), "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # 先看类型再看值：JSON 的 true 在 Python 里等于 1，不能算「版本 1」。
    if not _usage_cache_int(data.get("schema")):
        return None
    if data["schema"] != _USAGE_CACHE_SCHEMA:
        return None
    # kind 标签是双保险：文件被复制/改名成另一类的名字时，宁可当没有缓存。
    if data.get("kind") != kind:
        return None
    entries = data.get("entries")
    if not isinstance(entries, list):
        return None
    return entries

def _usage_cache_read_legacy_kind(kind):
    """v3 单文件里某一类的条目，或 None；文件只解析一次。

    调用方持有 _usage_cache_lock。条目取过即删（采用路径 pop），与 v3 的用法一致。
    """
    global _usage_cache_legacy_data
    if _usage_cache_legacy_data is _USAGE_CACHE_LEGACY_UNREAD:
        _usage_cache_legacy_data = _usage_cache_read_legacy_file()
    data = _usage_cache_legacy_data
    if not isinstance(data, dict):
        return None
    entries = data.get(kind)
    if not isinstance(entries, list):
        return None
    return entries

def _usage_cache_read_legacy_file():
    """v3 单文件的解析结果，或 None；只做顶层校验（对象 + schema 恰好是 3）。

    不校验 kind 字段：v3 文件没有这个字段，各类小节就是顶层键。
    """
    try:
        with open(_usage_cache_path(), "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not _usage_cache_int(data.get("schema")):
        return None
    if data["schema"] != _USAGE_CACHE_LEGACY_SCHEMA:
        return None
    return data

_usage_cache_digest_memo = {}

def _object_digest(obj):
    """一段 JSON 内容摘要，按对象身份记忆；算不出来返回 None。

    记忆表强引用对象，id() 不会回收复用；超过 8 条整体清空，给长期运行兜底。
    """
    hit = _usage_cache_digest_memo.get(id(obj))
    if hit is not None and hit[0] is obj:
        return hit[1]
    try:
        payload = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha1(payload).hexdigest()
    except Exception:
        return None
    if len(_usage_cache_digest_memo) > 8:
        _usage_cache_digest_memo.clear()
    _usage_cache_digest_memo[id(obj)] = (obj, digest)
    return digest

def _realm_inputs_key(inputs):
    if inputs is None:
        return None
    realm, accounts = inputs
    return [realm, [[uid, r] for uid, r in accounts]]

# 折叠会就地累加的字段集合从宿主构造函数推导：宿主加字段时这里自动跟着收，
# 漏一个就会在折叠半路抛 KeyError、报出半份数字。构造函数在 wb_usage 里，
# 导入期不可用，首次校验时求值并记住。
_NUMBERS = {}


def _usage_cache_numbers(kind):
    host = _host()
    hit = _NUMBERS.get(kind)
    if hit is None:
        if kind == "snapshot":
            hit = tuple(k for k, v in host._empty_stats().items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool))
        elif kind == "analytics":
            hit = tuple(host._new_analytics_stat().keys())
        else:
            hit = tuple(host._new_series_bucket(0).keys())
        _NUMBERS[kind] = hit
    return hit

_USAGE_CACHE_SNAPSHOT_MAPS = ("cost_missing", "by_model", "by_model_realm", "by_model_acct")
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                "cached_tokens", "total_tokens", "credit")

_USAGE_CACHE_BUCKET_NUMBERS = ("requests", "cost_cny") + USAGE_FIELDS

def _usage_cache_number(value):
    """折叠累加的数字。bool 是 int 的子类，必须挡掉。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)

def _usage_cache_int(value):
    """严格整数：JSON 里写成 12345.0 的不算。"""
    return isinstance(value, int) and not isinstance(value, bool)

def _usage_cache_counts_ok(counts):
    if not isinstance(counts, dict):
        return False
    for key, value in counts.items():
        if not isinstance(key, str) or not _usage_cache_number(value):
            return False
    return True

def _usage_cache_bucket_ok(bucket):
    if not isinstance(bucket, dict):
        return False
    for field in _USAGE_CACHE_BUCKET_NUMBERS:
        if not _usage_cache_number(bucket.get(field)):
            return False
    return _usage_cache_counts_ok(bucket.get("accounts"))

def _usage_cache_snapshot_bucket_ok(snap):
    if not isinstance(snap, dict):
        return False
    for field in _usage_cache_numbers("snapshot"):
        if not _usage_cache_number(snap.get(field)):
            return False
    for field in _USAGE_CACHE_SNAPSHOT_MAPS:
        if not isinstance(snap.get(field), dict):
            return False
    if not _usage_cache_counts_ok(snap["cost_missing"]):
        return False
    for _, bucket in snap["by_model"].items():
        if not _usage_cache_bucket_ok(bucket):
            return False
    for _, realms in snap["by_model_realm"].items():
        if not isinstance(realms, dict):
            return False
        for _, bucket in realms.items():
            if not _usage_cache_bucket_ok(bucket):
                return False
    for _, realms in snap["by_model_acct"].items():
        if not isinstance(realms, dict):
            return False
        for _, accts in realms.items():
            if not isinstance(accts, dict):
                return False
            for _, bucket in accts.items():
                if not _usage_cache_bucket_ok(bucket):
                    return False
    return True

def _usage_cache_snapshot_ok(snap):
    """窗口半快照桶 + 可能挂着的小时子表（表只挂在日桶上）。

    小时键是数字字符串（折叠侧一开始就写 str（序号），与 JSON 往返后一致）；
    子桶带 at（格点校验要用），不嵌表。
    """
    if not _usage_cache_snapshot_bucket_ok(snap):
        return False
    hours = snap.get("hours")
    if hours is None:
        return True
    if not isinstance(hours, dict):
        return False
    for hour, sub in hours.items():
        if not (isinstance(hour, str) and hour.isdigit()):
            return False
        if not isinstance(sub, dict) or "hours" in sub:
            return False
        if not _usage_cache_number(sub.get("at")):
            return False
        if not _usage_cache_snapshot_bucket_ok(sub):
            return False
    return True

def _usage_cache_stat_ok(stat):
    if not isinstance(stat, dict):
        return False
    for field in _usage_cache_numbers("analytics"):
        if not _usage_cache_number(stat.get(field)):
            return False
    return True

def _usage_cache_models_ok(models):
    if not isinstance(models, dict):
        return False
    for mid, stat in models.items():
        if not isinstance(mid, str) or not isinstance(stat, dict):
            return False
        for field in ("requests", "tokens", "reasoning", "cost_cny"):
            if not _usage_cache_number(stat.get(field)):
                return False
    return True

def _usage_cache_analytics_window_ok(maps):
    """窗口形状的 analytics maps（日桶/小时桶），装 window 那一半。

    与 all-time 形状是两份，不能互相顶替：用 all-time 的校验器会把整份 entry
    拒掉，窗口就退回整段扫描。
    """
    if not isinstance(maps, dict):
        return False
    if not _usage_cache_stat_ok(maps.get("summary")):
        return False
    accts, models, keys = maps.get("accts"), maps.get("models"), maps.get("keys")
    if not isinstance(accts, dict) or not isinstance(models, dict) or not isinstance(keys, dict):
        return False
    for uid, entry in accts.items():
        if not isinstance(uid, str) or not isinstance(entry, dict):
            return False
        for field in ("nickname", "realm", "domain"):
            if not isinstance(entry.get(field), str):
                return False
        if not _usage_cache_stat_ok(entry.get("window")):
            return False
        if not _usage_cache_models_ok(entry.get("window_models")):
            return False
    for mid, entry in models.items():
        if not isinstance(mid, str) or not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("model"), str):
            return False
        if not _usage_cache_stat_ok(entry.get("window")):
            return False
    for kid, entry in keys.items():
        if not isinstance(kid, str) or not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("key"), str):
            return False
        if not _usage_cache_stat_ok(entry.get("window")):
            return False
        if not _usage_cache_models_ok(entry.get("window_models")):
            return False
        if not _usage_cache_counts_ok(entry.get("realms")):
            return False
        if not _usage_cache_number(entry.get("last_at")):
            return False
    return True

def _usage_cache_analytics_hours_ok(hours):
    """analytics 日桶的小时子表：{小时键: 窗口形状 maps + at}，形状同时间序列。"""
    if not isinstance(hours, dict):
        return False
    for hour, sub in hours.items():
        if not (isinstance(hour, str) and hour.isdigit()):
            return False
        if not isinstance(sub, dict) or "hours" in sub:
            return False
        if not _usage_cache_number(sub.get("at")):
            return False
        if not _usage_cache_analytics_window_ok(sub):
            return False
    return True

def _usage_cache_analytics_day_ok(bucket):
    """analytics 的日桶：窗口形状 maps + at（本地午夜）+ 可选的 hours 子表。

    hours 缺席合法：剪枝线会摘掉保留窗口之外旧天的小时表，按日边界的窗口照旧
    回答，整点窗口由求和侧退回整段扫描。at 必须有：旧格式的日桶没有它，在这里
    被拒、升级后一次性重折，否则剪枝线天天判废整份状态。
    """
    if not isinstance(bucket, dict):
        return False
    if not _usage_cache_number(bucket.get("at")):
        return False
    hours = bucket.get("hours")
    if hours is not None and not _usage_cache_analytics_hours_ok(hours):
        return False
    return _usage_cache_analytics_window_ok(bucket)

def _usage_cache_analytics_ok(maps):
    if not isinstance(maps, dict):
        return False
    if not _usage_cache_stat_ok(maps.get("summary")):
        return False
    accts, models, keys = maps.get("accts"), maps.get("models"), maps.get("keys")
    if not isinstance(accts, dict) or not isinstance(models, dict) or not isinstance(keys, dict):
        return False
    for uid, entry in accts.items():
        if not isinstance(uid, str) or not isinstance(entry, dict):
            return False
        for field in ("nickname", "realm", "domain"):
            if not isinstance(entry.get(field), str):
                return False
        if not _usage_cache_stat_ok(entry.get("all_time")):
            return False
        if not _usage_cache_models_ok(entry.get("all_models")):
            return False
    for mid, entry in models.items():
        if not isinstance(mid, str) or not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("model"), str):
            return False
        if not _usage_cache_stat_ok(entry.get("all_time")):
            return False
    for kid, entry in keys.items():
        if not isinstance(kid, str) or not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("key"), str):
            return False
        if not _usage_cache_stat_ok(entry.get("all_time")):
            return False
        if not _usage_cache_models_ok(entry.get("all_models")):
            return False
        if not _usage_cache_counts_ok(entry.get("realms")):
            return False
        if not _usage_cache_number(entry.get("last_at")):
            return False
    return True

def _usage_cache_buckets_ok(buckets):
    if not isinstance(buckets, dict):
        return False
    for key, bucket in buckets.items():
        if not isinstance(key, str) or not isinstance(bucket, dict):
            return False
        if not isinstance(bucket.get("account"), str):
            return False
        for field in ("requests", "prompt_tokens", "completion_tokens",
                      "reasoning_tokens", "cached_tokens", "total_tokens"):
            if not _usage_cache_number(bucket.get(field)):
                return False
        if not _usage_cache_counts_ok(bucket.get("models")):
            return False
    return True

def _usage_cache_remaining_ok(entry):
    """剩余用量折叠：事件表 + 每个（账号, 模型）的 at/cum 平行列表。

    cum 必须单调（回退=坏数据）且与 at 等长，base 不得超过 cum[0]：坏一条就
    会静默算出错数字，所以逐元素校验。at 不要求有序：写入侧是请求完成时刻，
    多线程下会回退几毫秒（真机日志与路由器 checkpoint 都踩过），加载时由
    _pair_repair 稳定排序。
    """
    host = _host()
    if not isinstance(entry, dict):
        return False
    if not _usage_cache_int(entry.get("offset")) or entry.get("offset") <= 0:
        return False
    if not isinstance(entry.get("tail"), str):
        return False
    key = entry.get("key")
    if not isinstance(key, list) or len(key) != 3 or not all(
            _usage_cache_int(v) for v in key):
        return False
    file_pos = entry.get("file")
    if not isinstance(file_pos, dict) or not _usage_cache_int(file_pos.get("offset")):
        return False
    if not isinstance(file_pos.get("tail"), str):
        return False
    file_key = file_pos.get("key")
    if not isinstance(file_key, list) or len(file_key) != 3 or not all(
            _usage_cache_int(v) for v in file_key):
        return False
    events = entry.get("events")
    if not isinstance(events, list) or len(events) > host._REMAINING_MAX_EVENTS:
        return False
    for event in events:
        if not isinstance(event, dict):
            return False
        if not isinstance(event.get("account"), str) or not isinstance(event.get("model"), str):
            return False
        for field in ("at", "reset", "sample"):
            if not _usage_cache_number(event.get(field)):
                return False
    usage = entry.get("usage")
    if not isinstance(usage, dict):
        return False
    for pair_key, pair in usage.items():
        if (not isinstance(pair_key, str) or "\x1f" not in pair_key
                or not isinstance(pair, dict)):
            return False
        at = pair.get("at")
        cum = pair.get("cum")
        if not isinstance(at, list) or not isinstance(cum, list) or len(at) != len(cum):
            return False
        if not _usage_cache_int(pair.get("head")) or not _usage_cache_int(pair.get("base")):
            return False
        if pair["head"] < 0 or pair["head"] > len(at) or pair["base"] < 0:
            return False
        for value in at:
            if not _usage_cache_number(value):
                return False
        last_cum = None
        for value in cum:
            if not _usage_cache_int(value):
                return False
            if last_cum is not None and value < last_cum:
                return False
            last_cum = value
        # base 是「保留区之前那段的累计和」，不可能超过 cum[0]，超过必须整份
        # 拒绝——否则左端落在保留区之前的区间求和会算出负数（真机面板显示过 -1293.5M）。
        if cum and pair["base"] > cum[0]:
            return False
    return True

def _usage_cache_day_key_ok(key):
    """日键必须是 "YYYY-MM-DD"：窗口与日桶的比较靠的就是这个形状。"""
    return (isinstance(key, str) and len(key) == 10 and key[4] == "-"
            and key[7] == "-")

def _usage_cache_days_ok(days, floor, bucket_ok):
    """按日分桶的表 {日键: 桶}；floor 是「对哪些天完整」的下界（日键或 None）。

    键不是日期的桶永远不会被窗口选中，说明状态不是我们写出来的，直接拒绝。
    """
    if not isinstance(days, dict):
        return False
    if floor is not None and not _usage_cache_day_key_ok(floor):
        return False
    for key, bucket in days.items():
        if not _usage_cache_day_key_ok(key) or not bucket_ok(bucket):
            return False
    return True

def _usage_cache_series_bucket_ok(bucket):
    """时间序列桶：9 个数字；日桶还带 hours 子表（小时键是数字字符串）。"""
    if not isinstance(bucket, dict):
        return False
    for field in _usage_cache_numbers("series"):
        if not _usage_cache_number(bucket.get(field)):
            return False
    hours = bucket.get("hours")
    if hours is None:
        return True
    if not isinstance(hours, dict):
        return False
    for hour, sub in hours.items():
        if not (isinstance(hour, str) and hour.isdigit()):
            return False
        if not isinstance(sub, dict) or "hours" in sub:
            return False
        for field in _usage_cache_numbers("series"):
            if not _usage_cache_number(sub.get(field)):
                return False
    return True

def _usage_cache_series_minutes_ok(rows):
    """分钟环的落盘形状：[分钟, 8 个数字] 的严格升序列表。

    None 合法（旧 checkpoint 没有分钟环），采用端按「环只从采用点之后才完整」
    处理。行首必须是真整数（JSON 里 12345.0 不算），严格递增（同时保证不重复分钟）。
    """
    if rows is None:
        return True
    if not isinstance(rows, list):
        return False
    last = None
    for row in rows:
        if not isinstance(row, list) or len(row) != 1 + len(_SERIES_MINUTE_FIELDS):
            return False
        minute = row[0]
        if not _usage_cache_int(minute):
            return False
        if last is not None and minute <= last:
            return False
        last = minute
        for value in row[1:]:
            if not _usage_cache_number(value):
                return False
    return True

def _usage_cache_minutes_state(rows):
    """落盘行 -> 折叠时的字典桶（键是 epoch 分钟整数）。

    还原桶与冷折桶逐字段同类型，「采用后再切片」与「冷折后切片」输出逐字节一致。
    """
    host = _host()
    out = {}
    for row in rows or []:
        key = int(row[0])
        bucket = host._new_series_bucket(key * 60)
        for index, field in enumerate(_SERIES_MINUTE_FIELDS, start=1):
            bucket[field] = row[index]
        out[key] = bucket
    return out

def _usage_cache_credits_ok(credits):
    if not isinstance(credits, list):
        return False
    for item in credits:
        if not isinstance(item, dict):
            return False
        for field in ("at", "credit", "total_tokens"):
            if not _usage_cache_number(item.get(field)):
                return False
        for field in ("iso", "model", "account"):
            if not isinstance(item.get(field), str):
                return False
    return True

def _usage_cache_hits_ok(hits, overflow):
    """闭窗口边沿账的落盘形状：[整秒 epoch] 列表 + 严格 bool 溢出标记。

    旧 checkpoint 没有这两个键就整份拒绝、升级后一次性重折；缺了它闭窗口退回
    扫描（正确但慢），宁可重折一次补全。
    """
    if not isinstance(hits, list) or not isinstance(overflow, bool):
        return False
    for value in hits:
        if not _usage_cache_int(value):
            return False
    return True

def _usage_cache_take(kind, realm=None):
    """从 checkpoint 里取出一份状态（取过即删），没有合适的返回 None。

    snapshot/analytics 按 realm 匹配（JSON 的 null 表示不限 realm），其余只有一份。
    取过即删：同一份不会被第二次采用，不存在「校验失败的脏状态被下个 realm 拿到」。
    """
    entries = _usage_cache_kind_entries(kind)
    if not isinstance(entries, list):
        return None
    # 取走这一步也要在锁里：两个 realm 的首次刷新可能同时 pop，边遍历边 pop
    # 会跳过条目，后果只是这次不采用。
    with _usage_cache_lock:
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            if kind not in ("by_account", "remaining"):
                # realm 字段必须显式存在且类型正确：缺字段的条目被 None 匹配走
                # 会把「只折了一个 realm」的数字当「全部 realm」报出去。
                entry_realm = entry.get("realm")
                if "realm" not in entry or not (entry_realm is None
                                                or isinstance(entry_realm, str)):
                    continue
                if entry_realm != realm:
                    continue
            entries.pop(index)
            return entry
    return None

def _usage_cache_position_ok(entry, log_key):
    """校验一份状态的 (offset, key, tail)；不合格返回 None。

    offset 必须仍落在同一文件的现有字节里（dev/ino 相同、size >= offset），且
    offset 前最后 64 字节与写下时一致：文件被替换、截短、copytruncate 重写都会被挡住。
    """
    host = _host()
    offset = entry.get("offset")
    key = entry.get("key")
    tail_hex = entry.get("tail")
    if not _usage_cache_int(offset) or offset <= 0:
        return None
    if not isinstance(key, list) or len(key) != 3 or not all(
            _usage_cache_int(v) for v in key):
        return None
    if not isinstance(tail_hex, str):
        return None
    try:
        tail = bytes.fromhex(tail_hex)
    except ValueError:
        return None
    if not host._log_offset_holds(tuple(key), log_key, offset):
        return None
    if host._log_tail_signature(offset) != tail:
        return None
    return offset, tail

def _usage_cache_fingerprints_ok(entry, pricing_used_ok, realm_key):
    """「用到的价」与 realm 指纹必须与当前进程一致；任一侧取不到（None）都算不一致。

    取不到意味着「折叠当时读了什么」无法确认，缓存不能当有效用。
    """
    if not pricing_used_ok or realm_key is None:
        return False
    return entry.get("realm_inputs") == realm_key

def _usage_cache_note_adopted(kind, offset):
    """采用成功后的节流初始化：刚加载的 offset 不算「有新字节要写」，否则重启后
    第一次刷新会把「offset 从 0 涨到文件尾」当成大推进，白重写一份相同的文件。
    """
    global _usage_cache_last_attempt
    with _usage_cache_lock:
        if offset > _usage_cache_progress.get(kind, 0):
            _usage_cache_progress[kind] = offset
        if offset > _usage_cache_checkpointed.get(kind, 0):
            _usage_cache_checkpointed[kind] = offset
        _usage_cache_last_attempt = time.time()

def _usage_cache_adopt_snapshot(r, state, log_key, pricing, realm):
    """把 checkpoint 里对应 realm 的 snapshot 折叠装进 state；成功返回 True。

    调用方持有 _usage_snap_state_lock 且只在 state 全新时调用；采用只把 offset/
    tail 往前挪，折叠逻辑一行不动。hits/hits_overflow 必填，旧 checkpoint 没有就
    整份拒绝、升级后一次性重折。
    """
    host = _host()
    entry = _usage_cache_take("snapshot", r)
    if entry is None:
        return False
    position = _usage_cache_position_ok(entry, log_key)
    if position is None:
        return False
    if not _usage_cache_fingerprints_ok(
            entry, host._pricing_used_unchanged(entry.get("pricing"), pricing),
            _realm_inputs_key(realm)):
        return False
    snap = entry.get("snap")
    if not _usage_cache_snapshot_ok(snap):
        return False
    days = entry.get("days")
    floor = entry.get("days_floor")
    if not _usage_cache_days_ok(days, floor, _usage_cache_snapshot_ok):
        return False
    hours_floor = entry.get("hours_floor_at")
    if hours_floor is not None and not _usage_cache_number(hours_floor):
        return False
    hits = entry.get("hits")
    overflow = entry.get("hits_overflow")
    if not _usage_cache_hits_ok(hits, overflow):
        return False
    offset, tail = position
    state.update({"snap": snap, "offset": offset, "tail": tail,
                  "days": days, "days_floor": floor,
                  "hours_floor_at": host._hours_floor_on_adopt(days, hours_floor),
                  "hits": list(hits), "hits_overflow": overflow,
                  # 「用到的价」记录跟着状态一起装进来：后续增量折叠继续往
                  # 这份记录里加，下一次判废拿它比对当前价表。
                  "pricing": host._copy_pricing_record(entry.get("pricing"))})
    _usage_cache_note_adopted("snapshot", offset)
    return True

def _usage_cache_adopt_by_account(state, log_key):
    """by_account 折叠的采用；这一份没有价格/realm 指纹（折叠不读它们）。"""
    entry = _usage_cache_take("by_account")
    if entry is None:
        return False
    position = _usage_cache_position_ok(entry, log_key)
    if position is None:
        return False
    buckets = entry.get("buckets")
    if not _usage_cache_buckets_ok(buckets):
        return False
    offset, tail = position
    state.update({"buckets": buckets, "offset": offset, "tail": tail})
    _usage_cache_note_adopted("by_account", offset)
    return True

def _usage_cache_adopt_remaining(state, log_key):
    """剩余用量折叠的采用。事件日志的位置单独校验，对不上就整份放弃。

    每个 pair 还要过 _pair_repair：落盘缓冲按日志顺序，at 可能回退几毫秒，统一
    整理成按 at 稳定排序；整理不了（负增量=坏数据）整份放弃。
    """
    host = _host()
    entry = _usage_cache_take("remaining")
    if entry is None or not _usage_cache_remaining_ok(entry):
        return False
    position = _usage_cache_position_ok(entry, log_key)
    if position is None:
        return False
    try:
        file_state = {"offset": entry["file"]["offset"],
                      "key": tuple(entry["file"]["key"]),
                      "tail": bytes.fromhex(entry["file"]["tail"])}
    except ValueError:
        return False
    if not host._log_resume_ok(file_state, host._usage_log_key(_gw().LIMIT_EVENTS_FILE),
                          _gw().LIMIT_EVENTS_FILE):
        return False
    offset, tail = position
    usage = {}
    for pair_key, pair in entry["usage"].items():
        uid, _, model = pair_key.partition("\x1f")
        buf = {"at": list(pair["at"]), "cum": list(pair["cum"]),
               "head": pair["head"], "base": pair["base"]}
        if not host._pair_repair(buf):
            return False
        usage[(uid, model)] = buf
    state["log"] = {"offset": offset, "key": tuple(entry["key"]), "tail": tail}
    state["file"] = file_state
    state["usage"] = usage
    state["events"] = [dict(event) for event in entry["events"]]
    state["keys"] = {(event["account"], event["model"], float(event["reset"]))
                     for event in state["events"]}
    _usage_cache_note_adopted("remaining", offset)
    return True

def _usage_cache_adopt_analytics(realm, state, log_key, pricing, realm_inputs):
    """analytics all-time 折叠的采用；闭窗口的边沿账是必填项（见 snapshot 版）。"""
    host = _host()
    entry = _usage_cache_take("analytics", realm)
    if entry is None:
        return False
    position = _usage_cache_position_ok(entry, log_key)
    if position is None:
        return False
    if not _usage_cache_fingerprints_ok(
            entry, host._pricing_used_unchanged(entry.get("pricing"), pricing),
            _realm_inputs_key(realm_inputs)):
        return False
    maps = entry.get("maps")
    if not _usage_cache_analytics_ok(maps):
        return False
    days = entry.get("days")
    floor = entry.get("days_floor")
    if not _usage_cache_days_ok(days, floor, _usage_cache_analytics_day_ok):
        return False
    max_at = entry.get("max_at")
    if not _usage_cache_number(max_at):
        return False
    hits = entry.get("hits")
    overflow = entry.get("hits_overflow")
    if not _usage_cache_hits_ok(hits, overflow):
        return False
    offset, tail = position
    state.update({"maps": maps, "offset": offset, "tail": tail,
                  "days": days, "days_floor": floor, "max_at": max_at,
                  "hits": list(hits), "hits_overflow": overflow,
                  "pricing": host._copy_pricing_record(entry.get("pricing"))})
    _usage_cache_note_adopted("analytics", offset)
    return True

def _usage_cache_adopt_series(r, state, log_key, realm_inputs):
    """时间序列状态的采用；这一份只有 realm 指纹（折叠不算价）。"""
    entry = _usage_cache_take("series", r)
    if entry is None:
        return False
    position = _usage_cache_position_ok(entry, log_key)
    if position is None:
        return False
    realm_key = _realm_inputs_key(realm_inputs)
    if realm_key is None or entry.get("realm_inputs") != realm_key:
        return False
    days = entry.get("days")
    floor = entry.get("days_floor")
    if not _usage_cache_days_ok(days, floor, _usage_cache_series_bucket_ok):
        return False
    max_at = entry.get("max_at")
    if not _usage_cache_number(max_at):
        return False
    credits = entry.get("credits")
    if not _usage_cache_credits_ok(credits):
        return False
    minutes = entry.get("minutes")
    if not _usage_cache_series_minutes_ok(minutes):
        return False
    minutes_from = entry.get("minutes_from")
    if minutes_from is not None and not _usage_cache_number(minutes_from):
        return False
    hits = entry.get("hits")
    overflow = entry.get("hits_overflow")
    if not _usage_cache_hits_ok(hits, overflow):
        return False
    if minutes is None:
        # 旧 checkpoint 没有分钟环：日/小时桶照用，环从采用点之后才开始折。
        # 完整线记成采用时的 max_at，早于它的窗口一律退回整段扫描。
        minutes = []
        minutes_from = max_at
    offset, tail = position
    state.update({"days": days, "days_floor": floor, "max_at": max_at,
                  "credits": credits, "offset": offset, "tail": tail,
                  "minutes": _usage_cache_minutes_state(minutes),
                  "minutes_from": minutes_from,
                  "hits": list(hits), "hits_overflow": overflow})
    _usage_cache_note_adopted("series", offset)
    return True

def _usage_cache_days_copy(days, copy_bucket):
    """日桶表的深拷贝（序列化在锁外做，桶必须和活状态脱钩）。"""
    return {key: copy_bucket(bucket) for key, bucket in (days or {}).items()}

def _copy_series_day(bucket):
    copy = dict(bucket)
    copy["hours"] = {h: dict(sub) for h, sub in (bucket.get("hours") or {}).items()}
    return copy

def _copy_snapshot_day(bucket):
    host = _host()
    copy = host._copy_usage_snapshot(bucket)
    hours = bucket.get("hours")
    if hours is not None:
        copy["hours"] = {h: host._copy_usage_snapshot(sub)
                         for h, sub in hours.items()}
    return copy

# 分钟环落盘行的字段顺序：[分钟, 这 8 个数字]（at 由分钟推出，省下每桶 10
# 字节；credit 在最后，与 _new_series_bucket 的键序一致）。
_SERIES_MINUTE_FIELDS = ("requests", "errors", "prompt_tokens",
                         "completion_tokens", "reasoning_tokens",
                         "cached_tokens", "total_tokens", "credit")

def _usage_cache_minutes_copy(minutes):
    """分钟环的落盘形态：按分钟升序的 [分钟, 8 个数字] 行。

    字典桶每桶约 150 字节、满环 361 桶要 50 KB 级；位置数组每桶 40～50 字节
    （本机实测 52）。形状差异只在落盘/加载这一层，加载端还原成与冷折一样的
    字典桶，切片输出不受影响。排序写出，让文件内容与折叠顺序无关；没有环时
    写 null，加载端按「环不完整」处理。
    """
    if minutes is None:
        return None
    rows = []
    for key in sorted(minutes or {}):
        bucket = minutes[key]
        rows.append([int(key)] + [bucket.get(field)
                                  for field in _SERIES_MINUTE_FIELDS])
    return rows

def _usage_cache_snapshot_entry(realm, state):
    """一份 snapshot 状态的 JSON 形态；不适合落盘时返回 None。

    只写折叠成功过的状态：错误路径的 offset 归零，写进去只会让加载端白校验。
    """
    host = _host()
    offset = state.get("offset") or 0
    tail = state.get("tail")
    if offset <= 0 or not isinstance(tail, bytes) or state.get("snap") is None:
        return None
    # 「用到的价」记录跟着状态装进来，下次判废拿它比对当前价表；形状不合格
    # 或为 None（那份折叠没读任何价）都跳过。
    record = state.get("pricing")
    if not host._pricing_record_ok(record):
        return None
    realm_inputs = state.get("realm")
    realm_key = _realm_inputs_key(realm_inputs)
    if realm_key is None:
        return None
    if not host._realm_fold_reproducible(realm_inputs[1]):
        # 折叠的归属冷进程复现不了（见 _realm_fold_reproducible）：写下去会被
        # 按新归属当成有效，宁可这次不写。
        return None
    return {"realm": realm, "offset": offset,
            "key": list(state["key"] or (0, 0, 0)), "tail": tail.hex(),
            "pricing": host._copy_pricing_record(record), "realm_inputs": realm_key,
            # 深拷贝在锁内做，序列化在锁外做（见 _usage_cache_collect）。
            "snap": host._copy_usage_snapshot(state["snap"]),
            # 按日分桶的窗口半：与 all-time 半同一次扫描、同一条 offset/tail
            # 校验；日桶的小时子表跟着深拷贝。
            "days": _usage_cache_days_copy(state.get("days"),
                                           _copy_snapshot_day),
            "days_floor": state.get("days_floor"),
            # 小时层的剪枝线（见 _hours_cover），缺了它重启后小时层要么不折、
            # 要么服务被剪过的天，所以和日桶一起落盘。
            "hours_floor_at": state.get("hours_floor_at"),
            # 闭窗口的边沿账（见 _closed_window_hit_record），旧 checkpoint 没有。
            "hits": list(state["hits"]),
            "hits_overflow": bool(state["hits_overflow"])}

def _usage_cache_by_account_entry(state):
    """by_account 折叠的 JSON 形态；同上，只写折叠成功过的。"""
    offset = state.get("offset") or 0
    tail = state.get("tail")
    buckets = state.get("buckets")
    if offset <= 0 or not isinstance(tail, bytes) or not isinstance(buckets, dict):
        return None
    return {"offset": offset, "key": list(state["key"] or (0, 0, 0)),
            "tail": tail.hex(),
            # 深拷贝：序列化在锁外做，桶里的 models 必须和活状态脱钩。
            "buckets": {k: dict(v, models=dict(v.get("models") or {}))
                        for k, v in buckets.items()}}

def _usage_cache_remaining_entry(state):
    """剩余用量折叠的 JSON 形态；同上，只写折叠成功过的。

    两份位置都带上：usage.jsonl 的与事件日志的。缓冲键 "uid\\x1fmodel"
    （JSON 的对象键只能是字符串）。
    """
    log = state.get("log") or {}
    file_slot = state.get("file") or {}
    events = state.get("events")
    usage = state.get("usage")
    offset = log.get("offset") or 0
    tail = log.get("tail")
    if offset <= 0 or not isinstance(tail, bytes):
        return None
    if not isinstance(events, list) or not isinstance(usage, dict):
        return None
    buffers = {}
    for (uid, model), pair in usage.items():
        if not isinstance(pair, dict):
            return None
        buffers["%s\x1f%s" % (uid, model)] = {
            "at": list(pair.get("at") or ()),
            "cum": list(pair.get("cum") or ()),
            "head": int(pair.get("head") or 0),
            "base": int(pair.get("base") or 0),
        }
    return {"offset": offset, "key": list(log.get("key") or (0, 0, 0)),
            "tail": tail.hex(),
            "file": {"offset": int(file_slot.get("offset") or 0),
                     "key": list(file_slot.get("key") or (0, 0, 0)),
                     "tail": (file_slot.get("tail") or b"").hex()},
            "events": [dict(event) for event in events],
            "usage": buffers}

def _usage_cache_analytics_entry(realm, state):
    """analytics all-time 折叠的 JSON 形态；同上。"""
    host = _host()
    offset = state.get("offset") or 0
    tail = state.get("tail")
    if offset <= 0 or not isinstance(tail, bytes):
        return None
    record = state.get("pricing")
    if not host._pricing_record_ok(record):
        return None          # 同 snapshot：「用到的价」记录不合格就不写
    realm_inputs = state.get("realm")
    realm_key = _realm_inputs_key(realm_inputs)
    if realm_key is None:
        return None
    if not host._realm_fold_reproducible(realm_inputs[1]):
        return None          # 同 snapshot：这次折叠的归属冷进程复现不了
    return {"realm": realm, "offset": offset,
            "key": list(state["key"] or (0, 0, 0)), "tail": tail.hex(),
            "pricing": host._copy_pricing_record(record), "realm_inputs": realm_key,
            "maps": host._copy_analytics_all_time(state["maps"]),
            "days": _usage_cache_days_copy(state.get("days"),
                                           host._copy_analytics_window_maps),
            "days_floor": state.get("days_floor"),
            # 折过的最大 at：小时层靠它判断 until 有没有切掉桶里的行。
            "max_at": state.get("max_at") or 0,
            "hits": list(state["hits"]),
            "hits_overflow": bool(state["hits_overflow"])}

def _usage_cache_series_entry(realm, state):
    """时间序列状态的 JSON 形态；同上，只写折叠成功过的。

    这一份最大（日桶 + 24 小时子桶 × 保留天数），深拷贝只拷桶本身，credit 缓冲
    浅拷外层；分钟环用位置数组落盘（见 _usage_cache_minutes_copy，实测每桶 52 字节）。
    """
    offset = state.get("offset") or 0
    tail = state.get("tail")
    if offset <= 0 or not isinstance(tail, bytes):
        return None
    realm_key = _realm_inputs_key(state.get("realm"))
    if realm_key is None:
        return None
    return {"realm": realm, "offset": offset,
            "key": list(state["key"] or (0, 0, 0)), "tail": tail.hex(),
            "realm_inputs": realm_key,
            "days": _usage_cache_days_copy(state.get("days"), _copy_series_day),
            "days_floor": state.get("days_floor"),
            "max_at": state.get("max_at") or 0,
            "credits": [dict(item) for item in (state.get("credits") or [])],
            "minutes": _usage_cache_minutes_copy(state.get("minutes")),
            "minutes_from": state.get("minutes_from"),
            "hits": list(state["hits"]),
            "hits_overflow": bool(state["hits_overflow"])}

def _usage_cache_collect_kind(kind):
    """逐把取一类状态，做锁外可序列化的深拷贝；返回 (entries, offset) 或 None。

    锁逐把拿、随即放开，不嵌套——写盘路径不能制造「snapshot -> analytics」的
    锁序边。各类不强求同一个瞬间（加载端对每份分别校验）。entries 为空返回
    None：没得写就不写。offset 是该类各 realm 状态里的最远位置。
    """
    host = _host()
    if kind == "snapshot":
        entries = []
        done = 0
        with host._usage_snap_state_lock:
            for realm, state in host._usage_snap_state.items():
                entry = _usage_cache_snapshot_entry(realm, state)
                if entry is not None:
                    entries.append(entry)
                    done = max(done, state["offset"])
        return (entries, done) if entries else None
    if kind == "by_account":
        with host._byacct_state_lock:
            entry = _usage_cache_by_account_entry(host._byacct_state)
            if entry is None:
                return None
            return [entry], host._byacct_state["offset"]
    if kind == "analytics":
        entries = []
        done = 0
        with host._analytics_state_lock:
            for realm, state in host._analytics_state.items():
                entry = _usage_cache_analytics_entry(realm, state)
                if entry is not None:
                    entries.append(entry)
                    done = max(done, state["offset"])
        return (entries, done) if entries else None
    if kind == "series":
        entries = []
        done = 0
        with host._series_state_lock:
            for realm, state in host._series_state.items():
                entry = _usage_cache_series_entry(realm, state)
                if entry is not None:
                    entries.append(entry)
                    done = max(done, state["offset"])
        return (entries, done) if entries else None
    if kind == "remaining":
        with host._remaining_state_lock:
            entry = _usage_cache_remaining_entry(host._remaining_state)
            if entry is None:
                return None
            return [entry], entry["offset"]
    return None

def _usage_cache_write_kind(kind, entries):
    """原子写一类状态的 checkpoint：同目录临时文件 + os.replace，不 fsync。

    不排序键：dict 键顺序就是折叠时的插入顺序，加载回来必须与冷启动逐字节
    一致（响应 JSON 里键序可见）；排序只留给 _object_digest() 的指纹。
    盘上本进程没碰过的 realm 条目原样保留：否则重启后先折的 realm 触发写盘
    会清掉别的 realm 的旧条目，那些 realm 只能重折（实测 realm=intl 重折 15 s）。
    """
    if kind in ("snapshot", "analytics", "series"):
        live_realms = {entry.get("realm") for entry in entries}
        with _usage_cache_lock:
            old = _usage_cache_read_kind(kind) or []
        keep = [e for e in old
                if isinstance(e, dict) and e.get("realm") not in live_realms]
        if keep:
            entries = list(entries) + keep
    payload = {"schema": _USAGE_CACHE_SCHEMA, "kind": kind,
               "written_at": time.time(), "entries": entries}
    path = _usage_cache_kind_path(kind)
    tmp = path + ".tmp"
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        # 不在这里建目录：目标目录不可用时必须写失败，测试与面板都依赖这条语义。
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _usage_cache_maybe_save(kind, offset):
    """一次折叠成功后，按节流阈值把「有推进的类」落盘；任何异常都吞掉。

    调用点必须在状态锁之外：在状态锁里再取别的状态锁会构成锁序环。
    写哪几类：字节阈值到点的类里写调用方那一类和真正推进过的类；计时器到点而
    没有类到字节阈值时，写所有推进过的类。字节阈值 0（测试/探针的强制写）对
    所有折过的类都「到点」，没推进过的留在原地，避免写放大。
    """
    global _usage_cache_last_attempt
    if not _usage_cache_enabled():
        return
    try:
        # 串行化整段写盘：临时文件名固定（path + ".tmp"），两个写者同时打开
        # 同一个临时文件会互相截断。
        with _usage_cache_write_lock:
            now = time.time()
            with _usage_cache_lock:
                if offset > _usage_cache_progress.get(kind, 0):
                    _usage_cache_progress[kind] = offset
                min_bytes = _usage_cache_min_bytes()
                min_seconds = _usage_cache_min_seconds()
                due_kinds = [k for k in _usage_cache_progress
                             if _usage_cache_progress.get(k, 0)
                             - _usage_cache_checkpointed.get(k, 0) >= min_bytes]
                # 计时器分支只在确有推进时触发：进度没动时写出的与上次逐字节
                # 相同，面板开着就每 900 s 白写一份 120 KB（纯写放大）。
                advanced = [k for k in _usage_cache_progress
                            if _usage_cache_progress.get(k, 0)
                            > _usage_cache_checkpointed.get(k, 0)]
                if not due_kinds and (not advanced
                                      or (now - _usage_cache_last_attempt)
                                      < min_seconds):
                    return
                # 记「尝试」不记「成功」：只读文件系统上失败会一直成立，不能每次刷新都试写。
                _usage_cache_last_attempt = now
                write_kinds = [k for k in due_kinds
                               if k == kind or k in advanced] or advanced
            # 某一类收集不出东西就跳过它，其余类照常落盘，互不牵连。
            for advanced_kind in write_kinds:
                try:
                    collected = _usage_cache_collect_kind(advanced_kind)
                    if collected is None:
                        continue
                    entries, done = collected
                    _usage_cache_write_kind(advanced_kind, entries)
                except Exception as exc:
                    _gw().log("usage aggregate cache write failed (%s): %s"
                        % (advanced_kind, exc))
                    continue
                with _usage_cache_lock:
                    if done > _usage_cache_checkpointed.get(advanced_kind, 0):
                        _usage_cache_checkpointed[advanced_kind] = done
    except Exception as exc:
        _gw().log("usage aggregate cache write failed: %s" % exc)
