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
import wb_identity
import wb_settings
import wb_upstream_pool
import wb_webagent

# ---------------------------------------------------------------------------
# 网页版通道（issue #75 / #59 / #90）
#
# 网页版 app 的「对话」走 console/as 下的 agent 会话，只用 Authorization: Bearer
# <accessToken> 与 X-User-Id 两个凭据头，没有桌面端的 X-IDE-* 指纹。
#
# 建会话只是排队：agent 要等客户端接上沙箱（GET .../{id}/session 返回的
# link + token）并请求这一轮才会跑，否则会话停在 CREATING 且没有输出（#90 实测）。
# 顺序是 建会话 → 取 session → ACP over HTTP+SSE（实现见 wb_webagent）；每日活跃
# 奖励只认跑完的 agent 会话，因此国际版打卡在桌面端对话之外再走一次并跑到 completed。
# ---------------------------------------------------------------------------
WEB_ORIGIN = "https://www.workbuddy.ai"
WEB_ORIGIN_CN = "https://www.workbuddy.cn"
WEB_CONVERSATIONS_URL = WEB_ORIGIN + "/console/as/conversations/"
PROFILE_PATH = "/console/account"
WEB_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")
DAILY_CHAT_MODEL = "deepseek-v4.1-flash"
DAILY_CHAT_WEB_PROMPT = "Hi"
#: 网页通道一轮最多等多久（秒）。实测一次「Hi」十几秒就跑完，留足余量。
#: 没配 settings 的账号（如单测里手工构造的）用它兜底。
WEB_TURN_TIMEOUT = int(os.environ.get("WB_WEB_TURN_TIMEOUT") or "120")
#: 网页通道一共试几次（含第一轮）。第 N 轮的等待是首轮的 2^(N-1) 倍，所以
#: 3 次 = 最多等 base×(1+2+4) 秒。设置页可改（见 wb_settings.daily_chat_web_attempts）。
WEB_TURN_ATTEMPTS = 3


def _retryable(exc):
    """Transient network faults worth another attempt (TLS resets, timeouts, 5xx)."""
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
    """Web-console origin per realm (CN vs international)."""
    return WEB_ORIGIN_CN if str(realm or "").lower() == "cn" else WEB_ORIGIN


def fetch_account_profile(account, timeout=15):
    """Web-console account profile -> (uid, nickname), nothing else.

    The endpoint also returns phoneNumber and other personal fields; this
    function parses only uid and nickname, so sensitive values never enter
    logs, responses or storage. A uid mismatch against the credential raises
    (wrong-account guard).
    """
    origin = web_origin_for(getattr(account, "realm", ""))
    req = urllib.request.Request(
        origin + PROFILE_PATH, method="GET",
        headers={
            "Authorization": "Bearer " + str(account.access_token or ""),
            "Accept": "application/json, text/plain, */*",
            "x-client-platform": "web",
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


def opener_for_proxy(proxy):
    """Build (and cache) a urllib opener bound to one outbound proxy.

    Empty/blank means "direct", signalled by None so callers can fall back to
    the process default opener. One opener per account keeps each account's
    traffic pinned to its own exit IP instead of sharing a rotating pool.
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    with _OPENER_LOCK:
        opener = _OPENER_CACHE.get(proxy)
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            _OPENER_CACHE[proxy] = opener
        return opener


def urlopen(req, timeout=30, proxy=""):
    """urlopen honouring an optional per-account proxy, over a keep-alive pool.

    urllib 的 AbstractHTTPHandler.do_open 会写死 Connection: close，每个请求都得
    重新 TCP+TLS 握手（真机实测 118.0ms，是请求路径上最大的一笔开销）。默认改走
    wb_upstream_pool：按（目标, 代理串）分池复用连接。不能逐字节复现 urllib 语义
    的请求（非 http(s) 目标、非 HTTP 代理、需要跟随的 3xx 重定向）由 PoolBypass
    回退到下面这条原来的路，行为与改动前一致；面板「高级」设置关闭连接复用
    时整条路径都与改动前相同。
    """
    if wb_upstream_pool.enabled():
        try:
            return wb_upstream_pool.urlopen(req, timeout=timeout, proxy=proxy)
        except wb_upstream_pool.PoolBypass:
            pass
    opener = opener_for_proxy(proxy)
    if opener is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return opener.open(req, timeout=timeout)


def http_json(url, data=None, method=None, headers=None, timeout=30,
              retries=3, backoff=1.0, log=None, proxy=""):
    """urlopen + json decode with retries.

    Chinese networks and CDN edges routinely drop a TLS handshake with
    "SSL: UNEXPECTED_EOF_WHILE_READING"; a single retry almost always
    succeeds, so every upstream call goes through here.
    """
    attempts = max(1, int(retries or 1))
    last = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
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
        # 5.8.4 实测（2026-10-16 抓包）：桌面端 UA 是三段式，末段是客户端内置
        # CLI 的版本；取值与 wb_identity.DESKTOP_UA_INTL 保持同一来源。
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
        # 国内版 5.9.3：product.json 的 applicationName 与 productName 都是
        # WorkBuddy，内置 CLI 版本 2.165.0；版本号同样只从 wb_identity 取。
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

#: token / 域名里能认出区域的字样。国内版历史上换过出口：copilot.tencent.com →
#: codebuddy.cn → workbuddy.cn（新版国内客户端的 JWT issuer 就是
#: https://www.workbuddy.cn/…），三个都得认；只认前两个会把 workbuddy.cn 的
#: 国内账号判成国际版——手动导入 JSON 时"明明是国内的却进了国际版"就是这么来的。
CN_REALM_MARKERS = ("copilot.tencent.com", "codebuddy.cn", "workbuddy.cn")
INTL_REALM_MARKERS = ("workbuddy.ai", "codebuddy.ai")
REALM_MARKERS = {"cn": CN_REALM_MARKERS, "intl": INTL_REALM_MARKERS}

#: 「限额联动」只覆盖免费模型（用户口径，2026-10-10）：只有这些模型的 6004
#: 撞线才做「记录 → 跨重启禁用」；付费模型不参与——它们的消耗按积分计费，
#: 不在这套 24h 窗口配额里。键与 Account.realm 的取值一致（intl / cn），
#: 国内版的 deepseek-v4.1-flash 是收费模型（x0.11），不在名单内。
FREE_CAP_MODELS = {
    "intl": frozenset({"hy4-preview-f", "hy3", "deepseek-v4.1-flash"}),
    "cn": frozenset({"hy4-preview-f", "hy3"}),
}


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


def is_free_cap_model(realm, model):
    """该「账号区域 × 模型」是否在限额联动的范围内。

    未知区域或空模型一律不在：范围判断必须失败关闭，宁可漏记一次也不把
    付费模型拉进持久化禁用。
    """
    return bool(model) and str(model) in FREE_CAP_MODELS.get(str(realm or ""), ())

#: 上游用一个远超任何真实计费周期的抵扣截止时间表示「不会过期」。实测
#: （2026-10-08，31 行真实包数据）Free Plan Subscription 与个人体验版的
#: DeductionEndTime 落在 2034/2035 年，而真实包周期是 14 天或 1 个月。超过
#: 这个天数就按「不过期」处理，不再参与到期倒计时与临期判定。
EXPIRY_SENTINEL_DAYS = 730


def deduction_end_text(acc):
    """包的「抵扣截止时间」——积分到这个点就不能再抵扣，即作废时刻。

    取上游的 DeductionEndTime（epoch 毫秒）。上游没给就返回空串，调用方
    回退到 CycleEndTime。实测 Bonus Pack / 裂变包 / 体验版的 CycleEndTime
    与 DeductionEndTime 完全一致，但免费包/体验版这类包的 CycleEndTime 只是
    每月的计费周期边界，两者能差 8 年——所以到期要认这个字段。
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
    """X-Device-Token fallback: a literal value, or a desktop token file.

    Mirrors the panel's device_token.go: the desktop client writes its
    token to a file, the gateway reads it (max 1KB, trimmed) and caches
    the result for five minutes so the header hot path stays cheap.
    Failures degrade to an empty token instead of breaking a request.
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
    """Epoch of the next local 04:00.

    The daily reset that puts CN credits back happens overnight, so the
    04:00 wall is what an out-of-credits park waits for. A timestamp already
    past 04:00 rolls to tomorrow, so a deadline is never reused in place.
    """
    now = time.time() if now is None else now
    lt = time.localtime(now)
    stamp = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                         4, 0, 0, 0, 0, -1))
    if stamp <= now:
        stamp += 86400
    return stamp


# Account-level failure governance. A repeatedly soft-limited credential
# backs off exponentially instead of being retried on every request; a
# credential that keeps failing hard trips a breaker; unknown failures degrade
# it for a while. The counters live on Account, these helpers only turn a
# count into seconds (panel project's pool semantics).
SOFT_RATE_BASE = 600.0          # first verified account-level 429: 10 minutes
SOFT_RATE_MAX = 7200.0          # ... doubling up to 2 hours
# An *unscoped* 429 - the body names no reset clock, so nothing in it says
# whether the limit is on the credential or on one model - buys a window on that
# one model instead, shorter and capped at the credential ladder's first tier.
# The bare "usage exceeds frequency limit" body is the most common 429 the
# upstream sends (229 of the 263 in the incident environment), so it must not be
# able to park a credential for two hours.
SOFT_RATE_UNVERIFIED_BASE = 60.0
SOFT_RATE_UNVERIFIED_MAX = 600.0
BREAKER_THRESHOLD = 3           # consecutive hard failures before the breaker
BREAKER_COOLDOWN = 1800.0       # first breaker window: 30 minutes
BREAKER_COOLDOWN_MAX = 21600.0  # ... doubling up to 6 hours
# 上面那个「连续」必须有时间含义：老实现只认次数、不认间隔，fails 只有成功才
# 归零，于是同一条凭据几小时前的一次 502 + 几天前的一次超时 + 刚才一次抖动就凑成
# 「连续 3 次」，把账号关 30 分钟——而且关得越久下次越容易再凑满（1800 → 3600 →
# 7200…）。生产上的表现就是「明明测了能用，它还熔断」。现在两次失败间隔超过这个
# 窗口就重新从 1 数起，连续才真的是连续。可用 WB_FAIL_WINDOW 覆盖，设 0 关闭衰减
# （回到旧的累计行为）。
try:
    FAIL_WINDOW = max(0.0, float(os.environ.get("WB_FAIL_WINDOW") or 600.0))
except (TypeError, ValueError):
    FAIL_WINDOW = 600.0
DEGRADE_THRESHOLD = 5           # consecutive unknown failures before degrading
DEGRADE_COOLDOWN = 600.0        # first degrade window: 10 minutes
DEGRADE_COOLDOWN_MAX = 7200.0   # ... doubling up to 2 hours


def _exponential_backoff(count, base, cap, offset):
    """base * 2 ** (count - offset), clamped to cap and to 20 steps."""
    step = max(0, int(count) - int(offset))
    return min(float(base) * (2 ** min(step, 20)), float(cap))


def soft_backoff(streak):
    """Cooldown seconds for `streak` consecutive account-level soft limits."""
    return _exponential_backoff(streak, SOFT_RATE_BASE, SOFT_RATE_MAX, 1)


def unverified_soft_backoff(streak):
    """Cooldown for `streak` consecutive *unscoped* soft limits, on one model.

    Repetition still escalates, but from 60s and only up to the credential
    ladder's first tier (600s) - see SOFT_RATE_UNVERIFIED_MAX.
    """
    return _exponential_backoff(streak, SOFT_RATE_UNVERIFIED_BASE,
                                SOFT_RATE_UNVERIFIED_MAX, 1)


def breaker_backoff(fails):
    """Breaker cooldown for `fails` consecutive hard failures."""
    return _exponential_backoff(fails, BREAKER_COOLDOWN, BREAKER_COOLDOWN_MAX,
                                BREAKER_THRESHOLD)


def degrade_backoff(fails):
    """Degrade window for `fails` consecutive unknown failures."""
    return _exponential_backoff(fails, DEGRADE_COOLDOWN, DEGRADE_COOLDOWN_MAX,
                                DEGRADE_THRESHOLD)

def _access_token_value(raw):
    """取出可用的 access token；形状不对就地抛错。

    桌面端会把令牌存成加密信封（$wbEncrypted），也有把整段 JSON 当字符串塞进来
    的文件, 这些都不是网关能用的 JWT; 导入、扫描与导入行校验共用这一处判断。
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
#: Account files are hand-editable, so a priority outside this range is clamped
#: instead of skewing the selection order.
MAX_ACCOUNT_PRIORITY = 9999
#: 单账号单模型的并发上限：同一账号的每个模型各自计数，0 表示不限。
MAX_ACCOUNT_CONCURRENCY = 1000
#: 手动调度权重上限，面板与服务端共用这个值。
MAX_ACCOUNT_WEIGHT = 100
#: 评分分配按最近一小时被选中的次数判断账号忙不忙, 由 mark_pick() 记下。
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
    """Clamp a stored priority; anything unusable means the default."""
    if isinstance(value, bool):
        return DEFAULT_ACCOUNT_PRIORITY
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ACCOUNT_PRIORITY
    return max(0, min(MAX_ACCOUNT_PRIORITY, number))


def normalise_account_note(value):
    """账号备注的取值规则：去空白、限长 100 字。返回 (note, 错误说明)。

    空串表示清除备注；不是字符串或超过上限时返回说明，交给调用方决定。
    """
    if value is None:
        return "", ""
    if not isinstance(value, str):
        return "", "note must be a string"
    note = value.strip()
    if len(note) > MAX_ACCOUNT_NOTE:
        return "", ("note must be at most %d characters (got %d)"
                    % (MAX_ACCOUNT_NOTE, len(note)))
    return note, ""


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
        # The CN desktop build stores its nickname as an encrypted envelope
        # ({"$wbEncrypted": ...}) rather than plain text; anything that is not a
        # plain string is dropped and the uid prefix is shown instead.
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
        # 这个字段时从令牌自身读, 读不到就留 0。
        self.issued_at = (0 if self.is_api_key
                          else normalize_epoch(data.get("issuedAt") or jwt_iat(token)))
        # 企业账号登录时切到企业空间，企业 id 只写在 token 里，凭据文件的
        # enterpriseId 往往是空的。不回填会让每个请求都带上 X-No-Enterprise-Id: 1。
        if not self.enterprise_id and not self.is_api_key:
            self.enterprise_id = self.jwt_enterprise_id(token)
        self.added_at = data.get("addedAt") or time.time()
        self.source = str(data.get("source")
                          or (SOURCE_API_KEY if self.is_api_key else "oauth"))
        self.proxy_slot = str(data.get("proxySlot") or "").strip()
        self.proxy_legacy = str(data.get("proxy") or "").strip()
        # Runtime-resolved value; recomputed by AccountPool.apply_proxy_slots().
        self.proxy = self.proxy_legacy
        self.enabled = data.get("enabled", True)
        # 调用优先级：数字最小的先被选中, 数字相同保持原轮询顺序; 默认 100,
        # 没调过的账号池保持默认行为。会话粘性优先于优先级。
        self.priority = _normalise_priority(data.get("priority"))
        # 面板备注：跟着凭证文件走, 重新登录与桌面端导入不会带上它,
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
        # Per-model throttling. Upstream rate limits (code 6004 "usage exceeds
        # frequency limit") apply to ONE model for one account, not to the whole
        # account: other models keep working. Cooldown the offending model only,
        # otherwise a single throttled model blackholes every request on the pool.
        # Deliberately runtime-only (not persisted): see VOLATILE_FIELDS.
        self.model_cooldowns = {}
        # 每个模型冷却窗口是**什么时候挂上的**（model -> epoch）。model_cooldowns
        # 只留恢复时刻，起始时刻以前被丢掉：面板因此无法如实说「限流时刻」，只能
        # 用「—」占位，把 60s 的短时 parking 和几小时的 6004 撞线画成一样。与
        # model_cooldowns 同款运行时态：重启即清零（6004 的撞线时刻另有持久化的
        # model_caps["at"]）。
        self.model_cooldowns_at = {}
# 402 (out of credits): a hard park until the next local 04:00,
        # instead of a short cooldown that would retry an empty account all
        # day. Runtime-only, like the other throttle windows; a balance
        # refresh that shows credits again lifts it early.
        self.balance_until = 0.0
# Account-level failure streaks; see the *_backoff() helpers above.
        # All runtime-only, like the other throttle windows: a restart clears
        # them and the account gets a clean slate.
        self.soft_streak = 0
        # Unscoped soft 429s keep their own counter: the two ladders must not
        # feed each other (see note_unscoped_rate).
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
            if not is_free_cap_model(self.realm, mid):
                continue
            try:
                at = float(entry.get("at") or 0)
                reset = float(entry.get("reset") or 0)
            except (TypeError, ValueError):
                continue
            if at > 0 and reset > 0:
                self.model_caps[str(mid)] = {"at": at, "reset": reset}
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
        # The dashboard snapshots this state while request threads update it.
        # Keep it separate from _refresh_lock, which spans network requests.
        self._throttle_lock = threading.Lock()
        # 在途请求计数：键是模型名；独立锁，面板读计数时不挡请求线程。
        self._concurrency_lock = threading.Lock()
        self._in_flight = {}
        # 删除标记：面板删掉账号后, 请求线程手里的旧引用还会走到 save(),
        # 没有它就会把刚删掉的凭证文件重新写出来。
        self.deleted = False

    def to_dict(self):
        """Full view of the account, including live state.

        This is what an export is built from, so it carries the live error and
        cooldown for inspection. The local credential file must not: save()
        strips RUNTIME_ONLY_FIELDS before writing.
        """
        return {
            "uid": self.uid,
            "nickname": self.nickname,
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "product": self.product,
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
        }

    def _throttle_snapshot(self, now):
        """Return one consistent view of account and per-model cooldowns."""
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
            # 这个窗口是何时挂上的：面板据此把「限流时刻」如实画出来，而不是拿
            # 「—」占位（临时 429 与 6004 撞线都由此有真实的时间语义）。
            started = self.model_cooldowns_at.get(model)
            if started:
                entry["startedAt"] = int(started)
            # 撞线时间只在 cap 还在恢复期内时带上：它同时说明这次禁用是
            # 「额度用满」（6004）而不是一次临时限流，面板据此显示两段时间。
            if self.cap_until(model, now) > 0:
                entry["cappedAt"] = int(self.model_caps[model]["at"])
            models.append(entry)
        return error, deadline, models, detail

    def model_cooldowns_snapshot(self):
        """Active model cooldowns, ordered by recovery time (epoch seconds)."""
        return self._throttle_snapshot(time.time())[2]

    def credit_expiries(self):
        """套餐到期列表 [{at, amount, name}]，按到期时间升序。

        只收余额大于 0 且解析出到期时刻的套餐, 余额为 0 的套餐列出来只会让人
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
        """Record one selection; only the last hour is kept."""
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

        对每个到期时刻, 把在它之前到期的余额加总后除以离它的天数, 取最大值;
        已过期的套餐不计入; 查过余额但没有未到期的套餐时返回 0。
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
        # A unique temp name plus a per-account lock: two threads saving the same
        # account would otherwise share one temp file, and the loser's
        # os.replace() would fail with ENOENT.
        with self._save_lock:
            if self.deleted:
                return None
            tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
            try:
                payload = self.to_dict()
                # Live error/cooldown state stays out of the credential file;
                # writing it is what made a long-recovered 429 label reappear on
                # every restart (see RUNTIME_ONLY_FIELDS).
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

    def delete(self):
        with self._save_lock:
            self.deleted = True
            if self.path and os.path.exists(self.path):
                os.remove(self.path)

    def reserve_blocked(self):
        """True when the low-credit guard should keep this account idle.

        Only a *known* balance can block; an account whose credits were never
        fetched stays usable.
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
        """Days until the earliest credit package that still has credits left.

        None when nothing can be said: no credit data, no package with a
        usable remainder, or an end date the upstream never gave.

        Packages the upstream never really expires are skipped. The enterprise
        quota resets its allowance rather than voiding it, and the free plan /
        trial packs carry a deduction deadline years out, so both are marked
        `no_expiry` where they are built. Treating either as an expiry would
        make those accounts look permanently "about to lapse" - the free plan
        would even look urgent at every month end, when its billing cycle rolls
        over but its credits keep working.
        """
        credits = self.credits
        if not isinstance(credits, dict):
            return None
        soonest = None
        for package in (credits.get("packages") or []):
            if not isinstance(package, dict):
                continue
            # `no_expiry` is the general rule; the package_code check is a
            # belt-and-braces guard for credit blobs written before the flag
            # existed, which are still on disk until the next refresh.
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
        """True when this account should jump the dispatch queue.

        Unknown or missing credit data is never a preference: an account the
        gateway cannot judge keeps its plain round-robin turn instead of being
        pushed to the front on a guess.
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
        """True when today's counted usage has reached the configured limit.

        Only a *counted* day can block: the count is None until the proxy has
        folded the usage log once.
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
        """True when the realm catalogue marks `model` as a free one."""
        return bool(model) and model in (self.free_models or ())

    def credit_limit_reached(self):
        """True when today's counted spend reached the daily credit limit.

        The account-level fact, independent of which model is asked for:
        ready() pairs it with model_is_free() so free models keep serving,
        and the dashboard shows it as the guard's state.
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
        """True when the credit guard should keep this account off `model`.

        A free model stays available even after the cap is reached - that is
        the point of the guard. Without a model to judge (model is None) the
        account is not blocked here, because the caller cannot know whether
        the request would spend anything.
        """
        if not model or self.model_is_free(model):
            return False
        return self.credit_limit_reached()

    def note_model_cap(self, model, reset_at, now=None):
        """记下「这个模型的窗口额度被上游判定用满」（429 / code 6004）。

        持久化：6004 带的重置时刻是上游给的事实，跨重启仍然成立——否则重启后
        要等到下一次撞线才重新发现，而那个组合本来就在恢复时间内。返回是否新记
        （重复撞同一个窗口不算新，避免每次都重写凭证档）。

        只记免费模型（FREE_CAP_MODELS）：限额联动仅覆盖它们，范围外的模型
        即使收到 6004 也不写 cap、不跨重启禁用，只保留调用方给的运行时冷却。
        """
        if not model or not reset_at:
            return False
        if not is_free_cap_model(self.realm, model):
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
                # 就一直不会被禁用（真机实测）。
                if self.model_cooldowns.get(model, 0.0) < reset:
                    self.model_cooldowns[model] = reset
                return False
            self.model_caps[model] = {"at": now, "reset": reset}
            # 恢复时间之前这个模型接不了单：挂上模型冷却，分派与账号行因此
            # 立刻反映（重启后由 restore_model_caps() 重新挂）。
            self.model_cooldowns[model] = max(self.model_cooldowns.get(model, 0.0), reset)
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

    def restore_model_caps(self, now=None):
        """重启后把仍在恢复时间内的 6004 重新挂成模型冷却。

        6004 的恢复时间跨重启有效，而模型冷却是纯内存的——不挂回来的话，重启
        会让这些组合重新接单，直到下一次撞 429 才又停。范围检查再过一道：
        加载端已丢弃范围外的 cap，这里兜住直接写 model_caps 的调用方。
        """
        now = time.time() if now is None else now
        restored = []
        with self._throttle_lock:
            for mid, entry in self.model_caps.items():
                if not is_free_cap_model(self.realm, mid):
                    continue
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

    def model_token_limit_blocked(self, model=None):
        """True when `model` already burned its daily token budget today.

        Only the named model is refused; the account's other models keep
        working, and an uncounted model never blocks.
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
        """The models currently out of budget, as a set.

        Used by AccountPool.apply_model_daily_token_limit() to log only the
        models whose state actually changed instead of one line per model
        on every refresh.
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
        账号、各自卡在哪一条。生产上出现「明明有 9 个账号却报没有可用账号」时，
        缺的就是这句话——判定顺序与 ready() 保持一致，所以报出来的原因就是它
        拒绝的原因（唯一不覆盖的是临期凭证那条：它会真的发起刷新，诊断路径上
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
            # 剩余时间要精确到秒：还剩 42 秒时写「0 min」，读起来就是「已经该能用
            # 了却还被挡着」——生产日志里那串「软限流冷却 剩余 0 min」就是这么
            # 来的。连续次数一并带上，才看得出是在升级还是刚开始。
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
        # Parked below the reserve: serving a request here is what would push
        # the balance to zero and trigger the upstream reminder SMS.
        if self.reserve_blocked():
            return False
        # Today's token budget is spent: keep the seat for tomorrow instead
        # of letting the upstream answer 429 for the rest of the day.
        if self.daily_limit_blocked():
            return False
        # The day's credit spend reached the cap: paid models stop, free
        # ones keep serving (see credit_limit_blocked).
        if self.credit_limit_blocked(model):
            return False
        # One model out of budget does not take the account with it: only
        # that model is refused here.
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
        # Refresh is a last resort and its result decides availability: returning
        # True here handed out accounts whose stale credential came back 401/403.
        return self.refresh()

    def auth_headers(self):
        return wb_identity.auth_headers(self.access_token, self.is_api_key)

    def headers(self, purpose="chat"):
        """组出这一轮的出站标头。

        chat 用途走 wb_identity（CLI 头 / WorkBuddy 头，可切换）；
        billing 用途维持原本的轻量标头，计费端点不使用那套身份。

        chat 标头按桌面端对话请求整理：官方对话请求不带 X-Machine-ID /
        X-Session-ID（那两个只用于反馈上报），所以这里也不发，避免每个账号
        恒定的派生值成为一处「网关特征」。

        billing 标头仍带 X-Machine-ID / X-Session-ID，但两个都必须按账号派生：
        网关是一个进程冒充几十台设备，一旦取进程级的值，全部账号就会共用同一个
        session id，那是一条把它们串起来的关联键（与每账号一台设备的隔离口径
        相悖）。见 wb_fingerprint.session_id。
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
                "X-CodeBuddy-Request": "1",
                "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
                "X-Request-ID": generate_request_id(self.uid),
                "X-Machine-ID": derive_id(self.uid, "machine"),
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
        # The desktop CLI sends the identity headers and the JSON body only:
        # Origin / Referer / X-Requested-With / X-CodeBuddy-Request belong to
        # the panel's XHR calls and would be a gateway fingerprint on a model
        # request.
        headers = dict(identity)
        device_token = self._device_token()
        if device_token:
            headers["X-Device-Token"] = device_token
        return headers

    def _device_token(self):
        """Optional X-Device-Token from the configured literal or file."""
        directory = getattr(self, "accounts_dir", "")
        if not directory:
            return ""
        return resolve_device_token(directory)

    def sync_nickname(self):
        """Manual web-console nickname refresh.

        Only called from the explicit dashboard/API action - never from the
        balance scheduler - and persists the new nickname immediately.
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
        # Serialise refreshes per account, then re-check inside the lock: the
        # upstream rotates the refresh token, so two concurrent refreshes can
        # make the second one send a token that the first already consumed.
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
        cfg = get_realm_config(self.realm)
        url = cfg["chat_upstream"] + REFRESH_PATH
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg["billing_ua"],
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
            "X-Refresh-Token": self.refresh_token,
            "X-Auth-Refresh-Source": "workbuddy" if self.realm == "cn" else "plugin",
            "X-User-Id": self.uid,
            "X-Domain": self.domain or cfg["domain"],
            "X-CodeBuddy-Request": "1",
            "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
        }
        if self.enterprise_id:
            headers["X-Enterprise-Id"] = self.enterprise_id
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
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
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
        """Which model this account's daily activity runs on.

        The panel's setting wins when set; otherwise the built-in free id. Both
        legs (the desktop-identity completion and the web conversation) read
        this, so one setting moves the whole activity.
        """
        candidates = []
        if self.path:
            picked = wb_settings.daily_chat_model(os.path.dirname(self.path))
            if picked:
                candidates.append(picked)
        candidates.append(DAILY_CHAT_MODEL)
        # Add fallback free models for the account's realm if preferred model is cooling down
        if self.realm == "intl":
            candidates.extend(["hy3", "hy4-preview-f", "hy4-preview", "glm-5.3-flash"])
        else:
            candidates.extend(["hy3", "hy4-preview-f", "hy4-preview"])

        now = time.time()
        with self._throttle_lock:
            for cand in candidates:
                # Check model-level cooldown or cap directly
                cooldown_until = self.model_cooldowns.get(cand, 0.0)
                if cooldown_until <= now and self.cap_until(cand, now) <= 0.0:
                    return cand
        return candidates[0]

    def daily_chat_web(self, prompt=None, trigger="unknown"):
        """网页通道的每日活跃会话（issue #75 / #59 / #90）。

        只建会话不算有效对话: agent 要等客户端接上沙箱并请求这一轮才会跑, 否则
        会话停在 CREATING、没有任何输出（#90 实测）。按网页端的顺序走完:
        建会话 → 取沙箱 link+token → ACP 的 initialize / session/load /
        session/prompt（见 wb_webagent）→ 轮询到 completed。

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
        req = urllib.request.Request(
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

        # 建完只是排队：接上沙箱、请求这一轮，它才会真的跑起来（issue #90）。
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
        req = urllib.request.Request(url, headers=self.web_headers(), method="GET")
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

        两步：桌面端身份的轻量对话（一直以来的做法），以及网页通道的会话
        （issue #75/#59/#90：算数的是「跑完的 agent 会话」）。web=None 时按
        settings.json 里的 daily_chat_web 决定，True/False 可显式指定。

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
        proxy = sys.modules.get("__main__")
        if proxy is None or not hasattr(proxy, "CHAT_PATH"):
            proxy = sys.modules.get("wb_proxy")
        if proxy is None or not hasattr(proxy, "build_upstream_body"):
            import wb_proxy as proxy
        chat_path = getattr(proxy, "CHAT_PATH", "/v2/chat/completions")
        url = self.chat_base_url() + chat_path
        headers = self.headers("chat")
        body = {
            "model": self.daily_chat_model(),
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
            "max_tokens": 10,
            "reasoning_effort": "none",
        }
        req_body = proxy.build_upstream_body(body)
        data = json.dumps(req_body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
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
                    # 前几轮超时、多等了一轮才跑完（后者是慢，不是没跑）。
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
                if self.path and os.path.exists(os.path.dirname(self.path)):
                    self.save(os.path.dirname(self.path))
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
                if self.path and os.path.exists(os.path.dirname(self.path)):
                    self.save(os.path.dirname(self.path))
            return {"ok": ok, "code": code, "msg": msg, "data": payload.get("data")}
        except urllib.error.HTTPError as exc:
            try:
                raw_b = exc.read().decode("utf-8") or "{}"
                body = json.loads(raw_b)
                code = body.get("code")
                msg = body.get("msg") or ("HTTP %d" % exc.code)
                # 上游如果在今日已签到时返回 HTTP 400 且 code 为 10001 / "今天已签到"
                # 这说明今日事实上已经处于签到完成状态，按成功处理，并记录最后签到时间
                if code == 10001 or "今天已签到" in msg or "已签到" in msg:
                    self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
                    if self.path and os.path.exists(os.path.dirname(self.path)):
                        self.save(os.path.dirname(self.path))
                    return {"ok": True, "code": code, "msg": msg, "data": body.get("data")}
                return {"ok": False, "error": msg}
            except Exception:
                return {"ok": False, "error": "HTTP %d" % exc.code}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def report_activity(self):
        """国内版对话活跃上报：一条 /v2/report 点亮官方 growth 连登。

        这是客户端 chat_request_send 事件的复刻（事件形状见 wb_tasks.build_event），
        上游口径：必须带 userId，否则服务端 200 但静默丢弃；每号每天一次即可，
        不做多时点高频上报（风控口径）。成功后记 lastActivityReport 并落盘，再读
        一次 /activity/growth/streak 把连登天数带回去——读失败不影响上报本身的
        结论。只有国内版有这套成长体系，国际版直接拒绝。
        """
        if self.realm != "cn":
            return {"ok": False, "error": "activity report is only for CN realm accounts"}
        if not self.has_personal_program():
            reason = ("API key accounts have no activity programme"
                      if self.is_api_key
                      else "enterprise accounts do not support activity report")
            return {"ok": False, "error": reason}
        import wb_tasks
        # 这次上报与成长任务写入同一套成长进度，共用账号闸门，避免并发重复上报。
        lock = wb_tasks.acquire_growth(self.uid)
        if lock is None:
            return {"ok": False, "busy": True,
                    "error": "该账号正在执行其他成长任务，请稍后再试"}
        try:
            event = wb_tasks.build_event(self, "chat")
            if not wb_tasks.report_events(self, [event]):
                return {"ok": False, "error": "活跃上报被上游拒绝（未返回 code=0）"}
            self.last_activity_report = time.strftime("%Y-%m-%d %H:%M:%S")
            if self.path and os.path.exists(os.path.dirname(self.path)):
                self.save(os.path.dirname(self.path))
            days = wb_tasks.fetch_streak_days(self)
        finally:
            wb_tasks.release_growth(lock)
        if days is None:
            return {"ok": True, "msg": "对话活跃上报成功（连登天数未知）"}
        if days <= 0:
            return {"ok": True, "streak_days": 0,
                    "msg": "对话活跃上报成功，但连登仍是 0 天（可能被上游静默丢弃）"}
        return {"ok": True, "streak_days": days,
                "msg": "对话活跃上报成功（连续打卡 %d 天）" % days}

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

        # 提取创建时间（毫秒时间戳转换）
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
        # 到期认「抵扣截止时间」而不是「周期结束时间」：周期结束只是计费周期
        # 的边界，积分未必跟着作废。免费包/体验版这两个字段能差 8 年，只认
        # CycleEndTime 会让这类账号每到月底都被误判成「即将到期」。
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

        # 若具体包为空且 summary 含有汇总容量，则使用 summaryPkgs
        if not packages and summary_pkgs:
            for sp in summary_pkgs:
                tot_size += float(sp.get("CycleTotalCapacity") or 0)
                tot_remain += float(sp.get("CycleRemainCapacity") or 0)
                tot_used += float(sp.get("CycleUsedCapacity") or 0)

        tot_remain = round(tot_remain, 2)
        tot_used = round(tot_used, 2)
        tot_size = round(tot_size, 2)

        # 排序：使用中优先 -> 未过期中按到期时间升序 -> 已过期排最后
        def _pkg_sort_key(p):
            in_use_score = 0 if p.get("in_usage") else 1
            expired_score = 1 if p.get("is_expired") else 0
            days = p.get("days_left") if p.get("days_left") is not None else 99999
            if days < 0:
                days = 99999 + abs(days)
            return (in_use_score, expired_score, days, -p.get("remain", 0))

        packages.sort(key=_pkg_sort_key)

        # 查找最早到期的有效包（有剩余积分且未过期）
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

        self.credits = {
            "remain": tot_remain,
            "used": tot_used,
            "size": tot_size,
            "used_percent": f"{(tot_used / tot_size * 100):.1f}%" if tot_size > 0 else "0.0%",
            "remain_percent": f"{(tot_remain / tot_size * 100):.1f}%" if tot_size > 0 else "100.0%",
            "is_paid_user": bool(s_data.get("IsPaidUser")),
            "checkin": checkin_info,
            "earliest_expiring": earliest_expiring,
            "packages": packages,
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return {"ok": True, "credits": self.credits}

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
        self.credits = {
            "remain": tot_remain,
            "used": tot_used,
            "size": tot_size,
            "used_percent": f"{(tot_used / tot_size * 100):.1f}%" if tot_size > 0 else "0.0%",
            "remain_percent": f"{(tot_remain / tot_size * 100):.1f}%" if tot_size > 0 else "100.0%",
            "is_paid_user": False,
            "checkin": getattr(self, "credits", {}).get("checkin") if isinstance(getattr(self, "credits", None), dict) else None,
            "earliest_expiring": earliest_expiring,
            "packages": packages,
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return {"ok": True, "credits": self.credits}

    def jwt_enterprise_id(self, token=None):
        """企业空间 id，取自 token 自身（或已存的 accessToken）。

        企业账号是登录时切到企业空间换来的 token（token_source=enterprise_switch），
        账号文件里的 enterpriseId 常常是空的——只有 token 里有。拿不到就返回 ""。
        """
        return str(_jwt_claims(token or self.access_token).get("enterprise_id") or "")

    def is_enterprise(self):
        """True when this account belongs to an enterprise (team) space.

        Enterprise members have no personal resource packages, so the personal
        billing endpoint always answers TotalCount=0 and the panel shows 0/0.
        Their credit lives on /billing/meter/get-enterprise-user-usage instead.
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

        上游只返回 4 个字段：credit（本周期已消耗，实测随时间单调递增）、
        limitNum（周期额度）、cycleStartTime / cycleEndTime。没有包列表，
        所以合成一个包条目，让看板的明细弹窗与到期提示照常工作。
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
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return {"ok": True, "credits": self.credits}

    def fetch_credits(self):
        """Refresh the balance; a refresh that shows credits again also lifts
        an early 402 park (revive_balance_cooldown).

        Serialised per account: the background refresher and the sign-in /
        daily-activity tasks all land here, and without the lock two
        overlapping calls would each spend an upstream billing request and
        then race to publish whichever answer came back second.
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
        """Record refresh errors alongside the state shown in the panel."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self.last_error_detail = ""

    def note_error(self, message, cooldown=60, single_account=False, model=None, until=None,
                   detail=None):
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            # The visible label keeps its existing truncation; `detail` carries the
            # untouched upstream body so the panel can show it on hover.
            self.last_error_detail = str(detail)[:2000] if detail else ""
            if model:
                # Model-scoped throttle: keep the account usable for every other model.
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
        """402 / out of credits: park until the next local 04:00.

        A short cooldown would hand the empty account out again minutes
        later, and every request to it fails the same way; the daily reset
        is when credits come back, so that is the wall clock this waits for.
        A refresh that shows credits again can lift it early - see
        revive_balance_cooldown().
        """
        until = next_local_4am()
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self.balance_until = max(self.balance_until, until)
        return until

    def revive_balance_cooldown(self):
        """A balance refresh that shows credits again lifts the 402 park only.

        Rate-limit and model cooldowns stay exactly as they are; this is not
        a general clear_error().
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
        """Account-level rate limit: soft cooldown with exponential backoff."""
        with self._throttle_lock:
            self._decay_soft_streak()
            self.soft_streak += 1
            wait = soft_backoff(self.soft_streak)
            self.last_error = str(message)[:200]
            self.cooldown_until = max(self.cooldown_until, time.time() + wait)
        return wait

    def _decay_soft_streak(self):
        """软限流阶梯也要有时间窗（调用方已持有 _throttle_lock）。

        这两条阶梯原先只加不减、只有成功才归零：昨天被限过 3 次，今天第一个
        429 就直接从 2400s 起步，后天起步更高，直到 7200s 封顶——账号明明已经
        恢复正常（面板测试通过），却动不动被按几十分钟到两小时，这正是「不断
        软限流」的另一半。窗口内才算连续，隔得久就回到第一档（600s / 60s）。
        """
        now = time.time()
        if FAIL_WINDOW and self.last_soft_at and now - self.last_soft_at > FAIL_WINDOW:
            self.soft_streak = 0
            self.unscoped_streak = 0
        self.last_soft_at = now

    def note_unscoped_rate(self, model):
        """An unscoped soft 429: grow its own streak, return this model's window.

        Counting is deliberately separate from soft_streak. Sharing one counter
        let the ladders feed each other: four unscoped 429s pushed the first
        genuinely credential-scoped one straight to the 7200s ceiling instead of
        starting at 600s, and a credential streak made the next unscoped window
        start high as well. Each scope now has its own count, and a served
        request clears both through note_success().
        """
        with self._throttle_lock:
            self._decay_soft_streak()
            self.unscoped_streak += 1
            return unverified_soft_backoff(self.unscoped_streak)

    def note_failure(self, message):
        """5xx / transport failure: feed the breaker counter."""
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
        """Unknown error: degrade counter plus the shared breaker counter."""
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
        """A served request clears every account-level penalty."""
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
        """Seconds until this account can serve `model` again (0 = right now)."""
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
        """清掉限流状态；keep_caps 保留「上游判定额度用满」的禁用。

        换身份/重试要清的是限流窗口，而 6004 的禁用是上游给的窗口事实（恢复
        时刻之前这个组合就是不可用），不能顺手清掉——真机实测（2026-10-10）：
        auto-switch 开着时它会把刚记下的 cap 冷却一起清掉，6004 之后账号立刻
        又可用，禁用形同不存在。
        """
        with self._throttle_lock:
            if model:
                if not (keep_caps and self.cap_until(model) > 0):
                    self.model_cooldowns.pop(model, None)
                    self.model_cooldowns_at.pop(model, None)
            elif keep_caps:
                now = time.time()
                for mid in [m for m in self.model_cooldowns
                            if self.cap_until(m, now) <= 0]:
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
    """Return (limit, error) for the per-account concurrency cap.

    The cap counts in-flight requests per account and model; 0 means unlimited.
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
# 旅行、套餐到账都会体现在这里。基线就是凭证文件里的 credits, 因此重启后接着
# 比对; 首次读取只建立基线。写入目录由 wb_proxy 注入, wb_accounts 不认识 USAGE_DIR。
# ---------------------------------------------------------------------------
#: 返回积分流水文件路径的回调；未注入时一次也不记录。
CREDIT_EVENTS_PROVIDER = None


def set_credit_events_provider(provider):
    """注入「积分流水写到哪个文件」的来源，传 None 表示不记录。

    注入回调：--usage-dir 可以在启动时改变 USAGE_DIR, 每次重新求值才能一直
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

    before 为 None 表示还没有基线, 只建立基线不记流水; 写入失败直接抛出,
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
    """Which upstream account served a conversation, so the next turn can go
    back to it.

    Each conversation keeps a short most-recently-used list rather than a single
    account. The front entry is what `get()` returns - the account the last turn
    went to. The entries behind it are the accounts that served the conversation
    before that, most recent first: when the front one is unusable (cooling,
    parked, disabled) the next pick prefers an account this conversation has
    already warmed upstream over a cold one, so the prompt cache survives the
    move instead of being rebuilt from scratch. Every entry expires on its own
    clock, so an account the conversation has not visited for a while drops out
    by itself.

    The history is small on purpose (`history`, 3): it is a list of accounts
    that may still hold the conversation's prefix, not a load-balancing device.
    """

    def __init__(self, ttl=7200, max_entries=5000, history=3):
        self.ttl = ttl
        self.max_entries = max_entries
        self.history = max(1, int(history))
        self.bindings = {}
        self._lock = threading.Lock()
    def _live(self, key, now):
        """The conversation's unexpired entries, dropping the rest in place."""
        entries = self.bindings.get(key) or []
        live = [entry for entry in entries if entry[1] > now]
        if live:
            self.bindings[key] = live
        else:
            self.bindings.pop(key, None)
        return live
    def get(self, key):
        """The account the last turn went to (slides its expiry), or None."""
        if not key: return None
        with self._lock:
            now = time.time()
            live = self._live(key, now)
            if not live: return None
            uid, _exp = live[0]
            live[0] = (uid, now + self.ttl)
            return uid
    def recent(self, key):
        """Every unexpired account of this conversation, most recent first."""
        if not key: return []
        with self._lock:
            return [uid for uid, _exp in self._live(key, time.time())]
    def bind(self, key, uid):
        """Make `uid` the front entry, deduped and capped at `history`."""
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
        """Drop one account from the conversation, keeping the others.

        The failure paths use this instead of `unbind`: the account that just
        failed (429 / 402 / 401 / 5xx / dropped connection) must not be handed
        the next attempt, but the accounts before it may still hold this
        conversation's prefix upstream - the next pick should try them before
        falling back to a cold one. Dropping the front entry is the default;
        pass `uid` to drop a specific one.
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
#: bound_hit = 绑定命中且该账号接单; bound_lost = 有绑定但那个账号不能接单;
#: fresh_binding = 会话键第一次出现; no_key = 客户端没给会话标识;
#: smart_pick / cursor_pick 记录两种选号方式各被用过多少次。
ROUTING_COUNTERS = ("bound_hit", "bound_lost", "fresh_binding", "no_key",
                    "smart_pick", "cursor_pick")

#: 智能分配里「剩余接近耗尽」的倾斜量：权重每高一档，评分向前挪这么多。
#: 满档（权重 4）相当于 15 个百分点，能压过轻微的忙闲差，压不过明显欠账。
REMAINING_URGENCY_STEP = 0.05


#: 面板可改、而登录与桌面端导入数据里通常不带的字段（payload 键 -> 属性名）。
#: AccountPool.add() 替换同 uid 账号时, 只有新数据确实写了这个键才覆盖, 否则继承
#: 旧值：一次重新登录不该抹掉用户设置的优先级或出站身份。
KEEP_ON_REPLACE_FIELDS = (
    ("product", "product"),
    ("priority", "priority"),
    ("note", "note"),
    ("concurrencyLimit", "concurrency_limit"),
    ("proxySlot", "proxy_slot"),
    ("proxy", "proxy_legacy"),
    ("credits", "credits"),
    ("lastCheckin", "last_checkin"),
    ("lastDailyChat", "last_daily_chat"),
)


def _realm_limit(values, realm):
    """One guard's value for an account's realm.

    `values` is the map wb_settings.limit_values() hands over -
    {"global": g, "intl": ..., "cn": ...}. A blank or unrecognised realm falls
    back to the global default, so an account whose realm could not be detected
    is still guarded by the global number.
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
    """Keep account credit balances fresh, off the request path.

    The dispatch preference reads `account.credits`; without this thread only
    the sign-in and daily-activity tasks would refresh it, and a restart would
    inherit whatever balance was on disk. Every tick
    this refreshes the single stalest account that is past the TTL, so the
    upstream billing calls stay bounded - one per tick - and land spread across
    the pool instead of arriving as a burst. A failed account is parked for six
    hours so a broken credential cannot be retried on every tick.

    The request volume is capped by the tick, not by the pool size: at the
    default half-hour tick that is at most 48 billing calls a day however many
    accounts there are. With a 12-hour TTL a 28-account pool wants 56 calls a
    day, so the tick - not the TTL - is the binding limit and the effective
    refresh age settles a little above the TTL. That is the intended trade:
    the preference is measured in days, so a few extra hours of drift is worth
    far less than the calls it saves.
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
        self._parked = {}          # uid -> epoch until which we skip it
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
        """The current TTL in seconds - the one place the unit is applied."""
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
        """The account whose balance is oldest and past the TTL, or None.

        A balance that was never fetched counts as infinitely old, so a fresh
        install fills in its accounts before any timed-out one is revisited.
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
        """Refresh one stale account; returns its uid, or None when none was due."""
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
                self.log("刷新 %s 积分失败，暂缓一小时: %s"
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
        # Smooth weighted round-robin state for the expiring-credits window,
        # keyed by uid (see _weighted_pick). Runtime-only: a restart just
        # restarts the rotation.
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
        # 新建绑定用的选号方式：默认保持轮询顺序, 只有 apply_smart_routing()
        # 设过面板开关后才按评分选号, 直接构造的账号池行为不变。
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
            # 冷却，否则重启会把它们重新放出去，直到下次撞 429 才又停。
            self.restore_model_caps()
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
                # 这些字段, 显式 null 也不该把面板上的设置清掉。
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
        """Report what import_rows() would do, without touching the pool.

        Shares the same accept/skip rules as import_rows() so a dry run cannot
        disagree with the real thing.
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
        """Add accounts from exported/foreign rows.

        Returns a report dict:
            added       - uids that were new to the pool
            updated     - uids that already existed and were replaced
            skipped     - [{"uid","reason"}] rows that were not imported
            invalid     - [{"index","reason"}] rows that could not be parsed

        Nothing is written until a row parses cleanly, so one bad entry does
        not abort the rest of the file.
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

        名单按 normalise_disabled_models 归一；坏输入退化成空列表（= 不禁用）
        而不是抛错：面板传进来什么形状都不该让一个账号加载失败。
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
        """Re-resolve every account's runtime `proxy` from its bound slot.

        `proxy_slot` is the persisted source of truth; `proxy` is a derived
        runtime value so the many outbound call sites need no change.
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
        """Re-resolve the low-credit guard for every account.

        Same shape as apply_proxy_slots(): settings.json is the source of
        truth and the per-account value is derived here, so the request path
        needs no extra settings lookup. `values` is the guard resolved per
        realm ({"global": g, "intl": ..., "cn": ...}); each account picks its
        own realm, so a per-realm override lands only on that realm's accounts
        while the global default keeps covering both.
        """
        import wb_settings

        if values is None:
            values = wb_settings.limit_values(self.dir, "reserve_credits")
        with self._lock:
            for account in self.accounts:
                account.reserve_credits = _realm_limit(values, account.realm)
        return values

    def apply_expiring_window(self, values=None):
        """Re-resolve the expiring-credits window for every account.

        Same shape as apply_reserve_credits(): settings.json holds the window
        per realm (global by default) and each account reads its own, so the
        request path needs no extra settings lookup. The window is a dispatch
        preference, not a guard: it never makes an account unavailable, it
        only decides who is handed out first.
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
        """Re-resolve the daily token guard for every account.

        Same shape as apply_reserve_credits(): settings.json holds the limit
        per realm, while `usage` (uid -> tokens counted today) comes from the
        caller, because only the proxy reads the usage log. Passing None keeps
        the last known counts, so a settings change never turns them into
        "unknown".
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
        """Re-resolve the daily credit guard for every account.

        Same shape as apply_daily_token_limit(): settings.json holds the
        limit per realm, while `credits` (uid -> spent today) and `free_models`
        (realm -> free model ids) come from the caller, because only the
        proxy reads the usage log and the model catalogue. Passing None
        keeps the last known values, so a settings change never turns them
        into "unknown".
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
        """Re-resolve the per-model daily token guard for every account.

        `per_model` is uid -> {model: tokens counted today}; None keeps the
        last known counts. A blocked model never takes the whole account
        with it - ready(model) refuses exactly the models that are out of
        budget, and only their state changes are logged.
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
            # Selecting "direct" must mean direct: a stale legacy URL would keep
            # routing traffic through it while the panel shows direct.
            account.proxy_legacy = ""
        account.save(self.dir)
        self.apply_proxy_slots()
        return account.public()

    def set_priority(self, uid, value):
        """Persist one account's routing priority (lower number = picked first)."""
        account = self.get(uid)
        if account is None:
            return None
        account.priority = _normalise_priority(value)
        account.save(self.dir)
        return account.public()

    def set_note(self, uid, note):
        """Persist one account's note; an empty string clears it."""
        account = self.get(uid)
        if account is None:
            return None
        account.note = _stored_note(note)
        account.save(self.dir)
        return account.public()

    def set_concurrency_limit(self, uid, value):
        """Persist one account's per-model concurrency cap (0 = unlimited)."""
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
        """Re-resolve the placement setting for this pool.

        Same shape as the other guards: settings.json is the source of truth,
        the pool holds the resolved boolean; passing None reads the current setting.
        """
        import wb_settings

        if value is None:
            value = wb_settings.smart_routing(self.dir)
        self.smart_routing = bool(value)
        return self.smart_routing

    def apply_credit_rates(self, per_day=None):
        """Push the recent credit spend (credits per day) onto the accounts."""
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
        """Counters plus the resolved setting, for the panel."""
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

        测试台可以只用一个账号发请求, 拒绝时要说清是这个账号的哪一条限制。
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
        """Pick the account for one turn of a conversation.

        A bound conversation keeps the account the last turn went to; when that
        account is unusable, the conversation's other warm accounts are tried
        before the pool's own preferences. `page` marks an oversized
        conversation that rotates over one page instead of pinning. `claim`
        reserves a per-account request slot before the account is handed out.
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
                    # 份额」判断忙闲, 漏掉这些请求会把最忙的账号看成最闲的。
                    self.affinity.bind(session_key, uid)
                    account.mark_pick()
                    self.count_routing("bound_hit")
                    return account
            if had_binding:
                # 绑定的账号此刻都不能接单（冷却、限额、令牌失效、名额满）：
                # 这段对话改由别的账号接手, 接手账号重算完整前缀后缓存跟着它。
                self.count_routing("bound_lost")
        account = self.pick(realm=realm, exclude=exclude, model=model, page=page,
                            claim=claim)
        if account is None and page is not None:
            # The page has nobody usable right now. Serving the conversation
            # from outside it beats not serving it at all.
            account = self.pick(realm=realm, exclude=exclude, model=model, claim=claim)
        # A page conversation is not bound on purpose: its rotation is what keeps
        # every account of the page warm, and a binding would pin it back to one
        # (the state it was released from). The page cursor carries its rotation.
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
        # 临期积分优先：最早到期的积分包进入窗口时优先派单。
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
        """Serve the in-window accounts first, or None when none can.

        Within the window the pick is a weighted round-robin: an account's
        share grows the closer its credits are to lapsing, but no account is
        ever handed the whole window. A strict soonest-first order would put
        every request on the one account expiring tomorrow until the upstream
        rate-limited it, which is exactly the burst this spreads out; weighting
        keeps the preference while giving the accounts expiring later a real
        share. Accounts that lapse on the same day carry the same weight, so
        they keep taking turns.
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
        """Smooth weighted round-robin over (account, key, weight) triples.

        nginx's algorithm: every entry accrues its weight each pick, the
        largest running total wins, and the winner pays back the whole round's
        weight. Equal weights degenerate to a plain rotation, so same-weight
        entries keep taking turns.

        `key` is what the rotation state is kept under, and `state_name` names
        the pool attribute holding that state.

        The state is pruned to exactly the entries taking part this pick: an
        account that dropped out (cooling, filtered, out of capacity) loses its
        accumulator, and a later return starts level rather than with a debt
        from before it left.

        The rotation state is shared by every request thread, so the
        read-modify-write runs under the pool lock. The `ready()` checks that
        feed this do NOT: those can reach the network (a token refresh), and
        holding the lock across one expiring credential would serialise every
        dispatch in the realm behind it.
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
        """The accounts on one page of the pool.

        A page is a fixed slot: an account's rank among the realm's accounts
        (sorted by uid) modulo the page size. Ranking inside the realm - not
        across the whole pool, and not by load order or file name - is what
        keeps a page the size it says it is: with a mixed-realm pool, ranking
        globally would leave a page with one usable account for this realm and
        pin its conversations, the state the length cap exists to leave. Uid
        order also keeps a conversation's page stable across restarts and
        reloads; adding or removing an account reshuffles at most one slot per
        page.

        A page cannot be larger than the realm: a size that covers every
        account (or more) means the page is the realm's whole pool.
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
        """uid -> rank among the snapshot's accounts, cached until it changes.

        The snapshot is the realm-filtered pool (readiness is not part of it),
        so the ranks - and therefore the pages - only move when an account is
        added to or removed from that realm.
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

        每个账号到期前每天至少要消耗掉的积分是它的义务, 义务减去今天的消耗
        速度是它的欠账; 欠账占候选账号欠账总和的比例就是它应当承担的份额,
        再减去最近一小时实际接到的份额, 差得最多的先派过去。全员都跟得上时
        比例为 0, 退化成「最近一小时最少接单的先派」。

        候选账号只包括此刻能接单、又没被排除的账号: 拿不到新对话的账号若算进
        总和, 它的欠账永远消不掉, 会把其余账号的份额压平。

        评分之前先过临期积分层: 窗口内的账号按到期节奏加权先接单, 与关闭
        智能分配时一致; 都没命中才按欠账与忙闲评分。估计剩余接近耗尽的
        (账号, 模型) 组合在评分里获得一点倾斜, 越接近限额越靠前, 但不独占流量。
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

        # 临期积分优先：窗口内的账号先接单，与关闭智能分配时的偏好一致。
        # 窗口内按「临期余额除以剩余天数」加权轮询，余额多、离到期近的分到
        # 更多流量，但窗口之外的账号在它们都能接单时拿不到新对话。
        while True:
            urgent = self._pick_expiring_first(snapshot, exclude, model)
            if urgent is None:
                break
            if _claim(urgent, model, claim):
                return take(urgent)
            exclude.add(urgent.uid)
        # 临期层里没抢到名额的账号已经进了 exclude：候选集必须按最新的
        # exclude 重算，否则它的欠账仍会算进总和，压低其余账号的份额。
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
        """Round-robin one group of accounts, advancing its cursor.

        The whole-pool rotation keeps the single `_cursor` it always had. A
        page rotation keeps its own cursor per page, so page picks and plain
        picks never move each other's position.
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
            req_acct = urllib.request.Request(acct_url, method="GET", headers=acct_headers)
            with urllib.request.urlopen(req_acct, timeout=15) as resp_acct:
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

    桌面客户端 2026-09-24 之后把 token 存成 `$wbEncrypted` 信封，这里统一在
    读取处解密；仍然是明文的旧客户端原样返回、不去碰密钥。
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
    """Directories where the desktop client may keep its *.info credentials.

    Windows uses %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth; other
    platforms probe the plausible app names under Application Support. Missing
    directories are harmless: callers only read the files that exist.
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

def desktop_auth_dir():
    """First candidate credential directory."""
    return desktop_auth_dirs()[0]

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
    """Describe the desktop-app credentials found on this machine.

    Read-only: nothing is added to the pool. The dashboard shows the result
    and lets the user decide which ones to import, so the proxy never
    silently adopts the desktop client's login.

    桌面端加密之后（2026-09-24 起）扫描要多分辨一种情况：凭据是加密信封，
    得先把密钥回收回来才能读。这里只查内存里已有的密钥，不触发回收（回收要
    扫客户端进程内存，得由用户在弹窗里显式点一次），所以扫描始终是秒回的。
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


# --------------------------------------------------------------- export / import
#
# Accounts travel as a single JSON document so a pool can be moved between
# machines (or backed up) without reaching into the accounts directory by hand.
# Containers accepted on import:
#   1. this module's own export  -> {"format": "workbuddy-accounts", "accounts": [...]}
#   2. a bare list               -> [ {...}, {...} ]           (hand-written)
#   3. a single account object   -> {...}                       (one-off paste)
# A desktop-app credential ({"auth": {...}, "account": {...}}) is also accepted.

EXPORT_FORMAT = "workbuddy-accounts"
EXPORT_VERSION = 1

# Fields that describe live state rather than the credential itself. They are
# exported for inspection but never trusted on import: a stale cooldown or a
# disabled flag from another machine would silently cripple the target pool.
VOLATILE_FIELDS = ("cooldownUntil", "lastError", "credits", "lastCheckin",
                   "lastDailyChat", "lastActivityReport", "modelCaps")

# The subset of VOLATILE_FIELDS that must not round-trip through the local
# credential file at all: they are not trusted on load and not written by save(),
# exactly like model_cooldowns. An export still carries them (see to_dict()).
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
        "source": SOURCE_API_KEY,
        "apiKey": True,
        "enabled": True,
    })


def account_to_export(account):
    """Serialise one account for an export document."""
    data = account.to_dict()
    # to_dict() 已经是导出所需的全部内容；path 是运行时字段, 不进文档。
    data.pop("path", None)
    return data


def build_export_document(accounts, realm=None, include_secrets=True, uids=None):
    """Wrap accounts in a self-describing export document.

    `uids` narrows the export to specific accounts, applied on top of the realm
    filter, so asking for "this account" in the wrong realm gives an empty
    document rather than a wrong one.
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
    """Normalise any accepted container into a list of account dicts.

    Returns (rows, error). Accepts the export document, a bare list, a single
    account object, or a desktop-app credential.
    """
    if isinstance(blob, list):
        rows = blob
    elif isinstance(blob, dict) and isinstance(blob.get("accounts"), list):
        # Our own export document (or any object carrying an accounts array).
        rows = blob["accounts"]
    elif isinstance(blob, dict):
        # A single account object, or a desktop-app credential
        # ({"account": {...}, "auth": {...}}); anything else is a wrong shape
        # and must be reported rather than treated as one account.
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
    """Turn one exported/foreign row into Account kwargs.

    Accepts both the flat account shape and the nested desktop-credential shape.
    Raises ValueError when the row carries no usable credential.
    """
    # cockpit tools exports a bare array of snake_case OAuth rows. Map it onto
    # the flat shape the rest of this function already understands; expires_at
    # is milliseconds, which normalize_epoch() below converts to seconds.
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
        """Read a field from whichever layer holds it (flat, auth, account)."""
        for layer in (row, auth, profile):
            if isinstance(layer, dict) and layer.get(key) not in (None, ""):
                return layer.get(key)
        return default

    token = _access_token_value(pick("accessToken"))
    if pick("apiKey") is True and not wb_identity.is_api_key_token(token):
        raise ValueError("apiKey is set but accessToken is not a ck_/pt_ key")
    api_key = wb_identity.is_api_key_token(token)

    # Where the account belongs. A realm forced by the caller still wins (that
    # is what the API option is for), otherwise the token/domain decides: a row
    # carrying a stale "realm" (an account once filed under the wrong region,
    # then exported again) must not keep dragging itself back there, and the
    # wrong region sends every request to the wrong upstream.
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
        # Volatile state is intentionally reset - see VOLATILE_FIELDS.
        "lastError": "",
        "cooldownUntil": 0.0,
    }
    if api_key:
        kwargs["product"] = str(pick("product") or "cli")
    elif pick("product"):
        kwargs["product"] = str(pick("product"))
    # 优先级随导出的文档一起走, 文档里没写就不放进 kwargs: AccountPool.add()
    # 据此判断该不该覆盖已存在账号的同名字段。
    priority = pick("priority")
    if priority is not None:
        kwargs["priority"] = priority
    # 备注同样是面板设置：导出文档带着它, 外来行不带时保留原值。
    note = pick("note")
    if note is not None:
        kwargs["note"] = _stored_note(note)
    # 并发上限与优先级、备注同一个口径：导出文档带着它, 外来行不带时保留已存的值。
    concurrency = pick("concurrencyLimit")
    if concurrency is not None:
        kwargs["concurrencyLimit"] = _stored_concurrency_limit(concurrency)
    return kwargs
