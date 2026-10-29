"""「未活跃账号一键打卡」的服务端名单过滤（POST /accounts/daily-chat 的 uids）。

面板只把今天还没打过卡的国际版账号列进 uids；服务端必须严格按这份名单执行：
多打一个就白烧一个账号的额度（重复打卡不给积分），漏打一个则那天的积分悄悄丢掉。
名单是调用方的明确意图，所以空名单等于「一个都不打」，不能回落成「全部账号」。

用 python tests/run_all.py _test_daily_chat_pending_route 运行。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_proxy


class FakeAccount(object):
    def __init__(self, uid, realm="intl", enabled=True, nickname=None):
        self.uid = uid
        self.realm = realm
        self.enabled = enabled
        self.nickname = nickname
        self.calls = 0

    def daily_chat(self, trigger="unknown"):
        self.calls += 1
        return {"ok": True, "msg": "已打卡 (%s)" % trigger}


class FakePool(object):
    def __init__(self, accounts):
        self.accounts = accounts

    def get(self, uid):
        for account in self.accounts:
            if account.uid == uid:
                return account
        return None

    def list_public(self, realm=None):
        return []


class RouteTests(unittest.TestCase):
    class Handler(wb_proxy.Handler):
        def __init__(self):
            self.captured = None

        def _json(self, code, obj):
            self.captured = (code, obj)

        def _error(self, code, message, err_type="server_error"):
            self.captured = (code, {"error": message})

    def setUp(self):
        self.accounts = [
            FakeAccount("i1", nickname="国际一"),
            FakeAccount("i2", nickname="国际二"),
            FakeAccount("i3", nickname="国际三"),
            FakeAccount("i-off", enabled=False),
            FakeAccount("c1", realm="cn"),
        ]
        self.pool = FakePool(self.accounts)
        for target, value in (("POOL", self.pool),
                              ("log", lambda *a, **k: None)):
            patcher = mock.patch.object(wb_proxy, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        views = mock.patch.object(wb_accounts, "account_views", lambda realm=None: [])
        views.start()
        self.addCleanup(views.stop)

    def call(self, payload):
        handler = self.Handler()
        handler._route_accounts_daily_chat(payload)
        code, body = handler.captured
        self.assertEqual(code, 200)
        return body

    def called(self):
        return [a.uid for a in self.accounts if a.calls]

    def test_only_the_listed_accounts_are_checked_in(self):
        body = self.call({"uids": ["i2", "i3"]})
        self.assertEqual([r["uid"] for r in body["results"]], ["i2", "i3"])
        self.assertEqual(self.called(), ["i2", "i3"])
        self.assertEqual(self.accounts[0].calls, 0, "名单外的账号一个都不能碰")

    def test_disabled_and_other_realm_uids_are_dropped(self):
        body = self.call({"uids": ["i1", "i-off", "c1", "no-such-uid", None]})
        self.assertEqual([r["uid"] for r in body["results"]], ["i1"])
        self.assertEqual(self.called(), ["i1"])

    def test_an_empty_list_checks_in_nobody(self):
        body = self.call({"uids": []})
        self.assertEqual(body["results"], [])
        self.assertEqual(self.called(), [], "空名单不能被读成「全部账号」")

    def test_without_a_list_every_enabled_intl_account_is_checked_in(self):
        body = self.call({})
        self.assertEqual([r["uid"] for r in body["results"]], ["i1", "i2", "i3"])
        self.assertEqual(self.called(), ["i1", "i2", "i3"])

    def test_a_single_uid_still_wins(self):
        body = self.call({"uid": "i3", "uids": ["i1"]})
        self.assertEqual([r["uid"] for r in body["results"]], ["i3"])
        self.assertEqual(self.called(), ["i3"])

    def test_a_non_list_uids_is_ignored_like_no_filter(self):
        body = self.call({"uids": "i1"})
        self.assertEqual([r["uid"] for r in body["results"]], ["i1", "i2", "i3"])


if __name__ == "__main__":
    unittest.main()
