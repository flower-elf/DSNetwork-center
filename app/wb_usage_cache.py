"""聚合状态落盘：checkpoint 的读取、校验与写盘。

增量聚合的状态重启即丢，这里把五类状态连同位置与指纹写进数据目录，重启后
首次刷新只读文件尾部新增的字节。读写失败一律静默降级，checkpoint 只是加速，
不影响服务本身。网关运行态经 wb_usage 的活值读取，测试替换网关属性后立即
生效。
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

# ---------------------------------------------------------------------------
# 聚合状态落盘，重启后免冷扫。
#
# 增量聚合的状态放在模块级字典里，进程重启就全丢，第一次刷新要重扫整份日志，
# 冷扫描成本随日志线性上升。这里把每份状态连同位置信息写进数据目录，重启后
# 第一次刷新只读文件尾部新增的字节。
#
# 一类状态一份文件（usage-aggregate-cache-<kind>.json）：五份状态各有各的
# 位置与指纹校验、写入节流，加载端按需解析，只跑 /usage 的进程不为其余几份
# 付解析成本；写侧只重写有推进的那一类。旧的单文件只读兼容，某一类还没有
# 自己的文件时从旧文件取它那一节。
#
# 文件与日志同生命周期，跟着 sysupgrade 一起走。读写失败一律静默降级：
# checkpoint 只是加速，不能影响服务本身。
#
# 加载校验：offset 必须仍落在同一文件的现有字节里（_log_offset_holds），
# offset 前的最后 64 字节必须与写下时一致（_log_tail_signature），再加上价格
# 与 realm 指纹、schema 版本。任一项对不上就走过全量扫描。
# ---------------------------------------------------------------------------
# 数据目录下的文件名。usage/*.json 已被 .gitignore 覆盖。
# 带 <kind> 的是 v4 布局（一类一份）；不带的是升级前的单文件，只读不写。
_USAGE_CACHE_NAME = "usage-aggregate-cache.json"
_USAGE_CACHE_KIND_NAME = "usage-aggregate-cache-%s.json"
# 状态结构或折叠口径一变就 +1，旧文件整体作废。这是唯一挡在「旧结构喂进新
# 代码」前面的东西，改动状态形状时别忘了它。
# v2：写盘按插入顺序（v1 用 sort_keys 写，加载回来所有 dict 变字母序，响应
# 与冷启动逐字节不同）。键顺序算状态结构的一部分，所以旧文件必须整体作废
# ——否则那份字母序会一直粘在内存里，直到日志尾部被重写才会被清掉。
# v3：状态多了按日分桶（窗口半的增量）与时间序列的日/小时桶。载荷形状变了，
# 旧文件同样整体作废：升级后第一次刷新做一次全量折叠（一次性代价，之后
# checkpoint 照旧）——这正是把窗口半也变成增量的入场费。
# v4：一类状态一份文件（每份自带 schema 与 kind 标签），并且快照/analytics 的
# 日桶里多了小时子表（整点窗口的小时层）、series 多了分钟环。载荷形状的变化由
# 各类自己的校验器挡住：升级前的单文件（v3）只读兼容，作为各类还没有自己文件
# 时的回退来源——能通过新校验器的段（by_account / series / remaining）照常
# 采纳，通不过的（缺小时表的 snapshot / analytics）在升级后第一次刷新时做一次
# 全量折叠，之后 checkpoint 照旧。
# v5：snapshot / analytics 的 "pricing" 字段从「三张表的内容摘要」（["on", d1,
# d2, d3] / ["off"]）换成「折叠行真正用到的价」记录（T15）。旧条目的字段形状
# 对不上新判定，采纳端直接拒——升级后第一次刷新一次性重折（与 v3→v4 同处置），
# 之后价表再变只按「用到的价」判废（无关模型的价变动不再重折）。
_USAGE_CACHE_SCHEMA = 5
# 升级前唯一布局的版本号：v3 单文件。只出现在只读回退路径上。
_USAGE_CACHE_LEGACY_SCHEMA = 3
# 五类状态的固定集合（文件名、kind 标签与写盘循环都用它）。
_USAGE_CACHE_KINDS = ("snapshot", "by_account", "analytics", "series",
                      "remaining")

def _usage_cache_enabled():
    """落盘总开关，默认开；面板设置可关闭。

    每次调用都读设置，导入时不定死：现场切换后下一个刷新周期即生效。
    """
    return wb_settings.usage_cache(_gw().ACCOUNTS_DIR)

def _usage_cache_path():
    """升级前单文件（v3）的路径；只读回退与探针脚本用。

    每次调用重算，导入时不算成常量：--usage-dir、测试和探针脚本都会
    在导入之后改 _gw().USAGE_DIR，常量会写到旧目录去。
    """
    return os.path.join(_gw().USAGE_DIR, _USAGE_CACHE_NAME)

def _usage_cache_kind_path(kind):
    """某一类状态的 checkpoint 路径（v4 布局；同上，每次调用重算）。"""
    return os.path.join(_gw().USAGE_DIR, _USAGE_CACHE_KIND_NAME % kind)

def _usage_cache_min_bytes():
    """写盘节流：折叠推进不足这么多字节就不写。默认 1MB。

    1MB 是「重启后最多回放多少」的上限——路由器上扫描约 0.5s/MB，回放 1MB
    等于冷启动成本多半秒；而日志每天涨 6MB 上下，正常流量下一天也未必触发
    一次。写小了是写放大（路由器 flash 写入慢且费寿命），写大了重启变慢。
    """
    try:
        return max(0, int(os.environ.get("WB_USAGE_CACHE_MIN_BYTES", 1024 * 1024)))
    except (TypeError, ValueError):
        return 1024 * 1024

def _usage_cache_min_seconds():
    """写盘节流：距上次尝试不足这么多秒就不写。默认 900s（15 分钟）。

    这条兜住「字节阈值还没到、但偏移已经推进了一点」的情况：不设它，一段
    短流量之后停下的日志要等到下次凑满 1MB 才会落盘。15 分钟让面板开着时
    一天最多约百次写盘（每次几十 KB），对 flash 友好；重启回放的上限则是
    min（1MB, 15 分钟流量）。
    """
    try:
        return max(0.0, float(os.environ.get("WB_USAGE_CACHE_MIN_SECONDS", 900)))
    except (TypeError, ValueError):
        return 900.0

_usage_cache_lock = threading.Lock()
# 写盘串行锁：只被 _usage_cache_maybe_save() 拿一次、且永远是最外层（拿它
# 之后才拿状态锁/节流锁），所以它不参与任何锁序环。
_usage_cache_write_lock = threading.Lock()
# 进程启动后按「类」只读一次 checkpoint：读到就记下来，读不到也记 None——
# 坏文件、只读文件系统都不该让每次刷新都去解析一遍。只解析调用方真正要的
# 那一类，这正是拆文件的意义（只跑 /usage 的进程不解析 remaining）。
# 升级前的单文件（v3）也只读一次，作为某一类还没有自己文件时的回退来源。
_usage_cache_kind_cache = {}
_USAGE_CACHE_LEGACY_UNREAD = object()
_usage_cache_legacy_data = _USAGE_CACHE_LEGACY_UNREAD
# 写盘节流状态，全部由 _usage_cache_lock 保护：
#   progress[kind]     —— 本进程见过该类状态折叠到的最远 offset；
#   checkpointed[kind] —— 该类状态上次「尝试」落盘时的 offset（失败也记，
#                         只读文件系统上不能每次刷新都去试写一遍）；
#   last_attempt       —— 上次尝试写盘的时刻，时间阈值与失败退避都靠它。
# 按「类」记账，不记一个总数：某类状态第一次折叠（或中途重扫）时 offset
# 从 0 跳到文件尾，这一类要尽快落盘，而刚采用过 checkpoint 的类不能被别的
# 类的推进带着白写一遍。v4 起写盘也按类分开：只重写「有推进的那一类」。
_usage_cache_progress = {}
_usage_cache_checkpointed = {}
_usage_cache_last_attempt = time.time()

def _usage_cache_forget():
    """忘掉本进程读过的 checkpoint（模拟一次重启；测试与探针用）。

    读记忆按「类」记账，所以要清的是一个集合；调用点不许持有任何状态锁，
    本函数自己取 _usage_cache_lock（与读取路径同一把）。
    """
    global _usage_cache_legacy_data
    with _usage_cache_lock:
        _usage_cache_kind_cache.clear()
        _usage_cache_legacy_data = _USAGE_CACHE_LEGACY_UNREAD

def _usage_cache_kind_entries(kind):
    """某一类状态的条目列表（读一次、按类记忆）；没有可用来源时返回 None。

    本类文件不存在或读不出时回退到升级前的单文件里对应的那一节：升级后的
    第一次刷新因此不必把整个日志重折一遍。本类文件一旦存在（schema 与 kind
    都对得上）就是唯一来源，旧文件不再参与该类——旧文件里那份可能更旧，也
    可能已经和日志对不上，位置校验各自负责。

    调用点可能在状态锁内（采用阶段），所以本函数会取 _usage_cache_lock；
    反向的锁序不存在——写盘路径取状态锁之前一定先放开本锁，见
    _usage_cache_maybe_save。
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
    """读某一类的 checkpoint 文件并做顶层校验；任何一步不成立都返回 None。

    None 和「文件不存在」同义：调用方按没有缓存处理，绝不因此报错。
    调用方持有 _usage_cache_lock（解析在锁内做，与 v3 的单文件读取一致）。
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
    # 先看类型再看值：JSON 的 true 在 Python 里等于 1，schema: true 不能算
    # 「版本 1」。
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
    """升级前单文件（v3）里某一类的条目，或 None；文件只解析一次。

    调用方持有 _usage_cache_lock。条目列表是解析结果的一部分，采用路径从
    它里面 pop（取过即删），与 v3 时的用法一致。
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
    """升级前的单文件（v3）解析结果，或 None。

    只做顶层校验（对象 + schema 恰好是 3）。不校验它的 kind 字段——v3 的
    文件本来就没有这个字段，各类小节就是顶层键。任何一步不成立都返回 None。
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

    记忆表的值强引用对象本身，所以 id() 不会被回收复用；超过几条就整体清空
    ——正常进程里价表对象只在价格变化时换代，这里只是给长期运行兜底。
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
    """realm 指纹本来就是值比较（不是身份），JSON 化后原样可比。"""
    if inputs is None:
        return None
    realm, accounts = inputs
    return [realm, [[uid, r] for uid, r in accounts]]

# 折叠会就地累加/读取的字段集合直接从构造函数推导：上游往 _empty_stats() 或
# _new_analytics_stat() 里加字段时这里自动跟着收——漏一个，放行的缓存就会在
# 折叠半路抛 KeyError，那次刷新报出半份数字（折叠的错误路径只保留已折的
# 部分）。构造函数在宿主 wb_usage 里，导入期不可用，首次校验时求值并记住。
_NUMBERS = {}


def _usage_cache_numbers(kind):
    """构造函数的键集：snapshot / analytics / series，各求一次记住。"""
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
    """{str: 数字} 形状的计数表（cost_missing / models / realms）。"""
    if not isinstance(counts, dict):
        return False
    for key, value in counts.items():
        if not isinstance(key, str) or not _usage_cache_number(value):
            return False
    return True

def _usage_cache_bucket_ok(bucket):
    """一个 by_model* 桶：折叠会就地累加这些键。"""
    if not isinstance(bucket, dict):
        return False
    for field in _USAGE_CACHE_BUCKET_NUMBERS:
        if not _usage_cache_number(bucket.get(field)):
            return False
    return _usage_cache_counts_ok(bucket.get("accounts"))

def _usage_cache_snapshot_bucket_ok(snap):
    """一个窗口半快照桶的形状（all-time 半、日桶、小时桶共用）。

    少一个键，折叠或 _copy_usage_snapshot() 就会半路抛错，那次刷新报出半份
    数字——校验必须挡在采用之前。
    """
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
    """窗口半快照桶 + 它可能挂着的小时子表（见 _usage_hour_bucket）。

    小时子表的校验与时间序列那份（_usage_cache_series_bucket_ok）同一套：
    键是数字字符串（折叠侧从一开始就写 str（序号），与 JSON 往返后的形状
    一致），子桶形状同快照桶且不嵌表（表只挂在日桶上）。at 是格点，
    没有它求和侧就做不了格点校验，所以这里也要求它是数字。
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
    """一个 analytics 统计桶：折叠会就地累加它的每一个字段。"""
    if not isinstance(stat, dict):
        return False
    for field in _usage_cache_numbers("analytics"):
        if not _usage_cache_number(stat.get(field)):
            return False
    return True

def _usage_cache_models_ok(models):
    """analytics 的 {model: {requests, tokens, reasoning, cost_cny}} 表。"""
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
    """窗口形状的 analytics maps（日桶/小时桶）：同一批表，装的是 window 那一半。

    与 _usage_cache_analytics_ok 是两份形状，不能互相顶替：日桶只折窗口半，
    少 all_time/all_models 两个键，用 all-time 的校验器会把整份 analytics
    entry 拒掉（连它自己那份 all-time 也一起丢），窗口就退回整段扫描。
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
    """analytics 日桶的小时子表：{小时键: 窗口形状 maps + at}。

    小时键是数字字符串（JSON 的对象键只能是字符串，折叠侧从一开始就用
    str（小时序号）），子桶就是一份窗口形状 maps 再带它自己的 at——形状与
    时间序列的小时子表同一个道理。
    """
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

    hours 缺席是合法状态，不是损坏：剪枝线（_prune_day_buckets 的 hours_keep
    一段）会把保留窗口之外的旧天的小时表摘掉，本地午夜落在不存在的小时里那天
    也会被摘掉；只折窗口半的日桶在「这天没有小时表」时照旧回答按日边界的窗口，
    而整点窗口本来就由求和侧在 hours 为 None 时退回整段扫描
    （_analytics_window_from_hours），所以采纳仍然正确。与
    _usage_cache_snapshot_ok / _usage_cache_series_bucket_ok 对 hours 的容忍
    同一套规矩。

    at 则必须留着：旧格式写下的日桶没有这个键（也就没有小时层），在这里被拒，
    升级后 analytics 整体重折一次、小时层立刻就有历史可借。只放任 hours 缺席，
    这次一次性代价就不会被剪枝线反复引燃——否则只要还在折新行，剪枝线天天把旧天
    的小时表摘掉，整份状态就天天被判废、每次进程启动都要付一次全量重折。
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
    """analytics all-time maps：summary/accts/models/keys 四张表。"""
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
    """by_account 的 {account: 桶}：折叠会就地累加每个桶。"""
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
    """剩余用量折叠：事件表 + 每个（账号, 模型）的 at/cum 两个平行列表。

    cum 必须单调（回退=负增量=坏数据）且与 at 等长、base 不可能超过 cum[0]
    ——这些是加载后一切查询的前提，坏一条就会静默算出错数字，所以逐元素走
    一遍：一次重启付 1~3ms，换掉一次全量冷扫。
    `at` **不要求有序**：写入侧的 at 是请求完成时刻，多线程下会回退几毫秒
    （真机日志与路由器自己写出的 checkpoint 都踩过），加载时由 _pair_repair
    做一次稳定排序（配对不变、前缀和重算）。有序性校验在那里，形状检查
    在这里。
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
        # base 是「保留区之前那段的累计和」，cum 从最早一行累计且含被裁前缀，
        # 所以 base 不可能超过 cum[0]，超过的条目必须整份拒绝——否则左端落在
        # 保留区之前的区间求和会算出负数（真机面板显示过 -1293.5M）。
        if cum and pair["base"] > cum[0]:
            return False
    return True

def _usage_cache_day_key_ok(key):
    """日键必须是 "YYYY-MM-DD"：窗口与日桶的比较靠的就是这个形状。"""
    return (isinstance(key, str) and len(key) == 10 and key[4] == "-"
            and key[7] == "-")

def _usage_cache_days_ok(days, floor, bucket_ok):
    """按日分桶的表：{日键: 桶}，floor 是「对哪些天完整」的下界（日键或 None）。

    桶的形状由调用方给（snapshot / analytics / 时间序列各一套），日键与
    floor 的形状在这里统一校验：键不是日期的桶永远不会被窗口选中，但也说明
    这份状态不是我们写出来的，直接拒绝更干净。
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
    """时间序列桶：9 个数字；日桶还带 hours 子表（小时键是数字字符串）。

    小时键写成字符串是 JSON 往返决定的（JSON 的对象键只能是字符串），折叠
    侧因此从一开始就用 str（小时序号），加载回来的状态与现折的状态形状一致。
    """
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

    None 是合法的（旧 checkpoint 没有分钟环这个键）：采用端把它当「环只
    从采用点之后才完整」处理（见 _usage_cache_adopt_series），所以这里放行，
    不拒整份。行首必须是真整数（JSON 里 12345.0 不算）、严格递增——
    递增同时保证「没有重复分钟」；其余 8 个必须是折叠累加出来的数字。
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

    还原出的桶与冷折出来的逐字段同类型（int 的字段来自 JSON 的整数写法，
    credit 是浮点），所以「采用后再切片」与「冷折后切片」的输出逐字节一致。
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
    """credit 缓冲：{at, iso, model, account, credit, total_tokens} 的列表。"""
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

    旧 checkpoint 没有这两个键（entry.get 给 None）→ 拒：升级后第一次刷新
    重折一次（与 v3→v4「缺小时表的 snapshot/analytics 被拒、一次性重折」
    同一套处置）。账是「直到某刻没有行落在整秒上」的证明，缺了它闭窗口只能
    退回扫描（正确但慢），宁可重折一次把它补全。
    """
    if not isinstance(hits, list) or not isinstance(overflow, bool):
        return False
    for value in hits:
        if not _usage_cache_int(value):
            return False
    return True

def _usage_cache_take(kind, realm=None):
    """从 checkpoint 里取出一份状态（取过即删），没有合适的返回 None。

    snapshot/analytics 按 realm 匹配（JSON 的 null 表示不限 realm），
    by_account / remaining 只有一份。逐份取用：同一份不会被第二次采用，也就
    不存在「先取走、校验失败、下个 realm 又拿到一份脏状态」的路径。

    只解析、只取用调用方要的那一类文件：其余四类连文件都不打开。
    """
    entries = _usage_cache_kind_entries(kind)
    if not isinstance(entries, list):
        return None
    # 取走这一步也要在锁里：两个 realm 的首次刷新可能同时到这里，一个
    # 边遍历边 pop、另一个也在 pop，遍历就会跳过条目。跳过的后果只是这次
    # 不采用（安全方向），但少一次竞争就少一个要解释的路径。
    with _usage_cache_lock:
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            if kind not in ("by_account", "remaining"):
                # realm 字段必须显式存在且类型正确：realm=None 的折叠（全部
                # realm）和某个具体 realm 的折叠是两份不同的状态，缺字段的
                # 条目一旦被 None 匹配走，就会把「只折了一个 realm」的数字
                # 当成「全部 realm」报出去。
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

    复用 #189 的两个原语，不另造一套：offset 必须仍落在同一个文件的现有
    字节里（dev/ino 相同、当前 size >= offset），并且 offset 前的最后 64 字节
    与写下时一致——文件被替换、截短、copytruncate 后重写都会被这两条挡住。
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
    """「用到的价」与 realm 指纹必须和当前进程读到的一致。

    价格一侧由调用方用 _pricing_used_unchanged(entry["pricing"], pricing)
    判好（记录形状不符、价表读不出来、总开关翻转都在那里返回 False），这里
    只把它和 realm 一侧一起收口。任一侧取不到（None）都算不一致：取不到就
    意味着「折叠当时读了什么」已经无法确认，缓存不能当有效用。
    """
    if not pricing_used_ok or realm_key is None:
        return False
    return entry.get("realm_inputs") == realm_key

def _usage_cache_note_adopted(kind, offset):
    """采用成功后的节流初始化：刚加载的 offset 不算「有新字节要写」。

    不初始化的话，重启后第一次刷新会把「offset 从 0 涨到文件尾」当成一次大
    推进，白白重写一份内容相同的文件。
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

    调用方持有 _usage_snap_state_lock，并且只在 state 全新时调用：采用只把
    offset/tail 往前挪，折叠逻辑一行不动。

    闭窗口的边沿账（hits/hits_overflow）是这一份的必填项：旧 checkpoint
    没有这两个键，会被拒 → 升级后第一次刷新重折一次（一次性代价，与
    v3→v4 缺小时表时的处置一致）。
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
    """剩余用量折叠的采用；同样没有价格/realm 指纹（折叠不读它们）。

    事件日志的位置在这里单独校验：它很小，位置对不上就整份放弃——冷读一遍
    两个文件比「先采用一半、再发现对不上」简单得多。
    每个 pair 还要过一遍 _pair_repair：落盘的缓冲按日志顺序排列，`at`
    可能回退几毫秒，这里统一整理成按 `at` 稳定排序的规范形态（有序的文件
    上它就是一次 O(n) 的核对）。整理不了的（负增量=坏数据）整份放弃。
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
        # 旧 checkpoint（没有分钟环这个键）：这一份状态是能用的（日/小时桶
        # 与 credits 都在），只是分钟环要从采用点之后才开始折。把完整线记成
        # 采用时的 max_at——前缀里所有行的 at 都 <= 它，所以起点晚于它的窗口
        # 只碰得到之后折进来的行，环对它们是完整的；早于它的窗口一律退回
        # 整段扫描（见 _series_slice_minutes）。
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
    """一个时间序列日桶的深拷贝（含 hours 子表）。"""
    copy = dict(bucket)
    copy["hours"] = {h: dict(sub) for h, sub in (bucket.get("hours") or {}).items()}
    return copy

def _copy_snapshot_day(bucket):
    """一个窗口半日桶的深拷贝（含 hours 子表，见 _usage_hour_bucket）。"""
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

    形状与内存里的字典桶不同（键换成行首的整数、字段名省掉），这是刻意的
    体积预算：环最多 361 个桶（6 小时），字典形状每个桶 ~150 字节、满环要
    50KB 级，位置数组把每个桶压到 40~50 字节（本机真实数据实测 52）。形状
    差异只存在于落盘/加载这一层：加载端（_usage_cache_minutes_state）还原
    成与冷折完全一样的字典桶，切片与输出的字节不受影响。排序写出，不按
    插入顺序，是为了让文件内容与折叠顺序无关（冷折与「采用后再折」写出的
    这份字节一致）。状态里没有环（不该发生，但落盘不靠断言）时写 null：
    加载端按「环不完整」处理。
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

    只写「折叠成功过」的状态：错误路径会把 offset 归零，这里直接跳过——
    offset 为 0 的缓存没有任何加速作用，写进去只会让加载端白校验一遍。
    """
    host = _host()
    offset = state.get("offset") or 0
    tail = state.get("tail")
    if offset <= 0 or not isinstance(tail, bytes) or state.get("snap") is None:
        return None
    # 「用到的价」记录（T15；见「用到的价」状态指纹一节）：形状不合格就不写。
    # 记录为 None（总开关关着 / 价表读不出来）时也跳过——那份折叠没读任何价，
    # 落盘没有任何加速作用（采纳端只认「记录也是 None」这一种一致）。
    record = state.get("pricing")
    if not host._pricing_record_ok(record):
        return None
    realm_inputs = state.get("realm")
    realm_key = _realm_inputs_key(realm_inputs)
    if realm_key is None:
        return None
    if not host._realm_fold_reproducible(realm_inputs[1]):
        # 这次折叠的归属冷进程复现不了（见 _realm_fold_reproducible）：写下去
        # 会被按新归属当成有效。宁可这次不写，等池就绪/重载后再写。
        return None
    return {"realm": realm, "offset": offset,
            "key": list(state["key"] or (0, 0, 0)), "tail": tail.hex(),
            "pricing": host._copy_pricing_record(record), "realm_inputs": realm_key,
            # 深拷贝在锁内做，序列化在锁外做（见 _usage_cache_collect）。
            "snap": host._copy_usage_snapshot(state["snap"]),
            # 按日分桶的窗口半：与 all-time 半同一次扫描折出来的，位置也
            # 跟着同一条 offset/tail 校验，不另设一套。日桶里还挂着小时
            # 子表（T9 的小时层），深拷贝必须跟着一起做。
            "days": _usage_cache_days_copy(state.get("days"),
                                           _copy_snapshot_day),
            "days_floor": state.get("days_floor"),
            # 小时层的剪枝线（见 _hours_cover）：缺了它重启后的小时层要么
            # 不折（None 直接挡住 _usage_hour_bucket），要么服务了被剪过的
            # 天——两者都不对，所以它和日桶一起落盘。
            "hours_floor_at": state.get("hours_floor_at"),
            # 闭窗口的边沿账（见 _closed_window_hit_record）：直到某刻没有行
            # 落在整秒上的证明。旧 checkpoint 没有这两个键，采用端因此
            # 拒整份、升级后一次性重折。
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

    两份位置都要带上：usage.jsonl 的（加载端靠 _usage_cache_position_ok
    校验）和事件日志的（很小，加载端整份比对）。缓冲以 "uid\\x1fmodel" 为
    键——JSON 的对象键只能是字符串。
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
            # 闭窗口的边沿账（见 _closed_window_hit_record）：旧 checkpoint
            # 没有这两个键，采用端因此拒整份、升级后一次性重折。
            "hits": list(state["hits"]),
            "hits_overflow": bool(state["hits_overflow"])}

def _usage_cache_series_entry(realm, state):
    """时间序列状态的 JSON 形态；同上，只写折叠成功过的。

    这一份最大（日桶 + 24 小时子桶 × 保留天数），所以深拷贝只拷桶本身，
    credit 缓冲是 dict 列表、直接浅拷外层。分钟环再压一层：落盘用位置数组，
    不用字典桶（见 _usage_cache_minutes_copy），满环 361 个桶也就 10KB
    量级（本机实测每桶 52 字节），字典形状会平白多出几十 KB。
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
            # 闭窗口的边沿账（见 _closed_window_hit_record）：旧 checkpoint
            # 没有这两个键，采用端因此拒整份、升级后一次性重折。
            "hits": list(state["hits"]),
            "hits_overflow": bool(state["hits_overflow"])}

def _usage_cache_collect_kind(kind):
    """逐把取一类状态，做锁外可序列化的深拷贝；返回 (entries, offset) 或 None。

    每次只取一类：写盘路径按类分别调用，只重写「有推进的那一类」。状态各自
    加锁、各自拷贝，各类之间不强求同一个瞬间：加载端对每份分别校验 offset/
    tail/指纹，一份新一份旧也各自成立。锁是逐把拿、随即放开的，不存在嵌套
    ——写盘路径不能制造「snapshot -> analytics」这样的锁序边。

    entries 为空（状态都归零/不可复现）时返回 None：没得写就不写，等下一次
    折叠成功再落盘。offset 是该类各 realm 状态里的最远位置。
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
    """原子写一类状态的 checkpoint：同目录临时文件 + os.replace。

    直接在目标文件上写，崩在中间会留下半份 JSON；加载端虽然能识别（解析失败
    按没有缓存处理），但一份完整的旧文件更省事。不 fsync：这是一份加速件，
    掉电后写坏或丢掉的，加载端一律当没有缓存，不值得为它多刷一次盘。

    不排序键：checkpoint 里 dict 的键顺序就是折叠时的插入顺序（先出现的先
    排），加载回来必须与冷启动逐字节一致——响应的 JSON 字节里键序是可见的，
    而冷启动折叠出的顺序正是这份插入顺序。排序只留给 _object_digest() 那种
    「只要内容一样就算一样」的指纹。

    盘上「本进程还没碰过」的 realm 条目原样保留：一类里按 realm 分条目，
    写盘只重写活跃 realm 的那些；不保留的话，重启后先折的 realm 触发一次写盘
    就会把别的 realm 的旧条目整份清掉，那些 realm 下次请求只能重折（真机
    实测：重启后先请求 realm=all 再请求 realm=intl，intl 重折 15s）。保留的
    条目仍要过加载端的逐条校验，位置/指纹对不上照样被拒——宁可多扫一次。
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
    """一次折叠成功后，按节流阈值决定要不要把「有推进的类」落盘。

    调用点必须在状态锁之外：这里要逐把取状态，在状态锁里再取别的状态锁
    会构成锁序环（snapshot -> analytics 与反向同时存在）。任何异常都吞掉——
    落盘失败只损失一次加速，不能影响这次刷新。

    写盘按类分开（v4）：没推进的类连文件都不重写——旧布局里一份文件装五类，
    任何一类推进都要整份重写；现在单视图进程只重写自己那一类的几十 KB。

    写哪几类：字节阈值到点的类（progress - checkpointed >= min_bytes）里，写
    调用方那一类（它刚折完，状态最新）和真正推进过的类；计时器到点而没有任何
    类到字节阈值时，写所有推进过的类。字节阈值设成 0（测试/探针的强制写）时
    「到点」对所有折过的类都成立，过滤器把没推进过的那些留在原地——旧布局里
    它们的内容与盘上逐字节相同，重写只是写放大。
    """
    global _usage_cache_last_attempt
    if not _usage_cache_enabled():
        return
    try:
        # 串行化整段写盘：两个请求线程可能同时判定「该写」，而临时文件名是
        # 固定的（path + ".tmp"，和 save_runtime_override 一致），两个写者
        # 同时打开同一个临时文件会互相截断。等锁的一方在锁里会重新判定一遍，
        # 第一个写者已经把 checkpointed 推上去，于是它多半直接返回。
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
                # 计时器分支只在**确有推进**时才允许触发：进度没动时写出的
                # 是一份与上次逐字节相同的 checkpoint，纯写放大——面板开着
                # 就每 900s 白写一份 120KB（复现见套件 [7]）。空闲重写对
                # 「重启后免冷扫」没有任何贡献，只有日志增长才值得落盘。
                advanced = [k for k in _usage_cache_progress
                            if _usage_cache_progress.get(k, 0)
                            > _usage_cache_checkpointed.get(k, 0)]
                if not due_kinds and (not advanced
                                      or (now - _usage_cache_last_attempt)
                                      < min_seconds):
                    return
                # 记「尝试」，不记「成功」：只读文件系统上失败会一直成立，
                # 不退避的话每次刷新都去试写一遍。
                _usage_cache_last_attempt = now
                write_kinds = [k for k in due_kinds
                               if k == kind or k in advanced] or advanced
            # 某一类收集不出东西（状态归零/归属不可复现）就跳过它：其余类照常
            # 落盘，互不牵连——这是拆文件的意义所在。
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
