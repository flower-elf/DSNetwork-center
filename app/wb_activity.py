# 账号每日活动的结构化历史：一行一次真实尝试，只落运维元数据。
import datetime
import json
import os
import re
import sys
import threading
import time

import wb_host

HISTORY_NAME = "activity_history.jsonl"

TASK_CHECKIN = "checkin"
TASK_DAILY_CHAT = "daily_chat"
TASKS = (TASK_CHECKIN, TASK_DAILY_CHAT)

# 触发来源只用于面板标注；未知值归一成 unknown，标签写错不应丢掉一次真实尝试。
TRIGGERS = ("scheduler", "manual", "account_add", "account_import", "unknown")

RESULTS = ("ok", "failed")
RESULT_ALIASES = {"ok": "ok", "success": "ok", "succeeded": "ok",
                  "failed": "failed", "fail": "failed", "failure": "failed"}

REALMS = ("cn", "intl")

DEFAULT_RANGE = "7d"
RANGES = ("today", "1d", "7d", "30d", "90d", "all")
# today 是本地日历日，单独计算；其余为滚动窗口天数，None 表示不设下界。
RANGE_DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90, "all": None}

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

# 保留策略取 90 天与 5000 行的交集。整理不随每次追加执行：文件超过
# COMPACT_TRIGGER_BYTES 立即压缩（阈值高于上限行数本身的大小，一定缩得回去），
# 或距上次整理超过 COMPACT_INTERVAL_SECONDS（低流量按时间节奏保证上限生效）。
MAX_RECORDS = 5000
MAX_AGE_DAYS = 90
COMPACT_TRIGGER_BYTES = 2000000
COMPACT_INTERVAL_SECONDS = 6 * 3600

# 上游原文的截断长度，历史表一行只保留一句可读的原因。
MAX_MESSAGE = 200

_lock = threading.Lock()
_data_dir_override = None
# 进程内上次整理时刻。重启归零，首条记录会整理一次；失败也推进，避免每条记录重试。
_last_compact = 0.0


def set_data_dir(path):
    global _data_dir_override
    with _lock:
        _data_dir_override = path


def data_dir():
    """历史文件目录：wb_proxy 指定值优先，其次环境变量，最后仓库内默认值。"""
    if _data_dir_override:
        return _data_dir_override
    env = os.environ.get("WB_PROXY_USAGE_DIR")
    return env or os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage")


def history_path():
    return os.path.join(data_dir(), HISTORY_NAME)


def _gateway():
    return wb_host.loaded_gateway("log") or sys.modules.get("wb_proxy")


def _log(message):
    """把写失败反映到面板日志；记录本身不依赖这一步。"""
    try:
        module = _gateway()
        if module is not None:
            module.log("[活动记录] %s" % message, level="WARN")
    except Exception:
        pass


# 上游错误文本是自由文本，401 与 403 的正文里回显授权头是常见现象，写进历史前
# 按已知凭证形态打码。这里只做截断与已知形态替换，不承诺通用清洗。
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(?:access[_-]?token|refresh[_-]?token|api[_-]?key|apikey|"
               r"authorization|cookie|set-cookie|password|secret|token)\b"
               r"\s*[:=]\s*\S+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
)
_REDACTED = "[redacted]"


def safe_message(text):
    line = " ".join(str(text or "").split())
    for pattern in _SECRET_PATTERNS:
        line = pattern.sub(_REDACTED, line)
    return line[:MAX_MESSAGE]


_TRUE = ("1", "true", "yes", "ok", "success")


def _as_bool(value):
    """JSONL 里的 ok 可能是字符串，避免把 "false" 读成成功。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in _TRUE


def project(row):
    """把一行投影到固定字段；文件可能被别的写入方、手工编辑或夹带凭证的旧行改过。

    直接返回解析字典会泄露多余键；这里同时归一字段类型与长度，message 再脱敏一次。
    """
    return {
        "ts": str(row.get("ts") or "")[:40],
        "uid": str(row.get("uid") or "")[:80],
        "nickname": str(row.get("nickname") or "")[:120],
        "realm": row.get("realm") if row.get("realm") in REALMS else "",
        "task": row.get("task") if row.get("task") in TASKS else "",
        "trigger": row.get("trigger") if row.get("trigger") in TRIGGERS else "unknown",
        "ok": _as_bool(row.get("ok")),
        "message": safe_message(row.get("message")),
    }


def _timestamp():
    """本地时间带偏移量：面板按本地时间显示，读取端不必再猜是哪个时区。"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_ts(value):
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return None


def record(uid, nickname, realm, task, trigger, ok, message, usage_dir=None):
    """追加一次尝试，返回写入的行；写不进去时返回 None。

    磁盘满或文件被占用都不能把一次成功的签到变成失败。到点（超阈值或时间节奏）
    顺带整理一次，整理失败时原文件保持不变。
    """
    row = {
        "ts": _timestamp(),
        "uid": str(uid or ""),
        "nickname": str(nickname or ""),
        "realm": realm if realm in REALMS else "",
        "task": task,
        "trigger": trigger if trigger in TRIGGERS else "unknown",
        "ok": bool(ok),
        "message": safe_message(message),
    }
    path = os.path.join(usage_dir, HISTORY_NAME) if usage_dir else history_path()
    try:
        with _lock:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            if _compaction_due(path):
                _compact_locked(path)
    except Exception as exc:
        _log("写入失败: %s" % exc)
        return None
    return row


def record_attempt(account, task, trigger, result):
    """把一次账号尝试的结果记成一行；trigger 为 None 表示不单独记录。嵌套步骤
    由外层统一记录（例如 daily_chat 内部的网页通道），避免历史多出一行。"""
    if trigger is None:
        return None
    result = result if isinstance(result, dict) else {}
    ok = bool(result.get("ok"))
    message = result.get("msg") or result.get("error") or ("成功" if ok else "失败")
    return record(uid=getattr(account, "uid", ""),
                  nickname=getattr(account, "nickname", ""),
                  realm=getattr(account, "realm", ""),
                  task=task, trigger=trigger, ok=ok, message=message)


def _read_rows(path):
    """按写入顺序读出全部记录；坏行跳过，不让一条截断的行毁掉整个文件。"""
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except FileNotFoundError:
        return []
    except OSError as exc:
        _log("读取失败: %s" % exc)
    return rows


def load(usage_dir=None):
    """按写入顺序读出全部原始行，供整理使用；对外返回一律走 query（只有 project
    保证不带多余键）。"""
    path = os.path.join(usage_dir, HISTORY_NAME) if usage_dir else history_path()
    return _read_rows(path)


def local_midnight(now=None):
    """返回本地日历零点的 epoch 秒。零点必须按该时刻自身的本地时间规则推导，
    不能拿此刻生效的 UTC 偏移替换字段；tm_isdst 传 -1 交给平台判夏令时。
    """
    moment = time.localtime(now) if now is not None else time.localtime()
    return time.mktime((moment.tm_year, moment.tm_mon, moment.tm_mday,
                        0, 0, 0, 0, 0, -1))


def range_cutoff(value):
    """返回 range 参数对应的下界 epoch 秒；None 表示不设下界，认不出时抛 ValueError。

    today 是本地日历日（本地零点到现在），1d 保持滚动 24 小时。
    """
    key = str(value or DEFAULT_RANGE).strip().lower()
    if key == "today":
        return local_midnight()
    if key not in RANGE_DAYS:
        raise ValueError("range must be one of: " + ", ".join(RANGES))
    days = RANGE_DAYS[key]
    return None if days is None else time.time() - days * 86400


def query(range_key=None, uid=None, task=None, result=None, limit=None,
          usage_dir=None):
    """读取历史，最新在前；每行先投影到固定字段再筛选，多余键与凭证不会从 API 漏出。

    认不出来的筛选值抛 ValueError（静默忽略会让调用方误以为这几天没失败，实际
    拿到全部结果），由路由转成 400。limit 有默认值与上限。
    """
    key = str(range_key or DEFAULT_RANGE).strip().lower()
    cutoff = range_cutoff(key)
    task = (task or "").strip()
    if task and task not in TASKS:
        raise ValueError("task must be one of: " + ", ".join(TASKS))
    result = (result or "").strip().lower()
    if result and result not in RESULT_ALIASES:
        raise ValueError("result must be one of: " + ", ".join(RESULTS))
    result = RESULT_ALIASES.get(result, "")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(MAX_LIMIT, limit))

    uid = (uid or "").strip()
    matched = []
    # 文件按追加顺序写入，时间也按追加顺序增长，倒序遍历即最新在前。
    for raw in reversed(load(usage_dir=usage_dir)):
        row = project(raw)
        if uid and row["uid"] != uid:
            continue
        if task and row["task"] != task:
            continue
        if result and row["ok"] != (result == "ok"):
            continue
        if cutoff is not None:
            ts = _parse_ts(row["ts"])
            # 有界区间内，时间缺失或读不出来的行不算在区间内，否则坏行能绕过 cutoff。
            # range=all 没有下界，不存在这个问题。
            if ts is None or ts < cutoff:
                continue
        matched.append(row)
    return {"rows": matched[:limit], "total": len(matched), "limit": limit,
            "range": key,
            "filters": {"uid": uid, "task": task, "result": result}}


def compact(usage_dir=None, max_records=None, max_age_days=None):
    """按保留策略整理历史，返回 (保留行数, 丢弃行数)。"""
    path = os.path.join(usage_dir, HISTORY_NAME) if usage_dir else history_path()
    with _lock:
        return _compact_locked(path, max_records=max_records,
                               max_age_days=max_age_days)


def _compaction_due(path):
    if time.time() - _last_compact >= COMPACT_INTERVAL_SECONDS:
        return True
    try:
        return os.path.getsize(path) > COMPACT_TRIGGER_BYTES
    except OSError:
        return False


def _compact_locked(path, max_records=None, max_age_days=None):
    """执行整理；调用方必须已持有 _lock。先写临时文件再 os.replace，任何时刻读到
    的都是完整文件，中途失败也不碰原文件。_last_compact 在入口推进，失败后等到
    下个节奏再试，避免每条记录重试。"""
    global _last_compact
    _last_compact = time.time()
    max_records = MAX_RECORDS if max_records is None else max_records
    max_age_days = MAX_AGE_DAYS if max_age_days is None else max_age_days
    rows = _read_rows(path)
    if not rows:
        return (0, 0)
    cutoff = time.time() - max_age_days * 86400
    keep = []
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        # 时间读不出来的行不按过期丢弃，那是一次无声的数据损失，交给行数上限处理。
        if ts is not None and ts < cutoff:
            continue
        keep.append(row)
    if max_records > 0 and len(keep) > max_records:
        keep = keep[-max_records:]
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for row in keep:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception as exc:
        _log("整理失败: %s" % exc)
        return (len(rows), 0)
    return (len(keep), len(rows) - len(keep))
