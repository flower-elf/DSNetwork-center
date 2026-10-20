import io
import json
import os
import sys
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_tasks as T  # noqa: E402


class FakeAccount(object):
    def __init__(self):
        self.uid = "u-test"
        self.nickname = "tester"
        self.realm = "cn"
        self.proxy = ""
        self.access_token = "tok"
        self.credits = {"remain": 100}

    def headers(self, kind="chat"):
        return {}

    def fetch_credits(self):
        pass


def _task(code, **over):
    t = {"task_code": code, "status": "not_accepted", "current": 0, "target": 1,
         "name": code, "jump_url": "", "task_type": "single"}
    t.update(over)
    return t


class _Resp(object):
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(url, code, body):
    return urllib.error.HTTPError(url, code, "Bad Request", {},
                                  io.BytesIO(body.encode("utf-8")))


class GrowthPrereqTests(unittest.TestCase):
    def setUp(self):
        self._orig = (T.fetch_growth_tasks, T.query_growth_tasks, T.accept_tasks,
                      T.claim_task, T.report_events, T.do_cat_travel)
        self.events = []
        self.state = {"fb": 0, "cc": "not_accepted"}

    def tearDown(self):
        (T.fetch_growth_tasks, T.query_growth_tasks, T.accept_tasks,
         T.claim_task, T.report_events, T.do_cat_travel) = self._orig

    def _stub(self):
        def fetch(acc):
            fb_status = {0: "not_accepted", 1: "completed", 2: "claimed"}[self.state["fb"]]
            fb = _task("first_buddy", status=fb_status, current=min(self.state["fb"], 1),
                       target=1, task_type="auto")
            cc = _task("create_canvas", status=self.state["cc"])
            return [fb, cc]
        T.fetch_growth_tasks = fetch

        def report(acc, evs, base=None):
            code = "first_buddy" if evs and evs[0].get("eventCode") == "chat_request_send" else "other"
            self.events.append(("report", code))
            if code == "first_buddy":
                self.state["fb"] = 1
            return True
        T.report_events = report

        def claim(acc, code):
            self.events.append(("claim", code))
            if code == "first_buddy":
                self.state["fb"] = 2
            return {"ok": True, "credit": 300 if code == "first_buddy" else 100}
        T.claim_task = claim

        def accept(acc, codes, chunk=20):
            self.events.append(("accept", tuple(codes)))
            if "create_canvas" in codes:
                self.state["cc"] = "accepted"
            return {"ok": True, "accepted": list(codes), "failed": [], "msg": "",
                    "reasons": []}
        T.accept_tasks = accept
        T.do_cat_travel = lambda acc: {"ok": True, "msg": "ok"}

    def test_auto_task_completed_and_claimed_before_accept(self):
        self._stub()
        res = T.run_growth_tasks(FakeAccount(), gap=0)
        seq = self.events
        first_claim = next(i for i, e in enumerate(seq) if e[0] == "claim")
        first_accept = next(i for i, e in enumerate(seq) if e[0] == "accept")
        self.assertLess(first_claim, first_accept, seq)
        self.assertEqual(seq[first_claim], ("claim", "first_buddy"))
        self.assertEqual(seq[first_accept], ("accept", ("create_canvas",)))
        self.assertGreaterEqual(res["earned_credit"], 300)

    def test_specs_updated(self):
        self.assertEqual(T.TASK_SPECS["first_buddy"]["kind"], "chat")
        self.assertEqual(T.TASK_SPECS["first_buddy"]["reward"], 300)
        self.assertTrue(T.TASK_SPECS["wb_wechat_oa_subscribe_task"]["unforgeable"])

    def test_travel_without_buddy_is_friendly(self):
        calls = []
        T.fetch_growth_tasks = lambda acc: []      # 没有 first_buddy，无法补齐前置

        def fake_urlopen(req, timeout=10, proxy=""):
            calls.append(req.full_url)
            if req.full_url.endswith("/travel/status"):
                return _Resp({"code": 0, "data": {"state": "idle", "buddy_id": 0}})
            if req.full_url.endswith("/travel/config"):
                return _Resp({"code": 0, "data": {"locations": [{"id": 1, "name": "咖啡馆"}]}})
            if req.full_url.endswith("/travel/depart"):
                raise _http_error(req.full_url, 400, '{"code":400,"msg":"no active buddy"}')
            return _Resp({"code": 0, "data": {}})
        orig = T._accounts.urlopen
        T._accounts.urlopen = fake_urlopen
        try:
            res = T.do_cat_travel(FakeAccount())
        finally:
            T._accounts.urlopen = orig
        self.assertFalse(res["ok"])
        self.assertIn("Buddy", res["msg"])
        self.assertEqual(sum(1 for u in calls if "/depart" in u), 1, calls)

    def test_travel_completes_first_buddy_and_retries(self):
        calls = []
        state = {"fb": "not_accepted", "departs": 0}

        def fetch(acc):
            return [_task("first_buddy", status=state["fb"],
                          current=1 if state["fb"] != "not_accepted" else 0,
                          target=1, task_type="auto")]
        T.fetch_growth_tasks = fetch

        def report(acc, evs, base=None):
            calls.append("report")
            state["fb"] = "completed"
            return True
        T.report_events = report

        def claim(acc, code):
            calls.append("claim:" + code)
            state["fb"] = "claimed"
            return {"ok": True, "credit": 300}
        T.claim_task = claim

        def fake_urlopen(req, timeout=10, proxy=""):
            calls.append(req.full_url)
            if req.full_url.endswith("/travel/status"):
                return _Resp({"code": 0, "data": {"state": "idle", "buddy_id": 0}})
            if req.full_url.endswith("/travel/config"):
                return _Resp({"code": 0, "data": {"locations": [{"id": 1, "name": "咖啡馆"}]}})
            if req.full_url.endswith("/travel/depart"):
                state["departs"] += 1
                if state["departs"] == 1:
                    raise _http_error(req.full_url, 400,
                                      '{"code":400,"msg":"no active buddy"}')
                return _Resp({"code": 0, "data": {"location": {"name": "咖啡馆"}}})
            return _Resp({"code": 0, "data": {}})
        orig = T._accounts.urlopen
        T._accounts.urlopen = fake_urlopen
        try:
            res = T.do_cat_travel(FakeAccount())
        finally:
            T._accounts.urlopen = orig
        self.assertTrue(res["ok"], res)
        self.assertIn("report", calls)
        self.assertIn("claim:first_buddy", calls)
        self.assertEqual(state["departs"], 2, calls)

    def test_travel_departs_directly_when_buddy_exists(self):
        calls = []
        # status 里 buddy_id 恒为 0，猫在家，不得因此误走补齐前置
        T.fetch_growth_tasks = lambda acc: (_ for _ in ()).throw(
            AssertionError("有 Buddy 时不该走补齐前置"))

        def fake_urlopen(req, timeout=10, proxy=""):
            calls.append(req.full_url)
            if req.full_url.endswith("/travel/status"):
                return _Resp({"code": 0, "data": {"state": "idle", "buddy_id": 0}})
            if req.full_url.endswith("/travel/config"):
                return _Resp({"code": 0, "data": {"locations": [{"id": 1, "name": "咖啡馆"}]}})
            if req.full_url.endswith("/travel/depart"):
                return _Resp({"code": 0, "data": {"location": {"name": "咖啡馆"}}})
            return _Resp({"code": 0, "data": {}})
        orig = T._accounts.urlopen
        T._accounts.urlopen = fake_urlopen
        try:
            res = T.do_cat_travel(FakeAccount())
        finally:
            T._accounts.urlopen = orig
        self.assertTrue(res["ok"], res)
        self.assertTrue(any("/depart" in u for u in calls), calls)

    def test_night_task_accepts_before_report(self):
        calls = []
        state = {"cur": 0}

        def fetch(acc):
            if state["cur"]:
                return [_task("black_cat", status="completed", current=3, target=3)]
            return [_task("black_cat", status="not_accepted", current=0, target=3)]
        T.query_growth_tasks = lambda acc: (True, fetch(acc))
        T.fetch_growth_tasks = fetch

        def accept(acc, codes, chunk=20):
            calls.append(("accept", tuple(codes)))
            return {"ok": True, "accepted": list(codes), "failed": [], "msg": "",
                    "reasons": []}
        T.accept_tasks = accept

        def report(acc, evs, base=None):
            calls.append(("report",))
            state["cur"] = 3
            return True
        T.report_events = report

        def claim(acc, code):
            calls.append(("claim", code))
            return {"ok": True, "credit": 100}
        T.claim_task = claim

        orig_night = T.in_night_window
        T.in_night_window = lambda: True
        try:
            res = T.run_night_growth(FakeAccount())
        finally:
            T.in_night_window = orig_night
        kinds = [c[0] for c in calls]
        self.assertEqual(kinds[:2], ["accept", "report"], calls)
        self.assertTrue(res["ok"])

    def test_fetch_exposes_task_type(self):
        def fake_urlopen(req, timeout=15, proxy=""):
            return _Resp({"code": 0, "data": {"tasks": [
                {"task_code": "first_buddy", "title": "领取一只 Buddy",
                 "task_type": "auto", "accept_status": "not_accepted", "progress": None},
            ]}})
        orig = T._accounts.urlopen
        T._accounts.urlopen = fake_urlopen
        try:
            tasks = T.fetch_growth_tasks(FakeAccount())
        finally:
            T._accounts.urlopen = orig
        self.assertEqual(tasks[0]["task_type"], "auto")


if __name__ == "__main__":
    unittest.main(verbosity=2)
