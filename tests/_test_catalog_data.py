"""内置模型快照的数据约定。

快照是网关在拿不到远端目录时的唯一来源，字段缺失会让路由和面板出现空模型。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_catalog as C

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


LIMIT_KEYS = ("maxInputTokens", "maxOutputTokens", "maxAllowedSize")

for name, realm in (("STATIC_INTL_MODELS", "intl"), ("STATIC_CN_MODELS", "cn")):
    models = getattr(C, name)
    check("%s 是非空列表" % realm, isinstance(models, list) and bool(models))
    check("%s 元素都是字典" % realm, all(isinstance(m, dict) for m in models))

    ids = [m.get("id") for m in models]
    check("%s 每个模型都有非空 id" % realm,
          all(isinstance(i, str) and i.strip() for i in ids))
    check("%s 每个模型都有非空 name" % realm,
          all(isinstance(m.get("name"), str) and m["name"].strip() for m in models))
    check("%s 的 id 不重复" % realm, len(set(ids)) == len(ids),
          [i for i in ids if ids.count(i) > 1])

    check("%s 的上下文与输出上限都是正整数" % realm,
          all(isinstance(m.get(k), int) and m[k] > 0
              for m in models for k in LIMIT_KEYS),
          [(m.get("id"), k) for m in models for k in LIMIT_KEYS
           if not isinstance(m.get(k), int) or m[k] <= 0])
    check("%s 的 credits 是字符串" % realm,
          all(isinstance(m.get("credits"), str) for m in models),
          [m.get("id") for m in models if not isinstance(m.get("credits"), str)])

    related = [m for m in models if "relatedModels" in m]
    check("%s 的 relatedModels 是名称到 id 的映射" % realm,
          all(isinstance(m["relatedModels"], dict)
              and all(isinstance(k, str) and k.strip()
                      and isinstance(v, str) and v.strip()
                      for k, v in m["relatedModels"].items())
              for m in related),
          [m.get("id") for m in related])

    tags = [m for m in models if "tags" in m]
    check("%s 的 tags 是字符串列表" % realm,
          all(isinstance(m["tags"], list)
              and all(isinstance(t, str) and t.strip() for t in m["tags"])
              for m in tags),
          [m.get("id") for m in tags])

defaults = [m.get("id") for m in C.STATIC_INTL_MODELS if m.get("isDefault")]
check("国内版快照没有默认标记", not any(m.get("isDefault") for m in C.STATIC_CN_MODELS))
check("国际版快照只有一个默认模型", len(defaults) == 1, defaults)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
