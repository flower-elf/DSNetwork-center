"""Tests for fingerprint sanitization and the DSH system opener rewrite."""
import json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)
# Its own directories, removed when this process exits. This suite used to share
# tests/_acc_test and tests/_use_test with _test_daily_chat.py and
# _test_tasks_cache.py, which made them unsafe to run at the same time and left
# directories in the repo after a run.
_TMP = isolated_data_dirs("wb-fingerprint-")

import wb_proxy as P

class FingerprintSanitizationTests(unittest.TestCase):
    def test_omo_junior_fingerprint_neutralized(self):
        fp = "Sisyphus-Junior - Focused executor from OhMyOpenCode"
        self.assertEqual(P.sanitize_text(fp), "Sisyphus-Junior - Focused executor")

        self.assertEqual(P.sanitize_text(fp.lower()), "Sisyphus-Junior - Focused executor")

        text = f"You are a subagent. {fp}. Complete the task."
        expected = "You are a subagent. Sisyphus-Junior - Focused executor. Complete the task."
        self.assertEqual(P.sanitize_text(text), expected)

    def test_omo_partial_tokens_untouched(self):
        # Tokens alone stay untouched
        self.assertEqual(P.sanitize_text("Sisyphus-Junior alone"), "Sisyphus-Junior alone")
        self.assertEqual(P.sanitize_text("from OhMyOpenCode alone"), "from OhMyOpenCode alone")
        self.assertEqual(P.sanitize_text("Sisyphus master agent"), "Sisyphus master agent")

    def test_sanitize_messages_covers_all_roles_and_tool_args(self):
        fp = "Sisyphus-Junior - Focused executor from OhMyOpenCode"
        clean = "Sisyphus-Junior - Focused executor"
        messages = [
            {"role": "system", "content": f"System: {fp}"},
            {"role": "user", "content": f"User: {fp}"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run", "arguments": json.dumps({"query": fp})}
                }]
            },
            {"role": "tool", "tool_call_id": "call_1", "content": f"Tool output: {fp}"}
        ]
        sanitized = P.sanitize_messages(messages)
        self.assertIn(clean, sanitized[0]["content"])
        self.assertNotIn("from OhMyOpenCode", sanitized[0]["content"])
        self.assertIn(clean, sanitized[1]["content"])
        self.assertNotIn("from OhMyOpenCode", sanitized[1]["content"])
        args = json.loads(sanitized[2]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["query"], clean)
        self.assertIn(clean, sanitized[3]["content"])
        self.assertNotIn("from OhMyOpenCode", sanitized[3]["content"])

class DshSystemRewriteTests(unittest.TestCase):
    """DSH 的 system 自我介绍触发上游渠道校验时的改写。"""

    AI_SENTENCE = "You are an AI agent powered by DeepSeek Harness."
    CODING_SENTENCE = (
        "You are a coding agent powered by the deepseek-v4.1-flash model.")

    def test_both_sentences_rewritten(self):
        text = self.AI_SENTENCE + "\n" + self.CODING_SENTENCE
        self.assertEqual(
            P.sanitize_dsh_system(text),
            "You are a helpful AI assistant.\n"
            "You are an assistant built on the deepseek-v4.1-flash model.")

    def test_case_insensitive(self):
        self.assertEqual(P.sanitize_dsh_system(self.AI_SENTENCE.lower()),
                         "You are a helpful AI assistant.")

    def test_tool_contract_after_the_sentences_kept(self):
        text = self.AI_SENTENCE + "\nUse the read tool first."
        self.assertEqual(P.sanitize_dsh_system(text),
                         "You are a helpful AI assistant.\n"
                         "Use the read tool first.")

    def test_unrelated_powered_by_prompts_untouched(self):
        for text in ("You are a helpful assistant.",
                     "DeepSeek Harness alone",
                     "You are an AI agent.",
                     "powered by DeepSeek Harness alone",
                     "You are a coding agent powered by Acme Harness.",
                     "You are a coding agent powered by the acme-harness model."):
            self.assertEqual(P.sanitize_dsh_system(text), text)

    def test_text_blocks_rewritten_without_mutating_input(self):
        content = [{"type": "text", "text": self.AI_SENTENCE},
                   {"type": "image", "source": "x"},
                   {"type": "text", "text": self.CODING_SENTENCE}]
        out = P.sanitize_dsh_system(content)
        self.assertEqual(out[0]["text"],
                         "You are a helpful AI assistant.")
        self.assertEqual(out[1], {"type": "image", "source": "x"})
        self.assertEqual(out[2]["text"],
                         "You are an assistant built on the deepseek-v4.1-flash model.")
        self.assertEqual(content[0]["text"], self.AI_SENTENCE)

    def test_sanitize_messages_only_touches_system_role(self):
        messages = [
            {"role": "system", "content": self.AI_SENTENCE},
            {"role": "user", "content": self.AI_SENTENCE},
            {"role": "assistant", "content": self.CODING_SENTENCE},
        ]
        out = P.sanitize_messages(messages)
        self.assertEqual(out[0]["content"],
                         "You are a helpful AI assistant.")
        self.assertEqual(out[1]["content"], self.AI_SENTENCE)
        self.assertEqual(out[2]["content"], self.CODING_SENTENCE)

    def test_rewrite_is_idempotent(self):
        for text in (self.AI_SENTENCE, self.CODING_SENTENCE):
            once = P.sanitize_dsh_system(text)
            self.assertEqual(P.sanitize_dsh_system(once), once)

    def test_build_upstream_body_rewrites_developer_message(self):
        body = P.build_upstream_body({
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "developer", "content": self.AI_SENTENCE},
                         {"role": "user", "content": "hi"}],
        })
        self.assertEqual(body["messages"][0]["content"],
                         "You are a helpful AI assistant.")


class DshGuiParagraphTests(unittest.TestCase):
    """DSH 的 GUI 描述段触发上游渠道校验时的改写。"""

    A = ('You are interacting with the user through the DeepSeek Harness Web GUI '
         'whose URL the runtime context names.')
    CLEAN_A = ('You are interacting with the user through the GUI '
               'whose URL is named in the runtime context.')
    B = ('When the user refers to "this page", "this GUI", or "this app" without '
         'naming another target, they mean this GUI.')

    def test_paragraph_rewritten(self):
        self.assertEqual(P.sanitize_dsh_system(self.A + " " + self.B),
                         self.CLEAN_A + " " + self.B)

    def test_developer_message_rewritten(self):
        body = P.build_upstream_body({
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "developer", "content": self.A + " " + self.B},
                         {"role": "user", "content": "hi"}],
        })
        self.assertEqual(body["messages"][0]["content"],
                         self.CLEAN_A + " " + self.B)

    def test_user_and_assistant_untouched(self):
        text = self.A + " " + self.B
        out = P.sanitize_messages([
            {"role": "user", "content": text},
            {"role": "assistant", "content": text},
        ])
        self.assertEqual(out[0]["content"], text)
        self.assertEqual(out[1]["content"], text)

    def test_brand_outside_the_gui_sentence_untouched(self):
        for text in (
            "The runtime context names the DeepSeek Harness implementation checkout.",
            "DeepSeek Harness",
            "You are interacting with the user through the DeepSeek Harness CLI.",
        ):
            self.assertEqual(P.sanitize_dsh_system(text), text, text)

    def test_rewrite_is_idempotent(self):
        once = P.sanitize_dsh_system(self.A)
        self.assertEqual(P.sanitize_dsh_system(once), once)


if __name__ == "__main__":
    unittest.main()

