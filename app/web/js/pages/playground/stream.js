import { createParser } from '../../../vendor/eventsource-parser-4.1.1/index.js';
import { applyChunk, emptyAnswer, parseJson, responseError } from './logic.js';

// 流式对话的传输层：POST 请求体，用 eventsource-parser 解析响应体里的 SSE；请求头由调用方给出（面板会话令牌与 X-Realm），fetch 可替换以便在 Node 里测试。

export class StreamError extends Error {
  constructor(status, message) {
    super(message);
    this.name = 'StreamError';
    this.status = status;
  }
}

/**
 * 打开一次流式对话，返回最终状态；onUpdate 拿到累积后的回答状态。
 * 用户中止时返回的状态带 stopped: true，已经收到的内容保留。
 */
export async function streamChat({ url, headers, body, signal, onUpdate, fetchImpl }) {
  const send = fetchImpl || fetch;
  let response;
  try {
    response = await send(url, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
      cache: 'no-store',
    });
  } catch (err) {
    // 上游还没返回任何内容时用户就点了停止：按「已终止」返回，不算请求失败
    if (err && err.name === 'AbortError') return { ...emptyAnswer(), stopped: true };
    throw err;
  }
  if (!response.ok) {
    const text = await response.text();
    throw new StreamError(response.status, responseError(response.status, parseJson(text)));
  }
  if (!response.body) throw new StreamError(0, '浏览器没有把流式响应体交回来');

  let answer = emptyAnswer();
  const parser = createParser({
    onEvent(event) {
      const data = event.data;
      if (data === undefined || data === '') return;
      let payload;
      if (data.trim() === '[DONE]') {
        payload = '[DONE]';
      } else {
        // SSE 允许非 JSON 的数据行，网关也会把上游的原文转发过来，这类分片直接跳过
        try {
          payload = JSON.parse(data);
        } catch {
          return;
        }
      }
      answer = applyChunk(answer, payload);
      if (onUpdate) onUpdate(answer);
    },
  });

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    parser.feed(decoder.decode());
  } catch (err) {
    if (err && err.name === 'AbortError') return { ...answer, stopped: true };
    throw err;
  }
  return answer;
}
