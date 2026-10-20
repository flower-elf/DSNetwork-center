"""面板登录限流的键：反向代理下按真实客户端 IP 分桶，公网伪造头无效。

面板登录限流按客户端真实地址分桶。反向代理已经发来 X-Real-IP 或
X-Forwarded-For 时，只有可信对端才读取这些头；否则所有经代理的请求都会
落进同一个代理地址桶，一个客户端连错 5 次就会把其他客户端一起锁住。

套件钉两件事：

  1. 键的判定（login_rate_limit_key）：仅当对端可信（回环地址，或显式列进
     WB_TRUSTED_PROXIES 的代理）才读代理头，X-Real-IP 优先、X-Forwarded-For
     取链上第一个；其余对端一律退回 socket 对端地址，且任何多值/带端口/
     非法/超长的头都必须安静地退避，不能抛异常。
  2. 限流语义：仍是每 IP 5 次失败 / 60 秒，成功即清桶，裁剪逻辑保持原样。

前半是纯函数用例；后半把真实的 do_POST 拉起来跑一遍（键算在处理器里，只有
走分发器才算数），验证一个客户端连错 5 次、其他客户端不受影响，以及缓存
预热器不被 429 连带锁住。

    python tests/_test_login_ratelimit_proxy.py
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

from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)

_TMP = isolated_data_dirs("wb-login-ratelimit-")

import wb_proxy as proxy  # noqa: E402
import wb_settings  # noqa: E402

WRONG_PASSWORD = "definitely-not-the-panel-password"
RIGHT_PASSWORD = wb_settings.DEFAULT_PANEL_PASSWORD


class RateLimitKeyTests(unittest.TestCase):
    """键怎么算：可信来源读头，不可信来源忽略头，畸形头退避。"""

    def setUp(self):
        # 默认名单为空；需要显式可信代理的用例自己覆盖。
        patcher = mock.patch.object(proxy, "TRUSTED_PROXIES", ())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_loopback_peer_uses_x_real_ip(self):
        self.assertEqual(
            proxy.login_rate_limit_key("127.0.0.1", {"X-Real-IP": "203.0.113.7"}),
            "203.0.113.7")

    def test_ipv6_loopback_and_mapped_forms_are_trusted(self):
        # Windows 双栈下连 127.0.0.1 的对端会显示成 ::ffff:127.0.0.1，
        # 它同样必须是"本机代理"。
        for peer in ("::1", "::ffff:127.0.0.1"):
            with self.subTest(peer=peer):
                self.assertEqual(
                    proxy.login_rate_limit_key(peer, {"X-Real-IP": "203.0.113.8"}),
                    "203.0.113.8")

    def test_x_real_ip_wins_over_forwarded_for(self):
        headers = {"X-Real-IP": "203.0.113.7",
                   "X-Forwarded-For": "198.51.100.9, 10.0.0.1"}
        self.assertEqual(proxy.login_rate_limit_key("127.0.0.1", headers),
                         "203.0.113.7")

    def test_forwarded_for_takes_the_first_hop(self):
        headers = {"X-Forwarded-For": "198.51.100.9, 10.0.0.1, 172.16.0.3"}
        self.assertEqual(proxy.login_rate_limit_key("127.0.0.1", headers),
                         "198.51.100.9")

    def test_host_port_forms_are_accepted(self):
        # nginx 的几种变体写法（带端口 / 带方括号的 IPv6）都要认；
        # X-Forwarded-For 链上第一个带端口同样要认。
        cases = [
            ("X-Real-IP", "203.0.113.7:443", "203.0.113.7"),
            ("X-Real-IP", "[2001:db8::5]:8443", "2001:db8::5"),
            ("X-Forwarded-For", "198.51.100.9:443, 10.0.0.1", "198.51.100.9"),
        ]
        for header, raw, want in cases:
            with self.subTest(header=header, raw=raw):
                self.assertEqual(
                    proxy.login_rate_limit_key("127.0.0.1", {header: raw}), want)

    def test_malformed_headers_fall_back_without_raising(self):
        # 多值/非法/超长/半截方括号/非数字端口：X-Real-IP 不可用就退回对端。
        unusable = [
            "not-an-ip", "203.0.113.7, 198.51.100.9", "", "x" * 300,
            "203.0.113.7:", "unknown", "[2001:db8::5", "203.0.113.7:port",
        ]
        for raw in unusable:
            with self.subTest(raw=raw[:30]):
                self.assertEqual(
                    proxy.login_rate_limit_key("127.0.0.1", {"X-Real-IP": raw}),
                    "127.0.0.1")
        # X-Forwarded-For 整条链都是垃圾：同样退回对端。
        self.assertEqual(
            proxy.login_rate_limit_key(
                "127.0.0.1", {"X-Forwarded-For": "garbage, also-garbage"}),
            "127.0.0.1")

    def test_malformed_x_real_ip_falls_through_to_the_chain(self):
        headers = {"X-Real-IP": "1.2.3.4,5.6.7.8",
                   "X-Forwarded-For": "198.51.100.9"}
        self.assertEqual(proxy.login_rate_limit_key("127.0.0.1", headers),
                         "198.51.100.9")

    def test_untrusted_peer_ignores_forged_headers(self):
        forged = {"X-Real-IP": "203.0.113.7",
                  "X-Forwarded-For": "198.51.100.9"}
        self.assertEqual(proxy.login_rate_limit_key("192.168.1.50", forged),
                         "192.168.1.50")

    def test_configured_trusted_proxy_is_believed(self):
        with mock.patch.object(proxy, "TRUSTED_PROXIES",
                               proxy.parse_trusted_proxies("10.0.0.1, fd00::/8")):
            self.assertEqual(
                proxy.login_rate_limit_key("10.0.0.1", {"X-Real-IP": "203.0.113.7"}),
                "203.0.113.7")
            self.assertEqual(
                proxy.login_rate_limit_key("fd00::9", {"X-Real-IP": "203.0.113.8"}),
                "203.0.113.8")
            # 不在名单里的对端仍然不认头。
            self.assertEqual(
                proxy.login_rate_limit_key("10.0.0.2", {"X-Real-IP": "203.0.113.9"}),
                "10.0.0.2")

    def test_parse_trusted_proxies_drops_junk(self):
        nets = proxy.parse_trusted_proxies("10.0.0.1, fd00::/8, junk, , 300.1.2.3")
        self.assertEqual([str(n) for n in nets], ["10.0.0.1/32", "fd00::/8"])

    def test_unparsable_peer_is_not_trusted_and_never_raises(self):
        self.assertFalse(proxy.peer_is_trusted(""))
        self.assertFalse(proxy.peer_is_trusted(None))
        self.assertFalse(proxy.peer_is_trusted("garbage"))


class PruneTests(unittest.TestCase):
    """清理/裁剪逻辑保持原样：过期的丢、新鲜的留、空桶删掉。"""

    def test_prune_keeps_recent_drops_stale_and_removes_empty_buckets(self):
        with mock.patch.object(proxy, "_login_attempts",
                               {"a": [10.0, 70.0], "b": [10.0], "c": [100.0]}):
            proxy._prune_login_attempts(now=100.0, window=60)
            self.assertEqual(proxy._login_attempts,
                             {"a": [70.0], "c": [100.0]})


class LoginDispatch(proxy.Handler):
    """真实 do_POST 驱动的登录请求。

    限流键算在处理器里，所以必须走真实分发器（do_POST -> _handle_panel），
    直接调处理器内部方法就绕过了本套件要验证的那几行。
    """

    def __init__(self, headers=None, body=b"", client=("127.0.0.1", 40000)):
        self.path = "/panel/login"
        self.command = "POST"
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        self.client_address = client
        merged = {"Content-Length": str(len(body))}
        merged.update(headers or {})
        raw = "".join("%s: %s\r\n" % item for item in merged.items())
        self.headers = email.message_from_string(raw)
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        pass

    def dispatch(self):
        self.do_POST()
        raw = self.wfile.getvalue().decode("utf-8")
        payload = json.loads(raw) if raw else None
        return self.status, payload


class LoginFlowTests(unittest.TestCase):
    """走真实 do_POST 的登录：分桶、阈值、窗口都在这里钉住。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="login-ratelimit-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name
        proxy._login_attempts.clear()
        self.addCleanup(proxy._login_attempts.clear)
        # 错密码路径有 0.5s 反爆破延迟；这里只验证限流语义，不替它计时。
        sleep = mock.patch.object(proxy.time, "sleep", lambda _s: None)
        sleep.start()
        self.addCleanup(sleep.stop)
        trusted = mock.patch.object(proxy, "TRUSTED_PROXIES", ())
        trusted.start()
        self.addCleanup(trusted.stop)

    def login(self, password, headers=None, client=("127.0.0.1", 40000)):
        body = json.dumps({"password": password}).encode("utf-8")
        request = LoginDispatch(headers=headers, body=body, client=client)
        with mock.patch.object(proxy, "ACCOUNTS_DIR", self.directory):
            return request.dispatch()

    def test_a_locked_out_client_does_not_lock_others_behind_the_proxy(self):
        """反向代理现场：同一对端（nginx）下 A 连错 5 次，B 不受影响。

        失败次数落在 A 的真实 IP 桶里；B 使用另一个真实 IP，仍能正常登录。
        """
        nginx = ("127.0.0.1", 40000)
        client_a = {"X-Real-IP": "203.0.113.7"}
        client_b = {"X-Real-IP": "203.0.113.8"}
        for _ in range(5):
            status, _ = self.login(WRONG_PASSWORD, client_a, nginx)
            self.assertEqual(status, 401)
        # 失败次数落在 A 的真实 IP 上，而不是代理的对端地址上。
        self.assertEqual(set(proxy._login_attempts), {"203.0.113.7"})
        status, payload = self.login(WRONG_PASSWORD, client_a, nginx)
        self.assertEqual(status, 429)
        self.assertIn("too many login attempts", payload["error"]["message"])
        # 同一时刻、同一个对端：B 用正确密码照常进入。
        status, payload = self.login(RIGHT_PASSWORD, client_b, nginx)
        self.assertEqual(status, 200)
        self.assertIs(payload["ok"], True)
        # 直连 8788（对端回环、无代理头）也按自己的桶走，不被 A 连坐。
        status, _ = self.login(RIGHT_PASSWORD, None, nginx)
        self.assertEqual(status, 200)

    def test_forged_headers_from_an_untrusted_peer_change_nothing(self):
        """公网/局域网对端伪造头：仍按对端地址分桶，换"新"IP 也不解锁。"""
        attacker = ("192.168.1.50", 40000)
        for i in range(5):
            status, _ = self.login(
                WRONG_PASSWORD, {"X-Real-IP": "203.0.113.%d" % i}, attacker)
            self.assertEqual(status, 401)
        self.assertEqual(set(proxy._login_attempts), {"192.168.1.50"})
        # 第 6 次换一个伪造 IP：桶是同一把，照样 429。
        status, _ = self.login(
            WRONG_PASSWORD, {"X-Real-IP": "203.0.113.99"}, attacker)
        self.assertEqual(status, 429)
        # 正确密码同样被拦——锁的是这个对端，不是它声称的身份。
        status, _ = self.login(
            RIGHT_PASSWORD, {"X-Real-IP": "203.0.113.99"}, attacker)
        self.assertEqual(status, 429)

    def test_forwarded_for_first_hop_is_the_bucket(self):
        """只有 X-Forwarded-For 时（部分反向代理形态）按链上第一个地址分桶。"""
        chain_a = {"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
        chain_b = {"X-Forwarded-For": "203.0.113.8, 10.0.0.1"}
        for _ in range(5):
            status, _ = self.login(WRONG_PASSWORD, chain_a)
            self.assertEqual(status, 401)
        status, _ = self.login(WRONG_PASSWORD, chain_a)
        self.assertEqual(status, 429)
        self.assertIn("203.0.113.7", proxy._login_attempts)
        status, _ = self.login(RIGHT_PASSWORD, chain_b)
        self.assertEqual(status, 200)

    def test_window_and_threshold_are_unchanged(self):
        """阈值仍是 5 次 / 60 秒：第 5 次 401、第 6 次才 429，满 60 秒解锁。"""
        client = {"X-Real-IP": "203.0.113.7"}
        with mock.patch.object(proxy.time, "time", return_value=1000.0):
            for _ in range(4):
                status, _ = self.login(WRONG_PASSWORD, client)
                self.assertEqual(status, 401)
            status, _ = self.login(WRONG_PASSWORD, client)      # 第 5 次
            self.assertEqual(status, 401)
            status, payload = self.login(WRONG_PASSWORD, client)  # 第 6 次
            self.assertEqual(status, 429)
            self.assertIn("wait 60s", payload["error"]["message"])
        with mock.patch.object(proxy.time, "time", return_value=1061.0):
            status, _ = self.login(RIGHT_PASSWORD, client)      # 窗口滑出
            self.assertEqual(status, 200)

    def test_a_successful_login_clears_the_bucket(self):
        client = {"X-Real-IP": "203.0.113.7"}
        for _ in range(3):
            status, _ = self.login(WRONG_PASSWORD, client)
            self.assertEqual(status, 401)
        status, _ = self.login(RIGHT_PASSWORD, client)
        self.assertEqual(status, 200)
        self.assertNotIn("203.0.113.7", proxy._login_attempts)
        # 清桶之后重新计数：再错 3 次不触发限流（旧账没有残留）。
        for _ in range(3):
            status, _ = self.login(WRONG_PASSWORD, client)
            self.assertEqual(status, 401)

    def test_the_cache_warmer_is_not_locked_out_by_a_remote_clients_failures(self):
        """预热器视角：远程客户端连错 5 次，本机来源的登录照常。

        预热器与 nginx 同机：走 nginx 时 nginx 给本机来源也写
        X-Real-IP（127.0.0.1），或预热器直连 8788。两种走法都按本机来源
        分桶，不会与远程客户端共用 "127.0.0.1" 一把锁。
        """
        remote = {"X-Real-IP": "203.0.113.7"}
        for _ in range(5):
            status, _ = self.login(WRONG_PASSWORD, remote, ("127.0.0.1", 40001))
            self.assertEqual(status, 401)
        status, _ = self.login(WRONG_PASSWORD, remote, ("127.0.0.1", 40001))
        self.assertEqual(status, 429)          # 远程客户端确实被锁了
        # 预热器走 nginx（对端回环 + X-Real-IP: 127.0.0.1）：200，不再 429。
        status, payload = self.login(
            RIGHT_PASSWORD, {"X-Real-IP": "127.0.0.1"}, ("127.0.0.1", 40002))
        self.assertEqual(status, 200)
        self.assertIs(payload["ok"], True)
        # 预热器直连 8788（无代理头）：同样 200。
        status, _ = self.login(RIGHT_PASSWORD, None, ("127.0.0.1", 40003))
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
