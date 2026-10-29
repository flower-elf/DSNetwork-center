import base64
import json
import os
import sys
import tempfile
import time
import unittest

_startup_dir = tempfile.TemporaryDirectory(prefix="cockpit-import-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_proxy as P


_TOKEN_EXP = int(time.time()) + 3600


def fake_jwt(uid="uid-cockpit", issuer="https://www.workbuddy.ai"):
    def part(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    header = part({"alg": "none", "typ": "JWT"})
    # exp 取模块级常量：同一进程内同一个 uid 必须得到同一串 token，用例会比较
    # 两次调用拼出的 token 是否相同。
    payload = part({"sub": uid, "iss": issuer, "exp": _TOKEN_EXP})
    return header + "." + payload + ".signature"


def cockpit_row(**extra):
    row = {
        "id": "c1",
        "email": "user@example.com",
        "uid": "uid-cockpit",
        "nickname": "",
        "access_token": fake_jwt(),
        "refresh_token": "refresh-token",
        "token_type": "Bearer",
        "expires_at": int((time.time() + 3600) * 1000),
        "domain": "www.workbuddy.ai",
    }
    row.update(extra)
    return row


class CockpitRowTests(unittest.TestCase):
    def test_snake_case_row_maps_to_account_kwargs(self):
        kwargs = wb_accounts.normalise_import_row(cockpit_row())
        self.assertEqual(kwargs["uid"], "uid-cockpit")
        self.assertEqual(kwargs["accessToken"], fake_jwt())
        self.assertEqual(kwargs["refreshToken"], "refresh-token")
        self.assertEqual(kwargs["realm"], "intl")
        self.assertEqual(kwargs["source"], "cockpit")
        # expires_at 以毫秒计，账号里存的是秒。
        self.assertLess(abs(kwargs["expiresAt"] - (time.time() + 3600)), 120)

    def test_nickname_falls_back_to_email(self):
        kwargs = wb_accounts.normalise_import_row(cockpit_row(nickname=""))
        self.assertEqual(kwargs["nickname"], "user@example.com")
        named = wb_accounts.normalise_import_row(cockpit_row(nickname="Named"))
        self.assertEqual(named["nickname"], "Named")

    def test_cn_domain_stays_cn(self):
        row = cockpit_row(domain="copilot.tencent.com",
                          access_token=fake_jwt(issuer="https://copilot.tencent.com"))
        kwargs = wb_accounts.normalise_import_row(row)
        self.assertEqual(kwargs["realm"], "cn")

    def test_missing_token_is_rejected(self):
        row = cockpit_row()
        row["access_token"] = ""
        with self.assertRaises(ValueError):
            wb_accounts.normalise_import_row(row)


class CockpitPoolImportTests(unittest.TestCase):
    def test_import_rows_adds_cockpit_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = wb_accounts.AccountPool(directory)
            report = pool.import_rows([
                cockpit_row(uid="uid-a", access_token=fake_jwt("uid-a")),
                cockpit_row(uid="uid-b", access_token=fake_jwt("uid-b")),
            ])
            self.assertEqual(sorted(report["added"]), ["uid-a", "uid-b"])
            self.assertEqual(report["invalid"], [])
            self.assertEqual(len(pool.accounts), 2)
            for account in pool.accounts:
                self.assertEqual(account.source, "cockpit")
                self.assertTrue(account.refresh_token)
                self.assertTrue(os.path.exists(os.path.join(directory,
                                                            account.uid + ".json")))

    def test_duplicate_is_skipped_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = wb_accounts.AccountPool(directory)
            pool.import_rows([cockpit_row(uid="uid-a",
                                          access_token=fake_jwt("uid-a"))])
            report = pool.import_rows([cockpit_row(uid="uid-a",
                                                   access_token=fake_jwt("uid-a"))])
            self.assertEqual(report["added"], [])
            self.assertEqual(report["skipped"][0]["reason"], "already exists")


class _RouteHandler(P.Handler):
    """路由方法的替身：继承 Handler 只记录应答；不调用基类初始化，不走 HTTP。"""

    def __init__(self):
        self.code = None
        self.payload = None

    def _authorized(self):
        return True

    def _error(self, code, message, err_type="server_error"):
        self.code = code
        self.payload = message
        return None

    def _json(self, code, obj):
        self.code = code
        self.payload = obj
        return None


class AccountsDocumentRouteTests(unittest.TestCase):
    """/accounts/export 导出账号文档；/accounts/import 按选项预览或覆盖。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-accounts-io-")
        self.addCleanup(self._tmp.cleanup)
        self.pool = wb_accounts.AccountPool(self._tmp.name)
        self.addCleanup(setattr, P, "POOL", P.POOL)
        P.POOL = self.pool

    def _seed(self, uid):
        report = self.pool.import_rows(
            [cockpit_row(uid=uid, access_token=fake_jwt(uid))])
        self.assertEqual(report["added"], [uid])

    def test_export_document_carries_the_account_fields(self):
        self._seed("uid-exp")
        handler = _RouteHandler()
        P.Handler._get_accounts_export(handler, {})
        self.assertEqual(handler.code, 200)
        doc = handler.payload
        self.assertEqual(doc["format"], wb_accounts.EXPORT_FORMAT)
        self.assertEqual(doc["count"], 1)
        row = doc["accounts"][0]
        self.assertEqual(row["uid"], "uid-exp")
        self.assertEqual(row["accessToken"], fake_jwt("uid-exp"))
        self.assertEqual(row["refreshToken"], "refresh-token")
        self.assertEqual(row["realm"], "intl")

    def test_export_can_leave_out_the_tokens(self):
        self._seed("uid-exp")
        handler = _RouteHandler()
        P.Handler._get_accounts_export(handler, {"secrets": ["0"]})
        row = handler.payload["accounts"][0]
        self.assertEqual(row["uid"], "uid-exp")
        self.assertNotIn("accessToken", row)
        self.assertNotIn("refreshToken", row)

    def test_import_dry_run_previews_without_writing(self):
        before = sorted(os.listdir(self._tmp.name))
        handler = _RouteHandler()
        P.Handler._route_accounts_import(handler, {
            "data": [cockpit_row(uid="uid-new", access_token=fake_jwt("uid-new"))],
            "dryRun": True,
        })
        self.assertEqual(handler.code, 200)
        self.assertIs(handler.payload["dryRun"], True)
        self.assertEqual(handler.payload["result"]["added"], ["uid-new"])
        self.assertEqual(self.pool.accounts, [])
        self.assertEqual(sorted(os.listdir(self._tmp.name)), before,
                         "预览往账号目录写了文件")

    def test_import_overwrite_replaces_an_existing_uid(self):
        self._seed("uid-a")
        row = cockpit_row(uid="uid-a", access_token=fake_jwt("uid-a"),
                          refresh_token="replacement-refresh")
        handler = _RouteHandler()
        P.Handler._route_accounts_import(handler, {"data": [row]})
        self.assertEqual(handler.payload["result"]["skipped"],
                         [{"uid": "uid-a", "reason": "already exists"}])
        self.assertEqual(self.pool.get("uid-a").refresh_token, "refresh-token")
        handler = _RouteHandler()
        P.Handler._route_accounts_import(handler, {"data": [row],
                                                   "overwrite": True})
        self.assertEqual(handler.payload["result"]["updated"], ["uid-a"])
        self.assertEqual(self.pool.get("uid-a").refresh_token,
                         "replacement-refresh")
        reloaded = wb_accounts.AccountPool(self._tmp.name)
        reloaded.load()
        self.assertEqual(reloaded.get("uid-a").refresh_token,
                         "replacement-refresh")

    def test_bare_array_body_is_wrapped_as_an_import_document(self):
        handler = _RouteHandler()
        P.Handler._handle_accounts(handler, "/accounts/import",
                                   [cockpit_row(uid="uid-bare",
                                                access_token=fake_jwt("uid-bare"))])
        self.assertEqual(handler.code, 200)
        self.assertEqual(handler.payload["result"]["added"], ["uid-bare"])
        self.assertIsNotNone(self.pool.get("uid-bare"))


if __name__ == "__main__":
    unittest.main()
