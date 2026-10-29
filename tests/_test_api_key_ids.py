"""API Key 行 id 必须唯一，否则复制按钮会复制到错误的 key：reveal 按 id 取第一行匹配，读取时会把存储的重复 id 拆开。运行 python tests/_test_api_key_ids.py，无需网络。
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_settings as S

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def write_settings(d, entries):
    S.save(d, {"api_keys": entries})


print("[1] a new row cannot reuse the id of a row deleted earlier")

# 复现按位置取 id 的冲突场景：存 0..2 三行、删第 1 行、再在 index 1 追加新行。
d = tempfile.mkdtemp(prefix="wb-keyids-")
S.set_api_keys(d, [
    {"id": "k0", "name": "a", "key": "k-a"},
    {"id": "k1", "name": "b", "key": "k-b"},
    {"id": "k2", "name": "c", "key": "k-c"},
])
S.set_api_keys(d, [
    {"id": "k0", "name": "a", "key": "k-a"},
    {"id": "k2", "name": "c", "key": "k-c"},
    {"id": "", "name": "123", "key": "k-881c"},
])
reloaded = S.api_keys(d)
ids = [entry["id"] for entry in reloaded]
check("all ids stay unique", len(set(ids)) == len(ids), ids)
check("existing rows keep their ids", ids[:2] == ["k0", "k2"], ids)

print()
print("[2] a legacy file with a duplicate id is read back with unique ids")

d2 = tempfile.mkdtemp(prefix="wb-keyids-")
write_settings(d2, [
    {"id": "k6", "name": "ChatGPT-HOME_Global", "key": "key-2a1c", "realm": "intl"},
    {"id": "k6", "name": "123", "key": "key-881c", "realm": "cn"},
])
entries = S.api_keys(d2)
check("both rows survive the read", len(entries) == 2, entries)
ids = [entry["id"] for entry in entries]
check("duplicate id was disambiguated", len(set(ids)) == 2, ids)
check("the first row keeps the original id", ids[0] == "k6", ids)
check("keys are not swapped", [e["key"] for e in entries] == ["key-2a1c", "key-881c"],
      [e["key"] for e in entries])

print()
print("[3] the disambiguated id is what gets persisted")

# 保存不能把刚修复的重复 id 再写回。
S.set_api_keys(d2, entries)
on_disk = json.loads(open(S.settings_path(d2), encoding="utf-8").read())["api_keys"]
disk_ids = [entry["id"] for entry in on_disk]
check("persisted ids are unique", len(set(disk_ids)) == len(disk_ids), disk_ids)
check("persisted ids match what was read", disk_ids == ids, (disk_ids, ids))
check("the 123 row still holds its own key",
      on_disk[1]["key"] == "key-881c", on_disk[1])

print()
print("[4] a blank id on a legacy row does not swallow the stored key")

d3 = tempfile.mkdtemp(prefix="wb-keyids-")
write_settings(d3, [{"id": "", "name": "solo", "key": "key-solo"}])
entries = S.api_keys(d3)
check("a blank id is filled in on read", entries[0]["id"] != "", entries)
check("the stored key is preserved", entries[0]["key"] == "key-solo", entries)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
