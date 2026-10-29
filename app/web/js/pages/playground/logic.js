// 测试台（playground）的纯逻辑：请求体组装、流式分片累积、用量行的文字；不碰网络与 DOM，只依赖 core/format.js 与账号页的可用性判定，可以在 Node 里直接测试。
import { compareText, fmtCredit, fmtLatency, fmtNumber } from '../../core/format.js';
import { availabilityTier } from '../accounts/logic.js';

/** 宽松解析 JSON：不是 JSON 时把原文原样交回。 */
export function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** 把流式回答的增量写回消息列表的指定一项，其余消息不动。 */
export function withAnswer(messages, index, answer, elapsedMs) {
  return messages.map((m, i) => (i === index ? { ...m, ...answer, elapsedMs } : m));
}

/** 组装 /v1/chat/completions 的请求体；系统提示词留空时不发送 system 消息，由网关补默认值。 */
export function buildChatBody({ model, systemPrompt = '', messages = [] }) {
  if (!model) throw new Error('请先选择要调试的模型');
  const body = {
    model,
    stream: true,
    stream_options: { include_usage: true },
    messages: [],
  };
  const system = String(systemPrompt || '').trim();
  if (system) body.messages.push({ role: 'system', content: system });
  for (const item of messages) {
    if (item.role !== 'user' && item.role !== 'assistant') continue;
    const content = String(item.content || '').trim();
    if (!content) continue;
    body.messages.push({ role: item.role, content });
  }
  return body;
}

export function emptyAnswer() {
  return { content: '', reasoning: '', usage: null, finishReason: '', error: '', done: false };
}

/** 上游把整页 HTML 也塞进错误信息里，展示前去掉标签并压成一行。 */
export function cleanErrorMessage(text) {
  const flat = String(text === null || text === undefined ? '' : text)
    .replace(/<[^>]*>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return flat.length > 300 ? flat.slice(0, 297) + '...' : flat;
}

/** 非 2xx 响应体的错误说明。 */
export function responseError(status, parsed) {
  let message = '';
  if (parsed && typeof parsed === 'object') {
    if (parsed.error && typeof parsed.error === 'object' && parsed.error.message) message = parsed.error.message;
    else if (typeof parsed.error === 'string') message = parsed.error;
    else if (parsed.msg) message = parsed.msg;
  } else if (typeof parsed === 'string') {
    message = parsed;
  }
  const cleaned = cleanErrorMessage(message);
  if (cleaned) return cleaned;
  return status ? `请求失败，HTTP ${status}` : '请求失败';
}

export function chunkError(chunk) {
  if (!chunk || typeof chunk !== 'object' || !chunk.error) return '';
  const raw = typeof chunk.error === 'string' ? chunk.error : chunk.error.message;
  return cleanErrorMessage(raw);
}

export function normalizeUsage(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const details = raw.completion_tokens_details || {};
  const promptDetails = raw.prompt_tokens_details || {};
  const cached = raw.prompt_cache_hit_tokens || details.cached_tokens || promptDetails.cached_tokens;
  return {
    prompt: Number(raw.prompt_tokens) || 0,
    completion: Number(raw.completion_tokens) || 0,
    reasoning: Number(details.reasoning_tokens) || 0,
    cached: Number(cached) || 0,
    total: Number(raw.total_tokens) || 0,
    credit: Number(raw.credit) || 0,
    hasCredit: raw.credit !== undefined && raw.credit !== null,
  };
}

function usageIsEmpty(usage) {
  return !usage.total && !usage.prompt && !usage.completion && !usage.credit;
}

/** 累积一个流式分片，返回新的回答状态，不修改传入的对象。 */
export function applyChunk(answer, chunk) {
  if (chunk === null || chunk === undefined) return answer;
  if (typeof chunk === 'string') {
    return chunk.trim() === '[DONE]' ? { ...answer, done: true } : answer;
  }
  const error = chunkError(chunk);
  if (error) return { ...answer, error };
  let next = answer;
  const usage = normalizeUsage(chunk.usage);
  if (usage && (!usageIsEmpty(usage) || !answer.usage)) next = { ...next, usage };
  let content = next.content;
  let reasoning = next.reasoning;
  let finishReason = next.finishReason;
  for (const choice of chunk.choices || []) {
    const delta = choice.delta || {};
    if (delta.content) content += delta.content;
    if (delta.reasoning_content) reasoning += delta.reasoning_content;
    if (choice.finish_reason) finishReason = choice.finish_reason;
  }
  return { ...next, content, reasoning, finishReason };
}

/** 每条回答下方的用量说明；只用真实字段，缺什么就不写什么。 */
export function usageParts(usage, elapsedMs) {
  const parts = [];
  if (usage) {
    if (usage.total) {
      const tokens = [];
      if (usage.prompt) tokens.push(`提示 ${fmtNumber(usage.prompt)}`);
      if (usage.completion) tokens.push(`输出 ${fmtNumber(usage.completion)}`);
      tokens.push(`合计 ${fmtNumber(usage.total)} tokens`);
      if (usage.reasoning) tokens.push(`其中推理 ${fmtNumber(usage.reasoning)}`);
      if (usage.cached) tokens.push(`缓存命中 ${fmtNumber(usage.cached)}`);
      parts.push(tokens.join(' · '));
    }
    if (usage.hasCredit) parts.push(`消耗 ${fmtCredit(usage.credit)} 积分`);
    else parts.push('上游未返回扣费');
  }
  if (elapsedMs > 0) parts.push(`用时 ${fmtLatency(elapsedMs)}`);
  return parts;
}

/** 会话里累计消耗的积分，用于输入框旁的总量徽章。 */
export function sessionCredit(messages) {
  return (messages || []).reduce((sum, m) => sum + ((m.usage && m.usage.credit) || 0), 0);
}

/** 会话里是否已经出现过扣费记录，用来决定要不要显示总量徽章。 */
export function hasCreditRecord(messages) {
  return (messages || []).some((m) => m.usage && m.usage.hasCredit);
}

/**
 * 账号下拉的选项：空值表示全部账号，其余只列当前可用档位的账号；
 * 选中的账号这次调试只服务这一个请求。
 */
export function accountOptions(accounts, now = Date.now()) {
  return (Array.isArray(accounts) ? accounts : [])
    .filter((account) => account && account.uid && availabilityTier(account, now) === 'usable')
    .map((account) => {
      const short = String(account.uid).slice(0, 8);
      const name = String(account.nickname || '').trim() || short;
      return { value: String(account.uid), label: name === short ? short : name + ' · ' + short };
    })
    .sort((a, b) => compareText(a.label, b.label) || compareText(a.value, b.value));
}

/** 选中具体账号时带上 X-Debug-Account 头；空值表示交给账号池调度。 */
export function accountHeader(uid) {
  return uid ? { 'X-Debug-Account': String(uid) } : {};
}

/** 发送按钮是否可用：正在流式输出时必须能点停止。 */
export function canSend({ model, text, streaming }) {
  return !!model && (streaming || !!String(text || '').trim());
}
