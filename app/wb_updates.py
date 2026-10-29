# -*- coding: utf-8 -*-
"""更新发现（只发现，不安装）：拉 GitHub releases 元数据并比对版本号。

默认关闭，缺键或 false 都算关闭，关闭时后台一个请求都不发。失败不致命，只记
一行短原因。落盘只有时间、版本号和发布页链接；请求地址、请求头、响应体不落。
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import wb_settings

# 主路径：走 API 不抓 HTML，页面改版不影响检查。查的是本仓库，版本号与来源仓库无关。
REPO = "flower-elf/DSNetwork-center"
RELEASES_URL = "https://api.github.com/repos/%s/releases" % REPO
# 回退路径：302 Location 末尾是最新稳定版 tag；按网页计流量，不受 API 匿名配额
# 60 次/小时/出口 IP 的限制，也不需要凭据。
LATEST_RELEASE_URL = "https://github.com/%s/releases/latest" % REPO
# 发布页总入口：面板的「打开发布页」在没有单条发布链接时用它，按钮始终可点。
RELEASES_PAGE = "https://github.com/%s/releases" % REPO
TAG_RE = re.compile(r"/releases/tag/([^/?#]+)")
# 可选凭据：认证配额 5000 次/小时。只从环境变量读，绝不落盘。
TOKEN_ENV = "GITHUB_TOKEN"
USER_AGENT = "wb2api-hub-update-check"
REQUEST_TIMEOUT = 5
# releases 文档几十 KB；设上限，坏响应不能把内存拉满。
MAX_BODY = 2 * 1024 * 1024

# 自动检查的默认节流窗口（24 小时），面板可改成 1 小时到 30 天；重启不变频繁，
# 一次失败也不整天不再试。
SCHEDULED_INTERVAL_SECONDS = 24 * 3600
# 启动后先等一会儿再花掉这个请求，让监听端口和账号池先就绪。
FIRST_DELAY_SECONDS = 30
# 后台线程醒来的间隔；真正的节流靠 due() 判断，这里只决定看一眼的频率。
POLL_SECONDS = 300

# 只认能排序的稳定版本号 vX.Y.Z；-rc1、日期 tag、分支名一律跳过，猜顺序比不报更糟。
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(text):
    match = VERSION_RE.match(str(text or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def is_newer(candidate, current):
    left, right = parse_version(candidate), parse_version(current)
    return bool(left and right and left > right)


def safe_url(value):
    """发布页链接，或 ""。远端值会被面板原样显示，只放行 GitHub 的 https 发布页这一种形状。"""
    text = str(value or "").strip()
    if not text.startswith("https://github.com/"):
        return ""
    return text[:300]


def pick_latest(releases, current_version):
    """最新的稳定版条目，没有就 None。跳过草稿、预发布和非 vX.Y.Z tag；取版本号最大的，
    列表顺序不决定结果，重新发布的旧 tag 不能靠排序取胜。"""
    best = None
    for entry in releases if isinstance(releases, list) else []:
        if not isinstance(entry, dict):
            continue
        if entry.get("draft") or entry.get("prerelease"):
            continue
        version = parse_version(entry.get("tag_name"))
        if version is None:
            continue
        if best is None or version > best[0]:
            best = (version, entry)
    if best is None:
        return None
    version, entry = best
    text = "%d.%d.%d" % version
    return {
        "version": text,
        "tag": str(entry.get("tag_name") or "").strip()[:64],
        "url": safe_url(entry.get("html_url")),
        "published_at": str(entry.get("published_at") or "").strip()[:32],
        "newer": is_newer(text, current_version),
    }


def describe_failure(exc):
    """一行能安全外发的失败原因：异常消息可能带完整 URL 和响应体，只取异常类型和状态码。"""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return "HTTP %d" % code
    if isinstance(exc, json.JSONDecodeError):
        return "invalid JSON"
    if isinstance(exc, ValueError):
        return "unexpected release document"
    if isinstance(exc, (urllib.error.URLError, OSError)):
        return "network error: %s" % type(exc).__name__
    return type(exc).__name__


def fetch_releases(url=RELEASES_URL, timeout=REQUEST_TIMEOUT):
    """releases 文档的原始 JSON 文本；失败抛异常。

    API 走匿名配额 60 次/小时/出口 IP，共享代理出口可能被邻居耗光；API 一跳失败
    就退回网页端点拿同一条结论，两边都失败抛 API 的原因，它才是主路径。"""
    try:
        return _fetch_api(url, timeout)
    except Exception as api_error:
        try:
            return json.dumps([fetch_latest_release(timeout)])
        except Exception:
            raise api_error


def _fetch_api(url, timeout):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    token = str(os.environ.get(TOKEN_ENV) or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_BODY).decode("utf-8", "replace")


def fetch_latest_release(timeout=REQUEST_TIMEOUT):
    """网页端点上的最新稳定版，返回一条 releases 条目。releases/latest 302 到
    /releases/tag/<tag>，tag 从最终 URL 取，不解析 HTML；发布日期这一跳留空。"""
    request = urllib.request.Request(LATEST_RELEASE_URL,
                                     headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final = response.geturl()
    match = TAG_RE.search(final or "")
    if not match:
        raise ValueError("no release tag in the redirect target")
    return {
        "tag_name": match.group(1),
        "draft": False,
        "prerelease": False,
        "html_url": final,
        "published_at": "",
    }


def _stamp(value):
    if not value:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


class UpdateChecker(threading.Thread):
    def __init__(self, current_version, settings_dir, fetcher=None, log=None,
                 first_delay=FIRST_DELAY_SECONDS, poll_seconds=POLL_SECONDS):
        super().__init__(daemon=True, name="update-check")
        self.current_version = str(current_version or "").strip()
        self.settings_dir = settings_dir
        # 注入点：测试给一个假的 fetcher，就不必碰网络。
        self._fetch = fetcher or fetch_releases
        self._log = log
        self._first_delay = max(0.0, float(first_delay))
        self._poll_seconds = max(1.0, float(poll_seconds))
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._checking = False
        self._latest = None
        self._last_error = ""
        self._last_attempt = 0.0
        self._last_success = 0.0
        self._load_state()

    # ---- 状态 -----------------------------------------------------------
    def _load_state(self):
        try:
            state = wb_settings.update_check_state(self.settings_dir)
        except Exception:
            return
        if (state.get("repo") or "") != REPO:
            # 旧记录来自别的仓库：版本号与链接作废，清掉节奏时间让启动检查
            # 立刻按新目标查一次。
            return
        self._last_attempt = state.get("last_attempt") or 0.0
        self._last_success = state.get("last_success") or 0.0
        version = state.get("latest_version") or ""
        if version:
            self._latest = {
                "version": version, "tag": "",
                # 链接与发布时间跟着版本落盘：重启后未重新检查时弹窗也能给出
                # 可点的发布页链接。
                "url": state.get("latest_url") or "",
                "published_at": state.get("published_at") or "",
                "newer": is_newer(version, self.current_version),
            }

    def _remember(self, success, version="", url="", published_at=""):
        try:
            wb_settings.record_update_check(
                self.settings_dir, at=self._last_attempt,
                success=success, latest_version=version,
                latest_url=url, published_at=published_at, repo=REPO)
        except Exception:
            # 记账失败不该影响一次已经完成的检查：内存里的结论照样能用。
            pass

    def enabled(self):
        try:
            return wb_settings.update_check_enabled(self.settings_dir)
        except Exception:
            return False

    def interval_seconds(self):
        try:
            hours = wb_settings.update_check_interval_hours(self.settings_dir)
        except Exception:
            return SCHEDULED_INTERVAL_SECONDS
        return max(3600, int(hours) * 3600)

    def due(self, now=None):
        now = time.time() if now is None else float(now)
        if not self._last_attempt:
            return True
        return (now - self._last_attempt) >= self.interval_seconds()

    def status(self):
        picked = self._latest or {}
        latest = picked.get("version") or ""
        return {
            "current_version": self.current_version,
            "latest_version": latest or None,
            "update_available": is_newer(latest, self.current_version),
            "enabled": self.enabled(),
            "interval_hours": self.interval_seconds() // 3600,
            "checking": self._checking,
            "last_attempt": _stamp(self._last_attempt),
            "last_success": _stamp(self._last_success),
            "last_error": self._last_error,
            "release_url": picked.get("url") or "",
            # 面板的跳转按钮兜底：没有单条发布链接时也能打开发布页。
            "releases_url": RELEASES_PAGE,
            "published_at": picked.get("published_at") or "",
        }

    def log(self, msg):
        if not self._log:
            return
        try:
            self._log(msg)
        except Exception:
            pass

    # ---- 检查 -----------------------------------------------------------
    def check(self, manual=False):
        """跑一次发现，返回状态字典。manual=True 绕过开关和节流窗口，必须真的去问
        一次；后台路径关闭或没到点直接返回，一个请求都不发。不持有请求路径要等的锁。"""
        status = self.status()
        if not manual:
            if not self.enabled():
                status["skipped"] = "disabled"
                return status
            if not self.due():
                status["skipped"] = "not due yet"
                return status
        with self._lock:
            if self._checking:
                status["skipped"] = "already running"
                return status
            self._checking = True
        try:
            self._run_check()
        finally:
            with self._lock:
                self._checking = False
        return self.status()

    def _run_check(self):
        self._last_attempt = time.time()
        self._last_error = ""
        try:
            document = json.loads(self._fetch(RELEASES_URL, REQUEST_TIMEOUT))
            if not isinstance(document, list):
                raise ValueError("releases document is not a list")
            picked = pick_latest(document, self.current_version)
        except Exception as exc:
            self._last_error = describe_failure(exc)
            self._remember(success=False)
            self.log("更新检查失败：%s" % self._last_error)
            return
        self._latest = picked
        self._last_success = self._last_attempt
        version = (picked or {}).get("version") or ""
        self._remember(success=True, version=version,
                       url=(picked or {}).get("url") or "",
                       published_at=(picked or {}).get("published_at") or "")
        if picked and picked.get("newer"):
            self.log("发现新版本 %s（当前 %s）" % (picked["version"], self.current_version))
        elif picked:
            self.log("已是最新版本 %s" % self.current_version)

    # ---- 后台线程 -------------------------------------------------------
    def wake(self):
        """偏好刚从关拨到开时叫醒停着的循环。"""
        self._wake.set()

    def stop(self):
        self._stop_event.set()
        self._wake.set()

    def _park(self, seconds):
        self._wake.wait(seconds)
        self._wake.clear()

    def run(self):
        # 启动即查一次，面板登录后能立刻提示新版本；节流窗口照旧生效，重启不会变频繁。
        if self.enabled() and not self._stop_event.is_set():
            try:
                self.check(manual=False)
            except Exception as exc:
                self.log("更新检查异常：%s" % describe_failure(exc))
        if self._stop_event.wait(self._first_delay):
            return
        while not self._stop_event.is_set():
            try:
                self.check(manual=False)
            except Exception as exc:
                self.log("更新检查异常：%s" % describe_failure(exc))
            self._park(self._poll_seconds)
