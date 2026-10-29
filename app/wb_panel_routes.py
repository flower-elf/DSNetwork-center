"""面板与管理接口的路由处理方法，以 PanelRoutes 混入 Handler；运行态与各域能力经 _g() 取用。"""
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse

import wb_accounts
import wb_activity
import wb_atrest
import wb_catalog
import wb_identity
import wb_modelsdev
import wb_pricing
import wb_proxy_slots
import wb_settings
import wb_tasks
import wb_updates
import wb_upstream_pool
import wb_usage
import wb_usage_perf
import wb_usage_recent
import wb_usage_trend
from urllib.parse import unquote

from wb_models import (
    _catalog_excluded, _catalog_raw_meta, catalog_meta, fetch_models, model_entry,
    read_cached_remote_catalog, refresh_catalog, seed_default_disabled,
)


def _g():
    """网关模块；与 wb_http._g 同一对象。函数内导入避免与 wb_http 形成导入环。"""
    import wb_http
    return wb_http._g()


# ---------------------------------------------------------------------------
# 面板登录限流与可信代理判定
# ---------------------------------------------------------------------------
_login_attempts = {}  # ip -> 时间戳列表
_login_lock = threading.Lock()


def _prune_login_attempts(now=None, window=60):
    """清理过期的按 IP 记录，避免字典无界增长。
    调用方必须持有 _login_lock。
    """
    now = now or time.time()
    for ip in list(_login_attempts.keys()):
        recent = [t for t in _login_attempts[ip] if now - t < window]
        if recent:
            _login_attempts[ip] = recent
        else:
            del _login_attempts[ip]


# 面板登录限流按客户端 IP 分桶，每个 IP 60 秒内允许 5 次失败。
# 反向代理部署下 socket 对端不是真实客户端：路由器上的 nginx 转发到
# 127.0.0.1:8788 时，所有请求的对端都是 127.0.0.1。分桶键要取真实来源，
# 且只在对端可信时读取代理头：对端是回环地址或显式列入 WB_TRUSTED_PROXIES 时，
# 按 X-Real-IP 优先，其次取 X-Forwarded-For 的第一个地址；其余对端忽略这两个
# 头，仍按对端地址分桶，公网客户端无法伪造头绕过限流。
def parse_trusted_proxies(raw):
    """解析 WB_TRUSTED_PROXIES，逗号分隔的 IP 或 CIDR，例如 10.0.0.1,fd00::/8。
    无法解析的条目直接丢弃，拼写错误不应阻止网关启动。
    """
    entries = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            entries.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(entries)


# 保留模块级覆盖给测试与脚本；None 表示跟随面板设置。
TRUSTED_PROXIES = None


def trusted_proxy_networks():
    if TRUSTED_PROXIES is not None:
        return TRUSTED_PROXIES
    return parse_trusted_proxies(wb_settings.trusted_proxies(_g().ACCOUNTS_DIR))


def peer_is_trusted(peer_ip):
    """对端是否有资格代表客户端：回环地址或显式配置的代理。"""
    try:
        addr = ipaddress.ip_address(str(peer_ip))
    except ValueError:
        return False
    # Windows 双栈下连 127.0.0.1 的对端显示为 ::ffff:127.0.0.1，Python 3.13 之前的 ipaddress
    # 不把 IPv4 映射地址视为回环；不特判的话本机反向代理被当成不可信来源，所有客户端共用一个限流桶。
    mapped = addr.ipv4_mapped if addr.version == 6 else None
    if addr.is_loopback or (mapped is not None and mapped.is_loopback):
        return True
    for net in trusted_proxy_networks():
        if addr.version == net.version and addr in net:
            return True
    return False


_PROXY_HEADER_IP_MAX = 64  # 最长的 IPv6 文本也不到 46 字符，再长就是垃圾


def _proxy_header_ip(value):
    """从代理头取出合法 IP 的规范文本，取不到返回 None，不抛异常。接受裸 IPv4/IPv6 与
    nginx 可能写出的 host:port、[v6]:port；含逗号的多值、超长及其他畸形串一律判为不可用。"""
    text = str(value or "").strip()
    if not text or len(text) > _PROXY_HEADER_IP_MAX or "," in text:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return None
        text = text[1:end]
    elif text.count(":") == 1:
        # v4:port 形式；裸 IPv6 至少两个冒号，不会被这里截断
        host, _, port = text.partition(":")
        if port.isdigit():
            text = host
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def login_rate_limit_key(peer_ip, headers):
    """限流键：对端可信时取代理头里的真实客户端 IP，否则取对端地址。X-Real-IP 优先
    （nginx 里放的就是真实来源），其次取 X-Forwarded-For 链上最左的第一个地址；头不可用
    时回退到对端地址，不能拿解析不出来的串当键。"""
    if not peer_is_trusted(peer_ip):
        return peer_ip
    real = _proxy_header_ip(headers.get("X-Real-IP") if headers else None)
    if real:
        return real
    forwarded = (headers.get("X-Forwarded-For") if headers else None) or ""
    first = forwarded.split(",")[0]
    via_chain = _proxy_header_ip(first)
    if via_chain:
        return via_chain
    return peer_ip


# ---------------------------------------------------------------------------
# 账号测试探针
# ---------------------------------------------------------------------------
#: 一键测试的并发度：每个探针都是一次真实的上游请求，别把池子打成压测。
PROBE_CONCURRENCY = 4


def _is_public_addr(text):
    try:
        addr = ipaddress.ip_address(str(text).split("%")[0])
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def probe_local_network(realm):
    """本机到该区域上游这条路通不通：DNS 解析 + TCP 握手。一键测试里最容易被误判的就是
    这一项：本机一断网每个账号都会失败，用户看到的却是「账号全挂了」。目标是各区域写死的
    上游主机，只接受 https、拒绝解析到环回/私有段的结果——配置被改坏时宁可报错。"""
    url = str(wb_accounts.get_realm_config(realm).get("chat_upstream") or "")
    info = {"realm": realm, "url": url, "host": "", "ip": "", "ok": False,
            "dns_ms": None, "tcp_ms": None, "error": ""}
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        info["error"] = "上游地址必须是 https + 主机名：%s" % url
        return info
    host = parsed.hostname
    port = parsed.port or 443
    info["host"] = host
    t0 = time.time()
    try:
        addrinfos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        info["error"] = "DNS 解析失败：%s" % _g().exc_detail(exc)
        return info
    info["dns_ms"] = int((time.time() - t0) * 1000)
    for family, socktype, proto, _canon, sockaddr in addrinfos:
        if not _is_public_addr(sockaddr[0]):
            info["error"] = "解析到非公网地址 %s，已拒绝连接" % sockaddr[0]
            return info
        info["ip"] = str(sockaddr[0])
        t1 = time.time()
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(5)
                sock.connect(sockaddr)
        except Exception as exc:
            info["error"] = "TCP 连接失败：%s" % _g().exc_detail(exc)
            return info
        info["tcp_ms"] = int((time.time() - t1) * 1000)
        info["ok"] = True
        return info
    info["error"] = "没有可用的解析结果"
    return info


def probe_account(account, model=None, timeout=30):
    """给一个账号发一条最小请求，返回一行结果；成功即清掉它的本地惩罚。单个「测试」与
    「一键测试」共用这一份，清计数与识别本机断网的行为保持一致。"""
    model = model or wb_settings.DEFAULT_TEST_MODEL
    row = {"uid": account.uid, "nickname": account.nickname or account.uid[:8],
           "realm": account.realm, "model": model, "ok": False, "elapsed_ms": 0,
           "status": None, "reply": "", "error": "", "network": False}
    chat_url = account.chat_base_url() + _g().CHAT_PATH
    test_body = {"model": model, "messages": [{"role": "user", "content": "hi"}],
                 "stream": False}
    upstream_body = _g().build_upstream_body(test_body)
    body = json.dumps(upstream_body, ensure_ascii=False).encode("utf-8")
    req = wb_upstream_pool.Request(
        chat_url, data=body, method="POST",
        headers=account.headers(purpose="chat",
                                model=upstream_body.get("model") or model))
    t0 = time.time()
    try:
        with wb_accounts.urlopen(req, timeout=timeout, proxy=account.proxy) as resp:
            chat_obj = _g().aggregate_stream(resp, model, None)
            row["elapsed_ms"] = int((time.time() - t0) * 1000)
            choices = chat_obj.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices else {}
            reply = (msg.get("content") or msg.get("reasoning_content") or "OK").strip()
            row["reply"] = reply[:77] + "..." if len(reply) > 80 else reply
            # 请求真的被服务了，走语义成功路径：清掉账号级连续失败、熔断与降权计数。只解除
            # 被测模型的冷却，402 余额停用另有解除路径（积分刷新触发 revive_balance_cooldown）。
            account.note_success(model=model)
            row["ok"] = True
            return row
    except urllib.error.HTTPError as exc:
        row["elapsed_ms"] = int((time.time() - t0) * 1000)
        try:
            detail = _g().read_http_error(exc, 400).decode("utf-8", "replace")
        except Exception:
            detail = str(exc)
        if _g().looks_like_local_network(exc):
            row["network"] = True
            row["error"] = "本机网络不可用：%s" % _g().exc_detail(exc)
            account.note_error(row["error"], cooldown=0)
            return row
        # 面板标签保留 80 字符截断；detail 原样带上，供面板悬停时显示完整上游响应。
        row["status"] = exc.code
        row["error"] = "HTTP %d: %s" % (exc.code, detail[:150])
        account.note_error("HTTP %d: %s" % (exc.code, detail[:80]),
                           cooldown=60, detail=detail)
        row["fails"] = int(account.fails)
        return row
    except Exception as exc:
        row["elapsed_ms"] = int((time.time() - t0) * 1000)
        if _g().looks_like_local_network(exc):
            # 断网时上游一个字节都没收到：只报「本机网络不可用」，不给账号记冷却，
            # 否则网络一恢复，池子里全是刚才这轮测试留下的冷却账号。
            row["network"] = True
            row["error"] = "本机网络不可用：%s" % _g().exc_detail(exc)
            account.note_error(row["error"], cooldown=0)
            return row
        row["error"] = "%s: %s" % (type(exc).__name__, exc)
        # 测试是诊断动作：失败要看得见（last_error），但不停用账号——真有问题，
        # 真实流量会照常记账；测试顺手把账号关掉才是「越测越乱」。
        account.note_error(str(exc)[:80], cooldown=0)
        row["fails"] = int(account.fails)
        return row


class PanelRoutes(object):
    def _get_dashboard(self):
        return self._dashboard()

    def _get_panel_status(self):
        # 不带 token 也作答：看板要先知道是否显示登录页，才谈得上持有会话。
        info = {
            "panel_password_required": True,
            "panel_password_is_default": wb_settings.panel_password_is_default(_g().ACCOUNTS_DIR),
            "authenticated": self._panel_ok(),
        }
        # key 是否存在不算秘密；值不出进程，设置接口只报掩码形式。
        info["api_key_set"] = bool(_g().API_KEY)
        if self._panel_ok():
            info["accounts"] = len(_g().POOL.accounts) if _g().POOL else 0
            info["accounts_ready"] = _g().POOL.count_ready() if _g().POOL else 0
            info["stand_down"] = _g().stand_down_state()
        return self._json(200, info)

    def _get_console_state(self):
        return self._json(200, {
            "supported": True,
            "visible": _g()._console_viewer_running(),
        })

    def _route_console_toggle(self):
        # 按钮请求带 `{}` body 但不解析，必须排空残留字节，否则同一条 keep-alive 连接上的
        # 下一个请求会以 "{}POST /console/toggle" 进场 → 501。先应对 Expect: 100-continue。
        self._handle_expect_continue()
        self._discard_body()
        if _g()._console_viewer_running():
            _g()._kill_console_viewer()
            visible = False
        else:
            launched = _g()._launch_console_viewer()
            visible = bool(launched)
        return self._json(200, {"ok": True, "visible": visible})

    def _route_shutdown(self):
        # 同 toggle：`{}` body 不解析也必须排空，否则污染同一条 keep-alive
        # 连接上的下一个请求（连点触发 501）。
        self._handle_expect_continue()
        self._discard_body()
        # 先关掉日志终端窗口，避免服务退出后查看器轮询 /logs 报「上游拒绝连接」；回响应后
        # 延迟 0.4s 退出，让 _json 的 body 先 flush 到 socket，端口随进程释放。
        try:
            _g()._kill_console_viewer()
        except Exception:
            pass
        def _bye():
            time.sleep(0.4)
            try:
                sys.stdout.flush()
            except Exception:
                pass
            os._exit(0)
        threading.Thread(target=_bye, daemon=True).start()
        return self._json(200, {"ok": True, "shutting_down": True})

    def _get_panel_models(self, query):
        """面板的模型表：可用模型 + 上游列出但被筛选规则排除的模型。条目带 enabled 与
        excluded；被排除的模型只在此列出，不自动写进 disabled_models，操作员明确取消勾选后
        网关才拒绝它的请求。读取本页不改变路由状态。"""
        realm = str((query.get("realm") or [""])[0]).strip() or _g().CURRENT_REALM
        refresh = str((query.get("refresh") or [""])[0]).strip().lower() in (
            "1", "true", "yes", "on")
        refresh_result = None
        if refresh:
            refresh_result = refresh_catalog(realm)
        entries = fetch_models(realm=realm)
        info = catalog_meta(realm) or {}
        served = {mid for mid, _ in entries}
        saved = read_cached_remote_catalog(realm) or ([], {})
        extra = list(_catalog_excluded.get(realm) or [])
        for mid in saved[0]:
            if mid not in served and mid not in extra:
                extra.append(mid)
        seed_default_disabled(extra)
        disabled = {m.strip().lower() for m in wb_settings.disabled_models(_g().ACCOUNTS_DIR)}
        raw_meta = _catalog_raw_meta.get(realm) or {}
        data = []
        for mid, meta in list(entries) + [
                (m, raw_meta.get(m) or saved[1].get(m) or {}) for m in extra]:
            item = model_entry(mid, meta)
            item["enabled"] = mid.strip().lower() not in disabled
            item["excluded"] = mid not in served
            data.append(item)
        source = info.get("source")
        source_label = {"remote": "server", "endpoint": "server",
                        "cache": "cache", "legacy": "cache"}.get(source, source)
        reply = {"object": "list", "data": data, "realm": realm,
                 "source": source_label,
                 "fetched_at": info.get("fetched_at")}
        if refresh_result is not None:
            reply["refresh_ok"] = bool(refresh_result.get("ok"))
            reply["refresh_msg"] = refresh_result.get("msg") or ""
        return self._json(200, reply)

    def _get_v1_usage(self, query):
        if not self._authorized():
            return
        req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or _g().CURRENT_REALM
        req_range, req_since, req_until = wb_usage.range_query(query)
        # 取数与取校验符必须看到同一套参数，否则 tag 落到另一条缓存条目上；
        # 「先取戳、再取数、再取一次戳」的配对顺序由 _json_cached 保证。
        return self._json_cached(
            200,
            lambda: wb_usage.usage_snapshot(realm=req_realm, range=req_range,
                                   since=req_since, until=req_until),
            lambda: wb_usage.usage_snapshot_etag(realm=req_realm, range=req_range,
                                        since=req_since, until=req_until))

    def _get_usage_recent(self, query):
        if not self._authorized():
            return
        try:
            limit = max(1, min(1000, int((query.get("limit") or ["100"])[0])))
        except ValueError:
            limit = 100
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
        except ValueError:
            page = 1
        req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or _g().CURRENT_REALM
        req_range, req_since, req_until = wb_usage.range_query(query)
        since, until = wb_usage.range_window(req_range, req_since, req_until)
        filters = {}
        for name in ("key", "status", "model", "account", "ip"):
            value = (query.get(name) or [""])[0].strip()
            if value:
                filters[name] = value.lower() if name == "status" else value
        if since is not None:
            filters["since"] = since
        if until is not None:
            filters["until"] = until
        if not filters:
            filters = None
        return self._json(200, wb_usage_recent.recent_usage(limit, realm=req_realm, page=page,
                                            filters=filters))

    def _get_usage_trend(self, query):
        """调用趋势：返回零填充的请求、Token 与积分桶。"""
        if not self._authorized():
            return
        req_realm = (query.get('realm', [None])[0]
                     or getattr(self, "headers", {}).get('X-Realm')
                     or _g().CURRENT_REALM)
        req_range, req_since, req_until = wb_usage.range_query(query)
        raw_days = (query.get("days") or [""])[0]
        try:
            days = int(raw_days) if raw_days != "" else None
        except (TypeError, ValueError):
            days = None
        return self._json(200, wb_usage_trend.usage_trend(days=days, realm=req_realm,
                                           range=req_range, since=req_since,
                                           until=req_until))

    def _get_accounts_credit_events(self, query):
        if not self._authorized():
            return
        realm = query.get("realm", [None])[0]
        if realm is None:
            realm = _g().CURRENT_REALM
        try:
            limit = int((query.get("limit") or ["20"])[0])
        except (TypeError, ValueError):
            limit = 20
        try:
            offset = int((query.get("offset") or ["0"])[0])
        except (TypeError, ValueError):
            offset = 0
        return self._json(200, wb_usage.read_credit_events(realm=realm, limit=limit,
                                                  offset=offset))

    def _get_activity_history(self, query):
        """账号每日活动的结构化历史，只读。筛选条件写错返回 400，不静默忽略：被忽略的
        result=failed 会让调用方以为「这几天没有失败」，实际拿回的是全部结果。"""
        if not self._authorized():
            return

        def first(name):
            values = query.get(name) or [""]
            return (values[0] if values else "") or ""

        try:
            payload = wb_activity.query(range_key=first("range") or None,
                                        uid=first("uid"),
                                        task=first("task"),
                                        result=first("result"),
                                        limit=first("limit") or None)
        except ValueError as exc:
            return self._error(400, str(exc), "invalid_request_error")
        return self._json(200, payload)

    def _refresh_account_credits(self, targets):
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.fetch_credits()
            results.append({"uid": account.uid, "ok": res.get("ok", False),
                            "credits": account.credits,
                            "error": res.get("error", "")})
        return results

    def _get_accounts_credits(self):
        if not self._authorized():
            return
        if _g().stand_down_active():
            return self._error(503, _g().stand_down_message(_g().GatewayStandDown(
                _g().STAND_DOWN.get("detail") or "")), "overloaded_error")
        self._refresh_account_credits(_g().POOL.accounts if _g().POOL else [])
        return self._json(200, {"accounts": wb_accounts.account_views()})

    def _get_account_credits_grants(self):
        """积分获取历史：只读账号积分快照，不发上游请求。更新数字先走「一键刷新积分」
        （POST /accounts/credits）；这里只把最近一次快照里的积分包摊平成表，载荷带快照
        时刻，面板据此提醒数据有多旧。"""
        if not self._authorized():
            return
        return self._json(200, wb_usage.credit_grants())

    def _get_accounts(self, query):
        if not self._authorized():
            return
        # 建视图前先折叠用量日志，日限额徽标与停用计数才是此刻的值，不是上一次请求时的。
        wb_usage.apply_daily_guards()
        return self._json(200, {
            "accounts": wb_accounts.account_views(realm=query.get('realm', [None])[0] or _g().CURRENT_REALM),
            "storage": _g().ACCOUNTS_DIR,
            "usable": _g().POOL.count_ready() if _g().POOL else 0,
            "limits": wb_accounts.account_limits(),
            "stand_down": _g().stand_down_state(),
        })

    def _get_accounts_export(self, query):
        if not self._authorized():
            return
        # ?download=1 让浏览器存成文件；不带则内联返回文档，看板可展示摘要。
        # ?uid= 限定到指定账号（可重复、逗号拼接），按行导出时使用。
        realm = (query.get("realm") or [None])[0] or None
        if realm not in ("intl", "cn"):
            realm = None
        include_secrets = (query.get("secrets") or ["1"])[0] not in ("0", "false", "no")
        uids = []
        for raw in query.get("uid") or []:
            uids.extend(part.strip() for part in str(raw).split(",") if part.strip())
        if uids:
            known = {a.uid for a in (_g().POOL.accounts if _g().POOL else [])}
            missing = [u for u in uids if u not in known]
            if missing:
                return self._error(404, "no such account: %s" % ", ".join(missing[:5]),
                                   "invalid_request_error")
        doc = wb_accounts.build_export_document(
            _g().POOL.accounts if _g().POOL else [],
            realm=realm,
            include_secrets=include_secrets,
            uids=uids or None,
        )
        if (query.get("download") or ["0"])[0] in ("1", "true", "yes"):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            if len(uids) == 1:
                # 单账号导出以账号 id 命名，一堆导出文件放一个目录也认得出来。
                label = uids[0][:8]
            else:
                label = realm + "-" if realm else ""
            name = "workbuddy-accounts-%s%s.json" % (label, stamp)
            return self._download(name, doc)
        return self._json(200, doc)

    def _get_accounts_login_poll(self, query):
        if not self._authorized():
            return
        if _g().stand_down_active():
            return self._error(503, _g().stand_down_message(_g().GatewayStandDown(
                _g().STAND_DOWN.get("detail") or "")), "overloaded_error")
        state = (query.get("state") or [""])[0]
        return self._json(200, _g().POOL.poll_login(state))

    def _get_usage_analytics(self, query):
        if not self._authorized():
            return
        req_realm = query.get("realm", [None])[0] or None
        req_range, req_since, req_until = wb_usage.range_query(query)
        return self._json_cached(
            200,
            lambda: wb_usage.compute_usage_analytics(realm=req_realm, range=req_range,
                                            since=req_since, until=req_until),
            lambda: wb_usage.usage_analytics_etag(realm=req_realm, range=req_range,
                                         since=req_since, until=req_until))

    def _get_usage_cost_estimate(self, query):
        if not self._authorized():
            return
        req_realm = query.get("realm", [None])[0] or None
        req_range, req_since, req_until = wb_usage.range_query(query)
        return self._json(200, wb_usage.cost_estimate_view(
            realm=req_realm, range=req_range, since=req_since, until=req_until))

    def _get_usage_month_credit(self, query):
        if not self._authorized():
            return
        req_realm = query.get("realm", [None])[0] or None
        return self._json_cached(
            200,
            lambda: wb_usage.month_credit_view(realm=req_realm),
            lambda: wb_usage.month_credit_etag(realm=req_realm))

    def _get_usage_by_account(self):
        if not self._authorized():
            return
        return self._json_cached(200,
                                 lambda: {"accounts": wb_usage.usage_by_account()},
                                 wb_usage.usage_by_account_etag)

    def _get_usage_remaining(self):
        if not self._authorized():
            return
        # 载荷挂短 TTL（WB_REMAINING_TTL，默认 15 秒）+ ETag：面板 5 秒一轮的
        # 轮询在 TTL 内命中同一条缓存 → 校验符不变 → 304，重建与重传都省掉。
        return self._json_cached(200, wb_usage.remaining_usage, wb_usage.remaining_usage_etag)

    def _get_usage_timeseries(self, query):
        if not self._authorized():
            return
        req_realm = (query.get('realm', [None])[0]
                     or self.headers.get('X-Realm') or _g().CURRENT_REALM)
        req_range, req_since, req_until = wb_usage.range_query(query)
        bucket = (query.get("bucket") or [None])[0]
        try:
            bucket_seconds = int(bucket) if bucket else None
        except (TypeError, ValueError):
            bucket_seconds = None
        return self._json_cached(
            200,
            lambda: wb_usage.usage_timeseries(realm=req_realm, range=req_range,
                                     since=req_since, until=req_until,
                                     bucket_seconds=bucket_seconds),
            lambda: wb_usage.usage_timeseries_etag(realm=req_realm, range=req_range,
                                          since=req_since, until=req_until,
                                          bucket_seconds=bucket_seconds))

    def _get_usage_perf(self, query):
        if not self._authorized():
            return
        try:
            sample = max(10, min(20000, int((query.get("sample") or ["5000"])[0])))
        except ValueError:
            sample = 5000
        req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or _g().CURRENT_REALM
        req_range, req_since, req_until = wb_usage.range_query(query)
        return self._json_cached(
            200,
            lambda: wb_usage_perf.perf_stats(sample, realm=req_realm, range=req_range,
                               since=req_since, until=req_until),
            lambda: wb_usage_perf.perf_stats_etag(sample, realm=req_realm, range=req_range,
                                    since=req_since, until=req_until))

    def _get_tasks(self, query):
        if not self._authorized():
            return
        cn_accounts = [a for a in (_g().POOL.accounts if _g().POOL else []) if a.realm == "cn" and a.enabled]
        if not cn_accounts:
            return self._json(200, {"tasks": [], "summary": {}, "accounts": [], "msg": "未找到可用的国内版账号"})
        uid = (query.get("uid") or [None])[0]
        acc = None
        if uid and uid != "all":
            target = _g().POOL.get(uid) if _g().POOL else None
            if target and target.realm == "cn":
                acc = target
        if not acc:
            acc = cn_accounts[0]
        tasks, summary = wb_tasks.growth_snapshot(acc)
        acct_list = [{"uid": a.uid, "nickname": a.nickname or a.uid[:8]} for a in cn_accounts]
        return self._json(200, {
            "tasks": tasks,
            "summary": summary,
            "account": acc.public(),
            "accounts": acct_list,
        })

    def _get_scheduler(self):
        if not self._authorized():
            return
        return self._json(200, _g().SCHEDULER.status() if _g().SCHEDULER else {"enabled": False, "msg": "未运行"})

    def _get_pricing(self):
        if not self._authorized():
            return
        if _g().PRICING:
            return self._json(200, _g().PRICING.status())
        return self._json(200, {
            "interval_minutes": 0.0, "enabled": False, "running": False,
            "master_enabled": wb_settings.pricing_enabled(_g().ACCOUNTS_DIR),
            "policies": 0, "models": 0, "current": {}, "logs": [],
            "gaps": [], "gap_summary": {"total": 0, "or_missing": 0,
                                        "variant_unmatched": 0, "aliases": 0,
                                        "variants_enabled":
                                            wb_settings.pricing_variant_inherit(_g().ACCOUNTS_DIR)},
            "variant_inherit": wb_settings.pricing_variant_inherit(_g().ACCOUNTS_DIR),
            "overrides_file": wb_pricing.overrides_path(),
            "policies_file": wb_pricing.policies_path(),
            "timeline": wb_pricing.timeline_path(), "msg": "未运行",
        })

    def _get_settings(self):
        if not self._authorized():
            return
        return self._json(200, _g().runtime_settings_view())

    def _get_updates(self):
        """当前构建版本，以及是否存在更新的稳定版。只读且便宜：只汇报上一次检查的结果，
        不发起检查；看板的「立即检查」是下面的 POST。"""
        if not self._authorized():
            return
        if _g().UPDATES:
            return self._json(200, _g().UPDATES.status())
        return self._json(200, {
            "current_version": _g().running_version(),
            "latest_version": None, "update_available": False,
            "enabled": wb_settings.update_check_enabled(_g().ACCOUNTS_DIR),
            "interval_hours": wb_settings.update_check_interval_hours(_g().ACCOUNTS_DIR),
            "checking": False, "last_attempt": None, "last_success": None,
            "last_error": "", "release_url": "",
            "releases_url": wb_updates.RELEASES_PAGE,
            "published_at": "",
            "msg": "更新检查未运行",
        })

    def _get_logs(self, query):
        if not self._authorized():
            return
        try:
            limit = int(query.get("limit", ["200"])[0])
        except (ValueError, TypeError):
            limit = 200
        level = query.get("level", [""])[0]
        tag = query.get("tag", [""])[0]
        search = query.get("search", [""])[0]
        try:
            since_id = int(query.get("since_id", ["0"])[0])
        except (ValueError, TypeError):
            since_id = 0
        result = _g().get_logs(limit=limit, level=level, tag=tag, search=search, since_id=since_id)
        # 面板据 mode 决定顶部文案（内存保留与数据库保留）与是否显示库容量，
        # 所以把模式与库统计一并带回；统计失败不让整个日志请求失败。
        result["mode"] = "db" if _g()._log_db_active() else "memory"
        if _g()._log_db_active():
            try:
                stats = _g().LOG_STORE.stats() or {}
            except Exception:
                stats = {}
            result["stored"] = stats.get("rows", 0)
            result["db_bytes"] = stats.get("bytes", 0)
        return self._json(200, result)

    def _get_logs_export(self):
        if not self._authorized():
            return
        log_data = _g().get_logs(limit=5000)
        lines = [f"[{item['ts']}] [{item['level']}] [{item['tag']}] {item['msg']}" for item in log_data["logs"]]
        text_content = "\n".join(lines).encode("utf-8")
        filename = f"wb-proxy-{time.strftime('%Y%m%d-%H%M%S')}.log"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(text_content)))
        if _g().cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(text_content)
        return

    def _get_settings_reveal(self, query):
        # 面板只画掩码 key，复制必须显式请求；只认面板会话，持 API Key 不够。
        if not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        wanted = (query.get("id") or [""])[0]
        for entry in _g().configured_keys():
            if entry.get("id") == wanted:
                return self._json(200, {"id": wanted, "key": entry.get("key") or ""})
        return self._error(404, "no such key", "invalid_request_error")

    def _get_web_asset(self, relative):
        root = os.path.realpath(_g().WEB_DIR)
        full = os.path.realpath(os.path.normpath(
            os.path.join(root, unquote(relative))))
        try:
            inside = os.path.commonpath([full, root]) == root
        except ValueError:
            inside = False
        if not inside:
            return self._error(404, "not found", "invalid_request_error")
        return self._send_web_file(full)

    def _send_web_file(self, full):
        """静态文件按 ETag 协商缓存：浏览器每次都校验，文件没变时只回 304。"""
        content_type = _g().WEB_CONTENT_TYPES.get(os.path.splitext(full)[1].lower())
        if content_type is None or not os.path.isfile(full):
            return self._error(404, "not found", "invalid_request_error")
        stat = os.stat(full)
        etag = '"%x-%x"' % (stat.st_mtime_ns, stat.st_size)
        if _g()._if_none_match_hit(self.headers.get("If-None-Match") or "", etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _dashboard(self):
        return self._send_web_file(_g().WEB_INDEX)

    def _handle_settings_save(self):
        payload = self._payload_or_error()
        if payload is None:
            return
        reply = {}
        if "api_keys" in payload:
            raw = payload.get("api_keys")
            if not isinstance(raw, list):
                return self._error(400, "api_keys must be a list", "invalid_request_error")
            # 面板只显示掩码 key，行里的空值表示保留已存值，不表示清空。
            existing = {entry.get("id"): entry for entry in _g().configured_keys()}
            # 带显式 delete 列表的提交按 upsert 处理，只退役列出的 id，过期或不完整的列表
            # 才不会抹掉用户没删的 key；旧面板不带该字段，维持按省略替换的契约。
            delete_ids = payload.get("deleted_api_key_ids")
            upsert = isinstance(delete_ids, list)
            cleaned = []
            for item in raw:
                if not isinstance(item, dict):
                    return self._error(400, "each api key must be an object",
                                       "invalid_request_error")
                entry_id = str(item.get("id") or "").strip()
                stored = existing.get(entry_id) or {}
                value = str(item.get("key") or "").strip()
                if not value and entry_id and entry_id in existing:
                    value = existing[entry_id].get("key") or ""
                # 新行 id 留空，由 wb_settings 写入时随机生成；按位置推导会复用
                # 已删行的 id，两行共用 id 会让 /settings/reveal 取错密钥。
                if value and len(value) < 4:
                    return self._error(400, "api key must be at least 4 characters",
                                       "invalid_request_error")
                if not value:
                    return self._error(400, "a key entry is empty - fill it in or remove the row",
                                       "invalid_request_error")
                # 提交里省略的字段保留已存值，与 models 同一规则：旧的或残缺的
                # 客户端不得静默清掉出口绑定、名称、禁用状态。
                if "realm" in item:
                    realm = str(item.get("realm") or "").strip().lower()
                else:
                    realm = str(stored.get("realm") or "").strip().lower()
                if realm not in ("", "intl", "cn"):
                    return self._error(400, "realm must be intl, cn or empty",
                                       "invalid_request_error")
                # 旧面板缓存页根本不知道这个字段，省略它就保留已存值，不悄悄丢掉限制。
                if "models" in item:
                    models = item.get("models")
                else:
                    models = stored.get("models")
                if "name" in item:
                    name = str(item.get("name") or "").strip()
                else:
                    name = str(stored.get("name") or "").strip()
                if "enabled" in item:
                    enabled = item.get("enabled", True) is not False
                else:
                    enabled = stored.get("enabled", True) is not False
                created_at = item.get("created_at") or (stored.get("created_at") if entry_id in existing else None) or time.strftime("%Y/%m/%d %H:%M")
                key_limits = {}
                for field, cleaner, default, label in (
                        ("concurrency_limit", wb_accounts.normalise_concurrency_limit,
                         stored.get("concurrency_limit") or 0, "concurrency_limit"),
                        ("expires_at", wb_settings.clean_epoch_field,
                         stored.get("expires_at") or 0, "expires_at"),
                        ("quota_tokens", wb_settings.clean_quota_tokens,
                         stored.get("quota_tokens") or 0, "quota_tokens"),
                        ("quota_credit", wb_settings.clean_quota_credit,
                         stored.get("quota_credit") or 0.0, "quota_credit"),
                        ("usage_reset_at", wb_settings.clean_epoch_field,
                         stored.get("usage_reset_at") or 0, "usage_reset_at")):
                    raw_value = item[field] if field in item else default
                    if field in ("expires_at", "usage_reset_at"):
                        cleaned_value, problem = cleaner(raw_value, label)
                    else:
                        cleaned_value, problem = cleaner(raw_value)
                    if problem:
                        return self._error(400, problem, "invalid_request_error")
                    key_limits[field] = cleaned_value
                if "ip_allowlist" in item:
                    allow_raw = item.get("ip_allowlist")
                else:
                    allow_raw = stored.get("ip_allowlist") or []
                allowlist, problem = wb_settings.clean_ip_allowlist(allow_raw)
                if problem:
                    return self._error(400, problem, "invalid_request_error")
                key_limits["ip_allowlist"] = allowlist
                cleaned.append({
                    "id": entry_id,
                    "name": name,
                    "key": value,
                    "realm": realm,
                    "models": models,
                    "enabled": enabled,
                    "created_at": created_at,
                    **key_limits,
                })
            wb_settings.set_api_keys(
                _g().ACCOUNTS_DIR, cleaned,
                delete_ids=(delete_ids if upsert else None),
            )
            reply["api_keys_saved"] = len(cleaned)
        if "reset_key_usage" in payload:
            key_id = str(payload.get("reset_key_usage") or "").strip()
            entries = wb_settings.api_keys(_g().ACCOUNTS_DIR)
            target = next((entry for entry in entries if entry.get("id") == key_id), None)
            if target is None:
                return self._error(404, "no such key", "invalid_request_error")
            target["usage_reset_at"] = int(time.time())
            wb_settings.set_api_keys(_g().ACCOUNTS_DIR, entries)
            reply["reset_key_usage"] = key_id
            reply["usage_reset_at"] = target["usage_reset_at"]
        if "auth_disabled" in payload:
            raw = payload.get("auth_disabled")
            if not isinstance(raw, bool):
                return self._error(400, "auth_disabled must be true or false",
                                   "invalid_request_error")
            wb_settings.set_auth_disabled(_g().ACCOUNTS_DIR, raw)
            reply["auth_disabled"] = raw
        if "smart_routing" in payload:
            raw = payload.get("smart_routing")
            if not isinstance(raw, bool):
                return self._error(400, "smart_routing must be true or false",
                                   "invalid_request_error")
            wb_settings.set_smart_routing(_g().ACCOUNTS_DIR, raw)
            reply["smart_routing"] = raw
            if _g().POOL:
                _g().POOL.apply_smart_routing(raw)
                wb_usage.apply_routing_inputs(refresh=True)
        if "session_affinity" in payload:
            raw = payload.get("session_affinity")
            if not isinstance(raw, bool):
                return self._error(400, "session_affinity must be true or false",
                                   "invalid_request_error")
            wb_settings.set_session_affinity(_g().ACCOUNTS_DIR, raw)
            reply["session_affinity"] = raw
        if "messages_format" in payload:
            raw = payload.get("messages_format")
            if raw not in wb_settings.MESSAGES_FORMATS:
                return self._error(
                    400, "messages_format must be openai-completions or openai-responses",
                    "invalid_request_error")
            reply["messages_format"] = wb_settings.set_messages_format(
                _g().ACCOUNTS_DIR, raw)
        for field, realm in (("test_model_intl", "intl"), ("test_model_cn", "cn")):
            if field not in payload:
                continue
            raw, problem = wb_settings.parse_test_model(payload.get(field))
            if problem:
                return self._error(400, problem, "invalid_request_error")
            reply[field] = wb_settings.set_test_model(_g().ACCOUNTS_DIR, raw, realm)
        if "disabled_models" in payload:
            raw = payload.get("disabled_models")
            if not isinstance(raw, list) or any(not isinstance(m, str) for m in raw):
                return self._error(400, "disabled_models must be a list of model ids",
                                   "invalid_request_error")
            reply["disabled_models"] = wb_settings.set_disabled_models(
                _g().ACCOUNTS_DIR, raw)

        # 四条限额既可以按面板的形状归在 "limits" 下，也可以走旧客户端还在发的
        # 顶层平铺键（按全局默认读取）；两者落进同一套分组存储，谁写都只有一个事实来源。
        limits_payload = payload.get("limits")
        if limits_payload is not None and not isinstance(limits_payload, dict):
            return self._error(400, "limits must be an object",
                               "invalid_request_error")
        touched = set()
        for key in wb_settings.LIMIT_KEYS:
            grouped = (limits_payload or {}).get(key)
            if isinstance(grouped, dict):
                scopes = {scope: grouped[scope]
                          for scope in wb_settings.LIMIT_SCOPES
                          if scope in grouped}
            elif key in payload:
                scopes = {"global": payload.get(key)}
            else:
                continue
            for scope, raw in scopes.items():
                if scope == "global":
                    if isinstance(raw, bool) or raw is None:
                        return self._error(400, "%s must be a whole number" % key,
                                           "invalid_request_error")
                    try:
                        number = int(raw)
                    except (TypeError, ValueError):
                        return self._error(400, "%s must be a whole number" % key,
                                           "invalid_request_error")
                    if number < 0:
                        return self._error(400, "%s cannot be negative" % key,
                                           "invalid_request_error")
                else:
                    # 空值把覆盖清回「继承全局」。
                    if raw is None or raw == "":
                        number = None
                    else:
                        if isinstance(raw, bool):
                            return self._error(
                                400, "%s must be a whole number" % key,
                                "invalid_request_error")
                        try:
                            number = int(raw)
                        except (TypeError, ValueError):
                            return self._error(
                                400, "%s must be a whole number" % key,
                                "invalid_request_error")
                        if number < 0:
                            return self._error(
                                400, "%s cannot be negative" % key,
                                "invalid_request_error")
                wb_settings.set_limit(_g().ACCOUNTS_DIR, key, scope, number)
                touched.add(key)
            reply[key] = wb_settings.limit_value(_g().ACCOUNTS_DIR, key)
        if limits_payload is not None:
            reply["limits"] = wb_settings.limits_snapshot(_g().ACCOUNTS_DIR)
        if touched:
            if _g().POOL and "reserve_credits" in touched:
                _g().POOL.apply_reserve_credits()
            if any(k in touched for k in ("daily_token_limit", "daily_credit_limit",
                                          "model_daily_token_limit")):
                # 一次折叠同时刷新三条：限额编辑不常有，守护也从同一次折叠发布。
                wb_usage.apply_daily_guards(refresh=True)
            if "expiring_window_days" in touched and _g().POOL:
                _g().POOL.apply_expiring_window()

        if "pricing_enabled" in payload:
            raw = payload.get("pricing_enabled")
            if not isinstance(raw, bool):
                return self._error(400, "pricing_enabled must be true or false",
                                   "invalid_request_error")
            wb_settings.set_pricing_enabled(_g().ACCOUNTS_DIR, raw)
            reply["pricing_enabled"] = raw
            if _g().PRICING:
                _g().PRICING.wake()
        if "cost_estimate" in payload:
            try:
                reply["cost_estimate"] = wb_settings.set_cost_estimate(
                    _g().ACCOUNTS_DIR, payload.get("cost_estimate"))
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
        if "pricing_refresh_minutes" in payload or "pricing_refresh_hours" in payload:
            # 间隔以分钟计；旧字段名仍接受（×60 折算），上一版缓存下来的面板页
            # 才不会设错单位；应答一律用新字段名。
            field = "pricing_refresh_minutes" \
                if "pricing_refresh_minutes" in payload else "pricing_refresh_hours"
            raw = payload.get(field)
            if isinstance(raw, bool) or raw is None:
                return self._error(400, "%s must be a number" % field,
                                   "invalid_request_error")
            try:
                minutes = float(raw)
            except (TypeError, ValueError):
                return self._error(400, "%s must be a number" % field,
                                   "invalid_request_error")
            if field == "pricing_refresh_hours":
                minutes *= 60.0
            if minutes < 0:
                return self._error(400, "pricing_refresh_minutes cannot be negative",
                                   "invalid_request_error")
            stored = wb_settings.set_pricing_refresh_minutes(_g().ACCOUNTS_DIR, minutes)
            if _g().PRICING:
                # 正在等待的线程当场取用新间隔。
                _g().PRICING.set_interval(stored)
            reply["pricing_refresh_minutes"] = stored
        if "credits_refresh_hours" in payload:
            # 积分余额允许多旧才由后台刷新器更新；0 表示关掉刷新器。
            raw = payload.get("credits_refresh_hours")
            if isinstance(raw, bool) or raw is None:
                return self._error(400, "credits_refresh_hours must be a number",
                                   "invalid_request_error")
            try:
                hours = float(raw)
            except (TypeError, ValueError):
                return self._error(400, "credits_refresh_hours must be a number",
                                   "invalid_request_error")
            if hours < 0:
                return self._error(400, "credits_refresh_hours cannot be negative",
                                   "invalid_request_error")
            reply["credits_refresh_hours"] = \
                wb_settings.set_credits_refresh_hours(_g().ACCOUNTS_DIR, hours)
            if _g().CREDITS_REFRESHER:
                _g().CREDITS_REFRESHER.wake()
        if "section_max_height" in payload:
            # 面板窗口高度上限（像素），0 表示不限。bool 是 int 子类必须显式拒绝，
            # 否则 true 会存成 1，把每个窗口压成一条缝。
            raw = payload.get("section_max_height")
            if isinstance(raw, bool) or raw is None:
                return self._error(400, "section_max_height must be a whole number",
                                   "invalid_request_error")
            try:
                number = int(raw)
            except (TypeError, ValueError, OverflowError):
                # JSON 载荷可能带 Infinity（1e400），int() 拒绝，回 400 而不是 500。
                return self._error(400, "section_max_height must be a whole number",
                                   "invalid_request_error")
            # 小数拒绝而非截断：int(-0.5) 是 0，会绕过负数检查，悄悄把上限关掉。
            if float(raw) != number:
                return self._error(400, "section_max_height must be a whole number",
                                   "invalid_request_error")
            if number < 0:
                return self._error(400, "section_max_height cannot be negative",
                                   "invalid_request_error")
            reply["section_max_height"] = \
                wb_settings.set_section_max_height(_g().ACCOUNTS_DIR, number)
        if "ui_language" in payload:
            # 实例级默认语言；看板在每个浏览器用 localStorage 覆盖，本值是浏览器
            # 本地无偏好时的回退。
            raw = payload.get("ui_language")
            if not isinstance(raw, str) or raw not in ("zh", "zh-Hant", "en"):
                return self._error(400, "ui_language must be zh, zh-Hant or en",
                                   "invalid_request_error")
            reply["ui_language"] = wb_settings.set_ui_language(_g().ACCOUNTS_DIR, raw)
        if "pricing_variant_inherit" in payload:
            # 严格 JSON 布尔：字符串 "false" 为真值，会把功能悄悄打开。
            raw = payload.get("pricing_variant_inherit")
            if not isinstance(raw, bool):
                return self._error(400, "pricing_variant_inherit must be true or false",
                                   "invalid_request_error")
            previous = wb_settings.pricing_variant_inherit(_g().ACCOUNTS_DIR)
            wb_settings.set_pricing_variant_inherit(_g().ACCOUNTS_DIR, raw)
            reply["pricing_variant_inherit"] = raw
            if _g().PRICING and previous != raw:
                # 开关只在下次抓取生效——刷新会丢掉（关）或补上（开）后缀继承的
                # 指派——所以立刻踢一次，不等间隔走完。
                threading.Thread(target=_g().PRICING.run_once, daemon=True,
                                 name="price-refresh-inherit").start()
                reply["pricing_refresh_started"] = True
        if "enable_anthropic_messages" in payload:
            raw = payload.get("enable_anthropic_messages")
            if not isinstance(raw, bool):
                return self._error(400, "enable_anthropic_messages must be true or false", "invalid_request_error")
            wb_settings.set_enable_anthropic_messages(_g().ACCOUNTS_DIR, raw)
            reply["enable_anthropic_messages"] = raw
        if "dsh_tool_neutralization" in payload:
            raw = payload.get("dsh_tool_neutralization")
            if not isinstance(raw, bool):
                return self._error(400, "dsh_tool_neutralization must be true or false", "invalid_request_error")
            wb_settings.set_dsh_tool_neutralization(_g().ACCOUNTS_DIR, raw)
            reply["dsh_tool_neutralization"] = raw
        if "free_model_credit_fallback" in payload:
            raw = payload.get("free_model_credit_fallback")
            if not isinstance(raw, bool):
                return self._error(400, "free_model_credit_fallback must be true or false", "invalid_request_error")
            wb_settings.set_free_model_credit_fallback(_g().ACCOUNTS_DIR, raw)
            reply["free_model_credit_fallback"] = raw
        for key, setter in (
                ("prompt_cache_key", wb_settings.set_prompt_cache_key),
                ("affinity_debug", wb_settings.set_affinity_debug),
                ("upstream_keepalive", wb_settings.set_upstream_keepalive),
                ("usage_cache", wb_settings.set_usage_cache),
                ("usage_day_buckets", wb_settings.set_usage_day_buckets),
                ("recent_index", wb_settings.set_recent_index)):
            if key not in payload:
                continue
            raw = payload.get(key)
            if not isinstance(raw, bool):
                return self._error(400, "%s must be true or false" % key,
                                   "invalid_request_error")
            reply[key] = setter(_g().ACCOUNTS_DIR, raw)
        if "trusted_proxies" in payload:
            raw = payload.get("trusted_proxies")
            if not isinstance(raw, str):
                return self._error(400, "trusted_proxies must be a string",
                                   "invalid_request_error")
            cleaned = ",".join(str(net) for net in parse_trusted_proxies(raw))
            reply["trusted_proxies"] = wb_settings.set_trusted_proxies(
                _g().ACCOUNTS_DIR, cleaned)
        if "auto_switch_product" in payload:
            # 严格 JSON 布尔：字符串 "false" 为真值，会把功能悄悄打开，而这恰是
            # 操作者关闭它时最不能发生的事。
            raw = payload.get("auto_switch_product")
            if not isinstance(raw, bool):
                return self._error(400, "auto_switch_product must be true or false",
                                   "invalid_request_error")
            wb_settings.set_auto_switch_product(_g().ACCOUNTS_DIR, raw)
            reply["auto_switch_product"] = raw
        if "daily_chat_web" in payload:
            raw = payload.get("daily_chat_web")
            if not isinstance(raw, bool):
                return self._error(400, "daily_chat_web must be true or false",
                                   "invalid_request_error")
            wb_settings.set_daily_chat_web(_g().ACCOUNTS_DIR, raw)
            reply["daily_chat_web"] = raw
        if "daily_chat_web_retry" in payload:
            # 严格 JSON 布尔：字符串 "false" 为真值，会悄悄一直重试。
            raw = payload.get("daily_chat_web_retry")
            if not isinstance(raw, bool):
                return self._error(400, "daily_chat_web_retry must be true or false",
                                   "invalid_request_error")
            wb_settings.set_daily_chat_web_retry(_g().ACCOUNTS_DIR, raw)
            reply["daily_chat_web_retry"] = raw
        if "daily_chat_web_attempts" in payload:
            # 总尝试次数，含首次。等待逐次翻倍，多打一位数字就是一个账号卡几小时；
            # 两项校验与报错都在 wb_settings。
            raw = payload.get("daily_chat_web_attempts")
            try:
                wb_settings.set_daily_chat_web_attempts(_g().ACCOUNTS_DIR, raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            reply["daily_chat_web_attempts"] = raw
        if "daily_chat_web_timeout" in payload:
            # 只收整秒且有界：重试以此翻倍，多打一位数字会让调度器在一个账号上卡
            # 几小时；校验与报错在 wb_settings。
            raw = payload.get("daily_chat_web_timeout")
            try:
                wb_settings.set_daily_chat_web_timeout(_g().ACCOUNTS_DIR, raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            reply["daily_chat_web_timeout"] = raw
        if "task_jitter_max" in payload:
            # 整秒、2 秒起步：低于 2 秒的等待挡不住同批风控，高于上限只可能是
            # 多打了一位；上限见 wb_settings。
            raw = payload.get("task_jitter_max")
            if isinstance(raw, bool) or raw is None:
                return self._error(400, "task_jitter_max must be a whole number",
                                   "invalid_request_error")
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return self._error(400, "task_jitter_max must be a whole number",
                                   "invalid_request_error")
            if not wb_settings.TASK_JITTER_MIN <= value <= wb_settings.TASK_JITTER_MAX:
                return self._error(
                    400, "task_jitter_max must be between %d and %d"
                         % (wb_settings.TASK_JITTER_MIN, wb_settings.TASK_JITTER_MAX),
                    "invalid_request_error")
            reply["task_jitter_max"] = wb_settings.set_task_jitter_max(
                _g().ACCOUNTS_DIR, value)
        if "scheduler_hours" in payload:
            try:
                cleaned = wb_settings.set_scheduler_hours(
                    _g().ACCOUNTS_DIR, payload.get("scheduler_hours"))
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            reply["scheduler_hours"] = cleaned
            if _g().SCHEDULER:
                _g().SCHEDULER.apply_schedule(cleaned)
        if "daily_chat_model" in payload:
            raw = payload.get("daily_chat_model") or ""
            # 空值把覆盖清回内建 id；非空必须像模型 id，乱填的值会在每次日跑时
            # 发给上游。
            if not isinstance(raw, str) or len(raw) > 64 or \
                    (raw and not re.match(r"^[A-Za-z0-9._:-]+$", raw)):
                return self._error(400, "daily_chat_model must be a model id or empty",
                                   "invalid_request_error")
            wb_settings.set_daily_chat_model(_g().ACCOUNTS_DIR, raw)
            reply["daily_chat_model"] = raw
        if "local_web_tools" in payload:
            raw = payload.get("local_web_tools")
            if not isinstance(raw, bool):
                return self._error(400, "local_web_tools must be true or false",
                                   "invalid_request_error")
            wb_settings.set_local_web_tools(_g().ACCOUNTS_DIR, raw)
            reply["local_web_tools"] = raw
        if "egress_backoff_enabled" in payload:
            # 出口级瞬时失败退避开关。严格 JSON 布尔：字符串 "false" 为真值。
            raw = payload.get("egress_backoff_enabled")
            if not isinstance(raw, bool):
                return self._error(400, "egress_backoff_enabled must be true or false",
                                   "invalid_request_error")
            wb_settings.set_egress_backoff_enabled(_g().ACCOUNTS_DIR, raw)
            reply["egress_backoff_enabled"] = raw
        if "log_db_enabled" in payload or "log_db_max_rows" in payload \
                or "log_db_max_days" in payload or "log_db_max_bytes" in payload:
            # 日志写入数据库（默认关闭）：开关与三条保留上限可任选子集提交，
            # 未点名的键原样保留；上限留空（null / "" / 0）一律存成「不限」。
            log_db_touched = False
            if "log_db_enabled" in payload:
                raw = payload.get("log_db_enabled")
                if not isinstance(raw, bool):
                    return self._error(400, "log_db_enabled must be true or false",
                                       "invalid_request_error")
                wb_settings.set_log_db_enabled(_g().ACCOUNTS_DIR, raw)
                reply["log_db_enabled"] = raw
                log_db_touched = True
            for key, setter in (
                    ("log_db_max_rows", wb_settings.set_log_db_max_rows),
                    ("log_db_max_days", wb_settings.set_log_db_max_days),
                    ("log_db_max_bytes", wb_settings.set_log_db_max_bytes)):
                if key in payload:
                    reply[key] = setter(_g().ACCOUNTS_DIR, payload.get(key))
                    log_db_touched = True
            if log_db_touched:
                _g().apply_log_db_settings()
        if "accounts_collapsed" in payload:
            # 一个折叠状态，也是本分支唯一会动的东西：提交只带这个键，未点名的
            # 设置在写入后原样保留。
            raw = payload.get("accounts_collapsed")
            if not isinstance(raw, bool):
                return self._error(400, "accounts_collapsed must be true or false",
                                   "invalid_request_error")
            wb_settings.set_accounts_collapsed(_g().ACCOUNTS_DIR, raw)
            reply["accounts_collapsed"] = raw
        if "accounts_separate_tab" in payload:
            # 同上形状。严格 JSON 布尔：字符串 "false" 为真值，会把导航悄悄拆开。
            raw = payload.get("accounts_separate_tab")
            if not isinstance(raw, bool):
                return self._error(400, "accounts_separate_tab must be true or false",
                                   "invalid_request_error")
            wb_settings.set_accounts_separate_tab(_g().ACCOUNTS_DIR, raw)
            reply["accounts_separate_tab"] = raw
        if "key_before_hidden" in payload:
            # 同 accounts_collapsed 形状。严格 JSON 布尔：字符串 "false" 为真值，
            # 会把行悄悄折起来。
            raw = payload.get("key_before_hidden")
            if not isinstance(raw, bool):
                return self._error(400, "key_before_hidden must be true or false",
                                   "invalid_request_error")
            wb_settings.set_key_before_hidden(_g().ACCOUNTS_DIR, raw)
            reply["key_before_hidden"] = raw
        if "hidden_pages" in payload:
            # 页面键列表是本分支唯一会动的东西，未点名的设置写入后原样保留。裸字符串会被
            # 逐字符遍历只会隐藏一堆乱码，所以非字符串列表一律直接拒绝。
            raw = payload.get("hidden_pages")
            if not isinstance(raw, list) or any(not isinstance(k, str) for k in raw):
                return self._error(400, "hidden_pages must be a list of page keys",
                                   "invalid_request_error")
            # 应答规范化后的列表：拼错的键进来时已被丢弃，面板回显的是实际存下的内容。
            reply["hidden_pages"] = wb_settings.set_hidden_pages(_g().ACCOUNTS_DIR, raw)
        if "update_check_enabled" in payload:
            # 严格 JSON 布尔：字符串 "false" 为真值，会悄悄开始 GitHub 调用。
            raw = payload.get("update_check_enabled")
            if not isinstance(raw, bool):
                return self._error(400, "update_check_enabled must be true or false",
                                   "invalid_request_error")
            wb_settings.set_update_check_enabled(_g().ACCOUNTS_DIR, raw)
            reply["update_check_enabled"] = raw
            if _g().UPDATES and raw:
                # 打开开关不该等轮询睡眠走完。
                _g().UPDATES.wake()
        if "update_check_interval_hours" in payload:
            raw = payload.get("update_check_interval_hours")
            if isinstance(raw, bool) or not isinstance(raw, int):
                return self._error(400, "update_check_interval_hours must be an integer",
                                   "invalid_request_error")
            if raw < 1 or raw > wb_settings.UPDATE_CHECK_INTERVAL_MAX:
                return self._error(400, "update_check_interval_hours must be between 1 and %d"
                                   % wb_settings.UPDATE_CHECK_INTERVAL_MAX,
                                   "invalid_request_error")
            reply["update_check_interval_hours"] = \
                wb_settings.set_update_check_interval_hours(_g().ACCOUNTS_DIR, raw)
            if _g().UPDATES:
                # 新间隔可能已经放行，立刻让后台线程重新判断一次。
                _g().UPDATES.wake()
        if "upstream" in payload:
            raw = payload.get("upstream")
            if not isinstance(raw, dict):
                return self._error(400, "upstream must be an object",
                                   "invalid_request_error")
            try:
                patch = wb_settings.validate_upstream_patch(raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            wb_settings.set_upstream_config(_g().ACCOUNTS_DIR, patch)
            reply["upstream"] = wb_settings.upstream_config(_g().ACCOUNTS_DIR)
        if "usage" in payload:
            raw = payload.get("usage")
            if not isinstance(raw, dict):
                return self._error(400, "usage must be an object",
                                   "invalid_request_error")
            try:
                patch = wb_settings.validate_usage_patch(raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            wb_settings.set_usage_config(_g().ACCOUNTS_DIR, patch)
        if "prompt" in payload:
            raw = payload.get("prompt")
            if not isinstance(raw, dict):
                return self._error(400, "prompt must be an object",
                                   "invalid_request_error")
            try:
                patch = wb_settings.validate_prompt_patch(raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            wb_settings.set_prompt_config(_g().ACCOUNTS_DIR, patch)
            reply["prompt"] = wb_settings.prompt_config(_g().ACCOUNTS_DIR)
        new_key = payload.get("api_key")
        if new_key is not None:
            new_key = str(new_key).strip()
            if new_key and len(new_key) < 4:
                return self._error(400, "api key must be at least 4 characters",
                                   "invalid_request_error")
            wb_settings.set_api_key(_g().ACCOUNTS_DIR, new_key)
            _g().API_KEY = new_key
            # 与 _g().API_KEY 同写网关状态：runtime_settings_view 从那里读
            # api_key_set_by_panel，写成本模块的全局变量读不到。
            _g().API_KEY_FILE_SET = True
            reply["api_key_set"] = bool(new_key)
        if payload.get("restart_scheduler"):
            if _g().SCHEDULER:
                _g().SCHEDULER.stop()
                _g().SCHEDULER.start()
            reply["scheduler"] = "restarted"
        reply.update(_g().runtime_settings_view())
        return self._json(200, reply)

    def _handle_proxy_slots(self, path, payload):
        if path == "/proxy/slots":
            return self._json(200, {"slots": wb_proxy_slots.proxy_slots_view(_g().POOL, _g().ACCOUNTS_DIR)})
        if path == "/proxy/slots/save":
            raw = payload.get("slots")
            if not isinstance(raw, list):
                return self._error(400, "slots must be a list", "invalid_request_error")
            cleaned = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                # 用存储同款清理器规范化，面板回显的探测出口字段才能存回来，
                # 不会被这里手写的第二份字段清单丢掉。
                entry = wb_settings._clean_slot_entry(item)
                if entry is not None:
                    cleaned.append(entry)
            saved = wb_settings.set_proxy_slots(_g().ACCOUNTS_DIR, cleaned)
            if _g().POOL:
                # 槽位可能已被删掉：重算前先解绑还引用它的账号，过期 id 不能存活。
                dropped = wb_settings.drop_missing_bindings(_g().POOL, saved)
                _g().POOL.apply_proxy_slots(saved)
                if dropped:
                    _g().log("proxy slots: unbound %d account(s) from removed slots"
                        % dropped)
            _g().log("proxy slots saved: %d slot(s)" % len(saved))
            return self._json(200, {"slots": wb_proxy_slots.proxy_slots_view(_g().POOL, _g().ACCOUNTS_DIR)})
        if path == "/proxy/slots/test":
            slot_id = str(payload.get("id") or "").strip()
            slot = wb_settings.find_proxy_slot(_g().ACCOUNTS_DIR, slot_id)
            if slot is None:
                return self._error(404, "no such proxy slot")
            probe = wb_proxy_slots.probe_proxy_intel(slot["url"])
            reply = dict(probe)
            reply["id"] = slot_id
            reply["slot"] = None
            if probe["ok"]:
                # 记住探测到的出口，面板无需再探就能显示。geo 查询空手而归时保留已存的出口
                # 信息而不是抹成空白，查询抖动不能冲掉上次成功探测学到的东西。
                updated_fields = {
                    "ip": probe["exit_ip"],
                    "probed_at": int(time.time()),
                }
                if probe.get("intel_ok"):
                    updated_fields.update({
                        "country": probe["country"],
                        "country_code": probe["country_code"],
                        "ip_type": probe["ip_type"],
                        "isp": probe["isp"],
                        "asn": probe["asn"],
                    })
                else:
                    _g().log("proxy slots: %s probed %s but the geo lookup returned "
                        "nothing; keeping the exit info already stored"
                        % (slot_id, probe["exit_ip"] or "-"))
                # name 故意不动：空名是存储标记「自动命名」的方式，slot_label()
                # 从国家和类型派生标签；把派生文本写进 name 会把名字冻在当次出口上。
                updated = wb_settings.update_proxy_slot(
                    _g().ACCOUNTS_DIR,
                    slot_id,
                    updated_fields,
                )
                reply["slot"] = updated
                reply["name"] = (updated or {}).get("name", "")
            return self._json(200, reply)
        if path == "/proxy/discover":
            return self._json(200, {"candidates": wb_proxy_slots.discover_proxy_slots()})
        return self._error(404, "not found", "invalid_request_error")

    # ---- 一键配置客户端接入（wb_agents） ----

    def _agents_client_allowed(self):
        """本请求的来源地址能不能用一键配置（判定见模块级 agents_client_allowed）。"""
        return _g().agents_client_allowed(getattr(self, "client_address", None))

    def _agents_base_url_hint(self):
        """本地客户端应当指向的 URL 的最佳猜测，由绑定 socket 派生；绑 0.0.0.0 时客户端
        连不上，改报 127.0.0.1。"""
        host, port = "", 0
        try:
            host, port = self.server.server_address[:2]
        except Exception:
            pass
        host = str(host or "").strip() or "127.0.0.1"
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return "http://%s:%d/v1" % (host, int(port or 0))

    def _agents_models(self):
        """给选择器的扁平模型列表；内置 intl+cn 目录，去重。实时来源刻意不走这里：上游
        挂掉时面板也必须能回答，失败只是列表短一点，绝不报错。"""
        models = []
        seen = set()
        try:
            try:
                entries = fetch_models()
            except Exception:
                entries = []
            if entries:
                sources = ((mid, meta) for mid, meta in entries)
            else:
                sources = (
                    (item.get("id"), item)
                    for item in (list(getattr(wb_catalog, "STATIC_INTL_MODELS", []))
                                 + list(getattr(wb_catalog, "STATIC_CN_MODELS", [])))
                    if isinstance(item, dict)
                )
            for mid, meta in sources:
                mid = str(mid or "").strip()
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                meta = meta if isinstance(meta, dict) else {}
                entry = {"id": mid}
                # 与 /v1/models 同一条查找链（上游值 > 内建知识表 > 本地缓存 > 默认），否则
                # 一键配置写进客户端的窗口会和网关声明的不一致（桌面端默认档 300k vs 1M）。
                try:
                    ctx, out, _src = wb_modelsdev.lookup(
                        mid, meta.get("maxInputTokens"),
                        meta.get("maxOutputTokens"), directory=_g().ACCOUNTS_DIR)
                except Exception:
                    ctx = meta.get("maxInputTokens")
                    out = meta.get("maxOutputTokens")
                if not ctx:
                    window = meta.get("contextWindow")
                    if isinstance(window, dict):
                        lengths = window.get("supportedLengths") or []
                        ctx = window.get("defaultLength") or (
                            lengths[-1] if lengths else None)
                if ctx:
                    try:
                        entry["context_window"] = int(ctx)
                    except (TypeError, ValueError):
                        pass
                if out:
                    try:
                        entry["max_output"] = int(out)
                    except (TypeError, ValueError):
                        pass
                name = meta.get("name")
                if name:
                    entry["name"] = name
                # 视觉支持标记，DSH 与客户端读它
                vision = bool(meta.get("supportsImages")) and not meta.get("disabledMultimodal")
                if mid == "glm-5.3-flash":
                    vision = False
                if vision:
                    entry["supports_vision"] = True
                # 可选思考档位交给客户端（DSH 用它生成档位选择器）。只声明目录明确列出档位
                # 的模型：固定档位模型不列，选了也不会改变上游实际跑的档位。
                efforts = (meta.get("reasoning") or {}).get("supportedEfforts")
                if isinstance(efforts, (list, tuple)):
                    levels = [str(e).strip() for e in efforts
                              if str(e or "").strip()]
                    if levels:
                        entry["reasoning_efforts"] = levels
                models.append(entry)
        except Exception:
            pass
        return models

    def _get_agents_cli(self):
        """提供 wb_agents.py，让远程机器可以取回配置器。刻意不要面板会话、不做 loopback
        校验：调用方就是别的机器，这正是 CLI 的用途。文件不含秘密，与仓库里的副本逐字节相同。"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "wb_agents.py")
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            return self._error(500, "wb_agents.py unavailable: %s" % exc)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition",
                         'inline; filename="wb_agents.py"')
        self.send_header("Content-Length", str(len(body)))
        if _g().cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        return

    def _get_agents(self):
        if not self._agents_client_allowed():
            # 远程看板（服务端 / Docker / OpenWrt）：一键配置改的是网关所在
            # 机器的配置目录，到不了用户自己的电脑，直接按不可用返回。
            return self._json(200, {
                "enabled": False,
                "reason": "agent config is only available from the machine running the gateway",
            })
        wb_agents = _g().agents_module()
        keys = []
        try:
            for entry in _g().configured_keys():
                if entry.get("enabled"):
                    keys.append({
                        "id": entry.get("id"),
                        "name": entry.get("name") or "",
                        "enabled": True,
                    })
        except Exception:
            keys = []
        return self._json(200, {
            "enabled": True,
            "clients": list(wb_agents.overview(_g().ACCOUNTS_DIR).values()),
            "models": self._agents_models(),
            "keys": keys,
            "global_key_set": bool(_g().API_KEY),
            "auth_required": _g().auth_required(),
            "gateway": {"base_url": self._agents_base_url_hint()},
        })

    def _agents_resolve_key(self, payload, warnings):
        """按载荷挑选交给客户端的网关 key，请求应当失败时返回 None。失败消息只在软回退
        情形追加到 `warnings`；硬失败经调用方的 400 映射抛出。"""
        wb_agents = _g().agents_module()
        key_id = str(payload.get("key_id") or "").strip()
        if key_id == "__global":
            return _g().API_KEY or None
        keys = _g().configured_keys()
        if key_id:
            for entry in keys:
                if entry.get("id") == key_id:
                    return entry.get("key") or None
            raise wb_agents.AgentConfigError(
                "no configured key with id %r" % key_id)
        # 默认：全局 key 已设就用它，否则取第一个启用的面板 key。
        if _g().API_KEY:
            return _g().API_KEY
        for entry in keys:
            if entry.get("enabled"):
                return entry.get("key") or None
        return None

    def _handle_agents_apply(self, payload):
        wb_agents = _g().agents_module()
        client_id = str(payload.get("client") or payload.get("client_id") or "").strip()
        if not client_id:
            return self._error(400, "client is required", "invalid_request_error")
        base_url = str(payload.get("base_url") or "").strip()
        if not re.match(r"^https?://", base_url):
            return self._error(
                400, "base_url must start with http:// or https://",
                "invalid_request_error")
        model = str(payload.get("model") or "").strip() or None
        models = payload.get("models")
        if not models:
            # 回退到网关发现的目录：内嵌模型定义的客户端（OpenCode、DSH、
            # Crush、ZCode）即使前端没送这个字段也能拿到完整模型列表。
            models = self._agents_models()
        if not isinstance(models, list):
            return self._error(400, "models must be a list",
                               "invalid_request_error")
        # 失控的选择器不能变成兆级配置文件。
        models = models[:80]
        cleaned_models = []
        for item in models:
            if isinstance(item, dict) and item.get("id"):
                cleaned_models.append(item)
            elif isinstance(item, str) and item.strip():
                cleaned_models.append({"id": item.strip()})
        # 看板只送模型 id，而 DSH / Crush 这类客户端要求把规格写进自己的配置。这里按 id 补上
        # 网关目录规格（见 enrich_agent_models），显式带来的字段优先。
        try:
            specs = {}
            for spec in self._agents_models():
                if isinstance(spec, dict) and spec.get("id"):
                    specs[str(spec["id"])] = spec
        except Exception:
            specs = {}
        cleaned_models = wb_agents.enrich_agent_models(cleaned_models, specs)
        warnings = []
        try:
            api_key = self._agents_resolve_key(payload, warnings)
        except wb_agents.AgentConfigError as exc:
            return self._error(400, str(exc), "invalid_request_error")
        if not api_key:
            if _g().auth_required():
                return self._error(
                    400, "no gateway key available - 请先配置 API Key",
                    "invalid_request_error")
            api_key = "wb-local"
            warnings.append("gateway has no key configured; wrote placeholder "
                            "'wb-local' (auth is off, so any value works)")
        try:
            result = wb_agents.integrate(
                _g().ACCOUNTS_DIR, client_id, base_url, api_key,
                model=model, models=cleaned_models)
        except wb_agents.AgentConfigError as exc:
            return self._error(400, str(exc), "invalid_request_error")
        except Exception as exc:
            return self._error(500, "agents apply failed: %s" % exc)
        result["warnings"] = warnings
        _g().log("agents apply: client=%s base_url=%s model=%s files=%d"
            % (client_id, base_url, model or "-", len(result.get("files") or [])),
            tag="agents")
        return self._json(200, result)

    def _handle_agents_restore(self, payload):
        wb_agents = _g().agents_module()
        client_id = str(payload.get("client") or payload.get("client_id") or "").strip()
        if not client_id:
            return self._error(400, "client is required", "invalid_request_error")
        try:
            result = wb_agents.restore(_g().ACCOUNTS_DIR, client_id)
        except wb_agents.AgentConfigError as exc:
            return self._error(400, str(exc), "invalid_request_error")
        except Exception as exc:
            return self._error(500, "agents restore failed: %s" % exc)
        _g().log("agents restore: client=%s files=%d"
            % (client_id, len(result.get("restored") or [])), tag="agents")
        return self._json(200, result)

    def _handle_panel(self, path):
        payload = self._payload_or_error()
        if payload is None:
            return
        if path == "/panel/login":
            peer_ip = self.client_address[0] if hasattr(self, "client_address") and self.client_address else "127.0.0.1"
            # 限流键不能直接取对端地址：反向代理后对端是代理本身，见 login_rate_limit_key
            client_ip = login_rate_limit_key(peer_ip, getattr(self, "headers", None))
            now = time.time()
            with _login_lock:
                _prune_login_attempts(now)
                attempts = [t for t in _login_attempts.get(client_ip, []) if now - t < 60]
                _login_attempts[client_ip] = attempts
                if len(attempts) >= 5:
                    wait_sec = int(60 - (now - attempts[0]))
                    return self._error(429, f"too many login attempts, please wait {max(1, wait_sec)}s", "rate_limit_error")
            password = str(payload.get("password") or "")
            if not wb_settings.verify_panel_password(_g().ACCOUNTS_DIR, password):
                with _login_lock:
                    _login_attempts.setdefault(client_ip, []).append(now)
                # 短退避，缓解自动化暴力尝试。
                time.sleep(0.5)
                return self._error(401, "invalid panel password", "invalid_request_error")
            with _login_lock:
                _login_attempts.pop(client_ip, None)
            token = _g().PANEL.create()
            return self._json(200, {
                "ok": True,
                "token": token,
                "using_default_password": wb_settings.panel_password_is_default(_g().ACCOUNTS_DIR),
            })
        if path == "/panel/logout":
            _g().PANEL.revoke(self._panel_token())
            return self._json(200, {"ok": True})
        # 从这里往下都需要已认证的面板会话。
        if not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        if path == "/panel/password":
            current = str(payload.get("current") or "")
            new = str(payload.get("new") or "")
            if not wb_settings.verify_panel_password(_g().ACCOUNTS_DIR, current):
                return self._error(401, "current password is wrong", "invalid_request_error")
            if len(new) < 4:
                return self._error(400, "new password must be at least 4 characters", "invalid_request_error")
            wb_settings.set_panel_password(_g().ACCOUNTS_DIR, new)
            if new != wb_settings.DEFAULT_PANEL_PASSWORD:
                # 改密会让其他浏览器会话全部失效。
                _g().PANEL.revoke_all()
            token = _g().PANEL.create()
            return self._json(200, {"ok": True, "token": token})
        return self._error(404, "not found", "invalid_request_error")

    def _handle_accounts(self, path, payload):
        if _g().POOL is None:
            return self._error(503, "account pool unavailable")
        if path == "/accounts/import" and isinstance(payload, list):
            # 裸数组只对导入有意义；包一层，让本处理器其余部分继续假定是 dict。
            payload = {"data": payload}
        if not isinstance(payload, dict):
            return self._error(400, "expected a JSON object", "invalid_request_error")
        if path == "/accounts/credits":
            return self._route_accounts_credits(payload)
        if path == "/tasks/run":
            return self._route_tasks_run(payload)
        if path == "/tasks/travel":
            return self._route_tasks_travel(payload)
        if path == "/tasks/streak-bonus":
            return self._route_tasks_streak_bonus(payload)
        if path == "/scheduler/trigger":
            return self._route_scheduler_trigger(payload)
        if path == "/scheduler/toggle":
            return self._route_scheduler_toggle(payload)
        if path == "/pricing/refresh":
            return self._route_pricing_refresh(payload)
        if path == "/logs/clear":
            return self._route_logs_clear(payload)
        if path == "/realm":
            return self._route_realm(payload)
        if path == "/accounts/checkin":
            return self._route_accounts_checkin(payload)
        if path == "/accounts/daily-chat":
            return self._route_accounts_daily_chat(payload)
        if path == "/accounts/daily-chat-web":
            return self._route_accounts_daily_chat_web(payload)
        if path == "/accounts/login/start":
            return self._route_accounts_login_start(payload)
        if path == "/accounts/login/cancel":
            return self._route_accounts_login_cancel(payload)
        if path == "/accounts/import/desktop":
            return self._route_accounts_import_desktop(payload)
        if path == "/accounts/refresh":
            return self._route_accounts_refresh(payload)
        if path == "/accounts/sync-profile":
            return self._route_accounts_sync_profile(payload)
        if path == "/accounts/test":
            return self._route_accounts_test(payload)
        if path == "/accounts/set":
            return self._route_accounts_set(payload)
        if path == "/accounts/product":
            return self._route_accounts_product(payload)
        if path == "/accounts/set-all":
            return self._route_accounts_set_all(payload)
        if path == "/accounts/delete":
            return self._route_accounts_delete(payload)
        if path == "/accounts/import":
            return self._route_accounts_import(payload)
        if path == "/accounts/add-api-key":
            return self._route_accounts_add_api_key(payload)
        return self._error(404, "unknown account endpoint", "invalid_request_error")

    def _route_accounts_product(self, payload):
        """官方有两套身份、两条配额线。某条满了可以切到另一条继续用。"""
        target = str(payload.get("product") or "").strip().lower()
        uid = payload.get("uid")
        realm = payload.get("realm")

        if target not in wb_identity.VALID_PRODUCTS:
            return self._error(400, "product must be 'workbuddy', 'vscode', or 'cli'",
                               "invalid_request_error")

        if uid:
            targets = [_g().POOL.get(uid)]
        elif realm and realm != "all":
            targets = [a for a in _g().POOL.accounts if a.realm == realm]
        else:
            targets = list(_g().POOL.accounts)

        changed = []
        for account in targets:
            if account is None:
                continue
            try:
                if account.set_product(target):
                    # 立刻写盘：set_product() 只改内存，这一下是操作者的明确选择，不能等别的
                    # 路径碰巧存档才生效，否则切完就重启容器的人会白白丢掉这次切换。
                    try:
                        account.save(_g().ACCOUNTS_DIR)
                    except Exception as exc:
                        _g().log("product save failed for %s: %s" % (account.uid[:8], exc),
                            level="WARN")
                    changed.append(account.uid[:8])
                    _g().log("account %s: 面板手动切换身份 -> %s"
                        % (account.uid[:8], target), level="INFO")
            except Exception as exc:
                _g().log("product switch failed for %s: %s" % (account.uid[:8], exc),
                    level="WARN")

        return self._json(200, {
            "ok": True,
            "product": target,
            "changed": changed,
            "accounts": wb_accounts.account_views(),
        })

    def _route_accounts_credits(self, payload):
        uid = payload.get("uid")
        realm = payload.get("realm")
        if uid:
            targets = [_g().POOL.get(uid)]
        elif realm and realm != "all":
            targets = [a for a in _g().POOL.accounts if a.realm == realm]
        else:
            targets = list(_g().POOL.accounts)
        results = self._refresh_account_credits(targets)
        return self._json(200, {"results": results, "accounts": wb_accounts.account_views()})

    def _route_tasks_run(self, payload):
        if not _g().POOL:
            return self._json(200, {"ok": False, "msg": "账号池不可用"})
        wb_tasks.invalidate_tasks_cache()          # 跑完任务状态就变了，别再端旧快照
        uid = payload.get("uid")
        if uid and uid != "all":
            target = _g().POOL.get(uid)
            if not target or target.realm != "cn":
                return self._json(200, {"ok": False, "msg": "未找到指定的国内版账号"})
            targets = [target]
        else:
            targets = [a for a in _g().POOL.accounts if a.realm == "cn" and a.enabled]
        if not targets:
            return self._json(200, {"ok": False, "msg": "未找到已启用的国内版账号"})
        from wb_tasks import run_growth_tasks
        combined_logs = []
        total_credit = 0
        busy = []
        for i, acc in enumerate(targets):
            uid_str = acc.uid[:8] if acc.uid else "?"
            nick = acc.nickname or uid_str
            combined_logs.append(f"====== 正在为账号 [{nick} ({acc.uid})] 执行全自动成长任务 ({i+1}/{len(targets)}) ======")
            res = run_growth_tasks(acc, gap=1.0)
            # 合计字段名是 "earned_credit"；按旧名 "credit_added" 读会静默加出 0，
            # 看板就永远显示「+0 积分」。
            total_credit += res.get("earned_credit") or 0
            if res.get("busy"):
                busy.append(acc.uid)
            for l in res.get("logs") or []:
                combined_logs.append(f"  {l}")
            if i < len(targets) - 1:
                time.sleep(1.5)
        combined_logs.append(f"====== 全部 {len(targets)} 个账号任务执行完毕，累计新增积分: +{total_credit} ======")
        _g().add_log_entry(f"[任务福利] 批量成长任务执行完成，共 {len(targets)} 个账号，新增积分 +{total_credit}", tag="tasks")
        out = {
            "ok": not (busy and len(busy) == len(targets)),
            "credit_added": total_credit,
            "logs": combined_logs,
            "accounts_count": len(targets),
        }
        if busy:
            out["busy_accounts"] = busy
            out["msg"] = "部分账号正在执行其他成长任务，本次已跳过"
            if len(busy) == len(targets):
                out["msg"] = "所有目标账号都在执行其他成长任务，请稍后再试"
        return self._json(200, out)

    def _route_tasks_streak_bonus(self, payload):
        if not _g().POOL:
            return self._json(200, {"ok": False, "msg": "账号池不可用"})
        wb_tasks.invalidate_tasks_cache()
        uid = payload.get("uid")
        if uid and uid != "all":
            target = _g().POOL.get(uid)
            if not target or target.realm != "cn":
                return self._json(200, {"ok": False, "msg": "未找到指定的国内版账号"})
            targets = [target]
        else:
            targets = [a for a in _g().POOL.accounts if a.realm == "cn" and a.enabled]
        if not targets:
            return self._json(200, {"ok": False, "msg": "未找到已启用的国内版账号"})
        from wb_tasks import run_streak_bonus
        results = []
        busy = []
        for i, acc in enumerate(targets):
            uid_str = acc.uid[:8] if acc.uid else "?"
            nick = acc.nickname or uid_str
            res = run_streak_bonus(acc)
            if res.get("busy"):
                busy.append(acc.uid)
            results.append(f"{nick}: {res.get('msg')}")
            _g().add_log_entry(f"[任务福利] 账号 [{nick}] 连登管家与抽奖: {res.get('msg')}", tag="tasks")
            if i < len(targets) - 1:
                time.sleep(1.0)
        out = {
            "ok": not (busy and len(busy) == len(targets)),
            "msg": chr(10).join(results),
        }
        if busy:
            out["busy_accounts"] = busy
            if len(busy) == len(targets):
                out["msg"] = "所有目标账号都在执行其他成长任务，请稍后再试"
        return self._json(200, out)

    def _route_tasks_travel(self, payload):
        if not _g().POOL:
            return self._json(200, {"ok": False, "msg": "账号池不可用"})
        wb_tasks.invalidate_tasks_cache()          # 同上：旅行会改任务/体力状态
        uid = payload.get("uid")
        if uid and uid != "all":
            target = _g().POOL.get(uid)
            if not target or target.realm != "cn":
                return self._json(200, {"ok": False, "msg": "未找到指定的国内版账号"})
            targets = [target]
        else:
            targets = [a for a in _g().POOL.accounts if a.realm == "cn" and a.enabled]
        if not targets:
            return self._json(200, {"ok": False, "msg": "未找到已启用的国内版账号"})
        from wb_tasks import do_cat_travel
        results = []
        busy = []
        for i, acc in enumerate(targets):
            uid_str = acc.uid[:8] if acc.uid else "?"
            nick = acc.nickname or uid_str
            res = do_cat_travel(acc)
            if res.get("busy"):
                busy.append(acc.uid)
            results.append({
                "uid": acc.uid,
                "nickname": nick,
                "action": res.get("action"),
                "msg": res.get("msg") or "",
                "busy": bool(res.get("busy")),
                # do_cat_travel() 把数额放在 "credit" 字段。
                "reward_credit": res.get("credit", 0)
            })
            if i < len(targets) - 1:
                time.sleep(1.0)
        summary_msg = chr(10).join([f"{r['nickname']}: {r['msg']}" for r in results])
        for r in results:
            _g().add_log_entry(f"[任务福利] 账号 [{r['nickname']}] 猫猫旅行: {r['msg']}", tag="tasks")
        out = {
            "ok": not (busy and len(busy) == len(targets)),
            "results": results,
            "msg": summary_msg,
            "accounts_count": len(targets),
        }
        if busy:
            out["busy_accounts"] = busy
            if len(busy) == len(targets):
                out["msg"] = "所有目标账号都在执行其他成长任务，请稍后再试"
        return self._json(200, out)

    def _route_update_check(self):
        """按操作者的明确请求，立即跑一次版本检查，内联返回而不是「已启动，轮询 /updates」：
        单次有界的 GitHub 请求。manual=True 同时无视每日开关与 24 小时窗口。"""
        if not _g().UPDATES:
            return self._json(200, {"ok": False, "msg": "更新检查未运行"})
        status = _g().UPDATES.check(manual=True)
        status["ok"] = True
        return self._json(200, status)

    def _route_scheduler_trigger(self, payload):
        if _g().SCHEDULER:
            return self._json(200, _g().SCHEDULER.trigger_now())
        return self._json(200, {"ok": False, "msg": "调度器未初始化"})

    def _route_scheduler_toggle(self, payload):
        if _g().SCHEDULER:
            enabled = not _g().SCHEDULER.enabled
            wb_settings.set_scheduler_enabled(_g().ACCOUNTS_DIR, enabled)
            _g().SCHEDULER.enabled = enabled
            _g().SCHEDULER.log(f"用户把调度器切换为：{'启用' if enabled else '暂停'}")
            return self._json(200, _g().SCHEDULER.status())
        return self._json(200, {"ok": False, "msg": "调度器未初始化"})

    def _route_pricing_refresh(self, payload):
        # 抓取要几十秒，跑在自己的线程上，面板轮询 /pricing 取结果。
        if not _g().PRICING:
            return self._json(200, {"ok": False, "msg": "价格刷新未运行"})
        if not wb_settings.pricing_enabled(_g().ACCOUNTS_DIR):
            return self._json(200, {"ok": False, "msg": "价估算已关闭，请先启用"})
        threading.Thread(target=_g().PRICING.run_once, daemon=True,
                         name="price-refresh-manual").start()
        out = _g().PRICING.status()
        out["ok"] = True
        out["msg"] = "已开始抓取，稍候刷新查看结果"
        return self._json(200, out)

    def _route_catalog_refresh(self, payload):
        """面板「刷新模型清单」：强制向上游拉一次指定版本的目录。版本（realm）由面板显式
        传当前视图的 intl/cn，每个版本只用自己的代表账号与端点，绝不跨区。拉取同步执行
        （通常 1~3 秒，上游丢包时两次 10s 尝试封顶），同一版本有 30s 冷却；无论成败都以
        200 + ok 标志回答，msg 供面板直接展示。"""
        realm = str((payload or {}).get("realm") or "").strip().lower()
        return self._json(200, refresh_catalog(realm))

    def _route_logs_clear(self, payload):
        _g().clear_logs()
        return self._json(200, {"ok": True})

    def _route_pricing_mapping(self, payload):
        """面板手填一条「hub 模型 → OpenRouter id」映射，写进 usage/pricing-overrides.json
        （运行期覆盖，不改源码 OVERRIDES，也不随镜像升级丢失），随即触发一次取价。
        or_id 为空表示删除该映射。写入只认形状像 OpenRouter id 的值，快照里有该条目时予以
        确认；没有也接受但要如实说明，上游可能刚上架而本地清单还没刷新。"""
        if _g().PRICING is None:
            return self._json(200, {"ok": False, "msg": "价格刷新未运行"})
        if not wb_settings.pricing_enabled(_g().ACCOUNTS_DIR):
            return self._json(200, {"ok": False, "msg": "价估算已关闭，请先启用"})
        model = str(payload.get("model") or "").strip()
        or_id = str(payload.get("or_id") or "").strip()
        if not model:
            return self._error(400, "model is required", "invalid_request_error")
        if or_id and not re.match(r"^[A-Za-z0-9._\-]+/[A-Za-z0-9._\-:]+$", or_id):
            return self._error(400, "or_id must look like 'vendor/model'",
                               "invalid_request_error")
        or_models, _by_norm = wb_pricing.live_index()
        known = bool(or_id) and or_id in (or_models or {})
        wb_pricing.save_runtime_override(model, or_id)
        _g().PRICING.invalidate_gaps()
        threading.Thread(target=_g().PRICING.run_once, daemon=True,
                         name="price-refresh-mapping").start()
        if not or_id:
            msg = "已删除 %s 的手填映射，下次取价起按自动匹配" % model
        elif known:
            msg = "已记录 %s → %s，正在重新取价" % (model, or_id)
        else:
            msg = ("已记录 %s → %s；本次快照里还没看到该条目，"
                   "取价后仍可能显示未定价" % (model, or_id))
        _g().add_log_entry("[定价] 面板手填映射：%s → %s%s"
                      % (model, or_id or "(清除)",
                         "" if (known or not or_id) else "（快照暂无此条目）"),
                      tag="pricing")
        return self._json(200, {"ok": True, "model": model, "or_id": or_id,
                                "known": known, "msg": msg})

    def _route_realm(self, payload):
        # 切换出口影响所有未绑定 realm 的 key，属管理操作，只认面板会话；
        # GET /realm 只报当前出口，对 API Key 开放。
        if not self._panel_ok():
            return self._error(403, "changing the upstream exit requires the "
                                    "panel session, not an API key",
                               "invalid_request_error")
        new_realm = payload.get("realm")
        if new_realm in ("intl", "cn"):
            wb_usage.save_persisted_realm(new_realm)
        return self._json(200, {"ok": True, "current": _g().CURRENT_REALM, "persisted": True})

    def _route_accounts_checkin(self, payload):
        uid = payload.get("uid")
        wb_tasks.invalidate_tasks_cache()          # 签到会改连续打卡状态，成长任务快照随之作废
        targets = ([_g().POOL.get(uid)] if uid
                   else [a for a in (_g().POOL.accounts if _g().POOL else [])
                         if a.has_personal_program()])
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.checkin(trigger="manual")
            nick = account.nickname or account.uid[:8]
            _g().add_log_entry(f"[任务福利] 账号 [{nick}] 每日签到: {res.get('msg') if res.get('ok') else res.get('error')}",
                          level="INFO" if res.get("ok") else "WARN", tag="tasks")
            results.append({"uid": account.uid, "nickname": account.nickname, **res})
        return self._json(200, {"results": results, "accounts": wb_accounts.account_views()})

    def _route_accounts_daily_chat(self, payload):
        uid = payload.get("uid")
        uids = payload.get("uids")
        if uid:
            targets = [_g().POOL.get(uid)]
        elif isinstance(uids, list):
            # 「未活跃账号一键打卡」只按名单精确执行：打过的不打第二遍（重复打卡不给积分，
            # 只白烧额度），空名单就是「一个都不打」，不能悄悄回落成全部；打全部就别传 uids。
            wanted = {str(item) for item in uids if item}
            targets = [a for a in _g().POOL.accounts
                       if a.realm == "intl" and a.enabled and str(a.uid) in wanted]
        else:
            targets = [a for a in _g().POOL.accounts if a.realm == "intl" and a.enabled]
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.daily_chat(trigger="manual")
            # 与网页通道打卡一样把结果写进运行日志：msg 带着 completed 还是失败
            # 原因，面板 toast 的「成功」看不出来。
            nick = account.nickname or account.uid[:8]
            _g().log(f"[任务福利] 账号 [{nick}] 每日活跃打卡: {res.get('msg') if res.get('ok') else res.get('error')}",
                level="INFO" if res.get("ok") else "WARN", tag="tasks")
            results.append({"uid": account.uid, "nickname": account.nickname, **res})
        return self._json(200, {"results": results, "accounts": wb_accounts.account_views()})

    def _route_accounts_daily_chat_web(self, payload):
        """网页通道打卡：只建网页端会话，不发桌面端那条轻量对话，手动触发用。刻意不写
        lastDailyChat——那是「今天已打过卡」的闸门，手动补打不该让定时巡检跳过当天的
        正常流程。"""
        uid = payload.get("uid")
        if uid:
            targets = [_g().POOL.get(uid)]
        else:
            targets = [a for a in _g().POOL.accounts if a.realm == "intl" and a.enabled]
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.daily_chat_web(trigger="manual")
            nick = account.nickname or account.uid[:8]
            _g().log(f"[任务福利] 账号 [{nick}] 网页通道打卡: {res.get('conversation') if res.get('ok') else res.get('error')}",
                level="INFO" if res.get("ok") else "WARN", tag="tasks")
            results.append({"uid": account.uid, "nickname": account.nickname, **res})
        return self._json(200, {"results": results, "accounts": wb_accounts.account_views()})

    def _route_accounts_login_start(self, payload):
        product = str(payload.get("product") or "").strip().lower()
        if product not in wb_identity.VALID_PRODUCTS:
            return self._error(400, "product must be one of: %s"
                               % ", ".join(wb_identity.VALID_PRODUCTS),
                               "invalid_request_error")
        target_realm = payload.get("realm") or _g().CURRENT_REALM
        try:
            started = _g().POOL.start_login(realm=target_realm, product=product)
        except Exception as exc:
            return self._error(502, "could not start login: %s" % exc)
        _g().log("oauth login started (realm=%s, product=%s, platform=%s, state=%s)"
            % (target_realm, product, started["platform"], started["state"][:8]))
        return self._json(200, started)

    def _route_accounts_login_cancel(self, payload):
        state = payload.get("state") or ""
        return self._json(200, {"cancelled": _g().POOL.cancel_login(state)})

    def _route_accounts_import_desktop(self, payload):
        # body 形状：{} 只扫描（只读）；{"path"} 导入该凭据；{"all": true} 导入全部；
        # {"recoverKey": true} 从桌面客户端进程回收 at-rest 密钥。
        target_path = payload.get("path")
        if payload.get("recoverKey"):
            try:
                key = wb_accounts.desktop_atrest_key(
                    force=bool(payload.get("force")), log=lambda m: _g().log(m, tag="accounts"))
            except Exception as exc:
                _g().log("at-rest key recovery failed: %s" % exc, level="WARN", tag="accounts")
                return self._json(200, {"ok": False, "msg": str(exc),
                                        "atrest": wb_atrest.status()})
            return self._json(200, {
                "ok": True,
                "keyId": wb_atrest.derive_key_id(key),
                "msg": "已从桌面客户端进程回收密钥（只留在内存里）",
                "atrest": wb_atrest.status(),
            })
        if payload.get("keyText") is not None:
            # 手工导入解码密钥：macOS / Linux 上无法回收进程内存，只能让用户贴一次。
            # 与回收同一道闸——过不了 keyblob 自检就原样报错，不落内存。
            try:
                key = wb_atrest.adopt_key(payload.get("keyText"),
                                          log=lambda m: _g().log(m, tag="accounts"))
            except Exception as exc:
                _g().log("at-rest key import rejected: %s" % exc, level="WARN", tag="accounts")
                return self._json(200, {"ok": False, "msg": str(exc),
                                        "atrest": wb_atrest.status()})
            return self._json(200, {
                "ok": True,
                "keyId": wb_atrest.derive_key_id(key),
                "msg": "已导入解码密钥（只留在内存里）",
                "atrest": wb_atrest.status(),
            })
        if payload.get("forgetKey"):
            wb_atrest.forget_key()
            return self._json(200, {"ok": True, "atrest": wb_atrest.status()})
        if target_path:
            realm = payload.get("realm")
            # 只接受扫描列出的桌面端凭据文件：这个接口不能拿来读本机任意路径
            known = {os.path.normcase(os.path.abspath(p))
                     for p, _realm in wb_accounts.desktop_credential_candidates()}
            if os.path.normcase(os.path.abspath(str(target_path))) not in known:
                return self._error(400, "path is not a desktop credential found by the scan",
                                   "invalid_request_error")
            try:
                account = _g().POOL.import_desktop_credential(
                    path=target_path, realm=realm, source="desktop-app")
            except Exception as exc:
                return self._error(400, "import failed: %s" % exc)
            _g().log("imported %s from %s (user confirmed)" % (account.uid[:8], os.path.basename(target_path)))
            return self._json(200, {
                "imported": [account.public()],
                "accounts": wb_accounts.account_views(),
            })
        if payload.get("all"):
            imported = wb_accounts.import_desktop_accounts(payload.get("realm"))
            return self._json(200, {
                "imported": [a.public() for a in imported],
                "accounts": wb_accounts.account_views(),
            })
        return self._json(200, {
            "detected": wb_accounts.desktop_credential_scan(),
            "accounts": wb_accounts.account_views(),
            "pool_uids": [a.uid for a in _g().POOL.accounts],
            "atrest": wb_atrest.status(),
        })

    def _route_accounts_refresh(self, payload):
        uid = payload.get("uid")
        targets = [_g().POOL.get(uid)] if uid else list(_g().POOL.accounts)
        results = []
        for account in targets:
            if account is None:
                continue
            if not account.can_refresh():
                results.append({"uid": account.uid, "ok": True,
                                "msg": "API Key 长期有效，无需刷新"})
                continue
            ok = account.refresh()
            account.save(_g().ACCOUNTS_DIR)
            results.append({"uid": account.uid, "ok": ok, "error": account.last_error})
        return self._json(200, {"results": results})

    def _route_accounts_sync_profile(self, payload):
        """看板手动刷新昵称。上游响应带手机号等个人字段，wb_accounts.fetch_account_profile
        只解析 uid/nickname。"""
        uid = str(payload.get("uid") or "").strip()
        realm = payload.get("realm")
        if uid:
            targets = [_g().POOL.get(uid)]
        elif realm and realm != "all":
            targets = [a for a in _g().POOL.accounts if a.realm == realm]
        else:
            targets = list(_g().POOL.accounts)
        updated = []
        failed = []
        for account in targets:
            if account is None:
                continue
            try:
                nickname = account.sync_nickname()
            except Exception as exc:
                failed.append({"uid": account.uid, "error": str(exc)[:200]})
                _g().log("profile sync failed for %s: %s" % (account.uid[:8], exc),
                    level="WARN")
                continue
            updated.append({"uid": account.uid, "nickname": nickname})
            _g().log("account %s: nickname synced from the web console"
                % account.uid[:8], level="INFO")
        return self._json(200, {"updated": updated, "failed": failed,
                                "accounts": wb_accounts.account_views()})

    def _route_accounts_test(self, payload):
        uid = payload.get("uid")
        if not uid:
            # 不带 uid 就是「一键测试」：一次看全区域里每个账号 + 本机连通性。
            return self._route_accounts_test_all(payload)
        account = _g().POOL.get(uid)
        if not account:
            return self._error(404, "no such account")
        model = payload.get("model") or wb_settings.test_model(_g().ACCOUNTS_DIR, account.realm)
        row = probe_account(account, model)
        _g().log("account test: uid=%s model=%s wall=%sms ok=%s%s"
            % (account.uid[:8], model, row.get("elapsed_ms"), row.get("ok"),
               "" if row.get("ok") else " error=%s" % (row.get("error") or "")),
            level="INFO" if row.get("ok") else "WARN", tag="accounts")
        # 单账号测试的返回体保持原样（面板与客户端都在用），只在失败时多带一个
        # network 标记：提示这是本机网络的问题，不是账号的问题。
        result = {"ok": row["ok"], "uid": row["uid"], "model": row["model"],
                  "elapsed_ms": row["elapsed_ms"]}
        if row["ok"]:
            result["reply"] = row["reply"]
        else:
            result["status"] = row["status"] if row["status"] is not None else 500
            result["error"] = row["error"]
            if row["network"]:
                result["network"] = True
        return self._json(200, result)

    def _route_accounts_test_all(self, payload):
        """一键测试：该区域每个已启用账号各打一条最小请求，外加一条本机连通性。池子出
        问题时要一次看全：哪个账号真不行、哪个只是被本地窗口（熔断 / 降权 / 软限流）关着
        （测试成功会顺手清掉这些计数），以及本机网络通不通；断网时每个账号都会失败。"""
        realm = str(payload.get("realm") or "").strip().lower()
        if realm not in ("intl", "cn"):
            realm = _g().CURRENT_REALM
        model = payload.get("model") or wb_settings.test_model(_g().ACCOUNTS_DIR, realm)
        targets = [a for a in (_g().POOL.accounts if _g().POOL else [])
                   if a.realm == realm and a.enabled]
        started = time.time()
        network = probe_local_network(realm)
        rows = []
        if targets:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(PROBE_CONCURRENCY, len(targets))) as pool:
                rows = list(pool.map(lambda acc: probe_account(acc, model), targets))
        passed = [r for r in rows if r.get("ok")]
        failed = [r for r in rows if not r.get("ok")]
        summary = {"realm": realm, "model": model, "tested": len(rows),
                   "passed": len(passed), "failed": len(failed),
                   "networkDown": not network.get("ok"),
                   "elapsed_ms": int((time.time() - started) * 1000)}
        _g().log("test all: realm=%s model=%s tested=%d passed=%d failed=%d network=%s"
            % (realm, model, len(rows), len(passed), len(failed),
               "ok" if network.get("ok") else "DOWN"), tag="accounts")
        return self._json(200, {"ok": True, "summary": summary, "network": network,
                                "results": rows, "accounts": wb_accounts.account_views()})

    def _route_accounts_set(self, payload):
        uid = payload.get("uid")
        if not uid:
            return self._error(400, "uid required")
        changes, problem = wb_accounts.parse_account_set(payload)
        if problem:
            return self._error(400, problem)
        if _g().POOL.get(uid) is None:
            return self._error(404, "no such account")
        updated = None
        if "proxySlot" in changes:
            updated = _g().POOL.set_proxy_slot(uid, changes["proxySlot"])
            if updated is None:
                return self._error(404, "no such account")
            _g().log("account %s proxy slot set to %s"
                % (uid[:8], updated.get("proxySlot") or "(direct)"))
        if "proxy" in changes:
            updated = _g().POOL.set_proxy(uid, changes["proxy"])
            if updated is None:
                return self._error(404, "no such account")
            _g().log("account %s proxy set to %s"
                % (uid[:8], updated.get("proxy") or "(direct)"))
        if "enabled" in changes:
            updated = _g().POOL.set_enabled(uid, changes["enabled"])
            if updated is None:
                return self._error(404, "no such account")
            if changes["enabled"]:
                _g().clear_stand_down()
            _g().log("account %s %s"
                % (uid[:8], "enabled" if changes["enabled"] else "disabled"))
        if "weight" in changes:
            updated = _g().POOL.set_weight(uid, changes["weight"])
            if updated is None:
                return self._error(404, "no such account")
            _g().log("account %s weight set to %d" % (uid[:8], changes["weight"]))
        if "priority" in changes:
            updated = _g().POOL.set_priority(uid, changes["priority"])
            if updated is None:
                return self._error(404, "no such account")
            _g().log("account %s priority set to %d" % (uid[:8], changes["priority"]))
        if "concurrencyLimit" in changes:
            updated = _g().POOL.set_concurrency_limit(uid, changes["concurrencyLimit"])
            if updated is None:
                return self._error(404, "no such account")
            _g().log("account %s concurrency limit set to %d"
                % (uid[:8], changes["concurrencyLimit"]))
        if "note" in changes:
            updated = _g().POOL.set_note(uid, changes["note"])
            if updated is None:
                return self._error(404, "no such account")
            _g().log("account %s note set (%d chars)"
                % (uid[:8], len(updated.get("note") or "")))
        if "disabledModels" in changes:
            # 人工黑名单：这些模型不再派给该账号，其他模型与别的账号都不受影响。
            # 传空数组 = 清空禁用。
            updated = _g().POOL.set_disabled_models(uid, changes["disabledModels"])
            if updated is None:
                return self._error(404, "no such account")
            _g().log("account %s disabled models: %s"
                % (uid[:8],
                   ",".join(updated.get("disabledModels") or []) or "(none)"))
        return self._json(200, {"account": updated})

    def _route_accounts_set_all(self, payload):
        _g().POOL.set_all_enabled(bool(payload.get("enabled")))
        if payload.get("enabled"):
            _g().clear_stand_down()
        return self._json(200, {"accounts": wb_accounts.account_views(),
                                "stand_down": _g().stand_down_state()})

    def _route_accounts_delete(self, payload):
        uid = payload.get("uid")
        if not uid:
            return self._error(400, "uid required")
        removed = _g().POOL.remove(uid)
        _g().log("account %s deleted" % uid[:8])
        return self._json(200, {"deleted": removed, "accounts": wb_accounts.account_views()})

    def _route_accounts_add_api_key(self, payload):
        """新增静态 API Key 账号；先探测一次企业额度端点，成功才保存。"""
        key = str(payload.get("key") or payload.get("accessToken") or "").strip()
        if not key:
            return self._error(400, "key is required", "invalid_request_error")
        if not wb_identity.is_api_key_token(key):
            return self._error(
                400,
                "not a static API key: expected the ck_/pt_ prefix from the "
                "CodeBuddy API console",
                "invalid_request_error")
        try:
            account = wb_accounts.api_key_account(
                key, payload.get("nickname") or "", payload.get("uid") or "")
        except Exception as exc:
            return self._error(400, "cannot build account: %s" % exc,
                               "invalid_request_error")
        if not account.uid:
            return self._error(400, "cannot derive a uid from this key",
                               "invalid_request_error")
        probe = account.probe_api_key(apply_credits=True)
        if not probe.get("ok"):
            return self._error(
                400,
                "the key did not pass the balance probe, so it was not saved: %s"
                % (probe.get("error") or "unknown error"),
                "invalid_request_error")
        probe.pop("credits", None)
        existed = _g().POOL.get(account.uid) is not None if _g().POOL else False
        _g().POOL.add(account)
        _g().log("account added (api key): uid=%s probe=ok(%s/%s)"
            % (account.uid[:8], probe.get("used"), probe.get("limit")),
            tag="accounts")
        return self._json(200, {
            "ok": True,
            "uid": account.uid,
            "nickname": account.nickname or account.uid[:8],
            "updated": existed,
            "probe": probe,
            "account": account.public(),
            "accounts": wb_accounts.account_views(),
        })

    def _route_accounts_import(self, payload):
        # 导入先前导出的文档或手写列表（body 形状见 wb_accounts._coerce_account_rows）。选项：
        # dryRun 只校验不写入；overwrite 替换同 uid 账号；realm 强制版本；data 优先于裸 body。
        blob = payload.get("data") if "data" in payload else payload
        if not isinstance(blob, (dict, list)):
            return self._error(400, "the document must be a JSON object or array",
                               "invalid_request_error")
        rows, problem = wb_accounts._coerce_account_rows(blob)
        if problem:
            return self._error(400, "cannot read the document: %s" % problem,
                               "invalid_request_error")
        dry_run = bool(payload.get("dryRun"))
        overwrite = bool(payload.get("overwrite"))
        forced_realm = (payload.get("realm") or "").strip().lower() or None
        if forced_realm and forced_realm not in ("intl", "cn"):
            return self._error(400, "realm must be intl or cn", "invalid_request_error")
        if dry_run:
            # 不动池子、逐行校验，让调用方先看清导入会做什么再提交。与真实导入
            # 共用规则，预览不会与实际不一致。
            return self._json(200, {
                "dryRun": True,
                "count": len(rows),
                "result": _g().POOL.preview_import_rows(rows, realm=forced_realm, overwrite=overwrite),
                "accounts": wb_accounts.account_views(),
            })
        report = _g().POOL.import_rows(rows, realm=forced_realm, overwrite=overwrite)
        _g().log("account import: %d added, %d updated, %d skipped, %d invalid"
            % (len(report["added"]), len(report["updated"]),
               len(report["skipped"]), len(report["invalid"])))
        return self._json(200, {
            "count": len(rows),
            "result": report,
            "accounts": wb_accounts.account_views(),
        })
