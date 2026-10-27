import copy
import fnmatch
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time

import wb_cost

DEFAULT_PANEL_PASSWORD = "admin"
PBKDF2_ROUNDS = 120_000
# 存进 settings.json 的 cost 只接受这个区间：更低的哈希形同虚设，更高的值能把
# 一次面板登录拖成分钟级。
PBKDF2_MIN_ROUNDS = 10_000
PBKDF2_MAX_ROUNDS = 1_000_000
SESSION_TTL = 7 * 24 * 3600
# 价格刷新间隔，单位分钟，与 wb_pricing.DEFAULT_REFRESH_MINUTES 保持一致。
DEFAULT_PRICING_REFRESH_MINUTES = 5.0
# 以小时为单位的兼容键：首次读取时乘以 60 迁移到分钟键，避免 6 小时被当成 6 分钟。
LEGACY_PRICING_REFRESH_HOURS_KEY = "pricing_refresh_hours"
PRICING_REFRESH_MINUTES_KEY = "pricing_refresh_minutes"
# 上限为一个月，即 43200 分钟。
MAX_PRICING_REFRESH_MINUTES = 24 * 30 * 60
# 账号信用余额允许的陈旧时长，单位小时，超过后由后台刷新器更新。派发偏好
# 读取该余额，而余额只由登录与每日活跃任务刷新时，一次派发可能基于数天前的
# 数字。12 小时留有余量：偏好以天计，半天漂移在 7 天窗口内至多影响半天，
# 更宽的 TTL 也避免多花上游计费调用。
DEFAULT_CREDITS_REFRESH_HOURS = 12.0
MAX_CREDITS_REFRESH_HOURS = 24 * 30
CREDITS_REFRESH_HOURS_KEY = "credits_refresh_hours"
# 面板窗口高度上限，单位像素：超过该值的看板分区内部滚动、标题行固定，
# 不把整页撑高。0 表示不限制。默认值略高于日志终端自身的 620px，用户调小
# 之前终端不受影响。
SECTION_MAX_HEIGHT_KEY = "section_max_height"
DEFAULT_SECTION_MAX_HEIGHT = 640
MAX_SECTION_MAX_HEIGHT = 4000
# 模型名可否从去掉后缀的基名继承价格，例如 `deepseek-r1-0528-lkeap` 继承
# `deepseek-r1-0528`。缺失键读作开。
PRICING_VARIANT_INHERIT_KEY = "pricing_variant_inherit"
# OpenRouter 价格估算功能的总开关。缺失键读作开：未设置过的安装行为不变，
# 只有显式 false 才关闭整个功能。
PRICING_ENABLED_KEY = "pricing_enabled"
# 面板账号区是否折叠。缺失键或任何非布尔 true 的值都读作展开：新安装、手改
# 文件或旧客户端留下的值都保持默认视图，只有显式 true 才隐藏账号区。
ACCOUNTS_COLLAPSED_KEY = "accounts_collapsed"
# 面板的按 Key 用量表是否折叠 `(切换前)` 行。与上一个开关相同的归一化：
# 只有显式布尔 true 才隐藏，手改文件或旧客户端不会意外收起该行。
KEY_BEFORE_HIDDEN_KEY = "key_before_hidden"
# 面板顶栏隐藏的页面集合，元素为页面键，例如 "tasks"、"logs"。这里不按页面
# 注册表校验：页面定义在 dashboard 前端，未知键会被面板忽略，因此上游新增或
# 重命名页面无需改动这里。缺失键、手写字符串或其他畸形值都读作“不隐藏任何
# 页面”，这是显示偏好的安全方向。
HIDDEN_PAGES_KEY = "hidden_pages"
# 页面键即 switchMainTab() 使用的全小写标识。
PAGE_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# 无论请求或文件怎么写都不会隐藏的页面：该开关本身就在这里，隐藏后面板无法
# 再进入设置。归一化时直接丢弃，因此存储文件也无法保留它，任何 API 客户端都
# 不会把自己锁在外面。
PAGE_KEEP_VISIBLE = "settings"
# 存储列表的上限。面板每页最多提交一项，这里只限制手改文件写入的数量。
MAX_HIDDEN_PAGES = 32
# 面板账号区是否独占一个主标签页。缺失键或任何非布尔 true 的值都读作关：未
# 设置过的安装保持原有的单个“网关与账号”标签，只有显式 true 才拆成
# 网关 + 账号。与上面几个开关相同的 `is True` 归一化，手改的 "false"、1 或
# 对象都不会意外拆分导航。
ACCOUNTS_SEPARATE_TAB_KEY = "accounts_separate_tab"

# 实例级默认界面语言。面板可按浏览器用 localStorage 覆盖，没有覆盖时使用
# 这里的值。
UI_LANGUAGE_KEY = "ui_language"
UI_LANGUAGE_DEFAULT = "zh"
UI_LANGUAGE_VALUES = ("zh", "zh-Hant", "en")

# 网关是否按间隔自动检查 GitHub 新版本。缺失键读作关，与上面几个开关相反，
# 因为开启后网关会按自己的计划发送请求。
UPDATE_CHECK_ENABLED_KEY = "update_check_enabled"
# 自动检查的间隔（小时）：1～720，默认每天一次。
UPDATE_CHECK_INTERVAL_KEY = "update_check_interval_hours"
UPDATE_CHECK_INTERVAL_DEFAULT = 24
UPDATE_CHECK_INTERVAL_MAX = 720
# 该每日检查的记账数据，只有一个小对象，不做通用更新状态存储，详见
# update_check_state()。
UPDATE_CHECK_STATE_KEY = "update_check"

# 出口级瞬时失败退避，见 issue #271：传输类失败与 502/503/504 不再计入账号级
# 熔断，改记在出口上，退避降到秒级，见 wb_egress。缺失键读作关：它改变的是
# 派发路径上的失败归账与退避尺度，未设置过的安装保持原样，传输错误仍记入
# 账号熔断。
EGRESS_BACKOFF_ENABLED_KEY = "egress_backoff_enabled"

# 日志写入数据库：把运行日志追加写进 usage/logs.sqlite3，重启后仍能回看，方便事后
# 排查。缺失键读作关：关闭时日志只留在内存环形缓冲里，与上游一致；只有显式
# true 才建库并落盘。
LOG_DB_ENABLED_KEY = "log_db_enabled"
# 三条互相独立的保留上限，各自“留空 = 该维度不设限”：
#   * rows  只留最新的 N 条，空表示不限条数
#   * days  只留最近 N 天，空表示不限天数，默认 7 天
#   * bytes 库文件超过 N 字节就从最旧的删起，缩回 N 以内，空表示不限大小
# 天数带 7 天默认值，因为无限增长的日志库才是要防的问题；条数与字节数默认
# 留空，单独看任何一个都可能偏严或偏松，交给用户按需填写。
LOG_DB_MAX_ROWS_KEY = "log_db_max_rows"
LOG_DB_MAX_DAYS_KEY = "log_db_max_days"
LOG_DB_MAX_BYTES_KEY = "log_db_max_bytes"
DEFAULT_LOG_DB_MAX_DAYS = 7
# 上限只用来拦截输入失误：手编 settings.json 写个天文数字不该拖死裁剪循环，
# 也不该让面板显示溢出成科学计数法的值。这些值都远大于任何真实部署的量级。
MAX_LOG_DB_MAX_ROWS = 100_000_000
MAX_LOG_DB_MAX_DAYS = 36500
MAX_LOG_DB_MAX_BYTES = 100 * 1024 ** 3

_lock = threading.RLock()
# 路径到 mtime_ns、文件大小与解析结果的映射：请求路径每次都要读设置，文件
# 未变时直接复制上次解析的结果，省掉打开文件与解析 JSON。
_load_cache = {}


class SettingsError(Exception):
    """accounts/settings.json 读不出来时抛出，消息里带文件路径。

    解析失败绝不能退回空字典：那会让面板密码回到默认的 "admin"，紧随其后的
    save() 会把空字典写回磁盘，整份设置就此消失。
    """


def settings_path(accounts_dir):
    return os.path.join(accounts_dir, "settings.json")


def _digest(password, salt_hex, rounds=PBKDF2_ROUNDS):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), rounds
    ).hex()


# settings.json 的解析结果按 路径、mtime、size 缓存：请求热路径上它被读约
# 10 次，包括 prompt_config 两次、api_keys 两次、limits_data 三次、
# auto_switch_product 与 upstream_config 两次，单次 156~273µs，合计
# 1.6~2.7ms 每请求。所有写入都走 save() 的 os.replace 原子替换，mtime 与
# size 必变，所以面板改动仍然即时生效；save() 里再主动失效一次，连 mtime
# 精度较粗的同尺寸改写也不会读到旧值。调用方拿到的是深拷贝，修改返回值不会
# 弄脏缓存。


def load(accounts_dir):
    """读取持久化的设置，新安装返回空字典。

    文件缺失视为新安装；其他情况一律抛出 SettingsError 并带上文件名，包括
    JSON 无法解析与顶层不是对象。读取与写入持有同一把锁：在 Windows 上，
    读方保持文件打开会让写方的 os.replace() 以 WinError 5 失败。
    """
    path = settings_path(accounts_dir)
    with _lock:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            _load_cache.pop(path, None)
            return {}
        except OSError as exc:
            raise SettingsError("settings file %s cannot be read: %s" % (path, exc))
        cached = _load_cache.get(path)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return copy.deepcopy(cached[2])
        try:
            # utf-8-sig：编辑器写入的 BOM 仍是合法设置。
            with open(path, encoding="utf-8-sig") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            raise SettingsError("settings file %s cannot be read: %s" % (path, exc))
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise SettingsError("settings file %s is not valid JSON: %s" % (path, exc))
        if not isinstance(data, dict):
            raise SettingsError("settings file %s must hold a JSON object, found %s"
                                % (path, type(data).__name__))
        _load_cache[path] = (st.st_mtime_ns, st.st_size, copy.deepcopy(data))
        return data


def save(accounts_dir, data):
    """原子写入，进程崩溃不会留下写了一半的设置文件。

    临时文件名带上 pid 与线程 id，两个写方不会共用同一个临时文件。
    """
    with _lock:
        os.makedirs(accounts_dir, exist_ok=True)
        path = settings_path(accounts_dir)
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            _load_cache.pop(path, None)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path


def _secure_equals(left, right):
    """常数时间比较两个凭据。

    hmac.compare_digest() 遇到含非 ASCII 字符的 str 会抛 TypeError，因此改为
    比较 UTF-8 字节；不是字符串的值永远不匹配。
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _password_rounds(data, path):
    """派生摘要前校验存储的 PBKDF2 成本。

    手改文件能写出的非法值都以文件路径报错，而不是报告密码错误。
    """
    raw = data.get("panel_password_rounds")
    if raw is None:
        return PBKDF2_ROUNDS
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SettingsError("settings file %s: panel_password_rounds must be an "
                            "integer, found %r" % (path, raw))
    if not PBKDF2_MIN_ROUNDS <= raw <= PBKDF2_MAX_ROUNDS:
        raise SettingsError("settings file %s: panel_password_rounds must be between "
                            "%d and %d, found %d"
                            % (path, PBKDF2_MIN_ROUNDS, PBKDF2_MAX_ROUNDS, raw))
    return raw


def deep_merge(base, patch):
    """把 patch 递归合并进 base，未知的同级键保留。

    面板表单只提交自己管理的键。整组替换会静默丢掉手写或后续新增的键，因此
    嵌套对象按键合并。返回新字典，不修改入参。
    """
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out

def panel_password_is_default(accounts_dir):
    data = load(accounts_dir)
    if not data.get("panel_password_hash"):
        return True
    return data.get("panel_password_default") is True


def verify_panel_password(accounts_dir, password):
    """密码能打开网页面板时返回 True。"""
    password = password or ""
    data = load(accounts_dir)
    stored = data.get("panel_password_hash")
    if not stored:
        return password == DEFAULT_PANEL_PASSWORD
    if data.get("panel_password_default") is True:
        return password == DEFAULT_PANEL_PASSWORD
    salt = data.get("panel_password_salt")
    if not salt:
        return False
    rounds = _password_rounds(data, settings_path(accounts_dir))
    try:
        given = _digest(password, salt, rounds)
    except Exception:
        # 盐值被手工改坏时无法派生摘要，只能判定为不匹配。
        return False
    return _secure_equals(given, stored)


def set_panel_password(accounts_dir, password):
    with _lock:
        data = load(accounts_dir)
        if password == DEFAULT_PANEL_PASSWORD:
            data.pop("panel_password_salt", None)
            data.pop("panel_password_rounds", None)
            data["panel_password_hash"] = ""
            data["panel_password_default"] = True
        else:
            salt = secrets.token_hex(16)
            data["panel_password_salt"] = salt
            data["panel_password_rounds"] = PBKDF2_ROUNDS
            data["panel_password_hash"] = _digest(password, salt)
            data["panel_password_default"] = False
        save(accounts_dir, data)


def api_key_override(accounts_dir):
    """返回 key 与是否由面板管理。"""
    data = load(accounts_dir)
    if not data.get("api_key_set"):
        return None, False
    return str(data.get("api_key") or ""), True


def set_api_key(accounts_dir, key):
    with _lock:
        data = load(accounts_dir)
        data["api_key"] = key or ""
        data["api_key_set"] = True
        save(accounts_dir, data)


def ensure_launcher_key(accounts_dir):
    """返回持久化的局域网 Key，首次使用时生成。

    局域网模式绝不能自带众所周知默认值：网关消耗的是账号自己的上游额度，
    同一网络中的任何人都能把它耗光。返回 key 与是否新建，调用方据此告知
    用户本次运行是否生成了新凭据。
    """
    with _lock:
        data = load(accounts_dir)
        existing = str(data.get("launcher_key") or "").strip()
        if existing:
            return existing, False
        key = "wb-" + secrets.token_urlsafe(24)
        data["launcher_key"] = key
        save(accounts_dir, data)
        return key, True


# --------------------------------------------------------------- API keys
# 每个 Key 可绑定一个上游出口域，多个客户端能同时走不同出口，不必共用全局
# 切换。

REALMS = ("", "intl", "cn")


def clean_model_patterns(value):
    """把一份模型白名单归一化为小写模式列表。

    接受提交的列表或逗号分隔字符串，匹配使用 fnmatch，精确名与通配符行为
    一致。结果为空表示“不限制”，没有该字段的历史条目也读回这种行为。
    """
    if isinstance(value, str):
        raw = [part for part in re.split(r"[,;\n]", value)]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        return []
    out = []
    for item in raw:
        pattern = str(item or "").strip().lower()
        if pattern and pattern not in out:
            out.append(pattern)
    return out


def patterns_allow_model(patterns, model):
    """patterns 不构成限制，或 model 命中其中一条时返回 True。

    空列表的 Key 保持不限制，从未使用该字段的安装行为不变。受限 Key 未带
    模型名时直接拒绝而非放行：请求没有说明它要用的模型在 Key 允许范围内，
    转发只会带着空模型名到达上游，白白消耗一次必然失败的尝试。
    """
    cleaned = clean_model_patterns(patterns)
    if not cleaned:
        return True
    name = str(model or "").strip().lower()
    if not name:
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in cleaned)


def key_allows_model(entry, model):
    """entry 不构成模型限制，或 model 命中时返回 True。"""
    return patterns_allow_model((entry or {}).get("models"), model)


# --------------------------------------------------- API key limits
# Key 可带过期时间、token 或积分配额以及 IP 白名单；未设置的项读回
# 0 或空列表，表示不限制。

def _ip_entry_text(value):
    """一条存储条目的规范文本，不是 IP 时返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None
    # 裸地址保持裸地址：回写 "1.2.3.4/32" 会让面板看起来改动了操作者没改的东西。
    if net.num_addresses == 1:
        return str(net.network_address)
    return str(net)


def _ip_entry_source(value):
    """白名单的原始条目：列表或分隔符拼接的字符串，与 clean_model_patterns
    接受的形状相同。
    """
    if isinstance(value, str):
        return [part for part in re.split(r"[,;\n]", value)]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return []


def clean_ip_allowlist(value):
    """校验面板提交的白名单，返回条目与问题描述。

    每个条目必须是地址或 CIDR 块，否则一个笔误会在文件里看起来像规则，
    实际匹配不到任何地址。空条目丢弃；结果为空表示允许所有地址。
    """
    raw = _ip_entry_source(value)
    if value not in (None, "", []) and not isinstance(value, (str, list, tuple, set)):
        return [], "ip_allowlist must be a list of IPs or CIDR blocks"
    entries = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        canonical = _ip_entry_text(text)
        if canonical is None:
            return [], "not a valid IP or CIDR block: %s" % text
        if canonical not in entries:
            entries.append(canonical)
    return entries, ""


def stored_ip_allowlist(value):
    """从磁盘读取白名单，只保留能解析的条目。

    读取不得抛出异常：手改文件里有一行坏值，其余能解析的行仍应继续限制
    流量。完全解析不出条目的列表由 ip_allowed 视为不限制。
    """
    entries = []
    for item in _ip_entry_source(value):
        canonical = _ip_entry_text(item)
        if canonical and canonical not in entries:
            entries.append(canonical)
    return entries


def ip_allowed(allowlist, client_ip):
    """client_ip 在白名单内时返回 True；空列表允许所有地址。

    配置了列表后，无法解析的地址一律拒绝。
    """
    entries = stored_ip_allowlist(allowlist)
    if not entries:
        return True
    try:
        addr = ipaddress.ip_address(str(client_ip or "").strip())
    except ValueError:
        return False
    for entry in entries:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def clean_epoch_field(value, label):
    """校验面板提交的 epoch 秒，返回秒数与问题描述。

    空值或 0 表示不限制或永不过期，没有该字段的历史条目也读回 0。
    """
    if value in (None, "", False):
        return 0, ""
    if isinstance(value, bool):
        return 0, "%s must be a timestamp in seconds" % label
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return 0, "%s must be a timestamp in seconds" % label
    if seconds < 0:
        return 0, "%s cannot be negative" % label
    return seconds, ""


def clean_quota_tokens(value):
    """校验 token 配额，返回配额与问题描述。0 表示不限。"""
    if value in (None, "", False):
        return 0, ""
    if isinstance(value, bool):
        return 0, "quota_tokens must be a whole number"
    try:
        tokens = int(float(value))
    except (TypeError, ValueError):
        return 0, "quota_tokens must be a whole number"
    if tokens < 0:
        return 0, "quota_tokens cannot be negative"
    return tokens, ""


def clean_quota_credit(value):
    """校验积分配额，返回配额与问题描述。0 表示不限。"""
    if value in (None, "", False):
        return 0.0, ""
    if isinstance(value, bool):
        return 0.0, "quota_credit must be a number"
    try:
        credit = float(value)
    except (TypeError, ValueError):
        return 0.0, "quota_credit must be a number"
    if credit < 0:
        return 0.0, "quota_credit cannot be negative"
    return credit, ""


def key_expired(entry, now=None):
    """Key 带有已过去的过期时间时返回 True。"""
    expires_at = (entry or {}).get("expires_at") or 0
    if not expires_at:
        return False
    return (now if now is not None else time.time()) >= expires_at


def key_quota_reason(entry, usage):
    """Key 配额耗尽的原因文本，仍有余额时返回空串。

    达到配额即视为耗尽：拒绝的是那条会越过配额的请求。
    """
    usage = usage or {}
    limit_tokens = (entry or {}).get("quota_tokens") or 0
    used_tokens = usage.get("total_tokens") or 0
    if limit_tokens and used_tokens >= limit_tokens:
        return ("该 Key 的 Token 配额已用尽（配额 %d，已用 %d）。"
                "请在面板「设置」页调高配额，或执行「重置用量」重新累计。"
                % (limit_tokens, used_tokens))
    limit_credit = (entry or {}).get("quota_credit") or 0
    used_credit = usage.get("credit") or 0
    if limit_credit and used_credit >= limit_credit:
        return ("该 Key 的积分配额已用尽（配额 %g，已用 %g）。"
                "请在面板「设置」页调高配额，或执行「重置用量」重新累计。"
                % (limit_credit, used_credit))
    return ""


def key_status(entry, usage, now=None):
    """设置页徽标状态：ok / disabled / expired / quota_exceeded。"""
    entry = entry or {}
    if entry.get("enabled", True) is False:
        return "disabled"
    if key_expired(entry, now):
        return "expired"
    if key_quota_reason(entry, usage):
        return "quota_exceeded"
    return "ok"


def _clean_key_entry(entry):
    """归一化一条存储的 Key，无法使用时返回 None。

    已删除的条目保留行，但密钥已清空：用量日志按 id 记录，丢掉该行会让这个
    Key 过去的每条请求都无法归属。删除是单向的，密钥不会再存回，条目也只
    可读，无法重新启用。
    """
    if not isinstance(entry, dict):
        return None
    key = str(entry.get("key") or "").strip()
    deleted_at = str(entry.get("deleted_at") or "").strip()
    if not key and not deleted_at:
        return None
    realm = str(entry.get("realm") or "").strip().lower()
    if realm not in REALMS:
        realm = ""
    try:
        concurrency = int(entry.get("concurrency_limit") or entry.get("max_concurrency") or 0)
    except (TypeError, ValueError):
        concurrency = 0
    concurrency = max(0, concurrency)
    return {
        "id": str(entry.get("id") or secrets.token_hex(6)),
        "name": str(entry.get("name") or "").strip() or "未命名",
        "key": key,
        "realm": realm,
        "models": clean_model_patterns(entry.get("models")),
        "concurrency_limit": concurrency,
        "enabled": False if deleted_at else entry.get("enabled", True) is not False,
        "created_at": entry.get("created_at") or time.strftime("%Y/%m/%d %H:%M"),
        # 下面五项缺失时读回「无限制」，与旧文件的行为一致。
        "expires_at": clean_epoch_field(entry.get("expires_at"), "expires_at")[0],
        "quota_tokens": clean_quota_tokens(entry.get("quota_tokens"))[0],
        "quota_credit": clean_quota_credit(entry.get("quota_credit"))[0],
        "ip_allowlist": stored_ip_allowlist(entry.get("ip_allowlist")),
        "usage_reset_at": clean_epoch_field(entry.get("usage_reset_at"),
                                            "usage_reset_at")[0],
        "deleted_at": deleted_at,
    }


def _unique_key_id(candidate, used):
    """返回 candidate，或一个不在 used 中的变体。

    设置文件可能有两行同 id；后出现的重复项加数字后缀，后缀是确定的，因此
    /settings 读到的 id 在随后的 reveal 中仍能解析。
    """
    candidate = str(candidate or "").strip()
    if candidate and candidate not in used:
        return candidate
    if candidate:
        suffix = 2
        while True:
            alt = "%s-%d" % (candidate, suffix)
            if alt not in used:
                return alt
            suffix += 1
    while True:
        alt = secrets.token_hex(6)
        if alt not in used:
            return alt


def api_keys(accounts_dir, include_deleted=False):
    """所有已配置的 Key，按当前条目形状返回。

    只带 api_key 与 api_key_set 的设置文件会呈现为一个未绑定出口的条目，
    升级无需迁移步骤。id 在读与写时都会去重，文件里已有重复 id 时无需先保存
    才能得到可区分的行。

    已删除的条目默认隐藏：它们不再是凭据，match_api_key 绝不能看到。行仍留
    在磁盘上，include_deleted 时返回，因为用量日志按 id 指代 Key，没有名字的
    id 无法阅读。
    """
    data = load(accounts_dir)
    stored = data.get("api_keys")
    if isinstance(stored, list):
        out = []
        seen = set()
        seen_ids = set()
        for raw in stored:
            entry = _clean_key_entry(raw)
            if entry is None:
                continue
            # 按密钥去重，且只在有密钥时进行：已删除条目的密钥为空，它们是
            # 各自独立的历史行。
            if entry["key"]:
                if entry["key"] in seen:
                    continue
                seen.add(entry["key"])
            entry["id"] = _unique_key_id(entry["id"], seen_ids)
            seen_ids.add(entry["id"])
            out.append(entry)
        if not include_deleted:
            out = [e for e in out if not e.get("deleted_at")]
        return out

    if data.get("api_key_set"):
        legacy = str(data.get("api_key") or "").strip()
        if legacy:
            return [{
                "id": "legacy",
                "name": "默认（跟随面板切换）",
                "key": legacy,
                "realm": "",
                "models": [],
                "enabled": True,
                "created_at": "",
                "expires_at": 0,
                "quota_tokens": 0,
                "quota_credit": 0.0,
                "ip_allowlist": [],
                "usage_reset_at": 0,
                "deleted_at": "",
            }]
    return []


def _retired_key_entry(old):
    """存储 Key 的软删除形态：保留名称与日期，清空密钥。"""
    return {
        "id": old["id"],
        "name": old["name"],
        "key": "",
        "realm": old.get("realm") or "",
        "models": old.get("models") or [],
        "enabled": False,
        "created_at": old.get("created_at") or "",
        "deleted_at": old.get("deleted_at") or time.strftime("%Y/%m/%d %H:%M"),
    }


def set_api_keys(accounts_dir, keys, delete_ids=None):
    """保存 Key 列表，返回保存后的存活列表。

    删除采用软删除：用量日志按 id 归属花费，丢掉 id 会把该 Key 的整段历史
    倒进“(未知 key)”，因此删除时同步清空密钥，已删除的 Key 永远无法再通过
    认证。

    两种模式决定哪些已存 Key 被软删除：

    - delete_ids 为 None：替换语义。keys 中缺席的已存 Key 全部退役，省略某
      行即删除是该模式的既有用法，所以保持为默认。
    - delete_ids 为列表：upsert 语义。只有列表中的 id 被退役，提交中未提及
      的已存 Key 保持不变。面板使用该模式，因为它的提交内容就是内存中的行，
      列表可能过期或不完整，例如第二个浏览器标签、保存与保存后重载竞争、
      重载失败，这些情况不能退役用户从未删除的 Key。
    """
    with _lock:
        previous = api_keys(accounts_dir, include_deleted=True)
        upsert = delete_ids is not None
        drop = {str(entry_id) for entry_id in (delete_ids or [])}
        # upsert 模式：记录已存存活密钥，提交行丢失 id 时，例如过期面板重发
        # 已保存的 Key，可沿用已存 id 原地更新，不必新建一条把用量历史拆成
        # 两段。
        live_secret_id = {}
        if upsert:
            for old in previous:
                if old["key"] and not old.get("deleted_at"):
                    live_secret_id.setdefault(old["key"], old["id"])
        cleaned = []
        seen = set()
        seen_ids = set()
        for raw in keys or []:
            raw_id = str((raw or {}).get("id") or "").strip() if isinstance(raw, dict) else ""
            entry = _clean_key_entry(raw)
            if entry is None:
                continue
            if entry["key"]:
                if entry["key"] in seen:
                    continue
                seen.add(entry["key"])
            if upsert and entry["key"] and raw_id not in live_secret_id.values():
                entry["id"] = live_secret_id.get(entry["key"], entry["id"])
            entry["id"] = _unique_key_id(entry["id"], seen_ids)
            seen_ids.add(entry["id"])
            cleaned.append(entry)
        for old in previous:
            if old["id"] in seen_ids:
                continue
            seen_ids.add(old["id"])
            # upsert 模式保留提交中未提及的已存 Key。已删除的行也保留，它是
            # 只读历史。
            if upsert and old["id"] not in drop:
                # 提交行可能已经带着同一个密钥，过期提交用新 id 重发了它。
                # 两行同密钥正是该模式要避免的重复，因此退役已存副本。
                if old["key"] and old["key"] in seen:
                    cleaned.append(_retired_key_entry(old))
                    continue
                if old["key"]:
                    seen.add(old["key"])
                cleaned.append(old)
                continue
            cleaned.append(_retired_key_entry(old))
        data = load(accounts_dir)
        data["api_keys"] = cleaned
        # 单 Key 字段现在是派生数据，移除它们以保证只有一个数据源，列表也
        # 能在重启后保留。
        data.pop("api_key", None)
        data.pop("api_key_set", None)
        save(accounts_dir, data)
        return [e for e in cleaned if not e.get("deleted_at")]


def match_api_key(accounts_dir, supplied, extra_keys=()):
    """找出请求出示的是哪个已配置 Key，没有匹配时返回 None。

    返回值是条目副本，并带上 source 字段。
    """
    supplied = (supplied or "").strip()
    if not supplied:
        return None
    for entry in api_keys(accounts_dir):
        # 已删除条目存储时 enabled 就是 False，显式检查可防止手改的
        # settings.json 把它重新启用。
        if entry["enabled"] and not entry.get("deleted_at") \
                and _secure_equals(supplied, entry["key"]):
            out = dict(entry)
            out["source"] = "panel"
            return out
    for candidate in extra_keys:
        candidate = (candidate or "").strip()
        if candidate and _secure_equals(supplied, candidate):
            return {
                "id": "launcher",
                "name": "启动参数",
                "key": candidate,
                "realm": "",
                "models": [],
                "enabled": True,
                "source": "launcher",
                "expires_at": 0,
                "quota_tokens": 0,
                "quota_credit": 0.0,
                "ip_allowlist": [],
                "usage_reset_at": 0,
            }
    return None


def auth_disabled(accounts_dir):
    """操作者完全关闭 API Key 校验时返回 True。"""
    return load(accounts_dir).get("auth_disabled") is True


def set_auth_disabled(accounts_dir, disabled):
    with _lock:
        data = load(accounts_dir)
        data["auth_disabled"] = bool(disabled)
        save(accounts_dir, data)


# ------------------------------------------------------------------ limits
# 几个守卫形状相同：一个覆盖两个出口域的全局默认值，加一个可选的按域覆盖。
# 覆盖留空即继承全局值，从未设置过的安装行为不变；设置过的安装每个守卫只需
# 关心一个数字。
LIMIT_KEYS = ("reserve_credits", "daily_token_limit",
              "daily_credit_limit", "model_daily_token_limit",
              "expiring_window_days")
LIMIT_REALMS = ("intl", "cn")
LIMIT_SCOPES = ("global",) + LIMIT_REALMS
LIMITS_KEY = "limits"

# 全局默认值不是关闭的守卫。每个键仍把 0 读作关闭；只有临期额度窗口默认
# 开启，因为 0 会让该偏好静默失效，直到有人手工打开。
LIMIT_DEFAULTS = {"expiring_window_days": 7}


def _default_global(key):
    """安装尚未保存过任何值时读取的全局默认值。"""
    try:
        return max(0, int(LIMIT_DEFAULTS.get(key, 0)))
    except (TypeError, ValueError):
        return 0


def _empty_limit_entry(key=None):
    """一个守卫：全局默认值加每个出口域的槽位，None 表示继承。"""
    return {"global": _default_global(key), "intl": None, "cn": None}


def _coerce_global(value):
    """全局阈值。垃圾值与负数折叠为 0 即关闭，尚未设置该值的安装也读回
    0。"""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    return max(0, number)


def _coerce_override(value):
    """按出口域的覆盖值。None 或空表单字段表示继承全局默认值，与显式 0
    即该域关闭保持区别。"""
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)


def _fold_legacy_limits(data):
    """把四个平铺的顶层守卫移进 limits。

    返回是否改动与分组后的 map。折叠时删除原键，回滚后也不会从分组副本旁
    复活过期阈值。
    """
    limits = {}
    changed = False
    for key in LIMIT_KEYS:
        entry = _empty_limit_entry(key)
        if key in data:
            entry["global"] = _coerce_global(data.pop(key))
            changed = True
        limits[key] = entry
    return changed, limits


def _normalize_limits(raw):
    """存储 map 的副本，补齐所有键与作用域，残缺或手改过的 settings.json
    仍读回完整形状。"""
    limits = {}
    for key in LIMIT_KEYS:
        entry = raw.get(key)
        if not isinstance(entry, dict):
            entry = {}
        global_raw = entry.get("global")
        # 缺失的全局值回落到该守卫自己的默认值，除临期窗口外都是 0；显式 0
        # 是真正的关闭，按关闭保留，保存过的关闭不会静默变回开启。
        global_value = (_default_global(key) if global_raw is None
                        else _coerce_global(global_raw))
        limits[key] = {
            "global": global_value,
            "intl": _coerce_override(entry.get("intl")),
            "cn": _coerce_override(entry.get("cn")),
        }
    return limits


def limits_data(accounts_dir):
    """分组后的守卫 map，首次读取时迁移平铺格式的文件。

    读取即升级：第一次看到平铺的 settings.json 时把四个键折叠进 limits 并
    改写文件，网关其余部分只会看到一种形状。
    """
    with _lock:
        data = load(accounts_dir)
        raw = data.get(LIMITS_KEY)
        if isinstance(raw, dict):
            return _normalize_limits(raw)
        changed, limits = _fold_legacy_limits(data)
        if changed:
            data[LIMITS_KEY] = limits
            try:
                save(accounts_dir, data)
            except Exception:
                # 只读的 accounts 目录不应让面板崩溃；折叠后的值仍会返回，
                # 下次读取再重试保存。
                pass
        return limits


def limits_snapshot(accounts_dir):
    """面板可见的每个守卫取值，先按守卫再按作用域索引。"""
    return limits_data(accounts_dir)


def limit_value(accounts_dir, key, realm=None):
    """某个守卫在一个出口域的生效值。

    realm 为 None 或空串时返回全局默认值；intl/cn 覆盖存在时优先。未知键
    读作 0 即关闭，笔误不会卡住请求路径。
    """
    entry = limits_data(accounts_dir).get(key) or _empty_limit_entry()
    if realm in LIMIT_REALMS:
        override = entry.get(realm)
        if override is not None:
            return override
    return entry.get("global") or 0


def limit_values_many(accounts_dir, keys):
    """一次设置读取解析多个守卫在各出口域的取值。

    逐个调用 limit_values 会重复读文件并重新归一化整份分组 map；请求路径上
    的每日守卫需要同一份 map，因此在这里共享一次读取。实现只有一份，
    limit_values 就是单键版本。
    """
    grouped = limits_data(accounts_dir)
    out = {}
    for key in keys:
        entry = grouped.get(key) or _empty_limit_entry()
        global_value = entry.get("global") or 0
        values = {"global": global_value}
        for realm in LIMIT_REALMS:
            override = entry.get(realm)
            values[realm] = global_value if override is None else override
        out[key] = values
    return out


def limit_values(accounts_dir, key):
    """某个守卫在每个出口域的取值，含继承的全局值。

    返回 global、intl、cn 三个键。未设置覆盖时 intl 与 cn 已带全局值，
    账号池可直接按账号所属出口取值，无需第二次读取设置。
    """
    return limit_values_many(accounts_dir, (key,))[key]


def set_limit(accounts_dir, key, scope, value):
    """保存某个守卫在某个作用域的取值，返回存储的条目。

    scope 为 global、intl 或 cn；None 或空值把 intl/cn 覆盖清回继承，
    global 槽位始终存数字。
    """
    if key not in LIMIT_KEYS:
        raise ValueError("unknown limit: %s" % key)
    if scope not in LIMIT_SCOPES:
        raise ValueError("unknown scope: %s" % scope)
    with _lock:
        data = load(accounts_dir)
        raw = data.get(LIMITS_KEY)
        if isinstance(raw, dict):
            limits = _normalize_limits(raw)
        else:
            _changed, limits = _fold_legacy_limits(data)
        entry = limits[key]
        if scope == "global":
            entry["global"] = _coerce_global(value)
        else:
            entry[scope] = _coerce_override(value)
        data[LIMITS_KEY] = limits
        save(accounts_dir, data)
    return entry


def reserve_credits(accounts_dir, realm=None):
    """全局低额度守卫：余额等于或低于该值的账号保持空闲。

    0 关闭守卫，尚未设置该值的安装行为不变。
    """
    return limit_value(accounts_dir, "reserve_credits", realm)


def set_reserve_credits(accounts_dir, value):
    """保存守卫阈值，返回存储的值。"""
    return set_limit(accounts_dir, "reserve_credits", "global", value)["global"]


def daily_token_limit(accounts_dir, realm=None):
    """全局每日守卫：今天已消耗该数量 token 的账号保持空闲，直到本地零点。

    0 关闭守卫。
    """
    return limit_value(accounts_dir, "daily_token_limit", realm)


def set_daily_token_limit(accounts_dir, value):
    """保存每日 token 阈值，返回存储的值。"""
    return set_limit(accounts_dir, "daily_token_limit", "global", value)["global"]


def daily_credit_limit(accounts_dir, realm=None):
    """每日积分守卫：今天已花掉该数量积分的账号在本地零点前只服务免费
    模型，因此会继续使用付费模型的客户端会轮换到其他账号，而不是耗尽整份
    余额。

    0 关闭守卫，尚未设置该值的安装行为不变。
    """
    return limit_value(accounts_dir, "daily_credit_limit", realm)


def set_daily_credit_limit(accounts_dir, value):
    """保存每日积分阈值，返回存储的值。"""
    return set_limit(accounts_dir, "daily_credit_limit", "global", value)["global"]


def model_daily_token_limit(accounts_dir, realm=None):
    """按模型每日守卫：今天在单个模型上已消耗该数量 token 的账号，在本地
    零点前不再被派发该模型，其他模型不受影响。

    0 关闭守卫，尚未设置该值的安装行为不变。
    """
    return limit_value(accounts_dir, "model_daily_token_limit", realm)


def set_model_daily_token_limit(accounts_dir, value):
    """保存按模型每日 token 阈值，返回存储的值。"""
    return set_limit(accounts_dir, "model_daily_token_limit", "global", value)["global"]


def expiring_window_days(accounts_dir, realm=None):
    """临期额度窗口，单位天：账号最快到期的额度包落在此窗口内时，账号池
    优先派发该账号，让即将失效的额度先用掉。

    0 关闭该偏好，派发退化为纯轮询；默认值为 7 天。
    """
    return limit_value(accounts_dir, "expiring_window_days", realm)


def set_expiring_window_days(accounts_dir, value):
    """保存窗口天数，返回存储的值。"""
    return set_limit(accounts_dir, "expiring_window_days", "global", value)["global"]


def credits_refresh_hours(accounts_dir):
    """信用余额允许的陈旧时长，单位小时，超过后由后台刷新器更新。

    派发偏好读取余额，而余额只由登录与每日活跃任务刷新时，账号可能基于数天
    前的余额或进程启动时磁盘上的值被判定。0 关闭刷新器。非数字值回落到默认
    值，手改 settings.json 不会卡住循环。
    """
    with _lock:
        data = load(accounts_dir)
        raw = data.get(CREDITS_REFRESH_HOURS_KEY)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CREDITS_REFRESH_HOURS
    if value < 0:
        return DEFAULT_CREDITS_REFRESH_HOURS
    return min(value, MAX_CREDITS_REFRESH_HOURS)


def set_credits_refresh_hours(accounts_dir, value):
    """保存刷新 TTL，返回存储的值。"""
    try:
        hours = float(value)
    except (TypeError, ValueError):
        hours = 0.0
    hours = max(0.0, min(MAX_CREDITS_REFRESH_HOURS, hours))
    with _lock:
        data = load(accounts_dir)
        data[CREDITS_REFRESH_HOURS_KEY] = hours
        save(accounts_dir, data)
    return hours


def section_max_height(accounts_dir):
    """面板窗口高度上限，单位像素；0 表示不限制。

    任何非整数都回落到默认值，手改 settings.json 不会把看板窗口压到一像素
    高；超过 MAX_SECTION_MAX_HEIGHT 的值会被夹紧而非拒绝。bool 特意先检查：
    它是 int 的子类，否则 True 会读成 1px 上限。
    """
    with _lock:
        raw = load(accounts_dir).get(SECTION_MAX_HEIGHT_KEY)
    if isinstance(raw, bool):
        return DEFAULT_SECTION_MAX_HEIGHT
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError 单独处理：json.loads 会把 1e400 变成 inf，int(inf)
        # 会抛它，一行坏数据不该让设置页返回 500。
        return DEFAULT_SECTION_MAX_HEIGHT
    if value < 0:
        return DEFAULT_SECTION_MAX_HEIGHT
    return min(value, MAX_SECTION_MAX_HEIGHT)


def set_section_max_height(accounts_dir, value):
    """保存高度上限，返回存储的值。"""
    if isinstance(value, bool):
        number = DEFAULT_SECTION_MAX_HEIGHT
    else:
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            number = DEFAULT_SECTION_MAX_HEIGHT
    number = max(0, min(MAX_SECTION_MAX_HEIGHT, number))
    with _lock:
        data = load(accounts_dir)
        data[SECTION_MAX_HEIGHT_KEY] = number
        save(accounts_dir, data)
    return number


def _clamp_refresh_minutes(value):
    """一个月已远超“足够频繁”；上限只防止笔误把下次刷新推到面板无法展示
    的范围。"""
    return max(0.0, min(MAX_PRICING_REFRESH_MINUTES, value))


def _migrate_pricing_refresh(data, accounts_dir):
    """返回是否找到与换算后的分钟数，原地迁移以小时为单位的设置文件。

    pricing_refresh_hours 以小时存储，直接按分钟读取会把 6 小时当成 6 分钟，
    因此这里乘以 60，写入分钟键并删除原键，只在首次读取时执行一次。非数字值
    只删除该键并回落到默认值；0 仍表示关闭。
    """
    legacy = data.pop(LEGACY_PRICING_REFRESH_HOURS_KEY, None)
    if legacy is None:
        return False, None
    try:
        minutes = _clamp_refresh_minutes(float(legacy) * 60.0)
    except (TypeError, ValueError):
        minutes = None
    else:
        data[PRICING_REFRESH_MINUTES_KEY] = minutes
    try:
        save(accounts_dir, data)
    except Exception:
        # 只读的 accounts 目录不应让面板崩溃；换算后的值仍会返回，下次读取
        # 再迁移一次。
        pass
    return True, minutes


def pricing_refresh_minutes(accounts_dir):
    """网关刷新 OpenRouter 价格历史的间隔，单位分钟。

    0 关闭刷新，未设置该值的安装保持使用内置快照。以小时为单位写入的
    settings.json 带 pricing_refresh_hours，首次读取时在此迁移，见
    _migrate_pricing_refresh。非数字值回落到默认值，手改 settings.json 不会
    卡住刷新循环。
    """
    with _lock:
        data = load(accounts_dir)
        raw = data.get(PRICING_REFRESH_MINUTES_KEY)
        if raw is None:
            found, minutes = _migrate_pricing_refresh(data, accounts_dir)
            if not found:
                return DEFAULT_PRICING_REFRESH_MINUTES
            if minutes is None:
                return DEFAULT_PRICING_REFRESH_MINUTES
            return minutes
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PRICING_REFRESH_MINUTES
    return value if value > 0 else 0.0


def set_pricing_refresh_minutes(accounts_dir, value):
    """保存刷新间隔，单位分钟，返回存储的值。"""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return pricing_refresh_minutes(accounts_dir)
    value = _clamp_refresh_minutes(value)
    with _lock:
        data = load(accounts_dir)
        data[PRICING_REFRESH_MINUTES_KEY] = value
        # 分钟键才是生效值；残留的兼容键只会让回滚读到过期间隔。
        data.pop(LEGACY_PRICING_REFRESH_HOURS_KEY, None)
        save(accounts_dir, data)
    return value


def pricing_variant_inherit(accounts_dir):
    """模型名可否从去掉后缀的基名继承价格。

    默认开启，除非操作者显式关闭：带渠道后缀的模型如
    `deepseek-r1-0528-lkeap` 与上游基名是同一实体，关闭后网关会对本可精确
    定价的模型显示“未定价”。关闭后只有覆盖表与精确名称匹配可以定价，需要
    最严格规则的安装可保留该行为。没有该键的 settings.json 读作开启，这也是
    默认值。
    """
    value = load(accounts_dir).get(PRICING_VARIANT_INHERIT_KEY)
    return True if value is None else value is True


def set_pricing_variant_inherit(accounts_dir, enabled):
    """保存变体继承开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data[PRICING_VARIANT_INHERIT_KEY] = enabled
        save(accounts_dir, data)
    return enabled


def pricing_enabled(accounts_dir):
    """OpenRouter 价格估算总开关，默认开启，除非被关闭。

    关闭后功能端到端停用：不拉取价格、不生成策略表、不计算每行成本，也不
    显示成本列。没有该键的 settings.json 读作开启，这是所有安装的默认行为，
    开关只用于让操作者整体关闭该功能。
    """
    value = load(accounts_dir).get(PRICING_ENABLED_KEY)
    return True if value is None else value is True


def set_pricing_enabled(accounts_dir, enabled):
    """保存总开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data[PRICING_ENABLED_KEY] = enabled
        save(accounts_dir, data)
    return enabled


COST_ESTIMATE_KEY = "cost_estimate"


def cost_estimate(accounts_dir):
    """费用估算使用的官方价格表与积分折算单价。

    没有这个键的安装使用内置价格表，默认只有 deepseek-v4.1-flash 一项。
    设置文件损坏时向上抛出 SettingsError，由调用方显示读取失败，避免用内置
    价格掩盖损坏的配置。
    """
    stored = load(accounts_dir).get(COST_ESTIMATE_KEY)
    return wb_cost.normalise(stored)


def set_cost_estimate(accounts_dir, raw):
    """保存校验过的费用估算设置，返回存储的形状。"""
    value = wb_cost.clean(raw)
    with _lock:
        data = load(accounts_dir)
        data[COST_ESTIMATE_KEY] = value
        save(accounts_dir, data)
    return value


def ui_language(accounts_dir):
    """实例级默认界面语言，取值为 zh、zh-Hant 或 en。"""
    value = load(accounts_dir).get(UI_LANGUAGE_KEY)
    if isinstance(value, str) and value in UI_LANGUAGE_VALUES:
        return value
    return UI_LANGUAGE_DEFAULT


def set_ui_language(accounts_dir, value):
    """保存实例级默认界面语言。"""
    if not isinstance(value, str) or value not in UI_LANGUAGE_VALUES:
        raise ValueError("ui_language must be zh, zh-Hant or en")
    with _lock:
        data = load(accounts_dir)
        data[UI_LANGUAGE_KEY] = value
        save(accounts_dir, data)
    return value


def smart_routing(accounts_dir):
    """没有绑定的对话是否由评分选择放置账号。

    默认开启，除非操作者关闭：评分选择把新对话交给额度即将到期且当前最空闲
    的账号。只影响对话的第一次选择，已绑定的对话保持原账号，前缀缓存不受
    影响。该开关用于恢复纯轮询。
    """
    value = load(accounts_dir).get("smart_routing")
    return True if value is None else value is True


def set_smart_routing(accounts_dir, enabled):
    """保存放置开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["smart_routing"] = enabled
        save(accounts_dir, data)
    return enabled

def session_affinity(accounts_dir):
    """同一段对话是否固定使用同一个账号，默认打开。

    上游的前缀缓存按账号保存，对话换了账号就要重新计算整段前缀。
    """
    value = load(accounts_dir).get("session_affinity")
    return True if value is None else value is True


def set_session_affinity(accounts_dir, enabled):
    """保存对话亲和开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["session_affinity"] = enabled
        save(accounts_dir, data)
    return enabled



UPSTREAM_DEFAULTS = {
    "header_timeout_seconds": 120,
    "idle_timeout_seconds": 300,
    "device_token": "",
    "device_token_file": "",
}


def validate_upstream_patch(raw):
    """严格校验面板保存的上游配置补丁。"""
    out = {}
    for key, value in (raw or {}).items():
        if key in ("header_timeout_seconds", "idle_timeout_seconds"):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("%s must be a whole number of seconds" % key)
            if value < 1:
                raise ValueError("%s cannot be less than 1" % key)
            out[key] = value
        elif key in ("device_token", "device_token_file"):
            if not isinstance(value, str):
                raise ValueError("%s must be a string" % key)
            out[key] = value.strip()
        else:
            raise ValueError("unknown upstream setting %r" % key)
    return out


def upstream_config(accounts_dir):
    """聊天 socket 超时与可选的 X-Device-Token 来源。"""
    try:
        stored = load(accounts_dir).get("upstream")
    except SettingsError:
        # 这份配置每一项都有默认值，坏文件不该让每次请求都 500。
        stored = None
    stored = stored if isinstance(stored, dict) else {}
    out = {}
    for key, default in UPSTREAM_DEFAULTS.items():
        value = stored.get(key, default)
        if key in ("header_timeout_seconds", "idle_timeout_seconds"):
            out[key] = (value if isinstance(value, int) and not isinstance(value, bool)
                        and value >= 1 else default)
        else:
            out[key] = value.strip() if isinstance(value, str) else default
    return out


def set_upstream_config(accounts_dir, cfg):
    """保存上游配置，返回存储的配置。"""
    current = upstream_config(accounts_dir)
    if isinstance(cfg, dict):
        current.update({k: v for k, v in cfg.items() if k in UPSTREAM_DEFAULTS})
    clean = validate_upstream_patch(current)
    with _lock:
        data = load(accounts_dir)
        data["upstream"] = deep_merge(data.get("upstream"), clean)
        save(accounts_dir, data)
    return clean


USAGE_DEFAULTS = {
    "hour_keep_hours": 48,
    "day_keep_days": 32,
}

# 上限只防止输入失误：面板输入框贴错位数会把 checkpoint 撑到几百 MB，而它每
# 推进一点就要整份重写，路由器上的 flash 写入慢且损耗寿命。两周的小时层与
# 一年的日桶已远超任何窗口够得到的范围。
USAGE_LIMITS = {
    "hour_keep_hours": 24 * 14,
    "day_keep_days": 366,
}

def validate_usage_patch(raw):
    """严格校验面板保存的聚合保留窗口补丁。"""
    out = {}
    for key, value in (raw or {}).items():
        if key not in USAGE_DEFAULTS:
            raise ValueError("unknown usage setting %r" % key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("%s must be a whole number" % key)
        if value < 1:
            raise ValueError("%s cannot be less than 1" % key)
        if value > USAGE_LIMITS[key]:
            raise ValueError("%s cannot be more than %d" % (key, USAGE_LIMITS[key]))
        out[key] = value
    return out

def usage_config(accounts_dir):
    """聚合保留窗口，分小时层与日桶；面板没设过时使用内置默认值。"""
    stored = load(accounts_dir).get("usage")
    stored = stored if isinstance(stored, dict) else {}
    out = {}
    for key, default in USAGE_DEFAULTS.items():
        value = stored.get(key, default)
        if (isinstance(value, bool) or not isinstance(value, int)
                or value < 1 or value > USAGE_LIMITS[key]):
            value = default
        out[key] = value
    return out


def usage_keep_value(accounts_dir, key):
    """实际生效的聚合保留值；环境变量优先，其次面板显式保存，都没有返回 None。"""
    raw = os.environ.get(USAGE_ENV.get(key, ""))
    try:
        env_value = int(raw)
    except (TypeError, ValueError):
        env_value = 0
    if env_value >= 1:
        return min(env_value, USAGE_LIMITS.get(key, 1 << 30))
    stored = load(accounts_dir).get("usage")
    if not isinstance(stored, dict):
        return None
    value = stored.get(key)
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < 1 or value > USAGE_LIMITS.get(key, 1 << 30)):
        return None
    return value


USAGE_ENV = {
    "hour_keep_hours": "WB_USAGE_HOUR_KEEP_HOURS",
    "day_keep_days": "WB_USAGE_DAY_KEEP_DAYS",
}


def usage_effective(accounts_dir):
    """面板显示与实际生效的聚合保留值；环境变量优先于面板值。"""
    stored = load(accounts_dir).get("usage")
    stored = stored if isinstance(stored, dict) else {}
    out = {}
    for key, default in USAGE_DEFAULTS.items():
        raw = os.environ.get(USAGE_ENV.get(key, ""))
        if raw is not None:
            try:
                env_value = int(raw)
            except (TypeError, ValueError):
                env_value = 0
            if env_value >= 1:
                out[key] = min(env_value, USAGE_LIMITS[key])
                continue
        value = stored.get(key)
        if (not isinstance(value, bool) and isinstance(value, int)
                and 1 <= value <= USAGE_LIMITS[key]):
            out[key] = value
            continue
        out[key] = default
    return out


def set_usage_config(accounts_dir, patch):
    """保存校验过的聚合保留窗口补丁。"""
    clean = validate_usage_patch(patch)
    with _lock:
        data = load(accounts_dir)
        data["usage"] = deep_merge(data.get("usage"), clean)
        save(accounts_dir, data)
    return clean

PROMPT_DEFAULTS = {
    "mode": "passthrough",
    "file": "",
}


def validate_prompt_patch(raw):
    """严格校验面板保存的提示词补丁。"""
    out = {}
    for key, value in (raw or {}).items():
        if key == "mode":
            if not isinstance(value, str):
                raise ValueError("prompt mode must be a string")
            mode = value.strip().lower()
            if mode not in ("passthrough", "custom", "append"):
                raise ValueError("prompt mode must be passthrough, custom or append")
            out["mode"] = mode
        elif key == "file":
            if not isinstance(value, str):
                raise ValueError("prompt file must be a string")
            out["file"] = value.strip()
        else:
            raise ValueError("unknown prompt setting %r" % key)
    return out


def prompt_config(accounts_dir):
    """网关 system prompt 模式，默认 passthrough，即原样转发。"""
    stored = load(accounts_dir).get("prompt")
    stored = stored if isinstance(stored, dict) else {}
    mode = stored.get("mode", PROMPT_DEFAULTS["mode"])
    if not isinstance(mode, str) or mode.strip().lower() not in (
            "passthrough", "custom", "append"):
        mode = PROMPT_DEFAULTS["mode"]
    else:
        mode = mode.strip().lower()
    file_path = stored.get("file", PROMPT_DEFAULTS["file"])
    if not isinstance(file_path, str):
        file_path = PROMPT_DEFAULTS["file"]
    return {"mode": mode, "file": file_path.strip()}


def set_prompt_config(accounts_dir, cfg):
    """保存提示词设置，返回存储的配置。"""
    current = prompt_config(accounts_dir)
    if isinstance(cfg, dict):
        current.update({k: v for k, v in cfg.items() if k in PROMPT_DEFAULTS})
    clean = validate_prompt_patch(current)
    with _lock:
        data = load(accounts_dir)
        data["prompt"] = clean
        save(accounts_dir, data)
    return clean


def enable_anthropic_messages(accounts_dir):
    """面板设置是否启用了 Anthropic Messages 兼容层。"""
    return _flag(accounts_dir, "enable_anthropic_messages", False,
                 "WB_ENABLE_ANTHROPIC_MESSAGES")


def set_enable_anthropic_messages(accounts_dir, enabled):
    """保存 Anthropic Messages 开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["enable_anthropic_messages"] = enabled
        save(accounts_dir, data)
    return enabled


def dsh_tool_neutralization(accounts_dir):
    """是否对 DSH 工具调用做通用化处理，默认关闭。"""
    return _flag(accounts_dir, "dsh_tool_neutralization", False,
                 "WB_DSH_TOOL_NEUTRALIZATION")


def set_dsh_tool_neutralization(accounts_dir, enabled):
    """保存 DSH 工具调用通用化开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["dsh_tool_neutralization"] = enabled
        save(accounts_dir, data)
    return enabled


def _env_flag_value(name):
    """环境变量的布尔值：返回 (是否设置, 值)。

    只认 1/true/yes/on 与 0/false/no/off；空串、拼错的值与未知值都不算设置，
    避免把 WB_ENABLE_ANTHROPIC_MESSAGES=maybe 当成开启高风险入口。
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return False, False
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True, True
    if value in ("0", "false", "no", "off"):
        return True, False
    return False, False


def _env_flag(name, default):
    """环境变量的布尔值；没有设置或值非法时回到默认。"""
    present, value = _env_flag_value(name)
    return value if present else default


def _flag(accounts_dir, key, default=False, env=""):
    """读取一个布尔功能开关：环境变量优先于面板值，都没有时用默认值。

    环境变量存在时面板上的保存不生效，面板会显示一项黄色提示说明这一点。
    """
    if env:
        present, value = _env_flag_value(env)
        if present:
            return value
    value = load(accounts_dir).get(key)
    if isinstance(value, bool):
        return value
    return default


def _set_flag(accounts_dir, key, enabled):
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data[key] = enabled
        save(accounts_dir, data)
    return enabled


def prompt_cache_key(accounts_dir):
    """是否向上游附带 prompt_cache_key；默认关闭，等待实测收益。"""
    return _flag(accounts_dir, "prompt_cache_key", False, "WB_PROMPT_CACHE_KEY")


def set_prompt_cache_key(accounts_dir, enabled):
    return _set_flag(accounts_dir, "prompt_cache_key", enabled)


def affinity_debug(accounts_dir):
    """是否输出会话标识派生的调试日志；默认关闭。"""
    return _flag(accounts_dir, "affinity_debug", False, "WB_AFFINITY_DEBUG")


def set_affinity_debug(accounts_dir, enabled):
    return _set_flag(accounts_dir, "affinity_debug", enabled)


def upstream_keepalive(accounts_dir):
    """是否复用上游 HTTP 连接；默认开启。"""
    return _flag(accounts_dir, "upstream_keepalive", True, "WB_UPSTREAM_KEEPALIVE")


def set_upstream_keepalive(accounts_dir, enabled):
    return _set_flag(accounts_dir, "upstream_keepalive", enabled)


def usage_cache(accounts_dir):
    """是否把用量聚合状态写入磁盘；默认开启。"""
    return _flag(accounts_dir, "usage_cache", True, "WB_USAGE_CACHE")


def set_usage_cache(accounts_dir, enabled):
    return _set_flag(accounts_dir, "usage_cache", enabled)


def usage_day_buckets(accounts_dir):
    """窗口统计是否使用日/小时分桶；默认开启。"""
    return _flag(accounts_dir, "usage_day_buckets", True, "WB_USAGE_DAY_BUCKETS")


def set_usage_day_buckets(accounts_dir, enabled):
    return _set_flag(accounts_dir, "usage_day_buckets", enabled)


def recent_index(accounts_dir):
    """请求日志深分页是否使用索引；默认关闭。"""
    return _flag(accounts_dir, "recent_index", False, "WB_RECENT_INDEX")


def set_recent_index(accounts_dir, enabled):
    return _set_flag(accounts_dir, "recent_index", enabled)


def trusted_proxies(accounts_dir):
    """可信反向代理的 IP 或 CIDR 列表，逗号分隔；环境变量优先于面板值。"""
    env = os.environ.get("WB_TRUSTED_PROXIES")
    if env is not None and env.strip():
        return env.strip()
    value = load(accounts_dir).get("trusted_proxies")
    if isinstance(value, str):
        return value
    return ""


def set_trusted_proxies(accounts_dir, value):
    value = str(value or "").strip()
    with _lock:
        data = load(accounts_dir)
        data["trusted_proxies"] = value
        save(accounts_dir, data)
    return value


#: 面板字段 -> 控制它的环境变量。存在时面板值不生效，面板显示黄色提示。
ENV_OVERRIDE_FIELDS = (
    ("enable_anthropic_messages", "WB_ENABLE_ANTHROPIC_MESSAGES"),
    ("dsh_tool_neutralization", "WB_DSH_TOOL_NEUTRALIZATION"),
    ("free_model_credit_fallback", "WB_ENABLE_FREE_MODEL_CREDIT_FALLBACK"),
    ("prompt_cache_key", "WB_PROMPT_CACHE_KEY"),
    ("affinity_debug", "WB_AFFINITY_DEBUG"),
    ("upstream_keepalive", "WB_UPSTREAM_KEEPALIVE"),
    ("usage_cache", "WB_USAGE_CACHE"),
    ("usage_day_buckets", "WB_USAGE_DAY_BUCKETS"),
    ("recent_index", "WB_RECENT_INDEX"),
    ("trusted_proxies", "WB_TRUSTED_PROXIES"),
    ("update_check_enabled", "WB_UPDATE_CHECK_ENABLED"),
    ("update_check_interval_hours", "WB_UPDATE_CHECK_INTERVAL_HOURS"),
)


def panel_flag(accounts_dir, key, default=False):
    """面板保存的布尔开关值；没保存或不是布尔时用默认值，不读环境变量。"""
    value = load(accounts_dir).get(key)
    return value if isinstance(value, bool) else default


def panel_trusted_proxies(accounts_dir):
    """面板保存的可信反向代理列表；没保存时为空串，不读环境变量。"""
    value = load(accounts_dir).get("trusted_proxies")
    return value if isinstance(value, str) else ""


def env_overrides():
    """当前被环境变量控制的设置项：字段名 -> {"name": 变量名, "value": 生效值}。

    只供面板展示：控件显示面板自己的值，黄色提示说明环境变量此刻的取值。
    """
    out = {}
    for field, name in ENV_OVERRIDE_FIELDS:
        if name == "WB_TRUSTED_PROXIES":
            raw = str(os.environ.get(name) or "").strip()
            if raw:
                out[field] = {"name": name, "value": raw}
            continue
        if name == "WB_UPDATE_CHECK_INTERVAL_HOURS":
            try:
                hours = int(os.environ.get(name))
            except (TypeError, ValueError):
                hours = 0
            if 1 <= hours <= UPDATE_CHECK_INTERVAL_MAX:
                out[field] = {"name": name, "value": str(hours)}
            continue
        present, value = _env_flag_value(name)
        if present:
            out[field] = {"name": name, "value": "开启" if value else "关闭"}
    usage = {}
    for key, name in USAGE_ENV.items():
        raw = os.environ.get(name)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value >= 1:
            usage[key] = {"name": name, "value": str(min(value, USAGE_LIMITS[key]))}
    if usage:
        out["usage"] = usage
    return out


def free_model_credit_fallback(accounts_dir):
    """免费模型如 deepseek-v4.1-flash 额度耗尽时，是否可回退到同系列付费
    模型。"""
    return _flag(accounts_dir, "free_model_credit_fallback", False,
                 "WB_ENABLE_FREE_MODEL_CREDIT_FALLBACK")


def set_free_model_credit_fallback(accounts_dir, enabled):
    """保存免费模型付费回退开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["free_model_credit_fallback"] = enabled
        save(accounts_dir, data)
    return enabled

def auto_switch_product(accounts_dir):
    """上游返回 429 时是否轮换账号的出口身份。

    默认关闭，除非操作者打开：轮换身份会消耗请求的重试预算，并把账号留在
    没人选择的通道上。
    """
    return load(accounts_dir).get("auto_switch_product") is True


def set_auto_switch_product(accounts_dir, enabled):
    """保存自动切换开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["auto_switch_product"] = enabled
        save(accounts_dir, data)
    return enabled


def egress_backoff_enabled(accounts_dir):
    """瞬时失败是否记在出口上而非账号上。

    默认关闭，除非操作者打开。打开后传输失败或 502/503/504 不再记入账号
    熔断，改为在请求实际使用的出口上累积秒级退避，见 wb_egress。未设置该
    开关的安装保持原有归账方式。
    """
    return load(accounts_dir).get(EGRESS_BACKOFF_ENABLED_KEY) is True


def set_egress_backoff_enabled(accounts_dir, enabled):
    """保存出口级退避开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data[EGRESS_BACKOFF_ENABLED_KEY] = enabled
        save(accounts_dir, data)
    return enabled


def log_db_enabled(accounts_dir):
    """运行日志是否同时追加写入 usage/logs.sqlite3。

    默认关闭，除非操作者打开：关闭时日志只留在内存环形缓冲里，与上游一致，
    不创建数据库文件。打开后开始持久副本，崩溃、重启或处理缓慢的故障事后
    仍可回看。
    """
    return load(accounts_dir).get(LOG_DB_ENABLED_KEY) is True


def set_log_db_enabled(accounts_dir, enabled):
    """保存日志库开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data[LOG_DB_ENABLED_KEY] = enabled
        save(accounts_dir, data)
    return enabled


def _positive_cap(raw, fallback, hard_max):
    """读取一条保留上限：正整数并按硬上限夹紧。

    不是可用正整数的值一律回落到 fallback，包括 bool、文本、0、负数与手改
    的 1e400，坏掉的 settings.json 不应拖垮日志路径。
    """
    if isinstance(raw, bool):
        return fallback
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if value <= 0:
        return fallback
    return min(value, hard_max)


def _clearable_cap(value, hard_max):
    """写入磁盘前的一条保留上限：空白或无意义值表示不设上限。

    操作者清空字段时面板提交空字符串，输入 0 表达同一意图。两者都存为
    None，即 JSON null，读取端据此视为该维度不限。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number <= 0:
        return None
    return min(number, hard_max)


def log_db_max_rows(accounts_dir):
    """日志库只保留最新 N 行的上限；None 表示不限行数。

    缺失键读作 None：新安装保留每一行，直到天数上限裁剪，这正是行数留空
    的含义。
    """
    raw = load(accounts_dir).get(LOG_DB_MAX_ROWS_KEY)
    return _positive_cap(raw, None, MAX_LOG_DB_MAX_ROWS)


def set_log_db_max_rows(accounts_dir, value):
    """保存行数上限，留空即清除，返回存储的值。"""
    stored = _clearable_cap(value, MAX_LOG_DB_MAX_ROWS)
    with _lock:
        data = load(accounts_dir)
        data[LOG_DB_MAX_ROWS_KEY] = stored
        save(accounts_dir, data)
    return stored


def log_db_max_days(accounts_dir):
    """日志库的天数上限；None 表示不限天数。

    与另外两条不同，缺失键读作 7 天默认值：从未设置过的安装也能得到有界的
    数据库。主动清空字段会存 None 并关闭该上限。
    """
    data = load(accounts_dir)
    if LOG_DB_MAX_DAYS_KEY not in data:
        return DEFAULT_LOG_DB_MAX_DAYS
    raw = data.get(LOG_DB_MAX_DAYS_KEY)
    if raw is None:
        return None
    return _positive_cap(raw, DEFAULT_LOG_DB_MAX_DAYS, MAX_LOG_DB_MAX_DAYS)


def set_log_db_max_days(accounts_dir, value):
    """保存天数上限，留空即清除，返回存储的值。"""
    stored = _clearable_cap(value, MAX_LOG_DB_MAX_DAYS)
    with _lock:
        data = load(accounts_dir)
        data[LOG_DB_MAX_DAYS_KEY] = stored
        save(accounts_dir, data)
    return stored


def log_db_max_bytes(accounts_dir):
    """日志库文件大小上限，单位字节；None 表示不限大小。

    与行数上限一样，缺失键读作 None：新安装由天数默认值保持有界，大小上限
    留给需要对文件本身设硬顶的部署。
    """
    raw = load(accounts_dir).get(LOG_DB_MAX_BYTES_KEY)
    return _positive_cap(raw, None, MAX_LOG_DB_MAX_BYTES)


def set_log_db_max_bytes(accounts_dir, value):
    """保存大小上限，留空即清除，返回存储的值。"""
    stored = _clearable_cap(value, MAX_LOG_DB_MAX_BYTES)
    with _lock:
        data = load(accounts_dir)
        data[LOG_DB_MAX_BYTES_KEY] = stored
        save(accounts_dir, data)
    return stored


def daily_chat_web(accounts_dir):
    """intl 每日签到是否同时发起一次网页通道对话。

    默认开启，除非操作者关闭：该自动化发送的桌面端身份补全不会登记每日
    活跃，网页对话才会，见 issue #75 与 #59。该开关用于让部署恢复为单请求
    签到。
    """
    value = load(accounts_dir).get("daily_chat_web")
    return True if value is None else value is True


def set_daily_chat_web(accounts_dir, enabled):
    """保存网页通道开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["daily_chat_web"] = enabled
        save(accounts_dir, data)
    return enabled


def daily_chat_model(accounts_dir):
    """intl 每日活跃使用的模型；空串表示使用内置模型。

    该活动是两段只负责登记当天的临时对话，没有理由消耗积分。内置默认值已是
    免费目录模型，这里可把活动换到另一个免费模型：当默认模型正是真实流量
    集中的模型时，每日任务就不会占用它的窗口额度。空值表示未设置，调用方
    回落到内置 id。
    """
    value = load(accounts_dir).get("daily_chat_model")
    return str(value or "").strip()


def set_daily_chat_model(accounts_dir, model):
    """保存每日活跃模型 id，返回存储的字符串。"""
    model = str(model or "").strip()
    with _lock:
        data = load(accounts_dir)
        data["daily_chat_model"] = model
        save(accounts_dir, data)
    return model


#: 定时任务在账号之间随机等待的上下限：下限固定 2 秒，上限可由面板调整。
TASK_JITTER_MIN = 2
TASK_JITTER_MAX = 600
TASK_JITTER_DEFAULT = 5


def task_jitter_max(accounts_dir):
    """定时任务账号间隔的随机上限（秒）。

    每个账号开始处理前等待 uniform(2, 上限) 秒，默认值与原先的 2～5 秒一致。
    越界或非法值按默认值处理。
    """
    raw = load(accounts_dir).get("task_jitter_max")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return TASK_JITTER_DEFAULT
    return max(TASK_JITTER_MIN, min(TASK_JITTER_MAX, value))


def set_task_jitter_max(accounts_dir, value):
    """保存随机间隔上限，返回合法化后的秒数。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = TASK_JITTER_DEFAULT
    value = max(TASK_JITTER_MIN, min(TASK_JITTER_MAX, value))
    with _lock:
        data = load(accounts_dir)
        data["task_jitter_max"] = value
        save(accounts_dir, data)
    return value


#: 网页通道单轮等待秒数的默认值，与 wb_accounts.WEB_TURN_TIMEOUT 同值：
#: 没配过的部署保持既有行为，配过的部署按自己的值运行。
DAILY_CHAT_WEB_TIMEOUT_DEFAULT = 120
#: 上限只防止输入失误。每轮等待时间翻倍，配成几百秒会让单账号最坏情况达到
#: 小时级。
DAILY_CHAT_WEB_TIMEOUT_MAX = 3600
#: 网页通道总尝试次数的默认值，含第一轮，与 wb_accounts.WEB_TURN_ATTEMPTS
#: 同值。
DAILY_CHAT_WEB_ATTEMPTS_DEFAULT = 3
#: 上限同样只防止输入失误：最坏等待为 base 乘以 2 的次数次方减 1 秒，5 次已
#: 接近一小时。
DAILY_CHAT_WEB_ATTEMPTS_MAX = 5


def daily_chat_web_retry(accounts_dir):
    """失败的网页通道轮次是否重试。

    默认开启，除非操作者关闭。常见失败是首次等待结束时会话仍处于
    ``working``，属于慢而非坏，而每日活跃是唯一能拿到 30/50 积分的任务，
    首次超时就放弃会静默损失当天奖励。关闭后只尝试一次。
    """
    value = load(accounts_dir).get("daily_chat_web_retry")
    return True if value is None else value is True


def set_daily_chat_web_retry(accounts_dir, enabled):
    """保存网页通道重试开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["daily_chat_web_retry"] = enabled
        save(accounts_dir, data)
    return enabled


def daily_chat_web_attempts(accounts_dir, default=None):
    """网页通道总尝试次数，含第一轮。

    default 是未设置时的返回值，持有自己内置默认值的调用方可保留它。越界或
    非整数值同样回落到 default，手改 settings.json 不会让扫描停在一个账号上。
    """
    if default is None:
        default = DAILY_CHAT_WEB_ATTEMPTS_DEFAULT
    value = load(accounts_dir).get("daily_chat_web_attempts")
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < 1 or value > DAILY_CHAT_WEB_ATTEMPTS_MAX):
        return default
    return value


def set_daily_chat_web_attempts(accounts_dir, attempts):
    """保存总尝试次数，返回存储的整数。"""
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise ValueError("daily_chat_web_attempts must be a whole number")
    if attempts < 1:
        raise ValueError("daily_chat_web_attempts cannot be less than 1")
    if attempts > DAILY_CHAT_WEB_ATTEMPTS_MAX:
        raise ValueError("daily_chat_web_attempts cannot be more than %d"
                         % DAILY_CHAT_WEB_ATTEMPTS_MAX)
    with _lock:
        data = load(accounts_dir)
        data["daily_chat_web_attempts"] = attempts
        save(accounts_dir, data)
    return attempts


def daily_chat_web_timeout(accounts_dir, default=None):
    """网页通道首轮等待秒数，后续轮次翻倍。

    default 是未设置时的返回值，持有自己内置默认值的调用方，例如可由环境
    变量 WB_WEB_TURN_TIMEOUT 覆盖的一方，可保留自己的值，不会因为该设置存在
    而被静默重置为 120。非整数或越界值同样回落到 default，手改
    settings.json 不会让调度器卡在巨大值上或因字符串出错。
    """
    if default is None:
        default = DAILY_CHAT_WEB_TIMEOUT_DEFAULT
    value = load(accounts_dir).get("daily_chat_web_timeout")
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < 1 or value > DAILY_CHAT_WEB_TIMEOUT_MAX):
        return default
    return value


def set_daily_chat_web_timeout(accounts_dir, seconds):
    """保存单轮等待秒数，返回存储的整数秒。"""
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise ValueError("daily_chat_web_timeout must be a whole number of seconds")
    if seconds < 1:
        raise ValueError("daily_chat_web_timeout cannot be less than 1")
    if seconds > DAILY_CHAT_WEB_TIMEOUT_MAX:
        raise ValueError("daily_chat_web_timeout cannot be more than %d"
                         % DAILY_CHAT_WEB_TIMEOUT_MAX)
    with _lock:
        data = load(accounts_dir)
        data["daily_chat_web_timeout"] = seconds
        save(accounts_dir, data)
    return seconds


def local_web_tools(accounts_dir):
    """网关是否自己执行 web_search 与 web_fetch 调用。

    默认关闭，除非操作者打开：关闭时声明服务端搜索工具的客户端在自己的进程
    里执行。打开后网关把自己的函数声明换给模型并在本地执行调用，见
    wb_webtools，网关会亲自抓取模型请求的 URL，因此只做显式开启。
    """
    return load(accounts_dir).get("local_web_tools") is True


def set_local_web_tools(accounts_dir, enabled):
    """保存本地 web 工具开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["local_web_tools"] = enabled
        save(accounts_dir, data)
    return enabled


DEFAULT_TEST_MODEL = "deepseek-v4.1-flash"
TEST_MODEL_REALMS = ("intl", "cn")


def _test_model_key(realm):
    return "test_model_%s" % realm if realm in TEST_MODEL_REALMS else None


def test_model(accounts_dir, realm=None):
    """面板账号测试按出口发送的模型。

    测试会用账号自己的额度执行一次真实补全，因此必须使用该账号可调用的
    模型。只带一个共用值的设置文件，在面板写入某个出口的选择之前，该值继续
    生效。
    """
    data = load(accounts_dir)
    key = _test_model_key(realm)
    value = str(data.get(key) or "").strip() if key else ""
    if not value:
        value = str(data.get("test_model") or "").strip()
    return value or DEFAULT_TEST_MODEL


def test_model_default(accounts_dir, realm=None):
    """该出口自己的选择被清空时使用的模型。"""
    data = load(accounts_dir)
    key = _test_model_key(realm)
    if key and not str(data.get(key) or "").strip():
        legacy = str(data.get("test_model") or "").strip()
        if legacy:
            return legacy
    return DEFAULT_TEST_MODEL


def set_test_model(accounts_dir, value, realm=None):
    """保存某个出口的测试模型，空值恢复默认值。"""
    value = str(value or "").strip()
    with _lock:
        data = load(accounts_dir)
        key = _test_model_key(realm) or "test_model"
        if key != "test_model" and "test_model" in data \
                and not any(str(data.get(k) or "").strip() for k in
                            ("test_model_intl", "test_model_cn")):
            # 只有一个共用值的设置文件：先把该值平摊到两个出口，再删除共用
            # 键，避免改动一个出口时连带改掉另一个出口的取值。
            legacy = str(data.get("test_model") or "").strip()
            data["test_model_intl"] = legacy
            data["test_model_cn"] = legacy
            data.pop("test_model", None)
        data[key] = value
        save(accounts_dir, data)
    return value or test_model(accounts_dir, realm)


def _clean_model_ids(raw):
    """从存储的列表值中得到去重并去除首尾空白的模型 id。"""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, str):
            continue
        model = item.strip()
        if model and model not in out:
            out.append(model)
    return out


def disabled_models(accounts_dir):
    """面板已关闭的模型 id，这些模型的请求不会被服务。"""
    return _clean_model_ids(load(accounts_dir).get("disabled_models"))


def set_disabled_models(accounts_dir, models):
    """保存已关闭的模型 id，保留每个 id 的首次拼写。"""
    cleaned = _clean_model_ids(models)
    with _lock:
        data = load(accounts_dir)
        data["disabled_models"] = cleaned
        save(accounts_dir, data)
    return cleaned


def model_served(accounts_dir, model):
    """面板关闭了该模型 id 时返回 False。"""
    if not model:
        return True
    want = str(model).strip().lower()
    return all(str(entry).strip().lower() != want for entry in disabled_models(accounts_dir))


def excluded_seen(accounts_dir):
    """默认排除已经应用过的模型 id。

    目录里被筛选规则丢掉的模型默认加入停止名单；这份记录保证操作者把它们
    重新勾选之后，下一次加载不会又关掉。
    """
    return _clean_model_ids(load(accounts_dir).get("excluded_seen"))


def remember_excluded(accounts_dir, models):
    """记下已经应用过默认排除的模型，返回整份记录。"""
    with _lock:
        data = load(accounts_dir)
        seen = _clean_model_ids(data.get("excluded_seen"))
        for model in _clean_model_ids(models):
            if model not in seen:
                seen.append(model)
        data["excluded_seen"] = seen
        save(accounts_dir, data)
    return seen


DEFAULT_MESSAGES_FORMAT = "openai-completions"
MESSAGES_FORMATS = ("openai-completions", "openai-responses")


def messages_format(accounts_dir):
    """/v1/messages 翻译后的请求所走的管线。

    openai-completions 直接翻译成 Chat Completions，openai-responses 先翻译
    成 Responses。无法识别的值一律读回默认值。
    """
    value = str(load(accounts_dir).get("messages_format") or "").strip()
    return value if value in MESSAGES_FORMATS else DEFAULT_MESSAGES_FORMAT


def set_messages_format(accounts_dir, value):
    """保存 /v1/messages 使用的管线，未知值重置为默认。"""
    value = str(value or "").strip()
    if value not in MESSAGES_FORMATS:
        value = DEFAULT_MESSAGES_FORMAT
    with _lock:
        data = load(accounts_dir)
        data["messages_format"] = value
        save(accounts_dir, data)
    return value


def accounts_collapsed(accounts_dir):
    """面板账号区是否折叠。

    只有存储值为布尔 true 时才折叠。`is True` 就是全部归一化：手改的
    "false"、1、对象或缺失键都读作展开，任何一个都不会意外隐藏账号区。面板
    只通过 set_accounts_collapsed 写入真正的布尔值。
    """
    return load(accounts_dir).get(ACCOUNTS_COLLAPSED_KEY) is True


def set_accounts_collapsed(accounts_dir, collapsed):
    """保存账号区折叠状态，返回存储的布尔值。"""
    collapsed = bool(collapsed)
    with _lock:
        data = load(accounts_dir)
        data[ACCOUNTS_COLLAPSED_KEY] = collapsed
        save(accounts_dir, data)
    return collapsed


def key_before_hidden(accounts_dir):
    """按 Key 用量表是否折叠 `(切换前)` 行。

    只有存储值为布尔 true 时才隐藏，归一化与 accounts_collapsed 相同：该行
    是有价值的历史，手改的 "false"、1、对象或缺失键都必须让它保持可见。
    """
    return load(accounts_dir).get(KEY_BEFORE_HIDDEN_KEY) is True


def set_key_before_hidden(accounts_dir, hidden):
    """保存 `(切换前)` 行折叠状态，返回存储的布尔值。"""
    hidden = bool(hidden)
    with _lock:
        data = load(accounts_dir)
        data[KEY_BEFORE_HIDDEN_KEY] = hidden
        save(accounts_dir, data)
    return hidden


def _clean_page_keys(value):
    """把存储或提交的页面列表归一化为干净的键列表。

    不是合法键列表的值直接丢弃而非拒绝，理由与上面的布尔开关相同：文件可
    手改，显示偏好的安全方向是“不隐藏任何页面”。重复项与超长列表会被压缩，
    手改无法把面板导航变成同一个条目的重复。
    """
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        if not isinstance(item, str):
            continue
        key = item.strip()
        if not PAGE_KEY_RE.match(key) or key == PAGE_KEEP_VISIBLE or key in out:
            continue
        out.append(key)
        if len(out) >= MAX_HIDDEN_PAGES:
            break
    return out


def hidden_pages(accounts_dir):
    """面板顶栏隐藏的页面，新安装为空。

    每次读取都归一化，手改文件写入字符串、未知键或重复项时，页面不会被
    重复隐藏，也不会隐藏面板之后无法恢复的页面。
    """
    return _clean_page_keys(load(accounts_dir).get(HIDDEN_PAGES_KEY))


def set_hidden_pages(accounts_dir, pages):
    """保存隐藏页面列表，返回归一化后的存储列表。"""
    cleaned = _clean_page_keys(pages)
    with _lock:
        data = load(accounts_dir)
        data[HIDDEN_PAGES_KEY] = cleaned
        save(accounts_dir, data)
    return cleaned


SCHEDULER_GROUPS = ("checkin", "travel", "keepalive", "cat")
DEFAULT_SCHEDULER_HOURS = {
    "checkin": [9, 21],
    "travel": [9, 21],
    "keepalive": [22],
    "cat": [1],
}


def _strict_hours(value, group):
    """把面板提交的一组整点解析为去重升序列表；非法值抛 ValueError。"""
    if isinstance(value, str):
        parts = [part for part in re.split(r"[,;，、\s]+", value) if part]
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        raise ValueError("%s 必须是整点列表" % group)
    out = []
    for item in parts:
        if isinstance(item, bool):
            raise ValueError("%s 只能包含 0-23 的整点" % group)
        try:
            hour = int(item)
        except (TypeError, ValueError):
            raise ValueError("%s 只能包含 0-23 的整点" % group)
        if hour < 0 or hour > 23:
            raise ValueError("%s 只能包含 0-23 的整点" % group)
        if hour not in out:
            out.append(hour)
    return sorted(out)


def _read_hours(raw, default):
    """读取一组整点；缺失沿用默认，显式留空表示这一组不跑。"""
    if raw is None:
        return list(default)
    try:
        value = _strict_hours(raw, "")
    except ValueError:
        return list(default)
    if not value and raw not in ("", [], (), set()):
        return list(default)
    return value


def scheduler_hours(accounts_dir):
    """后台排程各组的整点时段。

    缺失或写坏的组回到默认值（09:00/21:00、22:00、01:00）；显式留空表示
    这一组不运行。读取不改文件，下一次面板保存才会写回。
    """
    raw = load(accounts_dir).get("scheduler_hours")
    if not isinstance(raw, dict):
        return {group: list(DEFAULT_SCHEDULER_HOURS[group])
                for group in SCHEDULER_GROUPS}
    return {group: _read_hours(raw.get(group), DEFAULT_SCHEDULER_HOURS[group])
            for group in SCHEDULER_GROUPS}


def set_scheduler_hours(accounts_dir, hours):
    """保存四组整点时段，返回归一化后的值。"""
    if not isinstance(hours, dict):
        raise ValueError("scheduler_hours 必须是对象")
    cleaned = {
        group: _strict_hours(hours.get(group, DEFAULT_SCHEDULER_HOURS[group]), group)
        for group in SCHEDULER_GROUPS
    }
    with _lock:
        data = load(accounts_dir)
        data["scheduler_hours"] = cleaned
        save(accounts_dir, data)
    return cleaned


def accounts_separate_tab(accounts_dir):
    """面板账号区是否独占一个主标签页。

    只有存储值为布尔 true 时才拆分，归一化与上面的显示开关相同：合并的
    “网关与账号”标签是默认界面，手改的 "false"、1、对象或缺失键都必须保持
    它，不会意外拆分导航。
    """
    return load(accounts_dir).get(ACCOUNTS_SEPARATE_TAB_KEY) is True


def set_accounts_separate_tab(accounts_dir, separate):
    """保存账号标签页开关，返回存储的布尔值。"""
    separate = bool(separate)
    with _lock:
        data = load(accounts_dir)
        data[ACCOUNTS_SEPARATE_TAB_KEY] = separate
        save(accounts_dir, data)
    return separate


def update_check_enabled(accounts_dir):
    """网关是否按间隔自动检查新版本；环境变量优先于面板设置。

    默认关闭，除非操作者打开；没有该键的安装也读作关闭。这是这里唯一缺失值
    表示“否”的设置，因为开启后网关会按自己的计划访问 GitHub。面板的手动
    检查完全不受该开关影响。
    """
    present, value = _env_flag_value("WB_UPDATE_CHECK_ENABLED")
    if present:
        return value
    return load(accounts_dir).get(UPDATE_CHECK_ENABLED_KEY) is True


def set_update_check_enabled(accounts_dir, enabled):
    """保存自动检查开关，返回存储的布尔值。"""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data[UPDATE_CHECK_ENABLED_KEY] = enabled
        save(accounts_dir, data)
    return enabled


def update_check_interval_hours(accounts_dir):
    """自动检查的间隔（小时）；环境变量优先，其次面板值，默认 24。"""
    raw = os.environ.get("WB_UPDATE_CHECK_INTERVAL_HOURS")
    try:
        env_hours = int(raw)
    except (TypeError, ValueError):
        env_hours = 0
    if 1 <= env_hours <= UPDATE_CHECK_INTERVAL_MAX:
        return env_hours
    return panel_update_check_interval_hours(accounts_dir)


def panel_update_check_interval_hours(accounts_dir):
    """面板保存的检查间隔；缺失或越界读作默认值，不读环境变量。"""
    value = load(accounts_dir).get(UPDATE_CHECK_INTERVAL_KEY)
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return UPDATE_CHECK_INTERVAL_DEFAULT
    if hours < 1 or hours > UPDATE_CHECK_INTERVAL_MAX:
        return UPDATE_CHECK_INTERVAL_DEFAULT
    return hours


def set_update_check_interval_hours(accounts_dir, hours):
    """保存自动检查间隔（小时），返回存储的整数值。"""
    hours = int(hours)
    if hours < 1 or hours > UPDATE_CHECK_INTERVAL_MAX:
        raise ValueError("interval must be between 1 and %d hours"
                         % UPDATE_CHECK_INTERVAL_MAX)
    with _lock:
        data = load(accounts_dir)
        data[UPDATE_CHECK_INTERVAL_KEY] = hours
        save(accounts_dir, data)
    return hours


def update_check_state(accounts_dir):
    """上次检查的记账数据：运行时间与看到的版本。

    固定三个字段。24 小时节奏需要 last_attempt 跨重启保留，latest_version
    让面板能在检查后直接回答；检查的其他信息只存在内存里。HTTP 交换中的
    URL、请求头与正文都不会存到这里。
    """
    stored = load(accounts_dir).get(UPDATE_CHECK_STATE_KEY)
    stored = stored if isinstance(stored, dict) else {}
    out = {"last_attempt": 0.0, "last_success": 0.0, "latest_version": ""}
    for key in ("last_attempt", "last_success"):
        value = stored.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            out[key] = float(value)
    version = stored.get("latest_version")
    if isinstance(version, str):
        out["latest_version"] = version.strip()[:32]
    return out


def record_update_check(accounts_dir, at=None, success=False, latest_version=""):
    """写入一次检查的结果，返回存储的状态。

    只有这一条窄写入路径：能写任意键的检查器会把 settings.json 变成更新
    状态数据库，而它明确不承担这个职责。
    """
    stamp = float(at if at is not None else time.time())
    with _lock:
        data = load(accounts_dir)
        state = data.get(UPDATE_CHECK_STATE_KEY)
        state = dict(state) if isinstance(state, dict) else {}
        state["last_attempt"] = stamp
        if success:
            state["last_success"] = stamp
        version = str(latest_version or "").strip()[:32]
        if version:
            state["latest_version"] = version
        data[UPDATE_CHECK_STATE_KEY] = state
        save(accounts_dir, data)
    return update_check_state(accounts_dir)

_SLOT_ID_RE = re.compile(r"^slot-(\d+)$")


def _clean_slot_entry(entry, fallback_id=None):
    """归一化一条存储的出口槽，无法使用时返回 None。

    出口信息由探测写入，探测运行前保持为空。名称为空表示按出口标注该槽，
    这是面板的显示方式；这里不会用 id 静默替换，否则操作者无法区分自动命名
    与手工改名的槽。
    """
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("url") or "").strip()
    if not url:
        return None
    slot_id = str(entry.get("id") or "").strip()
    if not slot_id:
        slot_id = fallback_id or ""
    try:
        probed_at = int(entry.get("probed_at") or 0)
    except (TypeError, ValueError):
        probed_at = 0
    return {
        "id": slot_id,
        "name": str(entry.get("name") or "").strip(),
        "url": url,
        "enabled": entry.get("enabled", True) is not False,
        "ip": str(entry.get("ip") or "").strip(),
        "country": str(entry.get("country") or "").strip(),
        "country_code": str(entry.get("country_code") or "").strip().upper(),
        "ip_type": str(entry.get("ip_type") or "").strip().lower(),
        "isp": str(entry.get("isp") or "").strip(),
        "asn": str(entry.get("asn") or "").strip(),
        "probed_at": probed_at,
    }


def _next_slot_id(used):
    """不在 used 中的下一个 `slot-<n>` id，用于没有 id 的历史列表。"""
    highest = 0
    for slot_id in used:
        match = _SLOT_ID_RE.match(str(slot_id))
        if match:
            highest = max(highest, int(match.group(1)))
    return "slot-%d" % (highest + 1)


def _slot_seq(data):
    """该存储中发放过的最大槽号。"""
    try:
        return int(data.get("proxy_slot_seq") or 0)
    except Exception:
        return 0


def proxy_slots(accounts_dir):
    """所有已配置的出口槽，按存储顺序。

    id 在第二遍补齐：缺少 id 的条目不能占用同一文件中后续条目已用的编号，
    否则两个槽会共用 id。
    """
    data = load(accounts_dir)
    stored = data.get("proxy_slots")
    if not isinstance(stored, list):
        return []
    out, seen, used_ids = [], set(), set()
    for raw in stored:
        entry = _clean_slot_entry(raw)
        if entry and entry["url"] not in seen:
            seen.add(entry["url"])
            if entry["id"]:
                used_ids.add(entry["id"])
            out.append(entry)
    for entry in out:
        if not entry["id"]:
            entry["id"] = _next_slot_id(used_ids)
            used_ids.add(entry["id"])
    return out


def set_proxy_slots(accounts_dir, slots):
    """替换整个槽列表，返回存储的列表。

    id 取自只增不减的计数器：账号会持久化自己绑定的 id，回收已释放的编号会
    让现有账号指向新槽的出口 IP。
    """
    with _lock:
        data = load(accounts_dir)
        stored = data.get("proxy_slots")
        stored = stored if isinstance(stored, list) else []

        seq = _slot_seq(data)
        # 同时从传入与现有列表取种子，这次保存中正在删除的 id 也不会发给
        # 新条目。
        for entry in list(stored) + list(slots or []):
            if not isinstance(entry, dict):
                continue
            match = _SLOT_ID_RE.match(str(entry.get("id") or ""))
            if match:
                seq = max(seq, int(match.group(1)))
        # 没有 id 的历史列表显示为 slot-1 到 slot-N，计数器也要高于这些
        # 编号。
        seq = max(seq, len(stored))

        cleaned, seen = [], set()
        for raw in slots or []:
            entry = _clean_slot_entry(raw)
            if not entry or entry["url"] in seen:
                continue
            seen.add(entry["url"])
            if not entry["id"] or any(e["id"] == entry["id"] for e in cleaned):
                seq += 1
                entry["id"] = "slot-%d" % seq
            cleaned.append(entry)
        data["proxy_slots"] = cleaned
        data["proxy_slot_seq"] = seq
        save(accounts_dir, data)
        return cleaned


def update_proxy_slot(accounts_dir, slot_id, fields, defaults=None):
    """把 fields 合并进一个已存槽，返回合并后的副本，槽不存在时返回 None。

    读取与写入都在存储锁内完成。调用方自行读改写会与面板保存竞争，写回一份
    已与磁盘不一致的列表，静默覆盖另一个写方的改动。

    defaults 只应用于写入时仍为空的字段，调用方工作期间操作者输入的值不会被
    派生值覆盖。
    """
    slot_id = str(slot_id or "").strip()
    if not slot_id:
        return None
    with _lock:
        out, found = [], None
        for entry in proxy_slots(accounts_dir):
            if entry["id"] == slot_id and found is None:
                entry = dict(entry)
                entry.update(fields)
                for key, value in (defaults or {}).items():
                    if not entry.get(key):
                        entry[key] = value
                found = entry
            out.append(entry)
        if found is None:
            return None
        data = load(accounts_dir)
        data["proxy_slots"] = out
        save(accounts_dir, data)
        return found


def drop_missing_bindings(pool, slots):
    """清除指向已不存在槽的绑定。

    否则过期 id 会留在账号上，之后拿到该 id 的槽会接管这个账号。
    """
    valid = {entry["id"] for entry in slots}
    changed = 0
    for account in list(getattr(pool, "accounts", []) or []):
        if account.proxy_slot and account.proxy_slot not in valid:
            account.proxy_slot = ""
            try:
                account.save(pool.dir)
            except Exception:
                pass
            changed += 1
    if changed:
        pool.apply_proxy_slots(slots)
    return changed


def find_proxy_slot(accounts_dir, slot_id):
    slot_id = str(slot_id or "").strip()
    if not slot_id:
        return None
    for entry in proxy_slots(accounts_dir):
        if entry["id"] == slot_id:
            return entry
    return None


class PanelSessions(object):
    """面板登录成功后发放的内存 bearer token。

    有意不持久化：重启网关会让浏览器退出登录，这对暴露在端口映射后的局域网
    工具是更安全的默认行为。
    """

    def __init__(self, ttl=SESSION_TTL):
        self.ttl = ttl
        self._tokens = {}
        self._lock = threading.RLock()

    def create(self):
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[token] = time.time() + self.ttl
        return token

    def valid(self, token):
        if not token:
            return False
        with self._lock:
            expiry = self._tokens.get(token)
            if not expiry:
                return False
            if expiry < time.time():
                self._tokens.pop(token, None)
                return False
            return True

    def revoke(self, token):
        if not token:
            return
        with self._lock:
            self._tokens.pop(token, None)

    def revoke_all(self):
        with self._lock:
            self._tokens.clear()
