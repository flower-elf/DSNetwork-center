/* 测试台纯逻辑：请求体组装、流式分片累积、用量行文字与错误信息清理。
 * node tests/javascript/_test_playground_logic.js */
import assert from 'node:assert/strict';
import {
  accountHeader, accountOptions, applyChunk, buildChatBody, canSend, chunkError,
  cleanErrorMessage, emptyAnswer, hasCreditRecord, normalizeUsage, parseJson,
  responseError, sessionCredit, usageParts, withAnswer,
} from '../../app/web/js/pages/playground/logic.js';

// 请求体：系统提示词可选，空内容消息不发送
assert.throws(() => buildChatBody({ model: '' }), /模型/);
assert.deepEqual(buildChatBody({ model: 'glm-5.3' }), {
  model: 'glm-5.3', stream: true, stream_options: { include_usage: true }, messages: [],
});
assert.deepEqual(buildChatBody({
  model: 'glm-5.3',
  systemPrompt: '  你是客服  ',
  messages: [
    { role: 'user', content: '你好' },
    { role: 'assistant', content: '你好，请问需要什么帮助' },
    { role: 'assistant', content: '   ' },
    { role: 'user', content: '' },
  ],
}).messages, [
  { role: 'system', content: '你是客服' },
  { role: 'user', content: '你好' },
  { role: 'assistant', content: '你好，请问需要什么帮助' },
]);

// 分片累积
let answer = emptyAnswer();
answer = applyChunk(answer, { choices: [{ delta: { reasoning_content: '先想一下' } }] });
answer = applyChunk(answer, { choices: [{ delta: { content: '你' } }] });
answer = applyChunk(answer, { choices: [{ delta: { content: '好' }, finish_reason: 'stop' }] });
assert.equal(answer.reasoning, '先想一下');
assert.equal(answer.content, '你好');
assert.equal(answer.finishReason, 'stop');
assert.equal(answer.usage, null);

// 用量分片：零值占位不覆盖已有用量
answer = applyChunk(answer, { choices: [], usage: { prompt_tokens: 10, completion_tokens: 20, total_tokens: 30 } });
assert.equal(answer.usage.total, 30);
answer = applyChunk(answer, { choices: [], usage: { total_tokens: 0 } });
assert.equal(answer.usage.total, 30, '空用量分片不覆盖已有用量');

// 流里的错误：HTML 要清理，且不当作内容累积
const failed = applyChunk(emptyAnswer(), { error: { message: '<html><body><h1>401</h1></body></html>' } });
assert.equal(failed.content, '');
assert.equal(failed.error, '401');

// [DONE] 结束
assert.equal(applyChunk(answer, '[DONE]').done, true);
assert.equal(applyChunk(answer, '[DONE]').content, '你好');
assert.equal(applyChunk(answer, 'not json').done, false);

// 用量字段归一化：缓存命中取三种字段里的第一个
assert.deepEqual(normalizeUsage({ prompt_tokens: '12', completion_tokens: 3, total_tokens: 15,
  completion_tokens_details: { reasoning_tokens: 2 },
  prompt_tokens_details: { cached_tokens: 4 }, credit: '0.25' }),
  { prompt: 12, completion: 3, reasoning: 2, cached: 4, total: 15, credit: 0.25, hasCredit: true });
assert.equal(normalizeUsage({ prompt_cache_hit_tokens: 7, prompt_tokens_details: { cached_tokens: 4 } }).cached, 7);
assert.equal(normalizeUsage(null), null);
assert.equal(normalizeUsage({ total_tokens: 5 }).hasCredit, false, '上游没给扣费项时不能当成 0');

// 用量行：有值才写
const parts = usageParts({ prompt: 1234, completion: 56, reasoning: 12, cached: 0, total: 1290,
  credit: 0.0123, hasCredit: true }, 3420);
assert.equal(parts.length, 3);
assert.ok(parts[0].includes('1,290 tokens'), parts[0]);
assert.ok(parts[0].includes('提示 1,234'), parts[0]);
assert.ok(parts[0].includes('输出 56'), parts[0]);
assert.ok(parts[0].includes('其中推理 12'), parts[0]);
assert.ok(parts[0].includes('缓存命中') === false, '缓存命中为 0 时不写');
assert.ok(parts[1].includes('0.0123'), parts[1]);
assert.ok(parts[2].includes('用时 3.42s'), parts[2]);
assert.deepEqual(usageParts({ prompt: 0, completion: 0, reasoning: 0, cached: 0, total: 0, credit: 0, hasCredit: true }, 0),
                 ['消耗 0 积分']);
assert.deepEqual(usageParts({ prompt: 0, completion: 0, reasoning: 0, cached: 0, total: 0, credit: 0, hasCredit: false }, 0),
                 ['上游未返回扣费']);
assert.deepEqual(usageParts(null, 0), []);
assert.deepEqual(usageParts(null, 900), ['用时 900ms']);

// 会话累计积分只统计回答
const conversation = [
  { role: 'user', content: '你好' },
  { role: 'assistant', content: '嗨', usage: { credit: 0.5, hasCredit: true } },
  { role: 'assistant', content: '再答', usage: { credit: 0.25, hasCredit: true } },
  { role: 'assistant', content: '失败', usage: null },
];
assert.equal(sessionCredit(conversation), 0.75);
assert.equal(sessionCredit([]), 0);
assert.equal(hasCreditRecord(conversation), true);
assert.equal(hasCreditRecord([{ role: 'assistant', content: 'x', usage: { credit: 0, hasCredit: false } }]), false);

// 错误信息清理
assert.equal(cleanErrorMessage('<html>\r\n<head><title>401</title></head>\r\n<body>x</body></html>'),
             '401 x');
assert.equal(cleanErrorMessage('很长的错误'.repeat(100)).length, 300);
assert.equal(responseError(401, { error: { message: '<html>401 Authorization Required</html>' } }),
             '401 Authorization Required');
assert.equal(responseError(502, { error: 'upstream unreachable' }), 'upstream unreachable');
assert.equal(responseError(500, 'plain text'), 'plain text');
assert.ok(responseError(503, {}).includes('HTTP 503'), '没有错误正文时要带上状态码');
assert.equal(responseError(0, ''), '请求失败');
assert.equal(chunkError({ error: { message: 'bad' } }), 'bad');
assert.equal(chunkError({ choices: [] }), '');

// 发送按钮：流式输出中必须能点停止
assert.equal(canSend({ model: 'glm-5.3', text: '你好', streaming: false }), true);
assert.equal(canSend({ model: 'glm-5.3', text: '   ', streaming: false }), false);
assert.equal(canSend({ model: '', text: '你好', streaming: false }), false);
assert.equal(canSend({ model: 'glm-5.3', text: '', streaming: true }), true);

// 账号下拉：只列可用账号，标签为「昵称 · 短 uid」
const now = Date.parse('2026-10-03T08:00:00Z');
const ready = (uid, nickname) => ({
  uid, nickname, enabled: true, expiresAt: Math.floor(now / 1000) + 3600,
  inCooldown: false, reserveBlocked: false, dailyLimitBlocked: false,
  lastError: '', modelCooldowns: [],
});
const options = accountOptions([
  ready('b2c3d4e5-1111', '测试二'),
  ready('a1b2c3d4-2222', ''),
  { ...ready('c3d4e5f6-3333', '停用'), enabled: false },
  { ...ready('d4e5f607-4444', '冷却'), inCooldown: true },
  { ...ready('e5f60718-5555', '过期'), expiresAt: Math.floor(now / 1000) - 60 },
], now);
assert.deepEqual(options.map((o) => o.value).sort(), ['a1b2c3d4-2222', 'b2c3d4e5-1111']);
assert.equal(options.find((o) => o.value === 'a1b2c3d4-2222').label, 'a1b2c3d4');
assert.equal(options.find((o) => o.value === 'b2c3d4e5-1111').label, '测试二 · b2c3d4e5');
assert.deepEqual(accountOptions(null, now), []);

// 宽松解析：不是 JSON 时把原文交回
assert.deepEqual(parseJson('{"a":1}'), { a: 1 });
assert.equal(parseJson('plain text'), 'plain text');

// 流式增量只改目标消息，其余消息不动
{
  const messages = [
    { id: 1, role: 'user', content: 'hi' },
    { id: 2, role: 'assistant', content: '' },
  ];
  const next = withAnswer(messages, 1, { content: '你好' }, 12);
  assert.deepEqual(next[0], messages[0]);
  assert.deepEqual(next[1], { id: 2, role: 'assistant', content: '你好', elapsedMs: 12 });
  assert.equal(messages[1].content, '', '不修改传入的数组与消息');
}

// 固定账号才带调试头；空值交给账号池调度
assert.deepEqual(accountHeader(''), {});
assert.deepEqual(accountHeader(null), {});
assert.deepEqual(accountHeader('u-1'), { 'X-Debug-Account': 'u-1' });

console.log('playground logic checks passed');
