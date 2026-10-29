"""一键切换所有出站身份：面板工具栏那个按钮背后的那条路由。

    python tests/_test_product_switch.py

按钮提交时不带 uid、也不带 realm，全量切换靠的就是服务端这条分支。它的语义没有别
处钉着，而错了的表现很安静：只切了一个账号、或者一个都没切，面板都提示成功。

Run with: python tests/_test_product_switch.py
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

# 在 wb_proxy 导入读取 ACCOUNTS_DIR 之前，先准备套件自己的 accounts/ 目录。
_TMP = isolated_data_dirs("wb-product-switch-")

import wb_accounts as A  # noqa: E402
import wb_identity  # noqa: E402
import wb_proxy as proxy  # noqa: E402

UIDS = ("intl-aaa-1", "intl-bbb-2", "cn-ccc-3")


class FakeRequest(object):
    """够被测路由使用的最小 Handler 替身。"""

    _route_accounts_product = proxy.Handler._route_accounts_product

    def __init__(self, payload):
        self.payload = payload
        self.answering = []

    def _error(self, status, message, kind=""):
        self.answering.append(("error", status, message))
        return status, message

    def _json(self, status, payload):
        self.answering.append(("json", status, payload))
        return status, payload

    def call(self):
        return self._route_accounts_product(self.payload)

    def answered(self):
        return self.answering[-1]


class ProductSwitchTests(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(_TMP.name, "accounts")
        os.makedirs(self.dir, exist_ok=True)
        for name in os.listdir(self.dir):
            path = os.path.join(self.dir, name)
            if os.path.isfile(path):
                os.unlink(path)
        self.pool = A.AccountPool(self.dir, log=lambda _m: None)
        for index, uid in enumerate(UIDS):
            self.pool.add(A.Account({
                "uid": uid,
                "nickname": uid,
                "realm": "cn" if index == 2 else "intl",
                "accessToken": "tok-" + uid,
                "product": "workbuddy",
            }))
        proxy.ACCOUNTS_DIR = self.dir
        proxy.POOL = self.pool
        # 这条路由记录每次切换，这正是日志的用途，但 8 个用例的日志会淹没
        # 套件自己的输出。
        real_log = proxy.log
        proxy.log = lambda *a, **k: None
        self.addCleanup(setattr, proxy, "log", real_log)

    def products(self):
        return dict((a.uid, a.product) for a in self.pool.accounts)

    def reload(self):
        pool = A.AccountPool(self.dir, log=lambda _m: None)
        pool.load()
        return dict((a.uid, a.product) for a in pool.accounts)

    def test_no_uid_switches_every_account(self):
        """工具栏按钮走的就是这条：不带 uid、不带 realm。"""
        status, payload = FakeRequest({"product": "cli"}).call()
        self.assertEqual(status, 200)
        self.assertEqual(payload["product"], "cli")
        self.assertEqual(payload["changed"],
                         [uid[:8] for uid in UIDS])
        self.assertEqual(set(self.products().values()), {"cli"})

    def test_realm_narrows_the_batch(self):
        status, payload = FakeRequest({"product": "vscode", "realm": "intl"}).call()
        self.assertEqual(status, 200)
        self.assertEqual(sorted(payload["changed"]), ["intl-aaa", "intl-bbb"])
        self.assertEqual(self.products()["cn-ccc-3"], "workbuddy")

    def test_realm_all_means_everyone(self):
        status, payload = FakeRequest({"product": "vscode", "realm": "all"}).call()
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["changed"]), 3)
        self.assertEqual(set(self.products().values()), {"vscode"})

    def test_a_single_uid_still_switches_one_account(self):
        """行内那三个按钮（WB / VSC / CLI）不能被批量分支抢走。"""
        status, _payload = FakeRequest({"product": "cli", "uid": "intl-bbb-2"}).call()
        self.assertEqual(status, 200)
        self.assertEqual(self.products()["intl-bbb-2"], "cli")
        self.assertEqual(self.products()["intl-aaa-1"], "workbuddy")
        self.assertEqual(self.products()["cn-ccc-3"], "workbuddy")

    def test_an_unknown_product_is_rejected(self):
        status, _payload = FakeRequest({"product": "vsc"}).call()
        self.assertEqual(status, 400)
        self.assertEqual(set(self.products().values()), {"workbuddy"})

    def test_the_switch_survives_a_restart(self):
        """set_product() 只改内存；这条路由必须当场落盘。"""
        FakeRequest({"product": "cli"}).call()
        self.assertEqual(set(self.reload().values()), {"cli"})

    def test_switching_to_the_same_product_reports_no_change(self):
        FakeRequest({"product": "cli"}).call()
        status, payload = FakeRequest({"product": "cli"}).call()
        self.assertEqual(status, 200)
        self.assertEqual(payload["changed"], [])

    def test_every_valid_product_is_accepted(self):
        for product in wb_identity.VALID_PRODUCTS:
            status, payload = FakeRequest({"product": product}).call()
            self.assertEqual(status, 200, product)
            self.assertEqual(payload["product"], product)


if __name__ == "__main__":
    unittest.main()
