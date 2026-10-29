import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402
# 独立目录，进程退出时删除。
_TMP = isolated_data_dirs("wb-stop-normalization-")

import wb_proxy as P

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def norm(stop, present=True):
    obj = {"model": "deepseek-v4.1-flash"}
    if present:
        obj["stop"] = stop
    P.normalize_stop(obj)
    return obj


print("[1] a single string becomes a one-element array")
o = norm("END")
check("string wrapped", o.get("stop") == ["END"], o.get("stop"))
o = norm("\n\n")
check("whitespace string survives as one element", o.get("stop") == ["\n\n"], o.get("stop"))

print()
print("[2] the array form is left semantically intact")
o = norm(["END", "STOP"])
check("two elements kept in order", o.get("stop") == ["END", "STOP"], o.get("stop"))
o = norm(["END", "", "STOP"])
check("empty elements dropped", o.get("stop") == ["END", "STOP"], o.get("stop"))
o = norm(["END", 7, None])
check("non-string elements dropped", o.get("stop") == ["END"], o.get("stop"))

print()
print("[3] empty and invalid values delete the field instead of forwarding junk")
for label, value in (("empty string", ""), ("empty array", []), ("null", None),
                     ("number", 7), ("object", {"a": 1}), ("bool", True)):
    o = norm(value)
    check("%s -> field removed" % label, "stop" not in o, o.get("stop"))

print()
print("[4] a body without stop never gains one")
o = norm(None, present=False)
check("no stop invented", "stop" not in o, o.get("stop"))

print()
print("[5] build_upstream_body normalizes end to end")
def body_with(stop):
    return P.build_upstream_body({
        "model": "deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "stop": stop,
    })

check("string -> array through the full pipeline",
      body_with("END").get("stop") == ["END"], body_with("END").get("stop"))
check("array -> array through the full pipeline",
      body_with(["END"]).get("stop") == ["END"], body_with(["END"]).get("stop"))
check("empty string -> field gone through the full pipeline",
      "stop" not in body_with(""), body_with("").get("stop"))
check("no stop stays absent through the full pipeline",
      "stop" not in body_with(None), body_with(None).get("stop"))

print()
print("[6] a tuple (Python-side caller) is normalized like a list")
o = norm(("END", "STOP"))
check("tuple becomes a list", o.get("stop") == ["END", "STOP"], o.get("stop"))

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
