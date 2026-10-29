"""流式转发与整段请求的完整性：三种协议各有收尾事件，上游截断时换成对应错误事件，整段请求回 502。

已发出的内容不重发，心跳注释照原样转发；全部离线，上游是可迭代的假对象，Handler 是只带真方法的桩。
"""

import http.client
import json
import os
import sys
import tempfile
import time
import unittest

from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-stream-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy as proxy
import wb_usage as U
import wb_translate as T
import wb_messages_responses

MODEL = "deepseek-v4.1-flash"


def data_lines(*frames):
    return [("data: " + json.dumps(f) + "\n\n").encode("utf-8") for f in frames]


def text_delta(piece):
    return {"choices": [{"delta": {"content": piece}}]}


def finish(reason="stop"):
    return {"choices": [{"delta": {}, "finish_reason": reason}]}


class FakeUpstream(object):
    """上游响应体：一列原始 SSE 行，可以一个感叹号收尾。"""

    def __init__(self, lines, raises=None):
        self.lines = lines
        self.raises = raises
        self.closed = False

    def __iter__(self):
        for line in self.lines:
            yield line
        if self.raises is not None:
            raise self.raises

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeAccount(object):
    uid = "0123456789abcdef"


class HandlerStub(object):
    """真实 Handler 的最小骨架，离线驱动各转发路径。"""

    _chat_stream_response = proxy.Handler._chat_stream_response
    _chat_nonstream_response = proxy.Handler._chat_nonstream_response
    _responses_stream_response = proxy.Handler._responses_stream_response
    _responses_nonstream_response = proxy.Handler._responses_nonstream_response
    _messages_stream_response = proxy.Handler._messages_stream_response

    def _key_id(self):
        return None

    def _messages_stream_from_responses(self, upstream, model, custom_names, request_meta,
                                        chat_req, namespace_map, session_meta, account,
                                        t_start, effort=None, lease=None):
        return wb_messages_responses._messages_stream(
            self, upstream, model, custom_names, request_meta, chat_req,
            namespace_map, None, account, t_start, None, effort)

    class _WFile(object):
        def __init__(self, sink):
            self.sink = sink

        def write(self, data):
            self.sink.append(data)

        def flush(self):
            pass

    def __init__(self, path="/v1/chat/completions"):
        self.path = path
        self.headers = {}
        self.status = []
        self.written = []
        self.reply = None

    @property
    def wfile(self):
        return HandlerStub._WFile(self.written)

    def send_response(self, code):
        self.status.append(("status", code))

    def send_header(self, name, value):
        self.status.append((name, value))

    def end_headers(self):
        self.status.append(("end", None))

    def _json(self, code, obj):
        self.status.append(("status", code))
        self.written.append(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        self.reply = obj
        return code, obj

    def _error(self, code, message, err_type="server_error"):
        return self._json(code, {"error": {"message": message, "type": err_type}})

    def _request_realm(self, explicit=None):
        return None

    def body(self):
        return b"".join(self.written).decode("utf-8", "replace")

    def events(self):
        """(event, payload-text) 对，按写入顺序排列。"""
        out = []
        for block in self.body().split("\n\n"):
            if not block.strip():
                continue
            event, payload = "", None
            for line in block.split("\n"):
                if line.startswith("event: "):
                    event = line[7:].strip()
                elif line.startswith("data: "):
                    payload = line[6:]
            out.append((event, payload))
        return out

    def comments(self):
        """SSE 注释行，也就是转发的上游心跳。"""
        return [line for line in self.body().split("\n") if line.startswith(":")]


def run(method, upstream, *args, **kwargs):
    """在桩 handler 上调用一条转发路径，拦截计费与错误调用。"""
    handler = HandlerStub()
    usage_rows, error_rows = [], []

    def capture_usage(model, usage=None, outcome="completed", **kw):
        usage_rows.append(dict(kw, model=model, usage=usage, outcome=outcome))

    def capture_error(model, status=None, message=None, **kw):
        error_rows.append(dict(kw, model=model, status=status, message=message))

    with mock.patch.multiple(U, record_usage=capture_usage, record_error=capture_error):
        getattr(handler, method)(upstream, *args, **kwargs)
    return handler, usage_rows, error_rows


CHAT_ARGS = (MODEL, None, FakeAccount(), time.time())
CHAT_REQ = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}


class HeartbeatTests(unittest.TestCase):
    """上游心跳的识别与转发。"""

    def test_bare_and_wrapped_comments_are_recognised(self):
        self.assertEqual(proxy.heartbeat_frame(b": ping\n"), b": ping\n\n")
        self.assertEqual(proxy.heartbeat_frame(b"data: : heartbeat\n"), b": heartbeat\n\n")
        self.assertEqual(proxy.heartbeat_frame(b"data:: heartbeat\n"), b": heartbeat\n\n")

    def test_data_lines_and_blanks_are_not_comments(self):
        self.assertIsNone(proxy.heartbeat_frame(b"\n"))
        self.assertIsNone(proxy.heartbeat_frame(b'data: {"a": 1}\n'))
        self.assertIsNone(proxy.heartbeat_frame(b"data: [DONE]\n"))

    def test_chat_relay_forwards_heartbeats(self):
        upstream = FakeUpstream([b": ping\n", b"\n"] + data_lines(text_delta("hi"), finish())
                                + [b"data: : heartbeat\n", b"data: [DONE]\n"])
        handler, usage, errors = run("_chat_stream_response", upstream, *CHAT_ARGS)
        self.assertEqual(handler.comments(), [": ping", ": heartbeat"])
        self.assertTrue(handler.body().rstrip().endswith("data: [DONE]"))
        self.assertFalse(errors)
        self.assertEqual(usage[0]["outcome"], "completed")

    def test_responses_relay_forwards_heartbeats(self):
        upstream = FakeUpstream([b": ping\n"] + data_lines(text_delta("hi"))
                                + [b"data: [DONE]\n"])
        handler, drop, errors = run("_responses_stream_response", upstream, MODEL, set(), {},
                                    None, FakeAccount(), time.time(), lease=proxy.SlotLease())
        self.assertEqual(handler.comments(), [": ping"])
        self.assertEqual(handler.events()[-1][0], "response.completed")
        self.assertFalse(errors)

    def test_messages_relay_forwards_heartbeats(self):
        upstream = FakeUpstream([b": ping\n"] + data_lines(text_delta("hi"), finish())
                                + [b"data: [DONE]\n"])
        handler, drop, errors = run("_messages_stream_response", upstream, MODEL, None,
                                    FakeAccount(), time.time(), base_body=CHAT_REQ)
        self.assertEqual(handler.comments(), [": ping"])
        self.assertEqual(handler.events()[-1][0], "message_stop")
        self.assertFalse(errors)


class ChatCompletionsStreamTests(unittest.TestCase):
    """Chat Completions 流式：结束标记、错误事件与提前 EOF。"""

    def test_truncated_stream_ends_in_an_error_event(self):
        upstream = FakeUpstream(data_lines(text_delta("half an answer")))
        handler, usage, errors = run("_chat_stream_response", upstream, *CHAT_ARGS)
        body = handler.body()
        self.assertIn("half an answer", body)
        self.assertNotIn("data: [DONE]", body)
        event, payload = handler.events()[-1]
        self.assertEqual(event, "")
        self.assertIn("terminal event", json.loads(payload)["error"]["message"])
        self.assertEqual(errors[0]["status"], 502)
        self.assertEqual(errors[0]["outcome"], "upstream_aborted")
        self.assertFalse(usage)

    def test_finish_reason_without_done_counts_as_complete(self):
        upstream = FakeUpstream(data_lines(text_delta("done"), finish()))
        handler, usage, errors = run("_chat_stream_response", upstream, *CHAT_ARGS)
        self.assertTrue(handler.body().rstrip().endswith("data: [DONE]"))
        self.assertFalse(errors)
        self.assertEqual(usage[0]["outcome"], "completed")

    def test_upstream_error_frame_becomes_an_error_event(self):
        upstream = FakeUpstream(data_lines(text_delta("half"), {"error": {"message": "boom"}}))
        handler, usage, errors = run("_chat_stream_response", upstream, *CHAT_ARGS)
        self.assertNotIn("data: [DONE]", handler.body())
        message = json.loads(handler.events()[-1][1])["error"]["message"]
        self.assertIn("boom", message)
        self.assertEqual(errors[0]["status"], 502)

    def test_gateway_error_envelope_is_recognised(self):
        upstream = FakeUpstream(data_lines({"code": 6004, "msg": "usage exceeds frequency limit"}))
        handler, usage, errors = run("_chat_stream_response", upstream, *CHAT_ARGS)
        message = json.loads(handler.events()[-1][1])["error"]["message"]
        self.assertIn("6004", message)
        self.assertIn("usage exceeds frequency limit", message)
        self.assertEqual(errors[0]["outcome"], "upstream_aborted")

    def test_read_exception_becomes_an_error_event(self):
        upstream = FakeUpstream(data_lines(text_delta("half")),
                                raises=http.client.IncompleteRead(b"x", 5))
        handler, usage, errors = run("_chat_stream_response", upstream, *CHAT_ARGS)
        self.assertNotIn("data: [DONE]", handler.body())
        self.assertIn("stream aborted", json.loads(handler.events()[-1][1])["error"]["message"])
        self.assertEqual(errors[0]["status"], 502)

    def test_empty_stream_keeps_its_error_event(self):
        upstream = FakeUpstream([b"data: [DONE]\n"])
        handler, usage, errors = run("_chat_stream_response", upstream, *CHAT_ARGS)
        payloads = [json.loads(p) for e, p in handler.events() if e == "" and p != "[DONE]"]
        self.assertIn("empty upstream stream", payloads[-1]["error"]["message"])
        self.assertTrue(handler.body().rstrip().endswith("data: [DONE]"))
        self.assertFalse(errors)

    def test_client_hangup_is_not_an_upstream_failure(self):
        upstream = FakeUpstream(data_lines(text_delta("half")), raises=BrokenPipeError())
        handler, usage, errors = run("_chat_stream_response", upstream, *CHAT_ARGS)
        self.assertFalse(errors)
        self.assertEqual(usage[0]["outcome"], "client_aborted")


class ChatCompletionsNonstreamTests(unittest.TestCase):
    """整段请求：上游截断时回 502。"""

    def test_truncated_stream_answers_502(self):
        upstream = FakeUpstream(data_lines(text_delta("half")))
        handler, usage, errors = run("_chat_nonstream_response", upstream, *CHAT_ARGS)
        self.assertIn(("status", 502), handler.status)
        self.assertIn("terminal event", handler.reply["error"]["message"])
        self.assertEqual(errors[0]["status"], 502)

    def test_error_frame_answers_502(self):
        upstream = FakeUpstream(data_lines({"choices": [{"delta": {"content": "x"}}]},
                                           {"code": 11140, "msg": "content rejected"}))
        handler, usage, errors = run("_chat_nonstream_response", upstream, *CHAT_ARGS)
        self.assertIn(("status", 502), handler.status)
        self.assertIn("11140", handler.reply["error"]["message"])

    def test_complete_stream_still_answers_200(self):
        upstream = FakeUpstream(data_lines(text_delta("hi"), finish()) + [b"data: [DONE]\n"])
        handler, usage, errors = run("_chat_nonstream_response", upstream, *CHAT_ARGS)
        self.assertIn(("status", 200), handler.status)
        self.assertEqual(handler.reply["choices"][0]["message"]["content"], "hi")
        self.assertFalse(errors)


class ResponsesStreamTests(unittest.TestCase):
    """Responses 流式：截断时以 response.failed 收尾。"""

    def test_truncated_stream_fails(self):
        upstream = FakeUpstream(data_lines(text_delta("half an answer")))
        handler, usage, errors = run("_responses_stream_response", upstream, MODEL, set(), {},
                                     None, FakeAccount(), time.time(), lease=proxy.SlotLease())
        events = handler.events()
        self.assertEqual(events[-1][0], "response.failed")
        self.assertNotIn("response.completed", [e for e, _ in events])
        failed = json.loads(events[-1][1])["response"]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("terminal event", failed["error"]["message"])
        self.assertEqual(errors[0]["status"], 502)
        self.assertEqual(errors[0]["outcome"], "upstream_aborted")

    def test_error_frame_fails_the_response(self):
        upstream = FakeUpstream(data_lines(text_delta("half"), {"error": {"message": "boom"}}))
        handler, usage, errors = run("_responses_stream_response", upstream, MODEL, set(), {},
                                     None, FakeAccount(), time.time(), lease=proxy.SlotLease())
        failed = json.loads(handler.events()[-1][1])["response"]
        self.assertIn("boom", failed["error"]["message"])
        self.assertEqual(errors[0]["status"], 502)

    def test_read_exception_fails_the_response(self):
        upstream = FakeUpstream(data_lines(text_delta("half")),
                                raises=http.client.IncompleteRead(b"x", 5))
        handler, usage, errors = run("_responses_stream_response", upstream, MODEL, set(), {},
                                     None, FakeAccount(), time.time(), lease=proxy.SlotLease())
        self.assertEqual(handler.events()[-1][0], "response.failed")
        self.assertIn("stream aborted", json.loads(handler.events()[-1][1])["response"]["error"]["message"])
        self.assertEqual(errors[0]["status"], 502)

    def test_complete_stream_still_completes(self):
        upstream = FakeUpstream(data_lines(text_delta("hi"), finish()) + [b"data: [DONE]\n"])
        handler, usage, errors = run("_responses_stream_response", upstream, MODEL, set(), {},
                                     None, FakeAccount(), time.time(), lease=proxy.SlotLease())
        self.assertEqual(handler.events()[-1][0], "response.completed")
        self.assertFalse(errors)
        self.assertEqual(usage[0]["outcome"], "completed")


class ResponsesNonstreamRecordingTests(unittest.TestCase):
    """整段 Responses 请求也要记下首字时间和生成耗时，吞吐才算得出来。"""

    def test_record_carries_ttft_and_generation_time(self):
        class PacedUpstream(FakeUpstream):
            def __iter__(self):
                for index, line in enumerate(self.lines):
                    if index == 1:
                        time.sleep(0.02)
                    yield line

        upstream = PacedUpstream(data_lines(
            text_delta("hi"),
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4,
                                      "total_tokens": 14}},
            finish()) + [b"data: [DONE]\n"])
        handler = HandlerStub()
        handler._responses_nonstream_response(upstream, MODEL, None, {}, None,
                                              FakeAccount(), time.time())
        self.assertIn(("status", 200), handler.status)
        with open(proxy.USAGE_LOG, encoding="utf-8") as fh:
            row = json.loads([line for line in fh if line.strip()][-1])
        self.assertIsNotNone(row.get("ttft_ms"), row)
        self.assertIsNotNone(row.get("gen_ms"), row)
        self.assertIsInstance(row.get("tokens_per_sec"), float, row)


class MessagesStreamTests(unittest.TestCase):
    """Messages 两条管线：截断时发 error 事件，不发 message_stop。"""

    def test_chat_pipeline_error_event(self):
        upstream = FakeUpstream(data_lines(text_delta("half")))
        handler, usage, errors = run("_messages_stream_response", upstream, MODEL, None,
                                     FakeAccount(), time.time(), base_body=CHAT_REQ)
        events = handler.events()
        self.assertEqual(events[-1][0], "error")
        self.assertNotIn("message_stop", [e for e, _ in events])
        self.assertIn("terminal event", json.loads(events[-1][1])["error"]["message"])
        self.assertEqual(errors[0]["status"], 502)
        self.assertEqual(errors[0]["outcome"], "upstream_aborted")

    def test_chat_pipeline_upstream_error_frame(self):
        upstream = FakeUpstream(data_lines(text_delta("half"), {"error": {"message": "boom"}}))
        handler, usage, errors = run("_messages_stream_response", upstream, MODEL, None,
                                     FakeAccount(), time.time(), base_body=CHAT_REQ)
        self.assertEqual(handler.events()[-1][0], "error")
        self.assertIn("boom", json.loads(handler.events()[-1][1])["error"]["message"])
        self.assertEqual(errors[0]["status"], 502)

    def test_chat_pipeline_read_exception(self):
        upstream = FakeUpstream(data_lines(text_delta("half")),
                                raises=http.client.IncompleteRead(b"x", 5))
        handler, usage, errors = run("_messages_stream_response", upstream, MODEL, None,
                                     FakeAccount(), time.time(), base_body=CHAT_REQ)
        self.assertEqual(handler.events()[-1][0], "error")
        self.assertIn("stream aborted", json.loads(handler.events()[-1][1])["error"]["message"])
        self.assertEqual(errors[0]["status"], 502)

    def test_chat_pipeline_complete_stream(self):
        upstream = FakeUpstream(data_lines(text_delta("hi"), finish()) + [b"data: [DONE]\n"])
        handler, usage, errors = run("_messages_stream_response", upstream, MODEL, None,
                                     FakeAccount(), time.time(), base_body=CHAT_REQ)
        self.assertEqual(handler.events()[-1][0], "message_stop")
        self.assertFalse(errors)
        self.assertEqual(usage[0]["outcome"], "completed")

    def test_responses_pipeline_error_event(self):
        upstream = FakeUpstream(data_lines(text_delta("half")))
        handler, usage, errors = run("_messages_stream_from_responses", upstream, MODEL, set(), {},
                                     CHAT_REQ, None, None, FakeAccount(), time.time(), None,
                                     lease=proxy.SlotLease())
        events = handler.events()
        self.assertEqual(events[-1][0], "error")
        self.assertNotIn("message_stop", [e for e, _ in events])
        self.assertEqual(errors[0]["status"], 502)

    def test_responses_pipeline_complete_stream(self):
        upstream = FakeUpstream(data_lines(text_delta("hi"), finish()) + [b"data: [DONE]\n"])
        handler, usage, errors = run("_messages_stream_from_responses", upstream, MODEL, set(), {},
                                     CHAT_REQ, None, None, FakeAccount(), time.time(), None,
                                     lease=proxy.SlotLease())
        self.assertEqual(handler.events()[-1][0], "message_stop")
        self.assertFalse(errors)


class AggregateStreamTests(unittest.TestCase):
    """整段折叠：错误帧与缺少结束标记都要抛出来。"""

    def test_complete_stream_folds(self):
        upstream = FakeUpstream(data_lines(text_delta("hi"), finish()) + [b"data: [DONE]\n"])
        out = proxy.aggregate_stream(upstream, MODEL, None)
        self.assertEqual(out["choices"][0]["message"]["content"], "hi")
        self.assertEqual(out["choices"][0]["finish_reason"], "stop")

    def test_truncated_stream_raises(self):
        upstream = FakeUpstream(data_lines(text_delta("hi")))
        with self.assertRaises(T.UpstreamStreamError):
            proxy.aggregate_stream(upstream, MODEL, None)

    def test_error_frame_raises(self):
        upstream = FakeUpstream(data_lines(text_delta("hi"), {"code": 11140, "msg": "rejected"}))
        with self.assertRaises(T.UpstreamStreamError):
            proxy.aggregate_stream(upstream, MODEL, None)

    def test_finish_reason_alone_counts_as_complete(self):
        upstream = FakeUpstream(data_lines(text_delta("hi"), finish("length")))
        out = proxy.aggregate_stream(upstream, MODEL, None)
        self.assertEqual(out["choices"][0]["finish_reason"], "length")

    def test_arguments_only_function_call_chunks_survive(self):
        # legacy function_call 的后续分片只带 arguments；按 name 判空会把这些
        # 分片全丢掉，客户端收到的工具参数只剩第一片。
        first = {"choices": [{"delta": {"function_call": {"name": "lookup", "arguments": ""}}}]}
        second = {"choices": [{"delta": {"function_call": {"arguments": "{\"q\":"}}}]}
        third = {"choices": [{"delta": {"function_call": {"arguments": "\"x\"}"}}}]}
        tail = {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        cleaned = json.loads(T.clean_chunk(json.dumps(second)))
        self.assertIn("function_call", cleaned["choices"][0]["delta"])
        out = proxy.aggregate_stream(
            FakeUpstream(data_lines(first, second, third, tail) + [b"data: [DONE]\n"]),
            MODEL, None)
        call = out["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "lookup")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"q": "x"})


class ErrorFrameShapeTests(unittest.TestCase):
    """错误帧的识别范围：带 choices 的帧永远算正常分片。"""

    def test_openai_shape(self):
        self.assertEqual(proxy.upstream_error_text({"error": "boom"}), "upstream error: boom")

    def test_gateway_envelope(self):
        self.assertIn("6004", proxy.upstream_error_text({"code": "6004", "msg": "throttled"}))

    def test_normal_frames_are_not_errors(self):
        self.assertIsNone(proxy.upstream_error_text({"choices": [{"delta": {"content": "x"}}]}))
        self.assertIsNone(proxy.upstream_error_text({"choices": [], "usage": {"total_tokens": 3}}))
        self.assertIsNone(proxy.upstream_error_text({"code": 0, "msg": "ok"}))
        self.assertIsNone(proxy.upstream_error_text({"id": "x", "created": 1}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
