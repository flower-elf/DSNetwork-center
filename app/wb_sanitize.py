"""请求内容的净化与工具调用修复。

客户端（Codex、Claude Code、DSH 等）的提示词与工具定义带着各自的指纹，上游按
指纹拦截；这里在出站前抹掉这些标记，并把打印成文本的工具调用还原成结构化调用。
"""

import json
import re
import time

import wb_host
import wb_ids
import wb_settings


# ---------------------------------------------------------------------------
# 指纹净化：抹掉会触发上游 WAF 的客户端标记
# ---------------------------------------------------------------------------
SANITIZE_FEATURES = (
    "x-anthropic-billing-header",
    "cc_entrypoint=",
    "You are Claude Code",
    "Main branch (",
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
    "11128",
)
SANITIZE_REWRITES = (
    ("You are Claude Code, Anthropic's official CLI for Claude",
     "You are Claude Code, Anthropic's official CLI tool for Claude"),
    ("Main branch (you will usually use this for PRs)",
     "Default branch (you will usually use this for PRs)"),
    ("You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
     "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."),
    ("To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
     "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues"),
    ("11128", "11-128"),
)
SANITIZE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header:[^;\r\n]*;?\s*")
SANITIZE_BARE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header")
SANITIZE_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\r\n]*;?\s*")
# 这条 OmO 身份指纹以连续子串出现在 system 消息里时，上游返回 11-128
# （Illegal API invocation from an unapproved channel）。匹配不区分大小写，前后
# 可有其他文本；只重写这一处确认过的指纹，保留代理身份与行为。
SANITIZE_OMO_JUNIOR_RE = re.compile(
    r"Sisyphus-Junior - Focused executor from OhMyOpenCode", re.IGNORECASE
)
# 指纹判定的字面量快路径（见 has_fingerprint）：每个文本段都过一遍正则太贵，
# 两个模式又都是纯字面量，改在小写副本上做 `in`，是纯 C 的单遍扫描。
#
# 等价性：re.IGNORECASE 与 str.lower() 只在三个码点上不同——'İ'(U+0130)、
# 'ı'(U+0131) 能匹配字面量 i，'ſ'(U+017F) 能匹配 s，而它们的 lower() 不落到
# i/s。先按 SANITIZE_REI_FOLD 归一化成基字母再小写，副本上的 `in` 与 (?i)
# search 完全等价；对拍见 tests/_test_hotpath_savings.py。
SANITIZE_REI_FOLD = str.maketrans({0x130: "i", 0x131: "i", 0x17F: "s"})
# 两个小写字面量直接从正则派生，保证与正则永远同源：手抄第二份迟早漂移，
# 派生出来的值不存在这个问题。pattern 里的 (?i) 是内联旗标，剥掉后就是
# 字面量本体。
SANITIZE_BARE_HDR_LOWER = SANITIZE_BARE_HDR_RE.pattern
if SANITIZE_BARE_HDR_LOWER.startswith("(?i)"):
    SANITIZE_BARE_HDR_LOWER = SANITIZE_BARE_HDR_LOWER[4:]
SANITIZE_OMO_JUNIOR_LOWER = SANITIZE_OMO_JUNIOR_RE.pattern.lower()
def has_fingerprint(text):
    if not isinstance(text, str) or not text:
        return False
    for f in SANITIZE_FEATURES:
        if f in text:
            return True
    # 三个特例字符几乎从不出现：先做三次 C 速度探测，命中才走归一化副本，
    # 常规文本只付一次 lower()。
    if "\u0130" in text or "\u0131" in text or "\u017f" in text:
        low = text.translate(SANITIZE_REI_FOLD).lower()
    else:
        low = text.lower()
    return SANITIZE_BARE_HDR_LOWER in low or SANITIZE_OMO_JUNIOR_LOWER in low
def sanitize_text(text):
    if not isinstance(text, str) or not text:
        return text
    if not has_fingerprint(text):
        return text
    # 改写范围刻意收窄：不整体删掉 "OhMyOpenCode" 或 "Sisyphus-Junior"，
    # 两个词单独出现时上游都接受，只中和这处确认过的连续指纹。
    text = SANITIZE_OMO_JUNIOR_RE.sub("Sisyphus-Junior - Focused executor", text)
    for old, new in SANITIZE_REWRITES:
        text = text.replace(old, new)
    text = SANITIZE_HDR_RE.sub("", text)
    if "cc_" in text:
        prev = ""
        while prev != text:
            prev = text
            text = SANITIZE_KV_RE.sub("", text)
    text = SANITIZE_BARE_HDR_RE.sub("x-anthropic-billing-hdr", text)
    return text.strip()
def sanitize_content(content):
    if isinstance(content, str):
        return sanitize_text(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and "text" in part:
                p = dict(part)
                p["text"] = sanitize_text(p["text"])
                out.append(p)
            else:
                out.append(part)
        return out
    return content
def sanitize_tool_calls(tool_calls):
    if not isinstance(tool_calls, list):
        return tool_calls
    out = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            item = dict(tc)
            fn = item.get("function")
            if isinstance(fn, dict):
                fn = dict(fn)
                raw = fn.get("arguments")
                if isinstance(raw, str):
                    fn["arguments"] = sanitize_text(raw)
                elif raw is None:
                    fn["arguments"] = "{}"
                else:
                    # 上游 Go 结构要求 arguments 是字符串；客户端传对象、数组、
                    # 数字或布尔值时统一转成 JSON 文本，避免 11101。
                    try:
                        fn["arguments"] = json.dumps(raw, ensure_ascii=False)
                    except Exception:
                        fn["arguments"] = str(raw)
                item["function"] = fn
            out.append(item)
        else:
            out.append(tc)
    return out
# 官方 file-reference 插件注入的 @ 路径说明段里，前两句连续出现即命中 11-128，
# 判据与 GUI 描述段相同：单发首句或次句都放行，两句连排就拒绝。
# 首句补一个 that 即可破坏这段连续原文，语义不变，负向前瞻保证重复执行不再改写。
SANITIZE_DSH_FILEREF_RE = re.compile(
    r"Tokens prefixed with @ are paths (?!that )the user explicitly referenced\.",
    re.IGNORECASE)
SANITIZE_DSH_FILEREF_REPLACEMENT = (
    "Tokens prefixed with @ are paths that the user explicitly referenced.")
# DSH 客户端独有标识：品牌全名、DSH 缩写、Web 启动命令与 boot 全局名。
# 这些字符串会出现在 system 提示和运行时上下文里，统一换成中性表述，
# 保留原有语义，同时缩小上游后续收紧客户端特征时的命中面。
SANITIZE_DSH_BRAND_RE = re.compile(r"DeepSeek Harness", re.IGNORECASE)
SANITIZE_DSH_BRAND_REPLACEMENT = "Harness"
SANITIZE_DSH_ABBREV_RE = re.compile(r"\bDSH\b")
SANITIZE_DSH_ABBREV_REPLACEMENT = "the harness"
SANITIZE_DSH_WEB_LAUNCH_RE = re.compile(r"\bdsh web\b")
SANITIZE_DSH_WEB_LAUNCH_REPLACEMENT = "the web launcher"
SANITIZE_DSH_BOOT_RE = re.compile(r"window\.__DSH_BOOT__")
SANITIZE_DSH_BOOT_REPLACEMENT = "the boot payload"
SANITIZE_DSH_DEV_WATCH_RE = re.compile(r"`pnpm run dev:web`")
SANITIZE_DSH_DEV_WATCH_REPLACEMENT = "the web dev command"
# DSH 运行时上下文由客户端自己拼进 user 消息，里面也有检出路径与 GUI URL。
# 只在出现运行时上下文标记时改写，普通用户文本保持原样。
SANITIZE_DSH_CONTEXT_MARKER = "Current runtime context."
SANITIZE_DSH_SKILL_MARKER = "<available_skills>"
SANITIZE_DSH_CONTEXT_REWRITES = (
    (re.compile(r"Current DSH file policy:"), "Current file policy:"),
    (re.compile(r"the DSH file sandbox"), "the file sandbox"),
    (re.compile(r"Current DeepSeek Harness implementation checkout:"),
     "Current implementation checkout:"),
    (re.compile(r"Current DeepSeek Harness Web GUI URL:"),
     "Current Web GUI URL:"),
)
# 运行时上下文与技能清单里的其余品牌、DSH 缩写也统一改写。
# 技能清单只改名称后的描述，技能名保持原样，工具仍能按名调用。
SANITIZE_DSH_USER_IDENTIFIER_REWRITES = (
    (SANITIZE_DSH_BRAND_RE, SANITIZE_DSH_BRAND_REPLACEMENT),
    (SANITIZE_DSH_ABBREV_RE, SANITIZE_DSH_ABBREV_REPLACEMENT),
)
SANITIZE_DSH_SKILL_LINE_RE = re.compile(r"(?m)^(- `[^`]+`: )(.*)$")
# DSH 的 system prompt 里有自我介绍、GUI 描述和 @ 路径说明。上游把其中已确认的
# 连续文本当成非官方渠道的判定依据，命中即返回 HTTP 400，错误消息为 Illegal API
# invocation from an unapproved channel，与请求规模、tools、stream 无关。
# 这里只改已确认的片段，其余提示词逐字保留：第一句自我介绍换成中性身份句，
# 第二句保留模型名；GUI 描述去掉品牌词并改写 URL 从句；文件引用说明只补一个
# that。其余客户端标识统一换成中性表述，避免后续特征收紧时再次命中。
SANITIZE_DSH_SYSTEM_REWRITES = (
    (re.compile(r"You are an AI agent powered by DeepSeek Harness\b", re.IGNORECASE),
     "You are a helpful AI assistant"),
    (re.compile(r"You are a coding agent powered by the (deepseek-[0-9a-z._-]+) model\b",
                re.IGNORECASE),
     r"You are an assistant built on the \1 model"),
    (re.compile(
        r"through the DeepSeek Harness Web GUI whose URL the runtime context names\.",
        re.IGNORECASE),
     "through the GUI whose URL is named in the runtime context."),
    (SANITIZE_DSH_FILEREF_RE, SANITIZE_DSH_FILEREF_REPLACEMENT),
    (SANITIZE_DSH_BRAND_RE, SANITIZE_DSH_BRAND_REPLACEMENT),
    (SANITIZE_DSH_ABBREV_RE, SANITIZE_DSH_ABBREV_REPLACEMENT),
    (SANITIZE_DSH_WEB_LAUNCH_RE, SANITIZE_DSH_WEB_LAUNCH_REPLACEMENT),
    (SANITIZE_DSH_BOOT_RE, SANITIZE_DSH_BOOT_REPLACEMENT),
    (SANITIZE_DSH_DEV_WATCH_RE, SANITIZE_DSH_DEV_WATCH_REPLACEMENT),
)
# assistant 历史与 system 共用同一组规则。上游对 assistant 里的身份句、GUI 段、
# 文件引用段和客户端标识同样判定，分开维护会让新规则漏掉历史消息。
SANITIZE_DSH_ASSISTANT_REWRITES = SANITIZE_DSH_SYSTEM_REWRITES


def _sanitize_dsh_text(text, rewrites):
    for pattern, replacement in rewrites:
        text = pattern.sub(replacement, text)
    return text


def _sanitize_dsh_content(content, rewrites):
    if isinstance(content, str):
        return _sanitize_dsh_text(content, rewrites)
    if isinstance(content, list):
        out = []
        for part in content:
            if (isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)):
                part = dict(part)
                part["text"] = _sanitize_dsh_text(part["text"], rewrites)
            out.append(part)
        return out
    return content


def sanitize_dsh_system(content):
    """改写 system 消息里的 DSH 提示词片段，其余内容原样返回。"""
    return _sanitize_dsh_content(content, SANITIZE_DSH_SYSTEM_REWRITES)


def sanitize_dsh_assistant(content):
    """改写 assistant 历史里的 DSH 提示词片段，其余内容原样返回。"""
    return _sanitize_dsh_content(content, SANITIZE_DSH_ASSISTANT_REWRITES)


def _sanitize_dsh_skill_block(text):
    def rewrite(match):
        body = _sanitize_dsh_text(match.group(2), SANITIZE_DSH_USER_IDENTIFIER_REWRITES)
        return match.group(1) + body
    return SANITIZE_DSH_SKILL_LINE_RE.sub(rewrite, text)


def _sanitize_dsh_user_context_text(text):
    if SANITIZE_DSH_CONTEXT_MARKER in text:
        text = _sanitize_dsh_text(text, SANITIZE_DSH_CONTEXT_REWRITES)
    if SANITIZE_DSH_SKILL_MARKER in text:
        text = _sanitize_dsh_skill_block(text)
    return text


def sanitize_dsh_user_context(content):
    """改写 DSH 运行时上下文与技能描述里的客户端标识，其他用户内容原样返回。"""
    if isinstance(content, str):
        return _sanitize_dsh_user_context_text(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if (isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)):
                part = dict(part)
                part["text"] = _sanitize_dsh_user_context_text(part["text"])
            out.append(part)
        return out
    return content


def _canonical_schema(value):
    """递归按键名排序 JSON 对象，数组顺序保持不变。"""
    if isinstance(value, dict):
        return {key: _canonical_schema(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_schema(item) for item in value]
    return value


def _dsh_tool_neutralization_enabled():
    """面板开关：关闭时保留客户端的原始工具定义。

    目录取正在运行的网关：脚本入口下它是 __main__，测试与 --accounts-dir 都会
    替换它，本模块不保存副本。
    """
    try:
        module = wb_host.loaded_gateway("ACCOUNTS_DIR", "build_upstream_body")
        if module is None:
            return False
        return wb_settings.dsh_tool_neutralization(module.ACCOUNTS_DIR)
    except Exception:
        return False


def _neutralized_tool(tool):
    if not isinstance(tool, dict):
        return tool
    out = dict(tool)
    fn = out.get("function")
    if isinstance(fn, dict):
        fn = dict(fn)
        out["function"] = fn
        target = fn
    else:
        target = out
    for key in ("description", "title"):
        if isinstance(target.get(key), str):
            target[key] = _sanitize_dsh_text(
                target[key], SANITIZE_DSH_USER_IDENTIFIER_REWRITES)
    for key in ("parameters", "input_schema"):
        if isinstance(target.get(key), dict):
            target[key] = _canonical_schema(target[key])
    return out


def neutralize_dsh_tool_defs(body):
    """无损处理工具定义：描述里的 DSH 标识改写，schema 键顺序规范化。

    工具名、参数名、类型、枚举与必填项一律不动，保证调用契约不变。
    """
    tools = body.get("tools")
    if not isinstance(tools, list):
        return
    body["tools"] = [_neutralized_tool(tool) for tool in tools]


DSH_TOOL_ALIASES = {
    "ask_user_question": "ask_user",
    "create_goal": "set_goal",
    "get_goal": "read_goal",
    "update_goal": "edit_goal",
    "interrupt_agent": "stop_agent",
    "job_kill": "stop_job",
    "job_list": "list_jobs",
    "job_output": "read_job",
    "present": "show_deliverables",
    "schedule_create": "create_reminder",
    "schedule_delete": "delete_reminder",
    "schedule_list": "list_reminders",
    "schedule_update": "update_reminder",
    "send_message": "message_agent",
    "subagent": "delegate",
    "subagent_fork": "delegate_with_context",
    "todo_write": "write_todo",
    "working_directory": "cwd",
}
def _tool_function(tool):
    if not isinstance(tool, dict):
        return None
    fn = tool.get("function")
    return fn if isinstance(fn, dict) else tool


def _rewrite_tool_tokens_content(content, aliases, pattern):
    if isinstance(content, str):
        return pattern.sub(lambda m: aliases[m.group(0)], content) if aliases else content
    if isinstance(content, list):
        out = []
        for part in content:
            if (isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)):
                part = dict(part)
                part["text"] = _rewrite_tool_tokens_content(
                    part["text"], aliases, pattern)
            out.append(part)
        return out
    return content


def _rename_tool_choice(choice, aliases):
    if isinstance(choice, str):
        return aliases.get(choice, choice)
    if isinstance(choice, dict):
        choice = dict(choice)
        fn = choice.get("function")
        if isinstance(fn, dict) and fn.get("name") in aliases:
            fn = dict(fn)
            fn["name"] = aliases[fn["name"]]
            choice["function"] = fn
        elif choice.get("name") in aliases:
            choice["name"] = aliases[choice["name"]]
    return choice


DSH_PROMPT_MARKERS = (
    "DeepSeek Harness",
    "Tokens prefixed with @ are paths the user explicitly referenced",
    "Current DSH file policy",
)
DSH_REQUEST_MARKERS = ("dsh_session_log", "dsh_plugin_packages")
DSH_SIGNATURE_TOOLS = frozenset(DSH_TOOL_ALIASES)


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text") or "" for part in content
            if isinstance(part, dict) and part.get("type") == "text")
    return ""


def is_dsh_payload(payload):
    """判断请求是否来自 DSH，避免影响其他客户端的同名工具。"""
    if not isinstance(payload, dict):
        return False
    if any(marker in payload for marker in DSH_REQUEST_MARKERS):
        return True
    names = set()
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            fn = _tool_function(tool)
            name = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(name, str):
                names.add(name)
    if len(names & DSH_SIGNATURE_TOOLS) >= 3:
        return True
    for msg in payload.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "system":
            text = _content_text(msg.get("content"))
            if any(marker in text for marker in DSH_PROMPT_MARKERS):
                return True
    return False


def apply_dsh_tool_aliases(payload):
    """按本请求实际提供的工具生成别名，返回 别名 -> 原名 的映射。

    Chat、Responses 与 Anthropic Messages 路由收到 chat 请求后调用本函数；
    返回时由各自的转换器解析 chat 对象或 SSE 块，还原原始工具名。
    """
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return {}
    present = []
    for tool in tools:
        fn = _tool_function(tool)
        name = fn.get("name") if isinstance(fn, dict) else None
        if isinstance(name, str):
            present.append(name)
    active = {}
    taken = set(present)
    for name in dict.fromkeys(present):
        alias = DSH_TOOL_ALIASES.get(name)
        if alias and alias not in taken:
            active[name] = alias
            taken.add(alias)
    if not active:
        return {}
    pattern = re.compile(r"\b(?:%s)\b" % "|".join(
        sorted((re.escape(name) for name in active), key=len, reverse=True)))
    renamed = []
    for tool in tools:
        fn = _tool_function(tool)
        name = fn.get("name") if isinstance(fn, dict) else None
        alias = active.get(name)
        if alias:
            tool = dict(tool)
            fn = dict(fn)
            fn["name"] = alias
            if isinstance(tool.get("function"), dict):
                tool["function"] = fn
            else:
                tool = fn
        renamed.append(tool)
    payload["tools"] = renamed
    if "tool_choice" in payload:
        payload["tool_choice"] = _rename_tool_choice(
            payload.get("tool_choice"), active)
    messages = payload.get("messages")
    if isinstance(messages, list):
        rewritten = []
        for msg in messages:
            if isinstance(msg, dict):
                msg = dict(msg)
                calls = msg.get("tool_calls")
                if isinstance(calls, list):
                    new_calls = []
                    for call in calls:
                        if isinstance(call, dict):
                            call = dict(call)
                            fn = call.get("function")
                            if isinstance(fn, dict) and fn.get("name") in active:
                                fn = dict(fn)
                                fn["name"] = active[fn["name"]]
                                call["function"] = fn
                        new_calls.append(call)
                    msg["tool_calls"] = new_calls
                if msg.get("role") in ("system", "assistant", "user"):
                    msg["content"] = _rewrite_tool_tokens_content(
                        msg.get("content"), active, pattern)
                elif msg.get("role") in ("tool", "function"):
                    name = msg.get("name")
                    if name in active:
                        msg["name"] = active[name]
            rewritten.append(msg)
        payload["messages"] = rewritten
    return {alias: name for name, alias in active.items()}


def prepare_dsh_tool_aliases(payload):
    """按设置与 DSH 检测结果准备别名；关闭或非 DSH 时返回空映射。"""
    if not payload.get("tools") or not _dsh_tool_neutralization_enabled():
        return {}
    if not is_dsh_payload(payload):
        return {}
    payload["_dsh_request"] = True
    return apply_dsh_tool_aliases(payload)


def restore_dsh_tool_names(obj, reverse):
    """把响应里的工具别名还原成客户端原始工具名。"""
    if not reverse:
        return
    if isinstance(obj, dict):
        fn = obj.get("function")
        if isinstance(fn, dict) and fn.get("name") in reverse:
            fn["name"] = reverse[fn["name"]]
        if (obj.get("type") in ("tool_use", "function_call", "custom_tool_call")
                and obj.get("name") in reverse):
            obj["name"] = reverse[obj["name"]]
        for value in obj.values():
            restore_dsh_tool_names(value, reverse)
    elif isinstance(obj, list):
        for value in obj:
            restore_dsh_tool_names(value, reverse)


def sanitize_messages(messages):
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            item = dict(m)
            role = item.get("role")
            if role == "system" and "content" in item:
                item["content"] = sanitize_dsh_system(item["content"])
            elif role == "assistant" and "content" in item:
                item["content"] = sanitize_dsh_assistant(item["content"])
            elif role == "user" and "content" in item:
                item["content"] = sanitize_dsh_user_context(item["content"])
            if "content" in item:
                item["content"] = sanitize_content(item["content"])
            if isinstance(item.get("reasoning_content"), str):
                item["reasoning_content"] = sanitize_text(item["reasoning_content"])
            if "tool_calls" in item:
                item["tool_calls"] = sanitize_tool_calls(item["tool_calls"])
            out.append(item)
        else:
            out.append(m)
    return out


# ---------------------------------------------------------------------------
# 工具调用配对修复
# ---------------------------------------------------------------------------
def _empty_content(value):
    """assistant 消息完全没有 content 时为 True。

    None / "" / [] 都算「没有内容」：客户端发出的空值形状各不相同，而拆成相邻
    assistant 消息的一批调用对哪种形状都要合并，漏判 [] 就会触发 11148。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, list):
        return len(value) == 0
    return False


def _merge_reasoning_content(dst, src):
    """把 src 的 reasoning_content 并进 dst，两段都有时换行拼接。"""
    rc = src.get("reasoning_content")
    if not isinstance(rc, str) or not rc:
        return
    prev = dst.get("reasoning_content")
    if isinstance(prev, str) and prev:
        dst["reasoning_content"] = prev + "\n" + rc
    else:
        dst["reasoning_content"] = rc


def merge_adjacent_tool_calls(messages):
    """把前后相邻的 assistant tool_calls 消息合并成一条。

    有些兼容 OpenAI 的 Agent 客户端把一批并行调用重放成几条相邻的 assistant 消息，
    每条只带一个 tool_call。DeepSeek 系上游对这种形状回 400 / code 11148
    （tool_call_sequence_broken）并终止会话：每次重试都重放同一段历史，换账号也
    救不回来。合并声明即可恢复成上游接受的形状。

    条件从严，不凭空造语义：消息必须相邻；后一条的 content 必须为空（None/""/[]）；
    前一条必须已经是带 tool_calls 的 assistant。

    第二种形式把普通 assistant 的字符串 content 并入前面那条没有 content 的
    tool_calls assistant（同一批历史被拆开、顺序相反）。两种情况都保留
    reasoning_content，DeepSeek 多轮思考要求带回去。
    """
    if not isinstance(messages, list) or len(messages) < 2:
        return messages, False
    out = []
    changed = False
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        if m.get("role") == "assistant" and out:
            prev = out[-1] if isinstance(out[-1], dict) else None
            if prev is not None and prev.get("role") == "assistant":
                tcs = m.get("tool_calls")
                prev_calls = prev.get("tool_calls")
                if (isinstance(tcs, list) and tcs
                        and _empty_content(m.get("content"))
                        and isinstance(prev_calls, list) and prev_calls):
                    prev["tool_calls"] = prev_calls + tcs
                    _merge_reasoning_content(prev, m)
                    changed = True
                    continue
                if ("tool_calls" not in m
                        and isinstance(m.get("content"), str) and m["content"]
                        and isinstance(prev_calls, list) and prev_calls
                        and _empty_content(prev.get("content"))):
                    prev["content"] = m["content"]
                    _merge_reasoning_content(prev, m)
                    changed = True
                    continue
        out.append(m)
    if not changed:
        return messages, False
    return out, True


def repack_tool_result_blocks(messages):
    """让一批 tool_calls 与其结果保持相邻。

    上游要求 role:"tool" 的结果紧跟发起它们的 assistant 消息，中间不能插东西。
    例如 Codex 的 image_resize_notice 会作为 developer 消息紧跟在某条工具输出之后；
    有并行调用时它正好落在两个结果中间，配对看起来就断了，上游据此拒掉整个请求
    （code 11148）并终止会话。这里只重排：结果不变、相对顺序不变，插进来的消息挪到
    这批结果之后。
    """
    if not isinstance(messages, list) or len(messages) < 3:
        return messages, False
    out = []
    changed = False
    i = 0
    while i < len(messages):
        m = messages[i]
        if not isinstance(m, dict) or m.get("role") != "assistant":
            out.append(m)
            i += 1
            continue
        calls = m.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            out.append(m)
            i += 1
            continue
        want = set()
        for tc in calls:
            if isinstance(tc, dict):
                tid = tc.get("id")
                if isinstance(tid, str) and tid:
                    want.add(tid)
        out.append(m)
        i += 1
        results = []
        between = []
        saw_non_tool = False
        while i < len(messages):
            mm = messages[i]
            if not isinstance(mm, dict):
                break
            role = mm.get("role")
            if role == "tool":
                tid = mm.get("tool_call_id")
                if not (isinstance(tid, str) and tid in want):
                    break
                results.append(mm)
                if saw_non_tool:
                    changed = True
                i += 1
                continue
            if not results:
                break
            # 后随的 assistant.tool_calls 开启下一批，回到外层循环处理，
            # 否则它自己的结果不会被重排。
            if role == "assistant" and isinstance(mm.get("tool_calls"), list) \
                    and mm["tool_calls"]:
                break
            between.append(mm)
            saw_non_tool = True
            i += 1
        out.extend(results)
        out.extend(between)
    if not changed:
        return messages, False
    return out, True


def cleanup_orphan_tool_calls(messages):
    """丢掉没有结果的工具调用，以及没有调用的结果。

    工具调用失败（参数错、超时、未知工具）时，客户端会留下一条永远无法回应的
    assistant tool_calls：结果消息不会写出来，这条记录却跟着历史进入之后的每一轮，
    上游逐轮拒绝（code 11148），一次失败的调用就能废掉整个会话。两侧按同一组 id
    裁剪，修完不会留下半配对。
    """
    if not isinstance(messages, list) or not messages:
        return messages, False
    call_ids = set()
    result_ids = set()
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "tool":
            tid = m.get("tool_call_id")
            if isinstance(tid, str) and tid:
                result_ids.add(tid)
        elif role == "assistant":
            calls = m.get("tool_calls")
            if isinstance(calls, list):
                for tc in calls:
                    if isinstance(tc, dict):
                        tid = tc.get("id")
                        if isinstance(tid, str) and tid:
                            call_ids.add(tid)
    if not call_ids and not result_ids:
        return messages, False
    keep = call_ids & result_ids
    changed = False
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        calls = m.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue
        kept = [tc for tc in calls
                if isinstance(tc, dict) and isinstance(tc.get("id"), str)
                and tc["id"] in keep]
        if len(kept) == len(calls):
            continue
        changed = True
        if kept:
            m["tool_calls"] = kept
        else:
            m.pop("tool_calls", None)
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if not (isinstance(tid, str) and tid in keep):
                changed = True
                continue
        out.append(m)
    if not changed:
        return messages, False
    return out, True


def is_truncated_arguments(raw):
    """非空且不是合法 JSON 的工具参数字符串才算截断。

    被 max_tokens 或断连截断的流，最后一条工具调用的 JSON 只写了一半。空串或纯空白
    是合法的无参工具；任何能解析的 JSON（含 null、标量、数组）都是模型自己的输出，
    交给客户端校验——只有非空且解析不了的字符串算截断。
    """
    if not isinstance(raw, str):
        return False
    trimmed = raw.strip()
    if not trimmed:
        return False
    try:
        json.loads(trimmed)
        return False
    except Exception:
        return True


def drop_truncated_tool_calls(calls):
    """返回参数不是半截 JSON 的工具调用。"""
    if not isinstance(calls, list):
        return calls
    kept = []
    for call in calls:
        if not isinstance(call, dict):
            kept.append(call)
            continue
        fn = call.get("function")
        if isinstance(fn, dict) and is_truncated_arguments(fn.get("arguments")):
            continue
        kept.append(call)
    return kept


# ---------------------------------------------------------------------------
# DeepSeek 多轮一致性：reasoning_content 回填
# ---------------------------------------------------------------------------
# 上游（code 11155）要求思考模式开启时每条 assistant 消息都带 reasoning_content，
# 两个条件各管一半：
#   - thinkingEnabled：deepseek 且思考开启 -> 一律回填，第三方客户端把 reasoning
#     整段丢掉时也要补，否则零轨迹的历史原样转发会被拒；
#   - hasTrace：已有 reasoning 轨迹 -> 不看思考开关，一律回填。
# 上游还校验 len(reasoning) > 0，空占位不够，`reasoning` 要给非空值。
def backfill_reasoning_content(messages, model, thinking_enabled=None):
    if not model or not str(model).lower().startswith("deepseek"):
        return messages
    if thinking_enabled is None:
        thinking_enabled = False
    has_trace = False
    for m in messages:
        if not isinstance(m, dict):
            continue
        reasoning = m.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            has_trace = True
            break
        if "reasoning_content" in m:
            has_trace = True
            break
    if not thinking_enabled and not has_trace:
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            item = dict(m)
            rc = item.get("reasoning_content")
            if not isinstance(rc, str):
                # 非字符串（null、数字、缺失）都算缺失，与官方 typeof 判定一致。
                legacy = item.get("reasoning")
                rc = legacy if isinstance(legacy, str) else ""
                item["reasoning_content"] = rc
            # reasoning 字段镜像一份非空值：上游拒绝空值或缺失，空白占位能过
            # 长度检查，且不带任何模型可见的语义。
            existing = item.get("reasoning")
            if not (isinstance(existing, str) and existing):
                item["reasoning"] = rc if rc else " "
            out.append(item)
        else:
            out.append(m)
    return out
# ---------------------------------------------------------------------------
# 工具与 tool_choice 归一化：对象形状 tool_choice 触发 11101
# ---------------------------------------------------------------------------
def normalize_tool_choice(obj):
    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        val = tc.strip().lower()
        if val == "none":
            # 保留 tools 声明。删掉工具声明会让 Agent 进入死循环：模型拿不到
            # 函数签名又没有工具通道，仍被要求完成任务，于是把调用打印成
            # DSML 或伪 JSON 塞进 content，tool_calls 为空、finish_reason 为
            # stop，客户端解析不到只能追问，上下文每轮加两条消息直到撑爆窗口。
            # tool_choice="none" 的语义是本轮不许调用工具，由该字段表达即可，
            # 不必抹掉能力声明。上游把 tool_choice 定为 string，发对象会 11101，
            # 故字符串原样透传。本上游不遵守 "none"，保留 tools 时仍返回
            # tool_calls，调用走 tool_calls 通道，客户端能执行与回填；要禁止
            # 调用就不要传 tools。
            obj["tool_choice"] = "none"
        return
    if isinstance(tc, dict):
        typ = (tc.get("type") or "").strip().lower()
        if typ == "none":
            # 同上：保留 tools 声明。上游只认字符串，对象形式必须降级成
            # "none"，否则 11101。
            obj["tool_choice"] = "none"
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            name = (tc.get("function") or {}).get("name") or tc.get("name") or ""
            obj["tool_choice"] = name.strip() or "auto"
        else:
            obj.pop("tool_choice", None)
    else:
        obj.pop("tool_choice", None)
_SCHEMA_TYPE_MAP = {
    "str": "string",
    "text": "string",
    "int": "integer",
    "bool": "boolean",
    "dict": "object",
    "list": "array",
    "float": "number",
}
_SCHEMA_VALID_TYPES = {"string", "number", "integer", "boolean", "array", "object", "null"}


def _deep_normalize_schema(schema):
    """递归规整工具参数 JSON Schema，避免上游 Go 校验器回 11129。

    处理嵌套对象、数组、definitions、anyOf/oneOf/allOf，以及客户端常见的
    Python 类型别名与 null 元数据。
    """
    if not isinstance(schema, dict):
        return schema
    for key in list(schema.keys()):
        if schema[key] is None and key in (
                "description", "title", "format", "pattern", "items", "default"):
            schema.pop(key, None)
    if "type" in schema:
        typ = schema["type"]
        if isinstance(typ, str):
            lower = typ.strip().lower()
            if lower in _SCHEMA_TYPE_MAP:
                schema["type"] = _SCHEMA_TYPE_MAP[lower]
            elif lower not in _SCHEMA_VALID_TYPES:
                if "properties" in schema:
                    schema["type"] = "object"
                elif "items" in schema:
                    schema["type"] = "array"
                else:
                    schema.pop("type", None)
            else:
                schema["type"] = lower
        elif isinstance(typ, list):
            schema["type"] = [
                _SCHEMA_TYPE_MAP.get(str(item).lower(), str(item).lower())
                for item in typ if item]
    if schema.get("type") == "object" or ("properties" in schema and schema.get("type") is None):
        if schema.get("type") is None:
            schema["type"] = "object"
        if not isinstance(schema.get("properties"), dict):
            schema["properties"] = {}
    if "required" in schema:
        required = schema["required"]
        if isinstance(required, list):
            cleaned = [str(item) for item in required
                       if item is not None and str(item).strip()]
            if cleaned:
                schema["required"] = cleaned
            else:
                schema.pop("required", None)
        elif isinstance(required, str) and required.strip():
            schema["required"] = [required.strip()]
        else:
            schema.pop("required", None)
    if "items" in schema:
        items = schema["items"]
        if isinstance(items, list):
            first = next((item for item in items if isinstance(item, dict)), None)
            schema["items"] = (_deep_normalize_schema(dict(first))
                               if first is not None else {"type": "string"})
        elif isinstance(items, dict):
            _deep_normalize_schema(items)
        elif items is None:
            schema.pop("items", None)
    if isinstance(schema.get("properties"), dict):
        for value in list(schema["properties"].values()):
            if isinstance(value, dict):
                _deep_normalize_schema(value)
    for key in ("$defs", "definitions"):
        if isinstance(schema.get(key), dict):
            for value in list(schema[key].values()):
                if isinstance(value, dict):
                    _deep_normalize_schema(value)
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(schema.get(key), list):
            for value in schema[key]:
                if isinstance(value, dict):
                    _deep_normalize_schema(value)
    return schema


def _normalize_tool_parameters(fn):
    """把函数参数规整成上游要求的对象 schema；函数参数不是对象时重置为对象。"""
    params = fn.get("parameters")
    if not isinstance(params, dict):
        params = {}
        fn["parameters"] = params
    if params.get("type") != "object":
        params["type"] = "object"
    if not isinstance(params.get("properties"), dict):
        params["properties"] = {}
    _deep_normalize_schema(params)
    fn["parameters"] = params


def normalize_tools(obj):
    # 旧客户端可能仍发根级 functions，先折叠成 tools。
    if "functions" in obj and "tools" not in obj:
        funcs = obj.pop("functions")
        if isinstance(funcs, list):
            obj["tools"] = [{"type": "function", "function": item}
                            for item in funcs if isinstance(item, dict)]
    tools = obj.get("tools")
    if not tools or not isinstance(tools, list):
        if "tools" in obj:
            obj.pop("tools", None)
        return
    norm = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        # 顶层 name 定义的简化工具：补成 Chat Completions 的 function schema。
        # 只处理没有类型或明确是 function 的条目；web_search、custom 等服务端
        # 工具必须原样保留，否则会改变客户端自己的工具声明。
        if ("name" in tool and "function" not in tool
                and tool.get("type") in (None, "", "function")):
            fn = {
                "name": tool.get("name") or "",
                "description": tool.get("description") or "",
                "parameters": tool.get("parameters") or {},
            }
            if "strict" in tool:
                fn["strict"] = tool["strict"]
            _normalize_tool_parameters(fn)
            norm.append({"type": "function", "function": fn})
            continue
        item = dict(tool)
        if item.get("type") in (None, ""):
            item["type"] = "function"
        if isinstance(item.get("function"), dict):
            fn = dict(item["function"])
            _normalize_tool_parameters(fn)
            item["function"] = fn
        norm.append(item)
    if norm:
        obj["tools"] = norm
    else:
        obj.pop("tools", None)


def normalize_roles(messages):
    """把上游不认的 role 名映射成它认的。

    WorkBuddy 只认 system / user / assistant / tool。OpenAI 新增的 "developer"
    role（Codex CLI 与新版 SDK 在用）等同 "system"，原样发过去会报 code 11-128。
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        item = m
        if m.get("role") == "developer":
            item = dict(m)
            item["role"] = "system"
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# stop 归一化：字符串 stop 触发 11101
# ---------------------------------------------------------------------------
def normalize_stop(obj):
    # OpenAI 把 stop 声明成 string | array，客户端传单个字符串完全合法；本上游的
    # Go 端却把该字段定死为 []string，收到字符串直接 400：
    #   Unmarshal chat params failed with error: json: cannot unmarshal string
    #   into Go struct field Request.stop of type []string   (code 11101)
    # 实测（deepseek-v4.1-flash，同一条 prompt，同一个上游账号）：
    #   "stop": "END"     -> 400 / code 11101
    #   "stop": ["END"]   -> 200
    # 归一化成数组后语义完全等价：OpenAI 里单字符串就是单元素数组，模型侧看到的
    # 停止序列一模一样。空字符串 / 空数组 / null / 非法类型一律删字段——它们的实际
    # 含义都是「没有停止序列」，而空值本身在上游同样会被判参数不合法。
    if "stop" not in obj:
        return
    val = obj["stop"]
    if isinstance(val, str):
        val = [val] if val else []
    elif isinstance(val, (list, tuple)):
        val = [s for s in val if isinstance(s, str) and s]
    else:
        val = []
    if val:
        obj["stop"] = val
    else:
        obj.pop("stop", None)
# ---------------------------------------------------------------------------
# DeepSeek DSML 工具调用兜底解析
# ---------------------------------------------------------------------------
TAG_START = r"<[^>]*DSML[^>]*"
DSML_CALLS_RE = re.compile(TAG_START + r"calls>(.*?)</[^>]*DSML[^>]*calls>", re.DOTALL)
DSML_INVOKE_RE = re.compile(TAG_START + r"invoke\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27]>(.*?)</[^>]*invoke>", re.DOTALL)
DSML_PARAM_RE = re.compile(TAG_START + r"parameter\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27][^>]*>(.*?)</[^>]*parameter>", re.DOTALL)
def parse_dsml_tool_calls(text):
    if not text or "DSML" not in text:
        return None, text
    match = DSML_CALLS_RE.search(text)
    if not match:
        return None, text
    calls_block = match.group(1)
    tool_calls = []
    for inv_match in DSML_INVOKE_RE.finditer(calls_block):
        func_name = inv_match.group(1)
        params_block = inv_match.group(2)
        params = {}
        for p_match in DSML_PARAM_RE.finditer(params_block):
            p_name = p_match.group(1)
            p_val = p_match.group(2).strip()
            params[p_name] = p_val
        tool_calls.append({
            "id": wb_ids.new_id("call_"),
            "name": func_name,
            "arguments": json.dumps(params, ensure_ascii=False),
        })
    clean = (text[:match.start()].strip() + " " + text[match.end():].strip()).strip()
    return tool_calls, clean
# ---- Chat Completions 路径上的「打印出来的工具调用」 -------------------------
#
# 模型把调用打印成文本而不是发结构化 tool_calls 时，客户端只看到纯文本、找不到调用，
# 这一轮就此结束：标记留在记录里，什么也没执行。线上有两种拼法：
#   1. 全角 DSML 形式：<||DSML|| calls><||DSML|| invoke name="run_code"> ... </||DSML|| invoke>
#   2. 混合体：普通开标签配 DSML 闭标签，第一个参数的闭标签整个被吞掉，形如
#      <run_code><parameter name="code">...</parameter>
#      <parameter name="description">...</|DSML| parameter>
#      </run_code></invoke></|DSML| parameter></|DSML| invoke></|DSML| calls>
#
# 上面的 parse_dsml_tool_calls() 只认 (1)：它要求 calls 块有 DSML 开标签，(2) 没有。
# 下面的解析器改由 <parameter> 元素驱动：两种拼法里写法相同，参数名又是一次调用不能缺的东西。
LEAK_PARAM_RE = re.compile(
    r"<\s*(?:\|\s*DSML\s*\|\s*)?(?P<tag>parameter|param|arg|argument)\b"
    r"(?P<attrs>[^>]*)>"
    r"(?P<value>.*?)"
    r"</\s*(?:\|\s*DSML\s*\|\s*)?(?:parameter|param|arg|argument)\s*>",
    re.DOTALL | re.IGNORECASE,
)
LEAK_TAG_RE = re.compile(
    r"<\s*/?\s*(?:\|\s*DSML\s*\|\s*)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<attrs>[^>]*)>"
)
LEAK_NAME_ATTR_RE = re.compile(r"""name\s*=\s*["']([^"']*)["']""")
LEAK_WRAPPER_TAGS = {"calls", "tool_calls", "function_calls", "function_call"}
LEAK_PARAM_TAGS = {"parameter", "param", "arg", "argument"}
# 流过滤器必须扣住文本的标签开头字符序列。
LEAK_TAG_PREFIXES = (
    "<run_code", "<run_shell", "<invoke", "<parameter", "<param", "<arg",
    "<argument", "<calls", "<tool_calls", "<function_calls", "<function_call",
    "<think", "<thinking",
    "</run_code", "</run_shell", "</invoke", "</parameter", "</param",
    "</arg", "</argument", "</calls", "</tool_calls", "</function_calls",
    "</function_call", "</think", "</thinking",
    "<|dsml|", "</|dsml|",
)


def _normalize_dsml_bars(text):
    """把 DSML 标记的全角竖线写法折回 ASCII 竖线。"""
    bar = chr(0xff5c)
    return re.sub(bar + r"+\s*DSML\s*" + bar + r"+", "|DSML|", text)


def _strip_spans(text, spans):
    """删掉每个 [start, end) 区间，再收掉留下的空白。"""
    if not spans:
        return text
    kept = []
    last = 0
    for start, end in sorted(spans):
        if start < last:
            continue
        kept.append(text[last:start])
        last = end
    kept.append(text[last:])
    return "".join(kept).strip()


def parse_leaked_tool_calls(text):
    """把模型打印出来的工具调用还原出来。

    返回 (calls, clean_text)，calls 的形状与 parse_dsml_tool_calls() 一致；什么都没
    认出来时返回 (None, text)。
    """
    if not text or "<" not in text:
        return None, text
    # 先折全角写法，两种拼法共用同一趟扫描。
    original = text
    text = _normalize_dsml_bars(text)
    found = []
    for m in LEAK_PARAM_RE.finditer(text):
        attr = LEAK_NAME_ATTR_RE.search(m.group("attrs") or "")
        if not attr or not attr.group(1):
            continue
        found.append((m.start(), attr.group(1), m.group("value").strip(), m.span()))
    if not found:
        return None, original
    tags = list(LEAK_TAG_RE.finditer(text))
    calls = []
    for start, pname, value, span in found:
        owner = None
        for tag in tags:
            if tag.start() >= start:
                break
            if tag.group(0).lstrip().startswith("</"):
                continue
            tname = tag.group("name").lower()
            if tname in LEAK_WRAPPER_TAGS or tname in LEAK_PARAM_TAGS:
                continue
            owner = tag
        name = ""
        if owner is not None:
            attr = LEAK_NAME_ATTR_RE.search(owner.group("attrs") or "")
            name = (attr.group(1) if attr else owner.group("name")).strip()
        if not name:
            continue
        key = owner.start() if owner is not None else None
        if calls and calls[-1]["open"] == key:
            calls[-1]["params"][pname] = value
            calls[-1]["spans"].append(span)
        else:
            calls.append({"name": name, "params": {pname: value},
                          "open": key, "spans": [span]})
    if not calls:
        return None, original
    # 整个区间从开标签到最后一个参数都是标记而非正文，一次性删掉。
    regions = []
    for call in calls:
        first = call["open"]
        if first is None:
            first = min(s for s, _e in call["spans"])
        last = max(e for _s, e in call["spans"])
        # 把跟在最后一个参数后面的闭标签一并吞进区间。
        while True:
            nxt = last
            while nxt < len(text) and text[nxt] in " \t\r\n":
                nxt += 1
            m = LEAK_TAG_RE.match(text, nxt)
            if not m or not m.group(0).lstrip().startswith("</"):
                break
            last = m.end()
        regions.append((first, last))
    clean = _strip_spans(text, regions)
    # 区间清理没覆盖到的包裹标签与散落标签也是标记，带 DSML 字样的标签一并删掉。
    leftover = []
    for m in LEAK_TAG_RE.finditer(clean):
        if "DSML" in _normalize_dsml_bars(m.group(0)).upper():
            leftover.append(m.span())
        elif m.group("name").lower() in LEAK_WRAPPER_TAGS:
            leftover.append(m.span())
    clean = _strip_spans(clean, leftover)
    return [{
        "id": wb_ids.new_id("call_"),
        "name": call["name"],
        "arguments": json.dumps(call["params"], ensure_ascii=False),
    } for call in calls], clean


def repair_leaked_tool_calls(message):
    """把打印出来的调用从 `content` 挪进 `tool_calls`。

    只在消息没有结构化调用时动手：真的 tool_calls 永远优先，模型两样都发时保持原样。
    消息被改过时返回 True。
    """
    if not isinstance(message, dict):
        return False
    existing = message.get("tool_calls")
    if isinstance(existing, list) and existing:
        return False
    content = message.get("content")
    if not isinstance(content, str) or "<" not in content:
        return False
    calls, clean = parse_leaked_tool_calls(content)
    if not calls:
        calls, clean = parse_dsml_tool_calls(content)
    if not calls:
        return False
    message["content"] = clean
    message["tool_calls"] = [{
        "id": call["id"],
        "type": "function",
        "function": {"name": call["name"], "arguments": call["arguments"]},
    } for call in calls]
    return True


def could_be_leaked_call(tail):
    """tail 以 "<" 开头、还可能长成打印出来的工具标记时返回 True。"""
    low = _normalize_dsml_bars(tail).lower()
    m = LEAK_TAG_RE.match(low)
    if m and any(m.group(0).startswith(p) for p in LEAK_TAG_PREFIXES):
        return True
    if len(low) > 96 or ">" in low:
        return False
    return any(p.startswith(low) or low.startswith(p) for p in LEAK_TAG_PREFIXES)


class LeakedToolCallStreamFilter:
    """扣住可能是「打印出来的工具调用」的 assistant 文本。

    /v1/chat/completions 收到文本就转发，模型打印的调用会在流结束、还来不及解析时
    就已经发出去。因此先缓存文本：一旦明显是散文就放行，流结束时把剩下的部分转换。

    feed(text) 返回由 ("text", str) 与 ("calls", [call, ...]) 组成的列表。
    """

    MAX_HOLD = 262144

    def __init__(self):
        self.buf = ""

    def _release_all(self):
        out, self.buf = self.buf, ""
        return [("text", out)] if out else []

    def feed(self, text):
        if not text:
            return []
        self.buf += text
        for idx in range(len(self.buf)):
            if self.buf[idx] != "<":
                continue
            if not could_be_leaked_call(self.buf[idx:]):
                continue
            if idx:
                head, self.buf = self.buf[:idx], self.buf[idx:]
                return [("text", head)]
            break
        else:
            return self._release_all()
        if len(self.buf) > self.MAX_HOLD:
            return self._release_all()
        return []

    def flush(self):
        if not self.buf:
            return []
        buf, self.buf = self.buf, ""
        calls, clean = parse_leaked_tool_calls(buf)
        if not calls:
            calls, clean = parse_dsml_tool_calls(buf)
        if not calls:
            return [("text", buf)]
        out = []
        if clean:
            out.append(("text", clean))
        out.append(("calls", calls))
        return out


def chat_chunk(model, delta, finish=None, chunk_id="chatcmpl-wb"):
    """一条 Chat Completions 流式分块。"""
    return json.dumps({
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }, ensure_ascii=False)


def chat_text_chunk(model, text, chunk_id="chatcmpl-wb"):
    return chat_chunk(model, {"content": text}, None, chunk_id)


def chat_tool_call_chunks(model, calls, chunk_id="chatcmpl-wb"):
    """把恢复出的调用以结构化 tool_calls 分块交出去。"""
    out = []
    for i, call in enumerate(calls):
        out.append(chat_chunk(model, {"tool_calls": [{
            "index": i,
            "id": call["id"],
            "type": "function",
            "function": {"name": call["name"], "arguments": call["arguments"]},
        }]}, None, chunk_id))
    return out


def chat_finish_chunk(model, reason, chunk_id="chatcmpl-wb"):
    return chat_chunk(model, {}, reason, chunk_id)


def translate_max_completion_tokens(obj):
    alias = obj.pop("max_completion_tokens", None)
    if alias is None:
        return
    if "max_tokens" in obj:
        return
    try:
        val = int(alias)
        if val > 0:
            obj["max_tokens"] = val
    except (TypeError, ValueError):
        pass
