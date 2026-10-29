"""添加账号时选择的官方客户端决定登录参数，也成为新账号的出站身份。

auth/state 的 platform 参数取客户端产品配置中的 authentication.attributes.platform：
国内版桌面端 5.9.3 为 workbuddy，国际版桌面端 5.8.4 为 workbuddy-ai，VS Code
插件为 VSCode，CodeBuddy CLI 为 CLI。登录完成后，账号的 product 与登录时选择的
客户端一致，重启后读回同一个值。

登录失败路径：无效 state、上游拒绝与轮询未完成都返回可读错误，账号池不落脏账号。

不联网：上游的 auth/state、auth/token 应答在本进程内构造。
"""
import base64
import json
import os
import sys
import unittest
import urllib.parse
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP_ENV = isolated_data_dirs("wb-login-product-")

import wb_accounts  # noqa: E402
import wb_identity  # noqa: E402
import wb_proxy as P  # noqa: E402

CASES = (
    ("cn", "workbuddy", "workbuddy"),
    ("intl", "workbuddy", "workbuddy-ai"),
    ("cn", "vscode", "VSCode"),
    ("intl", "vscode", "VSCode"),
    ("cn", "cli", "CLI"),
    ("intl", "cli", "CLI"),
)


def fake_token(uid):
    def part(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "%s.%s.sig" % (part({"alg": "none"}), part({"sub": uid, "exp": 4102444800}))


class FakeUpstream(object):
    def __init__(self, uid):
        self.uid = uid
        self.urls = []
        self.headers = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        self.headers.append(dict(kwargs.get("headers") or {}))
        if "/auth/state" in url:
            return {"code": 0, "data": {"state": "st-1", "authUrl": "https://login.example/x"}}
        return {"code": 0, "data": {"accessToken": fake_token(self.uid),
                                    "refreshToken": "rt"}}


PROFILE_HEADERS = []


class FakeProfileResponse(object):
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def fake_profile(req, **kwargs):
    PROFILE_HEADERS.append({k.lower(): v for k, v in req.headers.items()})
    return FakeProfileResponse(json.dumps({"data": {"nickname": "昵称"}}).encode("utf-8"))


class LoginProductTests(unittest.TestCase):
    def login(self, realm, product, uid):
        directory = os.path.join(os.environ["ACCOUNTS_DIR"], realm, product)
        os.makedirs(directory, exist_ok=True)
        pool = wb_accounts.AccountPool(directory)
        upstream = FakeUpstream(uid)
        PROFILE_HEADERS.clear()
        with mock.patch.object(wb_accounts, "http_json", upstream), \
             mock.patch.object(wb_accounts, "urlopen", fake_profile):
            started = pool.start_login(realm=realm, product=product)
            result = pool.poll_login(started["state"])
        return directory, started, result, upstream

    def test_platform_and_product_follow_the_chosen_client(self):
        for realm, product, platform in CASES:
            with self.subTest(realm=realm, product=product):
                uid = "uid-%s-%s" % (realm, product)
                directory, started, result, upstream = self.login(realm, product, uid)
                query = urllib.parse.parse_qs(urllib.parse.urlparse(upstream.urls[0]).query)
                self.assertEqual(query["platform"], [platform])
                self.assertEqual(started["product"], product)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["account"]["product"], product)
                self.assertEqual(result["account"]["platform"], platform)
                reloaded = {a.uid: a for a in wb_accounts.AccountPool(directory).load()}
                self.assertEqual(reloaded[uid].product, product)
                endpoint = wb_identity.endpoint_for(realm, product)[0]
                self.assertEqual(started["domain"],
                                 wb_identity.endpoint_for(realm, product)[1])
                auth_urls = [u for u in upstream.urls if "/auth/" in u]
                self.assertTrue(all(u.startswith(endpoint) for u in auth_urls), auth_urls)

    def test_login_headers_follow_the_chosen_client(self):
        for realm, product, _platform in CASES:
            with self.subTest(realm=realm, product=product):
                _, _, _, upstream = self.login(realm, product, "uid-h-%s-%s" % (realm, product))
                auth = [(u, h) for u, h in zip(upstream.urls, upstream.headers)
                        if "/auth/" in u]
                self.assertEqual(len(auth), 2, auth)
                for url, headers in auth:
                    if "auth/state" in url:
                        self.assertIn("Content-Type", headers)
                    else:
                        self.assertNotIn("Content-Type", headers)
                    if product in (wb_identity.PRODUCT_VSCODE,
                                   wb_identity.PRODUCT_CLI):
                        if "auth/state" in url:
                            self.assertEqual(
                                headers["X-Domain"],
                                wb_identity.endpoint_for(realm, product)[1])
                        else:
                            self.assertNotIn("X-Domain", headers)
                    self.assertEqual(headers["User-Agent"],
                                     wb_identity.identity_user_agent(realm, product))
                    for name in ("X-No-Authorization", "X-No-User-Id",
                                 "X-No-Enterprise-Id", "X-No-Department-Info"):
                        self.assertEqual(headers[name], "true")
                    self.assertIn("X-Trace-ID", headers)
                    self.assertIn("X-B3-TraceId", headers)
                    self.assertIn("X-B3-SpanId", headers)
                    self.assertEqual(headers["X-B3-Sampled"], "1")
                    self.assertIn("X-B3-ParentSpanId", headers)
                    self.assertTrue(headers["b3"].endswith("-1-"), headers["b3"])
                    self.assertNotIn("Origin", headers)
                    self.assertNotIn("Referer", headers)
                    self.assertNotIn("X-Requested-With", headers)
                    if product == wb_identity.PRODUCT_DESKTOP:
                        self.assertEqual(headers["X-Custom-Channel-Id"], "unknown")
                    else:
                        self.assertNotIn("X-Custom-Channel-Id", headers)
                # 拉昵称是带凭据的请求：只去掉 X-No-Authorization，其余标记头保留。
                self.assertTrue(PROFILE_HEADERS, "profile request was not captured")
                profile = PROFILE_HEADERS[0]
                self.assertTrue(profile["authorization"].startswith("Bearer "),
                                profile["authorization"])
                self.assertNotIn("x-no-authorization", profile)
                for name in ("x-no-user-id", "x-no-enterprise-id", "x-no-department-info"):
                    self.assertEqual(profile[name], "true")
                self.assertNotIn("content-type", profile)


class _RouteHandler(P.Handler):
    """路由方法的替身：继承 Handler 只记录应答；不调用基类初始化，不走 HTTP。"""

    def __init__(self):
        self.code = None
        self.payload = None

    def _error(self, code, message, err_type="server_error"):
        self.code = code
        self.payload = message
        return None

    def _json(self, code, obj):
        self.code = code
        self.payload = obj
        return None


def failure_upstream(state_response=None, poll_response=None, poll_error=None):
    """登录上游的应答替身：auth/state 与 auth/token 分开给。"""

    def stub(url, **kwargs):
        if "/auth/state" in url:
            return (state_response if state_response is not None
                    else {"code": 0, "data": {"state": "st-1",
                                              "authUrl": "https://login.example/x"}})
        if poll_error is not None:
            raise poll_error
        return (poll_response if poll_response is not None
                else {"code": 0, "data": {}})

    return stub


class LoginFailureTests(unittest.TestCase):
    """登录失败返回可读错误，账号池不落脏账号。"""

    def setUp(self):
        self.directory = os.path.join(os.environ["ACCOUNTS_DIR"], "failures")
        os.makedirs(self.directory, exist_ok=True)
        self.pool = wb_accounts.AccountPool(self.directory)

    def assert_no_account(self):
        self.assertEqual(self.pool.accounts, [], "失败路径落下了脏账号")
        leftovers = [name for name in os.listdir(self.directory)
                     if name.endswith(".json")]
        self.assertEqual(leftovers, [], "失败路径写了凭证文件")

    def _start(self, stub):
        with mock.patch.object(wb_accounts, "http_json", stub):
            return self.pool.start_login(realm="intl", product="cli")

    def test_polling_an_unknown_state_reports_it_and_adds_no_account(self):
        with mock.patch.object(wb_accounts, "http_json", failure_upstream()):
            result = self.pool.poll_login("state-never-started")
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["message"])
        self.assert_no_account()

    def test_polling_an_expired_login_reports_expired_and_adds_no_account(self):
        started = self._start(failure_upstream())
        with self.pool._lock:
            self.pool.logins[started["state"]]["created"] -= (
                wb_accounts.LOGIN_TTL_SECONDS + 1)
        with mock.patch.object(wb_accounts, "http_json", failure_upstream()):
            result = self.pool.poll_login(started["state"])
        self.assertEqual(result["status"], "expired")
        self.assertTrue(result["message"])
        self.assert_no_account()
        # 过期的登录状态被清掉，再查按未知状态回答。
        self.assertEqual(self.pool.poll_login(started["state"])["status"],
                         "unknown")

    def test_poll_upstream_rejection_reports_a_readable_error(self):
        started = self._start(failure_upstream())
        with mock.patch.object(wb_accounts, "http_json",
                               failure_upstream(poll_response={
                                   "code": 403, "msg": "denied by upstream"})):
            result = self.pool.poll_login(started["state"])
        self.assertEqual(result["status"], "error")
        self.assertIn("403", result["message"])
        self.assertIn("denied by upstream", result["message"])
        self.assert_no_account()

    def test_incomplete_poll_responses_stay_pending_without_an_account(self):
        cases = (("等待浏览器登录",
                  {"code": wb_accounts.LOGIN_PENDING,
                   "msg": "waiting for browser login"}),
                 ("应答还没有令牌", {"code": 0, "data": {}}))
        for label, response in cases:
            with self.subTest(case=label):
                started = self._start(failure_upstream())
                with mock.patch.object(wb_accounts, "http_json",
                                       failure_upstream(poll_response=response)):
                    result = self.pool.poll_login(started["state"])
                self.assertEqual(result["status"], "pending")
                self.assertTrue(result["message"])
                self.assert_no_account()
        # 轮询请求本身失败同样保持 pending。
        started = self._start(failure_upstream())
        with mock.patch.object(
                wb_accounts, "http_json",
                failure_upstream(poll_error=RuntimeError("connection reset"))):
            result = self.pool.poll_login(started["state"])
        self.assertEqual(result["status"], "pending")
        self.assertIn("connection reset", result["message"])
        self.assert_no_account()

    def test_start_login_rejection_raises_a_readable_error(self):
        with mock.patch.object(wb_accounts, "http_json",
                               failure_upstream(state_response={
                                   "code": 403, "msg": "denied"})):
            with self.assertRaises(RuntimeError) as caught:
                self.pool.start_login(realm="intl", product="cli")
        self.assertIn("auth/state", str(caught.exception))
        self.assert_no_account()

    def test_the_login_start_route_reports_upstream_failure_as_a_readable_error(self):
        handler = _RouteHandler()
        with mock.patch.object(P, "POOL", self.pool), \
                mock.patch.object(wb_accounts, "http_json",
                                  failure_upstream(state_response={
                                      "code": 403, "msg": "denied"})):
            P.Handler._route_accounts_login_start(
                handler, {"realm": "intl", "product": "cli"})
        self.assertEqual(handler.code, 502)
        self.assertIn("could not start login", str(handler.payload))
        self.assertIn("auth/state", str(handler.payload))
        self.assert_no_account()


if __name__ == "__main__":
    unittest.main(verbosity=2)
