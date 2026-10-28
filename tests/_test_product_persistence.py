"""账号的对外身份必须跨重启保留（issue #76）。

面板选择器与 429 身份切换写入的 product 字段保存在凭据文件里，加载时读回，
因此重启后仍是同一个出站身份。

面板的 WB、VSC、CLI 选择器与 429 身份切换都会改写 account.product，每条保存
路径都把它写回凭据文件，加载时按它恢复出站身份。

[5]-[7] 同时钉住出站身份：三套身份都要带会话头族（缺少时上游返回 400 / code
11-128，`conversation id missing`），桌面端的版本 / UA / agent 头，以及 id 形态
（带连字符的 UUIDv4 与 32 位 hex）。国内版取值来自 5.9.3 安装包，国际版来自
5.8.4 抓包。

不联网：账号由字典构造，写入临时目录。
"""
import json
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-product-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts as A
import wb_fingerprint
import wb_identity

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def credential(product="__missing__", **extra):
    data = {"uid": "u-product-1", "domain": "www.workbuddy.ai", "realm": "intl",
            "accessToken": ""}
    if product != "__missing__":
        data["product"] = product
    data.update(extra)
    return data


print("[1] the identity stored in the credential file is what loads")
for want in ("workbuddy", "vscode", "cli"):
    acc = A.Account(credential(want))
    check("a file saying %s loads as %s" % (want, want), acc.product == want, acc.product)
check("the aliases the panel may have written are accepted",
      A.Account(credential("vsc")).product == "vscode"
      and A.Account(credential("codebuddy")).product == "cli")

print()
print("[2] missing or unusable values keep the pre-upgrade default")
check("no product field -> workbuddy",
      A.Account(credential("__missing__")).product == "workbuddy")
check("an empty value -> workbuddy", A.Account(credential("")).product == "workbuddy")
check("None -> workbuddy", A.Account(credential(None)).product == "workbuddy")
check("an unknown value -> workbuddy", A.Account(credential("chatgpt")).product == "workbuddy")

print()
print("[3] a switched identity round-trips through the credential file")
acc = A.Account(credential("workbuddy"))
check("set_product reports a real change as True", acc.set_product("cli") is True)
check("set_product reports a no-op as False", acc.set_product("cli") is False)
acc.save(_TMP)
path = os.path.join(_TMP, "u-product-1.json")
on_disk = json.load(open(path, encoding="utf-8"))
check("the file carries the switched identity", on_disk.get("product") == "cli",
      on_disk.get("product"))
reloaded = A.Account(json.load(open(path, encoding="utf-8")))
check("a reload keeps the switched identity", reloaded.product == "cli", reloaded.product)

print()
print("[4] the loaded identity reaches everything that depends on it")
cn = A.Account(credential("vscode", realm="cn", domain="www.workbuddy.cn"))
check("public() reports it, so the panel highlights the right button",
      cn.public().get("product") == "vscode", cn.public().get("product"))
check("to_dict() writes it, so the next save keeps it",
      cn.to_dict().get("product") == "vscode", cn.to_dict().get("product"))
check("the chat endpoint follows it",
      cn.chat_base_url() == wb_identity.endpoint_for("cn", "vscode")[0], cn.chat_base_url())
check("the CN VSCode plugin uses the internal endpoint",
      cn.chat_base_url() == "https://copilot.tencent.com", cn.chat_base_url())
check("the intl VSCode plugin uses the external endpoint",
      wb_identity.endpoint_for("intl", "vscode")[0]
      == "https://www.codebuddy.ai")
check("an old VSCode credential uses the internal endpoint domain",
      cn.headers().get("X-Domain") == "copilot.tencent.com",
      cn.headers().get("X-Domain"))
cli_old = A.Account(credential("cli", realm="cn", domain="www.workbuddy.cn"))
check("an old CLI credential uses the codebuddy endpoint domain",
      cli_old.headers().get("X-Domain") == "www.codebuddy.cn",
      cli_old.headers().get("X-Domain"))
check("billing keeps the credential domain",
      cli_old.headers(purpose="billing").get("X-Domain") == "www.workbuddy.cn",
      cli_old.headers(purpose="billing").get("X-Domain"))
custom = A.Account(credential("cli", realm="cn", domain="ai.example.com"))
check("a custom domain is preserved",
      custom.headers().get("X-Domain") == "ai.example.com",
      custom.headers().get("X-Domain"))
desktop = A.Account(credential("workbuddy", realm="cn", domain="www.workbuddy.cn"))
check("the CN desktop uses the product chat endpoint",
      desktop.chat_base_url() == "https://www.workbuddy.cn",
      desktop.chat_base_url())
check("the CN desktop sends the credential domain",
      desktop.headers().get("X-Domain") == "www.workbuddy.cn",
      desktop.headers().get("X-Domain"))
check("the outbound headers follow it",
      cn.headers().get("X-IDE-Type") == "VSCode"
      and desktop.headers().get("X-IDE-Type") == "WorkBuddy",
      (cn.headers().get("X-IDE-Type"), desktop.headers().get("X-IDE-Type")))

print()
print("[5] the outbound identity carries the conversation header family")
# 上游的渠道校验在请求完全没有会话 id 时返回 400 / code 11-128
# （`conversation id missing`，displayTips「这次调用没通过渠道校验」）。这三种
# 身份都要带这组标头：VSCode、CLI 与桌面端（所有账号默认使用的那组）。形态按
# 桌面端对话请求的抓包：会话 id 是带连字符的 UUID，请求/消息 id 是 32 位无连字符 hex。
CONV_HEADERS = ("X-Conversation-ID", "X-Conversation-Request-ID",
                "X-Root-Request-ID", "X-Conversation-Message-ID")
HEX32 = ("X-Request-ID", "X-Conversation-Request-ID", "X-Root-Request-ID",
         "X-Conversation-Message-ID")
for want in ("workbuddy", "vscode", "cli"):
    h = wb_identity.build_identity_headers(want, "intl", "u1", "tok")
    check("%s sends the conversation header family" % want,
          all(h.get(k) for k in CONV_HEADERS),
          [k for k in CONV_HEADERS if not h.get(k)])
    check("%s: message id equals request id" % want,
          h.get("X-Conversation-Message-ID") == h.get("X-Request-ID"))
    check("%s: root request id equals conversation request id" % want,
          h.get("X-Root-Request-ID") == h.get("X-Conversation-Request-ID"))
    conv = str(h.get("X-Conversation-ID"))
    check("%s: the conversation id is a dashed uuid" % want,
          len(conv) == 36 and conv.count("-") == 4, conv)
    check("%s: the request/message ids are 32 hex chars" % want,
          all(len(str(h.get(k))) == 32
              and all(c in "0123456789abcdef" for c in str(h.get(k)))
              for k in HEX32),
          [h.get(k) for k in HEX32])
check("a fresh conversation id per call (upstream keeps no history by it)",
      wb_identity.build_identity_headers("workbuddy", "intl", "u1", "tok")["X-Conversation-ID"]
      != wb_identity.build_identity_headers("workbuddy", "intl", "u1", "tok")["X-Conversation-ID"])
a4 = wb_identity.new_message_id()
b4 = wb_identity.new_message_id()
check("request ids are 32 hex UUIDv4",
      len(a4) == 32 and a4 != b4
      and uuid.UUID(a4).version == 4
      and uuid.UUID(b4).version == 4, (a4, b4))

print()
print("[6] the desktop identity matches the official desktop clients")
d = wb_identity.build_identity_headers("workbuddy", "intl", "u1", "tok")
check("X-IDE-Version is 5.8.4", d.get("X-IDE-Version") == "5.8.4",
      d.get("X-IDE-Version"))
check("the UA is the captured one",
      d.get("User-Agent") == "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0",
      d.get("User-Agent"))
check("X-Product is SaaS", d.get("X-Product") == "SaaS", d.get("X-Product"))
check("X-Agent-Type / X-Agent-Purpose / X-Agent-Intent / X-Agent-Runtime",
      (d.get("X-Agent-Type"), d.get("X-Agent-Purpose"), d.get("X-Agent-Intent"),
       d.get("X-Agent-Runtime"))
      == ("main", "conversation", "craft", "other"),
      (d.get("X-Agent-Type"), d.get("X-Agent-Purpose"), d.get("X-Agent-Intent"),
       d.get("X-Agent-Runtime")))
check("X-Private-Data is true", d.get("X-Private-Data") == "true")
check("the trace family is present",
      all(d.get(k) for k in ("X-Trace-ID", "traceparent", "b3",
                             "X-B3-TraceId", "X-B3-SpanId", "X-B3-Sampled")))
check("traceparent follows the W3C shape",
      str(d.get("traceparent", "")).startswith("00-")
      and str(d.get("traceparent", "")).endswith("-01"), d.get("traceparent"))
check("the CN desktop keeps its own version and UA",
      wb_identity.build_identity_headers("workbuddy", "cn", "u1", "tok")["X-IDE-Version"]
      == "5.9.3" and
      wb_identity.build_identity_headers("workbuddy", "cn", "u1", "tok")["User-Agent"]
      == "WorkBuddy/5.9.3 WorkBuddy/5.9.3 CLI/2.165.0")
check("the CN chat endpoint is the CN product endpoint",
      wb_identity.endpoint_for("cn", "workbuddy")[0]
      == "https://www.workbuddy.cn")
check("the CLI intl endpoint is codebuddy.ai",
      wb_identity.endpoint_for("intl", "cli")[0]
      == "https://www.codebuddy.ai")
check("the CLI CN endpoint is codebuddy.cn",
      wb_identity.endpoint_for("cn", "cli")[0]
      == "https://www.codebuddy.cn")
check("the CLI CN identity defaults to the codebuddy.cn domain",
      wb_identity.build_identity_headers(
          "cli", "cn", "u1", "tok")["X-Domain"] == "www.codebuddy.cn")
check("X-Domain follows the credential domain",
      wb_identity.build_identity_headers(
          "workbuddy", "cn", "u1", "tok",
          domain="www.workbuddy.cn")["X-Domain"] == "www.workbuddy.cn")

print()
print("[7] chat headers drop the feedback-only ids and keep the fingerprint shape")
conv_acc = A.Account(credential("workbuddy"))
chat = conv_acc.headers(purpose="chat")
check("no X-Machine-ID on the chat request", "X-Machine-ID" not in chat)
check("no X-Session-ID on the chat request", "X-Session-ID" not in chat)
check("the billing headers carry no conversation family",
      "X-Conversation-ID" not in conv_acc.headers(purpose="billing"))
machine = wb_fingerprint.derive_id("u1", "machine")
check("the machine id is a stable dashed uuid",
      machine == wb_fingerprint.derive_id("u1", "machine")
      and len(machine) == 36 and machine.count("-") == 4, machine)
# 每账号一台设备：sessionId 必须按账号派生。取进程级的值会让全部账号共用
# 同一个 session id，那是把它们串起来的关联键（device_token 是同一类问题）。
sess = wb_fingerprint.session_id("u1")
check("the session id is a stable dashed uuid",
      sess == wb_fingerprint.session_id("u1")
      and len(sess) == 36 and sess.count("-") == 4, sess)
check("the session id differs across accounts",
      wb_fingerprint.session_id("u1") != wb_fingerprint.session_id("u2"))
check("the session id is not the machine id",
      wb_fingerprint.session_id("u1") != machine)
check("the request id is 32 hex chars",
      len(wb_fingerprint.generate_request_id("u1")) == 32)

# 计费路径同样按账号隔离：两个账号的 X-Session-ID 不能相同。
sess_acc_a = A.Account(credential("workbuddy"))
sess_acc_b = A.Account(credential("workbuddy"))
sess_acc_b.uid = sess_acc_a.uid + "-other"
check("the billing session id is per account",
      sess_acc_a.headers(purpose="billing").get("X-Session-ID")
      != sess_acc_b.headers(purpose="billing").get("X-Session-ID"),
      sess_acc_a.headers(purpose="billing").get("X-Session-ID"))
check("the billing session id is stable for one account",
      sess_acc_a.headers(purpose="billing").get("X-Session-ID")
      == sess_acc_a.headers(purpose="billing").get("X-Session-ID"))

print()
print("[8] every desktop UA / version comes from wb_identity (single source)")
# PR #381 把 wb_identity 立为桌面端身份的唯一来源，但漏改了 REALM_CONFIGS
# 与两个死常量里各写一份的旧值（intl 那份还停在 5.5.2）。REALM_CONFIGS 的
# chat_ua 不只是留档：remote_config_headers() 拿它拉产品目录，UA 不对上游
# 直接 400 / code 12403。这几条断言把「单一来源」钉住，下次对齐漏一处就红。
for realm in ("intl", "cn"):
    cfg = A.get_realm_config(realm)
    want_ua = (wb_identity.DESKTOP_UA_INTL if realm == "intl"
               else wb_identity.DESKTOP_UA_CN)
    want_ver = (wb_identity.DESKTOP_VERSION_INTL if realm == "intl"
                else wb_identity.DESKTOP_VERSION_CN)
    check("%s chat_ua is wb_identity's" % realm, cfg["chat_ua"] == want_ua,
          cfg["chat_ua"])
    check("%s billing_ua is built from wb_identity's version" % realm,
          cfg["billing_ua"] == "WorkBuddy/" + want_ver, cfg["billing_ua"])
check("DEFAULT_UA_VERSION is wb_identity's intl version",
      A.DEFAULT_UA_VERSION == wb_identity.DESKTOP_VERSION_INTL,
      A.DEFAULT_UA_VERSION)
check("USER_AGENT follows the intl chat UA", A.USER_AGENT == wb_identity.DESKTOP_UA_INTL,
      A.USER_AGENT)
# 5.5.2 是这次修掉的过时值，任何身份常量再退回它都算回归。
check("no realm still advertises the stale 5.5.2",
      all("5.5.2" not in str(A.get_realm_config(r).get(k, ""))
          for r in ("intl", "cn") for k in ("chat_ua", "billing_ua")))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
