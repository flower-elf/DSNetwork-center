"""上游风控限制 403（code 11140）必须停掉整个网关，而不是换号。

实测正文：{"code":11140,"msg":"request illegal",...,"displayMsg":{"en":"Your
account has been restricted due to unusual activity","zh":"账号存在异常行为，
已被限制使用"}}。限制发生后继续轮询下一个账号，正是把整个账号池关联起来的原因。

要钉住的行为：

  1. 风控 403 停用全部账号，并向调用方抛 GatewayStandDown（映射为 503）；
  2. 停止期间新请求在入口被拒绝，不发往上游；
  3. 内容审核 403 的原路径不变（降级重试、透传，不给账号上长冷却）；
  4. 一切 >=500 的未知状态码（实测 550）按瞬时故障轮转。

Run with the current interpreter (python tests/run_all.py restricted).
"""
import email
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="wb-restrict403-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_proxy as P


RESTRICT_BODY = json.dumps({
    "code": 11140, "msg": "request illegal", "requestId": "1" * 32,
    "displayMsg": {
        "en": "Your account has been restricted due to unusual activity",
        "zh": "账号存在异常行为，已被限制使用",
    },
}).encode("utf-8")

CONTENT_BODY = json.dumps({
    "code": 10001, "msg": "content rejected",
}).encode("utf-8")


def http_error(code, body):
    return urllib.error.HTTPError("https://upstream/chat", code, "err", {},
                                  io.BytesIO(body))


class FakeResp(object):
    pass


def make_account(uid):
    return wb_accounts.Account({"uid": uid, "accessToken": "t-" + uid,
                                "realm": "intl"})


class StubPool(object):
    """pick_for_session 走真实的 ready() 语义：冷却中的账号不再被选中。"""

    def __init__(self, accounts):
        self.accounts = accounts
        self.affinity = wb_accounts.SessionAffinity()

    def count_ready(self, realm, model=None):
        return sum(1 for a in self.accounts
                   if a.realm == realm and a.ready(model=model))

    def pick_for_session(self, realm=None, session_key=None, exclude=(),
                         model=None, page=None, claim=None):
        for a in self.accounts:
            if a.uid in exclude:
                continue
            if realm and a.realm != realm:
                continue
            if a.ready(model=model):
                return a
        return None

    def apply_remaining_weights(self, weights=None):
        return None

    def apply_daily_token_limit(self, value=None, usage=None):
        return value or 0

    def apply_daily_credit_limit(self, value=None, credits=None, free_models=None):
        return value or 0

    def apply_model_daily_token_limit(self, value=None, per_model=None):
        return value or 0

    def set_all_enabled(self, enabled, realm=None):
        for account in self.accounts:
            if realm and account.realm != realm:
                continue
            account.enabled = bool(enabled)


class ScriptedUpstream(object):
    """按尝试顺序给出默认结果：异常抛出、FakeResp 返回。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout=None, proxy=None):
        self.calls.append(req)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


PAYLOAD = {"model": "deepseek-v4.1-flash",
           "messages": [{"role": "user", "content": "hi"}]}


class Restricted403DetectionTests(unittest.TestCase):
    def test_real_restriction_body_matches(self):
        self.assertTrue(P.restriction_is_account_level(RESTRICT_BODY.decode("utf-8")))
        # 400 字节截断（旧读法）也能命中 code 与 en 措辞。
        self.assertTrue(P.restriction_is_account_level(
            RESTRICT_BODY.decode("utf-8")[:400]))
        # code 写成字符串、带空格的变体。
        self.assertTrue(P.restriction_is_account_level('{"code": "11140"}'))

    def test_content_and_junk_bodies_do_not_match(self):
        self.assertFalse(P.restriction_is_account_level(CONTENT_BODY.decode("utf-8")))
        self.assertFalse(P.restriction_is_account_level(""))
        self.assertFalse(P.restriction_is_account_level(None))
        # hex requestId 里可能出现 "11140" 子串，裸字符串不算数。
        self.assertFalse(P.restriction_is_account_level(
            '{"msg":"x","requestId":"11140abcdef"}'))


class Restricted403DispatchTests(unittest.TestCase):
    def setUp(self):
        self.old_pool = P.POOL
        P.clear_stand_down()

    def tearDown(self):
        P.POOL = self.old_pool
        P.clear_stand_down()

    def _run(self, accounts, script):
        P.POOL = StubPool(accounts)
        upstream = ScriptedUpstream(script)
        real_urlopen = wb_accounts.urlopen
        wb_accounts.urlopen = upstream
        try:
            result = P.open_upstream(dict(PAYLOAD), target_realm="intl")
            raised = None
        except Exception as exc:
            result = None
            raised = exc
        finally:
            wb_accounts.urlopen = real_urlopen
        return result, raised

    def test_restricted_account_stops_every_account(self):
        restricted = make_account("r1")
        healthy = make_account("h1")
        resp, raised = self._run(
            [restricted, healthy],
            [http_error(403, RESTRICT_BODY)])
        # 脚本只给了一次应答：若还去轮询下一个账号，会因脚本耗尽而报别的错。
        self.assertIsNone(resp)
        self.assertIsInstance(raised, P.GatewayStandDown)
        self.assertEqual(getattr(raised, "account_uid", None), "r1")
        self.assertIn("11140", raised.detail)
        # 所有账号一起停用：下一个账号不会再被选中。
        self.assertFalse(restricted.enabled)
        self.assertFalse(healthy.enabled)
        state = P.stand_down_state()
        self.assertTrue(state["active"])
        self.assertEqual(state["uid"], "r1")
        # 入口也挡住：停止期间不再向上游发请求。
        with self.assertRaises(P.GatewayStandDown):
            P.open_upstream(dict(PAYLOAD), target_realm="intl")

    def test_lone_restricted_account_stops_the_gateway(self):
        restricted = make_account("r-only")
        resp, raised = self._run([restricted], [http_error(403, RESTRICT_BODY)])
        self.assertIsNone(resp)
        self.assertIsInstance(raised, P.GatewayStandDown)
        self.assertEqual(getattr(raised, "account_uid", None), "r-only")
        self.assertIn("11140", raised.detail)
        self.assertFalse(restricted.enabled)

    def test_content_403_keeps_its_old_path(self):
        a = make_account("c1")
        b = make_account("c2")
        resp, raised = self._run(
            [a, b],
            [http_error(403, CONTENT_BODY), http_error(403, CONTENT_BODY)])
        # 内容审核 403 不给账号上长冷却（这不是账号的坏）。
        self.assertEqual(a.cooldown_until, 0.0)
        self.assertEqual(b.cooldown_until, 0.0)
        self.assertIsNone(resp)
        self.assertIsInstance(raised, P.ContentRejected)

    def test_unknown_5xx_rotates_instead_of_passing_through(self):
        flaky = make_account("f1")
        healthy = make_account("f2")
        # 同一账号先原地重试两次（保留前缀缓存），预算用完才换号。
        resp, raised = self._run(
            [flaky, healthy],
            [http_error(550, b"")] * 3 + [FakeResp()])
        self.assertIsNone(raised)
        self.assertEqual(resp[1].uid, "f2")
        self.assertEqual(flaky.fails, 1)


class DispatchRequest(P.Handler):
    """把真实 Handler 放在内存 socket 上跑：停止期间必须是 503。"""

    def __init__(self, path, command="POST", body=b""):
        self.path = path
        self.command = command
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        self.headers = email.message_from_string("Content-Type: application/json\r\n")
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        pass

    def dispatch(self):
        getattr(self, "do_" + self.command)()
        raw = self.wfile.getvalue().decode("utf-8")
        return self.status, json.loads(raw) if raw else None


class StandDownDispatchTests(unittest.TestCase):
    def setUp(self):
        self.old_pool = P.POOL
        P.clear_stand_down()
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-standdown-")
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        P.POOL = self.old_pool
        P.clear_stand_down()

    def test_chat_requests_answer_503_while_stopped(self):
        P.POOL = StubPool([make_account("r1")])
        P.stand_down_accounts("r1", '"code":11140')
        body = json.dumps(PAYLOAD).encode("utf-8")
        with mock.patch.multiple(P, ACCOUNTS_DIR=self._tmp.name, API_KEY=None):
            status, payload = DispatchRequest("/v1/chat/completions", body=body).dispatch()
        self.assertEqual(status, 503)
        message = payload["error"]["message"]
        self.assertIn("全部账号已停用", message)
        self.assertIn("全部启用", message)

    def test_re_enabling_clears_the_stop(self):
        P.stand_down_accounts("r1", "")
        self.assertTrue(P.stand_down_state()["active"])
        P.clear_stand_down()
        self.assertFalse(P.stand_down_state()["active"])


if __name__ == "__main__":
    unittest.main()
