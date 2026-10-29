"""HTTP 层入口：连接处理、鉴权、错误应答与路由分派。

对话链路的处理方法在 wb_chat_routes，面板管理接口在 wb_panel_routes，
两者以多继承并入 Handler；运行态与各域能力经 _g() 取用。
"""

import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler

import wb_accounts
import wb_host
import wb_modelsdev
import wb_proxy_slots
import wb_settings
import wb_usage
from urllib.parse import parse_qs, urlparse

from wb_chat_routes import ChatRoutes
from wb_models import catalog_meta, fetch_models, model_entry
from wb_panel_routes import PanelRoutes
from wb_translate import anthropic_error_obj

_GATEWAY = None


def _g():
    """网关模块对象；属性每次现读，测试替换网关属性后立即生效。"""
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = wb_host.loaded_gateway("log", "USAGE_DIR", "configured_keys")
        if _GATEWAY is None:
            import wb_proxy as _GATEWAY
    return _GATEWAY

class Handler(PanelRoutes, ChatRoutes, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # 关闭 Nagle（对连接设 TCP_NODELAY）：响应头与响应体是两次独立 write，
    # Nagle 会攒住第二次小写等第一次的 ACK，遇上客户端 delayed ACK，面板几乎
    # 所有 <64KB 的 JSON 小响应都要白付一个 RTT。StreamRequestHandler.setup()
    # 原生支持这个开关。
    disable_nagle_algorithm = True
    # 本次请求用的 API Key 条目，由 _key_ok() 设置；其 realm 绑定决定本请求的上游出口。
    key_entry = None
    # 本请求的审计字段，由 _begin_audit() 设置，写入的每条用量记录都带上它们。
    audit = None
    # 本次 _json() 响应附带的 ETag，由 _json_cached() 设置。做成实例属性而非
    # 参数：测试桩大量只覆写 _json(code, obj) 两个位置参数，加参数会全部报错，
    # 属性默认 None 对它们无感。
    _json_etag = None
    # 标准库默认把请求行限在 64KB，超长只回一句裸的 "414 Request-URI Too
    # Long"。放大上限并用正常 JSON 错误形状回答，超长 URL 才可诊断。
    max_request_line = 1024 * 1024
    # 客户端连接的读写超时：只发请求头不发正文、或保持连接却不发下一个请求的
    # 连接到时关闭，不会永久占住处理线程；上游连接另有超时。
    timeout = int(os.environ.get("WB_CLIENT_TIMEOUT", 120))

    def handle_one_request(self):
        # keep-alive 上同一个 Handler 实例服务整条连接，鉴权状态必须每请求复位：
        # 面板 token 通过的请求不会重设 key_entry，不复位就会继承上一条请求所用
        # API Key 的出口绑定，被发到错误的上游出口。
        self.key_entry = None
        self.audit = None
        wb_usage.bind_audit(None)
        # ETag 同理：留着上一轮的值，下一次 _json() 会把别的请求的 ETag 发出去。
        self._json_etag = None
        # 正文读取标记同样每请求复位，否则后面的排空会跳过尚未读的正文。
        self._body_consumed = False
        try:
            self.raw_requestline = self.rfile.readline(self.max_request_line + 1)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True
            return
        except Exception:
            self.close_connection = True
            return
        if len(self.raw_requestline) > self.max_request_line:
            self.requestline = ''
            self.request_version = ''
            self.command = ''
            # 超长行只读了 max_request_line + 1 字节，余下数据还在 socket 里；
            # 不排空就回包并关连接，OS 会发 RST 丢掉已缓冲的应答，客户端只看到
            # 连接重置、看不到 414。先限长排空再回，414 才能送达。
            self._drain_oversized_request_line()
            try:
                self._error(414, "request line too long (limit %d bytes); "
                                 "put long content in the POST body, not the URL"
                            % self.max_request_line, "invalid_request_error")
            except Exception:
                pass
            self.close_connection = True
            return
        if not self.raw_requestline:
            self.close_connection = True
            return
        if not self.parse_request():
            return
        mname = 'do_' + self.command
        if not hasattr(self, mname):
            self.send_error(501, "Unsupported method (%r)" % self.command)
            return
        getattr(self, mname)()
        self.wfile.flush()

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def finish(self):
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    server_version = "wb-proxy-center/"  # 版本后缀由 wb_proxy 导入后补全

    def log_message(self, fmt, *args):
        # 看板高频轮询的正常 200 GET（/logs、/usage 等）不记日志：读日志本身
        # 会产生日志，会刷屏；4xx/5xx 与非 GET 操作照常记录。
        try:
            status_code = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 200
            if status_code < 400 and getattr(self, "command", "GET") == "GET":
                req_path = (getattr(self, "path", None) or (args[0] if args else "")).split("?")[0]
                quiet_prefixes = (
                    "/logs", "/usage", "/accounts", "/scheduler",
                    "/health", "/panel/status", "/realm", "/favicon.ico"
                )
                if any(req_path == p or req_path.startswith(p + "/") for p in quiet_prefixes):
                    return
        except Exception:
            pass
        _g().log(fmt % args)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 只有 _json_cached() 覆盖的只读接口才有校验符；其余调用点保持原样，
        # 一个头都不多发。
        etag = self._json_etag
        if etag is not None:
            # no-cache 而非 no-store：浏览器可存一份但每次回源校验，配合 ETag 命中
            # 即 304，省掉 10~100KB JSON 重传；no-store 连条件请求都无从谈起。
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
        # 超长请求行在 parse_request() 之前就被拒绝，self.path 尚未设置，回退成 ""。
        if _g().cors_origin_allowed(getattr(self, "path", "") or ""):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        # 在这里 flush 而不指望调用方：keep-alive 下客户端要等应答真正上线才
        # 返回，攒到 handler 末尾才刷会让错误应答看起来像请求挂死。
        try:
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _json_cached(self, code, data_fn, etag_fn):
        """带 TTL 缓存的只读接口：_json() + 条件请求，命中 If-None-Match 回 304。

        data_fn() 取数、etag_fn() 取 ETag，取数前后各取一次 ETag，只有两次相同
        才发出，相等即证明响应体出自该 ETag 描述的缓存条目。期间重建过就本次不发
        ETag、走普通 200。顺序不能反过来：新 ETag 配旧数据，客户端带着它命中 304
        会一直显示过期数字。两个可调用对象都无参，取值顺序由这里保证；任何异常都
        退化成无 ETag 的普通 200，不让响应路径崩掉。
        """
        try:
            before = etag_fn()
        except Exception:
            before = None
        data = data_fn()
        try:
            after = etag_fn()
        except Exception:
            after = None
        etag = after if (after is not None and after == before) else None
        if etag is not None:
            try:
                inm = self.headers.get("If-None-Match")
            except Exception:
                # 没有 headers 对象（测试桩 / 未走 parse_request）时按不匹配处理。
                inm = None
            if inm is not None and _g()._if_none_match_hit(inm, etag):
                # 304 不带 body 也不带 Content-Length：RFC 7230 §3.3.3 规定 304
                # 在空行处结束，报长度会诱使客户端/代理等待永不出现的 body。
                # ETag 与 Cache-Control 必须重发（RFC 7232 §4.1），否则缓存丢状态。
                self.send_response(304)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self._json_etag = etag
        try:
            return self._json(code, data)
        finally:
            # 无论 _json() 是否抛异常，都不能把这次请求的 ETag 留给下一次。
            self._json_etag = None

    def _discard_body(self):
        """排空请求正文，让连接保持同步。

        正文未读就被拒绝的 POST（401、404、面板路由）会把载荷留在 socket 里，
        keep-alive 上下一条请求会把残留 JSON 当请求行解析，正常连接上冒出假的
        "414 Request-URI Too Long"。Content-Length 与 chunked 都要处理，大正文
        的客户端会切到后者。
        """
        if getattr(self, "_body_consumed", False):
            # 正文已被处理器读过（例如 _read_payload 之后才报错）：再读一遍会
            # 阻塞到客户端放弃，即时应答变成挂死。
            return
        # 未解析的请求没有 headers 对象也无正文可排：超长请求行在 parse_request()
        # 之前被拒，此时读 self.headers 会从 _error() 里抛出，客户端连应答都拿不到。
        headers = getattr(self, "headers", None)
        if headers is None:
            return
        transfer_encoding = (headers.get("Transfer-Encoding") or "").lower()
        try:
            if "chunked" in transfer_encoding:
                self._drain_chunked_body()
                return
            length = int(headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return
        if length > _g().MAX_PAYLOAD_BYTES:
            # 客户端声明了超限正文（413）：读它会阻塞到对方发完，直接关闭，
            # 让客户端收到应答加断开。
            self.close_connection = True
            return
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except Exception:
            # 读到空说明对端已断开，无需再对齐。
            pass

    def _drain_chunked_body(self):
        """读掉 chunked 正文（以零长度块结束）。"""
        try:
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    return
                size_field = line.split(b";", 1)[0].strip()
                if not size_field:
                    continue
                size = int(size_field, 16)
                if size == 0:
                    # 可选的 trailer，然后是结尾空行。
                    while True:
                        trailer = self.rfile.readline(65536)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            return
                remaining = size
                while remaining > 0:
                    data = self.rfile.read(min(remaining, 65536))
                    if not data:
                        return
                    remaining -= len(data)
                self.rfile.read(2)  # 每个块数据后的 CRLF
        except Exception:
            self.close_connection = True

    # 超长请求行的排空预算：对端已经异常，排到正常客户端能看到 414 应答即可。
    OVERSIZED_DRAIN_LIMIT = 8 * 1024 * 1024

    def _drain_oversized_request_line(self):
        """限额排掉超长请求行的剩余部分。

        不排空则应答会被 RST 丢掉（见调用方）。换行即行尾；超出预算说明对端
        不会停，放弃并关闭连接。
        """
        budget = self.OVERSIZED_DRAIN_LIMIT
        try:
            while budget > 0:
                chunk = self.rfile.readline(min(budget, 65536))
                if not chunk:
                    return
                budget -= len(chunk)
                if chunk.endswith(b"\n"):
                    return
        except Exception:
            pass

    def _handle_expect_continue(self):
        """先回应 'Expect: 100-continue'，再决定拒绝正文。

        带这个头的客户端会等临时应答才发大载荷；直接拒绝或先排空会让两边
        互等到超时。
        """
        # 未解析的请求没有 headers 对象，也没有临时应答可发；此时读 self.headers
        # 会从调用方正在生成的错误应答里抛出。
        headers = getattr(self, "headers", None)
        if headers is None:
            return
        expect = (headers.get("Expect") or "").lower()
        if "100-continue" not in expect:
            return
        try:
            self.send_response_only(100)
            self.end_headers()
            self.wfile.flush()
        except Exception:
            pass

    def _error(self, code, message, err_type="server_error"):
        # 所有提前拒绝都走这里，统一排空正文。先解除 "Expect: 100-continue"
        # 的等待，否则客户端不发正文，排空会等永远不会到的数据。
        self._handle_expect_continue()
        self._discard_body()
        error = {"message": message, "type": err_type, "code": code}
        hint = _g().gateway_hint(code, message)
        if hint:
            error["gateway_hint"] = hint
        self._json(code, {"error": error})

    def _anthropic_error(self, code, message, err_type=None):
        """按 Anthropic JSON 错误封套回复，区别于 OpenAI 形状。"""
        self._handle_expect_continue()
        self._discard_body()
        return self._json(code, anthropic_error_obj(code, message, err_type))

    def _rate_limited(self, exc):
        """429 带 Retry-After，让客户端退避。上游正文给出重置时间时保留其措辞，
        否则回退到已知最短的模型冷却时间。
        """
        wait = max(1, int(getattr(exc, "wait", 60) or 60))
        # 未发上游请求就抛的 429（账号池被每日 token 闸门暂停）带自己的文案，
        # 其余保留上游措辞。
        custom = getattr(exc, "message", "")
        text = custom or (
            "upstream rate limit reached for this model; retry in %ds" % wait)
        # 上游 detail 只缀在上游文案后；本地文案缀上去只是重复。
        detail = ""
        if exc.detail and not custom:
            detail = " - " + exc.detail[:200]
        # 模型冷却在入口就检查，429 可能在正文未读时回答，所以同 _error 一样先排空。
        self._handle_expect_continue()
        self._discard_body()
        body = json.dumps({
            "error": {
                "message": text + detail,
                "type": "rate_limit_error",
                "code": 429,
                "retry_after": wait,
            }
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(wait))
        if _g().cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _anthropic_rate_limited(self, exc):
        """429 带 Retry-After，用 Anthropic 错误封套。"""
        wait = max(1, int(getattr(exc, "wait", 60) or 60))
        text = (getattr(exc, "message", "") or
                "upstream rate limit reached for this model; retry in %ds" % wait)
        if getattr(exc, "detail", "") and not getattr(exc, "message", ""):
            text += " - " + str(exc.detail)[:200]
        self._handle_expect_continue()
        self._discard_body()
        body = json.dumps(anthropic_error_obj(429, text, "rate_limit_error"),
                          ensure_ascii=False).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(wait))
        if _g().cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _download(self, filename, obj):
        """把 JSON 文档作为浏览器下载发送。
        文件名由用户可控部分（realm 过滤条件）生成，Content-Disposition 必须
        加引号，否则会破坏头或构成响应拆分。
        """
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        safe = re.sub(r'[^A-Za-z0-9._-]', "_", str(filename))[:120] or "export.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % safe)
        self.send_header("Cache-Control", "no-store")
        if _g().cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _supplied_key(self):
        """调用方出示的 key。

        接受客户端实际会发的写法：带或不带 "Bearer" 方案的 Authorization 头、
        部分 OpenAI 兼容客户端用的 x-api-key / api-key 头，以及看板在无法设头
        时回退用的 ?key= 查询参数。
        """
        # 鉴权方案名大小写不敏感（RFC 7235），"bearer"/"BEARER" 前缀也要剥掉，
        # 否则整串会被当成 key 去比对。
        header = (self.headers.get("Authorization") or "").strip()
        supplied = ""
        if header:
            scheme, _, value = header.partition(" ")
            if scheme.lower() == "bearer":
                supplied = value.strip()
            else:
                supplied = header
            # 容忍部分 SDK 加的引号包裹凭据。
            if len(supplied) >= 2 and supplied[0] == supplied[-1] and supplied[0] in "\"'":
                supplied = supplied[1:-1].strip()
        if supplied:
            return supplied
        for name in ("x-api-key", "api-key", "x-auth-token"):
            value = (self.headers.get(name) or "").strip()
            if value:
                return value
        # 浏览器顶层跳转设不了请求头，所以也接受查询参数里的 key：看板从其他
        # 设备打开时用它。
        try:
            query = parse_qs(urlparse(self.path).query)
            for name in ("key", "api_key", "api-key"):
                value = (query.get(name) or [""])[0].strip()
                if value:
                    return value
            return ""
        except Exception:
            return ""

    def _key_ok(self):
        """请求带了有效 key 或无需 key 时为真。"""
        # 面板会话通过即可解锁管理接口，浏览器因此不必把 API Key 存在 localStorage。
        if self._panel_ok():
            return True
        self.key_entry = _g().identify_key(self._supplied_key())
        if self.key_entry:
            return True
        if not _g().auth_required():
            return True
        return False

    def _key_realm(self):
        """本请求所用 key 绑定的出口，未绑定为 ""。"""
        return (self.key_entry or {}).get("realm") or ""

    def _key_id(self):
        """为本请求付费的 key 的设置 id，无则 None。

        None 覆盖面板会话与未配置任何 key 的部署两种真实情况，用量日志把它们
        与 key 字段出现前的记录区分开。
        """
        return (self.key_entry or {}).get("id") or None

    def _begin_audit(self, api):
        """在请求开始时一次性捕获审计字段。

        流式应答与 web 工具的后续轮次远在请求行读完后才结束，所以在这里记录，
        之后不再回读。
        """
        if self._panel_ok():
            key_id = "panel"
        else:
            key_id = (self.key_entry or {}).get("id") or ""
        try:
            ip = self.client_address[0] if self.client_address else ""
        except Exception:
            ip = ""
        self.audit = {"key_id": key_id, "ip": ip, "api": api, "status": None}
        return wb_usage.bind_audit(self.audit)

    def _key_limits_ok(self, is_messages=False):
        """拒绝已过期、不在 IP 白名单或超出配额的 key。

        在 key 验证通过后执行；面板会话豁免，因为测试台跑在操作者自己的登录上。
        返回 True 表示可继续，否则拒绝应答已写出。
        """
        if self._panel_ok():
            return True
        entry = self.key_entry
        if not entry:
            return True
        if wb_settings.key_expired(entry):
            reason = ("该 API Key 已于 %s 过期。请在面板「设置」页延长有效期，"
                      "或改用其它 Key。"
                      % time.strftime("%Y-%m-%d %H:%M",
                                      time.localtime(entry.get("expires_at") or 0)))
            return self._refuse_key_limit(401, reason, is_messages)
        client_ip = ""
        try:
            client_ip = self.client_address[0] if self.client_address else ""
        except Exception:
            client_ip = ""
        if not wb_settings.ip_allowed(entry.get("ip_allowlist"), client_ip):
            reason = ("请求来源 %s 不在 API Key「%s」的 IP 白名单内（白名单：%s）。"
                      "请在面板「设置」页修改该 Key 的白名单。"
                      % (client_ip or "未知",
                         entry.get("name") or "未命名",
                         "、".join(entry.get("ip_allowlist") or []) or "-"))
            return self._refuse_key_limit(403, reason, is_messages)
        if (entry.get("quota_tokens") or 0) or (entry.get("quota_credit") or 0):
            quota = wb_settings.key_quota_reason(
                entry, wb_usage.key_usage(entry.get("id") or "", entry.get("usage_reset_at") or 0))
            if quota:
                return self._refuse_key_limit(429, quota, is_messages)
        return True

    def _refuse_key_limit(self, code, reason, is_messages):
        """写出一条 key 限额的拒绝应答；Messages 接口用自己的错误形状。"""
        if is_messages:
            err_type = {401: "authentication_error", 403: "permission_error",
                        429: "rate_limit_error"}.get(code, "invalid_request_error")
            self._anthropic_error(code, reason, err_type)
        else:
            self._error(code, reason, "invalid_request_error")
        return False

    def _cross_realm_error(self, model, realm):
        """提前说明模型与出口不匹配，免得上游只回一个含糊的 403。
        gpt-6-astra 发到国内出口或 deepseek-v4-pro 发到国际出口都属此类，
        在这里指出该 key 绑定了哪个出口。
        """
        if not realm or not model:
            return ""
        owner = _g().exclusive_realm(model)
        if not owner or owner == realm:
            return ""
        name = (self.key_entry or {}).get("name") or "当前 Key"
        served = "国内版" if owner == "cn" else "国际版"
        used = "国内版" if realm == "cn" else "国际版"
        return ("模型 %s 只在%s提供，但「%s」绑定的是%s出口。"
                "请改用对应出口的 Key，或把该 Key 的出口改为「跟随面板切换」。"
                % (model, served, name, used))

    def _banned_model_error(self, model):
        """被封锁的模型直接报错，不碰上游、不扣任何点数。"""
        if not wb_settings.model_served(_g().ACCOUNTS_DIR, model):
            return _g().disabled_model_message(model)
        if not _g().is_model_banned(model):
            return ""
        return _g().banned_model_message(model)

    def _key_model_error(self, model):
        """按 key 的模型限制在请求到上游前拒绝。未列模型的 key 不受限，
        操作者没设限时这里是空操作。
        """
        entry = self.key_entry
        if not entry:
            return ""
        if wb_settings.key_allows_model(entry, model):
            return ""
        return _g().key_model_message(entry, model)

    def _request_realm(self, explicit=None):
        """为本请求选择上游出口。
        优先级：显式 realm 参数、key 绑定的出口、X-Realm 头、?realm= 查询参数、
        全局开关。返回 None 时 open_upstream() 按模型检测。
        """
        if explicit:
            return explicit
        bound = self._key_realm()
        if bound:
            return bound
        header = self.headers.get("X-Realm")
        if header:
            return header
        try:
            return parse_qs(urlparse(self.path).query).get("realm", [None])[0]
        except Exception:
            return None

    def _authorized(self):
        if self._key_ok():
            return True
        # 在错误里说明 key 的正确出示方式，掩码副本、带尾空白这类看似一致的
        # 错值可直接从客户端报错诊断；刻意不回显 key 名称与值。
        hint = ("send it as 'Authorization: Bearer <key>' or '?key=<key>'; "
                "copy the value from the panel's 设置 page")
        try:
            if not any(k.get("enabled") for k in _g().configured_keys()) and not _g().API_KEY:
                hint = ("no key is configured - open the dashboard and add one, "
                        "or restart with --api-key")
        except Exception:
            pass
        self._error(401, "invalid api key - " + hint, "invalid_request_error")
        return False

    # ---- web 面板访问 ----
    def _panel_token(self):
        """X-Panel-Token 头里的会话 token。只走头不走查询串：查询串里的 token
        会经浏览器历史、Referer 头与反向代理访问日志泄漏。
        """
        token = (self.headers.get("X-Panel-Token") or "").strip()
        return token

    def _panel_ok(self):
        return _g().PANEL.valid(self._panel_token())

    def _debug_account(self):
        """X-Debug-Account 指定的调试账号，空串表示交给账号池调度。

        只认面板会话：这个头是测试台用的，客户端 Key 带它一律忽略。
        """
        uid = (self.headers.get(_g().DEBUG_ACCOUNT_HEADER) or "").strip()
        if not uid or not self._panel_ok():
            return ""
        return uid

    def _stand_down_block(self, path):
        """网关停止时，会带账号令牌访问上游的入口一律 503。返回是否已处理。"""
        if not (_g().stand_down_active() and path in _g().STAND_DOWN_POST_PATHS):
            return False
        self._error(503, _g().stand_down_message(_g().GatewayStandDown(
            _g().STAND_DOWN.get("detail") or "")), "overloaded_error")
        return True

    @staticmethod
    def _is_panel_route(path):
        """web 面板的管理端点。模型清单仅凭 API Key 仍可访问，普通 OpenAI
        客户端可继续发现模型。
        """
        if path.startswith("/accounts"):
            return True
        if path.startswith("/usage") or path.startswith("/v1/usage"):
            return True
        if path.startswith("/activity"):
            return True
        if path.startswith("/tasks") or path.startswith("/scheduler"):
            return True
        if path.startswith("/settings"):
            return True
        if path.startswith("/updates"):
            return True
        if path.startswith("/logs"):
            return True
        if path == "/panel/models":
            return True
        # 控制台窗口显隐 / 服务关闭是本机操作者的面板功能，与 /updates、
        # /logs 同属 panel-only。
        if path.startswith("/console"):
            return True
        # 定价表是面板自己的估算视图，不属于 OpenAI 兼容面：写入
        # /pricing/refresh、/pricing/mapping 一直是 panel-only，读取侧返回的
        # 也是管理状态加解析出的项目内文件路径。
        if path.startswith("/pricing"):
            return True
        if path.startswith("/agents"):
            return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        if _g().cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        # 独立配置器，供不跑网关的机器上的客户端取用。放在面板门禁之前且刻意
        # 不开会话：需要它的机器恰好过不了 loopback 检查；正文是公开源码，
        # 与仓库里同一份 wb_agents.py，不含机密。
        if path == "/agents/cli":
            return self._get_agents_cli()
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        if path in ("/", "/dashboard", "/ui"):
            return self._get_dashboard()
        if path.startswith("/web/"):
            return self._get_web_asset(path[len("/web/"):])
        if path == "/panel/models":
            return self._get_panel_models(query)
        if path == "/usage/trend":
            return self._get_usage_trend(query)
        if path == "/accounts/credit-events":
            return self._get_accounts_credit_events(query)
        if path == "/panel/status":
            return self._get_panel_status()
        if path == "/console/state":
            return self._get_console_state()
        if path == "/health":
            return self._get_health()
        if path == "/realm":
            return self._get_realm()
        if path in ("/v1/models", "/models"):
            return self._get_v1_models()
        if path in ("/usage", "/v1/usage"):
            return self._get_v1_usage(query)
        if path == "/usage/recent":
            return self._get_usage_recent(query)
        if path == "/accounts/credits":
            return self._get_accounts_credits()
        if path == "/accounts/credits/grants":
            return self._get_account_credits_grants()
        if path == "/accounts":
            return self._get_accounts(query)
        if path == "/accounts/export":
            return self._get_accounts_export(query)
        if path == "/accounts/login/poll":
            return self._get_accounts_login_poll(query)
        if path == "/usage/analytics":
            return self._get_usage_analytics(query)
        if path == "/usage/cost-estimate":
            return self._get_usage_cost_estimate(query)
        if path == "/usage/month-credit":
            return self._get_usage_month_credit(query)
        if path == "/usage/by-account":
            return self._get_usage_by_account()
        if path == "/usage/remaining":
            return self._get_usage_remaining()
        if path == "/usage/perf":
            return self._get_usage_perf(query)
        if path == "/usage/timeseries":
            return self._get_usage_timeseries(query)
        if path == "/activity/history":
            return self._get_activity_history(query)
        if path == "/tasks":
            return self._get_tasks(query)
        if path == "/scheduler":
            return self._get_scheduler()
        if path == "/pricing":
            return self._get_pricing()
        if path == "/settings":
            return self._get_settings()
        if path == "/updates":
            return self._get_updates()
        if path == "/proxy/slots":
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            return self._json(200, {"slots": wb_proxy_slots.proxy_slots_view(_g().POOL, _g().ACCOUNTS_DIR)})
        if path == "/logs":
            return self._get_logs(query)
        if path == "/logs/export":
            return self._get_logs_export()
        if path == "/settings/reveal":
            return self._get_settings_reveal(query)
        if path == "/agents":
            return self._get_agents()
        if path == "/agents/available":
            # 看板启动时用这个廉价判定决定要不要显示入口：不 import
            # wb_agents、不做任何探测。
            return self._json(200, {"enabled": self._agents_client_allowed()})
        return self._error(404, "not found", "invalid_request_error")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/console/toggle":
            # panel-only 管理写入，与设置、定价写入同一道门。按钮带 `{}`
            # body，路由内负责排空（见 _route_console_toggle）。
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._route_console_toggle()
        if path == "/shutdown":
            # 面板顶栏的「关闭服务」：优雅退出（先回响应再 os._exit），
            # 端口随进程释放；再次启动走原来的启动脚本。
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._route_shutdown()
        if path == "/settings/save":
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._handle_settings_save()
        if path == "/updates/check":
            # 面板脚本按下按钮时会 POST 一个 `{}`；这个路由不读 body，必须
            # 排空，否则残留字节会把 keep-alive 上的下一条请求解析坏。
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            self._handle_expect_continue()
            self._discard_body()
            return self._route_update_check()
        if path in ("/pricing/refresh", "/pricing/mapping"):
            # panel-only 管理写入，定价表是面板自己的估算视图。注册在这里是
            # 因为下面的通用分发器只路由 chat 与 accounts 路径。
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            payload = self._payload_or_error()
            if payload is None:
                return
            if path == "/pricing/mapping":
                return self._route_pricing_mapping(payload)
            return self._route_pricing_refresh(payload)
        if path == "/catalog/refresh":
            # panel-only 管理写入，与定价那两个同门：会花一次上游 /v3/config
            # 调用并重发该出口的清单，所以只认面板会话。
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            payload = self._payload_or_error()
            if payload is None:
                return
            return self._route_catalog_refresh(payload)
        if path.startswith("/proxy/"):
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            payload = self._payload_or_error()
            if payload is None:
                return
            return self._handle_proxy_slots(path, payload)
        if path in ("/agents/apply", "/agents/restore"):
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            if not self._agents_client_allowed():
                return self._error(
                    403,
                    "agent config is only available from the machine running the gateway",
                    "invalid_request_error",
                )
            payload = self._payload_or_error()
            if payload is None:
                return
            if path == "/agents/apply":
                return self._handle_agents_apply(payload)
            return self._handle_agents_restore(payload)
        if path in ("/panel/login", "/panel/logout", "/panel/password"):
            return self._handle_panel(path)
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        if self._stand_down_block(path):
            return
        is_messages_route = path in (
            "/v1/messages", "/messages",
            "/v1/messages/count_tokens", "/messages/count_tokens",
        )
        is_account_route = (
            path.startswith("/accounts/")
            or path == "/realm"
            or path.startswith("/tasks")
            or path.startswith("/scheduler")
            or path.startswith("/logs")
        )
        if not is_account_route and not is_messages_route and path not in (
                "/v1/chat/completions", "/chat/completions",
                "/v1/completions", "/completions",
                "/v1/responses", "/responses"):
            return self._error(404, "not found", "invalid_request_error")
        if is_messages_route:
            if not self._key_ok():
                return self._anthropic_error(
                    401, "missing or invalid API key", "authentication_error")
        elif not self._authorized():
            return
        if not is_account_route:
            self._begin_audit("messages" if is_messages_route else wb_usage.chat_api_name(path))
            if not self._key_limits_ok(is_messages_route):
                return
        # Anthropic-to-Chat 桥接验证提示缓存一致性之前默认关闭、拒绝请求，
        # 且在转发或记录任何付费用量之前拒绝。
        if is_messages_route and not _g()._anthropic_messages_enabled():
            return self._anthropic_error(
                400,
                "Anthropic Messages is disabled by default: this gateway "
                "translates requests to Chat Completions and ignores "
                "cache_control. This can cause excessive credit consumption. "
                "Enable it in Settings → Advanced to opt in at your own risk "
                "(see issue #367).",
                "invalid_request_error")
        payload = self._payload_or_error(allow_list=(path == "/accounts/import"))
        if payload is None:
            return
        if is_account_route:
            return self._handle_accounts(path, payload)
        _g()._apply_background_model_alias(payload)
        # 检查这个 API Key 的并发上限
        key_ok, cur_key_conc, max_key_conc = _g()._acquire_key_slot(self.key_entry)
        key_slot_held = bool(key_ok and max_key_conc > 0)
        if not key_ok:
            key_name = (self.key_entry or {}).get("name") or self._key_id() or "当前 Key"
            message = ("Rate limit reached: API key '%s' reached its concurrency limit (%d in flight); retry shortly"
                       % (key_name, max_key_conc))
            wb_usage.record_error(payload.get("model") or "unknown", 429, message,
                         stream=payload.get("stream"), key=self._key_id())
            if is_messages_route:
                return self._anthropic_error(429, message, "rate_limit_error")
            return self._error(429, message, "rate_limit_error")

        try:
            if is_messages_route:
                if path.endswith("/count_tokens"):
                    return self._handle_messages_count_tokens(payload)
                if not _g()._chat_slots.acquire(timeout=_g().CHAT_SLOT_WAIT_SECONDS):
                    message = ("gateway is at its concurrent chat limit "
                               "(%d in flight); retry shortly" % _g().MAX_CONCURRENT_CHAT)
                    wb_usage.record_error(payload.get("model") or "unknown", 503, message,
                                 stream=payload.get("stream"), key=self._key_id())
                    return self._anthropic_error(503, message, "overloaded_error")
                try:
                    return self._handle_messages(payload)
                finally:
                    _g()._chat_slots.release()
            # 下面两条 OpenAI 形状的路由最长占线程 600 秒，全程持有一个并发
            # 槽位，在 finally 释放，提前返回（含客户端断开）也会归还。
            if not _g()._chat_slots.acquire(timeout=_g().CHAT_SLOT_WAIT_SECONDS):
                return self._error(503, "gateway is at its concurrent chat limit "
                                        "(%d in flight); retry shortly" % _g().MAX_CONCURRENT_CHAT)
            try:
                return self._dispatch_chat_post(path, payload)
            finally:
                _g()._chat_slots.release()
        finally:
            _g()._release_key_slot(self.key_entry, key_slot_held)

    def _read_chunked_body(self, max_bytes=None):
        """把 Transfer-Encoding: chunked 正文解码成字节。

        部分 OpenAI 兼容客户端用 chunked 流式发大请求，只读 Content-Length 会
        看到空正文并回 400 invalid JSON。
        """
        if max_bytes is None:
            max_bytes = _g().MAX_PAYLOAD_BYTES
        chunks = []
        total = 0
        while True:
            line = self.rfile.readline(65536)
            if not line:
                break
            size = _g()._chunk_size(line)
            if size is None:
                raise _g().BadJSON()
            if size == 0:
                # 读掉可选 trailer 直到结尾空行。
                while True:
                    trailer = self.rfile.readline(65536)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            total += size
            if total > max_bytes:
                # 剩下的块没读：连接上的数据流已无法继续解析，回 413 后关闭，_error 也不再排空
                self.close_connection = True
                self._body_consumed = True
                raise _g().BodyTooLarge(total)
            remaining = size
            while remaining > 0:
                data = self.rfile.read(min(remaining, 65536))
                if not data:
                    raise _g().BadJSON()
                chunks.append(data)
                remaining -= len(data)
            self.rfile.read(2)  # 块数据后的 CRLF
        self._body_consumed = True
        return b"".join(chunks)

    def _read_payload(self, max_bytes=None, allow_list=False):
        """把请求正文解析成 dict，allow_list 时也接受 list。
        超限或坏 JSON 抛 BodyTooLarge / BadJSON，各调用方统一处理这两种情况。
        """
        if max_bytes is None:
            max_bytes = _g().MAX_PAYLOAD_BYTES
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        try:
            if "chunked" in transfer_encoding:
                raw_bytes = self._read_chunked_body(max_bytes=max_bytes)
                data = json.loads(raw_bytes.decode("utf-8", "replace") or "{}")
                if isinstance(data, dict):
                    return data
                if allow_list and isinstance(data, list):
                    return data
                return {}
            length = int(self.headers.get("Content-Length") or 0)
        except (_g().BodyTooLarge, _g().BadJSON):
            raise
        except Exception:
            raise _g().BadJSON()
        if length > max_bytes:
            raise _g().BodyTooLarge(length)
        if length < 0:
            raise _g().BadJSON()
        try:
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            # 标记正文已取，避免之后的错误应答再排空同一批字节（那会永久阻塞）。
            self._body_consumed = True
            data = json.loads(raw or "{}")
        except Exception:
            raise _g().BadJSON()
        if isinstance(data, dict):
            return data
        if allow_list and isinstance(data, list):
            # 账号导入接口接受裸数组，这是手写文件最自然的形状。
            return data
        return {}

    def _payload_or_error(self, allow_list=False):
        """读正文；出错时回对应错误并返回 None。"""
        try:
            return self._read_payload(allow_list=allow_list)
        except _g().BodyTooLarge as exc:
            message = ("payload too large (%d bytes > %d limit)"
                       % (exc.length, _g().MAX_PAYLOAD_BYTES))
            if self._anthropic_route():
                self._anthropic_error(413, message, "invalid_request_error")
            else:
                self._error(413, message, "invalid_request_error")
            return None
        except _g().BadJSON:
            if self._anthropic_route():
                self._anthropic_error(400, "invalid JSON body", "invalid_request_error")
            else:
                self._error(400, "invalid JSON body", "invalid_request_error")
            return None

    def _anthropic_route(self):
        """请求路径属于 Anthropic Messages 路由时为真。

        分发前抛出的错误（读正文、大小上限）必须用 Anthropic 封套回答，
        否则 Claude Code 客户端会收到无法解析的 OpenAI 形状错误。
        """
        return self.path.split("?")[0] in (
            "/v1/messages", "/messages",
            "/v1/messages/count_tokens", "/messages/count_tokens",
        )

    def _get_health(self):
        # 无论鉴权与否都应答（启动器靠它探测运行中的实例），但账号身份只对
        # 鉴权通过的调用方暴露。
        rep = _g().current_account()
        info = {
            "ok": True,
            # 报告实际使用的出口，跟随面板切换。
            "realm": _g().CURRENT_REALM,
            "accounts": len(_g().POOL.accounts) if _g().POOL else 0,
            "accounts_ready": _g().POOL.count_ready() if _g().POOL else 0,
            "api_key_required": _g().auth_required(),
        }
        if self._key_ok():
            info.update({
                "uid": rep.uid if rep else None,
                "domain": rep.domain if rep else None,
                "issuer": wb_accounts.jwt_issuer(rep.access_token) if rep else None,
                "credential_file": os.path.basename(rep.path) if rep and rep.path else None,
                "expires_at": rep.expires_at if rep else None,
            })
        return self._json(200, info)

    def _get_realm(self):
        return self._json(200, {"current": _g().CURRENT_REALM, "options": ["intl", "cn"]})

    def _get_v1_models(self):
        if not self._authorized():
            return
        req_realm = self._request_realm() or _g().CURRENT_REALM
        try:
            entries = fetch_models(realm=req_realm)
        except Exception as exc:
            return self._error(502, str(exc))
        # Level 4 尽力而为：冷却期内至多后台预热一次本地缓存，离线部署继续
        # 用回退清单。
        try:
            wb_modelsdev.refresh_async(_g().ACCOUNTS_DIR, log=_g().log)
        except Exception:
            pass
        disabled = {m.strip().lower()
                    for m in wb_settings.disabled_models(_g().ACCOUNTS_DIR)}
        data = [model_entry(mid, meta) for mid, meta in entries
                if mid.strip().lower() not in disabled]
        payload = {"object": "list", "data": data,
                   "realm": req_realm or _g().CURRENT_REALM}
        # 来源说明（面板展示用）：这批清单是上游 API 实时取的，还是读的
        # 本机桌面缓存文件/内置快照，以及各自的时间戳。
        meta = catalog_meta(req_realm)
        if meta:
            payload["catalog"] = meta
        return self._json(200, payload)
