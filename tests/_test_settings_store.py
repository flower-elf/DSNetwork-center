"""settings.json 的并发读写、坏文件处理与凭据比较;坏文件必须抛错,不能当成空字典。

无网络访问。
"""

import json
import os
import sys
import tempfile
import threading
import time

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


def attempt(fn, *args, **kwargs):
    """返回 (结果, 异常): 用来断言"必须抛错"的路径。"""
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:
        return None, exc


def hand_edit(directory, mutate):
    """直接改写 settings.json, 模拟手工编辑/外部工具写出的文件。"""
    path = S.settings_path(directory)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    mutate(data)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def read_raw(directory):
    with open(S.settings_path(directory), encoding="utf-8") as fh:
        return fh.read()


# ---- [1] 同进程内 load() 与 save() 不能互相踩 ----------------------------------
print("[1] 同进程并发读写不报错")

work = tempfile.mkdtemp(prefix="wb-store-race-")
S.save(work, {"test_model": "glm-5.3", "filler": "x" * 8192})
errors = []
stop = threading.Event()


def reader():
    while not stop.is_set():
        try:
            S.load(work)
        except Exception as exc:
            errors.append(("load", repr(exc)))
            return


def writer(idx):
    payload = {"test_model": "glm-5.3", "writer": idx, "filler": "x" * 8192}
    while not stop.is_set():
        try:
            S.save(work, payload)
        except Exception as exc:
            errors.append(("save", repr(exc)))
            return


threads = [threading.Thread(target=reader) for _ in range(3)]
threads += [threading.Thread(target=writer, args=(i,)) for i in range(2)]
for thread in threads:
    thread.start()
time.sleep(2.0)
stop.set()
for thread in threads:
    thread.join(5)

check("2 秒并发读写没有抛错 (Windows 上曾是 WinError 5)", not errors, errors[:2])
check("搏斗之后文件仍是有效 JSON", S.load(work).get("test_model") == "glm-5.3",
      S.load(work))

# ---- [2] 坏文件必须报错, 不能退回空字典 ---------------------------------------
print()
print("[2] 损坏的 settings.json 必须报错")

broken = tempfile.mkdtemp(prefix="wb-store-broken-")
with open(S.settings_path(broken), "w", encoding="utf-8") as fh:
    fh.write('{"panel_password_hash": "deadbeef", ')

data, exc = attempt(S.load, broken)
check("load() 抛错而不是返回 {}", exc is not None, data)
check("异常里带着文件路径", exc is not None and S.settings_path(broken) in str(exc), exc)
check("异常类型是 settings 专用异常",
      exc is not None and isinstance(exc, getattr(S, "SettingsError", ())), exc)

before = read_raw(broken)
_, exc = attempt(S.set_test_model, broken, "glm-5.3")
check("写入方不会把空字典覆盖到坏文件上", exc is not None, exc)
check("坏文件保持原样, 没被抹掉", read_raw(broken) == before, read_raw(broken))

accepted, exc = attempt(S.verify_panel_password, broken, S.DEFAULT_PANEL_PASSWORD)
check("坏文件下默认密码 admin 不会被放行", exc is not None or accepted is False,
      (accepted, exc))

# ---- [3] 顶层不是对象 --------------------------------------------------------
print()
print("[3] 顶层不是 JSON 对象时报错")

for raw, label in (("[1, 2, 3]", "数组"), ("null", "null"), ('"text"', "字符串"), ("7", "数字")):
    directory = tempfile.mkdtemp(prefix="wb-store-shape-")
    with open(S.settings_path(directory), "w", encoding="utf-8") as fh:
        fh.write(raw)
    data, exc = attempt(S.load, directory)
    check("顶层是 %s 时抛错" % label, exc is not None, data)

empty_object = tempfile.mkdtemp(prefix="wb-store-empty-")
S.save(empty_object, {})
data, exc = attempt(S.load, empty_object)
check("顶层是空对象属于正常情况", exc is None and data == {}, (data, exc))

# ---- [4] BOM ----------------------------------------------------------------
print()
print("[4] 带 BOM 的设置文件按合法 JSON 读取")

bom = tempfile.mkdtemp(prefix="wb-store-bom-")
with open(S.settings_path(bom), "w", encoding="utf-8-sig") as fh:
    json.dump({"test_model": "glm-5.3"}, fh, ensure_ascii=False)
check("BOM 文件读出真实内容", S.load(bom) == {"test_model": "glm-5.3"}, S.load(bom))
check("BOM 文件里的设置生效", S.test_model(bom) == "glm-5.3", S.test_model(bom))

# ---- [5] 文件不存在 ----------------------------------------------------------
print()
print("[5] 全新安装读成空字典")

fresh = tempfile.mkdtemp(prefix="wb-store-fresh-")
check("文件不存在 -> 空字典", S.load(fresh) == {}, S.load(fresh))
check("此时面板密码仍是默认 admin", S.panel_password_is_default(fresh) is True)

# ---- [6] panel_password_rounds ----------------------------------------------
print()
print("[6] panel_password_rounds 非法值必须报错")

rounds_dir = tempfile.mkdtemp(prefix="wb-store-rounds-")
S.set_panel_password(rounds_dir, "s3cret-pw")
check("正常密码可以校验通过",
      S.verify_panel_password(rounds_dir, "s3cret-pw") is True)
check("错误密码不通过", S.verify_panel_password(rounds_dir, "nope") is False)

for value, label in (("abc", "字符串"), (0, "0"), (1, "1"), (True, "布尔值"),
                     (2_000_000, "超出上限"), (-120_000, "负数")):
    hand_edit(rounds_dir, lambda data, v=value: data.update({"panel_password_rounds": v}))
    result, exc = attempt(S.verify_panel_password, rounds_dir, "s3cret-pw")
    check("rounds=%s 抛错而不是静默放行/静默拒绝" % label, exc is not None, result)
    check("rounds=%s 的报错点出字段名" % label,
          exc is not None and "panel_password_rounds" in str(exc), exc)

hand_edit(rounds_dir, lambda data: data.update({"panel_password_rounds": 120_000}))
check("恢复合法 cost 后原密码依旧有效",
      S.verify_panel_password(rounds_dir, "s3cret-pw") is True)

# ---- [7] 非 ASCII 凭据 --------------------------------------------------------
print()
print("[7] 非 ASCII API key 正常参与比较")

keys_dir = tempfile.mkdtemp(prefix="wb-store-keys-")
S.set_api_keys(keys_dir, [
    {"id": "cn", "name": "cn", "key": "密钥-测试-α", "realm": "cn"},
    {"id": "en", "name": "en", "key": "ASCII-key", "realm": "intl"},
])
entry, exc = attempt(S.match_api_key, keys_dir, "密钥-测试-α")
check("非 ASCII key 命中自己", exc is None and (entry or {}).get("id") == "cn", (entry, exc))
check("命中的 entry 带着自己的 realm", (entry or {}).get("realm") == "cn", entry)

miss, exc = attempt(S.match_api_key, keys_dir, "密钥-测试-β")
check("不匹配的非 ASCII key 返回 None", exc is None and miss is None, (miss, exc))

ascii_hit, exc = attempt(S.match_api_key, keys_dir, "ASCII-key")
check("ASCII key 仍然正常", exc is None and (ascii_hit or {}).get("id") == "en",
      (ascii_hit, exc))

extra, exc = attempt(S.match_api_key, keys_dir, "启动-钥匙", extra_keys=["启动-钥匙"])
check("非 ASCII 启动参数 key 命中 launcher 来源",
      exc is None and (extra or {}).get("source") == "launcher", (extra, exc))

extra_miss, exc = attempt(S.match_api_key, keys_dir, "启动-钥匙-错", extra_keys=["启动-钥匙"])
check("不匹配的启动参数 key 返回 None", exc is None and extra_miss is None,
      (extra_miss, exc))

# ---- [8] proxy_slots 的显式 id -----------------------------------------------
print()
print("[8] 补 id 时先让开显式 id")

slots_dir = tempfile.mkdtemp(prefix="wb-store-slots-")
S.save(slots_dir, {"proxy_slots": [
    {"name": "first", "url": "http://10.0.0.1:17901"},
    {"id": "slot-1", "name": "second", "url": "http://10.0.0.2:17902"},
]})
slots = S.proxy_slots(slots_dir)
ids = [slot["id"] for slot in slots]
check("显式 id 后面的空 id 不再撞车", len(set(ids)) == len(ids), ids)
check("显式 id 原样保留", slots[1]["id"] == "slot-1", ids)
check("空 id 拿到没被占用的编号", slots[0]["id"] not in ("", "slot-1"), ids)
check("顺序保持不变", [slot["url"] for slot in slots] ==
      ["http://10.0.0.1:17901", "http://10.0.0.2:17902"], slots)

S.save(slots_dir, {"proxy_slots": [
    {"name": "a", "url": "http://a:1"},
    {"id": "slot-3", "name": "b", "url": "http://b:2"},
    {"name": "c", "url": "http://c:3"},
    {"id": "slot-4", "name": "d", "url": "http://d:4"},
]})
ids = [slot["id"] for slot in S.proxy_slots(slots_dir)]
check("多个空 id 之间也不重复", len(set(ids)) == len(ids), ids)
check("显式 id 与补出来的 id 不相交", set(ids) >= {"slot-3", "slot-4"}, ids)

# ---- [9] 临时文件名 ----------------------------------------------------------
print()
print("[9] save() 不使用固定临时文件名")

tmp_dir = tempfile.mkdtemp(prefix="wb-store-tmp-")
foreign = S.settings_path(tmp_dir) + ".tmp"
with open(foreign, "w", encoding="utf-8") as fh:
    fh.write("另一个进程写到一半的内容")

S.save(tmp_dir, {"test_model": "glm-5.3"})
if os.path.exists(foreign):
    with open(foreign, encoding="utf-8") as fh:
        kept = fh.read()
    check("另一个进程的临时文件没有被截断", kept == "另一个进程写到一半的内容", kept)
else:
    check("另一个进程的临时文件没有被截断", False, "文件被 save() 搬走/删掉了")
check("设置本身写入成功", S.load(tmp_dir) == {"test_model": "glm-5.3"}, S.load(tmp_dir))
check("save() 没有留下自己的临时文件",
      sorted(os.listdir(tmp_dir)) == ["settings.json", "settings.json.tmp"],
      sorted(os.listdir(tmp_dir)))

# ---- [10] 高级功能开关：默认值与显式关闭 -----------------------------------
print()
print("[10] 高级功能开关默认值与读写")

flags_dir = tempfile.mkdtemp(prefix="wb-store-flags-")
flag_defaults = (
    ("prompt_cache_key", False),
    ("affinity_debug", False),
    ("upstream_keepalive", True),
    ("usage_cache", True),
    ("usage_day_buckets", True),
    ("recent_index", False),
)
for key, default in flag_defaults:
    check("%s 默认 %s" % (key, default), getattr(S, key)(flags_dir) is default,
          getattr(S, key)(flags_dir))
    setter = getattr(S, "set_" + key)
    setter(flags_dir, not default)
    check("%s 能改为 %s" % (key, not default),
          getattr(S, key)(flags_dir) is (not default), getattr(S, key)(flags_dir))
    setter(flags_dir, default)
    check("%s 能改回 %s" % (key, default),
          getattr(S, key)(flags_dir) is default, getattr(S, key)(flags_dir))

check("Anthropic Messages 默认关闭", S.enable_anthropic_messages(flags_dir) is False)
S.set_enable_anthropic_messages(flags_dir, True)
check("Anthropic Messages 能打开", S.enable_anthropic_messages(flags_dir) is True)
check("免费模型回退默认关闭", S.free_model_credit_fallback(flags_dir) is False)
S.set_free_model_credit_fallback(flags_dir, True)
check("免费模型回退能打开", S.free_model_credit_fallback(flags_dir) is True)

# 手写文件里的字符串 / 1 不能把默认开启的开关悄悄关掉，也不能把默认关闭的打开。
hand_edit(flags_dir, lambda data: data.update({
    "usage_cache": "false",
    "recent_index": 1,
}))
check("非布尔值按默认值处理", S.usage_cache(flags_dir) is True, S.usage_cache(flags_dir))
check("非布尔值不会打开默认关闭的开关",
      S.recent_index(flags_dir) is False, S.recent_index(flags_dir))

# 环境变量优先于面板值：面板保存仍然写入，读取时被环境变量覆盖。
env_dir = tempfile.mkdtemp(prefix="wb-store-flags-env-")
_old_env = os.environ.get("WB_USAGE_CACHE")
_old_recent = os.environ.get("WB_RECENT_INDEX")
try:
    os.environ["WB_USAGE_CACHE"] = "0"
    check("没有面板值时读取环境变量", S.usage_cache(env_dir) is False,
          S.usage_cache(env_dir))
    S.set_usage_cache(env_dir, True)
    check("环境变量优先于面板值", S.usage_cache(env_dir) is False,
          S.usage_cache(env_dir))
    os.environ.pop("WB_USAGE_CACHE")
    check("环境变量移除后面板值生效", S.usage_cache(env_dir) is True,
          S.usage_cache(env_dir))

    os.environ["WB_USAGE_CACHE"] = "maybe"
    os.environ["WB_RECENT_INDEX"] = "maybe"
    env_fresh = tempfile.mkdtemp(prefix="wb-store-flags-env2-")
    check("未知值回默认开启", S.usage_cache(env_fresh) is True, S.usage_cache(env_fresh))
    check("未知值不会打开默认关闭的开关",
          S.recent_index(env_fresh) is False, S.recent_index(env_fresh))
    os.environ["WB_RECENT_INDEX"] = "on"
    check("明确开启值生效", S.recent_index(env_fresh) is True, S.recent_index(env_fresh))

    os.environ["WB_USAGE_CACHE"] = "0"
    overrides = S.env_overrides()
    entry = overrides.get("usage_cache") or {}
    check("环境变量列入 env_overrides",
          entry.get("name") == "WB_USAGE_CACHE" and entry.get("value") == "关闭",
          overrides)
    check("未设置环境变量的项不列入",
          "prompt_cache_key" not in overrides, overrides)
    os.environ["WB_USAGE_HOUR_KEEP_HOURS"] = "12"
    try:
        usage_overrides = S.env_overrides().get("usage") or {}
        hour = usage_overrides.get("hour_keep_hours") or {}
        check("用量保留的小时数列出环境变量",
              hour.get("name") == "WB_USAGE_HOUR_KEEP_HOURS"
              and hour.get("value") == "12", usage_overrides)
    finally:
        os.environ.pop("WB_USAGE_HOUR_KEEP_HOURS", None)
finally:
    if _old_env is None:
        os.environ.pop("WB_USAGE_CACHE", None)
    else:
        os.environ["WB_USAGE_CACHE"] = _old_env
    if _old_recent is None:
        os.environ.pop("WB_RECENT_INDEX", None)
    else:
        os.environ["WB_RECENT_INDEX"] = _old_recent

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
