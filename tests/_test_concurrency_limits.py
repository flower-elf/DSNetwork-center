"""单账号单模型的并发上限：同一账号的不同模型各自计数，名额满时报 429，续写轮复用调用方的名额；不联网。

用 python _test_concurrency_limits.py 运行。
"""
import io
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_accounts as accounts
import wb_proxy as proxy


def stub_pool(account, pick=None):
    """open_upstream 需要的最小账号池替身；pick 覆盖选号，复现真实选号跳过满名额账号的效果。"""
    class Pool(object):
        accounts = [account]
        smart_routing = False
        affinity = types.SimpleNamespace(unbind=lambda _key: None,
                                         demote=lambda _key, _uid=None: None)

        def count_ready(self, realm, model=None):
            return sum(a.ready(model=model) for a in self.accounts)

        def get(self, uid):
            return account if uid == account.uid else None

        def pick_for_session(self, realm, session_key=None, exclude=(), model=None,
                             page=None, claim=None):
            if pick is not None:
                return pick(account, exclude, model)
            return next((a for a in self.accounts
                         if a.uid not in exclude and a.realm == realm
                         and a.ready(model=model) and (claim is None or claim(a))), None)

        def apply_daily_token_limit(self, value=None, usage=None):
            return value or 0

        def apply_remaining_weights(self, weights=None):
            return weights or {}

    return Pool()


class ConcurrencyLimitTests(unittest.TestCase):
    def account(self):
        return accounts.Account({"uid": "synthetic-cn", "realm": "cn",
                                 "accessToken": "token"})

    def call(self, account, payload, pool=None, lease=None, **kwargs):
        old_pool = proxy.POOL
        proxy.POOL = pool or stub_pool(account)
        try:
            return proxy.open_upstream(payload, target_realm="cn",
                                       lease=lease or proxy.SlotLease(), **kwargs)
        finally:
            proxy.POOL = old_pool

    def test_concurrency_limit_is_per_account_and_per_model(self):
        account = self.account()
        account.concurrency_limit = 1
        self.assertTrue(account.acquire_request("glm-5.3"))
        self.assertFalse(account.acquire_request("glm-5.3"),
                         "同一账号同一模型的第二个请求应当被挡下")
        self.assertTrue(account.acquire_request("deepseek-v4.1"),
                        "另一个模型有自己的名额，不该被 glm-5.3 占用")
        self.assertEqual(account.active_for_model("glm-5.3"), 1)
        self.assertEqual(account.active_for_model("deepseek-v4.1"), 1)
        self.assertEqual(account.public()["activeRequests"], 2)
        account.release_request("glm-5.3")
        self.assertTrue(account.acquire_request("glm-5.3"), "退掉的名额要能立刻再用")
        account.release_request("glm-5.3")
        account.release_request("glm-5.3")
        self.assertEqual(account.active_for_model("glm-5.3"), 0, "计数不能被退成负数")

    def test_zero_means_unlimited_and_the_limit_round_trips(self):
        account = self.account()
        self.assertEqual(account.concurrency_limit, 0, "没设过的账号按不限处理")
        for _ in range(50):
            self.assertTrue(account.acquire_request("m"), "0 表示不限，不该被挡")
        self.assertEqual(account.public()["activeRequests"], 50)
        for _ in range(50):
            account.release_request("m")
        self.assertEqual(account.public()["activeRequests"], 0)

        directory = tempfile.mkdtemp(prefix="concurrency-round-trip-")
        pool = accounts.AccountPool(directory)
        path = os.path.join(directory, "uid-1.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"uid": "uid-1", "realm": "cn", "accessToken": "t"}, fh)
        pool.load()
        row = pool.set_concurrency_limit("uid-1", 7)
        self.assertEqual(row["concurrencyLimit"], 7)
        reloaded = accounts.AccountPool(directory)
        reloaded.load()
        self.assertEqual(reloaded.get("uid-1").concurrency_limit, 7,
                         "重启后要读回同一个上限")
        self.assertEqual(reloaded.get("uid-1").public()["concurrencyLimit"], 7)

    def test_a_hand_edited_limit_is_clamped_to_a_sane_value(self):
        self.assertEqual(accounts.normalise_concurrency_limit(0), (0, ""))
        self.assertEqual(accounts.normalise_concurrency_limit(1000), (1000, ""))
        for bad in (-1, 1001, 1.5, True, None, "abc"):
            limit, problem = accounts.normalise_concurrency_limit(bad)
            self.assertIsNone(limit, bad)
            self.assertTrue(problem, bad)
        self.assertEqual(accounts.normalise_concurrency_limit("12"), (12, ""))
        # 文件里写坏的值退回不限，不据此限制账号。
        account = accounts.Account({"uid": "u", "realm": "cn", "accessToken": "t",
                                    "concurrencyLimit": -5})
        self.assertEqual(account.concurrency_limit, 0)

    def test_the_cap_is_honoured_by_the_selection_paths(self):
        """名额满的账号要从两个选号路径里都消失，退了名额再回来。"""
        account = self.account()
        account.concurrency_limit = 1
        pool = accounts.AccountPool(tempfile.mkdtemp(prefix="cap-pick-"))
        pool.accounts = [account]

        for smart in (False, True):
            pool.smart_routing = smart
            self.assertIs(pool.pick(realm="cn", model="glm-5.3"), account,
                          "smart=%s 时空闲账号应当被选中" % smart)
            self.assertTrue(account.acquire_request("glm-5.3"))
            self.assertIsNone(pool.pick(realm="cn", model="glm-5.3"),
                              "smart=%s 时名额已满的账号不该再被选中" % smart)
            self.assertIs(pool.pick(realm="cn", model="other-model"), account,
                          "smart=%s 时另一个模型不受影响" % smart)
            account.release_request("glm-5.3")

    def test_a_saturated_pool_answers_429_instead_of_503(self):
        """名额用满是「暂不接单」，要报 429，不能报「账号池不可用」。"""
        account = self.account()
        account.concurrency_limit = 1
        self.assertTrue(account.acquire_request("glm-5.3"))

        def skip_saturated(acct, exclude, model):
            return (acct if acct.uid not in exclude and acct.ready(model=model)
                    and acct.has_request_capacity(model) else None)

        old_pool = proxy.POOL
        proxy.POOL = stub_pool(account, pick=skip_saturated)
        try:
            with self.assertRaises(proxy.RateLimited) as caught:
                proxy.open_upstream({"model": "glm-5.3", "messages": [
                    {"role": "user", "content": "hello"}]}, target_realm="cn",
                    lease=proxy.SlotLease())
        finally:
            proxy.POOL = old_pool
        self.assertIn("concurrent request limit", caught.exception.message)
        self.assertNotIn("no usable account", str(caught.exception)), \
            "名额用满不等于账号池不可用"
        self.assertEqual(caught.exception.wait, 1)

    def test_a_web_tool_continuation_reuses_its_own_slot(self):
        """续写轮选回原账号时沿用调用方的名额，上限为 1 的账号也能完成续写。"""
        account = self.account()
        account.concurrency_limit = 1
        response = object()
        lease = proxy.SlotLease()

        old_pool, old_urlopen = proxy.POOL, accounts.urlopen
        proxy.POOL = stub_pool(account)
        accounts.urlopen = lambda *a, **k: response
        try:
            proxy.open_upstream({"model": "glm-5.3", "messages": [
                {"role": "user", "content": "hi"}]}, target_realm="cn", lease=lease)
            got, picked, _effort = proxy.follow_up_with_tool_results(
                [], {"base_body": {"model": "glm-5.3"}, "base_messages": [],
                     "convo_messages": [], "realm": "cn"},
                "glm-5.3", "sess-1", 0.0, lease)
        finally:
            proxy.POOL, accounts.urlopen = old_pool, old_urlopen
        self.assertIs(got, response, "续写轮要拿到新的上游连线")
        self.assertIs(picked, account)
        self.assertEqual(account.active_for_model("glm-5.3"), 1,
                         "续写轮不能额外占一个名额")
        lease.release()
        self.assertEqual(account.active_for_model("glm-5.3"), 0)

    def test_a_failed_continuation_keeps_the_outer_slot(self):
        account = self.account()
        account.concurrency_limit = 1
        lease = proxy.SlotLease()
        old_pool, old_urlopen = proxy.POOL, accounts.urlopen
        proxy.POOL = stub_pool(account)
        accounts.urlopen = lambda *a, **k: object()
        try:
            proxy.open_upstream({"model": "glm-5.3", "messages": [
                {"role": "user", "content": "hi"}]}, target_realm="cn", lease=lease)
        finally:
            proxy.POOL, accounts.urlopen = old_pool, old_urlopen

        error = urllib.error.HTTPError("https://upstream.invalid", 500, "boom", {},
                                       io.BytesIO(b""))
        proxy.POOL = stub_pool(account)
        accounts.urlopen = lambda *a, **k: (_ for _ in ()).throw(error)
        try:
            with self.assertRaises(Exception):
                proxy.follow_up_with_tool_results(
                    [], {"base_body": {"model": "glm-5.3"}, "base_messages": [],
                         "convo_messages": [], "realm": "cn"},
                    "glm-5.3", "sess-1", 0.0, lease)
        finally:
            proxy.POOL, accounts.urlopen = old_pool, old_urlopen
            error.close()
        self.assertEqual(account.active_for_model("glm-5.3"), 1,
                         "外层请求还拿着名额，不能被续写轮退掉")
        lease.release()
        self.assertEqual(account.active_for_model("glm-5.3"), 0)

    def test_a_throttled_continuation_moves_to_another_account(self):
        """续写轮 429 换号时，名额跟着转到新账号。"""
        first = accounts.Account({"uid": "uid-a", "realm": "cn", "accessToken": "a"})
        second = accounts.Account({"uid": "uid-b", "realm": "cn", "accessToken": "b"})
        pool = accounts.AccountPool(tempfile.mkdtemp(prefix="cap-follow-"))
        pool.accounts = [first, second]
        lease = proxy.SlotLease()
        replies = ["from-b",
                   urllib.error.HTTPError("https://upstream.invalid", 429, "busy", {},
                                          io.BytesIO(b"{}")),
                   "from-a"]

        def fake_urlopen(req, timeout=None, proxy=None):
            reply = replies.pop()
            if isinstance(reply, Exception):
                raise reply
            return reply

        old_pool, old_urlopen = proxy.POOL, accounts.urlopen
        old_switch = proxy.auto_switch_product_enabled
        proxy.POOL = pool
        accounts.urlopen = fake_urlopen
        proxy.auto_switch_product_enabled = lambda: False
        try:
            _resp, picked, _ = proxy.open_upstream(
                {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
                session_key="sess-follow", target_realm="cn", lease=lease)
            self.assertIs(picked, first)
            got, moved, _ = proxy.follow_up_with_tool_results(
                [], {"base_body": {"model": "glm-5.3"}, "base_messages": [],
                     "convo_messages": [], "realm": "cn"},
                "glm-5.3", "sess-follow", 0.0, lease)
        finally:
            proxy.POOL, accounts.urlopen = old_pool, old_urlopen
            proxy.auto_switch_product_enabled = old_switch
        self.assertEqual(got, "from-b")
        self.assertIs(moved, second)
        self.assertEqual(first.active_requests, 0, "换号后旧账号的名额要归还")
        self.assertEqual(second.active_for_model("glm-5.3"), 1)
        lease.release()
        self.assertEqual(second.active_requests, 0)

    def test_a_request_without_model_hands_its_slot_back(self):
        """不带 model 的请求，占名额与归还名额必须用同一个键。"""
        account = self.account()
        account.concurrency_limit = 1
        lease = proxy.SlotLease()
        old_urlopen = accounts.urlopen
        accounts.urlopen = lambda *a, **k: object()
        try:
            self.call(account, {"messages": [{"role": "user", "content": "hi"}]},
                      lease=lease)
        finally:
            accounts.urlopen = old_urlopen
        self.assertEqual(account.active_requests, 1)
        lease.release()
        self.assertEqual(account.active_requests, 0)

    def test_a_busy_bound_account_hands_the_conversation_over(self):
        """绑定账号名额满时换号接手，绑定跟着接手账号走。"""
        first = accounts.Account({"uid": "uid-a", "realm": "cn", "accessToken": "a"})
        second = accounts.Account({"uid": "uid-b", "realm": "cn", "accessToken": "b"})
        first.concurrency_limit = 1
        pool = accounts.AccountPool(tempfile.mkdtemp(prefix="cap-bound-"))
        pool.accounts = [first, second]
        pool.affinity.bind("sess", first.uid)
        self.assertTrue(first.acquire_request("glm-5.3"))
        self.assertIs(pool.pick_for_session(realm="cn", session_key="sess",
                                            model="glm-5.3"), second)
        self.assertEqual(pool.affinity.get("sess"), second.uid,
                         "接手账号重算了完整前缀，绑定要跟着它走")
        first.release_request("glm-5.3")
        self.assertIs(pool.pick_for_session(realm="cn", session_key="sess",
                                            model="glm-5.3"), second,
                      "原账号空出来也不回去，缓存已经不在它那里")

    def test_a_busy_debug_account_answers_429(self):
        """测试台固定账号名额满时报 429，不算请求本身有误。"""
        account = self.account()
        account.concurrency_limit = 1
        self.assertTrue(account.acquire_request("glm-5.3"))
        pool = accounts.AccountPool(tempfile.mkdtemp(prefix="cap-debug-"))
        pool.accounts = [account]
        self.assertEqual(pool.unavailable_reason(account.uid, realm="cn",
                                                 model="glm-5.3"), "")
        with self.assertRaises(proxy.RateLimited) as caught:
            self.call(account, {"model": "glm-5.3", "messages": [
                {"role": "user", "content": "hi"}]}, pool=pool, only_uid=account.uid)
        self.assertIn("concurrent request limit", caught.exception.message)

    def test_a_failed_attempt_hands_its_slot_back(self):
        """上游报错时名额要立刻归还，否则一次失败就会永久占住一个名额。"""
        account = self.account()
        account.concurrency_limit = 1
        error = urllib.error.HTTPError("https://upstream.invalid", 500, "boom", {},
                                       io.BytesIO(b""))

        old_pool, old_urlopen = proxy.POOL, accounts.urlopen
        proxy.POOL = stub_pool(account)
        accounts.urlopen = lambda *a, **k: (_ for _ in ()).throw(error)
        try:
            with self.assertRaises(Exception):
                proxy.open_upstream({"model": "glm-5.3", "messages": [
                    {"role": "user", "content": "hi"}]}, target_realm="cn",
                    lease=proxy.SlotLease())
        finally:
            proxy.POOL, accounts.urlopen = old_pool, old_urlopen
            error.close()
        self.assertEqual(account.active_for_model("glm-5.3"), 0,
                         "失败的尝试必须把名额还回来")
        self.assertTrue(account.has_request_capacity("glm-5.3"))


if __name__ == "__main__":
    unittest.main()
