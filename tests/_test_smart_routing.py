"""评分分配：新对话按积分到期节奏与忙闲选号，瞬时故障在原地重试。

已在服务的对话继续用原账号；不联网：账号是内存里的对象，出站请求由打桩的 urlopen 记录。
"""
import collections
import io
import itertools
import json
import os
import sys
import tempfile
import time
import threading
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-routing-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_settings
import wb_proxy as P
import wb_usage as U

MODEL = "deepseek-v4.1-flash"


def account(uid, realm="cn", remain=None, days=None, rate=None):
    """一个能接单的账号；remain/days 给出到期套餐，rate 是它的近期消耗速度。"""
    data = {"uid": uid, "accessToken": "tok-" + uid, "refreshToken": "refresh-" + uid,
            "realm": realm, "nickname": uid, "enabled": True}
    if remain is not None:
        data["credits"] = {
            "remain": remain, "size": remain,
            "packages": [{"remain": remain, "size": remain, "name": "包",
                          "days_left": days,
                          "expireAt": int(time.time() + days * 86400)}],
        }
    acc = wb_accounts.Account(data)
    if rate is not None:
        acc.credit_rate_per_day = rate
    return acc


def packages(*pairs):
    """余额快照：每一项是（余额，距今天数），天数为负表示已经过期。"""
    now = time.time()
    return {"remain": sum(remain for remain, _days in pairs), "packages": [
        {"remain": remain, "name": "包", "days_left": days,
         "expireAt": int(now + days * 86400)}
        for remain, days in pairs]}


def pool_with(*accounts, smart=True):
    pool = wb_accounts.AccountPool(os.path.join(_TMP, "pools"))
    pool.accounts = list(accounts)
    pool.smart_routing = smart
    return pool


def usage_row(at, uid, total, credit=None, outcome="completed"):
    row = {"at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
           "model": MODEL, "stream": True, "outcome": outcome,
           "total_tokens": total, "account": uid, "realm": "cn"}
    if credit is not None:
        row["credit"] = credit
    return row


def write_log(rows, mode="w"):
    with io.open(P.USAGE_LOG, mode, encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def reset_daily_cache():
    with U._daily_usage_lock:
        U._daily_usage.update({"day": "", "totals": None, "credits": None,
                               "offset": 0, "at": 0.0})


class SettingTests(unittest.TestCase):
    """开关默认打开，读写走 settings.json。"""

    def test_defaults_to_on_and_round_trips(self):
        with tempfile.TemporaryDirectory(prefix="routing-set-") as directory:
            self.assertTrue(wb_settings.smart_routing(directory),
                            "没写过这个键时必须按打开处理")
            self.assertFalse(wb_settings.set_smart_routing(directory, False))
            self.assertFalse(wb_settings.smart_routing(directory))
            self.assertTrue(wb_settings.set_smart_routing(directory, True))
            self.assertTrue(wb_settings.smart_routing(directory))

    def test_the_pool_resolves_the_setting_and_the_argument(self):
        with tempfile.TemporaryDirectory(prefix="routing-set-") as directory:
            pool = wb_accounts.AccountPool(directory)
            self.assertFalse(pool.smart_routing,
                             "直接构造的账号池默认要保持原来的轮询行为")
            pool.apply_smart_routing()
            self.assertTrue(pool.smart_routing)
            wb_settings.set_smart_routing(directory, False)
            pool.apply_smart_routing()
            self.assertFalse(pool.smart_routing)
            self.assertTrue(pool.apply_smart_routing(True))


class ScoredPlacementTests(unittest.TestCase):
    """新对话按到期节奏的欠账分配，欠账相同则按忙闲。"""

    def test_picks_split_by_what_each_account_must_spend(self):
        # uid-a 每天要花掉一百、uid-b 每天只花掉十六点七，两者都还没花，按欠账约 6 比 1 分配。
        pool = pool_with(account("uid-a", remain=1000, days=10, rate=0.0),
                         account("uid-b", remain=1000, days=60, rate=0.0))
        counts = collections.Counter(pool.pick(realm="cn").uid for _ in range(70))
        self.assertEqual(sum(counts.values()), 70)
        self.assertGreater(counts["uid-a"], counts["uid-b"] * 3,
                           "到期快的账号没拿到应有的份额：%r" % (counts,))
        self.assertGreater(counts["uid-b"], 0,
                           "到期慢的账号被完全饿着：%r" % (counts,))

    def test_equal_obligations_split_the_picks_evenly(self):
        pool = pool_with(account("uid-a", remain=1000, days=30),
                         account("uid-b", remain=1000, days=30),
                         account("uid-c", remain=1000, days=30))
        counts = collections.Counter(pool.pick(realm="cn").uid for _ in range(30))
        self.assertEqual(sum(counts.values()), 30)
        for uid in ("uid-a", "uid-b", "uid-c"):
            self.assertAlmostEqual(counts[uid], 10, delta=2,
                                   msg="义务相同时分配不均：%r" % (counts,))

    def test_an_account_that_is_on_pace_gives_way(self):
        # uid-a 欠账为零、uid-b 欠着一百点，几乎全部新对话归 uid-b。
        pool = pool_with(account("uid-a", remain=1000, days=10, rate=100.0),
                         account("uid-b", remain=1000, days=10, rate=0.0))
        self.assertEqual(pool.pick(realm="cn").uid, "uid-b")
        counts = collections.Counter(pool.pick(realm="cn").uid for _ in range(40))
        self.assertGreaterEqual(counts["uid-b"], 38,
                                "欠账的账号没有拿到新对话：%r" % (counts,))

    def test_an_account_without_credit_data_still_gets_picks(self):
        # 还没查过余额的账号按池内中位数对待，不能被一直排在最后。
        pool = pool_with(account("uid-a", remain=1000, days=10, rate=0.0),
                         account("uid-new"))
        counts = collections.Counter(pool.pick(realm="cn").uid for _ in range(20))
        self.assertGreater(counts["uid-new"], 0, "没有余额数据的账号没拿到单")
        self.assertGreater(counts["uid-a"], 0)

    def test_excluded_accounts_are_skipped(self):
        pool = pool_with(account("uid-a", remain=1000, days=10),
                         account("uid-b", remain=1000, days=10))
        self.assertEqual(pool.pick(realm="cn", exclude={"uid-a"}).uid, "uid-b")
        self.assertIsNone(pool.pick(realm="cn", exclude={"uid-a", "uid-b"}))

    def test_the_other_realm_is_not_offered(self):
        pool = pool_with(account("uid-cn", realm="cn", remain=1000, days=10),
                         account("uid-intl", realm="intl", remain=1000, days=10))
        self.assertEqual(pool.pick(realm="intl").uid, "uid-intl")

    def test_the_switch_puts_the_cursor_order_back(self):
        pool = pool_with(account("uid-a", remain=1000, days=10),
                         account("uid-b", remain=1000, days=60))
        pool.smart_routing = False
        picked = [pool.pick(realm="cn").uid for _ in range(4)]
        self.assertEqual(picked, ["uid-a", "uid-b", "uid-a", "uid-b"],
                         "关掉开关后应当回到轮询顺序")
        self.assertEqual(pool.routing_snapshot()["cursor_pick"], 4)

    def test_a_late_package_is_spread_over_its_own_term(self):
        # 500 积分两天后到期与 10000 积分三十天后到期并存：目标取 max(500/2, 10500/30) = 350。
        a = account("uid-a")
        a.credits = packages((500, 2), (10000, 30))
        self.assertAlmostEqual(a.credit_target_per_day(), 350.0, delta=0.5)
        b = account("uid-b", remain=10000, days=30, rate=0.0)
        counts = collections.Counter(pool_with(a, b).pick(realm="cn").uid for _ in range(100))
        self.assertLess(counts["uid-a"], 60, "晚到期的大包被算进了近期义务：%r" % (counts,))

    def test_an_expired_package_in_a_stale_snapshot_is_ignored(self):
        a = account("uid-a")
        a.credits = packages((50, -0.2), (3000, 25))
        self.assertAlmostEqual(a.credit_target_per_day(), 120.0, delta=0.5)

    def test_a_queried_empty_balance_owes_nothing(self):
        empty = account("uid-z")
        empty.credits = packages((0, 10))
        self.assertEqual(empty.credit_target_per_day(), 0.0)
        pool = pool_with(account("uid-a", remain=1000, days=10, rate=0.0),
                         account("uid-b", remain=1000, days=10, rate=0.0), empty)
        counts = collections.Counter(pool.pick(realm="cn").uid for _ in range(60))
        self.assertEqual(counts["uid-z"], 0, "余额为 0 的账号还在分新对话：%r" % (counts,))

    def test_an_account_that_cannot_serve_does_not_flatten_the_split(self):
        disabled = account("uid-d", remain=100000, days=10, rate=0.0)
        disabled.enabled = False
        pool = pool_with(account("uid-a", remain=1000, days=10, rate=0.0),
                         account("uid-b", remain=1000, days=100, rate=0.0), disabled)
        counts = collections.Counter(pool.pick(realm="cn").uid for _ in range(110))
        self.assertGreater(counts["uid-a"], counts["uid-b"] * 5,
                           "停用账号把份额压平了：%r" % (counts,))

    def test_concurrent_picks_do_not_trip_over_the_load_window(self):
        # 概率性守护：mark_pick 与 recent_load 不加锁时会出现 deque mutated during iteration。
        pool = pool_with(*[account("uid-%d" % i, remain=1000, days=10 + i) for i in range(6)])
        errors = []

        def worker():
            for _ in range(1500):
                try:
                    pool.pick(realm="cn")
                except RuntimeError as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(pool.routing_snapshot()["smart_pick"], 8 * 1500)


class ExpiringTierTests(unittest.TestCase):
    """智能分配打开时，窗口内的临期积分仍然先被消耗。"""

    def _pool(self, *accounts, window=7):
        pool = pool_with(*accounts)
        pool.apply_expiring_window({"global": window, "intl": window, "cn": window})
        return pool

    def test_window_accounts_take_the_new_conversations(self):
        soon = account("soon", remain=100, days=1)
        later = account("later", remain=1000, days=60)
        pool = self._pool(soon, later)
        picked = {pool.pick(realm="cn").uid for _ in range(8)}
        self.assertEqual(picked, {"soon"})

    def test_window_accounts_share_by_credits_at_risk(self):
        # 余额多、离到期近的账号分到更多流量，余额少的仍然有单可接。
        small = account("small", remain=10, days=0.5)
        big = account("big", remain=1000, days=6)
        pool = self._pool(small, big)
        counts = collections.Counter(pool.pick(realm="cn").uid for _ in range(20))
        self.assertGreater(counts["big"], counts["small"] * 3, counts)
        self.assertGreater(counts["small"], 0, counts)

    def test_window_off_keeps_the_scored_split(self):
        soon = account("soon-off", remain=100, days=1)
        later = account("later-off", remain=1000, days=60)
        pool = self._pool(soon, later, window=0)
        counts = collections.Counter(pool.pick(realm="cn").uid for _ in range(30))
        self.assertGreater(counts["soon-off"], 0)
        self.assertGreater(counts["later-off"], 0, "窗口关闭后仍按欠账分配")

    def test_rate_follows_each_expiry_tranche(self):
        # 100 一天后到期、10000 六天后到期：每日最低消耗是 10100/6，不是
        # 10100/1；大包不能压到明天。
        acc = account("tranches")
        acc.credits = packages((100, 1), (10000, 6))
        self.assertAlmostEqual(acc.expiring_rate_per_day(7), 10100.0 / 6, delta=1)

    def test_an_unavailable_window_account_falls_through(self):
        parked = account("parked", remain=100, days=1)
        parked.reserve_credits = 10000
        later = account("spare", remain=1000, days=60)
        pool = self._pool(parked, later)
        self.assertEqual({pool.pick(realm="cn").uid for _ in range(6)}, {"spare"})


class RemainingTierTests(unittest.TestCase):
    """智能分配打开时，估计剩余接近耗尽的组合在评分里更靠前。"""

    def test_tight_account_gets_more_picks(self):
        # 同等积分义务时才看剩余用量倾斜：欠账差很大时仍由智能分配主导。
        tight = account("tight-rem")
        loose = account("loose-rem")
        pool = pool_with(tight, loose)
        pool.apply_remaining_weights({("tight-rem", MODEL): 5})
        picks = [pool.pick(realm="cn", model=MODEL).uid for _ in range(20)]
        self.assertGreater(picks.count("tight-rem"), picks.count("loose-rem"), picks)
        self.assertGreater(picks.count("loose-rem"), 0, picks)

    def test_credit_obligation_outweighs_the_remaining_hint(self):
        # 余额多、到期近的账号义务更高，即使另一个组合接近估计限额，也仍多接单。
        tight = account("tight-low", remain=100, days=60)
        rich = account("rich-loose", remain=1000, days=60)
        pool = pool_with(tight, rich)
        pool.apply_remaining_weights({("tight-low", MODEL): 5})
        picks = [pool.pick(realm="cn", model=MODEL).uid for _ in range(20)]
        self.assertGreater(picks.count("rich-loose"), picks.count("tight-low"), picks)

    def test_weight_applies_per_model(self):
        # 权重只作用于登记的模型，其他模型照常参与评分分配。
        tight = account("tight-model", remain=100, days=60)
        loose = account("loose-model", remain=1000, days=60)
        pool = pool_with(tight, loose)
        pool.apply_remaining_weights({("tight-model", MODEL): 3})
        picked = {pool.pick(realm="cn", model="other-model").uid for _ in range(8)}
        self.assertEqual(picked, {"tight-model", "loose-model"})

    def test_without_weights_the_scored_split_stays(self):
        tight = account("tight-idle", remain=100, days=60)
        loose = account("loose-idle", remain=1000, days=60)
        pool = pool_with(tight, loose)
        picked = {pool.pick(realm="cn", model=MODEL).uid for _ in range(8)}
        self.assertEqual(picked, {"tight-idle", "loose-idle"})


class RoutingCounterTests(unittest.TestCase):
    """会话归属的四种结果各有计数，面板据此判断换号发生了多少次。"""

    def setUp(self):
        self.pool = pool_with(account("uid-a"), account("uid-b"))

    def test_the_four_session_outcomes(self):
        self.pool.pick_for_session(realm="cn", session_key="s1")
        self.pool.pick_for_session(realm="cn", session_key="s1")
        self.pool.pick_for_session(realm="cn")
        stats = self.pool.routing_snapshot()
        self.assertEqual(stats["fresh_binding"], 1)
        self.assertEqual(stats["bound_hit"], 1)
        self.assertEqual(stats["no_key"], 1)
        self.assertEqual(stats["bound_lost"], 0)
        self.assertEqual(stats["affinity_entries"], 1)
        self.assertEqual(stats["smart_pick"], 2)
        self.assertTrue(stats["smart_routing"])

    def test_a_binding_that_cannot_serve_counts_as_a_switch(self):
        bound = self.pool.pick_for_session(realm="cn", session_key="s1", model=MODEL)
        bound.note_error("HTTP 429", model=MODEL, cooldown=60)
        self.assertFalse(bound.ready(model=MODEL))
        again = self.pool.pick_for_session(realm="cn", session_key="s1", model=MODEL)
        self.assertNotEqual(again.uid, bound.uid, "绑定失效后没有换号")
        stats = self.pool.routing_snapshot()
        self.assertEqual(stats["bound_lost"], 1)
        self.assertEqual(stats["bound_hit"], 0)
        # 换号之后绑定跟到新账号上，下一轮又是命中
        self.assertEqual(
            self.pool.pick_for_session(realm="cn", session_key="s1", model=MODEL).uid,
            again.uid)
        self.assertEqual(self.pool.routing_snapshot()["bound_hit"], 1)

    def test_an_excluded_binding_counts_as_a_switch(self):
        # 请求路径换号时把上一个账号排除在外；这次也算换号，不算首次分配。
        bound = self.pool.pick_for_session(realm="cn", session_key="s1")
        other = "uid-b" if bound.uid == "uid-a" else "uid-a"
        again = self.pool.pick_for_session(realm="cn", session_key="s1",
                                           exclude={bound.uid})
        self.assertEqual(again.uid, other)
        stats = self.pool.routing_snapshot()
        self.assertEqual(stats["bound_lost"], 1)
        self.assertEqual(stats["fresh_binding"], 1, "只有第一次取号是首次分配")
        self.assertEqual(
            self.pool.pick_for_session(realm="cn", session_key="s1").uid, other)

    def test_an_unknown_counter_name_is_refused(self):
        with self.assertRaises(KeyError):
            self.pool.count_routing("nope")


class DailyCreditFoldTests(unittest.TestCase):
    """今日积分的折叠与今日 Token 共用一份增量扫描。"""

    def setUp(self):
        reset_daily_cache()

    def test_credits_fold_with_tokens_and_skip_old_and_cancelled_rows(self):
        midnight = U._local_midnight()
        write_log([
            usage_row(midnight - 60, "acct-A", 5000, credit=9.9),      # 昨天
            usage_row(midnight + 60, "acct-A", 1200, credit=0.25),
            usage_row(midnight + 120, "acct-B", 300, credit=1.5),
            usage_row(midnight + 180, "acct-A", 700, credit=0.75,
                      outcome="client_aborted"),                        # 客户端取消
            usage_row(midnight + 240, "acct-C", 100),                   # 没有积分字段
        ])
        tokens, credits = U.daily_usage_by_account(ttl=0)
        self.assertEqual(tokens, {"acct-A": 1200, "acct-B": 300, "acct-C": 100})
        self.assertEqual(credits, {"acct-A": 0.25, "acct-B": 1.5})
        self.assertEqual(U.daily_tokens_by_account(ttl=0), tokens)

        # 追加的行接着折叠，不重读整个文件
        write_log([usage_row(midnight + 300, "acct-B", 200, credit=0.5)], mode="a")
        tokens, credits = U.daily_usage_by_account(ttl=0)
        self.assertEqual(tokens["acct-B"], 500)
        self.assertAlmostEqual(credits["acct-B"], 2.0, places=6)

    def test_a_missing_log_reads_as_none(self):
        if os.path.exists(P.USAGE_LOG):
            os.remove(P.USAGE_LOG)
        self.assertEqual(U.daily_usage_by_account(ttl=0), (None, None))

    def test_the_scale_extrapolates_today_to_a_day(self):
        noon = time.struct_time((2026, 10, 6, 12, 0, 0, 1, 280, -1))
        self.assertAlmostEqual(U.credit_rate_scale(noon), 2.0, places=6)
        just_after_midnight = time.struct_time((2026, 10, 6, 0, 30, 0, 1, 280, -1))
        self.assertEqual(U.credit_rate_scale(just_after_midnight), 1.0)


class RoutingInputsTests(unittest.TestCase):
    """积分速度推到账号上；开关关掉时整段折叠都跳过。"""

    def setUp(self):
        reset_daily_cache()
        self.pool = wb_accounts.AccountPool(os.environ["ACCOUNTS_DIR"])
        self.pool.accounts = [account("acct-A"), account("acct-B"), account("acct-C")]
        midnight = U._local_midnight()
        write_log([usage_row(midnight + 60, "acct-A", 1000, credit=2.0),
                   usage_row(midnight + 120, "acct-B", 500, credit=1.0)])
        self.old_pool = P.POOL
        P.POOL = self.pool
        self.addCleanup(self.restore)

    def restore(self):
        P.POOL = self.old_pool
        reset_daily_cache()

    def test_rates_are_pushed_only_while_the_switch_is_on(self):
        self.pool.smart_routing = False
        self.pool.accounts[2].credit_rate_per_day = 99.0
        self.assertIsNone(U.apply_routing_inputs(refresh=True))
        self.assertEqual(self.pool.accounts[2].credit_rate_per_day, 99.0,
                         "开关关掉时不该动账号上的速度")

        self.pool.smart_routing = True
        U.apply_routing_inputs(refresh=True)
        scale = U.credit_rate_scale()
        self.assertAlmostEqual(self.pool.accounts[0].credit_rate_per_day, 2.0 * scale,
                               places=6)
        self.assertAlmostEqual(self.pool.accounts[1].credit_rate_per_day, 1.0 * scale,
                               places=6)
        self.assertEqual(self.pool.accounts[2].credit_rate_per_day, 0.0,
                         "日志里没有的账号要清掉上一次留下的速度")


class TransientRetryTests(unittest.TestCase):
    """5xx 与连接抖动先在同一个账号上重试，429 直接换号。"""

    def setUp(self):
        self.pool = pool_with(account("uid-a"), account("uid-b"), smart=False)
        self.old_pool = P.POOL
        P.POOL = self.pool
        self.calls = []
        self.old_urlopen = wb_accounts.urlopen
        self.addCleanup(self.restore)

    def restore(self):
        P.POOL = self.old_pool
        wb_accounts.urlopen = self.old_urlopen

    def _install(self, replies):
        def fake_urlopen(req, timeout=None, proxy=None):
            self.calls.append(req.get_header("Authorization"))
            reply = replies[min(len(self.calls), len(replies)) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply

        wb_accounts.urlopen = fake_urlopen

    def _payload(self):
        return {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}

    def _drive(self, replies, expected, session_key="s-transient"):
        self._install(replies)
        with self.assertRaises(expected):
            P.open_upstream(self._payload(), session_key=session_key,
                            target_realm="cn", lease=P.SlotLease())

    def test_a_5xx_is_absorbed_by_an_in_place_retry(self):
        ok = object()
        error = urllib.error.HTTPError("https://upstream.invalid", 503, "busy", {},
                                       io.BytesIO(b""))
        resp, acc, _ = self._drive_until_ok([error, error, ok])
        self.assertIs(resp, ok)
        self.assertEqual(acc.uid, "uid-a")
        self.assertEqual(self.calls, ["Bearer tok-uid-a"] * 3,
                         "两次 503 之后没有回到同一个账号：%r" % (self.calls,))

    def _drive_until_ok(self, replies, session_key="s-transient"):
        self._install(replies)
        return P.open_upstream(self._payload(), session_key=session_key, target_realm="cn",
                               lease=P.SlotLease())

    def test_a_connection_hiccup_is_absorbed_by_an_in_place_retry(self):
        ok = object()
        reply = urllib.error.URLError("connection reset by peer")
        resp, acc, _ = self._drive_until_ok([reply, ok])
        self.assertIs(resp, ok)
        self.assertEqual(self.calls, ["Bearer tok-uid-a"] * 2,
                         "连接抖动之后没有回到同一个账号：%r" % (self.calls,))
        self.assertEqual(acc.uid, "uid-a")

    def test_turning_affinity_off_picks_again_every_turn(self):
        ok = object()
        self._install([ok])
        wb_settings.set_session_affinity(P.ACCOUNTS_DIR, False)
        self.addCleanup(wb_settings.set_session_affinity, P.ACCOUNTS_DIR, True)
        uids = [P.open_upstream(self._payload(), session_key="s-off", target_realm="cn",
                                lease=P.SlotLease())[1].uid
                for _ in range(2)]
        self.assertEqual(uids, ["uid-a", "uid-b"], "关闭会话粘性后同一对话还固定在一个账号上")
        self.assertIsNone(self.pool.affinity.get("s-off"))

        wb_settings.set_session_affinity(P.ACCOUNTS_DIR, True)
        uids = [P.open_upstream(self._payload(), session_key="s-on", target_realm="cn",
                                lease=P.SlotLease())[1].uid
                for _ in range(2)]
        self.assertEqual(uids[0], uids[1], "默认的会话粘性没有生效")

    def test_a_persistent_5xx_retries_in_place_then_rotates(self):
        error = urllib.error.HTTPError("https://upstream.invalid", 503, "busy", {},
                                       io.BytesIO(b""))
        self._drive([error], urllib.error.HTTPError)
        limit = P.TRANSIENT_SAME_ACCOUNT_RETRIES
        self.assertEqual(set(self.calls[:limit + 1]), {self.calls[0]},
                         "第一次 503 就换了账号：%r" % (self.calls,))
        self.assertNotEqual(self.calls[limit + 1], self.calls[0],
                            "原地重试用完以后没有换到下一个账号：%r" % (self.calls,))
        runs = max(len(list(group)) for _key, group in itertools.groupby(self.calls))
        self.assertLessEqual(runs, limit + 1,
                             "同一个账号连续重试超过上限：%r" % (self.calls,))
        budget = max(2, 2) + 1 + P.MAX_TRANSIENT_SAME_ACCOUNT_RETRIES
        if P.auto_switch_product_enabled():
            budget += P.MAX_PRODUCT_SWITCHES
        self.assertLessEqual(len(self.calls), budget,
                             "重试次数超过了整次请求的预算：%r" % (self.calls,))

    def test_a_429_rotates_immediately(self):
        detail = json.dumps({"code": 6004,
                             "msg": "usage exceeds frequency limit"}).encode()
        error = urllib.error.HTTPError("https://upstream.invalid", 429, "throttled",
                                       {}, io.BytesIO(detail))
        self._drive([error], P.RateLimited)
        self.assertGreaterEqual(len(self.calls), 2)
        self.assertNotEqual(self.calls[0], self.calls[1],
                            "429 之后还在原账号上重试：%r" % (self.calls,))

    def test_the_helper_counts_two_per_account_and_four_per_request(self):
        counters = {}
        self.assertTrue(P._retry_same_account(account("uid-a"), counters))
        self.assertTrue(P._retry_same_account(account("uid-a"), counters))
        self.assertFalse(P._retry_same_account(account("uid-a"), counters))
        self.assertTrue(P._retry_same_account(account("uid-b"), counters))
        self.assertTrue(P._retry_same_account(account("uid-b"), counters))
        self.assertFalse(P._retry_same_account(account("uid-c"), counters),
                         "整次请求的原地重试上限没有生效")
        self.assertFalse(P._retry_same_account(None, counters))


if __name__ == "__main__":
    unittest.main()
