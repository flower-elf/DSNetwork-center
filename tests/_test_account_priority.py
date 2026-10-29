"""Account call priority 决定选号顺序：数字小的先试，数字相同按原有轮转顺序；不联网，账号用普通令牌由字典构造，只写临时目录。
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-priority-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_proxy as P

PRIORITY_ERROR = "priority must be a whole number between 0 and 9999"


def account(uid, realm="intl", **fields):
    """纯文本令牌让 jwt_exp() 保持 0。"""
    data = {"uid": uid, "accessToken": "t", "realm": realm}
    data.update(fields)
    return wb_accounts.Account(data)


def pool_with(*accounts):
    """由这些账号组成的池子，不依附任何账号目录。"""
    pool = wb_accounts.AccountPool(os.path.join(_TMP, "pools"))
    pool.accounts = list(accounts)
    return pool


def picked_uids(pool, count, **kwargs):
    return [pool.pick(**kwargs).uid for _ in range(count)]


class PriorityStorageTests(unittest.TestCase):
    def test_missing_or_unusable_priority_reads_as_the_default(self):
        self.assertEqual(wb_accounts.DEFAULT_ACCOUNT_PRIORITY, 100)
        cases = [
            ("no field at all", {}),
            ("null", {"priority": None}),
            ("bool", {"priority": True}),
            ("non-numeric string", {"priority": "abc"}),
        ]
        for label, fields in cases:
            with self.subTest(case=label):
                got = account("uid-a", **fields).priority
                self.assertEqual(got, 100, "%s normalised to %r" % (label, got))
        # 数字字符串按数字解析：手改文件里的 "50" 就是 50。
        self.assertEqual(account("uid-b", priority="50").priority, 50)

    def test_out_of_range_values_are_clamped(self):
        for value, want in ((-5, 0), (0, 0), (9999, 9999), (10000, 9999)):
            with self.subTest(value=value):
                got = account("uid-a", priority=value).priority
                self.assertEqual(got, want,
                                 "priority %r stored as %r, wanted %r" % (value, got, want))

    def test_priority_survives_a_round_trip(self):
        original = account("uid-a", priority=7)
        with tempfile.TemporaryDirectory(prefix="wb-priority-") as directory:
            path = original.save(directory)
            self.assertEqual(os.path.basename(path), "uid-a.json")
            with open(path, encoding="utf-8") as fh:
                on_disk = json.load(fh)
            self.assertEqual(on_disk.get("priority"), 7)
            with open(path, encoding="utf-8") as fh:
                reloaded = wb_accounts.Account(json.load(fh))
        self.assertEqual(reloaded.priority, 7)


class PickOrderTests(unittest.TestCase):
    """数字小的优先；数字相同时保持轮询顺序。"""

    def test_the_lowest_number_takes_every_pick_while_it_can_serve(self):
        pool = pool_with(account("uid-200", priority=200),
                         account("uid-50", priority=50),
                         account("uid-100", priority=100))
        # 优先级是严格顺序：列表顺序不影响选号，最优账号持续服务直到失败。
        self.assertEqual(picked_uids(pool, 3, realm="intl"),
                         ["uid-50", "uid-50", "uid-50"])

    def test_the_remaining_numbers_decide_the_fallback_order(self):
        pool = pool_with(account("uid-200", priority=200),
                         account("uid-50", priority=50),
                         account("uid-100", priority=100))
        # open_upstream 重试时把失败账号加入 exclude，由数字决定下一个。
        self.assertEqual(pool.pick(realm="intl", exclude={"uid-50"}).uid,
                         "uid-100")
        self.assertEqual(pool.pick(realm="intl",
                                   exclude={"uid-50", "uid-100"}).uid,
                         "uid-200")

    def test_ties_keep_the_pre_upgrade_rotation(self):
        pool = pool_with(account("uid-a"), account("uid-b"), account("uid-c"))
        order = [a.uid for a in pool.accounts]
        self.assertEqual(order, ["uid-a", "uid-b", "uid-c"])
        picked = picked_uids(pool, 3, realm="intl")
        self.assertEqual(picked, order,
                         "three equal-priority picks were %r, wanted each uid once "
                         "in account order" % (picked,))
        self.assertEqual(pool._cursor, 0,
                         "cursor did not wrap after three picks: %r" % (pool._cursor,))
        self.assertEqual(pool.pick(realm="intl").uid, "uid-a")


class SkipUnavailableTests(unittest.TestCase):
    def test_a_cooling_account_is_skipped(self):
        cooling = account("uid-cooling", priority=50)
        cooling.note_error("boom", cooldown=60)
        self.assertFalse(cooling.ready(),
                         "cooling account still reports ready (until %r)"
                         % (cooling.cooldown_until,))
        pool = pool_with(cooling, account("uid-mid", priority=100),
                         account("uid-last", priority=200))
        self.assertEqual(pool.pick(realm="intl").uid, "uid-mid",
                         "the pool did not fall through to the next priority")

    def test_a_disabled_account_is_skipped(self):
        disabled = account("uid-disabled", priority=50)
        disabled.enabled = False
        self.assertFalse(disabled.ready(), "disabled account still reports ready")
        pool = pool_with(disabled, account("uid-mid", priority=100))
        self.assertEqual(pool.pick(realm="intl").uid, "uid-mid")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-priority-set-")
        self.directory = self._tmp.name
        account("uid-a").save(self.directory)
        self.pool = wb_accounts.AccountPool(self.directory)
        self.pool.load()
        self.assertEqual([a.uid for a in self.pool.accounts], ["uid-a"])

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_new_priority_is_written_to_disk(self):
        updated = self.pool.set_priority("uid-a", 7)
        self.assertEqual(updated["priority"], 7)
        self.assertIsInstance(updated["priority"], int)
        path = self.pool.get("uid-a").path
        self.assertEqual(os.path.basename(path), "uid-a.json")
        with open(path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk.get("priority"), 7)
        with open(path, encoding="utf-8") as fh:
            reloaded = wb_accounts.Account(json.load(fh))
        self.assertEqual(reloaded.priority, 7)

    def test_an_unknown_uid_returns_none(self):
        self.assertIsNone(self.pool.set_priority("uid-missing", 7))

    def test_the_setter_clamps_like_the_loader(self):
        self.assertEqual(self.pool.set_priority("uid-a", -5)["priority"], 0)
        self.assertEqual(self.pool.set_priority("uid-a", 10000)["priority"], 9999)
        self.assertEqual(self.pool.set_priority("uid-a", "abc")["priority"], 100)


class SessionAffinityTests(unittest.TestCase):
    """会话绑定的账号只要还能服务，就优先于优先级数字更好的账号。"""

    def setUp(self):
        self.small = account("uid-small", priority=50)
        self.big = account("uid-big", priority=100)
        self.pool = pool_with(self.small, self.big)

    def _bind_to_big(self):
        # 跳过数字更小的账号一次，首轮就把会话绑到数字较大的账号。
        bound = self.pool.pick_for_session(realm="intl", session_key="s1",
                                           exclude={"uid-small"})
        self.assertEqual(bound.uid, "uid-big")

    def test_the_bound_session_keeps_its_account(self):
        self._bind_to_big()
        # 更优账号空闲时普通 pick 会选它，但会话仍留在绑定的账号上。
        self.assertEqual(self.pool.pick(realm="intl").uid, "uid-small")
        again = self.pool.pick_for_session(realm="intl", session_key="s1")
        self.assertEqual(again.uid, "uid-big")

    def test_a_disabled_binding_falls_back_to_the_next_account(self):
        self._bind_to_big()
        self.big.enabled = False
        fallback = self.pool.pick_for_session(realm="intl", session_key="s1")
        self.assertEqual(fallback.uid, "uid-small")


class UnavailableReasonTests(unittest.TestCase):
    """拒绝固定账号时说的原因，必须指出是这个账号的哪一条限制。"""

    def test_a_usable_account_has_no_reason(self):
        pool = pool_with(account("uid-ok"))
        self.assertEqual(pool.unavailable_reason("uid-ok", realm="intl", model="m"), "")

    def test_each_restriction_names_itself(self):
        cooling = account("uid-cooling")
        cooling.note_error("boom", cooldown=60)
        # 保留积分与日限额平时由 apply_* 从设置下推，这里直接写成账号字段。
        reserved = account("uid-reserve")
        reserved.reserve_credits = 100
        reserved.credits = {"remain": 50, "size": 200}
        daily = account("uid-daily")
        daily.daily_token_limit = 10
        daily.daily_tokens_today = 10
        pool = pool_with(
            cooling, reserved, daily,
            account("uid-off", enabled=False),
            account("uid-bare", accessToken=""),
            account("uid-expired", accessToken="x", expiresAt=time.time() - 10),
            account("uid-cn", realm="cn"),
        )
        cases = [
            ("uid-missing", "not in the account pool"),
            ("uid-cn", "the other realm"),
            ("uid-off", "disabled"),
            ("uid-bare", "no credential"),
            ("uid-cooling", "cooling down"),
            ("uid-reserve", "reserved credits"),
            ("uid-daily", "today's token limit"),
            ("uid-expired", "token expired"),
        ]
        for uid, want in cases:
            with self.subTest(uid=uid):
                reason = pool.unavailable_reason(uid, realm="intl", model="m")
                self.assertIn(want, reason)
        # 模型级冷却只挡那一个模型，其它模型照常可服务
        model_cooled = account("uid-model")
        model_cooled.note_error("throttled", model="m", cooldown=60)
        pool = pool_with(model_cooled)
        self.assertEqual(pool.unavailable_reason("uid-model", realm="intl", model="other"), "")
        self.assertIn("cooling down",
                      pool.unavailable_reason("uid-model", realm="intl", model="m"))


class DebugAccountPinTests(unittest.TestCase):
    """只用一个账号发请求：不换号、不参与会话粘性。"""

    def setUp(self):
        self.pin = account("uid-pin")
        self.other = account("uid-other")
        self.pool = pool_with(self.pin, self.other)
        self._pool = P.POOL
        P.POOL = self.pool
        self.calls = []
        self._urlopen = wb_accounts.urlopen

        def fake_urlopen(req, timeout=None, proxy=None):
            self.calls.append(req)
            return "response"

        wb_accounts.urlopen = fake_urlopen

    def tearDown(self):
        wb_accounts.urlopen = self._urlopen
        P.POOL = self._pool

    def open_pinned(self, uid, model="glm-5.3"):
        payload = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
        return P.open_upstream(payload, target_realm="intl", only_uid=uid,
                               lease=P.SlotLease())

    def test_the_named_account_serves_the_request(self):
        response, used, _ = self.open_pinned("uid-other")
        self.assertEqual(response, "response")
        self.assertEqual(used.uid, "uid-other")
        self.assertEqual(len(self.calls), 1)

    def test_a_blocked_pin_fails_instead_of_falling_back(self):
        self.pin.note_error("boom", cooldown=60)
        with self.assertRaises(RuntimeError) as caught:
            self.open_pinned("uid-pin")
        self.assertTrue(str(caught.exception).startswith("no usable account"),
                        str(caught.exception))
        self.assertEqual(self.calls, [], "冷却中的账号仍然发出了请求")

    def test_a_bound_session_does_not_steal_the_request(self):
        bound = self.pool.pick_for_session(realm="intl", session_key="s1")
        self.assertEqual(bound.uid, "uid-pin")
        _, used, _ = self.open_pinned("uid-other")
        self.assertEqual(used.uid, "uid-other", "会话粘性把请求抢回绑定的账号")
        self.assertEqual(self.pool.affinity.get("s1"), "uid-pin")

    def test_a_failure_leaves_the_session_binding_alone(self):
        self.pool.pick_for_session(realm="intl", session_key="s1")
        self.other.note_error("boom", cooldown=60)
        with self.assertRaises(RuntimeError):
            self.open_pinned("uid-other")
        self.assertEqual(self.pool.affinity.get("s1"), "uid-pin",
                         "固定账号失败后，会话绑定被清掉了")


class DebugAccountHeaderTests(unittest.TestCase):
    """X-Debug-Account 只认面板会话，客户端 Key 带它一律忽略。"""

    def handler(self, panel, uid):
        class Fake(object):
            headers = {"X-Debug-Account": uid} if uid is not None else {}

            def _panel_ok(self):
                return panel

        Fake._debug_account = P.Handler._debug_account
        return Fake()

    def test_a_panel_session_can_pin(self):
        self.assertEqual(self.handler(True, "uid-a")._debug_account(), "uid-a")
        self.assertEqual(self.handler(True, "  uid-a  ")._debug_account(), "uid-a")

    def test_a_client_key_is_ignored(self):
        self.assertEqual(self.handler(False, "uid-a")._debug_account(), "")

    def test_no_header_means_the_pool_decides(self):
        self.assertEqual(self.handler(True, "")._debug_account(), "")
        self.assertEqual(self.handler(True, None)._debug_account(), "")


class ParsePriorityTests(unittest.TestCase):
    """/accounts/set 只收 0..9999 的整数。"""

    def test_whole_numbers_in_range_pass(self):
        for value in (0, 100, 9999):
            with self.subTest(value=value):
                self.assertEqual(wb_accounts.parse_account_priority(value), (value, ""))

    def test_everything_else_is_rejected_with_the_same_message(self):
        for value in (True, False, "50", 1.5, -1, 10000, None):
            with self.subTest(value=value):
                got, problem = wb_accounts.parse_account_priority(value)
                self.assertIsNone(got)
                self.assertEqual(problem, PRIORITY_ERROR,
                                 "value %r reported %r" % (value, problem))


class ParseConcurrencyTests(unittest.TestCase):
    """/accounts/set 收 0 到 1000 的整数；0 表示不限。"""

    def test_whole_numbers_in_range_pass(self):
        for value in (0, 1, 1000, "12"):
            with self.subTest(value=value):
                self.assertEqual(wb_accounts.normalise_concurrency_limit(value),
                                 (int(value), ""))

    def test_everything_else_is_rejected_with_the_same_message(self):
        expected = ("concurrencyLimit must be between 0 and %d"
                    % wb_accounts.MAX_ACCOUNT_CONCURRENCY)
        for value in (-1, 1001):
            with self.subTest(value=value):
                got, problem = wb_accounts.normalise_concurrency_limit(value)
                self.assertIsNone(got)
                self.assertEqual(problem, expected)
        # 非整数与布尔值一律拒绝，错误文案与优先级同口径。
        for value in (True, False, 1.5, None, "abc"):
            with self.subTest(value=value):
                got, problem = wb_accounts.normalise_concurrency_limit(value)
                self.assertIsNone(got)
                self.assertEqual(
                    problem, "concurrencyLimit must be a non-negative whole number")

    def test_the_route_stores_it_and_lists_it_back(self):
        pool = pool_with(account("uid-cap"))
        row = pool.set_concurrency_limit("uid-cap", 3)
        self.assertEqual(row["concurrencyLimit"], 3)
        self.assertEqual(pool.get("uid-cap").public()["concurrencyLimit"], 3)
        self.assertEqual(pool.get("uid-cap").public()["activeRequests"], 0,
                         "面板要能读到当前在途请求数（这里还没有请求）")
        self.assertIsNone(pool.set_concurrency_limit("nobody", 3),
                          "没有这个账号时要返回 None，路由据此回 404")


class ParseAccountSetTests(unittest.TestCase):
    """/accounts/set 先校验全部字段，再写入任何一项。"""

    def test_invalid_later_field_rejects_the_whole_payload(self):
        changes, problem = wb_accounts.parse_account_set({"priority": 5, "weight": 999})
        self.assertIsNone(changes)
        self.assertEqual(problem, "weight out of range (1-100)")

    def test_valid_fields_are_normalised(self):
        changes, problem = wb_accounts.parse_account_set({
            "weight": "5", "priority": 7, "concurrencyLimit": "12",
            "enabled": 1,
        })
        self.assertEqual(problem, "")
        self.assertEqual(changes, {"weight": 5, "priority": 7,
                                   "concurrencyLimit": 12, "enabled": True})

    def test_empty_payload_names_the_fields(self):
        changes, problem = wb_accounts.parse_account_set({})
        self.assertIsNone(changes)
        self.assertIn("nothing to update", problem)

    def test_published_limits_match_server_constants(self):
        limits = wb_accounts.account_limits()
        self.assertEqual(limits["priorityMax"], wb_accounts.MAX_ACCOUNT_PRIORITY)
        self.assertEqual(limits["concurrencyMax"],
                         wb_accounts.MAX_ACCOUNT_CONCURRENCY)
        self.assertEqual(limits["weightMax"], wb_accounts.MAX_ACCOUNT_WEIGHT)
        self.assertEqual(limits["noteMax"], wb_accounts.MAX_ACCOUNT_NOTE)
        self.assertEqual(limits["apiKeyPrefixes"],
                         list(P.wb_identity.API_KEY_PREFIXES))


class SetRouteAtomicTests(unittest.TestCase):
    """/accounts/set 先校验全部字段，再写入任何一项。"""

    class _Handler(object):
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

    def setUp(self):
        self._original_pool = P.POOL

    def tearDown(self):
        P.POOL = self._original_pool

    def test_an_invalid_later_field_writes_nothing(self):
        target = account("uid-set", priority=100)
        P.POOL = pool_with(target)
        handler = self._Handler()
        P.Handler._route_accounts_set(handler, {
            "uid": "uid-set", "priority": 5, "weight": 999,
        })
        self.assertEqual(handler.code, 400)
        self.assertEqual(P.POOL.get("uid-set").priority, 100,
                         "校验失败时前面的 priority 不得写入")

    def test_valid_fields_are_applied_together(self):
        target = account("uid-set", priority=100)
        P.POOL = pool_with(target)
        handler = self._Handler()
        P.Handler._route_accounts_set(handler, {
            "uid": "uid-set", "priority": 5, "weight": 2,
        })
        self.assertEqual(handler.code, 200)
        self.assertEqual(P.POOL.get("uid-set").priority, 5)
        self.assertEqual(P.POOL.get("uid-set").weight, 2)


class PoolUpsertTests(unittest.TestCase):
    def test_same_uid_updates_credentials_without_duplicating(self):
        with tempfile.TemporaryDirectory(prefix="wb-upsert-") as directory:
            pool = wb_accounts.AccountPool(directory)
            original = account(
                "uid-upsert", note="保留", priority=7, concurrencyLimit=3,
                weight=4, disabledModels=["glm-5.3"])
            pool.add(original)
            added_at = pool.get("uid-upsert").added_at

            replacement = account(
                "uid-upsert", realm="cn", nickname="新昵称",
                accessToken="new-token")
            pool.add(replacement)

            self.assertEqual(len(pool.accounts), 1)
            stored = pool.get("uid-upsert")
            self.assertIsNot(stored, original)
            self.assertEqual(stored.access_token, "new-token")
            self.assertEqual(stored.nickname, "新昵称")
            self.assertEqual(stored.realm, "cn")
            self.assertEqual(stored.added_at, added_at)
            self.assertEqual(stored.note, "保留")
            self.assertEqual(stored.priority, 7)
            self.assertEqual(stored.concurrency_limit, 3)
            self.assertEqual(stored.weight, 4)
            self.assertEqual(stored.disabled_models, ["glm-5.3"])

            reloaded = wb_accounts.AccountPool(directory)
            reloaded.load()
            self.assertEqual(len(reloaded.accounts), 1)
            self.assertEqual(reloaded.get("uid-upsert").access_token, "new-token")

    def test_new_panel_fields_still_override_old_values(self):
        with tempfile.TemporaryDirectory(prefix="wb-upsert-") as directory:
            pool = wb_accounts.AccountPool(directory)
            pool.add(account("uid-upsert", priority=7, note="旧备注"))
            pool.add(account("uid-upsert", priority=2, note="新备注"))

            stored = pool.get("uid-upsert")
            self.assertEqual(stored.priority, 2)
            self.assertEqual(stored.note, "新备注")


if __name__ == "__main__":
    unittest.main()
