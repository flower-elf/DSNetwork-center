"""设置写入不得丢掉面板不管的键。

网页表单只提交它认识的键。如果 setter 整组替换对象，旁边手写的或将来的键
就会在下一次保存时消失，而且悄无声息，只有加过键的人会发现。本套件在面板
唯一的成组 setter 上钉住深合并契约，外加纯保存路径本来就有的顶层行为。

    python tests/_test_settings_preservation.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_settings
import wb_usage as U


class SettingsPreservationTests(unittest.TestCase):
    def _write(self, directory, data):
        with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _read(self, directory):
        with open(wb_settings.settings_path(directory), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_upstream_group_keeps_unknown_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {
                "user_custom_top": {"keep": 1},
                "upstream": {
                    "header_timeout_seconds": 120,
                    "unknown_upstream_key": 42,
                    "nested": {"deep": 7},
                },
            })
            wb_settings.set_upstream_config(directory, {"idle_timeout_seconds": 600})
            stored = self._read(directory)
        self.assertEqual(stored["upstream"]["unknown_upstream_key"], 42)
        self.assertEqual(stored["upstream"]["nested"], {"deep": 7})
        self.assertEqual(stored["upstream"]["header_timeout_seconds"], 120)
        self.assertEqual(stored["upstream"]["idle_timeout_seconds"], 600)
        self.assertEqual(stored["user_custom_top"], {"keep": 1})

    def test_known_values_still_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {"upstream": {}})
            wb_settings.set_upstream_config(directory, {"header_timeout_seconds": 45})
            view = wb_settings.upstream_config(directory)
            stored = self._read(directory)
        self.assertEqual(view["header_timeout_seconds"], 45)
        self.assertEqual(stored["upstream"]["header_timeout_seconds"], 45)

    def test_deep_merge_is_recursive_and_pure(self):
        base = {"a": 1, "nested": {"x": 1, "keep": True}}
        patch = {"nested": {"x": 2}, "b": 3}
        merged = wb_settings.deep_merge(base, patch)
        self.assertEqual(merged, {"a": 1, "nested": {"x": 2, "keep": True}, "b": 3})
        self.assertEqual(base, {"a": 1, "nested": {"x": 1, "keep": True}})
        self.assertEqual(patch, {"nested": {"x": 2}, "b": 3})
        # 非字典补丁直接替换该槽位，与普通赋值一致。
        self.assertEqual(wb_settings.deep_merge({"a": 1}, None), None)
        self.assertEqual(wb_settings.deep_merge(None, {"a": 1}), {"a": 1})


class UsageRetentionTests(unittest.TestCase):
    """用量聚合保留窗口：面板可设、有默认、有上限，面板设置优先于环境变量。

    这三个值决定 checkpoint 能覆盖多久（空间换时间），所以「面板没设过就是
    内置默认」和「显式设置优先、环境变量兜底」都要钉住——前者保证升级不改
    行为，后者保证现场调与测试里来回切的既有用法不被打断。
    """

    def _write(self, directory, data):
        with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_defaults_when_unset(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {})
            self.assertEqual(wb_settings.usage_config(directory),
                             {"hour_keep_hours": 48, "day_keep_days": 32})

    def test_round_trip_and_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {
                "usage": {"unknown_usage_key": 9, "nested": {"deep": 1}},
            })
            wb_settings.set_usage_config(directory, {"hour_keep_hours": 120})
            with open(wb_settings.settings_path(directory), encoding="utf-8") as fh:
                stored = json.load(fh)
            view = wb_settings.usage_config(directory)
        self.assertEqual(view["hour_keep_hours"], 120)
        self.assertEqual(view["day_keep_days"], 32)          # 未设的一侧仍是默认
        self.assertEqual(stored["usage"]["unknown_usage_key"], 9)
        self.assertEqual(stored["usage"]["nested"], {"deep": 1})

    def test_validation_rejects_bad_values(self):
        for bad in ({"hour_keep_hours": 0}, {"hour_keep_hours": -1},
                    {"day_keep_days": "32"}, {"day_keep_days": True},
                    {"day_keep_days": 1.5}, {"nope": 1}):
            with self.assertRaises(ValueError):
                wb_settings.validate_usage_patch(bad)

    def test_validation_rejects_absurd_values(self):
        # 上限防手滑：贴错位数会把 checkpoint 撑到几百 MB 且每轮写盘重写。
        with self.assertRaises(ValueError):
            wb_settings.validate_usage_patch(
                {"hour_keep_hours": wb_settings.USAGE_LIMITS["hour_keep_hours"] + 1})
        with self.assertRaises(ValueError):
            wb_settings.validate_usage_patch(
                {"day_keep_days": wb_settings.USAGE_LIMITS["day_keep_days"] + 1})

    def test_corrupt_stored_value_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {"usage": {"hour_keep_hours": "many",
                                              "day_keep_days": 99999}})
            view = wb_settings.usage_config(directory)
        self.assertEqual(view, {"hour_keep_hours": 48, "day_keep_days": 32})

    def test_keep_value_reports_the_effective_value(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {})
            self.assertIsNone(wb_settings.usage_keep_value(directory, "day_keep_days"))
            self._write(directory, {"usage": {"day_keep_days": 8}})
            self.assertEqual(
                wb_settings.usage_keep_value(directory, "day_keep_days"), 8)
            self.assertIsNone(wb_settings.usage_keep_value(directory, "hour_keep_hours"))
            self._write(directory, {"usage": {"day_keep_days": "many"}})
            self.assertIsNone(wb_settings.usage_keep_value(directory, "day_keep_days"))
            old = os.environ.get("WB_USAGE_DAY_KEEP_DAYS")
            try:
                os.environ["WB_USAGE_DAY_KEEP_DAYS"] = "2"
                self.assertEqual(
                    wb_settings.usage_keep_value(directory, "day_keep_days"), 2)
            finally:
                if old is None:
                    os.environ.pop("WB_USAGE_DAY_KEEP_DAYS", None)
                else:
                    os.environ["WB_USAGE_DAY_KEEP_DAYS"] = old

    def test_env_overrides_the_stored_value(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {})
            saved = {k: os.environ.get(k) for k in (
                "WB_USAGE_HOUR_KEEP_HOURS", "WB_USAGE_DAY_KEEP_DAYS")}
            try:
                os.environ["WB_USAGE_HOUR_KEEP_HOURS"] = "24"
                os.environ["WB_USAGE_DAY_KEEP_DAYS"] = "8"
                self.assertEqual(wb_settings.usage_effective(directory),
                                 {"hour_keep_hours": 24, "day_keep_days": 8})
                self._write(directory, {"usage": {"hour_keep_hours": 72}})
                self.assertEqual(wb_settings.usage_effective(directory),
                                 {"hour_keep_hours": 24, "day_keep_days": 8})
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_env_overrides_the_setting(self):
        import wb_proxy as P
        import wb_usage as U
        with tempfile.TemporaryDirectory() as directory:
            self._write(directory, {"usage": {"hour_keep_hours": 72,
                                              "day_keep_days": 8}})
            old_dir = P.ACCOUNTS_DIR
            P.ACCOUNTS_DIR = directory
            try:
                saved = {k: os.environ.get(k) for k in (
                    "WB_USAGE_DAY_KEEP_DAYS", "WB_USAGE_HOUR_KEEP_HOURS",
                    "WB_USAGE_HOUR_KEEP_DAYS")}
                for key in saved:
                    os.environ.pop(key, None)
                try:
                    self.assertEqual(U._usage_day_keep_days(), 8)
                    self.assertEqual(U._usage_hour_keep_hours(), 72)
                    # snapshot 的小时层按天剪枝，跟着同一个「小时层保留」走。
                    self.assertEqual(U._usage_hour_keep_days(), 3)
                    # 环境变量优先于面板值。
                    os.environ["WB_USAGE_DAY_KEEP_DAYS"] = "2"
                    os.environ["WB_USAGE_HOUR_KEEP_HOURS"] = "24"
                    os.environ["WB_USAGE_HOUR_KEEP_DAYS"] = "1"
                    self.assertEqual(U._usage_day_keep_days(), 2)
                    self.assertEqual(U._usage_hour_keep_hours(), 24)
                    self.assertEqual(U._usage_hour_keep_days(), 1)
                    self._write(directory, {})
                    self.assertEqual(U._usage_day_keep_days(), 2)
                    self.assertEqual(U._usage_hour_keep_hours(), 24)
                    self.assertEqual(U._usage_hour_keep_days(), 1)
                finally:
                    for key, value in saved.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
            finally:
                P.ACCOUNTS_DIR = old_dir


if __name__ == "__main__":
    unittest.main(verbosity=2)
