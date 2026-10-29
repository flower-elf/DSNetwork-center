"""桌面客户端的模型选择只作用于主聊天线程，后台任务（记忆生成、氛围建议、临时生成、自动化等）由内置的 model-routing-config（codex-apps/protocol/src/model-routing-config.ts）发往写死的模型；这些请求与正常流量共用同一个 key，UI 里即使选了免费模型也照样消耗付费额度。

``WB_BACKGROUND_MODEL_NAMES``（逗号分隔，默认值覆盖已知模型）标记要拦截的模型，``WB_BACKGROUND_MODEL_TARGET``（默认 ``deepseek-v4.1-flash``）是免费替换目标。改写发生在唯一的 POST 分发点、选账号之前，OpenAI 形和 Anthropic 形路由都覆盖。
Run with: python tests/run_all.py _test_background_model_alias
不使用上游凭据，也不访问外网。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

_startup_dir = tempfile.TemporaryDirectory(prefix="bg-model-alias-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


class BackgroundModelAliasTests(unittest.TestCase):
    """改写必须收窄：只动列出的模型，其他字段原样保留。"""

    def _apply(self, payload):
        wb_proxy._apply_background_model_alias(payload)
        return payload

    def test_listed_models_are_redirected(self):
        for name in ("gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.3-codex"):
            body = self._apply({"model": name, "messages": []})
            self.assertEqual(body["model"], "deepseek-v4.1-flash")

    def test_normal_models_are_untouched(self):
        for name in ("deepseek-v4.1-flash", "gpt-6-astra", "glm-5.3"):
            body = self._apply({"model": name})
            self.assertEqual(body["model"], name)

    def test_matching_ignores_case_and_whitespace(self):
        body = self._apply({"model": "  GPT-5.6-TERRA "})
        self.assertEqual(body["model"], "deepseek-v4.1-flash")

    def test_missing_or_non_string_model_is_left_alone(self):
        self.assertEqual(self._apply({}), {})
        self.assertEqual(self._apply({"model": None}), {"model": None})
        self.assertEqual(self._apply({"model": 42}), {"model": 42})

    def test_non_dict_payload_is_ignored(self):
        """分发点会转发任何解析结果，这里不能抛异常，也不改写负载。"""
        for payload in (None, "not-a-body", ["also", "not", "a-body"]):
            self.assertIsNone(wb_proxy._apply_background_model_alias(payload))

    def test_other_body_fields_survive_the_rewrite(self):
        body = self._apply({"model": "gpt-5.6-terra", "stream": True,
                            "max_tokens": 16, "messages": [{"role": "user",
                                                            "content": "hi"}]})
        self.assertEqual(body["model"], "deepseek-v4.1-flash")
        self.assertTrue(body["stream"])
        self.assertEqual(body["max_tokens"], 16)
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])

    def test_names_and_target_are_env_overridable(self):
        # 默认名单与目标在启动时从环境变量解析；子进程重新导入模块，验证自定义
        # 名单与目标模型生效。
        code = (
            "import json, wb_proxy\n"
            "body = {'model': 'MODEL-B'}\n"
            "wb_proxy._apply_background_model_alias(body)\n"
            "untouched = {'model': 'gpt-5.6-terra'}\n"
            "wb_proxy._apply_background_model_alias(untouched)\n"
            "print(json.dumps({'names': list(wb_proxy.BACKGROUND_MODEL_NAMES),\n"
            "                  'target': wb_proxy.BACKGROUND_MODEL_TARGET,\n"
            "                  'rewritten': body['model'],\n"
            "                  'untouched': untouched['model']}))\n"
        )
        env = dict(os.environ)
        env["WB_BACKGROUND_MODEL_NAMES"] = "Model-A, model-b"
        env["WB_BACKGROUND_MODEL_TARGET"] = "some-free-model"
        app_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
        env["PYTHONPATH"] = os.pathsep.join(
            [app_dir] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        out = subprocess.run([sys.executable, "-c", code], env=env,
                             capture_output=True, check=True)
        data = json.loads(out.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertEqual(data["names"], ["model-a", "model-b"])
        self.assertEqual(data["target"], "some-free-model")
        self.assertEqual(data["rewritten"], "some-free-model")
        self.assertEqual(data["untouched"], "gpt-5.6-terra")


if __name__ == "__main__":
    unittest.main()
