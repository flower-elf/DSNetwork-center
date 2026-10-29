# 网关日志环的可选 SQLite 持久化。开关默认关闭；开启后每条日志同时写入
# usage/logs.sqlite3，并受行数、天数与字节数三重上限约束。
import os
import queue
import sqlite3
import threading
import time

# 每条事务批量提交，突发请求日志只付一次 fsync。排空循环也处理已排队的读与
# 清理，查询不会被写入饿死。
_BATCH_MAX = 256
# 行数与天数上限的检查间隔，单位秒。裁剪只有几条 DELETE，不必每批都跑。
_TRIM_INTERVAL = 5.0
# 空闲时也按此周期唤醒，让没有新日志的网关仍会清理过期行。
_IDLE_TICK = 5.0
# 大小上限最贵，可能分批删除并执行 VACUUM，因此使用更慢的周期。
_TRIM_BYTES_INTERVAL = 30.0
# 单条消息原样存储；该上限防止异常载荷把文件一次撑到上限。64 KiB 已远超真实
# 日志行。
_MSG_MAX = 64 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id    INTEGER PRIMARY KEY,
    ts    TEXT NOT NULL,
    time  TEXT NOT NULL,
    level TEXT NOT NULL,
    tag   TEXT NOT NULL,
    msg   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_tag   ON logs(tag);
"""

_COLUMNS = ("id", "ts", "time", "level", "tag", "msg")


class LogStore:
    """网关日志环的持久化后端。

    生命周期：用数据库路径构造，再以面板设置调用 configure。开关切换时启动
    或停止工作线程；关闭时保留文件与行，重新打开后接着写。
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._thread = None
        self._enabled = False
        self._caps = {"rows": None, "days": None, "bytes": None}
        self._last_trim = 0.0
        self._last_trim_bytes = 0.0

    # -- 生命周期 ---------------------------------------------------------
    @property
    def enabled(self):
        return self._enabled

    def configure(self, enabled, max_rows=None, max_days=None, max_bytes=None):
        """应用面板设置，并按开关启动或停止工作线程。

        启动时与每次保存设置都会调用，要求幂等且廉价。
        """
        with self._lock:
            self._caps = {
                "rows": max_rows if max_rows and max_rows > 0 else None,
                "days": max_days if max_days and max_days > 0 else None,
                "bytes": max_bytes if max_bytes and max_bytes > 0 else None,
            }
            if enabled and not self._enabled:
                self._enabled = True
                self._thread = threading.Thread(
                    target=self._run, name="logstore", daemon=True)
                self._thread.start()
            elif not enabled and self._enabled:
                self._enabled = False
                self._queue.put(None)
                thread, self._thread = self._thread, None
                if thread is not None:
                    thread.join(timeout=5.0)
        return self._enabled

    def close(self):
        """停止工作线程并冲刷；可重复调用。"""
        with self._lock:
            if not self._enabled and self._thread is None:
                return
            self._enabled = False
            self._queue.put(None)
            thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    # -- 写入 --------------------------------------------------------------
    def append(self, entry):
        """排队一条记录，不阻塞请求路径。"""
        if not self._enabled:
            return
        try:
            row = (
                int(entry.get("id") or 0),
                str(entry.get("ts") or ""),
                str(entry.get("time") or ""),
                str(entry.get("level") or "INFO"),
                str(entry.get("tag") or "system"),
                str(entry.get("msg") or "")[:_MSG_MAX],
            )
        except Exception:
            return
        self._queue.put(("append", row, None))

    def clear(self):
        """清空全部日志行，提交被接受时返回 True。"""
        if not self._enabled:
            return False
        self._submit("clear", wait=True)
        return True

    # -- 读取 --------------------------------------------------------------
    def query(self, limit=200, level="", tag="", search="", since_id=0):
        """按看板筛选条件返回日志行，结构与 wb_proxy.get_logs 一致。"""
        if not self._enabled:
            return None
        payload = {"limit": limit, "level": level, "tag": tag,
                   "search": search, "since_id": since_id}
        return self._submit("query", payload, wait=True)

    def stats(self):
        """返回行数、文件大小与已存时间跨度；存储关闭时为 None。"""
        if not self._enabled:
            return None
        return self._submit("stats", wait=True)

    def max_id(self):
        """返回已存的最大 id，没有时为 0。

        启动时调用一次以续接编号。此处另开连接读取：工作线程可能尚未启动，
        而它供给的计数器必须在记录第一行之前就正确。
        """
        try:
            conn = sqlite3.connect(self.path, timeout=5)
        except Exception:
            return 0
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT MAX(id) FROM logs").fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception:
            return 0
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # -- 工作线程 ----------------------------------------------------------
    def _submit(self, kind, payload=None, wait=False, timeout=8.0):
        fut = {"event": threading.Event(), "result": None, "error": None}
        self._queue.put((kind, payload, fut))
        if not wait:
            return True
        if not fut["event"].wait(timeout):
            return None
        if fut["error"] is not None:
            raise fut["error"]
        return fut["result"]

    def _connect(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        # auto_vacuum 必须在第一张表建好之前设置；已有表的库上这条是空操作，
        # 大小上限就退回整库 VACUUM，同样能把文件压回上限以内。
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        # 所有语句都跑在这一个线程上，写入、读取、裁剪经队列串行，没有读写
        # 竞态要解决；回滚日志让「文件大小」始终等于运维设定上限的那一个
        # 文件（WAL 会把最新行放进旁文件，上限永远缩不到）。
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    def _run(self):
        try:
            conn = self._connect()
        except Exception as exc:
            self._fail(exc)
            return
        try:
            self._loop(conn)
        except Exception as exc:
            self._fail(exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _fail(self, exc):
        """停用日志库并唤醒所有等待中的查询：工作线程已不可用，不能静默挂住。"""
        with self._lock:
            self._enabled = False
            self._thread = None
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            _kind, _payload, fut = item
            if fut is not None:
                self._finish(fut, None, exc)

    def _loop(self, conn):
        stop = False
        while not stop:
            try:
                first = self._queue.get(timeout=_IDLE_TICK)
            except queue.Empty:
                # 空闲也要按节奏裁剪：没有新日志不代表旧日志可以一直留着。
                self._maybe_trim(conn)
                continue
            if first is None:
                break
            batch = [first]
            while len(batch) < _BATCH_MAX:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    stop = True
                    break
                batch.append(nxt)
            dirty = False
            for kind, payload, fut in batch:
                try:
                    if kind == "append":
                        conn.execute(
                            "INSERT OR REPLACE INTO logs"
                            " (id, ts, time, level, tag, msg)"
                            " VALUES (?, ?, ?, ?, ?, ?)", payload)
                        dirty = True
                    elif kind == "query":
                        self._finish(fut, self._query(conn, **payload))
                    elif kind == "stats":
                        self._finish(fut, self._stats(conn))
                    elif kind == "clear":
                        conn.execute("DELETE FROM logs")
                        dirty = True
                        self._finish(fut, True)
                except Exception as exc:
                    if fut is not None:
                        self._finish(fut, None, exc)
            if dirty:
                conn.commit()
                self._maybe_trim(conn)
            elif any(fut is not None for _k, _p, fut in batch):
                # 只读批次也可能已经开启隐式事务；不提交会一直持着读锁。
                conn.commit()
        try:
            conn.commit()
        except Exception:
            pass

    @staticmethod
    def _finish(fut, result, error=None):
        if fut is None:
            return
        fut["result"] = result
        fut["error"] = error
        fut["event"].set()

    # -- 查询 --------------------------------------------------------------
    def _query(self, conn, limit=200, level="", tag="", search="", since_id=0):
        where, args = [], []
        if since_id and since_id > 0:
            where.append("id > ?")
            args.append(since_id)
        if level:
            where.append("level = ?")
            args.append(level.upper())
        if tag:
            where.append("LOWER(tag) = ?")
            args.append(tag.lower())
        if search:
            where.append("(instr(LOWER(msg), ?) > 0 OR instr(LOWER(tag), ?) > 0)")
            needle = search.lower()
            args.extend([needle, needle])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute("SELECT COUNT(*) FROM logs" + clause, args).fetchone()[0]
        columns = ", ".join(_COLUMNS)
        if limit and limit > 0 and not since_id:
            rows = conn.execute(
                "SELECT %s FROM logs%s ORDER BY id DESC LIMIT ?" % (columns, clause),
                args + [int(limit)]).fetchall()
            rows.reverse()
        else:
            rows = conn.execute(
                "SELECT %s FROM logs%s ORDER BY id" % (columns, clause), args).fetchall()
        items = [dict(zip(_COLUMNS, row)) for row in rows]
        max_id = items[-1]["id"] if items else (since_id or 0)
        return {"total": total, "logs": items, "max_id": max_id}

    def _stats(self, conn):
        row = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM logs").fetchone()
        return {
            "rows": int(row[0] or 0),
            "oldest": row[1] or "",
            "newest": row[2] or "",
            "bytes": self._size(),
            "path": self.path,
        }

    # -- 保留清理 ----------------------------------------------------------
    def _maybe_trim(self, conn):
        now = time.time()
        if now - self._last_trim >= _TRIM_INTERVAL:
            self._last_trim = now
            try:
                self._trim(conn)
            except Exception:
                pass
        if now - self._last_trim_bytes >= _TRIM_BYTES_INTERVAL:
            self._last_trim_bytes = now
            cap = self._caps.get("bytes")
            if cap:
                try:
                    self._trim_bytes(conn, cap)
                except Exception:
                    pass

    def _trim(self, conn):
        """执行行数与保留天数上限，两者都是简单的 DELETE。"""
        days = self._caps.get("days")
        if days:
            cutoff = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(time.time() - days * 86400))
            conn.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
        rows_cap = self._caps.get("rows")
        if rows_cap:
            total = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
            if total > rows_cap:
                conn.execute(
                    "DELETE FROM logs WHERE id NOT IN "
                    "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
                    (int(rows_cap),))
        conn.commit()

    def _trim_bytes(self, conn, cap):
        """按从旧到新的顺序删除并重建，直到文件小于 `cap`。

        按平均行长估算要删的行数，一次重建完成，不逐页收缩：incremental_vacuum
        每次只释放一页，这种体量的库要跑上千次。估算偏小时再补一轮，文件装下即停。
        """
        size = self._size()
        if size <= cap:
            return
        # 目标压到上限以下，随后的写入不会立刻又触发裁剪。
        target = max(1, int(cap * 0.85))
        for _ in range(4):
            total = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
            if total <= 0:
                break
            avg = max(1.0, size / float(total))
            keep = max(1, int(target / avg))
            drop = total - keep
            if drop <= 0:
                break
            conn.execute(
                "DELETE FROM logs WHERE id IN "
                "(SELECT id FROM logs ORDER BY id ASC LIMIT ?)", (int(drop),))
            conn.commit()
            try:
                conn.execute("VACUUM")
                conn.commit()
            except Exception:
                break
            new_size = self._size()
            if new_size >= size:
                break
            size = new_size
            if size <= cap:
                break

    def _size(self):
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total
