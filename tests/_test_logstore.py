"""网关日志 SQLite 后端的写入、筛选、清空、重开与三档保留上限。

每个用例用独立的临时目录并在结束时删除，不开网络。保留上限在 close() 之后用
独立连接调用裁剪方法：工作线程可能仍在写同一份库，且它先答复查询再提交事务，
所以“查询成功”不等于“已提交”。max_id() 会另开连接，同样只在关闭后断言。
"""

import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_logstore as LS

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def entry(rid, level, tag, msg, ts="2026-10-28 10:00:00"):
    return {"id": rid, "ts": ts, "time": "10:00:00",
            "level": level, "tag": tag, "msg": msg}


def rows_of(result):
    return [row["msg"] for row in result["logs"]]


def trim(store, cap=None):
    """关闭后用一个独立连接执行裁剪，避开工作线程的事务。"""
    store.close()
    conn = sqlite3.connect(store.path)
    try:
        if cap is None:
            store._trim(conn)
        else:
            store._trim_bytes(conn, cap)
        conn.commit()
    finally:
        conn.close()
    store.configure(True)


work = tempfile.TemporaryDirectory(prefix="wb-logstore-")
tmp = work.name
path = os.path.join(tmp, "logs.sqlite3")

store = LS.LogStore(path)
check("默认关闭", store.enabled is False)
check("关闭时不建文件", not os.path.exists(path))
check("关闭时查询返回 None", store.query() is None)
check("关闭时统计返回 None", store.stats() is None)
check("关闭时清空返回 False", store.clear() is False)
store.append(entry(1, "INFO", "system", "不该落库"))
check("关闭时追加无效", store.query() is None)
check("没有文件时最大 id 为 0", store.max_id() == 0)

check("开启返回 True", store.configure(True) is True)
check("开启后状态为真", store.enabled is True)
check("重复开启保持幂等", store.configure(True) is True)

store.append(entry(1, "INFO", "system", "启动完成"))
store.append(entry(2, "WARN", "chat", "上游超时"))
store.append(entry(3, "ERROR", "pricing", "价格文件缺失"))

result = store.query()
check("三行按 id 升序返回", rows_of(result) == ["启动完成", "上游超时", "价格文件缺失"], result)
check("总数是三", result["total"] == 3, result)
check("最大 id 跟上最后一行", result["max_id"] == 3, result)
check("行字段齐全", set(result["logs"][0]) == {"id", "ts", "time", "level", "tag", "msg"},
      result["logs"][0])

check("按等级筛选", rows_of(store.query(level="warn")) == ["上游超时"])
check("按标签筛选且忽略大小写",
      rows_of(store.query(tag="PRICING")) == ["价格文件缺失"])
check("搜索命中正文", rows_of(store.query(search="超时")) == ["上游超时"])
check("搜索命中标签", rows_of(store.query(search="pric")) == ["价格文件缺失"])
check("since_id 只返回更新的行", rows_of(store.query(since_id=2)) == ["价格文件缺失"])
check("limit 取最新的若干行", rows_of(store.query(limit=2)) == ["上游超时", "价格文件缺失"])
check("限制里没有匹配行时列表为空", rows_of(store.query(level="WARN", search="价格")) == [])

stats = store.stats()
check("统计行数", stats["rows"] == 3, stats)
check("统计保留路径", stats["path"] == path, stats)
check("统计起止时间", (stats["oldest"], stats["newest"]) ==
      ("2026-10-28 10:00:00", "2026-10-28 10:00:00"), stats)
check("统计包含字节数", stats["bytes"] > 0, stats)

store.append({"id": 4, "ts": "2026-10-28 10:00:01", "time": "10:00:01",
              "level": "INFO", "tag": "system", "msg": "长" * (70 * 1024)})
long_row = store.query(since_id=3)["logs"][0]
check("超长消息截断到 64 KiB", len(long_row["msg"]) == 64 * 1024, len(long_row["msg"]))

check("清空返回 True", store.clear() is True)
check("清空后没有行", store.query()["total"] == 0)

store.append(entry(5, "INFO", "system", "重开之后仍在"))
store.close()
check("关闭后状态为假", store.enabled is False)
check("关闭后文件保留", os.path.exists(path))
check("关闭后最大 id 已提交", store.max_id() == 5)

reopened = LS.LogStore(path)
check("重开后默认仍是关闭", reopened.enabled is False)
reopened.configure(True)
check("重开后读到旧行", rows_of(reopened.query()) == ["重开之后仍在"])
reopened.close()

# --- 保留上限 ---------------------------------------------------------
store = LS.LogStore(os.path.join(tmp, "caps.sqlite3"))
store.configure(True, max_rows=2)
for rid in range(10, 15):
    store.append(entry(rid, "INFO", "system", "row-%d" % rid))
trim(store)
check("行数上限只留最新的两行", rows_of(store.query()) == ["row-13", "row-14"],
      rows_of(store.query()))

store.clear()
store.configure(True, max_days=1)
store.append(entry(20, "INFO", "system", "很久以前", ts="2000-01-01 00:00:00"))
store.append(entry(21, "INFO", "system", "今天", ts=time.strftime("%Y-%m-%d %H:%M:%S")))
trim(store)
check("天数上限删掉过期的行", rows_of(store.query()) == ["今天"], rows_of(store.query()))

store.configure(True, max_bytes=60 * 1024)
for rid in range(30, 230):
    store.append(entry(rid, "INFO", "system", "x" * 1024))
before = store.query()["total"]
trim(store, cap=60 * 1024)
after = store.query()["total"]
check("字节上限删除旧行直到装下", after < before and store._size() <= 60 * 1024,
      (before, after, store._size()))
check("字节上限至少保留一行", after >= 1, after)
store.close()

work.cleanup()
check("临时目录已清理", not os.path.exists(tmp))

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
