"""静态 API Key 账号：ck_/pt_ 凭据与 OAuth token 是两条鉴权路径。

这里固定识别、落盘形状、生命周期、出站头、脱敏与导入导出，不访问上游。
"""

import base64
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import wb_accounts as A  # noqa: E402
import wb_identity as I  # noqa: E402
import wb_tasks as T  # noqa: E402

KEY = "ck_sampleid123.samplesecret456"


def _part(obj):
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


JWT = "%s.%s.sig" % (_part({"alg": "none"}),
                     _part({"sub": "u1", "exp": 4102444800}))
BASE = {"uid": "apikey-sampleid123", "accessToken": KEY}


def api_key_account(**extra):
    data = dict(BASE)
    data.update(extra)
    return A.Account(data)


class RecognitionTests(unittest.TestCase):
    def test_ck_and_pt_are_api_keys(self):
        self.assertTrue(I.is_api_key_token("ck_a.b"))
        self.assertTrue(I.is_api_key_token("pt_a.b"))

    def test_jwt_and_junk_are_not(self):
        for token in (JWT, "", "sk_live", "ck", "CK_upper.case", None, 123,
                      "ck_a", "ck_a.", "ck_.b", "pt_"):
            self.assertFalse(I.is_api_key_token(token), token)

    def test_id_segment_is_sanitized(self):
        self.assertEqual(I.api_key_id(KEY), "sampleid123")
        self.assertEqual(I.api_key_id("pt_xyz.abc"), "xyz")
        self.assertEqual(I.api_key_id("ck_a/../b.c"), "a")


class CredentialShapeTests(unittest.TestCase):
    def test_uid_and_realm_are_derived(self):
        account = api_key_account(uid="")
        self.assertEqual(account.uid, "apikey-sampleid123")
        self.assertEqual(account.realm, "cn")
        self.assertTrue(account.is_api_key)

    def test_realm_is_forced_to_cn(self):
        account = api_key_account(realm="intl")
        self.assertEqual(account.realm, "cn")

    def test_no_expiry_and_api_key_source(self):
        account = api_key_account()
        self.assertEqual(account.expires_at, 0)
        self.assertEqual(account.refresh_token, "")
        self.assertEqual(account.source, A.SOURCE_API_KEY)

    def test_factory_builds_the_same_shape(self):
        account = A.api_key_account(KEY, nickname="团队")
        self.assertEqual(account.uid, "apikey-sampleid123")
        self.assertEqual(account.nickname, "团队")
        self.assertTrue(account.is_api_key)
        self.assertEqual(account.product, "cli")

    def test_oauth_account_is_unaffected(self):
        account = A.Account({"uid": "o1", "accessToken": JWT})
        self.assertFalse(account.is_api_key)
        self.assertEqual(account.source, "oauth")
        self.assertGreater(account.expires_at, 0)

    def test_flagged_non_key_is_rejected(self):
        with self.assertRaises(ValueError):
            A.Account({"accessToken": JWT, "apiKey": True})

    def test_string_false_flag_is_ignored(self):
        account = A.Account({"accessToken": JWT, "apiKey": "false"})
        self.assertFalse(account.is_api_key)
        self.assertEqual(account.source, "oauth")


class LifecycleTests(unittest.TestCase):
    def test_ready_and_refresh_are_stable(self):
        account = api_key_account()
        self.assertTrue(account.ready())
        self.assertFalse(account.refresh())
        self.assertTrue(account.ready())
        self.assertEqual(account.last_error, "")

    def test_no_personal_programmes(self):
        account = api_key_account()
        self.assertFalse(account.has_personal_program())
        self.assertTrue(account.uses_enterprise_billing())
        self.assertFalse(account.can_refresh())
        self.assertFalse(account.can_checkin())
        self.assertFalse(account.can_report_activity())
        self.assertIn("API key", account.checkin().get("error", ""))
        self.assertIn("API key", T.report_activity(account).get("error", ""))

    def test_growth_summary_returns_empty_without_network(self):
        account = api_key_account()
        with mock.patch.object(T._accounts, "urlopen",
                               side_effect=AssertionError("must not call upstream")):
            summary = T.fetch_growth_summary(account)
        self.assertEqual(summary, {"energy": 0, "streak_days": 0,
                                   "travel": {"state": "unknown"}})


class OutboundHeaderTests(unittest.TestCase):
    def test_chat_and_billing_send_both_auth_headers(self):
        account = api_key_account()
        for purpose in ("chat", "billing"):
            headers = account.headers(purpose=purpose)
            self.assertEqual(headers.get("Authorization"), "Bearer " + KEY)
            self.assertEqual(headers.get("X-API-Key"), KEY)

    def test_oauth_account_does_not_send_x_api_key(self):
        account = A.Account({"uid": "o2", "accessToken": JWT})
        self.assertNotIn("X-API-Key", account.headers(purpose="chat"))
        self.assertNotIn("X-API-Key", account.headers(purpose="billing"))

    def test_identity_builder_uses_the_same_rule(self):
        self.assertEqual(I.auth_headers(KEY)["X-API-Key"], KEY)
        self.assertNotIn("X-API-Key", I.auth_headers(JWT))


class MaskTests(unittest.TestCase):
    def test_mask_has_no_secret(self):
        account = api_key_account()
        self.assertEqual(account.credential_mask(), "ck_sampleid123.***")
        public = json.dumps(account.public(), ensure_ascii=False)
        self.assertNotIn("samplesecret456", public)
        self.assertTrue(account.public()["apiKey"])

    def test_oauth_account_exposes_no_mask(self):
        account = A.Account({"uid": "o3", "accessToken": JWT})
        self.assertEqual(account.credential_mask(), "")
        self.assertFalse(account.public()["apiKey"])


class BillingTests(unittest.TestCase):
    def test_enterprise_usage_uses_the_v2_path(self):
        account = api_key_account()
        calls = []

        def fake_http(url, **kwargs):
            calls.append((url, kwargs.get("headers") or {}))
            return {"code": 0, "data": {"credit": 3, "limitNum": 10,
                                        "cycleEndTime": "2026-11-21 23:59:59"}}

        with mock.patch.object(A, "http_json", side_effect=fake_http):
            res = account._fetch_credits_enterprise()
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(calls[0][0].endswith(
            "/v2/billing/meter/get-enterprise-user-usage"), calls[0][0])
        self.assertEqual(calls[0][1].get("X-API-Key"), KEY)

    def test_credits_go_straight_to_enterprise(self):
        account = api_key_account()
        with mock.patch.object(A.Account, "_fetch_credits_enterprise",
                               return_value={"ok": True}) as ent:
            self.assertEqual(account._fetch_credits_raw(), {"ok": True})
        ent.assert_called_once()

    def test_probe_validates_url_and_headers(self):
        account = api_key_account()
        calls = []

        def fake_http(url, **kwargs):
            calls.append((url, kwargs.get("headers") or {}))
            return {"code": 0, "data": {"credit": 1, "limitNum": 2,
                                        "cycleEndTime": "2026-11-21 23:59:59"}}

        with mock.patch.object(A, "http_json", side_effect=fake_http):
            res = account.probe_api_key()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(calls[0][0],
                         "https://www.codebuddy.cn/v2/billing/meter/get-enterprise-user-usage")
        self.assertEqual(calls[0][1].get("X-API-Key"), KEY)

    def test_probe_rejects_incomplete_payloads(self):
        account = api_key_account()
        for data in ({}, {"credit": 1}, {"credit": "1", "limitNum": 2},
                     {"credit": 1, "limitNum": "2"}):
            with mock.patch.object(A, "http_json",
                                   return_value={"code": 0, "data": data}):
                res = account.probe_api_key()
            self.assertFalse(res.get("ok"), data)
            self.assertIn("enterprise usage", res.get("error", ""))

    def test_probe_apply_credits_reuses_the_snapshot(self):
        account = api_key_account()
        payload = {"code": 0, "data": {"credit": 3, "limitNum": 10,
                                       "cycleEndTime": "2026-11-21 23:59:59"}}
        with mock.patch.object(A, "http_json", return_value=payload) as call:
            res = account.probe_api_key(apply_credits=True)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(account.credits.get("size"), 10)
        self.assertEqual(account.credits.get("remain"), 7)
        self.assertNotIn("samplesecret", json.dumps(res, ensure_ascii=False))


class ImportTests(unittest.TestCase):
    def test_import_accepts_a_bare_key(self):
        kwargs = A.normalise_import_row({"accessToken": KEY})
        self.assertEqual(kwargs["uid"], "apikey-sampleid123")
        self.assertEqual(kwargs["realm"], "cn")
        self.assertEqual(kwargs["expiresAt"], 0)
        self.assertTrue(kwargs["apiKey"])
        self.assertEqual(kwargs["source"], A.SOURCE_API_KEY)

    def test_import_forces_cn(self):
        kwargs = A.normalise_import_row({"accessToken": KEY, "realm": "intl"})
        self.assertEqual(kwargs["realm"], "cn")

    def test_import_rejects_a_broken_jwt(self):
        with self.assertRaises(ValueError):
            A.normalise_import_row({"accessToken": "not-a-jwt"})

    def test_import_rejects_a_flagged_non_key(self):
        with self.assertRaises(ValueError):
            A.normalise_import_row({"accessToken": JWT, "apiKey": True})

    def test_import_ignores_a_string_flag(self):
        oauth = A.normalise_import_row({"accessToken": JWT, "apiKey": "false"})
        self.assertFalse(oauth["apiKey"])
        self.assertNotEqual(oauth["source"], A.SOURCE_API_KEY)
        keyed = A.normalise_import_row({"accessToken": KEY, "apiKey": "false"})
        self.assertTrue(keyed["apiKey"])
        self.assertEqual(keyed["source"], A.SOURCE_API_KEY)

    def test_import_keeps_explicit_uid(self):
        kwargs = A.normalise_import_row({"accessToken": KEY, "uid": "custom-id"})
        self.assertEqual(kwargs["uid"], "custom-id")

    def test_export_round_trip(self):
        account = api_key_account()
        exported = A.account_to_export(account)
        self.assertTrue(exported["apiKey"])
        kwargs = A.normalise_import_row(exported)
        self.assertEqual(kwargs["uid"], account.uid)
        self.assertEqual(kwargs["realm"], "cn")
        self.assertTrue(kwargs["apiKey"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
