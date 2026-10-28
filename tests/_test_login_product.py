"""添加账号时选择的官方客户端决定登录参数，也成为新账号的出站身份。

auth/state 的 platform 参数取客户端产品配置中的 authentication.attributes.platform：
国内版桌面端 5.9.3 为 workbuddy，国际版桌面端 5.8.4 为 workbuddy-ai，VS Code
插件为 VSCode，CodeBuddy CLI 为 CLI。登录完成后，账号的 product 与登录时选择的
客户端一致，重启后读回同一个值。

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
             mock.patch.object(wb_accounts.urllib.request, "urlopen", fake_profile):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
