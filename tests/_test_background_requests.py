"""过滤器不能拒绝用户自己的压缩整理，靠 thread_source=memory_consolidation 区分；
thread_source=user 本身不算用户标记。不需要网络。
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-background-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP

import wb_proxy as proxy

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def nested(meta):
    """Codex 实际发送的形状：自己的键下放一个 JSON 字符串。"""
    return {"client_metadata": {"x-codex-turn-metadata": json.dumps(meta)}}


def flat(meta):
    return {"client_metadata": dict(meta)}


print("[1] the user's own compaction passes, in both metadata shapes")

check("nested compaction (request_kind + thread_source)",
      proxy.background_request_reason(
          nested({"request_kind": "compaction", "thread_source": "user"})) == "")
check("flat compaction (request_kind + thread_source)",
      proxy.background_request_reason(
          flat({"request_kind": "compaction", "thread_source": "user"})) == "")
check("compaction with no thread_source at all",
      proxy.background_request_reason(nested({"request_kind": "compaction"})) == "")
check("is_compaction_request agrees",
      proxy.is_compaction_request(nested({"request_kind": "compaction", "thread_source": "user"})))
check("a plain user turn is not a compaction",
      not proxy.is_compaction_request(nested({"request_kind": "chat", "thread_source": "user"})))

print()
print("[2] the background jobs the filter exists for are still refused")

background = [
    ("memory consolidation",
     {"request_kind": "memory", "thread_source": "memory_consolidation",
      "turn_trigger": "memory_consolidation"}),
    ("compaction started by the client",
     {"request_kind": "compaction", "thread_source": "memory_consolidation"}),
    ("title generation on the user's thread",
     {"request_kind": "title", "thread_source": "user"}),
    ("ambient suggestion",
     {"request_kind": "ambient_suggestion", "turn_trigger": "ambient_suggestions"}),
    ("auto review on the user's thread",
     {"request_kind": "auto_review", "thread_source": "user"}),
]
for label, meta in background:
    reason = proxy.background_request_reason(nested(meta))
    check("%s is refused" % label, bool(reason), (label, meta, reason))
    check("%s is not read as a user compaction" % label,
          not proxy.is_compaction_request(nested(meta)), (label, meta))

print()
print("[3] an unknown thread source is treated as the user's own request")

# 未见过的新来源按用户请求放行是刻意的：误拒会砍掉用户可见的按钮，误放行只损失过滤省下的积分。
check("compaction with an unrecognised source",
      proxy.background_request_reason(
          nested({"request_kind": "compaction", "thread_source": "compaction"})) == "")
check("compaction with an unrecognised turn_trigger",
      proxy.background_request_reason(
          nested({"request_kind": "compaction", "turn_trigger": "user_initiated"})) == "")

print()
print("[4] requests without metadata are left alone")

check("no client_metadata", proxy.background_request_reason({"model": "m"}) == "")
check("empty client_metadata", proxy.background_request_reason({"client_metadata": {}}) == "")
check("client_metadata is not an object",
      proxy.background_request_reason({"client_metadata": "nope"}) == "")
check("a payload that is not an object", proxy.background_request_reason(None) == "")
check("unrelated metadata keys only",
      proxy.background_request_reason(flat({"thread_id": "t-1"})) == "")

print()
print("[5] the reason names the fields it matched on, for the log")

reason = proxy.background_request_reason(
    nested({"request_kind": "memory", "thread_source": "memory_consolidation"}))
check("the reason lists the field names", "request_kind" in reason, reason)
check("the reason lists the values", "memory_consolidation" in reason, reason)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)