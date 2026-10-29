"""测 API Key 整表保存的合并契约：只停用 deleted_api_key_ids 点名的 key，提交中省略的字段沿用原值。运行 python tests/_test_api_key_save_merge.py，无需网络。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as proxy
import wb_settings as S


class FakeRequest(object):
    """保存路径的最小 Handler 桩。"""

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


class KeySaveMergeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-keymerge-")
        proxy.ACCOUNTS_DIR = self.dir
        proxy.API_KEY = None
        proxy.API_KEY_FILE_SET = False

    def _save(self, payload):
        request = FakeRequest(payload)
        request._handle_settings_save()
        for kind, status, _ in request.answering:
            if kind == "error":
                self.fail("save rejected with %s" % status)
        return proxy.runtime_settings_view()

    def _live(self):
        return {e["id"]: e for e in proxy.runtime_settings_view()["api_keys"]}

    def _all(self):
        return {e["id"]: e for e in S.api_keys(self.dir, include_deleted=True)}

    # 存储契约

    def test_replace_mode_retires_an_omitted_key(self):
        """不带删除列表时沿用旧契约：提交中缺失的 key 会被停用。"""
        S.set_api_keys(self.dir, [
            {"id": "k1", "name": "a", "key": "key-aaaa1111", "realm": "intl"},
            {"id": "k2", "name": "b", "key": "key-bbbb2222", "realm": "cn"},
        ])
        saved = S.set_api_keys(self.dir, [
            {"id": "k1", "name": "a", "key": "key-aaaa1111", "realm": "intl"},
        ])
        self.assertEqual([e["id"] for e in saved], ["k1"])
        self.assertEqual(self._all()["k2"]["deleted_at"] != "", True)
        self.assertEqual(self._all()["k2"]["key"], "")

    def test_upsert_mode_keeps_an_omitted_key(self):
        """带删除列表时保存变为 upsert：提交中缺失不等于删除。"""
        S.set_api_keys(self.dir, [
            {"id": "k1", "name": "a", "key": "key-aaaa1111", "realm": "intl"},
            {"id": "k2", "name": "b", "key": "key-bbbb2222", "realm": "cn"},
        ])
        saved = S.set_api_keys(self.dir, [
            {"id": "k1", "name": "a", "key": "key-aaaa1111", "realm": "intl"},
        ], delete_ids=[])
        self.assertEqual(sorted(e["id"] for e in saved), ["k1", "k2"])
        self.assertEqual(self._all()["k2"]["deleted_at"], "")

    def test_upsert_mode_retires_exactly_the_named_ids(self):
        S.set_api_keys(self.dir, [
            {"id": "k1", "name": "a", "key": "key-aaaa1111"},
            {"id": "k2", "name": "b", "key": "key-bbbb2222"},
            {"id": "k3", "name": "c", "key": "key-cccc3333"},
        ])
        saved = S.set_api_keys(self.dir, [
            {"id": "k1", "name": "a", "key": "key-aaaa1111"},
            {"id": "k3", "name": "c", "key": "key-cccc3333"},
        ], delete_ids=["k2"])
        self.assertEqual(sorted(e["id"] for e in saved), ["k1", "k3"])
        self.assertNotEqual(self._all()["k2"]["deleted_at"], "")

    def test_upsert_mode_does_not_duplicate_a_resubmitted_secret(self):
        """竞态场景：同一 secret 带着空 id 被重新提交。面板存完第一把 key 后，在保存后的重载完成前又添加了一把，第一把因此再次进入提交；该 secret 必须只留一行存活，不产生停用的重复行。
        """
        S.set_api_keys(self.dir, [
            {"id": "old1", "name": "第一把", "key": "key-first-1111"},
        ])
        saved = S.set_api_keys(self.dir, [
            {"id": "", "name": "第一把", "key": "key-first-1111"},
            {"id": "", "name": "第二把", "key": "key-second-2222"},
        ], delete_ids=[])
        live = [e for e in saved]
        self.assertEqual(len(live), 2, live)
        self.assertEqual(sorted(e["name"] for e in live), ["第一把", "第二把"])
        secrets = sorted(e["key"] for e in live)
        self.assertEqual(secrets, ["key-first-1111", "key-second-2222"])

    # Handler 契约

    def test_handler_preserves_realm_enabled_and_name_when_omitted(self):
        S.set_api_keys(self.dir, [
            {"id": "k1", "name": "国际", "key": "key-intl-1111",
             "realm": "intl", "models": ["deepseek/*"], "enabled": True},
            {"id": "k2", "name": "国内停用", "key": "key-cn-2222",
             "realm": "cn", "models": [], "enabled": False},
        ])
        # 旧看板只认识 id/name/key/enabled，这里连第一行的 name 也省略。
        self._save({"api_keys": [
            {"id": "k1", "key": "", "enabled": True},
            {"id": "k2", "name": "国内停用", "key": "", "enabled": True},
            {"id": "", "name": "新Key", "key": "key-new-3333", "enabled": True},
        ]})
        live = self._live()
        self.assertEqual(live["k1"]["name"], "国际")
        self.assertEqual(live["k1"]["realm"], "intl")
        self.assertEqual(live["k1"]["models"], ["deepseek/*"])
        self.assertEqual(live["k2"]["realm"], "cn")
        # 这里显式提交了 enabled 为 true，因此按提交值生效。
        self.assertEqual(live["k2"]["enabled"], True)

    def test_handler_keeps_concurrency_limit_when_omitted_or_validates_it(self):
        S.set_api_keys(self.dir, [
            {"id": "k1", "name": "限流", "key": "key-limit-1111",
             "concurrency_limit": 4},
        ])
        self._save({"api_keys": [{"id": "k1", "name": "限流", "key": ""}],
                    "deleted_api_key_ids": []})
        self.assertEqual(self._live()["k1"]["concurrency_limit"], 4,
                         "旧看板不带该字段时不能把已配置的上限清零")
        self._save({"api_keys": [{"id": "k1", "name": "限流", "key": "",
                                  "concurrency_limit": 6}],
                    "deleted_api_key_ids": []})
        self.assertEqual(self._live()["k1"]["concurrency_limit"], 6)
        bad = FakeRequest({"api_keys": [{"id": "k1", "name": "限流", "key": "",
                                        "concurrency_limit": -1}],
                           "deleted_api_key_ids": []})
        bad._handle_settings_save()
        self.assertTrue(any(kind == "error" and status == 400
                            for kind, status, _msg in bad.answering), bad.answering)
        self.assertEqual(self._live()["k1"]["concurrency_limit"], 6,
                         "非法值必须拒绝，不能写回")

    def test_handler_keeps_disabled_state_when_enabled_omitted(self):
        S.set_api_keys(self.dir, [
            {"id": "k2", "name": "国内停用", "key": "key-cn-2222",
             "realm": "cn", "enabled": False},
        ])
        self._save({"api_keys": [
            {"id": "k2", "name": "国内停用", "key": ""},
            {"id": "", "name": "新Key", "key": "key-new-3333"},
        ]})
        live = self._live()
        self.assertEqual(live["k2"]["enabled"], False)
        self.assertEqual(live["k2"]["realm"], "cn")

    def test_handler_end_to_end_two_quick_adds_keep_both_keys(self):
        """复现上报的操作流：加一把 key 保存，再加一把保存。"""
        # 第一次保存：面板里只有这把新 key。
        self._save({"api_keys": [
            {"id": "", "name": "第一把", "realm": "intl", "models": [],
             "enabled": True, "key": "key-first-1111"},
        ], "deleted_api_key_ids": []})
        # 第二次保存与重载竞态：第一把 key 本地仍无 id，于是与第二把一起再次提交。
        self._save({"api_keys": [
            {"id": "", "name": "第一把", "realm": "intl", "models": [],
             "enabled": True, "key": "key-first-1111"},
            {"id": "", "name": "第二把", "realm": "cn", "models": [],
             "enabled": True, "key": "key-second-2222"},
        ], "deleted_api_key_ids": []})
        live = self._live()
        self.assertEqual(len(live), 2, live)
        self.assertEqual(sorted(e["name"] for e in live.values()),
                         ["第一把", "第二把"])
        self.assertEqual(sorted(e["realm"] for e in live.values()), ["cn", "intl"])
        retired = [e for e in S.api_keys(self.dir, include_deleted=True)
                   if e["deleted_at"]]
        self.assertEqual(retired, [])

    def test_handler_explicit_delete_still_works(self):
        self._save({"api_keys": [
            {"id": "", "name": "甲", "realm": "", "models": [], "enabled": True,
             "key": "key-aaaa1111"},
            {"id": "", "name": "乙", "realm": "", "models": [], "enabled": True,
             "key": "key-bbbb2222"},
        ], "deleted_api_key_ids": []})
        live = self._live()
        keep = next(e for e in live.values() if e["name"] == "甲")
        drop = next(e for e in live.values() if e["name"] == "乙")
        self._save({
            "api_keys": [{"id": keep["id"], "name": "甲", "realm": "",
                          "models": [], "enabled": True, "key": ""}],
            "deleted_api_key_ids": [drop["id"]],
        })
        live = self._live()
        self.assertEqual(sorted(e["name"] for e in live.values()), ["甲"])
        all_rows = S.api_keys(self.dir, include_deleted=True)
        retired = next(e for e in all_rows if e["id"] == drop["id"])
        self.assertNotEqual(retired["deleted_at"], "")
        self.assertEqual(retired["key"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
