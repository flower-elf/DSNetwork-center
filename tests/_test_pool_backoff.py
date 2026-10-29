import base64
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-pool-backoff-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts as A
import wb_proxy


INTL_ISS = "https://www.workbuddy.ai/auth/realms/copilot"

# 事件环境里的真实 429 报文，对应 issue #70，已脱敏：去掉 requestId，
# UID 与主机从未写入。记录中的 263 个 429 里有 229 个是 S1；S2 是唯一可证实
# 为 intl 且没有 reset 字段的报文；S3/S4 带 reset 时钟，作为对照。
S1_BARE = "usage exceeds frequency limit"
# 日志只保留 str(message)[:200]，所以 S2 只剩这段被截断的前缀；
# S2_INTL_JSON 是同一报文补全为合法 JSON 的形态，用于覆盖 code 数字路径。
S2_INTL_RECORDED = ('{"code":14003,"msg":"too many requests",'
                    '"retry_policy":{"retry":true},'
                    '"displayMsg":{"en":"Too many requests. Please retry later.",'
                    '"zh":"请求过于频繁，请稍后重试。","z')
S2_INTL_JSON = ('{"code":14003,"msg":"too many requests",'
                '"retry_policy":{"retry":true},'
                '"displayMsg":{"en":"Too many requests. Please retry later.",'
                '"zh":"请求过于频繁，请稍后重试。"}}')
# 网关最多读取 600 个字符，而用量日志只留 200 个，所以真实报文比记录的
# 前缀更长。下面的结尾是这种结尾可能的形态；`accountId` / `credentialId`
# 只是字段名。
S2_INTL_FULL = ('{"code":14003,"msg":"too many requests",'
                '"accountId":"<redacted>","credentialId":"<redacted>",'
                '"requestId":"<redacted>","retry_policy":{"retry":true},'
                '"displayMsg":{"en":"Too many requests. Please retry later.",'
                '"zh":"请求过于频繁，请稍后重试。"},"ts":1790500000}')
S3_INTL_6004 = ('{"code":6004,"msg":"usage exceeds frequency limit, but don\'t '
                'worry, your usage will reset at 2026-10-06 21:00:09 UTC+8, '
                'alternatively, you can switch to the other models to continue '
                'using it."}')
S4_CN_6004 = ('{"code":6004,"msg":"您的使用量已超出频率限制，'
              '将在 2026-10-06 14:45:04 UTC+8 重置，'
              '您也可以切换其他模型继续使用。"}')


def jwt(iss=INTL_ISS, sub="u-1"):
    def part(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
        return raw.rstrip("=")
    return "%s.%s.sig" % (part({"alg": "RS256", "typ": "JWT"}),
                          part({"iss": iss, "sub": sub, "exp": 4102444800}))


def load_account(uid="u-backoff-1"):
    directory = os.path.join(_TMP, uid)
    os.makedirs(directory, exist_ok=True)
    data = {"uid": uid, "domain": "www.workbuddy.ai", "realm": "intl",
            "accessToken": jwt()}
    with open(os.path.join(directory, uid + ".json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    accounts = A.AccountPool(directory).load()
    assert len(accounts) == 1
    return accounts[0]


class BackoffMathTests(unittest.TestCase):
    def test_soft_backoff_doubles_and_caps(self):
        self.assertEqual(A.soft_backoff(1), A.SOFT_RATE_BASE)
        self.assertEqual(A.soft_backoff(2), A.SOFT_RATE_BASE * 2)
        self.assertEqual(A.soft_backoff(4), A.SOFT_RATE_BASE * 8)
        self.assertEqual(A.soft_backoff(50), A.SOFT_RATE_MAX)

    def test_breaker_starts_at_the_threshold_and_caps(self):
        self.assertEqual(A.breaker_backoff(A.BREAKER_THRESHOLD), A.BREAKER_COOLDOWN)
        self.assertEqual(A.breaker_backoff(A.BREAKER_THRESHOLD + 1),
                         A.BREAKER_COOLDOWN * 2)
        self.assertEqual(A.breaker_backoff(99), A.BREAKER_COOLDOWN_MAX)

    def test_degrade_starts_at_the_threshold_and_caps(self):
        self.assertEqual(A.degrade_backoff(A.DEGRADE_THRESHOLD), A.DEGRADE_COOLDOWN)
        self.assertEqual(A.degrade_backoff(A.DEGRADE_THRESHOLD + 2),
                         A.DEGRADE_COOLDOWN * 4)
        self.assertEqual(A.degrade_backoff(99), A.DEGRADE_COOLDOWN_MAX)

    def test_the_real_unscoped_shapes_are_recognised(self):
        """S1 占记录 429 的 87%，intl 的 14003 报文也要识别。"""
        self.assertTrue(wb_proxy.rate_limit_is_soft_shape(S1_BARE))
        self.assertTrue(wb_proxy.rate_limit_is_soft_shape(S2_INTL_JSON))
        # 记录形态在 JSON 中途被截断，仍按文本匹配。
        self.assertTrue(wb_proxy.rate_limit_is_soft_shape(S2_INTL_RECORDED))
        self.assertEqual(wb_proxy.rate_limit_code(S2_INTL_JSON), 14003)
        self.assertIsNone(wb_proxy.rate_limit_code(S1_BARE), "S1 is not JSON")
        # 无关的 429 报文不能被认作限流形态。
        self.assertFalse(wb_proxy.rate_limit_is_soft_shape("<html>bad gateway</html>"))
        self.assertFalse(wb_proxy.rate_limit_is_soft_shape(""))

    def test_none_of_the_real_shapes_is_account_level_without_evidence(self):
        """S1/S2 没有时间戳，S3/S4 是能解析出 reset 的对照。

        无可解析时间戳时，账号级判定必须为 False：否则几个 S1/S2 就能让
        整个 realm 冷却，而面板测试仍报告凭证健康。
        """
        for detail in (S1_BARE, S2_INTL_JSON, S2_INTL_RECORDED):
            self.assertIsNone(wb_proxy.parse_rate_limit_reset(detail), detail)
            self.assertFalse(wb_proxy.rate_limit_is_account_level(detail, None), detail)
        # S3/S4 精确解析到文本里写明的时刻。
        self.assertEqual(wb_proxy.parse_rate_limit_reset(S3_INTL_6004), 1791291609)
        self.assertEqual(wb_proxy.parse_rate_limit_reset(S4_CN_6004), 1791269104)
        for detail in (S3_INTL_6004, S4_CN_6004):
            reset_at = wb_proxy.parse_rate_limit_reset(detail)
            self.assertFalse(wb_proxy.rate_limit_is_account_level(detail, reset_at), detail)
        # 完全无法读取的报文同样不算。
        self.assertFalse(wb_proxy.rate_limit_is_account_level("", None))

    def test_metadata_field_names_are_not_scope_evidence(self):
        """`accountId` 只是字段名，不能说明账号被限流。

        网关最多读取 429 报文的 600 个字符，字段名出现在元数据尾部；按子串
        判定会让最常见的 intl 形态重新走上 600s 到 7200s 的阶梯。
        """
        self.assertTrue(wb_proxy.rate_limit_is_soft_shape(S2_INTL_FULL))
        self.assertIsNone(wb_proxy.parse_rate_limit_reset(S2_INTL_FULL))
        self.assertIsNone(wb_proxy.credential_scope_phrase(S2_INTL_FULL))
        self.assertFalse(wb_proxy.rate_limit_is_account_level(S2_INTL_FULL, None))
        for metadata in ('{"accountId":"x"}', '{"account_id":"x"}',
                         '{"accountName":"x"}', '{"credentialId":"x"}',
                         '{"subaccount":"x"}', '{"accountIds":["x"]}'):
            self.assertFalse(wb_proxy.rate_limit_is_account_level(metadata, None),
                             metadata)
        # 作用域必须写明，不能由字段名暗示。
        self.assertTrue(wb_proxy.rate_limit_is_account_level(
            '{"msg":"account-level rate limit reached"}', None))
        self.assertTrue(wb_proxy.rate_limit_is_account_level(
            '{"msg":"too many requests for this account"}', None))
        self.assertTrue(wb_proxy.rate_limit_is_account_level(
            '{"msg":"该账号请求过于频繁"}', None))
        self.assertEqual(wb_proxy.credential_scope_phrase(
            '{"msg":"This Account is rate limited"}'), "this account")
        self.assertIsNone(wb_proxy.credential_scope_phrase(S1_BARE))

    def test_the_two_ladders_do_not_feed_each_other(self):
        """两条阶梯各自计数，互不抬高对方的上限。

        4 次无作用域 429 不会让 soft_streak 变成 4，首次真正的凭证级 429
        仍从 600s 起步；反向也一样，凭证级失败不会抬高无作用域阶梯。
        """
        account = load_account("u-domains")
        for _ in range(4):
            account.note_unscoped_rate("glm-5.3")
        self.assertEqual(account.unscoped_streak, 4)
        self.assertEqual(account.soft_streak, 0)
        self.assertEqual(account.note_soft_rate("HTTP 429 (account soft rate)"),
                         A.SOFT_RATE_BASE,
                         "the credential ladder starts at its own base")

        other = load_account("u-domains-reverse")
        other.note_soft_rate("HTTP 429 (account soft rate)")
        other.note_soft_rate("HTTP 429 (account soft rate)")
        self.assertEqual(other.soft_streak, 2)
        self.assertEqual(other.unscoped_streak, 0)
        self.assertEqual(other.note_unscoped_rate("glm-5.3"),
                         A.SOFT_RATE_UNVERIFIED_BASE,
                         "the unscoped ladder starts at its own base")

    def test_a_served_request_clears_both_streaks(self):
        account = load_account("u-both-streaks")
        account.note_soft_rate("HTTP 429 (account soft rate)")
        account.note_unscoped_rate("glm-5.3")
        self.assertEqual((account.soft_streak, account.unscoped_streak), (1, 1))
        account.note_success(model="glm-5.3")
        self.assertEqual(account.soft_streak, 0)
        self.assertEqual(account.unscoped_streak, 0)
        self.assertEqual(account.public()["unscopedStreak"], 0)

    def test_the_unscoped_ladder_starts_short_and_caps_at_one_credential_tier(self):
        """S1 是最常见的形态，其上限不能是 2 小时那一档。"""
        self.assertEqual(A.unverified_soft_backoff(1), 60.0)
        self.assertEqual(A.unverified_soft_backoff(2), 120.0)
        self.assertEqual(A.unverified_soft_backoff(4), 480.0)
        self.assertEqual(A.unverified_soft_backoff(5), 600.0)
        self.assertEqual(A.unverified_soft_backoff(50), A.SOFT_RATE_UNVERIFIED_MAX)
        self.assertEqual(A.SOFT_RATE_UNVERIFIED_MAX, A.SOFT_RATE_BASE)
        self.assertLess(A.SOFT_RATE_UNVERIFIED_MAX, A.SOFT_RATE_MAX)

    def test_an_unscoped_429_parks_the_model_not_the_credential(self):
        account = load_account("u-unscoped")
        first = account.note_unscoped_rate("glm-5.3")
        self.assertEqual(first, 60.0)
        account.note_error("HTTP 429 (model throttled)", model="glm-5.3",
                           cooldown=first)
        second = account.note_unscoped_rate("glm-5.3")
        self.assertEqual(second, 120.0)
        account.note_error("HTTP 429 (model throttled)", model="glm-5.3",
                           cooldown=second)
        self.assertEqual(account.unscoped_streak, 2)
        self.assertEqual(account.soft_streak, 0, "the credential counter is untouched")
        # 凭证本身不被停用，同账号的其他模型仍可用，这是与 note_soft_rate
        # 的区别。
        self.assertEqual(account.cooldown_until, 0.0)
        self.assertEqual(account.throttle_wait(), 0)
        self.assertTrue(account.ready(model="another-model"))
        self.assertFalse(account.ready(model="glm-5.3"))

    def test_only_credential_wording_is_account_level(self):
        """正向证据仍能触发 10m 到 2h 的阶梯。

        措辞列表有意保持简短：目前还没有抓到真正的账号级 429 报文，无法
        确定它会怎么说。
        """
        self.assertTrue(wb_proxy.rate_limit_is_account_level(
            '{"message":"too many requests for this account"}', None))
        self.assertTrue(wb_proxy.rate_limit_is_account_level(
            '{"msg":"该账号请求过于频繁"}', None))
        # reset 时钟优先，这属于模型级形态。
        self.assertFalse(wb_proxy.rate_limit_is_account_level(
            '{"message":"account rate limit, reset at 2026-10-09 12:00:00 UTC"}',
            time.time() + 60))

    def test_a_genuine_account_level_429_still_backs_off(self):
        account = load_account("u-account-level")
        self.assertTrue(wb_proxy.rate_limit_is_account_level(
            '{"message":"too many requests for this account"}', None))
        self.assertEqual(account.note_soft_rate("HTTP 429 (account soft rate)"),
                         A.SOFT_RATE_BASE)
        self.assertEqual(account.note_soft_rate("HTTP 429 (account soft rate)"),
                         A.SOFT_RATE_BASE * 2)
        self.assertEqual(account.soft_streak, 2)

    def test_reset_clock_is_parsed_for_both_realm_wordings(self):
        cn = ('{"code":6004,"msg":"您的使用量已超出频率限制，'
              '将在 2026-10-09 14:44:59 UTC+8 重置。"}')
        intl = ('{"code":6004,"message":"your usage will reset at '
                '2026-10-09 14:44:59 UTC+8"}')
        cn_reset = wb_proxy.parse_rate_limit_reset(cn)
        self.assertEqual(cn_reset, wb_proxy.parse_rate_limit_reset(intl))
        self.assertIsNotNone(cn_reset)
        # 14:44:59 UTC+8 与 06:44:59 UTC 是同一墙钟时刻。
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(cn_reset)),
                         "2026-10-09 06:44:59")
        self.assertIsNone(wb_proxy.parse_rate_limit_reset('{"code":429}'))


class AccountGovernanceTests(unittest.TestCase):
    def test_soft_rate_backs_off_and_counts_the_streak(self):
        account = load_account("u-soft")
        first = account.note_soft_rate("HTTP 429 (account soft rate)")
        second = account.note_soft_rate("HTTP 429 (account soft rate)")
        self.assertEqual(account.soft_streak, 2)
        self.assertEqual(first, A.SOFT_RATE_BASE)
        self.assertEqual(second, A.SOFT_RATE_BASE * 2)
        self.assertGreater(account.throttle_wait(), 0)
        view = account.public()
        self.assertEqual(view["softStreak"], 2)
        self.assertTrue(view["inCooldown"])

    def test_breaker_needs_three_hard_failures(self):
        account = load_account("u-breaker")
        account.note_failure("HTTP 502")
        account.note_failure("HTTP 502")
        self.assertEqual(account.throttle_wait(), 0)
        account.note_failure("HTTP 503")
        self.assertGreater(account.breaker_until, 0)
        self.assertGreater(account.throttle_wait(), 0)
        self.assertTrue(account.public()["breakerFor"])

    def test_unknown_failures_degrade_after_the_threshold(self):
        account = load_account("u-degrade")
        account.note_unknown_failure("connection: TimeoutError")
        account.note_unknown_failure("connection: TimeoutError")
        self.assertEqual(account.throttle_wait(), 0)
        # 未知失败也计入共享 breaker 计数，所以循环越过两个阈值再断言；
        # degrade 窗口本身从 DEGRADE_THRESHOLD 开始。
        for _ in range(A.DEGRADE_THRESHOLD - 2):
            account.note_unknown_failure("connection: TimeoutError")
        self.assertEqual(account.degrade_count, A.DEGRADE_THRESHOLD)
        self.assertGreater(account.degrade_until, 0)
        self.assertGreater(account.throttle_wait(), 0)
        self.assertTrue(account.public()["degradeFor"])

    def test_success_clears_every_penalty(self):
        account = load_account("u-success")
        account.note_soft_rate("HTTP 429 (account soft rate)")
        for _ in range(A.BREAKER_THRESHOLD):
            account.note_failure("HTTP 502")
        account.note_unknown_failure("connection: TimeoutError")
        account.note_success(model="deepseek-v4.1-flash")
        self.assertEqual(account.throttle_wait(), 0)
        self.assertEqual(account.soft_streak, 0)
        self.assertEqual(account.fails, 0)
        self.assertEqual(account.degrade_count, 0)
        self.assertEqual(account.breaker_until, 0.0)
        self.assertEqual(account.degrade_until, 0.0)
        self.assertEqual(account.public()["lastError"], "")


class PanelTestSuccessTests(unittest.TestCase):
    """面板测试成功等于真正服务过一次请求，必须清零 soft_streak。

    /accounts/test 成功时调用 clear_error，只清可见的冷却字段，不清
    soft_streak；计数由 note_success 重置。否则下一次账号级 429 会从残留
    的 streak 继续退避阶梯。
    """

    class _Route(object):
        """供真实 _route_accounts_test 使用的极小 handler。"""

        def _json(self, code, obj):
            self.code, self.body = code, obj
            return code, obj

    class _Response(object):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _drive_panel_test(self, account):
        """用打桩的上游应答跑真实路由。"""
        class Pool(object):
            def get(self, uid):
                return account if uid == account.uid else None

        route = self._Route()
        old = (wb_proxy.POOL, A.urlopen, wb_proxy.aggregate_stream)
        wb_proxy.POOL = Pool()
        A.urlopen = lambda *args, **kwargs: self._Response()
        wb_proxy.aggregate_stream = lambda resp, model, key: {
            "choices": [{"message": {"content": "OK"}}]}
        try:
            return wb_proxy.Handler._route_accounts_test(route, {"uid": account.uid})
        finally:
            wb_proxy.POOL, A.urlopen, wb_proxy.aggregate_stream = old

    def test_a_successful_panel_test_clears_the_streak(self):
        account = load_account("u-panel-test")
        account.note_soft_rate("HTTP 429 (account soft rate)")
        account.note_soft_rate("HTTP 429 (account soft rate)")
        self.assertEqual(account.soft_streak, 2)

        code, body = self._drive_panel_test(account)

        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(account.soft_streak, 0)
        self.assertEqual(account.throttle_wait(), 0)
        self.assertEqual(account.public()["softStreak"], 0)

    def test_a_stale_streak_does_not_resume_on_the_next_event(self):
        account = load_account("u-panel-test-stale")
        for _ in range(3):
            account.note_soft_rate("HTTP 429 (account soft rate)")
        self.assertEqual(account.soft_streak, 3)

        self._drive_panel_test(account)

        # 下一次账号级 429 从头开始，不接续面板测试本应清零的 streak。
        wait = account.note_soft_rate("HTTP 429 (account soft rate)")
        self.assertEqual(account.soft_streak, 1)
        self.assertEqual(wait, A.SOFT_RATE_BASE)

    def test_a_panel_test_keeps_other_models_throttled(self):
        """这条窄成功路径不能解除同账号其他模型的冷却。"""
        account = load_account("u-panel-test-siblings")
        account.note_error("HTTP 429 (model throttled)", model="glm-5.3",
                           cooldown=600)

        self._drive_panel_test(account)

        self.assertFalse(account.ready(model="glm-5.3"))
        self.assertIn("glm-5.3", [item["model"]
                                  for item in account.public()["modelCooldowns"]])


class RetryRoundChargeTests(unittest.TestCase):
    """一次客户端请求不能对同一账号记两次失败，对应 issue #271。

    出现瞬时故障后重试轮会清空 `tried` 并重扫整个池。只剩一个可用账号时，
    重扫会把同一个账号再交回来，若无按请求去重就会在一条请求里记两次失败，
    两次抖动即可触发 3 次失败阈值。池接近空正是事故环境的场景。
    """

    def _drive(self, account, urlopen):
        class Pool(object):
            accounts = [account]
            affinity = type("Affinity", (), {
                "unbind": lambda self, key: None,
                "demote": lambda self, key, uid=None: None})()

            def count_ready(self, realm, model=None):
                return 1

            def pick_for_session(self, realm, session_key=None, exclude=(),
                                 model=None, page=None, claim=None):
                return None if account.uid in exclude else account

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

        old_pool, old_urlopen = wb_proxy.POOL, A.urlopen
        wb_proxy.POOL = Pool()
        A.urlopen = urlopen
        try:
            with mock.patch.object(wb_proxy, "ACCOUNTS_DIR",
                                   os.path.join(_TMP, account.uid)):
                try:
                    wb_proxy.open_upstream(
                        {"model": "deepseek-v4.1-flash",
                         "messages": [{"role": "user", "content": "hi"}]},
                        target_realm="intl")
                except Exception:
                    pass
        finally:
            wb_proxy.POOL, A.urlopen = old_pool, old_urlopen

    def test_a_transport_blip_charges_the_account_once(self):
        account = load_account("u-charge-transient")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.URLError("The handshake operation timed out")

        with mock.patch.object(time, "sleep"):
            self._drive(account, urlopen)
        # 重试轮重新选中了该账号，仍只能记一次。
        self.assertEqual(account.fails, 1)
        self.assertEqual(account.degrade_count, 1)

    def test_a_gateway_5xx_charges_the_account_once(self):
        account = load_account("u-charge-5xx")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)

        with mock.patch.object(time, "sleep"):
            self._drive(account, urlopen)
        self.assertEqual(account.fails, 1)

    def test_the_guard_is_per_request_not_per_process(self):
        # 第二条请求可以再记一次：守卫不能跨请求泄漏，否则会掩盖真正故障
        # 的账号。
        account = load_account("u-charge-two-requests")

        def urlopen(req, timeout=30, proxy=""):
            raise urllib.error.URLError("connection reset by peer")

        with mock.patch.object(time, "sleep"):
            self._drive(account, urlopen)
            self._drive(account, urlopen)
        self.assertEqual(account.fails, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
