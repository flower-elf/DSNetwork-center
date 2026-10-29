"""积分变动流水：只有余额增加才记一条，基线写在凭证文件里，首次读取只建立基线；写入失败必须就地抛出。
不联网，fetch_credits() 打桩 http_json，临时目录由 unittest 管理。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-credit-events-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts as A
import wb_proxy as P
import wb_usage as U


def billing(remain, size=1000):
    """一次成功的余额读取：单套餐。"""
    return {"data": {"Response": {"Data": {"Accounts": [{
        "PackageName": "套餐",
        "CapacitySize": size,
        "CapacityRemain": remain,
        "CapacityUsed": max(0, size - remain),
    }]}}}}


class FakeRequest(object):
    """GET 处理函数只需要鉴权与 JSON 输出。"""

    def _authorized(self):
        return True

    def _error(self, status, message, kind=None):
        return status, {"error": {"message": message, "code": status}}

    def _json(self, status, payload):
        return status, payload


class CreditEventTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-credit-events-write-")
        self.addCleanup(self._tmp.cleanup)
        self.usage = os.path.join(self._tmp.name, "usage")
        self.accounts = os.path.join(self._tmp.name, "accounts")
        os.makedirs(self.accounts)
        self.ledger = os.path.join(self.usage, "credit_events.jsonl")
        self.account = A.Account({
            "uid": "uid-cn", "nickname": "小北", "accessToken": "token-cn",
            "realm": "cn",
        })
        self.account.path = os.path.join(self.accounts, "uid-cn.json")
        self.account.save(self.accounts)
        self.addCleanup(A.set_credit_events_provider, U.credit_events_file)

    def fetch(self, remain):
        with mock.patch.object(P, "USAGE_DIR", self.usage):
            with mock.patch.object(A, "http_json", lambda *a, **k: billing(remain)):
                return self.account.fetch_credits()

    def events(self):
        if not os.path.exists(self.ledger):
            return []
        with open(self.ledger, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def reload(self):
        with open(self.account.path, encoding="utf-8") as fh:
            data = json.load(fh)
        return A.Account(data, self.account.path)

    def test_the_first_read_only_sets_the_baseline(self):
        self.assertTrue(self.fetch(40)["ok"])
        self.assertEqual(self.events(), [])
        self.assertFalse(os.path.exists(self.ledger))
        self.assertEqual(self.reload().credits["remain"], 40)

    def test_a_higher_balance_records_one_row(self):
        self.fetch(40)
        before = int(time.time())
        self.fetch(55)
        rows = self.events()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["uid"], "uid-cn")
        self.assertEqual(row["nickname"], "小北")
        self.assertEqual(row["realm"], "cn")
        self.assertEqual((row["before"], row["after"], row["delta"]), (40, 55, 15))
        self.assertGreaterEqual(row["at"], before)
        self.assertIsInstance(row["at"], int)
        self.assertTrue(row["iso"])
        self.assertIsInstance(row["iso"], str)

    def test_equal_or_lower_balances_are_not_recorded(self):
        self.fetch(40)
        self.fetch(40)
        self.fetch(12)
        self.fetch(12)
        self.assertEqual(self.events(), [])
        # 余额下降后基线跟着下降，之后的增加按新基线计算。
        self.fetch(20)
        rows = self.events()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["before"], rows[0]["after"], rows[0]["delta"]),
                         (12, 20, 8))

    def test_each_increase_appends_its_own_row(self):
        self.fetch(1)
        self.fetch(2)
        self.fetch(7)
        self.assertEqual([(r["before"], r["after"]) for r in self.events()],
                         [(1, 2), (2, 7)])

    def test_a_new_row_drops_rows_older_than_thirty_days(self):
        os.makedirs(self.usage, exist_ok=True)
        now = int(time.time())
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": now - 31 * 86400, "uid": "old"}) + "\n")
            fh.write(json.dumps({"at": now - 29 * 86400, "uid": "recent"}) + "\n")
        self.fetch(40)
        self.fetch(55)
        self.assertEqual([row["uid"] for row in self.events()], ["recent", "uid-cn"])

    def test_the_baseline_survives_a_reload(self):
        """重启后接着比：基线来自凭证文件。"""
        self.fetch(40)
        self.fetch(55)
        self.assertEqual(len(self.events()), 1)

        self.account = self.reload()
        self.assertEqual(self.account.credits["remain"], 55)
        self.fetch(80)
        rows = self.events()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]["before"], rows[1]["after"]), (55, 80))

    def test_a_write_failure_is_not_swallowed(self):
        self.fetch(40)
        blocked = os.path.join(self._tmp.name, "blocked")
        with open(blocked, "w", encoding="utf-8") as fh:
            fh.write("")
        A.set_credit_events_provider(
            lambda: os.path.join(blocked, "credit_events.jsonl"))
        with self.assertRaises(OSError):
            self.fetch(55)
        # 余额先写进凭证文件，流水这一行缺失是看得见的中断。
        self.assertEqual(self.reload().credits["remain"], 55)
        self.assertFalse(os.path.exists(self.ledger))

    def test_without_a_provider_nothing_is_written(self):
        A.set_credit_events_provider(None)
        self.assertTrue(self.fetch(40)["ok"])
        self.assertTrue(self.fetch(90)["ok"])
        self.assertEqual(self.events(), [])
        self.assertFalse(os.path.exists(self.ledger))

    def test_a_credits_read_that_fails_writes_nothing(self):
        self.fetch(40)

        def boom(*args, **kwargs):
            raise RuntimeError("upstream down")

        with mock.patch.object(P, "USAGE_DIR", self.usage):
            with mock.patch.object(A, "http_json", boom):
                res = self.account.fetch_credits()
        self.assertFalse(res["ok"])
        self.assertEqual(self.events(), [])


class LedgerFixtureMixin(object):
    """处理读取测试要用的写入，包括一行损坏的数据。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-credit-events-read-")
        self.addCleanup(self._tmp.cleanup)
        self.usage = self._tmp.name
        self.ledger = os.path.join(self.usage, "credit_events.jsonl")

    def write(self, uid, realm, before, after, raw=None, at=None):
        at = int(time.time()) if at is None else at
        row = raw if raw is not None else {
            "at": at, "iso": "", "uid": uid,
            "nickname": uid, "realm": realm, "before": before, "after": after,
            "delta": after - before,
        }
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write((row if isinstance(row, str) else json.dumps(row, ensure_ascii=False))
                     + "\n")

    def read(self, realm=None, limit=200, offset=0):
        with mock.patch.object(P, "USAGE_DIR", self.usage):
            return U.read_credit_events(realm=realm, limit=limit, offset=offset)


class CreditEventReadTests(LedgerFixtureMixin, unittest.TestCase):
    def test_a_missing_file_reads_as_empty(self):
        self.assertEqual(self.read(), {"events": [], "total": 0, "offset": 0, "limit": 200})

    def test_newest_first_with_a_total(self):
        self.write("uid-1", "cn", 1, 2)
        self.write("uid-2", "intl", 5, 9)
        got = self.read()
        self.assertEqual(got["total"], 2)
        self.assertEqual([row["uid"] for row in got["events"]], ["uid-2", "uid-1"])

    def test_the_limit_keeps_the_newest_rows(self):
        for index in range(5):
            self.write("uid-%d" % index, "cn", 1, 2)
        got = self.read(limit=2)
        self.assertEqual(got["total"], 5)
        self.assertEqual([row["uid"] for row in got["events"]], ["uid-4", "uid-3"])

    def test_realm_all_covers_both_exits(self):
        self.write("uid-cn", "cn", 1, 2)
        self.write("uid-intl", "intl", 1, 2)
        self.assertEqual(self.read(realm="cn")["total"], 1)
        self.assertEqual(self.read(realm="intl")["total"], 1)
        self.assertEqual(self.read(realm="all")["total"], 2)
        self.assertEqual(self.read(realm=None)["total"], 2)

    def test_corrupt_lines_are_skipped(self):
        self.write("uid-1", "cn", 1, 2)
        self.write(None, None, None, None, raw="{not json")
        self.write(None, None, None, None, raw='"a string"')
        self.write(None, None, None, None, raw="   ")
        self.write("uid-2", "cn", 2, 3)
        got = self.read()
        self.assertEqual(got["total"], 2)
        self.assertEqual([row["uid"] for row in got["events"]], ["uid-2", "uid-1"])

    def test_an_unusable_limit_falls_back_to_the_default(self):
        self.write("uid-1", "cn", 1, 2)
        self.assertEqual(len(self.read(limit="abc")["events"]), 1)
        self.assertEqual(len(self.read(limit=0)["events"]), 1)
        self.assertEqual(len(self.read(limit=-5)["events"]), 1)

    def test_pages_walk_from_newest_to_oldest(self):
        for index in range(5):
            self.write("uid-%d" % index, "cn", 1, 2)
        second = self.read(limit=2, offset=2)
        self.assertEqual(second["total"], 5)
        self.assertEqual([row["uid"] for row in second["events"]], ["uid-2", "uid-1"])
        self.assertEqual([row["uid"] for row in self.read(limit=2, offset=4)["events"]], ["uid-0"])
        self.assertEqual(self.read(limit=2, offset=10)["events"], [])

    def test_rows_older_than_thirty_days_are_not_listed(self):
        self.write("uid-old", "cn", 1, 2, at=int(time.time()) - 31 * 86400)
        self.write("uid-new", "cn", 2, 3)
        got = self.read()
        self.assertEqual(got["total"], 1)
        self.assertEqual(got["events"][0]["uid"], "uid-new")


class CreditEventRouteTests(LedgerFixtureMixin, unittest.TestCase):
    def test_the_route_serves_filtered_events(self):
        self.write("uid-cn-1", "cn", 1, 2)
        self.write("uid-cn-2", "cn", 2, 5)
        self.write("uid-intl", "intl", 1, 9)
        with mock.patch.object(P, "USAGE_DIR", self.usage):
            status, body = P.Handler._get_accounts_credit_events(
                FakeRequest(), {"realm": ["cn"], "limit": ["1"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 2)
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(body["events"][0]["uid"], "uid-cn-2")

    def test_the_route_accepts_all_and_the_default_exit(self):
        self.write("uid-cn", "cn", 1, 2)
        self.write("uid-intl", "intl", 1, 2)
        with mock.patch.object(P, "USAGE_DIR", self.usage):
            status, body = P.Handler._get_accounts_credit_events(
                FakeRequest(), {"realm": ["all"]})
        self.assertEqual((status, body["total"]), (200, 2))
        with mock.patch.object(P, "USAGE_DIR", self.usage):
            with mock.patch.object(P, "CURRENT_REALM", "intl"):
                status, body = P.Handler._get_accounts_credit_events(FakeRequest(), {})
        self.assertEqual((status, body["total"]), (200, 1))
        self.assertEqual(body["events"][0]["realm"], "intl")


if __name__ == "__main__":
    unittest.main()
