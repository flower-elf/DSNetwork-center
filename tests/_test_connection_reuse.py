"""被拒绝的请求必须读完请求体，keep-alive 连接不能因残留负载失步。

早拒绝（401、429、404）的回复若不读请求体，负载会留在 socket 里，下一条请求把
残留 JSON 当请求行解析，日志出现空请求行与假的“414 Request-URI Too Long”。连接池
客户端会把它当成随机失败。必须用裸 socket：HTTP 客户端库会换新连接掩盖问题。

每个响应读到自身边界，按服务器的回答同步。在备用端口启动真实网关与一次性存储，
不需要网络，也不动真实账号。用 python tests/_test_connection_reuse.py 运行。
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")
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


# 完整响应的等待上限，超过就判服务器故障。网关正常时毫秒级回复；这个上限让
# 卡住的服务器尽快失败，不必等运行器自己的超时。
RESPONSE_DEADLINE = 15.0


class IncompleteResponse(Exception):
    """对方在响应中途关闭连接。

    与超时分开：超时是回答来晚了，这个是永远不会来。读取是套件的同步点，
    截断的交换绝不能当成完整响应返回。
    """


def content_length(head):
    """响应头声明的响应体长度；未声明时返回 None。"""
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def read_response(sock, deadline=RESPONSE_DEADLINE):
    """从裸 socket 读一个完整 HTTP 响应，受 `deadline` 限制。

    按线上到达的字节原样返回，下方的断言因此保持在线格式。读到响应自身边界
    停止：头里声明 Content-Length 就读满该长度，否则读到连接关闭；连接因此能
    给下一个请求复用。响应永不完整时必须失败：期限到了对方仍开着就抛
    TimeoutError，对方提前关闭就抛 IncompleteResponse，调用方永远拿不到可能
    被当成完整响应的截断结果。

    刻意不实现 chunked 分帧。被测路由都带 Content-Length，没有结尾的响应体
    必须响亮失败，不能靠猜测。
    """
    end = time.monotonic() + deadline
    no_response = "no complete response within %.1fs" % deadline

    def recv(size):
        left = end - time.monotonic()
        if left <= 0:
            raise TimeoutError(no_response)
        sock.settimeout(left)
        try:
            return sock.recv(size)
        except socket.timeout:
            # socket.timeout 在 3.10 才成为 TimeoutError 的别名，CI 下限是 3.9，
            # 那里 recv() 抛独立的类型：统一转成 TimeoutError，一种异常表示到达上限。
            raise TimeoutError(no_response) from None

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = recv(4096)
        if not chunk:
            raise IncompleteResponse(
                "closed after %d bytes, before the header block ended" % len(buf))
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    length = content_length(head)
    if length is None:
        # 无长度声明：连接关闭结束响应体，414 路径就是这样回复的。
        while True:
            chunk = recv(4096)
            if not chunk:
                return head + b"\r\n\r\n" + body
            body += chunk
    while len(body) < length:
        chunk = recv(min(4096, length - len(body)))
        if not chunk:
            raise IncompleteResponse(
                "closed after %d of the %d declared body bytes" % (len(body), length))
        body += chunk
    return head + b"\r\n\r\n" + body


port = life.free_port()
work = tempfile.mkdtemp(prefix="connreuse_")
store = os.path.join(work, "accounts")
os.makedirs(store)
with open(os.path.join(store, "settings.json"), "w", encoding="utf-8") as fh:
    json.dump({"api_keys": [{"id": "k1", "name": "t", "key": "GOODKEY",
                             "enabled": True}]}, fh)

# 受管启动：独立的进程组或会话，finally 里的 stop_managed() 因此能收掉整棵
# 进程树；漏掉的辅助进程还会占着端口。
proc = life.spawn_managed(
    [PY, os.path.join(ROOT, "app", "wb_proxy.py"), "--port", str(port), "--host", "127.0.0.1",
     "--accounts-dir", store],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    env=dict(os.environ, WB_PROXY_USAGE_DIR=os.path.join(work, "usage")))


def wait_ready():
    """等待 /health 应答；绑定失败的网关会直接退出。"""
    for _ in range(40):
        time.sleep(0.5)
        try:
            c = socket.create_connection(("127.0.0.1", port), timeout=2)
            c.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = c.recv(200)
            c.close()
            if data.startswith(b"HTTP/1.1"):
                return True
        except Exception:
            if proc.poll() is not None:
                return False
    return False


def send_pair(bad_body):
    """同一 socket 上先用 `bad_body` 发一个坏 key 请求，再发一个好的小请求。"""
    sock = socket.create_connection(("127.0.0.1", port), timeout=20)
    try:
        sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
            b"Authorization: Bearer BADKEY\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(bad_body)).encode() + b"\r\n\r\n" + bad_body
        )
        first = read_response(sock)

        small = b'{"model":"x","messages":[{"role":"user","content":"hi"}]}'
        sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
            b"Authorization: Bearer GOODKEY\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(small)).encode() + b"\r\n\r\n" + small
        )
        try:
            second = read_response(sock)
        except Exception as exc:
            second = ("recv error: %s" % exc).encode()
    finally:
        sock.close()

    first_line = first.split(b"\r\n", 1)[0].decode("latin-1") if first else "<closed>"
    second_line = second.split(b"\r\n", 1)[0].decode("latin-1") if second else "<closed>"
    print("      first:  %s" % first_line)
    print("      second: %s" % second_line)
    return first, second


try:
    if not wait_ready():
        print("  [FAIL] server did not start")
        sys.exit(1)

    print("[1] large body rejected with 401, then a good request")
    big = json.dumps({"model": "x",
                      "messages": [{"role": "user", "content": "A" * 70000}]}).encode()
    first, second = send_pair(big)
    check("the bad request was rejected", b"401" in first)
    check("the connection stayed in sync (not 414)", b"414" not in second,
          second.split(b"\r\n", 1)[0].decode("latin-1", "replace"))
    check("the second request reached the application",
          second.startswith(b"HTTP/1.1") and b"414" not in second)

    print("[2] a good request alone still works")
    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    body = b'{"model":"x","messages":[{"role":"user","content":"hi"}]}'
    sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                 b"Authorization: Bearer GOODKEY\r\nContent-Type: application/json\r\n"
                 b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    try:
        resp = read_response(sock)
    except Exception:
        resp = b""
    sock.close()
    check("no accounts configured -> clean JSON 503, not a hang",
          resp.startswith(b"HTTP/1.1 503") and b"no usable account" in resp,
          resp.split(b"\r\n", 1)[0].decode("latin-1", "replace"))

    print("[3] an over-long request line answers JSON, not stdlib HTML")
    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    sock.sendall(b"GET /" + b"a" * (2 * 1024 * 1024) + b" HTTP/1.1\r\nHost: x\r\n\r\n")
    try:
        resp = read_response(sock)
    except Exception:
        resp = b""
    sock.close()
    check("over-long request line -> 414", b"414" in resp[:40],
          resp.split(b"\r\n", 1)[0].decode("latin-1", "replace"))
    check("the 414 body is our JSON error shape", b"application/json" in resp,
          resp[:120].decode("latin-1", "replace"))

    # 读取上限在这里验证：对方永不回答、读到一半停下，都必须按期限失败。没有
    # 这条检查，删掉期限后其余检查仍会通过，只要服务器正常回答。
    print("[4] the reader's deadline is a bound, not a wait")
    quiet, silent = socket.socketpair()
    try:
        for stall in ("never answers", "stops mid-body"):
            if stall == "stops mid-body":
                silent.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\nhalf")
            started = time.monotonic()
            try:
                read_response(quiet, deadline=0.5)
                raised = False
            except TimeoutError:
                raised = True
            waited = time.monotonic() - started
            check("a peer that %s fails the read" % stall, raised)
            # 刻意放宽：上限只需区分“放弃”与“卡死”，繁忙的运行器唤醒可能偏慢。
            check("and gives up on its deadline, not eventually",
                  0.5 <= waited < 8.0, "%.2fs" % waited)
    finally:
        quiet.close()
        silent.close()

    # “永不完整”的另一半：对方提前关闭。截断的交换必须让读取失败，不能返回
    # 短响应让调用方当成完整回答。
    print("[5] a truncated response fails instead of passing for complete")
    for truncation, payload in (
            ("headers", b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"),
            ("body", b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\nhalf")):
        quiet, peer = socket.socketpair()
        try:
            peer.sendall(payload)
            peer.close()              # EOF：响应的其余部分永远不会来
            try:
                partial = read_response(quiet, deadline=5.0)
                raised = None
            except Exception as exc:
                partial, raised = None, exc
            check("a response truncated in the %s fails the read" % truncation,
                  isinstance(raised, IncompleteResponse),
                  "%s: %r" % (type(raised).__name__ if raised else "returned", partial))
        finally:
            quiet.close()
            peer.close()
finally:
    life.stop_managed(proc)
    shutil.rmtree(work, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
