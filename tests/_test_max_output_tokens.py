"""模型目录给 max_tokens 补默认值的测试（issue #121）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_proxy as P


class MaxOutputTokensTests(unittest.TestCase):
    def test_responses_to_chat_defaults_max_output_tokens(self):
        # Responses 请求省略 max_output_tokens 时取目录默认 128000，避免上游按 32k 截断。
        payload = {"model": "deepseek-v4.1-flash", "input": "hello"}
        chat = P.responses_to_chat(payload)
        self.assertEqual(chat.get("max_tokens"), 128000)

    def test_responses_to_chat_honors_explicit_max_output_tokens(self):
        payload = {
            "model": "deepseek-v4.1-flash",
            "input": "hello",
            "max_output_tokens": 4096,
        }
        chat = P.responses_to_chat(payload)
        self.assertEqual(chat.get("max_tokens"), 4096)

    def test_build_upstream_body_defaults_max_tokens(self):
        # 普通 Chat Completions 请求缺 max_tokens 时同样取目录的 maxOutputTokens 默认值。
        chat = {
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "hello"}],
        }
        body = P.build_upstream_body(chat)
        self.assertEqual(body.get("max_tokens"), 128000)

    def test_build_upstream_body_honors_explicit_max_tokens(self):
        chat = {
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 2048,
        }
        body = P.build_upstream_body(chat)
        self.assertEqual(body.get("max_tokens"), 2048)

    def test_build_upstream_body_honors_max_completion_tokens(self):
        chat = {
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "max_completion_tokens": 8192,
        }
        body = P.build_upstream_body(chat)
        self.assertEqual(body.get("max_tokens"), 8192)

    def test_unknown_model_leaves_max_tokens_unset(self):
        chat = {
            "model": "unrecognized-custom-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
        body = P.build_upstream_body(chat)
        self.assertNotIn("max_tokens", body)


if __name__ == "__main__":
    unittest.main()
