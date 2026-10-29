"""隐藏顶部页签的清单以一个页面 key 列表持久化。

设置页的“顶部页签显示”开关需要一个稳定持久化的列表来读写，仅此而已。它存在
accounts/settings.json，与其他面板设置同处，共享同一把锁与原子替换，界面不需要
第二套持久化机制。

这些情形主要靠归一化兜住。面板只会发格式良好的 key 列表，但文件可手工编辑，本模块
也不了解有哪些页面（页面在 app/web），所以畸形值一律按“没有隐藏”理解。`settings`
是写入时丢掉的唯一 key：开关本身就在那一页，存成隐藏会让面板无法回头。

不需要网络。用 python tests/_test_page_visibility.py 运行。
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

# 独立的 accounts/ 与 usage/，在 wb_proxy import 读取 ACCOUNTS_DIR 之前创建。
_TMP = isolated_data_dirs("wb-page-visibility-")

import wb_proxy as proxy  # noqa: E402
import wb_settings as S  # noqa: E402


class FakeRequest(object):
    """保存路由所需的最小 handler 替身。"""

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


class HiddenPagesTests(unittest.TestCase):
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
        """运行保存路由，返回它答出的错误。"""
        request = FakeRequest(payload)
        request._handle_settings_save()
        return [entry for entry in request.answering if entry[0] == "error"]

    def _view(self):
        return proxy.runtime_settings_view()["hidden_pages"]

    # --- 存储值 ------------------------------------------------------------

    def test_missing_key_hides_nothing(self):
        self.assertFalse(os.path.exists(S.settings_path(self.dir)))
        self.assertEqual(S.hidden_pages(self.dir), [])
        self.assertEqual(self._view(), [])

    def test_a_list_round_trips_in_order(self):
        self.assertEqual(S.set_hidden_pages(self.dir, ["tasks", "logs"]),
                         ["tasks", "logs"])
        self.assertEqual(S.hidden_pages(self.dir), ["tasks", "logs"])
        self.assertEqual(self._read()["hidden_pages"], ["tasks", "logs"])
        self.assertEqual(self._view(), ["tasks", "logs"])

    def test_setting_an_empty_list_shows_everything_again(self):
        S.set_hidden_pages(self.dir, ["tasks"])
        self.assertEqual(S.set_hidden_pages(self.dir, []), [])
        self.assertEqual(S.hidden_pages(self.dir), [])
        self.assertEqual(self._read()["hidden_pages"], [])
        self.assertEqual(self._view(), [])

    def test_unknown_page_keys_are_kept_not_rejected(self):
        # 本模块刻意不带页面注册表：没见过的 key 由面板自行忽略，在这里拒绝的话，
        # 上游每加一页都要动 wb_settings.py。
        S.set_hidden_pages(self.dir, ["a-page-added-later"])
        self.assertEqual(S.hidden_pages(self.dir), ["a-page-added-later"])

    def test_the_settings_page_is_never_hidden(self):
        # 双向禁止：存不进去，文件里就永远不会出现让开关回不了头的状态。
        self.assertEqual(S.set_hidden_pages(self.dir, ["settings"]), [])
        self.assertEqual(S.hidden_pages(self.dir), [])
        self.assertEqual(self._read()["hidden_pages"], [])
        self.assertEqual(S.set_hidden_pages(self.dir, ["tasks", "settings"]), ["tasks"])

    def test_duplicates_collapse(self):
        S.set_hidden_pages(self.dir, ["logs", "logs", "tasks", "logs"])
        self.assertEqual(S.hidden_pages(self.dir), ["logs", "tasks"])

    def test_surrounding_whitespace_is_trimmed(self):
        S.set_hidden_pages(self.dir, ["  logs ", "\ttasks\n"])
        self.assertEqual(S.hidden_pages(self.dir), ["logs", "tasks"])

    def test_malformed_values_hide_nothing(self):
        for stored in ("tasks", "settings", 1, 0, 1.0, True, False, None, {},
                       [None], [{}], [[]], [True], ["TASKS"],
                       ["../etc"], ["has space"], [""], ["-leading"], ["a" * 40]):
            with self.subTest(stored=stored):
                self._write({"hidden_pages": stored})
                self.assertEqual(S.hidden_pages(self.dir), [])
                self.assertEqual(self._view(), [])

    def test_a_string_is_not_iterated_into_page_keys(self):
        # "tasks" 当序列迭代会逐字符拆成 key 存下，没有页面对应这些 key，按
        # “没有隐藏”读回。
        self._write({"hidden_pages": "tasks"})
        self.assertEqual(S.hidden_pages(self.dir), [])

    def test_a_hand_written_list_is_cleaned_on_read(self):
        self._write({"hidden_pages": ["logs", "logs", 7, "settings", "ok_page", "bad key"]})
        self.assertEqual(S.hidden_pages(self.dir), ["logs", "ok_page"])

    def test_an_over_long_list_is_bounded(self):
        many = ["page-%d" % i for i in range(S.MAX_HIDDEN_PAGES + 20)]
        S.set_hidden_pages(self.dir, many)
        self.assertEqual(len(S.hidden_pages(self.dir)), S.MAX_HIDDEN_PAGES)
        self.assertEqual(S.hidden_pages(self.dir), many[:S.MAX_HIDDEN_PAGES])

    # --- 与其他设置共存 ----------------------------------------------------

    def test_saving_the_list_preserves_unrelated_settings(self):
        self._write({
            "user_custom_top": {"keep": 1},
            "limits": {"global": {"reserve_credits": 7}},
            "panel_password": {"salt": "ab", "digest": "cd", "rounds": 1},
            "key_before_hidden": True,
        })
        S.set_hidden_pages(self.dir, ["logs"])
        stored = self._read()
        self.assertEqual(stored["user_custom_top"], {"keep": 1})
        self.assertEqual(stored["limits"], {"global": {"reserve_credits": 7}})
        self.assertEqual(stored["panel_password"],
                         {"salt": "ab", "digest": "cd", "rounds": 1})
        self.assertIs(stored["key_before_hidden"], True)
        self.assertEqual(stored["hidden_pages"], ["logs"])

    def test_another_setting_write_keeps_the_list(self):
        S.set_hidden_pages(self.dir, ["logs"])
        S.set_accounts_collapsed(self.dir, True)
        self.assertEqual(S.hidden_pages(self.dir), ["logs"])
        self.assertIs(S.accounts_collapsed(self.dir), True)

    # --- 保存路由 ----------------------------------------------------------

    def test_save_route_stores_a_list_of_keys(self):
        self.assertEqual(self._save({"hidden_pages": ["tasks"]}), [])
        self.assertEqual(self._view(), ["tasks"])
        self.assertEqual(self._read()["hidden_pages"], ["tasks"])
        self.assertEqual(self._save({"hidden_pages": []}), [])
        self.assertEqual(self._view(), [])

    def test_save_route_normalises_what_it_stores(self):
        self.assertEqual(self._save({"hidden_pages": ["logs", "logs", "settings", "bad key"]}), [])
        self.assertEqual(self._read()["hidden_pages"], ["logs"])

    def test_save_route_rejects_anything_that_is_not_a_list_of_strings(self):
        # 裸字符串最值得点名：接受它会逐字符迭代，存下一堆垃圾 key。
        for bad in ("tasks", "logs", 1, 0, None, True, {}, {"tasks": 1},
                    ["tasks", 1], [None], [[]], [{"page": "tasks"}]):
            with self.subTest(value=bad):
                errors = self._save({"hidden_pages": bad})
                self.assertEqual(len(errors), 1, errors)
                self.assertEqual(errors[0][1], 400)
                self.assertEqual(S.hidden_pages(self.dir), [])

    def test_save_route_ignores_a_payload_that_omits_the_list(self):
        S.set_hidden_pages(self.dir, ["tasks"])
        self.assertEqual(self._save({"user_custom_top": {"keep": 2}}), [])
        self.assertEqual(S.hidden_pages(self.dir), ["tasks"])

    def test_the_view_carries_the_normalised_list(self):
        self._write({"hidden_pages": ["logs", "logs", "settings"]})
        self.assertEqual(self._view(), ["logs"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
