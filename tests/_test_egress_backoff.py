"""测 issue #271 的出口级瞬时退避契约：开关打开后，传输故障与 502/503/504 记到出口的共享计数器上，秒级退避，一次成功请求即清零，账号的 fails/degrade 计数不动；500 仍记在账号上，401/403/429 走各自路径。事故实测数据是 4 小时内 196 个 URLError 把共享同一出口的 13 个账号封停最长约 3 小时。本测试不联网，直接运行本文件即可：账号由 dict 构造，上游调用打桩，time.sleep 只记录不睡眠。
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as A
import wb_egress
import wb_proxy
import wb_usage as U
import wb_settings
import wb_upstream_pool


class EgressKeyTests(unittest.TestCase):
    """出口身份必须与连接池的 key 完全一致。"""

    URL = "https://www.workbuddy.ai/v2/chat/completions"

    def test_direct_is_one_shared_egress(self):
        # 不走代理时所有账号同一出口，共享一个 key。
        self.assertEqual(wb_upstream_pool.egress_key(self.URL, ""),
                         ("https", "www.workbuddy.ai", 443, ""))

    def test_proxy_string_is_the_identity_not_the_slot(self):
        key = wb_upstream_pool.egress_key(self.URL, "http://10.0.0.9:8080")
        self.assertEqual(key, ("https", "www.workbuddy.ai", 443,
                               "http://10.0.0.9:8080"))

    def test_two_slots_pointing_at_one_address_share_a_bucket(self):
        # 指向同一地址的两个槽位不能拆成两个计数器。
        self.assertEqual(
            wb_upstream_pool.egress_key(self.URL, "http://10.0.0.9:8080"),
            wb_upstream_pool.egress_key(self.URL, " http://10.0.0.9:8080 "))

    def test_bypassed_proxy_folds_to_direct(self):
        # 连接池把被系统绕过的代理折算回直连，出口记账必须同样折算，否则一条物理路径会变成两个计数器。
        with mock.patch.object(wb_upstream_pool, "_proxy_bypassed",
                               return_value=True):
            key = wb_upstream_pool.egress_key(self.URL, "http://10.0.0.9:8080")
        self.assertEqual(key, ("https", "www.workbuddy.ai", 443, ""))

    def test_non_http_proxy_keeps_its_own_identity(self):
        # socks5 不经过连接池，但仍是独立的出口。
        key = wb_upstream_pool.egress_key(self.URL, "socks5://10.0.0.9:1080")
        self.assertEqual(key[3], "socks5://10.0.0.9:1080")


class EgressBackoffTests(unittest.TestCase):
    """秒级退避阶梯与共享计数器。"""

    def setUp(self):
        wb_egress.reset()

    def test_ladder_is_seconds_and_capped(self):
        self.assertEqual(wb_egress.transient_backoff(1), 1.0)
        self.assertEqual(wb_egress.transient_backoff(2), 2.0)
        self.assertEqual(wb_egress.transient_backoff(3), 4.0)
        self.assertEqual(wb_egress.transient_backoff(9),
                         wb_egress.EGRESS_BACKOFF_MAX)

    def test_backoff_is_three_orders_below_the_account_ladder(self):
        # 账号级熔断 30 分钟起步，出口退避必须从秒级起步。
        self.assertLess(wb_egress.EGRESS_BACKOFF_MAX, A.BREAKER_COOLDOWN)

    def test_note_transient_grows_one_shared_counter(self):
        key = ("https", "www.workbuddy.ai", 443, "")
        first = wb_egress.note_transient(key)
        second = wb_egress.note_transient(key)
        self.assertEqual(wb_egress.streak(key), 2)
        # 抖动让两次返回值不同，但底层阶梯必须已经涨了一档。
        self.assertGreaterEqual(first, 1.0 * wb_egress.EGRESS_JITTER_LOW)
        self.assertLessEqual(first, 1.0 * wb_egress.EGRESS_JITTER_HIGH)
        self.assertGreaterEqual(second, 2.0 * wb_egress.EGRESS_JITTER_LOW)
        self.assertLessEqual(second, 2.0 * wb_egress.EGRESS_JITTER_HIGH)

    def test_success_clears_the_streak(self):
        key = ("https", "www.workbuddy.ai", 443, "")
        wb_egress.note_transient(key)
        wb_egress.note_transient(key)
        self.assertEqual(wb_egress.streak(key), 2)
        wb_egress.note_success(key)
        self.assertEqual(wb_egress.streak(key), 0)
        # 且阶梯从基础档重新起步，不延续之前的档位。
        wait = wb_egress.note_transient(key)
        self.assertLessEqual(wait, 1.0 * wb_egress.EGRESS_JITTER_HIGH)

    def test_quiet_window_starts_a_new_ladder(self):
        key = ("https", "www.workbuddy.ai", 443, "")
        now = 1000.0
        wb_egress.note_transient(key, now=now)
        wb_egress.note_transient(key, now=now + 1)
        self.assertEqual(wb_egress.streak(key, now=now + 1), 2)
        later = now + wb_egress.EGRESS_STREAK_RESET + 5
        self.assertEqual(wb_egress.streak(key, now=later), 0)


class _Sock(object):
    def settimeout(self, value):
        self.timeouts = getattr(self, "timeouts", []) + [value]


class _Response(object):
    """urllib response 的最小替身，只覆盖 open_upstream 成功路径用到的接口。"""

    def __init__(self, sock):
        self._sock = sock
        self.closed = False

    @property
    def fp(self):
        sock = self._sock

        class Raw(object):
            _sock = sock

        class Fp(object):
            raw = Raw()

        return Fp()

    def close(self):
        self.closed = True


class _OpenUpstreamHarness(unittest.TestCase):
    """用打桩的连接池和上游调用驱动真实的 open_upstream()。"""

    def setUp(self):
        wb_egress.reset()
        self.sleeps = []
        self._patches = [
            mock.patch.object(time, "sleep", side_effect=self.sleeps.append),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.accounts = []

    def add_account(self, uid, proxy=""):
        account = A.Account(
            {"uid": uid, "accessToken": "t", "realm": "intl",
             "proxy": proxy},
            os.path.join(self.dir, uid + ".json"))
        self.accounts.append(account)
        return account

    def _pool(self):
        accounts = self.accounts

        class Pool(object):
            affinity = type("Affinity", (), {
                "unbind": lambda self, key: None,
                "demote": lambda self, key, uid=None: None})()

            def count_ready(self, realm, model=None):
                return len(accounts)

            def pick_for_session(self, realm, session_key=None, exclude=(),
                                 model=None, page=None, claim=None):
                for account in accounts:
                    if account.uid not in exclude and (claim is None or claim(account)):
                        return account
                return None

            def list_public(self):
                return []

            def apply_daily_token_limit(self, *a, **k):
                return 0

            def apply_daily_credit_limit(self, *a, **k):
                return 0

            def apply_model_daily_token_limit(self, *a, **k):
                return 0

            def apply_remaining_weights(self, weights=None):
                return weights or {}

        pool = Pool()
        pool.accounts = accounts
        return pool

    def run_request(self, urlopen, egress_backoff=None):
        """调用一次 open_upstream；抛异常时返回 (exc, None)，成功时返回 (None, result)。"""
        if egress_backoff is not None:
            wb_settings.set_egress_backoff_enabled(self.dir, egress_backoff)
        old_pool, old_urlopen = wb_proxy.POOL, A.urlopen
        wb_proxy.POOL = self._pool()
        A.urlopen = urlopen
        try:
            with mock.patch.object(wb_proxy, "ACCOUNTS_DIR", self.dir):
                try:
                    result = wb_proxy.open_upstream(
                        {"model": "deepseek-v4.1-flash",
                         "messages": [{"role": "user", "content": "hi"}]},
                        target_realm="intl")
                except Exception as exc:  # noqa: BLE001 - 这里就是要拿到异常来检查
                    return exc, None
                return None, result
        finally:
            wb_proxy.POOL, A.urlopen = old_pool, old_urlopen


class SwitchOnTests(_OpenUpstreamHarness):
    """开关打开后，失败记在出口路径上，账号不承担惩罚。"""

    def test_transport_error_charges_the_egress_not_the_account(self):
        account = self.add_account("u-egress")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.URLError("The handshake operation timed out")

        raised, _result = self.run_request(urlopen, egress_backoff=True)
        self.assertIsNotNone(raised)
        # 账号不受影响：无 fails、无 degrade、无熔断。
        self.assertEqual(account.fails, 0)
        self.assertEqual(account.degrade_count, 0)
        self.assertEqual(account.breaker_until, 0.0)
        self.assertEqual(account.degrade_until, 0.0)
        # 变的是出口计数器，只加一次：重试一轮重新选中同一账号也一样，而且所有
        # 睡眠都在秒级。
        key = wb_upstream_pool.egress_key(
            account.chat_base_url() + wb_proxy.CHAT_PATH, account.proxy)
        self.assertEqual(wb_egress.streak(key), 1)
        self.assertTrue(self.sleeps)
        self.assertLessEqual(max(self.sleeps), wb_proxy.EGRESS_SLEEP_MAX)

    def test_one_request_charges_a_shared_egress_once(self):
        # tried.clear() 会在瞬时故障后重新选账号，没有每次请求的守卫，一次客户端
        # 请求就会把共享计数器推上好几档（issue #271 “one request charges one
        # account twice” 的另一半）。
        account = self.add_account("u-once")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.URLError("The handshake operation timed out")

        self.run_request(urlopen, egress_backoff=True)
        key = wb_upstream_pool.egress_key(
            account.chat_base_url() + wb_proxy.CHAT_PATH, account.proxy)
        self.assertEqual(wb_egress.streak(key), 1)

    def test_free_model_retries_paid_sibling_only_after_6004_when_opted_in(self):
        account = self.add_account("u-credit-fallback")
        reset = time.time() + 600
        detail = ('{"code":6004,"msg":"usage exceeds frequency limit, '
                  'reset at 2026-10-16 23:00:00 UTC+8"}')
        error = urllib.error.HTTPError("https://upstream.invalid", 429,
                                       "rate limit", {}, io.BytesIO(detail.encode()))
        sent_models = []

        def urlopen(req, timeout=30, proxy=""):
            sent_models.append(json.loads(req.data.decode())["model"])
            if len(sent_models) == 1:
                raise error
            return _Response(_Sock())

        with mock.patch.object(wb_settings, "free_model_credit_fallback", return_value=True), \
                mock.patch.object(wb_proxy, "parse_rate_limit_reset", return_value=reset), \
                mock.patch.object(U, "apply_daily_guards", return_value=0), \
                mock.patch.object(wb_proxy, "upstream_timeouts", return_value=(2, 2)):
            raised, result = self.run_request(urlopen)

        self.assertIsNone(raised)
        self.assertEqual(sent_models, ["deepseek-v4.1-flash",
                                       "deepseek-v4.1-flash-sg"])
        self.assertEqual(result[0]._wb_billed_model, "deepseek-v4.1-flash-sg")
        self.assertGreater(account.cap_until("deepseek-v4.1-flash"), time.time())
        saved = []
        with mock.patch.object(U, "_persist_usage",
                               side_effect=lambda row, _label: saved.append(dict(row))):
            U.record_upstream_usage(
                result[0], "deepseek-v4.1-flash",
                {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
        self.assertEqual(saved[0]["model"], "deepseek-v4.1-flash-sg")
        self.assertEqual(saved[0]["requested_model"], "deepseek-v4.1-flash")
        result[0].close()
        error.close()

    def test_paid_fallback_tries_each_account_before_product_rotation(self):
        first = self.add_account("u-paid-first")
        second = self.add_account("u-paid-second")
        reset = time.time() + 600
        for account in (first, second):
            account.note_model_cap("deepseek-v4.1-flash", reset)

        pool = self._pool()
        pool.count_ready = lambda realm, model=None: sum(
            a.ready(model=model) for a in self.accounts if a.realm == realm)
        pool.pick_for_session = lambda realm, session_key=None, exclude=(), model=None, page=None, claim=None: next(
            (a for a in self.accounts
             if a.uid not in exclude and a.realm == realm and a.ready(model=model)
             and (claim is None or claim(a))), None)
        paid_cap = urllib.error.HTTPError(
            "https://upstream.invalid", 429, "rate limit", {},
            io.BytesIO(b'{"code":6004,"msg":"model limit"}'))
        sent = []
        switch_attempts = []

        def urlopen(req, timeout=30, proxy=""):
            model = json.loads(req.data.decode())["model"]
            headers = dict((k.lower(), v) for k, v in req.header_items())
            sent.append((model, headers.get("x-user-id"),
                         headers.get("x-model-id")))
            if headers.get("x-user-id") == first.uid:
                raise paid_cap
            return _Response(_Sock())

        old_pool, old_urlopen = wb_proxy.POOL, A.urlopen
        wb_proxy.POOL, A.urlopen = pool, urlopen
        try:
            with mock.patch.object(wb_settings, "free_model_credit_fallback", return_value=True), \
                    mock.patch.object(U, "apply_daily_guards", return_value=0), \
                    mock.patch.object(wb_proxy, "upstream_timeouts", return_value=(2, 2)), \
                    mock.patch.object(wb_proxy, "auto_switch_product_enabled", return_value=True), \
                    mock.patch.object(wb_proxy, "_try_switch_product",
                                      side_effect=lambda account, model: switch_attempts.append(
                                          (account.uid, model)) or False):
                response, _account, _effort = wb_proxy.open_upstream(
                    {"model": "deepseek-v4.1-flash", "messages": [
                        {"role": "user", "content": "hi"}]}, target_realm="intl")
        finally:
            wb_proxy.POOL, A.urlopen = old_pool, old_urlopen
            paid_cap.close()

        self.assertEqual(sent, [
            ("deepseek-v4.1-flash-sg", first.uid, "deepseek-v4.1-flash-sg"),
            ("deepseek-v4.1-flash-sg", second.uid, "deepseek-v4.1-flash-sg"),
        ])
        self.assertEqual(switch_attempts, [])
        self.assertEqual(response._wb_billed_model, "deepseek-v4.1-flash-sg")
        response.close()

    def test_two_accounts_on_one_egress_share_the_counter(self):
        # 整个改动的核心：同一路径后的 N 个账号不能各自背各自的惩罚。
        first = self.add_account("u-a")
        self.add_account("u-b")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.URLError("connection reset by peer")

        self.run_request(urlopen, egress_backoff=True)
        key = wb_upstream_pool.egress_key(
            first.chat_base_url() + wb_proxy.CHAT_PATH, first.proxy)
        self.assertEqual(wb_egress.streak(key), 1)
        for account in self.accounts:
            self.assertEqual(account.fails, 0)
            self.assertEqual(account.breaker_until, 0.0)

    def test_gateway_5xx_charges_the_egress(self):
        account = self.add_account("u-gateway")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)

        raised, _result = self.run_request(urlopen, egress_backoff=True)
        self.assertIsNotNone(raised)
        self.assertEqual(account.fails, 0)
        key = wb_upstream_pool.egress_key(
            account.chat_base_url() + wb_proxy.CHAT_PATH, account.proxy)
        self.assertEqual(wb_egress.streak(key), 1)

    def test_500_stays_on_the_account(self):
        # 500 刻意仍记在账号上，留待后续观察。
        account = self.add_account("u-500")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)

        self.run_request(urlopen, egress_backoff=True)
        self.assertGreater(account.fails, 0)

    def test_a_served_request_clears_the_egress_streak(self):
        account = self.add_account("u-recover")
        sock = _Sock()
        calls = {"n": 0}

        def urlopen(req, timeout=30, proxy=""):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("connection reset by peer")
            return _Response(sock)

        raised, result = self.run_request(urlopen, egress_backoff=True)
        self.assertIsNone(raised)
        self.assertIsNotNone(result)
        self.assertEqual(account.fails, 0)
        key = wb_upstream_pool.egress_key(
            account.chat_base_url() + wb_proxy.CHAT_PATH, account.proxy)
        self.assertEqual(wb_egress.streak(key), 0)


class SwitchOffTests(_OpenUpstreamHarness):
    """开关默认关闭，原有的计账行为必须保持不变。"""

    def test_transport_error_still_feeds_the_account(self):
        account = self.add_account("u-legacy")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.URLError("The handshake operation timed out")

        raised, _result = self.run_request(urlopen, egress_backoff=False)
        self.assertIsNotNone(raised)
        # 开关关闭时：传输错误使 fails 与 degrade_count 增长。
        self.assertGreater(account.fails, 0)
        self.assertGreater(account.degrade_count, 0)
        key = wb_upstream_pool.egress_key(
            account.chat_base_url() + wb_proxy.CHAT_PATH, account.proxy)
        self.assertEqual(wb_egress.streak(key), 0)

    def test_default_is_off(self):
        self.add_account("u-default")
        self.assertFalse(wb_settings.egress_backoff_enabled(self.dir))


if __name__ == "__main__":
    unittest.main()
