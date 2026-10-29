"""手动账号权重：面板设了 >1 权重的账号优先接单。

分派原本的偏好都来自自动信号（临期积分、剩余用量估算）。这条偏好来自用户的
明确设置，优先级也最高：weight > 1 的账号先于一切自动偏好接单，彼此之间按
权重平滑加权轮询——权重高的分到的流量多，但同层之内仍有轮转，谁也不独占。
它们都不可用时照旧落回自动偏好与轮询。

要钉住的行为：

  1. 权重随凭证档持久化，面板设置重启后还在；坏数据回落 1 不炸加载；
  2. 权重 >1 的账号压过临期账号与纯轮询，多个高权重账号按权重比分流量；
  3. 全部权重为 1（默认）时，分派与从前完全一致——这是不打破现有行为的
     底线，也是「临期子权重照旧生效」的另一半。

Run with the current interpreter (python tests/run_all.py weight).
"""
import json
import os
import sys
import tempfile
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts


def make_account(uid, realm="cn", credits=None, **extra):
    data = {"uid": uid, "accessToken": "token-%s" % uid, "realm": realm}
    if credits is not None:
        data["credits"] = credits
    data.update(extra)
    return wb_accounts.Account(data)


def credits(days_left):
    """只有一个额度包、按给定天数到期的余额数据。"""
    remain = 100
    return {
        "remain": remain, "used": 0, "size": remain,
        "packages": [{
            "name": "pkg", "package_code": "package", "remain": remain,
            "used": 0, "size": remain, "days_left": days_left,
            "no_expiry": False, "is_expired": days_left < 0,
        }],
    }


class AccountWeightFieldTests(unittest.TestCase):
    """weight 字段：默认 1、坏数据回落、随凭证档与公共视图走。"""

    def test_default_weight_is_one(self):
        self.assertEqual(make_account("w-def").weight, 1)

    def test_junk_falls_back_to_one(self):
        for junk in (None, "", "abc", [], {}, 0, -5, True):
            account = make_account("w-junk", weight=junk)
            self.assertEqual(account.weight, 1, "weight=%r should fall back to 1" % (junk,))

    def test_valid_values_are_kept_and_clamped(self):
        self.assertEqual(make_account("w-str", weight="5").weight, 5)
        self.assertEqual(make_account("w-big", weight=250).weight, 100)

    def test_to_dict_and_public_carry_weight(self):
        account = make_account("w-view", weight=4)
        self.assertEqual(account.to_dict()["weight"], 4)
        self.assertEqual(account.public()["weight"], 4)
        self.assertEqual(make_account("w-view2").public()["weight"], 1)

    def test_weight_reaches_the_disk_and_keeps_the_json_field(self):
        """两条写路径落盘重载可见，原始 JSON 保留 weight 字段。"""
        with tempfile.TemporaryDirectory(prefix="weight-persist-") as directory:
            path = make_account("w-disk", weight=3).save(directory)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["weight"], 3)
            pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
            loaded = {a.uid: a for a in pool.load()}
            self.assertEqual(loaded["w-disk"].weight, 3)
            # 面板改权重后落盘：换个新池重读，值还在。
            self.assertEqual(pool.set_weight("w-disk", 7)["weight"], 7)
            pool2 = wb_accounts.AccountPool(directory, log=lambda _m: None)
            self.assertEqual({a.uid: a for a in pool2.load()}["w-disk"].weight, 7)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["weight"], 7)


class SetWeightTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="weight-set-")
        self.pool = wb_accounts.AccountPool(self.directory, log=lambda _m: None)
        self.pool.accounts = [make_account("s-a", weight=1), make_account("s-b", weight=2)]

    def test_updates_and_returns_the_public_view(self):
        updated = self.pool.set_weight("s-a", 5)
        self.assertEqual(updated["weight"], 5)
        self.assertEqual(self.pool.get("s-a").weight, 5)

    def test_unknown_uid_returns_none(self):
        self.assertIsNone(self.pool.set_weight("nope", 3))

    def test_invalid_value_returns_none_and_keeps_the_old_one(self):
        self.assertIsNone(self.pool.set_weight("s-a", "abc"))
        self.assertEqual(self.pool.get("s-a").weight, 1)


class PickWeightedFirstTests(unittest.TestCase):
    def _pool(self, accounts, window=0):
        directory = tempfile.mkdtemp(prefix="weight-pool-")
        pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
        pool.accounts = accounts
        if window:
            pool.apply_expiring_window({"global": window, "intl": window, "cn": window})
        return pool

    def test_weighted_account_takes_every_pick(self):
        heavy = make_account("heavy", weight=5)
        light = make_account("light", weight=1)
        pool = self._pool([light, heavy])
        self.assertEqual({pool.pick(realm="cn").uid for _ in range(6)}, {"heavy"})

    def test_weighted_pair_splits_two_to_one(self):
        # nginx 平滑加权轮询的确定性：权重 (2,4) 的一个完整周期（6 次取）
        # 恰好 2:4，谁也不独占，重的先被选中。
        two = make_account("w-two", weight=2)
        four = make_account("w-four", weight=4)
        pool = self._pool([two, four])
        picked = [pool.pick(realm="cn").uid for _ in range(6)]
        self.assertEqual(Counter(picked), Counter({"w-two": 2, "w-four": 4}))
        self.assertEqual(picked[0], "w-four")

    def test_weighted_pair_splits_three_to_two(self):
        three = make_account("w-three", weight=3)
        two = make_account("w-two-b", weight=2)
        pool = self._pool([three, two])
        picked = [pool.pick(realm="cn").uid for _ in range(5)]
        self.assertEqual(Counter(picked), Counter({"w-three": 3, "w-two-b": 2}))

    def test_equal_weights_still_take_turns(self):
        first = make_account("eq-a", weight=2)
        second = make_account("eq-b", weight=2)
        pool = self._pool([first, second])
        picked = [pool.pick(realm="cn").uid for _ in range(4)]
        self.assertEqual(picked, ["eq-a", "eq-b", "eq-a", "eq-b"])

    def test_all_default_weights_keep_the_plain_round_robin(self):
        a = make_account("plain-a", weight=1)
        b = make_account("plain-b", weight=1)
        pool = self._pool([a, b])
        picked = [pool.pick(realm="cn").uid for _ in range(4)]
        self.assertEqual(sorted(set(picked)), ["plain-a", "plain-b"])

    def test_unavailable_weighted_account_falls_back_to_the_pool(self):
        parked = make_account("parked", weight=9, credits=credits(40))
        parked.reserve_credits = 1000
        spare = make_account("spare", weight=1)
        pool = self._pool([parked, spare])
        self.assertEqual({pool.pick(realm="cn").uid for _ in range(4)}, {"spare"})

    def test_excluded_weighted_account_falls_through(self):
        heavy = make_account("excl-heavy", weight=9)
        spare = make_account("excl-spare", weight=1)
        pool = self._pool([heavy, spare])
        self.assertEqual(pool.pick(realm="cn", exclude={"excl-heavy"}).uid, "excl-spare")

    def test_manual_weight_outranks_the_expiring_window(self):
        # 用户设的高权重压过自动偏好：临期账号让位给高权重账号。
        heavy = make_account("heavy-first", weight=5, credits=credits(40))
        soon = make_account("soon-credits", weight=1, credits=credits(2))
        pool = self._pool([soon, heavy], window=7)
        picked = {pool.pick(realm="cn").uid for _ in range(6)}
        self.assertEqual(picked, {"heavy-first"})

    def test_expiring_preference_still_works_for_weight_one_accounts(self):
        # 高权重账号都不可用时，落到临期偏好：权重 1 的账号身上，临期子权重
        # 一行不变。
        parked = make_account("parked-heavy", weight=5, credits=credits(40))
        parked.reserve_credits = 1000
        soon = make_account("soon-w1", weight=1, credits=credits(2))
        later = make_account("later-w1", weight=1, credits=credits(40))
        pool = self._pool([parked, later, soon], window=7)
        picked = {pool.pick(realm="cn").uid for _ in range(6)}
        self.assertEqual(picked, {"soon-w1"})


if __name__ == "__main__":
    unittest.main()
