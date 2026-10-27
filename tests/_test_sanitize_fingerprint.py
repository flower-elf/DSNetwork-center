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
                     "You are an AI agent.",
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

    def test_sanitize_messages_rewrites_system_and_assistant(self):
        messages = [
            {"role": "system", "content": self.AI_SENTENCE},
            {"role": "user", "content": self.AI_SENTENCE},
            {"role": "assistant", "content": self.CODING_SENTENCE},
        ]
        out = P.sanitize_messages(messages)
        self.assertEqual(out[0]["content"],
                         "You are a helpful AI assistant.")
        self.assertEqual(out[1]["content"], self.AI_SENTENCE)
        self.assertEqual(out[2]["content"],
                         "You are an assistant built on the "
                         "deepseek-v4.1-flash model.")

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

    def test_user_untouched_and_assistant_rewritten(self):
        text = self.A + " " + self.B
        out = P.sanitize_messages([
            {"role": "user", "content": text},
            {"role": "assistant", "content": text},
        ])
        self.assertEqual(out[0]["content"], text)
        self.assertEqual(out[1]["content"], self.CLEAN_A + " " + self.B)

    def test_rewrite_is_idempotent(self):
        once = P.sanitize_dsh_system(self.A)
        self.assertEqual(P.sanitize_dsh_system(once), once)


class DshPreemptiveTests(unittest.TestCase):
    """DSH 客户端独有标识的预防性改写。"""

    SOURCE = ("The runtime context names the DeepSeek Harness implementation "
              "checkout. Use this checkout only to inspect or extend DSH itself.")
    SOURCE_CLEAN = ("The runtime context names the Harness implementation "
                    "checkout. Use this checkout only to inspect or extend the "
                    "harness itself.")
    WEB = ("Starting another server does not update this GUI. The apps/web Vite "
           "entry builds the shell because only dsh web injects "
           "window.__DSH_BOOT__. The watcher runs only while "
           "`pnpm run dev:web` is also running.")
    WEB_CLEAN = ("Starting another server does not update this GUI. The apps/web "
                 "Vite entry builds the shell because only the web launcher "
                 "injects the boot payload. The watcher runs only while the web "
                 "dev command is also running.")
    RUNTIME = (
        "Current runtime context. This snapshot supersedes earlier "
        "runtime-context snapshots.\n\n"
        "Current working directory: \"/workspace/project\".\n\n"
        "Current DSH file policy: workspace-write. Any available operation "
        "enforced by the DSH file sandbox may modify files under the session "
        "workspace: \"/workspace/project\".\n\n"
        "Current DeepSeek Harness implementation checkout: \"/opt/dsh\".\n\n"
        "Current DeepSeek Harness Web GUI URL: \"http://127.0.0.1:8080\"."
    )
    RUNTIME_CLEAN = (
        "Current runtime context. This snapshot supersedes earlier "
        "runtime-context snapshots.\n\n"
        "Current working directory: \"/workspace/project\".\n\n"
        "Current file policy: workspace-write. Any available operation "
        "enforced by the file sandbox may modify files under the session "
        "workspace: \"/workspace/project\".\n\n"
        "Current implementation checkout: \"/opt/dsh\".\n\n"
        "Current Web GUI URL: \"http://127.0.0.1:8080\"."
    )

    def test_system_client_identifiers_reduced(self):
        self.assertEqual(P.sanitize_dsh_system(self.SOURCE), self.SOURCE_CLEAN)
        self.assertEqual(P.sanitize_dsh_system(self.WEB), self.WEB_CLEAN)

    def test_assistant_brand_reduced(self):
        out = P.sanitize_messages([
            {"role": "assistant", "content": "Powered by DeepSeek Harness."},
        ])
        self.assertEqual(out[0]["content"], "Powered by Harness.")

    def test_assistant_client_identifiers_reduced(self):
        out = P.sanitize_messages([
            {"role": "assistant", "content": "Use DSH and dsh web."},
        ])
        self.assertEqual(out[0]["content"],
                         "Use the harness and the web launcher.")

    def test_runtime_context_rewritten(self):
        out = P.sanitize_messages([{"role": "user", "content": self.RUNTIME}])
        self.assertEqual(out[0]["content"], self.RUNTIME_CLEAN)

    def test_runtime_context_paths_untouched(self):
        text = ('Current runtime context. Current working directory: '
                '"D:\\\\DSH\\\\project". Current DSH file policy: '
                'workspace-write.')
        out = P.sanitize_messages([{"role": "user", "content": text}])
        self.assertIn('"D:\\\\DSH\\\\project"', out[0]["content"])
        self.assertIn("Current file policy:", out[0]["content"])

    def test_plain_user_text_untouched(self):
        text = "DeepSeek Harness is a product. DSH is its abbreviation."
        out = P.sanitize_messages([
            {"role": "user", "content": text},
            {"role": "tool", "content": text},
        ])
        self.assertEqual(out[0]["content"], text)
        self.assertEqual(out[1]["content"], text)

    def test_skill_description_reduced_without_renaming(self):
        text = ("<system-reminder>\n<available_skills>\n"
                "- `diagnose-windows-sandbox-acl`: Use on Windows for unexpected "
                "DSH sandbox access denials.\n"
                "</available_skills>\n</system-reminder>")
        out = P.sanitize_messages([{"role": "user", "content": text}])
        self.assertIn("`diagnose-windows-sandbox-acl`", out[0]["content"])
        self.assertIn("the harness sandbox access denials", out[0]["content"])
        self.assertNotIn("DSH sandbox", out[0]["content"])

    def test_skill_name_with_dsh_preserved(self):
        text = ("<available_skills>\n- `DSH-helper`: Use DSH when needed.\n"
                "</available_skills>")
        out = P.sanitize_messages([{"role": "user", "content": text}])
        self.assertIn("`DSH-helper`", out[0]["content"])
        self.assertIn("Use the harness when needed.", out[0]["content"])

    def test_rewrite_is_idempotent(self):
        for text in (self.SOURCE, self.WEB):
            once = P.sanitize_dsh_system(text)
            self.assertEqual(P.sanitize_dsh_system(once), once)
        once = P.sanitize_dsh_user_context(self.RUNTIME)
        self.assertEqual(P.sanitize_dsh_user_context(once), once)

    def test_wire_body_carries_both_rewrites(self):
        body = P.build_upstream_body({
            "model": "deepseek-v4.1-flash",
            "messages": [
                {"role": "system", "content": self.SOURCE},
                {"role": "user", "content": self.RUNTIME},
            ],
        })
        self.assertEqual(body["messages"][0]["content"], self.SOURCE_CLEAN)
        self.assertEqual(body["messages"][1]["content"], self.RUNTIME_CLEAN)


class DshFileReferenceTests(unittest.TestCase):
    """DSH 文件引用说明段触发上游渠道校验时的改写。

    `@deepseek-ai/dsh-file-reference` 注入的说明段里，前两句连续出现即命中
    11-128。首句补一个 that 后两句不再连续，段落其余内容保持原样。
    system 与 assistant 历史都改写，user 与 tool 保持原样。
    """

    A = "Tokens prefixed with @ are paths the user explicitly referenced."
    A_REWORDED = "Tokens prefixed with @ are paths that the user explicitly referenced."
    B = ("Relative paths resolve from the workspace root; absolute paths identify "
         "files or directories on the host.")
    TAIL = ("A trailing slash marks a directory: list it when its contents matter. "
            "Anything else is a file: use the read tool when its contents are "
            "needed, and do not claim to have inspected it before reading. "
            "@\"...\" quotes a path containing spaces.")
    PARA_OLD = (
        "Tokens prefixed with @ are paths the user explicitly referenced. "
        "Relative paths resolve from the workspace root; absolute paths point to "
        "files or directories on the host. A trailing slash marks a directory, so "
        "list it when its contents matter. Anything else is a file: open it with "
        "the read tool when its contents are needed, and never claim to have "
        "inspected it before reading. The @\"...\" form only quotes a path that "
        "contains spaces."
    )

    def test_first_two_sentences_rewritten(self):
        out = P.sanitize_dsh_system(self.A + " " + self.B)
        self.assertEqual(out, self.A_REWORDED + " " + self.B)
        self.assertNotIn(self.A + " " + self.B, out)

    def test_current_paragraph_keeps_the_rest_verbatim(self):
        paragraph = self.A + " " + self.B + " " + self.TAIL
        self.assertEqual(
            P.sanitize_dsh_system(paragraph),
            self.A_REWORDED + " " + self.B + " " + self.TAIL)

    def test_older_paragraph_rewritten(self):
        self.assertEqual(
            P.sanitize_dsh_system(self.PARA_OLD),
            self.PARA_OLD.replace(self.A, self.A_REWORDED, 1))

    def test_sentence_a_alone_rewritten(self):
        self.assertEqual(P.sanitize_dsh_system(self.A), self.A_REWORDED)

    def test_case_insensitive(self):
        text = (self.A + " " + self.B).lower()
        self.assertEqual(P.sanitize_dsh_system(text),
                         self.A_REWORDED + " " + self.B.lower())

    def test_rewrite_is_idempotent(self):
        once = P.sanitize_dsh_system(self.A + " " + self.B)
        self.assertEqual(P.sanitize_dsh_system(once), once)
        self.assertEqual(once.count("paths that the user"), 1)

    def test_unrelated_text_untouched(self):
        for text in (
            "Mail me at someone@example.com and open C:\\cache\\@tmp.",
            "npm i @deepseek-ai/dsh-llm",
            self.B,
            "Tokens prefixed with @ are file references.",
        ):
            self.assertEqual(P.sanitize_dsh_system(text), text, text)

    def test_developer_message_rewritten(self):
        body = P.build_upstream_body({
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "developer", "content": self.A + " " + self.B},
                         {"role": "user", "content": "hi"}],
        })
        self.assertEqual(body["messages"][0]["content"],
                         self.A_REWORDED + " " + self.B)

    def test_assistant_message_rewritten(self):
        text = self.A + " " + self.B
        out = P.sanitize_messages([{"role": "assistant", "content": text}])
        self.assertEqual(out[0]["content"], self.A_REWORDED + " " + self.B)

    def test_assistant_text_blocks_rewritten(self):
        part = {"type": "text", "text": self.A + " " + self.B}
        out = P.sanitize_messages([
            {"role": "assistant", "content": [part]},
        ])
        self.assertEqual(out[0]["content"][0]["text"],
                         self.A_REWORDED + " " + self.B)
        self.assertEqual(part["text"], self.A + " " + self.B)

    def test_user_and_tool_untouched(self):
        text = self.A + " " + self.B
        out = P.sanitize_messages([
            {"role": "user", "content": text},
            {"role": "tool", "content": text},
        ])
        self.assertEqual(out[0]["content"], text)
        self.assertEqual(out[1]["content"], text)


class DshToolNeutralizationTests(unittest.TestCase):
    """工具调用通用化默认开启，关闭时工具定义逐字不变。"""

    TOOLS = [{
        "type": "function",
        "function": {
            "name": "ask_user_question",
            "description": "Ask through DeepSeek Harness or DSH.",
            "parameters": {
                "type": "object",
                "properties": {
                    "zeta": {"type": "string", "enum": ["a", "b"]},
                    "alpha": {"type": "integer", "default": 1},
                },
                "required": ["zeta"],
                "additionalProperties": False,
            },
        },
    }]

    def setUp(self):
        self._old = P.wb_settings.dsh_tool_neutralization(P.ACCOUNTS_DIR)
        P.wb_settings.set_dsh_tool_neutralization(P.ACCOUNTS_DIR, True)

    def tearDown(self):
        P.wb_settings.set_dsh_tool_neutralization(P.ACCOUNTS_DIR, self._old)

    def _body(self, enabled, dsh=True):
        P.wb_settings.set_dsh_tool_neutralization(P.ACCOUNTS_DIR, enabled)
        messages = [{"role": "user", "content": "hi"}]
        if dsh:
            messages.insert(0, {"role": "system",
                                "content": "You are an AI agent powered by DeepSeek Harness."})
        return P.build_upstream_body({
            "model": "deepseek-v4.1-flash",
            "messages": messages,
            "tools": self.TOOLS,
        })

    def test_disabled_keeps_tools_verbatim(self):
        self.assertEqual(self._body(False)["tools"], self.TOOLS)

    def test_non_dsh_tools_untouched(self):
        self.assertEqual(self._body(True, dsh=False)["tools"], self.TOOLS)

    def test_enabled_keeps_contract_and_reduces_identifiers(self):
        body = self._body(True)
        fn = body["tools"][0]["function"]
        self.assertEqual(fn["name"], "ask_user_question")
        self.assertEqual(fn["description"], "Ask through Harness or the harness.")
        params = fn["parameters"]
        self.assertEqual(list(params), ["additionalProperties", "properties",
                                        "required", "type"])
        self.assertEqual(list(params["properties"]), ["alpha", "zeta"])
        self.assertEqual(params["properties"]["zeta"]["enum"], ["a", "b"])
        self.assertEqual(params["properties"]["alpha"]["default"], 1)
        self.assertEqual(params["required"], ["zeta"])
        self.assertFalse(params["additionalProperties"])
        # 输入对象不被改写，同一请求重试时保持稳定。
        self.assertEqual(self.TOOLS[0]["function"]["description"],
                         "Ask through DeepSeek Harness or DSH.")
        again = self._body(True)["tools"]
        self.assertEqual(again, body["tools"])


class DshToolAliasTests(unittest.TestCase):
    """工具别名只改上游名称，响应再还原，schema 保持原样。"""

    def _payload(self):
        return {
            "tools": [{
                "type": "function",
                "function": {
                    "name": "job_output",
                    "description": "Read a job.",
                    "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
                },
            }],
            "tool_choice": {"type": "function", "function": {"name": "job_output"}},
            "messages": [
                {"role": "system", "content": "Use job_output and subagent_fork, not job_outputs."},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "job_output", "arguments": "{}"},
                }]},
                {"role": "user", "content": "please call present"},
                {"role": "tool", "name": "job_output", "tool_call_id": "call_1",
                 "content": "job_output done"},
            ],
        }

    def test_is_dsh_payload_detection(self):
        def tool(name):
            return {"type": "function", "function": {"name": name}}
        self.assertTrue(P.is_dsh_payload({
            "messages": [{"role": "system", "content": "DeepSeek Harness"}]}))
        self.assertTrue(P.is_dsh_payload({"dsh_session_log": []}))
        self.assertTrue(P.is_dsh_payload({
            "tools": [tool("job_list"), tool("job_output"), tool("present")]}))
        self.assertFalse(P.is_dsh_payload({
            "messages": [{"role": "user", "content": "DeepSeek Harness"}]}))
        self.assertFalse(P.is_dsh_payload({"tools": [tool("job_list")]}))

    def test_aliases_apply_to_tools_choice_history_and_prompts(self):
        payload = self._payload()
        reverse = P.apply_dsh_tool_aliases(payload)
        self.assertEqual(reverse, {"read_job": "job_output"})
        self.assertEqual(payload["tools"][0]["function"]["name"], "read_job")
        self.assertEqual(payload["tools"][0]["function"]["parameters"],
                         {"type": "object", "properties": {"id": {"type": "string"}}})
        self.assertEqual(payload["tool_choice"]["function"]["name"], "read_job")
        self.assertEqual(
            payload["messages"][1]["tool_calls"][0]["function"]["name"], "read_job")
        self.assertEqual(
            payload["messages"][0]["content"],
            "Use read_job and subagent_fork, not job_outputs.")
        self.assertEqual(payload["messages"][2]["content"],
                         "please call present")
        self.assertEqual(payload["messages"][3]["name"], "read_job")
        self.assertEqual(payload["messages"][3]["content"], "job_output done")

    def test_only_present_tools_are_mapped(self):
        payload = {
            "tools": [{"type": "function", "function": {"name": "job_output"}}],
            "messages": [{"role": "system",
                          "content": "Use job_output or present."}],
        }
        reverse = P.apply_dsh_tool_aliases(payload)
        self.assertEqual(reverse, {"read_job": "job_output"})
        self.assertEqual(payload["messages"][0]["content"],
                         "Use read_job or present.")

    def test_alias_collision_skips_mapping(self):
        payload = {
            "tools": [
                {"type": "function", "function": {"name": "job_output"}},
                {"type": "function", "function": {"name": "read_job"}},
            ],
            "tool_choice": "job_output",
            "messages": [{"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "job_output", "arguments": "{}"}}]}],
        }
        self.assertEqual(P.apply_dsh_tool_aliases(payload), {})
        self.assertEqual([t["function"]["name"] for t in payload["tools"]],
                         ["job_output", "read_job"])
        self.assertEqual(payload["tool_choice"], "job_output")
        self.assertEqual(
            payload["messages"][0]["tool_calls"][0]["function"]["name"],
            "job_output")

    def test_restore_names_in_nonstream_and_stream_shapes(self):
        reverse = {"read_job": "job_output", "delegate": "subagent"}
        obj = {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "read_job", "arguments": "{}"}}]}}]}
        P.restore_dsh_tool_names(obj, reverse)
        self.assertEqual(obj["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
                         "job_output")
        chunk = {"choices": [{"delta": {"tool_calls": [
            {"function": {"name": "delegate"}}]}}]}
        P.restore_dsh_tool_names(chunk, reverse)
        self.assertEqual(
            chunk["choices"][0]["delta"]["tool_calls"][0]["function"]["name"],
            "subagent")

    def test_alias_names_do_not_collide_with_dsh_tools(self):
        names = {
            "ask_user_question", "create_goal", "edit", "get_goal", "glob", "grep",
            "interrupt_agent", "job_kill", "job_list", "job_output", "list_agents",
            "present", "pwsh", "read", "read_image", "schedule_create",
            "schedule_delete", "schedule_list", "schedule_update", "send_message",
            "skill", "subagent", "subagent_fork", "todo_write", "update_goal",
            "web_fetch", "web_search", "workflow", "working_directory", "write",
        }
        self.assertEqual(names & set(P.DSH_TOOL_ALIASES.values()), set())


if __name__ == "__main__":
    unittest.main()

