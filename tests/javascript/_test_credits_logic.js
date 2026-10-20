/* 积分页纯逻辑：套餐明细规整、按来源归并、到期与汇总口径。
 * node tests/javascript/_test_credits_logic.js */
import assert from 'node:assert/strict';

import * as logic from '../../app/web/js/pages/credits/logic.js';

const now = new Date(2026, 9, 3, 12, 0, 0).getTime();
const sec = (ms) => Math.floor(ms / 1000);
const DAY = 86400000;

const pack = (over = {}) => Object.assign({
  name: 'CodeBuddy个人版国内运营裂变包', subProduct: '腾讯云代码助手 (IDE) - 赠送包',
  size: 100, remain: 100, used: 0,
  startAt: sec(now - 7 * DAY), expireAt: sec(now + 3 * DAY),
}, over);

const account = (over = {}) => Object.assign({
  uid: 'uid-a', nickname: '甲', realm: 'cn',
  credits: { remain: 100, used: 0, size: 100, packages: [pack()], updated_at: sec(now - 3600) },
}, over);

// ---- 套餐明细规整：脏条目跳过，秒级时刻换成毫秒
const dirty = account({ credits: { remain: 3, packages: [
  'not-a-dict',
  null,
  { name: '', remain: 0, used: 0, size: 0 },
  { name: '   ', size: 5, remain: 5, used: 0, subProduct: ' 说明 ' },
  { name: '无周期', size: 7, remain: 7, used: 0 },
] } });
const kept = logic.packagesOf(dirty);
assert.equal(kept.length, 2, '全零条目与非对象条目都要丢掉: ' + JSON.stringify(kept));
assert.equal(kept[0].name, '套餐', '名称只有空白时退回「套餐」');
assert.equal(kept[0].subProduct, '说明', '说明要去掉两端空白');
assert.equal(kept[1].startAt, 0, '上游没给周期时刻时按 0 处理');
assert.deepEqual(logic.packagesOf({ credits: { packages: 'bad' } }), [], 'packages 不是列表时返回空');
assert.deepEqual(logic.packagesOf(null), []);

const scaled = logic.packagesOf(account());
assert.equal(scaled[0].expireAt, (sec(now + 3 * DAY)) * 1000, '凭证文件里的秒级时刻要换成毫秒');
// 规整过的条目再进归并时，时刻保持毫秒不被重复换算
const twice = logic.groupPackages(scaled, now)[0];
assert.equal(twice.nextExpireAt, (sec(now + 3 * DAY)) * 1000, '已经换成毫秒的时刻要保持原值');
assert.equal(logic.packageState(scaled[0], now), 'active', '规整过的条目判状态同样成立');
assert.equal(logic.cycleText(scaled[0]), '9-26 至 10-6', '周期文案对规整过的条目同样成立');

// ---- 单笔状态：用完、过期、可用
assert.equal(logic.packageState(pack(), now), 'active');
assert.equal(logic.packageState(pack({ remain: 0, used: 100 }), now), 'used');
assert.equal(logic.packageState(pack({ expireAt: sec(now - 1000) }), now), 'expired');
assert.equal(logic.packageState(pack({ remain: 0, used: 100, expireAt: sec(now - 1000) }), now), 'used',
  '用完且过期时按「已用完」显示');
assert.deepEqual(logic.stateBadge('active'), { text: '可用', tone: 'success' });
assert.deepEqual(logic.stateBadge('expired'), { text: '已过期', tone: 'danger' });
assert.deepEqual(logic.stateBadge('used'), { text: '已用完', tone: 'muted' });

// ---- 按来源归并：同一来源多笔合成一行并累加
const many = logic.groupPackages([
  pack({ remain: 100, used: 0 }),
  pack({ remain: 40, used: 60, expireAt: sec(now + 1 * DAY) }),
  pack({ remain: 0, used: 100, expireAt: sec(now - 1 * DAY) }),
], now);
assert.equal(many.length, 1, '同名同说明的三笔合成一组');
assert.equal(many[0].count, 3);
assert.equal(many[0].size, 300);
assert.equal(many[0].remain, 140);
assert.equal(many[0].used, 160);
assert.equal(many[0].nextExpireAt, (sec(now + 1 * DAY)) * 1000, '最近到期只看还有额度且还没到期的那些');
assert.equal(many[0].state, 'active');
assert.deepEqual(many[0].packages.map((p) => p.remain), [0, 40, 100], '组内按到期升序，先到期的在前');

const allExpired = logic.groupPackages([pack({ expireAt: sec(now - 1 * DAY) })], now);
assert.equal(allExpired[0].state, 'expired');
assert.equal(allExpired[0].nextExpireAt, 0);

const lapse = logic.groupPackages([
  pack({ name: '过期批', subProduct: '', remain: 30, expireAt: sec(now - 5 * DAY) }),
  pack({ name: '过期批', subProduct: '', remain: 10, expireAt: sec(now - 2 * DAY) }),
], now);
assert.equal(lapse[0].state, 'expired');
assert.equal(lapse[0].lastExpireAt, (sec(now - 2 * DAY)) * 1000, '过期的组记下最后一个到期的时刻');

const noExpiry = logic.groupPackages([pack({ expireAt: 0 })], now);
assert.equal(noExpiry[0].state, 'active', '上游没给到期时间时不能当成过期');

const empty = logic.groupPackages([pack({ remain: 0, used: 100 })], now);
assert.equal(empty[0].state, 'used');
assert.deepEqual(logic.groupPackages([], now), []);

// ---- 排序：可用在前，同档内到期早的在前
const sorted = logic.groupPackages([
  pack({ name: '已用完的', remain: 0, used: 100, subProduct: '' }),
  pack({ name: '晚到期', subProduct: '', expireAt: sec(now + 9 * DAY) }),
  pack({ name: '过期但有余', subProduct: '', expireAt: sec(now - 2 * DAY) }),
  pack({ name: '早到期', subProduct: '', expireAt: sec(now + 1 * DAY) }),
], now);
assert.deepEqual(sorted.map((g) => g.name), ['早到期', '晚到期', '过期但有余', '已用完的']);

// ---- 展开键：同名来源在不同账号下必须分开
const sharedGroup = logic.groupPackages([pack(), pack({ remain: 40, used: 60 })], now)[0];
assert.notEqual(logic.creditRowKey(account({ uid: 'uid-a' }), sharedGroup),
  logic.creditRowKey(account({ uid: 'uid-b' }), sharedGroup), '同一来源在不同账号下的展开键要不同');
assert.equal(logic.creditRowKey(account({ uid: 'uid-a' }), sharedGroup),
  logic.creditRowKey(account({ uid: 'uid-a' }), sharedGroup), '同一账号同一来源的展开键要保持稳定');
assert.notEqual(logic.creditRowKey(account(), sorted[0]), logic.creditRowKey(account(), sorted[1]),
  '同一账号下不同来源的展开键要不同');

// ---- 账号汇总：余额取合计字段，最近到期取全部条目
const summary = logic.accountCredits(account({ credits: {
  remain: 140, used: 160, size: 300, updated_at: sec(now - 7200),
  packages: [pack({ remain: 100, expireAt: sec(now + 5 * DAY) }), pack({ remain: 40, expireAt: sec(now + 1 * DAY) })],
} }), now);
assert.equal(summary.queried, true);
assert.equal(summary.remain, 140);
assert.equal(summary.used, 160);
assert.equal(summary.size, 300);
assert.equal(summary.groups.length, 1, '两笔同来源合成一组');
assert.equal(summary.nextExpireAt, (sec(now + 1 * DAY)) * 1000);
assert.equal(summary.nextExpireAmount, 40);
assert.ok(summary.age.includes('更新'), '要给出上次读取的时间: ' + summary.age);

const unqueried = logic.accountCredits(account({ credits: null }), now);
assert.equal(unqueried.queried, false);
assert.equal(unqueried.remain, 0);
assert.deepEqual(unqueried.groups, []);

// ---- 页头汇总：只算查到余额的账号，未查询账号不计入汇总
const totals = logic.pageTotals([
  account(),
  account({ uid: 'uid-b', credits: { remain: 20, used: 5, size: 25, packages: [pack({ name: '体验版', subProduct: '' })] } }),
  account({ uid: 'uid-c', credits: null }),
]);
assert.deepEqual(totals, { remain: 120, used: 5, queried: 2 });

// ---- 最近到期：跳过余额为零、已过期、企业额度与无到期安排的条目
const soonest = logic.nextExpiry([
  account({ uid: 'uid-a', nickname: '甲', credits: { remain: 100, packages: [pack({ remain: 100, expireAt: sec(now + 4 * DAY) })] } }),
  account({ uid: 'uid-b', nickname: '乙', credits: { remain: 50, packages: [
    pack({ remain: 50, expireAt: sec(now + 2 * DAY) }),
    pack({ remain: 0, used: 50, expireAt: sec(now + 1 * DAY) }),
    pack({ remain: 30, expireAt: sec(now - 3 * DAY) }),
    pack({ remain: 9, expireAt: sec(now + 1 * DAY), package_code: 'enterprise' }),
  ] } }),
], now);
assert.equal(soonest.account, '乙');
assert.equal(soonest.amount, 50);
assert.equal(soonest.at, (sec(now + 2 * DAY)) * 1000);
assert.equal(logic.nextExpiry([account({ credits: { remain: 0, packages: [] } })], now), null);

// ---- 到期角标：一天内危险，七天内警告，其余常规
const urgent = logic.expiryChip(now + 3 * 3600000, 30, now);
assert.equal(urgent.tone, 'danger');
assert.ok(urgent.text.includes('到期'));
assert.ok(urgent.title.includes('30 积分'));
assert.equal(logic.expiryChip(now + 3 * DAY, 30, now).tone, 'warning');
assert.equal(logic.expiryChip(now + 30 * DAY, 30, now).tone, 'muted');
assert.equal(logic.expiryChip(0, 30, now), null);

// ---- 周期文案与归并行说明
assert.equal(logic.cycleText(pack()), '9-26 至 10-6');
assert.equal(logic.cycleText(pack({ startAt: 0 })), '10-6 到期');
assert.equal(logic.cycleText(pack({ expireAt: 0 })), '9-26 开始');
assert.equal(logic.cycleText(pack({ startAt: 0, expireAt: 0 })), '');
assert.equal(logic.groupSubText({ count: 1, subProduct: '赠送包' }), '赠送包');
assert.equal(logic.groupSubText({ count: 3, subProduct: '赠送包' }), '共 3 笔 · 赠送包');
assert.equal(logic.groupSubText({ count: 1, subProduct: '' }), '');

const enterpriseGroup = logic.groupPackages([
  pack({ remain: 9, expireAt: sec(now + DAY), package_code: 'enterprise' }),
], now)[0];
assert.equal(enterpriseGroup.state, 'active', '企业额度不应显示为已过期');
const freeGroup = logic.groupPackages([
  pack({ remain: 9, expireAt: sec(now + DAY), no_expiry: true }),
], now)[0];
assert.equal(freeGroup.state, 'active', '无到期安排的额度不应显示为已过期');

// ---- 待补查账号：跳过已查过的与正在补查的
const tried = new Set(['uid-b']);
const pending = logic.unqueriedAccounts([
  account({ uid: 'uid-b', credits: null }),
  account({ uid: 'uid-c', credits: null }),
  account({ uid: 'uid-d' }),
], tried);
assert.deepEqual(pending.map((a) => a.uid), ['uid-c']);

// ---- 打开积分页的重新读取：未到间隔不读，间隔为 0 只读首次
assert.equal(logic.dueForRefresh(0, now, 60), true, '本会话还没读过账号列表');
assert.equal(logic.dueForRefresh(now - 59000, now, 60), false, '未到间隔用缓存');
assert.equal(logic.dueForRefresh(now - 60000, now, 60), true, '刚好到间隔就重新读取');
assert.equal(logic.dueForRefresh(now - 600000, now, 60), true);
assert.equal(logic.dueForRefresh(now - 600000, now, 0), false, '禁用自动刷新后只用缓存');
assert.equal(logic.dueForRefresh(0, now, 0), true, '第一次打开总要读取');

// ---- 15 天内到期的积分：只算尚未过期、带到期时间且还有剩余的套餐
const expiringAccounts = [
  account({ uid: 'exp-a', credits: { remain: 160, packages: [
    pack({ remain: 40, expireAt: sec(now + 10 * DAY) }),
    pack({ remain: 60, expireAt: sec(now + 15 * DAY) }),
    pack({ remain: 20, expireAt: sec(now + 20 * DAY) }),
    pack({ remain: 30, expireAt: sec(now - DAY) }),
    pack({ remain: 10, expireAt: 0 }),
    pack({ remain: 7, expireAt: sec(now + 5 * DAY), no_expiry: true }),
    pack({ remain: 9, expireAt: sec(now + 5 * DAY), package_code: 'enterprise' }),
  ] } }),
  account({ uid: 'exp-b', credits: { remain: 25, packages: [pack({ remain: 25, expireAt: sec(now + 1 * DAY) })] } }),
  account({ uid: 'exp-c', credits: null }),
];
assert.equal(logic.expiringTotal(expiringAccounts, 15, now), 125,
  '只累加 0 天到 15 天之间到期的剩余额度');
assert.equal(logic.expiringTotal(expiringAccounts, 0, now), 0, '窗口为 0 时没有临期积分');
assert.equal(logic.expiringTotal([], 15, now), 0);

console.log('credits logic assertions passed');
