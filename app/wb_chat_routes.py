"""对话接口：Responses、Messages、Chat Completions 三条链路的处理方法。

方法以 ChatRoutes 混入 Handler，运行态与各域能力经 _g() 取用。
"""

import json
import time
import urllib.error

import wb_messages_responses
import wb_settings
import wb_usage
import wb_webtools

from wb_sanitize import (
    LeakedToolCallStreamFilter, chat_finish_chunk, chat_text_chunk,
    chat_tool_call_chunks, is_dsh_payload, prepare_dsh_tool_aliases,
    restore_dsh_tool_names,
)
from wb_translate import (
    _anthropic_estimate_chat_tokens, _responses_usage, additional_tools_from_input,
    aggregate_stream, anthropic_sse_frame, chat_to_messages, chat_to_response,
    clean_chunk, clean_responses_frame, custom_tool_names, estimate_tokens,
    follow_up_with_tool_results, heartbeat_frame, internal_calls_from_chat,
    iter_sse_lines, messages_to_chat, responses_to_chat, stream_messages_events,
    stream_responses_events, strip_data_prefix, sum_usage, upstream_error_text,
    web_tools_active,
)


def _g():
    """网关模块；与 wb_http._g 同一对象。函数内导入避免与 wb_http 形成导入环。"""
    import wb_http
    return wb_http._g()


class ChatRoutes(object):
    def _handle_responses(self, payload):
        """/v1/responses：翻译成 chat completions 后走上游。"""
        # 网关无状态，不存历史 response，无法回放上一轮；显式报 400。
        # 静默忽略 previous_response_id 会让续轮当新对话，客户端拿到缺上下文的正常回复。
        if payload.get("previous_response_id"):
            return self._error(
                400,
                "previous_response_id is not supported: this gateway does not "
                "store response state. Send the full conversation in 'input' "
                "instead, or use a stateless client.",
                "invalid_request_error")
        session_key = _g().extract_session_key(self.headers, payload)
        # 工具可能在顶层，也可能整份在 input 的 additional_tools 条目里。
        # 回程两处取值（custom_names、request_meta）必须与 responses_to_chat()
        # 出站合并后的工具清单一致，否则回程类型判错、客户端匹配不到执行器。
        all_tools = payload.get("tools")
        all_tools = list(all_tools) if isinstance(all_tools, list) else []
        all_tools.extend(additional_tools_from_input(payload))
        custom_names = custom_tool_names(all_tools)
        chat_req = responses_to_chat(payload)
        ns_map = chat_req.pop("_namespace_map", None)
        if ns_map:
            if bool(chat_req.get("tools")) and is_dsh_payload(chat_req):
                chat_req["_dsh_request"] = True
            tool_alias = {}
        else:
            tool_alias = prepare_dsh_tool_aliases(chat_req)
        # 回显到响应对象；见 chat_to_response。
        request_meta = {
            "tools": all_tools,
            "tool_choice": payload.get("tool_choice", "auto"),
            "parallel_tool_calls": payload.get("parallel_tool_calls", True),
        }
        model = payload.get("model") or "deepseek-v4.1-flash"
        want_stream = bool(payload.get("stream"))
        t_start = time.time()
        fp = _g().prompt_fingerprint(chat_req.get("messages"))
        _g().log(
            "responses: model=%s stream=%s msgs=%d effort=%r tools=%d/%d custom_tools=%s key=%s"
            % (model, want_stream, len(chat_req.get("messages") or []),
               chat_req.get("reasoning_effort"),
               len(chat_req.get("tools") or []), len(all_tools),
               sorted(custom_names) or "-", _g().key_label(self._key_id()) or "-")
        )
        try:
            req_realm = self._request_realm() or _g().CURRENT_REALM
            blocked = self._cross_realm_error(chat_req.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            banned = self._banned_model_error(chat_req.get("model"))
            if banned:
                return self._error(400, banned, "invalid_request_error")
            key_blocked = self._key_model_error(chat_req.get("model"))
            if key_blocked:
                return self._error(400, key_blocked, "invalid_request_error")
            upstream, account, effort = _g().open_upstream(
                chat_req, session_key=session_key, target_realm=req_realm)
        except _g().GatewayStandDown as exc:
            wb_usage.record_error(model, 503, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=exc.account_uid, key=self._key_id())
            return self._error(503, _g().stand_down_message(exc), "overloaded_error")
        except _g().ContentRejected as exc:
            wb_usage.record_error(model, 403, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(403, "upstream 403: %s" % (exc.detail or "content rejected"),
                               "invalid_request_error")
        except _g().LocalNetworkError as exc:
            wb_usage.record_error(model, 503, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(503, "本机网络不可用：%s - 请检查本机网络/代理后重试"
                                    "（本次不计入账号熔断）" % (exc.detail or "DNS/路由失败"),
                               "invalid_request_error")
        except _g().RateLimited as exc:
            t = time.time() - t_start
            wb_usage.record_error(model, 429, exc.detail[:200], elapsed_ms=int(t * 1000),
                         account=getattr(exc, "account_uid", None),
                         outcome="rate_limited", key=self._key_id())
            return self._rate_limited(exc)
        except urllib.error.HTTPError as exc:
            detail = _g().read_http_error(exc, 600).decode("utf-8", "replace")
            wb_usage.record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(exc.code, f"upstream {exc.code}: {detail}")
        except Exception as exc:
            message = str(exc)
            wb_usage.record_error(model, 502, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            if message.startswith("no usable account"):
                return self._error(503, message +
                                   " - add or enable one at the dashboard (/)")
            return self._error(502, f"upstream unreachable: {exc}")
        with upstream:
            if want_stream:
                return self._responses_stream_response(
                    upstream, model, custom_names, request_meta, fp, account, t_start, ns_map,
                    base_body=chat_req, session_key=session_key, realm=req_realm,
                    effort=effort, tool_alias=tool_alias)
            return self._responses_nonstream_response(
                upstream, model, custom_names, request_meta, fp, account, t_start, ns_map,
                base_body=chat_req, session_key=session_key, realm=req_realm,
                effort=effort, tool_alias=tool_alias)

    def _responses_stream_response(self, upstream, model, custom_names, request_meta, fp, account, t_start, namespace_map=None, base_body=None, session_key=None, realm=None, effort=None, lease=None, tool_alias=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if _g().cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        holder = {"usage": None, "custom_names": custom_names,
                  "request_meta": request_meta,
                  "namespace_map": namespace_map,
                  "tool_alias": tool_alias,
                  "base_body": base_body,
                  "base_messages": (base_body or {}).get("messages"),
                  "session_key": session_key,
                  "realm": realm}
        first_ms = None
        try:
            # 模型要 web_search / web_fetch 时由反向代理执行并把结果喂回再跑一轮，客户端只看到一则连续的回应。
            rounds = 0
            total_usage = None
            tools_withdrawn = False
            while True:
                holder.pop("internal_calls", None)
                holder.pop("suppress_completion", None)
                holder.pop("upstream_failure", None)
                holder["suppress_lifecycle"] = rounds > 0
                for frame in stream_responses_events(upstream, model, holder):
                    if first_ms is None:
                        first_ms = int((time.time() - t_start) * 1000)
                    self.wfile.write(clean_responses_frame(frame))
                    self.wfile.flush()
                # 每一轮的 token 都真实花掉，记账要加总
                total_usage = sum_usage(total_usage, holder.get("usage"))
                # 上游截断时翻译器已发过 response.failed，不再重放已流出的内容：重新请求会再次生成、再次计费。
                if holder.get("upstream_failure"):
                    break
                internal = holder.get("internal_calls") or []
                if not internal:
                    break
                if tools_withdrawn:
                    # 工具已收回后模型仍发同名调用：再跟一轮只耗上游配额，直接以失败收尾。
                    failure = "web tool rounds exceeded the limit"
                    holder["upstream_failure"] = failure
                    fail = holder.get("fail_frame")
                    if fail:
                        self.wfile.write(clean_responses_frame(fail(failure)))
                        self.wfile.flush()
                    break
                rounds += 1
                # 用完就收回工具，让模型自己收尾；这里不合成任何事件。
                give_up = rounds > wb_webtools.MAX_WEB_ROUNDS
                try:
                    upstream.close()
                except Exception:
                    pass
                upstream, account, _ = follow_up_with_tool_results(
                    internal, holder, model, session_key, t_start, drop_tools=give_up,
                    lease=lease)
                tools_withdrawn = give_up
            if total_usage:
                holder["usage"] = total_usage
            failure = holder.get("upstream_failure")
            if failure:
                wall = int((time.time() - t_start) * 1000)
                wb_usage.record_error(model, 502, failure,
                             elapsed_ms=wall, account=account.uid,
                             usage=holder.get("usage"), stream=True,
                             ttft_ms=first_ms,
                             gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, outcome="upstream_aborted", key=self._key_id())
                return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            wall = int((time.time() - t_start) * 1000)
            wb_usage.record_upstream_usage(upstream, model, holder.get("usage"), stream=True, elapsed_ms=wall,
                         ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, account=account.uid,
                         outcome="client_aborted", key=self._key_id(), effort=effort)
            return
        except Exception as exc:
            wall = int((time.time() - t_start) * 1000)
            message = "stream aborted: %s" % exc
            wb_usage.record_error(model, 502, message,
                         elapsed_ms=wall, account=account.uid,
                         usage=holder.get("usage"), stream=True,
                         ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, outcome="upstream_aborted", key=self._key_id())
            try:
                fail = holder.get("fail_frame")
                if fail:
                    self.wfile.write(clean_responses_frame(fail(message)))
                self.wfile.flush()
            except Exception:
                pass
            return
        finally:
            # 代跑多轮时 upstream 会被换掉，外层 with 只认得最初那条，最后一条在这里收掉。
            try:
                upstream.close()
            except Exception:
                pass
        wall = int((time.time() - t_start) * 1000)
        wb_usage.record_upstream_usage(upstream, model, holder.get("usage"), stream=True, elapsed_ms=wall,
                     ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid, key=self._key_id(), effort=effort)
        return

    def _responses_nonstream_response(self, upstream, model, custom_names, request_meta, fp, account, t_start, namespace_map=None, base_body=None, session_key=None, realm=None, effort=None, lease=None, tool_alias=None):
        # 同流式路径：客户端声明 web_search / web_fetch 时由反向代理代为执行，中间轮次对客户端不可见，最后组成一个 Responses 对象返回。
        # 不拦截的话 web_search 的 function_call 会漏给客户端，客户端只会回一句 unsupported call。
        sources = []
        rounds = 0
        # 开关关闭时不拦同名调用：那是客户端自己的工具。
        web_tools = web_tools_active(base_body)
        # 多轮 follow-up 共用一份会话，否则第二轮起重复搜索或答非所问：
        # 每轮重建会把上一轮的 assistant tool_call / tool 结果从上下文里抹掉。
        holder = {"base_messages": (base_body or {}).get("messages"),
                  "base_body": base_body, "realm": realm,
                  "web_sources": sources}
        total_usage = None
        tools_withdrawn = False
        first_at = None
        while True:
            try:
                chat_obj = aggregate_stream(upstream, model, None)
            except Exception as exc:
                wb_usage.record_error(model, 502, str(exc),
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid, key=self._key_id())
                return self._error(502, f"upstream stream error: {exc}")
            total_usage = sum_usage(total_usage, chat_obj.get("usage"))
            if first_at is None:
                first_at = chat_obj.get("first_chunk_at")
            if tool_alias:
                restore_dsh_tool_names(chat_obj, tool_alias)
            calls = internal_calls_from_chat(chat_obj, web_tools=web_tools)
            if not calls:
                break
            if tools_withdrawn:
                wb_usage.record_error(model, 502, "web tool rounds exceeded the limit",
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid, key=self._key_id())
                return self._error(502, "web tool rounds exceeded the limit")
            rounds += 1
            give_up = rounds > wb_webtools.MAX_WEB_ROUNDS
            try:
                upstream.close()
            except Exception:
                pass
            try:
                upstream, account, _ = follow_up_with_tool_results(
                    calls, holder, model, session_key, t_start, drop_tools=give_up,
                    lease=lease)
                tools_withdrawn = give_up
            except Exception as exc:
                wb_usage.record_error(model, 502, "web tool follow-up failed: %s" % exc,
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid, key=self._key_id())
                return self._error(502, "web tool follow-up failed: %s" % exc)
            sources = holder.get("web_sources") or sources
        wall = int((time.time() - t_start) * 1000)
        first_ms = int((first_at - t_start) * 1000) if first_at else None
        result = chat_to_response(chat_obj, model, custom_names, request_meta, namespace_map,
                                  sources=sources)
        cumulative = _responses_usage(total_usage)
        if cumulative:
            result["usage"] = cumulative
        wb_usage.record_upstream_usage(upstream, model, total_usage, stream=False, elapsed_ms=wall,
                     ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid, key=self._key_id(), effort=effort)
        return self._json(200, result)

    def _handle_messages_count_tokens(self, payload):
        """尽力估算的 Anthropic count_tokens 端点：上游没有 Anthropic tokenizer，
        用与网关记账相同的 CJK 感知估算，保持原生响应形状，
        刻意不冒充官方模型 token 计数。
        """
        try:
            chat = messages_to_chat(payload)
        except ValueError as exc:
            return self._anthropic_error(400, str(exc), "invalid_request_error")
        return self._json(200, {"input_tokens": _anthropic_estimate_chat_tokens(chat)})

    def _handle_messages(self, payload):
        """按选定管线处理 Anthropic Messages 请求。"""
        if payload.get("max_tokens") is None:
            return self._anthropic_error(400, "max_tokens is required",
                                         "invalid_request_error")
        if not isinstance(payload.get("messages"), list) or not payload.get("messages"):
            return self._anthropic_error(400, "messages must be a non-empty array",
                                         "invalid_request_error")
        if wb_settings.messages_format(_g().ACCOUNTS_DIR) == "openai-responses":
            return wb_messages_responses.handle(self, payload)
        try:
            chat_req = messages_to_chat(payload)
        except ValueError as exc:
            return self._anthropic_error(400, str(exc), "invalid_request_error")
        tool_alias = prepare_dsh_tool_aliases(chat_req)
        model = chat_req.get("model") or "unknown"
        want_stream = bool(chat_req.get("stream"))
        session_key = _g().extract_session_key(self.headers, chat_req)
        t_start = time.time()
        fp = _g().prompt_fingerprint(chat_req.get("messages"))
        _g().log("messages: model=%s stream=%s msgs=%d effort=%r tools=%d key=%s"
            % (model, want_stream, len(chat_req.get("messages") or []),
               chat_req.get("reasoning_effort"), len(chat_req.get("tools") or []),
               _g().key_label(self._key_id()) or "-"))
        try:
            req_realm = self._request_realm() or _g().CURRENT_REALM
            blocked = self._cross_realm_error(chat_req.get("model"), req_realm)
            if blocked:
                return self._anthropic_error(400, blocked, "invalid_request_error")
            banned = self._banned_model_error(chat_req.get("model"))
            if banned:
                return self._anthropic_error(400, banned, "invalid_request_error")
            key_blocked = self._key_model_error(chat_req.get("model"))
            if key_blocked:
                return self._anthropic_error(400, key_blocked, "invalid_request_error")
            upstream, account, effort = _g().open_upstream(
                chat_req, session_key=session_key, target_realm=req_realm)
        except _g().GatewayStandDown as exc:
            wb_usage.record_error(model, 503, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=exc.account_uid, key=self._key_id())
            return self._anthropic_error(503, _g().stand_down_message(exc), "overloaded_error")
        except _g().ContentRejected as exc:
            wb_usage.record_error(model, 403, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._anthropic_error(403, "upstream 403: %s" %
                                          (exc.detail or "content rejected"),
                                          "permission_error")
        except _g().LocalNetworkError as exc:
            wb_usage.record_error(model, 503, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._anthropic_error(503, "本机网络不可用：%s - 请检查本机网络/代理后重试"
                                              "（本次不计入账号熔断）"
                                              % (exc.detail or "DNS/路由失败"),
                                         "api_error")
        except _g().RateLimited as exc:
            wb_usage.record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         outcome="rate_limited", key=self._key_id())
            return self._anthropic_rate_limited(exc)
        except urllib.error.HTTPError as exc:
            try:
                detail = _g().read_http_error(exc, 600).decode("utf-8", "replace")
            except Exception:
                detail = str(exc)
            wb_usage.record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._anthropic_error(exc.code,
                                         "upstream %s: %s" % (exc.code, detail))
        except Exception as exc:
            message = str(exc)
            low = message.lower()
            status = 503 if ("no account" in low or "no enabled account" in low) else 502
            wb_usage.record_error(model, status, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            if status == 503:
                message += " - add or enable one at the dashboard (/)"
            return self._anthropic_error(status, message)
        with upstream:
            if want_stream:
                return self._messages_stream_response(
                    upstream, model, fp, account, t_start,
                    base_body=chat_req, session_key=session_key,
                    realm=req_realm, effort=effort, tool_alias=tool_alias)
            return self._messages_nonstream_response(
                upstream, model, fp, account, t_start,
                base_body=chat_req, session_key=session_key,
                realm=req_realm, effort=effort, tool_alias=tool_alias)

    def _messages_nonstream_response(self, upstream, model, fp, account, t_start,
                                     base_body=None, session_key=None, realm=None,
                                     effort=None, tool_alias=None):
        try:
            chat_obj = aggregate_stream(upstream, model, None)
            if tool_alias:
                restore_dsh_tool_names(chat_obj, tool_alias)
            result = chat_to_messages(chat_obj)
            wall = int((time.time() - t_start) * 1000)
            wb_usage.record_upstream_usage(upstream, model, chat_obj.get("usage"), stream=False, elapsed_ms=wall,
                         fp=fp, account=account.uid, key=self._key_id(), effort=effort)
            return self._json(200, result)
        except Exception as exc:
            wall = int((time.time() - t_start) * 1000)
            wb_usage.record_error(model, 502, "messages upstream error: %s" % exc,
                         elapsed_ms=wall, account=account.uid, key=self._key_id())
            return self._anthropic_error(502, "upstream stream error: %s" % exc)
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _messages_stream_response(self, upstream, model, fp, account, t_start,
                                  base_body=None, session_key=None, realm=None,
                                  session_meta=None, effort=None, tool_alias=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if _g().cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        holder = {"usage": None}
        first_ms = None
        try:
            for frame in stream_messages_events(upstream, model, holder,
                                                tool_alias=tool_alias):
                if first_ms is None:
                    first_ms = int((time.time() - t_start) * 1000)
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            wall = int((time.time() - t_start) * 1000)
            wb_usage.record_upstream_usage(upstream, model, holder.get("usage"), stream=True, elapsed_ms=wall,
                         ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, account=account.uid,
                         outcome="client_aborted", key=self._key_id(), effort=effort)
            return
        except Exception as exc:
            wall = int((time.time() - t_start) * 1000)
            wb_usage.record_error(model, 502, "messages stream aborted: %s" % exc,
                         elapsed_ms=wall, account=account.uid,
                         usage=holder.get("usage"), stream=True,
                         ttft_ms=first_ms, fp=fp, outcome="upstream_aborted",
                         key=self._key_id())
            try:
                self.wfile.write(anthropic_sse_frame("error", {
                    "error": {"type": "api_error",
                              "message": "messages stream aborted: %s" % exc}}))
                self.wfile.flush()
            except Exception:
                pass
            return
        finally:
            try:
                upstream.close()
            except Exception:
                pass
        wall = int((time.time() - t_start) * 1000)
        if holder.get("upstream_failure"):
            wb_usage.record_error(model, 502, holder["upstream_failure"],
                         elapsed_ms=wall, account=account.uid,
                         usage=holder.get("usage"), stream=True, ttft_ms=first_ms,
                         fp=fp, outcome="upstream_aborted", key=self._key_id())
            return
        wb_usage.record_upstream_usage(upstream, model, holder.get("usage"), stream=True, elapsed_ms=wall,
                     ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid, key=self._key_id(), effort=effort)
        return

    def _dispatch_chat_post(self, path, payload):
        # 先挡背景请求：Codex 自己发的记忆整理、环境建议、自动复核不算实际使用，一律本地拒绝，不碰上游。
        if _g().BLOCK_BACKGROUND_REQUESTS:
            reason = _g().background_request_reason(payload)
            if reason:
                try:
                    _g().log("background request blocked: model=%s trigger=(%s)"
                        % (payload.get("model"), reason), level="INFO")
                except Exception:
                    pass
                return self._error(400, _g().background_request_message(reason),
                                   "invalid_request_error")
        if path in ("/v1/responses", "/responses"):
            return self._handle_responses(payload)
        # 诊断日志：客户端实际请求与转发内容。
        # 只记影响行为的开关，绝不记消息正文。
        dsh_request = bool(payload.get("tools")) and is_dsh_payload(payload)
        tool_alias = prepare_dsh_tool_aliases(payload)
        forwarded = _g().build_upstream_body(payload, dsh_request=dsh_request)
        given = _g().client_effort_of(payload) or payload.get("reasoning") \
            or payload.get("thinking") or payload.get("enable_thinking")
        _g().log(
            "chat: model=%s client_effort=%r -> upstream_effort=%r stream=%s msgs=%d key=%s"
            % (
                payload.get("model"),
                given,
                _g().upstream_effort_of(forwarded, payload.get("model")),
                bool(payload.get("stream")),
                len(forwarded.get("messages") or []),
                _g().key_label(self._key_id()) or "-",
            )
        )
        session_key = _g().extract_session_key(self.headers, payload)
        fp = _g().prompt_fingerprint(forwarded.get("messages"))
        want_stream = bool(payload.get("stream"))
        model = payload.get("model") or "hy4-preview"
        t_start = time.time()
        try:
            req_realm = self._request_realm() or _g().CURRENT_REALM
            blocked = self._cross_realm_error(payload.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            banned = self._banned_model_error(payload.get("model"))
            if banned:
                return self._error(400, banned, "invalid_request_error")
            key_blocked = self._key_model_error(payload.get("model"))
            if key_blocked:
                return self._error(400, key_blocked, "invalid_request_error")
            upstream, account, effort = _g().open_upstream(
                payload, session_key=session_key, target_realm=req_realm,
                prebuilt_body=forwarded)
        except _g().GatewayStandDown as exc:
            wb_usage.record_error(model, 503, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=exc.account_uid, key=self._key_id())
            return self._error(503, _g().stand_down_message(exc), "overloaded_error")
        except _g().ContentRejected as exc:
            wb_usage.record_error(model, 403, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(403, "upstream 403: %s" % (exc.detail or "content rejected"),
                               "invalid_request_error")
        except _g().LocalNetworkError as exc:
            wb_usage.record_error(model, 503, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(503, "本机网络不可用：%s - 请检查本机网络/代理后重试"
                                    "（本次不计入账号熔断）" % (exc.detail or "DNS/路由失败"),
                               "invalid_request_error")
        except _g().RateLimited as exc:
            wb_usage.record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         outcome="rate_limited", key=self._key_id())
            return self._rate_limited(exc)
        except urllib.error.HTTPError as exc:
            detail = _g().read_http_error(exc, 600).decode("utf-8", "replace")
            wb_usage.record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(exc.code, f"upstream {exc.code}: {detail}")
        except Exception as exc:
            message = str(exc)
            wb_usage.record_error(model, 502, message, elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            if message.startswith("no usable account"):
                # 只有池子真空或冷却才报 503；被限流的模型由上面的 _rate_limited 报 429。
                return self._error(503, message +
                                   " - add or enable one at the dashboard (/)")
            return self._error(502, f"upstream unreachable: {exc}")
        with upstream:
            if want_stream:
                return self._chat_stream_response(
                    upstream, model, fp, account, t_start, effort=effort,
                    tool_alias=tool_alias)
            return self._chat_nonstream_response(
                upstream, model, fp, account, t_start, effort=effort,
                tool_alias=tool_alias)

    def _chat_stream_response(self, upstream, model, fp, account, t_start, effort=None,
                              tool_alias=None):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            if _g().cors_origin_allowed(self.path):
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            emitted = False
            last_usage = None
            first_ms = None
            streamed_text = []
            leak_filter = LeakedToolCallStreamFilter()
            synthesized_calls = False
            pending_finish = None
            saw_end = False
            # 写出缓冲：一个读取块里的事件合成一次 write。wfile 是 wbufsize=0 的无缓冲文件（StreamRequestHandler 默认），每个 chunk 直接一次 sendall。
            # 攒到下一个可能阻塞的读之前再写不改变延迟：块边界即 socket 当前全部字节（见 iter_sse_lines 的 before_read）。
            out_buf = []

            def flush_out():
                if out_buf:
                    self.wfile.write(b"".join(out_buf))
                    self.wfile.flush()
                    del out_buf[:]

            def write_chunk(payload):
                nonlocal emitted, first_ms
                if first_ms is None:
                    first_ms = int((time.time() - t_start) * 1000)
                emitted = True
                out_buf.append(f"data: {payload}\n\n".encode("utf-8"))

            def release(actions):
                nonlocal synthesized_calls
                for action in actions:
                    if action[0] == "text":
                        if action[1]:
                            write_chunk(chat_text_chunk(model, action[1]))
                    else:
                        synthesized_calls = True
                        calls = action[1]
                        if tool_alias:
                            for call in calls:
                                if isinstance(call, dict) and call.get("name") in tool_alias:
                                    call["name"] = tool_alias[call["name"]]
                        for payload in chat_tool_call_chunks(model, calls):
                            write_chunk(payload)

            try:
                for line in iter_sse_lines(upstream, before_read=flush_out):
                    hb = heartbeat_frame(line)
                    if hb is not None:
                        out_buf.append(hb)
                        continue
                    data = strip_data_prefix(line.decode("utf-8", "replace"))
                    if not data or data.startswith(":"):
                        continue
                    if data == "[DONE]":
                        saw_end = True
                        continue
                    maybe = None
                    try:
                        maybe = json.loads(data)
                    except Exception:
                        maybe = None
                    if maybe is not None and tool_alias:
                        restore_dsh_tool_names(maybe, tool_alias)
                    if isinstance(maybe, dict):
                        failure = upstream_error_text(maybe)
                        if failure:
                            flush_out()
                            err = json.dumps({"error": {"message": failure,
                                                        "type": "server_error"}})
                            self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
                            self.wfile.flush()
                            wall = int((time.time() - t_start) * 1000)
                            wb_usage.record_error(model, 502, failure, elapsed_ms=wall,
                                         account=account.uid, usage=last_usage,
                                         stream=True, ttft_ms=first_ms,
                                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                                         fp=fp, outcome="upstream_aborted",
                                         key=self._key_id())
                            return
                        u = maybe.get("usage")
                        if u:
                            if last_usage is None or (u.get("total_tokens") or 0) >= (last_usage.get("total_tokens") or 0):
                                last_usage = u
                        for ch in (maybe.get("choices") or []):
                            delta = ch.get("delta") or {}
                            if delta.get("reasoning_content"):
                                streamed_text.append(delta["reasoning_content"])
                            piece = delta.get("content")
                            if piece:
                                streamed_text.append(piece)
                                # 扣住文本不发：模型可能把工具调用当普通文本打印出来，
                                # 客户端当散文读完这轮就无事可做，表现为它自己不停机。
                                delta.pop("content", None)
                                release(leak_filter.feed(piece))
                            if ch.get("finish_reason"):
                                # 终止帧也扣住：这轮是否以 tool_calls 收尾，要等文本冲刷完才知道。
                                pending_finish = ch["finish_reason"]
                                saw_end = True
                                ch["finish_reason"] = None
                    cleaned = clean_chunk(
                        json.dumps(maybe, ensure_ascii=False)
                        if isinstance(maybe, dict) else data)
                    if not cleaned:
                        continue
                    write_chunk(cleaned)
                release(leak_filter.flush())
                if not saw_end:
                    flush_out()
                    message = "upstream stream ended before the terminal event"
                    err = json.dumps({"error": {"message": message,
                                                "type": "server_error"}})
                    self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    wall = int((time.time() - t_start) * 1000)
                    wb_usage.record_error(model, 502, message, elapsed_ms=wall,
                                 account=account.uid, usage=last_usage, stream=True,
                                 ttft_ms=first_ms,
                                 gen_ms=(wall - first_ms) if first_ms is not None else None,
                                 fp=fp, outcome="upstream_aborted", key=self._key_id())
                    return
                if synthesized_calls or pending_finish is not None:
                    write_chunk(chat_finish_chunk(
                        model, "tool_calls" if synthesized_calls else pending_finish))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # 客户端断开，仍要记账上游产出。
                wall = int((time.time() - t_start) * 1000)
                wb_usage.record_upstream_usage(upstream, model, last_usage, stream=True,
                             elapsed_ms=wall, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, account=account.uid,
                             outcome="client_aborted", key=self._key_id(), effort=effort)
                return
            except Exception as exc:
                # 上游中途断流：超时、读不全等。
                # 不拦的话客户端拿到没有终止标记的截断流，堆栈还会漏到 HTTP 层。
                wall = int((time.time() - t_start) * 1000)
                wb_usage.record_error(model, 502, "stream aborted: %s" % exc,
                             elapsed_ms=wall, account=account.uid,
                            usage=last_usage, stream=True, ttft_ms=first_ms,
                            gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, outcome="upstream_aborted", key=self._key_id())
                try:
                    flush_out()
                    err = json.dumps({"error": {"message": "stream aborted: %s" % exc,
                                                "type": "server_error"}})
                    self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    pass
                return
            flush_out()
            if not emitted:
                err = json.dumps({"error": {"message": "empty upstream stream", "type": "server_error"}})
                self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            wall = int((time.time() - t_start) * 1000)
            if last_usage is None or (last_usage.get("total_tokens") or 0) == 0:
                full_s = "".join(streamed_text)
                if full_s:
                    comp = estimate_tokens(full_s)
                    last_usage = {
                        "prompt_tokens": max(1, comp // 2),
                        "completion_tokens": comp,
                        "total_tokens": max(1, comp // 2) + comp,
                        "completion_tokens_details": {"reasoning_tokens": 0},
                        "prompt_tokens_details": {"cached_tokens": 0},
                    }
            outcome = "failed" if pending_finish == "error" else "completed"
            if outcome == "failed":
                wb_usage.record_error(getattr(upstream, "_wb_billed_model", model), 502, "upstream stream finished with error",
                             elapsed_ms=wall, account=account.uid,
                             usage=last_usage, stream=True, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, outcome="failed", key=self._key_id())
            else:
                wb_usage.record_upstream_usage(upstream, model, last_usage, stream=True,
                             elapsed_ms=wall, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, account=account.uid, key=self._key_id(), effort=effort)
            return

    def _chat_nonstream_response(self, upstream, model, fp, account, t_start, effort=None,
                                 tool_alias=None):
        try:
            result = aggregate_stream(upstream, model, None)
        except Exception as exc:
            wb_usage.record_error(model, 502, str(exc), elapsed_ms=int((time.time() - t_start) * 1000),
                         account=account.uid, key=self._key_id())
            return self._error(502, f"upstream stream error: {exc}")
        wall = int((time.time() - t_start) * 1000)
        first_at = result.get("first_chunk_at")
        # 从请求到达计时，流式与非流式才可比。
        first_ms = int((first_at - t_start) * 1000) if first_at else None
        if tool_alias:
            restore_dsh_tool_names(result, tool_alias)
        wb_usage.record_upstream_usage(upstream, model, result.get("usage"), stream=False,
                     elapsed_ms=wall, ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid, key=self._key_id(), effort=effort)
        return self._json(200, result)
