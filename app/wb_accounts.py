import re
import base64
import collections
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from wb_fingerprint import derive_id, generate_request_id, session_id
import wb_activity
import wb_atrest
import wb_host
import wb_identity
import wb_settings
import wb_upstream_pool
import wb_webagent

# ---------------------------------------------------------------------------
# 网页版通道：对话走 console/as 下的 agent 会话，只带
# Authorization: Bearer 与 X-User-Id，没有桌面端的 X-IDE-* 指纹。
#
# 建会话只是排队：agent 要等客户端接上沙箱（.../{id}/session 给的 link + token）
# 并请求这一轮才会跑，否则停在 CREATING 且没有输出。顺序是 建会话 → 取 session
# → ACP over HTTP+SSE（见 wb_webagent）；每日活跃奖励只认跑完的会话，所以国际版
# 打卡要在桌面端对话之外再走一次并跑到 completed。
# ---------------------------------------------------------------------------
WEB_ORIGIN = "https://www.workbuddy.ai"
WEB_ORIGIN_CN = "https://www.workbuddy.cn"
WEB_CONVERSATIONS_URL = WEB_ORIGIN + "/console/as/conversations/"
PROFILE_PATH = "/console/account"
WEB_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")
DAILY_CHAT_MODEL = "deepseek-v4.1-flash"
DAILY_CHAT_WEB_PROMPT = "Hi"
#: 网页通道一轮最多等多久（秒）。没配 settings 的账号（如单测里手工构造的）
#: 用它兜底。
WEB_TURN_TIMEOUT = int(os.environ.get("WB_WEB_TURN_TIMEOUT") or "120")
#: 网页通道一共试几次（含第一轮）。第 N 轮的等待是首轮的 2^(N-1) 倍，所以
#: 3 次 = 最多等 base×(1+2+4) 秒。设置页可改（见 wb_settings.daily_chat_web_attempts）。
WEB_TURN_ATTEMPTS = 3


def _retryable(exc):
    """值得再试一次的暂时性网络故障：TLS 重置、超时、5xx。"""
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError, OSError)):
        return True
    return False


def web_origin_for(realm):
    """按区域给出网页控制台的域名：国内版或国际版。"""
    return WEB_ORIGIN_CN if str(realm or "").lower() == "cn" else WEB_ORIGIN


def fetch_account_profile(account, timeout=15):
    """网页控制台的账号资料 → (uid, 昵称)，其余字段不取。

    端点还会返回手机号等个人字段，这里只解析 uid 与昵称，敏感值不会进入日志、
    响应或存储。uid 与凭据不符时抛错，防止读到别的账号。
    """
    origin = web_origin_for(getattr(account, "realm", ""))
    req = wb_upstream_pool.Request(
        origin + PROFILE_PATH, method="GET",
        headers={
            "Authorization": "Bearer " + str(account.access_token or ""),
            "Accept": "application/json, text/plain, */*",
            "X-Client-Platform": "web",
            "Origin": WEB_ORIGIN_CN,
            "Referer": WEB_ORIGIN_CN + "/profile/account-settings",
            "User-Agent": WEB_USER_AGENT,
        },
    )
    with urlopen(req, timeout=timeout, proxy=getattr(account, "proxy", "")) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("profile response is not an object")
    uid = str(data.get("uid") or "")
    if uid and account.uid and uid != account.uid:
        raise RuntimeError("profile uid mismatch")
    nickname = data.get("nickname")
    return uid, nickname.strip() if isinstance(nickname, str) else ""


_OPENER_CACHE = {}
_OPENER_LOCK = threading.Lock()
_DIRECT_OPENER = wb_upstream_pool.build_opener()


def opener_for_proxy(proxy):
    """为单个出站代理构造（并缓存）urllib opener。

    空代理表示直连，返回 None，由调用方改用直连 opener。一个账号一个
    opener，使每个账号的流量固定走自己的出口 IP，不与他人共用轮换池。
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    with _OPENER_LOCK:
        opener = _OPENER_CACHE.get(proxy)
        if opener is None:
            opener = wb_upstream_pool.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            _OPENER_CACHE[proxy] = opener
        return opener


def urlopen(req, timeout=30, proxy=""):
    """urlopen，支持按账号指定代理，并复用长连接池。

    urllib 的 AbstractHTTPHandler.do_open 会写死 Connection: close，每个请求都要
    重新做 TCP+TLS 握手。默认改走 wb_upstream_pool：按（目标, 代理串）分池复用
    连接。连接池处理不了的请求（非 http(s) 目标、非 HTTP 代理、需要跟随的 3xx
    重定向）由 PoolBypass 回退到 urllib opener；面板「高级」关闭连接复用时也只走
    这条路径。两条路径发送的头名写法相同。
    """
    if wb_upstream_pool.enabled():
        try:
            return wb_upstream_pool.urlopen(req, timeout=timeout, proxy=proxy)
        except wb_upstream_pool.PoolBypass:
            pass
    opener = opener_for_proxy(proxy) or _DIRECT_OPENER
    return opener.open(req, timeout=timeout)


def http_json(url, data=None, method=None, headers=None, timeout=30,
              retries=3, backoff=1.0, log=None, proxy=""):
    """urlopen 加 JSON 解码，失败自动重试。

    国内网络与 CDN 边缘时常在 TLS 握手上断开（SSL: UNEXPECTED_EOF_WHILE_READING），
    重试一次几乎都能成功，所以所有上游请求都走这里。
    """
    attempts = max(1, int(retries or 1))
    last = None
    for attempt in range(1, attempts + 1):
        req = wb_upstream_pool.Request(
            url,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers=headers or {},
        )
        try:
            with urlopen(req, timeout=timeout, proxy=proxy) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt >= attempts or not _retryable(exc):
                break
            if log:
                log("network retry %d/%d after %s" % (attempt, attempts, exc))
            time.sleep(backoff * attempt)
    raise last

# 旧凭证的登录域名仍是 WorkBuddy 主机。对话请求的 X-Domain 改为使用所选
# 身份对应的域名；自定义域名与企业域名继续使用凭证里的值。
LEGACY_SESSION_DOMAINS = ("www.workbuddy.cn", "www.workbuddy.ai")

REALM_CONFIGS = {
    "intl": {
        "name": "国际版 (Global)",
        "chat_upstream": "https://www.workbuddy.ai",
        "billing_upstream": "https://www.workbuddy.ai",
        "origin": "https://www.workbuddy.ai",
        "domain": "www.workbuddy.ai",
        # 桌面端 UA 是三段式，末段是客户端内置 CLI 的版本；取值与
        # wb_identity.DESKTOP_UA_INTL 保持同一来源。
        "chat_ua": wb_identity.DESKTOP_UA_INTL,
        "billing_ua": "WorkBuddy/%s" % wb_identity.DESKTOP_VERSION_INTL,
        "info_filename": "workbuddy-desktop-ai.info",
        "cache_dir_name": ".workbuddy-ai",
        "has_checkin": False,
    },
    "cn": {
        "name": "国内版 (China)",
        "chat_upstream": "https://www.workbuddy.cn",
        "billing_upstream": "https://www.codebuddy.cn",
        "origin": "https://www.codebuddy.cn",
        "domain": "www.workbuddy.cn",
        # 国内版的 applicationName 与 productName 都是 WorkBuddy；版本号同样
        # 只从 wb_identity 取。
        "chat_ua": wb_identity.DESKTOP_UA_CN,
        "billing_ua": "WorkBuddy/%s" % wb_identity.DESKTOP_VERSION_CN,
        "info_filename": "workbuddy-desktop.info",
        "cache_dir_name": ".workbuddy",
        "has_checkin": True,
    },
}

AUTH_STATE_PATH = "/v2/plugin/auth/state"
AUTH_TOKEN_PATH = "/v2/plugin/auth/token"
LOGIN_ACCOUNT_PATH = "/v2/plugin/login/account"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
GET_RESOURCE_PATH = "/v2/billing/meter/get-user-resource"
RESOURCE_SUMMARY_PATH = "/billing/meter/get-user-resource-summary"
RESOURCE_FREE_PACKAGES_PATH = "/billing/meter/get-user-resource-free-packages"
RESOURCE_PAID_PACKAGES_PATH = "/billing/meter/get-user-resource-paid-packages"
CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
ENTERPRISE_USAGE_PATH = "/billing/meter/get-enterprise-user-usage"
#: 静态 API Key 的企业用量端点必须带 /v2 前缀；同一路径不带 /v2 会被 APISIX
#: 直接返回 401。OAuth 企业账号继续用原路径。
ENTERPRISE_USAGE_PATH_V2 = "/v2/billing/meter/get-enterprise-user-usage"
#: 静态 API Key 账号的 source 标记。
SOURCE_API_KEY = "api-key"

LOGIN_PENDING = 11217
LOGIN_TTL_SECONDS = 600
USER_AGENT = REALM_CONFIGS['intl']['chat_ua']
#: 国际版桌面端的版本号，从 wb_identity 取，保持单一来源。
DEFAULT_UA_VERSION = wb_identity.DESKTOP_VERSION_INTL

def get_realm_config(realm):
    return REALM_CONFIGS.get(realm) or REALM_CONFIGS["intl"]


def login_headers(realm, product, method="POST"):
    """auth/state 与 auth/token 的请求头，与所选官方客户端一致。

    桌面端由主进程直接发送 Node 请求，带 X-Custom-Channel-Id 与链路头；VS Code
    插件与 CLI 的 auth/state 还带 X-Domain 与 X-No-* 标记头，auth/token 的
    GET 只带链路头与 X-No-*。三种客户端在登录时都不带浏览器的 Origin、Referer 与
    X-Requested-With；GET 请求也没有 Content-Type。设备令牌（X-Device-Token）
    与设备标识（X-Device-Id-List）来自本机原生 SDK，网关没有对应来源，
    因此不发送。
    """
    product = wb_identity.normalize_product(product)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": wb_identity.identity_user_agent(realm, product),
        # 告诉服务端不要从会话里补认证信息：官方客户端在登录请求上固定发送。
        "X-No-Authorization": "true",
        "X-No-User-Id": "true",
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
    }
    if method == "GET":
        headers.pop("Content-Type", None)
    headers.update(wb_identity.login_trace_headers())
    # 插件与 CLI 的 auth/state 带 enterpriseHeaders 的 X-Domain；auth/token 不带。
    if method != "GET" and product in (wb_identity.PRODUCT_VSCODE,
                                        wb_identity.PRODUCT_CLI):
        headers["X-Domain"] = wb_identity.endpoint_for(realm, product)[1]
    if product == wb_identity.PRODUCT_DESKTOP:
        headers["X-Custom-Channel-Id"] = "unknown"
    return headers

def _jwt_claims(token):
    try:
        segment = str(token).split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment))
    except Exception:
        return {}

def jwt_exp(token):
    try:
        return int(_jwt_claims(token).get("exp") or 0)
    except Exception:
        return 0

def jwt_iat(token):
    try:
        return int(_jwt_claims(token).get("iat") or 0)
    except Exception:
        return 0

def jwt_uid(token):
    return str(_jwt_claims(token).get("sub") or "")

def jwt_issuer(token):
    return str(_jwt_claims(token).get("iss") or "")

def normalize_epoch(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number > 1e11:
        number /= 1000.0
    return int(number)

#: token / 域名里能认出区域的字样。国内版有三套出口域名：
#: copilot.tencent.com、codebuddy.cn、workbuddy.cn（新版国内客户端的 JWT issuer
#: 就是 https://www.workbuddy.cn/…），三个都得认；只认前两个会把 workbuddy.cn
#: 的国内账号判成国际版。
CN_REALM_MARKERS = ("copilot.tencent.com", "codebuddy.cn", "workbuddy.cn")
INTL_REALM_MARKERS = ("workbuddy.ai", "codebuddy.ai")
REALM_MARKERS = {"cn": CN_REALM_MARKERS, "intl": INTL_REALM_MARKERS}


def normalise_disabled_models(raw):
    """把「人工禁用的模型名单」归一成干净的 list[str]。

    供账号档加载与面板写入两条路共用，保证「内存里的判断」和「落盘的内容」
    口径一致：保留非空字符串、按小写归一（与 is_model_banned 同口径）、去重、
    保持输入顺序。非列表输入一律当作「没配」返回空列表——坏数据不至于让账号
    加载失败。
    """
    out = []
    if not isinstance(raw, (list, tuple)):
        return out
    seen = set()
    for mid in raw:
        if not isinstance(mid, str):
            continue
        m = mid.strip().lower()
        if not m or m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out

#: 上游用一个远超任何真实计费周期的抵扣截止时间表示「不会过期」：免费版与
#: 体验包的 DeductionEndTime 落在 2034/2035 年，而真实包周期是 14 天或 1 个月。
#: 超过这个天数就按「不过期」处理，不再参与到期倒计时与临期判定。
EXPIRY_SENTINEL_DAYS = 730


def deduction_end_text(acc):
    """包的「抵扣截止时间」——积分到这个点就不能再抵扣，即作废时刻。

    取上游的 DeductionEndTime（epoch 毫秒）。上游没给就返回空串，调用方
    回退到 CycleEndTime。Bonus Pack / 裂变包 / 体验版的两个字段一致，但免费包
    这类包的 CycleEndTime 只是每月的计费周期边界，两者能差 8 年——所以到期要
    认这个字段。
    """
    raw = acc.get("DeductionEndTime")
    if raw in (None, "", 0, "0"):
        return ""
    try:
        stamp = float(raw)
    except (TypeError, ValueError):
        return ""
    if stamp > 1e11:
        stamp /= 1000.0
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
    except (OSError, ValueError):
        return ""


def realm_evidence(token, domain=None):
    """从 token / 域名里看区域，看不出返回 None（不要瞎猜成 intl）。

    先看 token 自己的 issuer，再看域名：域名是客户端随手记下来的字段，可能过期
    或干脆是另一边的（同一台机器切区登录过就会这样），token 才是要拿去请求的
    那一串，冲突时以它为准。
    """
    iss = jwt_issuer(token).lower()
    if any(marker in iss for marker in CN_REALM_MARKERS):
        return "cn"
    if any(marker in iss for marker in INTL_REALM_MARKERS):
        return "intl"
    dom = str(domain or "").lower()
    if any(marker in dom for marker in CN_REALM_MARKERS):
        return "cn"
    if any(marker in dom for marker in INTL_REALM_MARKERS):
        return "intl"
    return None

def detect_realm_from_token(token, domain=None):
    return realm_evidence(token, domain) or "intl"

def domain_for_realm(realm, domain):
    """把域名归到所在区域：域名写着另一个区域时换成该区域的规范域名。

    出站请求的 X-Domain 头跟着它走，区域既然以 token 为准，就不能让一个过期
    域名再把请求带回另一边的出口。域名本身看不出区域时原样保留。
    """
    dom = str(domain or "").strip()
    other = "intl" if realm == "cn" else "cn"
    if dom and not any(marker in dom.lower() for marker in REALM_MARKERS[other]):
        return dom
    return get_realm_config(realm)["domain"]

def desktop_effective_realm(hint, token, domain=None):
    """桌面凭据归哪个区域：token / 域名说了算，文件名只作兜底。

    文件名（workbuddy-desktop.info → 国内、workbuddy-desktop-ai.info → 国际）
    只是客户端两套安装的默认约定：同一个文件里完全可能登录另一个区域的账号
    （切区登录就是这么用的）。按 token 判定才不会把国内账号塞进国际版列表——
    那种账号导入后每个请求都会打到错区域的出口上。
    """
    evidence = realm_evidence(token, domain)
    if evidence:
        return evidence
    return hint if hint in ("intl", "cn") else "intl"

_DEVICE_TOKEN_CACHE = {"key": None, "token": "", "at": 0.0}
_DEVICE_TOKEN_LOCK = threading.Lock()
_DEVICE_TOKEN_TTL = 300
_DEVICE_TOKEN_MAX_BYTES = 1024


def resolve_device_token(accounts_dir):
    """X-Device-Token 的兜底来源：字面值，或桌面端令牌文件。

    与面板的 device_token.go 同一口径：桌面客户端把令牌写进文件，网关读取
    （上限 1 KB，去空白）并缓存 5 分钟，标头热路径因此不用反复读盘。
    读取失败退化为空令牌，不阻断请求。
    """
    try:
        cfg = wb_settings.upstream_config(accounts_dir)
    except Exception:
        return ""
    literal = str(cfg.get("device_token") or "").strip()
    path = str(cfg.get("device_token_file") or "").strip()
    key = literal or path
    if not key:
        return ""
    now = time.time()
    with _DEVICE_TOKEN_LOCK:
        if (_DEVICE_TOKEN_CACHE["key"] == key
                and now - _DEVICE_TOKEN_CACHE["at"] < _DEVICE_TOKEN_TTL):
            return _DEVICE_TOKEN_CACHE["token"]
    token = literal
    if not token and path:
        try:
            if os.path.getsize(path) <= _DEVICE_TOKEN_MAX_BYTES:
                with open(path, encoding="utf-8") as fh:
                    token = fh.read().strip()
        except Exception:
            token = ""
    with _DEVICE_TOKEN_LOCK:
        _DEVICE_TOKEN_CACHE.update({"key": key, "token": token, "at": now})
    return token


def next_local_4am(now=None):
    """下一个本地 04:00 的 epoch 秒。

    国内版积分的每日重置在夜间完成，欠费停机等的就是这道 04:00 的墙。
    已过 04:00 的时刻顺延到明天，同一截止时刻不会被重复使用。
    """
    now = time.time() if now is None else now
    lt = time.localtime(now)
    stamp = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                         4, 0, 0, 0, 0, -1))
    if stamp <= now:
        stamp += 86400
    return stamp


# 账号级失败治理：反复被软限流的凭据按指数退避，不再每个请求都重试；连续硬失败
# 触发熔断，未知失败先降级一段时间。计数在 Account 上，这里只把次数换算成秒
# （与面板项目的池语义一致）。
SOFT_RATE_BASE = 600.0          # 账号级 429 首次冷却：10 分钟
SOFT_RATE_MAX = 7200.0          # 逐次翻倍，最高 2 小时
# 无范围的 429 只在该模型上给一个窗口：响应体没给重置时刻，看不出限的是凭据还是
# 单个模型，所以窗口更短，且不超过凭据阶梯的第一档。裸的 usage exceeds frequency
# limit 响应是上游最常发的 429，不能让它在凭据上停两小时。
SOFT_RATE_UNVERIFIED_BASE = 60.0
SOFT_RATE_UNVERIFIED_MAX = 600.0
BREAKER_THRESHOLD = 3           # 连续硬失败到几次触发熔断
BREAKER_COOLDOWN = 1800.0       # 首次熔断窗口：30 分钟
BREAKER_COOLDOWN_MAX = 21600.0  # 逐次翻倍，最高 6 小时
# 上面那个「连续」必须有时间含义：fails 只在成功时归零，相隔很久的失败会凑成
# 「连续 3 次」，把本来正常的账号关掉，而且次数越高关得越久。两次失败间隔超过
# 这个窗口就重新从 1 数起。可用 WB_FAIL_WINDOW 覆盖，设 0 关闭衰减（按累计次数
# 计）。
try:
    FAIL_WINDOW = max(0.0, float(os.environ.get("WB_FAIL_WINDOW") or 600.0))
except (TypeError, ValueError):
    FAIL_WINDOW = 600.0
DEGRADE_THRESHOLD = 5           # 连续未知失败到几次触发降级
DEGRADE_COOLDOWN = 600.0        # 首次降级窗口：10 分钟
DEGRADE_COOLDOWN_MAX = 7200.0   # 逐次翻倍，最高 2 小时


def _exponential_backoff(count, base, cap, offset):
    """base * 2 ** (count - offset)，封顶 cap，指数最多取 20 步。"""
    step = max(0, int(count) - int(offset))
    return min(float(base) * (2 ** min(step, 20)), float(cap))


def soft_backoff(streak):
    """`streak` 次连续账号级软限流对应的冷却秒数。"""
    return _exponential_backoff(streak, SOFT_RATE_BASE, SOFT_RATE_MAX, 1)


def unverified_soft_backoff(streak):
    """`streak` 次无范围软限流（单个模型）的冷却秒数。

    次数增加仍然升级，但从 60 秒起步，最高只到凭据阶梯的第一档（600 秒），
    见 SOFT_RATE_UNVERIFIED_MAX。
    """
    return _exponential_backoff(streak, SOFT_RATE_UNVERIFIED_BASE,
                                SOFT_RATE_UNVERIFIED_MAX, 1)


def breaker_backoff(fails):
    """`fails` 次连续硬失败对应的熔断冷却秒数。"""
    return _exponential_backoff(fails, BREAKER_COOLDOWN, BREAKER_COOLDOWN_MAX,
                                BREAKER_THRESHOLD)


def degrade_backoff(fails):
    """`fails` 次连续未知失败对应的降级窗口秒数。"""
    return _exponential_backoff(fails, DEGRADE_COOLDOWN, DEGRADE_COOLDOWN_MAX,
                                DEGRADE_THRESHOLD)

def _access_token_value(raw):
    """取出可用的 access token；形状不对就地抛错。

    桌面端会把令牌存成加密信封（$wbEncrypted），也有把整段 JSON 当字符串塞进来
    的文件，这些都不是网关能用的 JWT；导入、扫描与导入行校验共用这一处判断。
    """
    if isinstance(raw, dict) or "$wbEncrypted" in str(raw or ""):
        raise ValueError("access token is encrypted ($wbEncrypted) - "
                         "add this account with OAuth instead")
    token = str(raw or "").strip()
    if not token:
        raise ValueError("no accessToken")
    if wb_identity.is_api_key_token(token):
        return token
    if token.count(".") != 2:
        raise ValueError("accessToken is not a JWT")
    return token


DEFAULT_ACCOUNT_PRIORITY = 100
#: 账号文件可手改，超出范围的优先级夹回边界值，选择顺序不受影响。
MAX_ACCOUNT_PRIORITY = 9999
#: 单账号单模型的并发上限：同一账号的每个模型各自计数，0 表示不限。
MAX_ACCOUNT_CONCURRENCY = 1000
#: 手动调度权重上限，面板与服务端共用这个值。
MAX_ACCOUNT_WEIGHT = 100
#: 评分分配按最近一小时被选中的次数判断账号忙不忙，由 mark_pick() 记下。
LOAD_WINDOW_SECONDS = 3600
#: 账号备注的最长字数，面板与导入行共用同一个上限。
MAX_ACCOUNT_NOTE = 100

#: 套餐周期时刻的布局与时区，取上游同款：腾讯下发的是不带时区标记的 UTC+8 墙钟
#: 字符串，按本机时区解析会在非 UTC+8 的机器上整体错开数小时，且界面上看不出来；
#: 上游用 time.ParseInLocation(layout, s, UTC+8)，这里等价。
PACKAGE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PACKAGE_TIME_TZ = timezone(timedelta(hours=8))
#: 令牌剩余寿命低于这个值（秒）就认为需要刷新，ready() 与 refresh() 用同一个阈值。
TOKEN_REFRESH_MARGIN = 120


def _normalise_priority(value):
    """把存储的优先级夹回合法范围；无法解析时取默认值。"""
    if isinstance(value, bool):
        return DEFAULT_ACCOUNT_PRIORITY
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ACCOUNT_PRIORITY
    return max(0, min(MAX_ACCOUNT_PRIORITY, number))


def _stored_note(value):
    """凭证文件与导入行里的备注：不是字符串当作没有，超长按上限截断。"""
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_ACCOUNT_NOTE]


def parse_package_time(value):
    """套餐周期时刻（epoch 秒），用于 CycleStartTime 与 CycleEndTime。

    字段缺失、为空、格式不对都返回 None，不连累整次余额读取；能解析时必须先
    贴上 UTC+8 再取时间戳，否则同一份响应在不同时区的机器上得出不同时刻。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        naive = datetime.strptime(text, PACKAGE_TIME_FORMAT)
    except ValueError:
        return None
    return int(naive.replace(tzinfo=PACKAGE_TIME_TZ).timestamp())


def _infer_login_product(platform, source, product):
    """旧凭证没有 loginProduct 时，从登录平台与来源推断签发渠道。"""
    if source == "desktop-app":
        return wb_identity.PRODUCT_DESKTOP
    key = str(platform or "").strip().lower()
    if key == "vscode":
        return wb_identity.PRODUCT_VSCODE
    if key == "cli":
        return wb_identity.PRODUCT_CLI
    if key in ("workbuddy", "workbuddy-ai"):
        return wb_identity.PRODUCT_DESKTOP
    if source in ("import", "cockpit"):
        return wb_identity.normalize_product(product)
    return wb_identity.normalize_product(product)


class Account(object):
    def __init__(self, data, path=None):
        data = data or {}
        self.path = path
        # 这份数据带了哪些字段：AccountPool.add() 用它判断替换账号时哪些面板
        # 设置该沿用旧值（登录数据里没有那些键）。
        self.payload_keys = frozenset(data.keys())
        # 凭证文件所在目录：发请求时解析可选的 X-Device-Token 用。
        self.accounts_dir = os.path.dirname(path) if path else ""
        token = str(data.get("accessToken") or "")
        # 静态 API Key 与 OAuth token 是两条鉴权路径：前者不是 JWT，没有
        # refreshToken / 过期时间 / 个人身份。识别只看 ck_/pt_ 前缀；显式
        # apiKey 字段用于导入时做一致性校验，标了 apiKey 但 accessToken
        # 没有 ck_/pt_ 前缀时直接报错。
        self.is_api_key = wb_identity.is_api_key_token(token)
        if data.get("apiKey") is True and not self.is_api_key:
            raise ValueError("apiKey is set but accessToken is not a ck_/pt_ key")
        uid = str(data.get("uid") or "").strip()
        if not uid and self.is_api_key:
            derived = wb_identity.api_key_id(token)
            uid = ("apikey-" + derived) if derived else ""
        self.uid = uid or jwt_uid(token)
        # 国内桌面端的昵称存成加密信封（{"$wbEncrypted": ...}），不是纯字符串
        # 就丢弃，改显示 uid 前缀。
        raw_nickname = data.get("nickname")
        if isinstance(raw_nickname, str) and "$wbEncrypted" not in raw_nickname:
            self.nickname = raw_nickname.strip()
        else:
            self.nickname = ""
        self.domain = str(data.get("domain") or "")
        if self.is_api_key:
            # 静态 Key 只在国内版出口有效，凭据文件里写着 intl 也不能直接采用。
            self.realm = "cn"
        else:
            self.realm = str(data.get("realm") or detect_realm_from_token(token, self.domain))
        if not self.domain:
            self.domain = get_realm_config(self.realm)["domain"]
        self.platform = str(data.get("platform") or "CLI")
        # 出站身份读回凭证文件里保存的值：面板手动切换与 429 自动切换都会经由
        # save() 写入（to_dict() 序列化的是当下身份），重启后继续使用上次实际
        # 生效的通道；普通账号缺失或值不合法时 normalize_product() 回退到
        # workbuddy，静态 API Key 默认 CLI。
        self.product = ("cli" if self.is_api_key and not data.get("product")
                        else wb_identity.normalize_product(data.get("product")))
        self.enterprise_id = str(data.get("enterpriseId") or "")
        self.access_token = token
        self.refresh_token = str(data.get("refreshToken") or "")
        self.expires_at = (0 if self.is_api_key
                           else normalize_epoch(data.get("expiresAt")) or jwt_exp(token))
        # 令牌签发时刻：面板的有效期进度按「到期 - 签发」取满格；凭证文件里没有
        # 这个字段时从令牌自身读，读不到就留 0。
        self.issued_at = (0 if self.is_api_key
                          else normalize_epoch(data.get("issuedAt") or jwt_iat(token)))
        # 企业账号登录时切到企业空间，企业 id 只写在 token 里，凭据文件的
        # enterpriseId 往往是空的。不回填会让每个请求都带上 X-No-Enterprise-Id: 1。
        if not self.enterprise_id and not self.is_api_key:
            self.enterprise_id = self.jwt_enterprise_id(token)
        self.added_at = data.get("addedAt") or time.time()
        self.source = str(data.get("source")
                          or (SOURCE_API_KEY if self.is_api_key else "oauth"))
        explicit_login_product = str(data.get("loginProduct") or "").strip()
        # 签发渠道独立于当前出站身份：切到别的 client 后，刷新仍回登录时的渠道。
        self.login_product = (wb_identity.normalize_product(explicit_login_product)
                              if explicit_login_product
                              else _infer_login_product(self.platform, self.source,
                                                        self.product))
        self.proxy_slot = str(data.get("proxySlot") or "").strip()
        self.proxy_legacy = str(data.get("proxy") or "").strip()
        # 运行期解析值，由 AccountPool.apply_proxy_slots() 重新计算。
        self.proxy = self.proxy_legacy
        self.enabled = data.get("enabled", True)
        # 调用优先级：数字最小的先被选中，数字相同保持原轮询顺序；默认 100，
        # 没调过的账号池保持默认行为。会话粘性优先于优先级。
        self.priority = _normalise_priority(data.get("priority"))
        # 面板备注：跟着凭证文件走，重新登录与桌面端导入不会带上它，
        # AccountPool.add() 因此把它列进 KEEP_ON_REPLACE_FIELDS。
        self.note = _stored_note(data.get("note"))
        # 单账号单模型的并发上限：0 表示不限。计数在内存里，重启即清空。
        self.concurrency_limit = _stored_concurrency_limit(data.get("concurrencyLimit"))
        # 手动调度权重（面板按账号设置，1 = 不干预）。随凭证文件持久化。
        try:
            self.weight = max(1, min(MAX_ACCOUNT_WEIGHT, int(data.get("weight") or 1)))
        except (TypeError, ValueError):
            self.weight = 1
        # 该账号人工禁用的模型名单：随凭证文件持久化，与自动节流互不干扰。
        raw_deny = data.get("disabledModels")
        self.disabled_models = normalise_disabled_models(raw_deny)
        # 运行期错误状态，不从凭证文件恢复：陈旧的上游答复不该在重启后复活。
        self.last_error = ""
        self.last_error_detail = ""
        self.cooldown_until = 0.0
        # 按模型分别节流。上游限流（code 6004「usage exceeds frequency limit」）
        # 只针对一个账号的单个模型，其他模型照常可用。只冷却出问题的模型：
        # 连坐整个账号的话，单个被限流的模型会拖死账号的全部请求。刻意只存
        # 运行期（不持久化）：见 VOLATILE_FIELDS。
        self.model_cooldowns = {}
        # 每个模型冷却窗口是**什么时候挂上的**（model -> epoch）。model_cooldowns
        # 只留恢复时刻，起始时刻以前被丢掉：面板因此无法如实说「限流时刻」，只能
        # 用「—」占位，把 60s 的短时 parking 和几小时的 6004 撞线画成一样。与
        # model_cooldowns 同款运行时态：重启即清零（6004 的撞线时刻另有持久化的
        # model_caps["at"]）。
        self.model_cooldowns_at = {}
        # 402（欠费）：一直停到下一个本地 04:00，不做短冷却，免得整天反复
        # 重试一个空账号。与其他节流窗口一样只存运行期；余额刷新看到积分
        # 恢复会提前解除。
        self.balance_until = 0.0
        # 账号级失败连击，换算见上面的 *_backoff() 辅助函数。都只存运行期，
        # 与其他节流窗口一样：重启即清空，账号重新从零计数。
        self.soft_streak = 0
        # 无范围软限流 429 单独计数：两条阶梯各自独立，不互相升级
        # （见 note_unscoped_rate）。
        self.unscoped_streak = 0
        self.fails = 0
        self.degrade_count = 0
        self.breaker_until = 0.0
        self.degrade_until = 0.0
        # 最近一次记失败的时刻：FAIL_WINDOW 用它判断「连续」是否还成立。
        self.last_fail_at = 0.0
        # 最近一次软限流（429）的时刻：两条软限流阶梯用同一个时间窗。
        self.last_soft_at = 0.0
        self.credits = data.get("credits") or None
        # 一次余额快照的到期列表：credits 整体替换时失效，避免每次列表接口
        # 与评分选号都重新遍历、排序套餐。
        self._expiry_cache = None
        # 每日到期目标只随时间缓慢变化：按余额快照与分钟缓存，选号热路径
        # 不必每次重新遍历到期列表。
        self._target_cache = None
        # 评分分配用的运行时数据：最近一小时被选中的时刻与每天消耗的积分
        # （由 wb_proxy 折叠 usage 日志后推下来），都不写回凭证文件。
        self._pick_stamps = collections.deque()
        self._pick_lock = threading.Lock()
        self.credit_rate_per_day = 0.0
        self.last_checkin = data.get("lastCheckin") or None
        self.last_daily_chat = data.get("lastDailyChat") or None
        # 国内版每天一次的对话活跃上报上次成功的时刻。
        self.last_activity_report = data.get("lastActivityReport") or None
        # 上游窗口额度用满（429 / code 6004）的持久记录：model -> {at, reset}。
        self.model_caps = {}
        for mid, entry in (data.get("modelCaps") or {}).items():
            if not isinstance(entry, dict):
                continue
            try:
                at = float(entry.get("at") or 0)
                reset = float(entry.get("reset") or 0)
            except (TypeError, ValueError):
                continue
            if at > 0 and reset > 0:
                self.model_caps[str(mid)] = {"at": at, "reset": reset}
        # 429 带明确恢复时间的普通限流窗口（非 6004）：model -> {at, until}。
        # 与 model_caps 分开：cap 表示「额度用满」，这里只是上游给的恢复时刻。
        self.model_holds = {}
        for mid, entry in (data.get("modelHolds") or {}).items():
            if not isinstance(entry, dict):
                continue
            try:
                at = float(entry.get("at") or 0)
                until = float(entry.get("until") or 0)
            except (TypeError, ValueError):
                continue
            if at > 0 and until > 0:
                self.model_holds[str(mid)] = {"at": at, "until": until}
        # 低余额保护：余额降到这个值时账号停止接单，避免收到上游提醒短信。
        # 由 AccountPool.apply_reserve_credits() 设置；0 表示关闭。
        self.reserve_credits = 0
        # 临期积分优先：最早到期的积分包进入这个天数时优先派单。0 表示关闭。
        self.expiring_window_days = 0
        # 每日 Token 限额：今日用量达到设定值时账号停止接单。
        # daily_tokens_today 在代理折叠用量日志之前保持 None。
        self.daily_token_limit = 0
        self.daily_tokens_today = None
        # 每日积分限额：只限制付费模型，免费模型照常接单。
        self.daily_credit_limit = 0
        self.daily_credits_today = None
        # 当前区域标记为免费的模型 id；未知模型按付费处理。
        self.free_models = frozenset()
        # 单账号单模型每日 Token 限额：只停这一个模型。0 表示关闭。
        self.model_daily_token_limit = 0
        self.model_daily_tokens = None
        # 串行化 Token 刷新与凭证文件写入，避免新 Token 被旧快照覆盖。
        self._refresh_lock = threading.Lock()
        self._save_lock = threading.Lock()
        # 串行化 fetch_credits()，避免并发查询重复消耗计费调用。
        self._credits_lock = threading.Lock()
        # 面板会快照这份状态，请求线程同时在更新它。与 _refresh_lock 分开
        # 持锁：那把锁横跨网络请求。
        self._throttle_lock = threading.Lock()
        # 在途请求计数：键是模型名；独立锁，面板读计数时不挡请求线程。
        self._concurrency_lock = threading.Lock()
        self._in_flight = {}
        # 删除标记：面板删掉账号后，请求线程手里的旧引用还会走到 save()，
        # 没有它就会把刚删掉的凭证文件重新写出来。
        self.deleted = False

    def to_dict(self):
        """账号的完整视图，含运行期状态。

        导出文档以它为基础，因此带上运行期错误与冷却供检查。本地凭证文件
        不能带这些：save() 写盘前剥掉 RUNTIME_ONLY_FIELDS。
        """
        return {
            "uid": self.uid,
            "nickname": self.nickname,
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "product": self.product,
            "loginProduct": self.login_product,
            "enterpriseId": self.enterprise_id,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": self.expires_at,
            "addedAt": self.added_at,
            "source": self.source,
            "apiKey": bool(self.is_api_key),
            "proxySlot": self.proxy_slot,
            "proxy": self.proxy_legacy,
            "enabled": self.enabled,
            "priority": self.priority,
            "note": self.note,
            "concurrencyLimit": self.concurrency_limit,
            "weight": self.weight,
            "disabledModels": list(self.disabled_models),
            "lastError": self.last_error,
            "cooldownUntil": self.cooldown_until,
            "credits": self.credits,
            "lastCheckin": self.last_checkin,
            "lastDailyChat": self.last_daily_chat,
            "lastActivityReport": self.last_activity_report,
            # 持久化：6004 的恢复时间是上游给的事实，重启后仍然成立（见 __init__）。
            "modelCaps": {mid: dict(entry) for mid, entry in self.model_caps.items()},
            # 普通 429 带恢复时间的窗口同样持久化，重启后不重新撞线。
            "modelHolds": {mid: dict(entry) for mid, entry in self.model_holds.items()},
        }

    def _throttle_snapshot(self, now):
        """返回账号冷却与各模型冷却的一致视图。"""
        with self._throttle_lock:
            error = self.last_error
            detail = self.last_error_detail
            deadline = max(self.cooldown_until, self.balance_until, self.breaker_until, self.degrade_until)
            active = [(model, until) for model, until in self.model_cooldowns.items()
                      if until > now]
        active.sort(key=lambda pair: (pair[1], pair[0]))
        models = []
        for model, until in active:
            entry = {"model": model, "expiresAt": int(until)}
            # 这个窗口是何时挂上的：面板据此把「限流时刻」如实画出来，缺了它
            # 只能用「—」占位（临时 429 与 6004 撞线都由此有真实的时间语义）。
            started = self.model_cooldowns_at.get(model)
            if started:
                entry["startedAt"] = int(started)
            # 撞线时间只在 cap 还在恢复期内时带上：它标明这次禁用属于
            # 「额度用满」（6004），面板据此把两段时间分开显示。
            if self.cap_until(model, now) > 0:
                entry["cappedAt"] = int(self.model_caps[model]["at"])
            models.append(entry)
        return error, deadline, models, detail

    def model_cooldowns_snapshot(self):
        """生效中的模型冷却，按恢复时间排序（epoch 秒）。"""
        return self._throttle_snapshot(time.time())[2]

    def credit_expiries(self):
        """套餐到期列表 [{at, amount, name}]，按到期时间升序。

        只收余额大于 0 且解析出到期时刻的套餐，余额为 0 的套餐列出来只会让人
        误以为还有额度。列表按余额快照缓存：积分刷新会整体替换 credits 对象，
        缓存自然失效；调用方只读，不要原地修改返回的列表。
        """
        source = self.credits
        cached = self._expiry_cache
        if cached is not None and cached[0] is source:
            return cached[1]
        credits = source if isinstance(source, dict) else {}
        packages = credits.get("packages")
        rows = []
        if isinstance(packages, list):
            for pack in packages:
                if not isinstance(pack, dict):
                    continue
                # 免费包与体验包的抵扣截止远在数年后，企业额度的周期结束是
                # 重置额度，两者都不该进入“快到期”的排序与评分。
                if pack.get("no_expiry") or pack.get("package_code") == "enterprise":
                    continue
                at = pack.get("expireAt")
                amount = pack.get("remain")
                if at is None or isinstance(amount, bool) or not isinstance(amount, (int, float)):
                    continue
                if amount <= 0:
                    continue
                rows.append({"at": int(at), "amount": amount,
                             "name": str(pack.get("name") or "")})
        rows.sort(key=lambda row: row["at"])
        self._expiry_cache = (source, rows)
        return rows

    def mark_pick(self, now=None):
        """记录一次被选中；只保留最近一小时。"""
        now = time.time() if now is None else now
        with self._pick_lock:
            stamps = self._pick_stamps
            stamps.append(now)
            while stamps and now - stamps[0] > LOAD_WINDOW_SECONDS:
                stamps.popleft()

    def recent_load(self, now=None):
        """最近一小时被选中的次数。"""
        now = time.time() if now is None else now
        with self._pick_lock:
            stamps = self._pick_stamps
            while stamps and now - stamps[0] > LOAD_WINDOW_SECONDS:
                stamps.popleft()
            return len(stamps)

    def credit_target_per_day(self, now=None):
        """到期前每天至少要消耗掉的积分；还没查过余额时返回 None。

        对每个到期时刻，把在它之前到期的余额加总后除以离它的天数，取最大值；
        已过期的套餐不计入；查过余额但没有未到期的套餐时返回 0。
        """
        credits = self.credits if isinstance(self.credits, dict) else {}
        if not isinstance(credits.get("packages"), list):
            return None
        now = time.time() if now is None else now
        bucket = int(now // 60)
        cached = self._target_cache
        if cached is not None and cached[0] is credits and cached[1] == bucket:
            return cached[2]
        best = cumulative = 0.0
        for row in self.credit_expiries():
            if row["at"] <= now:
                continue
            cumulative += row["amount"]
            days = max((row["at"] - now) / 86400.0, 1.0 / 24.0)
            best = max(best, cumulative / days)
        self._target_cache = (credits, bucket, best)
        return best

    def public(self):
        exp = self.expires_at or jwt_exp(self.access_token)
        now = time.time()
        last_error, deadline, models, last_error_detail = self._throttle_snapshot(now)
        return {
            "uid": self.uid,
            "nickname": self.nickname or (self.uid[:8] if self.uid else "?"),
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "product": self.product,
            "loginProduct": self.login_product,
            "enterpriseId": self.enterprise_id,
            "enabled": bool(self.enabled),
            "priority": int(self.priority),
            "note": self.note,
            "concurrencyLimit": int(self.concurrency_limit),
            "weight": int(self.weight),
            "disabledModels": list(self.disabled_models),
            "source": self.source,
            "apiKey": bool(self.is_api_key),
            "credentialMask": self.credential_mask(),
            "proxySlot": self.proxy_slot,
            "proxy": self.proxy,
            "expiresAt": exp,
            "issuedAt": self.issued_at,
            "expiresIn": _human_delta(exp - time.time()) if exp else None,
            "hasRefreshToken": bool(self.refresh_token),
            "activeRequests": self.active_requests,
            "lastError": last_error,
            "lastErrorDetail": last_error_detail or None,
            "inCooldown": deadline > now,
            "cooldownFor": round(max(0.0, deadline - now)) or None,
            "modelCooldowns": models,
            "softStreak": int(self.soft_streak),
            "unscopedStreak": int(self.unscoped_streak),
            "breakerFor": round(max(0.0, self.breaker_until - now)) or None,
            "degradeFor": round(max(0.0, self.degrade_until - now)) or None,
            # 连续失败计数（离熔断还差几次）与最近一次失败的时间：面板用它解释
            # 「为什么这个账号被关了」，也是「一键测试」判定成功/失败的依据。
            "fails": int(self.fails),
            "failWindow": int(FAIL_WINDOW),
            "lastFailFor": (round(max(0.0, now - self.last_fail_at)) if self.last_fail_at else None),
            "addedAt": self.added_at,
            "file": os.path.basename(self.path) if self.path else None,
            "credits": self.credits,
            "creditExpiries": self.credit_expiries(),
            "reserveCredits": int(self.reserve_credits or 0),
            "reserveBlocked": self.reserve_blocked(),
            "dailyTokenLimit": int(self.daily_token_limit or 0),
            "dailyTokensToday": (int(self.daily_tokens_today)
                                 if isinstance(self.daily_tokens_today, int)
                                 else None),
            "dailyLimitBlocked": self.daily_limit_blocked(),
            "dailyCreditLimit": int(self.daily_credit_limit or 0),
            "dailyCreditsToday": (round(float(self.daily_credits_today), 2)
                                  if isinstance(self.daily_credits_today, (int, float))
                                  else None),
            "creditLimitReached": self.credit_limit_reached(),
            "modelDailyTokenLimit": int(self.model_daily_token_limit or 0),
            "modelDailyTokens": ({str(k): int(v)
                                  for k, v in self.model_daily_tokens.items()}
                                 if isinstance(self.model_daily_tokens, dict)
                                 else None),
            "lastCheckin": self.last_checkin,
            "lastDailyChat": self.last_daily_chat,
            "lastActivityReport": self.last_activity_report,
            "canCheckin": self.has_personal_program(),
            "canDailyChat": self.realm == "intl",
            "canReportActivity": self.has_personal_program(),
            "machineId": derive_id(self.uid, "machine"),
            "sessionId": session_id(self.uid),
        }

    def credential_mask(self):
        """静态 API Key 的脱敏展示；OAuth token 不回显。"""
        if not self.is_api_key:
            return ""
        head = str(self.access_token or "").split(".", 1)[0]
        return (head + ".***") if head else "***"

    def save(self, directory):
        os.makedirs(directory, exist_ok=True)
        safe_uid = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.uid or "")).strip("_ ")
        name = (safe_uid or uuid.uuid4().hex) + ".json"
        path = os.path.abspath(os.path.join(directory, name))
        if not path.startswith(os.path.abspath(directory)):
            raise ValueError("invalid path for account save")
        # 临时文件名唯一，再加每账号一把锁：否则两个线程保存同一账号会共用
        # 临时文件，后写完的 os.replace() 以 ENOENT 失败。
        with self._save_lock:
            if self.deleted:
                return None
            tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
            try:
                payload = self.to_dict()
                # 运行期错误与冷却状态不进凭证文件；写进去的话，早已恢复的
                # 429 标记会在每次重启后复活（见 RUNTIME_ONLY_FIELDS）。
                for field in RUNTIME_ONLY_FIELDS:
                    payload.pop(field, None)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
        self.path = path
        return path

    def persist(self):
        """把账号写回磁盘；凭据文件还没落盘或目录已消失时跳过。"""
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))

    def delete(self):
        with self._save_lock:
            self.deleted = True
            if self.path and os.path.exists(self.path):
                os.remove(self.path)

    def reserve_blocked(self):
        """低余额保护应当让这个账号闲置时返回 True。

        只有已知的余额才会拦截；从未查过余额的账号照常可用。
        """
        reserve = int(self.reserve_credits or 0)
        if reserve <= 0:
            return False
        credits = self.credits
        if not isinstance(credits, dict):
            return False
        remain = credits.get("remain")
        if remain is None:
            return False
        try:
            remain = int(remain)
        except (TypeError, ValueError):
            return False
        return remain <= reserve

    def soonest_expiring_days(self):
        """最早到期且仍有余额的积分包还剩多少天到期。

        说不清时返回 None：没有余额数据、没有可用余额的包，或上游从未给出
        截止时间。

        上游不会真正过期的包要跳过。企业额度到期时重置额度，余额不作废；
        免费包与体验包的抵扣截止远在数年后。两者在生成处都标了 no_expiry，
        把它们当成到期会让这类账号永远显示「即将到期」：免费包甚至每到月底
        都告急，它的计费周期在滚动，积分却一直可用。
        """
        credits = self.credits
        if not isinstance(credits, dict):
            return None
        soonest = None
        for package in (credits.get("packages") or []):
            if not isinstance(package, dict):
                continue
            # no_expiry 是通用规则；package_code 检查是额外兜底，覆盖标志位
            # 出现之前写下的余额快照，在下次刷新前它们还留在盘上。
            if package.get("no_expiry"):
                continue
            if package.get("package_code") == "enterprise":
                continue
            if package.get("is_expired"):
                continue
            days = package.get("days_left")
            remain = package.get("remain")
            if not isinstance(days, (int, float)) or days < 0:
                continue
            if not isinstance(remain, (int, float)) or remain <= 0:
                continue
            if soonest is None or days < soonest:
                soonest = float(days)
        return soonest

    def _expiring_rows(self, within_days, now=None):
        """窗口内到期的 (距今天数, 剩余额度)，按天数升序。

        与 soonest_expiring_days() 同一套过滤：认抵扣截止时间，跳过企业额度、
        no_expiry 与已经过期的包。优先用 expireAt（秒），旧快照只有 days_left
        时退回它。
        """
        try:
            window = float(within_days or 0)
        except (TypeError, ValueError):
            return []
        if window <= 0:
            return []
        credits = self.credits
        if not isinstance(credits, dict):
            return []
        now = time.time() if now is None else now
        rows = []
        for package in (credits.get("packages") or []):
            if not isinstance(package, dict):
                continue
            if package.get("no_expiry"):
                continue
            if package.get("package_code") == "enterprise":
                continue
            if package.get("is_expired"):
                continue
            remain = package.get("remain")
            if not isinstance(remain, (int, float)) or remain <= 0:
                continue
            expire_at = package.get("expireAt")
            if isinstance(expire_at, (int, float)) and expire_at > 0:
                days = (float(expire_at) - now) / 86400.0
            else:
                days = package.get("days_left")
                if not isinstance(days, (int, float)):
                    continue
            if 0 <= days <= window:
                rows.append((float(days), float(remain)))
        rows.sort()
        return rows

    def expiring_rate_per_day(self, within_days, now=None):
        """窗口内的临期积分每天至少要消耗掉多少。

        对每个到期时刻，把在它之前到期的余额加总后除以离它的天数，取最大值。
        余额分几笔到期时按各自的梯度累计，不会把六天后才到期的大包压到明天。
        """
        best = 0.0
        cumulative = 0.0
        for days, amount in self._expiring_rows(within_days, now=now):
            cumulative += amount
            best = max(best, cumulative / max(days, 1.0 / 24.0))
        return best

    def in_expiring_window(self):
        """临期积分优先时这个账号应当插队派单。

        余额未知或缺失一律不做偏好：网关判不了的账号保持普通轮转次序，
        不做臆测插队。
        """
        try:
            window = int(self.expiring_window_days or 0)
        except (TypeError, ValueError):
            window = 0
        if window <= 0:
            return False
        soonest = self.soonest_expiring_days()
        return soonest is not None and soonest <= window

    def daily_limit_blocked(self):
        """今日已统计的用量达到设定限额时返回 True。

        只有统计过的用量才会拦截：代理折叠用量日志之前计数是 None。
        """
        try:
            limit = int(self.daily_token_limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return False
        used = self.daily_tokens_today
        if used is None:
            return False
        try:
            return int(used) >= limit
        except (TypeError, ValueError):
            return False

    def model_is_free(self, model):
        """当前区域的免费模型目录里有 `model` 时返回 True。"""
        return bool(model) and model in (self.free_models or ())

    def credit_limit_reached(self):
        """今日已统计的积分消耗达到每日限额时返回 True。

        这是账号级事实，与请求哪个模型无关：ready() 把它与 model_is_free()
        组合，免费模型照常服务；面板把它显示为限额守卫的状态。
        """
        try:
            limit = int(self.daily_credit_limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return False
        used = self.daily_credits_today
        if used is None:
            return False
        try:
            return float(used) >= limit
        except (TypeError, ValueError):
            return False

    def credit_limit_blocked(self, model=None):
        """积分限额守卫应当把这个账号挡在 `model` 之外时返回 True。

        达到限额后免费模型照常可用，这正是守卫的目的。没有模型可判
        （model 为 None）时这里不拦截：调用方无从知道这次请求是否会消耗积分。
        """
        if not model or self.model_is_free(model):
            return False
        return self.credit_limit_reached()

    def note_model_cap(self, model, reset_at, now=None):
        """记下「这个模型的窗口额度被上游判定用满」（429 / code 6004）。

        持久化：6004 带的重置时刻是上游给的事实，跨重启仍然成立——否则重启后
        要等到下一次撞线才重新发现，而那个组合本来就在恢复时间内。返回是否新记
        （重复撞同一个窗口不算新，避免每次都重写凭证档）。

        不按模型范围和计费方式过滤：付费模型同样会收到 6004，上游给了恢复
        时刻就该跨重启生效。
        """
        if not model or not reset_at:
            return False
        now = time.time() if now is None else now
        try:
            reset = float(reset_at)
        except (TypeError, ValueError):
            return False
        with self._throttle_lock:
            old = self.model_caps.get(model)
            if old is not None and float(old.get("reset") or 0) == reset:
                # 同一个窗口的重复撞线：不用重写凭证档，但冷却要重新挂上——
                # 中间可能被换身份之类的清理顺手清掉过；挂不回来，这个窗口
                # 就一直不会被禁用。
                if self.model_cooldowns.get(model, 0.0) < reset:
                    self.model_cooldowns[model] = reset
                return False
            self.model_caps[model] = {"at": now, "reset": reset}
            # 恢复时间之前这个模型接不了单：挂上模型冷却，分派与账号行因此
            # 立刻反映（重启后由 restore_model_caps() 重新挂）。
            self.model_cooldowns[model] = max(self.model_cooldowns.get(model, 0.0), reset)
            # 撞线时刻就是这次禁用的起点：面板的 startedAt 与 cappedAt 同一瞬间。
            self.model_cooldowns_at[model] = now
        return True

    def cap_until(self, model, now=None):
        """这个模型处于「上游判定额度用满」状态的截止时刻；不在其中返回 0。"""
        entry = self.model_caps.get(model)
        if not isinstance(entry, dict):
            return 0.0
        now = time.time() if now is None else now
        try:
            reset = float(entry.get("reset") or 0)
        except (TypeError, ValueError):
            return 0.0
        return reset if reset > now else 0.0

    def capped_model_names(self, now=None):
        """此刻仍在恢复时间内的模型（上游判定额度用满）。"""
        now = time.time() if now is None else now
        return sorted(mid for mid in self.model_caps
                      if self.cap_until(mid, now) > 0)

    def note_model_hold(self, model, until, now=None):
        """记下一次带明确恢复时间的普通 429 窗口（非 6004）。

        与 6004 一样持久化：恢复时刻是上游给的事实，重启后仍然成立。返回是否
        新记；已有更晚的恢复时刻时只补冷却，不重写凭证档。
        """
        if not model or not until:
            return False
        now = time.time() if now is None else now
        try:
            until_at = float(until)
        except (TypeError, ValueError):
            return False
        if until_at <= now:
            return False
        with self._throttle_lock:
            old = self.model_holds.get(model)
            if old is not None and float(old.get("until") or 0) >= until_at:
                if self.model_cooldowns.get(model, 0.0) < float(old["until"]):
                    self.model_cooldowns[model] = float(old["until"])
                return False
            self.model_holds[model] = {"at": now, "until": until_at}
            self.model_cooldowns[model] = max(self.model_cooldowns.get(model, 0.0), until_at)
            # 面板据此显示「限流时刻」，无需从恢复时刻倒推。
            self.model_cooldowns_at[model] = now
        return True

    def hold_until(self, model, now=None):
        """这个模型处于普通 429 恢复窗口的截止时刻；不在其中返回 0。"""
        entry = self.model_holds.get(model)
        if not isinstance(entry, dict):
            return 0.0
        now = time.time() if now is None else now
        try:
            until = float(entry.get("until") or 0)
        except (TypeError, ValueError):
            return 0.0
        return until if until > now else 0.0

    def restore_model_caps(self, now=None):
        """重启后把仍在恢复时间内的 6004 重新挂成模型冷却。

        6004 的恢复时间跨重启有效，而模型冷却是纯内存的——不挂回来的话，重启
        会让这些组合重新接单，直到下一次撞 429 才又停。
        """
        now = time.time() if now is None else now
        restored = []
        with self._throttle_lock:
            for mid, entry in self.model_caps.items():
                try:
                    reset = float(entry.get("reset") or 0)
                except (TypeError, ValueError):
                    continue
                if reset <= now:
                    continue
                if self.model_cooldowns.get(mid, 0.0) < reset:
                    self.model_cooldowns[mid] = reset
                    restored.append(mid)
        return sorted(restored)

    def restore_model_holds(self, now=None):
        """重启后把仍在恢复时间内的普通 429 窗口挂回模型冷却。"""
        now = time.time() if now is None else now
        restored = []
        with self._throttle_lock:
            for mid, entry in self.model_holds.items():
                try:
                    until = float(entry.get("until") or 0)
                except (TypeError, ValueError):
                    continue
                if until <= now:
                    continue
                if self.model_cooldowns.get(mid, 0.0) < until:
                    self.model_cooldowns[mid] = until
                    self.model_cooldowns_at[mid] = float(entry.get("at") or now)
                    restored.append(mid)
        return sorted(restored)

    def model_token_limit_blocked(self, model=None):
        """`model` 今天的 Token 额度已经用完时返回 True。

        只拒绝这一个模型，账号的其他模型照常工作；没有统计的模型不拦截。
        """
        try:
            limit = int(self.model_daily_token_limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0 or not model:
            return False
        per = self.model_daily_tokens
        if not isinstance(per, dict):
            return False
        used = per.get(model)
        if used is None:
            return False
        try:
            return int(used) >= limit
        except (TypeError, ValueError):
            return False

    def blocked_model_names(self):
        """当前额度用完的模型集合。

        AccountPool.apply_model_daily_token_limit() 用它只记录状态真正变化
        的模型，避免每次刷新都给每个模型写一行日志。
        """
        per = self.model_daily_tokens
        if not isinstance(per, dict):
            return set()
        out = set()
        for mid in per:
            if self.model_token_limit_blocked(mid):
                out.add(mid)
        return out

    def unavailable_reason(self, model=None):
        """这个账号此刻为什么接不了单；可用时返回空字符串。

        `ready()` 是个布尔判断：池子被抽干的时候它只能说「不行」，说不清有几个
        账号、各自卡在哪一条。判定顺序与 ready() 保持一致，所以报出来的原因就是
        它拒绝的原因（唯一不覆盖的是临期凭证那条：它会真的发起刷新，诊断路径上
        不该顺带打上游）。
        """
        if not self.enabled:
            return "已停用"
        if not self.access_token:
            return "无凭证"
        # 与 ready() 同序：人工黑名单排在所有「暂时不可用」之前，所以
        # 「所有账号都禁用了这个模型」时，池子抽干的报错里会逐个号写明原因。
        if self.model_disabled(model):
            return "该模型已被人工禁用"
        now = time.time()
        # 四种惩罚的到期时间各走各的，谁把时间推得最远就报谁：只报「冷却」
        # 会让人往 429 的方向查，而实际可能是上游连续断连触发的熔断（30 分钟
        # 起，与账号本身无关），或者余额保护这类完全不同的原因。
        penalties = [("熔断", self.breaker_until), ("降权", self.degrade_until),
                     ("余额保护", self.balance_until), ("软限流冷却", self.cooldown_until)]
        if model:
            penalties.append(("模型 %s 冷却" % model, self.model_cooldowns.get(model, 0.0)))
        label, until = max(penalties, key=lambda item: item[1])
        if until > now:
            # 剩余时间要精确到秒：还剩 42 秒时写「0 min」，读起来就是
            # 「已经该能用了却还被挡着」。连续次数一并带上，才看得出是在
            # 升级还是刚开始。
            streak = ""
            if label == "软限流冷却" and self.soft_streak > 1:
                streak = "（连续第 %d 次）" % self.soft_streak
            elif label.startswith("模型") and self.unscoped_streak > 1:
                streak = "（连续第 %d 次）" % self.unscoped_streak
            return "%s 剩余 %s%s" % (label, _human_left(until - now), streak)
        if self.reserve_blocked():
            return "余额低于保留线"
        if self.daily_limit_blocked():
            return "今日 Token 额度用尽"
        if self.credit_limit_blocked(model):
            return "今日积分额度用尽"
        if self.model_token_limit_blocked(model):
            return "该模型今日额度用尽"
        exp = self.expires_at or jwt_exp(self.access_token)
        if exp and exp - now <= 120:
            return "凭证已过期"
        return ""

    def model_disabled(self, model):
        """这个账号被人工禁用了该模型？空名单恒为 False。

        与自动节流区分：`model_cooldowns` / `model_caps` 表达「暂时不该用」，
        这里是用户明确指定的「这个号就别碰这几个模型」。精确匹配（名单在
        加载时已按小写归一），不做前缀匹配。
        """
        if not model or not self.disabled_models:
            return False
        return str(model).strip().lower() in self.disabled_models

    def servable(self, model=None):
        """不需要刷新凭证就能判断的接单条件：启用、有凭证、不在冷却与各项限额内。"""
        if not self.enabled or not self.access_token:
            return False
        # 人工黑名单排在最前：它是静态配置（最便宜），而且在语义上优先于所有
        # 「暂时不可用」的判断 —— 被禁用的模型不该因为账号恰好恢复了就漏进来。
        if self.model_disabled(model):
            return False
        if self.throttle_wait(model=model) > 0:
            return False
        # 停在保留线之下：这里接一单会把余额推到 0，触发上游的提醒短信。
        if self.reserve_blocked():
            return False
        # 今天的 Token 预算用完：把名额留给明天，免得余下时间都收到上游的 429。
        if self.daily_limit_blocked():
            return False
        # 当日积分消耗达到上限：付费模型停用，免费模型照常服务
        # （见 credit_limit_blocked）。
        if self.credit_limit_blocked(model):
            return False
        # 单个模型额度用完不连累整个账号：这里只拒绝这一个模型。
        if self.model_token_limit_blocked(model):
            return False
        return True

    def ready(self, model=None):
        if not self.servable(model=model):
            return False
        exp = self.expires_at or jwt_exp(self.access_token)
        if not exp:
            return True
        remaining = exp - time.time()
        if remaining > TOKEN_REFRESH_MARGIN:
            return True
        # 刷新是最后手段，结果决定可用性：陈旧凭证往往在上游吃 401/403，
        # 放行前必须先刷成功。
        return self.refresh()

    def auth_headers(self):
        return wb_identity.auth_headers(self.access_token, self.is_api_key)

    def headers(self, purpose="chat", model=""):
        """组出这一轮的出站标头。

        chat 用途走 wb_identity（CLI 头 / WorkBuddy 头，可切换）；
        billing 用途维持原本的轻量标头，计费端点不使用那套身份。

        model 非空时带上 X-Model-ID，与官方客户端发往该端点的模型 id 一致。

        chat 标头按桌面端对话请求整理：官方对话请求不带 X-Session-ID（该头只
        用于反馈上报），所以这里也不发，避免每个账号恒定的派生值成为一处
        「网关特征」。

        billing 标头带 X-Session-ID，按账号派生：网关是一个进程冒充几十台设备，
        一旦取进程级的值，全部账号就会共用同一个 session id，那是一条把它们
        串起来的关联键（与每账号一台设备的隔离口径相悖）。见
        wb_fingerprint.session_id。
        """
        cfg = get_realm_config(self.realm)

        if purpose != "chat":
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": cfg["billing_ua"],
                "Origin": cfg["origin"],
                "Referer": cfg["origin"] + "/",
                **self.auth_headers(),
                "X-User-Id": self.uid,
                "X-Domain": self.domain or cfg["domain"],
                "x-codebuddy-request": "1",
                "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
                "X-Request-ID": generate_request_id(self.uid),
                "X-Session-ID": session_id(self.uid),
            }
            if self.enterprise_id:
                headers["X-Enterprise-Id"] = self.enterprise_id
                headers["X-Tenant-Id"] = self.enterprise_id
            else:
                headers["X-No-Enterprise-Id"] = "1"
            if self.realm == "cn":
                headers["X-Product"] = "SaaS"
            device_token = self._device_token()
            if device_token:
                headers["X-Device-Token"] = device_token
            return headers

        chat_domain = self.domain or cfg["domain"]
        if chat_domain in LEGACY_SESSION_DOMAINS:
            chat_domain = wb_identity.endpoint_for(self.realm, self.product)[1]
        identity = wb_identity.build_identity_headers(
            product=self.product,
            realm=self.realm,
            uid=self.uid,
            token=self.access_token,
            enterprise_id=self.enterprise_id,
            tenant_id=self.enterprise_id,
            domain=chat_domain,
        )
        # 桌面 CLI 只发身份标头与 JSON 请求体：Origin / Referer /
        # X-Requested-With / x-codebuddy-request 是面板 XHR 调用的标头，
        # 出现在模型请求上会成为网关特征。
        headers = dict(identity)
        if model:
            headers["X-Model-ID"] = str(model)
        device_token = self._device_token()
        if device_token:
            headers["X-Device-Token"] = device_token
        return headers

    def _device_token(self):
        """可选的 X-Device-Token：取配置的字面值或文件。"""
        directory = getattr(self, "accounts_dir", "")
        if not directory:
            return ""
        return resolve_device_token(directory)

    def sync_nickname(self):
        """手动刷新网页控制台昵称。

        只由面板/API 的显式操作调用，余额调度器从不调用；新昵称立即持久化。
        """
        if self.is_api_key:
            return self.nickname
        _uid, nickname = fetch_account_profile(self)
        if nickname and nickname != self.nickname:
            self.nickname = nickname
            directory = getattr(self, "accounts_dir", "")
            if not directory and self.path:
                directory = os.path.dirname(self.path)
            if directory:
                self.save(directory)
        return nickname

    def set_product(self, value):
        """切换出站身份（cli <-> workbuddy）。返回 True 表示真的换了。

        身份会写回凭证档，重启后仍然有效。save() 需要目录参数。
        """
        new = wb_identity.normalize_product(value)
        if new == self.product:
            return False
        self.product = new
        try:
            # 清的是限流状态；上游判定额度用满的禁用（仍在恢复期内的
            # model_caps）跨身份成立，不能被这次切换顺手清掉。
            self.clear_error(keep_caps=True)
        except Exception:
            pass
        return True

    def chat_base_url(self):
        """这个账号当前身份应当请求的端点。"""
        return wb_identity.endpoint_for(self.realm, self.product)[0]

    def can_refresh(self):
        """OAuth 账号可以刷新；静态 API Key 按定义长期有效。"""
        return not self.is_api_key

    def refresh(self, force=False):
        # 按账号串行化刷新，进锁后再查一次：上游会轮换 refresh token，并发
        # 刷新会让后一次把前一次已消费的 token 发出去。
        # force=True 给面板的「刷新凭证」用：即使令牌还未过期也请求一次上游。
        with self._refresh_lock:
            return self._refresh_locked(force=force)

    def _refresh_locked(self, force=False):
        if self.is_api_key:
            # 静态 Key 无法 refresh。返回 False 但不置 token_expired：这类
            # 凭证按定义长期有效，标成过期会把正常账号移出账号池。
            return False
        # 进锁后令牌可能已经被前一个持锁线程换新：再刷一次既浪费又把还没
        # 用过的 refresh token 轮换掉，直接当作刷新成功。
        if not force:
            exp = self.expires_at or jwt_exp(self.access_token)
            if exp and exp - time.time() > TOKEN_REFRESH_MARGIN:
                return True
        if not self.refresh_token:
            self._set_last_error("no refresh token; sign in again")
            return False
        endpoint, endpoint_domain = wb_identity.endpoint_for(
            self.realm, self.login_product)
        # 刷新回签发渠道，与当前对话身份（self.product）无关。
        domain = self.domain or endpoint_domain
        if domain in LEGACY_SESSION_DOMAINS:
            domain = endpoint_domain
        url = endpoint + REFRESH_PATH
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": wb_identity.identity_user_agent(
                self.realm, self.login_product),
            "X-Refresh-Token": self.refresh_token,
            "X-Auth-Refresh-Source": "plugin",
            "X-Domain": domain,
        }
        headers.update(wb_identity.login_trace_headers())
        try:
            payload = http_json(url, data=b"{}", method="POST", headers=headers, timeout=30,
                                proxy=self.proxy)
        except Exception as exc:
            self._set_last_error("refresh failed: %s" % exc)
            return False
        data = (payload.get("data") or {})
        data = data.get("data") or data
        token = data.get("accessToken")
        if not token:
            self._set_last_error("refresh returned no token (%s)" % payload.get("msg"))
            return False
        self.access_token = token
        self.refresh_token = data.get("refreshToken") or self.refresh_token
        self.expires_at = jwt_exp(token) or self.expires_at
        with self._throttle_lock:
            self.last_error = ""
            self.cooldown_until = 0
        self.persist()
        return True

    def can_checkin(self):
        if self.realm != "cn":
            return False
        if not self.has_personal_program():
            return False
        if not self.last_checkin:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_checkin).startswith(today_str)

    def can_daily_chat(self):
        if self.realm != "intl":
            return False
        if not self.last_daily_chat:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_daily_chat).startswith(today_str)

    def can_report_activity(self):
        """国内版每天一次的对话活跃上报（点亮 growth 连登）是否还没做过。"""
        if self.realm != "cn":
            return False
        if not self.has_personal_program():
            return False
        if not self.last_activity_report:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_activity_report).startswith(today_str)

    def web_headers(self):
        """网页版 app 的出站头：只有 bearer 与 X-User-Id，没有桌面端指纹。"""
        return {
            "Authorization": "Bearer " + self.access_token,
            "X-User-Id": self.uid,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_ORIGIN + "/app",
            "User-Agent": WEB_USER_AGENT,
        }

    def daily_chat_model(self):
        """每日活跃跑在哪个模型上。

        设置页配置了就用设置页的，否则用内置免费模型 id。桌面腿与网页腿都读
        它，改一处就整体生效。
        """
        candidates = []
        if self.path:
            picked = wb_settings.daily_chat_model(os.path.dirname(self.path))
            if picked:
                candidates.append(picked)
        candidates.append(DAILY_CHAT_MODEL)
        if self.realm == "intl":
            candidates.extend(["hy3", "hy4-preview-f", "hy4-preview", "glm-5.3-flash"])
        else:
            candidates.extend(["hy3", "hy4-preview-f", "hy4-preview"])

        now = time.time()
        with self._throttle_lock:
            for cand in candidates:
                cooldown_until = self.model_cooldowns.get(cand, 0.0)
                if cooldown_until <= now and self.cap_until(cand, now) <= 0.0:
                    return cand
        return candidates[0]

    def daily_chat_web(self, prompt=None, trigger="unknown"):
        """网页通道的每日活跃会话。

        只建会话不算有效对话：agent 要等客户端接上沙箱并请求这一轮才会跑，否则
        会话停在 CREATING、没有任何输出。按网页端的顺序走完：建会话 → 取沙箱
        link+token → ACP 的 initialize / session/load / session/prompt（见
        wb_webagent）→ 轮询到 completed。

        返回 {"ok": True, "conversation": id, "status": "completed", "chunks": n,
        "elapsed_ms": n}，失败时 {"ok": False, "error": ...}（尽量带上会话 id）。

        trigger 只用于活动历史的来源标注；trigger=None 表示这一步算在外层尝试
        里、不单独记一行 —— daily_chat() 内部就是这么调的。
        """
        if self.realm != "intl":
            return {"ok": False, "error": "web daily chat is only for international accounts"}
        res = self._daily_chat_web_upstream(prompt)
        wb_activity.record_attempt(self, wb_activity.TASK_DAILY_CHAT, trigger, res)
        return res

    def _daily_chat_web_upstream(self, prompt=None):
        """网页通道的实际流程；返回形状见 daily_chat_web。

        失败会再试（见 daily_chat_web_policy）：一共最多 attempts 次（含第一轮），
        第 N 轮的等待是首轮的 2^(N-1) 倍 —— 默认 3 次、首轮 120 秒，也就是
        120 / 240 / 480。最典型的失败是「会话还没跑完」（状态=working），它往往
        只是慢，多等一轮就好，所以这里把等待时间当成重试的代价，不再额外 sleep。
        业务拒绝（建会话返回非 0 code）不再试：同一个被拒的请求再发一次也一样被拒。

        返回里带 attempts（实际跑了几轮），其余字段与 _daily_chat_web_once 一致。
        """
        base, total = self.daily_chat_web_policy()
        attempt = 0
        while True:
            res = self._daily_chat_web_once(prompt, wait_seconds=base * (2 ** attempt))
            res["attempts"] = attempt + 1
            if res.get("ok") or attempt + 1 >= total or not res.get("retryable"):
                return res
            attempt += 1

    def daily_chat_web_policy(self):
        """(首轮等待秒数, 总尝试次数)。设置页改过就按设置页的来。

        重试开关默认开（见 wb_settings.daily_chat_web_retry），关掉就是只试一次。
        等待时间没设过时用 WEB_TURN_TIMEOUT —— 它吃 WB_WEB_TURN_TIMEOUT 环境变量，
        别让这个新设置项把它悄悄顶掉。没配 settings 的账号（单测里手工构造的）
        同样按内置默认走。
        """
        if not self.path:
            return WEB_TURN_TIMEOUT, WEB_TURN_ATTEMPTS
        directory = os.path.dirname(self.path)
        total = 1
        if wb_settings.daily_chat_web_retry(directory):
            total = wb_settings.daily_chat_web_attempts(directory,
                                                        default=WEB_TURN_ATTEMPTS)
        return (wb_settings.daily_chat_web_timeout(directory, default=WEB_TURN_TIMEOUT),
                total)

    def _daily_chat_web_once(self, prompt=None, wait_seconds=None):
        """网页通道的一轮尝试；返回形状见 daily_chat_web。

        结果里的 retryable 说明这一轮的失败值不值得再试一次（重试策略见
        _daily_chat_web_upstream）。
        """
        if wait_seconds is None:
            wait_seconds = WEB_TURN_TIMEOUT
        model = self.daily_chat_model()
        body = {
            "prompt": prompt or DAILY_CHAT_WEB_PROMPT,
            "model": model,
            # 网页端建会话时固定带上这两项（抓包所得），保持请求形态一致。
            "conversationOrigin": "workbuddy-app",
            "plugins": [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}],
        }
        req = wb_upstream_pool.Request(
            WEB_CONVERSATIONS_URL,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=self.web_headers(), method="POST")
        try:
            with urlopen(req, timeout=30, proxy=self.proxy) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = str(exc)
            return {"ok": False, "error": "HTTP %d: %s" % (exc.code, detail[:120]),
                    "retryable": exc.code >= 500 or exc.code in (408, 429)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "retryable": True}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "unexpected response", "retryable": True}
        conversation = (payload.get("data") or {}).get("id")
        if payload.get("code") not in (0, None) or not conversation:
            return {"ok": False, "error": "code=%s msg=%s"
                    % (payload.get("code"), payload.get("msg")),
                    "retryable": False}

        # 建完只是排队：接上沙箱、请求这一轮，它才会真的跑起来。
        try:
            session = (self._web_conversation_get(conversation, "/session") or {}).get("data") or {}
        except Exception as exc:
            return {"ok": False, "conversation": conversation,
                    "error": "session 查询失败: %s" % exc, "retryable": True}
        link = session.get("link") or session.get("endpoint") or ""
        token = session.get("token") or ""
        session_id = session.get("sessionId") or session.get("session_id") or conversation
        cwd = session.get("cwd") or "/workspace"
        if not link or not token:
            return {"ok": False, "conversation": conversation,
                    "error": "沙箱未就绪（没有 link/token）", "retryable": True}

        result = wb_webagent.run_turn(
            link, token, session_id, cwd, prompt or DAILY_CHAT_WEB_PROMPT,
            WEB_USER_AGENT,
            poll_status=lambda: self._web_conversation_status(conversation),
            wait_seconds=wait_seconds, proxy=self.proxy,
            model=model)
        result["conversation"] = conversation
        if result.get("ok"):
            result["msg"] = "网页通道会话跑完：输出 %d 段，耗时 %d ms" % (
                result.get("chunks") or 0, result.get("elapsed_ms") or 0)
        else:
            result["retryable"] = True
        return result

    def _web_conversation_get(self, conversation, suffix=""):
        """读一条网页端会话（suffix 为空拿会话本身，"/session" 拿沙箱信息）。"""
        url = WEB_CONVERSATIONS_URL + urllib.parse.quote(str(conversation)) + suffix
        req = wb_upstream_pool.Request(url, headers=self.web_headers(), method="GET")
        with urlopen(req, timeout=30, proxy=self.proxy) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")

    def _web_conversation_status(self, conversation):
        """这条会话现在什么状态（completed 就是这一轮真的跑完了）。"""
        try:
            payload = self._web_conversation_get(conversation)
        except Exception:
            return ""
        data = payload.get("data") if isinstance(payload, dict) else None
        return str((data or {}).get("status") or "")

    def daily_chat(self, web=None, trigger="unknown"):
        """国际版每日活跃对话（官方每日活跃 30/50 积分）。

        两步：桌面端身份的轻量对话，以及网页通道的会话（算数的是「跑完的 agent
        会话」）。web=None 时按 settings.json 里的 daily_chat_web 决定，True/False
        可显式指定。

        trigger 只用于活动历史的来源标注；整次尝试记一行，网页通道那一步的结果
        并进同一行的说明里，不再多记一行。ok 以真正计入积分的网页腿为准：网页腿
        开着且失败时整次算失败（桌面腿跑过也不算数）。
        """
        if self.realm != "intl":
            return {"ok": False, "error": "daily chat is only for international accounts"}
        res = self._daily_chat_upstream(web)
        wb_activity.record_attempt(self, wb_activity.TASK_DAILY_CHAT, trigger, res)
        return res

    def _daily_chat_upstream(self, web=None):
        """每日活跃对话的实际流程；返回形状见 daily_chat。"""
        proxy = wb_host.loaded_gateway("CHAT_PATH", "build_upstream_body")
        if proxy is None:
            import wb_proxy as proxy
        chat_path = getattr(proxy, "CHAT_PATH", "/v2/chat/completions")
        url = self.chat_base_url() + chat_path
        body = {
            "model": self.daily_chat_model(),
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
            "max_tokens": 10,
            "reasoning_effort": "none",
        }
        req_body = proxy.build_upstream_body(body)
        headers = self.headers(
            "chat", model=req_body.get("model") or body["model"])
        data = json.dumps(req_body, ensure_ascii=False).encode("utf-8")
        req = wb_upstream_pool.Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=20, proxy=self.proxy) as resp:
                _ = resp.read()
            try:
                self.fetch_credits()
            except Exception:
                pass
            result = {"ok": True, "msg": "每日活跃对话成功完成"}
            web_ok = True
            if web is None:
                web = bool(self.path) and wb_settings.daily_chat_web(os.path.dirname(self.path))
            if web:
                res = self.daily_chat_web(trigger=None)
                result["web"] = res
                web_ok = bool(res.get("ok"))
                if web_ok:
                    # 重试过就把轮数带上：光看「成功」分不清是首轮就跑完，还是
                    # 前几轮超时、多等了一轮才跑完（后者只是慢）。
                    tries = res.get("attempts") or 1
                    retried = "（重试 %d 次后）" % (tries - 1) if tries > 1 else ""
                    result["msg"] = ("每日活跃对话成功完成（网页通道%s %s：%d 段输出，%d ms）"
                                     % (retried, res.get("status") or "completed",
                                        res.get("chunks") or 0, res.get("elapsed_ms") or 0))
                else:
                    # 桌面腿成功不等于当天打卡完成：计入官方「每日活跃」的只有网页腿
                    # （#75/#59/#90）。面板与活动历史按 ok 画成功/失败，网页腿失败却
                    # 报成功的话，「今天这 30/50 积分没到账」在界面上一直看不见。
                    tries = res.get("attempts") or 1
                    suffix = "（已重试 %d 次）" % (tries - 1) if tries > 1 else ""
                    result["ok"] = False
                    result["error"] = "网页通道失败：%s%s" % (res.get("error"), suffix)
                    result["msg"] = "每日活跃对话未完成（网页通道失败：%s%s）" % (
                        res.get("error"), suffix)
            # 只有真正计入官方「每日活跃」的网页通道成功了才算今天做过：网页腿失败时
            # 若照写 lastDailyChat，当天后续巡检会直接跳过该账号，那天的 30/50 积分静默
            # 丢掉且不再重试。web=False（没开网页通道）时维持原语义：桌面腿成功即算完成。
            if web_ok:
                self.last_daily_chat = time.strftime("%Y-%m-%d %H:%M:%S")
                self.persist()
            return result
        except urllib.error.HTTPError as exc:
            try:
                err = exc.read().decode("utf-8", "replace")
            except Exception:
                err = str(exc)
            return {"ok": False, "error": f"HTTP {exc.code}: {err[:100]}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def checkin(self, trigger="unknown"):
        """国内版每日签到。

        trigger 只用于活动历史的来源标注（scheduler / manual / account_add /
        account_import）；记录写失败不会改变签到结果。
        """
        if self.realm != "cn":
            return {"ok": False, "error": "checkin is only available for CN realm accounts"}
        if not self.has_personal_program():
            reason = ("API key accounts have no checkin programme"
                      if self.is_api_key
                      else "enterprise accounts do not support checkin")
            return {"ok": False, "error": reason}
        res = self._checkin_upstream()
        wb_activity.record_attempt(self, wb_activity.TASK_CHECKIN, trigger, res)
        return res

    def _checkin_upstream(self):
        """签到的实际请求；返回形状见 checkin。"""
        cfg = get_realm_config("cn")
        url = cfg["billing_upstream"] + CHECKIN_PATH
        headers = self.headers(purpose="billing")
        try:
            payload = http_json(url, data=b"{}", method="POST", headers=headers, timeout=15,
                                proxy=self.proxy)
            code = payload.get("code", -1)
            msg = payload.get("msg") or "ok"
            ok = code in (0, 10001)
            # 只有真的签到成功才记时存盘：失败响应（风控、服务端报错）写进
            # last_checkin 会让这个账号今天再也不会重试。
            if ok:
                self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
                self.persist()
            return {"ok": ok, "code": code, "msg": msg, "data": payload.get("data")}
        except urllib.error.HTTPError as exc:
            try:
                raw_b = exc.read().decode("utf-8") or "{}"
                body = json.loads(raw_b)
                code = body.get("code")
                msg = body.get("msg") or ("HTTP %d" % exc.code)
                # 上游今日已签到时返回 HTTP 400、code 10001 或消息「今天已签到」，
                # 按成功处理并记录最后签到时间。
                if code == 10001 or "今天已签到" in msg or "已签到" in msg:
                    self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
                    self.persist()
                    return {"ok": True, "code": code, "msg": msg, "data": body.get("data")}
                return {"ok": False, "error": msg}
            except Exception:
                return {"ok": False, "error": "HTTP %d" % exc.code}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _parse_package_account(self, acc):
        pkg_name = acc.get("PackageName") or "Package"
        pkg_code = acc.get("PackageCode") or ""

        def _val(*keys):
            for k in keys:
                v = acc.get(k)
                if v is not None and v != "":
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        pass
            return 0.0

        size = _val("CycleCapacitySizePrecise", "CycleCapacitySize", "CapacitySizePrecise", "CapacitySize")
        remain = _val("CycleCapacityRemainPrecise", "CycleCapacityRemain", "CapacityRemainPrecise", "CapacityRemain")
        used = _val("CycleCapacityUsedPrecise", "CycleCapacityUsed", "CapacityUsedPrecise", "CapacityUsed")

        if size > 0 and used <= 0 and remain <= size:
            used = max(0.0, size - remain)
        elif size > 0 and remain <= 0 and used < size:
            remain = max(0.0, size - used)

        # 提取发放原因（如“官方活动发放”、“拉新奖励”等）
        grant_reason = ""
        for attr in (acc.get("AccountAttributes") or []):
            if isinstance(attr, dict) and attr.get("Key") == "grantReason":
                grant_reason = str(attr.get("Value") or "")
                break

        create_time = ""
        raw_create = acc.get("CreateTime")
        if raw_create:
            try:
                ts = float(raw_create)
                if ts > 1e11:
                    ts /= 1000.0
                create_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            except Exception:
                create_time = str(raw_create)

        end_time_str = acc.get("CycleEndTime") or acc.get("ExpiredTime") or ""
        # 到期认「抵扣截止时间」。「周期结束时间」只是计费周期的边界，积分
        # 未必跟着作废；免费包/体验版这两个字段能差 8 年，只认 CycleEndTime
        # 会让这类账号每到月底都被误判成「即将到期」。
        expire_time_str = deduction_end_text(acc) or end_time_str
        days_left = None
        is_expired = False
        no_expiry = False
        if expire_time_str:
            try:
                clean_time = expire_time_str.replace("T", " ")[:19]
                end_ts = time.mktime(time.strptime(clean_time, "%Y-%m-%d %H:%M:%S"))
                diff_sec = end_ts - time.time()
                if diff_sec > EXPIRY_SENTINEL_DAYS * 86400.0:
                    no_expiry = True
                else:
                    days_left = round(diff_sec / 86400.0, 1)
                    is_expired = diff_sec < 0
            except Exception:
                pass

        in_usage = bool(acc.get("InUsage"))
        if not in_usage and not is_expired and remain > 0 and used > 0:
            in_usage = True

        return {
            "name": pkg_name,
            "package_code": pkg_code,
            "product_name": acc.get("ProductName") or "",
            "subProduct": acc.get("SubProductName") or "",
            "sub_product_name": acc.get("SubProductName") or "",
            "grant_reason": grant_reason,
            "resource_id": acc.get("ResourceId") or "",
            "deal_name": acc.get("DealName") or "",
            "create_time": create_time,
            "startAt": parse_package_time(acc.get("CycleStartTime")),
            "expireAt": parse_package_time(expire_time_str) if expire_time_str else None,
            "remain": round(remain, 2),
            "used": round(used, 2),
            "size": round(size, 2),
            "unit": acc.get("CapacityUnit") or "credits",
            "in_usage": in_usage,
            "auto_renew": bool(acc.get("AutoRenewFlag") or acc.get("SupportAutoRenew")),
            "cycle_start_time": acc.get("CycleStartTime") or "",
            "cycle_end_time": end_time_str,
            "expire_time": expire_time_str,
            "no_expiry": no_expiry,
            "days_left": days_left,
            "is_expired": is_expired,
            "status": acc.get("Status", 0),
        }

    def _build_credits(self, packages, totals, is_paid_user, checkin):
        """按统一形状写入 self.credits 并落盘；返回 fetch_credits 的成功形状。

        totals 为 (remain, used, size)。额度包就地排序，使用中优先、未过期
        按到期时间升序、已过期排最后，再挑出最近到期且仍有余额的一包做到期
        提示。
        """
        def _pkg_sort_key(p):
            in_use_score = 0 if p.get("in_usage") else 1
            expired_score = 1 if p.get("is_expired") else 0
            days = p.get("days_left") if p.get("days_left") is not None else 99999
            if days < 0:
                days = 99999 + abs(days)
            return (in_use_score, expired_score, days, -p.get("remain", 0))

        packages.sort(key=_pkg_sort_key)

        active_pkgs = [p for p in packages if p.get("remain", 0) > 0 and not p.get("is_expired") and p.get("days_left") is not None]
        active_pkgs.sort(key=lambda p: p["days_left"])
        earliest_expiring = None
        if active_pkgs:
            ep = active_pkgs[0]
            earliest_expiring = {
                "name": ep["name"],
                "package_code": ep.get("package_code", ""),
                "remain": ep["remain"],
                "cycle_end_time": ep["cycle_end_time"],
                "days_left": ep["days_left"],
            }

        tot_remain, tot_used, tot_size = totals
        self.credits = {
            "remain": tot_remain,
            "used": tot_used,
            "size": tot_size,
            "used_percent": f"{(tot_used / tot_size * 100):.1f}%" if tot_size > 0 else "0.0%",
            "remain_percent": f"{(tot_remain / tot_size * 100):.1f}%" if tot_size > 0 else "100.0%",
            "is_paid_user": is_paid_user,
            "checkin": checkin,
            "earliest_expiring": earliest_expiring,
            "packages": packages,
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.persist()
        return {"ok": True, "credits": self.credits}

    def _fetch_credits_cn_detailed(self):
        cfg = get_realm_config(self.realm)
        headers = self.headers(purpose="billing")
        headers["X-Client-Platform"] = "web"
        base_billing = cfg["billing_upstream"]

        # 1. 套餐概览
        summary_url = base_billing + RESOURCE_SUMMARY_PATH
        s_res = http_json(summary_url, data=b"{}", method="POST", headers=headers,
                          timeout=15, proxy=self.proxy)
        if s_res.get("code") != 0:
            return {"ok": False, "error": s_res.get("msg") or f"code {s_res.get('code')}"}
        s_data = s_res.get("data") or {}
        summary_pkgs = s_data.get("Packages") or []
        pkg_codes = [p.get("PackageCode") for p in summary_pkgs if p.get("PackageCode")]

        # 2. 免费包
        free_accs = []
        if pkg_codes:
            free_url = base_billing + RESOURCE_FREE_PACKAGES_PATH
            body = {"PackageCodes": pkg_codes, "PageNumber": 1, "PageSize": 200, "Status": [0]}
            try:
                f_res = http_json(free_url, data=json.dumps(body).encode(), method="POST",
                                  headers=headers, timeout=15, proxy=self.proxy)
                free_accs = (f_res.get("data") or {}).get("Accounts") or []
            except Exception:
                pass

        # 3. 付费包
        paid_accs = []
        if pkg_codes:
            paid_url = base_billing + RESOURCE_PAID_PACKAGES_PATH
            body = {"PackageCodes": pkg_codes, "PageNumber": 1, "PageSize": 200, "Status": [0, 3], "NeedRenewInfo": True}
            try:
                p_res = http_json(paid_url, data=json.dumps(body).encode(), method="POST",
                                  headers=headers, timeout=15, proxy=self.proxy)
                paid_accs = (p_res.get("data") or {}).get("Accounts") or []
            except Exception:
                pass

        # 4. 每日签到状态
        checkin_info = None
        try:
            checkin_url = cfg.get("chat_upstream", "https://www.workbuddy.cn") + CHECKIN_STATUS_PATH
            c_res = http_json(checkin_url, data=b"{}", method="POST", headers=headers,
                              timeout=10, proxy=self.proxy)
            c_data = c_res.get("data") or {}
            if c_data:
                checkin_info = {
                    "today_checked_in": c_data.get("today_checked_in", False),
                    "streak_days": c_data.get("streak_days", 0),
                    "daily_credit": c_data.get("daily_credit", 0),
                    "today_credit": c_data.get("today_credit", 0),
                    "active": c_data.get("active", True),
                }
        except Exception:
            pass

        packages = []
        seen_account_ids = set()
        for raw_acc in (free_accs + paid_accs):
            acc_id = raw_acc.get("AccountId")
            if acc_id and acc_id in seen_account_ids:
                continue
            if acc_id:
                seen_account_ids.add(acc_id)
            packages.append(self._parse_package_account(raw_acc))

        tot_remain = sum(p["remain"] for p in packages)
        tot_used = sum(p["used"] for p in packages)
        tot_size = sum(p["size"] for p in packages)

        if not packages and summary_pkgs:
            for sp in summary_pkgs:
                tot_size += float(sp.get("CycleTotalCapacity") or 0)
                tot_remain += float(sp.get("CycleRemainCapacity") or 0)
                tot_used += float(sp.get("CycleUsedCapacity") or 0)

        tot_remain = round(tot_remain, 2)
        tot_used = round(tot_used, 2)
        tot_size = round(tot_size, 2)

        return self._build_credits(packages, (tot_remain, tot_used, tot_size),
                                    bool(s_data.get("IsPaidUser")), checkin_info)

    def _fetch_credits_fallback(self):
        cfg = get_realm_config(self.realm)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        body = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": now,
            "PackageEndTimeRangeEnd": "2036-01-01 00:00:00",
        }
        url = cfg["billing_upstream"] + GET_RESOURCE_PATH
        headers = self.headers(purpose="billing")
        try:
            res = http_json(url, data=json.dumps(body).encode(), method="POST", headers=headers,
                            timeout=30, proxy=self.proxy)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        data = res.get("data", {}).get("Response", {}).get("Data", {})
        accounts = data.get("Accounts") or []
        packages = []
        for a in accounts:
            packages.append(self._parse_package_account(a))

        tot_remain = round(sum(p["remain"] for p in packages), 2)
        tot_used = round(sum(p["used"] for p in packages), 2)
        tot_size = round(sum(p["size"] for p in packages), 2)

        # 这条路径拿不到签到数据，checkin 保留上一次的值。
        return self._build_credits(
            packages, (tot_remain, tot_used, tot_size), False,
            getattr(self, "credits", {}).get("checkin")
            if isinstance(getattr(self, "credits", None), dict) else None)

    def jwt_enterprise_id(self, token=None):
        """企业空间 id，取自 token 自身（或已存的 accessToken）。

        企业账号是登录时切到企业空间换来的 token（token_source=enterprise_switch），
        账号文件里的 enterpriseId 常常是空的——只有 token 里有。拿不到就返回 ""。
        """
        return str(_jwt_claims(token or self.access_token).get("enterprise_id") or "")

    def is_enterprise(self):
        """这个账号属于企业（团队）空间时返回 True。

        企业成员没有个人资源包，个人计费端点总是返回 TotalCount=0，面板显示
        0/0。他们的额度在 /billing/meter/get-enterprise-user-usage 上。
        """
        return bool(self.enterprise_id or self.jwt_enterprise_id())

    def has_personal_program(self):
        """签到/成长/活跃这套个人体系是否适用。"""
        return (self.realm == "cn" and not self.is_enterprise()
                and not self.is_api_key)

    def uses_enterprise_billing(self):
        """额度是否走企业端点；静态 API Key 没有个人资源包。"""
        return bool(self.is_enterprise() or self.is_api_key)

    def probe_api_key(self, timeout=15, apply_credits=False):
        """用企业额度端点验证一次静态 Key。

        静态 Key 没有本地可校验的签名字段，唯一可靠的验证是请求一次上游；
        添加账号前先验证再保存，避免无效账号进入账号池后被反复选中、反复冷却。
        apply_credits 为真时把同一份响应直接写成余额快照，添加流程不再重复请求。
        """
        if not self.is_api_key:
            return {"ok": False, "error": "not an API key"}
        cfg = get_realm_config("cn")
        url = cfg["billing_upstream"] + ENTERPRISE_USAGE_PATH_V2
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": cfg["chat_ua"],
            **self.auth_headers(),
        }
        try:
            res = http_json(url, data=b"{}", method="POST", headers=headers,
                            timeout=timeout, proxy=self.proxy)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not isinstance(res, dict) or res.get("code") != 0:
            code = res.get("code") if isinstance(res, dict) else "?"
            msg = res.get("msg") if isinstance(res, dict) else ""
            return {"ok": False, "error": "code %s: %s" % (code, msg)}
        data = res.get("data") or {}
        try:
            credits = self._enterprise_credits(data, strict=True)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if apply_credits:
            self.credits = credits
        return {"ok": True, "used": credits["used"], "limit": credits["size"],
                "cycle_end": data.get("cycleEndTime"), "credits": credits}

    def _enterprise_credits(self, data, strict=False):
        """企业额度响应 -> 面板积分快照。

        strict 用于添加账号前探测：缺字段或字段不是数字直接拒绝，避免空 data
        因 code=0 通过校验，把无效 Key 写进账号池。
        """
        if strict:
            if not isinstance(data, dict):
                raise ValueError("enterprise usage payload is empty")
            for field in ("credit", "limitNum"):
                value = data.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(
                        "enterprise usage field %s is missing or not numeric" % field)
        data = data if isinstance(data, dict) else {}
        try:
            used = round(float(data.get("credit") or 0), 2)
        except (TypeError, ValueError):
            used = 0.0
        try:
            size = round(float(data.get("limitNum") or 0), 2)
        except (TypeError, ValueError):
            size = 0.0
        # 上游只给额度与已用；已用超过额度（或额度缺失）时不要算出负余额。
        remain = round(max(0.0, size - used), 2) if size > 0 else 0.0
        cycle_start = str(data.get("cycleStartTime") or "")
        cycle_end = str(data.get("cycleEndTime") or data.get("cycleResetTime") or "")
        package = {
            "name": "企业额度" if size else "企业周期额度",
            "package_code": "enterprise",
            "product_name": "",
            "sub_product_name": "",
            "grant_reason": "企业空间发放",
            "resource_id": "",
            "deal_name": "",
            "create_time": cycle_start,
            "remain": remain,
            "used": used,
            "size": size,
            "unit": "credits",
            "in_usage": bool(used > 0),
            "auto_renew": True,
            "cycle_start_time": cycle_start,
            "cycle_end_time": cycle_end,
            "expire_time": "",
            "no_expiry": True,
            "days_left": None,
            "is_expired": False,
            "status": 0,
        }
        return {
            "remain": remain,
            "used": used,
            "size": size,
            "used_percent": ("%.1f%%" % (used / size * 100)) if size > 0 else "0.0%",
            "remain_percent": ("%.1f%%" % (remain / size * 100)) if size > 0 else "100.0%",
            "is_paid_user": True,
            "is_enterprise": True,
            "checkin": None,
            "earliest_expiring": None,
            "packages": [package] if size > 0 else [],
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _fetch_credits_enterprise(self):
        """企业账号积分：周期内已用 + 周期额度，剩余 = 额度 - 已用。

        上游只返回 4 个字段：credit（本周期已消耗）、limitNum（周期额度）、
        cycleStartTime / cycleEndTime。没有包列表，所以合成一个包条目，让看板的
        明细弹窗与到期提示照常工作。
        """
        cfg = get_realm_config(self.realm)
        path = (ENTERPRISE_USAGE_PATH_V2 if self.is_api_key
                else ENTERPRISE_USAGE_PATH)
        url = cfg["billing_upstream"] + path
        headers = self.headers(purpose="billing")
        headers["X-Client-Platform"] = "web"
        try:
            res = http_json(url, data=b"{}", method="POST", headers=headers,
                            timeout=15, proxy=self.proxy)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        data = res.get("data") or {}
        self.credits = self._enterprise_credits(data)
        self.persist()
        return {"ok": True, "credits": self.credits}

    def fetch_credits(self):
        """刷新余额；余额恢复时还会提前解除 402 停机（revive_balance_cooldown）。

        按账号串行：后台刷新线程与签到/每日活跃任务都走到这里，没有锁的话
        两个重叠调用各花一次上游计费请求，后返回的答复还会覆盖先写入的余额。
        """
        with self._credits_lock:
            before = _credits_remain(self.credits)
            res = self._fetch_credits_raw()
            if isinstance(res, dict) and res.get("ok"):
                self.revive_balance_cooldown()
                record_credit_event(self, before, _credits_remain(self.credits))
            return res

    def _fetch_credits_raw(self):
        # 静态 API Key 没有个人资源包：个人端点对它返回空 Accounts，直接走
        # 企业端点，避免余额恒 0 被当成积分耗尽的账号。
        if self.is_api_key:
            return self._fetch_credits_enterprise()
        if self.realm == "cn":
            try:
                res = self._fetch_credits_cn_detailed()
                # 企业账号在个人计费接口上没有资源包：返回 ok 但全是 0。
                # 这时改问企业用量接口，否则看板永远是 0/0。
                if res.get("ok") and not (self.credits or {}).get("size"):
                    if self.uses_enterprise_billing():
                        ent = self._fetch_credits_enterprise()
                        if ent.get("ok"):
                            return ent
                if res.get("ok"):
                    return res
            except Exception:
                pass
            if self.uses_enterprise_billing():
                try:
                    ent = self._fetch_credits_enterprise()
                    if ent.get("ok"):
                        return ent
                except Exception:
                    pass
        return self._fetch_credits_fallback()

    def _set_last_error(self, message):
        """把刷新错误记进面板展示的状态里。"""
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self.last_error_detail = ""

    def note_error(self, message, cooldown=60, single_account=False, model=None, until=None,
                   detail=None):
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            # 可见标签维持原有截断；detail 带上未改动的上游原文，面板可在
            # 悬停时展示。
            self.last_error_detail = str(detail)[:2000] if detail else ""
            if model:
                # 模型级限流：账号对其他模型照常可用。
                wait = max(1.0, float(until) - time.time()) if until else (
                    3.0 if single_account else float(cooldown))
                now = time.time()
                self.model_cooldowns[model] = now + wait
                # 起始时刻：面板要如实说「限流时刻 + 恢复时间」，而恢复时刻减不出
                # 起始时刻（wait 来自 60s→10m 的阶梯，不持久化）。记这一次就够了。
                self.model_cooldowns_at[model] = now
                return
            actual_cooldown = 3 if single_account else cooldown
            self.cooldown_until = time.time() + actual_cooldown

    def note_balance_cooled(self, message="insufficient credits"):
        """402（欠费）：停到下一个本地 04:00。

        短冷却几分钟后又会把这个空账号发出去，请求照样失败；积分要到每日
        重置才回来，所以停到那时。余额刷新看到积分恢复可提前解除，见
        revive_balance_cooldown()。
        """
        until = next_local_4am()
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self.balance_until = max(self.balance_until, until)
        return until

    def revive_balance_cooldown(self):
        """余额刷新看到积分恢复时，只解除 402 停机。

        限流与模型冷却原样保留，通用清理走 clear_error()。
        """
        if not self.balance_until:
            return False
        credits = self.credits if isinstance(self.credits, dict) else {}
        try:
            remain = int(credits.get("remain"))
        except (TypeError, ValueError):
            remain = None
        if remain is None or remain <= 0:
            return False
        with self._throttle_lock:
            self.balance_until = 0.0
            self.last_error = ""
            self.last_error_detail = ""
        return True

    def note_soft_rate(self, message):
        """账号级限流：软冷却，按指数退避。"""
        with self._throttle_lock:
            self._decay_soft_streak()
            self.soft_streak += 1
            wait = soft_backoff(self.soft_streak)
            self.last_error = str(message)[:200]
            self.cooldown_until = max(self.cooldown_until, time.time() + wait)
        return wait

    def _decay_soft_streak(self):
        """软限流阶梯也要有时间窗（调用方已持有 _throttle_lock）。

        阶梯若只加不减、只有成功才归零：昨天被限过 3 次，今天第一个 429 就
        从 2400s 起步，直到 7200s 封顶，账号早已恢复正常也会被按几十分钟到
        两小时。窗口内才算连续，隔得久就回到第一档（600s / 60s）。
        """
        now = time.time()
        if FAIL_WINDOW and self.last_soft_at and now - self.last_soft_at > FAIL_WINDOW:
            self.soft_streak = 0
            self.unscoped_streak = 0
        self.last_soft_at = now

    def note_unscoped_rate(self, model):
        """无范围软限流 429：走自己的连击计数，返回这个模型的窗口。

        计数与 soft_streak 刻意分开。共用一个计数会让两条阶梯互相喂档：
        四次无范围 429 会把接下来真正的凭据级限流直接顶到 7200s 上限，而
        凭据连击也会让下一个无范围窗口起步变高。两种范围各有计数，一次
        成功请求通过 note_success() 同时清零。
        """
        with self._throttle_lock:
            self._decay_soft_streak()
            self.unscoped_streak += 1
            return unverified_soft_backoff(self.unscoped_streak)

    def note_failure(self, message):
        """5xx / 传输失败：计入熔断计数。"""
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self._decay_strikes()
            self.fails += 1
            if self.fails >= BREAKER_THRESHOLD:
                wait = breaker_backoff(self.fails)
                self.breaker_until = max(self.breaker_until, time.time() + wait)
        return self.breaker_until

    def _decay_strikes(self):
        """两次失败隔得太久就不再算「连续」，从头计数。

        调用方必须已持有 _throttle_lock。窗口设 0 时保持旧的累计语义。
        """
        now = time.time()
        if FAIL_WINDOW and self.last_fail_at and now - self.last_fail_at > FAIL_WINDOW:
            self.fails = 0
            self.degrade_count = 0
        self.last_fail_at = now

    def note_unknown_failure(self, message):
        """未知错误：计入降级计数与共用的熔断计数。"""
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self._decay_strikes()
            self.degrade_count += 1
            self.fails += 1
            now = time.time()
            if self.degrade_count >= DEGRADE_THRESHOLD:
                wait = degrade_backoff(self.degrade_count)
                self.degrade_until = max(self.degrade_until, now + wait)
            if self.fails >= BREAKER_THRESHOLD:
                wait = breaker_backoff(self.fails)
                self.breaker_until = max(self.breaker_until, now + wait)
        return self.degrade_until

    def note_success(self, model=None):
        """一次成功请求清掉全部账号级惩罚。"""
        with self._throttle_lock:
            if model:
                self.model_cooldowns.pop(model, None)
                self.model_cooldowns_at.pop(model, None)
            self.soft_streak = 0
            self.unscoped_streak = 0
            self.fails = 0
            self.degrade_count = 0
            self.last_fail_at = 0.0
            self.last_soft_at = 0.0
            self.breaker_until = 0.0
            self.degrade_until = 0.0
            self.last_error = ""
            self.last_error_detail = ""
            self.cooldown_until = 0.0

    def throttle_wait(self, model=None):
        """这个账号多久后能再服务 `model`（0 = 现在就能）。"""
        if not self.enabled or not self.access_token:
            return 0.0
        now = time.time()
        with self._throttle_lock:
            wait = max(0.0, self.cooldown_until - now,
self.balance_until - now, self.breaker_until - now, self.degrade_until - now)
            if model:
                wait = max(wait, max(0.0, self.model_cooldowns.get(model, 0.0) - now))
        return wait

    def clear_error(self, model=None, keep_caps=False):
        """清掉限流状态；keep_caps 保留上游给了恢复时刻的禁用。

        换身份/重试要清的是限流窗口，而 6004 的额度用满和普通 429 的恢复
        时刻都是上游给的事实（时刻之前这个组合就是不可用），不能顺手清掉：
        auto-switch 开着时清掉它，账号在撞线之后立刻又可用，禁用形同不存在。
        """
        with self._throttle_lock:
            if model:
                if not (keep_caps and (self.cap_until(model) > 0
                                       or self.hold_until(model) > 0)):
                    self.model_cooldowns.pop(model, None)
                    self.model_cooldowns_at.pop(model, None)
            elif keep_caps:
                now = time.time()
                for mid in [m for m in self.model_cooldowns
                            if self.cap_until(m, now) <= 0
                            and self.hold_until(m, now) <= 0]:
                    self.model_cooldowns.pop(mid, None)
                    self.model_cooldowns_at.pop(mid, None)
            else:
                self.model_cooldowns.clear()
                self.model_cooldowns_at.clear()
            if (self.last_error or self.cooldown_until
                    or self.last_error_detail or self.balance_until):
                self.last_error = ""
                self.last_error_detail = ""
                self.cooldown_until = 0
                self.balance_until = 0.0

    # -- 单账号单模型的并发名额 ---------------------------------------------

    def _capacity_key(self, model):
        return str(model or "")

    def active_for_model(self, model):
        with self._concurrency_lock:
            return self._in_flight.get(self._capacity_key(model), 0)

    @property
    def active_requests(self):
        with self._concurrency_lock:
            return sum(self._in_flight.values())

    def has_request_capacity(self, model=None):
        """这个账号在这个模型上还有空名额吗（上限为 0 时永远有）。"""
        with self._concurrency_lock:
            if self.concurrency_limit <= 0:
                return True
            return self._in_flight.get(self._capacity_key(model), 0) < self.concurrency_limit

    def acquire_request(self, model=None):
        """原子地占一个名额；已经满了返回 False。"""
        key = self._capacity_key(model)
        with self._concurrency_lock:
            used = self._in_flight.get(key, 0)
            if self.concurrency_limit > 0 and used >= self.concurrency_limit:
                return False
            self._in_flight[key] = used + 1
            return True

    def release_request(self, model=None):
        key = self._capacity_key(model)
        with self._concurrency_lock:
            used = self._in_flight.get(key, 0) - 1
            if used > 0:
                self._in_flight[key] = used
            else:
                self._in_flight.pop(key, None)


def _claim(account, model, claim):
    """选号时的名额判定：有 claim 就原子地占名额，没有就只检查。"""
    if claim is not None:
        return claim(account)
    return account.has_request_capacity(model)


def normalise_concurrency_limit(value):
    """返回（限额、错误）：单账号并发上限的校验结果。

    上限按「账号 + 模型」统计在途请求；0 表示不限。
    """
    if isinstance(value, bool) or value is None or (
            isinstance(value, float) and not value.is_integer()):
        return None, "concurrencyLimit must be a non-negative whole number"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None, "concurrencyLimit must be a non-negative whole number"
    if number < 0 or number > MAX_ACCOUNT_CONCURRENCY:
        return None, ("concurrencyLimit must be between 0 and %d"
                      % MAX_ACCOUNT_CONCURRENCY)
    return number, ""


def _stored_concurrency_limit(value):
    limit, problem = normalise_concurrency_limit(value)
    return 0 if problem else limit


def parse_account_priority(value):
    """校验 /accounts/set 的 priority；返回 (priority, error)。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "priority must be a whole number between 0 and 9999"
    if not 0 <= value <= MAX_ACCOUNT_PRIORITY:
        return None, "priority must be a whole number between 0 and 9999"
    return value, ""


def parse_account_set(payload):
    """校验 /accounts/set 的请求体；返回 (changes, error)。

    先整体校验再应用字段，靠后的非法字段才不会让靠前的字段先落盘。
    """
    changes = {}
    if "proxySlot" in payload:
        changes["proxySlot"] = payload.get("proxySlot")
    if "proxy" in payload:
        changes["proxy"] = payload.get("proxy")
    if "enabled" in payload:
        changes["enabled"] = bool(payload.get("enabled"))
    if "weight" in payload:
        try:
            weight = int(payload.get("weight"))
        except (TypeError, ValueError):
            return None, "weight must be an integer between 1 and %d" % MAX_ACCOUNT_WEIGHT
        if not 1 <= weight <= MAX_ACCOUNT_WEIGHT:
            return None, "weight out of range (1-%d)" % MAX_ACCOUNT_WEIGHT
        changes["weight"] = weight
    if "priority" in payload:
        priority, problem = parse_account_priority(payload.get("priority"))
        if problem:
            return None, problem
        changes["priority"] = priority
    if "concurrencyLimit" in payload:
        limit, problem = normalise_concurrency_limit(
            payload.get("concurrencyLimit"))
        if problem:
            return None, problem
        changes["concurrencyLimit"] = limit
    if "note" in payload:
        changes["note"] = payload.get("note")
    if "disabledModels" in payload:
        changes["disabledModels"] = payload.get("disabledModels")
    if not changes:
        return None, ("nothing to update: pass 'enabled', 'proxy', 'proxySlot', "
                      "'weight', 'priority', 'concurrencyLimit', 'note' or "
                      "'disabledModels'")
    return changes, ""


def _human_delta(seconds):
    if seconds is None: return None
    if seconds <= 0: return "expired"
    days = seconds / 86400.0
    if days >= 1: return "%.0f days" % days
    hours = seconds / 3600.0
    if hours >= 1: return "%.1f hours" % hours
    return "%d min" % int(seconds / 60)

# ---------------------------------------------------------------------------
# 积分变动流水
#
# 余额读取成功、而合计比上一次已知值增加时追加一行 JSONL：签到、每日活跃、
# 旅行、套餐到账都会体现在这里。基线就是凭证文件里的 credits，因此重启后接着
# 比对；首次读取只建立基线。写入目录由 wb_proxy 注入，wb_accounts 不认识
# USAGE_DIR。
# ---------------------------------------------------------------------------
#: 返回积分流水文件路径的回调；未注入时一次也不记录。
CREDIT_EVENTS_PROVIDER = None


def set_credit_events_provider(provider):
    """注入「积分流水写到哪个文件」的来源，传 None 表示不记录。

    注入回调：--usage-dir 可以在启动时改变 USAGE_DIR，每次重新求值才能一直
    指向真正在用的目录。
    """
    global CREDIT_EVENTS_PROVIDER
    CREDIT_EVENTS_PROVIDER = provider


def credit_events_file():
    """积分流水当前的写入路径，没有注入来源时为空串。"""
    provider = CREDIT_EVENTS_PROVIDER
    return str(provider() or "") if provider else ""


def _credits_remain(credits):
    """余额合计；读不出来时返回 None，表示这个账号还没有基线。"""
    if not isinstance(credits, dict):
        return None
    remain = credits.get("remain")
    if isinstance(remain, bool) or not isinstance(remain, (int, float)):
        return None
    return remain


#: 积分流水只保留最近 30 天，每次追加时顺带删掉更早的行。
CREDIT_EVENT_RETENTION_DAYS = 30
_CREDIT_EVENTS_LOCK = threading.Lock()


def record_credit_event(account, before, after):
    """余额增加时追加一条积分流水，返回这条记录；没有记录时为 None。

    before 为 None 表示还没有基线，只建立基线不记流水；写入失败直接抛出，
    流水缺行比一次余额读取失败更难发现。
    """
    if before is None or after is None or after <= before:
        return None
    path = credit_events_file()
    if not path:
        return None
    now = time.time()
    row = {
        "at": int(now),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "uid": account.uid,
        "nickname": account.nickname,
        "realm": account.realm,
        "before": before,
        "after": after,
        "delta": after - before,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cutoff = now - CREDIT_EVENT_RETENTION_DAYS * 86400
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    # 读旧行与写回放在同一把锁里，两个账号同时到账时不会互相覆盖。
    with _CREDIT_EVENTS_LOCK:
        kept = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        old = json.loads(text)
                    except ValueError:
                        continue
                    if isinstance(old, dict) and (old.get("at") or 0) >= cutoff:
                        kept.append(text)
        kept.append(json.dumps(row, ensure_ascii=False))
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept) + "\n")
        os.replace(tmp, path)
    return row

def _human_left(seconds):
    """还剩多久，精确到秒（对外解释「为什么现在不能用」时用）。

    `_human_delta` 是给凭据有效期那种以天计的场景用的，小于一分钟一律写
    「0 min」；而冷却/熔断的剩余时间经常就是几十秒，写成 0 分钟会让人以为
    账号已经可用、网关还在挡它。
    """
    secs = max(0, int(round(seconds)))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        m, s = divmod(secs, 60)
        return "%dm%02ds" % (m, s) if s else "%dm" % m
    if secs < 86400:
        h, rem = divmod(secs, 3600)
        m = rem // 60
        return "%dh%02dm" % (h, m) if m else "%dh" % h
    return "%.1f days" % (secs / 86400.0)

class SessionAffinity(object):
    """记录某个会话由哪个上游账号服务，下一轮可以回到同一个账号。

    每个会话保留一个最近使用的账号列表。队首是 `get()` 返回的账号，即上一轮的去
    向；其后是更早服务过该会话的账号，越近越靠前。队首不可用（冷却、停用、禁用）
    时，下一次选号优先挑这个会话已经在上游热过的账号，不挑冷账号，提示词缓存因此
    随迁移保留。每个条目各自到期，久未回访的账号自己掉出列表。

    历史长度故意很小（`history` 为 3）：它记录的是可能仍持有该会话前缀的账号，
    不是负载均衡手段。
    """

    def __init__(self, ttl=7200, max_entries=5000, history=3):
        self.ttl = ttl
        self.max_entries = max_entries
        self.history = max(1, int(history))
        self.bindings = {}
        self._lock = threading.Lock()
    def _live(self, key, now):
        """这个会话未过期的条目，其余就地丢弃。"""
        entries = self.bindings.get(key) or []
        live = [entry for entry in entries if entry[1] > now]
        if live:
            self.bindings[key] = live
        else:
            self.bindings.pop(key, None)
        return live
    def get(self, key):
        """上一轮去的账号（顺延其过期时间）；没有则返回 None。"""
        if not key: return None
        with self._lock:
            now = time.time()
            live = self._live(key, now)
            if not live: return None
            uid, _exp = live[0]
            live[0] = (uid, now + self.ttl)
            return uid
    def recent(self, key):
        """这个会话全部未过期的账号，最近在前。"""
        if not key: return []
        with self._lock:
            return [uid for uid, _exp in self._live(key, time.time())]
    def bind(self, key, uid):
        """把 `uid` 置于队首，去重并保留最近 `history` 个。"""
        if not key or not uid: return
        now = time.time()
        with self._lock:
            if len(self.bindings) >= self.max_entries:
                self.bindings = {k: v for k, v in self.bindings.items()
                                 if v and v[0][1] > now}
            entries = [entry for entry in (self.bindings.get(key) or [])
                       if entry[0] != uid]
            entries.insert(0, (uid, now + self.ttl))
            self.bindings[key] = entries[:self.history]
    def demote(self, key, uid=None):
        """从会话里丢掉一个账号，其余保留。

        失败路径用它替代 unbind：刚失败的账号（429 / 402 / 401 / 5xx /
        断连）不能接下一次尝试，排在它前面的账号可能还持有这个会话的前缀，
        下次选号应先试它们，再退到冷账号。默认丢队首；传 `uid` 丢指定账号。
        """
        if not key: return
        with self._lock:
            entries = self.bindings.get(key) or []
            if uid is None:
                entries = entries[1:]
            else:
                entries = [entry for entry in entries if entry[0] != uid]
            if entries:
                self.bindings[key] = entries
            else:
                self.bindings.pop(key, None)
    def unbind(self, key):
        if not key: return
        with self._lock:
            self.bindings.pop(key, None)
    def size(self):
        with self._lock:
            return len(self.bindings)

#: 会话归属的打点：面板据此判断「一段对话换了账号」这类缓存损失发生了多少次。
#: bound_hit = 绑定命中且该账号接单；bound_lost = 有绑定但那个账号不能接单；
#: fresh_binding = 会话键第一次出现；no_key = 客户端没给会话标识；
#: smart_pick / cursor_pick 记录两种选号方式各被用过多少次。
ROUTING_COUNTERS = ("bound_hit", "bound_lost", "fresh_binding", "no_key",
                    "smart_pick", "cursor_pick")

#: 智能分配里「剩余接近耗尽」的倾斜量：权重每高一档，评分向前挪这么多。
#: 满档（权重 4）相当于 15 个百分点，能压过轻微的忙闲差，压不过明显欠账。
REMAINING_URGENCY_STEP = 0.05


#: 面板可改、而登录与桌面端导入数据里通常不带的字段（payload 键 -> 属性名）。
#: AccountPool.add() 替换同 uid 账号时，只有新数据确实写了这个键才覆盖，否则继承
#: 旧值：一次重新登录不该抹掉用户设置的优先级或出站身份。
KEEP_ON_REPLACE_FIELDS = (
    ("product", "product"),
    ("loginProduct", "login_product"),
    ("priority", "priority"),
    ("note", "note"),
    ("concurrencyLimit", "concurrency_limit"),
    ("weight", "weight"),
    ("disabledModels", "disabled_models"),
    ("proxySlot", "proxy_slot"),
    ("proxy", "proxy_legacy"),
    ("credits", "credits"),
    ("lastCheckin", "last_checkin"),
    ("lastDailyChat", "last_daily_chat"),
)


def _realm_limit(values, realm):
    """某个守卫值按账号区域取值。

    `values` 是 wb_settings.limit_values() 给的映射：
    {"global": g, "intl": ..., "cn": ...}。区域为空或无法识别时退回 global
    默认值，区域判不出来的账号仍受全局数字约束。
    """
    if isinstance(values, dict):
        value = values.get(realm)
        if value is None:
            value = values.get("global")
    else:
        value = values
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class CreditsRefresher(threading.Thread):
    """在请求路径之外保持账号余额新鲜。

    派发偏好读 `account.credits`；没有这个线程时只有登录与每日活跃任务会刷新它，
    重启只能继承磁盘上的旧余额。每 tick 只刷新最陈旧且已过 TTL 的一个账号，上游
    计费调用因此有界（每 tick 一次），在账号间平摊、陆续发出。刷新失败的账号
    停用六小时，坏凭据不会每个 tick 重试。

    请求量由 tick 决定，与池子大小无关：默认半小时一个 tick，一天最多 48 次计费
    调用。TTL 为 12 小时时 28 个账号一天要 56 次，比 tick 能给的多，所以实际刷新
    间隔会略高于 TTL。这是有意的取舍：派发偏好以天计，几小时的偏差比省下的调用
    划算。
    """

    def __init__(self, pool, interval_seconds=1800):
        super().__init__(daemon=True, name="credits-refresher")
        self.pool = pool
        self.interval_seconds = max(60.0, float(interval_seconds))
        self.last_run = None
        self.last_uid = None
        self.last_error = None
        self.logs = []
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._parked = {}          # uid -> 暂停到哪个时刻
        self._failed_cooldown = 6 * 3600.0

    def log(self, msg):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.logs.append("[%s] %s" % (stamp, msg))
        if len(self.logs) > 40:
            self.logs = self.logs[-40:]

    def stop(self):
        self._stop_event.set()
        self._wake.set()

    def wake(self):
        self._wake.set()

    def ttl_seconds(self):
        """当前 TTL（秒）——单位换算只在这一处做。"""
        try:
            import wb_settings
            hours = wb_settings.credits_refresh_hours(self.pool.dir)
        except Exception:
            return 0.0
        try:
            return max(0.0, float(hours)) * 3600.0
        except (TypeError, ValueError):
            return 0.0

    def stalest_account(self, ttl_seconds, now=None):
        """余额最陈旧且已过 TTL 的账号；没有则返回 None。

        从未查过余额算无限陈旧，新装的环境先补齐全部账号，之后才回头刷新
        超时的。
        """
        now = time.time() if now is None else now
        with self.pool._lock:
            accounts = list(self.pool.accounts)
        chosen = None
        chosen_age = None
        for account in accounts:
            if not account.enabled or not account.access_token:
                continue
            if self._parked.get(account.uid, 0) > now:
                continue
            credits = account.credits
            updated = credits.get("updated_at") if isinstance(credits, dict) else None
            if isinstance(updated, (int, float)):
                age = now - updated
            else:
                age = float("inf")
            if age < ttl_seconds:
                continue
            if chosen_age is None or age > chosen_age:
                chosen, chosen_age = account, age
        return chosen

    def refresh_once(self, now=None):
        """刷新一个陈旧账号；返回它的 uid，没有到期的返回 None。"""
        ttl = self.ttl_seconds()
        if ttl <= 0:
            return None
        with self._lock:
            account = self.stalest_account(ttl, now=now)
            if account is None:
                return None
            try:
                result = account.fetch_credits()
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            if not isinstance(result, dict) or not result.get("ok"):
                error = (result or {}).get("error") or "unknown error"
                self._parked[account.uid] = \
                    (time.time() if now is None else now) + self._failed_cooldown
                self.last_error = str(error)
                self.log("刷新 %s 积分失败，暂缓六小时: %s"
                         % (account.uid[:8], error))
                return None
            self.last_run = time.time() if now is None else now
            self.last_uid = account.uid
            self.last_error = None
            self.log("已刷新 %s 的积分（%s）"
                     % (account.uid[:8], (account.credits or {}).get("updated_iso")))
            return account.uid

    def run(self):
        while not self._stop_event.is_set():
            self._wake.wait(timeout=self.interval_seconds)
            if self._stop_event.is_set():
                break
            self._wake.clear()
            try:
                self.refresh_once()
            except Exception as exc:
                self.last_error = str(exc)
                self.log("刷新循环异常: %s" % exc)

    def status(self):
        return {
            "ttl_hours": round(self.ttl_seconds() / 3600.0, 2),
            "interval_seconds": self.interval_seconds,
            "last_run": self.last_run,
            "last_uid": self.last_uid,
            "last_error": self.last_error,
            "logs": self.logs[-10:],
        }


class AccountPool(object):
    def __init__(self, directory, log=None):
        self.dir = directory
        self.log = log or (lambda msg: None)
        self.accounts = []
        self.logins = {}
        self._lock = threading.RLock()
        self._cursor = 0
        # 临期积分窗口的平滑加权轮询状态，按 uid 记（见 _weighted_pick）。
        # 只存运行期：重启就重新开始轮转。
        self._expiry_weights = {}
        # 剩余用量：wb_proxy 每请求推来的 {(uid, model): 权重}（只有它读得到
        # 剩余估算）。空表表示没有组合接近估计限额。智能分配把它折进评分。
        self._remaining_weights = {}
        # 手动权重优先（面板按账号设置，weight > 1 才生效）的平滑加权轮询
        # 累计值，键是 uid：与临期偏好的轮次分开。派生值，不落盘。
        self._manual_weights = {}
        # 页内轮转（超限对话）的每页游标，键是 (页号, 页大小)；整池轮转仍用
        # _cursor，两条轮转互不挪动对方的位置。派生值，不落盘。
        self._page_cursors = {}
        # uid 排序表的缓存：池成员没变就直接复用，变了重建（见 _page_ranks）。
        self._page_order_cache = None
        self.affinity = SessionAffinity()
        # 新建绑定用的选号方式：默认保持轮询顺序，只有 apply_smart_routing()
        # 设过面板开关后才按评分选号，直接构造的账号池行为不变。
        self.smart_routing = False
        self._stats_lock = threading.Lock()
        self.routing_stats = {name: 0 for name in ROUTING_COUNTERS}

    def load(self):
        with self._lock:
            self.accounts = []
            if not os.path.isdir(self.dir): return self.accounts
            for name in sorted(os.listdir(self.dir)):
                if not name.endswith(".json"): continue
                path = os.path.join(self.dir, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        account = Account(json.load(fh), path)
                except Exception as exc:
                    self.log("account %s unreadable: %s" % (name, exc))
                    continue
                if account.uid:
                    self.accounts.append(account)
            self.apply_reserve_credits()
            self.apply_expiring_window()
            # 6004 的恢复时间跨重启有效：把还在恢复时间内的组合重新挂成模型
            # 冷却，否则重启会把它们重新放出去，直到下次撞 429 才又停。普通
            # 429 带恢复时间的窗口同样挂回。
            self.restore_model_caps()
            self.restore_model_holds()
            return self.accounts

    def restore_model_caps(self, now=None):
        """把仍在恢复时间内的 6004 挂回每个账号的模型冷却上。"""
        now = time.time() if now is None else now
        restored = {}
        with self._lock:
            for account in self.accounts:
                mids = account.restore_model_caps(now)
                if mids:
                    restored[account.uid] = mids
        for uid, mids in sorted(restored.items()):
            self.log("account %s: 模型 %s 仍在额度恢复期内（至上游给定时刻）"
                     % (uid[:8], ", ".join(mids)))
        return restored

    def restore_model_holds(self, now=None):
        """把仍在恢复时间内的普通 429 窗口挂回每个账号的模型冷却上。"""
        now = time.time() if now is None else now
        restored = {}
        with self._lock:
            for account in self.accounts:
                mids = account.restore_model_holds(now)
                if mids:
                    restored[account.uid] = mids
        for uid, mids in sorted(restored.items()):
            self.log("account %s: 模型 %s 仍在上游恢复时间内"
                     % (uid[:8], ", ".join(mids)))
        return restored

    def list_public(self, realm=None):
        with self._lock:
            accs = list(self.accounts if (not realm or realm == "all")
                        else [a for a in self.accounts if a.realm == realm])
        # 序列化每个账号会派生 machineId、整理到期列表，没必要占着账号池锁。
        return [a.public() for a in accs]

    def get(self, uid):
        with self._lock:
            for account in self.accounts:
                if account.uid == uid: return account
        return None

    def add(self, account):
        with self._lock:
            existing = self.get(account.uid)
            if existing is not None:
                account.added_at = existing.added_at
                account.path = existing.path
                # 新数据里没写这个键、或写了个 null：沿用旧值。登录数据本来就没有
                # 这些字段，显式 null 也不该把面板上的设置清掉。
                for key, attr in KEEP_ON_REPLACE_FIELDS:
                    if key in account.payload_keys and getattr(account, attr) is not None:
                        continue
                    setattr(account, attr, getattr(existing, attr))
                self.accounts[self.accounts.index(existing)] = account
            else:
                self.accounts.append(account)
            account.save(self.dir)
            self.apply_proxy_slots()
            self.apply_reserve_credits()
            self.apply_expiring_window()
            return account

    def remove(self, uid):
        with self._lock:
            account = self.get(uid)
            if account is None: return False
            account.delete()
            self.accounts.remove(account)
            return True

    def preview_import_rows(self, rows, realm=None, overwrite=False):
        """报告 import_rows() 会做什么，不动账号池。

        与 import_rows() 共用同一套接收/跳过规则，预演与实际导入的结果一致。
        """
        preview = {"added": [], "updated": [], "skipped": [], "invalid": []}
        known = {a.uid for a in self.accounts}
        seen = set()
        for index, row in enumerate(rows):
            try:
                kwargs = normalise_import_row(row, realm=realm)
            except Exception as exc:
                preview["invalid"].append({"index": index + 1, "reason": str(exc)})
                continue
            uid = kwargs["uid"]
            if uid in seen:
                preview["skipped"].append({"uid": uid, "reason": "duplicate inside the document"})
            elif uid in known and not overwrite:
                preview["skipped"].append({"uid": uid, "reason": "already exists"})
            elif uid in known:
                preview["updated"].append(uid)
            else:
                preview["added"].append(uid)
            seen.add(uid)
        return preview

    def import_rows(self, rows, realm=None, overwrite=False):
        """从导出或外来行里添加账号。

        返回报告字典：
            added       —— 池里没有的新 uid
            updated     —— 原来就存在、被替换的 uid
            skipped     —— [{"uid","reason"}] 未导入的行
            invalid     —— [{"index","reason"}] 解析失败的行

        每行解析干净才写入，一条坏行不会中止整个文件的导入。
        """
        added, updated, skipped, invalid = [], [], [], []
        seen = set()
        for index, row in enumerate(rows):
            try:
                kwargs = normalise_import_row(row, realm=realm)
            except Exception as exc:
                invalid.append({"index": index + 1, "reason": str(exc)})
                continue

            uid = kwargs["uid"]
            if uid in seen:
                skipped.append({"uid": uid, "reason": "duplicate inside the document"})
                continue
            seen.add(uid)

            existing = self.get(uid) is not None
            if existing and not overwrite:
                skipped.append({"uid": uid, "reason": "already exists"})
                continue

            try:
                self.add(Account(kwargs))
            except Exception as exc:
                invalid.append({"index": index + 1, "reason": str(exc)})
                continue

            (updated if existing else added).append(uid)

        if added or updated:
            self.apply_proxy_slots()
        return {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "invalid": invalid,
        }

    def set_enabled(self, uid, enabled):
        account = self.get(uid)
        if account is None: return None
        account.enabled = bool(enabled)
        if enabled:
            account.clear_error()
        account.save(self.dir)
        self.apply_proxy_slots()
        return account.public()

    def set_weight(self, uid, weight):
        """设置账号的手动调度权重（1～100，1 = 不干预），保存并返回公共视图。"""
        account = self.get(uid)
        if account is None:
            return None
        try:
            account.weight = max(1, min(MAX_ACCOUNT_WEIGHT, int(weight)))
        except (TypeError, ValueError):
            return None
        account.save(self.dir)
        return account.public()

    def set_disabled_models(self, uid, models):
        """设置该账号**人工**禁用的模型名单，保存并返回公共视图。

        语义：这些模型不再派给这个账号。不影响该账号的其他模型，也不影响其他
        账号对同一模型的可用性 —— 请求会自动落到池子里别的号上。传空列表 =
        清空禁用、恢复原行为。

        名单按 normalise_disabled_models 归一；坏输入退化成空列表（= 不禁用），
        不抛错：面板传进来什么形状都不该让一个账号加载失败。
        """
        account = self.get(uid)
        if account is None:
            return None
        account.disabled_models = normalise_disabled_models(models)
        account.save(self.dir)
        return account.public()

    def set_proxy(self, uid, proxy):
        account = self.get(uid)
        if account is None:
            return None
        account.proxy_legacy = str(proxy or "").strip()
        account.save(self.dir)
        self.apply_proxy_slots()
        return account.public()

    def apply_proxy_slots(self, slots=None):
        """按绑定的代理槽重新解析每个账号的运行期 `proxy`。

        `proxy_slot` 是持久化的真值来源；`proxy` 是派生的运行期值，众多出站
        调用点因此不用改。
        """
        import wb_settings

        if slots is None:
            slots = wb_settings.proxy_slots(self.dir)
        by_id = {entry["id"]: entry for entry in slots if entry.get("enabled")}
        with self._lock:
            for account in self.accounts:
                slot = by_id.get(account.proxy_slot)
                if slot:
                    account.proxy = slot["url"]
                else:
                    account.proxy = account.proxy_legacy

    def apply_reserve_credits(self, values=None):
        """为每个账号重新解析低余额守卫。

        形态与 apply_proxy_slots() 相同：settings.json 是真值来源，账号值
        在这里派生，请求路径不用再查设置。`values` 是按区域解析的守卫值
        （{"global": g, "intl": ..., "cn": ...}）；每个账号取自己区域的值，
        区域覆盖只作用于该区域的账号，global 默认值继续覆盖两个区域。
        """
        import wb_settings

        if values is None:
            values = wb_settings.limit_values(self.dir, "reserve_credits")
        with self._lock:
            for account in self.accounts:
                account.reserve_credits = _realm_limit(values, account.realm)
        return values

    def apply_expiring_window(self, values=None):
        """为每个账号重新解析临期积分窗口。

        形态与 apply_reserve_credits() 相同：settings.json 按区域存窗口
        （默认 global），每个账号取自己那份，请求路径不用再查设置。窗口是
        派单偏好，不是守卫：它从不把账号变不可用，只决定谁先出池。
        """
        import wb_settings

        if values is None:
            values = wb_settings.limit_values(self.dir, "expiring_window_days")
        with self._lock:
            for account in self.accounts:
                account.expiring_window_days = _realm_limit(values, account.realm)
        return values

    def apply_remaining_weights(self, weights):
        """换上 wb_proxy 算好的 (账号, 模型) 权重表（剩余用量并入智能分配）。

        数据方向与 apply_daily_* 同款：只有 wb_proxy 读得到剩余估算（折叠、
        预算与载荷都在那边），池不反向依赖它，只存派生值。None / 空表表示
        这一刻没有组合接近估计限额（或智能分配关着），上次的权重会被清掉。
        权重 <= 1 的条目没有偏好意义，直接丢弃。返回存下来的表（测试用）。
        """
        table = {}
        for pair, weight in (weights or {}).items():
            try:
                uid, model = pair
                weight = int(weight)
            except (TypeError, ValueError):
                continue
            if weight > 1:
                table[(str(uid), str(model))] = weight
        with self._lock:
            self._remaining_weights = table
        return table

    def apply_daily_token_limit(self, values=None, usage=None):
        """为每个账号重新解析每日 Token 守卫。

        形态与 apply_reserve_credits() 相同：settings.json 按区域存限额，
        `usage`（uid -> 今日已统计 Token）由调用方传入，只有代理读得到用量
        日志。传 None 保留上次计数，改设置不会把计数变成「未知」。
        """
        import wb_settings

        if values is None:
            values = wb_settings.limit_values(self.dir, "daily_token_limit")
        with self._lock:
            for account in self.accounts:
                limit = _realm_limit(values, account.realm)
                was_blocked = account.daily_limit_blocked()
                account.daily_token_limit = limit
                if usage is not None:
                    try:
                        account.daily_tokens_today = int(usage.get(account.uid, 0))
                    except (TypeError, ValueError):
                        account.daily_tokens_today = None
                now_blocked = account.daily_limit_blocked()
                if now_blocked != was_blocked:
                    if now_blocked:
                        self.log("account %s parked: daily token limit reached "
                                 "(%s/%s tokens today)"
                                 % (str(account.uid)[:8],
                                    account.daily_tokens_today, limit))
                    else:
                        self.log("account %s resumed: daily token limit cleared"
                                 % str(account.uid)[:8])
        return values

    def apply_daily_credit_limit(self, values=None, credits=None, free_models=None):
        """为每个账号重新解析每日积分守卫。

        形态与 apply_daily_token_limit() 相同：settings.json 按区域存限额，
        `credits`（uid -> 今日已消耗）与 `free_models`（区域 -> 免费模型 id）
        由调用方传入，只有代理读得到用量日志与模型目录。传 None 保留上次
        数值，改设置不会把它们变成「未知」。
        """
        import wb_settings

        if values is None:
            values = wb_settings.limit_values(self.dir, "daily_credit_limit")
        with self._lock:
            for account in self.accounts:
                limit = _realm_limit(values, account.realm)
                was_blocked = account.credit_limit_reached()
                account.daily_credit_limit = limit
                if credits is not None:
                    try:
                        account.daily_credits_today = float(
                            credits.get(account.uid, 0) or 0)
                    except (TypeError, ValueError):
                        account.daily_credits_today = None
                if free_models is not None:
                    account.free_models = frozenset(
                        free_models.get(account.realm) or ())
                now_blocked = account.credit_limit_reached()
                if now_blocked != was_blocked:
                    if now_blocked:
                        self.log("account %s capped: daily credit limit reached "
                                 "(%s/%s credits today), free models only"
                                 % (str(account.uid)[:8],
                                    account.daily_credits_today, limit))
                    else:
                        self.log("account %s resumed: daily credit limit cleared"
                                 % str(account.uid)[:8])
        return values

    def apply_model_daily_token_limit(self, values=None, per_model=None):
        """为每个账号重新解析「单模型每日 Token」守卫。

        `per_model` 是 uid -> {model: 今日已统计 Token}；None 保留上次计数。
        单个模型被拦不连累整个账号：ready(model) 只拒绝额度用完的模型，
        也只记录这些模型的状态变化。
        """
        import wb_settings

        if values is None:
            values = wb_settings.limit_values(self.dir, "model_daily_token_limit")
        with self._lock:
            for account in self.accounts:
                limit = _realm_limit(values, account.realm)
                was_blocked = account.blocked_model_names()
                account.model_daily_token_limit = limit
                if per_model is not None:
                    raw = per_model.get(account.uid) or {}
                    try:
                        account.model_daily_tokens = {str(k): int(v)
                                                      for k, v in raw.items()}
                    except (TypeError, ValueError, AttributeError):
                        account.model_daily_tokens = None
                now_blocked = account.blocked_model_names()
                for mid in sorted(now_blocked - was_blocked):
                    self.log("account %s model %s parked: daily token limit "
                             "reached (%s/%s tokens today)"
                             % (str(account.uid)[:8], mid,
                                (account.model_daily_tokens or {}).get(mid), limit))
                for mid in sorted(was_blocked - now_blocked):
                    self.log("account %s model %s resumed: daily token limit "
                             "cleared" % (str(account.uid)[:8], mid))
        return values

    def set_proxy_slot(self, uid, slot_id):
        account = self.get(uid)
        if account is None:
            return None
        slot_id = str(slot_id or "").strip()
        account.proxy_slot = slot_id
        if not slot_id:
            # 选「直连」就必须直连：残留的旧代理 URL 会让流量继续走它，
            # 面板却显示直连。
            account.proxy_legacy = ""
        account.save(self.dir)
        self.apply_proxy_slots()
        return account.public()

    def set_priority(self, uid, value):
        """保存一个账号的调度优先级（数字越小越先被选中）。"""
        account = self.get(uid)
        if account is None:
            return None
        account.priority = _normalise_priority(value)
        account.save(self.dir)
        return account.public()

    def set_note(self, uid, note):
        """保存一个账号的备注；空字符串表示清空。"""
        account = self.get(uid)
        if account is None:
            return None
        account.note = _stored_note(note)
        account.save(self.dir)
        return account.public()

    def set_concurrency_limit(self, uid, value):
        """保存一个账号的单模型并发上限（0 = 不限）。"""
        account = self.get(uid)
        if account is None:
            return None
        account.concurrency_limit = value
        account.save(self.dir)
        return account.public()

    def set_all_enabled(self, enabled, realm=None):
        with self._lock:
            for account in self.accounts:
                if realm and account.realm != realm: continue
                account.enabled = bool(enabled)
                if enabled:
                    account.clear_error()
                account.save(self.dir)
        self.apply_proxy_slots()

    def apply_smart_routing(self, value=None):
        """重新解析这个池的派单设置。

        形态与其他守卫相同：settings.json 是真值来源，池里只存解析后的
        布尔值；传 None 表示读取当前设置。
        """
        import wb_settings

        if value is None:
            value = wb_settings.smart_routing(self.dir)
        self.smart_routing = bool(value)
        return self.smart_routing

    def apply_credit_rates(self, per_day=None):
        """把最近的积分消耗速度（每天积分）推给各账号。"""
        per_day = per_day or {}
        with self._lock:
            for account in self.accounts:
                try:
                    account.credit_rate_per_day = float(per_day.get(account.uid) or 0.0)
                except (TypeError, ValueError):
                    account.credit_rate_per_day = 0.0

    def count_routing(self, name, delta=1):
        if name not in ROUTING_COUNTERS:
            raise KeyError(name)
        with self._stats_lock:
            self.routing_stats[name] = self.routing_stats.get(name, 0) + delta

    def routing_snapshot(self):
        """计数器加解析后的设置，供面板使用。"""
        with self._stats_lock:
            out = dict(self.routing_stats)
        out["smart_routing"] = bool(self.smart_routing)
        out["affinity_entries"] = self.affinity.size()
        return out

    def count_ready(self, realm=None, model=None):
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        return sum(1 for a in snapshot if a.enabled and a.access_token and a.ready(model=model))

    def unavailable_reason(self, uid, realm=None, model=None):
        """为什么这个账号现在不能接单；可以接单时返回空串。

        测试台可以只用一个账号发请求，拒绝时要说清是这个账号的哪一条限制。
        """
        account = self.get(uid)
        if account is None:
            return "it is not in the account pool"
        if realm and account.realm != realm:
            return "it belongs to the other realm"
        if not account.enabled:
            return "it is disabled"
        if not account.access_token:
            return "it has no credential"
        if account.throttle_wait(model=model) > 0:
            return "it is cooling down after an upstream rejection"
        if account.reserve_blocked():
            return "its balance is below the reserved credits"
        if account.daily_limit_blocked():
            return "it reached today's token limit"
        if not account.ready(model=model):
            return "its token expired and could not be refreshed"
        return ""

    def pick_for_session(self, realm=None, session_key=None, exclude=None, model=None,
                         page=None, claim=None):
        """为对话的一轮挑选账号。

        有绑定的对话沿用上一轮的账号；该账号不可用时，先试这段对话的其他
        热账号，再走池子的自动偏好。`page` 标记超限对话，在一页内轮转，
        不做绑定。`claim` 在发出账号前先占好每账号的请求名额。
        """
        exclude = exclude or set()
        if session_key and not wb_settings.session_affinity(self.dir):
            session_key = None
        had_binding = False
        if session_key and page is None:
            recent = self.affinity.recent(session_key)
            had_binding = bool(recent)
            for uid in recent:
                if uid in exclude:
                    continue
                account = self.get(uid)
                if (account and account.realm == realm and account.ready(model=model)
                        and _claim(account, model, claim)):
                    # 绑定命中也算一次接单：评分分配按「最近一小时实际接到的
                    # 份额」判断忙闲，漏掉这些请求会把最忙的账号看成最闲的。
                    self.affinity.bind(session_key, uid)
                    account.mark_pick()
                    self.count_routing("bound_hit")
                    return account
            if had_binding:
                # 绑定的账号此刻都不能接单（冷却、限额、令牌失效、名额满）：
                # 这段对话改由别的账号接手，接手账号重算完整前缀后缓存跟着它。
                self.count_routing("bound_lost")
        account = self.pick(realm=realm, exclude=exclude, model=model, page=page,
                            claim=claim)
        if account is None and page is not None:
            # 这一页此刻没有可用账号。从页外找账号服务这段对话，总好过完全
            # 不服务。
            account = self.pick(realm=realm, exclude=exclude, model=model, claim=claim)
        # 页内对话刻意不绑定：轮转让这一页的每个账号保持热度，绑定会让它回到
        # 单个账号（正是它被释放出来的状态）。轮转位置由页游标保存。
        if account and session_key and page is None:
            self.affinity.bind(session_key, account.uid)
            if not had_binding:
                self.count_routing("fresh_binding")
        if not session_key:
            self.count_routing("no_key")
        return account

    def pick(self, realm=None, exclude=None, model=None, page=None, claim=None):
        exclude = set(exclude or ())
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        if page is not None:
            snapshot = self._page_slice(snapshot, page)
        if not snapshot:
            return None
        # 手动权重优先：面板给账号设了 >1 的权重时，这些账号先于一切自动
        # 偏好接单，彼此之间按权重平滑加权轮询。它们此刻都不可用（冷却、
        # 限额、被排除、名额满）时落回下面的自动偏好与轮询。
        while True:
            account = self._pick_weighted_first(snapshot, exclude, model)
            if account is None:
                break
            if _claim(account, model, claim):
                account.mark_pick()
                return account
            exclude.add(account.uid)
        # 智能调度打开时由积分义务与最近忙闲评分选号；它已经包含了对
        # 接单能力的判断与名额占用。
        if self.smart_routing:
            return self._pick_scored(realm=realm, exclude=exclude, model=model,
                                     claim=claim, snapshot=snapshot)
        while True:
            account = self._pick_expiring_first(snapshot, exclude, model)
            if account is None:
                break
            if _claim(account, model, claim):
                account.mark_pick()
                return account
            exclude.add(account.uid)
        # 智能分配关闭时按优先级与轮询顺序派单：剩余用量偏好已并入智能分配的
        # 评分，不再作为独立分层。
        return self._rotate_pick(snapshot, exclude, model, cursor_key=page, claim=claim)

    def _pick_weighted_first(self, snapshot, exclude, model):
        """手动权重 > 1 的账号优先接单，或 None（没有可交的）。

        与临期积分那条同款的两层结构，只是这层的权重来自用户在面板上的明确
        设置，优先级也最高：权重是「我就想让这个号多用」的直接表达，不该被
        自动偏好覆盖。同层之间按权重平滑加权轮询——两个高权重账号按权重比
        分流量，它们与权重 1 的账号之间则是「先用完我再轮别人」。权重 1 的
        账号完全不进这一层：临期积分、剩余用量这些自动子权重照旧在它们身上
        生效，一行都不变。
        """
        ready = []
        for account in snapshot:
            if account.uid in exclude:
                continue
            if int(getattr(account, "weight", 1) or 1) <= 1:
                continue
            if not account.ready(model=model):
                continue
            ready.append((account, account.uid, int(account.weight)))
        if not ready:
            return None
        return self._smooth_weighted_pick(ready, "_manual_weights")

    def _pick_expiring_first(self, snapshot, exclude, model):
        """窗口内的账号先服务；没有可用的返回 None。

        窗口内做加权轮询：积分越临近作废，份额越大，但没有任何账号独占整个
        窗口。严格按最早到期排序会把每个请求都压到明天到期的那个账号上，直到
        它被上游限流，这正是要摊开的突发；加权保留偏好，也给晚到期的账号真实
        份额。同一天到期的账号权重相同，因此继续轮流。
        """
        urgent = [a for a in snapshot if a.in_expiring_window()]
        if not urgent:
            return None
        ready = [a for a in urgent if a.uid not in exclude and a.ready(model=model)]
        if not ready:
            return None
        return self._weighted_pick(ready)

    def _expiry_weight(self, account):
        """窗口内的账号按「每天要消耗掉的临期积分」分配流量。

        权重等于窗口内各笔到期余额按到期梯度累计出的每日最低消耗：六天后才
        到期的大包不会压到明天，只按它自己的期限分摊。窗口边缘的账号也仍有
        一份，不会被饿到余额全过期。
        """
        try:
            window = int(account.expiring_window_days or 0)
        except (TypeError, ValueError):
            window = 0
        days = account.soonest_expiring_days()
        if days is None:
            return 1
        rate = account.expiring_rate_per_day(window)
        if rate > 0:
            return max(1, int(round(rate)))
        return max(1, int(round(window - days)) + 1)

    def _weighted_pick(self, ready):
        """临期积分那条的入口：在窗口内的账号之间平滑加权轮询。"""
        return self._smooth_weighted_pick(
            [(account, account.uid, self._expiry_weight(account))
             for account in ready], "_expiry_weights")

    def _smooth_weighted_pick(self, entries, state_name):
        """在 (账号, key, 权重) 三元组上做平滑加权轮询。

        nginx 的算法：每次挑选给每项累加自己的权重，累计值最大的胜出，胜出者付出整轮
        的权重。权重相同时退化成普通轮转，同权重的项因此依次轮流。

        `key` 是轮转状态的键，`state_name` 是持有该状态的池属性名。

        状态会裁到本轮实际参与的项：掉出的账号（冷却、被过滤、名额已满）丢掉自己的
        累计值，之后回来时从零起步，不背着离开前的欠账。

        轮转状态由所有请求线程共用，读改写都在池锁内完成。喂给它的 `ready()` 检查不
        持锁：那些检查可能访问网络（刷新令牌），为一张将过期的凭据持锁会把该区域的
        每次派发都串行化。
        """
        keys = set(key for _account, key, _weight in entries)
        with self._lock:
            stored = getattr(self, state_name)
            state = dict((key, value) for key, value in stored.items()
                         if key in keys)
            total = 0
            chosen = None
            for account, key, weight in entries:
                state[key] = state.get(key, 0) + weight
                total += weight
                if chosen is None or state[key] > state[chosen[1]]:
                    chosen = (account, key)
            if chosen is None:
                return None
            state[chosen[1]] -= total
            setattr(self, state_name, state)
        return chosen[0]

    def _page_slice(self, snapshot, page):
        """池子里某一页上的账号。

        一页是固定槽位：账号在本区域账号中的排名（按 uid 排序）对页大小取模。按区域
        排名（不跨整池，也不按加载顺序或文件名）才能让页保持声明的容量：混合区域的
        池子里，全局排名会让某页只剩一个可用账号，把会话绑死在那里。按 uid 排序也让
        会话的页在重启与重载后保持稳定；增删账号最多影响每页一个槽位。

        页不会大于区域：页大小覆盖全部账号（或更多）时，这一页就是该区域的全部。
        """
        try:
            index, size = int(page[0]), int(page[1])
        except (TypeError, ValueError, IndexError):
            return snapshot
        ranks = self._page_ranks(snapshot)
        if size <= 1 or size >= len(ranks):
            return snapshot
        out = []
        for account in snapshot:
            rank = ranks.get(account.uid)
            if rank is not None and rank % size == index % size:
                out.append(account)
        return out

    def _page_ranks(self, snapshot):
        """uid -> 在快照账号中的排名，缓存到快照变化为止。

        快照是按区域过滤后的池（不含可用性判断），排名（即分页）只在该区域
        增删账号时才会变。
        """
        uids = tuple(sorted(a.uid for a in snapshot if a.uid))
        cached = self._page_order_cache
        if cached is not None and cached[0] == uids:
            return cached[1]
        ranks = dict((uid, rank) for rank, uid in enumerate(uids))
        self._page_order_cache = (uids, ranks)
        return ranks

    def _pick_scored(self, realm=None, exclude=None, model=None, claim=None,
                     snapshot=None):
        """按积分到期节奏与最近一小时的忙闲给新对话选号。

        每个账号到期前每天至少要消耗掉的积分是它的义务，义务减去今天的消耗
        速度是它的欠账；欠账占候选账号欠账总和的比例就是它应当承担的份额，
        再减去最近一小时实际接到的份额，差得最多的先派过去。全员都跟得上时
        比例为 0，退化成「最近一小时最少接单的先派」。

        候选账号只包括此刻能接单、又没被排除的账号：拿不到新对话的账号若算进
        总和，它的欠账永远消不掉，会把其余账号的份额压平。

        评分之前先过临期积分层：窗口内的账号按到期节奏加权先接单，与关闭
        智能分配时一致；都没命中才按欠账与忙闲评分。估计剩余接近耗尽的
        （账号、模型）组合在评分里获得一点倾斜，越接近限额越靠前，但不独占流量。
        """
        exclude = exclude or set()
        if snapshot is None:
            with self._lock:
                snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        total = len(snapshot)
        if total == 0:
            return None
        with self._lock:
            start = self._cursor
        now = time.time()
        candidates = [index for index, account in enumerate(snapshot)
                      if account.uid not in exclude and account.servable(model=model)
                      and account.has_request_capacity(model)]
        if not candidates:
            return None

        def take(account):
            """结算一次智能分配命中：推进轮询游标、记录来源并计入忙闲。"""
            with self._lock:
                try:
                    index = snapshot.index(account)
                except ValueError:
                    index = -1
                if index >= 0:
                    self._cursor = (index + 1) % total
            self.count_routing("smart_pick")
            account.mark_pick(now)
            return account

        # 临期积分优先：窗口内的账号先接单，窗口内按「临期余额除以剩余天数」
        # 加权轮询。
        while True:
            urgent = self._pick_expiring_first(snapshot, exclude, model)
            if urgent is None:
                break
            if _claim(urgent, model, claim):
                return take(urgent)
            exclude.add(urgent.uid)
        # 临期层已把拿不到名额的账号加进 exclude，候选集按最新 exclude 重算。
        candidates = [index for index in candidates
                      if snapshot[index].uid not in exclude]
        if not candidates:
            return None
        targets = {index: snapshot[index].credit_target_per_day(now=now) for index in candidates}
        known = sorted(t for t in targets.values() if t is not None)
        # 还没查过余额的账号按候选账号的中位数对待，否则它的义务是 0，会在
        # 别的账号还在追赶时一直排在最后。
        if not known:
            median = 0.0
        else:
            mid = len(known) // 2
            median = known[mid] if len(known) % 2 else (known[mid - 1] + known[mid]) / 2.0
        backlog = {index: max(0.0, (median if targets[index] is None else targets[index])
                              - (snapshot[index].credit_rate_per_day or 0.0))
                   for index in candidates}
        total_backlog = sum(backlog.values())
        loads = {index: snapshot[index].recent_load(now) for index in candidates}
        total_hour = sum(loads.values())
        weights = self._remaining_weights
        ranked = []
        for index in candidates:
            share = (backlog[index] / total_backlog) if total_backlog > 0 else 0.0
            done = (loads[index] / float(total_hour)) if total_hour > 0 else 0.0
            # 剩余接近耗尽的组合往前挪一点：越接近估计限额越优先交出，但只作
            # 温和倾斜，欠账与忙闲仍然决定主要份额。
            urgency = 0.0
            if model:
                weight = weights.get((snapshot[index].uid, model), 1)
                urgency = max(0, weight - 1) * REMAINING_URGENCY_STEP
            # 评分相同时欠账多的先派，没有欠账的账号不会靠平局拿到新对话。
            ranked.append((done - share - urgency, -share, loads[index],
                           snapshot[index].priority, (index - start) % total, index))
        ranked.sort()
        for _score, _share, _hour, _priority, _offset, index in ranked:
            account = snapshot[index]
            if account.ready(model=model) and _claim(account, model, claim):
                with self._lock:
                    self._cursor = (index + 1) % total
                self.count_routing("smart_pick")
                account.mark_pick(now)
                return account
        return None

    def _rotate_pick(self, group, exclude, model, cursor_key=None, claim=None):
        """对一组账号轮询选号，推进它的游标。

        整池轮转用一直存在的 `_cursor`。页轮转每页各有游标，页内选号与普通
        选号互不挪动对方的位置。
        """
        total = len(group)
        if total == 0:
            return None
        with self._lock:
            start = (self._page_cursors.get(cursor_key, 0) if cursor_key is not None
                     else self._cursor)
        # 优先级数字小的先被选中，数字相同按轮询顺序。
        order = sorted(range(total), key=lambda i: (group[i].priority,
                                                    (i - start) % total))
        for index in order:
            account = group[index]
            if account.uid in exclude:
                continue
            if account.ready(model=model) and _claim(account, model, claim):
                with self._lock:
                    if cursor_key is not None:
                        self._page_cursors[cursor_key] = (index + 1) % total
                    else:
                        self._cursor = (index + 1) % total
                self.count_routing("cursor_pick")
                account.mark_pick()
                return account
        return None

    def representative(self, realm=None):
        with self._lock:
            candidates = [a for a in self.accounts if not realm or a.realm == realm]
            for account in candidates:
                if account.access_token: return account
            return candidates[0] if candidates else None

    def start_login(self, realm, product):
        """向上游申请授权链接，platform 参数与所选官方客户端一致。

        product 同时记下来，登录完成后成为新账号的出站身份，登录用的客户端与
        之后对话用的客户端保持同一个。
        """
        product = wb_identity.normalize_product(product)
        platform = wb_identity.login_platform(realm, product)
        endpoint, domain = wb_identity.endpoint_for(realm, product)
        url = "%s%s?platform=%s" % (endpoint, AUTH_STATE_PATH,
                                    urllib.parse.quote(platform))
        headers = login_headers(realm, product)
        payload = http_json(url, data=b"{}", method="POST", headers=headers,
                            timeout=30, retries=3, log=self.log)
        data = payload.get("data") or {}
        state = data.get("state")
        auth_url = data.get("authUrl")
        if not state or not auth_url:
            raise RuntimeError("auth/state returned no state/authUrl: %s" % payload)
        with self._lock:
            self.logins[state] = {"created": time.time(), "platform": platform,
                                  "product": product, "realm": realm}
        return {"state": state, "authUrl": auth_url, "realm": realm,
                "domain": domain, "platform": platform, "product": product}

    def poll_login(self, state):
        state = str(state or "").strip()
        with self._lock:
            info = self.logins.get(state)
        if not info:
            return {"status": "unknown", "message": "state not recognised - start the login again"}
        if time.time() - info["created"] > LOGIN_TTL_SECONDS:
            with self._lock: self.logins.pop(state, None)
            return {"status": "expired", "message": "login window expired - start again"}
        realm = info.get("realm") or "intl"
        url = "%s%s?state=%s" % (wb_identity.endpoint_for(realm, info["product"])[0],
                                 AUTH_TOKEN_PATH, urllib.parse.quote(state))
        headers = login_headers(realm, info["product"], method="GET")
        try:
            payload = http_json(url, method="GET", headers=headers, timeout=30, retries=2)
        except Exception as exc:
            return {"status": "pending", "message": "poll error: %s" % exc}
        code = payload.get("code")
        if code == LOGIN_PENDING:
            return {"status": "pending", "message": payload.get("msg") or "waiting for browser login"}
        if code != 0:
            return {"status": "error", "message": "code=%s msg=%s" % (code, payload.get("msg"))}
        data = payload.get("data") or {}
        token = data.get("accessToken")
        if not token:
            return {"status": "pending", "message": "waiting for token"}
        uid = jwt_uid(token)
        nickname = ""
        try:
            acct_url = "%s%s?state=%s" % (
                wb_identity.endpoint_for(realm, info["product"])[0],
                LOGIN_ACCOUNT_PATH, urllib.parse.quote(state))
            # 拉昵称是带凭据的请求：官方只去掉 X-No-Authorization，其余三个
            # 标记头与链路头保留，也不额外补 X-User-Id / X-Domain。
            acct_headers = dict(headers)
            acct_headers.pop("X-No-Authorization", None)
            acct_headers["Authorization"] = "Bearer " + token
            req_acct = wb_upstream_pool.Request(acct_url, method="GET", headers=acct_headers)
            with urlopen(req_acct, timeout=15) as resp_acct:
                profile = json.loads(resp_acct.read().decode("utf-8"))
                profile_data = profile.get("data") or {}
                nickname = str(profile_data.get("nickname") or "")
        except Exception: pass
        account = Account({
            "uid": uid,
            "nickname": nickname or uid[:8],
            "domain": data.get("domain") or wb_identity.endpoint_for(
                realm, info["product"])[1],
            "realm": realm,
            "platform": info["platform"],
            "product": info["product"],
            "loginProduct": info["product"],
            "accessToken": token,
            "refreshToken": data.get("refreshToken") or "",
            "expiresAt": normalize_epoch(data.get("expiresAt")) or jwt_exp(token),
            "source": "oauth",
            "enabled": True,
        })
        self.add(account)
        if realm == "cn":
            try: account.checkin(trigger="account_add")
            except Exception: pass
        with self._lock: self.logins.pop(state, None)
        return {"status": "ok", "account": account.public()}

    def cancel_login(self, state):
        with self._lock: return self.logins.pop(state, None) is not None

    def import_desktop_credential(self, path=None, realm=None, source="desktop-app"):
        if not path:
            found = []
            candidates = desktop_credential_candidates()
            for p, r in candidates:
                if realm and r != realm: continue
                try:
                    acc = self.import_desktop_credential(path=p, realm=r, source=source)
                    if acc: found.append(acc)
                except Exception: pass
            return found
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        auth = blob.get("auth") or {}
        profile = blob.get("account") or {}
        token, key = desktop_decrypt_tokens(auth, log=self.log)
        if not token: raise RuntimeError("no accessToken in %s" % path)
        detected_realm = desktop_effective_realm(realm, token, auth.get("domain"))
        if realm and detected_realm != realm:
            self.log("desktop credential %s is a %s account (file hints %s) - importing as %s"
                     % (os.path.basename(path), detected_realm, realm, detected_realm))
        nickname = profile.get("nickname")
        if key is not None and wb_atrest.is_envelope(nickname):
            # 国内版桌面端把昵称也一起加密了，能解就顺手解出来，省得只显示 UID 前缀
            nickname = wb_atrest.decrypt_field(key, nickname) or ""
        account = Account({
            "uid": profile.get("uid") or jwt_uid(token),
            "nickname": nickname if isinstance(nickname, str) else "",
            "domain": domain_for_realm(detected_realm, auth.get("domain")),
            "realm": detected_realm,
            "platform": "CLI",
            "product": wb_identity.PRODUCT_DESKTOP,
            "loginProduct": wb_identity.PRODUCT_DESKTOP,
            "enterpriseId": profile.get("enterpriseId") or "",
            "accessToken": token,
            "refreshToken": desktop_refresh_token(auth, key),
            "expiresAt": normalize_epoch(auth.get("expiresAt")) or jwt_exp(token),
            "source": source,
            "enabled": True,
        })
        self.add(account)
        if detected_realm == "cn":
            try: account.checkin(trigger="account_import")
            except Exception: pass
        return account

def desktop_atrest_key(force=False, log=None):
    """拿到桌面端 at-rest 密钥（内存里有就直接用，没有才去进程内存里找回）。

    只有真的遇到加密信封才会走到这里，所以旧客户端 / 明文凭据完全不受影响。
    回收失败（客户端没开、系统不是 Windows、权限不够）时抛 wb_atrest.AtRestError，
    由调用方翻译成给用户看的话。
    """
    return wb_atrest.recover_key(force=force, log=log or (lambda msg: None))


def desktop_decrypt_tokens(auth, log=None):
    """返回 (accessToken 明文, 密钥或 None)。

    桌面客户端把 token 存成 `$wbEncrypted` 信封，这里统一在读取处解密；
    仍然是明文的客户端原样返回、不去碰密钥。
    """
    raw = auth.get("accessToken")
    if not wb_atrest.is_envelope(raw):
        return str(raw or ""), None
    key = desktop_atrest_key(log=log)
    return wb_atrest.decrypt_field(key, raw) or "", key


def desktop_refresh_token(auth, key):
    """refreshToken 同样可能是信封；解不开时留空（有 accessToken 就还能跑）。"""
    raw = auth.get("refreshToken")
    if not wb_atrest.is_envelope(raw):
        return raw if isinstance(raw, str) else ""
    if key is None:
        return ""
    try:
        return wb_atrest.decrypt_field(key, raw) or ""
    except Exception:
        return ""


def desktop_auth_dirs():
    """桌面客户端可能存放 *.info 凭据的目录。

    Windows 用 %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth；其他
    平台探测 Application Support 下可能的程序名。目录不存在也无妨：调用方
    只读取存在的文件。
    """
    dirs = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            local = os.path.join(os.path.expanduser("~"), "AppData", "Local")
        dirs.append(os.path.join(local, "CodeBuddyExtension", "Data", "Public", "auth"))
    else:
        home = os.path.expanduser("~")
        base = (os.path.join(home, "Library", "Application Support")
                if sys.platform == "darwin"
                else os.path.join(home, ".local", "share"))
        for app in ("CodeBuddyExtension", "WorkBuddy", "CodeBuddy"):
            dirs.append(os.path.join(base, app, "Data", "Public", "auth"))
            dirs.append(os.path.join(base, app, "auth"))
    return dirs

def desktop_credential_candidates():
    out = []
    for base in desktop_auth_dirs():
        if not os.path.isdir(base): continue
        for name, realm in (("workbuddy-desktop-ai.info", "intl"),
                            ("workbuddy-desktop.info", "cn")):
            path = os.path.join(base, name)
            if os.path.isfile(path) and (path, realm) not in out:
                out.append((path, realm))
    return out


def scan_desktop_credentials():
    """描述这台机器上找到的桌面客户端凭据。

    只读：不会往池子里加任何东西。看板展示结果，由用户决定导入哪些，代理不会
    悄悄接管桌面端的登录。

    凭据可能是加密信封，得先把密钥回收回来才能读。这里只查内存里已有的密钥，
    不触发回收（回收要扫客户端进程内存，得由用户在弹窗里显式点一次），所以
    扫描总是很快返回。
    """
    found = []
    key = None
    for path, realm in desktop_credential_candidates():
        cfg = get_realm_config(realm)
        item = {
            "path": path,
            "file": os.path.basename(path),
            "realm": realm,
            "realmName": cfg["name"],
            "domain": cfg["domain"],
            "readable": False,
            "valid": False,
            "encrypted": False,
            "needsKey": False,
            "uid": "",
            "nickname": "",
            "expiresAt": 0,
            "error": "",
        }
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
            auth = blob.get("auth") or {}
            profile = blob.get("account") or {}
            raw = auth.get("accessToken")
            item["readable"] = True
            if wb_atrest.is_envelope(raw):
                item["encrypted"] = True
                if key is None:
                    try:
                        key = wb_atrest.cached_key()
                    except Exception:
                        key = None
                if key is None:
                    item["needsKey"] = True
                    item["error"] = ("凭据已加密：先点「回收密钥」，再回来导入"
                                     if wb_atrest.hunt_supported()
                                     else "凭据已加密：" + wb_atrest.hunt_hint())
                    found.append(item)
                    continue
                token = wb_atrest.decrypt_field(key, raw) or ""
            else:
                token = str(raw or "")
            if not token:
                item["error"] = "no accessToken inside the file"
                found.append(item)
                continue
            nickname = profile.get("nickname")
            if key is not None and wb_atrest.is_envelope(nickname):
                nickname = wb_atrest.decrypt_field(key, nickname)
            exp = normalize_epoch(auth.get("expiresAt")) or jwt_exp(token) or 0
            # 区域以 token 为准（文件名只兜底），跟导入时用的是同一套判断，
            # 免得列表里标着国内版、导入却跑进国际版
            item_realm = desktop_effective_realm(realm, token, auth.get("domain"))
            item_cfg = get_realm_config(item_realm)
            item.update({
                "valid": True,
                "uid": profile.get("uid") or jwt_uid(token),
                "nickname": nickname if isinstance(nickname, str) else "",
                "realm": item_realm,
                "realmName": item_cfg["name"],
                "realmHint": realm if item_realm != realm else "",
                "domain": domain_for_realm(item_realm, auth.get("domain")),
                "expiresAt": exp,
                "expiresIn": _human_delta(exp - time.time()) if exp else None,
            })
        except Exception as exc:
            item["error"] = str(exc)
        found.append(item)
    return found


# --------------------------------------------------------------- 导出 / 导入
#
# 账号以单个 JSON 文档流转，账号池因此可以在机器间迁移（或备份），无须手动
# 进出账号目录。导入接受的容器：
#   1. 本模块自己的导出 -> {"format": "workbuddy-accounts", "accounts": [...]}
#   2. 裸列表          -> [ {...}, {...} ]           （手写）
#   3. 单个账号对象    -> {...}                       （一次性粘贴）
# 桌面端凭据（{"auth": {...}, "account": {...}}）同样接受。

EXPORT_FORMAT = "workbuddy-accounts"
EXPORT_VERSION = 1

# 描述运行期状态的字段，与凭证本身无关。导出供检查，但导入时不采信：另一台
# 机器带来的陈旧冷却或停用标记会悄悄拖垮目标账号池。
VOLATILE_FIELDS = ("cooldownUntil", "lastError", "credits", "lastCheckin",
                   "lastDailyChat", "lastActivityReport", "modelCaps")

# VOLATILE_FIELDS 中完全不进本地凭证文件的子集：加载时不采信，save() 也不写，
# 与 model_cooldowns 同款。导出仍带上它们（见 to_dict()）。
RUNTIME_ONLY_FIELDS = ("lastError", "cooldownUntil")


def api_key_account(key, nickname="", uid=""):
    """构造静态 API Key 账号；有效性由调用方 probe_api_key() 判定。"""
    return Account({
        "uid": str(uid or "").strip(),
        "nickname": str(nickname or "").strip(),
        "accessToken": str(key or "").strip(),
        "realm": "cn",
        "platform": "CLI",
        "product": "cli",
        "loginProduct": "cli",
        "source": SOURCE_API_KEY,
        "apiKey": True,
        "enabled": True,
    })


def account_to_export(account):
    """为导出文档序列化一个账号。"""
    data = account.to_dict()
    data.pop("path", None)
    return data


def build_export_document(accounts, realm=None, include_secrets=True, uids=None):
    """把账号包进自描述的导出文档。

    `uids` 把导出限定到指定账号，在区域过滤之上再收窄，所以指定的账号不在
    该区域时得到空文档，不会导出错误的账号。
    """
    wanted = None
    if uids is not None:
        wanted = {str(u) for u in uids}
    rows = []
    for account in accounts:
        if realm and account.realm != realm:
            continue
        if wanted is not None and account.uid not in wanted:
            continue
        row = account_to_export(account)
        if not include_secrets:
            row.pop("accessToken", None)
            row.pop("refreshToken", None)
        rows.append(row)
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "accounts": rows,
    }


def _coerce_account_rows(blob):
    """把任何可接受的容器归一成账号字典列表。

    返回 (rows, error)。接受导出文档、裸列表、单个账号对象或桌面端凭据。
    """
    if isinstance(blob, list):
        rows = blob
    elif isinstance(blob, dict) and isinstance(blob.get("accounts"), list):
        # 本模块自己的导出文档（或任何带 accounts 数组的对象）。
        rows = blob["accounts"]
    elif isinstance(blob, dict):
        # 单个账号对象或桌面端凭据（{"account": {...}, "auth": {...}}）；
        # 其余形状一律报错，不能当成一个账号处理。
        looks_like_account = (
            blob.get("accessToken")
            or isinstance(blob.get("auth"), dict)
            or isinstance(blob.get("account"), dict)
        )
        if not looks_like_account:
            keys = ", ".join(sorted(blob.keys())[:6]) or "none"
            return [], ("not an account document (expected an accounts array, "
                        "a list, or an account object; got keys: %s)" % keys)
        rows = [blob]
    else:
        return [], "expected an object or a list of accounts"

    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return [], "account #%d is not an object" % (index + 1)
        out.append(row)
    if not out:
        return [], "no accounts found in the document"
    return out, ""


def normalise_import_row(row, realm=None):
    """把一条导出或外来行转成 Account 的 kwargs。

    扁平账号结构与嵌套的桌面凭据结构都接受。行里没有可用凭证时抛 ValueError。
    """
    # cockpit tools 导出的是裸的 snake_case OAuth 行数组。映射成本函数已支持的
    # 扁平结构；expires_at 是毫秒，下面的 normalize_epoch() 会换成秒。
    if (isinstance(row, dict) and "access_token" in row
            and "accessToken" not in row):
        row = {
            "uid": row.get("uid"),
            "nickname": row.get("nickname") or row.get("email") or "",
            "domain": row.get("domain"),
            "accessToken": row.get("access_token"),
            "refreshToken": row.get("refresh_token"),
            "expiresAt": row.get("expires_at"),
            "source": "cockpit",
        }
    auth = row.get("auth") if isinstance(row.get("auth"), dict) else None
    profile = row.get("account") if isinstance(row.get("account"), dict) else None

    def pick(key, default=None):
        """从持有该字段的层读取（扁平行、auth、account）。"""
        for layer in (row, auth, profile):
            if isinstance(layer, dict) and layer.get(key) not in (None, ""):
                return layer.get(key)
        return default

    token = _access_token_value(pick("accessToken"))
    if pick("apiKey") is True and not wb_identity.is_api_key_token(token):
        raise ValueError("apiKey is set but accessToken is not a ck_/pt_ key")
    api_key = wb_identity.is_api_key_token(token)

    # 账号归属的区域。调用方强制的区域优先（API 选项就是干这个的），否则由
    # token / 域名判定：行里带的过期 realm（曾被归错区域、又导出过的账号）
    # 不能把它拽回错的区域，错区域会把每个请求发到错误的上游。
    forced = str(realm or "").strip().lower()
    evidence = realm_evidence(token, pick("domain"))
    row_realm = str(pick("realm") or "").strip().lower()
    if api_key:
        detected = "cn"
    elif forced in ("intl", "cn"):
        detected = forced
    elif evidence:
        detected = evidence
    elif row_realm in ("intl", "cn"):
        detected = row_realm
    else:
        detected = detect_realm_from_token(token, pick("domain"))

    raw_uid = str(pick("uid") or "").strip()
    if not raw_uid and api_key:
        derived = wb_identity.api_key_id(token)
        raw_uid = ("apikey-" + derived) if derived else ""
    if not raw_uid:
        raw_uid = jwt_uid(token)
    uid = re.sub(r"[^A-Za-z0-9_-]", "_", raw_uid).strip("_ ")
    if not uid:
        raise ValueError("cannot determine uid (no uid field and no sub claim)")

    kwargs = {
        "uid": uid,
        "nickname": str(pick("nickname") or ""),
        "domain": (get_realm_config("cn")["domain"] if api_key
                   else domain_for_realm(detected, pick("domain"))),
        "realm": detected,
        "platform": str(pick("platform") or "CLI"),
        "enterpriseId": str(pick("enterpriseId") or ""),
        "accessToken": token,
        "apiKey": bool(api_key),
        "refreshToken": ("" if api_key else str(pick("refreshToken") or "")),
        "expiresAt": (0 if api_key
                      else normalize_epoch(pick("expiresAt")) or jwt_exp(token)),
        "source": str(pick("source")
                      or (SOURCE_API_KEY if api_key else "import")),
        "enabled": True,
        # 运行期状态刻意重置，见 VOLATILE_FIELDS。
        "lastError": "",
        "cooldownUntil": 0.0,
    }
    if api_key:
        kwargs["product"] = str(pick("product") or "cli")
    elif pick("product"):
        kwargs["product"] = str(pick("product"))
    if pick("loginProduct"):
        kwargs["loginProduct"] = str(pick("loginProduct"))
    elif auth is not None or profile is not None:
        kwargs["loginProduct"] = wb_identity.PRODUCT_DESKTOP
    # 优先级随导出的文档一起走，文档里没写就不放进 kwargs：AccountPool.add()
    # 据此判断该不该覆盖已存在账号的同名字段。
    priority = pick("priority")
    if priority is not None:
        kwargs["priority"] = priority
    note = pick("note")
    if note is not None:
        kwargs["note"] = _stored_note(note)
    concurrency = pick("concurrencyLimit")
    if concurrency is not None:
        kwargs["concurrencyLimit"] = _stored_concurrency_limit(concurrency)
    return kwargs


# ------------------------------------------------------------------ 面板视图

def _panel_gateway():
    """网关模块：账号池与日志都在它那里。"""
    gateway = wb_host.loaded_gateway("log", "POOL")
    if gateway is None:
        import wb_proxy as gateway
    return gateway


def account_views(realm=None):
    """账号列表视图，带上实时可用状态。"""
    gateway = _panel_gateway()
    if not gateway.POOL:
        return []
    return gateway.POOL.list_public(realm=realm)


def account_limits():
    """面板与网关共用一套账号字段上限。"""
    return {
        "priorityDefault": DEFAULT_ACCOUNT_PRIORITY,
        "priorityMax": MAX_ACCOUNT_PRIORITY,
        "concurrencyMax": MAX_ACCOUNT_CONCURRENCY,
        "weightMin": 1,
        "weightMax": MAX_ACCOUNT_WEIGHT,
        "noteMax": MAX_ACCOUNT_NOTE,
        "apiKeyPrefixes": list(wb_identity.API_KEY_PREFIXES),
    }


def import_desktop_accounts(realm=None):
    """导入本机桌面客户端保存的凭据，返回导入的账号。"""
    imported = []
    gateway = _panel_gateway()
    for path, cred_realm in desktop_credential_candidates():
        if realm and cred_realm != realm:
            continue
        try:
            account = gateway.POOL.import_desktop_credential(path=path, realm=cred_realm)
            imported.append(account)
            gateway.log("imported %s (%s) from %s" % (account.uid[:8], account.realm, os.path.basename(path)))
        except Exception as exc:
            gateway.log("skip %s: %s" % (os.path.basename(path), exc))
    return imported


def desktop_credential_scan():
    """只读扫描本机的桌面客户端凭据。"""
    return scan_desktop_credentials()
