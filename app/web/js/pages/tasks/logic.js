// 任务页的纯逻辑：调度器文案、成长任务状态、猫猫旅行状态、积分变动记录；只依赖 core/format.js 与账号页的 logic 模块，可以在 Node 里直接测试。

import { fmtCredit, fmtDateTime, fmtNumber, realmLabel } from '../../core/format.js';

/** 调度器状态整理成页面上要显示的四段文字。 */
export function schedulerView(status, realm) {
  const s = status || {};
  const enabled = !!s.enabled;
  const mode = realm === 'cn'
    ? (s.mode_cn || s.mode || '整点排程')
    : (s.mode_intl || s.mode || '每日 22:00 集中巡检');
  return {
    enabled,
    stateText: enabled ? '运行中 · ' + mode : '已暂停',
    timesText: '上次巡检 ' + (s.last_run_time || '尚未运行') + ' · 下次巡检 ' + (s.next_run_time || '待调度'),
    toggleText: enabled ? '暂停调度' : '恢复调度',
    triggerText: realm === 'intl' ? '立即保活刷新' : '立即巡检保活',
    logs: Array.isArray(s.logs) ? s.logs : [],
  };
}

/** 成长任务的进度、奖励与状态徽章。 */
export function growthTaskView(task) {
  const status = String((task && task.status) || '');
  const current = Number((task && task.current) || 0);
  const target = Number((task && task.target) || 0);
  let badge = { text: status || '未知', tone: 'muted' };
  if (status === 'claimed') badge = { text: '已领取', tone: 'muted' };
  else if (status === 'completed' || (target > 0 && current >= target)) badge = { text: '待领奖', tone: 'success' };
  else if (status === 'accepted' || status === 'in_progress') badge = { text: '进行中', tone: 'info' };
  else if (status === 'not_accepted') badge = { text: '未接取', tone: 'muted' };
  const done = status === 'claimed' || status === 'completed' || (target > 0 && current >= target);
  const manual = !!(task && task.manual);
  const mode = manual
    ? { text: '需手动操作', tone: 'warning', hint: (task && task.manual_reason) || '' }
    : { text: '可自动推进', tone: 'success', hint: '' };
  const rewards = [
    task && task.reward_credit ? '+' + fmtNumber(task.reward_credit) + ' 积分' : '',
    task && task.reward_energy ? '+' + fmtNumber(task.reward_energy) + ' 能量' : '',
  ].filter(Boolean).join(' · ');
  return {
    name: (task && task.name) || (task && task.task_code) || '未命名任务',
    code: (task && task.task_code) || '',
    description: (task && task.description) || '—',
    progress: done ? '已达成' : current + ' / ' + target,
    rewards: rewards || '—',
    badge,
    mode,
  };
}

/** 猫猫旅行的归来时刻。剩余时间取同一个响应里的两个字段之差，不依赖本机与服务器的时钟一致。 */
export function travelEtaText(travel, now = Date.now()) {
  const remain = Number((travel && travel.arrive_at) || 0) - Number((travel && travel.server_now) || 0);
  if (!(remain > 0)) return '';
  const d = new Date(now + remain * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return pad(d.getHours()) + ':' + pad(d.getMinutes());
}

export function travelView(travel, now = Date.now()) {
  const tr = travel || {};
  const state = String(tr.state || 'unknown');
  if (state === 'arrived') {
    const reward = Number(tr.reward_credit) || 0;
    return {
      text: '旅行归来，可以领奖',
      tone: 'success',
      desc: reward > 0 ? '奖励 ' + fmtNumber(reward) + ' 积分，下次出发前不领取就会作废' : '点「猫猫旅行」领取奖励',
    };
  }
  if (state === 'traveling') {
    const eta = travelEtaText(tr, now);
    return { text: '旅行中', tone: 'info', desc: eta ? '预计 ' + eta + ' 归来' : '预计稍后归来' };
  }
  if (tr.daily_limit_reached) {
    return { text: '今天已经旅行过', tone: 'muted', desc: '本地时间 0 点之后可以再次出发旅行' };
  }
  if (state === 'idle') {
    return { text: '在家', tone: 'muted', desc: '每天可以出发旅行一次，点「猫猫旅行」出发' };
  }
  return { text: '状态未知', tone: 'muted', desc: '没有读取到猫猫旅行的状态' };
}

export function growthAccountOptions(accounts, current) {
  const options = [{ value: 'all', label: '全部国内账号 (批量)' }];
  for (const account of accounts || []) {
    const uid = String(account.uid || '');
    options.push({ value: uid, label: (account.nickname || uid.slice(0, 8)) + ' ' + uid.slice(0, 6) });
  }
  const selected = options.some((option) => option.value === current) ? current : 'all';
  return { options, selected };
}

export function creditEventRows(events) {
  return (events || []).map((event) => {
    const delta = Number(event.delta) || 0;
    return {
      at: Number(event.at) || 0,
      time: fmtDateTime(event.at),
      account: event.nickname || String(event.uid || '').slice(0, 8) || '账号',
      realm: event.realm || '',
      before: fmtCredit(event.before),
      after: fmtCredit(event.after),
      deltaText: (delta >= 0 ? '+' : '−') + fmtCredit(Math.abs(delta)),
      tone: delta >= 0 ? 'success' : 'warning',
    };
  });
}

/** 积分变动记录为空时的说明，写清什么时候才会有记录。 */
export function creditEventsEmptyText(realm) {
  return '还没有读到' + realmLabel(realm) + '的积分变动记录。每次读取余额成功且余额比上一次增加时才会记一条，第一次读取只建立基线；'
    + 'Buddy 加油站、每日活跃打卡与猫猫旅行的奖励到账之后就会出现在这里。';
}

export function growthEmptyText(realm) {
  if (realm !== 'cn') return '成长任务只对国内版账号开放，切换到国内版之后可以在这里批量执行任务并领取积分。';
  return '当前没有读到成长任务。请确认至少有一个可用的国内版账号，然后点右上角的「刷新」重试。';
}

/** 调度器日志行的展示上限，取状态接口返回的最后若干条。 */
export function schedulerLogLines(status, limit = 20) {
  const logs = Array.isArray(status && status.logs) ? status.logs : [];
  return logs.slice(-limit);
}
