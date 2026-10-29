"""模型目录：内置快照、桌面缓存、远端 /v3/config 与定时刷新。

网关对客户端展示的模型清单由这里拼出：远端目录优先，取不到时回退桌面客户端
缓存、网关存档与内置快照。筛选规则把虚拟别名与不可对话的模型排除在清单外，
但保留它们的名称供面板展示。刷新线程每天至少拉一次，请求路径只读缓存。
"""

import json
import os
import threading
import time
import urllib.error

import wb_accounts
import wb_catalog
import wb_host
import wb_modelsdev
import wb_probes
import wb_settings
import wb_upstream_pool


APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(APP_DIR)


def _gateway():
    """网关模块：本模块运行期要用它的账号池、日志与目录。"""
    module = wb_host.loaded_gateway("log", "ACCOUNTS_DIR", "POOL")
    if module is None:
        import wb_proxy as module
    return module


UPSTREAM = "https://www.workbuddy.ai"

MODELS_PATH = "/v2/enterprises/personal/models"

_models_cache = {"intl": {"at": 0.0, "data": None}, "cn": {"at": 0.0, "data": None}}
#: 目录来源追踪，与 _models_cache 同批更新，供面板展示：source 取值见
#: catalog_meta；fetched_at 是网关拿到数据的时刻；file_mtime 是桌面缓存文件的
#: 写入时刻，仅配置文件来源有，可能远早于 fetched_at，是清单新鲜度的证据。
_catalog_meta = {}
# 每个 realm 被筛选规则丢弃的原始目录名称，面板把它们显示为关闭行。
_catalog_excluded = {}
_catalog_raw = {}
_catalog_raw_meta = {}

CATALOG_CACHE_DIR = os.environ.get("WB_PROXY_CATALOG_DIR") \
    or os.path.join(PROJECT_DIR, "catalog")

# 虚拟档位别名：面板展示但不当作可选模型，也不进对客户端公布的清单。
VIRTUAL_ALIAS_MODELS = {
    "default-model",
    "fast-model",
    "balanced-model",
    "primary-model",
    "deep-model",
    # 国内出口的自动路由入口：选择器会显示，但客户端无法指定，因此不公布。
    "auto",
}

# 上游会列出但不能用于对话的模型：lite 供内部标题生成与压缩，上游用 11102
# 拒绝；codewise 与 completion 系列是文本补全或 IDE 内联模型。
NON_CHAT_MODELS = {"lite"} | VIRTUAL_ALIAS_MODELS
NON_CHAT_PREFIXES = ("codewise-", "completion-")
NON_CHAT_SUFFIXES = ("-image-alpha", "-image-alpha-edit", "-taco-completion")


def is_chat_model(mid):
    if not mid:
        return False
    if mid in NON_CHAT_MODELS:
        return False
    if mid.startswith(NON_CHAT_PREFIXES):
        return False
    if mid.endswith(NON_CHAT_SUFFIXES):
        return False
    return True


CN_UI_ORDER = [
    "hy4-preview-f",
    "hy3",
    "deepseek-v4.1-flash",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "minimax-m3",
    "kimi-k3-1",
    "kimi-k2.8-preview",
    "kimi-k2.7",
    "kimi-k2.6",
    "deepseek-v4-pro",
]
INTL_UI_ORDER = [
    "hy4-preview-f",
    "hy3",
    "deepseek-v4.1-flash",
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "grok-4.7",
    "gemini-3.5-flash",
    "glm-5.3-flash",
    "glm-5.3",
    "glm-5.2",
    "kimi-k3",
    "kimi-k2.6",
    "kimi-k2.8-preview",
]
def merge_reasoning(base, live):
    """单个模型 reasoning 块的字段级合并。

    live 目录是事实来源，但常不重述每个字段；顶层整体覆盖会丢 supportedEfforts，
    而只带 effort、没有 supportedEfforts 会被读成锁定档位，模型在 /v1/models
    显示为不可选，发给它的请求也被按错误档位上报。所以逐字段合并：结果块带
    supportedEfforts 即可选，其中的 effort 只是默认值重述，不是锁定。
    """
    out = dict(base) if isinstance(base, dict) else {}
    if isinstance(live, dict):
        out.update(live)
    if out.get("supportedEfforts") and out.get("effort"):
        out.setdefault("defaultEffort", out["effort"])
        out.pop("effort", None)
    return out
def merge_catalog(primary, realm=None, extras=False):
    r = realm or _gateway().CURRENT_REALM
    merged = {}
    # "all" 取两个 realm 的并集：分析面板列出网关服务过的全部模型，不能漏掉
    # 只有一侧目录认识的模型。
    if r == "all":
        source_static = list(wb_catalog.STATIC_INTL_MODELS) + list(wb_catalog.STATIC_CN_MODELS)
    else:
        source_static = getattr(wb_catalog, "STATIC_CN_MODELS" if r == "cn" else "STATIC_INTL_MODELS", wb_catalog.STATIC_MODELS)
    primary_ids = set(m for m, _ in (primary or []))
    for item in source_static:
        mid = item.get("id")
        if mid == "hy4-preview-f" and "hy4-preview" in primary_ids and "hy4-preview-f" not in primary_ids:
            continue
        if mid and is_chat_model(mid) and mid not in merged:
            merged[mid] = dict(item)
    for mid, meta in primary or []:
        if not is_chat_model(mid):
            continue
        if meta:
            base = merged.get(mid) or {}
            base_reasoning = base.get("reasoning")
            base.update(meta)
            if isinstance(meta.get("reasoning"), dict):
                base["reasoning"] = merge_reasoning(base_reasoning, meta["reasoning"])
            merged[mid] = base
        elif mid not in merged:
            merged[mid] = {}
    order = CN_UI_ORDER if r == "cn" else INTL_UI_ORDER
    if r == "all":
        order = list(INTL_UI_ORDER) + [m for m in CN_UI_ORDER if m not in INTL_UI_ORDER]
    if primary_ids:
        order = [("hy4-preview" if (m == "hy4-preview-f" and "hy4-preview" in primary_ids and "hy4-preview-f" not in primary_ids) else m) for m in order]
    out = []
    seen = set()
    for mid in order:
        if mid in merged and mid not in seen:
            seen.add(mid)
            out.append((mid, merged[mid]))
    if extras:
        # 裁剪表没听过的模型，只要 live 目录列了就照样发布：新上架模型不必发版
        # 就能进 /v1/models。单靠内置快照不够，它还夹带选择器不展示的旧条目。
        for mid, _meta in primary or []:
            if mid in merged and mid not in seen:
                item = merged[mid]
                # 无倍率模型隐藏：内置表与远程 meta 都没给 credits 的模型不进对外目录
                if not item.get("credits"):
                    continue
                seen.add(mid)
                out.append((mid, item))
    return out
# 目录状态的自有锁，与登录限流的锁分开，互不阻塞。
_lock = threading.Lock()
_catalog_lock = threading.Lock()

_catalog_fallback_log = {}
def note_bundled_reasoning(realm, live, entries):
    """模型的 reasoning 控件来自内置表时记一次日志。

    live 目录是事实来源，快照只是兜底，快照独有值的新鲜度不会超过快照本身。
    两种情况会走到这里：live 目录完全没给 reasoning 块；live 只给裸 effort，
    而快照知道该档位可选。每次变化记一行即可。
    """
    live_meta = dict(live or [])

    def from_snapshot(mid, meta):
        reasoning = meta.get("reasoning") or {}
        if not reasoning:
            return False
        live_reasoning = (live_meta.get(mid) or {}).get("reasoning") or {}
        if not live_reasoning:
            return True
        return bool(reasoning.get("supportedEfforts")) \
            and not live_reasoning.get("supportedEfforts")

    missing = sorted(mid for mid, meta in entries if from_snapshot(mid, meta))
    if not missing:
        _catalog_fallback_log.pop(realm, None)
        return
    if _catalog_fallback_log.get(realm) == frozenset(missing):
        return
    _catalog_fallback_log[realm] = frozenset(missing)
    shown = ", ".join(missing[:6]) + (" ..." if len(missing) > 6 else "")
    _gateway().log("catalog    : bundled table fills in reasoning controls the live "
        "catalogue omits for %d model(s): %s" % (len(missing), shown))
def catalog_meta(realm=None):
    """该 realm 最近一次清单的来源信息，供面板展示。

    {"source": "remote" | "endpoint" | "cache" | "legacy" | "bundled",
     "fetched_at": 网关拿到该批数据的 unix 秒,
     "file_mtime": 桌面缓存文件写入时刻的 unix 秒，仅配置文件来源有，否则 None}
    """
    r = realm or _gateway().CURRENT_REALM
    with _lock:
        meta = _catalog_meta.get(r)
        return dict(meta) if meta else None

def fetch_models(realm=None, force=False):
    r = realm or _gateway().CURRENT_REALM
    with _lock:
        if force:
            _models_cache.pop(r, None)
        c = _models_cache.get(r) or {"at": 0.0, "data": None}
        if c["data"] and time.time() - c["at"] < 300:
            return c["data"]
    # 同一 realm 只做一次上游取数：并发的 /v1/models 缓存未命中不能变成一串
    # 上游请求。
    with _catalog_lock:
        with _lock:
            c = _models_cache.get(r) or {"at": 0.0, "data": None}
            if c["data"] and time.time() - c["at"] < 300:
                return c["data"]
        live, extras, source = curated_live_sources(r, force=force)
        if not live and r in ("intl", "all"):
            # 窄口径端点不是桌面目录，保持白名单语义：只发布排序表认识的名字。
            live = [(m, {}) for m in fetch_endpoint_models()]
            extras = False
            if live:
                source = "endpoint"
        entries = merge_catalog(live, realm=r, extras=extras)
        raw_ids = _catalog_raw.get(r) or [mid for mid, _meta in live]
        served_ids = {mid for mid, _meta in entries}
        note_bundled_reasoning(r, live, entries)
        if not live:
            # 没有任何活数据：清单完全来自内置快照，硬编码兜底。
            source = "bundled"
        elif source in ("remote", "endpoint"):
            # 打点「已从 API 拿到目录」，每日兜底按 24h 间隔判定跳过，
            # 见 catalog_daily_pass。
            note_remote_catalog(r)
        file_mtime = None
        if source in ("cache", "legacy") and r in ("intl", "cn"):
            try:
                file_mtime = os.path.getmtime(product_config_path(r))
            except OSError:
                file_mtime = None
        with _lock:
            _models_cache[r] = {"at": time.time(), "data": entries}
            _catalog_excluded[r] = [mid for mid in raw_ids if mid not in served_ids]
            _catalog_meta[r] = {"source": source, "fetched_at": time.time(),
                                "file_mtime": file_mtime}
        seed_default_disabled(_catalog_excluded[r])
        return entries
def model_entry(mid, meta):
    """用桌面端目录元数据拼一条富 /v1/models 条目。

    OpenAI 规范只定义 id/object/created/owned_by，能力字段没有标准，这里同时
    输出 capabilities、supports_*、vision、abilities、modalities、architecture
    等多种形状，供不同客户端各取所认的字段。
    """
    meta = meta or {}
    item = {
        "id": mid,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "workbuddy",
    }
    name = meta.get("name")
    if name:
        item["name"] = name
    desc = meta.get("descriptionEn") or meta.get("descriptionZh")
    if desc:
        item["description"] = desc
    # disabledMultimodal 显式关闭读图；缺省视为允许。
    vision = bool(meta.get("supportsImages")) and not meta.get("disabledMultimodal")
    if mid == "glm-5.3-flash":
        # 强制修正：上游元数据虽声明支持多模态，但后端实际未接入视觉通道，见 issue #315。
        vision = False
        item["vision_note"] = "上游声明多模态但实际通道不支持读图，已修正为纯文本"
    tools = bool(meta.get("supportsToolCall"))
    thinks = bool(meta.get("supportsReasoning"))
    inputs = ["text"] + (["image"] if vision else [])
    # 能力字段无标准拼法，按常见客户端的读法各出一份：capabilities.vision 通用、
    # supports_vision / supports_images 为 LobeChat 式扁平标志、vision 供
    # Cherry Studio / NextChat、abilities.vision 供 LobeChat、multimodal 杂项、
    # *_modalities 供 OpenRouter。
    item["capabilities"] = {
        "vision": vision,
        "tool_calls": tools,
        "reasoning": thinks,
    }
    item["supports_vision"] = vision
    item["supports_images"] = vision
    item["supports_tool_calls"] = tools
    item["supports_reasoning"] = thinks
    item["vision"] = vision
    item["multimodal"] = vision
    item["abilities"] = {
        "vision": vision,
        "functionCall": tools,
        "function_call": tools,
        "reasoning": thinks,
    }
    item["input_modalities"] = inputs
    item["output_modalities"] = ["text"]
    item["modalities"] = {"input": inputs, "output": ["text"]}
    # OpenRouter 形状的块，多供应商聚合客户端按它读取。
    item["architecture"] = {
        "input_modalities": inputs,
        "output_modalities": ["text"],
        "modality": "+".join(inputs) + "->text",
    }
    # 四级查找：远端 > 内建知识表 > 本地 model.json 缓存 > models.dev 异步预热、
    # 不阻塞。context_length 总有值，未知取 1M，宁高勿低；max_output_tokens 未知
    # 则省略。
    ctx_value, out_value, _limit_source = wb_modelsdev.lookup(
        mid, meta.get("maxInputTokens"), meta.get("maxOutputTokens"),
        directory=_gateway().ACCOUNTS_DIR)
    item["context_length"] = ctx_value
    item["max_input_tokens"] = ctx_value
    if out_value:
        item["max_output_tokens"] = out_value
        item["max_completion_tokens"] = out_value
    # 附上 output_probes.json 实测的上游钳制值，只标注、不覆盖规格值，面板在
    # 旁边显示「钳制 N×」。
    clamp = wb_probes.clamp_for(_gateway().ACCOUNTS_DIR, mid)
    if clamp:
        item["output_clamp"] = clamp
        item["max_output_tokens_clamped"] = clamp
    ctx = (meta.get("contextWindow") or {}).get("supportedLengths")
    if ctx:
        item["context_windows"] = ctx
    reasoning = meta.get("reasoning") or {}
    efforts = reasoning.get("supportedEfforts")
    if efforts:
        item["reasoning_efforts"] = efforts
    if reasoning.get("effort"):
        item["reasoning_fixed_effort"] = reasoning["effort"]
    if reasoning.get("defaultEffort"):
        item["reasoning_default_effort"] = reasoning["defaultEffort"]
    if reasoning.get("canDisableThinking") is not None:
        item["reasoning_can_disable"] = reasoning["canDisableThinking"]
    if meta.get("onlyReasoning") is not None:
        item["always_reasoning"] = bool(meta.get("onlyReasoning"))
    if meta.get("credits"):
        item["credits"] = meta["credits"]
    if meta.get("vendor"):
        item["vendor"] = meta["vendor"]
    if meta.get("temperature") is not None:
        item["temperature"] = meta["temperature"]
    if meta.get("top_p") is not None:
        item["top_p"] = meta["top_p"]
    if meta.get("isDefault"):
        item["is_default"] = True
    tags = [t for t in (meta.get("tags") or []) if isinstance(t, str) and not t.startswith("badge:")]
    if tags:
        item["tags"] = tags
    return item
def read_product_config_models(realm=None):
    """读桌面端的目录缓存：[(id, meta), ...]。

    "all" 时两个应用都装了就都读，合并视图才能拿到两侧各自的元数据，不只国内那份。
    """
    r = realm or _gateway().CURRENT_REALM
    if r == "all":
        out = _read_product_config_dir(".workbuddy-ai")
        seen = set(mid for mid, _ in out)
        for mid, meta in _read_product_config_dir(".workbuddy"):
            if mid not in seen:
                out.append((mid, meta))
        return out
    return _read_product_config_dir(".workbuddy-ai" if r == "intl" else ".workbuddy")

def _read_product_config_dir(cache_dir):
    home = os.path.expanduser("~")
    p = os.path.join(home, cache_dir, "cache", "acc-product-config-v3.json")
    try:
        with open(p, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        return []
    def find(node):
        if isinstance(node, dict):
            models = node.get("models")
            if isinstance(models, list) and models and isinstance(models[0], dict) and models[0].get("id"):
                return models
            for value in node.values():
                hit = find(value)
                if hit:
                    return hit
        return None
    models = find(cfg) or []
    out = []
    for m in models:
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            out.append((mid, m))
    return out
#: 桌面端自己的 product-config 端点。read_product_config_models 读的缓存文件
#: 就是它的响应落盘。没有桌面端的机器，如 Docker、NAS、无头服务器，直接调它
#: 才能公布含实时倍率的活目录，而不是窄口径端点或内置快照。
REMOTE_CONFIG_PATH = "/v3/config"

#: 目录已有的名字加这些后缀就是变体：区域版如 deepseek-v4.1-flash-sg，实验版
#: 如 hy3-x。两个出口实测：裸名免费 x0.00、变体收费，因此对外只发布裸名。
VARIANT_SUFFIXES = ("-sg", "-x")

def remote_config_headers(account, realm, ua=None):
    """product-config 请求的头。

    UA 决定返回哪份目录，只有桌面端 UA 拿到完整列表，未知 UA 直接 400 / code
    12403，因此 UA 用 realm 固定的桌面 UA，不用账号当前的身份。
    """
    cfg = wb_accounts.get_realm_config(realm)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": ua or cfg["chat_ua"],
        "Origin": cfg["origin"],
        "Referer": cfg["origin"] + "/",
        "X-User-Id": account.uid,
    }
    headers.update(account.auth_headers())
    return headers

def _agent_model_lists(payload):
    """各 Agent 的裸字符串模型列表，命令行命名的 Agent 排在前面。

    选择器展示的目录挂在 agents[].models 下。接口返回时套了一层 "data"，桌面缓存
    文件则是同一份文档直接落盘、没有信封，两种都要读。
    """
    roots = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        roots.append(data)
    cli, other = [], []
    for root in roots:
        agents = root.get("agents")
        if isinstance(agents, dict):
            entries = list(agents.items())
        elif isinstance(agents, list):
            entries = [((entry.get("name") if isinstance(entry, dict) else None),
                        entry) for entry in agents]
        else:
            continue
        for name, entry in entries:
            if not isinstance(entry, dict):
                continue
            models = entry.get("models")
            if not (isinstance(models, list) and models
                    and isinstance(models[0], str)):
                continue
            ids = [str(m).strip() for m in models if isinstance(m, str)]
            ids = [m for m in ids if m]
            if not ids:
                continue
            (cli if str(name or "").strip().lower() == "cli" else other).append(ids)
    return cli, other

def parse_remote_catalog(payload):
    """从 /v3/config 响应取 (ids, meta)，没有目录时返回 None。

    列表挂在 agents[].models 下，元素是裸 id；逐模型元数据在另一个 models 数组
    里。凭据无效时上游仍回 HTTP 200 但列表为空，空目录报 None 由调用方回退，
    避免发布出「这个出口没有模型」。
    """
    if not isinstance(payload, dict):
        return None
    cli_lists, other_lists = _agent_model_lists(payload)
    pool = cli_lists or other_lists
    best = max(pool, key=len) if pool else None
    ids, seen = [], set()
    for mid in best or []:
        if mid not in seen:
            seen.add(mid)
            ids.append(mid)
    if not ids:
        return None

    meta = {}

    def walk(node):
        if isinstance(node, dict):
            models = node.get("models")
            if isinstance(models, list) and models \
                and isinstance(models[0], dict) and models[0].get("id"):
                for item in models:
                    mid = str(item.get("id") or "").strip()
                    if mid:
                        meta.setdefault(mid, item)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return ids, meta

def seed_default_disabled(ids):
    """把筛选规则丢掉的模型默认加入停止名单，每个模型只做一次。

    默认拒绝这些模型的路由；操作者在模型页重新勾选后，excluded_seen 保证
    下一次加载不会再关掉它。
    """
    seen = {str(m).strip().lower() for m in wb_settings.excluded_seen(_gateway().ACCOUNTS_DIR)}
    fresh = [m for m in ids if str(m).strip().lower() not in seen]
    if not fresh:
        return
    wb_settings.remember_excluded(_gateway().ACCOUNTS_DIR, fresh)
    disabled = wb_settings.disabled_models(_gateway().ACCOUNTS_DIR)
    known = {str(m).strip().lower() for m in disabled}
    wb_settings.set_disabled_models(
        _gateway().ACCOUNTS_DIR, disabled + [m for m in fresh
                                  if str(m).strip().lower() not in known])

def curate_remote_catalog(realm, ids, meta=None):
    """把远程目录裁剪成选择器该展示的模型。

    剔除虚拟别名 default-model、auto 等非模型项，剔除 "-sg" / "-x" 付费变体，
    其余保持上游顺序。上游没列的名字仍可经 curated 顺序表与内置快照取到，
    merge_catalog 会保留它们。
    """
    out = []
    for mid in ids:
        if not is_chat_model(mid):
            continue
        if mid.endswith(VARIANT_SUFFIXES):
            continue
        out.append(mid)
    return out

def fetch_remote_product_config(realm):
    """从该 realm 自己的 product-config 端点取 (ids, meta)，取不到返回 None。

    最多两次 10 秒尝试，每个桌面 UA 一次。端点躲在 WAF 后面，连接被断是常态，
    而每个调用方都有回退：桌面缓存文件、窄口径模型端点、内置快照。
    """
    if realm not in ("intl", "cn") or _gateway().POOL is None:
        return None
    if _gateway().stand_down_active():
        _gateway().log("remote catalog: gateway is stopped after an upstream 403, skipping")
        return None
    account = _gateway().POOL.representative(realm=realm)
    if account is None or not account.access_token:
        _gateway().log("remote catalog: no usable %s account, skipping" % realm)
        return None
    cfg = wb_accounts.get_realm_config(realm)
    url = cfg["chat_upstream"] + REMOTE_CONFIG_PATH
    # chat UA 是网关其余部分在用的桌面身份；纯 app UA 是第二尝试，给只认它
    # 的构建用。
    uas = [cfg["chat_ua"]]
    if cfg.get("billing_ua") and cfg["billing_ua"] != cfg["chat_ua"]:
        uas.append(cfg["billing_ua"])
    last = None
    for ua in uas:
        headers = remote_config_headers(account, realm, ua)
        req = wb_upstream_pool.Request(url, headers=headers, method="GET")
        try:
            with wb_accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            continue
        parsed = parse_remote_catalog(payload)
        if parsed:
            # 成功副本落盘到 catalog/，纯存档，见 persist_remote_catalog。
            persist_remote_catalog(realm, payload)
            return parsed
        last = "empty catalogue"
    _gateway().log("remote catalog: %s fetch failed (%s)" % (realm, last))
    return None

#: 面板「刷新模型清单」按钮的服务端冷却：同一 realm 两次手动强拉的最短间隔。
#: 自动链已有 300s 缓存与桌面 UA 双尝试，这层再拦误连点，防止把 /v3/config 点成
#: 高频请求。上游有 WAF，连接被丢是常态，节制本身就是不触发风控的一部分。
CATALOG_REFRESH_COOLDOWN = 30.0
_catalog_refresh_at = {}

def refresh_catalog(realm):
    """手动刷新：立即向上游拉一次该 realm 的目录并发布到 /v1/models。

    与自动链共用取数管线：fetch_remote_product_config 只挑该 realm 的代表账号、
    只打该 realm 的端点，realm 隔离由 POOL.representative(realm=realm) 保证，
    绝无跨区取数；_catalog_lock 与自动链互斥，不会并发打到上游；失败不清旧缓存，
    清单保持可用。与自动链的差别：这里不读桌面缓存文件，拉不到就说拉不到，
    手动按钮要的是「这次真的从上游 API 取到了」的如实反馈。

    返回面板可直接展示的 dict，任何情况都不抛异常：
      {"ok": bool, "realm": str|None, "source": "remote"|None,
       "count": int, "cooldown": bool?, "retry_after": float?, "msg": str}
    """
    if realm not in ("intl", "cn"):
        return {"ok": False, "realm": None, "source": None, "count": 0,
                "msg": "realm 必须是 intl 或 cn"}
    now = time.time()
    with _lock:
        wait = CATALOG_REFRESH_COOLDOWN - (now - _catalog_refresh_at.get(realm, 0.0))
        if wait > 0:
            return {"ok": False, "realm": realm, "source": None, "count": 0,
                    "cooldown": True, "retry_after": round(wait, 1),
                    "msg": "刷新太频繁：约 %d 秒后可再试" % max(1, int(wait + 0.5))}
        # 先登记再拉取：失败也进冷却，连点的高频重试在这里就被拦住。
        _catalog_refresh_at[realm] = now
    with _catalog_lock:
        try:
            remote = fetch_remote_product_config(realm)
        except Exception as exc:
            _gateway().log("catalog refresh: %s failed (%s)" % (realm, exc))
            remote = None
        if not remote:
            _gateway().log("catalog refresh: %s kept the previous list (upstream gave none)" % realm)
            return {"ok": False, "realm": realm, "source": None, "count": 0,
                    "msg": "上游未返回可用目录（账号不可用或网络异常），已保留现有清单"}
        ids, meta = remote
        _catalog_raw[realm] = list(ids)
        _catalog_raw_meta[realm] = dict(meta or {})
        persist_catalog_snapshot(realm, ids, meta)
        live = [(mid, meta.get(mid) or {})
                for mid in curate_remote_catalog(realm, ids, meta)]
        entries = merge_catalog(live, realm=realm, extras=True)
        note_bundled_reasoning(realm, live, entries)
        with _lock:
            _models_cache[realm] = {"at": time.time(), "data": entries}
            _catalog_excluded[realm] = [mid for mid in ids
                                        if mid not in {m for m, _ in entries}]
            _catalog_meta[realm] = {"source": "remote", "fetched_at": time.time(),
                                    "file_mtime": None}
    seed_default_disabled(_catalog_excluded[realm])
    _gateway().log("catalog refresh: %s updated from the product-config endpoint (%d models)"
        % (realm, len(entries)))
    note_remote_catalog(realm)
    return {"ok": True, "realm": realm, "source": "remote", "count": len(entries),
            "msg": "已从上游 API 更新清单，共 %d 个模型" % len(entries)}

#: 目录的「每天至少从上游 API 取一次」兜底。活跃部署里 fetch_models 的 300s
#: 缓存把数据刷得更勤；这个后台循环只兜长时间没人访问、或自动链一直回退到缓存
#: 文件/内置快照两种情况。上游有 WAF，频率上限是每 realm 每天一次：成功即打点，
#: 距上次成功满 24h 才再触发；失败不动成功记录，随循环每小时再试。
CATALOG_DAILY_INTERVAL = 24 * 3600
_catalog_last_remote = {}

def note_remote_catalog(realm, when=None):
    """记下该 realm 最近一次成功从上游 API 拿到目录的时刻。"""
    if realm not in ("intl", "cn"):
        return
    with _lock:
        _catalog_last_remote[realm] = when if when is not None else time.time()

def catalog_daily_due(now=None):
    """到期的 realm 列表：距上次成功 API 获取 ≥ 24h，从未成功过也算到期。"""
    now = now if now is not None else time.time()
    with _lock:
        return [r for r in ("intl", "cn")
                if now - _catalog_last_remote.get(r, 0.0) >= CATALOG_DAILY_INTERVAL]

def catalog_daily_pass(now=None):
    """每日兜底的一轮：只为到期的 realm 触发一次刷新。

    与手动刷新共用 refresh_catalog，同一把 _catalog_lock、同一个 30s 冷却、
    同样的 realm 隔离，失败保留旧数据。没有可用账号的 realm 静默跳过，等有账号
    了下一轮再看，不留日志噪声。
    """
    done = []
    for realm in catalog_daily_due(now):
        if _gateway().POOL is None or _gateway().POOL.representative(realm=realm) is None:
            continue
        out = refresh_catalog(realm)
        _gateway().log("catalog daily: %s -> %s" % (realm, out.get("msg")))
        done.append((realm, out))
    return done

def catalog_daily_loop():
    # 先等启动流程把账号池、服务就位，之后每小时看一眼。
    time.sleep(60)
    while True:
        try:
            catalog_daily_pass()
        except Exception as exc:
            _gateway().log("catalog daily: pass failed (%s)" % exc)
        time.sleep(3600)

def start_catalog_daily():
    threading.Thread(target=catalog_daily_loop, name="catalog-daily",
                     daemon=True).start()

def product_config_path(realm):
    """该 realm 的桌面端缓存文件路径，两个桌面应用各写各的。"""
    home = os.path.expanduser("~")
    cache_dir = ".workbuddy-ai" if realm == "intl" else ".workbuddy"
    return os.path.join(home, cache_dir, "cache", "acc-product-config-v3.json")

def catalog_cache_path(realm=None):
    """网关自己的落盘副本：最近一次成功拉取的 /v3/config 响应体。"""
    return os.path.join(CATALOG_CACHE_DIR,
                        "product-config-%s.json" % (realm or _gateway().CURRENT_REALM))

def persist_remote_catalog(realm, payload):
    """把刚成功拉取的 /v3/config 响应体原样落盘一份，原子写、失败静默。

    落盘副本同时是兜底来源：快照不可用且远程拉取失败时 read_gateway_catalog
    会读它。写盘失败不是取数失败，绝不能影响本次结果。
    """
    try:
        os.makedirs(CATALOG_CACHE_DIR, exist_ok=True)
        path = catalog_cache_path(realm)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        _gateway().log("catalog cache: %s persist failed (%s)" % (realm, exc))

def read_cached_remote_catalog(realm):
    """读桌面缓存文件得 (ids, meta)，解析规则与远端一致。"""
    if realm == "all":
        first = read_cached_remote_catalog("intl")
        second = read_cached_remote_catalog("cn")
        if not first:
            return second
        if not second:
            return first
        ids = list(first[0]) + [m for m in second[0] if m not in set(first[0])]
        meta = dict(second[1])
        meta.update(first[1])
        return ids, meta
    try:
        with open(product_config_path(realm), encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    return parse_remote_catalog(payload)

def catalog_snapshot_path(realm):
    return os.path.join(CATALOG_CACHE_DIR, "models-%s.json" % (realm or _gateway().CURRENT_REALM))

def persist_catalog_snapshot(realm, ids, meta):
    try:
        os.makedirs(CATALOG_CACHE_DIR, exist_ok=True)
        path = catalog_snapshot_path(realm)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"ids": list(ids), "meta": meta or {}}, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        _gateway().log("catalog snapshot: %s persist failed (%s)" % (realm, exc))

def read_gateway_catalog(realm):
    """读网关自己最近一次成功的 /v3/config 副本，得 (ids, meta)。"""
    try:
        with open(catalog_snapshot_path(realm), encoding="utf-8") as fh:
            snapshot = json.load(fh)
        ids = snapshot.get("ids")
        meta = snapshot.get("meta")
        if isinstance(ids, list) and isinstance(meta, dict):
            return ids, meta
    except Exception:
        pass
    try:
        with open(catalog_cache_path(realm), encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    return parse_remote_catalog(payload)

def gateway_catalog_age(realm):
    path = catalog_snapshot_path(realm)
    if not os.path.exists(path):
        path = catalog_cache_path(realm)
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None

CATALOG_CACHE_TTL = 24 * 3600

def read_catalog_cache():
    """落盘目录存档的兼容视图。"""
    out = {"realms": {}}
    for realm in ("intl", "cn"):
        age = gateway_catalog_age(realm)
        if age is None:
            continue
        out["realms"][realm] = {"fetched_at": time.time() - age}
    return out

def write_catalog_cache(data):
    """套用补丁后的存档视图，测试用它把副本做旧。"""
    realms = (data or {}).get("realms") or {}
    for realm, item in realms.items():
        path = catalog_snapshot_path(realm)
        if not os.path.exists(path):
            path = catalog_cache_path(realm)
        if not os.path.exists(path):
            continue
        try:
            stamp = float((item or {}).get("fetched_at"))
            os.utime(path, (stamp, stamp))
        except (TypeError, ValueError, OSError):
            continue

def curated_live_sources(realm, force=False):
    """该 realm 活目录的 (entries, extras, source)，已按选择器规则裁剪。

    先远端后桌面缓存文件，缓存就是同一份响应落盘，两者走同一解析与规则。
    extras 表示条目来自桌面目录，其成员可能带裁剪表没见过的模型；legacy 读取
    保持旧白名单语义。source 标明批次出处：remote 为上游 /v3/config，cache 为
    同机桌面缓存文件，legacy 为同一文件的宽松解析，面板据此把「通过 API 获取」
    与「读取配置文件」分开说；None 表示哪里都没拿到。
    """
    gateway = read_gateway_catalog(realm)
    age = gateway_catalog_age(realm)
    if gateway and not force and age is not None and age < CATALOG_CACHE_TTL:
        ids, meta = gateway
        _catalog_raw[realm] = list(ids)
        _catalog_raw_meta[realm] = dict(meta or {})
        return ([(mid, meta.get(mid) or {})
                for mid in curate_remote_catalog(realm, ids, meta)], True, "cache")
    remote = None
    try:
        remote = fetch_remote_product_config(realm)
    except Exception as exc:
        _gateway().log("remote catalog: %s failed (%s)" % (realm, exc))
    if remote:
        ids, meta = remote
        _catalog_raw[realm] = list(ids)
        _catalog_raw_meta[realm] = dict(meta or {})
        persist_catalog_snapshot(realm, ids, meta)
        return ([(mid, meta.get(mid) or {})
                for mid in curate_remote_catalog(realm, ids, meta)], True, "remote")
    if gateway:
        ids, meta = gateway
        _catalog_raw[realm] = list(ids)
        _catalog_raw_meta[realm] = dict(meta or {})
        return ([(mid, meta.get(mid) or {})
                for mid in curate_remote_catalog(realm, ids, meta)], True, "cache")
    cached = read_cached_remote_catalog(realm)
    if cached:
        ids, meta = cached
        _catalog_raw[realm] = list(ids)
        _catalog_raw_meta[realm] = dict(meta or {})
        return ([(mid, meta.get(mid) or {})
                for mid in curate_remote_catalog(realm, ids, meta)], True, "cache")
    legacy = read_product_config_models(realm=realm)
    if legacy:
        ids = [mid for mid, _ in legacy]
        _catalog_raw[realm] = list(ids)
        meta = dict((mid, m) for mid, m in legacy if isinstance(m, dict))
        keep = set(curate_remote_catalog(realm, ids, meta))
        return ([(mid, m) for mid, m in legacy if mid in keep], False, "legacy")
    return [], False, None

def fetch_endpoint_models():
    account = _gateway().POOL.pick(realm="intl") if _gateway().POOL else None
    if account is None:
        _gateway().log("model discovery skipped: no usable account")
        cached = _models_cache.get("intl", {}).get("data")
        return [m for m, _ in (cached or [])]
    req = wb_upstream_pool.Request(UPSTREAM + MODELS_PATH, method="GET", headers=account.headers())
    try:
        with wb_accounts.urlopen(req, timeout=30, proxy=account.proxy) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _gateway().log(f"model discovery failed: {exc}")
        cached = _models_cache.get("intl", {}).get("data")
        return [m for m, _ in (cached or [])]
    ids, seen = [], set()
    for agent in (payload.get("data") or {}).get("agents") or []:
        for mid in agent.get("models") or []:
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ids
