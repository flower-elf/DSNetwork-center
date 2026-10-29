# 工具参数 schema 递归规整与 tool_calls.arguments 序列化的回归测试。
# 上游 Go 校验器对空参数、Python 类型别名与 null 元数据会回 11129/11101。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-tool-params-norm-")

import wb_proxy as P
import wb_sanitize as S

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


print("[1] 空参数补成对象 schema")
fn = {"name": "get_time", "parameters": {}}
S._normalize_tool_parameters(fn)
check("type 为 object", fn["parameters"].get("type") == "object", fn["parameters"])
check("properties 为字典", isinstance(fn["parameters"].get("properties"), dict), fn["parameters"])

print("[2] 非字典参数重置")
for value in (None, [], "str", True):
    fn = {"name": "t", "parameters": value}
    S._normalize_tool_parameters(fn)
    check("参数 %r 重置为对象" % (value,),
          fn["parameters"] == {"type": "object", "properties": {}}, fn["parameters"])

print("[3] required 清理")
fn = {"name": "t", "parameters": {"type": "object", "properties": {"a": {}}, "required": None}}
S._normalize_tool_parameters(fn)
check("required 为 None 时删除", "required" not in fn["parameters"], fn["parameters"])
fn = {"name": "t", "parameters": {"type": "object", "properties": {"a": {}}, "required": "a"}}
S._normalize_tool_parameters(fn)
check("required 字符串包成列表", fn["parameters"].get("required") == ["a"], fn["parameters"])
fn = {"name": "t", "parameters": {"type": "object", "properties": {"a": {}, "b": {}},
                                   "required": ["a", None, "b"]}}
S._normalize_tool_parameters(fn)
check("required 去掉 None", fn["parameters"].get("required") == ["a", "b"], fn["parameters"])

print("[4] 深度类型别名与 null 元数据")
fn = {"name": "calc", "parameters": {
    "type": "dict",
    "properties": {
        "a": {"type": "int", "description": None},
        "b": {"type": "float"},
        "c": {"type": "str"},
        "d": {"type": "bool"},
        "list": {"type": "list", "items": {"type": "str"}},
    },
}}
S._normalize_tool_parameters(fn)
p = fn["parameters"]
check("dict -> object", p.get("type") == "object", p)
check("int -> integer", p["properties"]["a"].get("type") == "integer", p)
check("float -> number", p["properties"]["b"].get("type") == "number", p)
check("str -> string", p["properties"]["c"].get("type") == "string", p)
check("bool -> boolean", p["properties"]["d"].get("type") == "boolean", p)
check("list -> array，内层 -> string",
      p["properties"]["list"].get("type") == "array"
      and p["properties"]["list"]["items"].get("type") == "string", p)

print("[5] 组合关键字与 definitions 递归")
fn = {"name": "deep", "parameters": {
    "type": "object",
    "properties": {
        "choice": {"anyOf": [{"type": "str"}, {"type": "int"}]},
        "nested": {"type": "object", "properties": {"x": {"title": None, "type": "str"}}},
    },
    "definitions": {"row": {"type": "obj"}},
}}
S._normalize_tool_parameters(fn)
check("anyOf 内类型被规整",
      fn["parameters"]["properties"]["choice"]["anyOf"][0]["type"] == "string"
      and fn["parameters"]["properties"]["choice"]["anyOf"][1]["type"] == "integer",
      fn["parameters"]["properties"]["choice"])
check("嵌套 null title 被删除",
      "title" not in fn["parameters"]["properties"]["nested"]["properties"]["x"],
      fn["parameters"]["properties"]["nested"])
check("definitions 内的类型被处理",
      fn["parameters"]["definitions"]["row"].get("type") is None,
      fn["parameters"]["definitions"])

print("[6] normalize_tools 端到端")
obj = {"tools": [
    {"type": "function", "function": {"name": "fn1", "parameters": {}}},
    {"type": "function", "name": "fn2", "parameters": {}},
]}
P.normalize_tools(obj)
check("嵌套 function 被规整",
      obj["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}, obj)
check("顶层 name 被包装并规整",
      obj["tools"][1]["function"]["parameters"] == {"type": "object", "properties": {}}, obj)

obj = {"tools": []}
P.normalize_tools(obj)
check("空 tools 被删除", "tools" not in obj, obj)

obj = {"functions": [{"name": "legacy", "parameters": {}}]}
P.normalize_tools(obj)
check("legacy functions 折叠成 tools", "functions" not in obj and len(obj.get("tools", [])) == 1, obj)
check("legacy 参数被规整",
      obj["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}, obj)

server_tool = {"type": "web_search", "name": "web_search"}
obj = {"tools": [server_tool]}
P.normalize_tools(obj)
check("非 function 服务端工具原样保留",
      obj["tools"] == [server_tool], obj)

print("[7] tool_calls.arguments 非字符串序列化")
calls = [
    {"id": "1", "type": "function", "function": {"name": "a", "arguments": {"x": 1}}},
    {"id": "2", "type": "function", "function": {"name": "b", "arguments": None}},
    {"id": "3", "type": "function", "function": {"name": "c", "arguments": [1, 2]}},
    {"id": "4", "type": "function", "function": {"name": "d", "arguments": True}},
]
res = S.sanitize_tool_calls(calls)
check("对象转 JSON 字符串", res[0]["function"]["arguments"] == '{"x": 1}', res[0])
check("None 转 {}", res[1]["function"]["arguments"] == "{}", res[1])
check("数组转 JSON 字符串", res[2]["function"]["arguments"] == "[1, 2]", res[2])
check("布尔转 JSON 字符串", res[3]["function"]["arguments"] == "true", res[3])

print("")
print("PASS %d  FAIL %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
