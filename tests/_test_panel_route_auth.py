"""管理路由只认面板会话，不认 API key。

看板的管理端点用面板会话访问，API key 只是模型 API 的凭证，不能代替面板密码。两道
边界分开，都在调度器执行，同一个界面的两半可能各自漂移，所以每个路由都按同一张表
检查。

本套件在内存 socket 上驱动真实 do_GET/do_POST，固定调度器的行为，覆盖整个管理面。
表里每个路由都给四种回答：匿名拒绝、仅有 API key 拒绝、面板 token 错误拒绝、真面板
会话放行。/agents 自己不带任何检查，只靠调度器前缀保护：契约是调度器执行的边界。

不联网：模型清单与发布检查器都用注入。用 python tests/_test_panel_route_auth.py 运行。
"""
import email
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _isolated_dirs import isolated_data_dirs  # noqa: E402  独立数据根目录

_TMP = isolated_data_dirs("wb-panel-route-auth-")

import wb_proxy as proxy
import wb_http as H  # noqa: E402
import wb_settings  # noqa: E402

KEY = "panel-route-key"
PANEL_ONLY_MESSAGE = "panel password required"
MODEL_ID = "injected-panel-route-model"
LATEST = "1.6.18"


def injected_models(realm=None):
    """模型清单，不访问任何上游。"""
    return [(MODEL_ID, {"name": "Injected"})]


class StubUpdates(object):
    """顶替发布检查器：被测的是边界。"""

    def __init__(self):
        self.manual_checks = 0
        self.last = {"current_version": proxy.running_version(),
                     "latest_version": None, "update_available": False}

    def check(self, manual=False):
        self.manual_checks += 1
        self.last = {"current_version": proxy.running_version(),
                     "latest_version": LATEST, "update_available": True}
        return dict(self.last)

    def status(self):
        return dict(self.last)


class DispatchRequest(proxy.Handler):
    """内存 socket 上驱动真实的 Handler。

    哪道边界拒绝管理路由属于调度器的性质，与端点方法无关，所以请求必须走真实
    的 do_GET/do_POST，测试才有意义。
    """

    def __init__(self, path, headers=None, command="GET", body=b""):
        self.path = path
        self.command = command
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        raw = "".join("%s: %s\r\n" % item for item in (headers or {}).items())
        self.headers = email.message_from_string(raw)
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        # 内存 socket 的对端就是回环；一键配置的可用性判定读的正是这个地址
        # （issue #246），远程那一侧在 RemoteAgentsTests 里改写它。
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        pass

    def dispatch(self):
        """运行真实调度器，返回状态码、JSON 内容与原始内容。"""
        getattr(self, "do_" + self.command)()
        raw = self.wfile.getvalue().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None      # 日志导出是纯文本，不是文档
        return self.status, payload, raw


class BoundaryCase(unittest.TestCase):
    """同一个管理面，每个用例一套独立设置目录。"""

    # 看板展示的管理读取路由，没有一个是模型 API。
    PANEL_ONLY_GET = ("/settings", "/updates", "/logs", "/tasks", "/scheduler",
                      "/accounts", "/usage", "/pricing", "/proxy/slots",
                      "/activity/history", "/agents", "/accounts/credits/grants")
    # 返回文件的路由也在同一道边界上。
    PANEL_ONLY_DOWNLOAD = ("/logs/export",)
    # 管理写入路由，各自在 do_POST 里有面板检查。/updates/check 是更新一对里的
    # 写入侧；两个 pricing 写入是看板自己的按钮提交的。
    PANEL_ONLY_POST = ("/updates/check", "/pricing/refresh",
                       "/pricing/mapping", "/settings/save",
                       "/catalog/refresh")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="panel-route-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name
        wb_settings.set_api_keys(self.directory, [
            {"id": "k1", "name": "k", "key": KEY, "enabled": True}])
        self.updates = StubUpdates()

    def dispatch(self, path, headers=None, command="GET", body=b""):
        request = DispatchRequest(path, headers=headers, command=command, body=body)
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory, API_KEY=None,
                                 POOL=None, PRICING=None, UPDATES=self.updates,
                                 SCHEDULER=None), \
                mock.patch.multiple(H, fetch_models=injected_models):
            return request.dispatch()

    def assert_refused(self, path, headers, command="GET", body=b""):
        status, payload, _raw = self.dispatch(path, headers, command, body)
        self.assertEqual(status, 401, "%s %s answered %s" % (command, path, status))
        self.assertIn(PANEL_ONLY_MESSAGE, payload["error"]["message"])
        # 拒绝本身就是完整回复：路由本会报告的内容不得随行泄露。
        self.assertEqual(list(payload), ["error"],
                         "%s %s leaked a payload with its refusal" % (command, path))
        return payload

    def assert_reached(self, path, command="GET", body=b""):
        status, payload, raw = self.dispatch(path, self.session(), command, body)
        self.assertEqual(status, 200, "%s %s answered %s" % (command, path, status))
        self.assertNotIn(PANEL_ONLY_MESSAGE, raw)
        if payload is not None:
            self.assertNotIn("error", payload)
        return payload

    def session(self):
        return {"X-Panel-Token": proxy.PANEL.create()}

    def key_only(self):
        return {"Authorization": "Bearer " + KEY}


class PanelOnlyGetTests(BoundaryCase):
    """每个管理读取路由只对面板会话开放。"""

    def test_an_anonymous_caller_is_refused(self):
        for path in self.PANEL_ONLY_GET:
            with self.subTest(path=path):
                self.assert_refused(path, {})

    def test_an_api_key_alone_is_refused(self):
        for path in self.PANEL_ONLY_GET:
            with self.subTest(path=path):
                self.assert_refused(path, self.key_only())

    def test_a_wrong_panel_token_is_refused(self):
        for path in self.PANEL_ONLY_GET:
            with self.subTest(path=path):
                self.assert_refused(path, {"X-Panel-Token": "not-a-session"})

    def test_a_panel_session_reaches_every_route(self):
        for path in self.PANEL_ONLY_GET:
            with self.subTest(path=path):
                self.assert_reached(path)

    def test_the_log_export_is_on_the_same_boundary(self):
        # 返回文件的路由也带同样四种回答。
        path = self.PANEL_ONLY_DOWNLOAD[0]
        self.assert_refused(path, {})
        self.assert_refused(path, self.key_only())
        self.assert_refused(path, {"X-Panel-Token": "not-a-session"})
        self.assert_reached(path)

    def test_the_update_status_is_not_readable_with_a_key(self):
        # 契约对应的事故在载荷上断言：拒绝回复里不得出现发布状态。
        payload = self.assert_refused("/updates", self.key_only())
        self.assertNotIn("current_version", json.dumps(payload))
        body = self.assert_reached("/updates")
        self.assertEqual(body["current_version"], proxy.running_version())


class PanelOnlyPostTests(BoundaryCase):
    """管理写入在同一道边界上，没有特例。"""

    def test_an_anonymous_caller_is_refused(self):
        for path in self.PANEL_ONLY_POST:
            with self.subTest(path=path):
                self.assert_refused(path, {}, command="POST", body=b"{}")

    def test_an_api_key_alone_is_refused(self):
        for path in self.PANEL_ONLY_POST:
            with self.subTest(path=path):
                self.assert_refused(path, self.key_only(), command="POST", body=b"{}")

    def test_a_wrong_panel_token_is_refused(self):
        for path in self.PANEL_ONLY_POST:
            with self.subTest(path=path):
                self.assert_refused(path, {"X-Panel-Token": "not-a-session"},
                                    command="POST", body=b"{}")

    def test_a_panel_session_reaches_every_route(self):
        for path in self.PANEL_ONLY_POST:
            with self.subTest(path=path):
                self.assert_reached(path, command="POST", body=b"{}")

    def test_the_manual_update_check_is_panel_only_too(self):
        # 更新一对的另一半。它不带请求体，也证明拒绝发生在读请求体之前。
        self.assert_refused("/updates/check", self.key_only(), command="POST")
        body = self.assert_reached("/updates/check", command="POST")
        self.assertIs(body["ok"], True)
        self.assertEqual(body["latest_version"], LATEST)
        self.assertEqual(self.updates.manual_checks, 1,
                         "only the session may spend the check")


class RemoteAgentsTests(BoundaryCase):
    """一键配置只在「浏览器和网关同一台机器」时可用（issue #246）。

    判据是请求来源地址：远程看板（手机 / 另一台电脑 / 容器端口映射）即使拿着
    面板会话，也读不到客户端清单、更写不了网关所在机器的客户端配置。环境标记
    （容器 / OpenWrt）另算一道，命中时连回环来源也不放行。
    """

    def dispatch_remote(self, path, headers=None, command="GET", body=b"",
                        deployment=None):
        request = DispatchRequest(path, headers=headers, command=command, body=body)
        request.client_address = ("192.168.1.20", 40000)
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory, API_KEY=None,
                                 POOL=None, PRICING=None, UPDATES=self.updates,
                                 SCHEDULER=None, fetch_models=injected_models,
                                 _SERVER_DEPLOYMENT=deployment):
            return request.dispatch()

    def test_a_remote_panel_reads_enabled_false(self):
        status, payload, _raw = self.dispatch_remote("/agents", self.session())
        self.assertEqual(status, 200)
        self.assertIs(payload.get("enabled"), False, payload)
        self.assertNotIn("clients", payload)
        self.assertNotIn("models", payload)

    def test_a_remote_panel_cannot_apply_or_restore(self):
        for path in ("/agents/apply", "/agents/restore"):
            with self.subTest(path=path):
                status, payload, _raw = self.dispatch_remote(
                    path, self.session(), command="POST",
                    body=b'{"client": "claude-code"}')
                self.assertEqual(status, 403, payload)
                self.assertIn("only available from the machine",
                              payload["error"]["message"])

    def test_the_capability_probe_answers_for_both_sides(self):
        # 看板启动时问的就是它：本机 true、远程 false，两侧都不 import
        # wb_agents、不做任何探测。
        request = DispatchRequest("/agents/available", headers=self.session())
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory, API_KEY=None,
                                 POOL=None, PRICING=None, UPDATES=self.updates,
                                 SCHEDULER=None, fetch_models=injected_models,
                                 _SERVER_DEPLOYMENT=False):
            status, payload, _raw = request.dispatch()
        self.assertEqual((status, payload), (200, {"enabled": True}))
        status, payload, _raw = self.dispatch_remote("/agents/available",
                                                    self.session(),
                                                    deployment=False)
        self.assertEqual((status, payload), (200, {"enabled": False}))

    def test_a_container_marker_disables_it_even_from_loopback(self):
        # --network host 的容器里，来源就是 127.0.0.1，但写进去的是容器里的
        # 配置目录；环境标记必须压过来源判定。
        request = DispatchRequest("/agents/available", headers=self.session())
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory, API_KEY=None,
                                 POOL=None, PRICING=None, UPDATES=self.updates,
                                 SCHEDULER=None, fetch_models=injected_models,
                                 _SERVER_DEPLOYMENT=True):
            status, payload, _raw = request.dispatch()
        self.assertEqual((status, payload), (200, {"enabled": False}))


class ApiKeySurfaceTests(BoundaryCase):
    """key 边界保持不变，它是另一道检查。"""

    def test_an_api_key_still_reaches_the_model_listing(self):
        status, body, _raw = self.dispatch("/v1/models", self.key_only())
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in body["data"]], [MODEL_ID])

    def test_an_anonymous_api_call_is_refused_as_a_key_problem(self):
        # 不给面板提示语：模型 API 从未迁到会话边界，忘带 key 的调用方要收到
        # key 相关的错误。
        status, body, _raw = self.dispatch("/v1/models")
        self.assertEqual(status, 401)
        self.assertIn("invalid api key", body["error"]["message"])
        self.assertNotIn(PANEL_ONLY_MESSAGE, body["error"]["message"])

    def test_a_panel_session_also_satisfies_the_key_check(self):
        # 面板边界不能用 `_authorized()` 的原因：会话也能通过该检查，它分不出
        # 会话与 key。更新状态读错边界靠的正是这个性质；这是已记录的行为，
        # 本套件只是把它固定下来。
        status, _body, _raw = self.dispatch("/v1/models", self.session())
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
