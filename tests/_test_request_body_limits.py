"""请求体解析与几个管理接口的输入校验，对真实 Handler 发原始字节；无网络访问，只连本机临时端口。
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-body-limits-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
# 上限与客户端超时在模块导入时从环境变量读取
os.environ["WB_MAX_PAYLOAD_BYTES"] = "4096"
os.environ["WB_CLIENT_TIMEOUT"] = "2"

import wb_accounts
import wb_proxy as proxy
import wb_settings


class BodyLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proxy.log = lambda *a, **k: None
        proxy.POOL = wb_accounts.AccountPool(proxy.ACCOUNTS_DIR)
        proxy.POOL.load()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def exchange(self, raw, wait=5):
        """发原始请求，返回（状态行，正文 JSON 或 None）；对端不回则状态行为空。"""
        sock = socket.create_connection(("127.0.0.1", self.port))
        sock.settimeout(wait)
        sock.sendall(raw)
        data = b""
        length = 0
        try:
            while b"\r\n\r\n" not in data:
                piece = sock.recv(65536)
                if not piece:
                    break
                data += piece
            head, _, rest = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value)
            while len(rest) < length:
                piece = sock.recv(65536)
                if not piece:
                    break
                rest += piece
        except socket.timeout:
            pass
        finally:
            sock.close()
        status = data.split(b"\r\n", 1)[0].decode("latin-1") if data else ""
        body = json.loads(rest[:length]) if length else None
        return status, body

    def chunked(self, path, chunks, terminate=True):
        out = (b"POST " + path + b" HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n")
        for size_line, data in chunks:
            out += size_line + b"\r\n" + data + b"\r\n"
        if terminate:
            out += b"0\r\n\r\n"
        return out

    def test_a_negative_chunk_size_is_refused(self):
        # -1388 (= -5000) 把累计长度拉回上限以下，随后 5000 字节的合法 JSON 就会被接受
        body = json.dumps({"password": "x"}).encode().ljust(5000)
        status, _ = self.exchange(self.chunked(b"/panel/login", [
            (b"-1388", b""), (b"1388", body)]))
        self.assertIn(" 400 ", status)

    def test_an_oversized_chunk_answers_413_without_reading_the_data_as_sizes(self):
        # 正文全是十六进制字符：被当成长度行时会等一个巨大的块，直到读超时才回复
        started = time.time()
        status, _ = self.exchange(self.chunked(b"/panel/login", [(b"ffff", b"a" * 70000)],
                                               terminate=False))
        self.assertIn(" 413 ", status)
        self.assertLess(time.time() - started, 1.5)

    def test_a_body_that_never_arrives_does_not_hold_the_thread(self):
        status, _ = self.exchange(b"POST /settings/save HTTP/1.1\r\nHost: x\r\n"
                                  b"Content-Length: 100\r\n\r\n", wait=6)
        self.assertIn(" 401 ", status)

    def test_a_normal_chunked_body_still_works(self):
        body = json.dumps({"password": "wrong"}).encode()
        status, _ = self.exchange(self.chunked(b"/panel/login",
                                               [(b"%x" % len(body), body)]))
        self.assertIn(" 401 ", status)


class PanelInputTests(unittest.TestCase):
    """直接调用路由函数，模拟已登录面板的请求。"""

    class Request(object):
        _handle_settings_save = proxy.Handler._handle_settings_save
        _route_accounts_import_desktop = proxy.Handler._route_accounts_import_desktop

        def __init__(self, payload):
            self.payload = payload

        def _read_payload(self, *args, **kwargs):
            return self.payload

        def _payload_or_error(self, *args, **kwargs):
            return self.payload

        def _json(self, status, payload):
            return status, payload

        def _error(self, status, message, err_type="server_error"):
            return status, {"error": {"message": message}}

    def test_auth_disabled_only_takes_a_real_boolean(self):
        status, body = proxy.Handler._handle_settings_save(self.Request({"auth_disabled": "false"}))
        self.assertEqual(status, 400, body)
        self.assertFalse(wb_settings.auth_disabled(proxy.ACCOUNTS_DIR))

    def test_desktop_import_refuses_a_path_the_scan_did_not_list(self):
        outside = os.path.join(_TMP, "not-a-credential.json")
        with open(outside, "w", encoding="utf-8") as fh:
            json.dump({"auth": {"accessToken": "a.b.c"}, "account": {"uid": "u-outside"}}, fh)
        status, body = proxy.Handler._route_accounts_import_desktop(
            self.Request({"path": outside}), {"path": outside})
        self.assertEqual(status, 400, body)
        self.assertNotIn("not-a-credential", json.dumps(body))
        self.assertIsNone(proxy.POOL.get("u-outside"))


if __name__ == "__main__":
    unittest.main()
