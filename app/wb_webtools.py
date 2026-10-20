# -*- coding: utf-8 -*-
# 反向代理代为执行 web_search 与 web_fetch，开关在 wb_proxy.LOCAL_WEB_TOOLS。
#
# 搜索按引擎链自动轮换：duckduckgo、bing 失败时依次换 wikipedia、
# stackoverflow、news；非常规来源的结果会标明来源，全部失败时回明确的错误，
# 不把失败伪装成查无结果。被限流的引擎进冷却，查询结果进 TTL 缓存，相同 key
# 的并发查询合并成一次对外请求。
#
# web_fetch 只收文本型内容，支持 gzip，读档上限 4 MB，charset 依标头与
# <meta> 推断，结果同样进缓存。URL 守卫在 DNS 解析后逐一核对，必须都是公网
# 地址，防止 DNS rebinding 把 fetch 指进内网。
#
# 环境变量：
#   WB_WEB_ENGINES       覆盖引擎链与顺序，逗号分隔
#   WB_WEB_COOLDOWN      被挡引擎的冷却秒数，默认 600，0 表示不冷却
#   WB_WEB_CACHE_TTL     搜索与抓取缓存秒数，默认 600，0 表示关闭
#   WB_WEB_MIN_INTERVAL  对外最小间隔秒数，默认 0.5，0 表示关闭
#   WB_WEB_BUDGET        单次搜索所有引擎的总预算秒数，默认 25，0 表示不限
#   WB_WEB_TOOLS_PROXY   搜索请求的出口代理；web_fetch 固定解析 IP，不走代理

import base64
import collections
import gzip
import html as _html
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

WEB_SEARCH_NAME = "web_search"
WEB_FETCH_NAME = "web_fetch"

# 客户端会用这几种 type 声明同一个工具
SEARCH_DECL_TYPES = ("web_search", "web_search_preview", "web_search_preview_2025_03_11")
FETCH_DECL_TYPES = ("web_fetch",)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_RESULTS = 10
MAX_FETCH_CHARS = 100000
MAX_BODY_BYTES = 4 * 1024 * 1024
SEARCH_BODY_BYTES = 3 * 1024 * 1024
MAX_CACHE_TEXT = 200000
HTTP_TIMEOUT = 20
FETCH_DEADLINE = 40
MAX_RESPONSE_BYTES = 4 << 20
MAX_REDIRECTS = 5
READ_CHUNK = 65536
REDIRECT_STATUS = (301, 302, 303, 307, 308)
SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"


def _env_float(name, default):
    try:
        value = float(os.environ.get(name, "") or "")
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


CACHE_TTL = _env_float("WB_WEB_CACHE_TTL", 600.0)
COOLDOWN_BLOCKED = _env_float("WB_WEB_COOLDOWN", 600.0)
COOLDOWN_ERROR = _env_float("WB_WEB_COOLDOWN_ERROR", 60.0)
MIN_INTERVAL = _env_float("WB_WEB_MIN_INTERVAL", 0.5)
SEARCH_BUDGET = _env_float("WB_WEB_BUDGET", 25.0)

_UNAVAILABLE_PREFIX = "Error: web search is temporarily unavailable"

# 反爬／限流页面的通用字眼；只在解析不到任何结果时用来分类
GENERIC_BLOCKED = (
    "captcha",
    "unusual traffic",
    "are you a robot",
    "access denied",
    "request blocked",
    "too many requests",
    "bot detection",
    "enable javascript and cookies to continue",
)

_CJK_RE = re.compile(u"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def _has_cjk(text):
    return bool(_CJK_RE.search(str(text or "")))


# ---------------------------------------------------------------------------
# 日志挂钩：wb_proxy 启动时把它的 log() 接进来，没接也照常运作
# ---------------------------------------------------------------------------

_LOGGER = None


def set_logger(fn):
    global _LOGGER
    _LOGGER = fn if callable(fn) else None


def _log(msg, level="INFO", tag="webtools"):
    logger = _LOGGER
    if logger is None:
        return
    try:
        logger(msg, level=level, tag=tag)
    except Exception:
        pass
# ---------------------------------------------------------------------------
# 缓存 / 并发去重 / 对外节流
# ---------------------------------------------------------------------------

class _TTLCache(object):
    def __init__(self, max_entries=128):
        self._lock = threading.Lock()
        self._data = collections.OrderedDict()
        self._max = max_entries

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expires, value = item
            if expires <= time.monotonic():
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key, value, ttl):
        if ttl <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self):
        with self._lock:
            self._data.clear()


_CACHE = _TTLCache()

_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT = {}


def _single_flight(key):
    """有人正在算同一个 key 时返回 event 等它；自己是第一个则返回 None。"""
    with _INFLIGHT_LOCK:
        event = _INFLIGHT.get(key)
        if event is not None:
            return event
        _INFLIGHT[key] = threading.Event()
        return None


def _flight_done(key):
    with _INFLIGHT_LOCK:
        event = _INFLIGHT.pop(key, None)
    if event is not None:
        event.set()


_THROTTLE_LOCK = threading.Lock()
_THROTTLE_LAST = [0.0]


def _throttle():
    """对外请求的最小间隔；多个线程共用一个节拍器。"""
    interval = MIN_INTERVAL
    if interval <= 0:
        return
    with _THROTTLE_LOCK:
        now = time.monotonic()
        wait = _THROTTLE_LAST[0] + interval - now
        _THROTTLE_LAST[0] = max(now, _THROTTLE_LAST[0] + interval)
    if wait > 0:
        time.sleep(min(wait, interval))


def reset_state():
    """清空缓存与冷却状态，测试与面板重设时调用。"""
    _CACHE.clear()
    with _ENGINE_STATE_LOCK:
        _ENGINE_STATE.clear()
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()


# ---------------------------------------------------------------------------
# HTTP 底层：UA / gzip / 字节上限 / 出口代理 / 安全转址
# ---------------------------------------------------------------------------

class _UnsafeRedirectError(Exception):
    """网页工具的响应重定向到不允许的地址时抛出。"""


def _ip_is_public(text):
    """单一 IP 是否属于公网，含 IPv4 映射 IPv6 的还原检查。"""
    try:
        ip = ipaddress.ip_address(str(text or "").split("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_global) and not ip.is_multicast


def _resolve_host_public(host):
    """返回 ok 与 problem 两个值；DNS 解析出来的每个地址都必须是公网。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as exc:
        return False, "could not resolve %s (%s)" % (host, type(exc).__name__)
    addrs = []
    for info in infos:
        addr = (info[4] or ("",))[0]
        if addr and addr not in addrs:
            addrs.append(addr)
    if not addrs:
        return False, "could not resolve %s" % host
    for addr in addrs:
        if not _ip_is_public(addr):
            return False, "%s resolves to a non-public address (%s)" % (host, addr)
    return True, ""
def _guard_url(url, resolve=False):
    """只允许对外的一般 http(s) 网址；resolve=True 时再核对 DNS 解析结果。"""
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except Exception:
        return None, "Invalid URL."
    if parsed.scheme.lower() not in ("http", "https"):
        return None, "Only http:// or https:// URLs are supported."
    if parsed.username or parsed.password:
        return None, "Credentials in the URL are not allowed."
    host = (parsed.hostname or "").lower()
    if not host or host.endswith(".localhost"):
        return None, "Private, loopback or single-label hosts are not allowed."
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        if not _ip_is_public(host):
            return None, "Private network addresses are not allowed."
    else:
        if "." not in host:
            return None, "Private, loopback or single-label hosts are not allowed."
        if re.match(r"^(127\.|10\.|192\.168\.|169\.254\.|0\.)", host) or \
                re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host):
            return None, "Private network addresses are not allowed."
    if resolve and not literal:
        ok, problem = _resolve_host_public(host)
        if not ok:
            return None, problem
    return url, ""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每个重定向目标都套用与初始 URL 相同的地址策略。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, str(newurl or ""))
        safe_url, problem = _guard_url(target, resolve=True)
        if problem:
            raise _UnsafeRedirectError(problem)
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


def _egress_proxy():
    """此工具专用的出口代理；没设就走 urllib 默认值，即环境变量 HTTP_PROXY 与 HTTPS_PROXY。"""
    raw = (os.environ.get("WB_WEB_TOOLS_PROXY") or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    return raw


def _open_url(url, timeout=HTTP_TIMEOUT, max_bytes=MAX_BODY_BYTES, accept=None):
    """GET 一个网址，返回 raw_bytes、status、content_type、charset 组成的元组。失败抛出异常。"""
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": accept or "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
    }
    req = urllib.request.Request(url, headers=headers)
    handlers = [_SafeRedirectHandler()]
    proxy = _egress_proxy()
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    _throttle()
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
        status = getattr(resp, "status", None)
        if status is None:
            try:
                status = resp.getcode()
            except Exception:
                status = 0
        ctype = resp.headers.get("Content-Type") or ""
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if "gzip" in encoding:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    charset = ""
    m = re.search(r"charset\s*=\s*[\"']?([\w\-]+)", ctype, re.I)
    if m:
        charset = m.group(1)
    return raw, status, ctype, charset


def _sniff_charset(raw):
    m = re.search(br'<meta[^>]+charset\s*=\s*["\']?([A-Za-z0-9_\-]+)', raw[:4096], re.I)
    return m.group(1).decode("ascii", "ignore") if m else ""


def _decode(raw, charset=""):
    for name in (charset, _sniff_charset(raw), "utf-8"):
        if not name:
            continue
        try:
            return raw.decode(name, "replace")
        except (LookupError, TypeError):
            continue
    return raw.decode("utf-8", "replace")


def _is_textual_content_type(ctype):
    if not ctype:
        return True
    main = ctype.split(";")[0].strip().lower()
    if main.startswith("text/"):
        return True
    if main in ("application/json", "application/xml", "application/xhtml+xml",
                "application/rss+xml", "application/atom+xml", "application/ld+json",
                "application/javascript", "application/x-javascript"):
        return True
    return main.endswith("+json") or main.endswith("+xml")


def _strip_tags(text):
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text or "")
    text = re.sub(r"(?is)<br\s*/?>", chr(10), text)
    text = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", chr(10), text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = text.replace(chr(160), " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", chr(10), text)
    text = re.sub(chr(10) + "{3,}", chr(10) + chr(10), text)
    return text.strip()


def _ddg_target(href):
    """解开 DuckDuckGo 的 /l/?uddg= 转址。"""
    href = _html.unescape(str(href or "").strip())
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urllib.parse.urlparse(href)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            target = (urllib.parse.parse_qs(parsed.query).get("uddg") or [""])[0]
            if target:
                return urllib.parse.unquote(target)
    except Exception:
        pass
    return href


def _bing_target(href):
    """解开 Bing 的 /ck/a?...&u=a1<base64url> 转址。"""
    href = _html.unescape(str(href or "").strip())
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urllib.parse.urlparse(href)
        query = urllib.parse.parse_qs(parsed.query)
        raw = (query.get("u") or [""])[0]
        if raw.startswith("a1"):
            raw = raw[2:]
            raw += "=" * ((4 - len(raw) % 4) % 4)
            decoded = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8", "replace")
            if decoded.startswith("http"):
                return decoded
    except Exception:
        pass
    return href

# ---------------------------------------------------------------------------
# 各引擎的 URL 与解析器：纯标准库、正则优先，页面改版只影响单个引擎
# ---------------------------------------------------------------------------

def _url_duckduckgo(query, n):
    return SEARCH_ENDPOINT + "?" + urllib.parse.urlencode({"q": query})


def _parse_duckduckgo(page, n, query):
    blocks = re.split(r'(?is)<div[^>]+class="[^"]*result__body[^"]*"', page)
    results = []
    seen = set()
    for block in blocks[1:]:
        m_link = re.search(
            r'(?is)<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m_link:
            continue
        href = _ddg_target(m_link.group(1))
        title = _strip_tags(m_link.group(2))
        if not href.startswith("http") or not title or href in seen:
            continue
        seen.add(href)
        m_snip = re.search(r'(?is)class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', block)
        snippet = _strip_tags(m_snip.group(1)) if m_snip else ""
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= n:
            break
    return results


def _url_bing(query, n):
    return "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "count": n})


def _parse_bing(page, n, query):
    results = []
    seen = set()
    for block in re.findall(r'(?is)<li class="b_algo".*?</li>', page):
        m_link = re.search(r'(?is)<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m_link:
            continue
        href = _bing_target(m_link.group(1))
        title = _strip_tags(m_link.group(2))
        if not href.startswith("http") or not title or href in seen:
            continue
        if "bing.com/search" in href:
            continue
        seen.add(href)
        m_snip = re.search(r'(?is)<p[^>]*>(.*?)</p>', block)
        snippet = _strip_tags(m_snip.group(1)) if m_snip else ""
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= n:
            break
    return results


def _url_stackoverflow(query, n):
    return "https://api.stackexchange.com/2.3/search/advanced?" + urllib.parse.urlencode({
        "order": "desc", "sort": "relevance", "q": query,
        "site": "stackoverflow", "pagesize": n, "filter": "default"})


def _parse_stackoverflow(page, n, query):
    try:
        data = json.loads(page)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    results = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        title = _strip_tags(item.get("title") or "")
        url = str(item.get("link") or "").strip()
        if not title or not url.startswith("http"):
            continue
        bits = ["Stack Overflow"]
        try:
            score = int(item.get("score") or 0)
            answers = int(item.get("answer_count") or 0)
            bits.append("score %d, %d answer(s)" % (score, answers))
        except (TypeError, ValueError):
            pass
        tags = item.get("tags") or []
        if isinstance(tags, list) and tags:
            bits.append("tags: " + ", ".join(str(x) for x in tags[:4]))
        results.append({"title": title, "url": url, "snippet": " - ".join(bits)})
        if len(results) >= n:
            break
    return results


def _wikipedia_lang(query):
    return "zh" if _has_cjk(query) else "en"


def _url_wikipedia(query, n):
    return ("https://%s.wikipedia.org/w/api.php?" % _wikipedia_lang(query)) + \
        urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": query,
                                "format": "json", "utf8": 1, "srlimit": n})


def _parse_wikipedia(page, n, query):
    try:
        data = json.loads(page)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    hits = ((data.get("query") or {}).get("search") or [])
    lang = _wikipedia_lang(query)
    results = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title") or "").strip()
        if not title:
            continue
        results.append({
            "title": title,
            "url": "https://%s.wikipedia.org/wiki/%s" % (
                lang, urllib.parse.quote(title.replace(" ", "_"))),
            "snippet": _strip_tags(hit.get("snippet") or ""),
        })
        if len(results) >= n:
            break
    return results


def _url_news(query, n):
    if _has_cjk(query):
        params = {"q": query, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh"}
    else:
        params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def _parse_news(page, n, query):
    results = []
    for item in re.findall(r'(?is)<item>(.*?)</item>', page):
        m_title = re.search(r'(?is)<title>(.*?)</title>', item)
        m_link = re.search(r'(?is)<link>(.*?)</link>', item)
        m_date = re.search(r'(?is)<pubDate>(.*?)</pubDate>', item)
        title = _strip_tags(m_title.group(1)) if m_title else ""
        url = _html.unescape((m_link.group(1) or "").strip()) if m_link else ""
        if not title or not url.startswith("http"):
            continue
        date = _strip_tags(m_date.group(1)) if m_date else ""
        results.append({"title": title, "url": url,
                        "snippet": ("News item - " + date) if date else "News item"})
        if len(results) >= n:
            break
    return results

_ENGINES = {
    "duckduckgo": {
        "url": _url_duckduckgo, "parse": _parse_duckduckgo,
        "blocked": ("anomaly", "bots use duckduckgo", "if this error persists"),
        "source_note": "",
    },
    "bing": {
        "url": _url_bing, "parse": _parse_bing,
        "blocked": (),
        "source_note": "",
    },
    "stackoverflow": {
        "url": _url_stackoverflow, "parse": _parse_stackoverflow,
        "blocked": (),
        "source_note": ("Source: Stack Overflow (Q&A site, not a general web search "
                        "engine) - tell the user these results came from Stack Overflow."),
    },
    "wikipedia": {
        "url": _url_wikipedia, "parse": _parse_wikipedia,
        "blocked": (),
        "source_note": ("Source: Wikipedia (encyclopedia entries, not a general web "
                        "search engine) - tell the user these results came from Wikipedia."),
    },
    "news": {
        "url": _url_news, "parse": _parse_news,
        "blocked": (),
        "source_note": ("Source: Google News RSS (news feed, not a general web search "
                        "engine) - tell the user these results came from Google News."),
    },
}

ENGINE_NAMES_DEFAULT = ("duckduckgo", "bing", "wikipedia", "stackoverflow", "news")


def engine_names():
    """设置里的引擎链；WB_WEB_ENGINES 可覆盖顺序与子集。"""
    raw = (os.environ.get("WB_WEB_ENGINES") or "").strip()
    if raw:
        picked = [x.strip().lower() for x in raw.split(",") if x.strip()]
        picked = [x for x in picked if x in _ENGINES]
        if picked:
            return picked
    return list(ENGINE_NAMES_DEFAULT)


def _chain_for(query):
    """这一轮要试的引擎顺序；之后若按查询语言重排，只需要改这里。"""
    return engine_names()


# ---------------------------------------------------------------------------
# 引擎执行、失败分类与冷却
# ---------------------------------------------------------------------------

_ENGINE_STATE_LOCK = threading.Lock()
_ENGINE_STATE = {}


def _cooling(name):
    now = time.monotonic()
    with _ENGINE_STATE_LOCK:
        entry = _ENGINE_STATE.get(name)
        if not entry:
            return False
        if entry[0] <= now:
            _ENGINE_STATE.pop(name, None)
            return False
        return True


def _mark_cooling(name, seconds, reason):
    if seconds <= 0:
        return
    with _ENGINE_STATE_LOCK:
        _ENGINE_STATE[name] = (time.monotonic() + seconds, reason)
    _log("webtools: %s cooling down %ds (%s)" % (name, int(seconds), reason), level="WARN")


def _clear_cooling(name):
    with _ENGINE_STATE_LOCK:
        _ENGINE_STATE.pop(name, None)


def _page_looks_blocked(text, spec):
    lower = str(text or "").lower()
    for marker in tuple(spec.get("blocked") or ()) + GENERIC_BLOCKED:
        if marker in lower:
            return True
    return False


def _run_engine(name, query, n, timeout):
    """跑一家引擎，返回 state、results、detail 三个键组成的字典。"""
    spec = _ENGINES.get(name)
    if not spec:
        return {"state": "error", "results": [], "detail": "unknown engine"}
    try:
        raw, status, _ctype, charset = _open_url(
            spec["url"](query, n), timeout=timeout, max_bytes=SEARCH_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        state = "blocked" if exc.code in (202, 403, 429) else "error"
        return {"state": state, "results": [], "detail": "HTTP %s" % exc.code}
    except _UnsafeRedirectError as exc:
        return {"state": "error", "results": [], "detail": "redirect blocked (%s)" % exc}
    except Exception as exc:
        return {"state": "error", "results": [], "detail": type(exc).__name__}
    page = _decode(raw, charset)
    try:
        results = spec["parse"](page, n, query) or []
    except Exception as exc:
        return {"state": "error", "results": [],
                "detail": "parse failed (%s)" % type(exc).__name__}
    if results:
        return {"state": "ok", "results": results, "detail": ""}
    if _page_looks_blocked(page, spec):
        return {"state": "blocked", "results": [], "detail": "anti-bot page"}
    if status == 202:
        return {"state": "blocked", "results": [], "detail": "HTTP 202"}
    if status and status >= 400:
        return {"state": "error", "results": [], "detail": "HTTP %s" % status}
    return {"state": "empty", "results": [], "detail": "no results"}

def _format_results(query, results, note=""):
    nl = chr(10)
    header = "Search results for: %s" % query
    if note:
        header += nl + "[%s]" % note
    lines = ["%d. %s%s   %s%s   %s" % (i, r["title"], nl, r["url"], nl, r["snippet"])
             for i, r in enumerate(results, 1)]
    return (header + nl + nl + (nl + nl).join(lines) + nl + nl +
            "Cite the sources you used at the end of your answer.")


def _search_fanout(query, n):
    """依序打所有引擎；第一家成功的直接回报，只有全部失败才回错误。

    常规引擎 DuckDuckGo 与 Bing 接手时结果与平常无异；换成 Wikipedia、
    Stack Overflow、Google News 这类非常规来源时，结果会带上 source_note，
    让模型知道手上的不是一般网页搜索结果。
    """
    names = _chain_for(query)
    if not names:
        return "Error: no search engines are configured."
    started = time.monotonic()
    deadline = started + SEARCH_BUDGET if SEARCH_BUDGET > 0 else float("inf")
    failures = []
    skipped = []
    attempts = [0]
    clean_empty = [False]

    def _remaining():
        return deadline - time.monotonic()

    def _finish_ok(name, results):
        spec = _ENGINES.get(name) or {}
        note = str(spec.get("source_note") or "")
        _log("webtools: search %r served by %s%s (%d attempt(s), %d ms, %d result(s))"
             % (query, name, " (non-standard source, labelled)" if note else "",
                attempts[0], int((time.monotonic() - started) * 1000), len(results)))
        return _format_results(query, results, note)

    def _try(name):
        attempts[0] += 1
        timeout = max(3.0, min(float(HTTP_TIMEOUT), _remaining()))
        outcome = _run_engine(name, query, n, timeout)
        state = str(outcome.get("state") or "error")
        detail = str(outcome.get("detail") or "")
        if state == "ok":
            _clear_cooling(name)
            return outcome.get("results") or []
        if state in ("blocked", "error"):
            cooldown = COOLDOWN_BLOCKED if state == "blocked" else COOLDOWN_ERROR
            _mark_cooling(name, cooldown, "%s: %s" % (state, detail or "?"))
        else:
            clean_empty[0] = True
        failures.append((name, "%s%s" % (state, (" (%s)" % detail) if detail else "")))
        _log("webtools: %s failed for %r: %s" % (name, query, failures[-1][1]),
             level="WARN")
        return None

    for name in names:
        if _cooling(name):
            skipped.append(name)
            continue
        if _remaining() <= 1:
            failures.append((name, "budget exhausted"))
            _log("webtools: search budget exhausted before %s" % name, level="WARN")
            break
        results = _try(name)
        if results is not None:
            return _finish_ok(name, results)

    # 其他引擎全部失败时，把冷却中的引擎补试一轮，可能已经恢复。
    for name in skipped:
        if _remaining() <= 1:
            break
        results = _try(name)
        if results is not None:
            return _finish_ok(name, results)

    if clean_empty[0]:
        _log("webtools: no results for %r (engines answered, all empty)" % query)
        return ("No results found for: %s%sTry a broader or differently worded query."
                % (query, chr(10) + chr(10)))
    detail = "; ".join("%s: %s" % (name, why) for name, why in failures) or "no engine available"
    _log("webtools: search %r failed on every backend (%s)" % (query, detail), level="ERROR")
    return (_UNAVAILABLE_PREFIX +
            ". Every backend was tried automatically and all failed (%s). Do not retry "
            "the same query right away; tell the user that web search is temporarily "
            "unavailable and continue with what you already know." % detail)

# ---------------------------------------------------------------------------
# 工具定义：给上游看的 function 形状
# ---------------------------------------------------------------------------

def web_search_tool_def():
    return {
        "type": "function",
        "name": WEB_SEARCH_NAME,
        "description": (
            "Searches the web for real-time information and returns ranked results "
            "with titles, URLs and snippets. Use it for current events, documentation "
            "lookup, or anything beyond your knowledge cutoff. To read a page in full, "
            "call web_fetch on its URL afterwards."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query (at least 2 characters).",
                },
                "numResults": {
                    "type": "number",
                    "description": "How many results to return (1-10, default 5).",
                },
            },
            "required": ["query"],
        },
    }


def web_fetch_tool_def():
    return {
        "type": "function",
        "name": WEB_FETCH_NAME,
        "description": (
            "Fetches a URL and returns its readable text. Use startIndex to page "
            "through a long page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute http:// or https:// URL to fetch.",
                },
                "startIndex": {
                    "type": "number",
                    "description": "Character offset to continue reading a long page.",
                },
            },
            "required": ["url"],
        },
    }


# ---------------------------------------------------------------------------
# 声明替换
# ---------------------------------------------------------------------------

def _declared(tools, types):
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("type") or "").strip().lower() in types:
            return True
        # 有些客户端会把它包成 function 形状
        if str(t.get("name") or "").strip().lower() in types:
            return True
    return False


def client_wants_web(tools):
    """客户端声明了哪几个网络工具，服务器端或 function 形状都算。"""
    return {
        "search": _declared(tools, SEARCH_DECL_TYPES + (WEB_SEARCH_NAME,)),
        "fetch": _declared(tools, FETCH_DECL_TYPES + (WEB_FETCH_NAME,)),
    }


def install_tool_defs(chat_tools, wants):
    """把客户端的网络工具声明换成我们的 function。

    同名项目一律先移除，包括服务器端的 {"type": "web_search"}、客户端自己
    带的 function，以及上一轮从我们这里学到的定义，只留唯一一份；否则上游会
    同时看到两个 web_search，模型会挑错那个去调用。
    """
    names = set()
    if wants.get("search"):
        names.add(WEB_SEARCH_NAME)
    if wants.get("fetch"):
        names.add(WEB_FETCH_NAME)

    kept = []
    for t in chat_tools or []:
        if not isinstance(t, dict):
            kept.append(t)
            continue
        type_name = str(t.get("type") or "").strip().lower()
        name = str(t.get("name") or "").strip().lower()
        if isinstance(t.get("function"), dict):
            name = name or str((t.get("function") or {}).get("name") or "").strip().lower()
        if name in names or type_name in names:
            continue
        kept.append(t)

    if wants.get("search"):
        kept.append(web_search_tool_def())
    if wants.get("fetch"):
        kept.append(web_fetch_tool_def())
    return kept


def is_internal_tool(name):
    return str(name or "").strip() in (WEB_SEARCH_NAME, WEB_FETCH_NAME)


def query_args(args):
    """从工具参数取出查询字符串。

    接受 query、queries、q 三种键，数组会用 " or " 接起来；模型改送数组时
    不会因为读不到 query 而收到「你没问问题」并反复重试。
    """
    if not isinstance(args, dict):
        return ""
    raw = args.get("query")
    if raw is None:
        raw = args.get("queries")
    if raw is None:
        raw = args.get("q")
    if isinstance(raw, (list, tuple)):
        parts = [str(x).strip() for x in raw if str(x or "").strip()]
        return " or ".join(parts)
    return str(raw or "").strip()


def url_arg(args):
    if not isinstance(args, dict):
        return ""
    raw = args.get("url")
    if raw is None:
        raw = args.get("urls")
    if isinstance(raw, (list, tuple)):
        for x in raw:
            if str(x or "").strip():
                return str(x).strip()
        return ""
    return str(raw or "").strip()


def max_rounds():
    """最多代跑几轮网络工具。环境变量可覆盖，方便临时调小。"""
    try:
        n = int(os.environ.get("WB_MAX_WEB_ROUNDS", "") or "")
    except (TypeError, ValueError):
        n = 0
    return min(8, n) if n > 0 else 3


MAX_WEB_ROUNDS = max_rounds()

# ---------------------------------------------------------------------------
# 对外动作：search 与 fetch。execute 永不抛出异常，永远返回一句能喂回模型的字符串
# ---------------------------------------------------------------------------

def search(query, num_results=5):
    """多引擎搜索；模型只会看到成功的结果或一句明确的失败。"""
    query = str(query or "").strip()
    if len(query) < 2:
        return ('Error: web_search needs a query of at least 2 characters; '
                'got %r. Pass it as {"query": "..."}.' % query)
    try:
        n = int(num_results)
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(MAX_RESULTS, n))

    key = ("search", query.lower(), n)
    hit = _CACHE.get(key)
    if hit is not None:
        _log("webtools: search cache hit for %r" % query)
        return hit
    event = _single_flight(key)
    owner = event is None
    if not owner:
        event.wait(30)
        hit = _CACHE.get(key)
        if hit is not None:
            _log("webtools: search cache hit after coalescing for %r" % query)
            return hit
    try:
        text = _search_fanout(query, n)
        if not text.startswith(_UNAVAILABLE_PREFIX):
            ttl = CACHE_TTL if text.startswith("Search results") else min(CACHE_TTL, 120.0)
            if ttl > 0:
                _CACHE.put(key, text, ttl)
        return text
    finally:
        if owner:
            _flight_done(key)


class WebToolError(Exception):
    """抓取失败的原因，如地址被拒、解析不了、超时、连不上，转成给模型的一句话。"""


class HttpStatusError(WebToolError):
    """上游回了非 2xx。"""

    def __init__(self, status):
        WebToolError.__init__(self, "HTTP %s" % status)
        self.status = status


_SSL_CONTEXT = None


def _ssl_context():
    """默认上下文：证书链与主机名都要校验；连接虽然固定在校验过的 IP 上，这里不受影响。"""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def _remaining(deadline):
    """离整次抓取的 deadline 还有多久；顺带把单次连接/读的超时压在里面。"""
    left = deadline - time.monotonic()
    if left <= 0:
        raise WebToolError("Timed out after %ds." % FETCH_DEADLINE)
    return min(float(HTTP_TIMEOUT), left)


def _read_body(resp, sock, limit, deadline):
    """分块读响应体：读满 limit 再探一个字节就知道有没有更多，多了立刻停。

    read1 每次最多做一次底层读取，慢速服务器也会在每次读取之间回到这里检查
    deadline；read(n) 会一直凑够 n 字节才返回。
    """
    chunks = []
    total = 0
    while total < limit:
        sock.settimeout(_remaining(deadline))
        piece = resp.read1(min(READ_CHUNK, limit - total))
        if not piece:
            return b"".join(chunks), False
        chunks.append(piece)
        total += len(piece)
    sock.settimeout(_remaining(deadline))
    return b"".join(chunks), bool(resp.read(1))


def _connect(parsed, addresses, deadline):
    """连到已经校验过的 IP；https 用 URL 里的主机名握手，SNI 与证书校验都按它来。"""
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    failure = None
    for ip in addresses:
        timeout = _remaining(deadline)
        try:
            sock = socket.create_connection((str(ip), port), timeout)
        except OSError as exc:
            failure = exc
            continue
        if parsed.scheme != "https":
            return sock
        try:
            return _ssl_context().wrap_socket(sock, server_hostname=host)
        except OSError as exc:
            failure = exc
            sock.close()
    raise WebToolError("Could not connect to %s (%s)." % (host, failure))


def _fetch(parsed, addresses, deadline):
    """发一个 GET，返回状态码、响应头、响应体与是否被字节上限截断。

    重定向那一跳只读响应头：跳过去的地址多半用不上它带的页面。
    """
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = cls(host, port, timeout=_remaining(deadline))
    # 已经连好、https 也已经握手；塞进去免得它按主机名再解析一次，堵住 DNS
    # 重绑定的口子。Host 与 Accept-Encoding identity 由 putrequest 按连接的
    # 主机和端口补上。
    sock = _connect(parsed, addresses, deadline)
    conn.sock = sock
    try:
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        conn.putrequest("GET", target)
        conn.putheader("User-Agent", _USER_AGENT)
        conn.putheader("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        conn.putheader("Accept-Language", "en-US,en;q=0.9")
        conn.endheaders()
        resp = conn.getresponse()
        try:
            if resp.status in REDIRECT_STATUS and resp.headers.get("Location"):
                return resp.status, resp.headers, b"", False
            body, truncated = _read_body(resp, sock, MAX_RESPONSE_BYTES, deadline)
        finally:
            resp.close()
        return resp.status, resp.headers, body, truncated
    finally:
        conn.close()


# is_private / is_global 各版本的覆盖范围不一样，这几段自己补齐
_EXTRA_BLOCKED_V4 = tuple(ipaddress.ip_network(n) for n in (
    "192.0.0.0/24",      # IETF 协议专用，未必被 is_private 覆盖
    "192.88.99.0/24",    # 6to4 中继 anycast，已废弃
))
_EXTRA_BLOCKED_V6 = tuple(ipaddress.ip_network(n) for n in (
    "2002::/16",         # 6to4，内嵌的 IPv4 可以是私网地址
))


def _pinned_as_ip(text):
    """解析成 ipaddress 对象；IPv4 映射的 IPv6 归一到 IPv4，例如 ::ffff:127.0.0.1。"""
    ip = ipaddress.ip_address(str(text).split("%")[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _pinned_is_blocked(ip):
    """loopback / private / link-local / reserved / multicast / unspecified 一律拒绝。"""
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified or not ip.is_global):
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return ip in _EXTRA_BLOCKED_V4
    return ip in _EXTRA_BLOCKED_V6


def _pinned_resolve(host):
    """解析主机的全部地址，IPv4 在前；有一个不可对外就整个主机名拒绝。"""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addresses = []
    for info in infos:
        try:
            ip = _pinned_as_ip(info[4][0])
        except ValueError:
            continue
        if ip not in addresses:
            addresses.append(ip)
    if not addresses:
        raise WebToolError("Could not resolve %s." % host)
    addresses.sort(key=lambda ip: ip.version)   # 没有 IPv6 出口时不必等它超时
    blocked = [str(ip) for ip in addresses if _pinned_is_blocked(ip)]
    if blocked:
        raise WebToolError("%s resolves to %s, which is not allowed." % (host, ", ".join(blocked)))
    return addresses


def _looks_like_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _pinned_guard_url(url):
    """只允许对外的一般 http(s) 网址，返回规范化 URL 与可连接的 IP 列表。

    主机名末尾的点先去掉，localhost. 与 localhost 是同一个名字；再看每一个
    解析结果。连接时只用这里返回的 IP，所以解析之后名字被改到内网也不受影响。
    """
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
        port = parsed.port
        host = (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        raise WebToolError("Invalid URL.")
    if parsed.scheme.lower() not in ("http", "https"):
        raise WebToolError("Only http:// or https:// URLs are supported.")
    if parsed.username or parsed.password:
        raise WebToolError("Credentials in the URL are not allowed.")
    if not host or host.endswith(".localhost") \
            or ("." not in host and not _looks_like_ip(host)):
        raise WebToolError("Private, loopback or single-label hosts are not allowed.")
    try:
        addresses = _pinned_resolve(host)
    except (OSError, UnicodeError):
        raise WebToolError("Could not resolve %s." % host)
    netloc = "[%s]" % host if ":" in host else host
    if port:
        netloc += ":%d" % port
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc)), addresses


def _http_get(url):
    """抓一个页面：逐跳校验重定向，返回文本、是否被截断与 Content-Type。"""
    deadline = time.monotonic() + FETCH_DEADLINE
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        target, addresses = _pinned_guard_url(current)
        parsed = urllib.parse.urlparse(target)
        try:
            status, headers, raw, truncated = _fetch(parsed, addresses, deadline)
        except (socket.timeout, TimeoutError):
            raise WebToolError("Timed out fetching %s (over %ds)." % (target, FETCH_DEADLINE))
        location = headers.get("Location")
        if status in REDIRECT_STATUS and location:
            current = urllib.parse.urljoin(target, location)
            continue
        if status >= 400:
            raise HttpStatusError(status)
        charset = headers.get_content_charset() or ""
        return _decode(raw, charset), truncated, headers.get("Content-Type") or ""
    raise WebToolError("Too many redirects (more than %d)." % MAX_REDIRECTS)


def _fetch_text(url):
    """抓一个网址并转成可读文字；结果进缓存。

    失败时回一句给模型的错误文本，成功时返回文字与是否被字节上限截断两个值。
    """
    key = ("fetch", url)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    try:
        text, truncated, ctype = _http_get(url)
    except HttpStatusError as exc:
        return "Error: %s answered HTTP %s." % (url, exc.status)
    except WebToolError as exc:
        return "Error: could not fetch %s (%s)." % (url, exc)
    except Exception as exc:
        return "Error: could not fetch %s (%s)." % (url, type(exc).__name__)
    if not _is_textual_content_type(ctype):
        main = ctype.split(";")[0].strip() or "a binary resource"
        _log("webtools: fetch refused non-text content-type %r for %s" % (ctype, url),
             level="WARN")
        return ("Error: %s is %s, which is not a text page the gateway can read."
                % (url, main))
    text = _strip_tags(text)
    data = (text, truncated)
    ttl = CACHE_TTL
    if ttl > 0 and len(text) <= MAX_CACHE_TEXT:
        _CACHE.put(key, data, ttl)
    return data


def fetch(url, start_index=0):
    try:
        start = max(0, int(start_index))
    except (TypeError, ValueError):
        start = 0
    data = _fetch_text(url)
    if isinstance(data, str):
        return data
    text, truncated = data
    if start >= len(text):
        return ("Error: startIndex %d is past the end of the page (%d characters "
                "total)." % (start, len(text)))
    end = min(start + MAX_FETCH_CHARS, len(text))
    head = "URL: %s%sCharacters: %d-%d of %d" % (url, chr(10), start, end, len(text))
    if truncated:
        head += (chr(10) + "Note: the response body was cut at %d bytes, so the page "
                 "may really be longer." % MAX_RESPONSE_BYTES)
    tail = ""
    if end < len(text):
        tail = (chr(10) + chr(10) + "[Truncated. Call web_fetch again with startIndex=%d "
                "to continue.]" % end)
    return head + chr(10) + chr(10) + text[start:end] + tail


def sources_from_result(result):
    """把搜索结果里的标题与网址读回来。

    喂给模型的是文字，但客户端要画引用来源需要结构化数据，所以在这里从我们
    自己产出的格式反解，不必另外保存状态。
    """
    out = []
    pattern = r"(?m)^\d+\.\s*(.+?)\s*\n\s*(https?://\S+)\s*$"
    for m in re.finditer(pattern, str(result or "")):
        url = m.group(2).strip().rstrip(".,;:!?")
        title = m.group(1).strip()
        if url and not any(s["url"] == url for s in out):
            out.append({"title": title or url, "url": url})
    return out


def execute(name, args_raw):
    """执行一次内部网络工具。永不抛出异常，永远返回一句能喂回模型的字符串。"""
    name = str(name or "").strip()
    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw or "{}")
        except Exception:
            args = {}
    elif isinstance(args_raw, dict):
        args = args_raw
    else:
        args = {}
    if not isinstance(args, dict):
        args = {}

    try:
        if name == WEB_SEARCH_NAME:
            return search(query_args(args), args.get("numResults") or 5)
        if name == WEB_FETCH_NAME:
            return fetch(url_arg(args), args.get("startIndex") or 0)
        return "Error: %s is not a tool this gateway runs." % name
    except Exception as exc:
        return "Error running %s: %s: %s" % (name, type(exc).__name__, exc)