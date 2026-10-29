"""出站 TLS 必须复用同一个进程级 SSLContext。

http.client 在 context=None 时每个 HTTPSConnection 都新建默认上下文，每次都重新解析
系统 CA 列表。v1.7.2 容器实测：

    create_default_context() 平均 20.4 ms（CA 凭证 121 张）

OpenSSL 的客户端会话缓存挂在 SSLContext 上，每连接一个上下文也就放弃了 TLS 会话
复用，每次握手都是完整握手。45 个账号各走自己的代理就是 45 个池键，连接过期重建时
这笔开销再付一次。

这些用例固定两件事：上下文只建一次并共享，校验设置与 create_default_context() 出厂
一致；这里的任何优化都不得放松一项检查。
"""
import os
import ssl
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402  独立数据根目录
# 独立的数据目录，进程退出时删除。
_TMP = isolated_data_dirs("wb-shared-ssl-")

import wb_upstream_pool as U  # noqa: E402

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(detail) if detail else ""))


print("-- one context, built once --")

# 刻意惰性：从不发起出站请求的进程（测试套件、只跑面板）不该在 import 时付
# 20 ms 的 CA 解析。
check("importing the module did not build the context yet",
      U._SSL_CONTEXT is None, "already built: %r" % (U._SSL_CONTEXT,))

first = U.shared_ssl_context()
check("returns an SSLContext", isinstance(first, ssl.SSLContext), type(first))
check("second call returns the identical object",
      U.shared_ssl_context() is first)
check("a third call too", U.shared_ssl_context() is first)
check("the module-level slot is the same object", U._SSL_CONTEXT is first)

print("-- verification is not relaxed --")

check("check_hostname is on", first.check_hostname is True,
      "check_hostname=%r" % (first.check_hostname,))
check("verify_mode is CERT_REQUIRED",
      first.verify_mode == ssl.CERT_REQUIRED,
      "verify_mode=%r" % (first.verify_mode,))
check("the system CA bundle is loaded", len(first.get_ca_certs()) > 10,
      "%d certs" % len(first.get_ca_certs()))

# 设置必须与新建的默认上下文完全一致，以后“只是共享一个上下文”的改动不得
# 悄悄放宽接受范围。
fresh = ssl.create_default_context()
check("check_hostname matches a fresh default context",
      first.check_hostname == fresh.check_hostname)
check("verify_mode matches a fresh default context",
      first.verify_mode == fresh.verify_mode)
check("minimum_version matches a fresh default context",
      first.minimum_version == fresh.minimum_version)
check("the same CA set as a fresh default context",
      {c["subject"] for c in first.get_ca_certs()}
      == {c["subject"] for c in fresh.get_ca_certs()})

print("-- _new_connection hands the shared context to HTTPS --")

PROXY = {"host": "127.0.0.1", "port": "3128", "user": "", "password": ""}

conn = U._new_connection("https", "example.com", 443, None, 10)
check("direct https uses the shared context",
      conn._context is first, "context=%r" % (conn._context,))

proxied = U._new_connection("https", "example.com", 443, PROXY, 10)
check("https through a proxy uses the shared context",
      proxied._context is first, "context=%r" % (proxied._context,))
check("the CONNECT tunnel target is still set",
      getattr(proxied, "_tunnel_host", None) == "example.com",
      "tunnel_host=%r" % (getattr(proxied, "_tunnel_host", None),))
check("the proxy port is honoured for the tunnel",
      proxied.port == 3128, "port=%r" % (proxied.port,))

plain = U._new_connection("http", "example.com", 80, None, 10)
check("plain http gets no TLS context at all",
      not isinstance(plain, ssl.SSLContext)
      and getattr(plain, "_context", None) is None)

print("-- the pooled class is what the factory actually builds --")
import http.client  # noqa: E402

check("_PooledHTTPSConnection subclasses HTTPSConnection",
      issubclass(U._PooledHTTPSConnection, http.client.HTTPSConnection))
check("the object built for direct https is a pooled one",
      isinstance(conn, U._PooledHTTPSConnection))
check("the object built for the proxy path is a pooled one",
      isinstance(proxied, U._PooledHTTPSConnection))

print("-- the stdlib seam still works: a swapped factory invalidates the cache --")

before = U.shared_ssl_context()

custom = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
custom.check_hostname = False          # 必须先于 verify_mode 关闭：check_hostname
custom.verify_mode = ssl.CERT_NONE     # 开启时 CERT_NONE 会被拒绝

# 像池套件那样同时补丁两种拼写，跨版本都能用：3.13+ 走
# http.client._create_https_context，3.9-3.12 走 ssl._create_default_https_context。
saved = {}
for module, name in ((http.client, "_create_https_context"),
                     (ssl, "_create_default_https_context")):
    if hasattr(module, name):
        saved[(module, name)] = getattr(module, name)
        setattr(module, name, lambda *a, **k: custom)

try:
    swapped = U.shared_ssl_context()
    check("swapping the default factory invalidates the cache",
          swapped is custom, "got %r" % (swapped,))
    check("the new context is not the previously cached one",
          swapped is not before)
finally:
    for (module, name), fn in saved.items():
        setattr(module, name, fn)

# 恢复工厂函数会再次作废缓存，下次调用重建出新对象。重要的是它重新做校验并
# 再次进缓存。
restored = U.shared_ssl_context()
check("restoring the factory brings back a verifying context",
      restored is not custom and restored.check_hostname is True
      and restored.verify_mode == ssl.CERT_REQUIRED,
      "check_hostname=%r verify_mode=%r" % (restored.check_hostname,
                                            restored.verify_mode))
check("and that rebuilt context is cached again",
      U.shared_ssl_context() is restored)
check("the relaxed context is gone from the cache",
      U._SSL_CONTEXT is not custom)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
