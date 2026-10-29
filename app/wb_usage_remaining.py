"""剩余用量估算：撞线事件、窗口预算与按天归档。

上游给「账号 × 模型」的免费额度是一个 24 小时窗口，预算只能从撞线前的用量
反推。本模块维护窗口状态与 estimate-daily.json，输出剩余载荷与调度权重。
网关运行态与日志位置经 wb_usage 的活值读取，测试替换网关属性后立即生效。
"""

import bisect
import json
import os
import threading
import time

import wb_usage_cache

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
# 剩余用量估算。
#
# 上游给「账号 × 模型」的免费额度是一个 24 小时窗口：用满时 429（code 6004）
# 带上重置时刻，到点计数归零，所以窗口起点就是重置时刻。窗口预算上游不给，
# 只能把 429 之前 24 小时的用量加总反推。
#
# 两个来源按「账号 + 模型 + 重置时刻」去重：limit-events.jsonl 由请求路径在
# 撞线那一刻写，必须实时写，因为重试循环可能接连撞几个账号，只有最后一个会
# 留下带账号归属的 429 用量行；usage.jsonl 里带账号的 429 行补齐缺失记录。
# 样本是重置前 24 小时内该账号在该模型上的成功用量；预算按区域先按账号平均、
# 再跨账号平均；每账号剩余 = 预算 − 自窗口起点以来的用量。
#
# 重置时刻仍在未来说明窗口已用满，剩余记 0；没有锚点时按最近 24 小时用量保守
# 估计并标 estimated。锚点也来自凭证档的 model_caps，本次进程没撞线时照它算。
#
# 分段记账：窗口在撞线处截断，恢复时刻起新窗口重新记账，旧窗口用量不计入。
# 载荷每行带 segments（start/end/used，截断段标 capped），预算样本同样按段截断。
#
# 按需过滤：昨天与今天都没消耗的模型不参与计算（见 _remaining_activity_floor），
# 其预算样本仍留在载荷里标 active=False。按模型的总剩余 = 各可用组剩余之和，
# 冷却或已满的组记 0，没有预算的组计入 unknown。
#
# 按天持久化到 estimate-daily.json，估算取最近 3 天加权平均（今天 6、昨天 3、
# 前天 1），只在有样本的天之间归一。跨天时把刚过去的一天算成 {预算, 分段} 合并
# 进文件，该天已存在就不覆盖；预算留 3 天，分段留 2 天。归档计算在状态锁内，
# 写盘在锁外，避免锁序环。
#
# 查询侧：每个组合的缓冲是时间戳与累计和两个列表，窗口求和是两次二分加一次
# 相减；载荷挂 WB_REMAINING_TTL（默认 15 秒），TTL 内直接返回上次结果，带
# If-None-Match 时命中 304。
# ---------------------------------------------------------------------------
LIMIT_WINDOW_SECONDS = 24 * 3600

_ESTIMATE_SCHEMA = 1

# 估算窗口的权重：今天 6 / 昨天 3 / 前天 1，就近高；只在有样本的天之间归一
# （某天没样本就跳过）。
_ESTIMATE_WEIGHTS = (6, 3, 1)
# 保留：预算留最近这么多天（与估算窗口一致，多留无用——估算读不到）；分段
# 只留最近 _ESTIMATE_SEGMENT_DAYS 天（用户要求「仅保留到第二天」，多留一天
# 防跨天边界），更早的天删掉 segments 字段、预算保留到窗口外一天才整条删。
_ESTIMATE_KEEP_DAYS = len(_ESTIMATE_WEIGHTS)
_ESTIMATE_SEGMENT_DAYS = 2
# 缓冲多留 2h：查询侧的窗口起点最远只能到 now-24h，留出余量避免边界丢行。
_REMAINING_KEEP_SECONDS = 26 * 3600
# 事件只留最近这么多条：预算估计吃的是近期样本，几十条足够，封顶防日志异常
# 时无界增长（每条只记数字与 uid/model，一千条不到 200KB）。文件本身不裁剪，
# 冷启动全读一遍也就几十毫秒。
_REMAINING_MAX_EVENTS = 1000
# 载荷缓存时长（秒）。比面板的 5 秒轮询长、远短于其它统计接口的 900 秒统计 TTL：
# 数字最多滞后这么久，但一轮重建能服务好几轮轮询。
_REMAINING_TTL = float(os.environ.get("WB_REMAINING_TTL", 15))

_remaining_state = {
    "events": [],          # 事件列表，src 标来源（file / log）
    "keys": set(),         #（账号, 模型, 重置时刻）去重
    "usage": {},           # (uid, model) -> _pair_buffer() 的 {at,cum,head,base}
    "log": {"offset": 0, "key": None, "tail": b""},      # usage.jsonl 的续读位
    "file": {"offset": 0, "key": None, "tail": b""},     # limit-events.jsonl 的
    # 内存字段（不进 checkpoint，schema 不动）：事件表当前覆盖到哪个本地日，
    # 跨天归档的触发依据。重启后为空 -> 第一次构建按「刚过去的那天」试归档
    # 一次（幂等；数据不在就跳过），再置为今天。
    "day": "",
}
_remaining_state_lock = threading.Lock()
# 载荷缓存（与上面的折叠状态分开两把锁：重建要持状态锁做扫描，命中缓存不该等它）。
_remaining_cache = {"at": 0.0, "built_at": 0.0, "data": None}
_remaining_cache_lock = threading.Lock()
# estimate-daily.json 的解析缓存：条目是一次赋值的二元组，即文件戳与 days
# 两项，读侧不加锁也不会看到半份缓存；写盘路径主动换掉它，外部改动靠 stat
# 变化发现。路径也在戳里：--usage-dir 换目录后不会被旧目录的戳骗过去。
_estimate_daily_cache = {"entry": ((None, None, None), {})}
# 文件读-改-写串行锁：只在 _estimate_daily_sync() 里拿一次，且永远在状态锁
# 之外（归档计算在状态锁内、写盘在锁外，同 checkpoint 的理由）。
_estimate_daily_lock = threading.Lock()

def _pair_buffer(state, key):
    """「账号 × 模型」的一个 (at, cum) 缓冲，按需创建。

    用两条平行列表，不用 (at, tokens) 元组队列：窗口求和是两次二分加一次相减，
    顺序追加是 O(1)，乱序到达时做一次有序插入。只留时间戳与累计和，行的
    token 数折进 cum 后不再读取。
    head 是第一个存活下标，base 是被裁掉部分的累计和；cum 从第一行起算，
    左端落在第一个存活条目时减 base，前一项不存在。不要改用 array：
    打包运行时的 REQUIRED_FILES 不含它。
    """
    buf = state["usage"].get(key)
    if buf is None:
        buf = state["usage"][key] = {"at": [], "cum": [], "head": 0, "base": 0}
    return buf

def _pair_append(buf, at, tokens):
    """并入一个 (at, tokens) 样本，保持缓冲按 at 有序。

    行按日志顺序到达，但 at 是请求完成时刻，两个线程可能差几毫秒收尾而乱序。
    _range_sum 依赖二分，一次逆序就会让窗口求和跳过或重复计算整段数据，所以
    这里做有序插入。常见的顺序追加仍是 O(1)。
    """
    # 日志行是自由文本，缓冲只放数字
    tokens = int(tokens)
    at = float(at)
    ats = buf["at"]
    if not ats or at >= ats[-1]:
        ats.append(at)
        buf["cum"].append((buf["cum"][-1] if buf["cum"] else 0) + tokens)
        return
    cum = buf["cum"]
    head = buf["head"]
    index = bisect.bisect_right(ats, at)
    if index < head:
        # 这个样本落在死前缀之前：先把死前缀压缩掉（绝对 base 规则与
        # _pair_trim 相同），刚折进来的行按定义就是存活的，绝不能落在
        # `head` 之后。
        buf["base"] = cum[head - 1]
        del ats[:head]
        del cum[:head]
        buf["head"] = 0
        index = bisect.bisect_right(ats, at)
    ats.insert(index, at)
    # cum 存的是绝对前缀和，`base` 也算在内。
    cum.insert(index, (cum[index - 1] if index else buf["base"]) + tokens)
    for i in range(index + 1, len(cum)):
        cum[i] += tokens

def _pair_repair(buf):
    """把可能乱序的缓冲整理成规范形态；返回 False 表示无法修复。

    旧格式的 checkpoint 按日志顺序保存，at 可能倒退几毫秒；加载时对存活区
    按 at 稳定排序并重建 cum/head/base，每个 (at, amount) 配对都保留，窗口
    求和与在有序序列上重算一致。负的增量是坏数据，返回 False 让调用方退回
    冷折叠。
    """
    at = buf["at"]
    cum = buf["cum"]
    head = buf["head"]
    if len(at) != len(cum) or not 0 <= head <= len(at):
        return False
    if head >= len(at):
        return False
    origin = cum[head - 1] if head else buf["base"]
    pairs = []
    prev = origin
    for i in range(head, len(cum)):
        amount = cum[i] - prev
        if amount < 0:
            return False
        pairs.append((at[i], amount))
        prev = cum[i]
    pairs.sort(key=lambda item: item[0])
    rebuilt_at = []
    rebuilt_cum = []
    total = origin
    for value, amount in pairs:
        rebuilt_at.append(value)
        total += amount
        rebuilt_cum.append(total)
    buf["at"] = rebuilt_at
    buf["cum"] = rebuilt_cum
    buf["head"] = 0
    buf["base"] = origin
    return True

def _pair_trim(buf, cutoff):
    """丢弃早于 `cutoff` 的条目（批量处理：只有死前缀足够大、值得付出
    memmove 代价时才真正压缩列表）。"""
    at = buf["at"]
    while buf["head"] < len(at) and at[buf["head"]] < cutoff:
        buf["head"] += 1
    if buf["head"] > 512 and buf["head"] * 2 > len(at):
        # base 是「被裁掉前缀的累计和」的**绝对值**，不是增量：cum 从最早的一行
        # 累计、切片不重置，所以 cum[head-1] 已经把之前裁掉的都算进去了。写成
        # += 会让第二次压缩起把前缀重复计入，之后任何左端落在保留区之前的
        # 区间求和都会变成负数（真机上「上段」显示过 -1293.5M）。
        buf["base"] = buf["cum"][buf["head"] - 1]
        del at[:buf["head"]]
        del buf["cum"][:buf["head"]]
        buf["head"] = 0

def _range_sum(buf, lo, hi):
    """一个 (account, model) 在 lo <= at <= hi 内的 token 数。O(log n)。"""
    if buf is None or buf["head"] >= len(buf["at"]):
        return 0
    at = buf["at"]
    j = bisect.bisect_left(at, lo, buf["head"])
    k = bisect.bisect_right(at, hi, buf["head"])
    if k <= j:
        return 0
    # cum 自最早的缓冲行累计；左端落到第一个存活条目时（j 在列表头），
    # 前面的累计值已被裁掉，只能拿 base 补回那段前缀。
    low = buf["cum"][j - 1] if j > 0 else buf["base"]
    return buf["cum"][k - 1] - low

def _range_sum_open(buf, lo, hi):
    """一个 (account, model) 在 lo <= at < hi 内的 token 数。O(log n)。

    与 _range_sum 只差右端：日界用左闭右开（恰好落在午夜那一刻的行属于后一
    天），归档的当天用量与分段都按这个口径算，pre + post 的不变式在整秒边界
    上也不会多算一行。
    """
    if buf is None or buf["head"] >= len(buf["at"]):
        return 0
    at = buf["at"]
    j = bisect.bisect_left(at, lo, buf["head"])
    k = bisect.bisect_left(at, hi, buf["head"])
    if k <= j:
        return 0
    low = buf["cum"][j - 1] if j > 0 else buf["base"]
    return buf["cum"][k - 1] - low

def _merge_event(state, event, source):
    """并入一条撞线事件，按 (account, model, reset) 去重。

    同一个撞线先记的来源算数：请求路径的事件在处理 429 的那一刻就写好，
    日志推导的那条要等扫描读到那一行才出现。
    """
    uid = str(event.get("account") or "")
    model = str(event.get("model") or "")
    try:
        reset = float(event.get("reset") or 0)
    except (TypeError, ValueError):
        return
    if not uid or not model or reset <= 0:
        return
    key = (uid, model, reset)
    if key in state["keys"]:
        return
    state["keys"].add(key)
    state["events"].append({
        "at": float(event.get("at") or 0), "reset": reset, "account": uid,
        "model": model, "realm": str(event.get("realm") or ""),
        "sample": int(event.get("sample") or 0), "src": source,
    })
    if len(state["events"]) > _REMAINING_MAX_EVENTS:
        # 从头部丢弃时同步清去重键：丢掉的键将来重新出现（重放日志）会再被
        # 收进来，不会因为键还留着而被静默跳过。
        dropped = state["events"][: len(state["events"]) - _REMAINING_MAX_EVENTS]
        del state["events"][: len(dropped)]
        for old in dropped:
            state["keys"].discard((old["account"], old["model"], old["reset"]))

def _drop_events(state, source):
    """丢掉一个来源贡献的全部事件（它的文件被重写过）。"""
    kept = [event for event in state["events"] if event["src"] != source]
    for event in state["events"]:
        if event["src"] == source:
            state["keys"].discard((event["account"], event["model"], event["reset"]))
    state["events"] = kept

def _window_sample(usage, uid, model, reset_at, at, start=None):
    """(uid, model) 在以 `reset_at` 结尾的窗口内的用量。

    `start` 覆盖默认的窗口起点（重置时刻 − 24h）：链式撞线（上一个重置时刻
    落在 24h 之内）时窗口从上一个重置时刻起算，旧窗口的用量不进这个样本。
    """
    lo = reset_at - LIMIT_WINDOW_SECONDS if start is None else start
    return _range_sum(usage.get((uid, model)), lo, at)

def _cap_anchor(entries, at, reset):
    """把一个撞线锚点（撞线时刻 at + 恢复时刻 reset）并进 entries。

    entries 以 reset 为键去重：同一个重置时刻从事件日志与凭证档各来一份时
    只算一个锚点；at 取两者中较晚的（这个窗口最后被观测到还在消耗的时刻，
    截断点宁晚勿早——截早了会漏掉真实计过量的行）。
    """
    try:
        at = float(at or 0)
        reset = float(reset or 0)
    except (TypeError, ValueError):
        return
    if reset <= 0:
        return
    old = entries.get(reset)
    if old is None or at > old["at"]:
        entries[reset] = {"at": at, "reset": reset}

def _cap_anchors(events, uid, model):
    """「账号 × 模型」的锚点表，来自事件日志里该组合的全部事件。"""
    entries = {}
    for event in events:
        if event["account"] == uid and event["model"] == model:
            _cap_anchor(entries, event["at"], event["reset"])
    return entries

def _cap_window_start(entries, reset):
    """窗口起点：链上上一个重置时刻（落在 24h 内时），否则重置时刻 − 24h。

    上游的窗口锚定比「固定 24h 链」复杂（实测同组合的重置时刻有 48h+ 的空
    档），所以只用上游给的重置时刻这一个事实：上一个重置时刻落在 24h 窗口
    之内时它就是本窗口的起点（链式撞线，旧窗口的用量不得计进新窗口）；之
    外时窗口不超过 24h，起点取重置时刻 − 24h。
    """
    prev = 0.0
    for other in entries:
        if other < reset and other > prev:
            prev = other
    return max(prev, reset - LIMIT_WINDOW_SECONDS)

def _remaining_activity_floor(now):
    """「昨日 00:00」：本地时区里 now 所在日的前一天零点。

    参与剩余用量计算的判定下界（昨天或今天有消耗才算在用）。取整天边界，
    不取 now − 24h，因为需求写的是「昨日或当日」；同一天里这个下界不变，
    面板因此不会在轮询之间来回抖动。
    """
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return midnight - LIMIT_WINDOW_SECONDS

def _save_account_window(account):
    """把 429 的恢复时刻写进凭证档（best-effort）。

    调用方先改好 cap 或 hold，再调用本函数落盘。写盘失败只影响「重启后是否
    记得」，不影响本次请求——真正的停用由内存里的模型冷却负责，重启后的补挂
    交给 AccountPool.restore_model_caps() / restore_model_holds()。
    """
    try:
        account.save(os.path.dirname(account.path) if account.path else _gw().ACCOUNTS_DIR)
    except Exception as exc:
        _gw().log("account %s: model window write failed: %s" % (account.uid[:8], exc))

def note_limit_event(account, model, reset_at):
    """撞线当场记一条模型级的上游额度事件（429 / code 6004）。

    在 429 被分类的那一刻由请求路径调用：样本对着此处现刷新的用量折叠计算，
    因为导致撞线的行已经写进日志、只是可能还没被扫描到。一切都是
    best-effort：记录失败绝不能弄坏本来就在失败的那个请求。
    """
    host = _host()
    try:
        uid = getattr(account, "uid", "") or ""
        if not uid or not model or not reset_at:
            return
        at = time.time()
        with _remaining_state_lock:
            _remaining_refresh(_remaining_state)
            anchors = _cap_anchors(_remaining_state["events"], uid, model)
            # 凭证档里的最近一次撞线（note_model_cap 还没记这次）也能锚住窗口
            # 起点：日志里没有上一个重置时刻时它是唯一的分段依据。
            cap = (getattr(account, "model_caps", None) or {}).get(model)
            if isinstance(cap, dict):
                _cap_anchor(anchors, cap.get("at"), cap.get("reset"))
            sample = _window_sample(_remaining_state["usage"], uid, model,
                                    reset_at, at,
                                    _cap_window_start(anchors, reset_at))
        event = {
            "at": at, "account": uid, "model": model,
            "realm": getattr(account, "realm", "") or "",
            "reset": float(reset_at), "sample": sample,
        }
        try:
            host._ensure_dir(_gw().USAGE_DIR)
            with host._open_append(_gw().LIMIT_EVENTS_FILE) as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as exc:
            _gw().log("limit event not persisted: %s" % exc)
        with _remaining_state_lock:
            _merge_event(_remaining_state, event, "file")
    except Exception as exc:
        _gw().log("limit event not recorded: %s" % exc)

def _fold_remaining(row, state):
    """把一行 usage.jsonl 折叠进剩余用量状态。

    行序就是日志的追加顺序，所以下面的裁剪直接拿每行自己的 `at` 当「现在」：
    行只会越来越新，早于保留窗口的行再也不会被用到。
    """
    host = _host()
    uid = row.get("account")
    model = row.get("model")
    if not uid or not model:
        return
    at = row.get("at") or 0
    if not at:
        return
    key = (uid, model)
    if row.get("status") == 429:
        reset = _gw().parse_rate_limit_reset(str(row.get("message") or ""))
        if reset is None:
            return
        # 样本按段截断：链式撞线时从上一个重置时刻起算（旧窗口的用量不进
        # 新窗口的样本），没撞过线时就是默认的重置时刻 − 24h。
        anchors = _cap_anchors(state["events"], uid, model)
        _merge_event(state, {
            "at": at, "reset": reset, "account": uid, "model": model,
            "realm": row.get("realm") or "",
            "sample": _window_sample(state["usage"], uid, model, reset, at,
                                     _cap_window_start(anchors, reset)),
        }, "log")
        return
    if row.get("error") or row.get("usage_missing"):
        return
    if host.row_outcome(row) == "client_aborted":
        return
    buf = _pair_buffer(state, key)
    _pair_append(buf, at, row.get("total_tokens") or 0)
    _pair_trim(buf, at - _REMAINING_KEEP_SECONDS)

def _remaining_refresh(state):
    """把两个文件的新行折叠进 `state`；调用方持有状态锁。

    与其它折叠同一套规则：文件读不到或被重写，就作废它贡献过的记录，不发布
    半份折叠结果。先折用量日志，让事件日志的样本（撞线时对着它算）与日志
    推导的事件描述同一批缓冲。

    全新进程先问 checkpoint：冷扫描是这里唯一随日志增长的开销（一天几 MB），
    重启不该再付一遍。缺失或过期 -> 从零折叠。
    """
    host = _host()
    if state["log"].get("key") is None:
        wb_usage_cache._usage_cache_adopt_remaining(state, host._usage_log_key())
    for source, path in (("log", _gw().USAGE_LOG), ("file", _gw().LIMIT_EVENTS_FILE)):
        slot = state[source]
        log_key = host._usage_log_key(path)
        if not host._log_resume_ok(slot, log_key, path):
            # 文件被换掉/截短：这个来源的贡献全部作废，从零重折。
            state[source] = {"offset": 0, "key": log_key, "tail": b""}
            if source == "log":
                state["usage"].clear()
            _drop_events(state, source)
        fold = (lambda row: _fold_remaining(row, state)) if source == "log" \
            else (lambda row: _merge_event(state, row, "file"))
        offset, error = host._scan_usage_from(state[source]["offset"], fold, path=path)
        if error is None:
            state[source]["offset"] = offset
            state[source]["tail"] = host._log_tail_signature(offset, path)
        else:
            _gw().log("remaining usage fold failed (%s): %s" % (source, error))
            state[source] = {"offset": 0, "key": log_key, "tail": b""}
            if source == "log":
                state["usage"].clear()
            _drop_events(state, source)

def _budget_means(per_account):
    """{账号: [样本]} -> {avg, min, max, n}：先按账号平均、再跨账号平均。

    一个账号反复撞线只算一个样本（它自己样本的均值），所以跨账号平均称的是
    账号；min/max/n 都是账号均值的极值与个数（沿用既有口径）。
    """
    means = [int(round(sum(v) / float(len(v)))) for v in per_account.values()]
    return {"avg": int(round(sum(means) / float(len(means)))),
            "min": min(means), "max": max(means), "n": len(means)}

def _estimate_budget_entry_ok(entry):
    """归档文件里一天的口径：{avg,min,max,n} 都是数字且 n > 0 才算数。

    文件在数据目录里、可能被手改或半写坏：任何一项不合形状就跳过这一天，
    估算退化成「那天没样本」，绝不因此让载荷构建报错。
    """
    if not isinstance(entry, dict):
        return False
    if not wb_usage_cache._usage_cache_int(entry.get("n")) or entry["n"] <= 0:
        return False
    return all(wb_usage_cache._usage_cache_number(entry.get(field))
               for field in ("avg", "min", "max"))

def _estimate_day_keys(now):
    """估算窗口的日键，就近在前：[今天, 昨天, 前天]。"""
    host = _host()
    return [host._local_day_key(host._local_midnight(now, days_back=back))
            for back in range(_ESTIMATE_KEEP_DAYS)]

def _estimate_daily_stamp():
    """estimate-daily.json 的（路径, mtime_ns, size）；文件不在时后两项为 None。

    路径也在戳里：--usage-dir 换目录后，不会被旧目录的戳骗过去（两个目录里
    恰好有 mtime/大小相同的文件几乎不可能，但也没必要赌）。
    """
    try:
        st = os.stat(_gw().ESTIMATE_DAILY_FILE)
        return (_gw().ESTIMATE_DAILY_FILE, st.st_mtime_ns, st.st_size)
    except OSError:
        return (_gw().ESTIMATE_DAILY_FILE, None, None)

def _estimate_daily_read():
    """estimate-daily.json 的 days 表；坏文件/读不到按空表处理。

    按戳记忆解析结果：写盘路径主动刷新缓存，外部改动（手改、另一个进程）靠
    stat 变化发现。这是估算的加速件，任何一步不成立都只是回到「没有归档」
    （今天现算、更早的天跳过），绝不能因此报错。
    """
    stamp = _estimate_daily_stamp()
    cached_stamp, cached_days = _estimate_daily_cache["entry"]
    if cached_stamp == stamp:
        return cached_days
    days = {}
    if stamp[1] is not None:
        try:
            with open(_gw().ESTIMATE_DAILY_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict) and data.get("schema") == _ESTIMATE_SCHEMA \
                and isinstance(data.get("days"), dict):
            days = {day: record for day, record in data["days"].items()
                    if wb_usage_cache._usage_cache_day_key_ok(day)
                    and isinstance(record, dict)}
    _estimate_daily_cache["entry"] = (stamp, days)
    return days

def _estimate_daily_write(days):
    """原子写 estimate-daily.json（同目录 .tmp + os.replace，同 checkpoint）。"""
    host = _host()
    host._ensure_dir(_gw().USAGE_DIR)
    tmp = _gw().ESTIMATE_DAILY_FILE + ".tmp"
    text = json.dumps({"schema": _ESTIMATE_SCHEMA, "days": days},
                      ensure_ascii=False, separators=(",", ":"))
    try:
        with host._open_write(tmp, newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, _gw().ESTIMATE_DAILY_FILE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _estimate_daily_view(daily, archived):
    """文件表 + 本次归档的内存视图：该天已在文件里就用文件里的（幂等口径）。

    本次构建的估算直接读这份视图，跨天那一轮的载荷不用等下一轮才看到昨天；
    已被持久化下来的那天不会被这次重算的值盖掉（「调昨天的数据不重新计算」）。
    """
    if not archived:
        return daily
    view = dict(daily)
    for day, record in archived.items():
        view.setdefault(day, record)
    return view

def _estimate_daily_sync(archived, now):
    """把归档记录合并进文件、按保留策略裁剪；内容不变就不写盘。

    幂等：该天已在文件里就不覆盖（重启/重复构建不会写第二遍、也不会用重算的
    值盖掉持久化下来的值）。保留：只留估算窗口内的最近 3 天，更早的天整条
    删掉；分段只留最近 2 天（用户要求「仅保留到第二天」，多留一天防跨天
    边界），更早的天删掉 segments 字段、预算保留到 3 天。

    写盘在状态锁外做（与 checkpoint 同款理由：写盘在状态锁里会构成锁序环），
    用写锁串行、原子替换；失败只记日志——这是估算的加速件，不影响请求。
    """
    with _estimate_daily_lock:
        days = dict(_estimate_daily_read())
        changed = False
        for day, record in (archived or {}).items():
            if day not in days:
                days[day] = record
                changed = True
        keep = _estimate_day_keys(now)
        segment_days = set(keep[:_ESTIMATE_SEGMENT_DAYS])
        pruned = {}
        for day, record in days.items():
            if day not in keep:
                changed = True
                continue
            if day not in segment_days and record.get("segments"):
                record = dict(record)
                record.pop("segments", None)
                changed = True
            pruned[day] = record
        if changed:
            try:
                _estimate_daily_write(pruned)
            except Exception as exc:
                _gw().log("estimate daily write failed: %s" % exc)
        # 缓存换成刚算好的表（含本次归档与裁剪）：写失败也不重试——这份表就是
        # 「本进程认为文件里有什么」，与 checkpoint 的「记尝试不记成功」同款。
        _estimate_daily_cache["entry"] = (_estimate_daily_stamp(), pruned)

def _remaining_archive_due(state, now, realm_of):
    """跨天（或重启后第一次构建）时归档刚过去的那天。

    调用方持有状态锁（归档要读事件表与用量缓冲）。返回 {日键: 记录}，没跨天
    或没有可归档的数据返回 None。state["day"] 是内存字段、不进 checkpoint：
    重启后为空，第一次构建按「刚过去的那天」试一次——幂等，数据不在就跳过，
    再置为今天（无论归档成没成）。
    """
    host = _host()
    today = host._local_day_key(now)
    stale = state.get("day") or ""
    if stale == today:
        return None
    state["day"] = today
    if not stale or stale >= today:
        # 重启后为空（或时钟回拨、stale 落在未来）：按「刚过去的那天」= 昨天。
        stale = host._local_day_key(host._local_midnight(now, days_back=1))
    if stale == today:
        return None
    record = _remaining_archive_day(state, stale, realm_of)
    return {stale: record} if record else None

def _remaining_archive_day(state, day, realm_of):
    """一天的量归档：当天样本的预算口径 + 每个组合的截断前后分段。

    budgets：撞线时刻 event["at"] 落在那天的样本，先按账号平均、再跨账号
    平均（沿用查询侧口径）。segments：那天每个 (uid, model) 的 pre/post。
    两样都空（数据不在缓冲里 / 当天没有样本）返回 None——调用方跳过不写。
    """
    host = _host()
    start = host._day_key_start(day)
    end = start + 86400
    events = [event for event in state["events"]
              if host._local_day_key(event["at"]) == day]
    samples = {}
    for event in events:
        if event["sample"] <= 0:
            continue
        realm = event["realm"] or realm_of.get(event["account"], "")
        samples.setdefault((realm, event["model"]), {}) \
               .setdefault(event["account"], []).append(event["sample"])
    budgets = {"%s\x1f%s" % key: _budget_means(per_account)
               for key, per_account in samples.items()}
    segments = _remaining_archive_segments(state, events, start, end)
    if not budgets and not segments:
        return None
    return {"budgets": budgets, "segments": segments}

def _remaining_archive_segments(state, events, start, end):
    """那天每个 (uid, model) 的截断前 / 截断后用量。

    post = 恢复时刻落在当天的窗口在当天内的用量之和（从恢复时刻到「下一个
    撞线时刻或当天结束」）；pre = 当天该组合总用量 − post（不变式：pre + post
    == 当天该组合的用量，用缓冲的区间求和算）。两个都是 0 的组合不写。
    """
    caps = {}
    for event in events:
        caps.setdefault((event["account"], event["model"]), []).append(
            (event["at"], event["reset"]))
    segments = {}
    for (uid, model), buf in state["usage"].items():
        total = _range_sum_open(buf, start, end)
        pair_caps = caps.get((uid, model)) or ()
        post = 0
        for _at, reset in pair_caps:
            if not start <= reset < end:
                continue
            nxt = end
            for other_at, _other_reset in pair_caps:
                if reset < other_at < nxt:
                    nxt = other_at
            post += _range_sum_open(buf, reset, min(nxt, end))
        if total <= 0 and post <= 0:
            continue
        segments["%s\x1f%s" % (uid, model)] = {"pre": total - post, "post": post}
    return segments

def _remaining_budgets(events, realm_of, now, daily):
    """(realm, model) -> 近 3 天加权的窗口预算估计。

    样本按撞线当天的本地日期分桶，取最近 3 天（今天/昨天/前天）：今天用内存
    里的事件现算，更早的天直接读 estimate-daily.json 里归档好的当天口径（不
    再重算——这就是持久化的意义）。权重就近高（今天 6 / 昨天 3 / 前天 1，见
    _ESTIMATE_WEIGHTS），只在有样本的天之间归一，某天没样本就跳过。只有撞过
    6004 的窗口才有样本（天然成立），某组合 3 天内没有样本就没有预算。

    载荷里保留 avg/min/max/n（min/max 取各天账号均值的极值，n 为各天账号数
    之和），并新增 days 列出各天构成（{day, w, avg, n}）供面板/排查用。
    """
    host = _host()
    keys = _estimate_day_keys(now)
    per_day = {}
    samples = {}
    for event in events:
        if event["sample"] <= 0 or host._local_day_key(event["at"]) != keys[0]:
            continue
        realm = event["realm"] or realm_of.get(event["account"], "")
        samples.setdefault((realm, event["model"]), {}) \
               .setdefault(event["account"], []).append(event["sample"])
    for key, per_account in samples.items():
        per_day.setdefault(key, {})[keys[0]] = _budget_means(per_account)
    # 昨天/前天：直接读归档文件里当天的口径（avg/min/max/n 都是现成的）。
    for day in keys[1:]:
        record = (daily or {}).get(day) or {}
        for pair_key, entry in (record.get("budgets") or {}).items():
            if not _estimate_budget_entry_ok(entry):
                continue
            realm, _, model = pair_key.partition("\x1f")
            if not model:
                continue
            per_day.setdefault((realm, model), {})[day] = entry
    budgets = {}
    for key, days in per_day.items():
        rows = []
        for index, day in enumerate(keys):
            entry = days.get(day)
            if not entry or entry.get("n", 0) <= 0:
                continue
            rows.append({"day": day, "w": _ESTIMATE_WEIGHTS[index],
                         "avg": int(round(entry["avg"])), "n": int(entry["n"])})
        if not rows:
            continue
        weight = sum(row["w"] for row in rows)
        budgets[key] = {
            "avg": int(round(sum(row["w"] * row["avg"] for row in rows)
                             / float(weight))),
            "min": min(int(round(days[row["day"]]["min"])) for row in rows),
            "max": max(int(round(days[row["day"]]["max"])) for row in rows),
            "n": sum(row["n"] for row in rows),
            "days": rows,
        }
    return budgets

def _remaining_payload(now=None, accounts=None):
    """按账号给出相对估算预算的剩余用量。

    折叠与构建载荷在状态锁内一次完成：缓冲不复制出去，直接读累计和，重建的
    代价是新增行加上 O(pairs × log n)，不必重加全部缓冲行。

    每个启用的账号都出现在本区域有预算的每个模型上，以及它保留窗口内用过的
    模型；用量为 0 表示额度仍是满的。窗口已用满、还在冷却的组合剩余记 0，
    同时给出交还额度的重置时刻。

    昨天零点起没人消耗的模型不参与计算（按区域与模型判定，见
    _remaining_activity_floor），其预算样本仍留在载荷里并标 inactive，面板
    因此能显示为未参与。

    跨天归档：本地日期与 state["day"] 不同时，在状态锁内把刚过去的一天算成
    {预算, 分段} 并立刻并入本次视图，文件写盘在锁外。
    """
    now = time.time() if now is None else now
    if accounts is None:
        accounts = list(_gw().POOL.accounts) if _gw().POOL else []
    accounts = [a for a in accounts
                if getattr(a, "enabled", True) and getattr(a, "uid", "")]
    with _remaining_state_lock:
        state = _remaining_state
        _remaining_refresh(state)
        realm_of = {a.uid: (getattr(a, "realm", "") or "") for a in accounts}
        archived = _remaining_archive_due(state, now, realm_of)
        daily = _estimate_daily_view(_estimate_daily_read(), archived)
        payload = _remaining_payload_locked(state, now, accounts, realm_of, daily)
        offset = state["log"].get("offset") or 0
    # 归档与 checkpoint 的写盘都在锁外做（见「聚合状态落盘」一节）：写盘要逐把读
    # 各份状态，在状态锁里再取别的状态锁会构成锁序环。折叠失败的那次 offset
    # 已归零，落盘函数会跳过。
    _estimate_daily_sync(archived, now)
    wb_usage_cache._usage_cache_maybe_save("remaining", offset)
    return payload

def _remaining_payload_locked(state, now, accounts, realm_of, daily):
    """载荷本体；调用方持有状态锁且已完成刷新。"""
    budgets = _remaining_budgets(state["events"], realm_of, now, daily)
    # strftime/localtime 是仅剩的几项按行开销之一（30 行 ≈ 0.12ms，占构建的
    # 74%），且各行的窗口起点 / 重置时刻高度重合——每个不同的秒每份载荷只
    # 格式化一次。
    iso_cache = {}

    def iso_at(ts):
        key = int(ts)
        out = iso_cache.get(key)
        if out is None:
            out = iso_cache[key] = time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.localtime(ts))
        return out

    # 撞线锚点：事件日志 + 凭证档里的 model_caps，按（账号, 模型, 重置时刻）
    # 去重——重启后日志可能丢失/被裁，凭证档里持久化的记录是那时唯一的依据；
    # 反之凭证档写失败时日志还在。锚点决定冷却、窗口起点与截断点。
    anchors = {}
    for event in state["events"]:
        _cap_anchor(anchors.setdefault((event["account"], event["model"]), {}),
                    event["at"], event["reset"])
    for acct in accounts:
        caps = getattr(acct, "model_caps", None)
        if not isinstance(caps, dict):
            continue
        try:
            items = list(caps.items())
        except RuntimeError:
            # 恰好并发在记新撞线（note_model_cap 在另一把锁下改这张表）：
            # 这轮少一个锚点，下一轮重建就补上，不值得为它拿别人的锁。
            continue
        for model, entry in items:
            if isinstance(entry, dict):
                _cap_anchor(anchors.setdefault((acct.uid, str(model)), {}),
                            entry.get("at"), entry.get("reset"))

    # 按需过滤：昨天或今天没消耗过的模型不参与（判定见 _remaining_activity_floor）。
    # 消耗 = 缓冲里有行（最后一行的时间戳就是该组合最后一次用量），或撞线锚点
    # 落在下界之后——撞线是「把窗口用满」的证据，冷却中的组合不能因为旧行被裁
    # 掉就从面板上消失。
    floor = _remaining_activity_floor(now)
    active = set()
    for (uid, model), buf in state["usage"].items():
        realm = realm_of.get(uid)
        if realm is None or not buf["at"]:
            continue
        if buf["at"][-1] >= floor:
            active.add((realm, model))
    for (uid, model), entries in anchors.items():
        realm = realm_of.get(uid)
        if realm is None:
            continue
        if any(max(entry["at"], entry["reset"]) >= floor
               for entry in entries.values()):
            active.add((realm, model))

    rows = []
    totals = {}
    for acct in accounts:
        uid = acct.uid
        realm = realm_of.get(uid, "")
        models = set(model for (r, model) in budgets
                     if r == realm and (r, model) in active)
        models.update(model for (u, model) in state["usage"]
                      if u == uid and (realm, model) in active)
        for model in sorted(models):
            entries = anchors.get((uid, model)) or {}
            buf = state["usage"].get((uid, model))
            past = [e for e in entries.values() if e["reset"] <= now]
            future = [e for e in entries.values() if e["reset"] > now]
            last = max(past, key=lambda e: e["reset"]) if past else None
            upcoming = min(future, key=lambda e: e["reset"]) if future else None
            segments = []
            if upcoming is not None:
                # 窗口已用满：正在冷却，额度到恢复时刻才回来。这个窗口的记账
                # 在撞线处截断——已用到撞线时刻为止（上游已经拒收，撞线之后
                # 残留的行不算这个窗口的消耗），所以已用量≈预算，与剩余 0 对
                # 得上。窗口起点是链上上一个重置时刻（链式撞线时），否则重
                # 置时刻 − 24h。
                reset_at = upcoming["reset"]
                window_start = _cap_window_start(entries, reset_at)
                end = min(now, upcoming["at"] or reset_at)
                used = _range_sum(buf, window_start, end)
                segments.append({"start": window_start, "end": end, "used": used,
                                 "capped": True, "current": True,
                                 "start_iso": iso_at(window_start),
                                 "end_iso": iso_at(end)})
                cooling = True
                estimated = False
            elif last is not None:
                reset_at = last["reset"]
                window_start = max(reset_at, now - LIMIT_WINDOW_SECONDS)
                used = _range_sum(buf, window_start, now)
                cooling = False
                # 重置时刻超过 24h 的组合锚不住当前窗口：它那个窗口早已到期，
                # 现在这个窗口的起点未知，落在最近 24h 内的用量只是下界，与
                # 从没撞过线的组合同样标 estimated。
                estimated = reset_at < now - LIMIT_WINDOW_SECONDS
                if not estimated:
                    # 分段相加：上一段（上一个窗口，在它的撞线处截断）+ 本段
                    # （自恢复时刻起重新记账，旧窗口的用量不计进本段的已用）。
                    if last["at"] > 0:
                        prev_end = min(last["at"], reset_at)
                        prev_start = _cap_window_start(entries, reset_at)
                        segments.append({
                            "start": prev_start, "end": prev_end,
                            "used": _range_sum(buf, prev_start, prev_end),
                            "capped": True, "current": False,
                            "start_iso": iso_at(prev_start),
                            "end_iso": iso_at(prev_end)})
                    segments.append({"start": reset_at, "end": now, "used": used,
                                     "capped": False, "current": True,
                                     "start_iso": iso_at(reset_at),
                                     "end_iso": iso_at(now)})
            else:
                window_start = now - LIMIT_WINDOW_SECONDS
                used = _range_sum(buf, window_start, now)
                reset_at = None
                cooling = False
                estimated = True
            budget = budgets.get((realm, model))
            remaining = None
            pct = None
            if cooling:
                # 冷却中的组剩余记 0（没有预算也照记）：额度到恢复时刻才回来。
                remaining = 0
                pct = round(used * 100.0 / budget["avg"], 1) \
                    if budget and budget["avg"] else None
            elif budget:
                remaining = max(0, budget["avg"] - used)
                pct = round(used * 100.0 / budget["avg"], 1) if budget["avg"] else None
            rows.append({
                "uid": uid,
                "nickname": getattr(acct, "nickname", "") or "",
                "realm": realm,
                "model": model,
                "used": used,
                "budget": budget["avg"] if budget else None,
                "budget_min": budget["min"] if budget else None,
                "budget_max": budget["max"] if budget else None,
                "samples": budget["n"] if budget else 0,
                "remaining": remaining,
                "used_pct": pct,
                "window_start": window_start,
                "window_iso": iso_at(window_start),
                "reset_at": reset_at,
                "reset_iso": iso_at(reset_at) if reset_at else None,
                "cooling": cooling,
                "estimated": estimated,
                "segments": segments,
            })
            # 按模型总剩余 = 可用组的剩余之和：冷却/已满的组 remaining 本身就是
            # 0（只加不减，绝不把别的组拉低），没有预算的组计入 unknown 不计数。
            total = totals.get((realm, model))
            if total is None:
                total = totals[(realm, model)] = {
                    "realm": realm, "model": model, "total_remaining": None,
                    "budget": budget["avg"] if budget else None,
                    "pairs": 0, "available": 0, "cooling": 0,
                    "unknown": 0, "estimated": 0,
                }
            total["pairs"] += 1
            if cooling:
                total["cooling"] += 1
            if estimated:
                total["estimated"] += 1
            if remaining is None:
                total["unknown"] += 1
            else:
                total["total_remaining"] = (total["total_remaining"] or 0) + remaining
                if not cooling:
                    total["available"] += 1
    rows.sort(key=lambda r: (r["remaining"] is None,
                             r["remaining"] if r["remaining"] is not None else 0,
                             r["realm"], r["nickname"], r["model"]))
    return {
        "window_seconds": LIMIT_WINDOW_SECONDS,
        "generated_at": now,
        "generated_iso": iso_at(now),
        "activity_floor": floor,
        "activity_floor_iso": iso_at(floor),
        "budgets": [dict(budget, realm=realm, model=model,
                         active=(realm, model) in active)
                    for (realm, model), budget in sorted(budgets.items())],
        "models": sorted(totals.values(),
                         key=lambda m: (m["total_remaining"] is None,
                                        -(m["total_remaining"] or 0),
                                        m["realm"], m["model"])),
        "rows": rows,
    }

def remaining_usage(ttl=None):
    """带缓存的载荷：面板每 5 秒轮询一次，数字变化慢得多。

    TTL 内原样返回上次的载荷——不重扫任何一个文件、不重建——所以两次重建之间
    的 5 秒轮询几乎不花代价。TTL 刻意取短（WB_REMAINING_TTL，默认 15 秒）：
    长到一轮重建能服务好几轮轮询，短到撞线或一波用量能及时出现在管理员眼前的
    面板上。
    """
    ttl = _REMAINING_TTL if ttl is None else ttl
    now = time.time()
    with _remaining_cache_lock:
        entry = _remaining_cache
        if entry["data"] is not None and (now - entry["at"]) < ttl:
            return entry["data"]
    # 重建在缓存锁外：它自己会拿状态锁扫描，命中等它的调用不该被拖住。
    data = _remaining_payload()
    with _remaining_cache_lock:
        _remaining_cache.update({"at": time.time(), "built_at": time.time(),
                                 "data": data})
    return data

def remaining_usage_etag():
    """当前 /usage/remaining 缓存条目的 ETag，没有则 None。

    与 usage_by_account_etag() 同款：键是常量（这个视图只有一条缓存），配对
    调用由 _json_cached() 保证。轮询在 TTL 内命中同一份载荷 → 同一枚校验符
    → 304；重建后自动换戳。
    """
    with _remaining_cache_lock:
        entry = _remaining_cache
        if entry["data"] is None:
            return None
        built_at = entry["built_at"]
    return _gw()._cache_etag("remaining", "all", built_at)

# ---------------------------------------------------------------------------
# 剩余用量（并入智能分配的评分）
#
# 估计剩余占预算的比例越小权重越高；权重在 AccountPool._pick_scored 里作为
# 评分倾斜，智能分配的欠账与忙闲仍然决定主要份额。按档位分段，不走线性：
# 面板上说得清（剩不到 30% 开始加权、5% 以内最重），测试里也有确切数字可断言。
# 权重表由请求路径每请求推给池——数据方向与 apply_daily_* 一致，池不反向依赖
# 本模块。
# ---------------------------------------------------------------------------
_REMAINING_PRIORITY_BANDS = ((0.05, 4), (0.15, 3), (0.30, 2))

def _remaining_schedule_weight(remaining, budget):
    """估计剩余对应的调度权重：1 = 不加权，2..4 = 越接近耗尽越重。

    没有可比较的尺度时不猜：预算样本缺失（budget 为 None/0）或剩余未知
    （remaining 为 None）一律不加权。剩余 <= 0 也不加权——它没有「先用掉」
    的意义（真正用满的组合正在冷却，本来就不可用；estimated 口径下它只是
    下界，不值得把流量往一个可能已经耗尽的组合上压）。
    """
    try:
        remaining = float(remaining)
        budget = float(budget)
    except (TypeError, ValueError):
        return 1
    if remaining <= 0 or budget <= 0:
        return 1
    ratio = remaining / budget
    for ceiling, weight in _REMAINING_PRIORITY_BANDS:
        if ratio <= ceiling:
            return weight
    return 1

def remaining_schedule_weights():
    """{(uid, model): 权重}：估计剩余接近耗尽的组合，权重 > 1。

    只读剩余估算载荷的既有字段（rows 的 uid / model / remaining / budget），
    不碰折叠与预算的实现。载荷自带短 TTL：面板在轮询时本来就维持着它，请求
    路径这里拿到的基本都是缓存，重建只发生在 TTL 到点时（与面板同款成本）。
    best-effort：任何失败都只意味着这一次不加权，绝不能连累请求本身。
    """
    try:
        payload = remaining_usage()
        weights = {}
        for row in payload.get("rows") or []:
            weight = _remaining_schedule_weight(row.get("remaining"),
                                                row.get("budget"))
            if weight > 1:
                weights[(row.get("uid") or "", row.get("model") or "")] = weight
        return weights
    except Exception as exc:
        _gw().log("remaining priority weights unavailable: %s" % exc)
        return {}
