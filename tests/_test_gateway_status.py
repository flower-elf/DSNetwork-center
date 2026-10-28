"""/health 公开宣告存活与能否接单，接不了单时回 503 与原因，不暴露账号身份、调度或密钥状态。

/panel/status 只给面板会话：没有会话直接 401，报体只有错误本身。
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-status-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP

import wb_proxy as proxy
import wb_settings


class Request(object):
    """不带凭据的请求：没有面板会话，也没有 API Key。"""

    _error = proxy.Handler._error

    def __init__(self, authenticated=False):
        self._authenticated = authenticated
        self.sent = None
        self.headers = {}
        self.key_entry = None

    def _panel_ok(self):
        return self._authenticated

    def _key_ok(self):
        return self._authenticated

    def _handle_expect_continue(self):
        pass

    def _discard_body(self):
        pass

    def _json(self, status, payload):
        self.sent = (status, payload)
        return self.sent


class FakePool(object):
    def __init__(self, total, ready):
        self.accounts = [object()] * total
        self._ready = ready

    def count_ready(self, realm=None, model=None):
        return self._ready

    def representative(self):
        return None


class HealthTests(unittest.TestCase):
    def health(self, pool):
        with mock.patch.object(proxy, "POOL", pool):
            return proxy.Handler._get_health(Request())

    def test_alive_with_a_ready_account(self):
        status, info = self.health(FakePool(total=2, ready=1))
        self.assertEqual(status, 200)
        self.assertTrue(info["ok"])
        self.assertEqual((info["accounts"], info["accounts_ready"]), (2, 1))

    def test_no_account_can_serve_is_still_alive(self):
        for pool in (FakePool(total=2, ready=0), FakePool(total=0, ready=0)):
            status, info = self.health(pool)
            self.assertEqual(status, 200)
            self.assertTrue(info["ok"])
            self.assertEqual(info["accounts_ready"], 0)

    def test_a_pool_that_never_loaded(self):
        status, info = self.health(None)
        self.assertEqual(status, 200)
        self.assertTrue(info["ok"])
        self.assertEqual(info["accounts"], 0)

    def test_a_configured_key_does_not_change_the_body(self):
        with tempfile.TemporaryDirectory(prefix="wb-status-keys-") as directory:
            key = {"name": "panel key", "key": "panel-secret", "enabled": True}
            wb_settings.set_api_keys(directory, [key])
            with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, POOL=FakePool(total=1, ready=1)):
                status, info = proxy.Handler._get_health(Request())
                self.assertEqual(status, 200)
                self.assertTrue(info["ok"])
                self.assertNotIn("uid", info)


class PanelStatusTests(unittest.TestCase):
    def test_without_a_session_it_reports_unauthenticated(self):
        request = Request(authenticated=False)
        with tempfile.TemporaryDirectory(prefix="wb-status-panel-") as directory:
            with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, POOL=FakePool(total=4, ready=2)):
                status, info = proxy.Handler._get_panel_status(request)
        self.assertEqual(status, 200)
        self.assertFalse(info["authenticated"])
        self.assertNotIn("accounts", info)

    def test_with_a_session_it_carries_the_account_counts(self):
        with tempfile.TemporaryDirectory(prefix="wb-status-panel-") as directory:
            with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, POOL=FakePool(total=4, ready=2)):
                status, info = proxy.Handler._get_panel_status(Request(authenticated=True))
        self.assertEqual(status, 200)
        self.assertEqual((info["accounts"], info["accounts_ready"]), (4, 2))
        self.assertIn("api_key_set", info)


class StartupUserAgentTests(unittest.TestCase):
    def test_each_present_realm_gets_its_own_desktop_ua(self):
        accounts = [
            SimpleNamespace(realm="intl"),
            SimpleNamespace(realm="cn"),
            SimpleNamespace(realm="cn"),
        ]
        self.assertEqual(proxy.startup_user_agents(accounts), [
            ("cn", proxy.wb_accounts.REALM_CONFIGS["cn"]["chat_ua"]),
            ("intl", proxy.wb_accounts.REALM_CONFIGS["intl"]["chat_ua"]),
        ])

    def test_a_realm_without_accounts_is_skipped(self):
        self.assertEqual(proxy.startup_user_agents([SimpleNamespace(realm="cn")]), [
            ("cn", proxy.wb_accounts.REALM_CONFIGS["cn"]["chat_ua"]),
        ])

    def test_no_accounts_or_unknown_realms_print_nothing(self):
        self.assertEqual(proxy.startup_user_agents([]), [])
        self.assertEqual(proxy.startup_user_agents(None), [])
        self.assertEqual(proxy.startup_user_agents([SimpleNamespace(realm="other")]), [])


if __name__ == "__main__":
    unittest.main()
