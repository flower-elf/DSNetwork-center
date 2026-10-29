"""用量流水记录本次请求实际使用的思考档位；不联网，设置与流水都写在临时目录。

档位解析顺序：目录固定档 > 关掉思考或 none > 客户端值 > 目录默认档；目录里没有 reasoning 元数据的模型与老流水行不带这个字段。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-effort-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_proxy as P
import wb_usage as U

MODEL = "deepseek-v4.1-flash"
#: 目录固定了这一档：reasoning.effort 有值、supportedEfforts 没有。
PINNED_MODEL = "fast-model"
#: 目录没有它的 reasoning 块，没有任何档位可报。
PLAIN_MODEL = "default-model"


def rows():
    if not os.path.exists(P.USAGE_LOG):
        return []
    with open(P.USAGE_LOG, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def chat(model, **extra):
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    payload.update(extra)
    return payload


USAGE = {"prompt_tokens": 20, "completion_tokens": 6, "reasoning_tokens": 3,
         "cached_tokens": 6, "total_tokens": 26}


class EffortResolutionTests(unittest.TestCase):
    def test_a_model_the_catalog_pins_always_runs_there(self):
        self.assertEqual(P.model_fixed_effort(PINNED_MODEL), "medium")
        self.assertIsNone(P.model_default_effort(PINNED_MODEL),
                          "固定档位的模型没有可挑的默认档")
        self.assertEqual(P.upstream_effort_of({}, PINNED_MODEL), "medium")
        self.assertEqual(P.upstream_effort_of({"reasoning_effort": "max"}, PINNED_MODEL),
                         "medium", "固定档位的模型不接受客户端挑档")
        self.assertEqual(P.upstream_effort_of({"thinking": {"type": "disabled"}}, PINNED_MODEL),
                         "medium")

    def test_the_effective_effort_reads_both_client_spellings(self):
        camel = P.build_upstream_body(chat(MODEL, reasoningEffort="max"))
        self.assertNotIn("reasoning_effort", camel,
                         "这个拼写不触发目录默认档注入,上游收到的是 camelCase 键")
        self.assertEqual(P.client_effort_of(camel), "max")
        self.assertEqual(P.upstream_effort_of(camel, MODEL), "max")

        snake = P.build_upstream_body(chat(MODEL, reasoning_effort="low"))
        self.assertEqual(P.upstream_effort_of(snake, MODEL), "low")

    def test_a_request_that_asks_for_nothing_falls_back_to_the_catalog_default(self):
        self.assertEqual(P.model_default_effort(MODEL), "high")
        self.assertEqual(P.upstream_effort_of(P.build_upstream_body(chat(MODEL)), MODEL), "high")
        self.assertEqual(P.upstream_effort_of({}, MODEL), "high",
                         "请求体里没有档位时写目录声明的默认档")

    def test_thinking_off_and_none_are_answers_not_gaps(self):
        for body in ({"thinking": {"type": "disabled"}}, {"reasoning_effort": "none"}):
            self.assertEqual(P.upstream_effort_of(body, MODEL), "none",
                             "关掉思考的请求不能被默认档填回: %r" % (body,))

    def test_a_model_without_reasoning_metadata_stays_unknown(self):
        self.assertEqual(P.model_reasoning_meta(PLAIN_MODEL), {})
        self.assertIsNone(P.upstream_effort_of({}, PLAIN_MODEL))


class UsageRowTests(unittest.TestCase):
    """流水行只在请求有档位时写这个字段。"""

    def setUp(self):
        if os.path.exists(P.USAGE_LOG):
            os.remove(P.USAGE_LOG)
        self.old_pool = P.POOL
        P.POOL = None
        self.addCleanup(self.restore)

    def restore(self):
        P.POOL = self.old_pool

    def test_the_row_carries_the_effort(self):
        U.record_usage(MODEL, USAGE, stream=True, elapsed_ms=1200, effort="high")
        row = rows()[-1]
        self.assertEqual(row.get("reasoning_effort"), "high")
        self.assertEqual(row.get("model"), MODEL)

    def test_a_request_without_an_effort_writes_no_field(self):
        U.record_usage(PLAIN_MODEL, USAGE, stream=False, elapsed_ms=900, effort=None)
        self.assertNotIn("reasoning_effort", rows()[-1])
        U.record_usage(PLAIN_MODEL, USAGE, stream=False, elapsed_ms=900)
        self.assertNotIn("reasoning_effort", rows()[-1])


class FakeResponse(object):
    def close(self):
        pass


class OpenUpstreamTests(unittest.TestCase):
    """真实 open_upstream() 把解析出的档位一并交给调用方。"""

    def setUp(self):
        self.pool = wb_accounts.AccountPool(os.path.join(_TMP, "pools"))
        self.pool.accounts = [wb_accounts.Account(
            {"uid": "uid-effort", "accessToken": "t", "realm": "intl"})]
        self.pool.smart_routing = False
        self.old_pool = P.POOL
        self.old_urlopen = wb_accounts.urlopen
        P.POOL = self.pool
        wb_accounts.urlopen = lambda req, timeout=None, proxy=None: FakeResponse()
        self.addCleanup(self.restore)

    def restore(self):
        P.POOL = self.old_pool
        wb_accounts.urlopen = self.old_urlopen

    def effort_of(self, payload):
        _resp, _account, effort = P.open_upstream(
            payload, session_key="s-effort", target_realm="intl", lease=P.SlotLease())
        return effort

    def test_the_camelcase_effort_comes_back(self):
        self.assertEqual(self.effort_of(chat(MODEL, reasoningEffort="max")), "max")

    def test_a_pinned_model_reports_its_level(self):
        self.assertIsNone(P.client_effort_of(P.build_upstream_body(chat(PINNED_MODEL))),
                          "固定档位的模型不需要网关往请求体里补档位")
        self.assertEqual(self.effort_of(chat(PINNED_MODEL)), "medium")

    def test_thinking_off_reports_none(self):
        self.assertEqual(self.effort_of(chat(MODEL, thinking={"type": "disabled"})), "none")

    def test_a_model_without_metadata_reports_nothing(self):
        self.assertIsNone(self.effort_of(chat(PLAIN_MODEL)))


ANSWER_CHUNKS = [
    {"choices": [{"index": 0, "delta": {"content": "Cats are "}, "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {"content": "fine."}, "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
     "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26}},
]


def sse_frames(chunks):
    frames = [("data: " + json.dumps(c) + "\n\n").encode("utf-8") for c in chunks]
    frames.append(b"data: [DONE]\n\n")
    return frames


class FakeUpstream(object):
    def __init__(self, frames):
        self.frames = frames

    def __iter__(self):
        return iter(self.frames)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        pass


class FakeAccount(object):
    uid = "acct-effort"

    def release_request(self, model=None):
        """账号并发名额：真实 Account 在响应体读完时释放，这个桩不需要计数。"""


class FakeHandler(object):
    """借来真实的调度、响应函数与判重方法，配一套最小的请求对象。"""

    _dispatch_chat_post = P.Handler._dispatch_chat_post
    _chat_stream_response = P.Handler._chat_stream_response
    _chat_nonstream_response = P.Handler._chat_nonstream_response
    _request_realm = P.Handler._request_realm
    _key_realm = P.Handler._key_realm
    _cross_realm_error = P.Handler._cross_realm_error
    _banned_model_error = P.Handler._banned_model_error
    _key_model_error = P.Handler._key_model_error
    _debug_account = P.Handler._debug_account

    def __init__(self):
        self.path = "/v1/chat/completions"
        self.headers = {}
        self.key_entry = None
        self.written = []
        self.json_body = None

    def _key_id(self):
        return None

    class _WFile(object):
        def __init__(self, sink):
            self.sink = sink

        def write(self, data):
            self.sink.append(data)

        def flush(self):
            pass

    @property
    def wfile(self):
        return FakeHandler._WFile(self.written)

    def send_response(self, code):
        self.written.append(("status", code))

    def send_header(self, name, value):
        self.written.append((name, value))

    def end_headers(self):
        pass

    def _json(self, code, obj):
        self.json_body = (code, obj)

    def _error(self, code, message, err_type="server_error"):
        raise AssertionError("网关拒绝了这个请求: %s %s" % (code, message))


class ChatRequestTests(unittest.TestCase):
    """走一遍真实的调度与响应函数：请求结束后，用量流水里能看到档位。"""

    def setUp(self):
        if os.path.exists(P.USAGE_LOG):
            os.remove(P.USAGE_LOG)
        self.old_pool = P.POOL
        P.POOL = None
        self.handler = FakeHandler()
        self.addCleanup(self.restore)

    def restore(self):
        P.POOL = self.old_pool

    def dispatch(self, stream, effort):
        upstream = FakeUpstream(sse_frames(ANSWER_CHUNKS))
        with mock.patch.object(P, "open_upstream",
                               lambda *a, **k: (upstream, FakeAccount(), effort)):
            self.handler._dispatch_chat_post(self.handler.path,
                                             chat(MODEL, stream=stream))

    def test_a_streaming_request_records_the_effort(self):
        self.dispatch(stream=True, effort="high")
        self.assertIn(b"data: [DONE]", b"".join(
            part for part in self.handler.written if isinstance(part, bytes)))
        row = rows()[-1]
        self.assertEqual(row.get("reasoning_effort"), "high")
        self.assertEqual(row.get("account"), "acct-effort")
        self.assertEqual(row.get("outcome"), "completed")
        self.assertEqual(row.get("stream"), True)

    def test_a_nonstreaming_request_records_the_effort(self):
        self.dispatch(stream=False, effort="low")
        self.assertEqual(self.handler.json_body[0], 200)
        row = rows()[-1]
        self.assertEqual(row.get("reasoning_effort"), "low")
        self.assertEqual(row.get("stream"), False)
        self.assertEqual(row.get("total_tokens"), 26)

    def test_a_request_without_an_effort_records_no_field(self):
        self.dispatch(stream=False, effort=None)
        self.assertNotIn("reasoning_effort", rows()[-1])


if __name__ == "__main__":
    unittest.main()
