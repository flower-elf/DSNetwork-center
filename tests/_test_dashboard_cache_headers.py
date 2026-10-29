"""面板首页的条件请求：缓存 + 每次回源校验，不重下整页。

GET / 是服务端逐字节原样发出的纯静态资源、不含机密，可以让浏览器存一份、每次使用前
回源校验：ETag + `no-cache`，客户端副本最新就回 304（无 body）。无校验符的
`no-store` 会让每次打开面板都重下整页。

套件在空闲端口上起一个真实网关，跑在源码树的临时副本里：条件请求的逻辑长在 HTTP
处理器里，只有真发请求才看得见；校验 mtime 变化、文件缺失这些分支又要改写被服务的
页面，副本让这些操作不碰检出目录。

用 python tests/_test_dashboard_cache_headers.py 运行。
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, "python", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
sys.path.insert(0, HERE)

import _lifecycle as life            # noqa: E402  空闲端口与受管进程

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


port = life.free_port()
work = tempfile.mkdtemp(prefix="dashcache_")
tree = os.path.join(work, "gateway")
# 整棵源码树复制到临时目录（跳过测试和缓存）：网关从 __file__ 旁边读
# app/web/index.html，只有副本才能随便改写、甚至删掉，去验 404 分支。
shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(
    "__pycache__", ".git", ".github", "tests", ".test-artifacts"))
store = os.path.join(work, "accounts")
os.makedirs(store)
settings_path = os.path.join(store, "settings.json")
with open(settings_path, "w", encoding="utf-8") as fh:
    json.dump({"ui_language": "zh"}, fh)


def set_language(value):
    """改实例界面语言（读改写，保留服务器自己写进去的键）。"""
    try:
        with open(settings_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    data["ui_language"] = value
    with open(settings_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


# 受管启动：独立的进程组或会话，finally 里的 stop_managed() 因此能收掉整棵
# 进程树；漏掉的辅助进程还会占着端口。
proc = life.spawn_managed(
    [PY, os.path.join(tree, "app", "wb_proxy.py"), "--port", str(port),
     "--host", "127.0.0.1", "--accounts-dir", store],
    cwd=tree, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    env=dict(os.environ, WB_PROXY_USAGE_DIR=os.path.join(work, "usage")))


def get(path="/", headers=None):
    """一次独立连接上的 GET；返回 (status, headers, body)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.headers, resp.read()
    finally:
        conn.close()


def wait_ready():
    """等 /health 作答；起不来的网关会自行退出。"""
    for _ in range(40):
        time.sleep(0.5)
        try:
            status, _, _ = get("/health")
            if status == 200:
                return True
        except Exception:
            if proc.poll() is not None:
                return False
    return False


try:
    if not wait_ready():
        print("  [FAIL] server did not start")
        sys.exit(1)

    dash_path = os.path.join(tree, "app", "web", "index.html")
    with open(dash_path, "rb") as fh:
        disk = fh.read()

    print("[1] 普通 GET：200，逐字节发出页面，并带上可校验的缓存头")
    status, headers, body = get("/")
    check("200", status == 200, status)
    check("body = 文件本身，逐字节一致", body == disk,
          "%d vs %d bytes" % (len(body), len(disk)))
    check("Content-Type 保持原样",
          headers.get("Content-Type") == "text/html; charset=utf-8",
          headers.get("Content-Type"))
    check("Content-Length 与 body 长度一致",
          headers.get("Content-Length") == str(len(body)),
          headers.get("Content-Length"))
    etag = headers.get("ETag") or ""
    check("Cache-Control 允许存储但要求回源校验（no-cache）",
          (headers.get("Cache-Control") or "").strip() == "no-cache",
          headers.get("Cache-Control"))
    check("Cache-Control 不再是 no-store",
          "no-store" not in (headers.get("Cache-Control") or ""))
    check("ETag 是带引号的强校验符",
          etag.startswith('"') and etag.endswith('"') and not etag.startswith("W/"),
          etag)
    check("不发布 Last-Modified",
          headers.get("Last-Modified") is None, headers.get("Last-Modified"))

    print("[2] If-None-Match 命中当前 ETag：304，无 body，校验头原样重发")
    status, headers, body = get("/", {"If-None-Match": etag})
    check("304", status == 304, status)
    check("body 为空", body == b"", "%d bytes" % len(body))
    check("304 不带 Content-Length", headers.get("Content-Length") is None,
          headers.get("Content-Length"))
    check("304 不带 Last-Modified", headers.get("Last-Modified") is None,
          headers.get("Last-Modified"))
    check("ETag 在 304 上重发", headers.get("ETag") == etag,
          headers.get("ETag"))
    check("Cache-Control 在 304 上重发",
          (headers.get("Cache-Control") or "").strip() == "no-cache",
          headers.get("Cache-Control"))

    print("[3] 弱比较：W/<etag> 同样命中（RFC 7232 §3.2）")
    status, _, _ = get("/", {"If-None-Match": "W/" + etag})
    check("304 for W/<etag>", status == 304, status)

    print("[4] 通配符与标签列表：* 和列表里的当前标签都命中")
    status, _, _ = get("/", {"If-None-Match": "*"})
    check("304 for *", status == 304, status)
    status, _, _ = get("/", {"If-None-Match": '"deadbeef", ' + etag})
    check("304 for a list carrying the current tag", status == 304, status)
    status, _, _ = get("/", {"If-None-Match": 'W/"deadbeef", W/' + etag})
    check("304 for a weak list carrying the current tag", status == 304, status)
    status, _, _ = get("/", {"If-None-Match": '"deadbeef", "cafebabe"'})
    check("200 for a list without the current tag", status == 200, status)

    print("[5] 过期 / 不匹配的 ETag：200 整页")
    status, _, body = get("/", {"If-None-Match": '"deadbeef"'})
    check("200 且 body 完整", status == 200 and body == disk, status)

    print("[6] 畸形的 If-None-Match 不崩，按不匹配走 200")
    for junk in ("not-a-tag", "", "W/", "*x", '",",'):
        status, _, body = get("/", {"If-None-Match": junk})
        check("200 for %r" % junk, status == 200 and body == disk, status)

    print("[7] 改界面语言：静态页面不变，ETag 继续有效")
    set_language("en")
    status, headers, body = get("/", {"If-None-Match": etag})
    check("旧 ETag -> 304（页面与语言无关）", status == 304, status)
    check("body 为空", body == b"", "%d bytes" % len(body))
    check("ETag 不变", (headers.get("ETag") or "") == etag, headers.get("ETag"))

    print("[8] 文件被整体替换：新 ETag，旧校验符不再 304")
    st_before = os.stat(dash_path)
    with open(dash_path, "ab") as fh:
        fh.write(b"\n<!-- cache test -->\n")
    # 把 mtime 明确拨到原值 +30s：复制和追加可能落在同一秒，靠拨表让
    # 变化确定地可区分。
    os.utime(dash_path, ns=(st_before.st_atime_ns,
                            st_before.st_mtime_ns + 30_000_000_000))
    with open(dash_path, "rb") as fh:
        disk2 = fh.read()
    status, headers, body = get("/")
    etag2 = headers.get("ETag") or ""
    check("200 且发出新字节", status == 200 and body == disk2, status)
    check("ETag 变了", etag2 and etag2 != etag, "%s -> %s" % (etag, etag2))
    status, _, _ = get("/", {"If-None-Match": etag})
    check("旧 ETag -> 200 整页", status == 200, status)
    status, _, _ = get("/", {"If-None-Match": etag2})
    check("新 ETag -> 304", status == 304, status)

    print("[9] 只拨 mtime、size 不变：ETag 仍然变化")
    st_now = os.stat(dash_path)
    os.utime(dash_path, ns=(st_now.st_atime_ns, st_now.st_mtime_ns + 5_000_000_000))
    status, headers, _ = get("/")
    etag3 = headers.get("ETag") or ""
    check("200", status == 200, status)
    check("ETag 随 mtime 变化", etag3 and etag3 != etag2,
          "%s -> %s" % (etag2, etag3))
    status, _, _ = get("/", {"If-None-Match": etag2})
    check("上一代 ETag -> 200", status == 200, status)

    print("[10] If-Modified-Since 不被支持：单独发它永远不产生 304")
    status, headers, body = get("/", {"If-Modified-Since": "Thu, 08 Oct 2026 16:42:23 GMT"})
    check("200 整页（IMS 被忽略）", status == 200 and body == disk2, status)
    status, _, _ = get("/", {"If-Modified-Since": "not a date at all"})
    check("畸形 IMS 也 200 且不崩", status == 200, status)

    print("[10] index.html 缺失：回 404 JSON")
    os.remove(dash_path)
    status, headers, body = get("/")
    check("404", status == 404, status)
    check("错误体是 JSON 且说明原因",
          b'"error"' in body and b"not found" in body, body[:120])
finally:
    life.stop_managed(proc)
    shutil.rmtree(work, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
