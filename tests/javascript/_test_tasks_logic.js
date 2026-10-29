/* 任务页纯逻辑：调度器文案、成长任务状态、猫猫旅行状态、积分变动记录。 */
import assert from 'node:assert/strict';

import * as logic from '../../app/web/js/pages/tasks/logic.js';

// ---- 调度器：两个版本的模式文案与按钮文字不同
const running = {
  enabled: true,
  mode: '通用模式',
  mode_cn: '每日 09:00、21:00 签到与猫猫旅行，22:00 保活，01:00 夜猫子任务',
  mode_intl: '每日 22:00 集中巡检，自动保活账号 Token，凭证长期有效',
  last_run_time: '2026-10-03 01:00:00',
  next_run_time: '2026-10-03 09:00:00',
  logs: ['a', 'b', 'c'],
};
const intlView = logic.schedulerView(running, 'intl');
assert.equal(intlView.stateText, '运行中 · ' + running.mode_intl);
assert.equal(intlView.triggerText, '立即保活刷新');
assert.equal(intlView.toggleText, '暂停调度');
assert.equal(intlView.timesText, '上次巡检 2026-10-03 01:00:00 · 下次巡检 2026-10-03 09:00:00');
const cnView = logic.schedulerView(running, 'cn');
assert.equal(cnView.stateText, '运行中 · ' + running.mode_cn);
assert.equal(cnView.triggerText, '立即巡检保活');

const paused = logic.schedulerView({ enabled: false }, 'cn');
assert.equal(paused.stateText, '已暂停');
assert.equal(paused.toggleText, '恢复调度');
assert.equal(paused.triggerText, '立即巡检保活');
assert.equal(paused.timesText, '上次巡检 尚未运行 · 下次巡检 待调度');
assert.deepEqual(logic.schedulerView({}, 'intl').logs, []);
assert.deepEqual(logic.schedulerView({ logs: Array.from({ length: 30 }, (_v, i) => i) }, 'intl').logs.length, 30);
assert.deepEqual(logic.schedulerLogLines({ logs: Array.from({ length: 30 }, (_v, i) => i) }, 20), Array.from({ length: 20 }, (_v, i) => i + 10));
assert.deepEqual(logic.schedulerLogLines(null).length, 0);

// ---- 成长任务：状态徽章、进度与奖励
const claimed = logic.growthTaskView({ name: '每日签到', task_code: 't1', status: 'claimed', current: 1, target: 1, reward_credit: 100 });
assert.equal(claimed.badge.text, '已领取');
assert.equal(claimed.progress, '已达成');
assert.equal(claimed.rewards, '+100 积分');
const completable = logic.growthTaskView({ task_code: 't2', status: 'accepted', current: 3, target: 3, reward_credit: 20, reward_energy: 5 });
assert.equal(completable.badge.text, '待领奖', '进度达成即视为待领奖');
assert.equal(completable.badge.tone, 'success');
assert.equal(completable.progress, '已达成');
assert.equal(completable.rewards, '+20 积分 · +5 能量');
assert.equal(completable.name, 't2', '没有标题时退回任务代码');
const inProgress = logic.growthTaskView({ name: 'x', task_code: 't3', status: 'in_progress', current: 1, target: 4 });
assert.equal(inProgress.badge.text, '进行中');
assert.equal(inProgress.progress, '1 / 4');
assert.equal(inProgress.rewards, '—');
assert.equal(logic.growthTaskView({ task_code: 't4', status: 'not_accepted' }).badge.text, '未接取');
const oddStatus = logic.growthTaskView({ task_code: 't5', status: 'unforgeable_x' });
assert.equal(oddStatus.badge.text, 'unforgeable_x', '没有映射过的状态原样显示');
assert.equal(logic.growthTaskView({}).name, '未命名任务');
const manualTask = logic.growthTaskView({ task_code: 't6', manual: true, manual_reason: '需真实操作' });
assert.equal(manualTask.mode.text, '需手动操作');
assert.equal(manualTask.mode.hint, '需真实操作');
assert.equal(logic.growthTaskView({ task_code: 't7' }).mode.text, '可自动推进');
assert.equal(logic.growthTaskView({ task_code: 't8' }).mode.hint, '');

// ---- 猫猫旅行：状态文案与归来时刻
const travelNow = new Date(2026, 9, 3, 12, 0, 0).getTime();
assert.equal(logic.travelEtaText({ arrive_at: Math.floor(travelNow / 1000) + 600, server_now: Math.floor(travelNow / 1000) }, travelNow), '12:10');
assert.equal(logic.travelEtaText({ arrive_at: 0, server_now: 0 }, travelNow), '');
assert.equal(logic.travelEtaText({}, travelNow), '');

const arrived = logic.travelView({ state: 'arrived', reward_credit: 30 }, travelNow);
assert.equal(arrived.text, '旅行归来，可以领奖');
assert.equal(arrived.tone, 'success');
assert.match(arrived.desc, /\+?30 积分/);
assert.match(logic.travelView({ state: 'arrived' }, travelNow).desc, /点「猫猫旅行」领取奖励/);

const traveling = logic.travelView({ state: 'traveling', arrive_at: Math.floor(travelNow / 1000) + 600, server_now: Math.floor(travelNow / 1000) }, travelNow);
assert.equal(traveling.text, '旅行中');
assert.equal(traveling.desc, '预计 12:10 归来');
assert.equal(logic.travelView({ state: 'traveling' }, travelNow).desc, '预计稍后归来');
assert.equal(logic.travelView({ state: 'idle' }, travelNow).text, '在家');
assert.equal(logic.travelView({ state: 'idle', daily_limit_reached: true }, travelNow).text, '今天已经旅行过');
assert.equal(logic.travelView({}, travelNow).text, '状态未知');
assert.equal(logic.travelView(null, travelNow).desc, '没有读取到猫猫旅行的状态');

// ---- 账号下拉：第一项是批量，失效值回到批量
const options = logic.growthAccountOptions([{ uid: 'abcdef1234', nickname: '国内一号' }], 'all');
assert.equal(options.selected, 'all');
assert.equal(options.options[0].label, '全部国内账号 (批量)');
assert.equal(options.options[1].label, '国内一号 abcdef');
assert.deepEqual(logic.growthAccountOptions([], 'missing').selected, 'all');
assert.deepEqual(logic.growthAccountOptions(null, '').options.length, 1);

// 账号选择器：「全部国内账号」留空，选中单个账号时返回它的 uid
assert.equal(logic.checkinSelection('all'), undefined);
assert.equal(logic.checkinSelection(''), undefined);
assert.equal(logic.checkinSelection('uid-1'), 'uid-1');

// ---- 积分变动记录
const rows = logic.creditEventRows([
  { at: 1770000000, uid: 'u1', nickname: '甲', realm: 'intl', before: 10.5, after: 50.25, delta: 39.75 },
  { at: 1770000100, uid: 'u2', nickname: '', realm: 'intl', before: 10, after: 5, delta: -5 },
]);
assert.equal(rows.length, 2);
assert.equal(rows[0].account, '甲');
assert.equal(rows[0].before, '10.50');
assert.equal(rows[0].after, '50.25');
assert.equal(rows[0].deltaText, '+39.75');
assert.equal(rows[0].tone, 'success');
assert.equal(rows[1].deltaText, '−5.00', '减少的记录用减号，默认不会出现');
assert.equal(rows[1].tone, 'warning');
assert.deepEqual(logic.creditEventRows(null), []);
assert.equal(logic.creditEventRows([{}])[0].account, '账号');

assert.match(logic.creditEventsEmptyText('intl'), /国际版/);
assert.match(logic.creditEventsEmptyText('cn'), /国内版/);
assert.match(logic.creditEventsEmptyText('intl'), /第一次读取只建立基线/);
assert.match(logic.growthEmptyText('intl'), /只对国内版账号开放/);
assert.match(logic.growthEmptyText('cn'), /国内版账号/);

console.log('任务页纯逻辑断言通过');
