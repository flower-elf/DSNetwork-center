"""验证按块缓冲读取 SSE 与逐行迭代逐字节一致，且每行一次系统调用降为每块一次。

对 http.client.HTTPResponse 直接 for line 迭代时，每个 SSE 事件一次 read() 系统调用，
每次都要释放再获取 GIL；一次流式回复有上千个事件，时间大多耗在 handler 线程间的 GIL 交接上。
v1.7.2 在 4 vCPU 机器、15-20 req/s 下的实测（strace -c -f -p <pid>，10 秒）：

    wb-proxy 的 10 秒系统调用统计
      71.44%   59.43s    559us/call   106214  futex     <- GIL 交接
      20.31%   16.89s  42659us/call      396  poll
       1.80%    1.50s     41us/call    36366  read      <- 每个 SSE 行一次
       0.54%    0.45s     88us/call     5043  sendto

`iter_sse_lines()` 用 read1() 读取（每次至多一次底层系统调用）并在内存中切分，所以这里
验证的等价性是关键：只改变系统调用次数，调用方看到的字节一个都不能变。参照实现用
io.BytesIO 迭代，与被替换的旧循环同为 readline() 语义：行保留终止符，末尾没有终止符的
最后一行照常输出，只按 "\\n" 切分。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402（独立数据根目录）
# 独立数据目录，进程退出时删除。
_TMP = isolated_data_dirs("wb-sse-read-batching-")

import wb_translate as T  # noqa: E402

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


class FakeStream:
    """把网络数据模拟成分块列表，经 read1() 按序吐出。

    read1() 从当前块头部至多返回 n 个字节，脚本耗尽后返回 b""。块切成不规则大小，
    让行跨越块边界，这正是读取器必须处理正确的情况。
    """

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.pending = b""
        self.read1_calls = 0

    def read1(self, n=-1):
        self.read1_calls += 1
        if not self.pending:
            if not self.chunks:
                return b""
            self.pending = self.chunks.pop(0)
        if n is None or n < 0:
            out, self.pending = self.pending, b""
            return out
        out, self.pending = self.pending[:n], self.pending[n:]
        return out


def same_as_plain_iteration(data, chunk_size):
    """按 chunk_size 分块读取 data，返回输出行、read1 调用次数和块数。"""
    wire = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)] or [b""]
    src = FakeStream(wire)
    got = list(T.iter_sse_lines(src))
    return got, src.read1_calls, len(wire)


print("-- equivalence with io.BytesIO iteration --")

CASES = [
    ("single event", b"data: {\"a\":1}\n\n"),
    ("two events, one block", b"data: {\"a\":1}\n\ndata: {\"b\":2}\n\n"),
    ("no trailing newline", b"data: {\"a\":1}\n\ndata: {\"b\":2}"),
    ("CRLF terminators", b"data: {\"a\":1}\r\n\r\ndata: {\"b\":2}\r\n\r\n"),
    ("blank lines and comments", b"\n: keepalive\n\ndata: x\n\n"),
    ("empty stream", b""),
    ("only a newline", b"\n"),
    ("no newline at all", b"data: {"),
    ("long single event", b"data: " + b"x" * 5000 + b"\n\n"),
    ("many events", b"".join(b"data: {\"i\":%d}\n\n" % i for i in range(300))),
]

for label, data in CASES:
    for chunk_size in (1, 7, 64, 4096):
        got, calls, wire = same_as_plain_iteration(data, chunk_size)
        ref = list(io.BytesIO(data))
        check("%s (chunk=%d) byte-identical" % (label, chunk_size),
              got == ref,
              "got=%r ref=%r" % (got[:4], ref[:4]))

print("-- the same bytes through the real io.BytesIO read1() --")
for label, data in CASES:
    got = list(T.iter_sse_lines(io.BytesIO(data)))
    check("%s via BytesIO" % label, got == list(io.BytesIO(data)),
          "got=%r" % (got[:4],))

print("-- a line is emitted as soon as it is complete, not when a block fills --")
src = FakeStream([b"data: one\n"])
gen = T.iter_sse_lines(src)
check("first complete line arrives on the first read1",
      next(gen) == b"data: one\n")
check("no second read1 was needed to emit it", src.read1_calls == 1,
      "%d calls" % src.read1_calls)

src = FakeStream([b"data: par", b"tial\n"])
got = list(T.iter_sse_lines(src))
check("a line straddling two chunks is reassembled", got == [b"data: partial\n"],
      repr(got))

print("-- syscall count: chunks, not lines --")

lines = b"".join(b"data: {\"i\":%d}\n\n" % i for i in range(2000))
ref = list(io.BytesIO(lines))
for chunk_size in (8192, 65536):
    got, calls, wire = same_as_plain_iteration(lines, chunk_size)
    check("%d-byte wire chunks stay byte-identical" % chunk_size, got == ref)
    check("%d-byte wire chunks: read1 calls bounded by wire chunks" % chunk_size,
          calls <= wire + 2,
          "%d calls for %d lines over %d chunks" % (calls, len(ref), wire))

# 汇总数字：旧循环按行计一次调用，现在按块计。
_, calls, wire = same_as_plain_iteration(lines, 65536)
print("    2000 events / 64 KiB blocks: %d read1 calls vs %d lines (%.0fx fewer)"
      % (calls, len(ref), float(len(ref)) / max(1, calls)))
check("at least an order of magnitude fewer syscalls",
      len(ref) >= 10 * calls, "%d lines, %d calls" % (len(ref), calls))

print("-- objects without read1 fall back to plain iteration --")
check("an iterator iterates unchanged",
      list(T.iter_sse_lines(iter([b"a\n", b"b"]))) == [b"a\n", b"b"])

src = FakeStream([b"y" * 100])
list(T.iter_sse_lines(src, block_size=16))
check("a custom block_size is passed to read1",
      src.read1_calls >= 7, "%d calls for 100 bytes at 16" % src.read1_calls)

print("-- before_read: the hook that lets a caller batch its writes --")

calls = [0]
src = FakeStream([b"a\n", b"b\n", b"c\n"])
got = list(T.iter_sse_lines(src, before_read=lambda: calls.__setitem__(0, calls[0] + 1)))
check("lines still come out intact with a hook attached",
      got == [b"a\n", b"b\n", b"c\n"], repr(got))
check("the hook fires exactly once per read1",
      calls[0] == src.read1_calls,
      "%d hook calls vs %d read1 calls" % (calls[0], src.read1_calls))

# 批处理零延迟的不变量：生成器即将阻塞在 socket 读取时，调用方缓冲区已经清空，
# 内存中不会滞留等待未到数据的内容。
drained = []
written = []
src = FakeStream([b"data: a\n", b"data: b\n", b"data: c\n"])
buf = []


def flush_out():
    if buf:
        written.append(b"".join(buf))
        del buf[:]


def before_read():
    # 钩子在 read1() 之前执行；在这里清空缓冲正是批处理零延迟的原因，所以记录
    # 清空后的大小。
    flush_out()
    drained.append(len(buf))


for line in T.iter_sse_lines(src, before_read=before_read):
    buf.append(line)
flush_out()
check("the write buffer is empty whenever a blocking read begins",
      drained and all(n == 0 for n in drained), drained)
check("every event still reached the wire",
      b"".join(written) == b"data: a\ndata: b\ndata: c\n", written)


def batched(data, chunk_size):
    """模拟 _chat_stream_response：缓冲事件，在 before_read 时刷出。"""
    wire = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)] or [b""]
    src = FakeStream(wire)
    writes = []
    pending = []

    def flush():
        if pending:
            writes.append(b"".join(pending))
            del pending[:]

    for line in T.iter_sse_lines(src, before_read=flush):
        pending.append(b"data: " + line)
    flush()
    return writes, len(wire)


print("-- batching: one write per read block, not one per event --")

events = b"".join(b"data: {\"i\":%d}\n\n" % i for i in range(2000))
expected = b"".join(b"data: " + l for l in io.BytesIO(events))
for chunk_size in (8192, 65536):
    writes, wire = batched(events, chunk_size)
    check("%d-byte blocks: batched output is byte-identical" % chunk_size,
          b"".join(writes) == expected)
    check("%d-byte blocks: at most one write per block" % chunk_size,
          len(writes) <= wire + 1,
          "%d writes over %d blocks" % (len(writes), wire))

writes, wire = batched(events, 65536)
n_events = len(list(io.BytesIO(events)))
print("    4000 SSE lines / 64 KiB blocks: %d writes vs %d unbuffered writes (%.0fx fewer)"
      % (len(writes), n_events, float(n_events) / max(1, len(writes))))
check("batching removes at least an order of magnitude of writes",
      n_events >= 10 * len(writes),
      "%d lines, %d writes" % (n_events, len(writes)))

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
