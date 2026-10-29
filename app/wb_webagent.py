# -*- coding: utf-8 -*-
# 网页端会话的 ACP 驱动：GET 沙箱 link 拿到 SSE 流与 Acp-Connection-Id，
# POST 同一 link 发 JSON-RPC 的 initialize / session/load / session/prompt，
# 再从 SSE 流收 session/update 直到本轮结束。只建会话、不接沙箱时，上游会一直
# 停在 CREATING，因此每日活跃必须由这里把这一轮真正跑完。

import base64
import http.client
import json
import queue
import threading
import time
import urllib.parse

PROTOCOL_VERSION = 1

#: 打卡只需要 agent 自己把话说完，不需要客户端提供文件系统/终端能力。
CLIENT_CAPABILITIES = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}


def _proxy_parts(proxy_url):
    """解析代理 URL，返回 host、port 与 CONNECT 头组成的元组。

    两种写法都要认：`http://user:pass@host:port`，以及无 scheme 的
    `user:pass@host:port`。凭据取值与 urllib.request.ProxyHandler.proxy_open
    一致，先 unquote 再 base64，同一个代理 URL 在桌面通道与网页通道生成同一份凭据。
    """
    raw = str(proxy_url or "").strip()
    parsed = urllib.parse.urlparse(raw)
    host, port = parsed.hostname, parsed.port
    user, password = parsed.username, parsed.password
    if not host:
        # 无 scheme 的写法 urlparse 认不出来，`user:pass@h:p` 会被当成 scheme。
        authority = raw.split("://")[-1].split("/")[0]
        userinfo, _, hostport = authority.rpartition("@")
        host, _, port_text = hostport.rpartition(":")
        if not host:                       # 没有端口
            host, port_text = hostport, ""
        try:
            port = int(port_text) if port_text else None
        except ValueError:
            port = None
        if userinfo:
            user, _, password = userinfo.partition(":")
    headers = {}
    if user and password:
        creds = "%s:%s" % (urllib.parse.unquote(user), urllib.parse.unquote(password))
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(
            creds.encode("utf-8")).decode("ascii")
    return host, port, headers


def _request_target(parsed):
    """path + query；沙箱链接的签名参数在 query 里，丢了就认证失败。"""
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return target


class AcpError(Exception):
    """驱动失败时抛出，调用方转成可读的错误文本。"""


class AcpChannel(object):
    """ACP 的 streamable-HTTP 传输：一条 SSE 收事件，POST 发请求。"""

    def __init__(self, link, token, user_agent, log=None, proxy="", timeout=30):
        self.link = link
        self.token = token
        self.user_agent = user_agent
        self.log = log or (lambda *a, **k: None)
        self.proxy = proxy or ""
        self.timeout = timeout
        self.connection_id = ""
        self.events = queue.Queue()
        self.updates = 0
        self.chunks = 0
        self._conn = None
        self._reader = None
        self._stop = threading.Event()

    # ---- 连接 ----------------------------------------------------------
    def _target(self):
        parsed = urllib.parse.urlparse(self.link)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise AcpError("沙箱地址不可用: %r" % self.link)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed, port

    def _connect(self):
        parsed, port = self._target()
        cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = cls(parsed.hostname, port, timeout=self.timeout)
        if self.proxy:
            # 账号配了代理时走 CONNECT，与 urlopen 那条路径保持一致。
            # 凭据必须显式塞进 CONNECT：urllib 的 ProxyHandler 会替我们加
            # Proxy-Authorization，裸 http.client + set_tunnel 不会；缺失时
            # 认证代理一律回 407。
            host, proxy_port, tunnel_headers = _proxy_parts(self.proxy)
            conn = cls(host, proxy_port or 80, timeout=self.timeout)
            conn.set_tunnel(parsed.hostname, port, headers=tunnel_headers)
        return conn

    def open(self):
        """建立 SSE 通道，返回服务端给的 Acp-Connection-Id。"""
        parsed, _port = self._target()
        conn = self._connect()
        conn.putrequest("GET", _request_target(parsed))
        conn.putheader("Accept", "text/event-stream")
        conn.putheader("Authorization", "Bearer " + self.token)
        conn.putheader("User-Agent", self.user_agent)
        conn.endheaders()
        resp = conn.getresponse()
        if resp.status != 200:
            conn.close()
            raise AcpError("SSE 通道返回 HTTP %s" % resp.status)
        connection_id = resp.getheader("Acp-Connection-Id") or ""
        if not connection_id:
            conn.close()
            raise AcpError("SSE 通道没有返回 Acp-Connection-Id")
        self._conn = conn
        self.connection_id = connection_id
        self._reader = threading.Thread(target=self._read_events, args=(resp,), daemon=True)
        self._reader.start()
        return connection_id

    def _read_events(self, resp):
        """把 SSE 流解析成 JSON-RPC 消息，塞进 self.events。"""
        try:
            while not self._stop.is_set():
                # 走 HTTPResponse 自己的 readline：分块编码在行中间切分时也能正确拼回
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue
                payload = text[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    message = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("method") == "session/update":
                    update = (message.get("params") or {}).get("update") or {}
                    self.updates += 1
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        self.chunks += 1
                self.events.put(message)
        except Exception as exc:      # 连接被关掉、超时等
            self.events.put({"__stream_error__": "%s: %s" % (type(exc).__name__, exc)})
        finally:
            self.events.put(None)     # 流结束的哨兵

    def request(self, method, params, request_id):
        """发一个 JSON-RPC 请求；成功返回 HTTP 状态码，正常是 202。"""
        parsed, _port = self._target()
        body = json.dumps({"jsonrpc": "2.0", "id": request_id,
                           "method": method, "params": params}).encode("utf-8")
        conn = self._connect()
        try:
            conn.putrequest("POST", _request_target(parsed))
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Accept", "application/json, text/event-stream")
            conn.putheader("Acp-Connection-Id", self.connection_id)
            conn.putheader("Authorization", "Bearer " + self.token)
            conn.putheader("User-Agent", self.user_agent)
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            conn.send(body)
            resp = conn.getresponse()
            resp.read()
            status = resp.status
        finally:
            conn.close()
        if status not in (200, 202):
            raise AcpError("%s 返回 HTTP %s" % (method, status))
        return status

    def close(self):
        self._stop.set()
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _set_model(channel, session_id, model, log):
    """把这一轮用的模型固定到会话上，与网页端的做法一致。

    建会话请求体里的 model 只写进会话的 manifest，真正决定这一轮用哪个模型的
    是这条 ACP 扩展请求：网页端在 session/load 之后、session/prompt 之前单独
    发它，客户端 SDK 里就是 extMethod("session/set_model", {sessionId,
    modelId})。少了它，会话跑的是账号的默认模型——网页端默认是 Auto，按付费
    模型计费，于是每天的活跃都在扣积分。

    失败不当致命错：这一轮还是要跑完，只是模型可能没切成，日志里留一句。
    """
    try:
        channel.request("session/set_model",
                        {"sessionId": session_id, "modelId": model}, 3)
        return True
    except Exception as exc:
        log("web agent: 设置模型 %s 失败 (%s)，本轮仍按会话默认模型跑" % (model, exc))
        return False


def _drain_stream_events(channel):
    """排空已到达的 SSE 事件，返回流错误；None 哨兵只表示流已结束。"""
    events = getattr(channel, "events", None)
    if events is None:
        return ""
    error = ""
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            return error
        if event is None:
            continue
        if isinstance(event, dict) and event.get("__stream_error__"):
            error = str(event["__stream_error__"])


def run_turn(link, token, session_id, cwd, prompt, user_agent, poll_status=None,
             wait_seconds=120, poll_interval=3, log=None, proxy="",
             channel_factory=AcpChannel, model=None):
    """接上沙箱、把这一轮跑完。

    poll_status 是调用方给的「这条会话现在什么状态」，状态来自调用方查询
    console API；看到 completed 就算跑完，failed/error 直接停。返回结果里带
    事件数与输出段数，方便在打卡结果里看出这一轮到底跑没跑。

    model 非空时，在 session/load 之后补一条 session/set_model —— 见 _set_model。
    """
    log = log or (lambda *a, **k: None)
    started = time.time()
    channel = channel_factory(link, token, user_agent, log=log, proxy=proxy)
    status = ""
    error = ""
    finished = False
    try:
        channel.open()
        channel.request("initialize",
                        {"protocolVersion": PROTOCOL_VERSION,
                         "clientCapabilities": CLIENT_CAPABILITIES}, 1)
        channel.request("session/load",
                        {"sessionId": session_id, "cwd": cwd or "/workspace",
                         "mcpServers": []}, 2)
        if model:
            _set_model(channel, session_id, model, log)
        channel.request("session/prompt",
                        {"sessionId": session_id,
                         "prompt": [{"type": "text", "text": prompt}]}, 4)
        deadline = time.time() + max(1, int(wait_seconds))
        while time.time() < deadline:
            stream_error = _drain_stream_events(channel)
            if stream_error:
                error = "SSE 通道断开: %s" % stream_error
                break
            if poll_status is not None:
                try:
                    status = str(poll_status() or "")
                except Exception as exc:
                    log("web agent: 状态查询失败 (%s)" % exc)
                if status == "completed":
                    finished = True
                    break
                if status in ("failed", "error"):
                    error = "会话状态=%s" % status
                    break
            if poll_interval:
                time.sleep(poll_interval)
    except AcpError as exc:
        error = str(exc)
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        channel.close()
    elapsed_ms = int((time.time() - started) * 1000)
    if not status and poll_status is not None:
        try:
            status = str(poll_status() or "")
        except Exception:
            pass
    if not finished and not error:
        error = "会话在 %ss 内没有跑完（状态=%s）" % (wait_seconds, status or "未知")
    ok = finished and not error
    return {"ok": ok, "status": status, "events": channel.updates,
            "chunks": channel.chunks, "elapsed_ms": elapsed_ms, "error": error}
