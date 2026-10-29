# -*- coding: utf-8 -*-
# Anthropic Messages 的 Responses 管线。
#
# 设置页的 messages_format=openai-responses 时，请求先翻译成 Responses 形状，
# 再由网关现有的 Responses 管线调用上游；回复再翻译回 Anthropic 的 message /
# SSE 事件。openai-completions 仍然走 wb_proxy 里的 Chat 管线。

import json
import time
import urllib.error

import wb_usage


# 运行中的网关模块。wb_proxy 导入本模块后调用 bind 注入自身；在这里 import
# wb_proxy 会在网关以 __main__ 运行时载入第二份副本。
P = None

# 本模块向网关索取的接口。bind 时逐个核对，接线缺东西在启动阶段即失败。
REQUIRED = (
    "CURRENT_REALM", "ContentRejected", "LocalNetworkError", "RateLimited",
    "_new_id", "aggregate_stream", "chat_to_response", "cors_origin_allowed",
    "custom_tool_names", "estimate_tokens", "extract_session_key",
    "follow_up_with_tool_results", "internal_calls_from_chat", "open_upstream",
    "prepare_dsh_tool_aliases", "prompt_fingerprint", "responses_to_chat", "restore_dsh_tool_names",
    "stream_responses_events", "sum_usage", "wb_webtools", "web_tools_active",
)


def bind(module):
    missing = [name for name in REQUIRED if not hasattr(module, name)]
    if missing:
        raise RuntimeError("网关缺少 Messages 管线依赖: %s" % ", ".join(missing))
    global P
    P = module


def _text(content, dump_unknown=False):
    """把消息内容摊平成字符串。

    dump_unknown 为真时（tool_result 的 content），非文本块按 JSON 展开，
    非字符串非列表的顶层值也整体序列化；否则这些输入都返回空串。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for block in content:
            if isinstance(block, str):
                pieces.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    pieces.append(str(block.get("text") or ""))
                elif dump_unknown and "content" in block:
                    pieces.append(json.dumps(block.get("content"),
                                             ensure_ascii=False))
        return "\n".join(piece for piece in pieces if piece)
    if not dump_unknown or content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _image_part(block):
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    stype = str(source.get("type") or "")
    if stype == "base64":
        data = str(source.get("data") or "")
        mime = str(source.get("media_type") or "image/png")
        if data:
            return {"type": "image_url",
                    "image_url": {"url": "data:%s;base64,%s" % (mime, data)}}
    elif stype == "url":
        url = str(source.get("url") or "")
        if url:
            return {"type": "image_url", "image_url": {"url": url}}
    return None


def _json_object(raw):
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"input": raw}
    return parsed if isinstance(parsed, dict) else {"input": parsed}


def _effort(budget):
    try:
        tokens = int(budget)
    except (TypeError, ValueError):
        return None
    if tokens <= 0:
        return None
    return "low" if tokens < 8192 else "high"


def _tools(tools):
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "description": str(tool.get("description") or ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        })
    return out


def _choice(choice):
    if not isinstance(choice, dict):
        return None, None
    ctype = str(choice.get("type") or "").strip().lower()
    disabled = choice.get("disable_parallel_tool_use")
    parallel = None if disabled is None else (not bool(disabled))
    if ctype in ("auto", "any", "none"):
        return ("required" if ctype == "any" else ctype), parallel
    if ctype == "tool":
        name = str(choice.get("name") or "").strip()
        if name:
            return {"type": "function", "name": name}, parallel
    return None, parallel


def messages_to_responses(payload):
    """Anthropic Messages 请求 -> Responses 请求体。"""
    out = {"model": payload.get("model")}
    instructions = _text(payload.get("system"))
    if instructions:
        out["instructions"] = instructions
    items = []
    for entry in payload.get("messages") or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "user")
        content = entry.get("content")
        if isinstance(content, str):
            items.append({
                "type": "message", "role": role,
                "content": [{"type": "input_text" if role != "assistant" else "output_text",
                             "text": content}],
            })
            continue
        parts, calls = [], []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, str):
                parts.append({"type": "input_text" if role != "assistant" else "output_text",
                              "text": block})
                continue
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype == "text":
                parts.append({"type": "input_text" if role != "assistant" else "output_text",
                              "text": str(block.get("text") or "")})
            elif btype == "image":
                part = _image_part(block)
                if part:
                    parts.append({"type": "input_image",
                                  "image_url": part["image_url"]["url"]})
            elif btype == "tool_use":
                calls.append({
                    "type": "function_call",
                    "call_id": str(block.get("id") or P._new_id("call_")),
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                })
            elif btype == "tool_result":
                items.append({
                    "type": "function_call_output",
                    "call_id": str(block.get("tool_use_id") or ""),
                    "output": _text(block.get("content"), dump_unknown=True),
                })
        if parts:
            items.append({"type": "message", "role": role, "content": parts})
        items.extend(calls)
    if items:
        out["input"] = items
    for key in ("temperature", "top_p", "stream"):
        if payload.get(key) is not None:
            out[key] = payload[key]
    if payload.get("max_tokens") is not None:
        out["max_output_tokens"] = payload["max_tokens"]
    tools = _tools(payload.get("tools"))
    if tools:
        out["tools"] = [{"type": "function", "name": fn["name"],
                         "description": fn["description"],
                         "parameters": fn["parameters"]} for fn in tools]
    resp_choice, parallel = _choice(payload.get("tool_choice"))
    if resp_choice is not None:
        out["tool_choice"] = resp_choice
    if parallel is not None:
        out["parallel_tool_calls"] = parallel
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and str(thinking.get("type") or "").lower() == "enabled":
        effort = _effort(thinking.get("budget_tokens"))
        if effort:
            out["reasoning"] = {"effort": effort}
    return out


def _usage(usage):
    if not usage:
        return {"input_tokens": 0, "output_tokens": 0}
    return {
        "input_tokens": usage.get("prompt_tokens") or 0,
        "output_tokens": usage.get("completion_tokens") or 0,
    }


def response_to_message(obj, model=None):
    """Responses 响应对象 -> Anthropic message。"""
    content = []
    for item in obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "")
        if itype == "reasoning":
            summary = item.get("summary")
            if isinstance(summary, list):
                text = "".join(str(part.get("text") or "") for part in summary
                               if isinstance(part, dict))
            else:
                text = str(summary or "")
            if text:
                content.append({"type": "thinking", "thinking": text, "signature": ""})
        elif itype == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    text = str(part.get("text") or "")
                    if text:
                        content.append({"type": "text", "text": text})
        elif itype == "function_call":
            content.append({
                "type": "tool_use",
                "id": str(item.get("call_id") or item.get("id") or P._new_id("toolu_")),
                "name": str(item.get("name") or ""),
                "input": _json_object(item.get("arguments")),
            })
    status = str(obj.get("status") or "completed")
    stop_reason = "max_tokens" if status == "incomplete" else "end_turn"
    if stop_reason == "end_turn" and any(b.get("type") == "tool_use" for b in content):
        stop_reason = "tool_use"
    return {
        "id": P._new_id("msg_"),
        "type": "message",
        "role": "assistant",
        "model": model or obj.get("model") or "",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _usage({"prompt_tokens": (obj.get("usage") or {}).get("input_tokens"),
                         "completion_tokens": (obj.get("usage") or {}).get("output_tokens")}),
    }


def _input_estimate(body):
    return max(1, P.estimate_tokens(json.dumps(body.get("messages") or [],
                                               ensure_ascii=False)))


def message_error_frame(err_type, message):
    body = json.dumps({"type": "error", "error": {"type": err_type, "message": message}},
                      ensure_ascii=False)
    return ("event: error\ndata: %s\n\n" % body).encode("utf-8")


class MessageStreamWriter:
    """把一个 Responses 流写成一条 Anthropic Messages SSE 流。"""

    def __init__(self, model, input_tokens=0):
        self.model = model
        self.message_id = P._new_id("msg_")
        self.input_tokens = input_tokens or 0
        self.usage = None
        self.stop_reason = None
        self.open_index = None
        self.next_index = 0
        self.text_index = None
        self.think_index = None
        self.text_parts = []
        self.reason_parts = []
        self.item_blocks = {}
        self.saw_output = False
        self.saw_tool_use = False
        self.saw_terminal = False
        self.failure = None

    def _frame(self, etype, payload):
        body = json.dumps(dict({"type": etype}, **payload), ensure_ascii=False)
        return ("event: %s\ndata: %s\n\n" % (etype, body)).encode("utf-8")

    def start(self):
        message = {
            "id": self.message_id,
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
        }
        return [self._frame("message_start", {"message": message})]

    def close_block(self):
        if self.open_index is None:
            return []
        index = self.open_index
        self.open_index = None
        return [self._frame("content_block_stop", {"index": index})]

    def open_block(self, block):
        frames = self.close_block()
        index = self.next_index
        self.next_index += 1
        self.open_index = index
        self.saw_output = True
        frames.append(self._frame("content_block_start",
                                  {"index": index, "content_block": block}))
        return frames, index

    def text_delta(self, piece):
        frames = []
        if self.text_index is None or self.open_index != self.text_index:
            frames, index = self.open_block({"type": "text", "text": ""})
            self.text_index = index
        self.text_parts.append(piece)
        frames.append(self._frame("content_block_delta", {
            "index": self.text_index, "delta": {"type": "text_delta", "text": piece},
        }))
        return frames

    def thinking_delta(self, piece):
        frames = []
        if self.think_index is None or self.open_index != self.think_index:
            frames, index = self.open_block({"type": "thinking", "thinking": "",
                                             "signature": ""})
            self.think_index = index
        self.reason_parts.append(piece)
        frames.append(self._frame("content_block_delta", {
            "index": self.think_index,
            "delta": {"type": "thinking_delta", "thinking": piece},
        }))
        return frames

    def responses_item_start(self, output_index, item):
        itype = str(item.get("type") or "")
        if itype == "reasoning":
            frames, index = self.open_block({"type": "thinking", "thinking": "",
                                             "signature": ""})
            self.think_index = index
        elif itype == "function_call":
            frames, index = self.open_block({
                "type": "tool_use",
                "id": str(item.get("call_id") or item.get("id") or P._new_id("toolu_")),
                "name": str(item.get("name") or ""),
                "input": {},
            })
            self.saw_tool_use = True
        else:
            return []
        self.item_blocks[output_index] = {"index": index, "sent": "", "flushed": False}
        return frames

    def responses_tool_delta(self, output_index, fragment, complete=None):
        entry = self.item_blocks.get(output_index)
        if entry is None:
            return []
        if fragment:
            entry["sent"] += fragment
        if complete is None or entry["flushed"]:
            return []
        entry["flushed"] = True
        text = str(complete) if complete else entry["sent"]
        if not text:
            return []
        return [self._frame("content_block_delta", {
            "index": entry["index"],
            "delta": {"type": "input_json_delta", "partial_json": text},
        })]

    def finish(self, usage=None, stop_reason=None):
        frames = self.close_block()
        if usage:
            self.usage = usage
        prompt, completion = self.final_usage()
        reason = stop_reason or self.stop_reason or "end_turn"
        if reason == "end_turn" and self.saw_tool_use:
            reason = "tool_use"
        frames.append(self._frame("message_delta", {
            "delta": {"stop_reason": reason, "stop_sequence": None},
            "usage": {"input_tokens": prompt, "output_tokens": completion},
        }))
        frames.append(self._frame("message_stop", {}))
        return frames

    def final_usage(self):
        prompt = self.input_tokens
        completion = 0
        if isinstance(self.usage, dict):
            if self.usage.get("prompt_tokens"):
                prompt = self.usage["prompt_tokens"]
            completion = self.usage.get("completion_tokens") or 0
        if not completion:
            completion = (P.estimate_tokens("".join(self.text_parts))
                          + P.estimate_tokens("".join(self.reason_parts)))
        return prompt or 0, completion or 0

    def usage_row(self):
        prompt, completion = self.final_usage()
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "completion_tokens_details": {
                "reasoning_tokens": P.estimate_tokens("".join(self.reason_parts))},
            "prompt_tokens_details": {"cached_tokens": 0},
        }

    def usage_for_log(self):
        usage = self.usage
        if not usage or (usage.get("total_tokens") or 0) == 0:
            usage = self.usage_row()
        return usage


def _parse_sse_frame(frame):
    text = frame.decode("utf-8", "replace") if isinstance(frame, (bytes, bytearray)) else str(frame)
    etype, payload = "", None
    for line in text.split("\n"):
        if line.startswith("event: "):
            etype = line[7:].strip()
        elif line.startswith("data: "):
            try:
                payload = json.loads(line[6:])
            except Exception:
                payload = None
    return etype, payload


def stream_responses_to_message(writer, frames):
    for frame in frames:
        if frame[:1] in (b":", ":"):
            yield frame
            continue
        etype, payload = _parse_sse_frame(frame)
        if not isinstance(payload, dict):
            continue
        if etype == "response.output_item.added":
            item = payload.get("item")
            if isinstance(item, dict):
                yield from writer.responses_item_start(payload.get("output_index"), item)
        elif etype == "response.output_text.delta":
            piece = payload.get("delta")
            if piece:
                yield from writer.text_delta(piece)
        elif etype == "response.reasoning_summary_text.delta":
            piece = payload.get("delta")
            if piece:
                yield from writer.thinking_delta(piece)
        elif etype == "response.function_call_arguments.delta":
            yield from writer.responses_tool_delta(payload.get("output_index"),
                                                   payload.get("delta"))
        elif etype == "response.function_call_arguments.done":
            yield from writer.responses_tool_delta(payload.get("output_index"), None,
                                                   complete=payload.get("arguments"))
        elif etype == "response.output_item.done":
            entry = writer.item_blocks.get(payload.get("output_index"))
            if entry is not None and writer.open_index == entry["index"]:
                yield from writer.close_block()
        elif etype == "response.completed":
            writer.saw_terminal = True
            response = payload.get("response") or {}
            if str(response.get("status") or "") == "incomplete":
                writer.stop_reason = "max_tokens"
        elif etype == "response.failed":
            error = (payload.get("response") or {}).get("error") or {}
            writer.failure = str(error.get("message") or "upstream reported an error")
            return
    if not writer.saw_terminal:
        writer.failure = "upstream stream ended before the terminal event"


def _stream_headers(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    if P.cors_origin_allowed(handler.path):
        handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()


def _record_usage(handler, model, usage, **kwargs):
    return wb_usage.record_usage(model, usage, key=handler._key_id(), **kwargs)


def _record_error(handler, model, status, message, **kwargs):
    return wb_usage.record_error(model, status, message, key=handler._key_id(), **kwargs)


def _stream_close(handler, writer, model, fp, account, t_start, first_ms, effort=None):
    if not writer.saw_terminal:
        _stream_aborted(handler, writer, model, fp, account, t_start, first_ms,
                        writer.failure or "upstream stream ended before the terminal event")
        return
    try:
        if writer.saw_output:
            for frame in writer.finish():
                handler.wfile.write(frame)
            handler.wfile.flush()
        else:
            handler.wfile.write(message_error_frame("api_error", "empty upstream stream"))
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    wall = int((time.time() - t_start) * 1000)
    _record_usage(handler, model, writer.usage_for_log(), stream=True, elapsed_ms=wall,
                  ttft_ms=first_ms,
                  gen_ms=(wall - first_ms) if first_ms is not None else None,
                  fp=fp, account=account.uid, effort=effort)


def _stream_aborted(handler, writer, model, fp, account, t_start, first_ms, message):
    wall = int((time.time() - t_start) * 1000)
    _record_error(handler, model, 502, message, elapsed_ms=wall,
                  account=account.uid, usage=writer.usage_for_log(), stream=True,
                  ttft_ms=first_ms,
                  gen_ms=(wall - first_ms) if first_ms is not None else None,
                  fp=fp, outcome="upstream_aborted")
    try:
        handler.wfile.write(message_error_frame("api_error", message))
        handler.wfile.flush()
    except Exception:
        pass


def _open_upstream(handler, chat_req, session_key, model, t_start):
    try:
        req_realm = handler._request_realm() or P.CURRENT_REALM
        for guard in (handler._cross_realm_error(chat_req.get("model"), req_realm),
                      handler._banned_model_error(chat_req.get("model")),
                      handler._key_model_error(chat_req.get("model"))):
            if guard:
                handler._anthropic_error(400, guard, "invalid_request_error")
                return None, None, None
        upstream, account, effort = P.open_upstream(chat_req, session_key=session_key,
                                                    target_realm=req_realm)
        return upstream, account, effort
    except P.ContentRejected as exc:
        _record_error(handler, model, 403, exc.detail[:200],
                      elapsed_ms=int((time.time() - t_start) * 1000),
                      account=getattr(exc, "account_uid", None))
        handler._anthropic_error(403, "upstream 403: %s" %
                                 (exc.detail or "content rejected"),
                                 "permission_error")
    except P.LocalNetworkError as exc:
        _record_error(handler, model, 503, exc.detail[:200],
                      elapsed_ms=int((time.time() - t_start) * 1000),
                      account=getattr(exc, "account_uid", None))
        handler._anthropic_error(
            503, "本机网络不可用：%s - 请检查本机网络/代理后重试"
                 "（本次不计入账号熔断）" % (exc.detail or "DNS/路由失败"),
            "api_error")
    except P.RateLimited as exc:
        _record_error(handler, model, 429, exc.detail[:200],
                      elapsed_ms=int((time.time() - t_start) * 1000),
                      account=getattr(exc, "account_uid", None))
        handler._anthropic_rate_limited(exc)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(600).decode("utf-8", "replace")
        except Exception:
            detail = str(exc)
        _record_error(handler, model, exc.code, detail,
                      elapsed_ms=int((time.time() - t_start) * 1000),
                      account=getattr(exc, "account_uid", None))
        handler._anthropic_error(exc.code,
                                 "upstream %s: %s" % (exc.code, detail))
    except Exception as exc:
        message = str(exc)
        no_account = message.startswith("no usable account")
        _record_error(handler, model, 503 if no_account else 502, message,
                      elapsed_ms=int((time.time() - t_start) * 1000),
                      account=getattr(exc, "account_uid", None))
        if no_account:
            handler._anthropic_error(503, message +
                                     " - add or enable one at the dashboard (/)",
                                     "overloaded_error")
        else:
            handler._anthropic_error(502, "upstream unreachable: %s" % exc)
    return None, None, None


def handle(handler, payload):
    """处理一条 messages_format=openai-responses 的 /v1/messages 请求。"""
    resp_req = messages_to_responses(payload)
    custom_names = P.custom_tool_names(resp_req.get("tools"))
    chat_req = P.responses_to_chat(resp_req)
    namespace_map = chat_req.pop("_namespace_map", None)
    tool_alias = {} if namespace_map else P.prepare_dsh_tool_aliases(chat_req)
    request_meta = {
        "tools": resp_req.get("tools") or [],
        "tool_choice": resp_req.get("tool_choice", "auto"),
        "parallel_tool_calls": resp_req.get("parallel_tool_calls", True),
    }
    model = resp_req.get("model") or "deepseek-v4.1-flash"
    want_stream = bool(payload.get("stream"))
    session_key = P.extract_session_key(handler.headers, payload)
    fp = P.prompt_fingerprint(chat_req.get("messages"))
    t_start = time.time()
    upstream, account, effort = _open_upstream(handler, chat_req, session_key, model, t_start)
    if upstream is None:
        return None
    with upstream:
        if want_stream:
            return _messages_stream(handler, upstream, model, custom_names, request_meta,
                                    chat_req, namespace_map, fp, account, t_start,
                                    session_key, effort, tool_alias=tool_alias)
        return _messages_nonstream(handler, upstream, model, custom_names, request_meta,
                                   chat_req, namespace_map, fp, account, t_start,
                                   session_key, effort, tool_alias=tool_alias)


def _messages_stream(handler, upstream, model, custom_names, request_meta, chat_req,
                     namespace_map, fp, account, t_start, session_key, effort,
                     tool_alias=None):
    _stream_headers(handler)
    writer = MessageStreamWriter(model, input_tokens=_input_estimate(chat_req))
    holder = {"usage": None, "custom_names": custom_names,
              "request_meta": request_meta, "namespace_map": namespace_map,
              "tool_alias": tool_alias,
              "base_body": chat_req, "base_messages": chat_req.get("messages"),
              "session_key": session_key,
              "realm": handler._request_realm() or P.CURRENT_REALM}
    first_ms = None
    total_usage = None
    try:
        for frame in writer.start():
            handler.wfile.write(frame)
        handler.wfile.flush()
        rounds = 0
        while True:
            holder.pop("internal_calls", None)
            holder.pop("suppress_completion", None)
            holder["suppress_lifecycle"] = rounds > 0
            frames = P.stream_responses_events(upstream, model, holder)
            for frame in stream_responses_to_message(writer, frames):
                if first_ms is None and frame[:1] != b":":
                    first_ms = int((time.time() - t_start) * 1000)
                handler.wfile.write(frame)
                handler.wfile.flush()
            total_usage = P.sum_usage(total_usage, holder.get("usage"))
            internal = holder.get("internal_calls") or []
            if not internal:
                break
            rounds += 1
            give_up = rounds > P.wb_webtools.MAX_WEB_ROUNDS
            try:
                upstream.close()
            except Exception:
                pass
            upstream, account, _ = P.follow_up_with_tool_results(
                internal, holder, model, session_key, t_start, drop_tools=give_up)
        if total_usage:
            writer.usage = total_usage
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        wall = int((time.time() - t_start) * 1000)
        _record_usage(handler, model, writer.usage_for_log(), stream=True, elapsed_ms=wall,
                      ttft_ms=first_ms,
                      gen_ms=(wall - first_ms) if first_ms is not None else None,
                      fp=fp, account=account.uid, effort=effort,
                      outcome="client_aborted")
        return None
    except Exception as exc:
        _stream_aborted(handler, writer, model, fp, account, t_start, first_ms,
                        "stream aborted: %s" % exc)
        return None
    finally:
        try:
            upstream.close()
        except Exception:
            pass
    _stream_close(handler, writer, model, fp, account, t_start, first_ms, effort)
    return None


def _messages_nonstream(handler, upstream, model, custom_names, request_meta, chat_req,
                        namespace_map, fp, account, t_start, session_key, effort,
                        tool_alias=None):
    sources = []
    rounds = 0
    web_tools = P.web_tools_active(chat_req)
    while True:
        try:
            chat_obj = P.aggregate_stream(upstream, model, None)
        except Exception as exc:
            _record_error(handler, model, 502, str(exc),
                          elapsed_ms=int((time.time() - t_start) * 1000),
                          account=account.uid)
            return handler._anthropic_error(502, "upstream stream error: %s" % exc)
        if tool_alias:
            P.restore_dsh_tool_names(chat_obj, tool_alias)
        calls = P.internal_calls_from_chat(chat_obj, web_tools=web_tools)
        if not calls:
            break
        rounds += 1
        give_up = rounds > P.wb_webtools.MAX_WEB_ROUNDS
        try:
            upstream.close()
        except Exception:
            pass
        holder = {"base_messages": chat_req.get("messages"), "base_body": chat_req,
                  "realm": handler._request_realm() or P.CURRENT_REALM,
                  "web_sources": sources}
        try:
            upstream, account, _ = P.follow_up_with_tool_results(
                calls, holder, model, session_key, t_start, drop_tools=give_up)
        except Exception as exc:
            _record_error(handler, model, 502, "web tool follow-up failed: %s" % exc,
                          elapsed_ms=int((time.time() - t_start) * 1000),
                          account=account.uid)
            return handler._anthropic_error(502, "web tool follow-up failed: %s" % exc)
        sources = holder.get("web_sources") or sources
    wall = int((time.time() - t_start) * 1000)
    result = P.chat_to_response(chat_obj, model, custom_names, request_meta, namespace_map,
                                sources=sources)
    _record_usage(handler, model, chat_obj.get("usage"), stream=False, elapsed_ms=wall,
                  fp=fp, account=account.uid, effort=effort)
    return handler._json(200, response_to_message(result, model))
