/* 账号页纯逻辑：可用性分档、筛选排序分页、积分与有效期口径、校验、出口选项、批量汇总。 */
import assert from 'node:assert/strict';

import * as logic from '../../app/web/js/pages/accounts/logic.js';

const now = new Date(2026, 9, 3, 12, 0, 0).getTime();
const sec = (ms) => Math.floor(ms / 1000);
const base = (over = {}) => Object.assign({
  uid: 'uid-a', nickname: '甲', realm: 'intl', enabled: true, source: 'oauth', file: 'uid-a.json',
  expiresAt: sec(now) + 30 * 86400, credits: null, note: '', priority: 100,
  inCooldown: false, reserveBlocked: false, dailyLimitBlocked: false, lastError: '', modelCooldowns: [],
}, over);

const cool = (over = {}) => base(Object.assign({
  modelCooldowns: [{ model: 'glm-5.3', expiresAt: sec(now) + 600 }],
}, over));

// ---- 可用性分档
assert.equal(logic.availabilityTier(base(), now), 'usable');
assert.equal(logic.availabilityBadge(base(), now).text, '可用');
assert.equal(logic.availabilityBadge(base(), now).tone, 'success');

const disabled = logic.availabilityBadge(base({ enabled: false, expiresAt: sec(now) - 10 }), now);
assert.equal(disabled.tier, 'disabled', '停用先于过期判定');
assert.equal(disabled.group, 'stopped');
assert.equal(disabled.tone, 'muted');

const expired = logic.availabilityBadge(base({ expiresAt: sec(now) - 10 }), now);
assert.equal(expired.tier, 'expired');
assert.equal(expired.group, 'attention');
assert.equal(expired.tone, 'danger');
assert.equal(logic.availabilityTier(base({ expiresIn: 'expired', expiresAt: 0 }), now), 'expired');

const cooling = logic.availabilityBadge(base({ inCooldown: true, cooldownFor: 30 }), now);
assert.equal(cooling.tier, 'cooling');
assert.equal(cooling.group, 'cooling');
assert.equal(cooling.tone, 'warning');

assert.equal(logic.availabilityTier(base({ reserveBlocked: true }), now), 'reserve');
const limited = logic.availabilityBadge(base({ dailyLimitBlocked: true, dailyTokensToday: 5000, dailyTokenLimit: 1000 }), now);
assert.equal(limited.tier, 'dailyLimit');
assert.equal(limited.group, 'attention');
assert.equal(limited.title, '今日已用 5,000 / 1,000 token，暂停接单，本地时间 0 点恢复。');

assert.equal(logic.availabilityTier(base({ lastError: 'HTTP 401' }), now), 'error');
assert.equal(logic.availabilityBadge(base({ lastError: 'HTTP 401' }), now).tone, 'severe');

const modelCooled = logic.availabilityBadge(cool(), now);
assert.equal(modelCooled.tier, 'modelCooled');
assert.equal(modelCooled.group, 'cooling', '只有模型受限的账号归入冷却中');
assert.equal(modelCooled.text, '模型冷却');

// ---- 模型冷却
assert.deepEqual(logic.activeModelCooldowns(base({ modelCooldowns: [{ model: 'x', expiresAt: sec(now) - 5 }] }), now), []);
assert.deepEqual(logic.activeModelCooldowns(base({ modelCooldowns: [{ model: 'x', expiresAt: 'bad' }] }), now), []);
const pills = logic.cooldownPills(base({
  modelCooldowns: [
    { model: 'glm-5.2', expiresAt: sec(now) + 3600 },
    { model: 'glm-5.3', expiresAt: sec(now) + 600 },
    { model: 'stale', expiresAt: sec(now) - 60 },
  ],
}), now);
assert.equal(pills.length, 2);
assert.equal(pills[0].model, 'glm-5.3', '恢复早的排前面');
assert.equal(pills[0].text, 'glm-5.3 · ' + logic.fmtClock((sec(now) + 600) * 1000) + ' 恢复');
const raw = logic.cooldownPills(base({ modelCooldowns: [{ model: '"><img src=x>', expiresAt: sec(now) + 60 }] }), now);
assert.equal(raw[0].model, '"><img src=x>', '模型名原样返回，转义由模板负责');

assert.equal(logic.errorLine(base({ lastError: 'HTTP 429 (model throttled)', modelCooldowns: [{ model: 'glm-5.3', expiresAt: sec(now) + 600 }] }), now), '');
assert.equal(logic.errorLine(base({ lastError: 'HTTP 429 (model throttled)' }), now), 'HTTP 429 (model throttled)');
assert.equal(logic.errorLine(base({ lastError: 'HTTP 401', modelCooldowns: [{ model: 'glm-5.3', expiresAt: sec(now) + 600 }] }), now), 'HTTP 401');

// ---- 分组计数与筛选
const list = [
  base({ uid: 'u1' }),
  base({ uid: 'u2', enabled: false }),
  base({ uid: 'u3', inCooldown: true }),
  base({ uid: 'u4', expiresAt: sec(now) - 10 }),
  cool({ uid: 'u5' }),
];
const counts = logic.groupCounts(list, now);
assert.deepEqual(counts, { all: 5, usable: 1, attention: 1, cooling: 2, stopped: 1 });

const picked = logic.selectAccounts(list, { filter: 'cooling' }, now);
assert.deepEqual(picked.map((a) => a.uid).sort(), ['u3', 'u5']);
assert.deepEqual(logic.selectAccounts(list, { filter: 'usable' }, now).map((a) => a.uid), ['u1']);
assert.deepEqual(logic.selectAccounts(list, { filter: 'all' }, now).map((a) => a.uid), ['u1', 'u3', 'u4', 'u5', 'u2'],
  '默认顺序把停用账号沉到最后');

// ---- 搜索范围
const searchable = base({ nickname: 'Alice', uid: 'AbC-123', file: 'abc-123.json', note: '张叔叔' });
assert.equal(logic.matchQuery(searchable, ''), true);
assert.equal(logic.matchQuery(searchable, 'alice'), true, '搜索不区分大小写');
assert.equal(logic.matchQuery(searchable, 'abc-1'), true);
assert.equal(logic.matchQuery(searchable, '.JSON'), true);
assert.equal(logic.matchQuery(searchable, '张叔'), true);
assert.equal(logic.matchQuery(searchable, '不存在'), false);
assert.deepEqual(logic.selectAccounts([searchable], { query: '  ' }, now).length, 1);

// ---- 排序
const sortable = [
  base({ uid: 'a', credits: { remain: 300 }, expiresAt: sec(now) + 5 * 86400, priority: 500 }),
  base({ uid: 'b', credits: null, expiresAt: sec(now) + 86400, priority: 100 }),
  base({ uid: 'c', credits: { remain: 0 }, expiresAt: null, priority: 100 }),
];
assert.deepEqual(logic.sortAccounts(sortable, 'default').map((a) => a.uid), ['a', 'b', 'c']);
assert.deepEqual(logic.sortAccounts(sortable, 'expiry').map((a) => a.uid), ['b', 'a', 'c'], '没有到期时间的排最后');
assert.deepEqual(logic.sortAccounts(sortable, 'credit').map((a) => a.uid), ['c', 'a', 'b'], '没查到积分的排最后');
assert.deepEqual(logic.sortAccounts(sortable, 'priority').map((a) => a.uid), ['b', 'c', 'a'], '优先级相同时保持原顺序');
assert.equal(logic.priorityOf(base({ priority: 'x' })), 100, '优先级缺失或非法时按默认值 100 参与排序');
assert.equal(logic.priorityOf(base({ priority: 0 })), 0);

// ---- 分页
const many = Array.from({ length: 21 }, (_v, i) => base({ uid: 'u' + i }));
const first = logic.paginate(many, 1);
assert.equal(first.items.length, 20);
assert.equal(first.pages, 2);
assert.equal(first.total, 21);
assert.equal(logic.paginate(many, 99).page, 2);
assert.equal(logic.paginate(many, 0).page, 1);
assert.equal(logic.paginate(many, -3).page, 1);
assert.equal(logic.paginate([], 5).page, 1);
assert.equal(logic.paginate([], 5).pages, 1);

// ---- 空态分类
assert.equal(logic.emptyKind([], [], []), 'none');
assert.equal(logic.emptyKind([base()], [], []), 'realm');
assert.equal(logic.emptyKind([base()], [base()], []), 'filtered');
assert.equal(logic.emptyKind([base()], [base()], [base()]), '');

// ---- 打卡动作按版本区分
assert.deepEqual(logic.realmActions('cn'), { checkin: true, dailyChat: false, dailyChatWeb: false });
assert.deepEqual(logic.realmActions('intl'), { checkin: false, dailyChat: true, dailyChatWeb: true });
assert.equal(logic.accountsForRealm([base({ uid: 'x', realm: 'cn' }), base({ uid: 'y', realm: 'intl' }), base({ uid: 'z', realm: '' })], 'intl').length, 2,
  '缺少版本的旧账号归入正在查看的版本');

// ---- 积分新鲜度与到期角标
assert.equal(logic.creditAgeText(null, now), '');
assert.equal(logic.creditAgeText({ updated_at: sec(now) - 120 }, now), '更新于2 分钟前');
assert.equal(logic.creditAgeText({ updated_at: sec(now) - 5 }, now), '刚刚更新');

assert.equal(logic.expiryCountdown(12 * 3600 * 1000), '12.0 小时后到期');
assert.equal(logic.expiryCountdown(40 * 60 * 1000), '40 分钟后到期');
assert.equal(logic.expiryCountdown(3 * 86400 * 1000), '3 天后到期');
assert.equal(logic.expiryCountdown(0), '已过期');
assert.equal(logic.urgencyTone(3600 * 1000), 'danger');
assert.equal(logic.urgencyTone(3 * 86400 * 1000), 'warning');
assert.equal(logic.urgencyTone(30 * 86400 * 1000), 'muted');

const chip = logic.creditExpiryChip(base({
  creditExpiries: [
    { at: sec(now) + 12 * 3600, amount: 500, name: '月度套餐' },
    { at: sec(now) + 10 * 86400, amount: 1000, name: '补充包' },
    { at: sec(now) + 86400, amount: 0, name: '已用完' },
    { at: 'bad', amount: 99, name: '解析不到' },
  ],
}), now);
assert.equal(chip.text, '500 积分 · 12.0 小时后到期');
assert.equal(chip.tone, 'danger', '一天内到期按危险色');
assert.match(chip.title, /月度套餐 500 积分 12.0 小时后到期/);
assert.match(chip.title, /共有 1,500 积分带到期时间/, '说明里只统计还有额度的套餐');
assert.equal(logic.creditExpiryChip(base(), now), null);
assert.equal(logic.creditExpiryChip(base({ creditExpiries: [{ at: sec(now) + 5 * 86400, amount: 10 }] }), now).tone, 'warning');
assert.equal(logic.creditExpiryChip(base({ creditExpiries: [{ at: sec(now) + 30 * 86400, amount: 10 }] }), now).tone, 'muted');

// ---- 积分进度条
assert.equal(Math.round(logic.creditBarPct(base({ credits: { remain: 3819, size: 4698 } }))), 81);
assert.equal(logic.creditBarPct(base({ credits: { remain: 5000, size: 4000 } })), 100, '可用超过总额时按满格');
assert.equal(logic.creditBarPct(base({ credits: { remain: 0, size: 4000 } })), 0);
assert.equal(logic.creditBarPct(base({ credits: { remain: 100, size: 0 } })), null, '没有总额时不画进度条');
assert.equal(logic.creditBarPct(base({ credits: { remain: 100 } })), null);
assert.equal(logic.creditBarPct(base({ credits: null })), null);
assert.equal(logic.creditBarPct(base()), null);

// ---- 有效期内的积分
const mixed = base({ credits: {
  remain: 1500, size: 6000,
  packages: [
    { name: '过期包', remain: 500, size: 1000, expireAt: sec(now) - 3600 },
    { name: '在期包', remain: 1000, size: 2000, expireAt: sec(now) + 86400 },
    { name: '用完没过期', remain: 0, size: 3000, expireAt: sec(now) + 86400 },
  ],
} });
assert.deepEqual(logic.validCredits(mixed, now), { remain: 1000, size: 5000 }, '过期套餐整条剔除，用完没过期的仍占总额');
assert.equal(Math.round(logic.creditBarPct(mixed, now)), 20);
assert.deepEqual(logic.validCredits(base({ credits: { remain: 700, size: 900 } }), now), { remain: 700, size: 900 }, '没有套餐明细时回退到汇总值');
assert.equal(logic.validCredits(base({ credits: null }), now), null);

// ---- 令牌有效期
const token = logic.tokenExpiryView(base(), now);
assert.equal(token.text, '30.0 天');
assert.equal(token.pct, null, '凭证文件与令牌都没有签发时间时不画进度条');
assert.equal(token.tone, 'success');
const issued = logic.tokenExpiryView(base({ expiresAt: sec(now) + 45 * 86400, issuedAt: sec(now) - 10 * 86400 }), now);
assert.equal(issued.text, '45.0 天');
assert.equal(Math.round(issued.pct), 82, '这次签发到到期共 55 天，剩下 45 天占 82%');
assert.equal(logic.tokenExpiryView(base({ expiresAt: sec(now) + 55 * 86400, issuedAt: sec(now) }), now).pct, 100, '刚签发的 55 天令牌取满格');
assert.equal(logic.tokenExpiryView(base({ expiresAt: sec(now) + 30 * 86400, issuedAt: sec(now) + 60 * 86400 }), now).pct, null, '签发时间晚于到期时间时不画进度条');
assert.deepEqual(logic.tokenExpiryView(base({ expiresAt: sec(now) - 1 }), now), { remain: -1, text: '已过期', pct: 0, tone: 'danger' });
assert.deepEqual(logic.tokenExpiryView(base({ apiKey: true, expiresAt: 0 }), now), { remain: null, text: '长期有效', pct: null, tone: 'success' });
assert.equal(logic.tokenExpiryView(base({ expiresAt: sec(now) + 3600 }), now).tone, 'danger');
assert.equal(logic.tokenExpiryView(base({ expiresAt: sec(now) + 3 * 86400 }), now).tone, 'warning');
const unknown = logic.tokenExpiryView(base({ expiresAt: 0, expiresIn: '未知' }), now);
assert.equal(unknown.text, '未知');
assert.equal(unknown.tone, 'muted');

// ---- 调度优先级校验
assert.deepEqual(logic.parsePriorityInput(0), { ok: true, value: 0 });
assert.deepEqual(logic.parsePriorityInput(' 9999 '), { ok: true, value: 9999 });
assert.equal(logic.parsePriorityInput(10000).ok, false);
assert.equal(logic.parsePriorityInput(10000).message, '优先级需要 0 到 9999 之间的整数，本次没有发送请求');
assert.equal(logic.parsePriorityInput('-1').ok, false);
assert.equal(logic.parsePriorityInput('').ok, false);
assert.equal(logic.parsePriorityInput('abc').ok, false);
assert.equal(logic.parsePriorityInput('3.5').ok, false);
assert.deepEqual(logic.parsePriorityInput(500, { priorityMax: 500 }), { ok: true, value: 500 });
assert.equal(logic.parsePriorityInput(501, { priorityMax: 500 }).ok, false);

// ---- 单账号单模型的并发上限：0 或空表示不限
assert.deepEqual(logic.parseConcurrencyInput(''), { ok: true, value: 0 });
assert.deepEqual(logic.parseConcurrencyInput(' 0 '), { ok: true, value: 0 });
assert.deepEqual(logic.parseConcurrencyInput('4'), { ok: true, value: 4 });
assert.deepEqual(logic.parseConcurrencyInput(1000), { ok: true, value: 1000 });
assert.equal(logic.parseConcurrencyInput('1001').ok, false);
assert.equal(logic.parseConcurrencyInput('-1').ok, false);
assert.equal(logic.parseConcurrencyInput('2.5').ok, false);
assert.equal(logic.parseConcurrencyInput('abc').ok, false);
assert.deepEqual(logic.parseConcurrencyInput(40, { concurrencyMax: 40 }), { ok: true, value: 40 });
assert.equal(logic.parseConcurrencyInput(41, { concurrencyMax: 40 }).ok, false);
assert.equal(logic.accountLimits({ priorityMax: 5, concurrencyMax: 6, noteMax: 7 }).noteMax, 7);
assert.ok(logic.parseConcurrencyInput('1001').message.includes('0 表示不限'),
  '拒绝时要说清填写 0 表示不限，否则操作员不知道怎么关掉');

// 旧账号缺少该字段时按不限处理
assert.equal(logic.concurrencyOf(base()), 0, '没设过的账号按不限处理');
assert.equal(logic.concurrencyOf(base({ concurrencyLimit: 3 })), 3);
assert.equal(logic.concurrencyOf(base({ concurrencyLimit: 0 })), 0);
assert.equal(logic.concurrencyOf(base({ concurrencyLimit: -2 })), 0, '手工改坏的负数按不限处理');
assert.equal(logic.concurrencyOf(base({ concurrencyLimit: 'x' })), 0);
assert.equal(logic.activeRequestsOf(base()), 0);
assert.equal(logic.activeRequestsOf(base({ activeRequests: 2 })), 2);

// ---- 心跳重绘时的草稿
const draft = { uid: 'uid-a', field: 'priority', value: '7', start: 1, end: 1 };
assert.equal(logic.draftFor(draft, 'uid-a', 'priority'), '7');
assert.equal(logic.draftFor(draft, 'uid-b', 'priority'), null);
assert.equal(logic.draftFor(draft, 'uid-a', 'query'), null);
assert.equal(logic.draftFor(null, 'uid-a', 'priority'), null);
assert.equal(logic.uidAttr('a b"c<d'), 'abcd', 'data 属性只保留安全字符');
assert.equal(logic.uidAttr('8e1f-2a'), '8e1f-2a');

// ---- 出口下拉：旧代理与未知槽位如实展示，不静默回退直连
const legacy = logic.slotOptions(base({ proxy: 'http://127.0.0.1:7890' }), [{ id: 's1', name: '香港', enabled: true }]);
assert.equal(legacy.selected, '__legacy__');
assert.equal(legacy.options[0].value, '__legacy__');
assert.match(legacy.options[0].label, /旧代理/);
assert.equal(legacy.options[1].value, '', '第二项是清除旧代理并直连');
assert.equal(legacy.options[2].label, '香港');

const slots = logic.slotOptions(base({ proxySlot: 's2' }), [
  { id: 's1', name: '香港', enabled: true },
  { id: 's2', name: '日本', enabled: false },
]);
assert.equal(slots.selected, 's2');
assert.equal(slots.options[0].label, '直连');
assert.equal(slots.options[2].label, '日本 · 已停用');
const unknownSlot = logic.slotOptions(base({ proxySlot: 'gone' }), [{ id: 's1', name: '香港' }]);
assert.equal(unknownSlot.selected, 'gone');
assert.equal(unknownSlot.options.at(-1).label, '未知槽位 gone');
assert.equal(logic.slotOptions(base({ proxy: 'http://x', proxySlot: 's1' }), []).selected, 's1', '已经绑定槽位时不再看旧代理');

// ---- 身份下拉
assert.equal(logic.productOptions(base()).selected, 'workbuddy');
assert.equal(logic.productOptions(base({ product: 'vscode' })).selected, 'vscode');
assert.equal(logic.productOptions(base({ product: 'bad' })).selected, 'workbuddy');
assert.equal(logic.productOptions(base()).options.length, 3);
assert.equal(logic.productLabel('cli'), '官方 CodeBuddy CLI');

// ---- 用量与批量结果
assert.deepEqual(logic.usageFor({}, 'u1'), { requests: 0, total_tokens: 0 });
assert.deepEqual(logic.usageFor({ u1: { requests: 7, total_tokens: 900 } }, 'u1'), { requests: 7, total_tokens: 900 });
assert.equal(logic.summarizeResults([
  { uid: 'u1', nickname: '甲', ok: true },
  { uid: 'u2', nickname: '乙', ok: false, error: 'HTTP 401' },
], { okText: '签到成功' }), '甲: 签到成功；乙: HTTP 401');
assert.equal(logic.summarizeResults([{ uid: 'abcdefgh', ok: true }], { okText: '成功' }), 'abcdefgh: 成功');
assert.equal(logic.summarizeResults([{ uid: 'u1', nickname: '甲', ok: true, conversation: 'c-1' }],
  { okText: '已建会话', withDetail: 'conversation' }), '甲: 已建会话 c-1');
assert.equal(logic.anyOk([{ ok: false }, { ok: true }]), true);
assert.equal(logic.anyOk([]), false);

// ---- 积分补查
const tried = new Set(['u2']);
assert.deepEqual(logic.creditsToFill([
  base({ uid: 'u1' }),
  base({ uid: 'u2' }),
  base({ uid: 'u3', credits: { remain: 1 } }),
  base({ uid: '' }),
], tried), ['u1']);
assert.deepEqual(logic.mergeCredits(
  [base({ uid: 'u1', credits: null }), base({ uid: 'u9', credits: null })],
  [{ uid: 'u1', credits: { remain: 42 } }],
)[0].credits, { remain: 42 });
assert.equal(logic.mergeCredits([base({ uid: 'u9', credits: null })], [{ uid: 'u1', credits: { remain: 1 } }])[0].credits, null);

// ---- 出口分配计划：轮流分配已启用槽位
const assign = logic.autoAssignPlan([
  base({ uid: 'a' }),
  base({ uid: 'b', proxySlot: 's1' }),
  base({ uid: 'c', enabled: false }),
  base({ uid: 'd' }),
  base({ uid: 'e' }),
], [{ id: 's1', enabled: true }, { id: 's2', enabled: false }, { id: 's3', enabled: true }]);
assert.deepEqual(assign.rows, [
  { uid: 'a', nickname: '甲', slotId: 's1' },
  { uid: 'd', nickname: '甲', slotId: 's3' },
  { uid: 'e', nickname: '甲', slotId: 's1' },
]);
assert.equal(assign.instead, '');
const noneToAssign = logic.autoAssignPlan([base({ proxySlot: 's1' })], [{ id: 's1' }]);
assert.deepEqual(noneToAssign.rows, []);
assert.match(noneToAssign.instead, /都已绑定/);
const noSlots = logic.autoAssignPlan([base()], []);
assert.deepEqual(noSlots.rows, []);
assert.match(noSlots.instead, /没有已启用的代理槽/);

// ---- 模型排除：初始勾选与保存时的排除名单
const sel = logic.modelSelection(['hy3', 'GLM-5.3', 'kimi-k3'], ['glm-5.3']);
assert.deepEqual([...sel.picked], ['hy3', 'kimi-k3'], '排除名单按大小写不敏感匹配');
assert.deepEqual(logic.deniedModels(sel.ids, sel.picked), ['GLM-5.3']);
const allOn = logic.modelSelection(['hy3', 'glm-5.3'], []);
assert.deepEqual(logic.deniedModels(allOn.ids, allOn.picked), []);
assert.deepEqual(logic.deniedModels(['hy3', 'glm-5.3'], new Set()), ['hy3', 'glm-5.3']);
assert.equal(logic.disabledModelsTitle({ disabledModels: ['hy3', 'glm-5.3'] }),
  '不派给这个账号的模型：hy3、glm-5.3');
assert.equal(logic.disabledModelsTitle({ disabledModels: [] }), '');

console.log('账号页纯逻辑断言通过');
