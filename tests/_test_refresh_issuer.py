"""令牌刷新必须回到登录签发渠道：CLI 登录用 CLI 端点与 UA，VSC 用 VSC，桌面用 WB。

刷新与当前对话身份（product）无关：登录后切到另一种身份，刷新仍走签发方。
刷新结果的持久化：成功把新令牌写回凭证文件，失败保留旧令牌并记下错误。
所有上游请求都由 stub 捕获，不联网。
"""

import base64
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_accounts
import wb_identity


def fake_jwt(uid="uid-desktop"):
    def part(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "%s.%s.sig" % (
        part({"alg": "none"}),
        part({"sub": uid, "exp": 4102444800}))


def make_account(realm, login_product, product):
    return wb_accounts.Account({
        "uid": "uid-refresh",
        "accessToken": "old-token",
        "refreshToken": "refresh-token",
        "realm": realm,
        "platform": "CLI",
        "product": product,
        "loginProduct": login_product,
        "domain": "www.workbuddy.cn",
    })


class RefreshIssuerTests(unittest.TestCase):
    def test_refresh_uses_the_login_issuer_not_the_current_product(self):
        for realm in ("intl", "cn"):
            for login_product in ("workbuddy", "vscode", "cli"):
                with self.subTest(realm=realm, issuer=login_product):
                    account = make_account(
                        realm, login_product,
                        "cli" if login_product == "workbuddy" else "workbuddy")
                    seen = []

                    def fake_http_json(url, **kwargs):
                        seen.append((url, dict(kwargs["headers"])))
                        return {"data": {"accessToken": "new-token",
                                         "refreshToken": "new-refresh"}}

                    with mock.patch.object(wb_accounts, "http_json", fake_http_json):
                        self.assertTrue(account.refresh(force=True))

                    self.assertEqual(len(seen), 1)
                    url, headers = seen[0]
                    endpoint, domain = wb_identity.endpoint_for(realm, login_product)
                    self.assertEqual(url, endpoint + wb_accounts.REFRESH_PATH)
                    self.assertEqual(headers["User-Agent"],
                                     wb_identity.identity_user_agent(realm, login_product))
                    self.assertEqual(headers["X-Auth-Refresh-Source"], "plugin")
                    self.assertEqual(headers["X-Domain"], domain)
                    self.assertEqual(headers["X-Refresh-Token"], "refresh-token")
                    self.assertIn("X-Trace-ID", headers)
                    self.assertIn("b3", headers)
                    self.assertNotIn("Origin", headers)
                    self.assertNotIn("Referer", headers)
                    self.assertNotIn("X-User-Id", headers)
                    self.assertEqual(account.access_token, "new-token")
                    self.assertEqual(account.refresh_token, "new-refresh")

    def test_legacy_credentials_infer_the_issuer(self):
        vscode = wb_accounts.Account({
            "uid": "uid-vsc", "accessToken": "t", "refreshToken": "r",
            "platform": "VSCode", "product": "workbuddy",
        })
        self.assertEqual(vscode.login_product, "vscode")

        desktop_import = wb_accounts.Account({
            "uid": "uid-desktop", "accessToken": "t", "refreshToken": "r",
            "platform": "CLI", "source": "desktop-app", "product": "workbuddy",
        })
        self.assertEqual(desktop_import.login_product, "workbuddy")

        cli_login = wb_accounts.Account({
            "uid": "uid-cli", "accessToken": "t", "refreshToken": "r",
            "platform": "CLI", "source": "oauth", "product": "workbuddy",
        })
        self.assertEqual(cli_login.login_product, "cli")

    def test_desktop_shaped_import_row_uses_desktop_issuer(self):
        kwargs = wb_accounts.normalise_import_row({
            "auth": {"accessToken": fake_jwt("uid-desktop"), "refreshToken": "r"},
            "account": {"uid": "uid-desktop", "nickname": "desktop"},
        }, realm="intl")
        self.assertEqual(kwargs["loginProduct"], "workbuddy")

    def test_explicit_login_product_survives_a_product_switch(self):
        account = make_account("cn", "cli", "workbuddy")
        self.assertEqual(account.login_product, "cli")
        self.assertEqual(account.to_dict()["loginProduct"], "cli")


class RefreshPersistenceTests(unittest.TestCase):
    """刷新成功把新令牌写进凭证文件；失败保留旧令牌并记下错误。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-refresh-save-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name

    def _saved_account(self):
        account = make_account("cn", "cli", "workbuddy")
        account.save(self.directory)
        return account

    def _on_disk(self):
        with open(os.path.join(self.directory, "uid-refresh.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_successful_refresh_writes_the_new_tokens_to_the_credential_file(self):
        account = self._saved_account()
        new_token = fake_jwt("uid-refresh")

        def fake_http_json(url, **kwargs):
            return {"data": {"accessToken": new_token,
                             "refreshToken": "new-refresh"}}

        with mock.patch.object(wb_accounts, "http_json", fake_http_json):
            self.assertTrue(account.refresh(force=True))
        self.assertEqual(account.access_token, new_token)
        self.assertEqual(account.refresh_token, "new-refresh")
        on_disk = self._on_disk()
        self.assertEqual(on_disk["accessToken"], new_token)
        self.assertEqual(on_disk["refreshToken"], "new-refresh")
        self.assertEqual(on_disk["expiresAt"], 4102444800)
        reloaded = wb_accounts.AccountPool(self.directory)
        reloaded.load()
        stored = reloaded.get("uid-refresh")
        self.assertEqual(stored.access_token, new_token)
        self.assertEqual(stored.refresh_token, "new-refresh")
        self.assertEqual(stored.expires_at, 4102444800)

    def test_a_failed_refresh_keeps_the_old_tokens_and_records_the_error(self):
        def raise_401(url, **kwargs):
            # HTTPError 抛出前先关掉它持有的响应流，避免退出时的资源告警。
            exc = urllib.error.HTTPError(url, 401, "Unauthorized", None,
                                         io.BytesIO(b""))
            exc.close()
            raise exc

        def raise_network(url, **kwargs):
            raise RuntimeError("connection reset")

        def no_token(url, **kwargs):
            return {"code": 401, "msg": "token rejected"}

        cases = (("上游 401", raise_401),
                 ("网络异常", raise_network),
                 ("应答没有令牌", no_token))
        for label, stub in cases:
            with self.subTest(case=label):
                account = self._saved_account()
                with mock.patch.object(wb_accounts, "http_json", stub):
                    self.assertFalse(account.refresh(force=True))
                self.assertEqual(account.access_token, "old-token")
                self.assertEqual(account.refresh_token, "refresh-token")
                self.assertTrue(account.last_error, "刷新失败没有记录错误")
                self.assertIn("refresh", account.last_error)
                on_disk = self._on_disk()
                self.assertEqual(on_disk["accessToken"], "old-token")
                self.assertEqual(on_disk["refreshToken"], "refresh-token")


if __name__ == "__main__":
    unittest.main()
