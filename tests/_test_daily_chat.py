"""国际版每日活跃对话（签到）的单测。

这里断言两条通道的契约：桌面身份补全，以及为 issue #75/#59 增加的网页会话，后者
的形状不得偏离网页应用。
"""
import io
import json
import urllib.error, os, sys, unittest, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from _isolated_dirs import isolated_data_dirs  # noqa: E402  自带数据根目录
# 套件自用的目录，进程退出时随临时根一起删除，不在检出里留文件。
_TMP = isolated_data_dirs("wb-daily-chat-")

import wb_accounts, wb_settings

def _chat_response(req, **_kwargs):
    """桌面腿那次轻量补全的打桩：回一个空响应体。"""
    class Response(object):
        def read(self, *_args):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return Response()


class DailyChatTests(unittest.TestCase):
    def test_realm_eligibility_matrix(self):
        """按 realm 的资格矩阵：can_daily_chat() 谓词与 daily_chat() 入口结果一致。

        intl 当天没做过才可做，隔天恢复，入口真实执行一次；cn 不做每日对话，
        入口直接拒绝。
        """
        with self.subTest(realm="intl"):
            acc = wb_accounts.Account({
                "uid": "test_intl_1",
                "realm": "intl",
                "accessToken": "dummy",
                "lastDailyChat": None
            })
            self.assertTrue(acc.can_daily_chat())
            self.assertFalse(acc.can_checkin())  # 仅限国内版
            old_urlopen = wb_accounts.urlopen
            old_fetch = wb_accounts.Account.fetch_credits
            wb_accounts.urlopen = _chat_response
            wb_accounts.Account.fetch_credits = lambda self: {"ok": True}
            try:
                res = acc.daily_chat(web=False)
            finally:
                wb_accounts.urlopen = old_urlopen
                wb_accounts.Account.fetch_credits = old_fetch
            self.assertTrue(res.get("ok"), res)
            # 入口跑完即完成当天打卡，今天不能再打卡。
            self.assertFalse(acc.can_daily_chat())
            # 任意当天时间戳同样不放行。
            acc.last_daily_chat = time.strftime("%Y-%m-%d 10:00:00")
            self.assertFalse(acc.can_daily_chat())

            acc.last_daily_chat = "2020-01-01 10:00:00"
            self.assertTrue(acc.can_daily_chat())
        with self.subTest(realm="cn"):
            acc = wb_accounts.Account({
                "uid": "test_cn_1",
                "realm": "cn",
                "accessToken": "dummy"
            })
            self.assertFalse(acc.can_daily_chat())
            self.assertTrue(acc.can_checkin())
            self.assertFalse(acc.daily_chat().get("ok"))

class WebChannelTests(unittest.TestCase):
    UID = "c90a4e93-edad-42fe-aa3a-133fb27cf6b6"

    def account(self, realm="intl"):
        return wb_accounts.Account({"uid": self.UID, "realm": realm,
                                    "accessToken": "dummy-token", "lastDailyChat": None})

    def stub(self, payload=None, session=None):
        calls = []
        create = payload if payload is not None else {
            "code": 0, "msg": "OK", "data": {"id": "2102411494602919936"}}
        if session is None:
            session = {
                "code": 0, "msg": "OK",
                "data": {"link": "https://box.example/acp", "token": "sandbox-token",
                         "sessionId": "2102411494602919936", "cwd": "/workspace"}}

        class Response(object):
            def __init__(self, payload):
                self.payload = payload

            def read(self, *_args):
                return json.dumps(self.payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake(req, **_kwargs):
            calls.append(req)
            if req.full_url.endswith("/session"):
                return Response(session)
            if req.full_url.endswith("/conversations/"):
                return Response(create)
            return Response({"code": 0, "msg": "OK",
                            "data": {"status": "completed"}})

        return calls, fake

    def stub_turn(self, result=None, sequence=None):
        """把 ACP 驱动换成记录器，返回记录下来的调用。

        sequence 按轮次给出结果（最后一轮之后重复最后一个），用来测重试：例如
        [{"ok": False, "status": "working"}, {"ok": True}] 就是「先失败一次、第二次
        跑完」。result 则是覆盖每一轮的一次性结果。
        """
        seen = []
        real = wb_accounts.wb_webagent.run_turn
        pending = list(sequence or [])

        def fake(link, token, session_id, cwd, prompt, user_agent, **kwargs):
            seen.append({"link": link, "token": token, "session_id": session_id,
                         "cwd": cwd, "prompt": prompt,
                         "wait_seconds": kwargs.get("wait_seconds"),
                         "status": kwargs.get("poll_status")})
            out = {"ok": True, "status": "completed", "events": 5, "chunks": 2,
                   "elapsed_ms": 1234, "error": ""}
            if sequence:
                out.update(pending.pop(0) if len(pending) > 1 else pending[0])
            out.update(result or {})
            return out

        wb_accounts.wb_webagent.run_turn = fake
        self.addCleanup(setattr, wb_accounts.wb_webagent, "run_turn", real)
        return seen

    def test_web_conversation_request_shape(self):
        """用网页身份 POST /console/as/conversations/，不带 X-IDE-*。"""
        acc = self.account()
        calls, fake = self.stub()
        turns = self.stub_turn()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("conversation"), "2102411494602919936")
        req = calls[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, "https://www.workbuddy.ai/console/as/conversations/")
        headers = {k.lower(): v for k, v in req.headers.items()}
        self.assertEqual(headers.get("authorization"), "Bearer dummy-token")
        self.assertEqual(headers.get("x-user-id"), self.UID)
        self.assertEqual(headers.get("origin"), "https://www.workbuddy.ai")
        self.assertFalse([k for k in headers if k.startswith("x-ide")],
                         "the web channel must not carry desktop identity headers")
        body = json.loads(req.data.decode("utf-8"))
        self.assertTrue(body.get("prompt"))
        self.assertEqual(body.get("model"), wb_accounts.DAILY_CHAT_MODEL)
        self.assertEqual(body.get("conversationOrigin"), "workbuddy-app")
        # 建会话之后必须接沙箱、把这一轮跑起来，否则会话永远停在 CREATING。
        self.assertTrue(any(r.full_url.endswith("/session") for r in calls),
                        [r.full_url for r in calls])
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["link"], "https://box.example/acp")
        self.assertEqual(turns[0]["token"], "sandbox-token")
        self.assertEqual(turns[0]["session_id"], "2102411494602919936")
        self.assertEqual(turns[0]["cwd"], "/workspace")
        self.assertTrue(turns[0]["prompt"])
        self.assertEqual(res.get("status"), "completed")
        self.assertEqual(res.get("chunks"), 2)

    def test_web_conversation_reports_a_business_error(self):
        acc = self.account()
        _calls, fake = self.stub({"code": 12302, "msg": "activity is offline"})
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertFalse(res.get("ok"))
        self.assertIn("activity is offline", res.get("error", ""))

    def test_web_conversation_is_intl_only(self):
        res = self.account(realm="cn").daily_chat_web()
        self.assertFalse(res.get("ok"))
        self.assertIn("international", res.get("error", ""))

    def test_daily_chat_runs_the_web_step_when_asked(self):
        acc = self.account()
        calls, fake = self.stub()
        self.stub_turn()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat(web=True)
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res.get("web", {}).get("ok"), res)
        self.assertIn("网页通道", res.get("msg", ""))
        self.assertTrue(any(r.full_url.endswith("/console/as/conversations/") for r in calls),
                        [r.full_url for r in calls])

    def test_a_turn_that_never_finishes_is_reported(self):
        acc = self.account()
        calls, fake = self.stub()
        self.stub_turn({"ok": False, "status": "working", "chunks": 0,
                        "error": "会话在 120s 内没有跑完（状态=working）"})
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("conversation"), "2102411494602919936")
        self.assertIn("没有跑完", res.get("error", ""))

    def test_a_missing_sandbox_is_reported(self):
        acc = self.account()
        calls, fake = self.stub(session={"code": 0, "msg": "OK", "data": {}})
        self.stub_turn()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertFalse(res.get("ok"), res)
        self.assertIn("沙箱", res.get("error", ""))

    def test_daily_chat_skips_the_web_step_when_off(self):
        acc = self.account()
        calls, fake = self.stub()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat(web=False)
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertNotIn("web", res)
        self.assertFalse(any("/console/as/" in r.full_url for r in calls),
                         [r.full_url for r in calls])

    def test_a_failed_web_step_is_reported_and_leaves_the_day_open(self):
        """网页腿失败要如实报失败，也不能写 lastDailyChat：否则 30 积分静默丢掉。"""
        acc = self.account()
        _calls, fake = self.stub()
        self.stub_turn({"ok": False, "status": "working", "chunks": 0,
                        "error": "会话在 120s 内没有跑完（状态=working）"})
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat(web=True)
        finally:
            wb_accounts.urlopen = old
        self.assertFalse(res.get("ok"), res)         # 只有网页腿算数，它失败就是失败
        self.assertFalse(res.get("web", {}).get("ok"), res)
        self.assertIn("网页通道失败", res.get("msg", ""))
        self.assertIn("网页通道失败", res.get("error", ""))
        self.assertIsNone(acc.last_daily_chat)       # 今天不算做过
        self.assertTrue(acc.can_daily_chat())        # 下一次巡检还会重试

    def test_a_finished_web_step_marks_the_day_done(self):
        acc = self.account()
        _calls, fake = self.stub()
        self.stub_turn()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat(web=True)
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("web", {}).get("ok"), res)
        self.assertTrue(acc.last_daily_chat)
        self.assertFalse(acc.can_daily_chat())

    def test_the_web_step_being_off_still_marks_the_day_done(self):
        acc = self.account()
        _calls, fake = self.stub()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat(web=False)
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(acc.last_daily_chat)
        self.assertFalse(acc.can_daily_chat())

    # ---- 失败重试 ----------------------------------------------------------
    # 一轮「会话还没跑完」往往只是慢，不是坏了。重试默认开，一共最多尝试 N 次
    # （含第一轮），每轮的等待是上一轮的两倍：默认 3 次 = 120 / 240 / 480。

    def account_in(self, name, retry=None, timeout=None, attempts=None):
        """一个带自己 settings.json 的账号，方便按设置页的口径测重试策略。"""
        directory = os.path.join(_TMP.name, name)
        os.makedirs(directory, exist_ok=True)
        if retry is not None:
            wb_settings.set_daily_chat_web_retry(directory, retry)
        if timeout is not None:
            wb_settings.set_daily_chat_web_timeout(directory, timeout)
        if attempts is not None:
            wb_settings.set_daily_chat_web_attempts(directory, attempts)
        acc = wb_accounts.Account({"uid": self.UID, "realm": "intl",
                                   "accessToken": "dummy-token", "lastDailyChat": None})
        acc.path = os.path.join(directory, "acc-intl.json")
        return acc

    def test_a_slow_turn_is_retried_and_the_wait_doubles(self):
        acc = self.account_in("web-retry-on", retry=True)
        _calls, fake = self.stub()
        turns = self.stub_turn(sequence=[{"ok": False, "status": "working",
                                          "chunks": 0,
                                          "error": "会话在 120s 内没有跑完（状态=working）"},
                                         {"ok": True, "status": "completed"}])
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("attempts"), 2)
        self.assertEqual([t["wait_seconds"] for t in turns], [120, 240])

    def test_retries_stop_at_the_cap_by_default_and_when_enabled(self):
        """一直跑不完就试到上限：默认设置与显式开启都试 3 次、等待 120 / 240 / 480。"""
        cases = (
            ("默认设置", "web-retry-default", None, 3),
            ("显式重试开启", "web-retry-cap", True, wb_accounts.WEB_TURN_ATTEMPTS),
        )
        for label, name, retry, attempts in cases:
            with self.subTest(settings=label):
                acc = self.account_in(name, retry=retry)
                _calls, fake = self.stub()
                turns = self.stub_turn({"ok": False, "status": "working", "chunks": 0,
                                        "error": "会话在 120s 内没有跑完（状态=working）"})
                old = wb_accounts.urlopen
                wb_accounts.urlopen = fake
                try:
                    res = acc.daily_chat_web()
                finally:
                    wb_accounts.urlopen = old
                self.assertFalse(res.get("ok"), res)
                self.assertEqual(res.get("attempts"), attempts)
                self.assertEqual([t["wait_seconds"] for t in turns], [120, 240, 480])

    def test_a_business_rejection_is_not_retried(self):
        """建会话被业务拒绝（非 0 code）时重试没有意义，只跑一轮。"""
        acc = self.account_in("web-retry-reject", retry=True)
        calls, fake = self.stub({"code": 12302, "msg": "activity is offline"})
        turns = self.stub_turn()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("attempts"), 1)
        self.assertEqual(turns, [])
        self.assertEqual(len([r for r in calls
                              if r.full_url.endswith("/conversations/")]), 1)

    def test_a_transient_create_failure_is_retried(self):
        """建会话时 503 是瞬时失败，值得再试一次（与业务拒绝区分开）。"""
        acc = self.account_in("web-retry-503", retry=True)
        calls, fake = self.stub()
        turns = self.stub_turn()
        state = {"left": 1}
        real_fake = fake

        def flaky(req, **kwargs):
            if req.full_url.endswith("/conversations/") and state["left"] > 0:
                state["left"] -= 1
                raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b""))
            return real_fake(req, **kwargs)

        old = wb_accounts.urlopen
        wb_accounts.urlopen = flaky
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("attempts"), 2)
        self.assertEqual([t["wait_seconds"] for t in turns], [240])

    def test_the_attempt_count_is_configurable(self):
        """设置页改过的次数就是总次数（含第一轮）：5 次 = 120/240/480/960/1920。"""
        acc = self.account_in("web-attempts", retry=True, attempts=5)
        _calls, fake = self.stub()
        turns = self.stub_turn({"ok": False, "status": "working", "chunks": 0,
                                "error": "会话在 120s 内没有跑完（状态=working）"})
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertEqual(res.get("attempts"), 5)
        self.assertEqual([t["wait_seconds"] for t in turns], [120, 240, 480, 960, 1920])

    def test_one_attempt_means_one_try_when_capped_or_switched_off(self):
        """次数填 1 或关掉重试开关：两种口径都只跑一轮、等待 120。"""
        cases = (
            ("attempts=1", "web-attempts-one", dict(retry=True, attempts=1)),
            ("retry 关闭", "web-retry-off", dict(retry=False)),
        )
        for label, name, kwargs in cases:
            with self.subTest(settings=label):
                acc = self.account_in(name, **kwargs)
                _calls, fake = self.stub()
                turns = self.stub_turn({"ok": False, "status": "working", "chunks": 0,
                                        "error": "会话在 120s 内没有跑完（状态=working）"})
                old = wb_accounts.urlopen
                wb_accounts.urlopen = fake
                try:
                    res = acc.daily_chat_web()
                finally:
                    wb_accounts.urlopen = old
                self.assertFalse(res.get("ok"), res)
                self.assertEqual(res.get("attempts"), 1)
                self.assertEqual([t["wait_seconds"] for t in turns], [120])

    def test_the_configured_wait_is_the_first_attempt(self):
        """设置页改过的等待时间就是首轮的等待，重试从它翻倍。"""
        acc = self.account_in("web-timeout", retry=True, timeout=45)
        _calls, fake = self.stub()
        turns = self.stub_turn(sequence=[{"ok": False, "status": "working", "chunks": 0},
                                         {"ok": True, "status": "completed"}])
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            res = acc.daily_chat_web()
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(res.get("ok"), res)
        self.assertEqual([t["wait_seconds"] for t in turns], [45, 90])

    def test_the_env_timeout_survives_the_new_setting(self):
        """没在设置页改过时，WB_WEB_TURN_TIMEOUT 仍然说了算，不被 120 顶掉。"""
        acc = self.account_in("web-timeout-env")
        real = wb_accounts.WEB_TURN_TIMEOUT
        wb_accounts.WEB_TURN_TIMEOUT = 77
        try:
            self.assertEqual(acc.daily_chat_web_policy()[0], 77)
        finally:
            wb_accounts.WEB_TURN_TIMEOUT = real
        # 设置页改过就以设置页的为准。
        wb_settings.set_daily_chat_web_timeout(os.path.dirname(acc.path), 45)
        self.assertEqual(acc.daily_chat_web_policy()[0], 45)

    def test_the_retry_settings_default_and_round_trip(self):
        directory = os.path.join(_TMP.name, "web-retry-settings")
        # 默认：重试开着、一共 3 次、首轮等待 120 秒。
        self.assertTrue(wb_settings.daily_chat_web_retry(directory))
        self.assertEqual(wb_settings.daily_chat_web_attempts(directory),
                         wb_settings.DAILY_CHAT_WEB_ATTEMPTS_DEFAULT)
        self.assertEqual(wb_settings.daily_chat_web_timeout(directory),
                         wb_settings.DAILY_CHAT_WEB_TIMEOUT_DEFAULT)
        self.assertFalse(wb_settings.set_daily_chat_web_retry(directory, False))
        self.assertFalse(wb_settings.daily_chat_web_retry(directory))
        self.assertEqual(wb_settings.set_daily_chat_web_attempts(directory, 5), 5)
        self.assertEqual(wb_settings.daily_chat_web_attempts(directory), 5)
        self.assertEqual(wb_settings.set_daily_chat_web_timeout(directory, 300), 300)
        self.assertEqual(wb_settings.daily_chat_web_timeout(directory), 300)
        # 越界/非法值一律回落到默认，不让手改的 settings.json 把巡检拖住。
        for bad in (0, -5, wb_settings.DAILY_CHAT_WEB_TIMEOUT_MAX + 1, "120", True):
            with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
                json.dump({"daily_chat_web_timeout": bad}, fh)
            self.assertEqual(wb_settings.daily_chat_web_timeout(directory),
                             wb_settings.DAILY_CHAT_WEB_TIMEOUT_DEFAULT, bad)
        for bad in (0, -1, wb_settings.DAILY_CHAT_WEB_ATTEMPTS_MAX + 1, "3", True):
            with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
                json.dump({"daily_chat_web_attempts": bad}, fh)
            self.assertEqual(wb_settings.daily_chat_web_attempts(directory),
                             wb_settings.DAILY_CHAT_WEB_ATTEMPTS_DEFAULT, bad)
        with self.assertRaises(ValueError):
            wb_settings.set_daily_chat_web_timeout(directory, 0)
        with self.assertRaises(ValueError):
            wb_settings.set_daily_chat_web_timeout(directory, "120")
        with self.assertRaises(ValueError):
            wb_settings.set_daily_chat_web_attempts(directory, 0)
        with self.assertRaises(ValueError):
            wb_settings.set_daily_chat_web_attempts(directory, "3")

    def test_the_web_toggle_defaults_on_and_round_trips(self):
        directory = os.path.join(_TMP.name, "web-toggle")
        self.assertTrue(wb_settings.daily_chat_web(directory))
        self.assertFalse(wb_settings.set_daily_chat_web(directory, False))
        self.assertFalse(wb_settings.daily_chat_web(directory))
        self.assertTrue(wb_settings.set_daily_chat_web(directory, True))
        self.assertTrue(wb_settings.daily_chat_web(directory))
        with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump({"daily_chat_web": "false"}, fh)
        self.assertFalse(wb_settings.daily_chat_web(directory))





class CnCheckinTests(unittest.TestCase):
    def test_already_checked_in_is_treated_as_success(self):
        acc = wb_accounts.Account({"uid": "u-test-cn", "realm": "cn", "accessToken": "token"})
        def fake_http_json(url, data, method, headers, timeout, proxy=""):
            err_body = json.dumps({"code": 10001, "msg": "今天已签到，请明天再来"}).encode("utf-8")
            fp = io.BytesIO(err_body)
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, fp)

        real_http_json = wb_accounts.http_json
        wb_accounts.http_json = fake_http_json
        try:
            res = acc._checkin_upstream()
            self.assertTrue(res.get("ok"), res)
            self.assertEqual(res.get("code"), 10001)
            self.assertIn("今天已签到", res.get("msg"))
            self.assertTrue(acc.last_checkin)
        finally:
            wb_accounts.http_json = real_http_json



    def test_daily_chat_model_falls_back_when_preferred_is_cooling(self):
        acc = wb_accounts.Account({"uid": "u-fallback", "access_token": "t-fb", "realm": "intl"})
        acc.model_cooldowns["deepseek-v4.1-flash"] = time.time() + 600
        picked = acc.daily_chat_model()
        self.assertNotEqual(picked, "deepseek-v4.1-flash")
        self.assertIn(picked, ["hy3", "hy4-preview-f", "hy4-preview", "glm-5.3-flash"])


if __name__ == "__main__":
    unittest.main()
