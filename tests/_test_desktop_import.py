"""桌面凭据导入：解密归一后进账号池，国内版账号以 account_import 来源签到。

不联网：签到请求由本地替身应答，at-rest 密钥回收换成固定测试密钥，只写临时目录。
"""
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-desktop-import-")

import wb_accounts  # noqa: E402
import wb_activity  # noqa: E402
import wb_atrest  # noqa: E402
import wb_identity  # noqa: E402


def fake_jwt(uid, issuer):
    def part(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "%s.%s.sig" % (
        part({"alg": "none"}),
        part({"sub": uid, "iss": issuer, "exp": int(time.time()) + 86400}))


class DesktopImportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-desktop-import-")
        self.addCleanup(self._tmp.cleanup)
        self.pool_dir = os.path.join(self._tmp.name, "accounts")
        self.pool = wb_accounts.AccountPool(self.pool_dir)

    def _write_credential(self, name, blob):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(blob, fh, ensure_ascii=False)
        return path

    def test_cn_credential_imports_and_checks_in_as_account_import(self):
        token = fake_jwt("uid-desk", "https://www.workbuddy.cn/oauth")
        expires_ms = int(time.time() * 1000) + 3600 * 1000
        path = self._write_credential("workbuddy-desktop.info", {
            "account": {"uid": "uid-desk", "nickname": "桌面用户"},
            "auth": {"accessToken": token, "refreshToken": "refresh-desk",
                     "domain": "copilot.tencent.com",
                     "expiresAt": expires_ms},
        })
        seen = []

        def fake_http_json(url, **kwargs):
            seen.append(url)
            return {"code": 0, "msg": "ok", "data": {}}

        with mock.patch.object(wb_accounts, "http_json", fake_http_json):
            account = self.pool.import_desktop_credential(path=path)
        # 解密归一后入库：字段按桌面凭据的口径映射。
        self.assertEqual(account.uid, "uid-desk")
        self.assertEqual(account.nickname, "桌面用户")
        self.assertEqual(account.realm, "cn")
        self.assertEqual(account.source, "desktop-app")
        self.assertEqual(account.product, wb_identity.PRODUCT_DESKTOP)
        self.assertEqual(account.login_product, wb_identity.PRODUCT_DESKTOP)
        self.assertEqual(account.access_token, token)
        self.assertEqual(account.refresh_token, "refresh-desk")
        self.assertLess(abs(account.expires_at - (time.time() + 3600)), 120)
        self.assertIs(self.pool.get("uid-desk"), account)
        self.assertTrue(os.path.exists(os.path.join(self.pool_dir,
                                                    "uid-desk.json")))
        reloaded = wb_accounts.AccountPool(self.pool_dir)
        reloaded.load()
        self.assertEqual([a.uid for a in reloaded.accounts], ["uid-desk"])
        # 签到请求打到国内版计费端点。
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].endswith(wb_accounts.CHECKIN_PATH), seen[0])
        self.assertTrue(account.last_checkin)
        # 活动历史里的来源是 account_import。
        rows = wb_activity.query(uid="uid-desk", task="checkin")["rows"]
        self.assertEqual([row["trigger"] for row in rows], ["account_import"])
        self.assertIs(rows[0]["ok"], True)

    def test_encrypted_credential_is_decrypted_before_it_is_stored(self):
        # at-rest 密钥回收读取客户端进程内存，这里用固定测试密钥代替回收结果。
        key = os.urandom(32)
        wb_atrest.forget_key()
        self.addCleanup(wb_atrest.forget_key)
        token = fake_jwt("uid-enc", "https://www.workbuddy.ai/oauth")
        path = self._write_credential("workbuddy-desktop-ai.info", {
            "account": {"uid": "uid-enc",
                        "nickname": wb_atrest.seal(key, "加密昵称", "field")},
            "auth": {"accessToken": wb_atrest.seal(key, token, "field"),
                     "refreshToken": wb_atrest.seal(key, "refresh-enc", "field"),
                     "domain": "www.workbuddy.ai",
                     "expiresAt": int(time.time() * 1000) + 3600 * 1000},
        })
        calls = []

        def fail_if_called(url, **kwargs):
            calls.append(url)
            raise AssertionError("国际版导入不该签到")

        with mock.patch.object(wb_accounts, "desktop_atrest_key",
                               lambda log=None: key), \
                mock.patch.object(wb_accounts, "http_json", fail_if_called):
            account = self.pool.import_desktop_credential(path=path)
        self.assertEqual(account.access_token, token)
        self.assertEqual(account.refresh_token, "refresh-enc")
        self.assertEqual(account.nickname, "加密昵称")
        self.assertEqual(account.realm, "intl")
        self.assertIsNone(account.last_checkin)
        self.assertEqual(calls, [])
        self.assertEqual(wb_activity.query(uid="uid-enc")["rows"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
