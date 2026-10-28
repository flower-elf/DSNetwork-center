# -*- coding: utf-8 -*-

import base64
import json
import re
import secrets
import uuid

# 国际版 5.8.4 桌面客户端抓包：对话请求的版本号与 UA。
# UA 末段 CLI/2.161.0 是客户端内置 CLI 的版本，与桌面端版本无关。
DESKTOP_VERSION_INTL = "5.8.4"
# 对话请求由客户端内置 CLI 子进程发出，该进程固定 CLIENT_INFO_IDE_TYPE=WorkBuddy，
# 见 sidecar-manager.js。buildUserAgent 的 platform 段取该值，application 段取
# product.json 的 applicationName，国际版为 workbuddy-ai，因此首段是大写
# WorkBuddy，次段是小写 workbuddy-ai。
# Electron 主进程的旁路请求 msg-center / conversations 使用
# workbuddy-ai/⋯⋯ workbuddy-ai/⋯⋯，属于另一条链路，不能照抄。
DESKTOP_UA_INTL = "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0"
# 国内版 5.9.3：product.json 的 productName 与 applicationName 都是 WorkBuddy，
# sidecar 把 CLIENT_INFO_PLATFORM 固定成 WorkBuddy，UA 首两段因此同名。内置
# CLI 2.165.0 由 cli/package.json 的 version 给出。
DESKTOP_VERSION_CN = "5.9.3"
DESKTOP_UA_CN = "WorkBuddy/5.9.3 WorkBuddy/5.9.3 CLI/2.165.0"

# 独立 CodeBuddy CLI 的最新版本，取自 npm 包 @tencent-ai/codebuddy-code。
DEFAULT_IDE_VERSION = "2.177.1"
# CodeBuddy 插件在标准 VS Code 宿主里的身份：X-IDE-Version 取 vscode.version，
# X-Product-Version 与 UA 次段取插件 package.json 的 version。
DEFAULT_VSCODE_VERSION = "1.143.1"
DEFAULT_VSCODE_PLUGIN_VERSION = "4.12.39997873"

PRODUCT_DESKTOP = "workbuddy"
PRODUCT_VSCODE = "vscode"
PRODUCT_CLI = "cli"
VALID_PRODUCTS = (PRODUCT_DESKTOP, PRODUCT_VSCODE, PRODUCT_CLI)

# 静态 API Key 由 CodeBuddy 平台签发，形如 ck_<id>.<secret>；pt_ 同属一套
# 签发体系。它不是 JWT，不能按段数校验，只能按前缀识别。
API_KEY_PREFIXES = ("ck_", "pt_")


def is_api_key_token(token):
    """静态 Key 必须是 ck_/pt_ 前缀，且 <id> 与 <secret> 两段都非空。"""
    text = str(token or "")
    for prefix in API_KEY_PREFIXES:
        if text.startswith(prefix):
            key_id, dot, secret = text[len(prefix):].partition(".")
            return bool(key_id and dot and secret)
    return False


def api_key_id(token):
    """ck_<id>.<secret> 的 <id> 段；净化后可拼进文件名。"""
    text = str(token or "")
    for prefix in API_KEY_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if "." in text:
        text = text.split(".", 1)[0]
    return re.sub(r"[^A-Za-z0-9_-]", "", text)


def auth_headers(token, api_key=None):
    """OAuth 与静态 API Key 共用的鉴权头。

    静态 Key 同时发送 Authorization 与 X-API-Key：只发送前者时上游仍返回 200，
    但模型清单会缺少几个模型，且没有任何提示。
    """
    token = str(token or "")
    headers = {"Authorization": "Bearer " + token}
    if (is_api_key_token(token) if api_key is None else api_key):
        headers["X-API-Key"] = token
    return headers

# 各官方客户端 OAuth 登录时 auth/state 携带的 platform 参数，取自客户端产品配置
# 的 authentication.attributes.platform，缺失时取顶层 platform：
#   国内版桌面端 5.9.3 为 workbuddy，
#   国际版桌面端 5.8.4（WorkBuddy-intl-win32-x64-user-5.8.4.41366630）为 workbuddy-ai，
#   VS Code 插件 4.12.39997873 为 VSCode，
#   CodeBuddy CLI 2.177.1 为 CLI。
_LOGIN_PLATFORMS = {
    ("cn", PRODUCT_DESKTOP): "workbuddy",
    ("intl", PRODUCT_DESKTOP): "workbuddy-ai",
}
_LOGIN_PLATFORMS_DEFAULT = {
    PRODUCT_VSCODE: "VSCode",
    PRODUCT_CLI: "CLI",
}


def login_platform(realm, product):
    """auth/state 的 platform 参数，与所选官方客户端的登录请求一致。"""
    product = normalize_product(product)
    realm = "cn" if realm == "cn" else "intl"
    return _LOGIN_PLATFORMS.get((realm, product)) or _LOGIN_PLATFORMS_DEFAULT[product]


def normalize_product(value):
    """把各种写法收敛成 'workbuddy'、'vscode' 或 'cli'。"""
    v = str(value or "").strip().lower()
    if v in ("workbuddy", "wb", "desktop"):
        return PRODUCT_DESKTOP
    if v in ("vscode", "vsc", "ide"):
        return PRODUCT_VSCODE
    if v in ("cli", "codebuddy"):
        return PRODUCT_CLI
    return PRODUCT_DESKTOP


_ENDPOINTS = {
    ("intl", PRODUCT_DESKTOP): ("https://www.workbuddy.ai", "www.workbuddy.ai"),
    # VS Code 插件的产品配置分内部与外部两套：内部 endpoint 是
    # copilot.tencent.com，外部 endpoint 是 www.codebuddy.ai。
    ("intl", PRODUCT_VSCODE): ("https://www.codebuddy.ai", "www.codebuddy.ai"),
    # CLI 使用自己的官方域名，与桌面端、VS Code 插件分开。
    ("intl", PRODUCT_CLI): ("https://www.codebuddy.ai", "www.codebuddy.ai"),
    ("cn", PRODUCT_DESKTOP): ("https://www.workbuddy.cn", "www.workbuddy.cn"),
    ("cn", PRODUCT_VSCODE): ("https://copilot.tencent.com", "copilot.tencent.com"),
    ("cn", PRODUCT_CLI): ("https://www.codebuddy.cn", "www.codebuddy.cn"),
}


def endpoint_for(realm, product):
    """返回 chat base URL 与 X-Domain。身份换了，端点也要跟着换。"""
    key = ("cn" if realm == "cn" else "intl", normalize_product(product))
    return _ENDPOINTS[key]


def domain_for_realm(realm):
    """X-Domain 的值，默认为桌面端。"""
    return endpoint_for(realm, PRODUCT_DESKTOP)[1]


def new_conversation_id():
    """X-Conversation-ID：带连字符的 UUIDv4。"""
    return str(uuid.uuid4())


def new_message_id():
    """X-Request-ID / X-Conversation-Message-ID / -Request-ID / -Root-Request-ID：
    32 位无连字符 hex，即去掉连字符的 UUIDv4。"""
    return uuid.uuid4().hex


def trace_headers():
    """对话请求带的链路头族：traceparent / b3 / X-B3-* / X-Trace-ID。

    main 请求还带 X-B3-ParentSpanId，该值需要真实的父子调用关系，网关无法
    提供，因此不伪造；辅助请求本来也不带这个头。
    """
    trace = new_message_id()
    span = secrets.token_hex(8)
    return {
        "X-Trace-ID": trace,
        "traceparent": "00-%s-%s-01" % (trace, span),
        "b3": "%s-%s-1" % (trace, span),
        "X-B3-TraceId": trace,
        "X-B3-SpanId": span,
        "X-B3-Sampled": "1",
    }


def identity_user_agent(realm, product):
    """这个身份在对话与登录请求上使用的 User-Agent。

    官方客户端由同一套 client info 拼出 UA，登录与对话因此是同一个值：
    桌面端是三段式，VS Code 插件与 CLI 是两段式。
    """
    product = normalize_product(product)
    is_cn = realm == "cn"
    if product == PRODUCT_DESKTOP:
        return DESKTOP_UA_CN if is_cn else DESKTOP_UA_INTL
    if product == PRODUCT_VSCODE:
        return "VSCode/%s CodeBuddy/%s" % (
            DEFAULT_VSCODE_VERSION, DEFAULT_VSCODE_PLUGIN_VERSION)
    return "CLI/%s CodeBuddy/%s" % (DEFAULT_IDE_VERSION, DEFAULT_IDE_VERSION)


def login_trace_headers():
    """auth/state 与 auth/token 的链路头，与官方客户端的 traceSpan 一致。

    登录请求是 root span，`X-B3-ParentSpanId` 为空但仍然发送；b3 是四段式。
    对话请求另有一套（见 trace_headers），两者不要混用。
    """
    trace = new_message_id()
    span = secrets.token_hex(8)
    return {
        "X-Trace-ID": trace,
        "X-B3-TraceId": trace,
        "X-B3-ParentSpanId": "",
        "X-B3-SpanId": span,
        "X-B3-Sampled": "1",
        "b3": "%s-%s-1-" % (trace, span),
    }


def build_identity_headers(product, realm, uid, token,
                           conversation_id=None, enterprise_id="",
                           tenant_id="", department="", domain=""):
    """返回这一轮要用的身份标头。product 取 'workbuddy'、'vscode' 或 'cli'。

    conversation_id 留空时每次请求新生成一个 UUIDv4，这是网关的常态。
    上游不按这个 id 保存历史：同一 id 连发两轮，第二轮只带 system 与 user
    消息时，模型仍答不出上一轮；换新 id 结果相同，因此网关不需要在会话
    维度维护映射。
    """
    product = normalize_product(product)
    is_cn = (realm == "cn")

    conv = str(conversation_id or "").strip() or new_conversation_id()
    msg_id = new_message_id()
    # main 请求里 X-Conversation-Request-ID 与 X-Root-Request-ID 取同一个值，
    # 且早于消息 id。该值在每次 user send 开始时生成，这一轮的所有请求复用。
    conv_req_id = new_message_id()

    if product == PRODUCT_DESKTOP:
        client_ver = DESKTOP_VERSION_CN if is_cn else DESKTOP_VERSION_INTL
        chat_ua = identity_user_agent(realm, product)
        headers = {
            "X-Agent-Intent": "craft",
            "X-Agent-Purpose": "conversation",
            "X-Agent-Type": "main",
            "X-Agent-Runtime": "other",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Version": client_ver,
            "X-Product": "SaaS",
            "X-Domain": str(domain or endpoint_for(realm, product)[1]),
            "User-Agent": chat_ua,
            "X-User-Id": str(uid or "anonymous"),
            "X-Conversation-ID": conv,
            "X-Conversation-Request-ID": conv_req_id,
            "X-Root-Request-ID": conv_req_id,
            "X-Conversation-Message-ID": msg_id,
            "X-Request-ID": msg_id,
        }
        headers.update(trace_headers())
    elif product == PRODUCT_VSCODE:
        headers = {
            "X-Agent-Intent": "craft",
            "X-Agent-Type": "main",
            "X-Agent-Purpose": "conversation",
            "X-Agent-Runtime": "other",
            "X-IDE-Type": "VSCode",
            "X-IDE-Name": "VSCode",
            "X-IDE-Version": DEFAULT_VSCODE_VERSION,
            "X-Product-Version": DEFAULT_VSCODE_PLUGIN_VERSION,
            "X-Env-ID": "production",
            "X-Product": "SaaS",
            "X-Domain": str(domain or endpoint_for(realm, product)[1]),
            "User-Agent": identity_user_agent(realm, product),
            "X-User-Id": str(uid or "anonymous"),
            "X-Conversation-ID": conv,
            "X-Conversation-Request-ID": conv_req_id,
            "X-Root-Request-ID": conv_req_id,
            "X-Conversation-Message-ID": msg_id,
            "X-Request-ID": msg_id,
        }
    else:
        ide_version = DEFAULT_IDE_VERSION
        headers = {
            "X-Agent-Intent": "craft",
            "X-Agent-Type": "main",
            "X-Agent-Purpose": "conversation",
            "X-Agent-Runtime": "other",
            "X-IDE-Type": "CLI",
            "X-IDE-Name": "CLI",
            "X-IDE-Version": ide_version,
            "X-Product": "SaaS",
            "X-Domain": str(domain or endpoint_for(realm, product)[1]),
            "User-Agent": identity_user_agent(realm, product),
            "X-User-Id": str(uid or "anonymous"),
            "X-Conversation-ID": conv,
            "X-Conversation-Request-ID": conv_req_id,
            "X-Root-Request-ID": conv_req_id,
            "X-Conversation-Message-ID": msg_id,
            "X-Request-ID": msg_id,
        }
        headers.update(trace_headers())

    headers.update(auth_headers(token))

    # 网关不提供数据用途选项，统一声明数据私有：true 表示不参与模型优化。
    headers["X-Private-Data"] = "true"

    if enterprise_id:
        headers["X-Enterprise-Id"] = str(enterprise_id)
        headers["X-Tenant-Id"] = str(tenant_id or enterprise_id)
        userinfo = {"uin": str(uid or ""), "owner_uin": str(enterprise_id)}
        headers["X-Userinfo"] = base64.b64encode(
            json.dumps(userinfo, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
    if department:
        headers["X-Department-Info"] = str(department)
    headers.setdefault("Accept", "application/json")
    headers.setdefault("Content-Type", "application/json")

    return headers
