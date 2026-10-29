"""面板账号区的折叠状态按一个布尔值持久化。

    python tests/_test_accounts_collapsed.py

#27 的第一阶段：看板只需要一个可持久化的布尔值来读写，不需要别的。它和其它
面板设置一起放在 accounts/settings.json，因此沿用同一把锁与同一种原子替换，
后续的界面改动不需要第二套持久化机制。

归一化承担了大部分情形。面板只会存真正的布尔值，但文件可以手改，所以不是
布尔 true 的一切——"true"、"false"、1、0、对象、缺失键——都必须读作展开。
`is True` 让真值字符串不可能被误当成选择。

不需要网络。
"""
import json
import os
import subprocess
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

# 在 wb_proxy 导入读取 ACCOUNTS_DIR 之前，先准备套件自己的 accounts/ 与
# usage/ 目录。
_TMP = isolated_data_dirs("wb-accounts-collapsed-")

import wb_proxy as proxy  # noqa: E402
import wb_settings as S  # noqa: E402


class FakeRequest(object):
    """够被测保存路由使用的最小 Handler 替身。"""

    _handle_settings_save = proxy.Handler._handle_settings_save

    def __init__(self, payload):
        self.payload = payload
        self.answering = []

    def _payload_or_error(self, allow_list=False):
        return self.payload

    def _error(self, status, message, kind=""):
        self.answering.append(("error", status, message))
        return status, message

    def _json(self, status, payload):
        self.answering.append(("json", status, payload))
        return status, payload


class AccountsCollapsedTests(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(_TMP.name, "accounts")
        os.makedirs(self.dir, exist_ok=True)
        proxy.ACCOUNTS_DIR = self.dir
        path = S.settings_path(self.dir)
        if os.path.exists(path):
            os.unlink(path)

    def _write(self, data):
        with open(S.settings_path(self.dir), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _read(self):
        with open(S.settings_path(self.dir), encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, payload):
        """跑保存路由，返回它报出的错误。"""
        request = FakeRequest(payload)
        request._handle_settings_save()
        return [entry for entry in request.answering if entry[0] == "error"]

    def _view(self):
        return proxy.runtime_settings_view()["accounts_collapsed"]

    # --- 存储值 -------------------------------------------------------------

    def test_missing_key_reads_as_expanded(self):
        self.assertFalse(os.path.exists(S.settings_path(self.dir)))
        self.assertIs(S.accounts_collapsed(self.dir), False)
        self.assertIs(self._view(), False)

    def test_explicit_true_persists_and_reloads(self):
        self.assertIs(S.set_accounts_collapsed(self.dir, True), True)
        self.assertIs(S.accounts_collapsed(self.dir), True)
        self.assertIs(self._read()["accounts_collapsed"], True)
        self.assertIs(self._view(), True)

    def test_explicit_false_persists_and_reloads(self):
        S.set_accounts_collapsed(self.dir, True)
        self.assertIs(S.set_accounts_collapsed(self.dir, False), False)
        self.assertIs(S.accounts_collapsed(self.dir), False)
        self.assertIs(self._read()["accounts_collapsed"], False)
        self.assertIs(self._view(), False)

    def test_malformed_values_normalize_to_expanded(self):
        # "true" 与 "false" 都是非空字符串，用真值判断的话两个都会被报成折叠。
        for stored in ("true", "false", "yes", 1, 0, 1.0, [], {}, None):
            with self.subTest(stored=stored):
                self._write({"accounts_collapsed": stored})
                self.assertIs(S.accounts_collapsed(self.dir), False)
                self.assertIs(self._view(), False)

    def test_a_new_process_sees_the_persisted_value(self):
        """重启会重新读文件，所以这个选择不是进程内的。"""
        S.set_accounts_collapsed(self.dir, True)
        code = ("import sys; sys.path.insert(0, %r); sys.path.insert(0, %r); "
                "import wb_settings as S; "
                "print(S.accounts_collapsed(%r))"
                % (os.path.join(ROOT, "app"), ROOT, self.dir))
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "True")

    # --- 与其它设置共存 -----------------------------------------------------

    def test_another_setting_write_keeps_the_flag(self):
        S.set_accounts_collapsed(self.dir, True)
        S.set_local_web_tools(self.dir, False)
        self.assertIs(S.accounts_collapsed(self.dir), True)
        self.assertIs(self._read()["local_web_tools"], False)

    def test_concurrent_writes_leave_a_readable_file(self):
        self._write({"user_custom_top": {"keep": 1}})
        errors = []

        def flip(value):
            try:
                for _ in range(25):
                    S.set_accounts_collapsed(self.dir, value)
            except Exception as exc:  # 由下面的断言检查
                errors.append(exc)

        threads = [threading.Thread(target=flip, args=(value,))
                   for value in (True, False, True, False, True, False)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        stored = self._read()  # 截断的文件会在这里抛错
        self.assertIsInstance(stored.get("accounts_collapsed"), bool)
        self.assertEqual(stored.get("user_custom_top"), {"keep": 1})

    # --- 保存路由 -----------------------------------------------------------

    def test_save_route_stores_a_json_boolean(self):
        self.assertEqual(self._save({"accounts_collapsed": True}), [])
        self.assertIs(self._view(), True)
        self.assertIs(self._read()["accounts_collapsed"], True)
        self.assertEqual(self._save({"accounts_collapsed": False}), [])
        self.assertIs(self._view(), False)

    def test_save_route_rejects_a_non_boolean(self):
        for bad in ("true", "false", "yes", 1, 0, None, [], {}):
            with self.subTest(value=bad):
                errors = self._save({"accounts_collapsed": bad})
                self.assertEqual(len(errors), 1, errors)
                self.assertEqual(errors[0][1], 400)
                self.assertIs(S.accounts_collapsed(self.dir), False)

    def test_save_route_patch_leaves_other_settings_alone(self):
        """折叠标记的两条写路径都只动自己，其他设置原样保留。"""
        self._write({
            "user_custom_top": {"keep": 1},
            "limits": {"global": {"reserve_credits": 7}},
            "panel_password": {"salt": "ab", "digest": "cd", "rounds": 1},
            "local_web_tools": True,
        })
        with self.subTest(layer="setter"):
            S.set_accounts_collapsed(self.dir, True)
            stored = self._read()
            self.assertEqual(stored["user_custom_top"], {"keep": 1})
            self.assertEqual(stored["limits"], {"global": {"reserve_credits": 7}})
            self.assertEqual(stored["panel_password"],
                             {"salt": "ab", "digest": "cd", "rounds": 1})
            self.assertIs(stored["local_web_tools"], True)
            self.assertIs(stored["accounts_collapsed"], True)
        with self.subTest(layer="route"):
            self.assertEqual(self._save({"accounts_collapsed": True}), [])
            stored = self._read()
            self.assertEqual(stored["user_custom_top"], {"keep": 1})
            self.assertEqual(stored["limits"], {"global": {"reserve_credits": 7}})
            self.assertEqual(stored["panel_password"],
                             {"salt": "ab", "digest": "cd", "rounds": 1})
            self.assertIs(stored["local_web_tools"], True)
            self.assertIs(stored["accounts_collapsed"], True)

    def test_save_route_ignores_a_payload_that_omits_the_flag(self):
        S.set_accounts_collapsed(self.dir, True)
        self.assertEqual(self._save({"user_custom_top": {"keep": 2}}), [])
        self.assertIs(S.accounts_collapsed(self.dir), True)

    # --- 存放位置 -----------------------------------------------------------

    def test_the_setting_lives_in_the_isolated_tree(self):
        S.set_accounts_collapsed(self.dir, True)
        path = os.path.abspath(S.settings_path(self.dir))
        self.assertTrue(path.startswith(os.path.abspath(_TMP.name)), path)
        self.assertFalse(path.startswith(os.path.abspath(os.path.join(ROOT, "accounts"))),
                         path)
        self.assertEqual(os.path.dirname(path), os.path.abspath(self.dir))


if __name__ == "__main__":
    unittest.main(verbosity=2)
