import email.utils
import hashlib
from collections import deque
import re
import json
import os
# 单请求体上限。body 会先整体读成 bytes、再 decode 成 str、最后 json.loads，
# 峰值内存约为 body 的数倍；读取发生在 chat 信号量之前，处理线程数没有上限，
# 几个并发大 body 就可能耗尽小内存设备。16MB 足够正常客户端使用，需要更大时
# 显式设置 WB_MAX_PAYLOAD_BYTES，内存代价由部署方承担。
MAX_PAYLOAD_BYTES = int(os.environ.get("WB_MAX_PAYLOAD_BYTES", 16 * 1024 * 1024))
# 一次 chat 上游调用最长占用处理线程 600 秒，每个请求各占一个线程。
# 并发 chat 与 responses 请求必须有上限，否则少量慢客户端就能占住大量线程
# 与内存；面板与管理接口不受限制。超出的调用短暂等待后返回 503。
#
# 默认上限固定为 32，与账号池规模无关；WB_MAX_CONCURRENT_CHAT=auto 的按池
# 计算语义见 resize_chat_slots。
_CHAT_SLOTS_ENV = os.environ.get("WB_MAX_CONCURRENT_CHAT", "32").strip().lower()
CHAT_SLOTS_AUTO = _CHAT_SLOTS_ENV in ("auto", "pool", "dynamic")
CHAT_SLOTS_FLOOR = 32
try:
    MAX_CONCURRENT_CHAT = CHAT_SLOTS_FLOOR if CHAT_SLOTS_AUTO else int(_CHAT_SLOTS_ENV)
except ValueError:
    MAX_CONCURRENT_CHAT = CHAT_SLOTS_FLOOR
CHAT_SLOT_WAIT_SECONDS = float(os.environ.get("WB_CHAT_SLOT_WAIT", 30))
import socket
import sys
import threading
import time
import urllib.error
import subprocess
import wb_accounts
import wb_catalog
import wb_egress
import wb_logstore
import wb_messages_responses
import wb_pricing
import wb_settings
import wb_upstream_pool
import wb_webtools
import wb_identity
import wb_prompt
from wb_ids import new_id as _new_id
from wb_sanitize import (
    DSML_CALLS_RE, LeakedToolCallStreamFilter, _dsh_tool_neutralization_enabled,
    backfill_reasoning_content, chat_finish_chunk, chat_text_chunk,
    chat_tool_call_chunks, cleanup_orphan_tool_calls, is_dsh_payload,
    is_truncated_arguments, merge_adjacent_tool_calls, neutralize_dsh_tool_defs,
    normalize_roles, normalize_stop, normalize_tool_choice, normalize_tools,
    parse_dsml_tool_calls, prepare_dsh_tool_aliases, repack_tool_result_blocks,
    repair_leaked_tool_calls, restore_dsh_tool_names, sanitize_messages,
    translate_max_completion_tokens,
)
import wb_usage
from wb_translate import (
    _anthropic_estimate_chat_tokens, _responses_usage,
    additional_tools_from_input, aggregate_stream, anthropic_error_obj,
    anthropic_sse_frame, chat_to_messages, chat_to_response, custom_tool_names,
    estimate_tokens, follow_up_with_tool_results, heartbeat_frame,
    internal_calls_from_chat, local_ip_addresses, messages_to_chat,
    responses_to_chat, stream_messages_events, stream_responses_events, sum_usage,
    upstream_error_text, web_tools_active,
)

from wb_models import (
    VIRTUAL_ALIAS_MODELS, _catalog_excluded, _catalog_raw_meta, _models_cache, catalog_meta, fetch_models, model_entry,
    read_cached_remote_catalog, refresh_catalog, seed_default_disabled,
    start_catalog_daily,
)

MessageStreamWriter = wb_messages_responses.MessageStreamWriter
messages_to_responses = wb_messages_responses.messages_to_responses
response_to_message = wb_messages_responses.response_to_message
stream_responses_to_message = wb_messages_responses.stream_responses_to_message
IS_WINDOWS = os.name == "nt"

# 一键配置客户端只在看板与网关同机打开时可用。
# 该功能写的是网关进程所在机器的客户端配置，服务端、Docker 与 OpenWrt 部署下
# 看板是远程打开的，改动到不了用户电脑，还会白付探测与常驻开销。
# wb_agents 因此懒加载：只有下面这道检查放行、路由真正命中时才 import。
_AGENTS_MODULE = None
_SERVER_DEPLOYMENT = None


def agents_module():
    """按需 import wb_agents；用不到它的部署不载入该模块。"""
    global _AGENTS_MODULE
    if _AGENTS_MODULE is None:
        import wb_agents
        _AGENTS_MODULE = wb_agents
    return _AGENTS_MODULE


def server_deployment_form():
    """进程是否以服务端形态运行，只认容器与 OpenWrt 标记。

    看板是否本机打开由 agents_client_allowed 按请求来源地址判断。
    结果缓存，进程存活期间不变。
    """
    global _SERVER_DEPLOYMENT
    if _SERVER_DEPLOYMENT is None:
        blocked = False
        try:
            if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
                blocked = True
            elif os.path.exists("/proc/1/cgroup"):
                with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as fh:
                    cgroup = fh.read().lower()
                blocked = any(m in cgroup for m in ("docker", "containerd", "kubepods"))
        except Exception:
            blocked = False
        if not blocked:
            try:
                if os.path.exists("/etc/openwrt_release"):
                    blocked = True
                elif os.path.exists("/etc/os-release"):
                    with open("/etc/os-release", encoding="utf-8", errors="replace") as fh:
                        blocked = "id=openwrt" in fh.read().lower()
            except Exception:
                blocked = False
        _SERVER_DEPLOYMENT = blocked
    return _SERVER_DEPLOYMENT


def agents_client_allowed(client_address):
    """这个来源地址能否使用一键配置，只接受回环地址。

    非回环说明看板是远程打开的，例如手机、另一台电脑或容器端口映射；
    此时 apply 改的是容器或服务器里的配置目录，按不可用处理。
    """
    if server_deployment_form():
        return False
    try:
        addr = str(client_address[0])
    except Exception:
        return False
    return addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def launcher_hint(port):
    """在另一个端口启动网关时使用的启动命令。"""
    if IS_WINDOWS:
        return "start-wb-proxy.bat %d" % port
    return "./start-wb-proxy.sh %d" % port
def port_owner_hint(port):
    """列出占用本地 TCP 端口的进程的命令。"""
    if IS_WINDOWS:
        return "netstat -ano | findstr :%d" % port
    return "lsof -nP -iTCP:%d -sTCP:LISTEN" % port
CURRENT_REALM = os.environ.get("WB_PROXY_DEFAULT_REALM", "intl")
INTL_EXCLUSIVE_PREFIXES = ("gpt-", "gemini-")
INTL_EXCLUSIVE = {
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash",
    "grok-4.7",
}
_CN_DETECT_ONLY = {
    "deepseek-v4-pro", "minimax-m3", "minimax-m2.7", "minimax-m2.5",
    "glm-5.1", "glm-5.0-turbo", "glm-4.6v",
    "kimi-k3-1", "kimi-k2.7", "kimi-k2-thinking",
    "hy3-x", "hy4-preview-dev", "hy4-preview-x",
}
def detect_model_realm(model_id):
    if not model_id:
        return CURRENT_REALM
    m = str(model_id).lower()
    if m in INTL_EXCLUSIVE or m.startswith(INTL_EXCLUSIVE_PREFIXES):
        return "intl"
    if m in _CN_DETECT_ONLY or m.startswith(("minimax-", "deepseek-v4-pro")):
        return "cn"
    return CURRENT_REALM
# 只在单侧出口提供的模型。deepseek-v4.1-flash、hy3、glm-5.3 等两侧都提供，
# 不能按冲突处理。glm-5.3-flash 由国际出口提供，也不在单侧列表里。
CN_EXCLUSIVE_PREFIXES = ("minimax-", "deepseek-v4-pro")
CN_EXCLUSIVE = {
    "deepseek-v4-pro", "glm-5.1", "glm-5v-turbo",
    "kimi-k3-1", "kimi-k2.7", "minimax-m3",
    "hy3-x", "hy4-preview-dev", "hy4-preview-x",
}
def exclusive_realm(model_id):
    """只有单侧出口提供时返回 "intl" 或 "cn"，否则返回空串。"""
    if not model_id:
        return ""
    m = str(model_id).lower()
    if m in INTL_EXCLUSIVE or m.startswith(INTL_EXCLUSIVE_PREFIXES):
        return "intl"
    if m in CN_EXCLUSIVE or m.startswith(CN_EXCLUSIVE_PREFIXES):
        return "cn"
    return ""
def install_console_close_handler():
    """控制台窗口被关闭时释放端口。

    Windows 关闭控制台不会杀掉子进程，由 .bat 启动的网关会继续运行并占着
    端口，下一次启动就会误报已有实例在运行。关闭窗口会向同一控制台的所有
    进程发送 CTRL_CLOSE_EVENT，注册处理函数即可在事件发生时退出，不必轮询
    父进程号。没有控制台时注册无副作用。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6
        def _handler(event):
            if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                os._exit(0)
            return False
        handler = PHANDLER_ROUTINE(_handler)   # keep the callback referenced
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
            return None
        return handler
    except Exception:
        return None

# ---------------------------------------------------------------------------
# 日志终端查看器采用双进程模型。
#
# 服务进程由 pythonw 静默后台运行，没有窗口，关闭窗口也不会停止。用户看到的
# 终端是独立的 console_tail.py 进程，只连本地 /logs 接口实时打印日志，关掉它
# 不影响服务。启动脚本会打开一个，面板的眼睛按钮可以再开或关一个。
# ---------------------------------------------------------------------------
CONSOLE_VIEWER_PROC = None
_LISTEN_PORT = 8788

def _console_python():
    """找到当前解释器旁边带控制台的 python。

    服务由 pythonw 启动，sys.executable 指向 pythonw.exe；要打开可见窗口，
    需要同目录下的 python.exe。找不到时返回当前解释器。
    """
    exe = sys.executable or "python"
    d = os.path.dirname(exe)
    cand = os.path.join(d, "python.exe")
    if os.path.exists(cand):
        return cand
    if exe.lower().endswith("pythonw.exe"):
        cand2 = exe[: -len("pythonw.exe")] + "python.exe"
        if os.path.exists(cand2):
            return cand2
    return exe

def _console_viewer_running():
    return bool(CONSOLE_VIEWER_PROC and CONSOLE_VIEWER_PROC.poll() is None)

def _launch_console_viewer():
    """在独立的新控制台窗口启动 console_tail.py。

    启动成功返回 True，已在运行返回 False，脚本不存在返回 None。
    传入服务端生成的面板令牌，面板密码被修改后查看器仍可用。
    """
    global CONSOLE_VIEWER_PROC
    if _console_viewer_running():
        return False
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "console_tail.py")
    if not os.path.exists(script):
        return None
    py = _console_python()
    try:
        token = PANEL.create()
    except Exception:
        token = ""
    try:
        proc = subprocess.Popen(
            [py, "-u", script, "--port", str(_LISTEN_PORT), "--token", token],
            creationflags=0x00000010,  # 新建控制台窗口
            stdin=subprocess.DEVNULL,
        )
        CONSOLE_VIEWER_PROC = proc
        return True
    except Exception:
        return None

def _kill_console_viewer():
    global CONSOLE_VIEWER_PROC
    if _console_viewer_running():
        try:
            CONSOLE_VIEWER_PROC.terminate()
        except Exception:
            pass
        CONSOLE_VIEWER_PROC = None
        return True
    CONSOLE_VIEWER_PROC = None
    return False

CHAT_PATH = "/v2/chat/completions"
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."

# ---------------------------------------------------------------------------
# 桌面客户端的后台模型改写。
#
# 内置 model routing config 把记忆生成、环境建议、临时生成与自动化等后台
# 任务发往硬编码模型，不跟随用户在主对话线程选的模型，同样消耗付费积分。
# 这里改写为免费目标，功能照常且不消耗付费积分。WB_BACKGROUND_MODEL_NAMES 与
# WB_BACKGROUND_MODEL_TARGET 覆盖默认值。
# ---------------------------------------------------------------------------
BACKGROUND_MODEL_TARGET = os.environ.get("WB_BACKGROUND_MODEL_TARGET", "deepseek-v4.1-flash")
BACKGROUND_MODEL_NAMES = tuple(
    m.strip().lower()
    for m in os.environ.get(
        "WB_BACKGROUND_MODEL_NAMES",
        "gpt-5.6-terra,gpt-5.6-luna,gpt-5.3-codex",
    ).split(",")
    if m.strip()
)


def _apply_background_model_alias(payload):
    """把客户端已知的后台任务模型改写为免费目标模型。"""
    if not isinstance(payload, dict):
        return
    name = payload.get("model")
    if not isinstance(name, str):
        return
    if name.strip().lower() not in BACKGROUND_MODEL_NAMES:
        return
    payload["model"] = BACKGROUND_MODEL_TARGET
    log("alias: background model %s -> %s" % (name, BACKGROUND_MODEL_TARGET))


# WorkBuddy AI 桌面应用每次启动会把账号产品配置缓存到这里。该文件带有应用
# 模型选择器展示的真实目录，共 21 个模型，含 deepseek-v4.1-flash 与
# gpt-6-astra；CLI 使用的 /v2/enterprises/personal/models 端点返回的列表更窄，
# 因此优先读缓存，读取失败再回退到端点。
PRODUCT_CONFIG_CACHE = os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "cache", "acc-product-config-v3.json")
_CHUNK_SIZE_RE = re.compile(rb"[0-9A-Fa-f]{1,16}")


def _chunk_size(line):
    """从 chunked 编码长度行取出块大小，格式不合法返回 None。

    直接用 int(x, 16) 还会接受 "-30d40" 与 "+1f"；负数会让累计长度落到
    请求体上限之下。
    """
    field = line.split(b";", 1)[0].strip()
    if not _CHUNK_SIZE_RE.fullmatch(field):
        return None
    return int(field, 16)


class BodyTooLarge(Exception):
    """请求体超过配置上限时抛出。"""
    def __init__(self, length):
        super(BodyTooLarge, self).__init__(length)
        self.length = length
class BadJSON(Exception):
    """请求体存在但不是 JSON 对象时抛出。"""
# CORS 只用于浏览器里的聊天客户端跨源调用 OpenAI 风格接口。
# 管理路由由看板同源访问，不返回 ACAO 头，局域网里的其他页面因此读不到响应。
# 位于 /v1 下的管理路径不能被当作 API，例如 /v1/usage 报告账号支出并受面板会话保护。
CORS_PATH_PREFIXES = ("/v1", "/chat", "/completions", "/models", "/responses")
MANAGEMENT_PATH_PREFIXES = ("/v1/usage", "/usage", "/accounts", "/settings",
                            "/tasks", "/scheduler", "/panel", "/logs")

#: 账号被上游限制、网关停止时仍然拒绝的 POST 路径。只放行本机管理动作
#: （启停账号、导入导出、设置），凡是会带账号令牌访问上游的入口都返回 503。
STAND_DOWN_POST_PATHS = frozenset((
    "/v1/chat/completions", "/chat/completions",
    "/v1/completions", "/completions",
    "/v1/responses", "/responses",
    "/v1/messages", "/messages",
    "/v1/messages/count_tokens", "/messages/count_tokens",
    "/accounts/checkin", "/accounts/credits",
    "/accounts/daily-chat", "/accounts/daily-chat-web",
    "/accounts/test", "/accounts/test-all", "/accounts/refresh",
    "/accounts/login/start",
    "/tasks/run", "/tasks/travel", "/tasks/streak-bonus",
    "/scheduler/trigger", "/catalog/refresh",
))
def cors_origin_allowed(path):
    """OpenAI 风格 API 路径是否需要返回 CORS 头。"""
    path = (path or "").split("?")[0]
    if path.startswith(MANAGEMENT_PATH_PREFIXES):
        return False
    return path.startswith(CORS_PATH_PREFIXES)


_chat_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CHAT)


def resize_chat_slots(ready_accounts):
    """按账号池规模调整 chat 并发上限，仅在配置为 auto 时生效。

    WB_MAX_CONCURRENT_CHAT=auto 时，上限等于就绪账号数，最低 CHAT_SLOTS_FLOOR；
    固定数字配置下本函数不做任何事。上限只增不减：已在执行的请求持有旧信号量
    的许可，缩小上限会让在途数量超过新上限，多余的释放会让 BoundedSemaphore
    抛出 ValueError。账号池在启动时加载一次，实际调用发生在监听开始之前。
    """
    global _chat_slots, MAX_CONCURRENT_CHAT
    if not CHAT_SLOTS_AUTO:
        return MAX_CONCURRENT_CHAT
    try:
        wanted = max(CHAT_SLOTS_FLOOR, int(ready_accounts or 0))
    except (TypeError, ValueError):
        return MAX_CONCURRENT_CHAT
    if wanted <= MAX_CONCURRENT_CHAT:
        return MAX_CONCURRENT_CHAT
    MAX_CONCURRENT_CHAT = wanted
    _chat_slots = threading.BoundedSemaphore(wanted)
    log("chat slots : %d (one per ready account; WB_MAX_CONCURRENT_CHAT=auto)"
        % wanted)
    return wanted
#: 目录落盘存档：每次成功从上游 /v3/config 拉到清单就写一份响应体副本，仅留档，
#: 网关自己不读。与 usage 一样运行时产生并列入 .gitignore；目录用
#: WB_PROXY_CATALOG_DIR 覆盖。
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(APP_DIR)
VERSION = "0.6.0"
# 用量记账：每个上游响应都带 usage 块，网关同时为每个请求写一行 JSONL。
# 默认目录在本脚本旁边，可用 --usage-dir 或 WB_PROXY_USAGE_DIR 覆盖。
USAGE_DIR = os.environ.get("WB_PROXY_USAGE_DIR") \
    or os.path.join(PROJECT_DIR, "usage")
USAGE_LOG = os.path.join(USAGE_DIR, "usage.jsonl")
WEB_DIR = os.path.join(APP_DIR, "web")
WEB_INDEX = os.path.join(WEB_DIR, "index.html")
WEB_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
# 看板访问控制：看板使用独立密码，默认 admin，与 /v1 API key 无关。
# 会话只存在内存里，重启后浏览器需要重新登录。
PANEL = wb_settings.PanelSessions()
API_KEY_FILE_SET = False
def configured_keys():
    """面板管理的 API key，每次重新读取，面板修改立即生效。"""
    try:
        return wb_settings.api_keys(ACCOUNTS_DIR)
    except Exception as exc:
        log("could not read api keys: %s" % exc)
        return []
def auth_required():
    """调用 /v1 接口是否必须提供 key。"""
    if wb_settings.auth_disabled(ACCOUNTS_DIR):
        return False
    if any(entry.get("enabled") for entry in configured_keys()):
        return True
    return bool(API_KEY)
def identify_key(supplied):
    """返回调用方使用的 key 条目，没有匹配时返回 None。

    面板配置了至少一个 key 后，这些 key 是唯一接受的凭据，启动参数里的
    本地 key 不能继续生效。
    """
    extra = () if configured_keys() else (API_KEY,)
    return wb_settings.match_api_key(ACCOUNTS_DIR, supplied, extra_keys=extra)
# ---------------------------------------------------------------------------
# API Key 在日志里的显示名。
# ---------------------------------------------------------------------------
# 请求日志按账号记录时，多人共用网关无法分辨发起人。这里把 key 的 name 拼进
# 日志行，未填 name 时使用 id。只做 id 到显示名的只读查表，key 本身仍由调用方
# 显式传入，见 record_usage 的说明；名单带短 TTL 缓存，避免每条请求都读
# settings.json。
_KEY_LABEL_TTL = 5.0
_key_label_cache = {"at": 0.0, "map": {}}
_key_label_lock = threading.Lock()


def key_label(key_id):
    """key 的 settings id 到日志显示名；name 为空时回退 id。

    空 id 包括未带 key、面板会话与未配置 key 的部署，返回空串，调用方据此
    决定是否拼接这一段；未知 id 返回「已删除的 Key」，日志里仍能对应到具体 key。
    """
    if not key_id:
        return ""
    key_id = str(key_id)
    if key_id == "panel":
        return "面板测试台"
    if key_id == wb_usage.KEY_BUCKET_BEFORE:
        return "(切换前)"
    if key_id == wb_usage.KEY_BUCKET_ANON:
        return "(无 key)"
    if key_id == wb_usage.KEY_BUCKET_UNKNOWN:
        return "已删除的 Key"
    now = time.time()
    with _key_label_lock:
        if (now - _key_label_cache["at"]) > _KEY_LABEL_TTL:
            try:
                _key_label_cache["map"] = {
                    str(entry.get("id")): (str(entry.get("name") or "").strip()
                                           or str(entry.get("id")))
                    for entry in configured_keys()
                }
            except Exception:
                _key_label_cache["map"] = {}
            _key_label_cache["at"] = now
        return _key_label_cache["map"].get(str(key_id)) or "已删除的 Key"




LIMIT_EVENTS_FILE = os.path.join(USAGE_DIR, "limit-events.jsonl")
# 按天持久化的估算数据：每天的预算与每个组合的截断前后分段。必须跟随
# --usage-dir，因为常量在 import 时按默认目录算过。
ESTIMATE_DAILY_FILE = os.path.join(USAGE_DIR, "estimate-daily.json")
# recent 的行偏移索引（页 = 日志尾部的第 N 页），同样必须跟随 --usage-dir。
RECENT_INDEX_FILE = os.path.join(USAGE_DIR, "usage-recent-index.json")


POOL = None
SCHEDULER = None
PRICING = None
CREDITS_REFRESHER = None
# 只做 Release 发现：检查器从不下载或安装任何东西。
UPDATES = None
ACCOUNTS_DIR = os.environ.get("ACCOUNTS_DIR") \
    or os.path.join(PROJECT_DIR, 'accounts')


API_KEY = None
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


def runtime_settings_view():
    """面板可见的当前设置；从不返回密码或 key 明文。"""
    key = API_KEY or ""
    if len(key) > 8:
        masked = key[:4] + "*" * 6 + key[-4:]
    else:
        masked = "*" * len(key)
    keys = []
    usage_by_key = wb_usage.key_usage_snapshot()
    now = time.time()
    for entry in configured_keys():
        raw = entry.get("key") or ""
        item = {
            "id": entry.get("id") or "",
            "name": entry.get("name") or "",
            "realm": entry.get("realm") or "",
            "models": list(entry.get("models") or []),
            "concurrency_limit": entry.get("concurrency_limit") or 0,
            "enabled": entry.get("enabled", True) is not False,
            "masked": (raw[:4] + "*" * 6 + raw[-4:]) if len(raw) > 8 else "*" * len(raw),
            "source": entry.get("source") or "panel",
            "created_at": entry.get("created_at") or "",
            "expires_at": entry.get("expires_at") or 0,
            "quota_tokens": entry.get("quota_tokens") or 0,
            "quota_credit": entry.get("quota_credit") or 0.0,
            "ip_allowlist": list(entry.get("ip_allowlist") or []),
            "usage_reset_at": entry.get("usage_reset_at") or 0,
        }
        stat = usage_by_key.get(item["id"]) or wb_usage._new_key_usage_stat()
        item["usage"] = {
            "requests": stat["requests"],
            "total_tokens": stat["total_tokens"],
            "credit": round(stat["credit"], 6),
            "last_used_at": stat["last_used_at"],
        }
        item["status"] = wb_settings.key_status(item, stat, now)
        keys.append(item)
    # 已删除的 key 是只读历史：密钥已不存在，面板只能列出名称与日期，
    # 不能提供复制按钮。
    deleted_keys = []
    for entry in wb_settings.api_keys(ACCOUNTS_DIR, include_deleted=True):
        if not entry.get("deleted_at"):
            continue
        deleted_keys.append({
            "id": entry.get("id") or "",
            "name": entry.get("name") or "",
            "realm": entry.get("realm") or "",
            "created_at": entry.get("created_at") or "",
            "deleted_at": entry.get("deleted_at") or "",
        })
    return {
        "panel_password_is_default": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
        "api_key_set": bool(key),
        "api_key_set_by_panel": API_KEY_FILE_SET,
        "api_key_masked": masked,
        "auth_required": auth_required(),
        "api_keys": keys,
        "deleted_api_keys": deleted_keys,
        "limits": wb_settings.limits_snapshot(ACCOUNTS_DIR),
        "reserve_credits": wb_settings.reserve_credits(ACCOUNTS_DIR),
        "daily_token_limit": wb_settings.daily_token_limit(ACCOUNTS_DIR),
        "daily_credit_limit": wb_settings.daily_credit_limit(ACCOUNTS_DIR),
        "model_daily_token_limit": wb_settings.model_daily_token_limit(ACCOUNTS_DIR),
        "pricing_refresh_minutes": wb_settings.pricing_refresh_minutes(ACCOUNTS_DIR),
        "credits_refresh_hours": wb_settings.credits_refresh_hours(ACCOUNTS_DIR),
        "cost_estimate": wb_settings.cost_estimate(ACCOUNTS_DIR),
        "section_max_height": wb_settings.section_max_height(ACCOUNTS_DIR),
        "ui_language": wb_settings.ui_language(ACCOUNTS_DIR),
        "pricing_variant_inherit": wb_settings.pricing_variant_inherit(ACCOUNTS_DIR),
        "pricing_enabled": wb_settings.pricing_enabled(ACCOUNTS_DIR),
        "auto_switch_product": wb_settings.auto_switch_product(ACCOUNTS_DIR),
        # 下面这些项可能被环境变量覆盖：视图给面板自己的值，环境变量取值在
        # env_overrides 里单独交给面板显示，运行时的读取仍然环境变量优先。
        "free_model_credit_fallback":
            wb_settings.panel_flag(ACCOUNTS_DIR, "free_model_credit_fallback", False),
        "enable_anthropic_messages":
            wb_settings.panel_flag(ACCOUNTS_DIR, "enable_anthropic_messages", False),
        "dsh_tool_neutralization":
            wb_settings.panel_flag(ACCOUNTS_DIR, "dsh_tool_neutralization", False),
        "daily_chat_web": wb_settings.daily_chat_web(ACCOUNTS_DIR),
        "daily_chat_web_retry": wb_settings.daily_chat_web_retry(ACCOUNTS_DIR),
        "daily_chat_web_attempts":
            wb_settings.daily_chat_web_attempts(ACCOUNTS_DIR,
                                                default=wb_accounts.WEB_TURN_ATTEMPTS),
        "daily_chat_web_attempts_default": wb_accounts.WEB_TURN_ATTEMPTS,
        "daily_chat_web_attempts_max": wb_settings.DAILY_CHAT_WEB_ATTEMPTS_MAX,
        "daily_chat_web_timeout":
            wb_settings.daily_chat_web_timeout(ACCOUNTS_DIR,
                                               default=wb_accounts.WEB_TURN_TIMEOUT),
        "daily_chat_web_timeout_default": wb_accounts.WEB_TURN_TIMEOUT,
        "daily_chat_web_timeout_max": wb_settings.DAILY_CHAT_WEB_TIMEOUT_MAX,
        "task_jitter_max": wb_settings.task_jitter_max(ACCOUNTS_DIR),
        "task_jitter_min": wb_settings.TASK_JITTER_MIN,
        "task_jitter_max_limit": wb_settings.TASK_JITTER_MAX,
        "scheduler_hours": wb_settings.scheduler_hours(ACCOUNTS_DIR),
        "scheduler_hours_default": wb_settings.DEFAULT_SCHEDULER_HOURS,
        "daily_chat_model": wb_settings.daily_chat_model(ACCOUNTS_DIR),
        "daily_chat_model_default": wb_accounts.DAILY_CHAT_MODEL,
        "local_web_tools": wb_settings.local_web_tools(ACCOUNTS_DIR),
        "prompt_cache_key": wb_settings.panel_flag(ACCOUNTS_DIR, "prompt_cache_key", False),
        "affinity_debug": wb_settings.panel_flag(ACCOUNTS_DIR, "affinity_debug", False),
        "upstream_keepalive": wb_settings.panel_flag(ACCOUNTS_DIR, "upstream_keepalive", True),
        "usage_cache": wb_settings.panel_flag(ACCOUNTS_DIR, "usage_cache", True),
        "usage_day_buckets": wb_settings.panel_flag(ACCOUNTS_DIR, "usage_day_buckets", True),
        "recent_index": wb_settings.panel_flag(ACCOUNTS_DIR, "recent_index", False),
        "trusted_proxies": wb_settings.panel_trusted_proxies(ACCOUNTS_DIR),
        "egress_backoff_enabled":
            wb_settings.egress_backoff_enabled(ACCOUNTS_DIR),
        "log_db_enabled": wb_settings.log_db_enabled(ACCOUNTS_DIR),
        "log_db_max_rows": wb_settings.log_db_max_rows(ACCOUNTS_DIR),
        "log_db_max_days": wb_settings.log_db_max_days(ACCOUNTS_DIR),
        "log_db_max_bytes": wb_settings.log_db_max_bytes(ACCOUNTS_DIR),
        "accounts_collapsed": wb_settings.accounts_collapsed(ACCOUNTS_DIR),
        "accounts_separate_tab": wb_settings.accounts_separate_tab(ACCOUNTS_DIR),
        "key_before_hidden": wb_settings.key_before_hidden(ACCOUNTS_DIR),
        "hidden_pages": wb_settings.hidden_pages(ACCOUNTS_DIR),
        "update_check_enabled":
            wb_settings.panel_flag(ACCOUNTS_DIR, "update_check_enabled", False),
        "update_check_interval_hours":
            wb_settings.panel_update_check_interval_hours(ACCOUNTS_DIR),
        # 被环境变量控制的项：面板标出环境变量的当前取值，控件仍可编辑。
        "env_overrides": wb_settings.env_overrides(),
        "upstream": wb_settings.upstream_config(ACCOUNTS_DIR),
        "usage": wb_settings.usage_config(ACCOUNTS_DIR),
        "prompt": wb_settings.prompt_config(ACCOUNTS_DIR),
        "accounts_dir": ACCOUNTS_DIR,
        "usage_dir": USAGE_DIR,
        "settings_file": wb_settings.settings_path(ACCOUNTS_DIR),
        "smart_routing": wb_settings.smart_routing(ACCOUNTS_DIR),
        "session_affinity": wb_settings.session_affinity(ACCOUNTS_DIR),
        "routing": (POOL.routing_snapshot() if POOL else {}),
        "messages_format": wb_settings.messages_format(ACCOUNTS_DIR),
        "messages_format_default": wb_settings.DEFAULT_MESSAGES_FORMAT,
        "test_model_intl": wb_settings.test_model(ACCOUNTS_DIR, "intl"),
        "test_model_cn": wb_settings.test_model(ACCOUNTS_DIR, "cn"),
        "test_model_intl_default": wb_settings.test_model_default(ACCOUNTS_DIR, "intl"),
        "test_model_cn_default": wb_settings.test_model_default(ACCOUNTS_DIR, "cn"),
        "test_model_is_custom": {
            "intl": wb_settings.test_model(ACCOUNTS_DIR, "intl")
                    != wb_settings.test_model_default(ACCOUNTS_DIR, "intl"),
            "cn": wb_settings.test_model(ACCOUNTS_DIR, "cn")
                  != wb_settings.test_model_default(ACCOUNTS_DIR, "cn"),
        },
        "disabled_models": wb_settings.disabled_models(ACCOUNTS_DIR),
        "version": VERSION,
    }
def current_account():
    """用于展示的账号（健康度与用量摘要）。"""
    return POOL.representative() if POOL else None
# ---------------------------------------------------------------------------
# 基于前缀的会话亲和（PATCHED-BY-OPS）
# ---------------------------------------------------------------------------
# 上游 prompt cache 是【账号级】的：只有同一个账号再次看到相同前缀才会命中。
# 实测证据（wk 实例 11 个号）：8 次完全相同的前缀请求被轮询分散到 8 个账号，
# 缓存率全部为 0%；而带上会话标识固定落到同一账号时，第 2 次起缓存率即 95.2%。
#
# sub2api / DSH 等客户端并不发送 X-Conversation-Id 之类的会话标识，
# 于是 hub 走纯轮询，同一对话每一轮都换账号，缓存必然归零。
#
# 这里在缺少显式会话键时，用【对话稳定前缀】派生亲和键：
# 取消息列表的前两条（system + 首条 user），它们在整段对话生命周期内不变，
# 因此同一对话的每一轮都会落到同一账号；而不同对话的首条 user 不同，
# 依旧会分散到各账号，负载均衡不受影响。
def affinity_debug_enabled():
    return wb_settings.affinity_debug(ACCOUNTS_DIR)


# 亲和的上限：对话超过这么多条消息后停止绑定账号。
#
# 为什么需要上限：亲和把整段对话固定在同一个账号上，而对话的上下文是单调
# 增长的，于是那一个账号要反复接收越来越大的请求体。实测（wk4 实例，
# 2026-10-08，333 个请求）请求体与上游断连率的关系：
#
#     msgs <150    82 个请求   断连率  0.0%
#     msgs 150-300 59 个请求   断连率  3.4%
#     msgs 300-400 64 个请求   断连率  7.8%
#     msgs 400-500 50 个请求   断连率 10.0%
#
# 断连（TimeoutError / RemoteDisconnected）会触发重试，把一个 8.8s 的请求
# 拖到 11.6s，首字延迟随之翻倍。超过阈值后放弃亲和，让这个对话重新参与
# 轮询：代价是它丢掉前缀缓存，收益是断连和重试消失。
#
# 阈值不能设得太低，否则会波及正常长度的对话（本实例 94% 的请求缓存命中率
# 来自亲和）。设 0 表示不限制，保持 1.6.x 的原有行为。
AFFINITY_MAX_MSGS = int(os.environ.get("WB_AFFINITY_MAX_MSGS", "400") or 0)
# 超限对话改成【页内轮转】而不是撒到整个池子。
#
# 为什么：整池轮转下每个账号要等「池子大小」轮才再见到这段对话，间隔一旦
# 超过上游缓存的存活期就整段漏。线上实测（2026-10-10，37706 条请求）：超限
# 对话占 59% 的 prompt token，却贡献了 64% 的漏掉量，签名是只命中约 24k 的
# 系统提示（账号见过同一客户端的系统提示、没见过这段历史）——典型的「落在
# 没预热过的账号上」。页内轮转把「再见到」的周期从「池子大小」轮缩到 size
# 轮，页里每个账号都留着前缀；同时超大请求体仍摊在 size 个账号上，不是压回
# 一个（那正是 400 条上限要解决的断连问题）。
#
# 取值：≥2 = 页大小（账号数）；1 = 和普通对话一样固定账号（等于取消上限）；
# 0 = 保持 1.6.x 的整池轮转。页的划分见 wb_accounts.AccountPool._page_slice。
AFFINITY_PAGE_SIZE = int(os.environ.get("WB_AFFINITY_PAGE_SIZE", "3") or 0)
def affinity_route(messages):
    """这段对话的 (session_key, page)。

    session_key 是稳定的对话键；page 是池页 (index, size)，仅消息数超过
    AFFINITY_MAX_MSGS 的对话用它做页内轮转，其余情况为 None（短对话、页开关
    关闭、客户端自带会话键的对话）。两者都来自同一个头部哈希，同一段对话每轮
    对键与页的取值一致，不需要服务端状态。

    客户端自带会话标识时用它的；没有标识就按对话前缀派生。两者共用一个开关：
    会话粘性，关掉就完全不绑定。
    """
    if not wb_settings.session_affinity(ACCOUNTS_DIR):
        return None, None
    try:
        msgs = messages or []
        if not msgs:
            return None, None
        head = msgs[:2]
        blob = json.dumps(head, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(blob).hexdigest()[:16]
        key = "pfx-" + digest
        if AFFINITY_MAX_MSGS > 0 and len(msgs) > AFFINITY_MAX_MSGS:
            if AFFINITY_PAGE_SIZE > 1:
                page = (int(digest[:8], 16) % AFFINITY_PAGE_SIZE, AFFINITY_PAGE_SIZE)
                if affinity_debug_enabled():
                    log("affinity: %d msgs (> %d), page %d/%d"
                        % (len(msgs), AFFINITY_MAX_MSGS, page[0], page[1]))
                return key, page
            if AFFINITY_PAGE_SIZE == 1:
                # 和普通对话一样固定账号，等于取消上限。
                return key, None
            if affinity_debug_enabled():
                log("affinity: skip %d msgs (> %d), letting the pool rotate"
                    % (len(msgs), AFFINITY_MAX_MSGS))
            return None, None
        return key, None
    except Exception:
        return None, None
def derive_affinity_key(messages):
    """只取对话键；池页见 affinity_route。"""
    return affinity_route(messages)[0]
def prompt_fingerprint(messages):
    """出站 prompt 的隐私安全指纹。

    缓存命中要求前缀逐字节相同，这组哈希回答「前缀是否稳定、对话是否连续」，
    不存储任何文本。
    """
    try:
        def h(obj):
            blob = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
            return hashlib.sha256(blob).hexdigest()[:12]
        msgs = messages or []
        out = {"msgs_sha": h(msgs), "n_msgs": len(msgs)}
        if msgs:
            out["system_sha"] = h(msgs[0]) if msgs[0].get("role") == "system" else ""
            out["prefix_sha"] = h(msgs[:-1]) if len(msgs) > 1 else ""
        return out
    except Exception:
        return {}

LOG_BUFFER = deque(maxlen=2000)
_LOG_LOCK = threading.Lock()
_LOG_COUNTER = 0

# 日志级别与模块的自动归类。调用点显式传了 level/tag 就用传的，没传才按内容猜。
#
# 级别：本仓大量日志是中文，中英关键字都要收，否则定价侧「抓取失败」这类中文
# 错误全落到 INFO，看板按 ERROR 筛选看不到。
_LOG_ERROR_HINTS = (
    "error", "exception", "failed", "fail:", "reject", "traceback", "errno", "fatal",
    "11-128", "11101", "11140",
    "失败", "异常", "错误", "拒绝", "不可用", "无效",
)
_LOG_WARN_HINTS = (
    "warn", "warning", "retry", "timeout", "throttl",
    "重试", "超时", "告警", "警告", "降级", "限流",
)
# 模块：顺序即优先级，越靠前越先判定。原有类别（chat/scheduler/tasks/accounts/
# catalog/auth/settings）的相对顺序是兼容约束，改动会改变既有日志的归类。
_LOG_TAG_HINTS = (
    ("chat", ("chat:", "chat done", "request error", "/v1/chat",
              "/chat/completions", "responses")),
    ("scheduler", ("scheduler", "调度器")),
    ("tasks", ("task", "任务", "打卡", "签到", "猫猫", "travel", "growth")),
    ("accounts", ("account", "账号", "pool", "imported")),
    ("pricing", ("pricing", "定价", "取价", "价格", "price", "抓取")),
    ("egress", ("egress", "出口", "槽位", "proxy slot", "mihomo")),
    ("upstream", ("upstream", "上游")),
    ("webtools", ("webtool", "web tool", "本地网络工具")),
    ("catalog", ("model", "catalog", "模型")),
    ("auth", ("auth", "token", "oauth")),
    ("settings", ("settings", "设置")),
    ("update", ("更新", "release")),
    ("usage", ("usage", "用量")),
    ("agents", ("agents", "智能体")),
)

# 可选的 SQLite 日志库（默认关闭）。None = 还没初始化（模块 import 期），启动时按
# settings 建好，之后由面板开关驱动 wb_logstore.LogStore.configure()。关闭时它
# 要么是 None、要么 enabled=False，整条日志路径与上游一字不差：只写内存环形
# 缓冲，不建库、不落盘。
LOG_STORE = None


def _log_db_active():
    return LOG_STORE is not None and LOG_STORE.enabled


def add_log_entry(msg, level=None, tag=None):
    global _LOG_COUNTER
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    t_short = time.strftime("%H:%M:%S")
    msg_str = str(msg).rstrip()
    if not level:
        lower = msg_str.lower()
        if any(k in lower for k in _LOG_ERROR_HINTS):
            level = "ERROR"
        elif any(k in lower for k in _LOG_WARN_HINTS):
            level = "WARN"
        else:
            level = "INFO"
    if not tag:
        lower = msg_str.lower()
        for candidate, hints in _LOG_TAG_HINTS:
            if any(k in lower for k in hints):
                tag = candidate
                break
        else:
            tag = "system"
    with _LOG_LOCK:
        _LOG_COUNTER += 1
        entry = {
            "id": _LOG_COUNTER,
            "ts": ts,
            "time": t_short,
            "level": level,
            "tag": tag,
            "msg": msg_str,
        }
        LOG_BUFFER.append(entry)
    # 写数据库在锁外：append 只是入队，但没必要把持锁时间拉长到调用 queue.put。
    # 入队失败（线程已停、队列坏了）只丢这一行日志，不把异常抛给调用方。
    if _log_db_active():
        try:
            LOG_STORE.append(entry)
        except Exception:
            pass
    return entry

def log(msg, level=None, tag=None):
    # 启动它的终端被回收后，stderr 的读端消失，写入会抛 OSError。异常发生在
    # 响应头之前时整条请求会空连接关闭，日志坏了最多丢一行，不能打死请求。
    try:
        sys.stderr.write(f"[wb-proxy] {time.strftime('%H:%M:%S')} {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass
    try:
        add_log_entry(msg, level=level, tag=tag)
    except Exception:
        pass


# 本地网络工具的多引擎轮换、冷却与失败分类写进看板日志（tag=webtools）
wb_webtools.set_logger(log)

def get_logs(limit=200, level="", tag="", search="", since_id=0):
    # 写入数据库开启时以数据库为准：它是完整历史（受保留上限约束），而内存环形缓冲
    # 只留最新 2000 条，拿它回答会看不到刚过去的窗口。查询失败或超时就退回内存，
    # 面板宁可少看历史，也不能因为日志库出问题而整页空白。
    if _log_db_active():
        try:
            stored = LOG_STORE.query(limit=limit, level=level, tag=tag,
                                     search=search, since_id=since_id)
        except Exception:
            stored = None
        if stored is not None:
            return stored
    with _LOG_LOCK:
        items = list(LOG_BUFFER)
    if since_id > 0:
        items = [x for x in items if x["id"] > since_id]
    if level:
        items = [x for x in items if x["level"] == level.upper()]
    if tag:
        items = [x for x in items if x["tag"].lower() == tag.lower()]
    if search:
        s = search.lower()
        items = [x for x in items if s in x["msg"].lower() or s in x["tag"].lower()]
    total = len(items)
    if limit and limit > 0 and since_id == 0:
        items = items[-limit:]
    max_id = items[-1]["id"] if items else since_id
    return {"total": total, "logs": items, "max_id": max_id}

def clear_logs():
    with _LOG_LOCK:
        LOG_BUFFER.clear()
    if _log_db_active():
        try:
            LOG_STORE.clear()
        except Exception:
            pass


def apply_log_db_settings():
    """把面板的日志库设置读进运行中的 store。

    启动时调用一次（USAGE_DIR 确定之后），每次保存设置再调一次，改开关或上限
    立即生效，无需重启。同时把 id 计数器抬到数据库已有的最大值，重启后 id 仍
    单调递增，面板的 since_id 增量拉取继续可用。
    """
    global _LOG_COUNTER
    if LOG_STORE is None:
        return False
    LOG_STORE.configure(
        wb_settings.log_db_enabled(ACCOUNTS_DIR),
        max_rows=wb_settings.log_db_max_rows(ACCOUNTS_DIR),
        max_days=wb_settings.log_db_max_days(ACCOUNTS_DIR),
        max_bytes=wb_settings.log_db_max_bytes(ACCOUNTS_DIR),
    )
    if LOG_STORE.enabled:
        try:
            highest = LOG_STORE.max_id()
        except Exception:
            highest = 0
        with _LOG_LOCK:
            if highest > _LOG_COUNTER:
                _LOG_COUNTER = highest
    return LOG_STORE.enabled

# ---------------------------------------------------------------------------
# 模型封锁表
# ---------------------------------------------------------------------------
# 客户端除用户对话外还会自己发背景请求（记忆整理、自动复核等），这些请求不经过
# 模型菜单，直接用目录上的模型 ID，可能在用户没有操作时消耗付费额度。把要拒绝的
# 模型填进 ALLOWED_MODELS / BANNED_SUBSTRING / EXTRA_BANNED，命中的请求在本机
# 直接回 400，完全不碰上游；默认全空即不封锁任何模型。
#
#   要放行某个模型 -> 加进 ALLOWED_MODELS 或 ALLOWED_PREFIXES
#   要连非 gpt 的模型一起挡 -> 加进 EXTRA_BANNED
# ---------------------------------------------------------------------------

# 允许放行的模型（你要用的）
ALLOWED_MODELS = {
    # 默认不封锁任何模型；填入模型 id 即可只放行这些
}

# 允许前缀：涵盖 -high / -preview / [1M] 等变体
ALLOWED_PREFIXES = ()

# 封锁字符串：模型名里含这个就拒绝
BANNED_SUBSTRING = ""

# 额外封锁的内部模型（不在 gpt- 前缀内，但也会烧点）
EXTRA_BANNED = set()


def is_model_banned(model):
    """True 表示这个模型名不该被送去上游。

    规则：ALLOWED_MODELS / ALLOWED_PREFIXES 命中就放行；其余只要命中
    BANNED_SUBSTRING 或 EXTRA_BANNED 就拒绝，没命中则照常送往上游。
    三个设置默认都是空的，所以默认不封锁任何模型。
    """
    if not model:
        return False
    m = str(model).strip().lower()
    if m in ALLOWED_MODELS:
        return False
    if any(m.startswith(a) for a in ALLOWED_PREFIXES):
        return False
    if BANNED_SUBSTRING and BANNED_SUBSTRING in m:
        return True
    if m in EXTRA_BANNED:
        return True
    return False


# ---------------------------------------------------------------------------
# 背景请求拦截
#
# Codex App 除了用户的对话，还会自己发背景请求（记忆整理、环境建议、自动复核）。
# 这些请求不经过模型菜单，所以单靠模型白名单挡不住 —— 它们可能直接用目录上
# 的付费模型（例如 gpt-6-astra 这类），在用户没有实际操作时照样消耗额度。
#
# Codex 会在 client_metadata 里带 x-codex-turn-metadata，内容像：
#   {"request_kind":"memory","thread_source":"memory_consolidation",
#    "turn_trigger":"memory_consolidation"}
# 这里就靠这个标记判断：命中背景关键字 -> 本地直接拒绝，不碰上游、不扣点。
# ---------------------------------------------------------------------------

# 要不要拦截背景请求（False = 全部放行）
BLOCK_BACKGROUND_REQUESTS = False

# 命中任一关键字就视为背景请求（不分大小写、子字符串比对）
BACKGROUND_TRIGGER_KEYWORDS = (
    "memory_consolidation",
    "memory-write",
    "memory_write",
    "memorywriting",
    "ambient",
    "suggestion",
    "auto_review",
    "auto-review",
    "autoreview",
    "title",
    "compaction",
    "compact",
    "summariz",
)


# 客户端自发任务的 thread_source 列表。客户端触发的压缩请求带这里的 source，
# 操作者按「压缩上下文」时带用户自己的线程；因此必须先读 source 再查下面的
# 关键字表："compaction" 两边都命中，顺序反了会把「压缩上下文」按钮拒掉。
BACKGROUND_THREAD_SOURCES = (
    "memory_consolidation",
    "memory",
    "ambient",
    "suggestion",
    "auto_review",
    "autoreview",
    "title",
)


def turn_metadata_fields(payload):
    """把我们能拿到的 request_kind / turn_trigger / thread_source 提示拍平。

    Codex 有时把它们作为 client_metadata 的普通键发送，有时塞进它自己的 metadata
    键下的 JSON 字符串，两种形状都读。都没有时返回 {}。
    """
    if not isinstance(payload, dict):
        return {}
    meta = payload.get("client_metadata")
    if not isinstance(meta, dict):
        return {}

    fields = {}
    for key, value in meta.items():
        if isinstance(value, str) and value.strip().startswith("{"):
            try:
                inner = json.loads(value)
            except Exception:
                inner = None
            if isinstance(inner, dict):
                for k in ("request_kind", "turn_trigger", "thread_source", "kind", "trigger", "source"):
                    if k in inner:
                        fields[k] = inner[k]
        if key in ("request_kind", "turn_trigger", "thread_source"):
            fields[key] = value
    return fields


def is_compaction_request(payload):
    """操作者主动发的「压缩上下文」请求返回 True。

    request_kind=compaction 与背景请求共用同一个词，但这一条是用户自己要的：客户端
    在用户的会话线程上发送它，客户端自发的压缩则写明发起该任务的名字。拒掉它等于
    拿走上下文压缩按钮。
    """
    fields = turn_metadata_fields(payload)
    kind = str(fields.get("request_kind") or "").strip().lower()
    if "compact" not in kind:
        return False
    source = str(fields.get("thread_source") or "").strip().lower()
    return source not in BACKGROUND_THREAD_SOURCES


def background_request_reason(payload):
    """若这是 Codex 自己发的背景请求，返回说明字符串；否则返回 ""。

    只看 client_metadata，不碰消息内容。
    """
    fields = turn_metadata_fields(payload)
    if not fields:
        return ""

    if is_compaction_request(payload):
        return ""

    blob = " ".join(str(v) for v in fields.values()).lower()
    for kw in BACKGROUND_TRIGGER_KEYWORDS:
        if kw in blob:
            return "%s=%s" % (
                ",".join(sorted(fields.keys())),
                ",".join(str(fields[k]) for k in sorted(fields)),
            )
    return ""


def client_effort_of(body):
    """请求自己带的思考档位，两种客户端拼写都认。

    build_upstream_body() 同时读 "reasoning_effort" 与 "reasoningEffort"，且只对
    需要注入的模型补默认值——camelCase 的请求保留 camelCase 键，打到目录固定档位
    模型的请求则完全不带档位。
    """
    if not isinstance(body, dict):
        return None
    return body.get("reasoning_effort") or body.get("reasoningEffort")


def upstream_effort_of(body, model=None):
    """请求实际以哪个思考档位运行，未知时为 None。

    按上游的应用顺序解析：
      - 目录把模型固定在某一档（reasoning.effort，无 supportedEfforts）时永远跑
        那一档，选择器不给选项，请求里带的值改不了结果；
      - 请求关掉了思考、或要的是 "none"，就没有思考过程，下面几条也不覆盖；
      - 否则以客户端自己的值为准，两种拼写都认；
      - 再否则用模型声明的 defaultEffort。

    后两条只看请求体不够：网关只对需要注入的模型写 effort，普通请求打到固定档模型
    时会被误报成「没有档位」。
    """
    given = client_effort_of(body)
    if model:
        fixed = model_fixed_effort(model)
        if fixed:
            return fixed
    thinking = (body or {}).get("thinking") if isinstance(body, dict) else None
    if isinstance(thinking, dict) and \
            str(thinking.get("type") or "").strip().lower() == "disabled":
        return "none"
    if str(given or "").strip().lower() == "none":
        return "none"
    if given:
        return given
    return model_default_effort(model) if model else None


def background_request_message(reason):
    return ("這是客戶端自己發的背景請求（%s），本機代理已擋下，"
            "避免在沒有實際操作時消耗上游額度。"
            "要放行請把 wb_proxy.py 的 BLOCK_BACKGROUND_REQUESTS 改成 False。"
            % reason)


def banned_model_message(model):
    allowed = "、".join(sorted(ALLOWED_MODELS))
    return ("模型 %s 已被本機代理封鎖（依 ALLOWED_MODELS / BANNED_SUBSTRING 設定）。"
            "目前允許：%s。要放行請編輯 wb_proxy.py 的 ALLOWED_MODELS。"
            % (model, allowed))


def disabled_model_message(model):
    return ("模型 %s 已在看板的模型列表里取消勾选，本网关不处理这个模型 id 的请求。"
            "要恢复请打开看板「网关与运维」页，在模型列表里重新勾上 %s。"
            % (model, model))


def key_model_message(entry, model):
    """按 key 的模型限制生成说明，口径与全局封锁一致。"""
    name = (entry or {}).get("name") or "未命名"
    allowed = "、".join((entry or {}).get("models") or []) or "-"
    asked = str(model or "").strip() or "(未指定模型)"
    return ("API Key「%s」的模型限制不允許 %s。該 Key 目前允許：%s。"
            "請在看板「設置」頁修改這個 Key 的模型限制，或改用允許該模型的 Key。"
            % (name, asked, allowed))


# 进程内的降级窗口：passthrough/append 模式被内容拦截的流量改用最小中性提示，
# 直到下一个 00:00 CST（面板 degrade.go）。重启会清掉窗口，可以接受：下次被拒
# 会重新触发。
PROMPT_DEGRADE = wb_prompt.DegradeGate()


def prompt_mode_config():
    """prompt 模式设置；任何读取错误都按 passthrough 放行。"""
    try:
        cfg = wb_settings.prompt_config(ACCOUNTS_DIR)
    except Exception:
        cfg = None
    if not isinstance(cfg, dict):
        cfg = {"mode": "passthrough", "file": ""}
    return cfg


def apply_prompt_mode(messages):
    """把配置的 prompt 模式应用到一次请求的 messages 上。

    passthrough 是默认行为（客户端 system 提示原样透传），custom/append 需要显式
    开启。降级窗口生效期间，passthrough 与 append 改用最小化的中性提示，custom
    不降级。prompt 文件读不出来时按 passthrough 放行，不阻塞请求。
    """
    cfg = prompt_mode_config()
    mode = cfg.get("mode") or "passthrough"
    degraded = PROMPT_DEGRADE.active()
    if mode in ("custom", "append"):
        try:
            text = wb_prompt.load_prompt(mode, cfg.get("file"))
        except Exception:
            text = ""
        if text:
            return wb_prompt.apply_mode(messages, mode, text, degraded=degraded)
        return messages
    if degraded:
        return wb_prompt.apply_mode(messages, mode, "", degraded=True)
    return messages


def build_upstream_body(payload, dsh_request=None):
    model = payload.get("model") or ""
    if dsh_request is None:
        dsh_request = payload.get("_dsh_request")
    if dsh_request is None:
        dsh_request = bool(payload.get("tools")) and is_dsh_payload(payload)
    # 先解析出实际生效的 thinking 状态再做下面的 backfill：thinking 开着时，
    # 上游要求每条 assistant 消息都带 reasoning_content，无论客户端有没有
    # 保留 reasoning 轨迹。
    thinking = payload.get("thinking")
    thinking_type = ""
    if isinstance(thinking, dict):
        thinking_type = str(thinking.get("type") or "").strip().lower()
    effort = payload.get("reasoning_effort") or payload.get("reasoningEffort")
    thinking_enabled = False
    if str(model).lower().startswith("deepseek"):
        if thinking_type == "enabled":
            thinking_enabled = True
        elif thinking_type != "disabled" and str(effort or "").strip().lower() != "none":
            thinking_enabled = True
    messages = normalize_roles(payload.get("messages") or [])
    # prompt 模式在 sanitize/backfill 之前执行：上游看到的是网关的 prompt，
    # 客户端带指纹特征的 system 文本到不了上游。
    messages = apply_prompt_mode(messages)
    messages = sanitize_messages(messages)
    messages = backfill_reasoning_content(
        messages, model, thinking_enabled=thinking_enabled
    )
    if not messages or (messages[0].get("role") != "system"):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    body = dict(payload)
    # Responses 路径在 chat body 上捎带私有请求标记（命名空间映射、
    # local-web-tools 标志）。它们不属于上游协议，在这里删掉，不指望上游
    # 忽略未知字段。
    for _marker in [k for k in body if str(k).startswith("_")]:
        body.pop(_marker, None)
    # dict(payload) 会把原始模型名一起带过去，所以别名要在这里覆盖回去
    body["model"] = model
    body["messages"] = messages
    # 请求发出前修复 tool-call 配对：结果没回来的调用、被插入消息拆散的
    # 结果批次、被拆成相邻 assistant 消息的并行批次，都会让上游拒绝该会话
    # 后续所有轮次。顺序不能变：先合并被拆开的声明，repack 才能看到一个完整
    # 批次。
    repaired, _merged = merge_adjacent_tool_calls(body["messages"])
    repaired, _repacked = repack_tool_result_blocks(repaired)
    repaired, _cleaned = cleanup_orphan_tool_calls(repaired)
    body["messages"] = repaired
    translate_max_completion_tokens(body)
    # 上游 GPT 系模型要求 max_output_tokens >= 16；更小的值（探测用的
    # max_tokens=1）报 400 integer_below_min_value（#364、#252）。
    if "max_tokens" in body and str(model).lower().startswith("gpt-"):
        try:
            mt = int(body["max_tokens"])
            if mt < 16:
                log("clamping %s max_tokens from %d to 16 for upstream requirements" % (model, mt))
                body["max_tokens"] = 16
        except (TypeError, ValueError):
            pass
    normalize_tool_choice(body)
    normalize_tools(body)
    if body.get("tools") and dsh_request and _dsh_tool_neutralization_enabled():
        neutralize_dsh_tool_defs(body)
    normalize_stop(body)
    if "max_tokens" not in body:
        default_max = model_default_max_output_tokens(model)
        if default_max:
            body["max_tokens"] = default_max
    # DeepSeek 模型的 thinking 注入。
    #
    # 只开 thinking.type=enabled 并不会打开 reasoning trace：不带 effort 时
    # 上游照样不给 reasoning。线上实测（deepseek-v4.1-flash，同一 prompt）：
    #   enabled + 无 effort      -> reasoning_tokens 0,  reasoning_content len 0
    #   reasoning_effort=high    -> reasoning_tokens 37, reasoning_content len 117
    # 客户端自己的选择永远优先；只有请求没填 effort 时才补默认档，显式关掉
    # thinking 的请求永不注入。
    if str(model).lower().startswith("deepseek"):
        thinking = body.get("thinking")
        opted_out = isinstance(thinking, dict) and \
            str(thinking.get("type") or "").strip().lower() == "disabled"
        effort = body.get("reasoning_effort") or body.get("reasoningEffort")
        if not opted_out and str(effort or "").strip().lower() != "none":
            if "thinking" not in body:
                body["thinking"] = {"type": "enabled"}
            if not effort:
                body["reasoning_effort"] = model_default_effort(model) or "high"
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body


def model_catalog_meta(model):
    """模型的目录元数据字典，取不到时返回 {}。

    读取与 /v1/models 相同的合并目录。刻意无副作用：只读已填充的模型缓存与
    随包静态表。这里调用 fetch_models() 会让冷缓存在请求处理中触发一次上游
    发现请求，把一次 chat 调用变成一次网络抓取。
    """
    if not model:
        return {}
    try:
        realm = detect_model_realm(model) or CURRENT_REALM
        entries = (_models_cache.get(realm) or {}).get("data")
        if not entries:
            name = "STATIC_CN_MODELS" if realm == "cn" else "STATIC_INTL_MODELS"
            table = getattr(wb_catalog, name, None) or wb_catalog.STATIC_MODELS
            entries = [(m.get("id"), m) for m in table if isinstance(m, dict)]
        m_lower = str(model).strip().lower()
        for mid, meta in entries:
            if str(mid).strip().lower() == m_lower:
                return meta or {}
    except Exception as exc:
        log("catalog meta lookup failed for '%s': %s" % (model, exc))
    return {}


def model_reasoning_meta(model):
    """目录里模型的 reasoning 块，取不到时返回 {}。"""
    return model_catalog_meta(model).get("reasoning") or {}


def model_default_max_output_tokens(model):
    """目录声明的模型最大输出 token 数，未声明时返回 None。"""
    val = model_catalog_meta(model).get("maxOutputTokens")
    if val is not None:
        try:
            val = int(val)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return None


def model_default_effort(model):
    """客户端没指定档位时目录给的默认档位，没有时返回 None。"""
    effort = model_reasoning_meta(model).get("defaultEffort")
    return effort.strip() if isinstance(effort, str) and effort.strip() else None


def model_fixed_effort(model):
    """目录把模型固定死的档位；档位可选时返回 None。

    reasoning.effort 有值而没有 supportedEfforts 表示模型永远跑这一档：
    /v1/models 把它标成 reasoning_fixed_effort，选择器不给选项，请求自带的
    档位也改不了结果。声明了 supportedEfforts 的模型档位可选，块里同级的
    effort 只是默认值，不固定档位。
    """
    reasoning = model_reasoning_meta(model)
    if reasoning.get("supportedEfforts"):
        return None
    effort = reasoning.get("effort")
    return effort.strip() if isinstance(effort, str) and effort.strip() else None


def prompt_cache_key_enabled():
    """是否往出站请求注入 prompt_cache_key。

    默认关闭。实测上游本身就缓存重复前缀：约 8k token 的相同前缀发两次，第二次
    已报 prompt_cache_hit_tokens=9600，扣费与不带该字段相同；两个出口
    （www.workbuddy.ai 与 www.workbuddy.cn）、免费与计费模型上都如此。注入它
    既不改变命中率也不改变扣费，因此只留作实验开关，不加进每个请求。
    """
    return wb_settings.prompt_cache_key(ACCOUNTS_DIR)


def inject_prompt_cache_key(body, uid, conversation):
    """加上游 prompt_cache_key，复用它的前缀缓存。

    只留作实验：实测上游没有这个字段也能缓存前缀（见 prompt_cache_key_enabled）。
    键里必须带账号 uid：上游缓存按账号隔离，账号间共用一个键会让一个账号的请求
    读到另一个账号的缓存前缀，绝不能改成固定字符串。

    优先级跟随客户端意图：请求显式带的 prompt_cache_key 永不覆盖；其次用 body
    自己的 conversation id；再其次用网关解析出的会话键（请求头或对话前缀派生）。
    """
    if not isinstance(body, dict):
        return body
    existing = body.get("prompt_cache_key")
    if isinstance(existing, str) and existing.strip():
        return body
    conv = conversation if isinstance(conversation, str) else ""
    for field in ("conversation_id", "conversationId"):
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            conv = value.strip()
            break
    uid8 = (uid or "")[:8] or "-"
    seed = "%s|%s" % (uid or "", conv)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    out = dict(body)
    out["prompt_cache_key"] = "wb2a-%s-%s" % (uid8, digest)
    return out
class ContentRejected(Exception):
    """上游内容审核拒绝了本次请求（403 / code 11140）。

    与账号无关：同一内容换别的凭据还是同样的 403，因此原样透传给客户端，
    不冷却账号池。
    """

    def __init__(self, http_error=None, detail=""):
        self.http_error = http_error
        self.detail = detail or ""
        super().__init__("upstream rejected the request content (403)")

    @property
    def code(self):
        return 403


class GatewayStandDown(Exception):
    """账号被上游限制（403），网关停止对外服务。

    全部账号一起停用：限制发生后继续轮询下一个账号正是把整个账号池关联起来的
    原因。操作者在看板重新启用账号之前，任何请求都不发往上游。
    """

    def __init__(self, detail="", account_uid=None):
        self.detail = detail or ""
        self.account_uid = account_uid
        super().__init__("gateway stopped: an account was restricted upstream (403)")


class RateLimited(Exception):
    """上游限流了该模型（429 / code 6004）。

    与账号池全挂不同：凭据本身正常，只是这个模型在冷却。"""

    def __init__(self, http_error=None, detail="", wait=60, message=""):
        self.http_error = http_error
        self.detail = detail or ""
        self.wait = max(1, int(wait or 60))
        # 未经上游调用就回答的 429（日 token 守卫停住了池子）自带文案，
        # 不带上游原文。
        self.message = message or ""
        super().__init__("upstream rate limit: %s" % (self.detail[:200] or "429"))


def gateway_hint(status, message):
    """附在上游原文旁边的网关提示。

    提示永不替换上游措辞（客户端仍按原封包解析），只补充网关能识别的分类，让
    看到 "400 Bad Request" 的用户知道该缩小上下文、换模型、重新登录还是等待。
    返回空串表示无可补充：不对未知故障编造提示。

    覆盖网关能从上游正文/状态识别的形状：11133 model_param_invalid、
    11135 invalid_image_data、限流、内容审核、积分耗尽、会话失效、模型不存在、
    prompt 超长拒绝、WAF 拦截与本地没有可用账号。
    """
    text = str(message or "")
    lower = text.lower()
    if status == 413 and "payload too large" in lower:
        return ("single request body exceeds the gateway cap "
                "(WB_MAX_PAYLOAD_BYTES, default 16MB); raise it and restart wb-proxy")
    if status == 503 and "concurrent chat limit" in lower:
        return "gateway is busy at its concurrency limit; retry shortly"
    if ("11133" in lower or "model_param_invalid" in lower
            or "invalid request parameters" in lower):
        return ("request parameters were rejected by the model provider; "
                "check message format and model capabilities")
    if ("11135" in lower or "invalid_image_data" in lower
            or "replace the image" in lower):
        return ("image data rejected by upstream; use a real/valid image, "
                "may need a new conversation")
    if "no usable account" in lower or "no healthy account" in lower:
        return "no healthy account available in pool; check /status or retry later"
    if "本机网络不可用" in text or "local network unavailable" in lower:
        return ("this machine has no working network (DNS/route failed); accounts were "
                "not penalised - check the host's network or proxy")
    if ("context length" in lower or "context_length" in lower
            or "prompt too long" in lower or "too many tokens" in lower
            or "maximum context" in lower):
        return ("request context exceeds the model's limit; reduce "
                "history/message size")
    if status == 429 or "rate limit" in lower or "frequency limit" in lower:
        return "rate limited by upstream; retry after reset"
    if status == 402 or ("insufficient" in lower and "credit" in lower):
        return ("account credits exhausted at upstream; waiting for daily "
                "check-in to restore")
    if "session" in lower and ("not found" in lower or "expired" in lower):
        return ("account session expired at upstream; the account is disabled "
                "until re-login")
    if "content" in lower and ("reject" in lower or "policy" in lower):
        return ("request content was rejected by content policy; adjust the "
                "prompt and retry")
    if ("no such model" in lower or "model not found" in lower
            or "unsupported model" in lower):
        return ("upstream has no such model on this backend; switch model or "
                "retry on another account")
    if status == 403 and "waf" in lower:
        return "upstream WAF blocked the gateway; retry after the block window"
    return ""


# ---------------------------------------------------------------------------
# 出站身份自动切换
#
# 官方有三套身份（workbuddy / vscode / cli），端点与配额通道各不相同，
# 对照表见 wb_identity._ENDPOINTS。
#
# 某模型在某条通道被限流（429 / code 6004）时，换成另一套身份通常还能继续
# 用 —— 那是另一条配额线。每轮最多切 MAX_PRODUCT_SWITCHES 次，避免来回弹跳。
#
# 身份会写进凭证档并在重启后读回（issue #76）：面板手动切换当下就写盘，
# 这里的自动切换则在下一次任何 save() 时一并写入。
#
# 这个开关交给面板设置决定（issue #67），默认关闭：自动切换会消耗重试预算，
# 也会把账号留在操作者没主动选过的身份上，需要时再手动打开。
# ---------------------------------------------------------------------------

MAX_PRODUCT_SWITCHES = 4
_SWITCH_LOG = {}

#: 出口级退避在单次请求里最多睡这么久（见 wb_egress）：退避阶梯可以到十几秒，
#: 但把一个客户端请求按住十几秒是不可接受的，所以只睡这个上限；共享计数器仍然
#: 让并发请求一起变慢（这才是「一次短暂的全局退避」）。
EGRESS_SLEEP_MAX = 5.0


def auto_switch_product_enabled():
    """429 时是否允许轮换出站身份（面板设置，默认关闭）。"""
    return wb_settings.auto_switch_product(ACCOUNTS_DIR)


def _switch_count(account, model):
    """这一轮已经切过几次（60 秒内的切换算同一轮）。"""
    entry = _SWITCH_LOG.get((account.uid, model))
    if not entry:
        return 0
    count, last = entry
    if time.time() - last > 60:
        return 0
    return count


def _try_switch_product(account, model):
    """429 时换身份重试。返回 True 表示已切换、可以重试。

    同一请求内最多切 MAX_PRODUCT_SWITCHES 次：
      cli -> workbuddy -> cli -> workbuddy
    四次都不行就放弃，让调用方返回真正的 429。
    """
    count = _switch_count(account, model)
    if count >= MAX_PRODUCT_SWITCHES:
        return False
    current = getattr(account, "product", wb_identity.PRODUCT_DESKTOP)
    if current == wb_identity.PRODUCT_DESKTOP:
        target = wb_identity.PRODUCT_VSCODE
    elif current == wb_identity.PRODUCT_VSCODE:
        target = wb_identity.PRODUCT_CLI
    else:
        target = wb_identity.PRODUCT_DESKTOP
    try:
        changed = account.set_product(target)
    except Exception as exc:
        log("product switch failed: %s" % exc, level="WARN")
        return False
    if not changed:
        return False
    _SWITCH_LOG[(account.uid, model)] = (count + 1, time.time())
    if len(_SWITCH_LOG) > 500:
        _SWITCH_LOG.clear()
    log("account %s: %s 被限流，自动切换身份 -> %s (第 %d/%d 次)"
        % (account.uid[:8], current, target, count + 1, MAX_PRODUCT_SWITCHES),
        level="WARN")
    return True


def reset_switch_counter(account, model):
    """成功之后归零，下一次请求重新享有 4 次切换额度。"""
    _SWITCH_LOG.pop((account.uid, model), None)


def retry_after_seconds(model, realm):
    """该区域任何账号能再次服务 model 的最短等待秒数。"""
    if not POOL:
        return 60
    waits = [a.throttle_wait(model=model) for a in POOL.accounts
             if a.realm == realm and a.enabled and a.access_token]
    active = [w for w in waits if w > 0]
    return int(min(active)) if active else 60


def realm_model_throttled(realm, model):
    """账号存在且健康、但都在冷却该模型时返回 True。

    只认模型级冷却。普通账号冷却多半来自瞬时网络错误，把它报成限流会让客户端
    对一个从未被限流的模型退避。
    """
    if not POOL:
        return (False, 0)
    existing = [a for a in POOL.accounts
                if a.realm == realm and a.enabled and a.access_token]
    if not existing:
        return (False, 0)
    waits = []
    for a in existing:
        wait = getattr(a, "model_cooldowns", {}).get(model, 0.0) - time.time()
        waits.append(max(0.0, wait))
    if waits and all(w > 0 for w in waits):
        return (True, int(min(waits)))
    return (False, 0)


def is_transient(exc):
    """网络层面的抖动：应当重试，不该冷却账号。

    上游偶发在流中途掐断 TLS 握手（SSL: UNEXPECTED_EOF_WHILE_READING /
    Remote end closed connection）。把它当成账号故障会把唯一的 intl 账号停用
    60 秒，一次抖动变成 502 风暴。
    """
    t = ("%s %s" % (type(exc).__name__, exc)).lower()
    markers = (
        "ssl", "unexpected_eof", "eof occurred", "remote end closed",
        "connection reset", "connection aborted", "connectionreseterror",
        "connectionabortederror", "timed out", "timeout", "temporarily unavailable",
        "bad gateway", "502", "503", "504", "incompleteread",
    )
    return any(m in t for m in markers)


# 上游正文表明「模型或服务不可用」而不是凭据问题：11134 模型提供方暂不可用、
# 11102 服务信息不存在、11150 思考档位不支持、11158 模型不可用、11129 函数
# 参数非法。命中时不该让这个账号背故障，否则一个坏模型会熔断整个账号池。
MODEL_SCOPED_ERROR_CODES = frozenset((11102, 11129, 11134, 11150, 11158))


def upstream_error_code(detail):
    """上游错误正文里的数字 code，取不到时返回 None。"""
    try:
        payload = json.loads(detail or "")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload.get("code"))
    except (TypeError, ValueError):
        return None


def http_error_detail(exc, limit=600):
    """读一次 HTTPError 正文并把字节留在异常上，后续读取仍能拿到。"""
    try:
        raw = exc.read(limit)
    except Exception:
        return ""
    try:
        exc.wb_body = raw
    except Exception:
        pass
    return raw.decode("utf-8", "replace")


def read_http_error(exc, limit=600):
    """取 http_error_detail 读到的字节；其他异常走普通读取。"""
    raw = getattr(exc, "wb_body", None)
    if raw is not None:
        return raw[:limit]
    try:
        return exc.read(limit)
    except Exception:
        return b""


def model_scoped_errors_penalize_account():
    """兼容开关：恢复「任何 5xx 都记在账号上」的旧行为。"""
    raw = os.environ.get("WB_MODEL_SCOPED_ERRORS_PENALIZE", "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


#: 401 正文里指向凭据本身的说法。中间层 nginx/openresty 的 HTML 错误页不算。
_AUTH_FAILURE_MARKERS = ("401", "unauthorized", "authorization failed",
                         "invalid_token", "invalid token", "token expired",
                         "token invalid")


def auth_failure_text(text):
    """401 是否指向凭据本身，而不是中间代理返回的 HTML 错误页。

    HTML 401 说明链路中间有别的服务在拒绝，令牌可能完全正常；据此冷却账号
    会把健康凭证锁到手动重新登录。
    """
    low = str(text or "").lower()
    if "<html" in low or "<!doctype" in low:
        return False
    return any(marker in low for marker in _AUTH_FAILURE_MARKERS)


# 「本机没网」的指纹：DNS 解析不出来、路由不可达、网卡没起来。这些失败里账号
# 连一个字节都没发出去，结局却和「上游挂了」长得一样（都是 URLError），所以必须
# 单独认出来。
LOCAL_NETWORK_MARKERS = (
    "gaierror", "getaddrinfo failed", "name or service not known",
    "nodename nor servname", "temporary failure in name resolution",
    "network is unreachable", "network is down", "no route to host",
    "errno -2", "errno -3", "errno -5", "errno 100", "errno 101", "errno 113",
    "winerror 11001", "winerror 10050", "winerror 10051", "winerror 10065",
)

def looks_like_local_network(exc):
    """这次失败是「本机没网」，还是「上游出问题」。

    两者在账号治理上必须分开：本机断网时每一次尝试都会失败，若照旧逐账号记一笔
    并把账号冷却 60 秒，等网络恢复时池子里已经全是冷却中的账号——生产上就是这么
    把 9 个能用的账号全关掉的，用户看到的是「明明测了能用，它还熔断」。
    """
    if isinstance(exc, socket.gaierror):
        return True
    reason = getattr(exc, "reason", None)
    text = "%s %s %s" % (type(exc).__name__, exc, "" if reason is None else reason)
    lower = text.lower()
    return any(marker in lower for marker in LOCAL_NETWORK_MARKERS)

class LocalNetworkError(Exception):
    """本机网络不通：不是账号的问题，也不是上游的问题。

    单独成一个类型，是因为调用方需要给出不同的建议（去查网线/Wi-Fi/代理，而不是
    去查账号或等上游），也因为它绝不该给账号记失败。
    """

    def __init__(self, detail=""):
        self.detail = detail or ""
        # 中英并列：日志里 grep「本机网络不可用」或 "local network unavailable" 都能
        # 命中，而 str(exc) 落到用户可见处（比如上游报错信封）时也是人话。
        super().__init__("本机网络不可用 (local network unavailable): %s"
                         % (self.detail[:200] or "DNS/路由失败"))

def exc_detail(exc, limit=180):
    """异常的可读原因：类型 + 底层文本，不能只有类型名。

    只有类型名时现场分不清 SSL 握手中断、连接被重置还是读超时，而这三者指向
    完全不同的根因（上游边缘在丢连接 / 连接被重置 / 连接池复用了已死的连接）。
    URLError 的真实原因挂在 `.reason` 上（`str(exc)` 会包一层 `<urlopen error
    ...>`，`.reason` 才是干净的内层文本），优先取 `.reason`，空则退回
    `str(exc)`，再空则退回类型名，保证这一格永远有内容。
    """
    reason = getattr(exc, "reason", None)
    text = str(reason) if reason is not None else str(exc)
    text = (text or "").strip()
    if not text:
        return type(exc).__name__
    return ("%s: %s" % (type(exc).__name__, text))[:limit]

# 上游限流的几种正文形状。命中即软限；正文本身不含作用域，调用方据重置时钟
# （单模型）、凭据措辞（账号级）判定，两者都没有时依据重复出现，且只作用于
# 单模型。
SOFT_LIMIT_MARKERS = ("usage exceeds frequency limit", "too many requests",
                      "请求过于频繁")
SOFT_LIMIT_CODES = (14003,)     # intl，实测出现在真实 429 正文

# 把 429 界定为账号级的说法。按整词或显式短语匹配，不做裸子串匹配：429 正文
# 常带 "accountId"、"account_id"、"credentialId" 这类元数据字段名，子串匹配
# 会把字段名误读成作用域声明，给凭据套上 10 分钟到 2 小时的冷却阶梯。暂定：
# 抓到的正文里还没有真正的账号级 429 措辞，清单保持简短，判定拿不准时收窄到
# 单模型窗口。
ACCOUNT_SCOPE_PHRASES = (
    "account-level", "account level", "account-wide", "account scope",
    "account quota", "account rate limit", "account limit",
    "per account", "per-account", "this account", "your account",
    "the account", "an account",
    "credential-level", "credential level", "credential scope",
    "credential limit", "per credential", "this credential",
    "the credential", "your credential",
    "账号级", "账户级", "该账号", "此账号", "该账户", "此账户",
    "账号限流", "账户限流", "凭证级", "该凭证",
)


def credential_scope_phrase(detail):
    """正文里的作用域措辞；没有指出作用域时返回 None。

    ASCII 短语按词边界锚定，accountId 这类标识符不会误命中。中文短语直接子串
    匹配：中文没有词边界可锚，也不可能出现在标识符内部。
    """
    lowered = (detail or "").lower()
    for phrase in ACCOUNT_SCOPE_PHRASES:
        if phrase.isascii():
            if re.search(r"\b%s\b" % re.escape(phrase), lowered):
                return phrase
        elif phrase in lowered:
            return phrase
    return None


def rate_limit_code(detail):
    """JSON 格式 429 正文的 code 字段；非 JSON 时返回 None。"""
    try:
        obj = json.loads(detail)
    except Exception:
        return None
    return obj.get("code") if isinstance(obj, dict) else None


def rate_limit_is_soft_shape(detail):
    """429 正文点名是限流时返回 True，不限作用域。

    覆盖故障环境出现过的两种形状：裸的 "usage exceeds frequency limit" 正文
    （263 条记录在案的 429 里占 229 条）与 intl 的 code 14003 正文（"too many
    requests"）。认出它们，调用方才能把窗口压在单模型上，避免锁整个凭据。
    """
    text = (detail or "").lower()
    if any(marker in text for marker in SOFT_LIMIT_MARKERS):
        return True
    return rate_limit_code(detail) in SOFT_LIMIT_CODES


# 上游「这个模型的窗口额度用满」的应答码。6004 是唯一带重置墙钟的额度判定，
# 也是用户口径里「达到限额」的唯一标志：别的 429（14003、裸频率限制文本、
# 账号级软限）即使碰巧带了个时钟，也不是额度用满的证据。
CAP_LIMIT_CODE = 6004


def rate_limit_is_cap(detail):
    """只对上游的额度用满应答（code 6004）返回 True。

    刻意容错：字段实测只出现过整数，但字符串形式不能悄悄丢掉真实的额度判定。
    解析不出来就不算额度用满，调用方的持久停用路径按拒绝处理。
    """
    try:
        return int(rate_limit_code(detail)) == CAP_LIMIT_CODE
    except (TypeError, ValueError):
        return False


def rate_limit_is_account_level(detail, reset_at):
    """只在有正面证据表明 429 作用于凭据时返回 True。

    模型级的形状会点名重置墙钟（code 6004）。把没有时钟当成凭据级，会让每个
    不带时间戳的 429（包括上游最常见的正文）给整个凭据买 600 秒冷却并翻倍到
    7200 秒：几次下来整个区域的账号同时冷却，面板测试却一直显示凭据健康。
    没有时钟什么也不证明：只有正文自己说限制作用在凭据上才停用凭据，其余都
    由调用方压成单模型窗口。

    暂定：抓到的正文里还没有出现过真正的账号级 429，措辞表保持简短，等抓到
    实样再补，不作猜测性扩充。
    """
    if reset_at is not None:
        return False
    return credential_scope_phrase(detail) is not None


#: 上游风控限制（HTTP 403, code 11140「账号存在异常行为，已被限制使用」）
#: 的账号冷却时长：限制按小时计，2 小时后自然重试，上游解封即自愈——比
#: 透传给客户端、账号继续被选用好，也比 402 那种「停到本地 04:00」轻。
RESTRICTED_403_COOLDOWN = 7200.0

#: 识别风控限制 403 的正文特征。内容审核的 403 走降级提示词那条路，二者必须
#: 分开：对被限制的账号换提示词没有意义。code 匹配用正则（requestId 是 hex，
#: 裸字符串 "11140" 可能撞上），措辞只收实测的限制用语，不猜。
_RESTRICTED_403_CODE_RE = re.compile(r'"code"\s*:\s*"?11140"?')
_RESTRICTION_403_MARKERS = ("unusual activity", "账号存在异常行为", "已被限制")


def restriction_is_account_level(detail):
    """403 正文指向账号被限制、而非内容被拒时返回 True。

    实测正文：{"code":11140,"msg":"request illegal",...,"displayMsg":{"en":
    "Your account has been restricted due to unusual activity","zh":"账号存在
    异常行为，已被限制使用"}}。en 在前 zh 在后，短截断也保得住 code 与 en；
    拿不全的正文宁可漏判回内容审核路径，也不误判。
    """
    if not detail:
        return False
    if _RESTRICTED_403_CODE_RE.search(detail):
        return True
    lowered = detail.lower()
    return any(marker in lowered for marker in _RESTRICTION_403_MARKERS)


#: 账号被上游限制后的网关状态。所有账号一起停用：限制发生后继续轮询下一个账号，
#: 正是把整个账号池关联起来的原因。恢复由操作者在看板重新启用账号，不自动恢复。
STAND_DOWN = {"active": False, "at": 0.0, "uid": "", "detail": ""}


def stand_down_accounts(trigger_uid, detail=""):
    """停用全部账号并记下原因，之后任何新请求都在入口被拒绝。"""
    if POOL is not None:
        POOL.set_all_enabled(False)
    STAND_DOWN.update({"active": True, "at": time.time(),
                       "uid": trigger_uid or "", "detail": (detail or "")[:300]})
    log("account %s restricted by upstream (403): all accounts disabled, "
        "gateway stopped" % (trigger_uid or "?")[:8], level="ERROR")


def stand_down_active():
    return bool(STAND_DOWN.get("active"))


def stand_down_state():
    """面板要的停止状态；没有停止时 active 为 False。"""
    return dict(STAND_DOWN)


def clear_stand_down():
    """操作者手动启用账号后解除停止状态。"""
    if STAND_DOWN.get("active"):
        log("gateway resumed: an account was enabled on the panel")
    STAND_DOWN.update({"active": False, "at": 0.0, "uid": "", "detail": ""})


def stand_down_message(exc=None):
    """503 的正文：说明为什么停、以及怎样恢复。"""
    text = ("网关已停止：一个账号被上游限制（403）。全部账号已停用，"
            "确认上游解封后在看板「全部启用」恢复")
    detail = (getattr(exc, "detail", "") or "").strip()
    return text + ("；上游原文：%s" % detail[:200] if detail else "")


def _retry_after_epoch(value):
    """把 HTTP Retry-After 值（秒数或 HTTP 日期）换算成 epoch。"""
    value = str(value or "").strip()
    if value.isdigit():
        return time.time() + int(value)
    if value:
        try:
            return email.utils.parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def parse_rate_limit_reset(detail, retry_after=None):
    """从上游 429 正文里取出重置时间；正文没给就返回 None。

    上游 code 6004 的应答带重置墙钟，但措辞按区域不同：intl 形如 "... your
    usage will reset at 2026-09-19 18:29:03 UTC+8 ..."，cn 形如 "... 将在
    2026-10-09 14:44:59 UTC+8 重置 ..."。只匹配英文形状时每个 cn 429 都取不到
    重置时间，被当成账号级软限冷却整个凭据，而非只停被限流的模型。返回 epoch 或
    None。刻意容错：解析不出的正文不能打断请求路径。retry_after 是 HTTP
    Retry-After 头（秒数或 HTTP 日期），只在正文没给时间时使用。
    """
    if not detail:
        return _retry_after_epoch(retry_after)
    m = re.search(r"(?:reset at|将在)\s*"
                  r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", detail)
    if not m:
        return _retry_after_epoch(retry_after)
    stamp = m.group(1).replace("T", " ")
    tz = re.search(r"UTC([+-]\d{1,2})(?::?(\d{2}))?", detail)
    offset = 8 * 3600
    if tz:
        hours = int(tz.group(1))
        minutes = int(tz.group(2) or 0)
        offset = hours * 3600 + (minutes * 60 if hours >= 0 else -minutes * 60)
    try:
        base = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")) - time.timezone
        return base - offset
    except Exception:
        return None


def upstream_timeouts():
    """chat 上游套接字的 (header, idle) 超时秒数。

    header 覆盖连接、TLS 与首字节；idle 限定流中每次读取，有数据就保活，静默流
    超时退出并归还在途的名额。没有总时长上限，与面板项目的语义一致。
    """
    try:
        cfg = wb_settings.upstream_config(ACCOUNTS_DIR)
    except Exception:
        cfg = None
    header = 120.0
    idle = 300.0
    if isinstance(cfg, dict):
        try:
            header = float(cfg.get("header_timeout_seconds") or header)
        except (TypeError, ValueError):
            pass
        try:
            idle = float(cfg.get("idle_timeout_seconds") or idle)
        except (TypeError, ValueError):
            pass
    return max(1.0, header), max(1.0, idle)


def _apply_stream_idle_timeout(response, seconds):
    """把上游套接字切到流中的 idle 超时。

    urllib 的 timeout 只覆盖初始请求，同一条套接字随后要读完整个 SSE 流。把套接字
    超时设成 idle 值，限定的是每次读取而非整条流。尽力而为：认不出的套接字结构
    保留 header 超时。
    """
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return False
    if seconds <= 0:
        return False
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is None:
        sock = getattr(fp, "_sock", None)
    if sock is None:
        return False
    try:
        sock.settimeout(seconds)
        return True
    except Exception:
        return False


DEBUG_ACCOUNT_HEADER = "X-Debug-Account"
#: 瞬时故障（5xx、连接抖动、超时）先在同一个账号上重试几次再换号：换号会把
#: 整段对话留在原账号的前缀缓存丢掉。429 / 401 / 403 不走这条路。
TRANSIENT_SAME_ACCOUNT_RETRIES = 2
#: 一次请求最多在原地重试几次。原地重试不占换号预算，否则账号池小的时候会被
#: 它吃光；但也不能不限，上游整体故障时每次重试都要客户端多等一轮。
MAX_TRANSIENT_SAME_ACCOUNT_RETRIES = 4


def _retry_same_account(account, counters):
    """在同一个账号上记一次原地重试；预算耗尽时返回 False。

    两个上限：单个账号最多 TRANSIENT_SAME_ACCOUNT_RETRIES 次，整次请求最多
    MAX_TRANSIENT_SAME_ACCOUNT_RETRIES 次。瞬时故障先原地重试，重试不动了
    才换号，避免把整段对话的前缀缓存留在原账号上。
    """
    if account is None:
        return False
    if sum(counters.values()) >= MAX_TRANSIENT_SAME_ACCOUNT_RETRIES:
        return False
    used = counters.get(account.uid, 0)
    if used >= TRANSIENT_SAME_ACCOUNT_RETRIES:
        return False
    counters[account.uid] = used + 1
    return True


class SlotLease(object):
    """一个请求占着的「账号 + 模型」并发名额，响应体读完才归还。

    open_upstream() 选号时占下、成功后交给它保管；续写轮传入同一个对象，
    选回原账号就沿用，换号就先占新名额再归还旧的。
    """

    def __init__(self):
        self.account = None
        self.model = None

    def _holds(self, account, model):
        return account is self.account and model == self.model

    def claim(self, account, model):
        """为这一次尝试占名额；已经持有同一个名额时直接沿用。"""
        if self._holds(account, model):
            return True
        return account.acquire_request(model)

    def drop(self, account, model):
        """这一次尝试没有用上：退掉刚占的名额，已经持有的那个不动。"""
        if not self._holds(account, model):
            account.release_request(model)

    def keep(self, account, model):
        """这一次尝试成功：持有它的名额，换号时归还旧的那个。"""
        if self._holds(account, model):
            return
        self.release()
        self.account, self.model = account, model

    def release(self):
        if self.account is not None:
            self.account.release_request(self.model)
            self.account = self.model = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


_FREE_MODEL_CREDIT_FALLBACKS = {
    ("intl", "deepseek-v4.1-flash"): "deepseek-v4.1-flash-sg",
}


def _free_model_credit_fallback_enabled():
    return wb_settings.free_model_credit_fallback(ACCOUNTS_DIR)


def open_upstream(payload, session_key=None, target_realm=None, prebuilt_body=None,
                  allow_credit_fallback=True, allow_product_switch=True,
                  only_uid=None, lease=None):
    # 选号前刷新日限额守卫。底层扫描是增量的且带 TTL 缓存，热路径上只花一次
    # stat() 加一份缓存字典；被任一守卫停住的账号和其他不可用账号一样跳过。
    # 三个守卫共用同一份折叠，一次调用全部覆盖，避免每请求重复计算。
    wb_usage.apply_daily_guards()
    # 评分分配用今日消耗速度算欠账：这一项必须跟着当天用量推进。底层的日折叠
    # 有 15 秒 TTL 且增量读取，热请求只付一次字典拷贝；智能分配关着时直接返回。
    wb_usage.apply_routing_inputs()
    # 账号被上游限制过就停止发送任何请求：继续轮询下一个账号会把账号池关联起来。
    if stand_down_active():
        raise GatewayStandDown(STAND_DOWN.get("detail") or "",
                               account_uid=STAND_DOWN.get("uid") or None)
    # 剩余用量估计并入智能分配：把「估计剩余接近耗尽」的 (账号, 模型) 权重
    # 推给池，作为评分的一点倾斜。智能分配关着时不需要它，直接清空，省掉
    # 剩余估算的读取成本。
    if POOL is not None:
        smart = bool(getattr(POOL, "smart_routing", False))
        POOL.apply_remaining_weights(wb_usage.remaining_schedule_weights() if smart else None)
    realm = target_realm or detect_model_realm(payload.get("model")) or CURRENT_REALM
    model = str(payload.get("model") or "")
    # 复用调用方已建好的 body：/v1/chat/completions 在进这里之前已经 build 过
    # 一次（那次的产物只用于一行日志和 prompt_fingerprint），这里再建一遍等于
    # 白付一次序列化。403 降级重试时仍按 payload 重建（degrade 会切换 prompt
    # 模式，必须拿到新 body）。
    upstream_body = (prebuilt_body if prebuilt_body is not None
                     else build_upstream_body(payload))
    # PATCHED-BY-OPS: 客户端未提供会话标识时，用对话稳定前缀兜底。
    # 位置放在 build_upstream_body 之后，保证键与真正发往上游的消息一致
    # （该函数可能在最前面插入 SYSTEM_PROMPT）。affinity_page 只在超限对话上
    # 有值（页内轮转），客户端自带会话键的请求不参与。
    affinity_page = None
    if not session_key:
        session_key, affinity_page = affinity_route(upstream_body.get("messages"))
        if session_key and affinity_debug_enabled():
            log("affinity: derived %s for %d msgs"
                % (session_key, len(upstream_body.get("messages") or [])))
    if not wb_settings.session_affinity(ACCOUNTS_DIR):
        # 面板关闭会话粘性：每一轮都重新选号。
        session_key = None
        affinity_page = None
    total = max(1, POOL.count_ready(realm, model=model)) if POOL else 1
    tried = set()
    # 同一次请求内每个账号最多刷新一次 token：401 可能只是访问令牌过期，
    # 先刷新再重试同号，减少把还能恢复的凭证当成坏号冷却。
    refreshed_401 = set()
    # 这一轮请求已经记过账的账号：重试轮（transient_hits 后 tried.clear() 整池
    # 重轮）会把同一个账号再挑一遍，若每次都记，一次请求就能给同一个账号记上
    # 好几笔 fails/degrade——池子越空、重轮越频繁，越容易把一个刚抖了一下的
    # 账号直接推过熔断阈值。一次请求对一个账号最多记一次。
    charged = set()

    def charge_account(account, message, unknown):
        """给账号记一笔失败，一次请求最多记一笔（见上面的 charged）。"""
        if account.uid in charged:
            return False
        charged.add(account.uid)
        if unknown:
            account.note_unknown_failure(message)
        else:
            account.note_failure(message)
        return True

    last_error = None
    last_uid = None
    last_429 = None
    last_429_detail = ""
    last_403_detail = ""
    transient_hits = 0
    # 每个账号在原地重试了几次，避免一个坏账号把整个重试预算吃光。
    transient_retries = {}
    degraded_retried = False
    # 本机断网的次数：这一轮里只要出现过，最终报错就按「网络不可用」报，不按
    # 「上游不稳」报——两者的处置完全相反（去查本机网络 vs 去查账号/上游）。
    local_net_hits = 0
    # 每个请求只读一次，不按尝试读：这是面板设置，每次重试都读是纯开销；
    # header/idle 超时同样每请求读一次，与面板口径一致，设置读取不进重试循环。
    auto_switch = (auto_switch_product_enabled()
                   if allow_product_switch else False)
    header_timeout, idle_timeout = upstream_timeouts()
    # 出口级退避开关同样每次请求只读一次；关掉时下面的重试循环不经过任何出口
    # 退避。
    egress_backoff = wb_settings.egress_backoff_enabled(ACCOUNTS_DIR)
    # 同一段出口在一次请求里只记一次：重试轮（tried.clear() 之后整池重轮）会把
    # 同一个账号 / 同一段出口再挑一遍，若每次都记，一次请求就能把共享计数器推高
    # 好几步。记过的那一段沿用同一段退避，不重复记。
    egress_wait = {}

    def charge_egress(url, proxy):
        key = wb_upstream_pool.egress_key(url, proxy)
        if key not in egress_wait:
            egress_wait[key] = wb_egress.note_transient(key)
        return egress_wait[key]

    max_attempts = max(2, total) + 1 + (MAX_PRODUCT_SWITCHES if auto_switch else 0)
    # 原地重试不占换号预算，加上它的上限，小账号池才不会在轮到下一个账号之前
    # 就把循环用完。
    max_attempts += MAX_TRANSIENT_SAME_ACCOUNT_RETRIES
    pending_lease = None
    # 缓存键关闭时每个候选账号发送的字节完全相同；每次重试都重新 dumps
    # 一个可能上百 KB 的 body 是纯浪费。body 只在 403 降级重建时失效。
    base_data = None
    # 每次失败都把【出错的那个账号】从这段对话的历史里降级（demote），而不是
    # 忘掉整段对话：重选时优先回到还留着这段前缀的暖号（见 SessionAffinity），
    # 一次大 prompt 的冷启动比重选一次账号贵得多。
    for _attempt in range(max_attempts):
        if pending_lease is not None:
            lease.drop(*pending_lease)
            pending_lease = None
        if only_uid:
            account = POOL.get(only_uid) if POOL else None
            if (account is not None and
                    (account.uid in tried or account.realm != realm
                     or not account.ready(model=model)
                     or (lease is not None and not lease.claim(account, model)))):
                account = None
        else:
            account = POOL.pick_for_session(
                realm=realm, session_key=session_key, exclude=tried, model=model,
                page=affinity_page,
                claim=(lambda candidate: lease.claim(candidate, model))
                      if lease is not None else None) if POOL else None
        if account is None:
            if transient_hits and _attempt < max_attempts - 1:
                tried.clear()
                time.sleep(min(1.5 * transient_hits, 3.0))
                continue
            break
        if account.realm != realm:
            if session_key and POOL: POOL.affinity.demote(session_key, account.uid)
            continue
        tried.add(account.uid)
        last_uid = account.uid
        if lease is not None:
            pending_lease = (account, model)
        chat_url = account.chat_base_url() + CHAT_PATH
        # 缓存键按账号隔离，所以按候选账号重建，不能提前建一次。仅 opt-in：
        # 实测上游没有这个字段也会缓存前缀（见 prompt_cache_key_enabled）。
        if prompt_cache_key_enabled():
            attempt_body = inject_prompt_cache_key(upstream_body, account.uid, session_key)
            attempt_data = json.dumps(attempt_body, ensure_ascii=False).encode("utf-8")
        else:
            attempt_body = upstream_body
            if base_data is None:
                base_data = json.dumps(upstream_body, ensure_ascii=False).encode("utf-8")
            attempt_data = base_data
        # 会话头族（X-Conversation-ID 等）由身份构造里每次请求现生成：上游不拿
        # 这个 id 存历史，不需要按会话复用；缺了它上游的渠道校验会回 400 /
        # code 11-128（`conversation id missing`），补的是「有」而不是「稳定」。
        try:
            req = wb_upstream_pool.Request(chat_url, data=attempt_data, method="POST",
                                           headers=account.headers(
                                               purpose="chat",
                                               model=attempt_body.get("model") or model))
        except BaseException:
            if lease is not None:
                lease.drop(account, model)
                pending_lease = None
            raise
        try:
            resp = wb_accounts.urlopen(req, timeout=header_timeout,
                                       proxy=account.proxy)
            _apply_stream_idle_timeout(resp, idle_timeout)
            account.note_success(model=model)
            if egress_backoff:
                # 这条出站路径刚成功服务了一次请求，瞬时故障连击清零
                # （与账号的 note_success 同规则）。
                wb_egress.note_success(
                    wb_upstream_pool.egress_key(chat_url, account.proxy))
            reset_switch_counter(account, model)
            if lease is not None:
                lease.keep(account, model)
                pending_lease = None
            # 第三个元素是本次请求实际运行的思考档位：body 每次尝试重建，档位
            # 属于模型与请求本身，调用方把它记进用量行。
            return resp, account, upstream_effort_of(upstream_body, model)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    detail = read_http_error(exc, 600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                body_reset = parse_rate_limit_reset(detail)
                header_reset = _retry_after_epoch(
                    getattr(exc, "headers", None) and exc.headers.get("Retry-After"))
                reset_at = body_reset if body_reset is not None else header_reset
                if rate_limit_is_account_level(detail, reset_at):
                    wait = account.note_soft_rate("HTTP 429 (account soft rate)")
                    log("account %s soft-rate limited (%r), cooling %.0fs (streak %d)"
                        % (account.uid[:8], credential_scope_phrase(detail), wait,
                           account.soft_streak))
                    if session_key and POOL:
                        POOL.affinity.demote(session_key, account.uid)
                    last_error = exc
                    last_429 = exc
                    last_429_detail = detail
                    continue
                # 没有重置时钟，正文分不清限流作用在凭据还是单模型。可识别的
                # 软限形状以重复出现为证据：窗口落在该模型，按短阶梯增长（60 秒
                # 到 10 分钟），不走凭据的 600 秒到 2 小时阶梯。无法识别的正文
                # 同样在单模型上停 60 秒，不计连击。
                wait = max(1.0, reset_at - time.time()) if reset_at else 60.0
                if reset_at is None and rate_limit_is_soft_shape(detail):
                    wait = account.note_unscoped_rate(model)
                    log("account %s: unscoped 429, parking '%s' for %.0fs "
                        "(unscoped streak %d)"
                        % (account.uid[:8], model, wait, account.unscoped_streak))
                # 模型级：只有该模型被限流，同一凭据下其他模型照常可用。
                if reset_at and reset_at > time.time():
                    # 上游给了未来的恢复时刻：这是限流窗口，不是账号故障，
                    # 不写 last_error；窗口本身持久化，重启后仍然生效。
                    if rate_limit_is_cap(detail):
                        # 带重置时钟的额度用满是唯一可观测的窗口预算起点；只靠
                        # 用量行会丢掉它（重试批次里只有最后一个账号保留归属）。
                        # 见 wb_usage.note_limit_event()。
                        wb_usage.note_limit_event(account, model, reset_at)
                        try:
                            if account.note_model_cap(model, reset_at):
                                wb_usage._save_account_window(account)
                        except Exception as exc:
                            log("account %s: model cap not persisted: %s"
                                % (account.uid[:8], exc))
                    else:
                        try:
                            if account.note_model_hold(model, reset_at):
                                wb_usage._save_account_window(account)
                        except Exception as exc:
                            log("account %s: model window not persisted: %s"
                                % (account.uid[:8], exc))
                else:
                    # 没有恢复时刻，或时刻已过（上游仍然拒绝）：按最短停靠处理。
                    account.note_error("HTTP 429 (model throttled)", model=model,
                                       cooldown=wait, detail=detail)
                if auto_switch and _try_switch_product(account, model):
                    # 换了身份就等于换了一条配额线：要把它从「已试过」拿掉，
                    # 并清掉刚刚记下的模型冷却，否则下一轮循环会找不到账号。
                    # keep_caps：6004 的禁用（仍在恢复期内）跨身份成立，不能被
                    # 这次清理带走，否则账号在 6004 之后立刻又可用。
                    tried.discard(account.uid)
                    try:
                        account.clear_error(model=model, keep_caps=True)
                    except Exception:
                        pass
                    if session_key and POOL:
                        POOL.affinity.demote(session_key, account.uid)
                    continue
                log("account %s throttled on '%s' (429), retry in %ds"
                    % (account.uid[:8], model, int(wait)))
                if session_key and POOL:
                    POOL.affinity.demote(session_key, account.uid)
                last_error = exc
                last_429 = exc
                last_429_detail = detail
                continue
            if exc.code == 403:
                try:
                    detail = read_http_error(exc, 600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                # 先分流：上游风控限制（code 11140「账号存在异常行为，已被限制
                # 使用」）是账号级的坏，降级提示词救不了，透传给客户端只会让
                # 一个被限制的账号把对话一次次打死。冷却这个账号、换下一个继
                # 续；全都试完还是不行才把原因交给客户端。
                if restriction_is_account_level(detail):
                    account.note_error("HTTP 403 (account restricted)",
                                       cooldown=RESTRICTED_403_COOLDOWN,
                                       single_account=(total <= 1), detail=detail)
                    if session_key and POOL:
                        POOL.affinity.demote(session_key, account.uid)
                    stand_down_accounts(account.uid, detail)
                    raise GatewayStandDown(detail, account_uid=account.uid)
                # 面板 degrade.go：passthrough/append 模式的内容拒绝通常是
                # system 提示指纹误伤，触发降级窗口（见 PROMPT_DEGRADE）后按新
                # prompt 重建 body，本轮重试一次；custom 模式不参与。
                if (not degraded_retried
                        and prompt_mode_config().get("mode") in ("passthrough", "append")):
                    PROMPT_DEGRADE.trigger()
                    degraded_retried = True
                    upstream_body = build_upstream_body(payload)
                    base_data = None
                    tried.discard(account.uid)
                    log("upstream 403 (content review) -> degraded prompt retry")
                    continue
                log("upstream 403 for '%s' (content review), passing through" % model)
                if session_key and POOL:
                    POOL.affinity.demote(session_key, account.uid)
                last_error = exc
                last_403_detail = detail
                break
            if exc.code == 402:
                try:
                    detail = read_http_error(exc, 400).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                account.note_balance_cooled(detail or "HTTP 402 (insufficient credits)")
                log("account %s out of credits (402), parked until 04:00"
                    % account.uid[:8])
                if session_key and POOL:
                    POOL.affinity.demote(session_key, account.uid)
                last_error = exc
                continue
            if exc.code == 401:
                if account.uid not in refreshed_401:
                    refreshed_401.add(account.uid)
                    if account.refresh():
                        tried.discard(account.uid)
                        log("account %s rejected (HTTP 401), token refreshed; "
                            "retrying the same account" % account.uid[:8])
                        last_error = exc
                        continue
                    log("account %s rejected (HTTP 401), refresh failed"
                        % account.uid[:8])
                detail = read_http_error(exc, 400).decode("utf-8", "replace")
                if not auth_failure_text("HTTP 401 " + detail):
                    # 中间层代理的 HTML 401 不代表令牌失效，冷却健康账号会让它
                    # 一直停到手动重新登录。只换号，不记故障。
                    log("account %s got a non-credential 401; rotating without "
                        "cooling" % account.uid[:8])
                    if session_key and POOL:
                        POOL.affinity.demote(session_key, account.uid)
                    last_error = exc
                    continue
                log("account %s rejected (HTTP 401), rotating" % account.uid[:8])
                if session_key and POOL:
                    POOL.affinity.demote(session_key, account.uid)
                account.note_error("HTTP 401",
                                   cooldown=60,
                                   single_account=(total <= 1))
                last_error = exc
                continue
            if exc.code >= 500:
                detail = http_error_detail(exc)
                code = upstream_error_code(detail)
                if (code in MODEL_SCOPED_ERROR_CODES
                        and not model_scoped_errors_penalize_account()):
                    # 坏模型不该熔断整个账号池：正文说这是模型或服务的问题，
                    # 不是凭据问题。停止轮换，把上游原文交回客户端。
                    log("upstream %s for '%s' (code %s, model-scoped); "
                        "not charging the account" % (exc.code, model, code))
                    last_error = exc
                    break
                # 一切服务端 5xx（含上游自定义的 550 这类）都按瞬时故障轮转，
                # 不在已知清单里的也不能把错误原样甩回客户端——换号是默认，
                # 全试完才交出去。
                transient_hits += 1
                if egress_backoff and exc.code in (502, 503, 504):
                    # 网关侧的抖动（502/503/504）说明出口有问题，不是凭据的问题：
                    # 记在出口上并退避几秒。500 仍记在账号上——它可能是这条凭据
                    # 自己的故障，要先查清再从熔断里放出来。
                    wait = charge_egress(chat_url, account.proxy)
                    log("upstream %s for '%s', backing off the egress %.1fs"
                        % (exc.code, model, wait))
                    if session_key and POOL:
                        POOL.affinity.demote(session_key, account.uid)
                    last_error = exc
                    time.sleep(min(wait, EGRESS_SLEEP_MAX))
                    continue
                if _retry_same_account(account, transient_retries):
                    tried.discard(account.uid)
                    log("upstream %s for '%s', retrying on the same account"
                        % (exc.code, model))
                    last_error = exc
                    continue
                charge_account(account, "HTTP %d" % exc.code, unknown=False)
                log("upstream %s for '%s', retrying (fails=%d)"
                    % (exc.code, model, account.fails))
                if session_key and POOL:
                    POOL.affinity.demote(session_key, account.uid)
                last_error = exc
                continue
            raise
        except Exception as exc:
            if looks_like_local_network(exc):
                # 本机没网：不记账。账号没发出去一个字节，这笔不该算它的；
                # 断网期间每次都记的话，网络一恢复池子里全是冷却中的账号。
                # 也不能只 break——网络抖动几秒很常见，先原地重试几轮。
                local_net_hits += 1
                if local_net_hits == 1:
                    log("local network looks down (%s); not charging any account"
                        % exc_detail(exc))
                last_error = exc
                time.sleep(min(0.3 * local_net_hits, 1.5))
                continue
            if is_transient(exc):
                transient_hits += 1
                if _retry_same_account(account, transient_retries):
                    tried.discard(account.uid)
                    log("upstream connection hiccup for '%s' (%s), retrying on the "
                        "same account" % (model, exc_detail(exc)))
                    last_error = exc
                    time.sleep(min(0.6 * transient_hits, 2.0))
                    continue
                if egress_backoff:
                    # 传输抖动是出口的问题，不是账号的问题：记在出口上并退避几秒，
                    # 账号的 fails/degrade 计数保持不动，几秒的抖动不会把健康账号
                    # 关掉。
                    wait = charge_egress(chat_url, account.proxy)
                    log("account %s: upstream connection hiccup for '%s' (%s), "
                        "backing off the egress %.1fs"
                        % (account.uid[:8], model, exc_detail(exc), wait))
                    last_error = exc
                    time.sleep(min(wait, EGRESS_SLEEP_MAX))
                    continue
                if session_key and POOL:
                    POOL.affinity.demote(session_key, account.uid)
                charge_account(account, "connection: %s" % exc_detail(exc, 120),
                               unknown=True)
                # 带上 UID：这一行同时是一次扣分（连续 3 次就把账号熔断 30
                # 分钟），不写清楚是谁挨的这一下，事后只能看着一串 hiccup 猜
                # 是哪几个账号被关掉、池子为什么突然空了。
                log("account %s: upstream connection hiccup for '%s' (%s), "
                    "retrying (degrade=%d)"
                    % (account.uid[:8], model, exc_detail(exc), account.degrade_count))
                last_error = exc
                time.sleep(min(0.6 * transient_hits, 2.0))
                continue
            if session_key and POOL:
                POOL.affinity.demote(session_key, account.uid)
            charge_account(account, str(exc)[:120], unknown=True)
            account.note_error(str(exc)[:120], cooldown=60, single_account=(total <= 1))
            # 这一类失败要留下 uid、异常原文与刚设的冷却时长：池子空了的报错只
            # 说得清「谁在冷却」，说不清「是谁把它记成冷却的」，事后只能对着一串
            # hiccup 猜是哪几个账号被关掉。
            log("account %s: %s, cooling %.0fs (fails=%d, ready=%d)"
                % (account.uid[:8], exc_detail(exc, 160),
                   3.0 if total <= 1 else 60.0, account.fails, total))
            last_error = exc
            continue
    if pending_lease is not None:
        lease.drop(*pending_lease)
        pending_lease = None
    # 只有该区域所有启用且带凭证的账号都对这个免费模型持久记有上游 6004 额度
    # 用满时，才允许花积分改用付费模型。开关 opt-in，重试可能产生真实费用。
    paid_model = _FREE_MODEL_CREDIT_FALLBACKS.get((realm, model))
    eligible = ([a for a in POOL.accounts
                 if a.realm == realm and a.enabled and a.access_token] if POOL else [])
    if (allow_credit_fallback and paid_model and _free_model_credit_fallback_enabled()
            and eligible and all(a.cap_until(model) > time.time() for a in eligible)):
        paid_payload = dict(payload)
        paid_payload["model"] = paid_model
        log("free model '%s' exhausted on all %d %s account(s); retrying paid model '%s'"
            % (model, len(eligible), realm, paid_model))
        response, account, effort = open_upstream(
            paid_payload, session_key=session_key, target_realm=realm,
            allow_credit_fallback=False, allow_product_switch=False,
            only_uid=only_uid, lease=lease)
        try:
            response._wb_billed_model = paid_model
        except Exception:
            pass
        return response, account, effort
    if last_error is not None:
        # 把出错账号带给调用方：错误行要能指名账号，而调用方没有保留它的
        # 局部变量。
        try:
            last_error.account_uid = last_uid
        except Exception:
            pass
        if looks_like_local_network(last_error):
            # 最后一笔就是本机网络的问题：按「本机网络不可用」上报，不要报成
            # 「上游不可达」——后者会让人去查上游/账号，而真正要查的是本机。
            exc = LocalNetworkError(exc_detail(last_error))
            exc.account_uid = last_uid
            raise exc
        if last_429 is not None:
            exc = RateLimited(last_429, last_429_detail,
                              wait=retry_after_seconds(model, realm))
            exc.account_uid = last_uid
            raise exc
        if last_403_detail:
            exc = ContentRejected(last_error, last_403_detail)
            exc.account_uid = last_uid
            raise exc
        raise last_error
    throttled, wait = realm_model_throttled(realm, model)
    if throttled:
        raise RateLimited(None, "usage exceeds frequency limit", wait=wait)
    enabled = [a for a in POOL.accounts
               if a.realm == realm and a.enabled and a.access_token] if POOL else []
    if enabled and model:
        servable = [a for a in enabled if a.ready(model=model)]
        if servable and all(not a.has_request_capacity(model) for a in servable):
            reason = ("concurrent request limit reached for this account and "
                      "model; retry when an in-flight request finishes")
            raise RateLimited(None, reason, wait=1, message=reason)
    if enabled and all(a.daily_limit_blocked() for a in enabled):
        reason = ("every usable account reached today's token limit (%s per "
                  "account); the pool resumes after local midnight"
                  % wb_settings.daily_token_limit(ACCOUNTS_DIR, realm))
        raise RateLimited(None, reason,
                          wait=wb_usage.seconds_until_local_midnight(), message=reason)
    if enabled and model and all(a.credit_limit_blocked(model) for a in enabled):
        reason = ("every usable account reached today's credit limit (%s per "
                  "account); paid models resume after local midnight, free "
                  "models keep working"
                  % wb_settings.daily_credit_limit(ACCOUNTS_DIR, realm))
        raise RateLimited(None, reason,
                          wait=wb_usage.seconds_until_local_midnight(), message=reason)
    if enabled and model and all(a.model_token_limit_blocked(model) for a in enabled):
        reason = ("every usable account reached today's token limit for %s "
                  "(%s per account); the model resumes after local midnight"
                  % (model, wb_settings.model_daily_token_limit(ACCOUNTS_DIR, realm)))
        raise RateLimited(None, reason,
                          wait=wb_usage.seconds_until_local_midnight(), message=reason)
    raise RuntimeError("no usable account for realm '%s': all are disabled, cooling down, "
                       "expired or parked by the daily token limit%s"
                       % (realm, pool_unavailable_detail(realm, model)))

def pool_unavailable_detail(realm, model=None):
    """把该区域每个账号不可用的原因拼成一句，附在「没有可用账号」后面。

    账号可能因停用、冷却、过期、日限额、熔断或降权窗口而不可用，只说「没有可用
    账号」看不出卡在哪一条。这里不改变判定，只把结果说出来：每个不可用账号
    「八位 UID + 原因」，看板上仍显示可用的账号也能对上号。
    """
    if POOL is None:
        return ""
    accounts = [a for a in POOL.accounts if a.realm == realm]
    if not accounts:
        return "；该区域没有任何账号"
    reasons = []
    for account in accounts:
        try:
            reason = account.unavailable_reason(model=model)
        except Exception:
            reason = ""
        if reason:
            reasons.append("%s %s" % ((account.uid or "?")[:8], reason))
    if not reasons:
        return ""
    return "；各账号：%s" % "，".join(reasons)

def extract_session_key(headers, payload):
    key = (
        headers.get("X-Conversation-Id") or
        headers.get("Conversation-Id") or
        headers.get("X-Session-Id") or
        headers.get("Session-Id") or
        payload.get("conversation_id") or
        payload.get("session_id") or
        (payload.get("metadata") or {}).get("conversation_id")
    )
    if key:
        return str(key).strip()
    return None

# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------
from wb_http import Handler

Handler.server_version = "wb-proxy-center/" + VERSION


def running_version():
    """进程对外报告的版本号，例如 "0.6.0"。

    Handler 与更新检查器都从这里取，避免出现与 VERSION、server_version 并列的
    第三份副本。
    """
    return Handler.server_version.split("/", 1)[-1]


def _if_none_match_hit(header_value, etag):
    """If-None-Match 头是否命中给定的 ETag。

    RFC 7232 §3.2：字段值是一个逗号分隔的 entity-tag 列表，且 If-None-Match
    用弱比较——W/ 前缀忽略，所以 W/"x" 与 "x" 等同；"*" 匹配任何已存在的表示。
    """
    for token in header_value.split(","):
        token = token.strip()
        if token == "*":
            return True
        if token.startswith("W/"):
            token = token[2:].strip()
        if token and token == etag:
            return True
    return False


def _cache_etag(kind, key, built_at):
    """给带 TTL 缓存的只读接口派生一个强校验符（ETag）。

    校验符由（接口名、缓存键、缓存条目的构建时刻）三者派生，不对响应字节做哈希。
    构建时刻在两次重建之间恒定，数字没变的轮询因此回 304，缓存一重建就必然变化。
    payload 里有 time.time() 派生的字段（/usage 的 started/since、
    /usage/timeseries 的桶边界与 until），同一份缓存序列化两次都不逐字节相同，
    哈希字节得到的 tag 永远不命中，还白付一次全量序列化与哈希；而键与时刻都是
    现成的，一次 sha1 只哈希几十字节。

    键文本进摘要而不直接拼进 tag：缓存键含调用方给的 realm，它可能带引号或反斜杠
    （`?realm=a"b` 一路原样进键），拼出来会发出畸形的 ETag 头，摘要则永远是合法的
    etagc 字符。摘要只取 16 个十六进制位：只需在同一实例的少量缓存条目之间区分，
    不是安全边界。
    """
    stamp = "%x" % int(built_at * 1_000_000)
    digest = hashlib.sha1(("%s\x1f%s" % (kind, key)).encode("utf-8")).hexdigest()[:16]
    return '"%s-%s"' % (stamp, digest)


def _anthropic_messages_enabled():
    return wb_settings.enable_anthropic_messages(ACCOUNTS_DIR)

# ---------------------------------------------------------------------------
# 按 API Key 统计并发
# ---------------------------------------------------------------------------
_key_active_concurrency = {}
_key_concurrency_lock = threading.Lock()

def _acquire_key_slot(key_entry):
    """尝试为 key_entry 占一个并发槽位；返回 (ok, current, limit)。"""
    if not isinstance(key_entry, dict):
        return True, 0, 0
    k_id = key_entry.get("id")
    if not k_id:
        return True, 0, 0
    limit = int(key_entry.get("concurrency_limit") or 0)
    if limit <= 0:
        return True, 0, 0
    with _key_concurrency_lock:
        current = _key_active_concurrency.get(k_id, 0)
        if current >= limit:
            return False, current, limit
        _key_active_concurrency[k_id] = current + 1
        return True, current + 1, limit

def _release_key_slot(key_entry, held=False):
    """归还 _acquire_key_slot 占的并发槽位。

    释放只看本次请求是否真的拿到了槽位，不重读当前限额：限额在请求进行中
    被改成 0 时，按新限额判断会漏减，把计数永久留在表里。
    """
    if not held or not isinstance(key_entry, dict):
        return
    k_id = key_entry.get("id")
    if not k_id:
        return
    with _key_concurrency_lock:
        current = _key_active_concurrency.get(k_id, 0)
        if current > 1:
            _key_active_concurrency[k_id] = current - 1
        else:
            _key_active_concurrency.pop(k_id, None)


















# Messages 管线反向调用网关的函数，注册放在全部定义之后：bind 会核对接口，
# 定义还没执行完时注册只会报出一堆假缺失。
wb_messages_responses.bind(sys.modules[__name__])


if __name__ == "__main__":
    # pythonw 下 stdout/stderr 为 None：重定向到空设备，看板日志仍走内存缓冲。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    import wb_startup
    wb_startup.main(sys.modules[__name__])
