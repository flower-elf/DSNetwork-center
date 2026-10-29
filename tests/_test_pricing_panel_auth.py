"""测 GET /pricing 的认证边界：它归面板，API key 进不来，与写侧的 POST /pricing/refresh、POST /pricing/mapping 同一条边界。四档凭据逐个试：无凭据、有效 API key、错误面板口令、真实面板会话；/v1/models 必须继续只凭 API key 可读。用 python tests/run_all.py _test_pricing_panel_auth 运行，不联网。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _isolated_dirs import isolated_data_dirs  # noqa: E402  自带数据根目录

_TMP_ENV = isolated_data_dirs("wb-pricing-panel-auth-")

import wb_proxy as proxy
import wb_http as H          # noqa: E402
import wb_modelsdev          # noqa: E402
import wb_settings as settings    # noqa: E402

API_KEY = {"name": "client key", "key": "sk-client-abc", "enabled": True}

#: PRICING.status() 返回的内容，面板拿到的必须一字不差。
PRICING_DOCUMENT = {
    "interval_minutes": 30.0,
    "enabled": True,
    "running": False,
    "policies": 3,
    "models": 7,
    "logs": [{"at": "2026-01-01 00:00:00", "msg": "refreshed"}],
    "gaps": [],
    "overrides_file": "/srv/workbuddy/pricing_overrides.json",
    "policies_file": "/srv/workbuddy/pricing_policies.json",
    "timeline": "/srv/workbuddy/pricing_timeline.json",
}


class FakePricing(object):
    def status(self):
        return dict(PRICING_DOCUMENT)


class Request(object):
    """够跑真实派发器的最小请求对象。

    被测边界在 do_GET/do_POST 里，方法直接借自真实 Handler，只替换响应出口
    和 _error() 要用的两个 socket 辅助函数。
    """

    _is_panel_route = staticmethod(proxy.Handler._is_panel_route)
    _panel_token = proxy.Handler._panel_token
    _panel_ok = proxy.Handler._panel_ok
    _key_ok = proxy.Handler._key_ok
    _key_realm = proxy.Handler._key_realm
    _authorized = proxy.Handler._authorized
    _supplied_key = proxy.Handler._supplied_key
    _request_realm = proxy.Handler._request_realm
    _error = proxy.Handler._error
    _get_pricing = proxy.Handler._get_pricing
    _get_v1_models = proxy.Handler._get_v1_models
    do_GET = proxy.Handler.do_GET
    do_POST = proxy.Handler.do_POST

    def __init__(self, path, headers=None):
        self.path = path
        self.headers = dict(headers or {})
        self.key_entry = None
        self.status = None
        self.body = None

    # 测试请求背后没有 socket。
    def _handle_expect_continue(self):
        pass

    def _discard_body(self):
        pass

    def _json(self, code, obj):
        self.status, self.body = code, obj


class PanelRouteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="panel-route-")
        self.addCleanup(self._cleanup)
        patches = [
            mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory,
                                API_KEY=None, POOL=None),
            mock.patch.object(proxy, "PRICING", FakePricing()),
            mock.patch.object(H, "fetch_models",
                              lambda realm=None: [("gpt-4o", {})]),
            mock.patch.object(wb_modelsdev, "refresh_async",
                              lambda *a, **k: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        # 配置了启用的 key，部署才进入需要认证的模式，匿名档才真正匿名。
        settings.set_api_keys(self.directory, [API_KEY])

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.directory, ignore_errors=True)

    def request(self, path, key=None, panel=None):
        headers = {}
        if key:
            headers["Authorization"] = "Bearer " + key
        if panel:
            headers["X-Panel-Token"] = panel
        return Request(path, headers)

    def assert_panel_rejected(self, request):
        """401 来自面板边界，与 API key 检查无关。

        区分二者是关键：有效的 API key 在这里也必须被拒，所以报错消息必须
        点名面板，不能说 key 的问题。
        """
        request.do_GET()
        self.assertIsNotNone(request.body, "请求没有被派发处理")
        self.assertEqual(request.status, 401)
        message = request.body["error"]["message"]
        self.assertIn("panel password required", message)
        self.assertNotIn("api key", message)

    def test_anonymous_cannot_read_pricing(self):
        self.assert_panel_rejected(self.request("/pricing"))

    def test_a_valid_api_key_alone_cannot_read_pricing(self):
        self.assert_panel_rejected(self.request("/pricing", key=API_KEY["key"]))

    def test_a_wrong_panel_token_cannot_read_pricing(self):
        self.assert_panel_rejected(self.request("/pricing",
                                                panel="not-a-real-session"))

    def test_a_panel_session_still_reads_the_pricing_document(self):
        request = self.request("/pricing", panel=proxy.PANEL.create())
        request.do_GET()
        self.assertEqual(request.status, 200)
        self.assertEqual(request.body, PRICING_DOCUMENT)

    def test_a_panel_session_can_read_pricing_without_any_api_key(self):
        """面板照常工作，这是另一半契约。"""
        request = self.request("/pricing", panel=proxy.PANEL.create())
        request.do_GET()
        self.assertEqual(request.status, 200)
        self.assertEqual(request.body["policies"], PRICING_DOCUMENT["policies"])

    def test_v1_models_stays_readable_with_an_api_key_alone(self):
        request = self.request("/v1/models", key=API_KEY["key"])
        request.do_GET()
        self.assertEqual(request.status, 200)
        self.assertEqual([entry["id"] for entry in request.body["data"]],
                         ["gpt-4o"])

    def test_v1_models_is_not_part_of_the_panel_boundary(self):
        request = self.request("/v1/models")
        request.do_GET()
        self.assertEqual(request.status, 401)
        self.assertIn("api key", request.body["error"]["message"])

    def test_pricing_writes_stay_panel_only(self):
        for path in ("/pricing/refresh", "/pricing/mapping"):
            with self.subTest(path=path):
                request = self.request(path, key=API_KEY["key"])
                request.do_POST()
                self.assertEqual(request.status, 401)
                self.assertIn("panel password required",
                              request.body["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
