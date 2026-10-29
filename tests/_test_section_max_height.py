"""面板窗口限高以一个整数像素值持久化。

设置页的“面板窗口限高”字段限制看板窗口的高度：区块与日志终端超过限高就在内部
滚动、标题行固定，页面不被撑高。值存在 accounts/settings.json，与其他面板设置同处，
共享同一把锁与原子替换，界面不需要第二套持久化机制。

0 表示不限高，是有意义的值，getter 要把真正的 0 与损坏文件区分开。非整数一律读回
默认值，不抛异常：文件可手工编辑，本模块也不了解看板的渲染方式，坏值按默认限高
理解最安全。true 单独点名的原因：bool 是 int 的子类，手工写入的 `true` 否则会读成
1px 限高。超出上限的值双向夹到边界，与 credits_refresh_hours 一致。

不需要网络。用 python tests/_test_section_max_height.py 运行。
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

# 独立的 accounts/ 与 usage/，在 wb_proxy import 读取 ACCOUNTS_DIR 之前创建。
_TMP = isolated_data_dirs("wb-section-max-height-")

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


class SectionMaxHeightTests(unittest.TestCase):
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
        # 直接写盘绕过了模块自己的写路径，所以得自己作废它的读缓存：缓存键是
        # (path, st_mtime_ns, st_size)，两次写落在同一个系统时钟刻里、长度又一样时
        # 键完全撞上，第二次读回的是上一次的值（Windows 上就撞过）。
        S._load_cache.pop(S.settings_path(self.dir), None)

    def _read(self):
        with open(S.settings_path(self.dir), encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, payload):
        """运行保存路由，返回它答出的错误。"""
        request = FakeRequest(payload)
        request._handle_settings_save()
        return [entry for entry in request.answering if entry[0] == "error"]

    def _reply(self, payload):
        """运行保存路由，返回它答出的 JSON 回复。"""
        request = FakeRequest(payload)
        request._handle_settings_save()
        replies = [entry for entry in request.answering if entry[0] == "json"]
        self.assertEqual(len(replies), 1, replies)
        return replies[0][2]

    def _view(self):
        return proxy.runtime_settings_view()["section_max_height"]

    # --- 存储值 ------------------------------------------------------------

    def test_missing_key_reads_as_the_default(self):
        self.assertFalse(os.path.exists(S.settings_path(self.dir)))
        self.assertEqual(S.section_max_height(self.dir), S.DEFAULT_SECTION_MAX_HEIGHT)
        self.assertEqual(self._view(), S.DEFAULT_SECTION_MAX_HEIGHT)

    def test_a_value_round_trips(self):
        self.assertEqual(S.set_section_max_height(self.dir, 500), 500)
        self.assertEqual(S.section_max_height(self.dir), 500)
        self.assertEqual(self._read()["section_max_height"], 500)
        self.assertEqual(self._view(), 500)

    def test_zero_means_no_cap_and_survives_a_round_trip(self):
        # 0 必须原样读回 0，不能落到默认值：它是“关”的开关，getter 的默认回退
        # 不能吞掉它。
        self.assertEqual(S.set_section_max_height(self.dir, 0), 0)
        self.assertEqual(S.section_max_height(self.dir), 0)
        self.assertEqual(self._read()["section_max_height"], 0)
        self.assertEqual(self._view(), 0)

    def test_digits_in_a_string_are_accepted(self):
        # 与 credits_refresh_hours 一样，给手工编辑的文件留出的余量。
        self._write({"section_max_height": "500"})
        self.assertEqual(S.section_max_height(self.dir), 500)

    def test_a_float_truncates_to_whole_pixels(self):
        self._write({"section_max_height": 500.9})
        self.assertEqual(S.section_max_height(self.dir), 500)

    def test_negative_reads_as_the_default(self):
        # 不能读成 0：负数是损坏文件，读成 0 会悄悄关掉限高。与
        # credits_refresh_hours 同一种解读。
        self._write({"section_max_height": -5})
        self.assertEqual(S.section_max_height(self.dir), S.DEFAULT_SECTION_MAX_HEIGHT)

    def test_a_value_past_the_ceiling_is_clamped_not_rejected(self):
        self.assertEqual(S.set_section_max_height(self.dir, 99999),
                         S.MAX_SECTION_MAX_HEIGHT)
        self._write({"section_max_height": 99999})
        self.assertEqual(S.section_max_height(self.dir), S.MAX_SECTION_MAX_HEIGHT)
        self.assertEqual(self._view(), S.MAX_SECTION_MAX_HEIGHT)

    def test_the_setter_clamps_a_negative_to_off(self):
        # 路由在此之前就拒绝负数；直接调用时夹到 0，与
        # set_credits_refresh_hours 一致。
        self.assertEqual(S.set_section_max_height(self.dir, -5), 0)
        self.assertEqual(S.section_max_height(self.dir), 0)

    def test_malformed_values_read_as_the_default(self):
        for stored in ("abc", "640px", "", "  ", None, {}, [], [640], {}, 1e400,
                       float("nan"), float("inf")):
            with self.subTest(stored=stored):
                self._write({"section_max_height": stored})
                self.assertEqual(S.section_max_height(self.dir),
                                 S.DEFAULT_SECTION_MAX_HEIGHT)

    def test_a_boolean_reads_as_the_default(self):
        # bool 是 int 的子类：true 否则会读成 1px 限高。
        for stored in (True, False):
            with self.subTest(stored=stored):
                self._write({"section_max_height": stored})
                self.assertEqual(S.section_max_height(self.dir),
                                 S.DEFAULT_SECTION_MAX_HEIGHT)

    def test_the_setter_treats_a_boolean_as_the_default(self):
        self.assertEqual(S.set_section_max_height(self.dir, True),
                         S.DEFAULT_SECTION_MAX_HEIGHT)
        self.assertEqual(S.set_section_max_height(self.dir, False),
                         S.DEFAULT_SECTION_MAX_HEIGHT)

    # --- 与其他设置共存 ----------------------------------------------------

    def test_saving_the_value_preserves_unrelated_settings(self):
        self._write({
            "user_custom_top": {"keep": 1},
            "limits": {"global": {"reserve_credits": 7}},
            "panel_password": {"salt": "ab", "digest": "cd", "rounds": 1},
            "hidden_pages": ["tasks"],
        })
        S.set_section_max_height(self.dir, 500)
        stored = self._read()
        self.assertEqual(stored["user_custom_top"], {"keep": 1})
        self.assertEqual(stored["limits"], {"global": {"reserve_credits": 7}})
        self.assertEqual(stored["panel_password"],
                         {"salt": "ab", "digest": "cd", "rounds": 1})
        self.assertEqual(stored["hidden_pages"], ["tasks"])
        self.assertEqual(stored["section_max_height"], 500)

    def test_another_setting_write_keeps_the_value(self):
        S.set_section_max_height(self.dir, 500)
        S.set_hidden_pages(self.dir, ["logs"])
        self.assertEqual(S.section_max_height(self.dir), 500)
        self.assertEqual(S.hidden_pages(self.dir), ["logs"])

    # --- 保存路由 ----------------------------------------------------------

    def test_save_route_stores_the_value_and_replies_with_it(self):
        reply = self._reply({"section_max_height": 500})
        self.assertEqual(reply["section_max_height"], 500)
        self.assertEqual(self._view(), 500)
        self.assertEqual(self._read()["section_max_height"], 500)

    def test_save_route_accepts_zero(self):
        self.assertEqual(self._save({"section_max_height": 0}), [])
        self.assertEqual(self._view(), 0)
        self.assertEqual(self._read()["section_max_height"], 0)

    def test_save_route_clamps_a_value_past_the_ceiling(self):
        reply = self._reply({"section_max_height": 99999})
        self.assertEqual(reply["section_max_height"], S.MAX_SECTION_MAX_HEIGHT)
        self.assertEqual(self._view(), S.MAX_SECTION_MAX_HEIGHT)

    def test_save_route_rejects_anything_that_is_not_a_whole_number(self):
        # True 最值得点名：int 的子类，接受它会存下 1px 限高，所有窗口只剩一条缝。
        # inf 是另一个：JSON 载荷可能带 Infinity，int() 对它抛 OverflowError，
        # 要答 400，绝不能答 500。
        for bad in (True, False, None, "abc", "640px", "", {}, [], [640], -1, -0.5,
                    float("inf"), float("nan")):
            with self.subTest(value=bad):
                errors = self._save({"section_max_height": bad})
                self.assertEqual(len(errors), 1, errors)
                self.assertEqual(errors[0][1], 400)
                self.assertEqual(S.section_max_height(self.dir),
                                 S.DEFAULT_SECTION_MAX_HEIGHT)

    def test_save_route_ignores_a_payload_that_omits_the_value(self):
        S.set_section_max_height(self.dir, 500)
        self.assertEqual(self._save({"user_custom_top": {"keep": 2}}), [])
        self.assertEqual(S.section_max_height(self.dir), 500)

    def test_the_view_carries_the_normalised_value(self):
        self._write({"section_max_height": 99999})
        self.assertEqual(self._view(), S.MAX_SECTION_MAX_HEIGHT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
