"""改写本地 AI 客户端的配置，让请求指向本网关。

每个动过的文件留逐字节备份，可一次还原（list / apply / restore 子命令）；客户端
不在本机时可当独立配置器运行，用法见 --help。只依赖标准库；YAML/TOML 是保守的
文本级写入器，不动不属于自己的行，手写注释与顺序因此保留。
"""
import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

PROVIDER_ID = "wb-proxy"
PROVIDER_NAME = "WB Proxy"
CREDENTIAL_REF = "WB_PROXY_API_KEY"

# 超过这个大小的文件基本不可能是手改的配置文件，直接拒绝，避免破坏用户数据。
MAX_CONFIG_BYTES = 8 * 1024 * 1024
# 每个文件保留的 backup 数量，每次 apply 后清理更旧的。
BACKUP_KEEP = 10

STATE_FILE = "integration-state.json"
BACKUP_DIRNAME = "agent-backups"


class AgentConfigError(Exception):
    """探测、接入或还原客户端时的可读错误。"""


# ---------------------------------------------------------------------------
# 小型文件工具
# ---------------------------------------------------------------------------

def _resolve_home(home=None):
    """返回 home 目录基路径；可注入，测试保持封闭。"""
    if home:
        return os.path.abspath(home)
    return os.path.expanduser("~")


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) > MAX_CONFIG_BYTES:
        raise AgentConfigError(
            "refusing to edit %s: file is %d bytes (limit %d)"
            % (path, len(data), MAX_CONFIG_BYTES))
    return data


def _read_text(path):
    try:
        data = _read_bytes(path)
    except FileNotFoundError:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentConfigError("%s is not valid UTF-8: %s" % (path, exc))


# 本模块写出的每个文件都带网关 API key（内联，或经同目录 credentials 文件解析的环境
# 变量引用），所以新文件一律按仅属主权限创建，不走 open() 的 0666 & ~umask（常见
# umask 022 下得到 0644，会暴露其他客户端的 key；DSH 在 credentials 可被属主以外
# 读取时拒绝启动）。
WRITE_MODE = 0o600


def _target_mode(path):
    """下次写入 `path` 时采用的权限。新文件或权限比仅属主更宽（或属主自己都读不回）
    的文件用 `WRITE_MODE`，其余保留文件自身权限——重复接入不把用户手改的更严权限
    放宽回去（0600 -> 0600、0400 -> 0400、0644 -> 0600）。
    """
    try:
        current = os.stat(path).st_mode & 0o777
    except OSError:
        return WRITE_MODE
    if current & 0o077 or not current & 0o400:
        return WRITE_MODE
    return current


def _atomic_write(path, data):
    """tmp + os.replace 原子写入，崩溃不会留下写了一半的配置。临时文件按仅属主权限
    创建（见 `_target_mode`）；途中任何失败（含 chmod 与 replace）都会删掉临时文件，
    重试不会被写不进去的残留文件卡住。
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    mode = _target_mode(path)
    try:
        os.unlink(tmp)  # 被杀进程留下的残留 tmp 可能不可写
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, WRITE_MODE)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path, text):
    _atomic_write(path, text.encode("utf-8"))


def _atomic_write_bytes(path, data):
    _atomic_write(path, data)


def _prune_empty_parents(path, home=None):
    """清掉本模块写配置留下的空目录。只删空目录，且上溯到 home 目录即停：home 之上
    用户自己的目录树（哪怕为空）一律不动，仍有文件的目录绝不删除。
    """
    home = os.path.abspath(home or _resolve_home())
    directory = os.path.dirname(os.path.abspath(path))
    while True:
        if not directory or os.path.abspath(directory) == home:
            return
        if not os.path.abspath(directory).startswith(home + os.sep):
            return
        try:
            os.rmdir(directory)
        except OSError:
            return
        directory = os.path.dirname(directory)


def _load_json(path, what):
    """解析 JSON 配置文件；文件缺失视作空文档。"""
    text = _read_text(path)
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AgentConfigError("%s is not valid JSON (%s): %s" % (what, path, exc))
    if not isinstance(data, dict):
        raise AgentConfigError("%s must be a JSON object: %s" % (what, path))
    return data


def _dump_json(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def deep_merge(base, patch):
    """递归合并 patch 到 base，patch 之外的同级键保留。返回新 dict，不改动入参。
    """
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# 文本级 YAML 编辑器（仅支持子集，只用标准库）
# ---------------------------------------------------------------------------

def _yaml_indent(line):
    return len(line) - len(line.lstrip(" "))


def _yaml_key(line):
    """块式 `key: ...` 行的映射键，不匹配时返回 None。冒号后的 inline 值与注释忽略；
    非 `word: value` 形式的行（列表项、带引号的键）不在支持范围内。
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("-"):
        return None
    m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?:\s|$)", line.lstrip(" "))
    if not m:
        return None
    return m.group(1)


def _yaml_block_end(lines, start, indent):
    """`start` 行所开块的结束下标（块后第一行）：块覆盖其后所有缩进深于 `indent`
    的非空、非注释行。
    """
    idx = start + 1
    while idx < len(lines):
        line = lines[idx]
        if line.strip() and _yaml_indent(line) <= indent:
            break
        idx += 1
    return idx


def _yaml_insert_point(lines, start, end, indent):
    """[start, end) 内最后一个内容行的插入点，让属于*下一*节的空行与注释
    尾随行留在原处。"""
    idx = end
    while idx > start and not lines[idx - 1].strip():
        idx -= 1
    return idx if idx > start else end


def _yaml_inline_to_block(lines, idx, key, inline_value):
    """把 `key: <inline>` 展开成 `key:` 加一层嵌套块行；只有单行 flow 映射
    `{k: v, ...}` 能在该键下展开，其余结构不在支持范围内。
    """
    indent = _yaml_indent(lines[idx])
    value = inline_value.strip()
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        out = [" " * indent + key + ":"]
        if inner:
            for part in inner.split(","):
                part = part.strip()
                if part:
                    out.append(" " * (indent + 2) + part)
        lines[idx:idx + 1] = out
        return
    if value == "":
        return
    raise AgentConfigError(
        "cannot nest keys under `%s: %s` - only an inline {..} mapping "
        "can be expanded" % (key, inline_value))


def yaml_upsert_inline(text, key_path, value):
    """写入嵌套映射键，值用 inline-flow（JSON 兼容）形式。`key_path` 是点分路径，叶子
    按所在缩进写成 `wb-proxy: <json value>`。被碰到的那块之外的行逐字节保留；路径上
    缺失的键会新建，父键当前是 inline 映射时先展开成块；超出支持范围的结构抛
    AgentConfigError。
    """
    keys = [part.strip() for part in str(key_path).split(".") if part.strip()]
    if not keys:
        raise AgentConfigError("yaml_upsert_inline: empty key path")
    lines = text.split("\n")
    lines = _yaml_upsert_at(lines, 0, len(lines), 0, keys,
                            json.dumps(value, ensure_ascii=False))
    return "\n".join(lines)


def _yaml_upsert_at(lines, start, end, indent, keys, value_json):
    """yaml_upsert_inline 的递归块扫描；返回新的行列表。"""
    key = keys[0]
    idx = start
    while idx < end:
        line = lines[idx]
        if line.strip() and not line.strip().startswith("#") \
                and _yaml_indent(line) == indent and _yaml_key(line) == key:
            after = line.split(":", 1)[1].strip()
            child_end = _yaml_block_end(lines, idx, indent)
            if len(keys) == 1:
                lines[idx] = " " * indent + key + ": " + value_json
                return lines
            if after:
                _yaml_inline_to_block(lines, idx, key, after)
                child_end = _yaml_block_end(lines, idx, indent)
                lines = _yaml_upsert_at(lines, idx + 1, child_end,
                                        indent + 2, keys[1:], value_json)
                return lines
            lines = _yaml_upsert_at(lines, idx + 1, child_end,
                                    indent + 2, keys[1:], value_json)
            return lines
        idx += 1
    insert = _yaml_insert_point(lines, start, end, indent)
    if len(keys) == 1:
        new_lines = [" " * indent + key + ": " + value_json]
    else:
        new_lines = [" " * indent + keys[0] + ":"]
        new_lines.extend(
            _yaml_upsert_at([], 0, 0, indent + 2, keys[1:], value_json))
    lines[insert:insert] = new_lines
    return lines


def yaml_refs_upsert(text, key, value):
    """在顶层 `refs:` 块内新增或更新 `key: value`，文档没有该块时在末尾补一个。
    `refs:` 行已带 inline 值（如 `refs: {}`）不在支持范围内，抛 AgentConfigError，
    避免静默写坏。
    """
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.strip() and not line.strip().startswith("#") \
                and _yaml_indent(line) == 0 and _yaml_key(line) == "refs":
            after = line.split(":", 1)[1].strip()
            if after:
                raise AgentConfigError(
                    "cannot upsert into `refs: %s` - only a block-style "
                    "refs: mapping is supported" % after)
            end = _yaml_block_end(lines, idx, 0)
            j = idx + 1
            while j < end:
                if lines[j].strip() and _yaml_indent(lines[j]) == 2 \
                        and _yaml_key(lines[j]) == key:
                    lines[j] = "  %s: %s" % (key, value)
                    return "\n".join(lines)
                j += 1
            insert = _yaml_insert_point(lines, idx + 1, end, 2)
            lines[insert:insert] = ["  %s: %s" % (key, value)]
            return "\n".join(lines)
        idx += 1
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append("refs:")
    lines.append("  %s: %s" % (key, value))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 文本级 TOML 编辑器（仅支持子集，只用标准库）
# ---------------------------------------------------------------------------

_TOML_TABLE_RE = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?$")
_TOML_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def _toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        raise AgentConfigError("TOML has no null value")
    return json.dumps(value, ensure_ascii=False)


def toml_upsert_top_key(text, key, value):
    """新增或更新顶层 TOML 键（必须位于第一个 [table] 之前）。已有的赋值原地替换；
    缺失的插到最后一个顶层赋值之后，不惊动任何表体。
    """
    rendered = "%s = %s" % (key, _toml_value(value))
    lines = text.split("\n")
    idx = 0
    last_top = -1
    while idx < len(lines):
        line = lines[idx]
        if _TOML_TABLE_RE.match(line):
            break
        m = _TOML_KEY_RE.match(line)
        if m:
            last_top = idx
            if m.group(1) == key:
                lines[idx] = rendered
                return "\n".join(lines)
        idx += 1
    lines.insert(last_top + 1, rendered)
    return "\n".join(lines)


def toml_upsert_table(text, table, mapping):
    """新增或更新 [table] 内的若干键；表缺失时整体追加。"""
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        m = _TOML_TABLE_RE.match(lines[idx])
        if m and m.group(1) == table:
            end = idx + 1
            while end < len(lines) and not _TOML_TABLE_RE.match(lines[end]):
                end += 1
            return "\n".join(
                _toml_upsert_in_range(lines, idx + 1, end, mapping))
        idx += 1
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.append("[%s]" % table)
    for key in mapping:
        lines.append("%s = %s" % (key, _toml_value(mapping[key])))
    return "\n".join(lines) + "\n"


def _toml_upsert_in_range(lines, start, end, mapping):
    remaining = dict(mapping)
    last_content = start
    idx = start
    while idx < end:
        stripped = lines[idx].strip()
        if stripped and not stripped.startswith("#"):
            last_content = idx + 1
            m = _TOML_KEY_RE.match(lines[idx])
            if m and m.group(1) in remaining:
                key = m.group(1)
                lines[idx] = "%s = %s" % (key, _toml_value(remaining.pop(key)))
        idx += 1
    insert = last_content
    for key, value in remaining.items():
        lines.insert(insert, "%s = %s" % (key, _toml_value(value)))
        insert += 1
    return lines


# ---------------------------------------------------------------------------
# 文本级 dotenv 编辑器
# ---------------------------------------------------------------------------

def env_upsert(text, mapping):
    """新增或更新 KEY=VALUE 行；注释与无关行原样保留。"""
    lines = text.split("\n")
    remaining = dict(mapping)
    for idx, line in enumerate(lines):
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            lines[idx] = "%s=%s" % (key, remaining.pop(key))
    if remaining:
        while lines and not lines[-1].strip():
            lines.pop()
        for key, value in remaining.items():
            lines.append("%s=%s" % (key, value))
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# backup 与状态记录
# ---------------------------------------------------------------------------

def _state_path(accounts_dir):
    return os.path.join(accounts_dir, STATE_FILE)


def _load_state(accounts_dir):
    try:
        with open(_state_path(accounts_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {}


def _save_state(accounts_dir, state):
    os.makedirs(accounts_dir, exist_ok=True)
    _atomic_write_text(_state_path(accounts_dir), _dump_json(state))


def _backup_dir(accounts_dir, client_id):
    return os.path.join(accounts_dir, BACKUP_DIRNAME, client_id)


def _backup_file(accounts_dir, client_id, path, original):
    """保存 `path` 的原始字节；返回 backup 文件路径。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "%s-%s" % (stamp, os.path.basename(path) or "config")
    directory = _backup_dir(accounts_dir, client_id)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, name)
    seq = 1
    while os.path.exists(target):
        target = os.path.join(directory, "%s-%d" % (name, seq))
        seq += 1
    _atomic_write_bytes(target, original)
    _prune_backups(directory)
    return target


def _prune_backups(directory, keep=BACKUP_KEEP):
    """每个 basename 只保留最新的 `keep` 份 backup。"""
    by_name = {}
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return
    for name in entries:
        base = re.sub(r"^\d{8}-\d{6}-", "", name)
        by_name.setdefault(base, []).append(name)
    for names in by_name.values():
        for stale in names[:-keep] if len(names) > keep else []:
            try:
                os.remove(os.path.join(directory, stale))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 客户端注册表
# ---------------------------------------------------------------------------

def _claude_settings_path(home):
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return os.path.join(override, "settings.json")
    return os.path.join(home, ".claude", "settings.json")


def _apply_claude_code(paths, ctx):
    path = paths["settings"]
    data = _load_json(path, "claude settings")
    env = dict(data.get("env") or {})
    # Claude Code 需要时自己补 /v1，这里只留根路径。
    env["ANTHROPIC_BASE_URL"] = re.sub(r"/v1/?$", "", ctx["base_url"])
    env["ANTHROPIC_AUTH_TOKEN"] = ctx["api_key"]
    if ctx.get("model"):
        for name in ("ANTHROPIC_MODEL",
                     "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            env[name] = ctx["model"]
    data["env"] = env
    return {path: _dump_json(data)}


def _codex_home(home):
    return os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")


def _apply_codex(paths, ctx):
    config_path = paths["config"]
    text = _read_text(config_path)
    text = toml_upsert_top_key(text, "model_provider", PROVIDER_ID)
    if ctx.get("model"):
        text = toml_upsert_top_key(text, "model", ctx["model"])
    text = toml_upsert_table(text, "model_providers." + PROVIDER_ID, {
        "name": PROVIDER_NAME,
        "base_url": ctx["base_url_v1"],
        "wire_api": "responses",
        "env_key": "OPENAI_API_KEY",
    })
    auth_path = paths["auth"]
    auth = _load_json(auth_path, "codex auth")
    auth = deep_merge(auth, {"OPENAI_API_KEY": ctx["api_key"]})
    return {config_path: text, auth_path: _dump_json(auth)}


def _apply_opencode(paths, ctx):
    path = paths["config"]
    data = _load_json(path, "opencode config")
    if "$schema" not in data:
        data["$schema"] = "https://opencode.ai/config.json"
    models = {}
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if mid:
            models[mid] = {}
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": PROVIDER_NAME,
        "options": {
            "baseURL": ctx["base_url_v1"],
            "apiKey": ctx["api_key"],
        },
    }
    if models:
        provider["models"] = models
    data = deep_merge(data, {"provider": {PROVIDER_ID: provider}})
    if ctx.get("model"):
        data["model"] = "%s/%s" % (PROVIDER_ID, ctx["model"])
    return {path: _dump_json(data)}


def dsh_reasoning_levels(raw):
    """DSH 的 {档位: 线上拼写} 映射，取自网关的 `reasoning_efforts` 列表。DSH 的模型
    条目用 `reasoningEfforts` 声明可选思考档位（键=选择器里的档位，值=请求里实际发的
    拼写）；它只读 id / name / contextWindow / maxTokens，不读 /v1/models 里的
    reasoning_efforts。固定档位的模型（网关只给 reasoning_fixed_effort）不声明列表：
    给了选择器也不会改变上游实际跑的档位。
    """
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        pairs = [(lvl, lvl) for lvl in raw]
    else:
        return None
    levels = {}
    for level, wire in pairs:
        name = str(level or "").strip()
        if not name:
            continue
        levels[name] = str(wire or "").strip() or name
    return levels or None


def dsh_default_model(text, provider, model):
    """要写入的 `agent-default-model` 条目，保留不属于本程序的键。yaml_upsert_inline()
    会整块替换该叶子，只写 {provider, model} 会悄悄丢掉旁边已有的内容（例如默认思考
    档位在重新接入时消失）；这里先读当前 inline 映射再把路由合并上去，块式写法或
    解析不了的值退回裸的一对，不猜。
    """
    out = {"provider": provider, "model": model}
    for line in str(text or "").split("\n"):
        stripped = line.strip()
        if not stripped.startswith("agent-default-model:"):
            continue
        raw = stripped.split(":", 1)[1].strip()
        if raw.startswith("{"):
            try:
                current = json.loads(raw)
            except ValueError:
                current = None
            if isinstance(current, dict):
                out = dict(current)
        break
    out["provider"] = provider
    out["model"] = model
    return out


def _apply_dsh(paths, ctx):
    settings_path = paths["settings"]
    text = _read_text(settings_path)
    models = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if not mid:
            continue
        item = {"id": mid}
        if entry.get("context_window"):
            item["contextWindow"] = entry["context_window"]
        if entry.get("max_output"):
            try:
                item["maxTokens"] = int(entry["max_output"])
            except (TypeError, ValueError):
                pass
        levels = dsh_reasoning_levels(entry.get("reasoning_efforts"))
        if levels:
            item["reasoningEfforts"] = levels
        if entry.get("supports_vision") or entry.get("supportsVision"):
            item["supportsVision"] = True
        models.append(item)
    provider = {
        "displayName": PROVIDER_NAME,
        "apiKeyEnv": CREDENTIAL_REF,
        "api": "openai-completions",
        "baseURL": ctx["base_url_v1"],
        # wb-proxy 不在 DSH 的内置目录里，推理字段的线上形状必须自己说明：走标准
        # reasoning_effort、不发 thinking 块，DeepSeek 系需要的 thinking / reasoning_content
        # 由网关补（wb_proxy.build_upstream_body）。
        "compat": {"thinkingFormat": "openai", "supportsReasoningEffort": True},
    }
    if models:
        provider["models"] = models
    text = yaml_upsert_inline(
        text, "llm-pi-ai.providers.%s" % PROVIDER_ID, provider)
    if ctx.get("model"):
        text = yaml_upsert_inline(
            text, "agent-default-model",
            dsh_default_model(text, PROVIDER_ID, ctx["model"]))
    credentials_path = paths["credentials"]
    cred_text = _read_text(credentials_path)
    cred_text = yaml_refs_upsert(cred_text, CREDENTIAL_REF, ctx["api_key"])
    return {settings_path: text, credentials_path: cred_text}


def _apply_crush(paths, ctx):
    path = paths["config"]
    data = _load_json(path, "crush config")
    models = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if not mid:
            continue
        item = {"id": mid, "name": mid}
        if entry.get("context_window"):
            item["context_window"] = entry["context_window"]
        if entry.get("max_output"):
            item["default_max_tokens"] = entry["max_output"]
        models.append(item)
    provider = {
        "name": PROVIDER_NAME,
        "type": "openai",
        "base_url": ctx["base_url_v1"],
        "api_key": ctx["api_key"],
        "models": models,
    }
    data = deep_merge(data, {"providers": {PROVIDER_ID: provider}})
    return {path: _dump_json(data)}


def _apply_zcode(paths, ctx):
    """在 ZCode 的 provider 配置里新增或更新 wb-proxy 规则。规则用稳定的 providerId
    定位，重复接入只更新那一条、不叠出新的；文档里其它内容原样合并保留。该文件属于
    ZCode，运行中会被它重新写回：要在 ZCode 关闭时配置（面板卡片与 CLI 都会提示），
    否则它下一次保存可能把新条目丢掉。
    """
    path = paths["config"]
    data = _load_json(path, "zcode provider config")
    if not data:
        # 本机还没有 ZCode profile：按 3.x 实际写出的外层结构初始化（schemaVersion + config
        # + 两个规则容器），全新安装不用先跑一遍应用即可配置。
        data = {"schemaVersion": 1,
                "config": {"providerOrder": [],
                           "providerConfigRules": {"providerRules": []}}}
    config = data.get("config")
    if not isinstance(config, dict):
        raise AgentConfigError(
            "zcode provider config has no config object: %s" % path)
    holder = config.get("providerConfigRules")
    if not isinstance(holder, dict):
        raise AgentConfigError(
            "zcode provider config has no providerConfigRules: %s" % path)
    rules = holder.get("providerRules")
    if not isinstance(rules, list):
        raise AgentConfigError(
            "zcode providerConfigRules has no providerRules list: %s" % path)

    model_ids = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id") if isinstance(entry, dict) else entry
        mid = str(mid or "").strip()
        if mid and mid not in model_ids:
            model_ids.append(mid)

    rule = {
        "providerId": PROVIDER_ID,
        "providerName": PROVIDER_NAME,
        "config": {
            "group": "standard-personal",
            "access": {"type": "api-key", "apiKey": ctx["api_key"]},
            "api": {"type": "openai-chat-completions",
                    "baseUrl": ctx["base_url_v1"]},
        },
    }
    if model_ids:
        rule["config"]["personalModelIds"] = model_ids
        rule["config"]["modelOrder"] = list(model_ids)

    replaced = False
    for index, existing in enumerate(rules):
        if isinstance(existing, dict) and existing.get("providerId") == PROVIDER_ID:
            rules[index] = deep_merge(existing, rule)
            replaced = True
            break
    if not replaced:
        rules.append(rule)

    order = config.get("providerOrder")
    if not isinstance(order, list):
        order = []
        config["providerOrder"] = order
    if PROVIDER_ID not in order:
        order.append(PROVIDER_ID)
    return {path: _dump_json(data)}


CLIENTS = {
    "claude-code": {
        "id": "claude-code",
        "label": "Claude Code",
        "desc": "Anthropic CLI; points ANTHROPIC_BASE_URL at this gateway.",
        "protocol": "anthropic",
        "files": lambda home: {
            "settings": _claude_settings_path(home),
        },
        "probe": lambda home: (
            os.path.isdir(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude"))
            or os.path.isfile(_claude_settings_path(home))
            or bool(shutil.which("claude"))
        ),
        "apply": _apply_claude_code,
    },
    "codex": {
        "id": "codex",
        "label": "Codex CLI",
        "desc": "OpenAI Codex CLI; adds a wb-proxy model provider.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(_codex_home(home), "config.toml"),
            "auth": os.path.join(_codex_home(home), "auth.json"),
        },
        "probe": lambda home: (
            os.path.isdir(_codex_home(home))
            or os.path.isfile(os.path.join(_codex_home(home), "config.toml"))
            or bool(shutil.which("codex"))
        ),
        "apply": _apply_codex,
    },
    "opencode": {
        "id": "opencode",
        "label": "OpenCode",
        "desc": "OpenCode; registers an openai-compatible provider.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".config", "opencode", "opencode.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".config", "opencode"))
            or os.path.isfile(os.path.join(home, ".config", "opencode", "opencode.json"))
            or bool(shutil.which("opencode"))
        ),
        "apply": _apply_opencode,
    },
    "dsh": {
        "id": "dsh",
        "label": "DeepSeek Harness",
        "desc": "DSH; adds a wb-proxy provider to settings.yaml.",
        "protocol": "openai",
        "files": lambda home: {
            "settings": os.path.join(home, ".dsh", "settings.yaml"),
            "credentials": os.path.join(home, ".dsh", ".credentials.yaml"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".dsh"))
            or os.path.isfile(os.path.join(home, ".dsh", "settings.yaml"))
            or bool(shutil.which("dsh"))
        ),
        "apply": _apply_dsh,
    },
    "crush": {
        "id": "crush",
        "label": "Crush",
        "desc": "Charm Crush; adds a wb-proxy provider entry.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".config", "crush", "crush.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".config", "crush"))
            or os.path.isfile(os.path.join(home, ".config", "crush", "crush.json"))
            or bool(shutil.which("crush"))
        ),
        "apply": _apply_crush,
    },
    "zcode": {
        "id": "zcode",
        "label": "ZCode",
        "desc": "ZCode; adds a wb-proxy provider to its provider config. "
                "Quit ZCode first - it rewrites that file while running.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".zcode", "v2", "provider_config.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".zcode"))
            or os.path.isfile(os.path.join(home, ".zcode", "v2",
                                           "provider_config.json"))
        ),
        "apply": _apply_zcode,
    },
}


def _client(client_id):
    client = CLIENTS.get(client_id)
    if client is None:
        raise AgentConfigError(
            "unknown client %r (known: %s)"
            % (client_id, ", ".join(sorted(CLIENTS))))
    return client


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def enrich_agent_models(models, specs):
    """把只有 id 的模型条目补上网关自己的目录规格（上下文窗口、输出上限与思考档位）：
    DSH、Crush 这类客户端要写进规格才会生成对应控件，只写 id 时只能用内置默认值。
    `specs` 是模型 id 到规格的映射，条目里显式带来的字段优先，空值不算显式。
    """
    if not specs:
        return models
    out = []
    for item in models or []:
        spec = specs.get(str((item or {}).get("id")))
        if not spec:
            out.append(item)
            continue
        merged = dict(spec)
        merged.update({k: v for k, v in item.items() if v not in (None, "")})
        out.append(merged)
    return out


def overview(accounts_dir, home=None):
    home = _resolve_home(home)
    state = _load_state(accounts_dir)
    out = {}
    for client_id in sorted(CLIENTS):
        client = CLIENTS[client_id]
        paths = client["files"](home)
        existing = [p for p in paths.values() if os.path.exists(p)]
        applied = None
        record = state.get(client_id)
        if isinstance(record, dict):
            external = False
            for entry in record.get("files") or []:
                path = entry.get("path")
                if not path or not os.path.exists(path):
                    external = True
                    continue
                # 与上次写入的字节比对；backup 不参与比较，接入后手改必须算
                # 外部改动。
                try:
                    if _sha256_bytes(_read_bytes(path)) != entry.get("sha256"):
                        external = True
                except AgentConfigError:
                    external = True
            applied = {
                "at": record.get("applied_at"),
                "base_url": record.get("base_url"),
                "model": record.get("model"),
                "files": len(record.get("files") or []),
                "external_change": external,
            }
        out[client_id] = {
            "id": client_id,
            "label": client["label"],
            "desc": client["desc"],
            "protocol": client["protocol"],
            "installed": bool(client["probe"](home)) or bool(existing),
            # "configured" 只认本网关的接入记录。磁盘上恰好有配置文件不算接入，
            # 否则原有的配置会让面板显示无法执行的 restore。
            "configured": isinstance(record, dict),
            "config_paths": list(paths.values()),
            "applied": applied,
        }
    return out


def plan(client_id, base_url, api_key, model=None, models=None, home=None):
    """integrate() 将要写入的确切文件内容，不碰任何文件。`files` 每项带目标 `path`、
    新 `content` 与文件此前是否 `existed`，供调用方在备份与写入之前预览（CLI 的
    --dry-run）；dry run 打印的内容即实际写入的内容。
    """
    client = _client(client_id)
    home = _resolve_home(home)
    base_url = str(base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base_url):
        raise AgentConfigError("base_url must start with http:// or https://")
    base_url_v1 = base_url if base_url.endswith("/v1") else base_url + "/v1"
    ctx = {
        "base_url": base_url,
        "base_url_v1": base_url_v1,
        "api_key": str(api_key or ""),
        "model": str(model or "").strip() or None,
        "models": list(models or []),
    }
    paths = client["files"](home)
    try:
        new_contents = client["apply"](paths, ctx)
    except AgentConfigError:
        raise
    except Exception as exc:
        raise AgentConfigError("failed to build %s config: %s"
                               % (client_id, exc))
    files = []
    for path in paths.values():
        content = new_contents.get(path)
        if content is None:
            continue
        files.append({
            "path": path,
            "content": content,
            "existed": os.path.exists(path),
        })
    return {
        "client": client_id,
        "label": client["label"],
        "base_url": base_url,
        "model": ctx["model"],
        "files": files,
    }


def integrate(accounts_dir, client_id, base_url, api_key, model=None,
              models=None, home=None):
    """把 `client_id` 指向本网关，返回写入的内容。首次接入备份每个动过文件的原始
    字节；之后的接入保留最早的 backup，保证 restore 总能回到首次接入之前的状态。
    """
    built = plan(client_id, base_url, api_key, model=model, models=models,
                 home=home)
    state = _load_state(accounts_dir)
    record = state.get(client_id) or {}
    previous_files = {entry.get("path"): entry
                      for entry in record.get("files") or []
                      if isinstance(entry, dict)}
    # 阶段 1：动任何文件之前，先为全部目标文件备好 backup。
    staged = []
    for entry in built["files"]:
        path, content, existed = entry["path"], entry["content"], entry["existed"]
        prior = previous_files.get(path)
        if prior is not None and not prior.get("existed") and existed:
            # 该文件是之前接入时新建的：内容与当时写入的完全一致时算本程序所有，restore 直接
            # 删除（文档承诺，且那些字节可能含 API key）；此后被手改过就归还用户、照常备份。
            try:
                if _sha256_bytes(_read_bytes(path)) == prior.get("sha256"):
                    existed = False
            except AgentConfigError:
                pass
        backup = None
        if existed and prior and prior.get("backup"):
            # 保留最早的 backup：接入前的原始字节在里面。
            backup = prior.get("backup")
        elif existed:
            backup = _backup_file(accounts_dir, client_id, path,
                                  _read_bytes(path))
        staged.append((path, content, existed, backup))

    # 阶段 2：写入按事务处理。任一写入失败就回滚已写的全部文件，多文件接入
    # （如 DSH）不会留下配置了一半、状态记录缺失的局面。
    written = []
    entries = []
    try:
        for path, content, existed, backup in staged:
            _atomic_write_text(path, content)
            written.append((path, existed, backup))
            entries.append({
                "path": path,
                "existed": existed,
                "backup": backup,
                "sha256": _sha256_bytes(content.encode("utf-8")),
                "bytes": len(content.encode("utf-8")),
            })
    except Exception as exc:
        for w_path, w_existed, w_backup in written:
            try:
                if w_existed and w_backup:
                    # w_backup 是 _backup_file() 记下的完整备份路径，直接读回即可。
                    _atomic_write_bytes(w_path, _read_bytes(w_backup))
                elif not w_existed and os.path.exists(w_path):
                    os.unlink(w_path)
            except Exception:
                pass
        raise AgentConfigError("failed to write %s files: %s" % (client_id, exc))

    state[client_id] = {
        "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": built["base_url"],
        "model": built["model"],
        "files": entries,
    }
    _save_state(accounts_dir, state)
    return {
        "client": client_id,
        "label": built["label"],
        "applied_at": state[client_id]["applied_at"],
        "base_url": built["base_url"],
        "model": built["model"],
        "files": entries,
    }


def restore(accounts_dir, client_id, home=None):
    """撤销 integrate()：把 backup 的原始内容逐字节写回。首次接入前不存在的文件删除；
    该客户端的状态记录清除。
    """
    client = _client(client_id)
    home = _resolve_home(home)
    state = _load_state(accounts_dir)
    record = state.get(client_id)
    if not isinstance(record, dict) or not record.get("files"):
        raise AgentConfigError(
            "%s has no recorded integration to restore" % client["label"])
    restored = []
    for entry in record.get("files") or []:
        path = entry.get("path")
        if not path:
            continue
        if entry.get("existed") and entry.get("backup"):
            backup = entry["backup"]
            if not os.path.exists(backup):
                raise AgentConfigError(
                    "backup missing for %s: %s" % (path, backup))
            _atomic_write_bytes(path, _read_bytes(backup))
            restored.append({"path": path, "action": "restored"})
        else:
            try:
                os.remove(path)
                restored.append({"path": path, "action": "deleted"})
            except FileNotFoundError:
                restored.append({"path": path, "action": "already-absent"})
            # 接入时连父目录一起新建过。留下空目录会让 probe 把客户端误报成已安装（它唯一的配置
            # 本来就是我们写的），所以把清空的目录链一并剪掉，仍有内容的目录绝不动。
            _prune_empty_parents(path, home)
    state.pop(client_id, None)
    _save_state(accounts_dir, state)
    return {
        "client": client_id,
        "label": client["label"],
        "restored": restored,
    }


# ---------------------------------------------------------------------------
# 独立配置器：面板的一键接入跑在网关进程里，只能改网关所在那台机器的配置（这也是
# /agents/apply 拒绝非 loopback 调用与容器部署的原因）；CLI 跑在客户端所在机器，
# 仅经 HTTP 访问网关，网关装在服务器、NAS 或路由器上时仍能服务其他机器的客户端。
# ---------------------------------------------------------------------------


def fetch_models_from_gateway(base_url, api_key, timeout=10):
    """从网关取模型列表（GET /v1/models），单凭 API key 即可访问（非面板路由）。
    顺带校验 key：401 说明 key 不对，其余错误说明网关或其上游不可达。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base):
        raise AgentConfigError("base_url must start with http:// or https://")
    if base.endswith("/v1"):
        base = base[:-3]
    request = urllib.request.Request(
        base + "/v1/models",
        headers={"Authorization": "Bearer " + str(api_key or "")})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AgentConfigError(
                "the gateway rejected the API key (HTTP 401)")
        raise AgentConfigError("gateway /v1/models failed: HTTP %s" % exc.code)
    except ValueError as exc:
        raise AgentConfigError(
            "gateway /v1/models returned invalid JSON: %s" % exc)
    except Exception as exc:
        raise AgentConfigError(
            "cannot reach the gateway /v1/models: %s" % exc)
    models = []
    seen = set()
    for item in (payload.get("data") or []):
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        entry = {"id": mid}
        context = item.get("context_length") or item.get("max_input_tokens")
        if context:
            try:
                entry["context_window"] = int(context)
            except (TypeError, ValueError):
                pass
        out_tokens = item.get("max_output_tokens")
        if out_tokens:
            try:
                entry["max_output"] = int(out_tokens)
            except (TypeError, ValueError):
                pass
        efforts = item.get("reasoning_efforts")
        if isinstance(efforts, (list, tuple)):
            levels = [str(e).strip() for e in efforts if str(e or "").strip()]
            if levels:
                entry["reasoning_efforts"] = levels
        name = item.get("name")
        if name:
            entry["name"] = str(name)
        models.append(entry)
    # 与面板 apply 相同的上限：失控的模型目录不能变成 MB 级配置文件。
    return models[:80]


def _default_state_dir():
    """独立 CLI 存放状态与 backup 的位置，刻意避开网关的 accounts/ 目录：CLI 跑在
    另一台机器上，各机器自留记录与 backup。
    """
    return os.path.join(os.path.expanduser("~"), ".wb-agent-config")


def _mask_key(key):
    key = str(key or "")
    if not key:
        return "(none)"
    if len(key) <= 4:
        return "***"
    return "***" + key[-4:]


def _cli_error(message):
    sys.stderr.write("error: %s\n" % message)
    return 1


def main(argv=None):
    # --home/--state-dir/--json 在子命令前后都接受：共享 parent 以 SUPPRESS
    # 默认值把它们带进每个子解析器，子解析器不会覆盖顶层给的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=argparse.SUPPRESS,
                        help="home directory to configure "
                             "(default: the current user's home)")
    common.add_argument("--state-dir", default=argparse.SUPPRESS,
                        dest="state_dir",
                        help="where the integration state and backups live "
                             "(default: ~/.wb-agent-config)")
    common.add_argument("--json", action="store_true", dest="as_json",
                        default=argparse.SUPPRESS,
                        help="print machine-readable JSON")

    parser = argparse.ArgumentParser(
        prog="wb_agents.py", parents=[common],
        description="One-click agent integration for local AI clients. Runs "
                    "where the clients are, so the gateway may live on "
                    "another machine.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", parents=[common],
                   help="supported clients and what is installed on this machine")
    sub.add_parser("status", parents=[common],
                   help="this machine's integration records")

    apply_p = sub.add_parser("apply", parents=[common],
                             help="point a client at the gateway")
    apply_p.add_argument("--client", required=True,
                         help="one of: " + ", ".join(sorted(CLIENTS)))
    apply_p.add_argument("--base-url", required=True, dest="base_url",
                         help="gateway URL, e.g. http://gateway:8788/v1")
    apply_p.add_argument("--key", default=None,
                         help="gateway API key "
                              "(default: $WB_PROXY_API_KEY, then a prompt)")
    apply_p.add_argument("--model", default=None,
                         help="default model id to select in the client")
    apply_p.add_argument("--models-from-gateway", action="store_true",
                         dest="models_from_gateway",
                         help="fetch the model list from /v1/models "
                              "(this is the default)")
    apply_p.add_argument("--no-models", action="store_true", dest="no_models",
                         help="skip the model list; also skips key validation")
    apply_p.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="print what would be written; touch nothing")

    restore_p = sub.add_parser("restore", parents=[common],
                               help="undo an apply, byte for byte")
    restore_p.add_argument("--client", required=True,
                           help="one of: " + ", ".join(sorted(CLIENTS)))

    args = parser.parse_args(argv)

    # 输出里的中文路径或模型名不能让旧式 Windows 控制台上的运行中断——
    # tests/run_all.py 对自身也加了同样的保护。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    as_json = getattr(args, "as_json", False)
    state_dir = getattr(args, "state_dir", None) or _default_state_dir()
    home = _resolve_home(getattr(args, "home", None))

    if args.command in ("list", "status"):
        data = overview(state_dir, home)
        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        for client_id in sorted(data):
            entry = data[client_id]
            print("%-12s %-18s %s" % (
                client_id, entry["label"],
                "installed" if entry["installed"] else "not detected"))
            for path in entry["config_paths"]:
                print("    config:  %s" % path)
            if args.command == "status":
                applied = entry.get("applied")
                if applied:
                    print("    applied: %s  base_url=%s  model=%s%s" % (
                        applied["at"], applied["base_url"],
                        applied["model"] or "-",
                        "  (external change detected)"
                        if applied["external_change"] else ""))
                else:
                    print("    applied: never")
        return 0

    if args.command == "apply":
        if args.no_models and args.models_from_gateway:
            return _cli_error(
                "--no-models and --models-from-gateway are mutually exclusive")
        client_id = str(args.client).strip()
        try:
            _client(client_id)
        except AgentConfigError as exc:
            return _cli_error(str(exc))
        base_url = str(args.base_url).strip()
        if not re.match(r"^https?://", base_url):
            return _cli_error("--base-url must start with http:// or https://")
        key = str(args.key or os.environ.get("WB_PROXY_API_KEY") or "")
        # 只在有真实控制台时才提示输入：Windows 上 NUL 设备也会让 isatty() 为真（被拉起或
        # 服务方式运行的进程常以它作 stdin），而 getpass 在那里直接读 CONIN$，无控制台的
        # 进程会永远卡住。
        if not key and sys.stdin.isatty() and sys.stdout.isatty():
            try:
                key = getpass.getpass("Gateway API key: ").strip()
            except (EOFError, KeyboardInterrupt):
                key = ""
        if not key:
            return _cli_error(
                "no API key: pass --key, set WB_PROXY_API_KEY, or run "
                "interactively")

        models = []
        if not args.no_models:
            try:
                models = fetch_models_from_gateway(base_url, key)
            except AgentConfigError as exc:
                return _cli_error(
                    "%s - retry, or pass --no-models to configure without "
                    "the model list and key validation" % exc)

        if args.dry_run:
            try:
                built = plan(client_id, base_url, key, model=args.model,
                             models=models, home=home)
            except AgentConfigError as exc:
                return _cli_error(str(exc))
            if as_json:
                print(json.dumps({
                    "client": built["client"],
                    "label": built["label"],
                    "base_url": built["base_url"],
                    "model": built["model"],
                    "files": [{"path": item["path"],
                               "existed": item["existed"],
                               "content": item["content"]}
                              for item in built["files"]],
                }, ensure_ascii=False, indent=2))
                return 0
            print("dry run: nothing will be written")
            print("client:  %s (%s)" % (built["label"], built["client"]))
            print("gateway: %s" % built["base_url"])
            print("key:     %s" % _mask_key(key))
            for item in built["files"]:
                print()
                print("--- %s %s" % (
                    "modify" if item["existed"] else "create", item["path"]))
                print(item["content"], end="")
                if not item["content"].endswith("\n"):
                    print()
            return 0

        try:
            result = integrate(state_dir, client_id, base_url, key,
                               model=args.model, models=models, home=home)
        except AgentConfigError as exc:
            return _cli_error(str(exc))
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print("configured %s (%s) -> %s" % (
            result["label"], result["client"], result["base_url"]))
        for entry in result["files"]:
            print("  %-8s %s" % (
                "updated" if entry["existed"] else "created", entry["path"]))
        if models:
            print("models:  %d fetched from the gateway (key accepted)"
                  % len(models))
        print("key:     %s" % _mask_key(key))
        print("state:   %s" % state_dir)
        print("restore: python %s restore --client %s"
              % (os.path.basename(__file__), client_id))
        return 0

    if args.command == "restore":
        client_id = str(args.client).strip()
        try:
            result = restore(state_dir, client_id, home=home)
        except AgentConfigError as exc:
            return _cli_error(str(exc))
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print("restored %s (%s)" % (result["label"], result["client"]))
        for entry in result["restored"]:
            print("  %-9s %s" % (entry["action"], entry["path"]))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
