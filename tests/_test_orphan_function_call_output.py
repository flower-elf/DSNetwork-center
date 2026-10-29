"""孤儿 function_call_output（无 call_id）携带图片时的转换测试。

output 带图片分片时 _flatten_content() 返回结构化 parts 列表；孤儿分支必须原样保留
这些分片，一旦对列表调用 .strip() 会抛 AttributeError，socketserver 吞掉异常后连接被
直接断开（Codex Desktop 实测每次重试都得到裸 502）。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_proxy as P


class OrphanFunctionCallOutputTests(unittest.TestCase):
    def test_orphan_output_with_image_parts_becomes_multimodal_user_message(self):
        # 孤儿分支必须保留结构化 parts，不能对其调用 .strip()。
        payload = {"model": "deepseek-v4.1-flash", "input": [
            {"type": "function_call_output",
             "output": [{"type": "input_image",
                         "image_url": "data:image/png;base64,iVBORw0KGgo="}]},
        ]}
        chat = P.responses_to_chat(payload)
        messages = chat["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")
        parts = messages[0]["content"]
        self.assertIsInstance(parts, list)
        self.assertTrue(str(parts[0].get("text", "")).startswith("[Message from another task"))
        self.assertEqual(parts[1].get("type"), "image_url")

    def test_orphan_output_text_only_still_flattens_to_text(self):
        payload = {"model": "deepseek-v4.1-flash", "input": [
            {"type": "function_call_output",
             "output": [{"type": "output_text", "text": "done"}]},
        ]}
        chat = P.responses_to_chat(payload)
        messages = chat["messages"]
        self.assertEqual(messages[0]["role"], "user")
        self.assertIn("done", messages[0]["content"])

    def test_orphan_output_dict_still_serializes(self):
        payload = {"model": "deepseek-v4.1-flash", "input": [
            {"type": "function_call_output",
             "output": {"type": "output_text", "text": "done"}},
        ]}
        chat = P.responses_to_chat(payload)
        messages = chat["messages"]
        self.assertTrue(messages[0]["content"].endswith(
            json.dumps({"type": "output_text", "text": "done"}, ensure_ascii=False)))

    def test_function_call_output_with_call_id_unchanged(self):
        payload = {"model": "deepseek-v4.1-flash", "input": [
            {"type": "function_call", "call_id": "call_1", "name": "shell",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1",
             "output": [{"type": "output_text", "text": "done"}]},
        ]}
        chat = P.responses_to_chat(payload)
        roles = [m["role"] for m in chat["messages"]]
        self.assertEqual(roles, ["assistant", "tool"])


if __name__ == "__main__":
    unittest.main()
