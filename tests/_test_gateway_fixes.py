import unittest
import sys
import os
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_proxy

class GatewayFixesTest(unittest.TestCase):
    def test_gateway_hint_413(self):
        hint = wb_proxy.gateway_hint(413, "payload too large (20375735 bytes > 16777216 limit)")
        self.assertIn("WB_MAX_PAYLOAD_BYTES", hint)
        self.assertIn("raise it and restart", hint)

    def test_gpt_max_tokens_clamping(self):
        body = {"model": "gpt-6-luna", "messages": [], "max_tokens": 1}
        upstream = wb_proxy.build_upstream_body(body)
        self.assertEqual(upstream["max_tokens"], 16)

        body_ds = {"model": "deepseek-v4.1-flash", "messages": [], "max_tokens": 1}
        upstream_ds = wb_proxy.build_upstream_body(body_ds)
        self.assertEqual(upstream_ds["max_tokens"], 1)

        body_ok = {"model": "gpt-6.1-sol", "messages": [], "max_tokens": 32}
        upstream_ok = wb_proxy.build_upstream_body(body_ok)
        self.assertEqual(upstream_ok["max_tokens"], 32)

class LogWriteTests(unittest.TestCase):
    def test_a_dead_stderr_does_not_raise(self):
        calls = []

        class Dead(object):
            def write(self, *args):
                calls.append(args)
                raise OSError("stderr is gone")

            def flush(self):
                raise OSError("stderr is gone")

        with mock.patch.object(wb_proxy.sys, "stderr", Dead()):
            wb_proxy.log("probe")  # 抛异常就会让这次请求空连接关闭
        self.assertEqual(len(calls), 1, calls)

    def test_an_entry_still_reaches_the_buffer(self):
        before = len(wb_proxy.LOG_BUFFER)
        entry = wb_proxy.add_log_entry("buffer probe", level="INFO", tag="system")
        self.assertEqual(len(wb_proxy.LOG_BUFFER), min(before + 1, wb_proxy.LOG_BUFFER.maxlen))
        self.assertEqual(entry["msg"], "buffer probe")


if __name__ == "__main__":
    unittest.main()
