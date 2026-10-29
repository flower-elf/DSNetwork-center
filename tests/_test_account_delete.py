"""账号删除与批量开关：/accounts/delete 移除账号与凭证文件，/accounts/set-all 批量翻转启用状态。

不联网：路由方法直接驱动，账号用普通令牌由字典构造，只写临时目录。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-account-delete-")

import wb_accounts  # noqa: E402
import wb_proxy as P  # noqa: E402


def account(uid, **fields):
    data = {"uid": uid, "accessToken": "token-" + uid, "realm": "intl"}
    data.update(fields)
    return wb_accounts.Account(data)


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


class AccountDeleteRouteTests(unittest.TestCase):
    """删除后账号从账号池与磁盘一起消失。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-account-delete-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name
        self.pool = wb_accounts.AccountPool(self.directory)
        self.pool.add(account("uid-del"))
        self.pool.add(account("uid-keep"))
        self.addCleanup(setattr, P, "POOL", P.POOL)
        P.POOL = self.pool

    def _delete(self, payload):
        handler = _RouteHandler()
        P.Handler._route_accounts_delete(handler, payload)
        return handler

    def test_delete_removes_the_account_from_the_pool_and_disk(self):
        path = self.pool.get("uid-del").path
        self.assertTrue(os.path.exists(path))
        handler = self._delete({"uid": "uid-del"})
        self.assertEqual(handler.code, 200)
        self.assertIs(handler.payload["deleted"], True)
        self.assertEqual([row["uid"] for row in handler.payload["accounts"]],
                         ["uid-keep"])
        self.assertIsNone(self.pool.get("uid-del"))
        self.assertFalse(os.path.exists(path), "删除后凭证文件还留在磁盘上")
        reloaded = wb_accounts.AccountPool(self.directory)
        reloaded.load()
        self.assertEqual([a.uid for a in reloaded.accounts], ["uid-keep"])

    def test_deleting_an_unknown_uid_reports_it_and_keeps_the_rest(self):
        handler = self._delete({"uid": "uid-missing"})
        self.assertEqual(handler.code, 200)
        self.assertIs(handler.payload["deleted"], False)
        self.assertEqual([row["uid"] for row in handler.payload["accounts"]],
                         ["uid-del", "uid-keep"])

    def test_delete_without_a_uid_is_rejected(self):
        handler = self._delete({})
        self.assertEqual(handler.code, 400)
        self.assertIn("uid required", str(handler.payload))
        self.assertEqual(len(self.pool.accounts), 2)


class AccountSetAllRouteTests(unittest.TestCase):
    """/accounts/set-all 批量翻转启用状态，并写回每个凭证文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-account-set-all-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name
        self.pool = wb_accounts.AccountPool(self.directory)
        self.pool.add(account("uid-a"))
        self.pool.add(account("uid-b", realm="cn"))
        self.addCleanup(setattr, P, "POOL", P.POOL)
        P.POOL = self.pool

    def _set_all(self, enabled):
        handler = _RouteHandler()
        P.Handler._route_accounts_set_all(handler, {"enabled": enabled})
        self.assertEqual(handler.code, 200)
        return handler.payload

    def test_set_all_disables_every_account_and_persists_it(self):
        payload = self._set_all(False)
        self.assertTrue(all(not row["enabled"] for row in payload["accounts"]))
        self.assertTrue(all(not a.enabled for a in self.pool.accounts))
        reloaded = wb_accounts.AccountPool(self.directory)
        reloaded.load()
        self.assertTrue(all(not a.enabled for a in reloaded.accounts))

    def test_set_all_enables_every_account_and_lifts_stand_down(self):
        self.pool.set_all_enabled(False)
        P.STAND_DOWN.update({"active": True, "at": 1.0, "uid": "uid-a",
                             "detail": "restricted"})
        self.addCleanup(P.STAND_DOWN.update,
                        {"active": False, "at": 0.0, "uid": "", "detail": ""})
        payload = self._set_all(True)
        self.assertTrue(all(row["enabled"] for row in payload["accounts"]))
        self.assertTrue(all(a.enabled for a in self.pool.accounts))
        self.assertIs(payload["stand_down"]["active"], False)
        self.assertFalse(P.stand_down_active())
        reloaded = wb_accounts.AccountPool(self.directory)
        reloaded.load()
        self.assertTrue(all(a.enabled for a in reloaded.accounts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
