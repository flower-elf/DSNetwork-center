# VS Code 渠道的出站身分：UA 与版本头必须来自同一组值。
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)
sys.path.insert(0, ROOT)

import wb_identity  # noqa: E402


class VSCodeIdentityTest(unittest.TestCase):
    def test_headers_follow_the_codebuddy_plugin_shape(self):
        headers = wb_identity.build_identity_headers(
            "vscode", "intl", "uid-1", "token-1")
        self.assertEqual(headers["X-IDE-Type"], "VSCode")
        self.assertEqual(headers["X-IDE-Version"], "1.142.0")
        self.assertEqual(headers["X-Product-Version"], "4.12.39997873")
        self.assertEqual(headers["X-Agent-Type"], "main")
        self.assertEqual(headers["X-Agent-Purpose"], "conversation")
        self.assertEqual(headers["X-Agent-Runtime"], "other")
        self.assertEqual(
            headers["User-Agent"],
            "VSCode/%s CodeBuddy/%s" % (
                headers["X-IDE-Version"], headers["X-Product-Version"]))


class CliIdentityTest(unittest.TestCase):
    def test_headers_follow_the_codebuddy_cli_shape(self):
        headers = wb_identity.build_identity_headers(
            "cli", "intl", "uid-1", "token-1")
        self.assertEqual(headers["X-IDE-Type"], "CLI")
        self.assertEqual(headers["X-IDE-Version"], "2.161.0")
        self.assertEqual(headers["X-Agent-Type"], "main")
        self.assertEqual(headers["X-Agent-Purpose"], "conversation")
        self.assertEqual(headers["X-Agent-Runtime"], "other")
        self.assertEqual(
            headers["User-Agent"],
            "CLI/%s CodeBuddy/%s" % (
                headers["X-IDE-Version"], headers["X-IDE-Version"]))


class PrivateDataTest(unittest.TestCase):
    # 网关不提供数据用途选项，三种出站身份对个人号与企业号都声明数据私有。
    def test_every_identity_declares_private_data(self):
        for product in wb_identity.VALID_PRODUCTS:
            for realm in ("intl", "cn"):
                for enterprise in ("", "ent-1"):
                    headers = wb_identity.build_identity_headers(
                        product, realm, "uid-1", "token-1",
                        enterprise_id=enterprise)
                    self.assertEqual(headers.get("X-Private-Data"), "true",
                                     (product, realm, enterprise))


if __name__ == "__main__":
    unittest.main(verbosity=2)
